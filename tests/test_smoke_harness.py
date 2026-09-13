"""Tests for the smoke-test harness itself.

`scripts/smoke.py` exists to be trusted the one time it matters: the moment real
tokens land in `.env`, under time pressure, with no chance for a dry run against
a live account. It cannot be tested against live APIs — there are none in this
environment — so every guarantee it makes is proven here with a fake `Adapter`
that never opens a socket, injected through `run_app`'s `build_adapter` seam.

What is proven:

* An app with no credentials in the environment is SKIPPED, not FAILED.
* A `probe()` that raises is reported with the app's `CredentialSpec.gotcha`
  attached, not just the bare exception.
* One app failing never stops the others from running.
* The read-only default never calls `act()`.
* `--write` (via `run_app(write=True)`) calls `act()` and then `undo()`.
* The process-level exit code is non-zero exactly when some app failed.
"""

from __future__ import annotations

import sys
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

# `scripts/` is deliberately not a package (the build brief asks for exactly two
# new files: this test and the script itself) — import it by path instead of
# adding an __init__.py that was never requested.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import smoke  # noqa: E402

from walnut.connections import APP_SPECS  # noqa: E402
from walnut.contract import (  # noqa: E402
    Action,
    ActionCapabilities,
    ActionReceipt,
    ActionTier,
    Evidence,
    SourcePointer,
    SourceProfile,
    content_hash,
    utcnow,
)

# ---------------------------------------------------------------------------
# a fake Adapter, shaped like the live Slack adapter, that never touches a network
# ---------------------------------------------------------------------------


def _slack_evidence() -> Evidence:
    """One record shaped exactly like `SlackAdapter._to_evidence` would build,
    real 64-char hash and https:// URI included, so the EVIDENCE and TARGETS
    steps see live-shaped data rather than a shortcut."""
    locator = {"channel": "C_ENG", "ts": "1700000000.000100"}
    pointer = SourcePointer(
        app="slack",
        resource_uri="https://meridian.slack.com/archives/C_ENG/p1700000000000100",
        locator=locator,
        content_hash=content_hash(locator),
    )
    return Evidence(id="C_ENG:1700000000.000100", pointer=pointer, text="ship it", author="U01")


class FakeAdapter:
    """A minimal, fully-controllable stand-in for a live `Adapter`.

    Every failure mode the harness has to survive is a constructor flag here:
    an exception from any read method, an empty probe, an unstable resolve hash,
    a missing act/undo. `act_calls`/`undo_calls` let tests assert the write path
    ran, or did not.
    """

    def __init__(
        self,
        app: str,
        *,
        scopes: tuple[str, ...] = ("general",),
        records: list[Evidence] | None = None,
        probe_error: Exception | None = None,
        fetch_error: Exception | None = None,
        resolve_returns_none: bool = False,
        unstable_resolve: bool = False,
        operations: dict[str, ActionTier] | None = None,
        act_error: Exception | None = None,
        undo_error: Exception | None = None,
    ) -> None:
        self.name = app
        self._scopes = scopes
        self._records = records if records is not None else [_slack_evidence()]
        self._probe_error = probe_error
        self._fetch_error = fetch_error
        self._resolve_returns_none = resolve_returns_none
        self._unstable_resolve = unstable_resolve
        self._operations = operations or {"add_reaction": ActionTier.TRIVIAL}
        self._act_error = act_error
        self._undo_error = undo_error
        self.act_calls: list[Action] = []
        self.undo_calls: list[ActionReceipt] = []

    def probe(self) -> SourceProfile:
        if self._probe_error is not None:
            raise self._probe_error
        return SourceProfile(app=self.name, display_name=self.name, scopes=self._scopes)

    def fetch(self, scope: str | None = None, limit: int = 100) -> list[Evidence]:
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._records[:limit]

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        if self._resolve_returns_none:
            return None
        # Match on locator, the way every live adapter does, so a pointer that
        # does not correspond to a real fetched record comes back None — exactly
        # what `run_conformance`'s `resolve_missing_returns_none` check requires.
        match = next((r for r in self._records if r.pointer.locator == pointer.locator), None)
        if match is None:
            return None
        if self._unstable_resolve:
            return Evidence(
                id=match.id,
                pointer=SourcePointer(
                    app=self.name,
                    resource_uri=match.pointer.resource_uri,
                    locator=match.pointer.locator,
                    content_hash="1" * 64,
                ),
                text=match.text,
            )
        return match

    def capabilities(self) -> ActionCapabilities:
        return ActionCapabilities(app=self.name, operations=dict(self._operations))

    def act(self, action: Action) -> ActionReceipt:
        self.act_calls.append(action)
        if self._act_error is not None:
            raise self._act_error
        return ActionReceipt(action_id="fake-1", action=action, result={"ok": True}, prior_state=None)

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        self.undo_calls.append(receipt)
        if self._undo_error is not None:
            raise self._undo_error
        return ActionReceipt(
            action_id=receipt.action_id,
            action=receipt.action,
            result=receipt.result,
            prior_state=receipt.prior_state,
            executed_at=receipt.executed_at,
            undone_at=utcnow(),
        )


def _builder(adapter: FakeAdapter) -> smoke.BuildAdapter:
    """A `build_adapter` that hands back a pre-made fake regardless of creds."""
    return lambda app, creds: adapter


def _clear_all_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_map in smoke._ENV_MAP.values():
        for env_var in env_map.values():
            monkeypatch.delenv(env_var, raising=False)


# ---------------------------------------------------------------------------
# skip vs fail
# ---------------------------------------------------------------------------


def test_app_with_no_credentials_is_skipped_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)

    calls: list[str] = []

    def build_adapter(app: str, creds: dict[str, str]) -> FakeAdapter:
        calls.append(app)
        return FakeAdapter(app)

    result = smoke.run_app("slack", build_adapter=build_adapter)

    assert result.skipped is True
    assert "credentials" in result.skip_reason
    assert result.failed is False
    # A skipped app never even tries to construct an adapter.
    assert calls == []


def test_partial_credentials_are_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub requires both a token and a repo; a token with no repo is not
    enough to attempt a connection — same rule `ConnectionManager.connect()`
    enforces for missing required fields."""
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    # GITHUB_REPO left unset.

    result = smoke.run_app("github", build_adapter=_builder(FakeAdapter("github")))

    assert result.skipped is True
    assert result.failed is False


# ---------------------------------------------------------------------------
# a failing step carries the gotcha
# ---------------------------------------------------------------------------


def test_probe_failure_is_reported_with_the_gotcha_attached(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    boom = RuntimeError("missing_scope")
    adapter = FakeAdapter("slack", probe_error=boom)

    result = smoke.run_app("slack", build_adapter=_builder(adapter))

    assert result.failed is True
    assert result.step_reached == "probe"
    probe_step = next(s for s in result.steps if s.step == "probe")
    assert probe_step.ok is False
    assert "RuntimeError: missing_scope" in probe_step.detail
    # The gotcha is not optional trivia here — it is the diagnosis.
    assert APP_SPECS["slack"].gotcha in probe_step.detail
    assert APP_SPECS["slack"].where in probe_step.detail


def test_probe_with_zero_scopes_fails_with_diagnosis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Notion's own gotcha: the integration connects but has not been shared
    with any page, so probe() reports no scopes at all — not an exception."""
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("NOTION_TOKEN", "secret_fake")

    adapter = FakeAdapter("notion", scopes=())
    result = smoke.run_app("notion", build_adapter=_builder(adapter))

    assert result.failed is True
    assert result.step_reached == "probe"
    probe_step = next(s for s in result.steps if s.step == "probe")
    assert "zero scopes" in probe_step.detail
    assert APP_SPECS["notion"].gotcha in probe_step.detail


def test_construct_failure_stops_before_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_fake")

    def build_adapter(app: str, creds: dict[str, str]) -> FakeAdapter:
        raise ValueError("bad constructor args")

    result = smoke.run_app("linear", build_adapter=build_adapter)

    assert result.failed is True
    assert result.step_reached == "construct"
    assert len(result.steps) == 1
    assert "ValueError: bad constructor args" in result.steps[0].detail
    assert APP_SPECS["linear"].gotcha in result.steps[0].detail


# ---------------------------------------------------------------------------
# one broken app never stops the others
# ---------------------------------------------------------------------------


def test_one_failing_app_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_fake")

    broken = FakeAdapter("slack", probe_error=RuntimeError("invalid_auth"))
    healthy = FakeAdapter(
        "linear",
        records=[
            Evidence(
                id="ENG-1",
                pointer=SourcePointer(
                    app="linear",
                    resource_uri="https://linear.app/meridian/issue/ENG-1",
                    locator={"type": "issue", "id": "uuid-1", "identifier": "ENG-1"},
                    content_hash=content_hash({"id": "uuid-1"}),
                ),
                text="fix the thing",
            )
        ],
        operations={"comment": ActionTier.INTERNAL},
    )
    adapters = {"slack": broken, "linear": healthy}

    results = smoke.run_all(["slack", "linear"], build_adapter=lambda app, creds: adapters[app])

    by_app = {r.app: r for r in results}
    assert by_app["slack"].failed is True
    assert by_app["slack"].step_reached == "probe"
    # linear's own run is unaffected by slack's failure.
    assert by_app["linear"].step_reached in {"conformance", "targets"}
    assert len(results) == 2


# ---------------------------------------------------------------------------
# read-only default vs --write
# ---------------------------------------------------------------------------


def _full_slack_adapter() -> FakeAdapter:
    return FakeAdapter(
        "slack",
        scopes=("eng", "general"),
        records=[_slack_evidence()],
        operations={"add_reaction": ActionTier.TRIVIAL},
    )


def test_read_only_default_never_calls_act(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    adapter = _full_slack_adapter()
    result = smoke.run_app("slack", write=False, build_adapter=_builder(adapter))

    assert adapter.act_calls == []
    assert adapter.undo_calls == []
    assert result.write_attempted is False
    assert result.write_ok is None
    assert result.undo_ok is None


def test_write_flag_calls_act_then_undo(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    adapter = _full_slack_adapter()
    result = smoke.run_app("slack", write=True, build_adapter=_builder(adapter))

    assert result.failed is False, result.steps
    assert result.write_attempted is True
    assert len(adapter.act_calls) == 1
    assert adapter.act_calls[0].operation == "add_reaction"
    assert adapter.act_calls[0].target.get("channel") == "C_ENG"
    assert adapter.act_calls[0].target.get("ts") == "1700000000.000100"
    assert result.write_ok is True
    # undo() must run, and must be called with the exact receipt act() returned.
    assert len(adapter.undo_calls) == 1
    assert adapter.undo_calls[0].action_id == "fake-1"
    assert result.undo_ok is True


def test_write_failure_is_reported_and_undo_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    adapter = _full_slack_adapter()
    adapter._act_error = RuntimeError("channel_not_found")

    result = smoke.run_app("slack", write=True, build_adapter=_builder(adapter))

    assert result.write_ok is False
    assert "channel_not_found" in result.write_detail
    assert adapter.undo_calls == []  # nothing to undo — act() never succeeded
    assert result.undo_ok is None
    assert result.failed is True


def test_undo_failure_is_reported_independently_of_write_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    adapter = _full_slack_adapter()
    adapter._undo_error = RuntimeError("cannot_undo")

    result = smoke.run_app("slack", write=True, build_adapter=_builder(adapter))

    assert result.write_ok is True
    assert result.undo_ok is False
    assert "cannot_undo" in result.undo_detail
    assert result.failed is True


# ---------------------------------------------------------------------------
# resolve stability and evidence shape
# ---------------------------------------------------------------------------


def test_unstable_resolve_hash_fails_the_resolve_step(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    adapter = FakeAdapter("slack", unstable_resolve=True)
    result = smoke.run_app("slack", build_adapter=_builder(adapter))

    assert result.step_reached == "resolve"
    assert result.failed is True
    resolve_step = next(s for s in result.steps if s.step == "resolve")
    assert "unstable hash" in resolve_step.detail


def test_bad_content_hash_fails_the_evidence_step(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    bad_record = Evidence(
        id="bad",
        pointer=SourcePointer(
            app="slack",
            resource_uri="https://meridian.slack.com/archives/C1/p1",
            locator={"channel": "C1", "ts": "1"},
            content_hash="f" * 64,  # valid at construction time...
        ),
        text="hi",
    )
    # ...but corrupted afterwards, the way a live payload bug might produce a
    # short or non-hex digest that still made it past __post_init__.
    object.__setattr__(bad_record.pointer, "content_hash", "not-a-hash")

    adapter = FakeAdapter("slack", records=[bad_record])
    result = smoke.run_app("slack", build_adapter=_builder(adapter))

    assert result.step_reached == "evidence"
    assert result.failed is True


def test_empty_fetch_fails_the_fetch_step(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    adapter = FakeAdapter("slack", records=[])
    result = smoke.run_app("slack", build_adapter=_builder(adapter))

    assert result.step_reached == "fetch"
    assert result.failed is True


# ---------------------------------------------------------------------------
# exit code
# ---------------------------------------------------------------------------


def test_exit_code_is_zero_when_nothing_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    results = smoke.run_all(list(smoke.APPS), build_adapter=_builder(FakeAdapter("slack")))
    # every app skipped (no credentials at all) -> nothing failed
    assert all(r.skipped for r in results)
    assert smoke.compute_exit_code(results) == 0


def test_exit_code_is_nonzero_when_any_app_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_credentials(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_fake")

    broken = FakeAdapter("slack", probe_error=RuntimeError("nope"))
    healthy_records = [
        Evidence(
            id="ENG-1",
            pointer=SourcePointer(
                app="linear",
                resource_uri="https://linear.app/meridian/issue/ENG-1",
                locator={"type": "issue", "id": "uuid-1", "identifier": "ENG-1"},
                content_hash=content_hash({"id": "uuid-1"}),
            ),
            text="fix the thing",
        )
    ]
    healthy = FakeAdapter("linear", records=healthy_records, operations={"comment": ActionTier.INTERNAL})
    adapters = {"slack": broken, "linear": healthy}

    results = smoke.run_all(["slack", "linear"], build_adapter=lambda app, creds: adapters[app])

    assert smoke.compute_exit_code(results) == 1


def test_exit_code_helper_is_pure_and_ignores_skips() -> None:
    passing = smoke.AppResult(app="a", steps=[smoke.StepResult("targets", True, "ok")])
    skipped = smoke.AppResult(app="b", skipped=True, skip_reason="no creds")
    assert smoke.compute_exit_code([passing, skipped]) == 0

    failing = smoke.AppResult(app="c", steps=[smoke.StepResult("probe", False, "boom")])
    assert smoke.compute_exit_code([passing, skipped, failing]) == 1


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def test_parse_args_defaults_to_read_only_and_all_apps() -> None:
    args = smoke.parse_args([])
    assert args.app is None
    assert args.write is False


def test_parse_args_accepts_app_and_write_flags() -> None:
    args = smoke.parse_args(["--app", "slack", "--write"])
    assert args.app == "slack"
    assert args.write is True


def test_parse_args_rejects_unknown_app() -> None:
    with pytest.raises(SystemExit):
        smoke.parse_args(["--app", "not-a-real-app"])


# ---------------------------------------------------------------------------
# report rendering does not crash and mentions skip reasons / failures
# ---------------------------------------------------------------------------


def test_render_report_mentions_skip_reason_and_exit_summary() -> None:
    results = [
        smoke.AppResult(app="slack", skipped=True, skip_reason="no credentials in the environment for this app"),
        smoke.AppResult(
            app="linear",
            steps=[smoke.StepResult("targets", True, "3 ok, 0 failed, 3 skipped — ...")],
        ),
    ]
    report = smoke.render_report(results, write=False)
    assert "no credentials in the environment for this app" in report
    assert "1 passed, 0 failed, 1 skipped" in report


def test_render_report_all_skipped_says_so_clearly() -> None:
    results = [smoke.AppResult(app=app, skipped=True, skip_reason="no credentials in the environment for this app") for app in smoke.APPS]
    report = smoke.render_report(results, write=False)
    assert "No credentials found in the environment for any app" in report
