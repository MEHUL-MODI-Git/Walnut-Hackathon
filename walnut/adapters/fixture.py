"""A contract-complete adapter backed by the Meridian CSV fixtures.

This exists for one reason: **the demo must not depend on five live API tokens.**

Five real integrations mean five ways to be dead in the water at 03:00 — a revoked
token, a rate limit, a workspace someone reset, a network that drops. The fixture
adapter implements the same six methods with the same semantics over local CSV, so the
full three-act run is exercisable right now, offline, and stays exercisable as a
fallback if a live connector fails during the window.

It is emphatically **not** a mock. It performs real content hashing, real drift
detection, real prior-state capture and real retraction-style undo, and it is held to
the same `run_conformance` suite as every network adapter. If the fixture adapter
passes and a live adapter does not, the bug is in the live adapter — which makes this
a diagnostic instrument as much as a fallback.

What it deliberately does NOT do is pretend to be a network. There is no fake latency
and no simulated failure. It reads a file.
"""

from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..contract import (
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

__all__ = ["FixtureAdapter", "load_all_fixtures", "load_identities"]

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"

# How each app's CSV maps onto the Evidence shape. Keeping this declarative means one
# adapter class serves all five apps rather than five near-identical copies.
_SPEC: dict[str, dict[str, Any]] = {
    "slack": {
        "file": "slack.csv",
        "id": "msg_id",
        "text": lambda r: r["text"],
        "author": "author_person_id",
        "labels": lambda r: (r["channel"],),
        "uri": lambda r: f"https://meridian.slack.com/archives/{r['channel']}/p{r['msg_id']}",
        "hash": ("msg_id", "channel", "text", "author_person_id"),
    },
    "linear": {
        "file": "linear.csv",
        "id": "issue_key",
        "text": lambda r: f"{r['issue_key']} {r['title']} — state: {r['state']}. {r['description']}",
        "author": "assignee_person_id",
        "labels": lambda r: tuple(x for x in (r.get("labels") or "").split("|") if x),
        "uri": lambda r: f"https://linear.app/meridian/issue/{r['issue_key']}",
        "hash": ("issue_key", "title", "description", "state", "assignee_person_id"),
    },
    "github": {
        "file": "github.csv",
        "id": lambda r: f"{r['kind']}-{r['number']}",
        "text": lambda r: (
            f"{r['kind'].upper()} #{r['number']} {r['title']} — state: {r['state']}, "
            f"approvals: {r['approvals']}. {r['body']}"
        ),
        "author": "author_person_id",
        "labels": lambda r: (r["kind"],),
        "uri": lambda r: f"https://github.com/meridian/platform/{r['kind']}/{r['number']}",
        "hash": ("kind", "number", "title", "body", "state", "approvals"),
    },
    "notion": {
        "file": "notion.csv",
        "id": "page_id",
        "text": lambda r: f"{r['title']} — Status: {r['status']}. {r['body']}",
        "author": "author_person_id",
        "labels": lambda r: (r.get("parent") or "",),
        "uri": lambda r: f"https://notion.so/meridian/{r['page_id']}",
        "hash": ("page_id", "title", "body", "status"),
    },
    "email": {
        "file": "email.csv",
        "id": "msg_id",
        "text": lambda r: f"Subject: {r['subject']}\nFrom: {r['from_addr']}\n\n{r['body']}",
        "author": "from_addr",
        "labels": lambda r: (r.get("folder") or "INBOX",),
        "uri": lambda r: f"imap://mail.meridian.dev/{r.get('folder') or 'INBOX'}/{r['msg_id']}",
        "hash": ("msg_id", "from_addr", "to_addr", "subject", "body"),
    },
}

# Tiers mirror the live adapters exactly, so a demo run through fixtures exercises the
# same governance decisions the live run would.
_TIERS: dict[str, dict[str, ActionTier]] = {
    "slack": {"add_reaction": ActionTier.TRIVIAL, "post_reply": ActionTier.INTERNAL,
              "post_message": ActionTier.INTERNAL, "dm_user": ActionTier.GATED},
    "linear": {"add_label": ActionTier.TRIVIAL, "link_issues": ActionTier.TRIVIAL,
               "create_issue": ActionTier.INTERNAL, "comment": ActionTier.INTERNAL,
               "set_state": ActionTier.INTERNAL, "assign": ActionTier.INTERNAL},
    "github": {"add_label": ActionTier.TRIVIAL, "comment": ActionTier.INTERNAL,
               "create_issue": ActionTier.INTERNAL, "set_status": ActionTier.INTERNAL},
    "notion": {"append_block": ActionTier.TRIVIAL, "set_property": ActionTier.INTERNAL,
               "create_page": ActionTier.INTERNAL},
    "email": {"flag": ActionTier.TRIVIAL, "move_folder": ActionTier.TRIVIAL,
              "save_draft": ActionTier.INTERNAL, "send_email": ActionTier.GATED},
}


def _field(row: dict[str, str], spec: Any) -> Any:
    return spec(row) if callable(spec) else row.get(spec, "")


# app -> the column in people.csv holding that app's native handle.
_HANDLE_COLUMN = {
    "slack": "slack_handle",
    "github": "github_login",
    "linear": "linear_name",
    "notion": "notion_name",
    "email": "email",
}


def _load_people(fixture_dir: Path) -> dict[str, dict[str, str]]:
    path = fixture_dir / "people.csv"
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {r["person_id"]: r for r in csv.DictReader(fh) if r.get("person_id")}


class FixtureAdapter:
    """One class, five apps, driven by `_SPEC`."""

    def __init__(self, app: str, fixture_dir: Path | None = None) -> None:
        if app not in _SPEC:
            raise ValueError(f"No fixture spec for {app!r}. Known: {sorted(_SPEC)}")
        self.name = app
        self._spec = _SPEC[app]
        self._dir = fixture_dir or FIXTURE_DIR
        self._rows: dict[str, dict[str, str]] = {}
        self._writes: list[ActionReceipt] = []
        # Records store a person_id; every app knows that person by a different handle.
        # Emitting the raw id meant 71 of 88 facts rendered an author of "p01", and
        # identity-aware search could never match a person's name to their records.
        self._people = _load_people(self._dir)
        self._load()

    def _resolve_author(self, raw: str) -> str:
        person = self._people.get(raw)
        if person is None:
            return raw
        column = _HANDLE_COLUMN.get(self.name, "full_name")
        return person.get(column) or person.get("full_name") or raw

    def _load(self) -> None:
        path = self._dir / self._spec["file"]
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                # Fixtures escape newlines as a literal backslash-n to keep one record
                # per physical line; restore them so text reads naturally.
                clean = {k: (v or "").replace("\\n", "\n") for k, v in row.items()}
                self._rows[str(_field(clean, self._spec["id"]))] = clean

    # -- read ---------------------------------------------------------------

    def probe(self) -> SourceProfile:
        scopes = sorted({lbl for r in self._rows.values()
                         for lbl in _field(r, self._spec["labels"]) if lbl})
        return SourceProfile(
            app=self.name,
            display_name=f"Meridian {self.name} (fixture)",
            scopes=tuple(scopes) or ("default",),
            record_count_estimate=len(self._rows),
        )

    def _to_evidence(self, key: str, row: dict[str, str]) -> Evidence:
        spec = self._spec
        days = int(row.get("days_ago") or 0)
        return Evidence(
            id=key,
            pointer=SourcePointer(
                app=self.name,
                resource_uri=_field(row, spec["uri"]),
                locator={"id": key},
                content_hash=content_hash({f: row.get(f, "") for f in spec["hash"]}),
            ),
            text=_field(row, spec["text"]),
            author=self._resolve_author(_field(row, spec["author"]) or "") or None,
            occurred_at=utcnow() - timedelta(days=days),
            labels=tuple(x for x in _field(row, spec["labels"]) if x),
            raw=dict(row),
        )

    def fetch(self, scope: str | None = None, limit: int = 100) -> list[Evidence]:
        out: list[Evidence] = []
        for key, row in self._rows.items():
            if scope and scope not in _field(row, self._spec["labels"]):
                continue
            out.append(self._to_evidence(key, row))
            if len(out) >= limit:
                break
        return out

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        key = str(pointer.locator.get("id", ""))
        row = self._rows.get(key)
        return self._to_evidence(key, row) if row is not None else None

    # -- write --------------------------------------------------------------

    def capabilities(self) -> ActionCapabilities:
        return ActionCapabilities(app=self.name, operations=dict(_TIERS[self.name]))

    def act(self, action: Action) -> ActionReceipt:
        """Apply the write to the in-memory rows, capturing prior state first."""
        key = str(action.target.get("id", ""))
        prior = dict(self._rows.get(key, {})) or None

        if key in self._rows:
            self._rows[key].update({k: str(v) for k, v in action.payload.items()})
        else:
            created = {k: str(v) for k, v in action.payload.items()}
            created.setdefault("days_ago", "0")
            # Key the synthetic row under the plain id field when the spec uses one,
            # so a created row round-trips through fetch()/resolve() like a real one.
            id_spec = self._spec["id"]
            if isinstance(id_spec, str):
                created[id_spec] = key
            self._rows[key] = created

        receipt = ActionReceipt(
            action_id=f"{self.name}-{len(self._writes) + 1}",
            action=action,
            result={"id": key, "app": self.name},
            prior_state=prior,
        )
        self._writes.append(receipt)
        return receipt

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        """Restore prior state, or mark the row retracted if it was newly created.

        Matches the live adapters' rule: retract rather than erase. A row this adapter
        created is annotated as withdrawn rather than dropped, so the history of the
        action survives the undo.
        """
        key = str(receipt.action.target.get("id", ""))
        if receipt.prior_state is not None:
            self._rows[key] = dict(receipt.prior_state)
        elif key in self._rows:
            self._rows[key]["_retracted"] = f"retracted by Walnut · {receipt.action_id}"

        return ActionReceipt(
            action_id=receipt.action_id,
            action=receipt.action,
            result=receipt.result,
            prior_state=receipt.prior_state,
            executed_at=receipt.executed_at,
            undone_at=utcnow(),
        )


# ---------------------------------------------------------------------------


def load_all_fixtures(fixture_dir: Path | None = None) -> dict[str, FixtureAdapter]:
    """Every app, ready to ingest. The offline equivalent of five live connections."""
    return {app: FixtureAdapter(app, fixture_dir) for app in _SPEC}


def load_identities(fixture_dir: Path | None = None) -> list[Any]:
    """Per-app handles from `people.csv`, for cross-app identity resolution.

    Only `email` carries a verified address, so the other four apps have to be linked
    by name and handle inference — which is the point. If every app handed us an email
    address the resolution problem would not exist.
    """
    from ..identity import Identity

    path = (fixture_dir or FIXTURE_DIR) / "people.csv"
    identities: list[Identity] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = row["full_name"]
            identities.extend(
                [
                    Identity("email", row["email"], name, row["email"]),
                    Identity("slack", row["slack_handle"], name),
                    Identity("github", row["github_login"], name),
                    Identity("linear", row["linear_name"], row["linear_name"]),
                    Identity("notion", row["notion_name"], row["notion_name"]),
                ]
            )
    return identities
