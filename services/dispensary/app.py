"""Meridian Dispensary — a mock of a customer's internal pharmacy system.

This is not a real pharmacy system, was never connected to one, and holds nothing but
fictional data (see `seed.py`). It exists for one reason: Walnut claims it can connect
to a customer's own bespoke internal tools through `RESTAdapter`, described entirely
by configuration rather than a bespoke adapter — see `walnut/adapters/rest.py`'s
module docstring for the argument in full. Everywhere else that claim is demonstrated
against fixtures or fakes constructed *for* the adapter's tests. This module is a real
FastAPI application, runnable on a real port, standing in for "a clinic group's
internal pharmacy system nobody at Walnut has ever seen the source of" — so that the
extensibility claim is something a reader can watch happen against a live HTTP
service, not just trust because the code says so.

## Shape choices made to exercise `RESTAdapter` honestly

* **The list endpoint nests records under `"data"`, not a bare array.**
  `RESTAdapter._extract_records` handles both shapes; the brief specifically wants the
  harder, nested one (`records_key="data"`) exercised here, so the easier bare-array
  shape is never used by this service.

* **`POST /api/annotations` takes the prescription id as a query parameter, not a
  path segment.** `RESTAdapter.act()` always appends the target id onto whatever
  `annotate_path` template it is given (`{id}` substitution, or a trailing
  `/<id>` if there is no placeholder) — there is no way to ask it to put the id
  somewhere else, because the adapter's `json=action.payload` body carries only the
  *content* of the write, never the target. Putting the id in the query string
  (`annotate_path="/api/annotations?prescription_id={id}"`) is what lets the URL
  *path* stay exactly `/api/annotations`, as specified, while still giving the
  adapter a `{id}` placeholder to fill in. See `tests/test_dispensary.py` for the
  exact adapter configuration this produces.

* **`DELETE /api/annotations/{annotation_id}` exists to make `undo()` real, not
  decorative.** It is not in the brief's endpoint list, but `RESTAdapter.undo()` for
  an `annotate` action needs *some* `delete_path` to call, mirroring the
  `DELETE /api/holds/{hold_id}` pattern the brief already specifies. Without it,
  `run_conformance`'s write half would either fail on `undo_restores` or have to be
  skipped outright by omitting `write_target` — and the point of this service is to
  show the write path actually working end to end, not to dodge it.

## What is deliberately *not* here

There is no persistence layer. All state lives in one process-local dict and is lost
on restart — `POST /api/_reset` puts it back to the seed, it does not save it anywhere.
There is no authentication, because `RESTAdapter`'s `headers` config is what a real
deployment would use to carry a token, and a mock demonstrating adapter *shape* gains
nothing from also faking an auth scheme. Concurrency safety is not a concern: this is
a single-process demo service, not a production pharmacy system.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from .seed import build_seed

__all__ = ["app", "reset"]

app = FastAPI(
    title="Meridian Dispensary (mock)",
    description=(
        "A mock of a clinic group's internal pharmacy system, built to demonstrate "
        "Walnut's RESTAdapter against a real HTTP service. Entirely fictional data. "
        "Not a real pharmacy system."
    ),
    version="0.1.0",
)

_STATE: dict[str, Any] = {}
"""Process-local, in-memory state. Replaced wholesale by `reset()`, never mutated by
anything else at this module level — every endpoint below reaches into whatever
`_STATE` currently points at, so a `reset()` mid-process is visible to every request
that follows it without restarting the server."""


def reset() -> dict[str, Any]:
    """Repopulate `_STATE` from a fresh seed and return the new state.

    Exposed both as a plain function (for tests and `demo.py`-style scripts that
    import this module directly) and as `POST /api/_reset` (for a demo that wants to
    reset the live, already-running process — place a hold, show it, reset, show the
    hold is gone, without restarting the service). Both paths go through this one
    function so there is exactly one definition of "clean state."
    """
    global _STATE
    _STATE = build_seed()
    return _STATE


reset()  # seed at import time: the service is usable the instant it is created


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------


class HoldCreate(BaseModel):
    """Body for `POST /api/holds`."""

    prescription_id: str
    reason: str
    placed_by: str


class AnnotationCreate(BaseModel):
    """Body for `POST /api/annotations`. This is exactly `Action.payload` for the
    `annotate` operation — see the module docstring's note on why the target
    prescription id travels as a query parameter instead of living in this body."""

    note: str
    author: str | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _today_iso() -> str:
    return date.today().isoformat()


def _prescription_or_404(prescription_id: str) -> dict[str, Any]:
    rx = _STATE["prescriptions"].get(prescription_id)
    if rx is None:
        raise HTTPException(status_code=404, detail=f"no prescription {prescription_id!r}")
    return rx


def _holds_on(prescription_id: str) -> list[dict[str, Any]]:
    """Every hold currently standing on one prescription, oldest first.

    Holds are addressed individually but their EFFECT is shared: `dispense_status` is
    a single field on the prescription, so two holds and one release cannot each own
    it. Everything that reads or restores that field goes through this helper.
    """
    return [h for h in _STATE["holds"].values() if h["prescription_id"] == prescription_id]


def _patient_or_404(mrn: str) -> dict[str, Any]:
    patient = _STATE["patients"].get(mrn)
    if patient is None:
        raise HTTPException(status_code=404, detail=f"no patient {mrn!r}")
    return patient


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "meridian-dispensary",
        "patients": len(_STATE["patients"]),
        "prescriptions": len(_STATE["prescriptions"]),
    }


# ---------------------------------------------------------------------------
# prescriptions
# ---------------------------------------------------------------------------


@app.get("/api/prescriptions")
def list_prescriptions(patient: str | None = Query(default=None)) -> dict[str, Any]:
    """The main list endpoint `RESTAdapter.fetch()` is pointed at (`list_path`).

    Returns only *active* prescriptions — this is "active prescriptions", not "every
    prescription this pharmacy has ever seen" — optionally narrowed to one patient via
    `?patient={mrn}`. `RESTAdapter` itself never sends `patient`; the filter exists
    for direct API callers (and this module's own tests) to exercise. Records are
    nested under `"data"`, not returned as a bare array — see the module docstring.
    """
    records = [
        rx
        for rx in _STATE["prescriptions"].values()
        if rx["status"] == "active" and (patient is None or rx["patient_mrn"] == patient)
    ]
    records.sort(key=lambda rx: rx["id"])
    return {"data": records}


@app.get("/api/prescriptions/{prescription_id}")
def get_prescription(prescription_id: str) -> dict[str, Any]:
    """`item_path` for `RESTAdapter.resolve()` — re-fetches one record by id so a
    citation can be re-verified. Returns the record regardless of `status`
    (`resolve()` re-checks whatever it was pointed at; it does not re-apply the list
    endpoint's active-only filter, or a prescription that lapsed between fetch and
    resolve would wrongly look "gone" rather than "still there, now inactive")."""
    return _prescription_or_404(prescription_id)


# ---------------------------------------------------------------------------
# patients
# ---------------------------------------------------------------------------


@app.get("/api/patients/{mrn}")
def get_patient(mrn: str) -> dict[str, Any]:
    return _patient_or_404(mrn)


# ---------------------------------------------------------------------------
# allergies
# ---------------------------------------------------------------------------


@app.get("/api/allergies")
def list_allergies(patient: str = Query(...)) -> dict[str, Any]:
    """What the PHARMACY believes about this patient's allergies — not what any other
    system of record (the EHR, the prescriber's own notes) might say. The two can
    legitimately disagree; this endpoint answers only for the dispensary itself."""
    _patient_or_404(patient)
    return {"data": _STATE["allergies"].get(patient, [])}


# ---------------------------------------------------------------------------
# dispense queue
# ---------------------------------------------------------------------------


@app.get("/api/dispense-queue")
def dispense_queue() -> dict[str, Any]:
    """Everything scheduled to be handed out today, regardless of whether it has
    already gone out (`dispense_status == "dispensed"`), is waiting
    (`"queued"`), or has been pulled back (`"held"`)."""
    today = _today_iso()
    records = [rx for rx in _STATE["prescriptions"].values() if rx.get("dispense_date") == today]
    records.sort(key=lambda rx: rx["id"])
    return {"data": records}


# ---------------------------------------------------------------------------
# holds
# ---------------------------------------------------------------------------


@app.post("/api/holds", status_code=201)
def create_hold(body: HoldCreate) -> dict[str, Any]:
    """Place a hold on a prescription. Flips that prescription's `dispense_status`
    to `"held"`, remembering what it was before so `release_hold` can restore the
    exact prior value rather than guessing it was always `"queued"`."""
    rx = _prescription_or_404(body.prescription_id)

    hold_id = f"hold-{_STATE['_next_hold_id']:04d}"
    _STATE["_next_hold_id"] += 1

    # The status to restore is the one from BEFORE the first hold, not the one this
    # call happens to find. Place two holds on the same prescription and the naive
    # version records `"held"` as the second hold's prior status, so releasing both
    # leaves the dose held forever with no hold explaining why — a dose that can never
    # be dispensed and nothing in the system saying so. Any existing hold on this
    # prescription already knows the true pre-hold value, so reuse it.
    existing = _holds_on(body.prescription_id)
    prior = existing[0]["_prior_dispense_status"] if existing else rx["dispense_status"]

    hold = {
        "id": hold_id,
        "prescription_id": body.prescription_id,
        "reason": body.reason,
        "placed_by": body.placed_by,
        "placed_at": _now_iso(),
        "_prior_dispense_status": prior,
    }
    _STATE["holds"][hold_id] = hold
    rx["dispense_status"] = "held"

    return {k: v for k, v in hold.items() if not k.startswith("_")}


@app.delete("/api/holds/{hold_id}")
def release_hold(hold_id: str) -> dict[str, Any]:
    """Release a hold. Restores the prescription's `dispense_status` to whatever it
    was immediately before this hold was placed, and removes the hold record."""
    hold = _STATE["holds"].pop(hold_id, None)
    if hold is None:
        raise HTTPException(status_code=404, detail=f"no hold {hold_id!r}")

    rx = _STATE["prescriptions"].get(hold["prescription_id"])
    # A dose stays held while ANY hold is still on it. Restoring unconditionally meant
    # releasing the first of two holds put the dose back in the queue while a second
    # hold was still standing — the hold record said stopped, the queue said going
    # out, and the queue is the one that hands the drug over.
    remaining = _holds_on(hold["prescription_id"])
    if rx is not None and not remaining:
        rx["dispense_status"] = hold["_prior_dispense_status"]

    return {
        "released": True,
        "hold_id": hold_id,
        "prescription_id": hold["prescription_id"],
        "holds_remaining": len(remaining),
    }


# ---------------------------------------------------------------------------
# annotations
# ---------------------------------------------------------------------------


@app.post("/api/annotations", status_code=201)
def create_annotation(
    body: AnnotationCreate, prescription_id: str = Query(...)
) -> dict[str, Any]:
    """Add a note to a prescription. `prescription_id` arrives as a query parameter,
    not a path segment or a body field — see the module docstring for why
    `RESTAdapter.act()` needs it there."""
    _prescription_or_404(prescription_id)

    annotation_id = f"note-{_STATE['_next_annotation_id']:04d}"
    _STATE["_next_annotation_id"] += 1

    annotation = {
        "id": annotation_id,
        "prescription_id": prescription_id,
        "note": body.note,
        "author": body.author,
        "created_at": _now_iso(),
    }
    _STATE["annotations"][annotation_id] = annotation
    return annotation


@app.delete("/api/annotations/{annotation_id}")
def delete_annotation(annotation_id: str) -> dict[str, Any]:
    """Remove an annotation. This is what `RESTAdapter.undo()` calls for an
    `annotate` action (`delete_path="/api/annotations/{id}"`) — see the module
    docstring for why this route exists beyond the brief's literal endpoint list."""
    annotation = _STATE["annotations"].pop(annotation_id, None)
    if annotation is None:
        raise HTTPException(status_code=404, detail=f"no annotation {annotation_id!r}")
    return {"deleted": True, "annotation_id": annotation_id}


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


@app.post("/api/_reset")
def reset_endpoint() -> dict[str, Any]:
    """Put the live, already-running service back to its seed state. Exists so a
    demo can place a hold, show it, reset, and show it is gone — without restarting
    the process."""
    state = reset()
    return {
        "reset": True,
        "patients": len(state["patients"]),
        "prescriptions": len(state["prescriptions"]),
    }
