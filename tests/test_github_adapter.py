"""Conformance and behavioural tests for the GitHub adapter.

No GitHub credentials exist yet, so `FakeGitHubTransport` below is not a shortcut —
it is the only way this adapter's behaviour can be checked at all. It implements
enough of the real REST surface (pulls, issues, reviews, labels, comments, and commit
statuses) to exercise every method the adapter defines, including the parts of the
GitHub API that are easy to get subtly wrong: the issues endpoint re-listing PRs, and
commit statuses being additive (many statuses can share a sha) rather than a single
mutable field.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from walnut.adapters.github import GitHubAdapter, GitHubNotFound
from walnut.conformance import run_conformance
from walnut.contract import Action, ActionTier


class FakeGitHubTransport:
    """A minimal, stateful stand-in for `api.github.com`.

    Records every call it receives (`self.calls`) so tests can assert on request
    shape, and keeps enough mutable state (pulls, issues, reviews, comments,
    statuses) that writes made through the adapter are actually visible to
    subsequent reads through the same transport — exactly as the real API would
    behave.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

        self.repos: list[dict[str, Any]] = [
            {"full_name": "acme/widgets", "id": 1, "private": False},
        ]

        self.pulls: dict[int, dict[str, Any]] = {
            42: {
                "number": 42,
                "title": "fix: export timeout on large workspaces",
                "body": "Adds cursor-based pagination so large workspaces stop "
                "timing out. Holding on merge until QA signs off.",
                "state": "open",
                "merged": False,
                "merged_at": None,
                "html_url": "https://github.com/acme/widgets/pull/42",
                "user": {"login": "sarah-k"},
                "labels": [{"name": "bug"}],
                "created_at": "2026-09-06T12:00:00Z",
                "updated_at": "2026-09-06T12:00:00Z",
            }
        }
        self.reviews: dict[int, list[dict[str, Any]]] = {
            42: [
                {
                    "user": {"login": "amir-hassan"},
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-09-06T13:00:00Z",
                }
            ]
        }
        self.issues: dict[int, dict[str, Any]] = {
            50: {
                "number": 50,
                "title": "rate limit export API per workspace",
                "body": "A workspace kicking off 10+ concurrent exports can starve "
                "the worker pool for everyone else.",
                "state": "open",
                "html_url": "https://github.com/acme/widgets/issues/50",
                "user": {"login": "priya"},
                "labels": [],
                "created_at": "2026-09-07T09:00:00Z",
                "updated_at": "2026-09-07T09:00:00Z",
            }
        }
        self._next_issue_number = 51
        self._next_comment_id = 1000
        self.comments: dict[int, dict[str, Any]] = {}
        # sha -> list of status dicts, most recently posted first (as GitHub returns them)
        self.statuses: dict[str, list[dict[str, Any]]] = {}
        self._next_status_id = 9000

    # -- dispatch -------------------------------------------------------------

    def __call__(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path, kwargs))
        parts = [p for p in path.split("/") if p]

        if method == "GET" and path == "/user/repos":
            return self.repos

        if len(parts) >= 3 and parts[0] == "repos":
            owner, repo = parts[1], parts[2]
            rest = parts[3:]

            if rest == ["pulls"] and method == "GET":
                return list(self.pulls.values())
            if rest == ["issues"] and method == "GET":
                # The real issues endpoint also returns PRs, each carrying a
                # "pull_request" key. Reproduce that so the adapter's dedup logic
                # has something real to filter.
                pr_as_issue = []
                for pr in self.pulls.values():
                    item = dict(pr)
                    item["pull_request"] = {"url": f"{pr['html_url']}"}
                    pr_as_issue.append(item)
                return list(self.issues.values()) + pr_as_issue
            if rest == ["issues"] and method == "POST":
                number = self._next_issue_number
                self._next_issue_number += 1
                body = kwargs.get("json", {})
                issue = {
                    "number": number,
                    "title": body.get("title", ""),
                    "body": body.get("body", ""),
                    "state": "open",
                    "html_url": f"https://github.com/{owner}/{repo}/issues/{number}",
                    "user": {"login": "walnut-bot"},
                    "labels": [],
                    "created_at": "2026-09-12T00:00:00Z",
                    "updated_at": "2026-09-12T00:00:00Z",
                }
                self.issues[number] = issue
                return issue

            if len(rest) == 2 and rest[0] == "pulls" and method == "GET":
                number = int(rest[1])
                if number not in self.pulls:
                    raise GitHubNotFound(path)
                return self.pulls[number]

            if len(rest) == 2 and rest[0] == "issues" and method == "GET":
                number = int(rest[1])
                if number in self.issues:
                    return self.issues[number]
                if number in self.pulls:
                    return self.pulls[number]  # GitHub serves PR-as-issue here too
                raise GitHubNotFound(path)

            if len(rest) == 2 and rest[0] == "issues" and method == "PATCH":
                number = int(rest[1])
                record = self.issues.get(number) or self.pulls.get(number)
                if record is None:
                    raise GitHubNotFound(path)
                record.update(kwargs.get("json", {}))
                return record

            if len(rest) == 3 and rest[0] == "pulls" and rest[2] == "reviews" and method == "GET":
                number = int(rest[1])
                return self.reviews.get(number, [])

            if len(rest) == 3 and rest[0] == "issues" and rest[2] == "comments" and method == "POST":
                number = int(rest[1])
                comment_id = self._next_comment_id
                self._next_comment_id += 1
                comment = {
                    "id": comment_id,
                    "body": kwargs.get("json", {}).get("body", ""),
                    "html_url": f"https://github.com/{owner}/{repo}/issues/{number}#issuecomment-{comment_id}",
                }
                self.comments[comment_id] = comment
                return comment

            if len(rest) == 3 and rest[0] == "issues" and rest[2] == "labels" and method == "POST":
                number = int(rest[1])
                record = self.issues.get(number) or self.pulls.get(number)
                if record is None:
                    raise GitHubNotFound(path)
                existing = {_name(label) for label in record.get("labels", [])}
                for name in kwargs.get("json", {}).get("labels", []):
                    if name not in existing:
                        record.setdefault("labels", []).append({"name": name})
                        existing.add(name)
                return record["labels"]

            if len(rest) == 4 and rest[0] == "issues" and rest[2] == "labels" and method == "DELETE":
                number, label_name = int(rest[1]), rest[3]
                record = self.issues.get(number) or self.pulls.get(number)
                if record is None:
                    raise GitHubNotFound(path)
                labels = record.get("labels", [])
                remaining = [label for label in labels if _name(label) != label_name]
                if len(remaining) == len(labels):
                    raise GitHubNotFound(path)  # label was not present, mirrors real API
                record["labels"] = remaining
                return {}

            if len(rest) == 3 and rest[0] == "issues" and rest[1] == "comments" and method == "PATCH":
                comment_id = int(rest[2])
                comment = self.comments.get(comment_id)
                if comment is None:
                    raise GitHubNotFound(path)
                comment.update(kwargs.get("json", {}))
                return comment

            if len(rest) == 2 and rest[0] == "statuses" and method == "POST":
                sha = rest[1]
                body = kwargs.get("json", {})
                status_id = self._next_status_id
                self._next_status_id += 1
                status = {
                    "id": status_id,
                    "state": body.get("state"),
                    "context": body.get("context"),
                    "description": body.get("description", ""),
                    "target_url": body.get("target_url"),
                }
                self.statuses.setdefault(sha, []).insert(0, status)
                return status

            if len(rest) == 3 and rest[0] == "commits" and rest[2] == "statuses" and method == "GET":
                sha = rest[1]
                return self.statuses.get(sha, [])

        raise AssertionError(f"FakeGitHubTransport has no route for {method} {path}")


def _name(label: Any) -> str:
    return label["name"] if isinstance(label, dict) else str(label)


# -- fixtures -----------------------------------------------------------------


@pytest.fixture
def transport() -> FakeGitHubTransport:
    return FakeGitHubTransport()


@pytest.fixture
def adapter(transport: FakeGitHubTransport) -> GitHubAdapter:
    return GitHubAdapter(transport=transport)


# -- conformance ----------------------------------------------------------------


def test_github_adapter_conforms(adapter: GitHubAdapter) -> None:
    report = run_conformance(
        adapter,
        write_target={
            "operation": "add_label",
            "target": {"owner": "acme", "repo": "widgets", "number": 42},
            "payload": {"label": "walnut-conformance"},
        },
    )
    assert report.ok, report.render()


# -- hashing --------------------------------------------------------------------


def test_content_hash_is_stable_across_repeated_fetches(adapter: GitHubAdapter) -> None:
    first = {e.id: e for e in adapter.fetch(scope="acme/widgets")}
    second = {e.id: e for e in adapter.fetch(scope="acme/widgets")}
    assert first.keys() == second.keys()
    for evidence_id in first:
        assert first[evidence_id].pointer.content_hash == second[evidence_id].pointer.content_hash


def test_content_hash_changes_when_the_pr_changes(adapter: GitHubAdapter, transport: FakeGitHubTransport) -> None:
    before = adapter.resolve(
        SourcePointerFor(adapter, owner="acme", repo="widgets", number=42, kind="pull")
    )
    assert before is not None

    transport.pulls[42]["title"] = "fix: export timeout on large workspaces (v2)"
    transport.pulls[42]["state"] = "closed"
    transport.pulls[42]["merged"] = True

    after = adapter.resolve(
        SourcePointerFor(adapter, owner="acme", repo="widgets", number=42, kind="pull")
    )
    assert after is not None
    assert after.pointer.content_hash != before.pointer.content_hash


def SourcePointerFor(adapter: GitHubAdapter, *, owner: str, repo: str, number: int, kind: str):
    from walnut.contract import SourcePointer

    return SourcePointer(
        app=adapter.name,
        resource_uri=f"https://github.com/{owner}/{repo}/{'pull' if kind == 'pull' else 'issues'}/{number}",
        locator={"owner": owner, "repo": repo, "number": number, "kind": kind},
        content_hash="0" * 64,  # resolve() re-derives the real hash; this is a lookup key only
    )


# -- 404 handling -----------------------------------------------------------------


def test_resolve_returns_none_for_a_missing_pull_request(adapter: GitHubAdapter) -> None:
    pointer = SourcePointerFor(adapter, owner="acme", repo="widgets", number=9999, kind="pull")
    assert adapter.resolve(pointer) is None


def test_resolve_returns_none_without_owner_repo_number(adapter: GitHubAdapter) -> None:
    from walnut.contract import SourcePointer

    ghost = SourcePointer(
        app=adapter.name,
        resource_uri="https://example.invalid/nope",
        locator={"id": "not-a-real-locator"},
        content_hash="0" * 64,
    )
    assert adapter.resolve(ghost) is None


# -- PR vs issue discrimination -----------------------------------------------------


def test_fetch_does_not_double_count_prs_from_the_issues_endpoint(adapter: GitHubAdapter) -> None:
    records = adapter.fetch(scope="acme/widgets", limit=100)
    ids = [e.id for e in records]
    assert ids.count("acme/widgets#42") == 1  # the PR, not also its issue-shaped twin
    assert "acme/widgets#50" in ids  # the real issue is still there

    pr_evidence = next(e for e in records if e.id == "acme/widgets#42")
    issue_evidence = next(e for e in records if e.id == "acme/widgets#50")
    assert "pull_request" in pr_evidence.labels
    assert "issue" in issue_evidence.labels


def test_pull_request_evidence_carries_review_state(adapter: GitHubAdapter) -> None:
    records = adapter.fetch(scope="acme/widgets")
    pr_evidence = next(e for e in records if e.id == "acme/widgets#42")
    assert pr_evidence.raw["_reviews"][0]["state"] == "CHANGES_REQUESTED"


# -- set_status payload shape --------------------------------------------------


def test_set_status_posts_the_expected_payload_and_reads_prior_state_first(
    adapter: GitHubAdapter, transport: FakeGitHubTransport
) -> None:
    action = Action(
        app="github",
        operation="set_status",
        target={"owner": "acme", "repo": "widgets", "sha": "deadbeef"},
        payload={
            "state": "success",
            "description": "Walnut verified this claim.",
            "target_url": "https://walnut.example/evidence/1",
        },
        justified_by=("github:acme/widgets#42",),
    )
    receipt = adapter.act(action)

    post_calls = [c for c in transport.calls if c[0] == "POST" and c[1].endswith("/statuses/deadbeef")]
    assert len(post_calls) == 1
    _, _, kwargs = post_calls[0]
    body = kwargs["json"]
    assert body == {
        "state": "success",
        "context": "walnut/evidence",
        "description": "Walnut verified this claim.",
        "target_url": "https://walnut.example/evidence/1",
    }
    # No status existed before this call, so prior_state must say so rather than
    # invent one.
    assert receipt.prior_state is None

    # A second call for the same sha+context must see the first one as prior state.
    action2 = Action(
        app="github",
        operation="set_status",
        target={"owner": "acme", "repo": "widgets", "sha": "deadbeef"},
        payload={"state": "failure", "description": "Regressed."},
        justified_by=("github:acme/widgets#42",),
    )
    receipt2 = adapter.act(action2)
    assert receipt2.prior_state == {
        "state": "success",
        "description": "Walnut verified this claim.",
        "target_url": "https://walnut.example/evidence/1",
        "context": "walnut/evidence",
    }


def test_set_status_capability_is_internal_tier(adapter: GitHubAdapter) -> None:
    caps = adapter.capabilities()
    assert caps.tier_of("set_status") == ActionTier.INTERNAL
    assert caps.tier_of("add_label") == ActionTier.TRIVIAL
    assert caps.tier_of("comment") == ActionTier.INTERNAL
    assert caps.tier_of("create_issue") == ActionTier.INTERNAL


# -- undo retracts rather than deletes -----------------------------------------


def test_undo_comment_edits_the_comment_rather_than_deleting_it(
    adapter: GitHubAdapter, transport: FakeGitHubTransport
) -> None:
    action = Action(
        app="github",
        operation="comment",
        target={"owner": "acme", "repo": "widgets", "number": 42},
        payload={"body": "Walnut: this PR is cited as evidence for claim X."},
        justified_by=("github:acme/widgets#42",),
    )
    receipt = adapter.act(action)
    comment_id = receipt.result["comment_id"]
    assert comment_id in transport.comments

    undone = adapter.undo(receipt)

    assert undone.is_undone
    assert comment_id in transport.comments, "undo must not delete the comment"
    assert "[retracted by Walnut" in transport.comments[comment_id]["body"]
    patch_calls = [c for c in transport.calls if c[0] == "PATCH" and "comments" in c[1]]
    assert patch_calls, "undo must PATCH (edit) the comment, not DELETE it"


def test_undo_create_issue_closes_with_a_comment_rather_than_deleting(
    adapter: GitHubAdapter, transport: FakeGitHubTransport
) -> None:
    action = Action(
        app="github",
        operation="create_issue",
        target={"owner": "acme", "repo": "widgets"},
        payload={"title": "Walnut: contradiction detected", "body": "See evidence trail."},
        justified_by=("github:acme/widgets#42", "github:acme/widgets#50"),
    )
    receipt = adapter.act(action)
    number = receipt.result["number"]
    assert number in transport.issues
    assert transport.issues[number]["state"] == "open"

    undone = adapter.undo(receipt)

    assert undone.is_undone
    assert number in transport.issues, "undo must not delete the issue"
    assert transport.issues[number]["state"] == "closed"
    retraction_comments = [c for c in transport.comments.values() if "[retracted by Walnut" in c["body"]]
    assert retraction_comments, "closing on undo must leave a retraction comment"


def test_undo_add_label_removes_only_the_label_that_was_added(
    adapter: GitHubAdapter, transport: FakeGitHubTransport
) -> None:
    action = Action(
        app="github",
        operation="add_label",
        target={"owner": "acme", "repo": "widgets", "number": 42},
        payload={"label": "walnut-flagged"},
        justified_by=("github:acme/widgets#42",),
    )
    receipt = adapter.act(action)
    assert any(_name(l) == "walnut-flagged" for l in transport.pulls[42]["labels"])
    assert any(_name(l) == "bug" for l in transport.pulls[42]["labels"]), "pre-existing label must survive"

    adapter.undo(receipt)

    labels = {_name(l) for l in transport.pulls[42]["labels"]}
    assert "walnut-flagged" not in labels
    assert "bug" in labels


def test_undo_add_label_leaves_a_pre_existing_label_alone(
    adapter: GitHubAdapter, transport: FakeGitHubTransport
) -> None:
    action = Action(
        app="github",
        operation="add_label",
        target={"owner": "acme", "repo": "widgets", "number": 42},
        payload={"label": "bug"},  # already present before this action
        justified_by=("github:acme/widgets#42",),
    )
    receipt = adapter.act(action)
    adapter.undo(receipt)
    labels = {_name(l) for l in transport.pulls[42]["labels"]}
    assert "bug" in labels, "undo must not remove a label that pre-dates the action"


def test_undo_set_status_supersedes_with_prior_state_not_a_deletion(
    adapter: GitHubAdapter, transport: FakeGitHubTransport
) -> None:
    first = Action(
        app="github",
        operation="set_status",
        target={"owner": "acme", "repo": "widgets", "sha": "cafef00d"},
        payload={"state": "success", "description": "verified"},
        justified_by=("github:acme/widgets#42",),
    )
    receipt = adapter.act(first)
    assert receipt.prior_state is None

    undone = adapter.undo(receipt)
    assert undone.is_undone
    # There was nothing before, so undo posts a new, superseding neutral status
    # rather than trying to delete the one it wrote (GitHub has no such endpoint).
    latest = transport.statuses["cafef00d"][0]
    assert latest["state"] == "pending"
    assert "[retracted by Walnut" in latest["description"]
    # The original status is still in the history, not erased.
    assert any(s["state"] == "success" for s in transport.statuses["cafef00d"])


def test_undo_set_status_restores_the_earlier_status_when_one_existed(
    adapter: GitHubAdapter, transport: FakeGitHubTransport
) -> None:
    original = Action(
        app="github",
        operation="set_status",
        target={"owner": "acme", "repo": "widgets", "sha": "abc123"},
        payload={"state": "pending", "description": "queued"},
        justified_by=("github:acme/widgets#42",),
    )
    adapter.act(original)

    supersede = Action(
        app="github",
        operation="set_status",
        target={"owner": "acme", "repo": "widgets", "sha": "abc123"},
        payload={"state": "success", "description": "verified"},
        justified_by=("github:acme/widgets#42",),
    )
    receipt = adapter.act(supersede)
    assert receipt.prior_state == {
        "state": "pending",
        "description": "queued",
        "target_url": None,
        "context": "walnut/evidence",
    }

    adapter.undo(receipt)
    latest = transport.statuses["abc123"][0]
    assert latest["state"] == "pending"
    assert latest["description"] == "queued"


def test_probe_reports_the_single_accessible_repo(adapter: GitHubAdapter) -> None:
    profile = adapter.probe()
    assert profile.app == "github"
    assert profile.scopes == ("acme/widgets",)


def test_fetch_honours_limit_across_pulls_and_issues(adapter: GitHubAdapter) -> None:
    records = adapter.fetch(scope="acme/widgets", limit=1)
    assert len(records) == 1
