"""Turning scattered records into an answer a person would actually want.

Grouping results by which app they came from is how the *system* thinks. Nobody asks
"what does Slack know about this patient" — they ask about the patient, and the fact that
one line came from a chat message and the next from a database is an implementation
detail they should be able to check but should not have to read.

So this organises an `Assembly` by **meaning** rather than by source: who they are, what
they are on, what is flagged, what has happened recently. Each line carries its citation,
but the citation is folded away until someone wants it. The receipts are always there;
they are just not the content.

**No model is involved.** Nothing here writes prose — every string rendered is either a
label this module supplies or text lifted verbatim from a record, and each carries the
fact it came from. A summary that *generates* sentences would be a summary that can
invent them, and there is no way to tell a well-phrased invention from a true one. The
grouping is deterministic; the words are the source's own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Briefing", "Line", "Section", "build_briefing"]


@dataclass(frozen=True, slots=True)
class Line:
    """One statement, and the record it came from."""

    label: str
    value: str
    fact: Any
    flag: str = ""
    """"warn" when this line is part of a contradiction."""


@dataclass
class Section:
    title: str
    lines: list[Line] = field(default_factory=list)


@dataclass
class Briefing:
    subject: str
    headline: str = ""
    """One identifying line — who or what this is. Lifted from a record, never written."""

    headline_fact: Any = None
    conflicts: list[Any] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    timeline: list[Line] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.sections and not self.timeline


# Fields worth pulling out of a structured record, in the order a reader wants them.
# The key is what we call it; the pattern finds it in the record's own text.
_FIELDS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("Conditions", "problem", re.compile(
        r"(?:problem[_ ]list|conditions?)\s*[:\-]?\s*([^\n|]{3,160})", re.I)),
    ("Medications", "meds", re.compile(
        r"(?:medications?|meds|prescri\w+)\s*[:\-]?\s*([^\n|]{3,160})", re.I)),
    ("Allergies", "allergy", re.compile(
        r"allergies?\s*[:\-]?\s*([^\n|]{2,120})", re.I)),
)

# A record asserting a field is empty. Same vocabulary the contradiction detector uses.
_ABSENT = re.compile(r"\b(?:none\s+(?:recorded|known)|nil|not\s+recorded|nkda)\b", re.I)


def _first_sentence(text: str, limit: int = 160) -> str:
    cleaned = " ".join(text.split())
    cut = re.split(r"(?<=[.!?])\s", cleaned, maxsplit=1)[0]
    return cut[:limit].rstrip(" ,;—-")


# Columns a database-shaped record may carry, and what a clinician calls them. Read
# from the structured row rather than matched out of prose: the adapter concatenates
# column VALUES into the text with no labels, so pattern-matching the text finds
# nothing. The row is right there on the fact; use it.
_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Age / sex", ("dob", "sex")),
    ("Conditions", ("problem_list", "conditions", "diagnosis")),
    ("Medications", ("medications", "meds", "drug", "prescription")),
    ("Allergies", ("allergies", "allergy")),
    ("Site", ("site", "clinic", "location")),
)


def _structured_lines(facts: list[Any]) -> list[Line]:
    """Pull named fields out of database-shaped records.

    Prefers the structured row (`Evidence.raw`) and falls back to matching the text,
    so a source that supplies no row still contributes what can be read from prose.
    """
    out: list[Line] = []
    seen: set[str] = set()

    for fact in facts:
        row = {k.lower(): v for k, v in (getattr(fact, "raw", None) or {}).items()}
        for label, keys in _COLUMNS:
            if label in seen:
                continue
            parts = [str(row[k]).strip() for k in keys if row.get(k)]
            if not parts:
                continue
            value = " · ".join(parts)[:180]
            seen.add(label)
            out.append(Line(label, value, fact,
                            flag="warn" if _ABSENT.search(value) else ""))

    for fact in facts:
        for label, key, pattern in _FIELDS:
            if label in seen:
                continue
            match = pattern.search(fact.text)
            if not match:
                continue
            value = " ".join(match.group(1).split())[:160]
            if value:
                seen.add(label)
                out.append(Line(label, value, fact,
                                flag="warn" if _ABSENT.search(value) else ""))
    return out


def _lab_lines(facts: list[Any]) -> list[Line]:
    """Most recent result per test, so a six-month series reads as one line each."""
    latest: dict[str, Any] = {}
    for fact in facts:
        name = fact.text.split()[1] if len(fact.text.split()) > 1 else "result"
        stamp = fact.occurred_at or fact.pointer.retrieved_at
        if name not in latest or stamp > (
            latest[name].occurred_at or latest[name].pointer.retrieved_at
        ):
            latest[name] = fact
    return [
        Line("Latest result", _first_sentence(f.text, 110), f,
             flag="warn" if re.search(r"\b(low|high|abnormal|critical)\b", f.text, re.I) else "")
        for f in list(latest.values())[:4]
    ]


def build_briefing(assembly: Any, conflicts: list[Any] | None = None) -> Briefing:
    """Organise an assembled result into something a person would read top to bottom."""
    brief = Briefing(subject=assembly.query)
    conflicts = conflicts or []

    # The conflicts that actually concern this subject, not every conflict in the estate.
    node_ids = {f.node_id for f in assembly.facts}
    brief.conflicts = [
        c for c in conflicts
        if c.left.fact.node_id in node_ids or c.right.fact.node_id in node_ids
    ]

    structured_apps = ("ehr", "dispensary", "labs")
    record_facts = [f for f in assembly.facts if f.app in ("ehr", "dispensary")]
    lab_facts = [f for f in assembly.facts if f.app == "labs"]
    narrative = [f for f in assembly.facts if f.app not in structured_apps]

    if record_facts:
        row = {k.lower(): v for k, v in (getattr(record_facts[0], "raw", None) or {}).items()}
        bits = [str(row[k]) for k in ("full_name", "sex", "mrn", "site") if row.get(k)]
        brief.headline = " · ".join(bits) if bits else _first_sentence(
            record_facts[0].text, 120)
        brief.headline_fact = record_facts[0]
    elif assembly.facts:
        brief.headline_fact = assembly.facts[0]

    # 1. What the record says.
    lines = _structured_lines(record_facts)
    if lines:
        brief.sections.append(Section("On record", lines))

    # 2. What the instruments say.
    if lab_facts:
        brief.sections.append(Section("Results", _lab_lines(lab_facts)))

    # 3. Anything a person said that contradicts the record — promoted out of the
    #    timeline, because burying it in chronological order is how it got missed the
    #    first time.
    flagged = [
        Line("Reported, not on record", _first_sentence(c.right.fact.text, 180),
             c.right.fact, flag="warn")
        for c in brief.conflicts
        if c.right.fact.app not in structured_apps
    ] + [
        Line("Reported, not on record", _first_sentence(c.left.fact.text, 180),
             c.left.fact, flag="warn")
        for c in brief.conflicts
        if c.left.fact.app not in structured_apps
    ]
    if flagged:
        brief.sections.append(Section("Needs attention", flagged[:3]))

    # 4. What has happened, newest first.
    flagged_ids = {ln.fact.node_id for ln in flagged}
    brief.timeline = [
        Line(
            (f.occurred_at or f.pointer.retrieved_at).strftime("%d %b"),
            _first_sentence(f.text, 190), f,
        )
        for f in sorted(
            (f for f in narrative if f.node_id not in flagged_ids),
            key=lambda f: f.occurred_at or f.pointer.retrieved_at,
            reverse=True,
        )[:8]
    ]
    return brief
