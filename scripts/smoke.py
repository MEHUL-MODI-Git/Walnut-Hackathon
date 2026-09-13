"""Live-credential smoke test.

Five connectors — Slack, Linear, GitHub, Notion, Email — are built and pass their
conformance suite against fake transports. None of them has ever spoken to a real
API. The moment an operator pastes real tokens into `.env`, the question that
matters is not "does the code exist" but "does *this* credential actually work,
and if not, exactly why" — answered in the couple of minutes before a demo, not
discovered mid-demo.

This script runs, in order, for every app that has credentials present in the
environment, and **stops that app at its first failure** — but never lets one
broken token stop the other four:

    1. CONSTRUCT     build the live adapter from env vars
    2. PROBE         call probe(); what scopes can this credential actually see
    3. FETCH         call fetch(limit=3); how many records came back
    4. EVIDENCE      every record has a real 64-char sha256 content_hash and a
                      followable http(s):// or imap:// resource_uri
    5. RESOLVE       re-resolve the first record; the content hash must be STABLE
    6. CONFORMANCE   run_conformance(adapter), read-only
    7. TARGETS       for every operation the adapter declares, can targeting.py
                      build a real live write target from the evidence just fetched

Steps 1-5 are exactly what `ConnectionManager.connect()` does at bind time; this
script exists because that path is only ever exercised interactively, one app at a
time, through the console. Step 7 exists because `target_for()` translates a
`SourcePointer.locator` into the keys a live adapter's `act()` actually reads —
and that translation has no fixture to fail against, since fixture data is
addressed by plain `id` regardless of the app. A bug there is invisible until the
first live write, which is the worst possible moment to discover it.

Read-only by default: nothing above ever calls `act()`. `--write` opts into ONE
harmless, reversible write per app — a Slack reaction, a Linear/GitHub label
re-application (or a comment, if the record has no existing label to reuse), a
Notion block, an email draft — immediately followed by `undo()`, with both
outcomes reported. Nothing here is ever `send_email`, which the adapter itself
refuses to undo.

When a step fails, the report always carries three things together: the
exception's type and message, the app's `CredentialSpec.gotcha` (the specific,
named mistake this app's tokens are known for — it is usually the actual answer),
and `CredentialSpec.where` (where a human goes to fix it).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from walnut.brain import Fact
from walnut.connections import APP_SPECS, ConnectionManager
from walnut.conformance import run_conformance
from walnut.contract import Action, Adapter, Evidence
from walnut.targeting import REQUIRED_TARGET_KEYS, missing_context, target_for

__all__ = [
    "APPS",
    "AppResult",
    "StepResult",
    "compute_exit_code",
    "main",
    "parse_args",
    "render_report",
    "run_all",
    "run_app",
]

APPS: tuple[str, ...] = tuple(APP_SPECS)
"""slack, linear, github, notion, email — in `APP_SPECS`' own order, so this file
never drifts from the set of apps `connections.py` actually knows about."""

_WRITE_MARKER = (
    "Walnut smoke test — verifying the live write path. This will be undone "
    "immediately."
)

# Mirrors `ConnectionManager.load_from_environment`'s `env_map` exactly. Kept as a
# private copy rather than imported: that mapping is local to a method body, not a
# module constant, and duplicating five short lines is a smaller liability than
# reaching into a method's closure. If the env var names in connections.py change,
# this table has to change with them — the same is true of any other caller.
_ENV_MAP: dict[str, dict[str, str]] = {
    "slack": {"token": "SLACK_BOT_TOKEN"},
    "linear": {"api_key": "LINEAR_API_KEY"},
    "github": {"token": "GITHUB_TOKEN", "repo": "GITHUB_REPO"},
    "notion": {"token": "NOTION_TOKEN", "database_id": "NOTION_DATABASE_ID"},
    "email": {
        "host": "EMAIL_HOST",
        "smtp_host": "EMAIL_SMTP_HOST",
        "user": "EMAIL_USER",
        "password": "EMAIL_PASSWORD",
    },
}

BuildAdapter = Callable[[str, dict[str, str]], Adapter]

# `ConnectionManager._build` is the actual construction logic production code
# uses — same lazy imports, same constructor calls. Reusing it here rather than
# re-deriving "how do I build a live SlackAdapter" is what keeps this script from
# silently drifting out of sync with `connections.py` the next time an adapter's
# constructor signature changes. Tests inject a fake in its place; see
# `tests/test_smoke_harness.py`.
_default_build_adapter: BuildAdapter = ConnectionManager._build


# ---------------------------------------------------------------------------
# result types
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    step: str
    ok: bool
    detail: str


@dataclass
class AppResult:
    """Everything observed about one app's smoke run."""

    app: str
    skipped: bool = False
    skip_reason: str = ""
    steps: list[StepResult] = field(default_factory=list)
    write_attempted: bool = False
    write_ok: bool | None = None
    write_detail: str = ""
    undo_ok: bool | None = None
    undo_detail: str = ""

    @property
    def failed(self) -> bool:
        """Whether this app counts against the process exit code.

        A skipped app (no credentials offered) is not a failure — silence on
        absence is the documented behaviour of `load_from_environment` too. An
        app that never recorded a single step is a harness bug, not a credential
        problem, and counts as failed rather than silently passing.
        """
        if self.skipped:
            return False
        if not self.steps:
            return True
        if any(not s.ok for s in self.steps):
            return True
        if self.write_attempted and (self.write_ok is False or self.undo_ok is False):
            return True
        return False

    @property
    def step_reached(self) -> str:
        if self.skipped:
            return "-"
        if not self.steps:
            return "-"
        return self.steps[-1].step


# ---------------------------------------------------------------------------
# small formatting helpers
# ---------------------------------------------------------------------------


def _exc_detail(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _step(name: str, app: str, ok: bool, detail: str) -> StepResult:
    """Build one `StepResult`, attaching the app's gotcha and where-to-fix-it on
    failure. This is the single place that guarantees error reporting stays the
    point: every failing step, whatever raised it, carries the same three things —
    what happened, the app's known gotcha, and where a human goes to fix it.
    """
    if not ok:
        spec = APP_SPECS[app]
        hints = []
        if spec.gotcha:
            hints.append(f"gotcha — {spec.gotcha}")
        hints.append(f"where — {spec.where}")
        detail = f"{detail}\n      " + "\n      ".join(hints)
    return StepResult(name, ok, detail)


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""


def _truncate(text: str, width: int = 88) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _credentials_for(app: str) -> dict[str, str]:
    return {field_key: os.environ.get(env_var, "") for field_key, env_var in _ENV_MAP[app].items()}


def _has_required_credentials(app: str, creds: dict[str, str]) -> bool:
    required = [f.key for f in APP_SPECS[app].fields if not f.optional]
    return all((creds.get(k) or "").strip() for k in required)


def _context_for(app: str, creds: dict[str, str]) -> dict[str, Any]:
    """The container context `target_for` needs for creation operations, which no
    fetched record can carry on its own — a GitHub repo, a Notion database, a
    Linear team. `repo`/`database_id` come straight from the credentials the
    operator already supplied; `LINEAR_TEAM_ID` has no home in `APP_SPECS` yet, so
    it is read directly, and its absence is reported by `missing_context()` rather
    than treated as a construction failure.
    """
    context: dict[str, Any] = {}
    if app == "github":
        repo = creds.get("repo", "")
        context["repo"] = repo
        context["github_repo"] = repo
    if app == "notion":
        database_id = creds.get("database_id", "")
        context["database_id"] = database_id
        context["notion_database_id"] = database_id
    if app == "linear":
        team_id = os.environ.get("LINEAR_TEAM_ID", "")
        context["team_id"] = team_id
        context["linear_team_id"] = team_id
    return context


def _evidence_to_fact(app: str, evidence: Evidence) -> Fact:
    """`target_for` takes `Fact`, not `Evidence` — the brain's internal shape, not
    the adapter's. The two carry the same pointer and text; this is the
    translation, done once here rather than duplicated at every call site."""
    return Fact(
        node_id=evidence.id,
        text=evidence.text,
        app=app,
        pointer=evidence.pointer,
        author=evidence.author,
        occurred_at=evidence.occurred_at,
    )


# ---------------------------------------------------------------------------
# the seven read-only steps
# ---------------------------------------------------------------------------


def _step_construct(app: str, creds: dict[str, str], build_adapter: BuildAdapter) -> tuple[Adapter | None, StepResult]:
    try:
        adapter = build_adapter(app, creds)
    except Exception as exc:  # noqa: BLE001 - any construction failure is reportable, not fatal to the run
        return None, _step("construct", app, False, _exc_detail(exc))
    return adapter, _step("construct", app, True, f"{type(adapter).__name__} constructed")


def _step_probe(app: str, adapter: Adapter) -> tuple[Any, StepResult]:
    try:
        profile = adapter.probe()
    except Exception as exc:  # noqa: BLE001
        return None, _step("probe", app, False, _exc_detail(exc))
    if not profile.scopes:
        return profile, _step(
            "probe", app, False,
            "connected, but the credential can see nothing (probe() reported zero scopes)",
        )
    preview = ", ".join(profile.scopes[:5])
    more = f" (+{len(profile.scopes) - 5} more)" if len(profile.scopes) > 5 else ""
    return profile, _step("probe", app, True, f"{len(profile.scopes)} scope(s) visible: {preview}{more}")


def _step_fetch(app: str, adapter: Adapter) -> tuple[list[Evidence], StepResult]:
    try:
        records = adapter.fetch(limit=3)
    except Exception as exc:  # noqa: BLE001
        return [], _step("fetch", app, False, _exc_detail(exc))
    if not records:
        return [], _step(
            "fetch", app, False,
            "fetch(limit=3) returned zero records — nothing to check evidence shape, "
            "resolve stability, or write targets against",
        )
    return records, _step("fetch", app, True, f"{len(records)} record(s) returned")


def _step_evidence(app: str, records: list[Evidence]) -> StepResult:
    problems: list[str] = []
    for record in records:
        content_hash = record.pointer.content_hash
        if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash.lower()):
            problems.append(f"{record.id}: content_hash is not a 64-char sha256 hex digest ({content_hash!r})")
        resource_uri = record.pointer.resource_uri
        if not resource_uri.startswith(("http://", "https://", "imap://")):
            problems.append(f"{record.id}: resource_uri is not followable ({resource_uri!r})")
    if problems:
        return _step("evidence", app, False, "; ".join(problems))
    return _step("evidence", app, True, f"{len(records)} record(s) carry a real content_hash and a followable resource_uri")


def _step_resolve(app: str, adapter: Adapter, first: Evidence) -> StepResult:
    try:
        again = adapter.resolve(first.pointer)
    except Exception as exc:  # noqa: BLE001
        return _step("resolve", app, False, _exc_detail(exc))
    if again is None:
        return _step(
            "resolve", app, False,
            "resolve() returned None for a record fetch() just returned — the pointer's "
            "locator does not round-trip",
        )
    if again.pointer.content_hash != first.pointer.content_hash:
        return _step(
            "resolve", app, False,
            f"content_hash changed on re-resolve ({first.pointer.content_hash[:12]}... -> "
            f"{again.pointer.content_hash[:12]}...) — an unstable hash means drift "
            "detection would fire constantly",
        )
    return _step("resolve", app, True, "content_hash is stable across resolve()")


def _step_conformance(app: str, adapter: Adapter) -> StepResult:
    report = run_conformance(adapter)  # no write_target: read-only, as documented
    detail = f"{len(report.passed)} passed, {len(report.failed)} failed, {len(report.skipped)} skipped"
    if report.failed:
        detail += " — " + "; ".join(f"{name}: {why}" for name, why in report.failed)
    return _step("conformance", app, report.ok, detail)


def _step_targets(app: str, adapter: Adapter, first: Evidence, context: dict[str, Any]) -> StepResult:
    try:
        caps = adapter.capabilities()
    except Exception as exc:  # noqa: BLE001
        return _step("targets", app, False, _exc_detail(exc))

    fact = _evidence_to_fact(app, first)
    facts = [fact]

    passed = failed = skipped = 0
    lines: list[str] = []
    for operation in caps.operations:
        required = REQUIRED_TARGET_KEYS.get(app, {}).get(operation, ())
        target = target_for(app, operation, facts, context=context)
        if target is None:
            reason = missing_context(app, operation, context)
            if reason is not None:
                # Genuinely needs container context this smoke run was not given
                # (no GITHUB_REPO / NOTION_DATABASE_ID / LINEAR_TEAM_ID), not a
                # broken mapping — informational, not a failure.
                skipped += 1
                lines.append(f"{operation}: skip ({reason})")
            else:
                failed += 1
                lines.append(
                    f"{operation}: FAIL — target_for() produced no target from a live "
                    "record; check targeting.py's locator mapping for this app/op"
                )
            continue
        missing_keys = [k for k in required if target.get(k) in (None, "")]
        if missing_keys:
            failed += 1
            lines.append(f"{operation}: FAIL — missing key(s) {missing_keys} in {target}")
        else:
            passed += 1
            lines.append(f"{operation}: ok {target}")

    detail = f"{passed} ok, {failed} failed, {skipped} skipped — " + "; ".join(lines)
    return _step("targets", app, failed == 0, detail)


# ---------------------------------------------------------------------------
# the optional write
# ---------------------------------------------------------------------------


def _existing_label(app: str, evidence: Evidence) -> str | None:
    """An existing label already on this record, if there is one.

    Re-applying a label that is already present is a real round trip through
    `act()`/`undo()` — Linear and GitHub both read current state before writing,
    so the write and its undo are exercised for real — without inventing a label
    that may not exist in this workspace, which would fail for a reason that has
    nothing to do with whether the connector works.
    """
    if app == "linear":
        nodes = ((evidence.raw or {}).get("labels") or {}).get("nodes") or []
        label_id = nodes[0].get("id") if nodes else None
        return str(label_id) if label_id else None
    if app == "github":
        synthetic = {"pull_request", "issue", "open", "closed", "merged"}
        real_labels = [label for label in evidence.labels if label not in synthetic]
        return real_labels[0] if real_labels else None
    return None


def _plan_write(
    app: str, first: Evidence | None, context: dict[str, Any], creds: dict[str, str]
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """Choose the one harmless, reversible write for this app, and build its
    target and payload from live data. Returns `None` when no safe target can be
    built — never guesses at a destructive alternative."""
    if app == "email":
        # save_draft needs no existing record: target_for special-cases it to the
        # operator's drafts folder regardless of what was fetched.
        return (
            "save_draft",
            {"folder": "Drafts"},
            {"to": creds.get("user", ""), "subject": "Walnut smoke test", "body": _WRITE_MARKER},
        )

    if first is None:
        return None
    fact = _evidence_to_fact(app, first)

    if app == "slack":
        target = target_for(app, "add_reaction", [fact])
        return ("add_reaction", target, {"emoji": "eyes"}) if target else None

    if app == "notion":
        target = target_for(app, "append_block", [fact])
        return ("append_block", target, {"text": _WRITE_MARKER, "block_type": "callout"}) if target else None

    if app == "linear":
        label_id = _existing_label("linear", first)
        if label_id:
            target = target_for(app, "add_label", [fact])
            if target:
                return ("add_label", target, {"label_id": label_id})
        target = target_for(app, "comment", [fact])
        return ("comment", target, {"body": _WRITE_MARKER}) if target else None

    if app == "github":
        label_name = _existing_label("github", first)
        if label_name:
            target = target_for(app, "add_label", [fact])
            if target:
                return ("add_label", target, {"label": label_name})
        target = target_for(app, "comment", [fact])
        return ("comment", target, {"body": _WRITE_MARKER}) if target else None

    return None


def _attempt_write(
    app: str, adapter: Adapter, first: Evidence | None, context: dict[str, Any], creds: dict[str, str]
) -> tuple[StepResult, StepResult | None]:
    """Attempt the one planned write, then immediately undo it. Returns the write
    result and, only if a write actually happened, the undo result."""
    plan = _plan_write(app, first, context, creds)
    if plan is None:
        return _step(
            "write", app, False,
            "no safe write target could be built (no evidence fetched, or targeting "
            "could not aim at it — see the targets step above)",
        ), None
    operation, target, payload = plan

    try:
        action = Action(
            app=app,
            operation=operation,
            target=target,
            payload=payload,
            justified_by=("walnut-smoke:live-credential-check",),
            rationale="Walnut smoke test: one harmless, reversible write, undone immediately.",
        )
    except ValueError as exc:
        return _step("write", app, False, f"{operation}: Action refused construction — {exc}"), None

    try:
        receipt = adapter.act(action)
    except Exception as exc:  # noqa: BLE001
        return _step("write", app, False, f"{operation}: {_exc_detail(exc)}"), None

    write_result = _step("write", app, True, f"{operation} succeeded -> {receipt.result}")

    try:
        undone = adapter.undo(receipt)
    except Exception as exc:  # noqa: BLE001
        return write_result, _step("undo", app, False, f"{operation}: {_exc_detail(exc)}")

    if not undone.is_undone:
        return write_result, _step("undo", app, False, "undo() returned a receipt not marked undone")
    return write_result, _step("undo", app, True, f"undone at {undone.undone_at.isoformat()}")


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def run_app(app: str, *, write: bool = False, build_adapter: BuildAdapter | None = None) -> AppResult:
    """Run every step for one app, stopping at the first failure.

    `build_adapter` defaults to the real `ConnectionManager._build`; tests inject
    a fake so this function never has to touch a network to be exercised.
    """
    build_adapter = build_adapter or _default_build_adapter
    result = AppResult(app=app)

    creds = _credentials_for(app)
    if not _has_required_credentials(app, creds):
        result.skipped = True
        result.skip_reason = "no credentials in the environment for this app"
        return result

    adapter, step = _step_construct(app, creds, build_adapter)
    result.steps.append(step)
    if adapter is None:
        return result

    profile, step = _step_probe(app, adapter)
    result.steps.append(step)
    if not step.ok:
        return result

    records, step = _step_fetch(app, adapter)
    result.steps.append(step)
    if not step.ok:
        return result

    step = _step_evidence(app, records)
    result.steps.append(step)
    if not step.ok:
        return result

    step = _step_resolve(app, adapter, records[0])
    result.steps.append(step)
    if not step.ok:
        return result

    step = _step_conformance(app, adapter)
    result.steps.append(step)
    if not step.ok:
        return result

    context = _context_for(app, creds)
    step = _step_targets(app, adapter, records[0], context)
    result.steps.append(step)
    if not step.ok:
        return result

    if write:
        result.write_attempted = True
        write_step, undo_step = _attempt_write(app, adapter, records[0], context, creds)
        result.write_ok = write_step.ok
        result.write_detail = write_step.detail
        if undo_step is not None:
            result.undo_ok = undo_step.ok
            result.undo_detail = undo_step.detail

    return result


def run_all(apps: Sequence[str], *, write: bool = False, build_adapter: BuildAdapter | None = None) -> list[AppResult]:
    """Run every app independently. One app's exception never reaches this loop —
    `run_app` catches everything a live call can raise — so a broken token in app
    N never prevents app N+1 from running."""
    return [run_app(app, write=write, build_adapter=build_adapter) for app in apps]


def compute_exit_code(results: list[AppResult]) -> int:
    return 1 if any(r.failed for r in results) else 0


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def render_report(results: list[AppResult], *, write: bool) -> str:
    lines: list[str] = []
    rule = "=" * 78
    lines.append(rule)
    lines.append("Walnut live-credential smoke test")
    mode = "read-only + one reversible write per app" if write else "read-only (pass --write to also exercise act()/undo())"
    lines.append(f"mode: {mode}")
    lines.append(rule)

    for result in results:
        lines.append("")
        header = f"-- {result.app} "
        lines.append(header + "-" * max(0, 78 - len(header)))
        if result.skipped:
            lines.append(f"  SKIPPED — {result.skip_reason}")
            continue
        for step in result.steps:
            mark = "PASS" if step.ok else "FAIL"
            lines.append(f"  [{mark}] {step.step:<12} {step.detail}")
        if result.write_attempted:
            mark = "PASS" if result.write_ok else "FAIL"
            lines.append(f"  [{mark}] {'write':<12} {result.write_detail}")
            if result.undo_ok is not None:
                mark = "PASS" if result.undo_ok else "FAIL"
                lines.append(f"  [{mark}] {'undo':<12} {result.undo_detail}")

    lines.append("")
    lines.append(rule)
    lines.append(f"{'app':<10}{'step reached':<16}{'result':<10}detail")
    lines.append("-" * 78)
    for result in results:
        if result.skipped:
            lines.append(f"{result.app:<10}{'-':<16}{'SKIPPED':<10}{result.skip_reason}")
            continue
        last = result.steps[-1] if result.steps else None
        detail = _truncate(_first_line(last.detail)) if last else "(no steps recorded)"
        word = "FAIL" if result.failed else "PASS"
        lines.append(f"{result.app:<10}{result.step_reached:<16}{word:<10}{detail}")
        if result.write_attempted:
            word = "PASS" if result.write_ok else "FAIL"
            lines.append(f"{result.app:<10}{'write':<16}{word:<10}{_truncate(_first_line(result.write_detail))}")
            if result.undo_ok is not None:
                word = "PASS" if result.undo_ok else "FAIL"
                lines.append(f"{result.app:<10}{'undo':<16}{word:<10}{_truncate(_first_line(result.undo_detail))}")
    lines.append(rule)

    tested = [r for r in results if not r.skipped]
    skipped = [r for r in results if r.skipped]
    failed = [r for r in tested if r.failed]
    passed = [r for r in tested if not r.failed]

    if not tested:
        lines.append(
            "No credentials found in the environment for any app — nothing to test. "
            "Paste tokens into .env (SLACK_BOT_TOKEN / LINEAR_API_KEY / GITHUB_TOKEN / "
            "NOTION_TOKEN / EMAIL_HOST+EMAIL_USER+EMAIL_PASSWORD) and re-run."
        )
    else:
        lines.append(
            f"summary: {len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped "
            f"of {len(results)} app(s)"
        )
        if failed:
            lines.append(f"FAILED: {', '.join(r.app for r in failed)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Live-credential smoke test for Walnut's five connectors. Tests every "
            "app with credentials present in the environment; strictly read-only "
            "unless --write is passed."
        ),
    )
    parser.add_argument(
        "--app",
        choices=APPS,
        default=None,
        help="Test only this app instead of every app with credentials present.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "After the read-only checks pass, attempt one harmless, reversible "
            "write per app (a reaction, a label/comment, a draft) and immediately "
            "undo it. Default is strictly read-only: act() is never called."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    apps = [args.app] if args.app else list(APPS)
    results = run_all(apps, write=args.write)
    print(render_report(results, write=args.write))
    return compute_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
