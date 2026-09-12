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
from .contradiction import Conflict
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


def build_plan(
    conflict: Conflict, brain: Brain, *, context: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The five-app response, aimed at records that actually exist.

    Targets are translated from each fact's own locator into the shape that app's
    adapter actually reads. Emitting `{'id': ...}` everywhere worked against
    fixtures and would have raised KeyError on the first live connection — after
    earlier steps in the plan had already written to other systems.
    """
    subject = conflict.subject
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
        "payload": {
            "title": f"Contradiction on {subject} — status claim is unsupported",
            "state": "Todo",
        },
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
            "payload": {"text": f"Confirmed: {subject} is not complete. Evidence attached."},
            "rationale": "Close the loop with whoever raised it.",
        })

    if (nt := pick_fact("notion", [subject, "spec", "status"])) is not None:
        steps.append({
            "app": "notion", "operation": "set_property",
            "target": target_for("notion", "set_property", [nt], context=context) or {},
            "payload": {"status": "Disputed"},
            "rationale": "Repair the stale record that caused the confusion.",
        })

    if (em := pick_fact("email", ["customer", "hospital", "re:"])) is not None:
        steps.append({
            "app": "email", "operation": "send_email",
            "target": target_for("email", "send_email", [em], context=context) or {},
            "payload": {"subject": f"Re: {subject}",
                        "body": "Correcting our earlier note — the fix is not yet live."},
            "rationale": "Reply to the customer. Customer-facing, so it must be gated.",
        })

    # A step with an empty target cannot be performed. Dropping it is better than
    # proposing an action that will fail at the API boundary looking like a bug.
    return [st for st in steps if st["target"]]
