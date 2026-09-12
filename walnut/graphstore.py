"""Graph backends for the brain, and the fallback that makes the swap claim real.

`brain.py` says the Semantica dependency can be swapped out by touching one module.
That was an assertion with nothing behind it, which on this project of all projects is
not good enough — so here is the second implementation, and a test that runs the entire
suite against it.

`SimpleGraph` is a few hundred lines of dictionaries. It is not as capable as Semantica
(no PROV-O, no causal analysis, no embeddings) but it implements everything `Brain`
actually calls, which turns out to be a small surface. That smallness is the finding:
the dependency looked load-bearing and mostly was not.

Selection order:

1. `WALNUT_GRAPH=simple` in the environment — force the fallback, no questions.
2. Semantica if it imports.
3. `SimpleGraph` otherwise.

So an import failure at 3am during the hackathon window degrades the provenance
features rather than stopping the demo.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

__all__ = ["GraphBackend", "SimpleGraph", "build_backend", "backend_name"]


class GraphBackend(Protocol):
    """Exactly what `Brain` uses. Nothing more — the surface is the contract."""

    def add_node(self, node_id: str, *, node_type: str, attributes: dict[str, Any]) -> Any: ...
    def add_edge(self, src: str, dst: str, *, edge_type: str) -> Any: ...
    def get_neighbors(self, node_id: str) -> list[dict[str, Any]]: ...
    def query(self, text: str, limit: int | None = None) -> list[dict[str, Any]]: ...
    def state_at(self, timestamp: str) -> dict[str, Any]: ...
    def retract_node(self, node_id: str, *, reason: str) -> Any: ...
    def record_decision(self, **kwargs: Any) -> str: ...
    def trace_decision_chain(self, decision_id: str) -> list[dict[str, Any]]: ...
    def get_decision_summary(self) -> dict[str, Any]: ...
    def stats(self) -> dict[str, Any]: ...
    def save_to_file(self, path: str) -> None: ...


@dataclass
class _Node:
    id: str
    type: str
    attributes: dict[str, Any]
    created_at: datetime
    retracted: str | None = None


@dataclass
class SimpleGraph:
    """Dictionaries. No dependency, no import cost, no surprises."""

    nodes: dict[str, _Node] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    retractions: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- structure ----------------------------------------------------------

    def add_node(self, node_id: str, *, node_type: str = "", attributes: dict[str, Any] | None = None) -> bool:
        from .contract import utcnow

        self.nodes[node_id] = _Node(
            id=node_id, type=node_type, attributes=dict(attributes or {}),
            created_at=utcnow(),
        )
        return True

    def add_edge(self, src: str, dst: str, *, edge_type: str = "related") -> bool:
        self.edges.append((src, dst, edge_type))
        return True

    def get_neighbors(self, node_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for src, dst, rel in self.edges:
            other = dst if src == node_id else src if dst == node_id else None
            if other is None or other not in self.nodes:
                continue
            out.append({"id": other, "type": self.nodes[other].type, "relationship": rel})
        return out

    # -- retrieval ----------------------------------------------------------

    def query(self, text: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Substring match over id and stored text, ranked by number of term hits.

        Semantica does embedding-backed retrieval here. This does not, and the
        difference is visible: it will miss a paraphrase. Recorded rather than papered
        over — the fallback is a fallback, not an equal.
        """
        terms = [t for t in re.split(r"\W+", text.lower()) if len(t) > 2]
        scored: list[tuple[int, dict[str, Any]]] = []
        for node in self.nodes.values():
            if node.retracted:
                continue
            haystack = f"{node.id} {node.attributes.get('text', '')}".lower()
            score = sum(1 for t in terms if t in haystack)
            if score:
                scored.append((score, {"node": {"id": node.id, "type": node.type}}))
        scored.sort(key=lambda pair: -pair[0])
        rows = [row for _, row in scored]
        return rows[:limit] if limit else rows

    def state_at(self, timestamp: str) -> dict[str, Any]:
        cutoff = str(timestamp)
        return {
            "timestamp": cutoff,
            "nodes": [
                {"id": n.id, "type": n.type, "properties": n.attributes}
                for n in self.nodes.values()
                if n.created_at.isoformat() <= cutoff
            ],
        }

    # -- retraction ---------------------------------------------------------

    def retract_node(self, node_id: str, *, reason: str = "") -> bool:
        """Tombstone, never erase. The node stays queryable as history."""
        from .contract import utcnow

        if node_id not in self.nodes:
            return False
        self.nodes[node_id].retracted = reason
        self.retractions[node_id] = {
            "entity_id": node_id, "entity_kind": "node",
            "retracted_at": utcnow().isoformat(), "reason": reason,
        }
        return True

    def get_retraction(self, node_id: str) -> dict[str, Any] | None:
        return self.retractions.get(node_id)

    def list_tombstones(self) -> list[dict[str, Any]]:
        return list(self.retractions.values())

    # -- decisions ----------------------------------------------------------

    def record_decision(self, **kwargs: Any) -> str:
        from .contract import utcnow

        decision_id = str(uuid.uuid4())
        self.decisions[decision_id] = {
            "id": decision_id, "recorded_at": utcnow().isoformat(), **kwargs,
        }
        return decision_id

    def trace_decision_chain(self, decision_id: str, **_: Any) -> list[dict[str, Any]]:
        decision = self.decisions.get(decision_id)
        if decision is None:
            return []
        return [
            {"decision": decision_id, "entity": e, "node": self.nodes[e].type}
            for e in decision.get("entities", []) or []
            if e in self.nodes
        ]

    def get_decision_summary(self) -> dict[str, Any]:
        cats: dict[str, int] = {}
        outs: dict[str, int] = {}
        for d in self.decisions.values():
            cats[d.get("category", "?")] = cats.get(d.get("category", "?"), 0) + 1
            outs[d.get("outcome", "?")] = outs.get(d.get("outcome", "?"), 0) + 1
        return {"total_decisions": len(self.decisions), "categories": cats, "outcomes": outs}

    # -- housekeeping -------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        types: dict[str, int] = {}
        for n in self.nodes.values():
            types[n.type] = types.get(n.type, 0) + 1
        return {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "node_types": types,
            "backend": "simple",
        }

    def save_to_file(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "nodes": [
                        {"id": n.id, "type": n.type, "attributes": n.attributes,
                         "retracted": n.retracted}
                        for n in self.nodes.values()
                    ],
                    "edges": [{"src": s, "dst": d, "type": t} for s, d, t in self.edges],
                    "decisions": list(self.decisions.values()),
                },
                fh, indent=2, default=str,
            )


_BACKEND_NAME = "unset"


def build_backend() -> GraphBackend:
    """Pick a backend. Never raises — a missing dependency degrades, it does not stop."""
    global _BACKEND_NAME

    if os.environ.get("WALNUT_GRAPH", "").lower() == "simple":
        _BACKEND_NAME = "simple (forced)"
        return SimpleGraph()

    try:
        from semantica.context import ContextGraph

        backend = ContextGraph()
        _BACKEND_NAME = "semantica"
        return backend  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 - any import or construction failure falls back
        _BACKEND_NAME = "simple (semantica unavailable)"
        return SimpleGraph()


def backend_name() -> str:
    return _BACKEND_NAME
