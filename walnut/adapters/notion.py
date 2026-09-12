"""The Notion adapter.

Notion is the messiest of the five sources this project ingests, for one reason:
every property is a differently-shaped nested object depending on its type. A
`title` property is a list of rich-text runs; a `select` is `{"name": ...}`; a
`status` is also `{"name": ...}` but is a *distinct* property type from `select`
even though it looks identical on the wire; a `date` is `{"start": ..., "end":
...}`. Most broken Notion integrations are broken because they special-cased one
or two of these and fell over on the rest. `_plain()` is the one place in this
file that has to get all of them right, so it is written once and tested against
every type the fixture data and the demo actually use.

The other property of this adapter worth calling out: Notion has no native
"retract" primitive. There is no way to strike through a property value the way
Slack lets you edit a message. So `undo()` does the closest honest thing for each
operation:

* `set_property`  — writes the prior value back. This is a real, clean undo.
* `append_block`  — cannot be deleted without destroying the record that Walnut
  ever wrote it, so it is edited in place to carry a strikethrough annotation and
  a `[retracted by Walnut ...]` marker instead.
* `create_page`   — archived (`PATCH .../pages/{id}` with `archived: true`), which
  is Notion's own soft-delete. The page is gone from every view but still exists
  and can be un-archived by a human, which is exactly the audit-trail property
  this project cares about.

Construction takes an injectable `transport` for the same reason every adapter in
this codebase does: there are no Notion credentials in this environment, and the
conformance suite is the only thing that can prove this file is correct. The
default transport is the only code path that ever touches the network; every test
in `tests/test_notion_adapter.py` replaces it with a fake that plays back
realistic Notion REST payloads.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
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

__all__ = ["NotionAdapter"]

_NOTION_VERSION = "2022-06-28"
_NOTION_BASE_URL = "https://api.notion.com/v1"

Transport = Callable[..., dict[str, Any]]

# Block types whose rendered text should carry a prefix so flattened evidence
# still reads like the outline it came from, rather than a wall of run-on prose.
_BLOCK_PREFIXES: dict[str, str] = {
    "heading_1": "# ",
    "heading_2": "## ",
    "heading_3": "### ",
    "bulleted_list_item": "- ",
    "numbered_list_item": "1. ",
    "quote": "> ",
    "callout": "> ",
    "to_do": "[ ] ",
}


def _plain(prop: dict[str, Any] | None) -> str:
    """Flatten one Notion property object to a readable string.

    Every Notion property is `{"id": ..., "type": <t>, <t>: <value>}` where the
    shape of `<value>` depends entirely on `<t>`. This is the one place that
    switches on every type the adapter and its tests care about, so a caller
    never has to know that `status` and `select` are different property types
    that happen to share a `{"name": ...}` shape, or that `date` uses `start`/
    `end` rather than a single value.

    Unknown or empty property types resolve to `""` rather than raising — a
    property Walnut does not understand yet should degrade to "no text", not
    crash the whole page's ingestion.
    """
    if not prop:
        return ""
    ptype = prop.get("type")
    if not ptype:
        return ""
    value = prop.get(ptype)

    if ptype in ("title", "rich_text"):
        return "".join(run.get("plain_text", "") for run in (value or []))
    if ptype in ("select", "status"):
        return (value or {}).get("name", "")
    if ptype == "multi_select":
        return ", ".join(v.get("name", "") for v in (value or []))
    if ptype == "people":
        return ", ".join(p.get("name") or p.get("id", "") for p in (value or []))
    if ptype == "date":
        if not value:
            return ""
        start = value.get("start", "")
        end = value.get("end")
        return f"{start} → {end}" if end else start
    if ptype == "checkbox":
        return "true" if value else "false"
    if ptype == "number":
        return "" if value is None else str(value)
    if ptype in ("url", "email", "phone_number"):
        return value or ""
    if ptype == "relation":
        return ", ".join(r.get("id", "") for r in (value or []))
    if ptype in ("created_by", "last_edited_by"):
        return (value or {}).get("name") or (value or {}).get("id", "")
    if ptype in ("created_time", "last_edited_time"):
        return value or ""
    if ptype == "formula":
        inner = value or {}
        inner_type = inner.get("type")
        if not inner_type:
            return ""
        return _plain({"type": inner_type, inner_type: inner.get(inner_type)})
    # Fall back to a best-effort string rather than raising: a property type
    # this adapter has not been taught about is still better surfaced as
    # something than dropped silently.
    return "" if value is None else str(value)


def _flatten_properties(properties: dict[str, Any]) -> str:
    """Render a page's properties as `Name: value` lines, sorted for stability.

    Sorting by property name means the same page always hashes the same way
    regardless of whatever order the Notion API happens to serialise the
    `properties` object in on a given call.
    """
    lines = []
    for name in sorted(properties):
        rendered = _plain(properties[name])
        if rendered:
            lines.append(f"{name}: {rendered}")
    return "\n".join(lines)


def _block_text(block: dict[str, Any]) -> str:
    """Flatten one child block to one line of text, or `""` if it carries none."""
    btype = block.get("type")
    if not btype:
        return ""
    data = block.get(btype) or {}
    rich_text = data.get("rich_text")
    text = "".join(run.get("plain_text", "") for run in rich_text) if rich_text else ""
    if not text:
        return ""
    prefix = _BLOCK_PREFIXES.get(btype, "")
    if btype == "to_do" and data.get("checked"):
        prefix = "[x] "
    return f"{prefix}{text}"


def _title_text(obj: dict[str, Any]) -> str:
    """A database or page object's own `title` field (not a `properties` entry)."""
    return "".join(run.get("plain_text", "") for run in (obj.get("title") or []))


def _parse_dt(value: str | None) -> datetime | None:
    """Parse a Notion ISO-8601 timestamp. Notion always suffixes `Z`; `datetime`
    only accepts `+00:00`, so the two are not interchangeable without this."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_missing_or_archived(resp: dict[str, Any] | None) -> bool:
    """True if a `GET /pages/{id}` response is not a live, resolvable page.

    Notion's REST API does not raise transport-level errors for a bad id — it
    returns `{"object": "error", ...}` with a 2xx-adjacent JSON body. Treating
    that, plus an archived or trashed page, as "gone" is what lets `resolve()`
    honour the contract's "never invent a record" rule without the adapter
    having to know anything about HTTP status codes.
    """
    if not isinstance(resp, dict):
        return True
    if resp.get("object") == "error":
        return True
    if resp.get("archived"):
        return True
    if resp.get("in_trash"):
        return True
    return False


def _property_write_payload(prop_type: str, value: Any) -> dict[str, Any]:
    """Build the `{<type>: ...}` body Notion expects to write one property.

    The property *type* always comes from a live read of the page (see
    `_act_set_property`), never from caller input — that is what makes a
    `set_property` write safe even when the caller only supplies a plain
    string value.
    """
    if prop_type in ("select", "status"):
        return {prop_type: ({"name": value} if value else None)}
    if prop_type == "rich_text":
        return {"rich_text": [{"type": "text", "text": {"content": value}}] if value else []}
    if prop_type == "title":
        return {"title": [{"type": "text", "text": {"content": value}}] if value else []}
    if prop_type == "date":
        return {"date": ({"start": value} if value else None)}
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}
    if prop_type == "number":
        return {"number": value}
    if prop_type in ("url", "email", "phone_number"):
        return {prop_type: value}
    raise ValueError(
        f"notion adapter does not know how to write property type {prop_type!r}"
    )


def _build_block(block_type: str, text: str) -> dict[str, Any]:
    rich_text = [{"type": "text", "text": {"content": text}}]
    if block_type == "callout":
        return {
            "object": "block",
            "type": "callout",
            "callout": {"rich_text": rich_text, "icon": {"type": "emoji", "emoji": "\U0001f4dd"}},
        }
    if block_type == "paragraph":
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}}
    raise ValueError(f"append_block does not support block_type {block_type!r}")


def _mark_undone(receipt: ActionReceipt) -> ActionReceipt:
    return ActionReceipt(
        action_id=receipt.action_id,
        action=receipt.action,
        result=receipt.result,
        prior_state=receipt.prior_state,
        executed_at=receipt.executed_at,
        undone_at=utcnow(),
    )


class NotionAdapter:
    """Reads Notion pages as cited evidence; writes property changes and blocks.

    `transport` is `(method, path, **kwargs) -> dict` — one REST call against
    `https://api.notion.com/v1`, always returning the parsed JSON body (Notion
    encodes both success and error as JSON, so the transport never needs to
    raise for the adapter to tell the two apart). Pass a fake in tests; leave it
    `None` in production to get a real `httpx`-backed transport built from
    `token`.
    """

    name = "notion"

    def __init__(self, token: str | None = None, transport: Transport | None = None) -> None:
        self._token = token
        self._transport: Transport = transport or self._build_default_transport(token)

    @staticmethod
    def _build_default_transport(token: str | None) -> Transport:
        """The only code path in this file that touches the network.

        `httpx` is imported here rather than at module scope so that importing
        this module — and running the conformance suite against a fake
        transport — never requires a network-capable client to exist, only to
        be constructed when someone actually asks for one.
        """
        import httpx

        headers = {
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        client = httpx.Client(base_url=_NOTION_BASE_URL, headers=headers, timeout=30.0)

        def transport(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            response = client.request(method, path, **kwargs)
            try:
                return response.json()
            except ValueError:
                return {
                    "object": "error",
                    "status": response.status_code,
                    "message": response.text,
                }

        # Exposed so tests can assert the header contract without a socket:
        # the Notion-Version header is required and its absence fails silently
        # (a 400 with a message easy to miss), so this is worth being able to
        # check directly.
        transport.headers = headers  # type: ignore[attr-defined]
        return transport

    # -- read -----------------------------------------------------------------

    def probe(self) -> SourceProfile:
        """List the databases and pages this integration has been shared with.

        Scopes are database ids — the same identifiers `fetch(scope=...)`
        accepts — so a caller can go straight from `probe()` output to a
        targeted `fetch()` without a translation step.
        """
        db_resp = self._transport(
            "POST",
            "/search",
            json={"filter": {"property": "object", "value": "database"}, "page_size": 100},
        )
        databases = db_resp.get("results", []) or []

        page_resp = self._transport(
            "POST",
            "/search",
            json={"filter": {"property": "object", "value": "page"}, "page_size": 100},
        )
        pages = [p for p in (page_resp.get("results", []) or []) if not p.get("archived")]

        scopes = tuple(d["id"] for d in databases if d.get("id"))
        if not scopes:
            # Nothing has been shared as a database — fall back to a synthetic
            # scope so probe() never reports zero scopes when pages *are*
            # reachable. fetch(scope=None) performs the same page search.
            scopes = ("*",)

        return SourceProfile(
            app=self.name,
            display_name="Notion",
            scopes=scopes,
            record_count_estimate=len(pages) + len(databases),
            detail={
                "databases": {d["id"]: _title_text(d) for d in databases if d.get("id")},
                "page_count": len(pages),
            },
        )

    def fetch(self, scope: str | None = None, limit: int = 100) -> list[Evidence]:
        """Pull pages as evidence. `scope` is a database id; `None` searches
        every page this integration can see."""
        raw_pages = (
            self._query_database_pages(scope, limit)
            if scope and scope != "*"
            else self._search_pages(limit)
        )
        return [
            self._page_to_evidence(page)
            for page in raw_pages
            if not page.get("archived")
        ][:limit]

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        """Re-fetch one page live. `None` if it is gone, archived, or trashed —
        never raises, never fabricates a stand-in record."""
        page_id = pointer.locator.get("id")
        if not page_id:
            return None
        page = self._transport("GET", f"/pages/{page_id}")
        if _is_missing_or_archived(page):
            return None
        return self._page_to_evidence(page)

    # -- write ------------------------------------------------------------------

    def capabilities(self) -> ActionCapabilities:
        return ActionCapabilities(
            app=self.name,
            operations={
                "append_block": ActionTier.TRIVIAL,
                "set_property": ActionTier.INTERNAL,
                "create_page": ActionTier.INTERNAL,
            },
        )

    def act(self, action: Action) -> ActionReceipt:
        if action.operation == "append_block":
            return self._act_append_block(action)
        if action.operation == "set_property":
            return self._act_set_property(action)
        if action.operation == "create_page":
            return self._act_create_page(action)
        raise KeyError(f"notion adapter has no operation {action.operation!r}")

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        op = receipt.action.operation
        if op == "append_block":
            return self._undo_append_block(receipt)
        if op == "set_property":
            return self._undo_set_property(receipt)
        if op == "create_page":
            return self._undo_create_page(receipt)
        raise KeyError(f"notion adapter has no undo for operation {op!r}")

    # -- read helpers -------------------------------------------------------

    def _query_database_pages(self, database_id: str, limit: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(results) < limit:
            body: dict[str, Any] = {"page_size": min(100, limit)}
            if cursor:
                body["start_cursor"] = cursor
            resp = self._transport("POST", f"/databases/{database_id}/query", json=body)
            results.extend(resp.get("results", []) or [])
            if not resp.get("has_more") or not resp.get("next_cursor"):
                break
            cursor = resp["next_cursor"]
        return results[:limit]

    def _search_pages(self, limit: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(results) < limit:
            body: dict[str, Any] = {
                "filter": {"property": "object", "value": "page"},
                "page_size": min(100, limit),
            }
            if cursor:
                body["start_cursor"] = cursor
            resp = self._transport("POST", "/search", json=body)
            results.extend(resp.get("results", []) or [])
            if not resp.get("has_more") or not resp.get("next_cursor"):
                break
            cursor = resp["next_cursor"]
        return results[:limit]

    def _fetch_block_texts(self, page_id: str) -> list[str]:
        texts: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = self._transport("GET", f"/blocks/{page_id}/children", params=params)
            for block in resp.get("results", []) or []:
                rendered = _block_text(block)
                if rendered:
                    texts.append(rendered)
            if not resp.get("has_more") or not resp.get("next_cursor"):
                break
            cursor = resp["next_cursor"]
        return texts

    def _page_to_evidence(self, page: dict[str, Any]) -> Evidence:
        page_id = page["id"]
        properties: dict[str, Any] = page.get("properties", {}) or {}
        block_texts = self._fetch_block_texts(page_id)

        props_text = _flatten_properties(properties)
        blocks_text = "\n".join(block_texts)
        text = f"{props_text}\n\n{blocks_text}" if blocks_text else props_text

        # The hash is over flattened, human-readable values rather than the raw
        # property objects: Notion mutates unrelated bookkeeping fields (ids,
        # cursors) on every response, which would make the hash change even
        # when nothing a human would call "the content" has changed.
        hashed_payload = {
            "properties": {name: _plain(prop) for name, prop in properties.items()},
            "blocks": block_texts,
        }

        pointer = SourcePointer(
            app=self.name,
            resource_uri=page.get("url") or f"https://notion.so/{page_id}",
            locator={"id": page_id},
            content_hash=content_hash(hashed_payload),
        )

        author = None
        status = ""
        for prop in properties.values():
            if prop.get("type") == "people" and not author:
                rendered = _plain(prop)
                author = rendered or None
            if prop.get("type") == "status" and not status:
                status = _plain(prop)

        return Evidence(
            id=page_id,
            pointer=pointer,
            text=text,
            author=author,
            occurred_at=_parse_dt(page.get("last_edited_time")),
            labels=(status,) if status else (),
            raw=page,
        )

    # -- write helpers --------------------------------------------------------

    def _act_append_block(self, action: Action) -> ActionReceipt:
        page_id = action.target["page_id"]
        block_type = action.payload.get("block_type", "callout")
        text = action.payload["text"]
        block = _build_block(block_type, text)
        resp = self._transport("PATCH", f"/blocks/{page_id}/children", json={"children": [block]})
        created = (resp.get("results") or [{}])[0]
        block_id = created.get("id", "")
        return ActionReceipt(
            action_id=f"notion-append-{block_id or page_id}",
            action=action,
            result={"page_id": page_id, "block_id": block_id, "block_type": block_type},
            # Purely additive: there is no prior value for a block that did not
            # exist before this call, so undo is an edit-in-place, not a restore.
            prior_state=None,
        )

    def _act_set_property(self, action: Action) -> ActionReceipt:
        page_id = action.target["page_id"]
        prop_name = action.payload["property"]
        new_value = action.payload["value"]

        # Read-before-write: this GET is what makes undo() possible at all, and
        # what lets the write use the property's *real* type rather than trust
        # whatever the caller assumed it was.
        page = self._transport("GET", f"/pages/{page_id}")
        if _is_missing_or_archived(page):
            raise ValueError(f"cannot set a property on missing/archived page {page_id!r}")
        current = (page.get("properties") or {}).get(prop_name)
        if current is None:
            raise KeyError(f"page {page_id!r} has no property {prop_name!r}")
        prop_type = current["type"]
        prior_value = _plain(current)

        self._transport(
            "PATCH",
            f"/pages/{page_id}",
            json={"properties": {prop_name: _property_write_payload(prop_type, new_value)}},
        )

        return ActionReceipt(
            action_id=f"notion-setprop-{page_id}-{prop_name}",
            action=action,
            result={"page_id": page_id, "property": prop_name, "new_value": new_value},
            prior_state={"property": prop_name, "type": prop_type, "value": prior_value},
        )

    def _act_create_page(self, action: Action) -> ActionReceipt:
        parent = action.payload.get("parent") or {"database_id": action.target["database_id"]}
        properties = action.payload.get("properties", {})
        resp = self._transport("POST", "/pages", json={"parent": parent, "properties": properties})
        return ActionReceipt(
            action_id=f"notion-create-{resp.get('id', '')}",
            action=action,
            result={"page_id": resp.get("id", ""), "url": resp.get("url", "")},
            # Purely additive: a newly created page has no prior state to
            # restore. undo() archives it instead of restoring anything.
            prior_state=None,
        )

    def _undo_append_block(self, receipt: ActionReceipt) -> ActionReceipt:
        block_id = receipt.result.get("block_id")
        block_type = receipt.result.get("block_type", "callout")
        if block_id:
            original_text = receipt.action.payload.get("text", "")
            marker = f" [retracted by Walnut · action {receipt.action_id}]"
            rich_text = [
                {
                    "type": "text",
                    "text": {"content": original_text + marker},
                    "annotations": {"strikethrough": True},
                }
            ]
            self._transport("PATCH", f"/blocks/{block_id}", json={block_type: {"rich_text": rich_text}})
        return _mark_undone(receipt)

    def _undo_set_property(self, receipt: ActionReceipt) -> ActionReceipt:
        prior = receipt.prior_state or {}
        page_id = receipt.action.target["page_id"]
        prop_name = prior.get("property", receipt.action.payload.get("property"))
        prop_type = prior.get("type")
        prior_value = prior.get("value", "")
        self._transport(
            "PATCH",
            f"/pages/{page_id}",
            json={"properties": {prop_name: _property_write_payload(prop_type, prior_value)}},
        )
        return _mark_undone(receipt)

    def _undo_create_page(self, receipt: ActionReceipt) -> ActionReceipt:
        page_id = receipt.result.get("page_id")
        if page_id:
            self._transport("PATCH", f"/pages/{page_id}", json={"archived": True})
        return _mark_undone(receipt)
