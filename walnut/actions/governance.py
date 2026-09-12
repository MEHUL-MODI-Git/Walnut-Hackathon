"""Refusals, taint, and human gates — the rules that decide whether an action runs.

The central claim of this project is that an agent connected to five real systems is
only trustworthy if it can say **no**, and say it for a reason you can read. So refusal
is a first-class typed outcome here, never an exception that fell out of a try block.

The distinction that matters most, and the one this module exists to encode:

    An instruction from a human principal is a request.
    An instruction found inside ingested content is DATA.

An agent that cannot tell those apart will cheerfully execute whatever a malicious
document tells it to. Content read from Slack, Notion, email or a PR body is evidence
about the world — it is never a command addressed to the agent, no matter how
imperatively it is phrased.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from ..contract import Action, ActionTier
from ..contract import utcnow

__all__ = [
    "AlwaysApprove",
    "AlwaysDeny",
    "GateDecision",
    "HumanGate",
    "QueueGate",
    "Refusal",
    "RefusalReason",
    "TaintReport",
    "scan_for_injected_instructions",
]


class RefusalReason(StrEnum):
    """Why the agent declined. Every refusal names exactly one of these."""

    NO_EVIDENCE = "no_evidence"
    """The justifying evidence does not exist in the brain."""

    TAINTED_INSTRUCTION = "tainted_instruction"
    """The action traces to instructions embedded in ingested content."""

    CONTRADICTED_EVIDENCE = "contradicted_evidence"
    """The justifying facts conflict and the agent cannot rank them."""

    STALE_EVIDENCE = "stale_evidence"
    """The source moved since we read it; the citation no longer holds."""

    FORBIDDEN_OPERATION = "forbidden_operation"
    """Tier 3. Never executed, under any approval."""

    UNKNOWN_OPERATION = "unknown_operation"
    """The adapter declares no such operation."""

    GATE_DENIED = "gate_denied"
    """A human was asked and said no."""

    GATE_TIMEOUT = "gate_timeout"
    """A human was asked and did not answer. Timeout is a refusal, never a guess."""


@dataclass(frozen=True, slots=True)
class Refusal:
    """A typed no. Carries enough detail for a human to audit the decision.

    Deliberately NOT an exception. A refusal is a legitimate, expected outcome of a
    correctly functioning agent, and modelling it as an error trains everyone —
    including the code — to treat it as a fault to be suppressed.
    """

    reason: RefusalReason
    action: Action
    explanation: str
    evidence_ids: tuple[str, ...] = ()
    taint_path: tuple[str, ...] = ()
    """When the refusal is TAINTED_INSTRUCTION: which sources carried the injection,
    in the order they were read. This is what gets shown in the demo."""

    refused_at: object = field(default_factory=utcnow)

    def render(self) -> str:
        lines = [
            f"REFUSED: {self.action.app}.{self.action.operation}",
            f"  reason: {self.reason.value}",
            f"  {self.explanation}",
        ]
        if self.taint_path:
            lines.append("  taint path:")
            lines.extend(f"    {i + 1}. {p}" for i, p in enumerate(self.taint_path))
        if self.evidence_ids:
            lines.append(f"  cited: {', '.join(self.evidence_ids)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Taint
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("override", re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instruction", re.I)),
    ("override", re.compile(r"disregard\s+(all\s+)?(previous|prior|the\s+above)", re.I)),
    ("role-switch", re.compile(r"you\s+are\s+now\s+(a|an|the)\b", re.I)),
    ("role-switch", re.compile(r"\bsystem\s*(prompt|message)\s*:", re.I)),
    ("exfiltration", re.compile(r"email\s+(the\s+)?(customer|user|client|contact)\s*list", re.I)),
    ("exfiltration", re.compile(r"send\s+(all|every|the\s+full)\b.{0,30}\b(to|external)", re.I)),
    ("bulk-mutation", re.compile(r"mark\s+all\s+\w+\s+(as\s+)?(resolved|closed|done|complete)", re.I)),
    ("bulk-mutation", re.compile(r"(delete|remove|drop|purge)\s+(all|every)\b", re.I)),
    ("credential", re.compile(r"(api[_\s-]?key|password|secret|token)\s*[:=]", re.I)),
)


@dataclass(frozen=True, slots=True)
class TaintReport:
    """What a scan found in one piece of ingested content."""

    is_tainted: bool
    categories: tuple[str, ...] = ()
    matched: tuple[str, ...] = ()
    """The offending substrings, for showing a human exactly what was found."""

    def summary(self) -> str:
        if not self.is_tainted:
            return "clean"
        return f"{', '.join(sorted(set(self.categories)))} ({len(self.matched)} match)"


def scan_for_injected_instructions(text: str) -> TaintReport:
    """Detect text that is trying to instruct the agent rather than inform it.

    **This is a tripwire, not a perimeter.** Pattern matching over adversarial text is
    false-negative-prone by nature and a determined attacker will phrase around it.
    It is safe to rely on only because it is not the thing keeping us safe — the
    structural rule is. Tainted or not, ingested content can never justify a
    state-changing action (see `ActionExecutor`), so a miss here downgrades the
    quality of the explanation we show a human, not the safety of the outcome.

    A tripwire produces evidence. It never makes the decision.
    """
    categories: list[str] = []
    matched: list[str] = []
    for category, pattern in _INJECTION_PATTERNS:
        for hit in pattern.finditer(text):
            categories.append(category)
            matched.append(hit.group(0)[:80])
    return TaintReport(
        is_tainted=bool(matched),
        categories=tuple(categories),
        matched=tuple(matched),
    )


# ---------------------------------------------------------------------------
# Human gates
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateDecision:
    approved: bool
    decided_by: str
    note: str = ""
    timed_out: bool = False


class HumanGate(Protocol):
    """Asked to approve any action at `ActionTier.GATED` or above.

    Implementations must fail closed: no answer is a denial, never an approval.
    """

    def request(self, action: Action, context: str) -> GateDecision: ...


class AlwaysApprove:
    """Test double. Never use in a demo — the gate is the point."""

    def request(self, action: Action, context: str) -> GateDecision:
        return GateDecision(approved=True, decided_by="auto-approve(test)")


class AlwaysDeny:
    def request(self, action: Action, context: str) -> GateDecision:
        return GateDecision(approved=False, decided_by="auto-deny(test)")


class QueueGate:
    """Queues requests for out-of-band approval — the steward console's backend.

    Nothing is approved by default. `pending` is what the console renders; a human
    calls `resolve()` to answer. An action whose gate request is never answered stays
    unexecuted forever, which is the correct failure mode.
    """

    def __init__(self, decided_by: str = "steward") -> None:
        self.pending: dict[str, tuple[Action, str]] = {}
        self._answers: dict[str, GateDecision] = {}
        self._decided_by = decided_by
        self._seq = 0

    def request(self, action: Action, context: str) -> GateDecision:
        key = f"gate-{self._seq}"
        self._seq += 1
        if key in self._answers:
            return self._answers[key]
        self.pending[key] = (action, context)
        return GateDecision(
            approved=False,
            decided_by="pending",
            note=f"awaiting human approval ({key})",
            timed_out=True,
        )

    def resolve(self, key: str, *, approved: bool, note: str = "") -> GateDecision:
        decision = GateDecision(approved=approved, decided_by=self._decided_by, note=note)
        self._answers[key] = decision
        self.pending.pop(key, None)
        return decision


def tier_requires_human(tier: ActionTier) -> bool:
    return tier >= ActionTier.GATED
