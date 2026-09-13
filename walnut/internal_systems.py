"""Auto-registering a customer's own internal systems.

The product's claim is that a company can point Walnut at its *own* systems — the
database, the internal service somebody wrote years ago — and have them become part of
the brain like any first-party connector. `services/dispensary/` exists so that claim is
demonstrable against a real HTTP service rather than a fixture, and this module is what
picks it up.

Two behaviours matter more than the registration itself:

**It probes before registering.** A configured-but-unreachable source that silently
contributes nothing is the failure mode the coverage table exists to expose, so an
unreachable service is registered as a *failure* the console can show rather than
quietly skipped.

**It never blocks startup.** If the service is not running, the console still loads,
every other source still works, and the coverage table reports `dispensary · not
searched` with a reason. The failure mode demonstrates the honesty feature instead of
breaking the product.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["DISPENSARY_SPEC", "register_internal_systems"]

DISPENSARY_URL = os.environ.get("DISPENSARY_URL", "http://localhost:8900")

# The exact configuration the REST adapter passes conformance with, verified against
# the live service in tests/test_dispensary.py.
DISPENSARY_SPEC: dict[str, Any] = {
    "kind": "rest",
    "name": "dispensary",
    "base_url": DISPENSARY_URL,
    "list_path": "/api/prescriptions",
    "item_path": "/api/prescriptions/{id}",
    "records_key": "data",
    "id_field": "id",
    # The patient is part of the text, not just a column. Without it a prescription
    # shares no searchable subject with the nurse's message or the chart, so the
    # pharmacy's own queue — the system holding the dose about to be handed over —
    # never appeared in an answer about the patient it belongs to. A source that is
    # connected, healthy, and invisible to every question is worse than a disconnected
    # one, because the coverage table reports it as searched.
    "text_fields": ["patient_mrn", "patient_name", "drug", "dose", "instructions",
                    "status", "dispense_status"],
    "author_field": "prescriber",
    "timestamp_field": "prescribed_at",
    "uri_field": "url",
    "annotate_path": "/api/annotations?prescription_id={id}",
    "delete_path": "/api/annotations/{id}",
    # What this system lets Walnut do, declared by the people who run it.
    #
    # The tiers are the point. They follow the DIRECTION OF RISK, not the kind of
    # write: placing a hold stops a dose from going out, which fails safe, so the
    # agent may do it alone; releasing a hold puts a dose back into a patient's hand,
    # which fails dangerous, so a human confirms. The agent can stop a dose. It
    # cannot start one. "Writes are dangerous" would have got this exactly backwards
    # — it is the *release* that needs the human, and it is the *hold* that must not
    # wait for one.
    "operations": {
        "place_hold": {
            "method": "POST",
            "path": "/api/holds",
            "tier": "INTERNAL",
            "undo": {"method": "DELETE", "path": "/api/holds/{id}"},
            "result_id_field": "id",
        },
        "release_hold": {
            "method": "DELETE",
            "path": "/api/holds/{id}",
            "tier": "GATED",
        },
    },
}


def _reachable(url: str, timeout: float = 1.5) -> bool:
    """Is the service up? Cheap, fails fast, never raises.

    A short timeout on purpose: the console must not hang for several seconds on
    startup because an optional internal system happens to be down.
    """
    try:
        import httpx

        return httpx.get(f"{url.rstrip('/')}/healthz", timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001 - unreachable is an answer, not an error
        return False


def register_internal_systems(registry: Any) -> list[str]:
    """Register the customer's internal systems. Returns the names registered.

    Called at startup. Anything that fails here is reported through the registry's
    normal conformance path, so a broken internal system appears on the Connectors
    screen with a reason rather than vanishing.
    """
    registered: list[str] = []

    # Registered whether or not it answers. Skipping an unreachable system would make
    # it disappear from the coverage table entirely — and a source that silently is
    # not there is indistinguishable from one that was searched and held nothing,
    # which is the exact confusion this product exists to remove. Registering it lets
    # conformance fail honestly, so it surfaces as "not searched, service unreachable"
    # with a link to the connector that fixes it.
    source = registry.add_spec(dict(DISPENSARY_SPEC))
    registered.append(source.name)

    return registered
