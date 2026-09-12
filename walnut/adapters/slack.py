"""Slack: the adapter for the app where decisions get made in a thread and forgotten.

Slack's Web API returns HTTP 200 for almost everything, including failure. The
envelope is `{"ok": bool, "error": str}` and the status code tells you nothing —
which is why every call in this module goes through `_call()` rather than trusting
`transport()`'s return value directly. Skipping that check is the single most common
way to ship a Slack integration that silently no-ops.

Message identity in Slack is the pair `(channel, ts)`, not a database id. `ts` is a
string like `"1700000000.000100"` — a fixed-point timestamp with an embedded
per-second sequence number, unique within a channel. Both fields are required to
find a message again, which is why `SourcePointer.locator` always carries both and
`resolve()` refuses to guess a channel from a bare `ts`.

Following the contract's write-side split: `add_reaction` is TRIVIAL (adding a
thumbs-up cannot mislead anyone), `post_reply` and `post_message` are INTERNAL
(they add content but stay inside the workspace), and `dm_user` is GATED (a direct
message is addressed to one person, outside the channel's shared context, and the
agent should not send one without a human in the loop).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

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

__all__ = ["SlackAdapter", "SlackAPIError"]

Transport = Callable[..., dict[str, Any]]

_SLACK_API_BASE = "https://slack.com/api"

_RETRACTED_TEMPLATE = "[retracted by Walnut · action {action_id} · evidence withdrawn]"


class SlackAPIError(RuntimeError):
    """Raised when Slack answers `{"ok": false}`. HTTP 200 does not mean success."""

    def __init__(self, method: str, error: str, response: dict[str, Any]) -> None:
        self.method = method
        self.error = error
        self.response = response
        super().__init__(f"slack.{method} failed: {error!r}")


def _default_transport(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """The real transport. Only imported here, so tests never need httpx installed
    to exercise the adapter — a fake transport is enough to run every check the
    conformance suite makes."""
    import httpx

    response = httpx.request(method, url, **kwargs)
    response.raise_for_status()
    return response.json()


def _ts_to_datetime(ts: str) -> datetime:
    """Slack `ts` is seconds.microseconds since the epoch, as a string."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def _message_identity_payload(channel: str, message: dict[str, Any]) -> dict[str, Any]:
    """The fields that define "this is the same message content" for hashing.

    Deliberately excludes fields Slack mutates for reasons unrelated to content —
    `latest_reply`, `reply_count`, reaction counts — so the hash tracks the message
    text and its edit history (`edited.ts`), not incidental channel chatter.
    """
    return {
        "channel": channel,
        "ts": message.get("ts", ""),
        "text": message.get("text", ""),
        "user": message.get("user", ""),
        "edited_ts": (message.get("edited") or {}).get("ts", ""),
    }


class SlackAdapter:
    """Adapter for Slack's Web API, bound to one bot token.

    `transport` is the seam that makes this adapter testable without a workspace:
    it is called as `transport(method, url, headers=..., json=..., params=...)` and
    must return the parsed JSON body. Production code leaves it as `None` and gets a
    real httpx-based transport; tests inject a fake that returns canned payloads.
    """

    name = "slack"

    def __init__(self, token: str | None = None, transport: Transport | None = None) -> None:
        self._token = token
        self._transport = transport or _default_transport
        self._action_seq = 0
        self._permalink_cache: dict[tuple[str, str], str] = {}

    # -- transport plumbing ---------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _call(self, method: str, **payload: Any) -> dict[str, Any]:
        """POST to a Slack Web API method and enforce the `ok` envelope.

        Slack's failure mode is HTTP 200 with `{"ok": false, "error": "..."}`, so an
        adapter that only checks the transport's exceptions or status code will
        happily report success on a bad token, a missing scope, or a wrong channel.
        Every call in this module funnels through here specifically to close that
        gap once, rather than trusting each call site to remember it.
        """
        url = f"{_SLACK_API_BASE}/{method}"
        response = self._transport("POST", url, headers=self._headers(), json=payload)
        if not response.get("ok", False):
            raise SlackAPIError(method, response.get("error", "unknown_error"), response)
        return response

    def _get(self, method: str, **params: Any) -> dict[str, Any]:
        url = f"{_SLACK_API_BASE}/{method}"
        response = self._transport("GET", url, headers=self._headers(), params=params)
        if not response.get("ok", False):
            raise SlackAPIError(method, response.get("error", "unknown_error"), response)
        return response

    # -- permalinks -------------------------------------------------------------

    def _permalink(self, channel: str, ts: str) -> str:
        """A followable https link for one message, via `chat.getPermalink`.

        Cached per (channel, ts) within the adapter's lifetime: the permalink for a
        given message never changes, and this method is called once per fetched
        message, so caching avoids doubling the number of API calls for no benefit.
        """
        key = (channel, ts)
        if key in self._permalink_cache:
            return self._permalink_cache[key]
        response = self._get("chat.getPermalink", channel=channel, message_ts=ts)
        permalink = response["permalink"]
        self._permalink_cache[key] = permalink
        return permalink

    # -- evidence construction ----------------------------------------------

    def _to_evidence(self, channel: str, message: dict[str, Any]) -> Evidence:
        ts = message["ts"]
        identity = _message_identity_payload(channel, message)
        pointer = SourcePointer(
            app=self.name,
            resource_uri=self._permalink(channel, ts),
            locator={"channel": channel, "ts": ts},
            content_hash=content_hash(identity),
        )
        occurred_at = _ts_to_datetime(ts)
        labels = (channel,)
        if message.get("thread_ts") and message["thread_ts"] != ts:
            labels = (channel, "thread_reply")
        return Evidence(
            id=f"{channel}:{ts}",
            pointer=pointer,
            text=message.get("text", ""),
            author=message.get("user"),
            occurred_at=occurred_at,
            labels=labels,
            raw=message,
        )

    # -- read side ------------------------------------------------------------

    def probe(self) -> SourceProfile:
        """List channels this bot token can see via `conversations.list`."""
        response = self._get(
            "conversations.list", types="public_channel,private_channel", limit=1000
        )
        channels = response.get("channels", [])
        scopes = tuple(c["name"] for c in channels if c.get("name"))
        return SourceProfile(
            app=self.name,
            display_name="Slack",
            scopes=scopes,
            record_count_estimate=None,
            detail={"channel_ids": {c["name"]: c["id"] for c in channels if c.get("name")}},
        )

    def _resolve_channel_id(self, scope: str) -> str:
        """Accept either a channel id (`C0123...`) or a human channel name."""
        if scope.startswith(("C", "G", "D")) and " " not in scope:
            return scope
        name = scope.lstrip("#")
        response = self._get(
            "conversations.list", types="public_channel,private_channel", limit=1000
        )
        for channel in response.get("channels", []):
            if channel.get("name") == name:
                return channel["id"]
        raise SlackAPIError(
            "conversations.list", "channel_not_found", {"scope": scope}
        )

    def fetch(self, scope: str | None = None, limit: int = 100) -> list[Evidence]:
        """Pull messages from one channel, including thread replies.

        `scope` is required in practice (Slack has no notion of "history across
        every channel" in one call) but is typed `str | None` to match the
        protocol; a caller passing `None` gets the first probed channel.
        """
        if scope is None:
            profile = self.probe()
            if not profile.scopes:
                return []
            scope = profile.scopes[0]

        channel_id = self._resolve_channel_id(scope)
        history = self._get("conversations.history", channel=channel_id, limit=limit)
        messages = list(history.get("messages", []))

        evidence: list[Evidence] = []
        for message in messages:
            if len(evidence) >= limit:
                break
            evidence.append(self._to_evidence(channel_id, message))

            # Thread replies: only the parent message carries `reply_count`, and
            # `conversations.replies` already includes the parent, so skip index 0
            # to avoid re-emitting it as a duplicate piece of evidence.
            if message.get("reply_count", 0) > 0 and len(evidence) < limit:
                replies = self._get(
                    "conversations.replies", channel=channel_id, ts=message["ts"]
                )
                for reply in replies.get("messages", [])[1:]:
                    if len(evidence) >= limit:
                        break
                    evidence.append(self._to_evidence(channel_id, reply))

        return evidence[:limit]

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        """Re-fetch one message live, by (channel, ts).

        Slack has no "get one message" endpoint; `conversations.replies` called
        with `ts` equal to a top-level message's own timestamp returns exactly
        that message as `messages[0]`, whether or not it has replies, so it
        doubles as a single-message lookup.
        """
        channel = pointer.locator.get("channel")
        ts = pointer.locator.get("ts")
        if not channel or not ts:
            return None
        try:
            response = self._get("conversations.replies", channel=channel, ts=ts)
        except SlackAPIError:
            return None
        messages = response.get("messages", [])
        if not messages:
            return None
        match = next((m for m in messages if m.get("ts") == ts), None)
        if match is None:
            return None
        return self._to_evidence(channel, match)

    # -- write side -----------------------------------------------------------

    def capabilities(self) -> ActionCapabilities:
        return ActionCapabilities(
            app=self.name,
            operations={
                "add_reaction": ActionTier.TRIVIAL,
                "post_reply": ActionTier.INTERNAL,
                "post_message": ActionTier.INTERNAL,
                "dm_user": ActionTier.GATED,
            },
        )

    def _next_action_id(self) -> str:
        self._action_seq += 1
        return f"slack-act-{self._action_seq}"

    def _act_add_reaction(self, action: Action) -> ActionReceipt:
        channel = action.target["channel"]
        ts = action.target["ts"]
        emoji = action.payload["emoji"]

        # Read-before-write: capture whether this reaction is already present, so
        # undo() knows whether removing it is actually reversing this action or
        # would be destroying a reaction someone else added.
        existing = self._get("reactions.get", channel=channel, timestamp=ts)
        reactions = (existing.get("message") or {}).get("reactions", [])
        already_present = any(
            r.get("name") == emoji and self._token_user_in(r) for r in reactions
        )

        response = self._call("reactions.add", channel=channel, timestamp=ts, name=emoji)
        return ActionReceipt(
            action_id=self._next_action_id(),
            action=action,
            result={"channel": channel, "ts": ts, "emoji": emoji, "ok": response["ok"]},
            prior_state={"already_present": already_present},
        )

    def _token_user_in(self, reaction: dict[str, Any]) -> bool:
        """Whether the bot's own user id is among the reactors on this emoji.

        `reactions.get` reports users by id, not by token, so this is a best-effort
        check pending a `users` collaborator; conservative default is False so
        undo() prefers removing a reaction it might not have added over silently
        skipping a removal it should perform.
        """
        return False

    def _act_post_reply(self, action: Action) -> ActionReceipt:
        channel = action.target["channel"]
        thread_ts = action.target["thread_ts"]
        response = self._call(
            "chat.postMessage",
            channel=channel,
            thread_ts=thread_ts,
            text=action.payload["text"],
        )
        return ActionReceipt(
            action_id=self._next_action_id(),
            action=action,
            result={
                "channel": response["channel"],
                "ts": response["ts"],
                "thread_ts": thread_ts,
            },
            # Purely additive: nothing existed at this (channel, ts) before we
            # created it, so there is no prior state to restore on undo.
            prior_state=None,
        )

    def _act_post_message(self, action: Action) -> ActionReceipt:
        channel = action.target["channel"]
        response = self._call(
            "chat.postMessage", channel=channel, text=action.payload["text"]
        )
        return ActionReceipt(
            action_id=self._next_action_id(),
            action=action,
            result={"channel": response["channel"], "ts": response["ts"]},
            prior_state=None,
        )

    def _act_dm_user(self, action: Action) -> ActionReceipt:
        user = action.target["user"]
        # Slack DMs go through chat.postMessage with a user id as the channel:
        # opening the conversation is implicit on Slack's side.
        response = self._call(
            "chat.postMessage", channel=user, text=action.payload["text"]
        )
        return ActionReceipt(
            action_id=self._next_action_id(),
            action=action,
            result={"channel": response["channel"], "ts": response["ts"], "user": user},
            prior_state=None,
        )

    _HANDLERS: dict[str, str] = {
        "add_reaction": "_act_add_reaction",
        "post_reply": "_act_post_reply",
        "post_message": "_act_post_message",
        "dm_user": "_act_dm_user",
    }

    def act(self, action: Action) -> ActionReceipt:
        handler_name = self._HANDLERS.get(action.operation)
        if handler_name is None:
            raise KeyError(f"slack adapter has no handler for operation {action.operation!r}")
        handler = getattr(self, handler_name)
        return handler(action)

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        """Retract, never erase. See module docstring and contract.Adapter.undo."""
        action = receipt.action
        if action.operation == "add_reaction":
            already_present = (receipt.prior_state or {}).get("already_present", False)
            if not already_present:
                self._call(
                    "reactions.remove",
                    channel=action.target["channel"],
                    timestamp=action.target["ts"],
                    name=action.payload["emoji"],
                )
            result = receipt.result
        elif action.operation in ("post_reply", "post_message", "dm_user"):
            channel = receipt.result["channel"]
            ts = receipt.result["ts"]
            retraction_text = _RETRACTED_TEMPLATE.format(action_id=receipt.action_id)
            self._call("chat.update", channel=channel, ts=ts, text=retraction_text)
            result = {**receipt.result, "retracted_text": retraction_text}
        else:
            raise KeyError(f"slack adapter has no undo for operation {action.operation!r}")

        return ActionReceipt(
            action_id=receipt.action_id,
            action=receipt.action,
            result=result,
            prior_state=receipt.prior_state,
            executed_at=receipt.executed_at,
            undone_at=utcnow(),
        )
