"""Both graph backends must behave identically where Brain depends on them.

`brain.py` claims the graph dependency is swappable. A claim like that is worth
nothing unless something exercises the other side of the swap, so these tests run the
same assertions against Semantica and against the dependency-free `SimpleGraph`.

The whole suite also runs green under `WALNUT_GRAPH=simple`, which is the stronger
statement — this file exists to make the equivalence explicit and to fail loudly if
the two ever diverge on something Brain relies on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from walnut.brain import Brain
from walnut.contract import Evidence, SourcePointer, content_hash
from walnut.graphstore import SimpleGraph, build_backend

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def ev(app: str, ident: str, text: str) -> Evidence:
    return Evidence(
        id=ident,
        pointer=SourcePointer(
            app=app, resource_uri=f"https://{app}.example/{ident}",
            locator={"id": ident}, content_hash=content_hash({"i": ident, "t": text}),
        ),
        text=text, occurred_at=NOW,
    )


def semantica_backend():
    try:
        from semantica.context import ContextGraph

        return ContextGraph()
    except Exception:  # noqa: BLE001
        pytest.skip("semantica not installed")


@pytest.fixture(params=["simple", "semantica"])
def brain(request) -> Brain:
    backend = SimpleGraph() if request.param == "simple" else semantica_backend()
    return Brain(backend=backend)


# -- everything Brain actually needs from a graph ---------------------------


def test_remembers_and_retrieves_a_cited_fact(brain):
    fact = brain.remember(ev("notion", "spec", "Dosing Engine v2 — Status: Shipped"))
    got = brain.get(fact.node_id)
    assert got is not None
    assert got.pointer.content_hash == fact.pointer.content_hash


def test_links_facts_and_walks_the_edge(brain):
    a = brain.remember(ev("notion", "spec", "Status: Shipped"))
    b = brain.remember(ev("github", "pr-288", "PR open, 0 approvals"))
    brain.relate(a.node_id, b.node_id, "contradicted_by")
    assert any(f.app == "github" for _, f in brain.neighbours(a.node_id))


def test_search_finds_an_ingested_fact(brain):
    brain.remember(ev("slack", "m1", "paediatric doses still calculating wrong"))
    assert brain.search("paediatric"), "search returned nothing for an indexed term"


def test_retraction_leaves_history_intact(brain):
    fact = brain.remember(ev("notion", "spec", "Status: Shipped"))
    brain.supersede(fact.node_id, reason="contradicted by PR #288")
    assert brain.get(fact.node_id) is not None, "retraction destroyed the record"


def test_state_at_answers_a_point_in_time_query(brain):
    brain.remember(ev("notion", "spec", "Status: Shipped"))
    assert "nodes" in brain.state_at(NOW + timedelta(days=1))


def test_decisions_are_recorded_and_summarised(brain):
    a = brain.remember(ev("notion", "spec", "Shipped"))
    b = brain.remember(ev("github", "pr-288", "open"))
    did = brain.record_decision(
        category="contradiction", scenario="is it shipped?",
        reasoning="spec says shipped; PR is open", outcome="flag",
        confidence=0.9, entities=[a.node_id, b.node_id],
    )
    assert did
    assert brain.decision_summary()["total_decisions"] == 1


def test_stats_report_the_apps_and_the_backend(brain):
    for app in ("slack", "linear", "github", "notion", "email"):
        brain.remember(ev(app, f"{app}-1", "x"))
    stats = brain.stats()
    assert stats["apps"] == ["email", "github", "linear", "notion", "slack"]
    assert stats["facts_indexed"] == 5


# -- selection behaviour ----------------------------------------------------


def test_the_fallback_is_selected_when_forced(monkeypatch):
    monkeypatch.setenv("WALNUT_GRAPH", "simple")
    assert isinstance(build_backend(), SimpleGraph)


def test_a_broken_graph_dependency_degrades_rather_than_stopping(monkeypatch):
    """An import failure at 3am must cost provenance features, not the demo."""
    import builtins

    real_import = builtins.__import__

    def explode(name, *args, **kwargs):
        if name.startswith("semantica"):
            raise ImportError("simulated: semantica is unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.delenv("WALNUT_GRAPH", raising=False)
    monkeypatch.setattr(builtins, "__import__", explode)
    assert isinstance(build_backend(), SimpleGraph)


def test_the_whole_product_works_on_the_fallback():
    """End to end on SimpleGraph: ingest, detect, act, refuse."""
    from walnut.actions.executor import ActionExecutor
    from walnut.actions.governance import QueueGate, Refusal
    from walnut.adapters.fixture import load_all_fixtures
    from walnut.agent import WalnutAgent
    from walnut.contract import Action
    from walnut.contradiction import detect_contradictions

    adapters = load_all_fixtures()
    b = Brain(backend=SimpleGraph())
    executor = ActionExecutor(adapters, b, QueueGate(), verify_freshness=False)
    agent = WalnutAgent(adapters, b, executor)

    assert agent.ingest(limit=200) > 50
    assert detect_contradictions(b), "no contradiction found on the fallback backend"

    poisoned = [f for f in b.by_app("notion")
                if "ignore previous instructions" in f.text.lower()]
    assert poisoned
    result = executor.execute(Action(
        app="linear", operation="set_state", target={"id": "x"},
        payload={"state": "Done"}, justified_by=(poisoned[0].node_id,),
        rationale="a document asked",
    ))
    assert isinstance(result, Refusal)
    assert result.reason.value == "tainted_instruction"
