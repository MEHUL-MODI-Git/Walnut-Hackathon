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

from typing import Any

from .brain import Brain, Fact
from .contradiction import Conflict

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


def build_plan(conflict: Conflict, brain: Brain) -> list[dict[str, Any]]:
    """The five-app response, aimed at records that actually exist."""
    subject = conflict.subject
    involved = {conflict.left.fact.app: conflict.left.fact,
                conflict.right.fact.app: conflict.right.fact}

    def target_for(app: str, prefer: list[str]) -> Fact | None:
        # A record already implicated in the conflict is the best target available:
        # it is provably about the subject, not merely adjacent to it.
        return involved.get(app) or _pick(brain, app, prefer=prefer)

    steps: list[dict[str, Any]] = []

    # 1. File it as tracked work. This one creates rather than updates, so it needs no
    #    existing target and is always safe to include.
    steps.append({
        "app": "linear", "operation": "create_issue",
        "target": {"id": f"WALNUT-{abs(hash(subject)) % 9000 + 1000}"},
        "payload": {
            "title": f"Contradiction on {subject} — status claim is unsupported",
            "state": "Todo",
        },
        "rationale": "File the contradiction as tracked work, carrying its evidence chain.",
    })

    if (gh := target_for("github", [subject, "fix", "pr"])) is not None:
        steps.append({
            "app": "github", "operation": "comment",
            "target": {"id": gh.pointer.locator.get("id", "")},
            "payload": {"body": f"Walnut: {subject} is cited as complete elsewhere, "
                                f"but this is still open. Evidence attached."},
            "rationale": "Tell the engineer where the false claim is being made.",
        })

    if (sl := target_for("slack", ["support", "customer", "still"])) is not None:
        steps.append({
            "app": "slack", "operation": "post_reply",
            "target": {"id": sl.pointer.locator.get("id", "")},
            "payload": {"text": f"Confirmed: {subject} is not complete. Evidence attached."},
            "rationale": "Close the loop with whoever raised it.",
        })

    if (nt := target_for("notion", [subject, "spec", "status"])) is not None:
        steps.append({
            "app": "notion", "operation": "set_property",
            "target": {"id": nt.pointer.locator.get("id", "")},
            "payload": {"status": "Disputed"},
            "rationale": "Repair the stale record that caused the confusion.",
        })

    if (em := target_for("email", ["customer", "hospital", "re:"])) is not None:
        steps.append({
            "app": "email", "operation": "send_email",
            "target": {"id": em.pointer.locator.get("id", "")},
            "payload": {"subject": f"Re: {subject}",
                        "body": "Correcting our earlier note — the fix is not yet live."},
            "rationale": "Reply to the customer. Customer-facing, so it must be gated.",
        })

    return steps
