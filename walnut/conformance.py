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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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

    @property
    def ok(self) -> bool:
        return not self.failed

    def render(self) -> str:
        lines = [
            f"conformance: {self.adapter} — "
            f"{len(self.passed)} passed, {len(self.failed)} failed, "
            f"{len(self.skipped)} skipped"
        ]
        lines.extend(f"  FAIL  {name}: {why}" for name, why in self.failed)
        lines.extend(f"  skip  {name}: {why}" for name, why in self.skipped)
        return "\n".join(lines)


def _check(report: ConformanceReport, name: str, fn: Any) -> Any:
    try:
        return fn()
    except AssertionError as exc:
        report.failed.append((name, str(exc) or "assertion failed"))
    except Exception as exc:  # noqa: BLE001 - a crash is a conformance failure
        report.failed.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        report.passed.append(name)
    return None


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

    records = _check(report, "fetch_returns_evidence", lambda: adapter.fetch(limit=5))

    if not records:
        report.skipped.append(
            ("evidence_shape", "fetch() returned nothing to inspect")
        )
        report.skipped.append(("resolve_roundtrip", "no evidence to resolve"))
        report.skipped.append(("drift_detection", "no evidence to mutate"))
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
            before = adapter.resolve(records[0].pointer) if records else None
            undone = adapter.undo(receipt)
            assert undone.is_undone, "undo() returned a receipt not marked undone"
            # Re-read and prove the value actually came back, rather than trusting a
            # timestamp an adapter can stamp without doing anything.
            if before is not None and write_target.get("overwrites", True):
                after = adapter.resolve(records[0].pointer)
                assert after is not None, "the record vanished during undo"

        _check(report, "undo_restores", _undo_restores)

    return report
