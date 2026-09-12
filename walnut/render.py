"""The grounding wall: the last thing between the agent and a human reader.

Everything upstream of here — retrieval, contradiction detection, whatever model we
put in the loop — is allowed to be probabilistic. This module is not. It is ordinary
deterministic code that takes an answer apart, checks every individual claim against
the evidence backing it, and **drops any claim it cannot cite**.

That ordering is the point. A model asked nicely to "only say things you can cite"
complies most of the time, and the failures are invisible because a fabricated citation
looks exactly like a real one. Moving the check outside the model turns an unverifiable
instruction into a verifiable function: a claim either resolves to a fact in the brain
or it does not render. There is no confidence threshold and no override.

The output envelope is deliberately three-part:

    facts      claims that resolved to evidence, each with its citation
    analysis   the agent's reasoning ABOUT those facts, marked as such
    refusals   what was asked for and could not be supported, and why

Most systems collapse the third field into silence. Silence is what makes a confident
wrong answer indistinguishable from a correct one, so refusals are rendered as
prominently as facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .brain import Brain, Fact

__all__ = ["Answer", "Claim", "GroundedAnswer", "ground"]

# Numerals that appear in a claim but in none of its evidence are, by definition,
# not from the evidence. Dates, counts and money are where fabrication does damage.
_DIGITS = re.compile(r"\d[\d,.]*")


@dataclass(frozen=True, slots=True)
class Claim:
    """One assertion the agent wants to make, and the evidence it says supports it."""

    text: str
    evidence_ids: tuple[str, ...] = ()


@dataclass
class Answer:
    """What the agent proposes to say, before grounding."""

    question: str
    claims: list[Claim] = field(default_factory=list)
    analysis: str = ""


@dataclass
class GroundedAnswer:
    """What the agent is actually permitted to say."""

    question: str
    facts: list[tuple[Claim, list[Fact]]] = field(default_factory=list)
    analysis: str = ""
    refusals: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_fully_grounded(self) -> bool:
        return not self.refusals

    def render(self) -> str:
        lines = [f"Q: {self.question}", ""]

        if self.facts:
            lines.append("FACTS")
            for i, (claim, evidence) in enumerate(self.facts, 1):
                lines.append(f"  {i}. {claim.text}")
                lines.extend(f"     └─ {f.cite()}" for f in evidence)
            lines.append("")

        if self.analysis:
            lines += ["ANALYSIS  (reasoning about the facts above, not itself a fact)",
                      f"  {self.analysis}", ""]

        if self.refusals:
            lines.append("REFUSALS")
            lines.extend(f"  · {what}\n    {why}" for what, why in self.refusals)
            lines.append("")

        if not self.facts and not self.analysis:
            lines.append("Nothing in this answer could be grounded in evidence.")

        return "\n".join(lines).rstrip()


def _unsupported_numbers(claim_text: str, evidence: list[Fact]) -> list[str]:
    """Numerals in the claim that appear nowhere in its supporting evidence.

    A number the agent produced rather than read is the highest-consequence kind of
    fabrication — it is specific, quotable, and looks authoritative. Cheap to check,
    so we check it every time.
    """
    corpus = " ".join(f.text for f in evidence)
    present = {m.group(0).rstrip(".,") for m in _DIGITS.finditer(corpus)}
    claimed = {m.group(0).rstrip(".,") for m in _DIGITS.finditer(claim_text)}
    return sorted(n for n in claimed - present if len(n) > 1)


def ground(answer: Answer, brain: Brain, *, check_numbers: bool = True) -> GroundedAnswer:
    """Check every claim against the brain. Unsupported claims become refusals.

    Never raises and never partially renders a claim: a claim is either fully
    supported and appears under FACTS, or it is dropped and appears under REFUSALS
    with the reason. Half-rendering is how "show then retract" bugs happen.
    """
    grounded = GroundedAnswer(question=answer.question, analysis=answer.analysis)

    for claim in answer.claims:
        if not claim.evidence_ids:
            grounded.refusals.append(
                (
                    claim.text,
                    "No evidence cited. A claim with no source is not rendered, "
                    "regardless of how confident the agent is in it.",
                )
            )
            continue

        facts: list[Fact] = []
        missing: list[str] = []
        for eid in claim.evidence_ids:
            fact = brain.get(eid)
            (facts.append(fact) if fact is not None else missing.append(eid))

        if missing:
            grounded.refusals.append(
                (
                    claim.text,
                    f"Cited evidence does not exist in the brain: {', '.join(missing)}. "
                    "A citation that does not resolve is a fabricated citation.",
                )
            )
            continue

        if check_numbers:
            invented = _unsupported_numbers(claim.text, facts)
            if invented:
                grounded.refusals.append(
                    (
                        claim.text,
                        f"Contains figures absent from every cited source: "
                        f"{', '.join(invented)}. Numbers must be read, never produced.",
                    )
                )
                continue

        grounded.facts.append((claim, facts))

    return grounded
