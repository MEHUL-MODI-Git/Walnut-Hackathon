"""Conformance and behavioural tests for the Slack adapter.

Everything here runs against `FakeSlackTransport`, a small in-memory stand-in for
the Slack Web API. We have no Slack credentials, so this fake — returning the same
`{"ok": ..., ...}` envelopes the real API returns — is the only way to verify the
adapter without a live workspace. That is also why `SlackAdapter.__init__` takes an
injectable `transport`: production code never has a reason to fake anything, but
tests have no other way in.
"""

from __future__ import annotations

from typing import Any

import pytest

from walnut.adapters.slack import SlackAdapter, SlackAPIError
from walnut.conformance import run_conformance
from walnut.contract import Action, SourcePointer


class FakeSlackTransport:
    """Records every call and answers like the real Slack Web API would.

    State is plain dicts keyed by channel id, which keeps the fake legible and
    lets tests mutate it directly (e.g. to simulate someone editing a message in
    Slack, outside of anything the adapter did) without going through `act()`.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_on: set[str] = set()

        self.channels: dict[str, str] = {"C_ENG": "eng", "C_GENERAL": "general"}
        self.messages: dict[str, list[dict[str, Any]]] = {
            "C_ENG": [
                {"ts": "1700000000.000100", "user": "U_ALICE", "text": "shipped v2!!!"},
                {
                    "ts": "1700000001.000200",
                    "user": "U_BOB",
                    "text": "nice, tracking the timeout issue in ENG-412",
                    "thread_ts": "1700000000.000100",
                },
            ],
            "C_GENERAL": [
                {"ts": "1700000100.000100", "user": "U_CARL", "text": "congrats team"},
            ],
        }
        # The parent gets its reply_count computed on read, matching how Slack's
        # own `conversations.history` annotates thread parents.
        self._sync_reply_counts()
        self._next_ts = 1700000900.000100

    def _sync_reply_counts(self) -> None:
        for channel, msgs in self.messages.items():
            counts: dict[str, int] = {}
            for m in msgs:
                parent = m.get("thread_ts")
                if parent and parent != m["ts"]:
                    counts[parent] = counts.get(parent, 0) + 1
            for m in msgs:
                if m["ts"] in counts:
                    m["reply_count"] = counts[m["ts"]]

    def _mint_ts(self) -> str:
        ts = f"{self._next_ts:.6f}"
        self._next_ts += 1
        return ts

    def _method_from_url(self, url: str) -> str:
        return url.rsplit("/", 1)[-1]

    def __call__(self, http_method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        method = self._method_from_url(url)
        params = kwargs.get("json") or kwargs.get("params") or {}
        self.calls.append((method, dict(params)))

        if method in self.fail_on:
            return {"ok": False, "error": f"forced_failure_for_{method}"}

        handler = getattr(self, f"_handle_{method.replace('.', '_')}", None)
        if handler is None:
            return {"ok": False, "error": "unsupported_test_method"}
        return handler(params)

    # -- handlers -------------------------------------------------------------

    def _handle_conversations_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "channels": [{"id": cid, "name": name} for cid, name in self.channels.items()],
        }

    def _handle_conversations_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Real Slack only returns channel-level messages here: a thread reply is
        visible via `conversations.replies`, not as a second top-level entry."""
        channel = params["channel"]
        self._sync_reply_counts()
        top_level = [
            m
            for m in self.messages.get(channel, [])
            if m.get("thread_ts") in (None, m["ts"])
        ]
        return {"ok": True, "messages": top_level}

    def _handle_conversations_replies(self, params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        ts = params["ts"]
        msgs = self.messages.get(channel, [])
        parent = next((m for m in msgs if m["ts"] == ts), None)
        if parent is None:
            return {"ok": False, "error": "message_not_found"}
        replies = sorted(
            (m for m in msgs if m.get("thread_ts") == ts and m["ts"] != ts),
            key=lambda m: m["ts"],
        )
        return {"ok": True, "messages": [parent, *replies]}

    def _handle_chat_getPermalink(self, params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        ts = params["message_ts"]
        if channel not in self.channels:
            return {"ok": False, "error": "channel_not_found"}
        return {
            "ok": True,
            "permalink": f"https://example.slack.com/archives/{channel}/p{ts.replace('.', '')}",
        }

    def _handle_chat_postMessage(self, params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        ts = self._mint_ts()
        message = {"ts": ts, "user": "U_WALNUT_BOT", "text": params.get("text", "")}
        if params.get("thread_ts"):
            message["thread_ts"] = params["thread_ts"]
        self.messages.setdefault(channel, []).append(message)
        self._sync_reply_counts()
        return {"ok": True, "channel": channel, "ts": ts}

    def _handle_chat_update(self, params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        ts = params["ts"]
        msgs = self.messages.get(channel, [])
        target = next((m for m in msgs if m["ts"] == ts), None)
        if target is None:
            return {"ok": False, "error": "message_not_found"}
        target["text"] = params.get("text", "")
        target["edited"] = {"ts": self._mint_ts()}
        return {"ok": True, "channel": channel, "ts": ts, "text": target["text"]}

    def _handle_chat_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        # Present only so a test can assert this is never called by undo().
        channel = params["channel"]
        ts = params["ts"]
        self.messages[channel] = [m for m in self.messages.get(channel, []) if m["ts"] != ts]
        return {"ok": True, "channel": channel, "ts": ts}

    def _handle_reactions_add(self, params: dict[str, Any]) -> dict[str, Any]:
        channel, ts, name = params["channel"], params["timestamp"], params["name"]
        target = next((m for m in self.messages.get(channel, []) if m["ts"] == ts), None)
        if target is None:
            return {"ok": False, "error": "message_not_found"}
        reactions = target.setdefault("reactions", [])
        existing = next((r for r in reactions if r["name"] == name), None)
        if existing is None:
            reactions.append({"name": name, "users": ["U_WALNUT_BOT"], "count": 1})
        return {"ok": True}

    def _handle_reactions_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        channel, ts, name = params["channel"], params["timestamp"], params["name"]
        target = next((m for m in self.messages.get(channel, []) if m["ts"] == ts), None)
        if target is None:
            return {"ok": False, "error": "message_not_found"}
        reactions = target.get("reactions", [])
        before = len(reactions)
        target["reactions"] = [r for r in reactions if r["name"] != name]
        if len(target["reactions"]) == before:
            return {"ok": False, "error": "no_reaction"}
        return {"ok": True}

    def _handle_reactions_get(self, params: dict[str, Any]) -> dict[str, Any]:
        channel, ts = params["channel"], params["timestamp"]
        target = next((m for m in self.messages.get(channel, []) if m["ts"] == ts), None)
        if target is None:
            return {"ok": False, "error": "message_not_found"}
        return {"ok": True, "message": {"reactions": target.get("reactions", [])}}

    def _handle_users_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "members": [{"id": "U_ALICE"}, {"id": "U_BOB"}, {"id": "U_CARL"}]}

    # -- test helpers -----------------------------------------------------------

    def edit_message_text(self, channel: str, ts: str, new_text: str) -> None:
        """Simulate a message being edited directly in Slack, outside of Walnut."""
        target = next(m for m in self.messages[channel] if m["ts"] == ts)
        target["text"] = new_text
        target["edited"] = {"ts": self._mint_ts()}


@pytest.fixture()
def transport() -> FakeSlackTransport:
    return FakeSlackTransport()


@pytest.fixture()
def adapter(transport: FakeSlackTransport) -> SlackAdapter:
    return SlackAdapter(token="xoxb-fake-token", transport=transport)


# -- conformance --------------------------------------------------------------


def test_slack_adapter_conforms(adapter: SlackAdapter) -> None:
    write_target = {
        "operation": "add_reaction",
        "target": {"channel": "C_ENG", "ts": "1700000000.000100"},
        "payload": {"emoji": "eyes"},
    }
    report = run_conformance(adapter, write_target=write_target)
    assert report.ok, report.render()


# -- content hashing ------------------------------------------------------------


def test_content_hash_is_a_real_sha256(adapter: SlackAdapter) -> None:
    evidence = adapter.fetch(scope="#eng")[0]
    assert len(evidence.pointer.content_hash) == 64
    int(evidence.pointer.content_hash, 16)  # raises if not hex


def test_content_hash_is_stable_across_repeated_fetches(adapter: SlackAdapter) -> None:
    first = adapter.fetch(scope="#eng")[0]
    second = adapter.fetch(scope="#eng")[0]
    assert first.pointer.content_hash == second.pointer.content_hash


def test_content_hash_changes_when_the_message_is_edited(
    adapter: SlackAdapter, transport: FakeSlackTransport
) -> None:
    before = adapter.fetch(scope="#eng")[0]
    transport.edit_message_text("C_ENG", "1700000000.000100", "shipped v2, edited")
    after = adapter.resolve(before.pointer)
    assert after is not None
    assert after.pointer.content_hash != before.pointer.content_hash


# -- resource_uri ---------------------------------------------------------------


def test_resource_uri_is_a_followable_permalink(adapter: SlackAdapter) -> None:
    evidence = adapter.fetch(scope="#eng")[0]
    assert evidence.pointer.resource_uri.startswith("https://")


# -- fetch / threads --------------------------------------------------------------


def test_fetch_includes_thread_replies(adapter: SlackAdapter) -> None:
    evidence = adapter.fetch(scope="#eng", limit=10)
    ids = {e.id for e in evidence}
    assert "C_ENG:1700000000.000100" in ids
    assert "C_ENG:1700000001.000200" in ids
    reply = next(e for e in evidence if e.id == "C_ENG:1700000001.000200")
    assert "thread_reply" in reply.labels


def test_fetch_accepts_a_human_channel_name(adapter: SlackAdapter) -> None:
    evidence = adapter.fetch(scope="#general")
    assert evidence and evidence[0].pointer.locator["channel"] == "C_GENERAL"


# -- resolve ----------------------------------------------------------------------


def test_resolve_missing_message_returns_none(adapter: SlackAdapter) -> None:
    ghost = SourcePointer(
        app="slack",
        resource_uri="https://example.slack.com/archives/C_ENG/pNOPE",
        locator={"channel": "C_ENG", "ts": "9999999999.000000"},
        content_hash="0" * 64,
    )
    assert adapter.resolve(ghost) is None


def test_resolve_missing_never_raises_even_with_bad_channel(adapter: SlackAdapter) -> None:
    ghost = SourcePointer(
        app="slack",
        resource_uri="https://example.slack.com/archives/C_NOPE/pNOPE",
        locator={"channel": "C_NOPE", "ts": "1.000000"},
        content_hash="0" * 64,
    )
    assert adapter.resolve(ghost) is None


# -- ok:false handling --------------------------------------------------------------


def test_ok_false_is_treated_as_an_error_not_a_success(
    adapter: SlackAdapter, transport: FakeSlackTransport
) -> None:
    transport.fail_on.add("conversations.list")
    with pytest.raises(SlackAPIError):
        adapter.probe()


def test_ok_false_on_a_write_raises_slack_api_error(
    adapter: SlackAdapter, transport: FakeSlackTransport
) -> None:
    transport.fail_on.add("chat.postMessage")
    action = Action(
        app="slack",
        operation="post_message",
        target={"channel": "C_ENG"},
        payload={"text": "hello"},
        justified_by=("slack:C_ENG:1700000000.000100",),
    )
    with pytest.raises(SlackAPIError):
        adapter.act(action)


# -- capabilities / tiers --------------------------------------------------------------


def test_dm_user_is_gated(adapter: SlackAdapter) -> None:
    from walnut.contract import ActionTier

    caps = adapter.capabilities()
    assert caps.tier_of("dm_user") is ActionTier.GATED
    assert caps.tier_of("add_reaction") is ActionTier.TRIVIAL
    assert caps.tier_of("post_reply") is ActionTier.INTERNAL
    assert caps.tier_of("post_message") is ActionTier.INTERNAL


# -- act / undo --------------------------------------------------------------


def test_post_reply_is_purely_additive(adapter: SlackAdapter) -> None:
    action = Action(
        app="slack",
        operation="post_reply",
        target={"channel": "C_ENG", "thread_ts": "1700000000.000100"},
        payload={"text": "following up"},
        justified_by=("slack:C_ENG:1700000000.000100",),
    )
    receipt = adapter.act(action)
    assert receipt.prior_state is None
    assert receipt.result["thread_ts"] == "1700000000.000100"


def test_undo_retracts_rather_than_deletes(
    adapter: SlackAdapter, transport: FakeSlackTransport
) -> None:
    action = Action(
        app="slack",
        operation="post_message",
        target={"channel": "C_ENG"},
        payload={"text": "an automated note"},
        justified_by=("slack:C_ENG:1700000000.000100",),
    )
    receipt = adapter.act(action)
    posted_ts = receipt.result["ts"]
    assert any(m["ts"] == posted_ts for m in transport.messages["C_ENG"])

    undone = adapter.undo(receipt)

    assert undone.is_undone
    # The message must still exist — retracted, not erased.
    message = next(m for m in transport.messages["C_ENG"] if m["ts"] == posted_ts)
    assert "retracted by Walnut" in message["text"]
    assert not any(method == "chat.delete" for method, _ in transport.calls)
    assert any(method == "chat.update" for method, _ in transport.calls)


def test_undo_add_reaction_removes_it(
    adapter: SlackAdapter, transport: FakeSlackTransport
) -> None:
    action = Action(
        app="slack",
        operation="add_reaction",
        target={"channel": "C_ENG", "ts": "1700000000.000100"},
        payload={"emoji": "tada"},
        justified_by=("slack:C_ENG:1700000000.000100",),
    )
    receipt = adapter.act(action)
    message = next(m for m in transport.messages["C_ENG"] if m["ts"] == "1700000000.000100")
    assert any(r["name"] == "tada" for r in message.get("reactions", []))

    adapter.undo(receipt)
    message = next(m for m in transport.messages["C_ENG"] if m["ts"] == "1700000000.000100")
    assert not any(r["name"] == "tada" for r in message.get("reactions", []))


def test_act_needs_justifying_evidence() -> None:
    with pytest.raises(ValueError, match="no justifying evidence"):
        Action(
            app="slack",
            operation="post_message",
            target={"channel": "C_ENG"},
            payload={"text": "hi"},
            justified_by=(),
        )
