"""Turning a piece of evidence into a target the live adapter can actually use.

Every adapter records, in `SourcePointer.locator`, the coordinates it needs to find a
record again. But the coordinates it needs to *write* to that record are not always
spelled the same way, and nothing connected the two — so the action planner emitted
`{"id": …}` for every app while the real adapters wanted:

    slack     channel, ts, thread_ts
    linear    issue_id, team_id
    github    owner, repo, number
    notion    page_id
    email     folder, message_id

Against fixture data this worked, because the fixture adapter reads `id`. Against a
real account every write would have raised `KeyError` at the API boundary — after the
earlier steps in the plan had already written to other systems. It is exactly the class
of defect that is invisible until the first live connection, which is the worst moment
to find it.

So the mapping lives here, in one table, next to a test that checks it against what the
adapters actually read. Adding a connector means adding one row.
"""

from __future__ import annotations

from typing import Any

from .brain import Fact

__all__ = ["REQUIRED_TARGET_KEYS", "missing_context", "target_for"]


# What each operation needs in `action.target`, per app. Kept declarative so the test
# suite can hold both sides of the contract to it: the planner must produce these, and
# the adapter must read only these.
REQUIRED_TARGET_KEYS: dict[str, dict[str, tuple[str, ...]]] = {
    "slack": {
        "post_reply": ("channel", "thread_ts"),
        "post_message": ("channel",),
        "add_reaction": ("channel", "ts"),
        "dm_user": ("user",),
    },
    "linear": {
        "create_issue": ("team_id",),
        "comment": ("issue_id",),
        "set_state": ("issue_id",),
        "assign": ("issue_id",),
        "add_label": ("issue_id",),
        "link_issues": ("issue_id", "related_issue_id"),
    },
    "github": {
        "comment": ("owner", "repo", "number"),
        "create_issue": ("owner", "repo"),
        "add_label": ("owner", "repo", "number"),
        "set_status": ("owner", "repo", "sha"),
    },
    "notion": {
        "set_property": ("page_id",),
        "append_block": ("page_id",),
        "create_page": ("database_id",),
    },
    "email": {
        "send_email": (),
        "save_draft": ("folder",),
        "flag": ("folder", "message_id"),
        "move_folder": ("folder", "message_id"),
    },
}


# The keys each live adapter actually stores. A locator carrying none of them came
# from a fixture or a custom source and is addressed by plain id.
_LIVE_LOCATOR_KEYS: dict[str, frozenset[str]] = {
    "slack": frozenset({"channel", "ts"}),
    "linear": frozenset({"identifier", "issue_id", "type"}),
    "github": frozenset({"owner", "repo", "number"}),
    "notion": frozenset({"page_id"}),
    "email": frozenset({"folder", "message_id"}),
}


def _is_generic(app: str, locator: dict[str, Any]) -> bool:
    """Is this an id-addressed locator rather than a live one?"""
    if not locator:
        return False
    return "id" in locator and not (_LIVE_LOCATOR_KEYS.get(app, frozenset()) & set(locator))


def _from_locator(app: str, locator: dict[str, Any]) -> dict[str, Any]:
    """Translate one adapter's read coordinates into its write coordinates."""
    if app == "slack":
        ts = locator.get("ts")
        return {
            "channel": locator.get("channel"),
            "ts": ts,
            # A reply threads under the message we are citing, so the parent is the
            # evidence's own timestamp.
            "thread_ts": locator.get("thread_ts") or ts,
        }

    if app == "linear":
        # A comment's locator already carries its parent issue; an issue's own id is
        # under "id". Both have to arrive as `issue_id`.
        return {
            "issue_id": locator.get("issue_id") or locator.get("id"),
            "identifier": locator.get("identifier"),
        }

    if app == "github":
        return {
            "owner": locator.get("owner"),
            "repo": locator.get("repo"),
            "number": locator.get("number"),
        }

    if app == "notion":
        return {"page_id": locator.get("page_id") or locator.get("id")}

    if app == "email":
        return {
            "folder": locator.get("folder") or "INBOX",
            "message_id": locator.get("message_id") or locator.get("id"),
        }

    # Custom sources (SQL, REST, plugins) address records by id, which is also what
    # the fixture adapter uses — so the fallback is the common case, not a guess.
    return dict(locator)


def target_for(
    app: str,
    operation: str,
    evidence: list[Fact],
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build the target for one action, or `None` if it cannot be aimed.

    Returning `None` rather than a partial target is deliberate. An action pointed at a
    record that does not exist fails at the API boundary with an error indistinguishable
    from a connector bug, and by then the other steps in a multi-app plan have already
    written. Better to drop the step and say why.

    `context` supplies the things no locator can carry — a Linear team id, a GitHub
    repo — which come from the connection the operator configured.
    """
    context = context or {}
    same_app = [f for f in evidence if f.app == app]

    target: dict[str, Any] = {}
    if same_app:
        target = _from_locator(app, same_app[0].pointer.locator)
        # A direct message is addressed to a person, and the person is on the evidence
        # itself rather than in its locator.
        if app == "slack" and operation == "dm_user" and same_app[0].author:
            target["user"] = same_app[0].author

    # Creating a record has no existing record to aim at; it needs container context.
    if app == "linear" and operation == "create_issue":
        team = context.get("team_id") or context.get("linear_team_id")
        return {"team_id": team} if team else None
    if app == "notion" and operation == "create_page":
        database = context.get("database_id") or context.get("notion_database_id")
        return {"database_id": database} if database else None
    if app == "github" and operation == "create_issue":
        owner_repo = target or _repo_from_context(context)
        return owner_repo if owner_repo.get("owner") else None
    if app == "email" and operation in {"send_email", "save_draft"}:
        return {"folder": context.get("drafts_folder", "Drafts")}

    if not same_app:
        return None

    raw = same_app[0].pointer.locator
    required = REQUIRED_TARGET_KEYS.get(app, {}).get(operation)

    if required and any(target.get(k) in (None, "") for k in required):
        # Fall back to the raw locator ONLY when it is a generic, id-addressed one —
        # a fixture or a custom source. Refusing those silently deleted two steps from
        # the demo plan the first time this ran, which is how a correct-looking change
        # removes half a feature.
        #
        # A LIVE locator that is merely missing a key is a different situation and must
        # still refuse: `slack.dm_user` needs a user id, which no message locator
        # carries, and handing the adapter `{channel, ts}` instead would raise KeyError
        # at the API boundary — the exact failure this module exists to prevent.
        if _is_generic(app, raw):
            return dict(raw)
        return None

    return {k: v for k, v in target.items() if v is not None}


def _repo_from_context(context: dict[str, Any]) -> dict[str, Any]:
    repo = context.get("repo") or context.get("github_repo") or ""
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return {"owner": owner, "repo": name}
    return {}


def missing_context(app: str, operation: str, context: dict[str, Any]) -> str | None:
    """What a human would need to supply to make this action aimable.

    Used to explain a dropped step instead of silently omitting it.
    """
    if app == "linear" and operation == "create_issue" and not (
        context.get("team_id") or context.get("linear_team_id")
    ):
        return "Linear needs a team id to create an issue (set LINEAR_TEAM_ID)."
    if app == "notion" and operation == "create_page" and not (
        context.get("database_id") or context.get("notion_database_id")
    ):
        return "Notion needs a database id to create a page (set NOTION_DATABASE_ID)."
    if app == "github" and operation == "create_issue" and not _repo_from_context(context):
        return "GitHub needs owner/repo to create an issue (set GITHUB_REPO)."
    return None
