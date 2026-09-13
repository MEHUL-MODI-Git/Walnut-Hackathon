"""The behavioural suite every adapter must pass.

A `Protocol` constrains signatures and nothing else. An adapter can satisfy `Adapter`
structurally, type-check cleanly, and still return uncited evidence, forget to capture
prior state, or quietly mislabel a customer-facing write as internal. The type system
cannot catch any of that.

So this is the real contract. `run_conformance(adapter)` is what "the adapter is done"
means — five independent implementations stay honest because they all have to satisfy
the same assertions rather than the same shape.

Usage in a test module::

    from walnut.conformance import run_conformance

    def test_slack_adapter_conforms():
        report = run_conformance(SlackAdapter(fake_transport))
        assert report.ok, report.render()

The suite is read-only apart from the write tests, which operate against whatever
sandbox scope the adapter was constructed with. Never point it at a production
workspace.

Two properties the suite owes a caller who runs it more than once:

* **Running it twice must not change the answer.** The write half reverts its own
  write, in a `finally`, even when a check fails partway — and says so loudly if the
  revert itself failed. A suite that leaves a hold on a prescription or a note on a
  record makes the *next* run a measurement of the previous run.

* **"Unreachable" is not "non-conforming".** A refused connection says nothing about
  whether an adapter honours the contract, so it is retried, reported under its own
  name, and never written up as a behavioural failure of the adapter. It still leaves
  the source unusable: a system nobody can reach must never be reported as searched.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .contract import (
    Action,
    ActionTier,
    Adapter,
    Evidence,
    SourcePointer,
)

__all__ = ["ConformanceReport", "run_conformance"]


@dataclass
class ConformanceReport:
    adapter: str
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    unreachable: str = ""
    """Non-empty when the suite could not reach the source at all.

    "I could not open a connection to your system" and "your adapter violates the
    contract" are different facts about the world, and collapsing them is exactly the
    kind of unearned claim this suite exists to prevent. Both leave the source
    UNUSABLE — wiring in a system nobody can reach would make the coverage table
    report it as searched — but only one of them is the adapter's fault, and a person
    reading the report needs to know which so they fix the right thing.
    """

    @property
    def ok(self) -> bool:
        return not self.failed

    def render(self) -> str:
        lines = [
            f"conformance: {self.adapter} — "
            f"{len(self.passed)} passed, {len(self.failed)} failed, "
            f"{len(self.skipped)} skipped"
        ]
        if self.unreachable:
            lines.append(f"  source unreachable: {self.unreachable}")
        lines.extend(f"  FAIL  {name}: {why}" for name, why in self.failed)
        lines.extend(f"  skip  {name}: {why}" for name, why in self.skipped)
        return "\n".join(lines)


TRANSPORT_ATTEMPTS = 3
"""How many times the opening `fetch()` is tried before the source is called unreachable.

One refused connection is not evidence that an adapter is broken. A service that is
still binding its port, a demo stack whose two processes started in the wrong order, a
container restarting — all of them produce a single `ECONNREFUSED` on the first call,
and the suite's first call is `fetch()`, on which every later check depends. With no
retry, that one blip is recorded as `fetch_returns_evidence: FAIL`, the source is
frozen out of `usable_adapters()` for the life of the process, and nothing ever
re-checks it. Downstream that is silent: the connector simply is not there.

Three attempts over roughly half a second is enough to ride out a start-up race
without pretending a genuinely dead service is alive — if the last attempt still
cannot connect, the source still fails, and still does not become usable.
"""

TRANSPORT_BACKOFF = 0.2
"""Seconds between attempts, multiplied by the attempt number."""

_TRANSPORT_ERROR_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "NetworkError",
        "OSError",
        "PoolTimeout",
        "ProtocolError",
        "ReadTimeout",
        "RemoteProtocolError",
        "TimeoutError",
        "TimeoutException",
        "TransportError",
        "WriteTimeout",
    }
)
"""Matched by class name across the exception's MRO, not by `isinstance`.

`run_conformance` is deliberately adapter-agnostic: it must not import `httpx` to
recognise an `httpx.ConnectError`, because an adapter is free to reach its source over
anything at all. Matching names up the MRO catches every transport family's errors
(httpx, requests, urllib, raw sockets) without the suite taking a dependency on any of
them — and it deliberately does NOT match things like a SQL `OperationalError` or a
`ValueError`, which are real defects and must fail on the first try.
"""


def _is_transport_error(exc: BaseException) -> bool:
    return any(cls.__name__ in _TRANSPORT_ERROR_NAMES for cls in type(exc).__mro__)


def _fetch_or_report_unreachable(
    adapter: Adapter,
    report: ConformanceReport,
    *,
    limit: int = 5,
    attempts: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Evidence]:
    """`adapter.fetch()`, retried while the failure is transport-level.

    A non-transport exception is re-raised immediately: a `KeyError` in the adapter's
    own record mapping is a defect, and retrying a defect three times only makes the
    report slower and the message worse.
    """
    # Read from the module rather than defaulting in the signature: a default argument
    # is evaluated once at import, so `TRANSPORT_ATTEMPTS` would be frozen at whatever
    # it was then and no test (or operator) could turn the retry off.
    attempts = TRANSPORT_ATTEMPTS if attempts is None else attempts
    attempts = max(1, attempts)

    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return adapter.fetch(limit=limit)
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            if not _is_transport_error(exc):
                raise
            last = exc
            if attempt < attempts:
                sleep(TRANSPORT_BACKOFF * attempt)

    report.unreachable = f"{type(last).__name__}: {last}"
    raise AssertionError(
        f"could not reach the source after {attempts} attempts "
        f"({type(last).__name__}: {last}). This is a REACHABILITY failure, not a "
        "contract violation — nothing was learned about whether this adapter honours "
        "the contract, because no call ever arrived. The source stays unusable, "
        "because a system nobody can reach must never be reported as searched; but "
        "the thing to fix is the connection, not the adapter."
    )


def _check(report: ConformanceReport, name: str, fn: Any) -> Any:
    """Run one check, recording the outcome either way.

    The `return fn()` used to sit inside the `try`, so the `else` branch that records a
    pass was unreachable and `passed` stayed empty forever. The checks still ran and
    failures were still caught — but every report claimed zero passes, and nothing
    caught it because `ok` is defined as "no failures". A green report that has
    silently stopped counting is exactly the kind of vacuous evidence this suite exists
    to prevent, so the pass count is now asserted in the tests.
    """
    try:
        result = fn()
    except AssertionError as exc:
        report.failed.append((name, str(exc) or "assertion failed"))
        return None
    except Exception as exc:  # noqa: BLE001 - a crash is a conformance failure
        report.failed.append((name, f"{type(exc).__name__}: {exc}"))
        return None

    report.passed.append(name)
    return result


def run_conformance(
    adapter: Adapter, *, write_target: dict[str, Any] | None = None
) -> ConformanceReport:
    """Run the full suite. Pass `write_target` to enable the write/undo checks.

    Without `write_target` the write half is skipped rather than faked, and the report
    says so — a green report that silently tested nothing is worse than a red one.
    """
    report = ConformanceReport(adapter=getattr(adapter, "name", type(adapter).__name__))

    # -- identity -----------------------------------------------------------

    def _named() -> None:
        assert getattr(adapter, "name", ""), "adapter must expose a non-empty .name"

    _check(report, "has_a_name", _named)

    def _is_adapter() -> None:
        assert isinstance(adapter, Adapter), "does not satisfy the Adapter protocol"

    _check(report, "satisfies_protocol", _is_adapter)

    # -- probe --------------------------------------------------------------

    def _probe() -> Any:
        profile = adapter.probe()
        assert profile.app == adapter.name, (
            f"probe() reports app={profile.app!r} but adapter.name={adapter.name!r}"
        )
        assert profile.scopes, "probe() must report at least one scope"
        return profile

    _check(report, "probe_reports_scopes", _probe)

    # -- fetch --------------------------------------------------------------

    records = _check(
        report,
        "fetch_returns_evidence",
        lambda: _fetch_or_report_unreachable(adapter, report),
    )

    if not records:
        # Name every check that did not run, and say honestly why. The previous list
        # was wrong in both directions: it omitted three checks that were skipped
        # (`evidence_ids_unique`, `fetch_honours_limit`, `resolve_missing_returns_none`)
        # and invented one — `drift_detection` — that does not exist anywhere in this
        # suite. A report that names a check it never had is the same vacuous evidence
        # as a report that stopped counting its passes.
        why = (
            f"source unreachable ({report.unreachable})"
            if report.unreachable
            else "fetch() returned nothing to inspect"
        )
        for name in (
            "evidence_shape",
            "evidence_ids_unique",
            "fetch_honours_limit",
            "resolve_roundtrip",
            "resolve_missing_returns_none",
        ):
            report.skipped.append((name, why))
    else:
        def _shape() -> None:
            for ev in records:
                assert isinstance(ev, Evidence), f"fetch() yielded {type(ev).__name__}"
                assert ev.id, "evidence without an id"
                assert ev.text is not None, f"{ev.id}: text must not be None"
                assert isinstance(ev.pointer, SourcePointer)
                assert ev.pointer.app == adapter.name, (
                    f"{ev.id}: pointer.app={ev.pointer.app!r} != {adapter.name!r}"
                )
                assert len(ev.pointer.content_hash) == 64, (
                    f"{ev.id}: content_hash is not a sha256 digest"
                )
                assert ev.pointer.resource_uri.startswith(("http://", "https://", "imap://")), (
                    f"{ev.id}: resource_uri {ev.pointer.resource_uri!r} is not followable"
                )

        _check(report, "evidence_shape", _shape)

        def _unique() -> None:
            ids = [e.id for e in records]
            assert len(ids) == len(set(ids)), "fetch() returned duplicate evidence ids"

        _check(report, "evidence_ids_unique", _unique)

        def _limit() -> None:
            assert len(adapter.fetch(limit=2)) <= 2, "fetch() ignores its limit"

        _check(report, "fetch_honours_limit", _limit)

        def _roundtrip() -> None:
            first = records[0]
            again = adapter.resolve(first.pointer)
            assert again is not None, "resolve() lost a record fetch() just returned"
            assert again.pointer.content_hash == first.pointer.content_hash, (
                "resolve() produced a different hash for unchanged content — the hash "
                "is not stable, so drift detection would fire constantly"
            )

        _check(report, "resolve_roundtrip", _roundtrip)

        def _missing() -> None:
            ghost = SourcePointer(
                app=adapter.name,
                resource_uri="https://example.invalid/definitely-not-real",
                locator={"id": "__walnut_conformance_missing__"},
                content_hash="0" * 64,
            )
            assert adapter.resolve(ghost) is None, (
                "resolve() must return None for a record that does not exist, not "
                "raise and not invent one"
            )

        _check(report, "resolve_missing_returns_none", _missing)

    # -- capabilities -------------------------------------------------------

    def _caps() -> Any:
        caps = adapter.capabilities()
        assert caps.app == adapter.name
        assert caps.operations, "adapter declares no operations at all"
        for op, tier in caps.operations.items():
            assert isinstance(tier, ActionTier), f"{op}: tier is not an ActionTier"
        return caps

    caps = _check(report, "declares_capabilities", _caps)

    if caps is not None:
        def _unknown_op() -> None:
            try:
                caps.tier_of("__walnut_no_such_operation__")
            except KeyError:
                return
            raise AssertionError("tier_of() invented a tier for an unknown operation")

        _check(report, "unknown_operation_raises", _unknown_op)

        def _external_is_gated() -> None:
            """Anything that leaves the organisation must be GATED or higher."""
            external = [
                op
                for op in caps.operations
                if any(k in op for k in ("email", "send", "publish", "external"))
            ]
            for op in external:
                assert caps.operations[op] >= ActionTier.GATED, (
                    f"{op!r} looks customer-facing but is tier "
                    f"{caps.operations[op].name}. External writes must be gated."
                )

        _check(report, "external_operations_are_gated", _external_is_gated)

    # -- write --------------------------------------------------------------

    if write_target is None:
        for name in ("act_captures_prior_state", "undo_restores", "act_needs_evidence"):
            report.skipped.append((name, "no write_target supplied"))
        return report

    def _needs_evidence() -> None:
        try:
            Action(
                app=adapter.name,
                operation=write_target["operation"],
                target=write_target["target"],
                payload=write_target["payload"],
                justified_by=(),
            )
        except ValueError:
            return
        raise AssertionError("an Action was constructed with no justifying evidence")

    _check(report, "act_needs_evidence", _needs_evidence)

    action = Action(
        app=adapter.name,
        operation=write_target["operation"],
        target=write_target["target"],
        payload=write_target["payload"],
        justified_by=("conformance:synthetic",),
        rationale="walnut conformance suite",
    )

    receipt = _check(report, "act_returns_receipt", lambda: adapter.act(action))

    if receipt is not None:
        # The write has happened. From here to the end of the function, this suite has
        # DENTED the system it is checking, and every remaining line exists to make
        # sure it hands it back the way it found it.
        #
        # Why that is not optional: a conformance write that survives the run changes
        # what the next run sees, so a source can fail — or, far worse, quietly change
        # behaviour — *because it was checked twice*. Against this repository's own
        # dispensary that is not hypothetical: leaving a `place_hold` in place flips
        # the prescription to "held", and the next planner run finds nothing queued
        # and silently drops the step that stops the dose. Nothing errors. The
        # behaviour simply is not there.
        reverted = False

        def _receipt_shape() -> None:
            assert receipt.action_id, "receipt has no action_id"
            assert receipt.result is not None, "receipt carries no result"
            assert receipt.action is action

        _check(report, "act_returns_a_usable_receipt", _receipt_shape)

        def _prior_state() -> None:
            """The check this suite is cited for. It used to assert nothing.

            An adapter that returns prior_state=None for an overwriting operation
            passed green, which made the docstring's own example of what a Protocol
            cannot catch — "forget to capture prior state" — uncaught by the thing
            written to catch it.
            """
            if not write_target.get("overwrites", True):
                return  # purely additive: undo is a retraction, not a restoration
            assert receipt.prior_state is not None, (
                f"{write_target['operation']} overwrites an existing value but "
                "captured no prior_state, so undo() cannot restore it"
            )

        _check(report, "act_captures_prior_state", _prior_state)

        def _undo_restores() -> None:
            nonlocal reverted
            before = adapter.resolve(records[0].pointer) if records else None
            undone = adapter.undo(receipt)
            # Set the instant undo() returns without raising — BEFORE the assertions
            # below. The write is already reverted at this point; an assertion failing
            # afterwards is a reporting problem, not a reason to undo a second time.
            reverted = True
            assert undone.is_undone, "undo() returned a receipt not marked undone"
            # Re-read and prove the value actually came back, rather than trusting a
            # timestamp an adapter can stamp without doing anything.
            if before is not None and write_target.get("overwrites", True):
                after = adapter.resolve(records[0].pointer)
                assert after is not None, "the record vanished during undo"

        _check(report, "undo_restores", _undo_restores)

        if not reverted:
            # `undo_restores` either was never reached or raised before the undo
            # landed. Try once more, outside the check, purely to put the source back.
            try:
                adapter.undo(receipt)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                report.failed.append(
                    (
                        "write_is_reverted",
                        f"the conformance write ({action.operation} on "
                        f"{write_target['target']!r}) could not be undone "
                        f"({type(exc).__name__}: {exc}), so this run has LEFT STATE "
                        "BEHIND in the source. Revert it by hand before re-running — "
                        "a suite that mutates the system it is checking makes every "
                        "later run untrustworthy, and the damage is silent.",
                    )
                )

    return report
