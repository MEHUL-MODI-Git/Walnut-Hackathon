"""Endpoint and conformance tests for the Meridian Dispensary mock service.

`services/dispensary` exists to demonstrate Walnut's `RESTAdapter` against a real
HTTP service rather than a fixture or a fake transport — see `services/dispensary/
app.py`'s module docstring for the full argument. This module proves two separate
things and keeps them in separate test classes:

1. The service itself behaves as specified (`TestEndpoints`), exercised directly with
   `fastapi.testclient.TestClient` — no adapter involved.
2. The *unmodified* `RESTAdapter`, pointed at this service, passes the full
   `run_conformance` suite (`TestRESTAdapterConformance`) — the headline result.

For (2), the service runs in-process (no real socket, no port 8900 needed for tests):
`_asgi_transport()` below wraps `fastapi.testclient.TestClient` — itself a sync
`httpx.Client` subclass that runs an ASGI app through a blocking portal — to reproduce
exactly the `(method, url, **kwargs) -> Any` shape `RESTAdapter._build_default_transport`
uses for a real deployment (raise `RESTNotFound` on 404, `RESTAPIError` on other error
status, `{}` on an empty/204 body, else the parsed JSON). This is not a simplified test
double — it is the same request/response translation the real transport performs,
routed to the ASGI app in memory instead of over a socket. Running the actual service
on port 8900 (`uvicorn services.dispensary.app:app --port 8900`) and swapping in the
default `httpx`-backed transport (by constructing `RESTAdapter` without a `transport`
argument) exercises the identical code path over a real HTTP connection.

## The exact `RESTAdapter` configuration that makes conformance pass

    RESTAdapter(
        name="meridian_dispensary",
        base_url="https://dispensary.meridian-health.example",  # or http://localhost:8900
        list_path="/api/prescriptions",
        item_path="/api/prescriptions/{id}",
        records_key="data",
        id_field="id",
        text_fields=["drug", "dose", "instructions", "status", "dispense_status"],
        author_field="prescriber",
        timestamp_field="prescribed_at",
        uri_field="url",
        annotate_path="/api/annotations?prescription_id={id}",
        delete_path="/api/annotations/{id}",
        transport=<your transport>,
    )

`REST_ADAPTER_CONFIG` below is this exact dict (minus `transport`, which is supplied
per-test) — copy it directly rather than retyping it.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from services.dispensary.app import app as dispensary_app
from services.dispensary.app import reset as reset_dispensary
from services.dispensary.seed import BASE_URL
from walnut.adapters.rest import RESTAdapter, RESTAPIError, RESTNotFound
from walnut.conformance import run_conformance
from walnut.contract import SourcePointer

# ---------------------------------------------------------------------------
# the config the docstring above promises, kept honest by being the one actually used
# ---------------------------------------------------------------------------

REST_ADAPTER_CONFIG: dict[str, Any] = {
    "name": "meridian_dispensary",
    "base_url": BASE_URL,
    "list_path": "/api/prescriptions",
    "item_path": "/api/prescriptions/{id}",
    "records_key": "data",
    "id_field": "id",
    "text_fields": ["drug", "dose", "instructions", "status", "dispense_status"],
    "author_field": "prescriber",
    "timestamp_field": "prescribed_at",
    "uri_field": "url",
    "annotate_path": "/api/annotations?prescription_id={id}",
    "delete_path": "/api/annotations/{id}",
}


def _asgi_transport() -> Callable[..., Any]:
    """A `RESTAdapter` transport that speaks to `dispensary_app` in-process.

    Same status-code handling as `RESTAdapter._build_default_transport`: 404 becomes
    `RESTNotFound`, any other >=400 becomes `RESTAPIError`, an empty/204 body becomes
    `{}`, everything else is the parsed JSON body. The only thing swapped out is *how*
    the bytes move: `httpx.ASGITransport` only implements the *async* transport
    interface, so a plain sync `httpx.Client` cannot use it directly. `TestClient` is
    itself a sync `httpx.Client` subclass built for exactly this — it runs the ASGI
    app through a blocking portal — so it is reused here purely as that sync-over-ASGI
    transport, not for its assertion helpers. This is a faithful stand-in for pointing
    the adapter's real transport at `uvicorn services.dispensary.app:app --port 8900`.
    """
    client = TestClient(dispensary_app, base_url=BASE_URL)

    def _transport(method: str, url: str, **kwargs: Any) -> Any:
        response = client.request(method, url, **kwargs)
        if response.status_code == 404:
            raise RESTNotFound(url)
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            raise RESTAPIError(response.status_code, url, payload)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    return _transport


def _make_adapter(**overrides: Any) -> RESTAdapter:
    config = dict(REST_ADAPTER_CONFIG)
    config.update(overrides)
    config.setdefault("transport", _asgi_transport())
    return RESTAdapter(**config)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Every test starts from the same seed, regardless of what an earlier test in
    the same run mutated (a hold placed, an annotation added)."""
    reset_dispensary()


@pytest.fixture
def client() -> TestClient:
    return TestClient(dispensary_app)


# ---------------------------------------------------------------------------
# the service itself, exercised directly
# ---------------------------------------------------------------------------


class TestEndpoints:
    def test_healthz(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["patients"] >= 13
        assert body["prescriptions"] >= 15

    def test_list_prescriptions_default_shape_is_nested_under_data(
        self, client: TestClient
    ) -> None:
        response = client.get("/api/prescriptions")
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, dict), "must be an object with a 'data' key, not a bare array"
        assert isinstance(body["data"], list)
        assert len(body["data"]) >= 10

        record = body["data"][0]
        for key in (
            "id",
            "patient_mrn",
            "patient_name",
            "drug",
            "dose",
            "status",
            "prescriber",
            "prescribed_at",
            "dispense_status",
            "url",
        ):
            assert key in record, f"prescription record missing {key!r}"
        assert record["url"].startswith("https://")

    def test_list_prescriptions_excludes_inactive_and_expired(self, client: TestClient) -> None:
        ids = {rx["id"] for rx in client.get("/api/prescriptions").json()["data"]}
        assert "rx-0016" not in ids  # expired
        assert "rx-0017" not in ids  # inactive

    def test_list_prescriptions_filtered_by_patient(self, client: TestClient) -> None:
        response = client.get("/api/prescriptions", params={"patient": "MR-4417"})
        records = response.json()["data"]
        assert len(records) == 1
        assert records[0]["patient_mrn"] == "MR-4417"

    def test_get_prescription_by_id(self, client: TestClient) -> None:
        response = client.get("/api/prescriptions/rx-0001")
        assert response.status_code == 200
        assert response.json()["drug"] == "co-amoxiclav"

    def test_get_prescription_404(self, client: TestClient) -> None:
        response = client.get("/api/prescriptions/rx-does-not-exist")
        assert response.status_code == 404

    def test_get_patient(self, client: TestClient) -> None:
        response = client.get("/api/patients/MR-4417")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Ankusha Rao"
        assert body["mrn"] == "MR-4417"
        assert body["email"].endswith(".example")

    def test_get_patient_404(self, client: TestClient) -> None:
        response = client.get("/api/patients/MR-9999")
        assert response.status_code == 404

    def test_allergies_shape_and_404(self, client: TestClient) -> None:
        response = client.get("/api/allergies", params={"patient": "MR-1003"})
        assert response.status_code == 200
        records = response.json()["data"]
        assert records[0]["allergen"] == "penicillin"

        missing = client.get("/api/allergies", params={"patient": "MR-9999"})
        assert missing.status_code == 404

    def test_dispense_queue_includes_todays_queued_prescriptions(
        self, client: TestClient
    ) -> None:
        records = client.get("/api/dispense-queue").json()["data"]
        ids = {rx["id"] for rx in records}
        assert "rx-0001" in ids  # Ankusha's co-amoxiclav, queued today
        for rx in records:
            assert rx["dispense_date"] is not None

    def test_hold_flips_dispense_status_and_persists(self, client: TestClient) -> None:
        before = client.get("/api/prescriptions/rx-0001").json()
        assert before["dispense_status"] == "queued"

        created = client.post(
            "/api/holds",
            json={
                "prescription_id": "rx-0001",
                "reason": "pharmacist verifying dose with prescriber",
                "placed_by": "pharm-tech-07",
            },
        )
        assert created.status_code == 201
        hold = created.json()
        for key in ("id", "prescription_id", "reason", "placed_at"):
            assert key in hold
        assert hold["prescription_id"] == "rx-0001"

        # the hold is visible in a subsequent GET — it actually persists
        held = client.get("/api/prescriptions/rx-0001").json()
        assert held["dispense_status"] == "held"

        released = client.delete(f"/api/holds/{hold['id']}")
        assert released.status_code == 200
        assert released.json()["released"] is True

        after = client.get("/api/prescriptions/rx-0001").json()
        assert after["dispense_status"] == "queued"

    def test_release_unknown_hold_404s(self, client: TestClient) -> None:
        response = client.delete("/api/holds/hold-does-not-exist")
        assert response.status_code == 404

    def test_create_and_delete_annotation(self, client: TestClient) -> None:
        created = client.post(
            "/api/annotations",
            params={"prescription_id": "rx-0001"},
            json={"note": "confirmed no known penicillin-class allergy on file", "author": "rph.chen"},
        )
        assert created.status_code == 201
        annotation = created.json()
        assert annotation["prescription_id"] == "rx-0001"
        assert annotation["note"].startswith("confirmed")
        assert "id" in annotation

        deleted = client.delete(f"/api/annotations/{annotation['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        again = client.delete(f"/api/annotations/{annotation['id']}")
        assert again.status_code == 404

    def test_reset_restores_seed(self, client: TestClient) -> None:
        client.post(
            "/api/holds",
            json={"prescription_id": "rx-0001", "reason": "x", "placed_by": "y"},
        )
        assert client.get("/api/prescriptions/rx-0001").json()["dispense_status"] == "held"

        response = client.post("/api/_reset")
        assert response.status_code == 200
        assert response.json()["reset"] is True

        assert client.get("/api/prescriptions/rx-0001").json()["dispense_status"] == "queued"
        assert client.get("/api/holds/does-not-matter").status_code in (404, 405)

    def test_ankusha_scenario(self, client: TestClient) -> None:
        """The specific scenario this service exists to hand Walnut on a plate: an
        active co-amoxiclav prescription, queued for today, for a patient the
        pharmacy has no allergy information on at all."""
        patient = client.get("/api/patients/MR-4417").json()
        assert patient["name"] == "Ankusha Rao"

        prescriptions = client.get("/api/prescriptions", params={"patient": "MR-4417"}).json()[
            "data"
        ]
        assert len(prescriptions) == 1
        rx = prescriptions[0]
        assert rx["drug"] == "co-amoxiclav"
        assert rx["status"] == "active"
        assert rx["dispense_status"] == "queued"
        assert rx["dispense_date"] is not None

        queue_ids = {r["id"] for r in client.get("/api/dispense-queue").json()["data"]}
        assert rx["id"] in queue_ids

        allergies = client.get("/api/allergies", params={"patient": "MR-4417"}).json()["data"]
        assert allergies == []


# ---------------------------------------------------------------------------
# the headline: the unmodified RESTAdapter, pointed at this service
# ---------------------------------------------------------------------------


class TestRESTAdapterConformance:
    def test_rest_adapter_conforms_against_the_live_service(self) -> None:
        adapter = _make_adapter()
        report = run_conformance(
            adapter,
            write_target={
                "operation": "annotate",
                "target": {"id": "rx-0002"},
                "payload": {"note": "walnut conformance", "author": "walnut-conformance"},
                # Annotating a prescription is additive — a new note, not an
                # overwrite of the prescription itself — so there is no prior_state
                # to capture or restore. Matches the internal-wiki fixture's own
                # annotate write_target in test_rest_adapter.py.
                "overwrites": False,
            },
        )
        assert report.ok, report.render()
        # A green report that silently stopped counting is exactly the failure mode
        # conformance.py's own _check() docstring warns about — assert it actually
        # ran checks, not just that none of them failed.
        assert len(report.passed) >= 10
        assert not report.skipped, (
            "every check should have run against a live service with real data and "
            f"a real write_target, not been skipped: {report.skipped}"
        )

    def test_resolve_roundtrip_against_live_service(self) -> None:
        adapter = _make_adapter()
        evidence = adapter.fetch(limit=5)
        assert evidence, "fetch() returned nothing to resolve"

        first = evidence[0]
        again = adapter.resolve(first.pointer)
        assert again is not None
        assert again.pointer.content_hash == first.pointer.content_hash

    def test_resolve_missing_record_returns_none(self) -> None:
        adapter = _make_adapter()
        ghost = SourcePointer(
            app=adapter.name,
            resource_uri=f"{BASE_URL}/api/prescriptions/does-not-exist",
            locator={"id": "does-not-exist"},
            content_hash="0" * 64,
        )
        assert adapter.resolve(ghost) is None

    def test_fetch_finds_ankushas_prescription_as_evidence(self) -> None:
        adapter = _make_adapter()
        evidence = adapter.fetch(limit=100)
        ankusha = [e for e in evidence if e.raw.get("patient_mrn") == "MR-4417"]
        assert len(ankusha) == 1

        ev = ankusha[0]
        assert "co-amoxiclav" in ev.text
        assert ev.author == "Dr. Layla Chen"
        assert ev.occurred_at is not None
        assert ev.pointer.resource_uri == "https://dispensary.meridian-health.example/prescriptions/rx-0001"
        assert len(ev.pointer.content_hash) == 64

    def test_annotate_writes_and_undo_removes_it(self) -> None:
        adapter = _make_adapter()
        from walnut.contract import Action

        action = Action(
            app=adapter.name,
            operation="annotate",
            target={"id": "rx-0002"},
            payload={"note": "single annotate/undo check", "author": "test"},
            justified_by=("test:synthetic",),
        )
        receipt = adapter.act(action)
        assert receipt.result["annotation_id"]

        undone = adapter.undo(receipt)
        assert undone.is_undone

        # a second undo of the same, already-deleted annotation must not succeed
        # silently — the delete_path route 404s, and RESTAdapter does not swallow
        # that inside undo(), which is the correct behaviour: undo is not idempotent
        # once the thing it removes is already gone.
        with pytest.raises(RESTAPIError):
            adapter.undo(undone)
