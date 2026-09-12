"""The company brain: a graph of cited facts drawn from every connected app.

Semantica is doing the heavy lifting underneath — graph storage, W3C PROV-O
provenance, decision chains, temporal state, retraction tombstones. **Every call
into it lives in this one file.** That is deliberate: if Semantica misbehaves under
time pressure, `walnut/graphstore.py` provides a dependency-free `SimpleGraph`
implementing the same small surface, and the entire test suite is run against it in
`tests/test_graph_backends.py`. The rest of Walnut only ever sees `Brain`.

What the brain guarantees to callers:

* **Nothing enters without provenance.** `remember()` takes `Evidence`, which cannot
  exist without a resolvable `SourcePointer`. There is no `add_raw_text()`.
* **Nothing leaves without a citation.** Every `Fact` returned carries the pointer it
  came from, so the renderer can always show its work.
* **Nothing is deleted.** `supersede()` retracts and tombstones; the old fact stays
  queryable as history. This is what makes "what did we believe on Monday?" answerable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .contract import Evidence, SourcePointer, utcnow
from .graphstore import GraphBackend, backend_name, build_backend

__all__ = ["Brain", "Contradiction", "Fact"]


@dataclass(frozen=True, slots=True)
class Fact:
    """A claim the brain holds, and the receipt proving where it came from."""

    node_id: str
    text: str
    app: str
    pointer: SourcePointer
    author: str | None = None
    occurred_at: datetime | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def cite(self) -> str:
        who = f"{self.author} · " if self.author else ""
        when = (
            self.occurred_at.strftime("%Y-%m-%d")
            if self.occurred_at
            else self.pointer.retrieved_at.strftime("%Y-%m-%d")
        )
        return f"[{self.app}] {who}{when} — {self.pointer.resource_uri}"


@dataclass(frozen=True, slots=True)
class Contradiction:
    """Two facts the brain cannot reconcile. Surfaced, never silently resolved."""

    left: Fact
    right: Fact
    reason: str
    confidence: float

    def render(self) -> str:
        return (
            f"CONTRADICTION ({self.confidence:.0%}): {self.reason}\n"
            f"  A: {self.left.text}\n     {self.left.cite()}\n"
            f"  B: {self.right.text}\n     {self.right.cite()}"
        )


class Brain:
    """The company brain. One graph, many apps, every fact cited."""

    def __init__(self, backend: GraphBackend | None = None) -> None:
        # Injectable so the whole suite can be run against the fallback, which is the
        # only way the "swappable" claim in this module's docstring means anything.
        self._g = backend if backend is not None else build_backend()
        self._facts: dict[str, Fact] = {}
        """Sidecar index. Semantica stores the graph; we keep the typed view so a
        caller never has to unpack its nested dict shape."""

    # -- writing ------------------------------------------------------------

    def remember(self, evidence: Evidence) -> Fact:
        """Ingest one piece of evidence as a cited fact."""
        node_id = f"{evidence.pointer.app}:{evidence.id}"
        self._g.add_node(
            node_id,
            node_type=evidence.pointer.app,
            attributes={
                "text": evidence.text[:2000],
                "app": evidence.pointer.app,
                "author": evidence.author or "",
                "uri": evidence.pointer.resource_uri,
                "content_hash": evidence.pointer.content_hash,
                "labels": list(evidence.labels),
                "occurred_at": evidence.occurred_at.isoformat()
                if evidence.occurred_at
                else "",
            },
        )
        fact = Fact(
            node_id=node_id,
            text=evidence.text,
            app=evidence.pointer.app,
            pointer=evidence.pointer,
            author=evidence.author,
            occurred_at=evidence.occurred_at,
            attributes={"labels": list(evidence.labels)},
        )
        self._facts[node_id] = fact
        return fact

    def relate(self, src: str, dst: str, relation: str) -> None:
        """Link two facts. `relation` is the edge label: mentions, contradicted_by,
        implements, resolves, same_person_as."""
        self._g.add_edge(src, dst, edge_type=relation)

    def supersede(self, node_id: str, reason: str) -> None:
        """Retire a fact without destroying it. Retraction leaves a tombstone."""
        self._g.retract_node(node_id, reason=reason)

    # -- reading ------------------------------------------------------------

    def get(self, node_id: str) -> Fact | None:
        return self._facts.get(node_id)

    def search(self, text: str, limit: int = 10) -> list[Fact]:
        """Free-text search across everything the brain holds.

        Backend results first, then a substring sweep over the typed index. The sweep
        is not redundancy for its own sake: Semantica matches against node ids and
        content rather than the text attribute we store, so a term that plainly appears
        in an ingested record could return nothing at all. Two backends that disagree
        about whether a word is findable would make every downstream result depend on
        which graph library happened to be installed.
        """
        out: list[Fact] = []
        seen: set[str] = set()

        for hit in self._g.query(text, limit=limit) or []:
            node = hit.get("node", hit)
            fact = self._facts.get(node.get("id", ""))
            if fact is not None and fact.node_id not in seen:
                seen.add(fact.node_id)
                out.append(fact)

        needle = text.lower().strip()
        if needle:
            for fact in self._facts.values():
                if len(out) >= limit:
                    break
                if fact.node_id in seen:
                    continue
                if needle in fact.text.lower() or needle in fact.node_id.lower():
                    seen.add(fact.node_id)
                    out.append(fact)
        return out[:limit]

    def by_app(self, app: str) -> list[Fact]:
        return [f for f in self._facts.values() if f.app == app]

    def neighbours(self, node_id: str) -> list[tuple[str, Fact]]:
        """Adjacent facts and the relation that connects them."""
        out: list[tuple[str, Fact]] = []
        for n in self._g.get_neighbors(node_id) or []:
            fact = self._facts.get(n.get("id", ""))
            if fact is not None:
                out.append((n.get("relationship", "related"), fact))
        return out

    def state_at(self, when: datetime | str) -> dict[str, Any]:
        """What the brain believed at a point in time. The as-of query."""
        ts = when.isoformat() if isinstance(when, datetime) else when
        return self._g.state_at(ts)

    # -- decisions ----------------------------------------------------------

    def record_decision(
        self,
        *,
        category: str,
        scenario: str,
        reasoning: str,
        outcome: str,
        confidence: float,
        entities: list[str] | None = None,
    ) -> str:
        """Write a decision into the audit trail.

        `entities` links the decision to the facts that drove it — pass them or
        `trace_decision_chain` comes back empty and the audit trail is decorative.

        The metadata key is `walnut_recorded_at`, not `recorded_at`: Semantica passes
        metadata through as keyword arguments to its own `add_node`, which already
        binds `recorded_at`, and the collision raises TypeError *inside* a bare except
        in its own code — so decisions vanish silently rather than failing loudly.
        """
        return self._g.record_decision(
            category=category,
            scenario=scenario,
            reasoning=reasoning,
            outcome=outcome,
            confidence=confidence,
            entities=entities or [],
            decision_maker="walnut-agent",
            metadata={"walnut_recorded_at": utcnow().isoformat()},
        )

    def trace_decision(self, decision_id: str) -> list[dict[str, Any]]:
        return self._g.trace_decision_chain(decision_id) or []

    def decision_summary(self) -> dict[str, Any]:
        return self._g.get_decision_summary() or {}

    # -- housekeeping -------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        s = dict(self._g.stats() or {})
        s["facts_indexed"] = len(self._facts)
        s.setdefault("backend", backend_name())
        s["apps"] = sorted({f.app for f in self._facts.values()})
        return s

    def save(self, path: str) -> None:
        self._g.save_to_file(path)
