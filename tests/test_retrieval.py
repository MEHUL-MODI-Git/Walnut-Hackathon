"""Retrieval is the product, so these are the assertions that matter most.

Each one corresponds to a defect found by running the thing rather than by a test
failing — which is why they exist as tests now.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from walnut.adapters.fixture import load_all_fixtures, load_identities
from walnut.brain import Brain
from walnut.identity import resolve_identities
from walnut.retrieval import assemble, link_by_subject, normalise


@pytest.fixture(scope="module")
def rig():
    brain = Brain()
    for adapter in load_all_fixtures().values():
        for record in adapter.fetch(limit=300):
            brain.remember(record)
    link_by_subject(brain)
    return brain, resolve_identities(load_identities())


# -- the three defects ------------------------------------------------------


def test_authors_are_people_not_internal_ids(rig):
    """71 of 88 facts rendered an author of "p01". A person's name is the single most
    likely thing anyone types into a company brain."""
    brain, _ = rig
    authors = {f.author for f in brain._facts.values() if f.author}  # noqa: SLF001
    bare_ids = {a for a in authors if len(a) == 3 and a[0] == "p" and a[1:].isdigit()}
    assert not bare_ids, f"unresolved person ids still rendering as authors: {bare_ids}"


def test_punctuation_does_not_lose_a_subject(rig):
    """Exact-substring matching lost a subject to an apostrophe: a customer appearing
    fifteen times across three systems returned zero records."""
    brain, ids = rig
    assert normalise("St. Anne's Children's Hospital") == "st anne s children s hospital"
    assert normalise("co-amoxiclav") == "co amoxiclav"
    # The same query with and without punctuation must reach the same records.
    with_punct = assemble(brain, "co-amoxiclav", identities=ids)
    without = assemble(brain, "co amoxiclav", identities=ids)
    assert with_punct.total == without.total
    assert with_punct.total > 0


def test_the_graph_has_edges(rig):
    """Facts were ingested and never linked — a bag of records, not a graph."""
    brain, _ = rig
    assert brain.stats().get("edge_count", 0) > 0


# -- identity-aware retrieval -----------------------------------------------


def test_a_person_is_found_under_every_name_they_are_filed_under(rig):
    """The obvious question — "what do we know about this colleague?" — returned
    nothing at all before identity resolution was wired into retrieval."""
    brain, ids = rig
    found = assemble(brain, "Priya Patel", identities=ids)
    assert len(found.aliases) >= 3, (
        "the alias list is what proves the scattering was undone"
    )
    assert found.total > 0 and len(found.apps_with_hits) >= 2


def test_the_patient_assembles_from_across_the_estate(rig):
    """The product's whole claim, on the corpus it ships with."""
    brain, ids = rig
    found = assemble(brain, "Ankusha Rao", identities=ids)
    assert found.total >= 5, f"only {found.total} records for the demo subject"
    assert len(found.apps_with_hits) >= 3, (
        f"only {found.apps_with_hits} — a patient should span the estate"
    )


def test_related_subjects_make_it_traversable(rig):
    brain, ids = rig
    found = assemble(brain, "Ankusha Rao", identities=ids)
    assert found.related_subjects or found.total > 0


# -- coverage: the honesty of the whole screen ------------------------------


def test_every_source_is_accounted_for_on_every_result(rig):
    """A search that quietly read four of six sources looks identical to one that read
    all six. Every source must appear, including the ones that held nothing."""
    brain, ids = rig
    found = assemble(brain, "Ankusha Rao", identities=ids)
    assert len(found.coverage) == len(brain.stats()["apps"])
    assert all(c.state in {"found", "nothing", "not searched"} for c in found.coverage)


def test_a_source_that_held_nothing_says_so_rather_than_disappearing(rig):
    brain, ids = rig
    found = assemble(brain, "zzz-nothing-matches-this", identities=ids)
    assert found.total == 0
    assert found.coverage, "a zero-hit result reported no coverage at all"
    assert all(c.state == "nothing" for c in found.coverage)


def test_link_by_subject_does_not_link_on_common_words(rig):
    """An edge between every record mentioning a common word is noise that makes
    traversal useless rather than informative."""
    brain, _ = rig
    edges = brain.stats().get("edge_count", 0)
    assert edges < len(brain._facts) ** 2 / 4, (  # noqa: SLF001
        f"{edges} edges is a cartesian blast, not a link structure"
    )


# -- the screen -------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    from walnut.connections import ConnectionManager
    from walnut.web import app as web

    web.state = web.State(connections=ConnectionManager(autoload_env=False))
    return TestClient(web.app)


def test_the_landing_screen_invites_a_question(client):
    """It opens as a question, not a dashboard — the product is asking and getting
    an answer."""
    body = client.get("/").text
    assert "What would you like to know" in body
    assert "Connected" in body, "the idle screen must state what is connected"


def test_an_answer_reads_as_a_briefing_with_its_receipts(client):
    """Organised by meaning, not by which app it came from — with every line's source
    one click away rather than printed alongside it."""
    body = client.get("/?q=Ankusha+Rao").text
    assert "On record" in body, "the answer is not organised by meaning"
    assert 'details class="cite"' in body, "citations are not reachable"
    assert "Where this came from" in body, "coverage is not shown"


def test_a_colleague_query_surfaces_the_names_they_are_filed_under(client):
    """Aliases belong to staff, not patients — a patient has one record, a colleague
    has a different handle in every system."""
    body = client.get("/?q=Priya+Patel").text
    assert "known as" in body.lower(), (
        "resolved identities are not surfaced — the clearest proof of unification"
    )


def test_a_zero_hit_query_proves_the_guarantee_rather_than_saying_no_results(client):
    body = client.get("/?q=zzznotathing").text
    assert "Every source was searched" in body
    assert "0 results" not in body


def test_an_unreachable_internal_system_is_reported_not_hidden(client, monkeypatch):
    """An internal system that cannot be reached must surface as a source that could
    not be searched — silence here is indistinguishable from a source that WAS
    searched and held nothing, which is the one confusion this product exists to end.

    Pointed at a dead port on purpose. The first version of this test asserted on the
    real dispensary being down, so it passed only while nobody happened to be running
    the service — and it started failing the moment the demo stack was up, which is
    exactly when the console most needs to be right.
    """
    from walnut.web import app as web

    monkeypatch.setattr(web, "state", web.State())
    monkeypatch.setenv("DISPENSARY_URL", "http://127.0.0.1:9")  # discard port: nothing listens
    monkeypatch.setattr("walnut.internal_systems.DISPENSARY_URL", "http://127.0.0.1:9")
    monkeypatch.setitem(
        __import__("walnut.internal_systems", fromlist=["DISPENSARY_SPEC"]).DISPENSARY_SPEC,
        "base_url", "http://127.0.0.1:9",
    )
    client.get("/")

    names = [n for n, _ in web.state.unsearchable()]
    assert "dispensary" in names, (
        "an unreachable internal system vanished instead of being reported"
    )
    assert "not searched" in client.get("/?q=Ankusha").text
