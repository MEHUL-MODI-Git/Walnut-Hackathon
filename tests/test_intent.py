"""Natural-language requests become proposed actions — or questions.

The parser is deliberately the weakest component in the system, so the tests that
matter most are not "does it understand this sentence" but "what happens when it does
not". A misread request must cost a refused action, never a wrong write.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from walnut.actions.executor import ActionExecutor
from walnut.actions.governance import AlwaysApprove, QueueGate, Refusal
from walnut.adapters.fixture import load_all_fixtures
from walnut.brain import Brain
from walnut.intent import parse_intent


@pytest.fixture(scope="module")
def rig():
    adapters = load_all_fixtures()
    brain = Brain()
    for adapter in adapters.values():
        for record in adapter.fetch(limit=200):
            brain.remember(record)
    return brain, adapters


# -- it understands ordinary requests ---------------------------------------


def test_a_two_part_request_becomes_two_actions_in_the_right_apps(rig):
    brain, adapters = rig
    intent = parse_intent(
        "file a Linear issue about MR-4417 and update the status of the notion page",
        brain, adapters,
    )
    routed = {(p.action.app, p.action.operation) for p in intent.proposed}
    assert ("linear", "create_issue") in routed
    assert ("notion", "set_property") in routed


def test_the_customer_reply_routes_to_email_not_slack(rig):
    """'reply to the customer' and 'reply in the thread' are different systems."""
    brain, adapters = rig
    intent = parse_intent("reply to the customer about the dosing bug", brain, adapters)
    assert [(p.action.app, p.action.operation) for p in intent.proposed] == [
        ("email", "send_email")
    ]


def test_a_page_update_routes_to_notion(rig):
    brain, adapters = rig
    intent = parse_intent(
        "update the status of the dosing engine spec in notion", brain, adapters
    )
    assert any(p.action.app == "notion" for p in intent.proposed)


# -- it asks rather than guesses --------------------------------------------


def test_an_ambiguous_app_becomes_a_question(rig):
    """When two connected apps can both perform an operation, guessing writes to the
    wrong system — so the parser asks.

    Tested against the FULL adapter set rather than the clinic's: the clinic runs no
    two apps that share an operation, which is a property of that deployment, not of
    the parser. The parser must still refuse to guess when a deployment does.
    """
    brain, _ = rig
    every_app = load_all_fixtures()  # includes github, which also takes a comment
    intent = parse_intent("comment on something", brain, every_app)
    assert intent.needs_clarification
    assert not intent.proposed
    assert any("could mean" in c for c in intent.clarifications)


def test_an_unrecognised_request_proposes_nothing(rig):
    brain, adapters = rig
    intent = parse_intent("make me a sandwich", brain, adapters)
    assert intent.proposed == []
    assert intent.clarifications


def test_a_request_about_something_the_brain_does_not_hold_proposes_nothing(rig):
    _, adapters = rig
    empty = Brain()
    intent = parse_intent("file a Linear issue about the quarterly audit", empty, adapters)
    assert intent.proposed == []
    assert any("no evidence" in c.lower() for c in intent.clarifications)


# -- it cannot escape the governance layer ----------------------------------


def test_every_proposed_action_carries_evidence(rig):
    brain, adapters = rig
    intent = parse_intent(
        "file a Linear issue about MED-412 and comment on the PR", brain, adapters
    )
    assert intent.proposed
    for proposal in intent.proposed:
        assert proposal.action.justified_by, "an action was proposed with no evidence"


def test_a_request_cannot_launder_injected_instructions_into_an_action(rig):
    """The load-bearing test. If a user can be socially engineered into typing what a
    poisoned document told them to, the proposal still has to die at the executor."""
    brain, adapters = rig
    poisoned = [f for f in brain.by_app("notion")
                if "ignore previous instructions" in f.text.lower()]
    assert poisoned

    intent = parse_intent(
        "file a Linear issue about the support macros page", brain, adapters,
        evidence_hint=poisoned,
    )
    assert intent.proposed, "nothing proposed, so this proves nothing about the executor"

    executor = ActionExecutor(adapters, brain, AlwaysApprove(), verify_freshness=False)
    results = [executor.execute(a) for a in intent.actions]
    assert all(isinstance(r, Refusal) for r in results)
    assert all(r.reason.value == "tainted_instruction" for r in results)


def test_a_requested_customer_email_still_stops_for_a_human(rig):
    """Asking for it out loud does not make it un-gated."""
    brain, adapters = rig
    gate = QueueGate()
    executor = ActionExecutor(adapters, brain, gate, verify_freshness=False)

    intent = parse_intent("reply to the customer about the dosing bug", brain, adapters)
    assert intent.proposed
    results = [executor.execute(a) for a in intent.actions]

    assert any(isinstance(r, Refusal) and r.reason.value == "gate_timeout"
               for r in results)
    assert gate.pending, "the requested email was not queued for approval"


def test_proposed_actions_aim_at_records_that_exist(rig):
    """Targets come from evidence locators, not from a guessed id — otherwise the
    action fails at the API boundary looking like a connector bug."""
    brain, adapters = rig
    intent = parse_intent("reply in the thread about MR-4417", brain, adapters)
    for proposal in intent.proposed:
        assert proposal.action.target, "an action was proposed with an empty target"


# -- the console ------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    from walnut.connections import ConnectionManager
    from walnut.web import app as web

    web.state = web.State(connections=ConnectionManager(autoload_env=False))
    return TestClient(web.app)


def test_typing_a_request_shows_what_it_would_do_before_doing_it(client):
    client.get("/")
    client.post("/request",
                data={"request": "file a Linear issue about MR-4417"},
                follow_redirects=True)
    body = client.get("/actions?tab=activity").text
    assert "Proposed" in body
    assert "linear.create_issue" in body

    from walnut.web import app as web

    assert web.state.agent.executor.ledger.summary()["executed"] == 0, (
        "proposing executed something — the review step is not a review"
    )


def test_executing_a_proposal_runs_it_through_the_normal_path(client):
    client.get("/")
    client.post("/request",
                data={"request": "file a Linear issue about MR-4417"},
                follow_redirects=True)
    client.post("/request/execute", follow_redirects=True)

    from walnut.web import app as web

    executed = web.state.agent.executor.ledger.live()
    assert executed, "nothing executed"
    assert all(r.action.justified_by for r in executed)


def test_an_unactionable_request_renders_the_question(client):
    """A request naming no operation any connected app declares becomes a question,
    listing what it CAN do — never a guess."""
    client.get("/")
    client.post("/request", data={"request": "make me a sandwich"},
                follow_redirects=True)
    body = client.get("/actions?tab=activity").text
    assert "Needs clarification" in body
