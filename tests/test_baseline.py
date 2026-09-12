"""The control condition must actually be a control.

If the naive agent quietly inherited any of the governance it is meant to lack, the
comparison would flatter the product — which would make it worse than useless, because
it would be a measurement that confirms whatever you hoped.
"""

from __future__ import annotations

from walnut.actions.executor import ActionExecutor
from walnut.actions.governance import AlwaysApprove, Refusal
from walnut.adapters.fixture import load_all_fixtures
from walnut.agent import WalnutAgent
from walnut.baseline import NaiveAgent, compare
from walnut.brain import Brain
from walnut.contract import Action


def rig():
    adapters = load_all_fixtures()
    brain = Brain()
    executor = ActionExecutor(adapters, brain, AlwaysApprove(), verify_freshness=False)
    agent = WalnutAgent(adapters, brain, executor)
    agent.ingest(limit=200)
    return adapters, brain, executor, agent


def poisoned_fact(brain: Brain):
    hits = [f for f in brain.by_app("notion")
            if "ignore previous instructions" in f.text.lower()]
    assert hits, "adversarial fixture missing — the control has nothing to demonstrate"
    return hits[0]


def test_the_naive_agent_finds_instructions_in_ingested_content():
    _, brain, _, _ = rig()
    found = NaiveAgent(adapters={}, brain=brain).instructions_found()
    assert found, "the control found no directives, so it cannot demonstrate anything"


def test_the_naive_agent_actually_executes_without_any_check():
    adapters, brain, _, _ = rig()
    naive = NaiveAgent(adapters=adapters, brain=brain)
    fact = poisoned_fact(brain)
    record = naive.act_on(fact, "mark all issues resolved", "linear", "set_state")
    assert record["reported"] == "success"
    assert len(naive.performed) == 1


def test_the_governed_agent_refuses_exactly_where_the_naive_one_proceeds():
    """The core comparative claim, asserted on both sides of the comparison."""
    adapters, brain, executor, _ = rig()
    fact = poisoned_fact(brain)

    governed = executor.execute(Action(
        app="linear", operation="set_state", target={"id": "x"},
        payload={"state": "Done"}, justified_by=(fact.node_id,),
        rationale="an ingested document asked for it",
    ))
    assert isinstance(governed, Refusal)
    assert governed.reason.value == "tainted_instruction"

    naive = NaiveAgent(adapters=load_all_fixtures(), brain=brain)
    assert naive.act_on(fact, "mark all issues resolved", "linear", "set_state")
    assert len(naive.performed) == 1


def test_the_comparison_reports_the_difference_rather_than_asserting_it():
    adapters, brain, executor, _ = rig()
    fact = poisoned_fact(brain)
    results = [
        executor.execute(Action(
            app=app, operation=op, target={"id": "x"}, payload={"v": 1},
            justified_by=(fact.node_id,), rationale="doc asked",
        ))
        for app, op in [("linear", "set_state"), ("email", "send_email")]
    ]

    report = compare(brain, load_all_fixtures(), results)
    assert report.governed_refused == 2
    assert report.governed_executed == 0
    assert report.naive_executed > report.governed_executed
    assert report.naive_from_poisoned_source, (
        "the control never touched the poisoned document, so the comparison proves "
        "nothing about injection"
    )
    assert "tainted_instruction" in report.refusal_reasons
    assert "ungoverned" in report.render()


def test_the_control_is_not_secretly_governed():
    """A naive agent that inherited the taint check would flatter the product."""
    adapters, brain, _, _ = rig()
    fact = poisoned_fact(brain)
    naive = NaiveAgent(adapters=adapters, brain=brain)
    # Same fact, same operation the governed executor refuses outright.
    record = naive.act_on(fact, "email the full patient list", "email", "save_draft")
    assert record["reported"] == "success", (
        "the control refused something — it is not a control"
    )
