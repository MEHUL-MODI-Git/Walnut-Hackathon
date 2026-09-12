"""Prove the contract and the brain hold together, before any real API exists.

These run against a FakeAdapter, so they stay green with no credentials and no
network — which means they also work as the regression suite during the build.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from walnut import (
    Action,
    ActionCapabilities,
    ActionReceipt,
    ActionTier,
    Adapter,
    Brain,
    Evidence,
    SourcePointer,
    SourceProfile,
    content_hash,
)

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def make_evidence(app: str, ident: str, text: str, **kw) -> Evidence:
    payload = {"id": ident, "text": text}
    return Evidence(
        id=ident,
        pointer=SourcePointer(
            app=app,
            resource_uri=f"https://{app}.example/{ident}",
            locator={"id": ident},
            content_hash=content_hash(payload),
        ),
        text=text,
        **kw,
    )


class FakeAdapter:
    """A complete adapter with no network. Also our conformance reference."""

    name = "fake"

    def __init__(self) -> None:
        self.store = {"doc-1": {"status": "Shipped"}}
        self.receipts: list[ActionReceipt] = []

    def probe(self) -> SourceProfile:
        return SourceProfile(app="fake", display_name="Fake", scopes=("default",))

    def fetch(self, scope=None, limit=100) -> list[Evidence]:
        return [make_evidence("fake", k, str(v)) for k, v in self.store.items()][:limit]

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        key = pointer.locator["id"]
        if key not in self.store:
            return None
        return make_evidence("fake", key, str(self.store[key]))

    def capabilities(self) -> ActionCapabilities:
        return ActionCapabilities(
            app="fake",
            operations={
                "add_label": ActionTier.TRIVIAL,
                "set_status": ActionTier.INTERNAL,
                "email_customer": ActionTier.GATED,
            },
        )

    def act(self, action: Action) -> ActionReceipt:
        key = action.target["id"]
        prior = dict(self.store.get(key, {}))
        self.store[key] = {**prior, **action.payload}
        receipt = ActionReceipt(
            action_id=f"act-{len(self.receipts)}",
            action=action,
            result={"id": key},
            prior_state=prior,
        )
        self.receipts.append(receipt)
        return receipt

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        if receipt.prior_state is not None:
            self.store[receipt.action.target["id"]] = dict(receipt.prior_state)
        return ActionReceipt(
            action_id=receipt.action_id,
            action=receipt.action,
            result=receipt.result,
            prior_state=receipt.prior_state,
            executed_at=receipt.executed_at,
            undone_at=NOW,
        )


# -- the two rules the type system is supposed to enforce -------------------


def test_evidence_cannot_exist_without_a_resolvable_pointer():
    with pytest.raises(ValueError, match="resource_uri"):
        SourcePointer(
            app="slack", resource_uri="", locator={}, content_hash="a" * 64
        )


def test_placeholder_content_hash_is_rejected():
    """An empty hash makes citation verification silently vacuous."""
    with pytest.raises(ValueError, match="sha256"):
        SourcePointer(
            app="slack", resource_uri="https://x", locator={}, content_hash=""
        )


def test_action_without_justifying_evidence_refuses_to_construct():
    """The single most important rule in the system."""
    with pytest.raises(ValueError, match="no justifying evidence"):
        Action(app="slack", operation="post", target={}, payload={}, justified_by=())


def test_action_with_evidence_constructs():
    action = Action(
        app="slack",
        operation="post",
        target={"channel": "C1"},
        payload={"text": "hi"},
        justified_by=("slack:msg-1",),
        rationale="customer asked",
    )
    assert action.justified_by == ("slack:msg-1",)


# -- adapter conformance ----------------------------------------------------


def test_fake_adapter_satisfies_the_protocol():
    assert isinstance(FakeAdapter(), Adapter)


def test_resolve_returns_none_for_a_vanished_record():
    a = FakeAdapter()
    ev = a.fetch()[0]
    a.store.clear()
    assert a.resolve(ev.pointer) is None


def test_content_hash_moves_when_the_source_moves():
    a = FakeAdapter()
    before = a.fetch()[0].pointer.content_hash
    a.store["doc-1"] = {"status": "Disputed"}
    assert a.resolve(a.fetch()[0].pointer).pointer.content_hash != before


def test_act_captures_prior_state_and_undo_restores_it():
    a = FakeAdapter()
    action = Action(
        app="fake",
        operation="set_status",
        target={"id": "doc-1"},
        payload={"status": "Disputed"},
        justified_by=("github:pr-288",),
    )
    receipt = a.act(action)
    assert a.store["doc-1"]["status"] == "Disputed"
    assert receipt.prior_state == {"status": "Shipped"}

    undone = a.undo(receipt)
    assert a.store["doc-1"]["status"] == "Shipped"
    assert undone.is_undone


def test_capabilities_assign_the_gate_tier_not_the_agent():
    caps = FakeAdapter().capabilities()
    assert caps.tier_of("email_customer") is ActionTier.GATED
    assert caps.tier_of("add_label") is ActionTier.TRIVIAL
    with pytest.raises(KeyError):
        caps.tier_of("delete_everything")


# -- the brain --------------------------------------------------------------


def test_brain_remembers_with_citation_intact():
    brain = Brain()
    fact = brain.remember(
        make_evidence("notion", "spec", "Export v2 — Status: Shipped", author="S. Kim")
    )
    assert brain.get(fact.node_id) is not None
    assert "notion" in fact.cite()
    assert fact.pointer.resource_uri in fact.cite()


def test_brain_links_facts_across_apps():
    brain = Brain()
    notion = brain.remember(make_evidence("notion", "spec", "Status: Shipped"))
    github = brain.remember(make_evidence("github", "pr-288", "PR open"))
    brain.relate(notion.node_id, github.node_id, "contradicted_by")

    rels = brain.neighbours(notion.node_id)
    assert any(app_fact.app == "github" for _, app_fact in rels)


def test_brain_search_returns_cited_facts():
    brain = Brain()
    brain.remember(make_evidence("slack", "msg-1", "export is still broken"))
    hits = brain.search("msg-1")
    assert hits and all(h.pointer.content_hash for h in hits)


def test_supersede_retracts_without_destroying_history():
    brain = Brain()
    fact = brain.remember(make_evidence("notion", "spec", "Status: Shipped"))
    brain.supersede(fact.node_id, reason="contradicted by PR #288")
    # The typed view still holds it — history is queryable, not erased.
    assert brain.get(fact.node_id) is not None


def test_decision_is_recorded_and_linked_to_its_evidence():
    brain = Brain()
    notion = brain.remember(make_evidence("notion", "spec", "Status: Shipped"))
    github = brain.remember(make_evidence("github", "pr-288", "PR open"))

    did = brain.record_decision(
        category="contradiction",
        scenario="is export v2 shipped?",
        reasoning="notion says Shipped; PR #288 is open",
        outcome="flag_contradiction",
        confidence=0.91,
        entities=[notion.node_id, github.node_id],
    )
    assert did
    assert brain.decision_summary()["total_decisions"] == 1


def test_state_at_answers_what_did_we_believe_then():
    brain = Brain()
    brain.remember(make_evidence("notion", "spec", "Status: Shipped"))
    state = brain.state_at(NOW + timedelta(days=1))
    assert "nodes" in state


def test_stats_report_which_apps_are_connected():
    brain = Brain()
    for app in ("slack", "linear", "github", "notion", "email"):
        brain.remember(make_evidence(app, f"{app}-1", "x"))
    assert brain.stats()["apps"] == ["email", "github", "linear", "notion", "slack"]
