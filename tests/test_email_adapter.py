"""Tests for the email adapter — IMAP read, SMTP send, no network required.

There is no mail account behind this build, so `FakeIMAP` and `FakeSMTP` are the
only way to exercise `EmailAdapter` at all. They hold a handful of realistic RFC 822
messages in memory and respond to the exact `imaplib`/`smtplib` calls the adapter
makes, closely enough that swapping in the real `imaplib.IMAP4_SSL` /
`smtplib.SMTP_SSL` later should not require touching the adapter's logic.
"""

from __future__ import annotations

from email import message_from_bytes
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.message import EmailMessage
from typing import Any

import pytest

from walnut import Action, ActionTier
from walnut.adapters.email import EmailAdapter, EmailAdapterError, EmailUndoImpossibleError
from walnut.conformance import run_conformance

# ---------------------------------------------------------------------------
# Fixture messages
# ---------------------------------------------------------------------------

MSG1_ID = "<msg1@example.com>"
MSG2_ID = "<msg2@example.com>"
MSG3_ID = "<msg3@example.com>"

_ENCODED_SUBJECT_TEXT = "Café renewal — 30% off ☕"
_ENCODED_SUBJECT = Header(_ENCODED_SUBJECT_TEXT, "utf-8").encode()


def _plain_message(message_id: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["From"] = "alice@customer.example"
    msg["To"] = "support@walnut.example"
    msg["Subject"] = subject
    msg["Date"] = "Fri, 12 Sep 2025 10:00:00 +0000"
    msg.set_content(body)
    return msg.as_bytes()


def _encoded_subject_message() -> bytes:
    # Built with a raw header string (not EmailMessage's own unicode handling) so
    # the fixture actually exercises RFC 2047 decoding on the read side, rather
    # than round-tripping through the same modern policy that wrote it.
    raw = (
        f"Message-ID: {MSG2_ID}\r\n"
        "From: billing@vendor.example\r\n"
        "To: ap@walnut.example\r\n"
        f"Subject: {_ENCODED_SUBJECT}\r\n"
        "Date: Sat, 13 Sep 2025 08:30:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Your subscription renews soon.\r\n"
    )
    return raw.encode("utf-8")


def _multipart_message() -> bytes:
    msg = MIMEMultipart("alternative")
    msg["Message-ID"] = MSG3_ID
    msg["From"] = "notifications@service.example"
    msg["To"] = "team@walnut.example"
    msg["Subject"] = "Weekly digest"
    msg["Date"] = "Sun, 14 Sep 2025 09:00:00 +0000"
    msg.attach(MIMEText("Plain text digest body.", "plain", "utf-8"))
    msg.attach(MIMEText("<p>HTML digest body.</p>", "html", "utf-8"))
    return msg.as_bytes()


def _seed_folders() -> dict[str, list[dict[str, Any]]]:
    return {
        "INBOX": [
            {"uid": 1, "raw": _plain_message(MSG1_ID, "Invoice question", "Can you confirm the total?"), "flags": set()},
            {"uid": 2, "raw": _encoded_subject_message(), "flags": set()},
            {"uid": 3, "raw": _multipart_message(), "flags": set()},
        ],
        "Drafts": [],
        "Archive": [],
    }


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeIMAP:
    """Stands in for `imaplib.IMAP4`/`IMAP4_SSL`, backed by an in-memory mailbox."""

    def __init__(self, folders: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.folders = folders if folders is not None else _seed_folders()
        self.current: str | None = None
        self.login_calls: list[tuple[str, str]] = []
        self.closed = False
        self.logged_out = False
        self._next_uid = 1 + max(
            (m["uid"] for msgs in self.folders.values() for m in msgs), default=0
        )

    @staticmethod
    def _unquote(mailbox: str) -> str:
        if mailbox.startswith('"') and mailbox.endswith('"'):
            return mailbox[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return mailbox

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.login_calls.append((user, password))
        return ("OK", [b"LOGIN completed"])

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        name = self._unquote(mailbox)
        self.folders.setdefault(name, [])
        self.current = name
        return ("OK", [str(len(self.folders[name])).encode()])

    def list(self) -> tuple[str, list[bytes]]:
        lines = [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.folders]
        return ("OK", lines)

    def _messages(self) -> list[dict[str, Any]]:
        assert self.current is not None, "select() must be called before uid()"
        return self.folders[self.current]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        command = command.lower()
        if command == "search":
            criteria = args[-1]
            msgs = self._messages()
            if criteria == "ALL":
                matched = msgs
            else:
                message_id = criteria.split('"')[1] if '"' in criteria else ""
                matched = [
                    m for m in msgs
                    if message_from_bytes(m["raw"]).get("Message-ID", "").strip() == message_id
                ]
            data = b" ".join(str(m["uid"]).encode() for m in matched)
            return ("OK", [data])

        if command == "fetch":
            uid = int(args[0])
            spec = args[1]
            msg = next((m for m in self._messages() if m["uid"] == uid), None)
            if msg is None:
                return ("NO", [None])
            if "FLAGS" in spec and "RFC822" not in spec:
                flags_str = " ".join(sorted(msg["flags"]))
                return ("OK", [f"{uid} (FLAGS ({flags_str}))".encode()])
            header = f"{uid} (RFC822 {{{len(msg['raw'])}}}".encode()
            return ("OK", [(header, msg["raw"]), b")"])

        if command == "store":
            uid = int(args[0])
            op = args[1]
            flags = set(args[2].strip("()").split())
            msg = next((m for m in self._messages() if m["uid"] == uid), None)
            if msg is None:
                return ("NO", [None])
            if op == "+FLAGS":
                msg["flags"] |= flags
            elif op == "-FLAGS":
                msg["flags"] -= flags
            return ("OK", [b"STORE completed"])

        if command == "copy":
            uid = int(args[0])
            dest = self._unquote(args[1])
            msg = next((m for m in self._messages() if m["uid"] == uid), None)
            if msg is None:
                return ("NO", [None])
            self.folders.setdefault(dest, [])
            new_uid = self._next_uid
            self._next_uid += 1
            self.folders[dest].append(
                {"uid": new_uid, "raw": msg["raw"], "flags": set(msg["flags"])}
            )
            return ("OK", [b"COPY completed"])

        raise NotImplementedError(f"FakeIMAP.uid does not implement {command!r}")

    def expunge(self) -> tuple[str, list[bytes]]:
        name = self.current
        assert name is not None
        self.folders[name] = [m for m in self.folders[name] if "\\Deleted" not in m["flags"]]
        return ("OK", [b""])

    def append(
        self, mailbox: str, flags: str, date_time: Any, message: bytes
    ) -> tuple[str, list[bytes]]:
        name = self._unquote(mailbox)
        self.folders.setdefault(name, [])
        uid = self._next_uid
        self._next_uid += 1
        self.folders[name].append({"uid": uid, "raw": message, "flags": set()})
        return ("OK", [f"[APPENDUID 1 {uid}] APPEND completed".encode()])

    def close(self) -> tuple[str, list[bytes]]:
        self.closed = True
        return ("OK", [b"CLOSE completed"])

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return ("BYE", [b"logging out"])


class FakeSMTP:
    """Stands in for `smtplib.SMTP`/`SMTP_SSL`."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.login_calls: list[tuple[str, str]] = []
        self.quit_called = False

    def login(self, user: str, password: str) -> None:
        self.login_calls.append((user, password))

    def send_message(self, msg: EmailMessage) -> dict[str, Any]:
        self.sent.append(msg)
        return {}

    def quit(self) -> None:
        self.quit_called = True


def make_adapter(imap: FakeIMAP | None = None, smtp: FakeSMTP | None = None) -> tuple[
    EmailAdapter, FakeIMAP, FakeSMTP
]:
    imap = imap or FakeIMAP()
    smtp = smtp or FakeSMTP()
    adapter = EmailAdapter(
        host="imap.example.com",
        user="bot@walnut.example",
        password="secret",
        smtp_host="smtp.example.com",
        imap_factory=lambda: imap,
        smtp_factory=lambda: smtp,
    )
    return adapter, imap, smtp


# ---------------------------------------------------------------------------
# Conformance suite
# ---------------------------------------------------------------------------


def test_email_adapter_conforms():
    adapter, _, _ = make_adapter()
    report = run_conformance(
        adapter,
        write_target={
            "operation": "flag",
            "target": {"folder": "INBOX", "message_id": MSG1_ID},
            "payload": {"flag": "\\Flagged"},
        },
    )
    assert report.ok, report.render()


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def test_probe_lists_imap_folders():
    adapter, _, _ = make_adapter()
    profile = adapter.probe()
    assert profile.app == "email"
    assert set(profile.scopes) == {"INBOX", "Drafts", "Archive"}


def test_fetch_decodes_rfc2047_subject():
    adapter, _, _ = make_adapter()
    records = {ev.id: ev for ev in adapter.fetch("INBOX")}
    subject = records[MSG2_ID].raw["subject"]
    assert subject == _ENCODED_SUBJECT_TEXT


def test_fetch_prefers_text_plain_over_html_in_multipart():
    adapter, _, _ = make_adapter()
    records = {ev.id: ev for ev in adapter.fetch("INBOX")}
    body = records[MSG3_ID].text
    assert "Plain text digest body." in body
    assert "<p>" not in body


def test_fetch_honours_limit():
    adapter, _, _ = make_adapter()
    assert len(adapter.fetch("INBOX", limit=2)) == 2


def test_content_hash_stable_across_repeated_fetches():
    adapter, _, _ = make_adapter()
    first = {ev.id: ev.pointer.content_hash for ev in adapter.fetch("INBOX")}
    second = {ev.id: ev.pointer.content_hash for ev in adapter.fetch("INBOX")}
    assert first == second
    assert all(len(h) == 64 for h in first.values())


def test_resolve_roundtrips_by_message_id():
    adapter, _, _ = make_adapter()
    original = next(ev for ev in adapter.fetch("INBOX") if ev.id == MSG1_ID)
    again = adapter.resolve(original.pointer)
    assert again is not None
    assert again.pointer.content_hash == original.pointer.content_hash
    assert again.id == MSG1_ID


def test_resolve_missing_returns_none_never_raises():
    adapter, _, _ = make_adapter()
    from walnut import SourcePointer

    ghost = SourcePointer(
        app="email",
        resource_uri="imap://imap.example.com/INBOX/%3Cghost%40example.com%3E",
        locator={"folder": "INBOX", "message_id": "<ghost@example.com>"},
        content_hash="0" * 64,
    )
    assert adapter.resolve(ghost) is None


def test_resource_uri_uses_imap_scheme():
    adapter, _, _ = make_adapter()
    ev = adapter.fetch("INBOX", limit=1)[0]
    assert ev.pointer.resource_uri.startswith("imap://")


# ---------------------------------------------------------------------------
# Write side: flag / move (trivial, reversible)
# ---------------------------------------------------------------------------


def test_flag_sets_and_undo_removes_it():
    adapter, imap, _ = make_adapter()
    action = Action(
        app="email",
        operation="flag",
        target={"folder": "INBOX", "message_id": MSG1_ID},
        payload={"flag": "\\Flagged"},
        justified_by=("email:conformance",),
    )
    receipt = adapter.act(action)
    imap.select("INBOX")
    msg = next(m for m in imap.folders["INBOX"] if m["uid"] == 1)
    assert "\\Flagged" in msg["flags"]

    adapter.undo(receipt)
    assert "\\Flagged" not in msg["flags"]


def test_flag_undo_leaves_preexisting_flag_alone():
    imap = FakeIMAP()
    imap.folders["INBOX"][0]["flags"] = {"\\Flagged"}
    adapter, _, _ = make_adapter(imap=imap)
    action = Action(
        app="email",
        operation="flag",
        target={"folder": "INBOX", "message_id": MSG1_ID},
        payload={"flag": "\\Flagged"},
        justified_by=("email:conformance",),
    )
    receipt = adapter.act(action)
    adapter.undo(receipt)
    msg = next(m for m in imap.folders["INBOX"] if m["uid"] == 1)
    # It was already flagged before our action; undo must not remove a flag we
    # did not add.
    assert "\\Flagged" in msg["flags"]


def test_move_folder_and_undo_restores_original_folder():
    adapter, imap, _ = make_adapter()
    action = Action(
        app="email",
        operation="move_folder",
        target={"folder": "INBOX", "message_id": MSG1_ID},
        payload={"destination": "Archive"},
        justified_by=("email:conformance",),
    )
    receipt = adapter.act(action)
    assert not any(
        message_from_bytes(m["raw"]).get("Message-ID") == MSG1_ID
        for m in imap.folders["INBOX"]
    )
    assert any(
        message_from_bytes(m["raw"]).get("Message-ID") == MSG1_ID
        for m in imap.folders["Archive"]
    )

    adapter.undo(receipt)
    assert any(
        message_from_bytes(m["raw"]).get("Message-ID") == MSG1_ID
        for m in imap.folders["INBOX"]
    )


# ---------------------------------------------------------------------------
# Write side: save_draft (internal, executes freely)
# ---------------------------------------------------------------------------


def test_save_draft_appends_to_drafts_folder():
    adapter, imap, _ = make_adapter()
    action = Action(
        app="email",
        operation="save_draft",
        target={"folder": "Drafts"},
        payload={
            "to": "alice@customer.example",
            "subject": "Re: Invoice question",
            "body": "Confirmed — the total is $482.00.",
            "in_reply_to": MSG1_ID,
        },
        justified_by=(MSG1_ID,),
    )
    receipt = adapter.act(action)
    assert receipt.result["folder"] == "Drafts"
    assert len(imap.folders["Drafts"]) == 1
    stored = message_from_bytes(imap.folders["Drafts"][0]["raw"])
    assert stored["To"] == "alice@customer.example"
    assert stored["In-Reply-To"] == MSG1_ID
    assert "Confirmed" in stored.get_payload()


def test_save_draft_undo_deletes_the_draft():
    adapter, imap, _ = make_adapter()
    action = Action(
        app="email",
        operation="save_draft",
        target={"folder": "Drafts"},
        payload={"to": "alice@customer.example", "subject": "Re", "body": "Draft body"},
        justified_by=(MSG1_ID,),
    )
    receipt = adapter.act(action)
    adapter.undo(receipt)
    assert imap.folders["Drafts"] == []


# ---------------------------------------------------------------------------
# Write side: send_email (gated, irreversible)
# ---------------------------------------------------------------------------


def test_send_email_capability_is_gated():
    adapter, _, _ = make_adapter()
    caps = adapter.capabilities()
    assert caps.tier_of("send_email") is ActionTier.GATED
    assert caps.tier_of("flag") is ActionTier.TRIVIAL
    assert caps.tier_of("move_folder") is ActionTier.TRIVIAL
    assert caps.tier_of("save_draft") is ActionTier.INTERNAL


def test_send_email_actually_calls_smtp():
    adapter, _, smtp = make_adapter()
    action = Action(
        app="email",
        operation="send_email",
        target={},
        payload={
            "to": "alice@customer.example",
            "subject": "Re: Invoice question",
            "body": "Confirmed — the total is $482.00.",
            "in_reply_to": MSG1_ID,
        },
        justified_by=(MSG1_ID,),
    )
    receipt = adapter.act(action)
    assert len(smtp.sent) == 1
    sent = smtp.sent[0]
    assert sent["To"] == "alice@customer.example"
    assert receipt.result["to"] == "alice@customer.example"
    assert receipt.result["message_id"]


def test_send_email_refuses_without_smtp_configured():
    imap = FakeIMAP()
    adapter = EmailAdapter(host="imap.example.com", imap_factory=lambda: imap)
    action = Action(
        app="email",
        operation="send_email",
        target={},
        payload={"to": "alice@customer.example", "subject": "Hi", "body": "Hello"},
        justified_by=(MSG1_ID,),
    )
    with pytest.raises(EmailAdapterError):
        adapter.act(action)


def test_undo_send_email_raises_rather_than_pretending_to_succeed():
    adapter, _, smtp = make_adapter()
    action = Action(
        app="email",
        operation="send_email",
        target={},
        payload={"to": "alice@customer.example", "subject": "Hi", "body": "Hello"},
        justified_by=(MSG1_ID,),
    )
    receipt = adapter.act(action)
    with pytest.raises(EmailUndoImpossibleError):
        adapter.undo(receipt)
    # And critically, the message was really sent — undo failing is not a
    # substitute for the send never having happened.
    assert len(smtp.sent) == 1
