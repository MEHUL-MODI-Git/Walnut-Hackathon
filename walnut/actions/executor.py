"""The single choke point through which every write to every external app passes.

One class, one method. If an action did not go through `ActionExecutor.execute()` it
did not happen, and there is no second path — the adapters' `act()` methods are not
called anywhere else in the system. Concentrating writes into one audited function is
what makes "show how you know it works" answerable at all: there is exactly one place
to look.

The order of the checks is deliberate and is itself the safety argument:

    1. is the operation real?            unknown ops cannot be executed by accident
    2. is it categorically forbidden?    before any approval can be sought
    3. is it justified by real evidence? before we look at what the evidence says
    4. is that evidence tainted?         BEFORE a human is ever asked to approve
    5. is the evidence still current?    a stale citation is not a citation
    6. does a human need to approve?     last, on an action already known to be sound

Step 4 sitting above step 6 is the part worth defending. If injected content could
reach a human approval prompt, the attack would simply become social engineering with
extra steps — a plausible-looking request, rubber-stamped by a tired reviewer at 3am.
The agent refuses tainted actions outright rather than delegating the decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..brain import Brain, Fact
from ..contract import Action, ActionReceipt, ActionTier, Adapter
from .governance import (
    HumanGate,
    Refusal,
    RefusalReason,
    TaintReport,
    scan_for_injected_instructions,
    tier_requires_human,
)

__all__ = ["ActionExecutor", "ActionLedger", "ExecutionResult"]

ExecutionResult = ActionReceipt | Refusal


@dataclass
class ActionLedger:
    """Append-only record of every write, and the means to reverse each one."""

    receipts: dict[str, ActionReceipt] = field(default_factory=dict)
    refusals: list[Refusal] = field(default_factory=list)
    _order: list[str] = field(default_factory=list)

    def append(self, receipt: ActionReceipt) -> None:
        self.receipts[receipt.action_id] = receipt
        self._order.append(receipt.action_id)

    def record_refusal(self, refusal: Refusal) -> None:
        self.refusals.append(refusal)

    def history(self) -> list[ActionReceipt]:
        return [self.receipts[i] for i in self._order]

    def live(self) -> list[ActionReceipt]:
        """Actions that have not been undone."""
        return [r for r in self.history() if not r.is_undone]

    def summary(self) -> dict[str, Any]:
        return {
            "executed": len(self._order),
            "undone": sum(1 for r in self.receipts.values() if r.is_undone),
            "refused": len(self.refusals),
            "refusal_reasons": sorted({r.reason.value for r in self.refusals}),
        }


class ActionExecutor:
    """Routes every proposed action through the governance checks, then executes it."""

    def __init__(
        self,
        adapters: dict[str, Adapter],
        brain: Brain,
        gate: HumanGate,
        *,
        verify_freshness: bool = True,
        tracer: Any = None,
    ) -> None:
        self._adapters = adapters
        self._brain = brain
        self._gate = gate
        self._verify_freshness = verify_freshness
        self._tracer = tracer
        self.ledger = ActionLedger()

    # -- the one entry point ------------------------------------------------

    def execute(self, action: Action) -> ExecutionResult:
        result = self._execute(action)
        if isinstance(result, Refusal):
            self.ledger.record_refusal(result)
            self._trace_decision(action, "refused", 1.0, result.reason.value)
        else:
            self.ledger.append(result)
            self._trace_decision(action, "executed", 1.0, result.action_id)
        return result

    def _execute(self, action: Action) -> ExecutionResult:
        adapter = self._adapters.get(action.app)
        if adapter is None:
            return Refusal(
                reason=RefusalReason.UNKNOWN_OPERATION,
                action=action,
                explanation=f"No adapter registered for app {action.app!r}.",
            )

        # 1 + 2. Is the operation real, and is it categorically forbidden?
        try:
            tier = adapter.capabilities().tier_of(action.operation)
        except KeyError as exc:
            return Refusal(
                reason=RefusalReason.UNKNOWN_OPERATION,
                action=action,
                explanation=str(exc),
            )

        if tier is ActionTier.FORBIDDEN:
            return Refusal(
                reason=RefusalReason.FORBIDDEN_OPERATION,
                action=action,
                explanation=(
                    f"{action.app}.{action.operation} is tier FORBIDDEN. No approval "
                    "path exists for it by design."
                ),
            )

        # 3. Is it justified by evidence the brain actually holds?
        facts: list[Fact] = []
        missing: list[str] = []
        for evidence_id in action.justified_by:
            fact = self._brain.get(evidence_id)
            (facts.append(fact) if fact is not None else missing.append(evidence_id))

        if missing:
            return Refusal(
                reason=RefusalReason.NO_EVIDENCE,
                action=action,
                explanation=(
                    "Cited evidence is not in the brain: "
                    f"{', '.join(missing)}. An action justified by a fact that does "
                    "not exist is an action justified by nothing."
                ),
                evidence_ids=tuple(action.justified_by),
            )

        # 4. Is that evidence trying to instruct us?
        tainted: list[tuple[Fact, TaintReport]] = []
        for fact in facts:
            report = scan_for_injected_instructions(fact.text)
            if report.is_tainted:
                tainted.append((fact, report))

        if tainted and tier > ActionTier.TRIVIAL:
            return Refusal(
                reason=RefusalReason.TAINTED_INSTRUCTION,
                action=action,
                explanation=(
                    f"{action.app}.{action.operation} (tier {tier.name}) is justified by "
                    "ingested content containing embedded instructions "
                    f"[{'; '.join(r.summary() for _, r in tainted)}]. Content read from a "
                    "connected app is evidence about the world, never a command "
                    "addressed to this agent. Quarantining or labelling it is permitted; "
                    "acting on it is not."
                ),
                evidence_ids=tuple(action.justified_by),
                taint_path=tuple(
                    f"{f.app} · {f.pointer.resource_uri} · matched {r.matched[0]!r}"
                    for f, r in tainted
                ),
            )

        # 5. Does the evidence still say what we think it says?
        if self._verify_freshness:
            drifted = self._find_drift(facts)
            if drifted:
                return Refusal(
                    reason=RefusalReason.STALE_EVIDENCE,
                    action=action,
                    explanation=(
                        "The source has changed since it was read: "
                        f"{', '.join(drifted)}. Re-ingest before acting — a citation "
                        "that no longer resolves is not a citation."
                    ),
                    evidence_ids=tuple(action.justified_by),
                )

        # 6. Does a human have to say yes?
        if tier_requires_human(tier):
            decision = self._gate.request(action, self._gate_context(action, facts))
            if not decision.approved:
                return Refusal(
                    reason=(
                        RefusalReason.GATE_TIMEOUT
                        if decision.timed_out
                        else RefusalReason.GATE_DENIED
                    ),
                    action=action,
                    explanation=(
                        f"Human gate did not approve ({decision.decided_by})"
                        + (f": {decision.note}" if decision.note else "")
                    ),
                    evidence_ids=tuple(action.justified_by),
                )

        # Everything holds. Write it.
        return adapter.act(action)

    # -- undo ---------------------------------------------------------------

    def undo(self, action_id: str) -> ActionReceipt:
        """Reverse a previously executed action.

        Reversal goes back through the adapter that performed it, so each app can
        retract in whatever way preserves its own audit trail.
        """
        receipt = self.ledger.receipts.get(action_id)
        if receipt is None:
            raise KeyError(f"No action {action_id!r} in the ledger.")
        if receipt.is_undone:
            return receipt

        adapter = self._adapters[receipt.action.app]
        undone = adapter.undo(receipt)
        self.ledger.receipts[action_id] = undone
        self._trace_decision(receipt.action, "undone", 1.0, action_id)
        return undone

    # -- internals ----------------------------------------------------------

    def _find_drift(self, facts: list[Fact]) -> list[str]:
        drifted: list[str] = []
        for fact in facts:
            adapter = self._adapters.get(fact.app)
            if adapter is None:
                continue
            current = adapter.resolve(fact.pointer)
            if current is None:
                drifted.append(f"{fact.node_id} (vanished)")
            elif current.pointer.content_hash != fact.pointer.content_hash:
                drifted.append(f"{fact.node_id} (content changed)")
        return drifted

    @staticmethod
    def _gate_context(action: Action, facts: list[Fact]) -> str:
        """What the human sees. Citations, not a bare 'approve?' prompt."""
        lines = [
            f"{action.app}.{action.operation} — {action.rationale or 'no rationale given'}",
            "",
            "Justified by:",
        ]
        lines.extend(f"  · {f.text[:120]}\n    {f.cite()}" for f in facts)
        return "\n".join(lines)

    def _trace_decision(
        self, action: Action, outcome: str, confidence: float, detail: str
    ) -> None:
        self._brain.record_decision(
            category=f"action.{action.app}.{action.operation}",
            scenario=action.rationale or f"{action.app}.{action.operation}",
            reasoning=detail,
            outcome=outcome,
            confidence=confidence,
            entities=list(action.justified_by),
        )
        if self._tracer is not None:
            self._tracer.decision(
                name=f"{action.app}.{action.operation}",
                outcome=outcome,
                confidence=confidence,
                evidence_ids=list(action.justified_by),
            )
