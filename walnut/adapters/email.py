"""Email: the one adapter that reaches outside the company, and the one that matters most.

Every other adapter in this project reads and writes systems the org already owns —
a Slack workspace, a Linear board, a GitHub repo. Email is different in two ways that
shape everything below:

1. **It speaks two protocols, not one.** IMAP for reading a mailbox, SMTP for sending.
   They are unrelated wire protocols with unrelated failure modes, so this adapter
   keeps two lazily-opened connections rather than pretending there is one "email
   transport" the way Slack has one HTTP client.

2. **`send_email` is the only action anywhere in Walnut that leaves the building.**
   Flagging a message, moving it, even saving a draft are all still inside the
   mailbox — reversible, internal, invisible to the customer. Sending is not. Once
   the SMTP server accepts the message, it is gone: possibly already relayed,
   possibly already read. That is why `send_email` is `ActionTier.GATED` and why
   `undo()` refuses it outright rather than pretending a recall is possible (see
   `undo()` below). The demo's whole point runs through this: the agent drafts a
   fully-cited customer reply with `save_draft` (`INTERNAL`, executes freely, shows
   up in the real mailbox for a human to read), a person approves it, and only then
   does `send_email` (`GATED`) fire over SMTP.

Message identity is the `Message-ID` header, not the IMAP UID. UIDs are only valid
within one `UIDVALIDITY` epoch for a folder and can be reassigned after a folder
rewrite; `Message-ID` is minted once by the originating MTA and never changes. Every
`SourcePointer.locator` here carries both — the UID as a same-session optimisation,
the `Message-ID` as the thing `resolve()` actually searches on — and `Evidence.id` is
always the `Message-ID`, because that is the identifier a second Walnut session, or a
human replying six months later, can still use.

`imap_factory` and `smtp_factory` are the seam that makes this adapter testable
without a mailbox: each is a zero-argument callable returning an object shaped like
`imaplib.IMAP4` / `smtplib.SMTP`. Production code leaves both `None` and gets real
`imaplib.IMAP4_SSL` / `smtplib.SMTP_SSL` connections, opened lazily on first use so
constructing an `EmailAdapter` never touches the network. Tests inject fakes that
hold canned RFC 822 messages in memory.
"""

from __future__ import annotations

import re
from datetime import timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import format_datetime, make_msgid, parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import quote

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

__all__ = ["EmailAdapter", "EmailAdapterError", "EmailUndoImpossibleError"]

IMAPFactory = Callable[[], Any]
SMTPFactory = Callable[[], Any]

_DEFAULT_FOLDER = "INBOX"

_LIST_LINE_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$')
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")


class EmailAdapterError(RuntimeError):
    """A configuration or protocol failure this adapter refuses to paper over."""


class EmailUndoImpossibleError(EmailAdapterError):
    """Raised by `undo()` for `send_email`. See that method's docstring — this is
    not a bug to fix, it is the reason `send_email` is gated in the first place."""


def _decode_header_value(value: str | None) -> str:
    """Decode an RFC 2047 header (`=?UTF-8?B?...?=`) into plain text.

    `imaplib`/`email.message_from_bytes` hand back headers exactly as the wire sent
    them, encoded words and all. `decode_header` splits a header into its encoded
    fragments and their charsets; `make_header` reassembles them into one string.
    Malformed encoding falls back to the raw value rather than raising — a header
    Walnut cannot decode should still surface as *something* citable.
    """
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - a bad header must not sink the whole fetch
        return value


def _decode_payload(part: Message) -> str:
    """Decode one MIME part's body, trying its declared charset then two fallbacks.

    Charset declarations lie often enough (missing, wrong, or absent entirely on
    hand-rolled mail) that a single `.decode(charset)` call is not safe to trust.
    """
    payload = part.get_payload(decode=True)
    if payload is None:
        # get_payload(decode=True) returns None for a part with no transfer
        # encoding to undo (e.g. already a str) — fall back to the raw value.
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset)
    except (LookupError, UnicodeDecodeError):
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return payload.decode("latin-1", errors="replace")


def _get_body(msg: Message) -> str:
    """Extract the readable body, preferring `text/plain` over `text/html`.

    The brain reasons over plain text; HTML markup is noise it would have to strip
    again downstream. A multipart message is walked once, remembering the first
    plain-text and first HTML part it finds and preferring the former — attachments
    and inline images are skipped, since they are not text a citation should quote.
    """
    if not msg.is_multipart():
        return _decode_payload(msg)

    plain: str | None = None
    html: str | None = None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if "attachment" in str(part.get("Content-Disposition") or ""):
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain" and plain is None:
            plain = _decode_payload(part)
        elif content_type == "text/html" and html is None:
            html = _decode_payload(part)

    if plain is not None:
        return plain
    if html is not None:
        return html
    return ""


def _imap_quote(mailbox: str) -> str:
    """Quote a mailbox name for use as an IMAP string literal.

    `imaplib` does not quote mailbox arguments itself, and folder names like
    Gmail's `[Gmail]/Drafts` contain characters ("[", "]", " ") that are not safe
    to send unquoted. Quoting every mailbox name, including plain ones like
    `INBOX`, is always valid and removes the need to guess which names need it.
    """
    if mailbox.startswith('"') and mailbox.endswith('"'):
        return mailbox
    escaped = mailbox.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _parse_list_line(line: bytes) -> str | None:
    """Pull the mailbox name out of one line of an IMAP `LIST` response."""
    match = _LIST_LINE_RE.match(line)
    if not match:
        return None
    name = match.group("name").decode("utf-8", errors="replace").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name or None


def _parse_flags(fetch_data: list[Any]) -> set[str]:
    """Pull the flag set out of an IMAP `FETCH ... (FLAGS ...)` response."""
    flags: set[str] = set()
    for item in fetch_data:
        blob = item[0] if isinstance(item, tuple) else item
        if not isinstance(blob, (bytes, bytearray)):
            continue
        match = _FLAGS_RE.search(bytes(blob))
        if match:
            flags.update(f.decode("ascii", "replace") for f in match.group(1).split())
    return flags


def _extract_message_bytes(fetch_data: list[Any]) -> bytes | None:
    """Pull the raw RFC 822 payload out of an IMAP `FETCH ... (RFC822)` response.

    The response shape is a list mixing plain status lines and `(header, body)`
    tuples; the body we want is the second element of the first tuple.
    """
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


def _decode_uid(uid: bytes | str) -> str:
    return uid.decode("ascii", "replace") if isinstance(uid, (bytes, bytearray)) else str(uid)


def _resource_uri(host: str | None, folder: str, message_id: str) -> str:
    """A followable `imap://` URI. Not a real fetchable link the way a Slack
    permalink is — IMAP has no browser-openable message URL — but it uniquely
    identifies host, folder, and message, which is what the contract requires of
    `resource_uri` and what a steward console can use to look the message up."""
    host_part = host or "localhost"
    return (
        f"imap://{quote(host_part, safe='')}/"
        f"{quote(folder, safe='')}/{quote(message_id, safe='')}"
    )


class EmailAdapter:
    """Adapter for one mailbox, reachable over IMAP (read) and SMTP (send).

    Connections are opened lazily — `__init__` never touches the network — and
    cached for the adapter's lifetime; call `close()` when done with it. Passing
    `imap_factory` / `smtp_factory` bypasses the default real connections entirely,
    which is the only way this adapter can be exercised in tests: there is no mail
    account behind this build, so a fake standing in for `imaplib.IMAP4`/
    `smtplib.SMTP` is the sole way to verify the logic in this file.
    """

    name = "email"

    def __init__(
        self,
        host: str | None = None,
        user: str | None = None,
        password: str | None = None,
        smtp_host: str | None = None,
        imap_factory: IMAPFactory | None = None,
        smtp_factory: SMTPFactory | None = None,
    ) -> None:
        self._host = host
        self._user = user
        self._password = password
        self._smtp_host = smtp_host
        self._imap_factory = imap_factory
        self._smtp_factory = smtp_factory
        self._imap_conn: Any | None = None
        self._smtp_conn: Any | None = None
        self._action_seq = 0

    # -- connection plumbing --------------------------------------------------

    def _imap(self) -> Any:
        """Open the IMAP connection on first use, then reuse it."""
        if self._imap_conn is None:
            if self._imap_factory is not None:
                conn = self._imap_factory()
            else:
                if not self._host:
                    raise EmailAdapterError(
                        "EmailAdapter has no IMAP host configured and no "
                        "imap_factory was supplied; cannot open a connection."
                    )
                import imaplib

                conn = imaplib.IMAP4_SSL(self._host)
            if self._user and self._password:
                conn.login(self._user, self._password)
            self._imap_conn = conn
        return self._imap_conn

    def _smtp(self) -> Any:
        """Open the SMTP connection on first use, then reuse it.

        This is also where `send_email` gets its "refuse rather than no-op" check
        for free: an adapter with neither `smtp_host` nor `smtp_factory` raises
        here before any message is even constructed.
        """
        if self._smtp_conn is None:
            if self._smtp_factory is not None:
                conn = self._smtp_factory()
            else:
                if not self._smtp_host:
                    raise EmailAdapterError(
                        "EmailAdapter has no SMTP host configured and no "
                        "smtp_factory was supplied; send_email refuses to run "
                        "rather than silently doing nothing."
                    )
                import smtplib

                conn = smtplib.SMTP_SSL(self._smtp_host)
            if self._user and self._password:
                conn.login(self._user, self._password)
            self._smtp_conn = conn
        return self._smtp_conn

    def close(self) -> None:
        """Close whichever connections were actually opened. Safe to call anytime,
        including when nothing was ever opened."""
        if self._imap_conn is not None:
            try:
                self._imap_conn.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            try:
                self._imap_conn.logout()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            self._imap_conn = None
        if self._smtp_conn is not None:
            try:
                self._smtp_conn.quit()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            self._smtp_conn = None

    # -- evidence construction --------------------------------------------------

    def _to_evidence(self, folder: str, uid: bytes | str, raw: bytes) -> Evidence:
        msg = message_from_bytes(raw)
        message_id = (msg.get("Message-ID") or "").strip()
        if not message_id:
            # Every real MTA stamps a Message-ID; a message without one is rare
            # and usually hand-crafted. Synthesizing a stable id from its content
            # keeps the record usable instead of silently dropping it, at the
            # cost of that id not surviving if the content is later edited.
            synthetic = content_hash({"folder": folder, "raw": raw.decode("utf-8", "replace")})
            message_id = f"<walnut-synthetic-{synthetic[:16]}@walnut.local>"

        from_ = _decode_header_value(msg.get("From"))
        to_ = _decode_header_value(msg.get("To"))
        subject = _decode_header_value(msg.get("Subject"))
        date_header = msg.get("Date") or ""
        body = _get_body(msg)

        identity = {
            "folder": folder,
            "message_id": message_id,
            "from": from_,
            "to": to_,
            "subject": subject,
            "date": date_header,
            "body": body,
        }

        occurred_at = None
        if date_header:
            try:
                occurred_at = parsedate_to_datetime(date_header)
                if occurred_at is not None and occurred_at.tzinfo is None:
                    occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                occurred_at = None

        pointer = SourcePointer(
            app=self.name,
            resource_uri=_resource_uri(self._host, folder, message_id),
            locator={"folder": folder, "message_id": message_id, "uid": _decode_uid(uid)},
            content_hash=content_hash(identity),
        )
        return Evidence(
            id=message_id,
            pointer=pointer,
            text=body,
            author=from_ or None,
            occurred_at=occurred_at,
            labels=(folder,),
            raw=identity,
        )

    def _find_uid(self, conn: Any, message_id: str) -> bytes | None:
        """Look up the current UID for a `Message-ID` in whatever folder is
        currently `SELECT`ed. UIDs are only a same-session convenience — this is
        the operation that makes `Message-ID` the identifier that actually lasts.
        """
        typ, data = conn.uid("search", None, f'HEADER Message-ID "{message_id}"')
        if typ != "OK" or not data or not data[0]:
            return None
        uids = data[0].split()
        return uids[0] if uids else None

    # -- read side ------------------------------------------------------------

    def probe(self) -> SourceProfile:
        """List every folder this connection can see, via IMAP `LIST`."""
        conn = self._imap()
        typ, data = conn.list()
        if typ != "OK":
            raise EmailAdapterError("IMAP LIST failed")
        scopes = tuple(
            name for name in (_parse_list_line(line) for line in data if line) if name
        )
        return SourceProfile(
            app=self.name,
            display_name="Email (IMAP)",
            scopes=scopes,
            record_count_estimate=None,
            detail={},
        )

    def fetch(self, scope: str | None = None, limit: int = 100) -> list[Evidence]:
        """Pull messages from one folder (`scope`, default `INBOX`) as evidence."""
        folder = scope or _DEFAULT_FOLDER
        conn = self._imap()
        typ, _ = conn.select(_imap_quote(folder))
        if typ != "OK":
            raise EmailAdapterError(f"could not select IMAP folder {folder!r}")

        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK":
            raise EmailAdapterError(f"IMAP search failed in folder {folder!r}")
        uids = data[0].split() if data and data[0] else []
        uids = uids[:limit]

        evidence: list[Evidence] = []
        for uid in uids:
            typ, fetch_data = conn.uid("fetch", uid, "(RFC822)")
            if typ != "OK":
                continue
            raw = _extract_message_bytes(fetch_data)
            if raw is None:
                continue
            evidence.append(self._to_evidence(folder, uid, raw))
        return evidence

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        """Re-fetch one message live by folder + `Message-ID`.

        Never raises: a message that has been deleted, moved by someone else, or
        never existed (a conformance-suite probe pointer, say) is simply gone, and
        `None` is the honest answer — not an exception, and not an invented record.
        """
        folder = pointer.locator.get("folder")
        message_id = pointer.locator.get("message_id")
        if not folder or not message_id:
            return None

        conn = self._imap()
        try:
            typ, _ = conn.select(_imap_quote(folder))
            if typ != "OK":
                return None
            uid = self._find_uid(conn, message_id)
            if uid is None:
                return None
            typ, fetch_data = conn.uid("fetch", uid, "(RFC822)")
            if typ != "OK":
                return None
            raw = _extract_message_bytes(fetch_data)
            if raw is None:
                return None
        except EmailAdapterError:
            return None
        return self._to_evidence(folder, uid, raw)

    # -- write side -----------------------------------------------------------

    def capabilities(self) -> ActionCapabilities:
        return ActionCapabilities(
            app=self.name,
            operations={
                "flag": ActionTier.TRIVIAL,
                "move_folder": ActionTier.TRIVIAL,
                "save_draft": ActionTier.INTERNAL,
                "send_email": ActionTier.GATED,
            },
        )

    def _next_action_id(self) -> str:
        self._action_seq += 1
        return f"email-act-{self._action_seq}"

    def _act_flag(self, action: Action) -> ActionReceipt:
        folder = action.target["folder"]
        message_id = action.target["message_id"]
        flag = action.payload["flag"]
        # An allow-list, because this value goes straight into an IMAP STORE. The
        # destructive flags (\\Deleted in particular) are not reversible annotations:
        # on a server that expunges, setting one loses the message.
        _ALLOWED_FLAGS = {"\\Seen", "\\Flagged", "\\Answered", "\\Draft", "walnut"}
        if flag not in _ALLOWED_FLAGS and not flag.startswith("walnut"):
            raise EmailAdapterError(
                f"refusing to set IMAP flag {flag!r}: not in the allow-list "
                f"{sorted(_ALLOWED_FLAGS)}. Destructive flags are not annotations."
            )

        conn = self._imap()
        conn.select(_imap_quote(folder))
        uid = self._find_uid(conn, message_id)
        if uid is None:
            raise EmailAdapterError(
                f"no message with Message-ID {message_id!r} in folder {folder!r}"
            )

        # Read-before-write: if the flag is already set, undo() must not clear a
        # flag someone else set for an unrelated reason.
        typ, fetch_data = conn.uid("fetch", uid, "(FLAGS)")
        already_present = flag in _parse_flags(fetch_data) if typ == "OK" else False

        conn.uid("store", uid, "+FLAGS", f"({flag})")
        return ActionReceipt(
            action_id=self._next_action_id(),
            action=action,
            result={"folder": folder, "message_id": message_id, "flag": flag},
            prior_state={"already_present": already_present},
        )

    def _act_move_folder(self, action: Action) -> ActionReceipt:
        source = action.target["folder"]
        message_id = action.target["message_id"]
        destination = action.payload["destination"]

        conn = self._imap()
        conn.select(_imap_quote(source))
        uid = self._find_uid(conn, message_id)
        if uid is None:
            raise EmailAdapterError(
                f"no message with Message-ID {message_id!r} in folder {source!r}"
            )

        # IMAP has no native MOVE in the baseline command set this adapter
        # targets: copy to the destination, mark the source deleted, expunge.
        conn.uid("copy", uid, _imap_quote(destination))
        conn.uid("store", uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()

        return ActionReceipt(
            action_id=self._next_action_id(),
            action=action,
            result={"folder": destination, "message_id": message_id},
            prior_state={"folder": source},
        )

    def _build_message(self, payload: dict[str, Any]) -> tuple[EmailMessage, str]:
        msg = EmailMessage()
        msg["From"] = payload.get("from") or self._user or ""
        msg["To"] = payload["to"]
        msg["Subject"] = payload.get("subject", "")
        msg["Date"] = format_datetime(utcnow())
        message_id = make_msgid(domain="walnut.local")
        msg["Message-ID"] = message_id
        if payload.get("in_reply_to"):
            msg["In-Reply-To"] = payload["in_reply_to"]
            msg["References"] = payload["in_reply_to"]
        msg.set_content(payload.get("body", ""))
        return msg, message_id

    def _act_save_draft(self, action: Action) -> ActionReceipt:
        """APPEND a drafted reply into the Drafts folder. `INTERNAL`: the message
        never leaves the mailbox, so it executes without waiting on a human — the
        human reviews it as a normal draft, in their normal mail client."""
        folder = action.target.get("folder", "Drafts")
        msg, message_id = self._build_message(action.payload)

        conn = self._imap()
        typ, _ = conn.append(_imap_quote(folder), "", None, msg.as_bytes())
        if typ != "OK":
            raise EmailAdapterError(f"IMAP APPEND to {folder!r} failed")

        return ActionReceipt(
            action_id=self._next_action_id(),
            action=action,
            result={"folder": folder, "message_id": message_id},
            # Purely additive: the draft did not exist before this call, so
            # there is nothing to restore — undo() deletes it outright.
            prior_state=None,
        )

    def _act_send_email(self, action: Action) -> ActionReceipt:
        """Send over SMTP. `GATED`: this is the one write in the whole system that
        reaches a customer, and it runs only after a human approves it."""
        conn = self._smtp()  # raises EmailAdapterError if SMTP is not configured
        msg, message_id = self._build_message(action.payload)
        conn.send_message(msg)

        return ActionReceipt(
            action_id=self._next_action_id(),
            action=action,
            result={
                "to": action.payload["to"],
                "subject": action.payload.get("subject", ""),
                "message_id": message_id,
            },
            # There is no "before" state to capture for an SMTP send — the
            # message did not exist anywhere until the server accepted it, and
            # once accepted it is out of this adapter's control. See undo().
            prior_state=None,
        )

    _HANDLERS: dict[str, str] = {
        "flag": "_act_flag",
        "move_folder": "_act_move_folder",
        "save_draft": "_act_save_draft",
        "send_email": "_act_send_email",
    }

    def act(self, action: Action) -> ActionReceipt:
        handler_name = self._HANDLERS.get(action.operation)
        if handler_name is None:
            raise KeyError(f"email adapter has no handler for operation {action.operation!r}")
        return getattr(self, handler_name)(action)

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        """Reverse a write — except the one write that cannot be reversed.

        `flag`, `move_folder`, and `save_draft` are all still inside the mailbox,
        so undoing them is an ordinary IMAP operation. `send_email` is not: once
        the SMTP server has accepted a message there is no protocol operation,
        IMAP or otherwise, that un-sends it. It may already be relayed, delivered,
        or read. Returning a receipt claiming success would be a lie the same
        shape as the one this whole product exists to catch, so this raises
        instead — loudly, specifically, and by design. That irreversibility is
        the actual reason `send_email` is `GATED`: it is the one action a human
        must approve *before* it happens, because nothing can approve it after.
        """
        action = receipt.action

        if action.operation == "send_email":
            raise EmailUndoImpossibleError(
                "send_email cannot be undone: the message has already left this "
                "mailbox over SMTP and may already be delivered or read. There is "
                "no IMAP or SMTP operation that recalls a sent message. This is "
                "exactly why send_email requires human approval before it runs, "
                "rather than relying on the ability to take it back afterwards."
            )

        if action.operation == "flag":
            already_present = (receipt.prior_state or {}).get("already_present", False)
            if not already_present:
                conn = self._imap()
                conn.select(_imap_quote(action.target["folder"]))
                uid = self._find_uid(conn, action.target["message_id"])
                if uid is not None:
                    conn.uid("store", uid, "-FLAGS", f"({action.payload['flag']})")
            result = receipt.result

        elif action.operation == "move_folder":
            source = (receipt.prior_state or {}).get("folder")
            if source is None:
                raise EmailAdapterError(
                    "cannot undo move_folder: no prior folder was recorded"
                )
            message_id = action.target["message_id"]
            destination = action.payload["destination"]
            conn = self._imap()
            conn.select(_imap_quote(destination))
            uid = self._find_uid(conn, message_id)
            if uid is not None:
                conn.uid("copy", uid, _imap_quote(source))
                conn.uid("store", uid, "+FLAGS", "(\\Deleted)")
                conn.expunge()
            result = {**receipt.result, "restored_to": source}

        elif action.operation == "save_draft":
            folder = receipt.result["folder"]
            message_id = receipt.result["message_id"]
            conn = self._imap()
            conn.select(_imap_quote(folder))
            uid = self._find_uid(conn, message_id)
            if uid is not None:
                conn.uid("store", uid, "+FLAGS", "(\\Deleted)")
                conn.expunge()
            result = receipt.result

        else:
            raise KeyError(f"email adapter has no undo for operation {action.operation!r}")

        return ActionReceipt(
            action_id=receipt.action_id,
            action=receipt.action,
            result=result,
            prior_state=receipt.prior_state,
            executed_at=receipt.executed_at,
            undone_at=utcnow(),
        )
