"""The agent loop: ingest, investigate, propose, execute.

This is the only place the pieces meet, and it is deliberately thin. Almost everything
that matters has already been decided by the time control reaches here — the adapters
decided what evidence looks like, the executor decided what may be written, the
grounding wall decided what may be said. The loop's job is to sequence those, not to
add judgement of its own.

That thinness is the architecture's claim. An agent whose safety lives in its
orchestration logic is one refactor away from unsafe. An agent whose safety lives in
the constructors of the objects it passes around stays safe even when the loop is
wrong, because the unsafe call simply cannot be expressed.

The loop is model-optional. Every step below — ingestion, identity resolution,
contradiction detection, action proposal — is deterministic Python. A language model
can be dropped in to phrase the customer reply or summarise a thread, but it is never
in the path of a decision about what is true or what may be written. That is what
makes the run reproducible, and it is why the reliability story survives contact with
a judge who asks "what if the model has a bad day?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .actions.executor import ActionExecutor, ExecutionResult
from .actions.governance import Refusal
from .brain import Brain, Fact
from .contract import Action, Adapter
from .contradiction import Conflict, detect_contradictions
from .identity import Identity, ResolutionReport, resolve_identities
from .render import Answer, Claim, GroundedAnswer, ground

__all__ = ["Investigation", "WalnutAgent"]


@dataclass
class Investigation:
    """Everything the agent found, and what it proposes to do about it."""

    question: str
    answer: GroundedAnswer | None = None
    conflicts: list[Conflict] = field(default_factory=list)
    proposed: list[Action] = field(default_factory=list)
    results: list[ExecutionResult] = field(default_factory=list)

    @property
    def executed(self) -> list[ExecutionResult]:
        return [r for r in self.results if not isinstance(r, Refusal)]

    @property
    def refused(self) -> list[Refusal]:
        return [r for r in self.results if isinstance(r, Refusal)]

    def render(self) -> str:
        parts: list[str] = []
        if self.answer:
            parts.append(self.answer.render())
        if self.conflicts:
            parts.append("\n\n".join(c.render() for c in self.conflicts))
        if self.executed:
            parts.append(
                "ACTIONS TAKEN\n"
                + "\n".join(
                    f"  · {r.action.app}.{r.action.operation} → {r.action_id}"
                    for r in self.executed  # type: ignore[union-attr]
                )
            )
        if self.refused:
            parts.append("\n\n".join(r.render() for r in self.refused))
        return "\n\n".join(parts)


class WalnutAgent:
    """Reads five systems, reasons over cited evidence, acts under governance."""

    def __init__(
        self,
        adapters: dict[str, Adapter],
        brain: Brain,
        executor: ActionExecutor,
        *,
        tracer: Any = None,
    ) -> None:
        self.adapters = adapters
        self.brain = brain
        self.executor = executor
        self.tracer = tracer
        self.identities: ResolutionReport | None = None

    # -- 1. ingest ----------------------------------------------------------

    def ingest(self, scopes: dict[str, str | None] | None = None, limit: int = 100) -> int:
        """Pull evidence from every connected app into the brain.

        One adapter failing must not abort the ingest — a company brain that needs all
        five systems healthy to answer anything is less useful than four fifths of a
        brain. Failures are recorded as decisions so the gap is visible rather than
        silently absent.
        """
        scopes = scopes or {}
        total = 0
        for name, adapter in self.adapters.items():
            try:
                records = adapter.fetch(scope=scopes.get(name), limit=limit)
            except Exception as exc:  # noqa: BLE001 - degraded, not fatal
                self.brain.record_decision(
                    category="ingest.failure",
                    scenario=f"fetch from {name}",
                    reasoning=f"{type(exc).__name__}: {exc}",
                    outcome="skipped",
                    confidence=1.0,
                )
                if self.tracer is not None:
                    self.tracer.tool(f"ingest.{name}", {"scope": scopes.get(name)}, None, error=str(exc))
                continue

            for record in records:
                self.brain.remember(record)
                total += 1
            if self.tracer is not None:
                self.tracer.tool(f"ingest.{name}", {"scope": scopes.get(name)}, {"records": len(records)})
        return total

    # -- 2. resolve identities ---------------------------------------------

    def resolve_people(self, identities: list[Identity]) -> ResolutionReport:
        """Collapse per-app handles into people. Uncertain matches go to a human."""
        self.identities = resolve_identities(identities)
        self.brain.record_decision(
            category="identity.resolution",
            scenario="cross-app identity resolution",
            reasoning=str(self.identities.summary()),
            outcome="resolved",
            confidence=1.0,
        )
        return self.identities

    # -- 3. investigate -----------------------------------------------------

    def investigate(self, question: str, *, search_terms: list[str] | None = None) -> Investigation:
        """Answer a question from cited evidence and surface any contradictions."""
        inv = Investigation(question=question)

        relevant: dict[str, Fact] = {}
        for term in search_terms or [question]:
            for fact in self.brain.search(term, limit=25):
                relevant[fact.node_id] = fact

        inv.conflicts = [
            c
            for c in detect_contradictions(self.brain)
            # Keep only conflicts touching what was asked about, when a scope was given.
            if not relevant
            or c.left.fact.node_id in relevant
            or c.right.fact.node_id in relevant
        ]

        claims = [
            Claim(text=f.text[:200], evidence_ids=(f.node_id,))
            for f in list(relevant.values())[:8]
        ]
        analysis = (
            f"{len(inv.conflicts)} contradiction(s) found across "
            f"{len({f.app for f in relevant.values()})} connected system(s)."
            if inv.conflicts
            else "No contradictions detected among the retrieved evidence."
        )
        inv.answer = ground(Answer(question=question, claims=claims, analysis=analysis), self.brain)

        if self.tracer is not None:
            self.tracer.tool(
                "investigate",
                {"question": question},
                {
                    "facts": len(inv.answer.facts),
                    "refusals": len(inv.answer.refusals),
                    "conflicts": len(inv.conflicts),
                },
            )
        return inv

    # -- 4. propose ---------------------------------------------------------

    def propose_for_conflict(self, conflict: Conflict, plan: list[dict[str, Any]]) -> list[Action]:
        """Turn a conflict into concrete, evidence-cited proposed actions.

        `plan` is supplied by the caller rather than invented here, because which apps
        to write to is a product decision, not something to be guessed per run. Every
        proposed action inherits the conflict's own evidence as its justification —
        which is what makes the chain from customer complaint to written artefact
        auditable end to end.
        """
        evidence = conflict.evidence_ids()
        actions: list[Action] = []
        for step in plan:
            actions.append(
                Action(
                    app=step["app"],
                    operation=step["operation"],
                    target=step["target"],
                    payload=step["payload"],
                    justified_by=evidence,
                    rationale=step.get("rationale", conflict.explanation[:200]),
                )
            )
        return actions

    # -- 5. execute ---------------------------------------------------------

    def execute_all(self, actions: list[Action]) -> list[ExecutionResult]:
        """Run every proposed action through the one governed choke point.

        Execution continues past a refusal rather than aborting. A refused customer
        email must not prevent the internal Linear issue from being filed — the whole
        point of tiering is that low-consequence work proceeds while the consequential
        step waits for a person.
        """
        return [self.executor.execute(a) for a in actions]

    # -- reporting ----------------------------------------------------------

    def reliability_report(self) -> dict[str, Any]:
        """The numbers behind "show how you know it works"."""
        return {
            "brain": self.brain.stats(),
            "decisions": self.brain.decision_summary(),
            "actions": self.executor.ledger.summary(),
            "identities": self.identities.summary() if self.identities else None,
        }
