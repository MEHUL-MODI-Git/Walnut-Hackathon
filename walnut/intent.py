"""Turning a sentence into proposed actions.

"File a Linear issue about the dosing bug and tell the customer" has to become two
concrete `Action` objects aimed at real records, or it has to become a question. This
module does that, and it is deliberately the *weakest* component in the system rather
than the smartest one.

Three rules it lives by:

1. **It proposes; it never executes.** Everything it produces goes through
   `ActionExecutor` like any other action, so an intent parser that misreads a sentence
   cannot bypass the evidence check, the taint rule, or the human gate. The blast
   radius of a parsing mistake is a refused action, not a wrong write.

2. **It cannot invent justification.** Every proposed action is bound to evidence found
   in the brain. If the request references something the brain does not hold, no action
   is proposed — the parser has no way to construct one, because `Action` refuses to be
   built without `justified_by`.

3. **Ambiguity becomes a question, not a guess.** Two plausible targets, or a verb that
   maps to no declared operation, produces a clarification rather than a coin flip.
   An agent that resolves ambiguity silently is choosing to be confidently wrong at
   some rate, and nobody finds out which requests were the wrong ones.

**No model is involved.** Verb-to-operation mapping is a table, and operations come
from each adapter's own `capabilities()` rather than from a list here — so a newly
connected custom source becomes addressable in natural language the moment it declares
its operations, with no change to this file. A language model could be dropped in to
widen the phrasing it understands, and it still would not get to decide what is true or
what may be written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .brain import Brain, Fact
from .contract import Action, Adapter

__all__ = ["Intent", "ProposedAction", "parse_intent"]


# Verb phrases mapped to the operation name adapters declare. Ordered longest-first at
# match time so "reply to" wins over "reply", and "create issue" over "create".
# An optional app word is allowed to sit inside the phrase: people write "file a
# LINEAR issue", not "file an issue in Linear". Matching only the literal phrase
# silently dropped half of the most natural requests.
_MID = r"(?:\s+\w+){0,2}\s+"

_VERBS: dict[str, tuple[str, ...]] = {
    "create_issue": (rf"file\s+an?{_MID}(?:issue|ticket|bug)", r"file (?:an? )?(?:issue|ticket)",
                     rf"(?:open|raise|create|log)\s+an?{_MID}(?:issue|ticket)",
                     r"(?:open|raise|create|log) an? (?:issue|ticket)", r"track this"),
    "comment": (r"comment on", r"(?:leave|add|post) a comment", r"note on"),
    "post_reply": (r"reply (?:in|on|to) (?:the )?(?:thread|channel|slack|#\w+)",
                   r"respond in", r"post a reply", r"answer in"),
    "post_message": (r"post (?:in|to)", r"message the", r"announce in", r"tell the team"),
    "send_email": (r"email (?:the )?(?:customer|client|them)",
                   r"reply to (?:the )?(?:customer|client)", r"send an email",
                   r"(?:tell|notify) (?:the )?(?:customer|client)"),
    "save_draft": (r"draft an? (?:email|reply)", r"prepare an email"),
    "set_property": (rf"(?:update|change|fix|correct)\s+the{_MID}?(?:status|page|spec)",
                     r"(?:update|change|fix|correct) the (?:status|page|spec)",
                     r"mark the page"),
    "set_state": (r"reopen", r"close the issue", r"mark it done", r"change the state"),
    "add_label": (r"label it", r"add a label", r"tag it", r"flag it"),
    "add_reaction": (r"react with", r"add a reaction"),
    "assign": (r"assign (?:it )?to",),
    "annotate": (r"annotate", r"add a note to"),
}

# Which app a request means, when it names one.
_APP_WORDS: dict[str, tuple[str, ...]] = {
    "linear": ("linear", "ticket tracker", "issue tracker"),
    "github": ("github", "pr", "pull request", "the repo", "the code"),
    "slack": ("slack", "the channel", "the thread"),
    "notion": ("notion", "the spec", "the wiki", "the doc", "the page"),
    "email": ("email", "e-mail", "mail", "the customer", "the client"),
}

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "to", "in", "on", "for", "of", "about", "that",
    "this", "it", "is", "was", "with", "from", "please", "can", "you", "then", "also",
})


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """One action the parser believes was asked for, and why it thinks so."""

    action: Action
    matched_phrase: str
    evidence: tuple[Fact, ...]

    def explain(self) -> str:
        return (
            f"{self.action.app}.{self.action.operation} "
            f"(from \"{self.matched_phrase}\") justified by "
            + ", ".join(f.node_id for f in self.evidence)
        )


@dataclass
class Intent:
    """What the parser made of a request."""

    raw: str
    proposed: list[ProposedAction] = field(default_factory=list)
    clarifications: list[str] = field(default_factory=list)
    """Questions that must be answered before anything can be proposed."""

    @property
    def actions(self) -> list[Action]:
        return [p.action for p in self.proposed]

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarifications) and not self.proposed

    def render(self) -> str:
        lines = [f'Request: "{self.raw}"', ""]
        if self.proposed:
            lines.append("Proposed:")
            lines.extend(f"  · {p.explain()}" for p in self.proposed)
        if self.clarifications:
            lines.append("Needs clarification:")
            lines.extend(f"  ? {c}" for c in self.clarifications)
        if not self.proposed and not self.clarifications:
            lines.append("Nothing actionable found in this request.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------


def _find_operations(text: str, available: dict[str, set[str]]) -> list[tuple[str, str]]:
    """Which operations does this text ask for? Returns (operation, matched phrase).

    Only operations some connected adapter actually declares are considered, so the
    vocabulary grows automatically when a new source is connected and never contains
    something no adapter can perform.
    """
    declared = {op for ops in available.values() for op in ops}
    hits: list[tuple[str, str, int]] = []
    lowered = text.lower()

    for operation, patterns in _VERBS.items():
        if operation not in declared:
            continue
        for pattern in patterns:
            for m in re.finditer(pattern, lowered):
                hits.append((operation, m.group(0), m.start()))

    # Longest phrase wins where two overlap: "reply to the customer" is an email, not
    # a Slack reply, and matching the shorter phrase first would send it to the wrong
    # system entirely.
    hits.sort(key=lambda h: (h[2], -len(h[1])))
    seen_positions: list[tuple[int, int]] = []
    chosen: list[tuple[str, str]] = []
    for operation, phrase, position in hits:
        span = (position, position + len(phrase))
        if any(s[0] < span[1] and span[0] < s[1] for s in seen_positions):
            continue
        seen_positions.append(span)
        chosen.append((operation, phrase))
    return chosen


def _apps_named(text: str) -> list[str]:
    lowered = text.lower()
    return [
        app for app, words in _APP_WORDS.items()
        if any(w in lowered for w in words)
    ]


def _nearest_app(text: str, phrase: str, candidates: list[str]) -> str | None:
    """Which candidate app is named closest to this phrase?

    "file a Linear issue and comment on the PR" names both apps, so asking only
    "is this app mentioned anywhere?" leaves every operation ambiguous and the whole
    request collapses into questions. Proximity resolves it the way a reader does.
    """
    lowered = text.lower()
    anchor = lowered.find(phrase.lower())
    if anchor < 0:
        return None

    best, best_distance = None, 10**6
    for app in candidates:
        for word in _APP_WORDS.get(app, ()):
            position = lowered.find(word)
            while position >= 0:
                distance = abs(position - anchor)
                if distance < best_distance:
                    best, best_distance = app, distance
                position = lowered.find(word, position + 1)
    # Only trust proximity when the app word is actually near the verb.
    return best if best_distance <= 40 else None


def _search_terms(text: str) -> list[str]:
    """Content words that might name the thing being talked about."""
    words = re.findall(r"[A-Za-z][\w\-]{2,}|[A-Z]{2,5}-\d+|#\d+", text)
    terms = [w for w in words if w.lower() not in _STOPWORDS]
    # Identifiers first — "MED-412" is a far better retrieval key than "dosing".
    terms.sort(key=lambda w: 0 if re.match(r"^[A-Z]{2,5}-\d+$|^#\d+$", w) else 1)
    return terms[:6]


def parse_intent(
    request: str,
    brain: Brain,
    adapters: dict[str, Adapter],
    *,
    evidence_hint: list[Fact] | None = None,
) -> Intent:
    """Read a request and propose actions, or ask for clarification.

    `evidence_hint` lets a caller supply the evidence already on screen — the facts
    from an answer the user is looking at when they type the request. Without it the
    parser retrieves from the brain, which is less precise, because a sentence like
    "file an issue about this" contains no retrievable subject at all.
    """
    intent = Intent(raw=request)

    available: dict[str, set[str]] = {}
    for name, adapter in adapters.items():
        try:
            available[name] = set(adapter.capabilities().operations)
        except Exception:  # noqa: BLE001 - an adapter that cannot describe itself is skipped
            continue

    operations = _find_operations(request, available)
    if not operations:
        intent.clarifications.append(
            "No recognised action in that request. Try naming what to do — file an "
            "issue, comment on the PR, reply in Slack, update the page, email the "
            f"customer. Available: {', '.join(sorted({o for ops in available.values() for o in ops}))}"
        )
        return intent

    # Evidence: what the user was looking at, else retrieve it.
    evidence: list[Fact] = list(evidence_hint or [])
    if not evidence:
        for term in _search_terms(request):
            for fact in brain.search(term, limit=4):
                if fact not in evidence:
                    evidence.append(fact)
            if len(evidence) >= 4:
                break

    if not evidence:
        intent.clarifications.append(
            "Nothing in the brain matches that request, so there is no evidence to "
            "justify an action. Ask a question first, or name a specific record."
        )
        return intent

    named = _apps_named(request)

    # Retrieval that returns nothing from the app the user named produces a useless
    # "no record to act on" question. Top it up from that app directly.
    for app in named:
        if any(f.app == app for f in evidence):
            continue
        for term in _search_terms(request):
            hits = [f for f in brain.search(term, limit=8) if f.app == app]
            if hits:
                evidence.append(hits[0])
                break

    for operation, phrase in operations:
        candidates = [app for app, ops in available.items() if operation in ops]
        if named:
            narrowed = [a for a in candidates if a in named]
            if narrowed:
                candidates = narrowed

        if not candidates:
            intent.clarifications.append(
                f'No connected app can perform "{phrase}".'
            )
            continue

        if len(candidates) > 1:
            nearest = _nearest_app(request, phrase, candidates)
            if nearest is not None:
                candidates = [nearest]

        if len(candidates) > 1:
            intent.clarifications.append(
                f'"{phrase}" could mean {" or ".join(sorted(candidates))}. '
                "Name the app — a guess here writes to the wrong system."
            )
            continue

        app = candidates[0]
        target = _target_for(app, operation, evidence)
        if target is None:
            intent.clarifications.append(
                f'"{phrase}" needs a record in {app} to act on, and none of the '
                "retrieved evidence comes from there."
            )
            continue

        intent.proposed.append(
            ProposedAction(
                action=Action(
                    app=app,
                    operation=operation,
                    target=target,
                    payload=_payload_for(operation, request, evidence),
                    justified_by=tuple(f.node_id for f in evidence[:3]),
                    rationale=f'requested: "{request[:140]}"',
                ),
                matched_phrase=phrase,
                evidence=tuple(evidence[:3]),
            )
        )

    return intent


def _target_for(app: str, operation: str, evidence: list[Fact]) -> dict[str, object] | None:
    """Aim the action at a real record.

    Targets come from the evidence's own `locator`, which every adapter populates with
    the coordinates it needs to find that record again. Synthesising a target from a
    guessed id produces an action that fails at the API boundary with an error looking
    like a connector bug.
    """
    creating = operation in {"create_issue", "post_message", "send_email", "save_draft"}
    from_app = [f for f in evidence if f.app == app]

    if creating:
        # Nothing to aim at: these make a new record. Carry any same-app context we
        # have so the adapter can thread it (a channel, a repo) without guessing.
        return dict(from_app[0].pointer.locator) if from_app else {"id": "new"}

    if not from_app:
        return None
    return dict(from_app[0].pointer.locator)


def _payload_for(operation: str, request: str, evidence: list[Fact]) -> dict[str, object]:
    """What to write. Citations are included so the artefact carries its own evidence."""
    citations = "\n".join(f"· {f.cite()}" for f in evidence[:3])
    body = f"{request.strip()}\n\nEvidence:\n{citations}"

    if operation == "create_issue":
        return {"title": request.strip()[:110], "state": "Todo", "description": body}
    if operation in {"comment", "post_reply", "post_message"}:
        return {"body": body, "text": body}
    if operation in {"send_email", "save_draft"}:
        return {"subject": request.strip()[:80], "body": body}
    if operation == "set_property":
        return {"status": "Disputed"}
    if operation == "set_state":
        return {"state": "Todo"}
    if operation in {"add_label", "flag"}:
        return {"label": "walnut", "flag": "walnut"}
    if operation == "add_reaction":
        return {"emoji": "eyes"}
    return {"note": body}
