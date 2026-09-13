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
from walnut.actions.executor import ActionExecutor
from walnut.actions.governance import QueueGate, Refusal, RefusalReason
from walnut.adapters.fixture import load_all_fixtures
from walnut.adapters.rest import RESTAdapter, RESTAPIError, RESTNotFound
from walnut.brain import Brain
from walnut.clinical_sources import register_clinical_sources
from walnut.conformance import run_conformance
from walnut.contract import Action, ActionTier, SourcePointer
from walnut.contradiction import detect_contradictions
from walnut.internal_systems import DISPENSARY_SPEC
from walnut.playbook import build_plan
from walnut.plugins import SourceRegistry

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


# ---------------------------------------------------------------------------
# the direction of risk: the agent can stop a dose, it cannot start one
# ---------------------------------------------------------------------------
#
# `DISPENSARY_SPEC` declares `place_hold` INTERNAL — performed alone — and
# `release_hold` GATED — a human confirms first. Both are writes against the same
# resource, so the tier cannot be following "is this a write". It follows which way
# the risk points: stopping a dose fails safe, starting one does not.
#
# README.md and the demo narration both state that as a property of the product, so
# it is proved here through the executor that actually enforces tiers and against the
# service's own state, rather than by reading the configuration back to itself.


def _dispensary_adapter() -> RESTAdapter:
    """The shipped `DISPENSARY_SPEC`, verbatim, wired to the in-process service.

    Nothing in the spec is rewritten for the test — not the tiers, and not the
    `text_fields` that decide whether a prescription is findable by the patient the
    plan is about. Only the bytes move differently: the ASGI transport answers the
    spec's own `http://localhost:8900` URLs from `dispensary_app` in memory, so what
    these tests exercise is the configuration the product registers at startup.
    """
    spec = {key: value for key, value in DISPENSARY_SPEC.items() if key != "kind"}
    return RESTAdapter(transport=_asgi_transport(), **spec)


def _ingest(adapter: Any, brain: Brain) -> Brain:
    for evidence in adapter.fetch(limit=200):
        brain.remember(evidence)
    return brain


def _fact_id(adapter: RESTAdapter, prescription_id: str) -> str:
    """The brain's node id for one prescription — what an action must cite."""
    return f"{adapter.name}:{adapter.name}:{prescription_id}"


def _hold_action(adapter: RESTAdapter, prescription_id: str) -> Action:
    return Action(
        app=adapter.name,
        operation="place_hold",
        target={"id": prescription_id},
        payload={
            "prescription_id": prescription_id,
            "reason": "Walnut: contradiction on the allergy record. Verify before dispensing.",
            "placed_by": "walnut",
        },
        justified_by=(_fact_id(adapter, prescription_id),),
        rationale="Hold the queued dose until a clinician has seen the disagreement.",
    )


def _annotate_action(adapter: RESTAdapter, prescription_id: str) -> Action:
    """The other kind of write this source offers: additive, and harmless to retract."""
    return Action(
        app=adapter.name,
        operation="annotate",
        target={"id": prescription_id},
        payload={"note": "Walnut: allergy record disputed — see the care-team thread.",
                 "author": "walnut"},
        justified_by=(_fact_id(adapter, prescription_id),),
        rationale="Leave the disagreement beside the script, without changing it.",
    )


def _release_action(adapter: RESTAdapter, hold_id: str, prescription_id: str) -> Action:
    return Action(
        app=adapter.name,
        operation="release_hold",
        target={"id": hold_id},
        payload={},
        justified_by=(_fact_id(adapter, prescription_id),),
        rationale="Release the hold now a clinician has checked the chart.",
    )


class TestDirectionOfRisk:
    @pytest.fixture
    def stack(self) -> tuple[RESTAdapter, Brain, QueueGate, ActionExecutor]:
        """Adapter, brain, gate and executor, wired the way the console wires them.

        Freshness checking stays ON — this is the live path, not the fixture one — so
        a test that acts on a record it has not re-read after changing it will be
        refused as stale rather than quietly passing.
        """
        adapter = _dispensary_adapter()
        brain = _ingest(adapter, Brain())
        gate = QueueGate()
        executor = ActionExecutor({adapter.name: adapter}, brain, gate)
        return adapter, brain, gate, executor

    def test_the_declared_tiers_follow_the_direction_of_risk(self) -> None:
        """Tiering by "is it a write" gets this exactly backwards, and the mistake is
        invisible in review: both operations write, and the dangerous one is the
        DELETE that looks like a cleanup."""
        operations = _dispensary_adapter().capabilities().operations
        assert operations["place_hold"] is ActionTier.INTERNAL
        assert operations["release_hold"] is ActionTier.GATED
        assert operations["place_hold"] < operations["release_hold"]

    def test_placing_a_hold_runs_with_no_human_and_the_dose_actually_stops(
        self, stack, client: TestClient
    ) -> None:
        """A hold that queues for approval is not a hold. The dose is handed over
        while the request sits in a console overnight, and the agent's one genuinely
        protective power is spent waiting for the person it was meant to protect
        against needing."""
        adapter, _brain, gate, executor = stack
        assert client.get("/api/prescriptions/rx-0001").json()["dispense_status"] == "queued"

        receipt = executor.execute(_hold_action(adapter, "rx-0001"))

        if isinstance(receipt, Refusal):
            pytest.fail(receipt.render())
        assert gate.pending == {}, "no human was asked, and none should have been"
        assert receipt.result["prescription_id"] == "rx-0001"
        assert client.get("/api/prescriptions/rx-0001").json()["dispense_status"] == "held"

    def test_releasing_a_hold_is_refused_while_no_human_has_approved(
        self, stack, client: TestClient
    ) -> None:
        """Releasing a hold puts the dose back into a patient's hand. An agent able to
        do that alone can undo its own safety decision, which makes the hold worth
        nothing. The refusal must also leave the service exactly as it was — a refusal
        that half-executed is worse than either clean outcome."""
        adapter, brain, gate, executor = stack
        hold = executor.execute(_hold_action(adapter, "rx-0001"))
        assert not isinstance(hold, Refusal)
        # The hold changed the record the release will cite, so re-read it. Acting on
        # a stale citation is a different refusal, and it would mask this one.
        _ingest(adapter, brain)

        refusal = executor.execute(_release_action(adapter, hold.result["id"], "rx-0001"))

        assert isinstance(refusal, Refusal), "the dose was released with nobody asked"
        # An unanswered QueueGate request reports as a timeout: nothing is approved by
        # default, so "no answer yet" and "no answer ever" are the same refusal.
        assert refusal.reason is RefusalReason.GATE_TIMEOUT
        assert len(gate.pending) == 1, "the request must be visible to a human to answer"
        assert executor.ledger.summary()["refused"] == 1

        assert client.get("/api/prescriptions/rx-0001").json()["dispense_status"] == "held"
        # The hold record itself survived, not just the status field: releasing it by
        # hand still finds something to release.
        assert client.delete(f"/api/holds/{hold.result['id']}").status_code == 200

    def test_releasing_a_hold_executes_once_a_human_approves_it_through_the_gate(
        self, stack, client: TestClient
    ) -> None:
        """A gate nothing can pass is a ban wearing a gate's clothes. The pharmacist
        who checks the chart and clears the dose has to be able to let it go, and the
        same action must then run unchanged — an approval that does not actually
        release anything trains people to work around the console."""
        adapter, brain, gate, executor = stack
        hold = executor.execute(_hold_action(adapter, "rx-0001"))
        _ingest(adapter, brain)
        release = _release_action(adapter, hold.result["id"], "rx-0001")
        assert isinstance(executor.execute(release), Refusal)

        (key,) = gate.pending
        gate.resolve(key, approved=True, note="pharmacist checked the chart with Dr Chen")

        receipt = executor.execute(release)

        if isinstance(receipt, Refusal):
            pytest.fail(receipt.render())
        assert gate.pending == {}
        assert client.get("/api/prescriptions/rx-0001").json()["dispense_status"] == "queued"

    def test_undoing_a_hold_restores_the_status_the_prescription_really_had(
        self, stack, client: TestClient
    ) -> None:
        """rx-0002 is `not_scheduled`, not `queued`. An undo that wrote a hardcoded
        "queued" back would take a prescription nobody had scheduled and put it into
        today's dispensing queue — the agent manufacturing a dispensing event out of
        a reversal, which is the one thing it is not allowed to do."""
        adapter, _brain, _gate, executor = stack
        before = client.get("/api/prescriptions/rx-0002").json()["dispense_status"]
        assert before == "not_scheduled"

        receipt = executor.execute(_hold_action(adapter, "rx-0002"))
        assert not isinstance(receipt, Refusal)
        assert client.get("/api/prescriptions/rx-0002").json()["dispense_status"] == "held"

        # Undo goes through the gate now, because reversing a hold IS a release.
        gate = stack[2]
        refused = executor.undo(receipt.action_id)
        assert isinstance(refused, Refusal), "undo of a hold ran without a human"
        for key in list(gate.pending):
            gate.resolve(key, approved=True)
        undone = executor.undo(receipt.action_id)

        assert undone.is_undone
        after = client.get("/api/prescriptions/rx-0002").json()["dispense_status"]
        assert after == before
        assert after != "queued", "the prior status was invented, not restored"
        # The hold record is gone, not merely detached from the prescription.
        assert client.delete(f"/api/holds/{receipt.result['id']}").status_code == 404

    def test_undoing_a_hold_needs_the_same_human_as_releasing_it(
        self, stack, client: TestClient
    ) -> None:
        """Undo of a `place_hold` *is* a release: same endpoint, same consequence —
        a dose that was stopped goes back out.

        This was a real hole, not a hypothetical one. `ActionExecutor.undo()` looked
        up no tier and consulted no gate, so the agent reached "the dose goes back
        out" in two steps that each looked INTERNAL, and the GATED tier on
        `release_hold` protected nothing against anyone who took the second step.
        The console only offers Undo to a person, but nothing in the executor
        required that — `brief.py` calls `undo()` with no human anywhere near it.
        """
        adapter, _brain, gate, executor = stack
        receipt = executor.execute(_hold_action(adapter, "rx-0001"))
        assert not isinstance(receipt, Refusal)

        refused = executor.undo(receipt.action_id)

        assert isinstance(refused, Refusal), "undo released the dose with no human"
        assert refused.reason is RefusalReason.GATE_TIMEOUT
        assert (
            client.get("/api/prescriptions/rx-0001").json()["dispense_status"] == "held"
        ), "the dose went back out while the gate was still waiting"
        assert gate.pending, "undo bypassed the gate rather than queueing for it"

    def test_undo_still_needs_no_human_when_the_reversal_is_harmless(
        self, stack, client: TestClient
    ) -> None:
        """Gating every undo would be the easy over-correction, and it would make
        undo useless: taking back a note nobody has read is not a decision anyone
        needs to approve. The tier follows the reversal's own consequence, so an
        annotation retracts on its own."""
        adapter, _brain, gate, executor = stack
        receipt = executor.execute(_annotate_action(adapter, "rx-0001"))
        assert not isinstance(receipt, Refusal)

        undone = executor.undo(receipt.action_id)

        assert not isinstance(undone, Refusal), "a harmless reversal asked for approval"
        assert undone.is_undone
        assert not gate.pending


# ---------------------------------------------------------------------------
# the plan only stops a dose that is actually about to go out
# ---------------------------------------------------------------------------


def _clinic_brain(dispensary: RESTAdapter | None) -> tuple[Brain, dict[str, Any]]:
    """The brain the console assembles: five fixture apps, the clinic's own database,
    and — when the customer has connected one — their dispensary.

    The EHR arrives through `register_clinical_sources` rather than the fixture
    loader because that is where the other half of the allergy contradiction lives:
    the record saying `Allergies: None recorded` is a database row, and until the
    database is a source the brain holds only the nurse's message.
    """
    brain = Brain()
    adapters: dict[str, Any] = dict(load_all_fixtures())
    registry = SourceRegistry()
    register_clinical_sources(registry)
    adapters.update(registry.usable_adapters())
    if dispensary is not None:
        adapters[dispensary.name] = dispensary
    for adapter in adapters.values():
        _ingest(adapter, brain)
    return brain, adapters


def _allergy_conflict(brain: Brain):
    conflicts = [c for c in detect_contradictions(brain) if c.subject == "MR-4417"]
    assert len(conflicts) == 1, (
        "expected exactly the MR-4417 allergy contradiction, got "
        + "; ".join(f"{c.subject} {c.apps}" for c in conflicts)
    )
    return conflicts[0]


def _hold_steps(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in plan if step["operation"] == "place_hold"]


class TestPlanStopsTheDose:
    def test_the_plan_holds_the_queued_dose_for_the_allergy_contradiction(self) -> None:
        """The whole scenario turns on this one step existing. The record says no
        allergies, a nurse reported a rash after amoxicillin, and a co-amoxiclav dose
        is queued for today — a plan that files a ticket about that and lets the dose
        go out has documented the harm rather than prevented it."""
        adapter = _dispensary_adapter()
        brain, adapters = _clinic_brain(adapter)

        plan = build_plan(_allergy_conflict(brain), brain, adapters=adapters)

        holds = _hold_steps(plan)
        assert len(holds) == 1, [f"{s['app']}.{s['operation']}" for s in plan]
        assert holds[0]["app"] == "dispensary"
        assert holds[0]["target"] == {"id": "rx-0001"}
        assert holds[0]["payload"]["prescription_id"] == "rx-0001"
        assert adapter.capabilities().tier_of("place_hold") is ActionTier.INTERNAL

    def test_the_plan_does_not_hold_a_dose_that_has_already_gone_out(self) -> None:
        """A hold on a dose the patient swallowed yesterday changes nothing and tells
        the clinician reading the plan that the agent does not know what has already
        happened — which is the one thing it is being trusted about."""
        state = reset_dispensary()
        state["prescriptions"]["rx-0001"]["dispense_status"] = "dispensed"
        brain, adapters = _clinic_brain(_dispensary_adapter())

        plan = build_plan(_allergy_conflict(brain), brain, adapters=adapters)

        assert _hold_steps(plan) == []
        assert len(plan) >= 4, "the rest of the plan must be unaffected"

    def test_the_plan_does_not_place_a_second_hold_on_a_dose_already_held(
        self, client: TestClient
    ) -> None:
        """The dose is already stopped, and a second hold is not merely redundant:
        each hold remembers the status it found, so releasing them out of order
        restores `queued` while the other hold still stands, and the dose goes out
        with an outstanding hold against it."""
        client.post(
            "/api/holds",
            json={
                "prescription_id": "rx-0001",
                "reason": "pharmacist verifying dose with prescriber",
                "placed_by": "pharm-tech-07",
            },
        )
        brain, adapters = _clinic_brain(_dispensary_adapter())

        plan = build_plan(_allergy_conflict(brain), brain, adapters=adapters)

        assert _hold_steps(plan) == []

    def test_no_hold_is_planned_when_the_dispensary_is_not_among_the_adapters(self) -> None:
        """The prescription is still in the brain from an earlier ingest, so the
        capability has to be checked against the adapters actually passed in rather
        than remembered from the evidence. A step aimed at a source that is no longer
        connected raises at the API boundary — after Linear, GitHub and Notion have
        already been written to — and reads as a connector bug, not a planning one."""
        adapter = _dispensary_adapter()
        brain, adapters = _clinic_brain(adapter)
        conflict = _allergy_conflict(brain)
        assert brain.get(_fact_id(adapter, "rx-0001")) is not None

        without = {name: a for name, a in adapters.items() if name != adapter.name}
        assert _hold_steps(build_plan(conflict, brain, adapters=without)) == []
        assert _hold_steps(build_plan(conflict, brain)) == []
