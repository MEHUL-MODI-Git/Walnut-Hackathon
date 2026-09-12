"""The GitHub adapter: pull requests and issues as cited evidence, plus governed writes.

GitHub is unusual among Walnut's five apps in one respect: it is the only source where
an action Walnut takes (a commit status) shows up as a visible, third-party-verifiable
artefact on the thing it is about — a red or green check on a pull request. That is
also why `set_status` gets the most careful treatment in this file: it is the one
write where "read the prior state before writing, and supersede rather than erase on
undo" has an externally visible consequence if we get it wrong.

Design choices worth recording:

* **`transport` is the only seam.** The constructor takes a plain callable
  `(method, path, **kwargs) -> Any` rather than an httpx client, a requests session, or
  a PyGithub object. That is deliberate: this adapter has never made a real network
  call against api.github.com (no credentials exist yet), so the fake transport used
  in `tests/test_github_adapter.py` is not a mocking convenience, it is the *only*
  evidence this adapter behaves correctly. If the real transport and the fake one
  disagree about shape, that is a bug worth finding now rather than at demo time.

* **The transport returns whatever `response.json()` produces**, which is a `dict` for
  single-resource endpoints (`GET .../pulls/42`) and a `list` for collection endpoints
  (`GET .../pulls`). The task brief that seeded this module described the transport's
  return type as `dict`; that is not quite what the GitHub REST API does, so this file
  types it as `Any` rather than silently mistyping every list-returning call.

* **404 is a first-class transport outcome, not an exception someone forgot to catch.**
  The transport raises `GitHubNotFound` for a 404 response. `resolve()` catches it and
  returns `None`, exactly as the contract requires: a missing record is a fact, not a
  crash.

* **The issues endpoint also returns pull requests.** GitHub does this because a PR
  *is* an issue with a diff attached. Every item from `GET /repos/{o}/{r}/issues` that
  carries a `"pull_request"` key is a PR we already picked up from the pulls endpoint,
  and is dropped here to avoid double-counting the same record under two identities.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import httpx

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

__all__ = ["GitHubAdapter", "GitHubAPIError", "GitHubNotFound"]

_API_ROOT = "https://api.github.com"
_DEFAULT_STATUS_CONTEXT = "walnut/evidence"

Transport = Callable[..., Any]
"""`(method, path, **kwargs) -> Any`. One REST call against api.github.com. Kwargs are
forwarded verbatim to the underlying HTTP call (`params=`, `json=`) so the transport
stays a thin seam rather than a second place that knows about GitHub's request shapes."""


class GitHubAPIError(RuntimeError):
    """The GitHub API returned an error this adapter is not entitled to swallow."""

    def __init__(self, status_code: int, path: str, payload: Any = None) -> None:
        super().__init__(f"GitHub API error {status_code} for {path}: {payload!r}")
        self.status_code = status_code
        self.path = path
        self.payload = payload


class GitHubNotFound(GitHubAPIError):
    """HTTP 404. The record does not exist — never raised past `resolve()`."""

    def __init__(self, path: str) -> None:
        super().__init__(404, path, payload=None)


class GitHubAdapter:
    """Pull requests and issues as evidence; labels, comments, issues, and commit
    statuses as governed actions.

    `token` and `transport` are both optional and mutually exclusive in practice: pass
    `token` alone to get a real `httpx`-backed transport authorised as that token, or
    pass `transport` (typically a fake, in tests) and `token` is ignored because there
    is no default transport left for it to authorise.
    """

    name = "github"

    def __init__(self, token: str | None = None, transport: Transport | None = None) -> None:
        self._token = token
        self._transport: Transport = transport or self._build_default_transport(token)

    @staticmethod
    def _build_default_transport(token: str | None) -> Transport:
        """The real transport. Never exercised by the test suite — there are no
        GitHub credentials available in this environment yet — so keep it as thin
        and literal a translation of the contract as possible: anything clever here
        would be untested cleverness."""

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        def _real_transport(method: str, path: str, **kwargs: Any) -> Any:
            response = httpx.request(
                method, f"{_API_ROOT}{path}", headers=headers, timeout=30.0, **kwargs
            )
            if response.status_code == 404:
                raise GitHubNotFound(path)
            if response.status_code >= 400:
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
                raise GitHubAPIError(response.status_code, path, payload)
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        return _real_transport

    # -- transport helpers ----------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._transport("GET", path, params=params or {})

    # -- read -------------------------------------------------------------------

    def probe(self) -> SourceProfile:
        """List the repos this token can see. Cheap and read-only, as the contract
        requires — a single page of `/user/repos`, no per-repo detail fetched."""
        repos = self._get(
            "/user/repos",
            params={"per_page": 100, "affiliation": "owner,collaborator,organization_member"},
        )
        if not isinstance(repos, list):
            repos = []
        scopes = tuple(sorted({r["full_name"] for r in repos if r.get("full_name")}))
        return SourceProfile(
            app=self.name,
            display_name="GitHub",
            scopes=scopes,
            record_count_estimate=len(scopes),
            detail={"repo_count": len(scopes)},
        )

    def fetch(self, scope: str | None = None, limit: int = 100) -> list[Evidence]:
        """Pull requests and issues for one `owner/repo`, newest activity first.

        `scope=None` falls back to the first repo `probe()` reports, so the
        conformance suite (which calls `fetch(limit=5)` with no scope) has something
        real to inspect rather than an empty list that just happens to pass.
        """
        if scope is None:
            profile = self.probe()
            if not profile.scopes:
                return []
            scope = profile.scopes[0]

        owner, _, repo = scope.partition("/")
        if not repo:
            raise ValueError(f"github scope must be 'owner/repo', got {scope!r}")

        per_page = max(1, min(limit, 100))
        pulls = self._get(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "all", "per_page": per_page, "sort": "updated", "direction": "desc"},
        )
        issues_raw = self._get(
            f"/repos/{owner}/{repo}/issues",
            params={"state": "all", "per_page": per_page, "sort": "updated", "direction": "desc"},
        )
        pulls = pulls if isinstance(pulls, list) else []
        issues_raw = issues_raw if isinstance(issues_raw, list) else []
        # The issues endpoint re-lists every PR too; those are dropped here because
        # `pulls` above already carries them (with review state the issues endpoint
        # does not report at all).
        issues = [item for item in issues_raw if "pull_request" not in item]

        evidence = [self._pr_to_evidence(owner, repo, pr) for pr in pulls]
        evidence += [self._issue_to_evidence(owner, repo, issue) for issue in issues]
        evidence.sort(key=lambda e: e.occurred_at or e.pointer.retrieved_at, reverse=True)
        return evidence[:limit]

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        """Re-fetch one PR or issue by the repo+number the pointer's locator carries.

        A pointer without `owner`/`repo`/`number` (for instance a synthetic one built
        by a caller checking the missing-record path) is treated as unresolvable
        rather than as a reason to guess at a path and hit the network anyway.
        """
        locator = pointer.locator
        owner, repo, number = locator.get("owner"), locator.get("repo"), locator.get("number")
        if not owner or not repo or number is None:
            return None

        kind = locator.get("kind", "issue")
        endpoint = "pulls" if kind == "pull" else "issues"
        try:
            item = self._get(f"/repos/{owner}/{repo}/{endpoint}/{number}")
        except GitHubNotFound:
            return None

        if kind == "pull":
            return self._pr_to_evidence(owner, repo, item)
        return self._issue_to_evidence(owner, repo, item)

    # -- evidence construction --------------------------------------------------

    def _pr_to_evidence(self, owner: str, repo: str, pr: dict[str, Any]) -> Evidence:
        number = pr["number"]
        reviews = self._get(f"/repos/{owner}/{repo}/pulls/{number}/reviews", params={"per_page": 100})
        reviews = reviews if isinstance(reviews, list) else []
        decision = _review_decision(reviews)

        stable = {
            "title": pr.get("title") or "",
            "body": pr.get("body") or "",
            "state": pr.get("state") or "",
            "merged": bool(pr.get("merged") or pr.get("merged_at")),
            "review_decision": decision,
        }
        pointer = SourcePointer(
            app=self.name,
            resource_uri=pr.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}",
            locator={"owner": owner, "repo": repo, "number": number, "kind": "pull"},
            content_hash=content_hash(stable),
        )
        raw = dict(pr)
        raw["_reviews"] = reviews
        labels = tuple(_label_name(label) for label in pr.get("labels") or [])
        labels += ("pull_request", (pr.get("state") or "").lower())
        return Evidence(
            id=f"{owner}/{repo}#{number}",
            pointer=pointer,
            text=_compose_text(pr.get("title"), pr.get("body")),
            author=(pr.get("user") or {}).get("login"),
            occurred_at=_parse_dt(pr.get("updated_at") or pr.get("created_at")),
            labels=labels,
            raw=raw,
        )

    def _issue_to_evidence(self, owner: str, repo: str, issue: dict[str, Any]) -> Evidence:
        number = issue["number"]
        stable = {
            "title": issue.get("title") or "",
            "body": issue.get("body") or "",
            "state": issue.get("state") or "",
            "merged": False,
            "review_decision": None,
        }
        pointer = SourcePointer(
            app=self.name,
            resource_uri=issue.get("html_url") or f"https://github.com/{owner}/{repo}/issues/{number}",
            locator={"owner": owner, "repo": repo, "number": number, "kind": "issue"},
            content_hash=content_hash(stable),
        )
        labels = tuple(_label_name(label) for label in issue.get("labels") or [])
        labels += ("issue", (issue.get("state") or "").lower())
        return Evidence(
            id=f"{owner}/{repo}#{number}",
            pointer=pointer,
            text=_compose_text(issue.get("title"), issue.get("body")),
            author=(issue.get("user") or {}).get("login"),
            occurred_at=_parse_dt(issue.get("updated_at") or issue.get("created_at")),
            labels=labels,
            raw=dict(issue),
        )

    # -- write --------------------------------------------------------------

    def capabilities(self) -> ActionCapabilities:
        return ActionCapabilities(
            app=self.name,
            operations={
                "add_label": ActionTier.TRIVIAL,
                "comment": ActionTier.INTERNAL,
                "create_issue": ActionTier.INTERNAL,
                "set_status": ActionTier.INTERNAL,
            },
        )

    def act(self, action: Action) -> ActionReceipt:
        if action.operation == "add_label":
            return self._act_add_label(action)
        if action.operation == "comment":
            return self._act_comment(action)
        if action.operation == "create_issue":
            return self._act_create_issue(action)
        if action.operation == "set_status":
            return self._act_set_status(action)
        raise KeyError(f"github adapter has no act handler for operation {action.operation!r}")

    def _act_add_label(self, action: Action) -> ActionReceipt:
        owner, repo, number = action.target["owner"], action.target["repo"], action.target["number"]
        label = action.payload["label"]

        current = self._get(f"/repos/{owner}/{repo}/issues/{number}")
        prior_labels = [_label_name(label_obj) for label_obj in (current.get("labels") or [])]

        self._transport(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/labels", json={"labels": [label]}
        )
        return ActionReceipt(
            action_id=f"github:add_label:{owner}/{repo}#{number}:{label}",
            action=action,
            result={"owner": owner, "repo": repo, "number": number, "added": label},
            prior_state={"labels": prior_labels},
        )

    def _act_comment(self, action: Action) -> ActionReceipt:
        owner, repo, number = action.target["owner"], action.target["repo"], action.target["number"]
        body = action.payload["body"]

        created = self._transport(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body}
        )
        return ActionReceipt(
            action_id=f"github:comment:{created.get('id')}",
            action=action,
            result={
                "owner": owner,
                "repo": repo,
                "comment_id": created.get("id"),
                "html_url": created.get("html_url"),
            },
            # Additive: there was no prior comment to overwrite, so undo is a
            # retracting edit rather than a restore.
            prior_state=None,
        )

    def _act_create_issue(self, action: Action) -> ActionReceipt:
        owner, repo = action.target["owner"], action.target["repo"]
        payload: dict[str, Any] = {"title": action.payload["title"]}
        if "body" in action.payload:
            payload["body"] = action.payload["body"]

        created = self._transport("POST", f"/repos/{owner}/{repo}/issues", json=payload)
        return ActionReceipt(
            action_id=f"github:create_issue:{owner}/{repo}#{created.get('number')}",
            action=action,
            result={
                "owner": owner,
                "repo": repo,
                "number": created.get("number"),
                "html_url": created.get("html_url"),
            },
            # Additive: closing-with-a-comment on undo, never a delete.
            prior_state=None,
        )

    def _act_set_status(self, action: Action) -> ActionReceipt:
        """Post a commit status. Reads whatever status already exists *for this
        context* before writing, because the whole point of `undo()` for a status is
        to supersede it with that prior state rather than pretend the write never
        happened."""
        owner, repo, sha = action.target["owner"], action.target["repo"], action.target["sha"]
        context = action.payload.get("context", _DEFAULT_STATUS_CONTEXT)
        prior = self._current_status(owner, repo, sha, context)

        body = {
            "state": action.payload["state"],
            "context": context,
            "description": action.payload.get("description", ""),
        }
        if action.payload.get("target_url"):
            body["target_url"] = action.payload["target_url"]

        created = self._transport("POST", f"/repos/{owner}/{repo}/statuses/{sha}", json=body)
        return ActionReceipt(
            action_id=f"github:set_status:{owner}/{repo}@{sha}:{context}:{created.get('id', '')}",
            action=action,
            result={
                "owner": owner,
                "repo": repo,
                "sha": sha,
                "context": context,
                "state": body["state"],
                "id": created.get("id"),
            },
            prior_state=prior,
        )

    def _current_status(
        self, owner: str, repo: str, sha: str, context: str
    ) -> dict[str, Any] | None:
        try:
            statuses = self._get(f"/repos/{owner}/{repo}/commits/{sha}/statuses", params={"per_page": 100})
        except GitHubNotFound:
            return None
        if not isinstance(statuses, list):
            return None
        for status in statuses:
            if status.get("context") == context:
                return {
                    "state": status.get("state"),
                    "description": status.get("description"),
                    "target_url": status.get("target_url"),
                    "context": context,
                }
        return None

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        op = receipt.action.operation
        if op == "add_label":
            self._undo_add_label(receipt)
        elif op == "comment":
            self._undo_comment(receipt)
        elif op == "create_issue":
            self._undo_create_issue(receipt)
        elif op == "set_status":
            self._undo_set_status(receipt)
        else:
            raise KeyError(f"github adapter has no undo handler for operation {op!r}")

        return ActionReceipt(
            action_id=receipt.action_id,
            action=receipt.action,
            result=receipt.result,
            prior_state=receipt.prior_state,
            executed_at=receipt.executed_at,
            undone_at=utcnow(),
        )

    def _undo_add_label(self, receipt: ActionReceipt) -> None:
        target = receipt.action.target
        owner, repo, number = target["owner"], target["repo"], target["number"]
        label = receipt.result["added"]
        prior_labels = set((receipt.prior_state or {}).get("labels", []))
        if label in prior_labels:
            # The label was already there before we acted — nothing to remove.
            return
        try:
            self._transport("DELETE", f"/repos/{owner}/{repo}/issues/{number}/labels/{label}")
        except GitHubNotFound:
            # Already gone (removed by hand, or undone twice). Undo is idempotent.
            pass

    def _undo_comment(self, receipt: ActionReceipt) -> None:
        owner, repo = receipt.result["owner"], receipt.result["repo"]
        comment_id = receipt.result["comment_id"]
        retraction = f"[retracted by Walnut · action {receipt.action_id}]"
        self._transport(
            "PATCH", f"/repos/{owner}/{repo}/issues/comments/{comment_id}", json={"body": retraction}
        )

    def _undo_create_issue(self, receipt: ActionReceipt) -> None:
        owner, repo, number = receipt.result["owner"], receipt.result["repo"], receipt.result["number"]
        retraction = f"[retracted by Walnut · action {receipt.action_id}]"
        self._transport(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": retraction}
        )
        self._transport("PATCH", f"/repos/{owner}/{repo}/issues/{number}", json={"state": "closed"})

    def _undo_set_status(self, receipt: ActionReceipt) -> None:
        owner, repo, sha = receipt.result["owner"], receipt.result["repo"], receipt.result["sha"]
        context = receipt.result["context"]
        prior = receipt.prior_state

        if prior is not None:
            body = {
                "state": prior["state"],
                "context": context,
                "description": prior.get("description") or "",
            }
            if prior.get("target_url"):
                body["target_url"] = prior["target_url"]
        else:
            # There was no status for this context before we posted one. GitHub has
            # no "delete a status" endpoint, so retraction means superseding with a
            # neutral state that says, on the record, that the prior check no longer
            # stands — never silently leaving the superseded state as the last word.
            body = {
                "state": "pending",
                "context": context,
                "description": f"[retracted by Walnut · action {receipt.action_id}]",
            }
        self._transport("POST", f"/repos/{owner}/{repo}/statuses/{sha}", json=body)


# ---------------------------------------------------------------------------
# module-private helpers
# ---------------------------------------------------------------------------


def _review_decision(reviews: list[dict[str, Any]]) -> str | None:
    """Collapse a review list to one decision, GitHub's own way: only the most
    recent review per person counts, and one outstanding CHANGES_REQUESTED beats
    any number of approvals."""
    latest_by_user: dict[str, str] = {}
    for review in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        state = review.get("state")
        if state not in ("APPROVED", "CHANGES_REQUESTED"):
            continue  # COMMENTED/DISMISSED/PENDING carry no standing of their own
        user = (review.get("user") or {}).get("login", "")
        latest_by_user[user] = state

    decisions = set(latest_by_user.values())
    if "CHANGES_REQUESTED" in decisions:
        return "CHANGES_REQUESTED"
    if "APPROVED" in decisions:
        return "APPROVED"
    return None


def _label_name(label: Any) -> str:
    """GitHub sometimes serialises labels as bare strings, usually as objects with a
    `name` field. Handle both rather than assume the richer shape everywhere."""
    if isinstance(label, dict):
        return str(label.get("name", ""))
    return str(label)


def _compose_text(title: str | None, body: str | None) -> str:
    title = title or ""
    body = body or ""
    return f"{title}\n\n{body}" if body else title


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
