"""Tests for `seed.seed.Seeder`.

`FixtureAdapter` instances stand in for the five real apps here, on both ends: a
"source" set (pointed at a small hand-written fixture directory) plays the role of the
Meridian CSVs, and a "target" set (pointed at a separate, initially-empty fixture
directory) plays the role of a blank live workspace. Both are real `FixtureAdapter`s —
they implement `act()`/`undo()` for real, over an in-memory row store — which is exactly
what the module docstring in `walnut/adapters/fixture.py` means by "not a mock."

`RecordingAdapter` wraps a target adapter and records every `act()`/`undo()` call, in
order, into one shared list per test — the thing none of these tests could otherwise
observe directly, since `FixtureAdapter` itself only tracks its own writes, not their
relation to other adapters' writes.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from seed.seed import Containers, Seeder, load_report
from walnut.adapters.fixture import FixtureAdapter
from walnut.contract import Action, ActionReceipt, Adapter

# ---------------------------------------------------------------------------
# Fixture data helpers
# ---------------------------------------------------------------------------

_HEADERS: dict[str, list[str]] = {
    "slack": ["msg_id", "channel", "author_person_id", "text", "days_ago", "thread_parent_id"],
    "linear": ["issue_key", "title", "description", "state", "assignee_person_id", "priority", "days_ago", "labels"],
    "github": ["kind", "number", "title", "body", "state", "author_person_id", "days_ago", "approvals", "linked_issue"],
    "notion": ["page_id", "title", "body", "status", "author_person_id", "days_ago", "parent"],
    "email": ["msg_id", "from_addr", "to_addr", "subject", "body", "days_ago", "folder"],
}

_SOURCE_ROWS: dict[str, list[dict[str, str]]] = {
    "slack": [
        {
            "msg_id": "sl001",
            "channel": "eng",
            "author_person_id": "p1",
            "text": "shipped v2!",
            "days_ago": "9",
            "thread_parent_id": "",
        },
    ],
    "linear": [
        {
            "issue_key": "ENG-412",
            "title": "Export times out on large workspaces",
            "description": "Still open.",
            "state": "In Progress",
            "assignee_person_id": "p2",
            "priority": "Urgent",
            "days_ago": "20",
            "labels": "bug",
        },
    ],
    "github": [
        {
            "kind": "issue",
            "number": "42",
            "title": "Slow analytics dashboard",
            "body": "Dashboard loads slowly for big accounts.",
            "state": "open",
            "author_person_id": "p2",
            "days_ago": "5",
            "approvals": "0",
            "linked_issue": "",
        },
        {
            "kind": "pr",
            "number": "288",
            "title": "fix: export timeout",
            "body": "Should fix ENG-412.",
            "state": "open",
            "author_person_id": "p2",
            "days_ago": "6",
            "approvals": "0",
            "linked_issue": "ENG-412",
        },
    ],
    "notion": [
        {
            "page_id": "nt001",
            "title": "Export v2 - Spec",
            "body": "v2.1 is now GA.",
            "status": "Shipped",
            "author_person_id": "p1",
            "days_ago": "9",
            "parent": "",
        },
    ],
    "email": [
        {
            "msg_id": "em003",
            "from_addr": "jordan.alvarez@meridian.dev",
            "to_addr": "contact@northstar-analytics.com",
            "subject": "Re: export timeout",
            "body": "We expect a fix by September 5th.",
            "days_ago": "18",
            "folder": "Sent",
        },
    ],
}


def _write_csv(path: Path, app: str, rows: list[dict[str, str]]) -> None:
    headers = _HEADERS[app]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in headers})


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    """A small, hand-written stand-in for `fixtures/`: one or two rows per app,
    covering an `issue` and a `pr` kind row for GitHub so the create_pr-skip path is
    exercised."""
    d = tmp_path / "source"
    d.mkdir()
    for app, rows in _SOURCE_ROWS.items():
        _write_csv(d / f"{app}.csv", app, rows)
    return d


@pytest.fixture()
def target_dir(tmp_path: Path) -> Path:
    """An initially-empty stand-in for a blank live workspace: header-only CSVs."""
    d = tmp_path / "target"
    d.mkdir()
    for app in _HEADERS:
        _write_csv(d / f"{app}.csv", app, [])
    return d


class RecordingAdapter:
    """Wraps a real `Adapter`, recording every `act()`/`undo()` call, in order, into a
    list shared across every wrapped adapter passed to the same `Seeder` — the only way
    to observe cross-app ordering, since each `FixtureAdapter` only knows about itself.
    """

    def __init__(self, inner: Adapter, calls: list[tuple[str, str, str]]) -> None:
        self._inner = inner
        self._calls = calls
        self.name = inner.name

    def probe(self):
        return self._inner.probe()

    def fetch(self, scope: str | None = None, limit: int = 100):
        return self._inner.fetch(scope=scope, limit=limit)

    def resolve(self, pointer):
        return self._inner.resolve(pointer)

    def capabilities(self):
        return self._inner.capabilities()

    def act(self, action: Action) -> ActionReceipt:
        receipt = self._inner.act(action)
        self._calls.append(("act", self.name, str(action.target.get("id", ""))))
        return receipt

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        result = self._inner.undo(receipt)
        self._calls.append(("undo", self.name, str(receipt.action.target.get("id", ""))))
        return result


class FailingAdapter:
    """A target that always fails to `act()`, everything else delegated. Used to prove
    one app's failure does not prevent the other four from seeding."""

    def __init__(self, inner: Adapter) -> None:
        self._inner = inner
        self.name = inner.name

    def probe(self):
        return self._inner.probe()

    def fetch(self, scope: str | None = None, limit: int = 100):
        return self._inner.fetch(scope=scope, limit=limit)

    def resolve(self, pointer):
        return self._inner.resolve(pointer)

    def capabilities(self):
        return self._inner.capabilities()

    def act(self, action: Action) -> ActionReceipt:
        raise RuntimeError("simulated GitHub outage")

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        return self._inner.undo(receipt)


def _wrapped_targets(target_dir: Path, calls: list[tuple[str, str, str]]) -> dict[str, RecordingAdapter]:
    return {app: RecordingAdapter(FixtureAdapter(app, target_dir), calls) for app in _HEADERS}


def _containers() -> Containers:
    return Containers(
        github_repo="meridian/platform",
        linear_team_id="team_eng",
        notion_database_id="db_demo",
        email_folder="Drafts",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_plan_calls_nothing_and_produces_ops_for_every_app(source_dir, target_dir, tmp_path):
    calls: list[tuple[str, str, str]] = []
    targets = _wrapped_targets(target_dir, calls)
    seeder = Seeder(
        targets, fixture_dir=source_dir, dry_run=True, containers=_containers(),
        runs_dir=tmp_path / "runs",
    )

    ops = seeder.plan()

    assert calls == [], "plan() must never call act() or undo()"
    apps_planned = {op.app for op in ops}
    assert apps_planned == {"slack", "linear", "github", "notion", "email"}
    # The PR-kind GitHub row must not have produced an op: no create_pr operation.
    github_ops = [op for op in ops if op.app == "github"]
    assert len(github_ops) == 1
    assert github_ops[0].fixture_row_id == "issue-42"
    # Every op carries its own justification handle via fixture_row_id and a marker
    # embedded somewhere in its payload text.
    for op in ops:
        payload_text = " ".join(str(v) for v in op.payload.values())
        assert f"[walnut-seed:{op.fixture_row_id}]" in payload_text


def test_dry_run_never_calls_act(source_dir, target_dir, tmp_path):
    calls: list[tuple[str, str, str]] = []
    targets = _wrapped_targets(target_dir, calls)
    seeder = Seeder(
        targets, fixture_dir=source_dir, dry_run=True, containers=_containers(),
        runs_dir=tmp_path / "runs",
    )
    ops = seeder.plan()

    report = seeder.apply(ops)

    assert calls == [], "apply() with dry_run=True must never call act()"
    assert report.dry_run is True
    assert len(report.planned) == len(ops)
    assert report.created == []


def test_apply_creates_artefacts(source_dir, target_dir, tmp_path):
    calls: list[tuple[str, str, str]] = []
    targets = _wrapped_targets(target_dir, calls)
    seeder = Seeder(
        targets, fixture_dir=source_dir, dry_run=False, containers=_containers(),
        rate_limit_delay=0.0, runs_dir=tmp_path / "runs",
    )
    ops = seeder.plan()

    report = seeder.apply(ops)

    assert report.failures == []
    assert len(report.created) == len(ops)
    assert len(calls) == len(ops)
    assert all(kind == "act" for kind, _, _ in calls)
    # The created rows are genuinely visible through fetch() now, marker and all.
    for record in report.created:
        adapter = targets[record.app]
        evidence = adapter.fetch(limit=1000)
        assert any(f"[walnut-seed:{record.fixture_row_id}]" in ev.text for ev in evidence)


def test_second_plan_skips_already_seeded_rows(source_dir, target_dir, tmp_path):
    calls: list[tuple[str, str, str]] = []
    targets = _wrapped_targets(target_dir, calls)
    seeder = Seeder(
        targets, fixture_dir=source_dir, dry_run=False, containers=_containers(),
        rate_limit_delay=0.0, runs_dir=tmp_path / "runs",
    )
    first_ops = seeder.plan()
    seeder.apply(first_ops)

    second_ops = seeder.plan()

    assert second_ops == [], "every row was just seeded; a second plan() must find nothing new"
    assert any("already seeded" in note for note in seeder.last_plan_notes)


def test_teardown_reverses_in_reverse_order(source_dir, target_dir, tmp_path):
    calls: list[tuple[str, str, str]] = []
    targets = _wrapped_targets(target_dir, calls)
    seeder = Seeder(
        targets, fixture_dir=source_dir, dry_run=False, containers=_containers(),
        rate_limit_delay=0.0, runs_dir=tmp_path / "runs",
    )
    ops = seeder.plan()
    report = seeder.apply(ops)
    creation_order = [r.fixture_row_id for r in report.created]

    # Round-trip through the persisted run file first, proving the report that
    # teardown() actually needs can come from disk, not just the in-memory object.
    reloaded = load_report(report.path)

    calls.clear()
    seeder.teardown(reloaded)

    assert reloaded.teardown_failures == []
    assert all(r.undone for r in reloaded.created)
    undo_order = [row_id for kind, _, row_id in calls if kind == "undo"]
    assert undo_order == list(reversed(creation_order))


def test_one_app_failure_does_not_prevent_the_others(source_dir, target_dir, tmp_path):
    calls: list[tuple[str, str, str]] = []
    targets = _wrapped_targets(target_dir, calls)
    # Swap in a target that always fails act() for github only.
    broken_targets: dict = dict(targets)
    broken_targets["github"] = FailingAdapter(FixtureAdapter("github", target_dir))

    seeder = Seeder(
        broken_targets, fixture_dir=source_dir, dry_run=False, containers=_containers(),
        rate_limit_delay=0.0, max_attempts=2, runs_dir=tmp_path / "runs",
    )
    ops = seeder.plan()
    assert any(op.app == "github" for op in ops)

    report = seeder.apply(ops)

    github_failures = [f for f in report.failures if f.app == "github"]
    assert len(github_failures) == 1
    assert github_failures[0].attempts == 2
    assert "simulated GitHub outage" in github_failures[0].error

    other_apps_created = {r.app for r in report.created}
    assert other_apps_created == {"slack", "linear", "notion", "email"}
    non_github_ops = [op for op in ops if op.app != "github"]
    assert len(report.created) == len(non_github_ops)


def test_run_file_is_written_incrementally(source_dir, target_dir, tmp_path):
    calls: list[tuple[str, str, str]] = []
    targets = _wrapped_targets(target_dir, calls)
    seeder = Seeder(
        targets, fixture_dir=source_dir, dry_run=False, containers=_containers(),
        rate_limit_delay=0.0, runs_dir=tmp_path / "runs",
    )
    ops = seeder.plan()
    assert len(ops) >= 2, "need at least two ops to observe incremental growth"

    snapshots: list[int] = []
    original_save = seeder._save

    def spying_save(report, path):
        original_save(report, path)
        snapshots.append(len(report.created))

    seeder._save = spying_save  # type: ignore[method-assign]

    report = seeder.apply(ops)

    # Saved at least once before any op ran (0), and the created count in the
    # persisted file grows monotonically rather than jumping straight to the end.
    assert snapshots[0] == 0
    assert snapshots[-1] == len(report.created)
    assert snapshots == sorted(snapshots)
    assert len(snapshots) >= len(ops)  # one save per op, at minimum, plus the initial one

    # The file on disk reflects the final state and round-trips.
    on_disk = json.loads(report.path.read_text(encoding="utf-8"))
    assert len(on_disk["created"]) == len(report.created)
    reloaded = load_report(report.path)
    assert len(reloaded.created) == len(report.created)


def test_apply_needs_evidence_like_every_other_action(source_dir, target_dir, tmp_path):
    """Sanity check that seeded writes go through the same `Action` construction the
    rest of Walnut uses: an unjustified action is refused by the type itself."""
    with pytest.raises(ValueError):
        Action(app="slack", operation="post_message", target={}, payload={}, justified_by=())
