"""The standard response to a contradiction, with targets derived from real data.

Both the CLI demo and the web console need the same five-app plan: file it, tell the
engineer, close the loop, repair the stale record, reply to the customer. Writing that
plan twice guarantees the two drift; hard-coding fixture ids into it guarantees the
whole thing breaks the moment the seed data changes.

So the plan is computed. Each step names the app and the operation — which are fixed
product decisions — and then asks the brain which record to aim at. If the brain holds
no suitable target for a step, that step is dropped rather than pointed at an id that
does not exist, because an action against a non-existent target fails at the API
boundary with an error that looks like a connector bug.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .brain import Brain, Fact
from .contradiction import ClaimStatus, Conflict
from .targeting import target_for

__all__ = ["build_plan"]


def _pick(brain: Brain, app: str, *, prefer: list[str] | None = None) -> Fact | None:
    """Choose a plausible target record in `app`.

    Preference order: a fact named in the conflict itself, then one whose text matches
    a hint, then the most recent record from that app. Never returns something from a
    different app, because acting on the wrong system is worse than not acting.
    """
    candidates = brain.by_app(app)
    if not candidates:
        return None
    for hint in prefer or []:
        for fact in candidates:
            if hint.lower() in fact.text.lower():
                return fact
    return max(
        candidates,
        key=lambda f: f.occurred_at or f.pointer.retrieved_at,
    )


def _phrasing(conflict: Conflict) -> dict[str, str]:
    """What to actually say, given the SHAPE of the disagreement.

    The first version of this file had one set of sentences: "the status claim is
    unsupported", "the fix is not yet live". Fine for two systems disagreeing about
    whether a feature shipped — and nonsense on an approval card about a patient,
    where the disagreement is that one system holds a fact another one is missing.
    A human reading "correcting our earlier note — the fix is not yet live" above a
    patient's name learns nothing and trusts the tool less.

    So the wording follows the shape the detector already classified. Still no model,
    still no generated prose: two fixed vocabularies, chosen by a typed value, with
    the apps and subject substituted in.
    """
    left, right = conflict.left, conflict.right
    absence = frozenset({left.status, right.status}) == frozenset(
        {ClaimStatus.ABSENT, ClaimStatus.PRESENT}
    )
    # Name the sides by what they are asserting, not by which arrived first.
    missing = left if left.status is ClaimStatus.ABSENT else right
    reporting = right if missing is left else left

    if absence:
        return {
            "title": (f"Reconcile {conflict.subject}: {missing.fact.app} holds nothing "
                      f"on this; {reporting.fact.app} reports it"),
            "reply": (f"{conflict.subject}: {reporting.fact.app} reports something the "
                      f"{missing.fact.app} record does not hold. Evidence attached — "
                      f"please confirm before the next appointment."),
            "email_subject": f"Query on {conflict.subject}",
            "email_body": (
                f"Our record for {conflict.subject} holds nothing on this, but it has "
                f"been reported elsewhere. Could you confirm what you have on file? "
                f"Evidence is attached; nothing has been changed on our side."
            ),
        }

    return {
        "title": f"Contradiction on {conflict.subject} — status claim is unsupported",
        "reply": f"Confirmed: {conflict.subject} is not complete. Evidence attached.",
        "email_subject": f"Re: {conflict.subject}",
        "email_body": "Correcting our earlier note — the fix is not yet live.",
    }


def _queued_prescription(
    brain: Brain, subject: str, adapters: dict[str, Any] | None
) -> Fact | None:
    """A prescription for this subject that has not been handed over yet.

    Deliberately knows nothing about the word "dispensary". Any connected source that
    names the subject, reports the dose as still queued, and declares `place_hold` in
    its own capabilities participates — which is what makes a custom source a first
    class part of the plan rather than a special case bolted beside it.

    The capability is checked against the live adapter, not assumed. A step aimed at
    an operation the source does not have raises at the API boundary, after earlier
    steps have already written to other systems, and reads to the user as a connector
    bug rather than as a planning mistake.
    """
    if not adapters:
        return None
    holders = {
        app for app, adapter in adapters.items()
        if "place_hold" in adapter.capabilities().operations
    }
    candidates = [
        fact
        for app in sorted(holders)
        for fact in brain.by_app(app)
        if subject.lower() in fact.text.lower() and "queued" in fact.text.lower()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.occurred_at or f.pointer.retrieved_at)


def build_plan(
    conflict: Conflict,
    brain: Brain,
    *,
    context: dict[str, Any] | None = None,
    adapters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The five-app response, aimed at records that actually exist.

    Targets are translated from each fact's own locator into the shape that app's
    adapter actually reads. Emitting `{'id': ...}` everywhere worked against
    fixtures and would have raised KeyError on the first live connection — after
    earlier steps in the plan had already written to other systems.
    """
    subject = conflict.subject
    says = _phrasing(conflict)
    involved = {conflict.left.fact.app: conflict.left.fact,
                conflict.right.fact.app: conflict.right.fact}

    def pick_fact(app: str, prefer: list[str]) -> Fact | None:
        # A record already implicated in the conflict is the best target available:
        # it is provably about the subject, not merely adjacent to it.
        return involved.get(app) or _pick(brain, app, prefer=prefer)

    steps: list[dict[str, Any]] = []

    # 1. File it as tracked work. This one creates rather than updates, so it needs no
    #    existing target and is always safe to include.
    # Stable across processes. Python's built-in hash() is randomised per interpreter
    # by PYTHONHASHSEED, so using it here would give the same contradiction a different
    # issue id on every run — which breaks idempotent seeding and makes two runs of the
    # demo disagree for no reason a viewer could understand.
    slug = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:6].upper()

    linear_target = target_for("linear", "create_issue", [], context=context)
    steps.append({
        "app": "linear", "operation": "create_issue",
        # Fixtures address records by id; a live Linear needs a team id, which no
        # locator can carry because it is a property of the connection, not the record.
        "target": linear_target or {"id": f"WALNUT-{slug}"},
        "payload": {"title": says["title"], "state": "Todo"},
        "rationale": "File the contradiction as tracked work, carrying its evidence chain.",
    })

    if (gh := pick_fact("github", [subject, "fix", "pr"])) is not None:
        steps.append({
            "app": "github", "operation": "comment",
            "target": target_for("github", "comment", [gh], context=context) or {},
            "payload": {"body": f"Walnut: {subject} is cited as complete elsewhere, "
                                f"but this is still open. Evidence attached."},
            "rationale": "Tell the engineer where the false claim is being made.",
        })

    if (sl := pick_fact("slack", ["support", "customer", "still"])) is not None:
        steps.append({
            "app": "slack", "operation": "post_reply",
            "target": target_for("slack", "post_reply", [sl], context=context) or {},
            "payload": {"text": says["reply"]},
            "rationale": "Close the loop with whoever raised it.",
        })

    if (nt := pick_fact("notion", [subject, "spec", "status"])) is not None:
        steps.append({
            "app": "notion", "operation": "set_property",
            "target": target_for("notion", "set_property", [nt], context=context) or {},
            "payload": {"status": "Disputed"},
            "rationale": "Repair the stale record that caused the confusion.",
        })

    # 2. Stop the dose, if a dose is about to go out.
    #
    # This step is conditional on three things being true at once, and it is dropped
    # unless all three hold: the customer has connected an internal dispensing system,
    # that system holds a prescription for this subject, and that prescription has not
    # been handed over yet. A hold placed on a script that was dispensed last Tuesday
    # is theatre, and a hold on the wrong patient's script is harm.
    #
    # It is placed WITHOUT waiting for a human because it fails safe: the worst case
    # is a dose delayed while a clinician checks a chart. Releasing it is gated, for
    # the same reason in the opposite direction. See DISPENSARY_SPEC for the tiers.
    if (rx := _queued_prescription(brain, subject, adapters)) is not None:
        rx_id = rx.pointer.locator.get("id") or rx.node_id.rsplit(":", 1)[-1]
        steps.append({
            "app": rx.app, "operation": "place_hold",
            "target": {"id": rx_id},
            "payload": {
                "prescription_id": rx_id,
                "reason": f"Walnut: contradiction on {subject} — "
                          f"{conflict.left.fact.app} and {conflict.right.fact.app} "
                          f"disagree. Verify before dispensing.",
                "placed_by": "walnut",
            },
            "rationale": "Hold the queued dose until a clinician has seen the "
                         "disagreement. Stopping a dose fails safe; releasing it does not.",
        })

    if (em := pick_fact("email", ["customer", "hospital", "re:"])) is not None:
        steps.append({
            "app": "email", "operation": "send_email",
            "target": target_for("email", "send_email", [em], context=context) or {},
            "payload": {"subject": says["email_subject"], "body": says["email_body"]},
            "rationale": "Reply to the customer. Customer-facing, so it must be gated.",
        })

    # A step with an empty target cannot be performed. Dropping it is better than
    # proposing an action that will fail at the API boundary looking like a bug.
    return [st for st in steps if st["target"]]
