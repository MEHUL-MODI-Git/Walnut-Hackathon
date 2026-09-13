"""Contradiction detection across apps.

Notion says the feature shipped. Slack celebrated it nine days ago. Linear has the
issue in progress and GitHub has the fix sitting in an unmerged PR with no approvals.
All four are sincere; two of them are wrong; nobody noticed because no human reads all
four systems at once. That is the failure this project exists to catch.

Two rules govern what happens next, and they are the difference between a useful tool
and a confident liar:

1. **Surface, never silently rank.** The tempting move is to resolve the conflict by
   recency and move on. Recency is a heuristic, not evidence — the stale claim is
   sometimes the true one and the fresh claim is sometimes a mistake. A contradiction
   is reported with both sides cited, and a human or a stronger signal decides.

2. **Authority is not correctness.** A status field in the official spec is not more
   true than an open PR; it is merely more official. Weighting sources by their
   prestige is exactly the mistake this whole product was designed against.

Detection is deliberately shallow and deterministic — lexical status extraction plus
subject linking. No model is involved, so it cannot hallucinate a conflict, and its
misses are inspectable rather than mysterious.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

from .brain import Brain, Fact

__all__ = ["Claim", "ClaimStatus", "Conflict", "detect_contradictions", "extract_subjects"]


class ClaimStatus(StrEnum):
    """What a piece of text asserts about the state of a thing."""

    COMPLETE = "complete"
    IN_FLIGHT = "in_flight"
    BLOCKED = "blocked"

    ABSENT = "absent"
    """A record asserts a thing is NOT there: "none recorded", "no known allergies"."""

    PRESENT = "present"
    """Another source reports that same thing IS there.

    Absent-versus-present is a different shape of disagreement from the status ladder,
    and the more dangerous one. A status conflict means two systems disagree about
    progress; an absence conflict means one system is missing something another one
    knows — which is precisely the failure a company brain exists to catch. It is also
    entirely general: "no known issues" against a reported issue is the same shape.
    """

    UNKNOWN = "unknown"


_COMPLETE = re.compile(
    r"\b(shipped|ship it|released|launched|live|deployed|merged|completed?|"
    r"done|resolved|closed|fixed)\b",
    re.I,
)
_IN_FLIGHT = re.compile(
    r"\b(in progress|in review|wip|open|todo|to do|backlog|triage|planned|"
    r"unstarted|not started|started|ongoing|"
    r"under review|pending|awaiting|draft)\b",
    re.I,
)
_BLOCKED = re.compile(
    r"\b(blocked|broken|failing|still (?:not|isn'?t|timing out|broken)|"
    # "regression pass/test/suite" is QA vocabulary, not a defect report. Matching it
    # read a completed QA issue as broken, which then outranked the real contradiction.
    r"regressed|regression(?!\s+(?:pass|test|suite|run|testing))|"
    r"does ?n[o']t work|cannot|can'?t)\b",
    re.I,
)
# "none recorded", "no known allergies", "nil", "not documented" — a record stating
# that a field is empty. Deliberately narrow: this must fire on a structured assertion
# of absence, not on any sentence containing the word "no".
_ABSENT = re.compile(
    r"\b(?:none\s+(?:recorded|known|documented|reported)"
    r"|no\s+known\s+\w+"
    r"|nil\s+(?:known|recorded)"
    r"|not\s+(?:recorded|documented|on\s+file)"
    r"|allergies\s*[:=]\s*(?:none|nil|nkda)\b)",
    re.I,
)

# Someone reporting the thing the record says is absent.
_PRESENT = re.compile(
    r"\b(?:reports?|reported|reaction\s+to|allergic\s+to|came\s+out\s+in\s+a\s+rash"
    r"|flagging|flagged\s+(?:an?|that)|confirmed\s+\w+\s+allergy)\b",
    re.I,
)

_NEGATED_COMPLETE = re.compile(
    r"\b(not|isn'?t|was ?n'?t|never|no longer)\s+(?:\w+\s+){0,2}"
    r"(shipped|released|live|deployed|merged|done|fixed|resolved)\b",
    re.I,
)

# Subject keys the demo turns on: issue keys, PR references, and feature names.
_ISSUE_KEY = re.compile(r"\b([A-Z]{2,5}-\d{1,6})\b")
# Record identifiers (an MRN here) link a database row to the prose about it.
_RECORD_ID = re.compile(r"\b(MR-\d{3,6})\b", re.I)
_PR_REF = re.compile(r"(?:\bPR\s*#?|#)(\d{1,6})\b", re.I)
# A versioned product name: one or two words immediately before a version token.
# Deliberately NOT a hard-coded vocabulary — an earlier version listed the feature
# words of one seed corpus ("export|billing|auth|…") and silently found nothing at all
# when the data changed domain, which is the worst kind of failure: no error, no
# output, and a detector that looks like it is working.
#
# The first word before the version carries the identity, so "dosing engine v2" and
# "dosing v2" resolve to the same subject while "export v2" stays distinct from both.
_FEATURE = re.compile(
    r"\b((?:[a-z][a-z-]{2,18}\s+){1,2})(v\d+(?:\.\d+)?)\b", re.I
)

# Words that are never a product name, however often they precede a version number.
_NOT_A_FEATURE = frozenset({
    "the", "this", "that", "our", "their", "its", "a", "an", "and", "for", "with",
    "from", "into", "onto", "release", "version", "ship", "shipped", "using", "via",
    "to", "in", "on", "of", "is", "was", "are", "were", "we", "they", "it",
})


def extract_subjects(text: str) -> set[str]:
    """What things is this text about? Returns normalised subject keys.

    Kept narrow on purpose. A subject extractor that matches loosely produces
    contradictions between unrelated facts, and a false contradiction costs a human's
    attention — the scarcest resource in the whole system.
    """
    subjects: set[str] = set()
    subjects.update(m.group(1).upper() for m in _ISSUE_KEY.finditer(text))
    subjects.update(f"PR#{m.group(1)}" for m in _PR_REF.finditer(text))
    subjects.update(m.group(1).upper() for m in _RECORD_ID.finditer(text))
    for m in _FEATURE.finditer(text):
        # Take the FIRST non-stopword of the one or two words before the version.
        # A leftmost-greedy single capture grabs the preposition in "for dosing v2"
        # and then discards the whole match as a stopword — finding nothing.
        words = [w for w in m.group(1).lower().split() if w not in _NOT_A_FEATURE]
        if not words:
            continue
        name, version = words[0], m.group(2).lower()
        # The version is required. An UNVERSIONED feature word is a shared word, not a
        # shared referent: two messages both saying "dosing" are not talking about the
        # same thing in any sense a contradiction can be built on, and treating them as
        # though they were produced 303 conflicts from an 88-record corpus on the first
        # real run — noise wearing detection's clothes.
        if name not in _NOT_A_FEATURE:
            subjects.add(f"feature:{name}{version}")
    return subjects


# Most records carry an explicit status field. Reading it beats guessing from prose.
_STATUS_FIELD = re.compile(r"\b(?:state|status)\s*:\s*([A-Za-z][A-Za-z ]{0,18})", re.I)
# Text inside quotes is reported speech — someone describing a claim, often to dispute
# it ("the 'shipped' miscommunication"). Classifying on it inverts the meaning.
_QUOTED = re.compile(r"[\"'‘’“”]([^\"'‘’“”]{1,80})[\"'‘’“”]")


def _classify_prose(text: str) -> ClaimStatus:
    # Absence and presence are checked first: they are a stronger, more specific signal
    # than the status ladder, and a record can carry both kinds of vocabulary.
    if _ABSENT.search(text):
        return ClaimStatus.ABSENT
    if _PRESENT.search(text):
        return ClaimStatus.PRESENT
    if _NEGATED_COMPLETE.search(text):
        return ClaimStatus.BLOCKED
    if _BLOCKED.search(text):
        return ClaimStatus.BLOCKED
    # In-flight is checked before complete: "open" and "in review" are more specific
    # signals than a stray "closed" appearing elsewhere in a long body.
    if _IN_FLIGHT.search(text):
        return ClaimStatus.IN_FLIGHT
    if _COMPLETE.search(text):
        return ClaimStatus.COMPLETE
    return ClaimStatus.UNKNOWN


def classify(text: str) -> ClaimStatus:
    """What does this record assert about the state of its subject?

    **The structured status field wins.** Reading prose first was a real defect, not a
    theoretical one: a Linear issue in `state: Backlog` whose description ended "...
    before calling it fully resolved" was classified COMPLETE on the word "resolved",
    and then ranked as the top contradiction in the UI — a confident wrong answer with
    a citation attached, which is precisely the failure this product exists to prevent.

    Prose is the fallback for records that have no status field at all (chat messages,
    email). Quoted spans are stripped from it first, because reported speech usually
    means someone is describing a claim rather than making it — often to dispute it.
    """
    field = _STATUS_FIELD.search(text)
    if field is not None:
        status = _classify_prose(field.group(1).strip())
        if status is not ClaimStatus.UNKNOWN:
            return status

    return _classify_prose(_QUOTED.sub(" ", text))


@dataclass(frozen=True, slots=True)
class Claim:
    """One fact's assertion about one subject."""

    fact: Fact
    subject: str
    status: ClaimStatus
    is_primary: bool = False
    """True when this record *is* the subject rather than merely mentioning it.

    The Linear issue ENG-412 is authoritative about ENG-412's state. A different issue
    whose description says "once ENG-412 lands..." is not — it is a passing reference,
    and reading a status off it produces confident nonsense. Without this distinction
    the detector cheerfully reports that ENG-433 contradicts an email about ENG-412.
    """

    def render(self) -> str:
        mark = "" if self.is_primary else "  (mentions)"
        return (
            f"{self.status.value:>10}{mark}  {self.fact.text[:90]}\n"
            f"            {self.fact.cite()}"
        )


@dataclass(frozen=True, slots=True)
class Conflict:
    """Two claims about one subject that cannot both be true."""

    subject: str
    left: Claim
    right: Claim
    explanation: str

    @property
    def apps(self) -> tuple[str, str]:
        return (self.left.fact.app, self.right.fact.app)

    def render(self) -> str:
        return (
            f"CONTRADICTION · {self.subject}\n"
            f"  {self.explanation}\n"
            f"  {self.left.render()}\n"
            f"  {self.right.render()}"
        )

    def evidence_ids(self) -> tuple[str, ...]:
        return (self.left.fact.node_id, self.right.fact.node_id)

    @property
    def id(self) -> str:
        """Stable identity for this exact conflict, not just its subject.

        Six distinct conflicts can share one subject. Addressing them by subject meant
        the console executed whichever happened to be first while showing the human a
        different one — so the evidence chain on screen and the evidence chain written
        into four external systems were not the same chain. That is the provenance
        break the whole product turns on, arriving through the UI.
        """
        material = f"{self.subject}|{self.left.fact.node_id}|{self.right.fact.node_id}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


# Which status pairs are genuinely incompatible. UNKNOWN conflicts with nothing —
# absence of a signal is not evidence of the opposite signal.
_INCOMPATIBLE: frozenset[frozenset[ClaimStatus]] = frozenset(
    {
        frozenset({ClaimStatus.COMPLETE, ClaimStatus.IN_FLIGHT}),
        frozenset({ClaimStatus.COMPLETE, ClaimStatus.BLOCKED}),
        # One system says the field is empty; another says it is not. The most
        # consequential disagreement a company brain can surface.
        frozenset({ClaimStatus.ABSENT, ClaimStatus.PRESENT}),
    }
)


def _is_primary(fact: Fact, subject: str) -> bool:
    """Is this record the subject itself, or just something that mentions it?

    Matched on the record's own identifier rather than its text: `linear:ENG-412` IS
    ENG-412; `linear:ENG-433` is not, however often it says the words.
    """
    ident = fact.node_id.split(":", 1)[-1].upper()
    if subject.startswith("PR#"):
        return ident.endswith(f"-{subject[3:]}")
    if subject.startswith("feature:"):
        # No record "is" a feature the way an issue is itself. The workable proxy is
        # title position: a page called "Dosing Engine v2 — Spec" is about dosing v2; a
        # Slack message mentioning it forty words in is not.
        #
        # Match the name and the version SEPARATELY rather than the concatenated
        # subject key. The key normalises "dosing engine v2" to "dosingv2" by dropping
        # the middle word, so searching for "dosingv2" in the text finds nothing and
        # every such record is misjudged as a passing mention — which silently reduced
        # the detector to only those records that happened to omit the middle word.
        head = fact.text[:80].lower()
        m = re.fullmatch(r"feature:(.+?)(v\d+(?:\.\d+)?)", subject)
        if m is None:
            return False
        name, version = m.group(1), m.group(2)
        return name in head and version in head
    return ident == subject


def detect_contradictions(
    brain: Brain, *, cross_app_only: bool = True, require_primary: bool = True
) -> list[Conflict]:
    """Find claims that cannot all be true.

    `cross_app_only` defaults to True because a disagreement inside one app is usually
    just a stale comment thread, whereas a disagreement *between* systems of record is
    the signal worth a human's attention — and is the thing no single dashboard can see.
    """
    claims: dict[str, list[Claim]] = {}
    for fact in brain._facts.values():  # noqa: SLF001 - Brain owns this index
        status = classify(fact.text)
        if status is ClaimStatus.UNKNOWN:
            continue
        for subject in extract_subjects(fact.text):
            claims.setdefault(subject, []).append(
                Claim(fact, subject, status, is_primary=_is_primary(fact, subject))
            )

    # One conflict per (subject, app-pair). Ten Slack messages disagreeing with one
    # Linear issue is ONE disagreement between Slack and Linear about one subject, not
    # ten findings. Emitting the cartesian product is how a detector with a genuine hit
    # buries it under its own output.
    seen: set[tuple[str, frozenset[str]]] = set()
    conflicts: list[Conflict] = []
    for subject, subject_claims in sorted(claims.items()):
        # Authoritative first, then most recent. Ordering by recency alone picks the
        # freshest passing mention as a pair's representative — which is how "the
        # export v2 spec contradicts PR #288" degrades into "this week's all-hands
        # agenda contradicts PR #281". Primacy is the stronger signal; recency only
        # breaks ties within it.
        ordered = sorted(
            subject_claims,
            key=lambda c: (
                c.is_primary,
                c.fact.occurred_at or c.fact.pointer.retrieved_at,
            ),
            reverse=True,
        )
        for i, left in enumerate(ordered):
            for right in ordered[i + 1 :]:
                if frozenset({left.status, right.status}) not in _INCOMPATIBLE:
                    continue
                if cross_app_only and left.fact.app == right.fact.app:
                    continue
                # At least one side must be authoritative for the subject. Two passing
                # mentions disagreeing is gossip, not a contradiction worth a human.
                if require_primary and not (left.is_primary or right.is_primary):
                    continue
                key = (subject, frozenset({left.fact.app, right.fact.app}))
                if key in seen:
                    continue
                seen.add(key)
                conflicts.append(
                    Conflict(
                        subject=subject,
                        left=left,
                        right=right,
                        explanation=(
                            f"{left.fact.app} asserts {left.status.value}; "
                            f"{right.fact.app} asserts {right.status.value}. "
                            "Both cannot hold. Not ranked automatically — recency and "
                            "source authority are heuristics, not evidence."
                        ),
                    )
                )

    # Strongest first: both sides authoritative outranks one side plus a mention.
    conflicts.sort(key=lambda c: -(int(c.left.is_primary) + int(c.right.is_primary)))
    return conflicts
