"""The Walnut adapter contract.

Every external app speaks exactly this interface — Slack, Linear, GitHub, Notion,
and Email are five implementations of one protocol, not five bespoke integrations.

The contract is deliberately symmetric: three read methods that feed the brain, and
three write methods that take action. That symmetry is the point. The same adapter
that sourced a claim is the one that can act on it, so every action can be traced
back through the evidence that justified it.

    READ                                WRITE
    probe()      what is here           capabilities()  what can I do here
    fetch()      pull evidence          act()           perform, capturing prior state
    resolve()    re-verify one item     undo()          restore prior state

Two rules are enforced by the types themselves rather than by convention:

1. **No evidence without a pointer.** `Evidence` cannot be constructed without a
   `SourcePointer` carrying the app, a resolvable URI, and a content hash. A claim
   that cannot be traced back to a specific byte range in a specific system is not
   evidence, it is a guess.

2. **No action without justification.** `Action.justified_by` is non-empty or the
   action refuses to construct. The agent may not take an action it cannot cite a
   reason for. This is the single most important line in the file.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AGENT_SIGNATURE",
    "Action",
    "ActionCapabilities",
    "ActionReceipt",
    "ActionTier",
    "Adapter",
    "Evidence",
    "SourcePointer",
    "SourceProfile",
    "content_hash",
    "utcnow",
]


def utcnow() -> datetime:
    """Timestamps are system-minted, never model-authored."""
    return datetime.now(timezone.utc)


def content_hash(payload: Any) -> str:
    """Stable sha256 over a canonical JSON encoding.

    Sorted keys and tight separators so the same logical record hashes identically
    across runs and machines. This is what lets `detect_drift` and citation
    re-verification mean something: if the hash moved, the source moved.
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourcePointer:
    """A reference back into the system of record. Never a copy of the payload.

    Walnut stores summarised claims plus one of these. The full record stays where
    it lives and is re-fetched on demand via `Adapter.resolve()`.
    """

    app: str
    """Adapter name: slack, linear, github, notion, email."""

    resource_uri: str
    """Something a human can click. A permalink wherever the app offers one."""

    locator: dict[str, Any]
    """Machine-readable coordinates for re-fetching: ids, keys, offsets."""

    content_hash: str
    """sha256 of the record as retrieved. Moves when the source moves."""

    retrieved_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.app:
            raise ValueError("SourcePointer.app is required")
        if not self.resource_uri:
            raise ValueError(
                f"SourcePointer for {self.app!r} has no resource_uri. A pointer that "
                "cannot be followed back to the source is not a pointer."
            )
        if len(self.content_hash) != 64:
            raise ValueError(
                f"SourcePointer.content_hash must be a sha256 hex digest, got "
                f"{self.content_hash!r}. Empty or placeholder hashes make citation "
                "verification silently vacuous."
            )


@dataclass(frozen=True, slots=True)
class Evidence:
    """One retrieved record, with everything needed to cite it.

    `text` is what the brain reasons over. `pointer` is how a human checks it.
    """

    id: str
    pointer: SourcePointer
    text: str
    author: str | None = None
    occurred_at: datetime | None = None
    """When the thing happened in the world, as distinct from when we fetched it."""

    labels: tuple[str, ...] = ()
    """Free-form tags from the source: channel, repo, status, folder."""

    raw: dict[str, Any] = field(default_factory=dict)
    """The original payload, kept for re-hashing and debugging."""

    def cite(self) -> str:
        """One-line citation for rendering into an answer or an action body."""
        who = f"{self.author} · " if self.author else ""
        when = (
            self.occurred_at.strftime("%Y-%m-%d")
            if self.occurred_at
            else self.pointer.retrieved_at.strftime("%Y-%m-%d")
        )
        return f"[{self.pointer.app}] {who}{when} — {self.pointer.resource_uri}"


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """What `probe()` found: the scopes this adapter can see, and what it is."""

    app: str
    display_name: str
    scopes: tuple[str, ...]
    """Channels, projects, repos, databases, folders — whatever this app calls them."""

    record_count_estimate: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------


AGENT_SIGNATURE = "Filed by Walnut"
"""Every record the agent writes into an external system carries this line.

The reason is a loop that showed up the first time a live system was connected: the
agent filed a Linear issue titled "…slack reports it", the next ingest read that
issue back as a fresh claim about the patient, the contradiction detector ranked it
above the nurse's message it was summarising, and the next issue the agent filed said
"…linear reports it" — the agent citing itself, one hop further from the evidence on
every pass. Signed writes are recognisable as derivative, and derivative records are
never evidence: they can be shown, linked and undone, but nothing is inferred from
them.
"""


class ActionTier(IntEnum):
    """How much consequence an action carries. Higher tiers need more permission.

    The tier is a property of the *action*, assigned by the adapter that knows what
    the operation actually does — not something the agent gets to choose for itself.
    """

    TRIVIAL = 0
    """Reversible, internal, low-consequence: labels, flags, reactions, links."""

    INTERNAL = 1
    """Real internal writes. Auto-executed, logged, reversible."""

    GATED = 2
    """External-facing, bulk, or destructive. Blocks on human approval."""

    FORBIDDEN = 3
    """Never executed by the agent under any approval. Present so the vocabulary
    can express it and the refusal is typed rather than improvised."""


@dataclass(frozen=True, slots=True)
class ActionCapabilities:
    """What an adapter can do, and how consequential each operation is."""

    app: str
    operations: dict[str, ActionTier]

    def tier_of(self, operation: str) -> ActionTier:
        if operation not in self.operations:
            raise KeyError(
                f"{self.app!r} adapter declares no operation {operation!r}. "
                f"Known: {sorted(self.operations)}"
            )
        return self.operations[operation]


@dataclass(frozen=True, slots=True)
class Action:
    """A proposed write. Construction fails if it is not justified by evidence."""

    app: str
    operation: str
    target: dict[str, Any]
    """What to act on: channel + thread ts, issue id, PR number, page id."""

    payload: dict[str, Any]
    """What to write."""

    justified_by: tuple[str, ...]
    """Evidence ids. **Non-empty, always.** An agent that cannot cite a reason for
    an action does not get to take it."""

    rationale: str = ""
    """One human-readable sentence. Goes into the audit trail and, where the app
    supports it, into the written artefact itself."""

    def __post_init__(self) -> None:
        if not self.justified_by:
            raise ValueError(
                f"Action {self.app}.{self.operation} has no justifying evidence. "
                "Every action must cite the evidence that motivated it — this is "
                "the rule the whole system exists to enforce."
            )


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    """Proof an action happened, and everything needed to reverse it."""

    action_id: str
    action: Action
    result: dict[str, Any]
    """What the app returned: created id, permalink, timestamp."""

    prior_state: dict[str, Any] | None
    """Captured *before* the write. `None` means the action was purely additive and
    undo is a delete/retract rather than a restore."""

    executed_at: datetime = field(default_factory=utcnow)
    undone_at: datetime | None = None

    @property
    def is_undone(self) -> bool:
        return self.undone_at is not None


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


@runtime_checkable
class Adapter(Protocol):
    """Six methods. Three read, three write. Every app implements exactly these."""

    name: str

    # -- read ---------------------------------------------------------------

    def probe(self) -> SourceProfile:
        """Discover what this connection can see. Cheap, read-only, no side effects.

        Called at bind time so a human can approve the scope before the first real
        read, and called again later to detect that the scope has changed.
        """
        ...

    def fetch(self, scope: str | None = None, limit: int = 100) -> list[Evidence]:
        """Pull records as cited evidence.

        `scope` narrows to one channel / project / repo / database / folder. Every
        returned record carries a resolvable pointer and a content hash.
        """
        ...

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        """Re-fetch a single record live, to verify a citation still holds.

        Returns `None` if the record is gone. A caller comparing the returned
        `content_hash` against the pointer's own detects drift: the claim we stored
        is no longer what the source says.
        """
        ...

    # -- write --------------------------------------------------------------

    def capabilities(self) -> ActionCapabilities:
        """Declare every operation this adapter supports and its consequence tier."""
        ...

    def act(self, action: Action) -> ActionReceipt:
        """Perform a write, capturing prior state first so it can be undone.

        Implementations must read-before-write for any operation that overwrites an
        existing value, and record what they saw in `ActionReceipt.prior_state`.
        """
        ...

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        """Reverse a write.

        Retract rather than erase wherever the app allows it: edit the message to
        mark it withdrawn, archive the issue, revert the property but leave the
        note. An undo that destroys the audit trail is not an undo.
        """
        ...
