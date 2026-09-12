"""Conformance and behavioural tests for the Linear adapter.

We have no Linear credentials, so `FakeLinearTransport` — an in-memory stand-in for
the GraphQL endpoint — is the only way to exercise `LinearAdapter` at all. Dispatch
inside the fake is keyed off the operation name each document in
`walnut/adapters/linear.py` carries (`WalnutProbe`, `WalnutIssues`, ...), not by
parsing GraphQL, which keeps this fixture honest about testing the adapter's request
shaping and response parsing rather than re-implementing a GraphQL engine.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from walnut.adapters.linear import LinearAdapter, LinearAPIError
from walnut.conformance import run_conformance
from walnut.contract import Action, SourcePointer

TEAM = {"id": "team-1", "key": "ENG", "name": "Engineering"}

STATE_TODO = {"id": "state-todo", "name": "Todo"}
STATE_IN_PROGRESS = {"id": "state-in-progress", "name": "In Progress"}
STATE_DONE = {"id": "state-done", "name": "Done"}
_STATES_BY_ID = {s["id"]: s for s in (STATE_TODO, STATE_IN_PROGRESS, STATE_DONE)}

USER_P01 = {"id": "user-p01", "name": "Priya", "email": "priya@example.com"}
USER_P04 = {"id": "user-p04", "name": "Dev", "email": "dev@example.com"}
_USERS_BY_ID = {u["id"]: u for u in (USER_P01, USER_P04)}

LABEL_BUG = {"id": "label-bug", "name": "bug"}
LABEL_CUSTOMER = {"id": "label-customer-reported", "name": "customer-reported"}
_LABELS_BY_ID = {l["id"]: l for l in (LABEL_BUG, LABEL_CUSTOMER)}


def _issue_412() -> dict[str, Any]:
    return {
        "id": "issue-412-uuid",
        "identifier": "ENG-412",
        "title": "Export times out on large workspaces",
        "description": (
            "Customer (northstar analytics, ~80k row workspace) reports export "
            "hangs and eventually times out on workspaces above ~50k rows."
        ),
        "url": "https://linear.app/walnut/issue/ENG-412/export-times-out",
        "priority": 1,
        "createdAt": "2026-08-23T10:00:00.000Z",
        "updatedAt": "2026-09-01T10:00:00.000Z",
        "state": dict(STATE_IN_PROGRESS),
        "assignee": dict(USER_P01),
        "team": dict(TEAM),
        "labels": {"nodes": [dict(LABEL_BUG), dict(LABEL_CUSTOMER)]},
        "comments": {
            "nodes": [
                {
                    "id": "comment-1-uuid",
                    "body": "PR #288 is up, should land by Friday.",
                    "url": "https://linear.app/walnut/issue/ENG-412#comment-1",
                    "createdAt": "2026-08-24T10:00:00.000Z",
                    "updatedAt": "2026-08-24T10:00:00.000Z",
                    "user": dict(USER_P01),
                }
            ]
        },
        "archivedAt": None,
    }


def _issue_415() -> dict[str, Any]:
    return {
        "id": "issue-415-uuid",
        "identifier": "ENG-415",
        "title": "Add rate limiting to export API",
        "description": "Prevent a single workspace from starving the worker pool.",
        "url": "https://linear.app/walnut/issue/ENG-415/add-rate-limiting",
        "priority": 2,
        "createdAt": "2026-09-06T10:00:00.000Z",
        "updatedAt": "2026-09-06T10:00:00.000Z",
        "state": dict(STATE_TODO),
        "assignee": dict(USER_P04),
        "team": dict(TEAM),
        "labels": {"nodes": [dict(LABEL_BUG)]},
        "comments": {"nodes": []},
        "archivedAt": None,
    }


class FakeLinearTransport:
    """A realistic, in-memory replacement for one GraphQL POST to Linear."""

    def __init__(self) -> None:
        issue_412 = _issue_412()
        issue_415 = _issue_415()
        self.issues: dict[str, dict[str, Any]] = {
            issue_412["id"]: issue_412,
            issue_415["id"]: issue_415,
        }
        self.comments: dict[str, dict[str, Any]] = {
            issue_412["comments"]["nodes"][0]["id"]: issue_412["comments"]["nodes"][0],
        }
        self.calls: list[str] = []
        self.force_errors_on: str | None = None
        self._next_id = 1000

    def _issue_by_identifier_or_id(self, ident: str) -> dict[str, Any] | None:
        for issue in self.issues.values():
            if issue["id"] == ident or issue["identifier"] == ident:
                return issue
        return None

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(query)
        if self.force_errors_on and self.force_errors_on in query:
            return {"errors": [{"message": "internal error (forced by test)"}]}

        if "WalnutProbe" in query:
            return self._probe()
        if "WalnutIssues(" in query:
            return self._list_issues(variables)
        if "WalnutIssueById" in query:
            return self._issue_by_id(variables)
        if "WalnutCommentById" in query:
            return self._comment_by_id(variables)
        if "WalnutIssueLabels" in query:
            return self._issue_labels(variables)
        if "WalnutIssueState" in query:
            return self._issue_state(variables)
        if "WalnutIssueAssignee" in query:
            return self._issue_assignee(variables)
        if "WalnutIssueUpdate" in query:
            return self._issue_update(variables)
        if "WalnutIssueCreate" in query:
            return self._issue_create(variables)
        if "WalnutCommentCreate" in query:
            return self._comment_create(variables)
        if "WalnutIssueRelationCreate" in query:
            return self._issue_relation_create(variables)
        if "WalnutIssueRelationDelete" in query:
            return self._issue_relation_delete(variables)
        if "WalnutIssueArchive" in query:
            return self._issue_archive(variables)
        if "WalnutCommentUpdate" in query:
            return self._comment_update(variables)

        raise AssertionError(f"FakeLinearTransport got an unrecognised query:\n{query}")

    # -- query handlers ---------------------------------------------------

    def _probe(self) -> dict[str, Any]:
        return {
            "data": {
                "viewer": {"id": "bot", "name": "Walnut Bot", "email": "bot@example.com"},
                "teams": {"nodes": [dict(TEAM)]},
            }
        }

    def _list_issues(self, variables: dict[str, Any]) -> dict[str, Any]:
        filt = variables.get("filter")
        first = variables.get("first") or 100
        issues = list(self.issues.values())
        if filt:
            scope = None
            for clause in filt.get("or", []):
                team_clause = clause.get("team")
                if team_clause:
                    scope = team_clause["key"]["eq"]
            if scope is not None:
                issues = [i for i in issues if i["team"]["key"] == scope]
        issues = sorted(issues, key=lambda i: i["identifier"])[:first]
        return {"data": {"issues": {"nodes": [copy.deepcopy(i) for i in issues]}}}

    def _issue_by_id(self, variables: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue_by_identifier_or_id(variables["id"])
        return {"data": {"issue": copy.deepcopy(issue) if issue else None}}

    def _comment_by_id(self, variables: dict[str, Any]) -> dict[str, Any]:
        comment = self.comments.get(variables["id"])
        if not comment:
            return {"data": {"comment": None}}
        owning = None
        for issue in self.issues.values():
            if any(c["id"] == comment["id"] for c in issue["comments"]["nodes"]):
                owning = issue
                break
        out = copy.deepcopy(comment)
        out["issue"] = (
            {"id": owning["id"], "identifier": owning["identifier"], "url": owning["url"]}
            if owning
            else None
        )
        return {"data": {"comment": out}}

    def _issue_labels(self, variables: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue_by_identifier_or_id(variables["id"])
        if not issue:
            return {"data": {"issue": None}}
        return {"data": {"issue": {"id": issue["id"], "labels": copy.deepcopy(issue["labels"])}}}

    def _issue_state(self, variables: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue_by_identifier_or_id(variables["id"])
        if not issue:
            return {"data": {"issue": None}}
        return {"data": {"issue": {"id": issue["id"], "state": dict(issue["state"])}}}

    def _issue_assignee(self, variables: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue_by_identifier_or_id(variables["id"])
        if not issue:
            return {"data": {"issue": None}}
        assignee = dict(issue["assignee"]) if issue["assignee"] else None
        return {"data": {"issue": {"id": issue["id"], "assignee": assignee}}}

    # -- mutation handlers --------------------------------------------------

    def _issue_update(self, variables: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue_by_identifier_or_id(variables["id"])
        if not issue:
            return {"data": {"issueUpdate": {"success": False, "issue": None}}}
        input_ = variables["input"]
        if "labelIds" in input_:
            issue["labels"] = {
                "nodes": [dict(_LABELS_BY_ID[i]) for i in input_["labelIds"] if i in _LABELS_BY_ID]
            }
        if "stateId" in input_:
            sid = input_["stateId"]
            issue["state"] = dict(_STATES_BY_ID.get(sid, {"id": sid, "name": sid}))
        if "assigneeId" in input_:
            aid = input_["assigneeId"]
            issue["assignee"] = dict(_USERS_BY_ID[aid]) if aid in _USERS_BY_ID else (
                None if aid is None else {"id": aid}
            )
        return {"data": {"issueUpdate": {"success": True, "issue": copy.deepcopy(issue)}}}

    def _issue_create(self, variables: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        new_id = f"issue-{self._next_id}-uuid"
        identifier = f"ENG-{self._next_id}"
        input_ = variables["input"]
        issue = {
            "id": new_id,
            "identifier": identifier,
            "title": input_["title"],
            "description": input_.get("description", ""),
            "url": f"https://linear.app/walnut/issue/{identifier}",
            "priority": 0,
            "createdAt": "2026-09-12T00:00:00.000Z",
            "updatedAt": "2026-09-12T00:00:00.000Z",
            "state": dict(STATE_TODO),
            "assignee": None,
            "team": dict(TEAM),
            "labels": {"nodes": []},
            "comments": {"nodes": []},
            "archivedAt": None,
        }
        self.issues[new_id] = issue
        return {
            "data": {
                "issueCreate": {
                    "success": True,
                    "issue": {
                        "id": new_id,
                        "identifier": identifier,
                        "url": issue["url"],
                        "title": issue["title"],
                    },
                }
            }
        }

    def _comment_create(self, variables: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        new_id = f"comment-{self._next_id}-uuid"
        input_ = variables["input"]
        issue = self._issue_by_identifier_or_id(input_["issueId"])
        comment = {
            "id": new_id,
            "body": input_["body"],
            "url": f"{issue['url']}#comment-{new_id}",
            "createdAt": "2026-09-12T00:00:00.000Z",
            "updatedAt": "2026-09-12T00:00:00.000Z",
            "user": {"id": "bot", "name": "Walnut Bot", "email": "bot@example.com"},
        }
        issue["comments"]["nodes"].append(comment)
        self.comments[new_id] = comment
        return {
            "data": {
                "commentCreate": {
                    "success": True,
                    "comment": {
                        "id": new_id,
                        "body": comment["body"],
                        "url": comment["url"],
                        "issue": {"id": issue["id"], "identifier": issue["identifier"]},
                    },
                }
            }
        }

    def _issue_relation_create(self, variables: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        rel_id = f"relation-{self._next_id}-uuid"
        input_ = variables["input"]
        a = self._issue_by_identifier_or_id(input_["issueId"])
        b = self._issue_by_identifier_or_id(input_["relatedIssueId"])
        return {
            "data": {
                "issueRelationCreate": {
                    "success": True,
                    "issueRelation": {
                        "id": rel_id,
                        "type": input_.get("type", "related"),
                        "issue": {"id": a["id"], "identifier": a["identifier"]},
                        "relatedIssue": {"id": b["id"], "identifier": b["identifier"]},
                    },
                }
            }
        }

    def _issue_relation_delete(self, variables: dict[str, Any]) -> dict[str, Any]:
        return {"data": {"issueRelationDelete": {"success": True}}}

    def _issue_archive(self, variables: dict[str, Any]) -> dict[str, Any]:
        issue = self._issue_by_identifier_or_id(variables["id"])
        if issue:
            issue["archivedAt"] = "2026-09-12T00:00:00.000Z"
        return {"data": {"issueArchive": {"success": bool(issue)}}}

    def _comment_update(self, variables: dict[str, Any]) -> dict[str, Any]:
        comment = self.comments.get(variables["id"])
        if not comment:
            return {"data": {"commentUpdate": {"success": False, "comment": None}}}
        comment["body"] = variables["input"]["body"]
        return {"data": {"commentUpdate": {"success": True, "comment": {"id": comment["id"], "body": comment["body"]}}}}


# -- conformance --------------------------------------------------------------


def test_linear_adapter_conforms():
    transport = FakeLinearTransport()
    adapter = LinearAdapter(transport=transport)
    report = run_conformance(
        adapter,
        write_target={
            "operation": "set_state",
            "target": {"issue_id": "issue-412-uuid"},
            "payload": {"state_id": "state-done"},
        },
    )
    assert report.ok, report.render()


# -- hashing --------------------------------------------------------------


def test_content_hash_stable_across_repeated_fetches():
    adapter = LinearAdapter(transport=FakeLinearTransport())
    first = {e.id: e for e in adapter.fetch(limit=10)}
    second = {e.id: e for e in adapter.fetch(limit=10)}
    assert first["ENG-412"].pointer.content_hash == second["ENG-412"].pointer.content_hash


def test_content_hash_changes_when_the_issue_changes():
    transport = FakeLinearTransport()
    adapter = LinearAdapter(transport=transport)
    before = {e.id: e for e in adapter.fetch(limit=10)}["ENG-412"].pointer.content_hash

    transport.issues["issue-412-uuid"]["description"] = "Root cause found: missing pagination."
    after = {e.id: e for e in adapter.fetch(limit=10)}["ENG-412"].pointer.content_hash

    assert before != after


def test_content_hash_does_not_move_for_a_cosmetic_field():
    """updatedAt is not part of the stable payload — bumping it alone must not move
    the hash, or drift detection would fire on every no-op sync."""
    transport = FakeLinearTransport()
    adapter = LinearAdapter(transport=transport)
    before = {e.id: e for e in adapter.fetch(limit=10)}["ENG-412"].pointer.content_hash

    transport.issues["issue-412-uuid"]["updatedAt"] = "2026-09-12T09:00:00.000Z"
    after = {e.id: e for e in adapter.fetch(limit=10)}["ENG-412"].pointer.content_hash

    assert before == after


# -- resolve --------------------------------------------------------------


def test_resolve_returns_none_for_a_missing_issue():
    adapter = LinearAdapter(transport=FakeLinearTransport())
    ghost = SourcePointer(
        app="linear",
        resource_uri="https://linear.app/walnut/issue/ENG-9999",
        locator={"type": "issue", "id": "issue-does-not-exist"},
        content_hash="0" * 64,
    )
    assert adapter.resolve(ghost) is None


def test_resolve_returns_none_rather_than_raising_on_a_graphql_error():
    """A malformed id can come back from Linear as a GraphQL error instead of a
    null result — resolve() must treat that the same way: gone, not a crash."""
    transport = FakeLinearTransport()
    transport.force_errors_on = "WalnutIssueById"
    adapter = LinearAdapter(transport=transport)
    ghost = SourcePointer(
        app="linear",
        resource_uri="https://linear.app/walnut/issue/not-a-real-id",
        locator={"id": "not-a-real-uuid"},
        content_hash="0" * 64,
    )
    assert adapter.resolve(ghost) is None


def test_resolve_roundtrips_a_fetched_issue():
    adapter = LinearAdapter(transport=FakeLinearTransport())
    fetched = {e.id: e for e in adapter.fetch(limit=10)}["ENG-412"]
    resolved = adapter.resolve(fetched.pointer)
    assert resolved is not None
    assert resolved.pointer.content_hash == fetched.pointer.content_hash


def test_resolve_fetches_a_comment_too():
    adapter = LinearAdapter(transport=FakeLinearTransport())
    comment_evidence = next(
        e for e in adapter.fetch(limit=10) if e.pointer.locator.get("type") == "comment"
    )
    resolved = adapter.resolve(comment_evidence.pointer)
    assert resolved is not None
    assert resolved.text == "PR #288 is up, should land by Friday."


# -- errors --------------------------------------------------------------


def test_graphql_errors_array_is_treated_as_an_error_not_a_success():
    transport = FakeLinearTransport()
    transport.force_errors_on = "WalnutIssues("
    adapter = LinearAdapter(transport=transport)
    with pytest.raises(LinearAPIError):
        adapter.fetch(limit=5)


# -- act / undo --------------------------------------------------------------


def test_set_state_captures_prior_state_before_writing():
    adapter = LinearAdapter(transport=FakeLinearTransport())
    action = Action(
        app="linear",
        operation="set_state",
        target={"issue_id": "issue-412-uuid"},
        payload={"state_id": "state-done"},
        justified_by=("linear:ENG-412",),
        rationale="PR #288 merged",
    )
    receipt = adapter.act(action)
    assert receipt.prior_state == {"state_id": "state-in-progress", "state_name": "In Progress"}
    assert receipt.result["issue"]["state"]["id"] == "state-done"


def test_undo_restores_the_prior_state():
    transport = FakeLinearTransport()
    adapter = LinearAdapter(transport=transport)
    action = Action(
        app="linear",
        operation="set_state",
        target={"issue_id": "issue-412-uuid"},
        payload={"state_id": "state-done"},
        justified_by=("linear:ENG-412",),
    )
    receipt = adapter.act(action)
    assert transport.issues["issue-412-uuid"]["state"]["id"] == "state-done"

    undone = adapter.undo(receipt)
    assert undone.is_undone
    assert transport.issues["issue-412-uuid"]["state"]["id"] == "state-in-progress"


def test_undo_create_issue_archives_rather_than_deletes():
    transport = FakeLinearTransport()
    adapter = LinearAdapter(transport=transport)
    action = Action(
        app="linear",
        operation="create_issue",
        target={"team_id": "team-1"},
        payload={"title": "Follow-up from customer call"},
        justified_by=("linear:ENG-412",),
    )
    receipt = adapter.act(action)
    created_id = receipt.result["issue"]["id"]
    assert created_id in transport.issues

    undone = adapter.undo(receipt)
    assert undone.is_undone
    assert transport.issues[created_id]["archivedAt"] is not None
    assert created_id in transport.issues  # archived, never removed


def test_undo_comment_redacts_rather_than_deletes():
    transport = FakeLinearTransport()
    adapter = LinearAdapter(transport=transport)
    action = Action(
        app="linear",
        operation="comment",
        target={"issue_id": "issue-412-uuid"},
        payload={"body": "Walnut: this looks related to ENG-405."},
        justified_by=("linear:ENG-405",),
    )
    receipt = adapter.act(action)
    comment_id = receipt.result["comment"]["id"]
    assert transport.comments[comment_id]["body"] == "Walnut: this looks related to ENG-405."

    undone = adapter.undo(receipt)
    assert undone.is_undone
    assert transport.comments[comment_id]["body"].startswith("[retracted by Walnut")
    assert comment_id in transport.comments  # edited, never removed
