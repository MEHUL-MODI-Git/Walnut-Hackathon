"""Linear adapter — issues and comments as cited evidence, six governed writes.

Linear's API is a single GraphQL endpoint that always answers HTTP 200; the only
reliable failure signal is an `"errors"` array in the body, so every call site here
goes through `_gql`, which is the one place that array is inspected. Nothing in this
module trusts the HTTP status code.

The `transport` seam exists because we have no Linear credentials in this
repository. A fake transport — a plain callable of `(query, variables) -> dict` — is
the only way to exercise request shaping and response parsing without a network, so
the adapter is built around that seam from the constructor down rather than having
it bolted on for tests.

Two identity fields matter for every issue: a uuid `id` (what every mutation and
most queries need) and a human key like `"ENG-412"` (what a person recognises and
what the demo shows). Both are kept — on `Evidence.id`, in `SourcePointer.locator`,
and in the mutation results returned in `ActionReceipt`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
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

__all__ = ["LinearAdapter", "LinearAPIError"]

_ENDPOINT = "https://api.linear.app/graphql"

Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


class LinearAPIError(RuntimeError):
    """A GraphQL request to Linear answered with a populated "errors" array.

    Linear returns HTTP 200 for both success and application-level failure, so a
    caller that only checks the status code will treat a malformed mutation or a
    permissions refusal as a success. Every request in this module is routed
    through `_gql`, which raises this instead.
    """


def _default_transport(api_key: str | None) -> Transport:
    """The real transport: one POST per call, auth header exactly as Linear wants it.

    Linear's personal API keys go in `Authorization` bare — NOT `Bearer <key>`. That
    is a common mistake carried over from OAuth-style APIs and it silently fails
    Linear auth rather than erroring clearly, so it is worth stating explicitly here
    rather than leaving it to be rediscovered.
    """

    def _call(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            _ENDPOINT,
            json={"query": query, "variables": variables},
            headers={
                "Authorization": api_key or "",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()

    return _call


def _parse_dt(value: str | None) -> datetime | None:
    """Linear timestamps are ISO-8601 with a trailing `Z`; `fromisoformat` wants `+00:00`."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# GraphQL documents
#
# Each document's operation name (WalnutXxx) is unique and is how the test fixture
# dispatches without parsing GraphQL. Field selections are kept to what the adapter
# actually reads — nothing here is fetched for completeness.
# ---------------------------------------------------------------------------

_ISSUE_FIELDS = """
  id
  identifier
  title
  description
  url
  priority
  createdAt
  updatedAt
  state { id name }
  assignee { id name email }
  team { id key name }
  labels { nodes { id name } }
"""

_COMMENT_FIELDS = """
  id
  body
  url
  createdAt
  updatedAt
  user { id name email }
"""

_PROBE_QUERY = """
query WalnutProbe {
  viewer { id name email }
  teams { nodes { id key name } }
}
"""

_ISSUES_QUERY = f"""
query WalnutIssues($filter: IssueFilter, $first: Int) {{
  issues(filter: $filter, first: $first) {{
    nodes {{
      {_ISSUE_FIELDS}
      comments {{ nodes {{ {_COMMENT_FIELDS} }} }}
    }}
  }}
}}
"""

_ISSUE_BY_ID_QUERY = f"""
query WalnutIssueById($id: String!) {{
  issue(id: $id) {{
    {_ISSUE_FIELDS}
    comments {{ nodes {{ {_COMMENT_FIELDS} }} }}
  }}
}}
"""

_COMMENT_BY_ID_QUERY = f"""
query WalnutCommentById($id: String!) {{
  comment(id: $id) {{
    {_COMMENT_FIELDS}
    issue {{ id identifier url }}
  }}
}}
"""

_ISSUE_LABELS_QUERY = """
query WalnutIssueLabels($id: String!) {
  issue(id: $id) { id labels { nodes { id name } } }
}
"""

_ISSUE_STATE_QUERY = """
query WalnutIssueState($id: String!) {
  issue(id: $id) { id state { id name } }
}
"""

_ISSUE_ASSIGNEE_QUERY = """
query WalnutIssueAssignee($id: String!) {
  issue(id: $id) { id assignee { id name email } }
}
"""

_ISSUE_UPDATE_MUTATION = """
mutation WalnutIssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue {
      id identifier url
      state { id name }
      assignee { id name email }
      labels { nodes { id name } }
    }
  }
}
"""

_ISSUE_CREATE_MUTATION = """
mutation WalnutIssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier url title }
  }
}
"""

_COMMENT_CREATE_MUTATION = """
mutation WalnutCommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success
    comment { id body url issue { id identifier } }
  }
}
"""

_ISSUE_RELATION_CREATE_MUTATION = """
mutation WalnutIssueRelationCreate($input: IssueRelationCreateInput!) {
  issueRelationCreate(input: $input) {
    success
    issueRelation {
      id type
      issue { id identifier }
      relatedIssue { id identifier }
    }
  }
}
"""

_ISSUE_RELATION_DELETE_MUTATION = """
mutation WalnutIssueRelationDelete($id: String!) {
  issueRelationDelete(id: $id) { success }
}
"""

_ISSUE_ARCHIVE_MUTATION = """
mutation WalnutIssueArchive($id: String!) {
  issueArchive(id: $id) { success }
}
"""

_COMMENT_UPDATE_MUTATION = """
mutation WalnutCommentUpdate($id: String!, $input: CommentUpdateInput!) {
  commentUpdate(id: $id, input: $input) {
    success
    comment { id body }
  }
}
"""


class LinearAdapter:
    """Linear issues and their comments as cited evidence; six governed writes.

    `probe()` and `fetch()` never write. `act()` always reads current state before
    writing anything that overwrites a prior value (label set, state, assignee) so
    `undo()` has something to restore. Created issues are archived rather than
    deleted on undo, and retracted comments are edited to say so rather than
    removed — the audit trail is the point of this adapter existing at all.
    """

    name = "linear"

    def __init__(self, api_key: str | None = None, transport: Transport | None = None) -> None:
        self._api_key = api_key
        self._transport = transport or _default_transport(api_key)

    # -- GraphQL plumbing -----------------------------------------------------

    def _gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one GraphQL request and raise if it reports an error.

        Linear answers HTTP 200 for both success and application failure; the
        `"errors"` array is the only trustworthy signal, so it is checked here,
        once, rather than at every call site.
        """
        raw = self._transport(query, variables or {})
        errors = raw.get("errors")
        if errors:
            messages = "; ".join(e.get("message", str(e)) for e in errors)
            raise LinearAPIError(messages)
        return raw.get("data") or {}

    def _new_receipt(
        self, action: Action, result: dict[str, Any], prior_state: dict[str, Any] | None
    ) -> ActionReceipt:
        return ActionReceipt(
            action_id=f"linear-act-{uuid.uuid4().hex[:12]}",
            action=action,
            result=result,
            prior_state=prior_state,
        )

    def _issue_evidence(self, issue: dict[str, Any]) -> Evidence:
        """Build the cited `Evidence` for one issue.

        The content hash covers only the fields that constitute what the issue
        currently *asserts* — title, description, state, assignee — so it moves
        exactly when one of those changes and not, for example, when Linear bumps
        an internal cache timestamp we never read.
        """
        state = issue.get("state") or {}
        assignee = issue.get("assignee") or {}
        stable = {
            "id": issue["id"],
            "title": issue.get("title"),
            "description": issue.get("description"),
            "state": state.get("id"),
            "assignee": assignee.get("id"),
        }
        label_names = tuple(
            label.get("name", "") for label in (issue.get("labels") or {}).get("nodes") or []
        )
        labels = label_names + ((state["name"],) if state.get("name") else ())
        title = issue.get("title") or ""
        description = issue.get("description") or ""
        return Evidence(
            id=issue.get("identifier") or issue["id"],
            pointer=SourcePointer(
                app=self.name,
                resource_uri=issue.get("url") or f"https://linear.app/issue/{issue['id']}",
                locator={
                    "type": "issue",
                    "id": issue["id"],
                    "identifier": issue.get("identifier"),
                },
                content_hash=content_hash(stable),
            ),
            text=f"{title}\n\n{description}".strip(),
            author=assignee.get("name"),
            occurred_at=_parse_dt(issue.get("updatedAt") or issue.get("createdAt")),
            labels=labels,
            raw=issue,
        )

    def _comment_evidence(self, issue: dict[str, Any], comment: dict[str, Any]) -> Evidence:
        """Build the cited `Evidence` for one comment on an issue.

        Comments do not always carry their own permalink from every Linear API
        version, so we fall back to an anchor on the issue URL when Linear omits
        `Comment.url` — a comment without *some* resolvable link would violate the
        no-evidence-without-a-pointer rule.
        """
        user = comment.get("user") or {}
        stable = {
            "id": comment["id"],
            "issue_id": issue.get("id"),
            "body": comment.get("body"),
            "user": user.get("id"),
        }
        identifier = issue.get("identifier") or issue.get("id") or ""
        issue_url = issue.get("url") or ""
        resource_uri = comment.get("url") or f"{issue_url}#comment-{comment['id']}"
        return Evidence(
            id=f"{identifier}#comment-{comment['id']}",
            pointer=SourcePointer(
                app=self.name,
                resource_uri=resource_uri,
                locator={
                    "type": "comment",
                    "id": comment["id"],
                    "issue_id": issue.get("id"),
                    "identifier": identifier,
                },
                content_hash=content_hash(stable),
            ),
            text=comment.get("body") or "",
            author=user.get("name"),
            occurred_at=_parse_dt(comment.get("updatedAt") or comment.get("createdAt")),
            labels=("comment",),
            raw=comment,
        )

    def _issue_filter(self, scope: str) -> dict[str, Any]:
        """`scope` is a team key ("ENG") or a project name — the caller does not say
        which, so we match either. A team key and a project name are never the same
        string in practice, so the ambiguity costs nothing.
        """
        return {
            "or": [
                {"team": {"key": {"eq": scope}}},
                {"project": {"name": {"eq": scope}}},
            ]
        }

    # -- read -------------------------------------------------------------------

    def probe(self) -> SourceProfile:
        data = self._gql(_PROBE_QUERY)
        teams = (data.get("teams") or {}).get("nodes") or []
        viewer = data.get("viewer") or {}
        scopes = tuple(t["key"] for t in teams if t.get("key"))
        return SourceProfile(
            app=self.name,
            display_name="Linear",
            scopes=scopes,
            record_count_estimate=None,
            detail={
                "viewer": viewer.get("email") or viewer.get("name") or "",
                "teams": [{"key": t.get("key"), "name": t.get("name")} for t in teams],
            },
        )

    def fetch(self, scope: str | None = None, limit: int = 100) -> list[Evidence]:
        """Pull issues, and their comments, as evidence.

        `limit` caps the total number of `Evidence` records returned (issues plus
        comments together), not the number of issues requested from Linear — a
        popular issue's comment thread should not be able to silently starve out
        every other issue in the scope.
        """
        filter_ = self._issue_filter(scope) if scope else None
        data = self._gql(_ISSUES_QUERY, {"filter": filter_, "first": max(limit, 1)})
        nodes = (data.get("issues") or {}).get("nodes") or []

        evidence: list[Evidence] = []
        for issue in nodes:
            evidence.append(self._issue_evidence(issue))
            if len(evidence) >= limit:
                return evidence[:limit]
            for comment in (issue.get("comments") or {}).get("nodes") or []:
                evidence.append(self._comment_evidence(issue, comment))
                if len(evidence) >= limit:
                    return evidence[:limit]
        return evidence[:limit]

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        """Re-fetch one issue or comment live.

        Linear can answer a missing or malformed id either with a null result or
        with a GraphQL error, depending on why the id didn't resolve. Both mean
        the same thing to a caller trying to re-verify a citation — the record
        cannot be confirmed — so both come back as `None` rather than one being a
        crash and the other a quiet miss.
        """
        record_id = pointer.locator.get("id")
        if not record_id:
            return None
        kind = pointer.locator.get("type", "issue")
        try:
            if kind == "comment":
                data = self._gql(_COMMENT_BY_ID_QUERY, {"id": record_id})
                comment = data.get("comment")
                if not comment:
                    return None
                issue = comment.get("issue") or {}
                return self._comment_evidence(issue, comment)

            data = self._gql(_ISSUE_BY_ID_QUERY, {"id": record_id})
            issue = data.get("issue")
            if not issue:
                return None
            return self._issue_evidence(issue)
        except LinearAPIError:
            return None

    # -- write --------------------------------------------------------------

    def capabilities(self) -> ActionCapabilities:
        return ActionCapabilities(
            app=self.name,
            operations={
                "add_label": ActionTier.TRIVIAL,
                "link_issues": ActionTier.TRIVIAL,
                "create_issue": ActionTier.INTERNAL,
                "comment": ActionTier.INTERNAL,
                "set_state": ActionTier.INTERNAL,
                "assign": ActionTier.INTERNAL,
            },
        )

    def act(self, action: Action) -> ActionReceipt:
        handlers: dict[str, Callable[[Action], ActionReceipt]] = {
            "add_label": self._act_add_label,
            "link_issues": self._act_link_issues,
            "create_issue": self._act_create_issue,
            "comment": self._act_comment,
            "set_state": self._act_set_state,
            "assign": self._act_assign,
        }
        handler = handlers.get(action.operation)
        if handler is None:
            raise KeyError(f"linear adapter has no handler for operation {action.operation!r}")
        return handler(action)

    def _act_add_label(self, action: Action) -> ActionReceipt:
        """Add a label. Reads the current label set first so undo can restore it
        exactly — including any labels that were already there before this call."""
        issue_id = action.target["issue_id"]
        label_id = action.payload["label_id"]
        current = self._gql(_ISSUE_LABELS_QUERY, {"id": issue_id})
        issue = current.get("issue") or {}
        prior_label_ids = [l["id"] for l in (issue.get("labels") or {}).get("nodes") or []]
        new_label_ids = (
            prior_label_ids if label_id in prior_label_ids else [*prior_label_ids, label_id]
        )
        data = self._gql(_ISSUE_UPDATE_MUTATION, {"id": issue_id, "input": {"labelIds": new_label_ids}})
        payload = data.get("issueUpdate") or {}
        return self._new_receipt(
            action,
            result={"issue": payload.get("issue"), "success": payload.get("success")},
            prior_state={"label_ids": prior_label_ids},
        )

    def _act_link_issues(self, action: Action) -> ActionReceipt:
        """Relate two issues. Purely additive — there is nothing to overwrite, so
        `prior_state` is `None` and undo deletes the relation rather than restoring
        anything."""
        issue_id = action.target["issue_id"]
        related_issue_id = action.target["related_issue_id"]
        relation_type = action.payload.get("type", "related")
        data = self._gql(
            _ISSUE_RELATION_CREATE_MUTATION,
            {
                "input": {
                    "issueId": issue_id,
                    "relatedIssueId": related_issue_id,
                    "type": relation_type,
                }
            },
        )
        payload = data.get("issueRelationCreate") or {}
        return self._new_receipt(
            action,
            result={"relation": payload.get("issueRelation"), "success": payload.get("success")},
            prior_state=None,
        )

    def _act_create_issue(self, action: Action) -> ActionReceipt:
        """Create an issue. Purely additive; undo archives it rather than deleting
        it, per the retract-not-erase rule."""
        team_id = action.target["team_id"]
        input_: dict[str, Any] = {"teamId": team_id, "title": action.payload["title"]}
        if "description" in action.payload:
            input_["description"] = action.payload["description"]
        data = self._gql(_ISSUE_CREATE_MUTATION, {"input": input_})
        payload = data.get("issueCreate") or {}
        return self._new_receipt(
            action,
            result={"issue": payload.get("issue"), "success": payload.get("success")},
            prior_state=None,
        )

    def _act_comment(self, action: Action) -> ActionReceipt:
        """Post a comment. Purely additive; undo redacts the body rather than
        deleting the comment, so the fact that Walnut said something stays visible."""
        issue_id = action.target["issue_id"]
        body = action.payload["body"]
        data = self._gql(_COMMENT_CREATE_MUTATION, {"input": {"issueId": issue_id, "body": body}})
        payload = data.get("commentCreate") or {}
        return self._new_receipt(
            action,
            result={"comment": payload.get("comment"), "success": payload.get("success")},
            prior_state=None,
        )

    def _act_set_state(self, action: Action) -> ActionReceipt:
        """Move an issue to a new workflow state. Reads the current state first —
        without this, undo would have nothing to restore."""
        issue_id = action.target["issue_id"]
        state_id = action.payload["state_id"]
        current = self._gql(_ISSUE_STATE_QUERY, {"id": issue_id})
        prior = (current.get("issue") or {}).get("state") or {}
        data = self._gql(_ISSUE_UPDATE_MUTATION, {"id": issue_id, "input": {"stateId": state_id}})
        payload = data.get("issueUpdate") or {}
        return self._new_receipt(
            action,
            result={"issue": payload.get("issue"), "success": payload.get("success")},
            prior_state={"state_id": prior.get("id"), "state_name": prior.get("name")},
        )

    def _act_assign(self, action: Action) -> ActionReceipt:
        """Reassign an issue. `payload["assignee_id"]` of `None` unassigns. Reads
        the current assignee first so undo can restore the previous owner, or clear
        the field again if it was unassigned before."""
        issue_id = action.target["issue_id"]
        assignee_id = action.payload.get("assignee_id")
        current = self._gql(_ISSUE_ASSIGNEE_QUERY, {"id": issue_id})
        prior = (current.get("issue") or {}).get("assignee") or {}
        data = self._gql(_ISSUE_UPDATE_MUTATION, {"id": issue_id, "input": {"assigneeId": assignee_id}})
        payload = data.get("issueUpdate") or {}
        return self._new_receipt(
            action,
            result={"issue": payload.get("issue"), "success": payload.get("success")},
            prior_state={"assignee_id": prior.get("id"), "assignee_name": prior.get("name")},
        )

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        handlers: dict[str, Callable[[ActionReceipt], dict[str, Any]]] = {
            "add_label": self._undo_add_label,
            "link_issues": self._undo_link_issues,
            "create_issue": self._undo_create_issue,
            "comment": self._undo_comment,
            "set_state": self._undo_set_state,
            "assign": self._undo_assign,
        }
        handler = handlers.get(receipt.action.operation)
        if handler is None:
            raise KeyError(
                f"linear adapter has no undo handler for operation {receipt.action.operation!r}"
            )
        result = handler(receipt)
        return ActionReceipt(
            action_id=receipt.action_id,
            action=receipt.action,
            result=result,
            prior_state=receipt.prior_state,
            executed_at=receipt.executed_at,
            undone_at=utcnow(),
        )

    def _undo_add_label(self, receipt: ActionReceipt) -> dict[str, Any]:
        issue_id = receipt.action.target["issue_id"]
        prior_ids = (receipt.prior_state or {}).get("label_ids", [])
        data = self._gql(_ISSUE_UPDATE_MUTATION, {"id": issue_id, "input": {"labelIds": prior_ids}})
        return {"issue": (data.get("issueUpdate") or {}).get("issue")}

    def _undo_link_issues(self, receipt: ActionReceipt) -> dict[str, Any]:
        relation = (receipt.result or {}).get("relation") or {}
        relation_id = relation.get("id")
        if relation_id is None:
            return {"skipped": "no relation id recorded on the receipt to delete"}
        data = self._gql(_ISSUE_RELATION_DELETE_MUTATION, {"id": relation_id})
        return {"deleted": (data.get("issueRelationDelete") or {}).get("success")}

    def _undo_create_issue(self, receipt: ActionReceipt) -> dict[str, Any]:
        """Archive, never delete — the issue Walnut created stays in the audit
        trail, just no longer live."""
        issue = (receipt.result or {}).get("issue") or {}
        issue_id = issue.get("id")
        if issue_id is None:
            return {"skipped": "no issue id recorded on the receipt to archive"}
        data = self._gql(_ISSUE_ARCHIVE_MUTATION, {"id": issue_id})
        return {"archived": (data.get("issueArchive") or {}).get("success")}

    def _undo_comment(self, receipt: ActionReceipt) -> dict[str, Any]:
        """Edit the comment to say it was retracted, rather than deleting it — the
        fact that Walnut posted something, and then withdrew it, stays visible."""
        comment = (receipt.result or {}).get("comment") or {}
        comment_id = comment.get("id")
        if comment_id is None:
            return {"skipped": "no comment id recorded on the receipt to redact"}
        redacted_body = f"[retracted by Walnut - action {receipt.action_id}]"
        data = self._gql(
            _COMMENT_UPDATE_MUTATION, {"id": comment_id, "input": {"body": redacted_body}}
        )
        return {"comment": (data.get("commentUpdate") or {}).get("comment")}

    def _undo_set_state(self, receipt: ActionReceipt) -> dict[str, Any]:
        issue_id = receipt.action.target["issue_id"]
        prior_state_id = (receipt.prior_state or {}).get("state_id")
        data = self._gql(
            _ISSUE_UPDATE_MUTATION, {"id": issue_id, "input": {"stateId": prior_state_id}}
        )
        return {"issue": (data.get("issueUpdate") or {}).get("issue")}

    def _undo_assign(self, receipt: ActionReceipt) -> dict[str, Any]:
        issue_id = receipt.action.target["issue_id"]
        prior_assignee_id = (receipt.prior_state or {}).get("assignee_id")
        data = self._gql(
            _ISSUE_UPDATE_MUTATION, {"id": issue_id, "input": {"assigneeId": prior_assignee_id}}
        )
        return {"issue": (data.get("issueUpdate") or {}).get("issue")}
