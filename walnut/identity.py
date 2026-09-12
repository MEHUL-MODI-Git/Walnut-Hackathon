"""Cross-app identity resolution — the thing that makes it a company brain.

Sarah Kim is `@sarah` in Slack, `sarah-k` on GitHub, `Sarah Kim` in Linear, `S. Kim`
in Notion, and `sarah.kim@meridian.dev` in email. Five systems, five identifiers, one
human. Until those are the same node, the graph is five disconnected graphs sharing a
database, and no question that crosses an app boundary can be answered.

**Over-merging is worse than under-merging.** Fusing two different real people is not a
degraded answer, it is a data-protection incident: it can expose one person's
information under another person's identity. So this module refuses to guess. Matches
land in one of three bands:

    CERTAIN    a shared verified identifier (email). Merge.
    LIKELY     strong but inferential (name match + corroborating handle). Merge, logged.
    UNCERTAIN  plausible and unproven. **Never merged — raised for a human.**

The uncertain band routing to a person rather than to a coin flip is the whole design.
A system that auto-merges its uncertain band has simply chosen to be confidently wrong
at a measurable rate.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Identity",
    "MatchBand",
    "Person",
    "ResolutionReport",
    "resolve_identities",
]


class MatchBand(StrEnum):
    CERTAIN = "certain"
    LIKELY = "likely"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class Identity:
    """One app's handle for someone."""

    app: str
    handle: str
    display_name: str = ""
    email: str = ""

    def key(self) -> str:
        return f"{self.app}:{self.handle}"


@dataclass
class Person:
    """A resolved human: one node, many identities."""

    person_id: str
    canonical_name: str
    identities: list[Identity] = field(default_factory=list)
    band: MatchBand = MatchBand.CERTAIN
    evidence: list[str] = field(default_factory=list)
    """Why we believe these are the same person. Shown to a human on review."""

    @property
    def apps(self) -> list[str]:
        return sorted({i.app for i in self.identities})

    def handle_in(self, app: str) -> str | None:
        for i in self.identities:
            if i.app == app:
                return i.handle
        return None

    def render(self) -> str:
        pairs = ", ".join(f"{i.app}:{i.handle}" for i in sorted(self.identities, key=lambda x: x.app))
        return f"{self.canonical_name} [{self.band.value}] — {pairs}"


@dataclass
class ResolutionReport:
    resolved: list[Person] = field(default_factory=list)
    needs_review: list[tuple[Identity, Identity, str]] = field(default_factory=list)
    """Uncertain pairs. These are H4-gate items, not failures."""

    def summary(self) -> dict[str, int]:
        return {
            "people": len(self.resolved),
            "identities": sum(len(p.identities) for p in self.resolved),
            "cross_app": sum(1 for p in self.resolved if len(p.apps) > 1),
            "needs_human_review": len(self.needs_review),
        }

    def find(self, name_fragment: str) -> Person | None:
        needle = _normalise(name_fragment)
        for p in self.resolved:
            if needle in _normalise(p.canonical_name):
                return p
        return None


# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Fold case, strip accents and punctuation. `S. Kim` and `s kim` must agree."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", stripped.lower()).strip()


def _name_tokens(name: str) -> list[str]:
    return [t for t in _normalise(name).split() if t]


def _initial_form_matches(a: str, b: str) -> bool:
    """Does `S. Kim` plausibly denote `Sarah Kim`?

    True when surnames agree and one side's forename is an initial of the other's.
    Surname agreement alone is deliberately NOT enough — there are a lot of Kims.
    """
    ta, tb = _name_tokens(a), _name_tokens(b)
    if len(ta) < 2 or len(tb) < 2 or ta[-1] != tb[-1]:
        return False
    fa, fb = ta[0], tb[0]
    if fa == fb:
        return True
    return (len(fa) == 1 and fb.startswith(fa)) or (len(fb) == 1 and fa.startswith(fb))


def _handle_supports(handle: str, name: str) -> bool:
    """Does a handle like `sarah-k` corroborate the name `Sarah Kim`?"""
    tokens = _name_tokens(name)
    if not tokens:
        return False
    h = _normalise(handle.replace("-", " ").replace("_", " ").replace(".", " "))
    parts = [p for p in h.split() if p]
    if not parts:
        return False
    forename, surname = tokens[0], tokens[-1]
    # sarah-k  ->  ["sarah", "k"]
    if parts[0] == forename and len(parts) > 1 and surname.startswith(parts[1]):
        return True
    # A bare forename handle ("sarah") corroborates only in combination with a name
    # match, which the caller already requires. Two Sarahs trip the ambiguity guard.
    if parts == [forename]:
        return True
    # skim / sarahk
    joined = "".join(parts)
    return joined in {forename + surname, forename[0] + surname, forename + surname[0]}


def resolve_identities(identities: list[Identity]) -> ResolutionReport:
    """Cluster app-specific handles into people.

    Runs in two passes so that the strongest signal always wins: every verified
    email link is applied before any name-based inference is considered. Doing it the
    other way round lets a weak name match capture an identity that a verified email
    would have placed elsewhere.
    """
    report = ResolutionReport()
    clusters: list[Person] = []
    by_email: dict[str, Person] = {}

    # Pass 1 — CERTAIN: shared verified email address.
    for ident in identities:
        email = ident.email.strip().lower()
        if not email:
            continue
        person = by_email.get(email)
        if person is None:
            person = Person(
                person_id=f"p:{email}",
                canonical_name=ident.display_name or ident.handle,
                identities=[],
                band=MatchBand.CERTAIN,
                evidence=[f"verified email {email}"],
            )
            by_email[email] = person
            clusters.append(person)
        person.identities.append(ident)
        if ident.display_name and len(ident.display_name) > len(person.canonical_name):
            person.canonical_name = ident.display_name

    # Pass 2 — LIKELY / UNCERTAIN for identities with no email to anchor them.
    for ident in identities:
        if ident.email.strip():
            continue

        best: Person | None = None
        best_reason = ""
        ambiguous = False

        for person in clusters:
            if ident.app in person.apps:
                continue  # one identity per app per person
            name_match = _initial_form_matches(ident.display_name, person.canonical_name)
            handle_match = _handle_supports(ident.handle, person.canonical_name)
            if not (name_match or handle_match):
                continue
            reason = (
                f"name {ident.display_name!r} ~ {person.canonical_name!r}"
                if name_match
                else f"handle {ident.handle!r} ~ {person.canonical_name!r}"
            )
            if best is not None:
                ambiguous = True  # two candidates — a human decides, not us
                break
            best, best_reason = person, reason

        if best is None:
            clusters.append(
                Person(
                    person_id=f"p:{ident.key()}",
                    canonical_name=ident.display_name or ident.handle,
                    identities=[ident],
                    band=MatchBand.CERTAIN,
                    evidence=["singleton — no cross-app match found"],
                )
            )
            continue

        if ambiguous:
            report.needs_review.append(
                (ident, best.identities[0], "matches more than one person")
            )
            continue

        # A name match corroborated by a matching handle is LIKELY; either alone is
        # UNCERTAIN and goes to a human rather than into the graph.
        corroborated = _initial_form_matches(
            ident.display_name, best.canonical_name
        ) and _handle_supports(ident.handle, best.canonical_name)

        if corroborated:
            best.identities.append(ident)
            best.band = MatchBand.LIKELY if best.band is not MatchBand.CERTAIN else best.band
            best.evidence.append(best_reason + " (corroborated)")
        else:
            report.needs_review.append((ident, best.identities[0], best_reason))

    report.resolved = clusters
    return report
