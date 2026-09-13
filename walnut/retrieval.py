"""Assembling everything the company knows about one thing.

This is the product. Not the refusals, not the governance — those exist to make this
safe to point at a real company. The thing a customer is buying is that they ask about
a person, a feature, or a customer, and get back everything, from everywhere, with its
source attached, no matter how scattered it was.

Three problems stood between the brain and that promise, and all three were invisible
because the corpus is small enough that a flat keyword search *looks* like it works:

1. **Asking about a person returned nothing.** Slack stores an author as `p01`, GitHub
   as `sarah-k`, Notion as `S. Kim`. Identity resolution had been built and was never
   wired into retrieval, so "Sarah Kim" matched no record in any system — the single
   most obvious question a person would ask.

2. **The graph had no edges.** Facts were ingested and never linked, so it was a bag of
   cited records rather than a graph. Nothing could be traversed, so "what else is
   connected to this" had no answer.

3. **Nothing reported coverage.** A search that quietly read four of six sources looks
   identical to one that read all six. For a product whose promise is that nothing is
   lost, silently missing a source is the worst failure available — so every result
   carries which sources were searched and which had nothing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .brain import Brain, Fact
from .contradiction import extract_subjects
from .identity import Person, ResolutionReport

__all__ = ["Assembly", "SourceCoverage", "assemble", "link_by_subject", "normalise"]


def normalise(text: str) -> str:
    """Fold case and punctuation so `st anne` matches `St. Anne's`.

    Exact-substring matching returned ZERO records for a customer who appears fifteen
    times across three systems, purely because of an apostrophe and a full stop. For a
    product whose promise is that nothing is lost, losing a subject to punctuation is
    the most embarrassing failure available.
    """
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


@dataclass(frozen=True, slots=True)
class SourceCoverage:
    """Whether a source was searched, and what it had. The 'nothing is lost' receipt."""

    app: str
    searched: bool
    hits: int
    total_held: int

    @property
    def state(self) -> str:
        if not self.searched:
            return "not searched"
        return "found" if self.hits else "nothing"


@dataclass
class Assembly:
    """Everything the company knows about one subject, from every source."""

    query: str
    person: Person | None = None
    aliases: list[str] = field(default_factory=list)
    """The handles the query resolved through — why a Slack message by `p01` came back
    when you asked about Sarah Kim."""

    by_app: dict[str, list[Fact]] = field(default_factory=dict)
    coverage: list[SourceCoverage] = field(default_factory=list)
    related_subjects: list[str] = field(default_factory=list)

    @property
    def facts(self) -> list[Fact]:
        return [f for facts in self.by_app.values() for f in facts]

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.by_app.values())

    @property
    def apps_with_hits(self) -> list[str]:
        return sorted(a for a, f in self.by_app.items() if f)

    def timeline(self) -> list[Fact]:
        """Everything, oldest first, across every system — the story in order."""
        return sorted(
            self.facts,
            key=lambda f: f.occurred_at or f.pointer.retrieved_at or datetime.min,
        )

    def render(self) -> str:
        lines = [f'Everything about "{self.query}"']
        if self.aliases:
            lines.append(f"  resolved through: {', '.join(self.aliases)}")
        lines.append(f"  {self.total} records across {len(self.apps_with_hits)} systems")
        for cov in self.coverage:
            mark = {"found": "*", "nothing": "-", "not searched": "!"}[cov.state]
            lines.append(f"   {mark} {cov.app:12} {cov.hits:>3} of {cov.total_held}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------


def link_by_subject(brain: Brain, *, max_edges: int = 4000) -> int:
    """Connect facts that talk about the same specific thing.

    Without this the brain holds facts and no relationships, which is a database with
    extra steps. Edges are built on *specific* shared referents only — issue keys, PR
    numbers, versioned feature names — never on a shared common word, because an edge
    between every pair of records mentioning "dosing" is noise that makes traversal
    useless rather than informative.
    """
    by_subject: dict[str, list[str]] = defaultdict(list)
    for fact in brain._facts.values():  # noqa: SLF001 - Brain owns this index
        for subject in extract_subjects(fact.text):
            by_subject[subject].append(fact.node_id)

    made = 0
    for subject, node_ids in by_subject.items():
        # A subject mentioned by half the corpus is a topic, not a link. Skip it rather
        # than emit a quadratic blast of edges nobody can read.
        if len(node_ids) > 25:
            continue
        for i, left in enumerate(node_ids):
            for right in node_ids[i + 1:]:
                if made >= max_edges:
                    return made
                brain.relate(left, right, f"shares:{subject}")
                made += 1
    return made


def _person_aliases(query: str, identities: ResolutionReport | None) -> tuple[Person | None, list[str]]:
    """Every handle the named person is known by, across every system."""
    if identities is None:
        return None, []
    person = identities.find(query)
    if person is None:
        return None, []

    aliases = {person.canonical_name}
    for ident in person.identities:
        aliases.add(ident.handle)
        if ident.display_name:
            aliases.add(ident.display_name)
        if ident.email:
            aliases.add(ident.email)
            aliases.add(ident.email.split("@")[0])
    return person, sorted(a for a in aliases if a)


def assemble(
    brain: Brain,
    query: str,
    *,
    identities: ResolutionReport | None = None,
    limit_per_app: int = 25,
) -> Assembly:
    """Everything the company knows about `query`, from every connected source."""
    assembly = Assembly(query=query)
    needle = query.strip().lower()
    if not needle:
        return assembly

    # A person is asked about by name and stored under a different handle in every
    # system. Expanding the query through resolved identities is what makes the
    # obvious question work at all.
    person, aliases = _person_aliases(query, identities)
    assembly.person = person
    assembly.aliases = aliases

    terms = {needle} | {a.lower() for a in aliases}
    # Subject keys let "MED-412" find records that name it without the exact string
    # appearing in the text we matched on.
    terms |= {s.lower() for s in extract_subjects(query)}

    all_apps = sorted({f.app for f in brain._facts.values()})  # noqa: SLF001
    hits: dict[str, list[Fact]] = {app: [] for app in all_apps}

    norm_terms = {normalise(t) for t in terms if normalise(t)}
    for fact in brain._facts.values():  # noqa: SLF001
        haystack = normalise(f"{fact.text} {fact.author or ''} {fact.node_id}")
        subjects = {normalise(s) for s in extract_subjects(fact.text)}
        if any(t in haystack for t in norm_terms) or (subjects & norm_terms):
            if len(hits[fact.app]) < limit_per_app:
                hits[fact.app].append(fact)

    for app in all_apps:
        hits[app].sort(
            key=lambda f: f.occurred_at or f.pointer.retrieved_at or datetime.min,
            reverse=True,
        )

    assembly.by_app = {a: v for a, v in hits.items() if v}
    assembly.coverage = [
        SourceCoverage(app=app, searched=True, hits=len(hits[app]),
                       total_held=len(brain.by_app(app)))
        for app in all_apps
    ]

    # What else these records talk about — the traversal that makes it a graph.
    nearby: dict[str, int] = defaultdict(int)
    for fact in assembly.facts:
        for subject in extract_subjects(fact.text):
            if subject.lower() not in terms:
                nearby[subject] += 1
    assembly.related_subjects = [
        s for s, _ in sorted(nearby.items(), key=lambda kv: -kv[1])[:8]
    ]

    return assembly
