"""Push Meridian's fixture CSVs into the five real apps, over their real APIs.

This is Sunday's job (`PLAN.md` Phase 7): "push fixtures into the five real apps. One
'hello world' write per app to prove every token actually has the scope it claims.
This is the step that most often fails at 03:00 if skipped." A seed run that half-fails
at 3am with no way to tell what happened, resume, or undo it is worse than not running
it at all — so every property below exists because the alternative is a hazard on a
non-negotiable deadline, not because it is elegant.

**Safety.** `dry_run` defaults to `True`. Constructing a `Seeder` and calling `apply()`
on it writes nothing anywhere until a caller explicitly passes `dry_run=False` — the
same discipline `ActionTier.GATED` enforces for the agent itself, applied to the tool
that seeds the agent's world. The CLI goes one step further: `--apply` alone still
refuses; only `--apply --confirm` together write.

**Idempotency.** Every seeded artefact's body carries the literal marker
`[walnut-seed:<fixture_row_id>]`. `plan()` reads back whatever the target adapter's
`fetch()` reports, extracts every marker already present, and skips any fixture row
whose marker is already there — so running this script twice, or after a partial
failure, never duplicates a row. This is real evidence discipline applied to seed data:
the claim "this row is already seeded" is checked against the app, not assumed from a
local record that might be stale or missing.

**Resumability.** `apply()` writes the running `SeedReport` to
`runs/seed-<timestamp>.json` after every successful op, not once at the end. A crash
halfway through leaves a file that names exactly what succeeded, what failed, and
enough of each `ActionReceipt` to tear down or resume. `load_report()` reconstructs a
`SeedReport` from that file well enough to hand straight to `teardown()`.

**Reversibility.** `teardown()` calls each target adapter's `undo()` in the *reverse*
of creation order, because later seeded artefacts can reference earlier ones (a Slack
reply's channel, a comment on a just-created issue) even though this first cut does not
create such references itself (see "Scope of this cut" below) — reversing in creation
order is the only ordering that is safe regardless.

**Per-app isolation.** `apply()` iterates one `SeedOp` at a time, not one app at a
time. If GitHub's token has the wrong scope, that failure is recorded per-op and the
loop moves on; Slack, Linear, Notion, and Email are unaffected. There is no app-level
try/except, because there does not need to be — the failure boundary is already as
tight as it can be.

**Rate limits.** A small delay (`rate_limit_delay`, default 0.2s) follows every
successful write. A failed write is retried up to `max_attempts` times (default 3)
with exponential backoff (`rate_limit_delay * 2**attempt`) before it is recorded as a
failure and the seeder moves to the next op.

## Why `save_draft`, not `send_email`

`EmailAdapter.act()` tiers `send_email` as `ActionTier.GATED`: the message leaves the
building over SMTP and cannot be recalled (see `EmailAdapter.undo`, which raises
`EmailUndoImpossibleError` for exactly this operation). A seed script that ran
`send_email` would either (a) require a human to approve every seeded email before an
unattended overnight run could finish, defeating the point of a seed script, or (b)
require weakening the gate, which is not this script's decision to make. `save_draft`
is `ActionTier.INTERNAL` — it never leaves the mailbox, executes without approval, is
fully reversible (delete the draft), and a human reviewing the demo mailbox sees
exactly the same customer-facing content a draft would have shown before sending. Email
rows are seeded as drafts in `Containers.email_folder` (default `"Drafts"`); nothing
in this module ever calls `send_email`.

## Scope of this cut — assumptions worth re-reading before Sunday

These are documented rather than silently guessed at, per this project's own
evidence-discipline rule. Every one of them is a place a stronger version of this
script could do more:

- **Never run against a live workspace.** No Slack/Linear/GitHub/Notion/Email
  credentials exist in this environment, so every line here is verified against the
  `FixtureAdapter` conformance-style fake (which the codebase already treats as a real
  target, not a mock — see `walnut/adapters/fixture.py`) and against a close reading of
  each real adapter's source. It has not been exercised against a live API. Phase 7's
  own smoke test is what closes that gap.
- **Slack channels are assumed to already exist.** `chat.postMessage` cannot create a
  channel; `#eng`/`#support`/`#general`/`#product` (or whatever the fixture's channel
  values are on the day) must already exist in the target workspace.
- **Thread replies are flattened to top-level messages.** A genuine threaded reply
  needs the parent's live `ts`, which only exists after the parent has actually been
  posted — correct handling means either strict two-pass ordering across a resumable
  run or a forward-reference mechanism, either of which is more moving parts than a
  first cut needs. `slack.csv` rows are seeded as independent top-level posts.
- **GitHub PRs are skipped, not seeded.** A pull request needs a branch and a commit;
  `GitHubAdapter.capabilities()` has no `create_pr` operation (rightly — that is out of
  scope for an evidence adapter), so `kind == "pr"` rows are skipped with a note. Only
  `kind == "issue"` rows are seeded.
- **Linear needs a real `team_id`; Notion needs a real `database_id`.** Neither can be
  invented from fixture data (`linear.csv`'s issue-key prefix is a team *key*, not a
  team *id*; `notion.csv` has no database reference at all). Supply them via
  `Containers` or `LINEAR_TEAM_ID` / `NOTION_DATABASE_ID` — the latter already has an
  env var slot in `.env.example`, the former does not and would be a one-line addition
  to it. Absent either, that app's rows are skipped with a note, not seeded against a
  guessed id.
- **Notion seeding only writes a title (and a `Status` property, if the destination
  page has one) — not page body content.** `NotionAdapter.act()`'s `create_page` sets
  properties only; writing body text needs a second `append_block` call against the
  page id the first call returns. Chaining two ops per row is a reasonable next step,
  left out of this cut to keep every fixture row a single, independently retryable
  `SeedOp`. The marker still lands in the title, so idempotency is unaffected.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from walnut.adapters.fixture import load_all_fixtures
from walnut.contract import Action, ActionReceipt, Adapter, utcnow

__all__ = [
    "Containers",
    "SeedFailure",
    "SeedOp",
    "SeedRecord",
    "Seeder",
    "SeedReport",
    "TeardownFailure",
    "load_report",
]

_MARKER_RE = re.compile(r"\[walnut-seed:([^\]]+)\]")


def _marker(row_id: str) -> str:
    return f"[walnut-seed:{row_id}]"


# ---------------------------------------------------------------------------
# Where seeded artefacts land
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Containers:
    """The external "container" each app's seeded rows are written into.

    `github_repo` has a demo-sane default matching the fixture's own hardcoded URI
    template (`walnut/adapters/fixture.py`'s `_SPEC["github"]["uri"]`). `linear_team_id`
    and `notion_database_id` have none, deliberately: a fabricated Linear team id or
    Notion database id would fail against a live API in a way that is confusing to
    debug at 3am, so absent a real one, the corresponding app's rows are skipped with a
    reason rather than guessed at (see the module docstring).
    """

    github_repo: str = "meridian/platform"
    linear_team_id: str | None = None
    notion_database_id: str | None = None
    email_folder: str = "Drafts"


# ---------------------------------------------------------------------------
# What plan() produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeedOp:
    """One fixture row, translated into one write `apply()` would perform.

    `target` always carries an `"id"` key set to `fixture_row_id` and, for apps whose
    real `fetch(scope=...)` semantics differ from `FixtureAdapter`'s label-based
    filtering, a `"_scope"` key naming the real container to check for an existing
    marker. Real adapters ignore both — every `act()` handler in this codebase reads
    named keys off `target`/`payload` and ignores the rest — so these are free to
    carry without risk of colliding with a real field.
    """

    app: str
    operation: str
    target: dict[str, Any]
    payload: dict[str, Any]
    fixture_row_id: str


# ---------------------------------------------------------------------------
# Recipes: one fixture row -> one SeedOp, per app
#
# Every field a target `_SPEC` text/label/uri lambda accesses with `row[...]` (bracket
# access, not `.get`) is included in `payload`, even when the real adapter's `act()`
# ignores it, so a `FixtureAdapter` used as the seed target (as the conformance suite
# and this module's own tests do) never raises reading back a row this module created.
# ---------------------------------------------------------------------------

Recipe = Callable[[str, dict[str, str], Containers], "SeedOp | None"]


def _plan_slack(row_id: str, row: dict[str, str], containers: Containers) -> SeedOp | None:
    channel = row.get("channel", "")
    text = f"{row.get('text', '')} {_marker(row_id)}".strip()
    return SeedOp(
        app="slack",
        operation="post_message",
        target={"id": row_id, "channel": channel, "_scope": channel},
        payload={"text": text, "channel": channel},
        fixture_row_id=row_id,
    )


def _plan_linear(row_id: str, row: dict[str, str], containers: Containers) -> SeedOp | None:
    if not containers.linear_team_id:
        return None  # no destination team configured — see module docstring
    description = f"{row.get('description', '')}\n\n{_marker(row_id)}".strip()
    return SeedOp(
        app="linear",
        operation="create_issue",
        target={"id": row_id, "team_id": containers.linear_team_id, "_scope": None},
        payload={
            "title": row.get("title", ""),
            "description": description,
            "state": row.get("state", ""),
        },
        fixture_row_id=row_id,
    )


def _plan_github(row_id: str, row: dict[str, str], containers: Containers) -> SeedOp | None:
    if row.get("kind", "").strip().lower() != "issue":
        return None  # PRs need a branch and a commit; there is no create_pr op
    owner, _, repo = containers.github_repo.partition("/")
    body = f"{row.get('body', '')}\n\n{_marker(row_id)}".strip()
    return SeedOp(
        app="github",
        operation="create_issue",
        target={
            "id": row_id,
            "owner": owner,
            "repo": repo,
            "_scope": containers.github_repo,
        },
        payload={
            "title": row.get("title", ""),
            "body": body,
            # Carried through only so a FixtureAdapter target's own `_SPEC["github"]`
            # lambdas (which bracket-index kind/number/state/approvals) never raise
            # reading this row back; the real GitHub adapter ignores all of these.
            "kind": "issue",
            "number": row.get("number", ""),
            "state": row.get("state", "open"),
            "approvals": row.get("approvals", "0"),
        },
        fixture_row_id=row_id,
    )


def _plan_notion(row_id: str, row: dict[str, str], containers: Containers) -> SeedOp | None:
    if not containers.notion_database_id:
        return None  # no destination database configured — see module docstring
    title = f"{row.get('title', '')} {_marker(row_id)}".strip()
    status = row.get("status", "")
    properties: dict[str, Any] = {
        "Name": {"title": [{"type": "text", "text": {"content": title}}]},
    }
    if status:
        properties["Status"] = {"status": {"name": status}}
    return SeedOp(
        app="notion",
        operation="create_page",
        target={
            "id": row_id,
            "database_id": containers.notion_database_id,
            "_scope": containers.notion_database_id,
        },
        payload={
            "properties": properties,
            "parent": {"database_id": containers.notion_database_id},
            # Flat fields for a FixtureAdapter target's `_SPEC["notion"]` lambdas.
            # The real adapter's create_page reads only "properties"/"parent".
            "title": title,
            "body": row.get("body", ""),
            "status": status,
        },
        fixture_row_id=row_id,
    )


def _plan_email(row_id: str, row: dict[str, str], containers: Containers) -> SeedOp | None:
    subject = f"{row.get('subject', '')} {_marker(row_id)}".strip()
    to_addr = row.get("to_addr", "")
    from_addr = row.get("from_addr", "")
    return SeedOp(
        app="email",
        operation="save_draft",
        target={"id": row_id, "folder": containers.email_folder, "_scope": containers.email_folder},
        payload={
            "to": to_addr,
            "from": from_addr,
            "subject": subject,
            "body": row.get("body", ""),
            # Flat CSV-style names for a FixtureAdapter target's `_SPEC["email"]`
            # lambdas, which read `from_addr`/`to_addr` rather than `from`/`to`.
            "to_addr": to_addr,
            "from_addr": from_addr,
        },
        fixture_row_id=row_id,
    )


_RECIPES: dict[str, Recipe] = {
    "slack": _plan_slack,
    "linear": _plan_linear,
    "github": _plan_github,
    "notion": _plan_notion,
    "email": _plan_email,
}


# ---------------------------------------------------------------------------
# What apply() and teardown() produce
# ---------------------------------------------------------------------------


@dataclass
class SeedFailure:
    app: str
    fixture_row_id: str
    operation: str
    error: str
    attempts: int = 0


@dataclass
class TeardownFailure:
    app: str
    fixture_row_id: str
    error: str


@dataclass
class SeedRecord:
    """One artefact this run actually created, with everything `teardown()` needs."""

    app: str
    fixture_row_id: str
    operation: str
    receipt: ActionReceipt
    undone: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "fixture_row_id": self.fixture_row_id,
            "operation": self.operation,
            "receipt": _receipt_to_dict(self.receipt),
            "undone": self.undone,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SeedRecord:
        return cls(
            app=d["app"],
            fixture_row_id=d["fixture_row_id"],
            operation=d["operation"],
            receipt=_receipt_from_dict(d["receipt"]),
            undone=d.get("undone", False),
        )


@dataclass
class SeedReport:
    """The record of one `apply()` run: what was created, what failed, and — once a
    dry run has been made real — enough to resume a crash or reverse a success.

    `path` is where this report is persisted; it is set by `Seeder._save` and is not
    itself part of the JSON (it names the file, it does not belong inside it).
    """

    run_id: str
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None = None
    created: list[SeedRecord] = field(default_factory=list)
    failures: list[SeedFailure] = field(default_factory=list)
    planned: list[SeedOp] = field(default_factory=list)
    """Populated instead of `created` when `apply()` runs with `dry_run=True`."""
    teardown_failures: list[TeardownFailure] = field(default_factory=list)
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "created": [r.to_dict() for r in self.created],
            "failures": [asdict(f) for f in self.failures],
            "planned": [_op_to_dict(o) for o in self.planned],
            "teardown_failures": [asdict(f) for f in self.teardown_failures],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SeedReport:
        return cls(
            run_id=d["run_id"],
            dry_run=d["dry_run"],
            started_at=datetime.fromisoformat(d["started_at"]),
            finished_at=datetime.fromisoformat(d["finished_at"]) if d.get("finished_at") else None,
            created=[SeedRecord.from_dict(r) for r in d.get("created", [])],
            failures=[SeedFailure(**f) for f in d.get("failures", [])],
            planned=[_op_from_dict(o) for o in d.get("planned", [])],
            teardown_failures=[TeardownFailure(**f) for f in d.get("teardown_failures", [])],
        )


def load_report(path: Path | str) -> SeedReport:
    """Reconstruct a `SeedReport` from a `runs/seed-*.json` file, for `--teardown`."""
    path = Path(path)
    report = SeedReport.from_dict(json.loads(path.read_text(encoding="utf-8")))
    report.path = path
    return report


# -- JSON (de)serialisation for the dataclasses this module doesn't own ------


def _op_to_dict(op: SeedOp) -> dict[str, Any]:
    return {
        "app": op.app,
        "operation": op.operation,
        "target": op.target,
        "payload": op.payload,
        "fixture_row_id": op.fixture_row_id,
    }


def _op_from_dict(d: dict[str, Any]) -> SeedOp:
    return SeedOp(
        app=d["app"],
        operation=d["operation"],
        target=d["target"],
        payload=d["payload"],
        fixture_row_id=d["fixture_row_id"],
    )


def _action_to_dict(action: Action) -> dict[str, Any]:
    return {
        "app": action.app,
        "operation": action.operation,
        "target": action.target,
        "payload": action.payload,
        "justified_by": list(action.justified_by),
        "rationale": action.rationale,
    }


def _action_from_dict(d: dict[str, Any]) -> Action:
    return Action(
        app=d["app"],
        operation=d["operation"],
        target=d["target"],
        payload=d["payload"],
        justified_by=tuple(d["justified_by"]),
        rationale=d.get("rationale", ""),
    )


def _receipt_to_dict(receipt: ActionReceipt) -> dict[str, Any]:
    return {
        "action_id": receipt.action_id,
        "action": _action_to_dict(receipt.action),
        "result": receipt.result,
        "prior_state": receipt.prior_state,
        "executed_at": receipt.executed_at.isoformat(),
        "undone_at": receipt.undone_at.isoformat() if receipt.undone_at else None,
    }


def _receipt_from_dict(d: dict[str, Any]) -> ActionReceipt:
    return ActionReceipt(
        action_id=d["action_id"],
        action=_action_from_dict(d["action"]),
        result=d["result"],
        prior_state=d["prior_state"],
        executed_at=datetime.fromisoformat(d["executed_at"]),
        undone_at=datetime.fromisoformat(d["undone_at"]) if d.get("undone_at") else None,
    )


# ---------------------------------------------------------------------------
# The seeder
# ---------------------------------------------------------------------------


class Seeder:
    """Plans, applies, and reverses pushing fixture rows into real app adapters.

    `adapters` are the *targets* being written to — the real Slack/Linear/GitHub/
    Notion/Email adapters in production, or `FixtureAdapter` instances standing in for
    a blank live workspace in tests (they implement `act()`/`undo()` for real, which is
    exactly what a target needs to be). `fixture_dir` is the *source*: where the
    Meridian CSVs describing what should exist live, independent of where `adapters`
    write to. Defaulting it to `None` reads the project's own `fixtures/` directory,
    via `walnut.adapters.fixture.load_all_fixtures`.
    """

    def __init__(
        self,
        adapters: dict[str, Adapter],
        fixture_dir: Path | None = None,
        dry_run: bool = True,
        containers: Containers | None = None,
        rate_limit_delay: float = 0.2,
        max_attempts: int = 3,
        runs_dir: Path | None = None,
    ) -> None:
        self.adapters = adapters
        self.fixture_dir = fixture_dir
        self.dry_run = dry_run
        self.containers = containers or Containers()
        self.rate_limit_delay = rate_limit_delay
        self.max_attempts = max_attempts
        self._runs_dir = runs_dir or (Path(__file__).resolve().parent.parent / "runs")
        self.last_plan_notes: list[str] = []
        """Human-readable notes from the most recent `plan()` call: skipped rows,
        missing config, apps with no target adapter. Not part of the return value —
        `plan()`'s contract is `list[SeedOp]` — but is what the CLI prints alongside
        the plan table so a skip is never silent."""

    # -- plan -----------------------------------------------------------------

    def plan(self) -> list[SeedOp]:
        """Compute every write this run would perform. Read-only: only `probe()`/
        `fetch()` are ever called here, never `act()`/`undo()`."""
        self.last_plan_notes = []
        sources = load_all_fixtures(self.fixture_dir)
        ops: list[SeedOp] = []
        marker_cache: dict[tuple[str, str | None], set[str]] = {}

        for app, recipe in _RECIPES.items():
            source_adapter = sources.get(app)
            if source_adapter is None:
                continue

            target_adapter = self.adapters.get(app)
            if target_adapter is None:
                self.last_plan_notes.append(f"{app}: no target adapter configured, skipping")
                continue

            for evidence in source_adapter.fetch(limit=10_000):
                row_id = evidence.id
                op = recipe(row_id, evidence.raw, self.containers)
                if op is None:
                    self.last_plan_notes.append(
                        f"{app}:{row_id}: skipped (unsupported row, or no destination configured)"
                    )
                    continue

                scope = op.target.get("_scope")
                cache_key = (app, scope)
                if cache_key not in marker_cache:
                    marker_cache[cache_key] = self._existing_markers(target_adapter, scope)
                if row_id in marker_cache[cache_key]:
                    self.last_plan_notes.append(f"{app}:{row_id}: already seeded, skipping")
                    continue

                ops.append(op)

        return ops

    def _existing_markers(self, adapter: Adapter, scope: str | None) -> set[str]:
        """Every `walnut-seed` marker `adapter.fetch()` already reports for `scope`.

        Also tries an unscoped fetch and unions the result. `FixtureAdapter.fetch()`
        filters by content label, not by container id, so for apps whose scope and
        label vocabularies differ (GitHub: repo vs. issue/pr; Notion: database vs.
        parent) a scoped-only check would never see what a fixture-backed test target
        already holds. The extra read is cheap next to the cost of a duplicate write.
        """
        found: set[str] = set()
        for candidate_scope in ({scope, None} if scope is not None else {None}):
            try:
                evidence = adapter.fetch(scope=candidate_scope, limit=10_000)
            except Exception:  # noqa: BLE001 - a bad scope must not sink the plan
                continue
            for ev in evidence:
                found.update(_MARKER_RE.findall(ev.text))
        return found

    # -- apply ------------------------------------------------------------------

    def apply(self, ops: list[SeedOp]) -> SeedReport:
        """Execute `ops` via each target adapter's `act()`, one at a time.

        When `self.dry_run` is `True`, no adapter method is called at all — every op
        is recorded under `report.planned` instead of `report.created`. This makes
        `apply()` itself safe to call in dry-run mode, on top of the CLI never
        reaching it without `--apply --confirm`.
        """
        run_id = utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        report = SeedReport(run_id=run_id, dry_run=self.dry_run, started_at=utcnow())
        run_path = self._runs_dir / f"seed-{run_id}.json"
        self._save(report, run_path)

        for op in ops:
            if self.dry_run:
                report.planned.append(op)
                self._save(report, run_path)
                continue

            adapter = self.adapters.get(op.app)
            if adapter is None:
                report.failures.append(
                    SeedFailure(
                        app=op.app,
                        fixture_row_id=op.fixture_row_id,
                        operation=op.operation,
                        error="no adapter configured for this app",
                    )
                )
                self._save(report, run_path)
                continue

            try:
                receipt = self._apply_one(adapter, op)
            except Exception as exc:  # noqa: BLE001 - one row's failure must not sink the run
                report.failures.append(
                    SeedFailure(
                        app=op.app,
                        fixture_row_id=op.fixture_row_id,
                        operation=op.operation,
                        error=f"{type(exc).__name__}: {exc}",
                        attempts=self.max_attempts,
                    )
                )
                self._save(report, run_path)
                continue

            report.created.append(
                SeedRecord(
                    app=op.app,
                    fixture_row_id=op.fixture_row_id,
                    operation=op.operation,
                    receipt=receipt,
                )
            )
            # Persist after every successful op — not once at the end — so a crash
            # mid-run leaves a file naming exactly what already happened.
            self._save(report, run_path)

            if self.rate_limit_delay:
                time.sleep(self.rate_limit_delay)

        report.finished_at = utcnow()
        self._save(report, run_path)
        return report

    def _apply_one(self, adapter: Adapter, op: SeedOp) -> ActionReceipt:
        action = Action(
            app=op.app,
            operation=op.operation,
            target=dict(op.target),
            payload=dict(op.payload),
            justified_by=(f"seed-fixture:{op.app}:{op.fixture_row_id}",),
            rationale=f"Walnut seed data · {op.app} · {op.fixture_row_id}",
        )
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return adapter.act(action)
            except Exception as exc:  # noqa: BLE001 - retried below; re-raised once exhausted
                last_exc = exc
                if attempt < self.max_attempts:
                    time.sleep(self.rate_limit_delay * (2 ** (attempt - 1)))
        assert last_exc is not None
        raise last_exc

    # -- teardown -----------------------------------------------------------

    def teardown(self, report: SeedReport) -> None:
        """Undo every artefact `report` recorded as created, in reverse order.

        Reversed because a later artefact can reference an earlier one; undoing
        newest-first never leaves a dangling reference to something already gone.
        One artefact's `undo()` failing is recorded in `report.teardown_failures` and
        does not stop the rest from being reversed.
        """
        for record in reversed(report.created):
            if record.undone:
                continue
            adapter = self.adapters.get(record.app)
            if adapter is None:
                report.teardown_failures.append(
                    TeardownFailure(
                        app=record.app,
                        fixture_row_id=record.fixture_row_id,
                        error="no adapter configured for this app",
                    )
                )
                continue
            try:
                record.receipt = adapter.undo(record.receipt)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                report.teardown_failures.append(
                    TeardownFailure(
                        app=record.app,
                        fixture_row_id=record.fixture_row_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            record.undone = True
            if report.path is not None:
                self._save(report, report.path)

    # -- persistence ----------------------------------------------------------

    def _save(self, report: SeedReport, path: Path) -> None:
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        report.path = path


# ---------------------------------------------------------------------------
# CLI
#
#     python -m seed.seed --plan
#     python -m seed.seed --apply --confirm
#     python -m seed.seed --teardown runs/seed-X.json
# ---------------------------------------------------------------------------


def _build_adapters_from_env() -> dict[str, Adapter]:
    """One real adapter per credential actually present. An app with no credential is
    left out of the dict entirely, so `plan()`/`apply()` skip it and say why, rather
    than the CLI crashing on a partially-configured `.env`."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass  # python-dotenv is a declared dependency; degrade to bare os.environ

    adapters: dict[str, Adapter] = {}

    if slack_token := os.environ.get("SLACK_BOT_TOKEN"):
        from walnut.adapters.slack import SlackAdapter

        adapters["slack"] = SlackAdapter(token=slack_token)

    if linear_key := os.environ.get("LINEAR_API_KEY"):
        from walnut.adapters.linear import LinearAdapter

        adapters["linear"] = LinearAdapter(api_key=linear_key)

    if github_token := os.environ.get("GITHUB_TOKEN"):
        from walnut.adapters.github import GitHubAdapter

        adapters["github"] = GitHubAdapter(token=github_token)

    if notion_token := os.environ.get("NOTION_TOKEN"):
        from walnut.adapters.notion import NotionAdapter

        adapters["notion"] = NotionAdapter(token=notion_token)

    email_host = os.environ.get("EMAIL_HOST")
    email_user = os.environ.get("EMAIL_USER")
    email_password = os.environ.get("EMAIL_PASSWORD")
    if email_host and email_user and email_password:
        from walnut.adapters.email import EmailAdapter

        adapters["email"] = EmailAdapter(
            host=email_host,
            user=email_user,
            password=email_password,
            smtp_host=os.environ.get("EMAIL_SMTP_HOST"),
        )

    return adapters


def _containers_from_env() -> Containers:
    return Containers(
        github_repo=os.environ.get("GITHUB_REPO", "meridian/platform"),
        linear_team_id=os.environ.get("LINEAR_TEAM_ID"),
        notion_database_id=os.environ.get("NOTION_DATABASE_ID"),
        email_folder=os.environ.get("EMAIL_DRAFTS_FOLDER", "Drafts"),
    )


def _print_plan(ops: list[SeedOp], notes: list[str]) -> None:
    if ops:
        header = f"{'APP':<8} {'OPERATION':<14} {'ROW':<16} TARGET"
        print(header)
        print("-" * len(header))
        for op in ops:
            target_bits = ", ".join(
                f"{k}={v}" for k, v in op.target.items() if k not in ("id", "_scope")
            )
            print(f"{op.app:<8} {op.operation:<14} {op.fixture_row_id:<16} {target_bits}")
        apps = sorted({op.app for op in ops})
        print(f"\n{len(ops)} operation(s) planned across {len(apps)} app(s): {', '.join(apps)}")
    else:
        print("Nothing to seed.")

    if notes:
        print("\nSkipped / notes:")
        for note in notes:
            print(f"  - {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m seed.seed",
        description="Push Walnut's Meridian fixture data into the five real apps.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="show what would happen; writes nothing")
    mode.add_argument("--apply", action="store_true", help="write for real; requires --confirm")
    mode.add_argument("--teardown", metavar="RUN_FILE", help="undo a run file, in reverse order")
    parser.add_argument("--confirm", action="store_true", help="required alongside --apply")
    parser.add_argument("--fixture-dir", type=Path, default=None, help="source CSVs (default: fixtures/)")
    parser.add_argument("--delay", type=float, default=0.2, help="seconds between writes (default: 0.2)")
    args = parser.parse_args(argv)

    if args.teardown:
        report = load_report(args.teardown)
        seeder = Seeder(
            _build_adapters_from_env(),
            fixture_dir=args.fixture_dir,
            dry_run=False,
            rate_limit_delay=args.delay,
        )
        seeder.teardown(report)
        undone = sum(1 for r in report.created if r.undone)
        print(f"Teardown: {undone}/{len(report.created)} artefact(s) reversed, "
              f"{len(report.teardown_failures)} failure(s).")
        for tf in report.teardown_failures:
            print(f"  FAIL  {tf.app}:{tf.fixture_row_id}: {tf.error}")
        return 0 if not report.teardown_failures else 1

    if args.apply and not args.confirm:
        parser.error("--apply requires --confirm as well — refusing to write without both")

    applying = args.apply and args.confirm
    seeder = Seeder(
        _build_adapters_from_env(),
        fixture_dir=args.fixture_dir,
        dry_run=not applying,
        containers=_containers_from_env(),
        rate_limit_delay=args.delay,
    )
    ops = seeder.plan()
    _print_plan(ops, seeder.last_plan_notes)

    if not applying:
        return 0

    print()
    report = seeder.apply(ops)
    print(f"Applied: {len(report.created)} created, {len(report.failures)} failed.")
    for f in report.failures:
        print(f"  FAIL  {f.app}:{f.fixture_row_id} ({f.operation}): {f.error}")
    if report.path is not None:
        print(f"Run recorded at {report.path}")
    return 0 if not report.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
