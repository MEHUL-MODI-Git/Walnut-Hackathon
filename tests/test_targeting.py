"""The planner and the adapters must agree on what a target looks like.

They did not, and nothing noticed. Every plan emitted `{"id": …}` while the live
adapters read `channel`/`ts`, `owner`/`repo`/`number`, `page_id`, `issue_id`,
`folder`/`message_id`. Against fixture data — which does read `id` — it worked
perfectly. The first live connection would have raised `KeyError` on the second step of
a five-app plan, after the first step had already written to a real system.

That class of defect is invisible to every test that runs offline, so this file exists
to hold both sides of the contract to one declared table: what `REQUIRED_TARGET_KEYS`
says an operation needs, what the planner produces, and what the adapter source
actually reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from walnut.brain import Brain, Fact
from walnut.contract import SourcePointer
from walnut.targeting import REQUIRED_TARGET_KEYS, missing_context, target_for

ADAPTER_DIR = Path(__file__).resolve().parents[1] / "walnut" / "adapters"

# Live locator shapes, copied from what each adapter actually stores.
LIVE_LOCATORS = {
    "slack": {"channel": "C123", "ts": "1699.01"},
    "linear": {"type": "issue", "id": "uuid-1", "identifier": "MED-412"},
    "github": {"owner": "meridian", "repo": "platform", "number": 288, "kind": "pull"},
    "notion": {"id": "page-abc"},
    "email": {"folder": "INBOX", "message_id": "<m1@example>", "uid": 7},
}


def fact(app: str, locator: dict) -> Fact:
    return Fact(
        node_id=f"{app}:x", text="evidence", app=app,
        pointer=SourcePointer(app=app, resource_uri="https://x/1",
                              locator=locator, content_hash="a" * 64),
    )


# -- the drift guard --------------------------------------------------------


def _keys_read_by(app: str) -> set[str]:
    """Every `action.target[...]` key the adapter source actually reads."""
    source = (ADAPTER_DIR / f"{app}.py").read_text()
    return set(re.findall(r'action\.target(?:\.get)?[\[(]"([a-z_]+)"', source))


@pytest.mark.parametrize("app", sorted(REQUIRED_TARGET_KEYS))
def test_the_table_covers_every_key_the_adapter_reads(app):
    """If an adapter starts reading a new target key, this fails until the table and
    the planner learn about it — which is the whole point of declaring it."""
    declared = {k for keys in REQUIRED_TARGET_KEYS[app].values() for k in keys}
    # Optional extras the planner may supply but no operation strictly requires.
    optional = {"ts", "identifier", "thread_ts", "user", "sha", "related_issue_id",
                "database_id", "message_id", "folder"}
    read = _keys_read_by(app)
    unknown = read - declared - optional
    assert not unknown, (
        f"{app} adapter reads target keys nothing declares: {sorted(unknown)}. "
        "Add them to REQUIRED_TARGET_KEYS so the planner produces them."
    )


@pytest.mark.parametrize("app", sorted(LIVE_LOCATORS))
def test_a_live_locator_produces_every_required_key(app):
    """The defect this file exists for: a real record must yield an aimable target."""
    evidence = [fact(app, LIVE_LOCATORS[app])]
    for operation, required in REQUIRED_TARGET_KEYS[app].items():
        if not required:
            continue
        target = target_for(app, operation, evidence,
                            context={"team_id": "T1", "database_id": "D1",
                                     "repo": "meridian/platform"})
        if target is None:
            continue  # needs context this test deliberately does not supply
        missing = [k for k in required if target.get(k) in (None, "")]
        assert not missing, (
            f"{app}.{operation} target {target} is missing {missing} — this would "
            "raise KeyError at the API boundary on a live connection"
        )


# -- specific translations --------------------------------------------------


def test_slack_reply_threads_under_the_message_it_cites():
    target = target_for("slack", "post_reply", [fact("slack", LIVE_LOCATORS["slack"])])
    assert target["channel"] == "C123"
    assert target["thread_ts"] == "1699.01", "a reply must thread under its evidence"


def test_a_linear_issue_id_arrives_as_issue_id_not_id():
    target = target_for("linear", "comment", [fact("linear", LIVE_LOCATORS["linear"])])
    assert target["issue_id"] == "uuid-1"


def test_a_linear_comment_targets_its_parent_issue():
    locator = {"type": "comment", "id": "c-9", "issue_id": "uuid-1"}
    assert target_for("linear", "comment", [fact("linear", locator)])["issue_id"] == "uuid-1"


def test_a_notion_page_id_arrives_as_page_id_not_id():
    target = target_for("notion", "set_property", [fact("notion", LIVE_LOCATORS["notion"])])
    assert target == {"page_id": "page-abc"}


# -- fallbacks and refusals -------------------------------------------------


def test_a_fixture_locator_still_works():
    """Fixtures and custom sources address by plain id. Requiring live coordinates
    silently deleted two steps from the demo plan the first time this ran."""
    target = target_for("github", "comment", [fact("github", {"id": "pr-288"})])
    assert target == {"id": "pr-288"}


def test_evidence_from_another_app_cannot_aim_an_action():
    """Acting on the wrong system is worse than not acting."""
    assert target_for("github", "comment", [fact("slack", LIVE_LOCATORS["slack"])]) is None


def test_creating_a_linear_issue_without_a_team_id_is_refused_not_guessed():
    assert target_for("linear", "create_issue", [], context={}) is None
    assert target_for("linear", "create_issue", [], context={"team_id": "T1"}) == {
        "team_id": "T1"
    }


def test_missing_context_explains_what_a_human_must_supply():
    assert "LINEAR_TEAM_ID" in (missing_context("linear", "create_issue", {}) or "")
    assert "NOTION_DATABASE_ID" in (missing_context("notion", "create_page", {}) or "")
    assert "GITHUB_REPO" in (missing_context("github", "create_issue", {}) or "")
    assert missing_context("linear", "create_issue", {"team_id": "T1"}) is None


# -- the plan still works end to end ----------------------------------------


def test_the_five_app_plan_survives_target_translation():
    from walnut.adapters.fixture import load_all_fixtures
    from walnut.contradiction import detect_contradictions
    from walnut.playbook import build_plan

    brain = Brain()
    for adapter in load_all_fixtures().values():
        for record in adapter.fetch(limit=200):
            brain.remember(record)

    plan = build_plan(detect_contradictions(brain)[0], brain)
    assert len(plan) == 5, f"the plan lost steps: {[s['app'] for s in plan]}"
    assert all(step["target"] for step in plan), "a step was proposed with no target"
