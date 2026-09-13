"""Connection management and the web console.

The security assertions here matter more than the rendering ones: a console that
echoes a pasted token back into the page has turned a credential into a screenshot
hazard, and this is a product people will demo over screen share.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from walnut.connections import APP_SPECS, ConnectionManager, ConnectionState
from walnut.contract import SourceProfile

REAL_TOKEN = "xoxb-9999-super-secret-do-not-render"


@pytest.fixture
def manager() -> ConnectionManager:
    # autoload_env off: a developer's real .env must not change test outcomes.
    return ConnectionManager(autoload_env=False)


# -- connection state -------------------------------------------------------


def test_every_app_starts_in_demo_and_is_immediately_usable(manager):
    """Every app the demo company runs is usable before anything is connected.

    Not every app Walnut *supports*: GitHub is a real, tested connector but a clinic
    does not use one, so it ships with no seeded data. It stays connectable.
    """
    from walnut.adapters.fixture import CLINIC_APPS

    assert len(manager.all()) == len(APP_SPECS)
    assert all(c.state is ConnectionState.DEMO for c in manager.all())
    adapters = manager.adapters()
    assert set(adapters) == set(CLINIC_APPS)
    # Demo mode is not a stub: each adapter returns real, hashed, cited evidence.
    for app, adapter in adapters.items():
        records = adapter.fetch(limit=3)
        assert records, f"{app} fixture returned nothing"
        assert all(len(r.pointer.content_hash) == 64 for r in records)


def test_missing_required_field_is_an_error_not_a_connection(manager):
    conn = manager.connect("slack", {})
    assert conn.state is ConnectionState.ERROR
    assert "Missing required field" in conn.error
    assert manager.adapters()["slack"].name == "slack"


def test_a_credential_that_cannot_read_anything_is_reported_as_an_error(manager, monkeypatch):
    """A token that parses but sees nothing is not a working connection."""

    class Blind:
        name = "slack"

        def probe(self):
            return SourceProfile(app="slack", display_name="Slack", scopes=())

    monkeypatch.setattr(ConnectionManager, "_build", staticmethod(lambda a, c: Blind()))
    conn = manager.connect("slack", {"token": REAL_TOKEN})
    assert conn.state is ConnectionState.ERROR
    assert "can see nothing" in conn.error


def test_a_rejected_credential_leaves_the_app_on_fixtures(manager, monkeypatch):
    def explode(app, creds):
        raise RuntimeError("invalid_auth")

    monkeypatch.setattr(ConnectionManager, "_build", staticmethod(explode))
    conn = manager.connect("linear", {"api_key": "lin_api_bad"})
    assert conn.state is ConnectionState.ERROR
    assert "invalid_auth" in conn.error
    # Still usable — a failed connection must not break the product.
    assert manager.adapters()["linear"].fetch(limit=1)


def test_a_working_credential_connects_and_swaps_the_adapter_in(manager, monkeypatch):
    class Live:
        name = "slack"

        def probe(self):
            return SourceProfile(app="slack", display_name="Slack",
                                 scopes=("#eng", "#support"), record_count_estimate=42)

    monkeypatch.setattr(ConnectionManager, "_build", staticmethod(lambda a, c: Live()))
    conn = manager.connect("slack", {"token": REAL_TOKEN})
    assert conn.state is ConnectionState.CONNECTED
    assert conn.profile.scopes == ("#eng", "#support")
    assert manager.adapters()["slack"] is not manager._fixtures["slack"]
    assert manager.summary()["connected"] == 1


def test_disconnect_returns_to_fixtures_and_forgets_the_credential(manager, monkeypatch):
    class Live:
        name = "slack"

        def probe(self):
            return SourceProfile(app="slack", display_name="S", scopes=("#eng",))

    monkeypatch.setattr(ConnectionManager, "_build", staticmethod(lambda a, c: Live()))
    manager.connect("slack", {"token": REAL_TOKEN})
    conn = manager.disconnect("slack")
    assert conn.state is ConnectionState.DEMO
    assert conn.credentials == {}
    assert manager.adapters()["slack"] is manager._fixtures["slack"]


def test_redacted_view_never_exposes_a_secret(manager, monkeypatch):
    class Live:
        name = "slack"

        def probe(self):
            return SourceProfile(app="slack", display_name="S", scopes=("#eng",))

    monkeypatch.setattr(ConnectionManager, "_build", staticmethod(lambda a, c: Live()))
    manager.connect("slack", {"token": REAL_TOKEN})
    assert REAL_TOKEN not in str(manager.get("slack").redacted())


def test_every_spec_documents_where_to_get_the_credential():
    """Half of connector support burden is not knowing which page to open."""
    for app, spec in APP_SPECS.items():
        assert spec.where, f"{app} does not say where to get its credential"
        assert spec.fields, f"{app} declares no fields"
        assert spec.blurb


# -- the web console --------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    from walnut.web import app as web

    web.state = web.State(connections=ConnectionManager(autoload_env=False))
    return TestClient(web.app)


@pytest.mark.parametrize("path", ["/", "/connections", "/investigate", "/approvals", "/evidence", "/audit"])
def test_every_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "Walnut" in r.text


def test_the_console_runs_with_no_credentials_at_all(client):
    """The whole product must be demonstrable before anything is connected.

    Asserted on behaviour rather than on the word "demo": that term was deliberately
    renamed to "Sample data", because telling a buyer a connector is in "demo" mode
    says the PRODUCT is a demo, when what is true is that the connector is
    unconfigured and still fully functional.
    """
    body = client.get("/").text
    assert "Walnut" in body
    # Sources are connected and holding records without a single credential.
    assert "records held" in body or "sources" in body.lower()
    assert client.get("/evidence").text.count("evidence") > 1


def test_a_pasted_token_is_never_echoed_back_into_any_page(client):
    """Credentials are demoed over screen share. They must not render."""
    client.post("/connections/slack/connect", data={"token": REAL_TOKEN})
    for path in ["/connections", "/", "/evidence"]:
        assert REAL_TOKEN not in client.get(path).text


def test_a_failed_connection_is_shown_but_does_not_break_the_app(client):
    r = client.post("/connections/slack/connect", data={"token": "xoxb-nope"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert client.get("/").status_code == 200


def test_investigating_produces_cited_facts(client):
    """Asking is now Retrieval; the old path redirects there rather than 404ing."""
    body = client.get("/?q=Ankusha+Rao").text
    assert "Where this came from" in body
    assert "records" in body
    assert client.get("/investigate", follow_redirects=True).status_code == 200


def test_acting_on_a_contradiction_executes_internally_and_gates_the_email(client):
    from walnut.contradiction import detect_contradictions
    from walnut.web import app as web

    client.get("/")  # trigger ingest
    conflicts = detect_contradictions(web.state.brain)
    assert conflicts, "fixtures produced no contradiction to act on"

    client.post("/act", data={"conflict_id": conflicts[0].id}, follow_redirects=True)
    results = web.state.last_results
    assert results, "no actions were attempted"

    refused = [r for r in results if hasattr(r, "reason")]
    executed = [r for r in results if not hasattr(r, "reason")]
    assert executed, "nothing executed"
    assert any(r.action.app == "email" for r in refused), "the customer email was not gated"
    assert web.state.gate.pending, "the gated action was not queued for a human"


def test_the_approvals_page_lists_what_is_waiting(client):
    from walnut.contradiction import detect_contradictions
    from walnut.web import app as web

    client.get("/")
    conflicts = detect_contradictions(web.state.brain)
    client.post("/act", data={"conflict_id": conflicts[0].id}, follow_redirects=True)
    body = client.get("/approvals").text
    assert "gated" in body and "Approve" in body


def test_an_executed_action_can_be_undone_from_the_console(client):
    from walnut.contradiction import detect_contradictions
    from walnut.web import app as web

    client.get("/")
    conflicts = detect_contradictions(web.state.brain)
    client.post("/act", data={"conflict_id": conflicts[0].id}, follow_redirects=True)

    live = web.state.agent.executor.ledger.live()
    assert live, "nothing to undo"
    client.post(f"/undo/{live[0].action_id}", follow_redirects=True)
    assert web.state.agent.executor.ledger.receipts[live[0].action_id].is_undone


def test_approving_in_the_console_executes_the_held_action(client):
    """End-to-end round trip for the demo's central beat."""
    from walnut.contradiction import detect_contradictions
    from walnut.web import app as web

    client.get("/")
    conflicts = detect_contradictions(web.state.brain)
    client.post("/act", data={"conflict_id": conflicts[0].id}, follow_redirects=True)

    assert web.state.gate.pending, "nothing was held for a human"
    key = next(iter(web.state.gate.pending))
    before = len(web.state.agent.executor.ledger.live())

    client.post(f"/approvals/{key}/approve", follow_redirects=True)

    after = web.state.agent.executor.ledger.live()
    assert len(after) == before + 1, "approval did not result in the action executing"
    assert any(r.action.app == "email" for r in after), "the approved email never sent"


def test_the_audit_page_reports_refusals_and_admits_its_limits(client):
    """The reliability surface must state what it does NOT prove."""
    from walnut.contradiction import detect_contradictions
    from walnut.web import app as web

    client.get("/")
    conflicts = detect_contradictions(web.state.brain)
    client.post("/act", data={"conflict_id": conflicts[0].id}, follow_redirects=True)

    body = client.get("/audit").text
    assert "Refusals" in body
    assert "does not prove" in body, "the audit page makes no honest limitations claim"
    assert "tripwire, not a perimeter" in body


def test_every_screen_in_the_console_actually_renders(client):
    """A syntax error in one screen module took /knowledge to a 500 and the whole
    suite still passed — no test walked the routes. The screens are imported lazily
    inside their handlers, so a broken screen does not even fail at startup: the app
    boots, five pages work, and the sixth is a stack trace nobody sees until it is on
    a projector.

    Parameter-free GET routes only; the ones taking a path parameter are covered by
    their own tests with a real id.
    """
    from walnut.web.app import app as web_app

    paths = sorted(
        route.path
        for route in web_app.routes
        if "GET" in getattr(route, "methods", set())
        and "{" not in getattr(route, "path", "{")
    )
    assert len(paths) >= 6, f"expected the full console, found only {paths}"

    broken: list[str] = []
    for path in paths:
        response = client.get(path, follow_redirects=True)
        if response.status_code != 200:
            broken.append(f"{path} -> {response.status_code}")
        elif path != "/healthz" and "Walnut" not in response.text:
            broken.append(f"{path} -> 200 but rendered no page shell")
    assert not broken, "screens that do not render: " + "; ".join(broken)
