"""Conformance and unit tests for the Notion adapter.

There are no Notion credentials in this environment, so `FakeNotionTransport`
below is the only thing standing between this adapter and "looks right but has
never actually been exercised." It plays back the same JSON shapes the real
Notion REST API returns — including the deeply nested, type-dependent property
objects that `walnut.adapters.notion._plain()` exists to flatten — so the
conformance suite and the unit tests below run against something that fails the
same way a real integration would if `_plain()`, the hashing, or the
read-before-write discipline in `act()` were wrong.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from walnut.adapters.notion import NotionAdapter, _plain
from walnut.conformance import run_conformance
from walnut.contract import Action, ActionReceipt, SourcePointer

# ---------------------------------------------------------------------------
# Fixture data — one Notion database ("db1") holding two pages. nt001 carries
# one of every property type the adapter's _plain() branches on, plus child
# blocks, so a single fetch exercises the whole flattening path end to end.
# ---------------------------------------------------------------------------


def _make_page(page_id: str) -> dict[str, Any]:
    return {
        "object": "page",
        "id": page_id,
        "created_time": "2026-09-01T12:00:00.000Z",
        "last_edited_time": "2026-09-03T15:30:00.000Z",
        "archived": False,
        "url": f"https://www.notion.so/Export-v2-Spec-{page_id}",
        "properties": {
            "Name": {
                "id": "title",
                "type": "title",
                "title": [{"type": "text", "plain_text": "Export v2 \u2014 Spec"}],
            },
            "Summary": {
                "id": "sum1",
                "type": "rich_text",
                "rich_text": [{"type": "text", "plain_text": "Async export rewrite"}],
            },
            "Team": {
                "id": "team1",
                "type": "select",
                "select": {"name": "Engineering"},
            },
            "Status": {
                "id": "stat1",
                "type": "status",
                "status": {"name": "Shipped"},
            },
            "Due": {
                "id": "due1",
                "type": "date",
                "date": {"start": "2026-09-05"},
            },
            "Owner": {
                "id": "own1",
                "type": "people",
                "people": [{"id": "u1", "name": "Marcus Chen"}],
            },
        },
    }


def _make_blocks() -> list[dict[str, Any]]:
    return [
        {
            "object": "block",
            "id": "blk-h1",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "plain_text": "What shipped"}]},
        },
        {
            "object": "block",
            "id": "blk-b1",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [{"type": "text", "plain_text": "Async job queue (ENG-405)"}]
            },
        },
        {
            "object": "block",
            "id": "blk-p1",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "plain_text": "QA pass complete."}]},
        },
    ]


class FakeNotionTransport:
    """A minimal in-memory stand-in for `POST https://api.notion.com/v1/...`.

    Dispatches on `(method, path)` the same way the real API's router does, and
    mutates its own store on PATCH/POST the same way the real API would — so
    that hash-stability, hash-on-edit, and undo tests observe real state
    changes rather than a mocked-out no-op.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._pages: dict[str, dict[str, Any]] = {
            "nt001": _make_page("nt001"),
            "nt002": _make_page("nt002"),
        }
        self._blocks: dict[str, list[dict[str, Any]]] = {
            "nt001": _make_blocks(),
            "nt002": [],
        }
        self._block_owner: dict[str, str] = {}
        for page_id, blocks in self._blocks.items():
            for block in blocks:
                self._block_owner[block["id"]] = page_id
        self._databases = {
            "db1": {"object": "database", "id": "db1", "title": [{"plain_text": "Docs"}]}
        }
        self._db_pages = {"db1": ["nt001", "nt002"]}
        self._next_block_id = 0
        self._next_page_id = 0

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path, kwargs))
        body = kwargs.get("json", {}) or {}

        if method == "POST" and path == "/search":
            return self._search(body)
        if method == "POST" and path == "/pages":
            return self._create_page(body)

        if path.count("/") == 2 and path.startswith("/pages/"):
            page_id = path.split("/")[2]
            if method == "GET":
                return self._get_page(page_id)
            if method == "PATCH":
                return self._patch_page(page_id, body)

        if path.startswith("/databases/") and path.endswith("/query") and method == "POST":
            database_id = path.split("/")[2]
            return self._query_database(database_id)

        if path.startswith("/blocks/") and path.endswith("/children"):
            block_id = path.split("/")[2]
            if method == "GET":
                return self._get_children(block_id)
            if method == "PATCH":
                return self._append_children(block_id, body)

        if path.startswith("/blocks/") and method == "PATCH":
            block_id = path.split("/")[2]
            return self._patch_block(block_id, body)

        raise AssertionError(f"FakeNotionTransport: unhandled {method} {path}")

    # -- routes ---------------------------------------------------------

    def _search(self, body: dict[str, Any]) -> dict[str, Any]:
        value = (body.get("filter") or {}).get("value")
        if value == "database":
            results = [copy.deepcopy(d) for d in self._databases.values()]
        else:
            results = [copy.deepcopy(p) for p in self._pages.values() if not p.get("archived")]
        return {"object": "list", "results": results, "has_more": False, "next_cursor": None}

    def _query_database(self, database_id: str) -> dict[str, Any]:
        ids = self._db_pages.get(database_id, [])
        results = [
            copy.deepcopy(self._pages[i])
            for i in ids
            if i in self._pages and not self._pages[i].get("archived")
        ]
        return {"object": "list", "results": results, "has_more": False, "next_cursor": None}

    def _get_page(self, page_id: str) -> dict[str, Any]:
        page = self._pages.get(page_id)
        if page is None:
            return {"object": "error", "status": 404, "code": "object_not_found"}
        return copy.deepcopy(page)

    def _patch_page(self, page_id: str, body: dict[str, Any]) -> dict[str, Any]:
        page = self._pages.get(page_id)
        if page is None:
            return {"object": "error", "status": 404, "code": "object_not_found"}
        if "archived" in body:
            page["archived"] = body["archived"]
        for name, update in (body.get("properties") or {}).items():
            ptype = next(iter(update))
            existing = page["properties"].get(name, {"id": ptype})
            page["properties"][name] = {"id": existing.get("id", ptype), "type": ptype, ptype: update[ptype]}
        page["last_edited_time"] = "2026-09-13T00:00:00.000Z"
        return copy.deepcopy(page)

    def _create_page(self, body: dict[str, Any]) -> dict[str, Any]:
        self._next_page_id += 1
        page_id = f"new{self._next_page_id}"
        page = {
            "object": "page",
            "id": page_id,
            "created_time": "2026-09-13T00:00:00.000Z",
            "last_edited_time": "2026-09-13T00:00:00.000Z",
            "archived": False,
            "url": f"https://www.notion.so/{page_id}",
            "properties": body.get("properties", {}),
        }
        self._pages[page_id] = page
        self._blocks[page_id] = []
        return copy.deepcopy(page)

    def _get_children(self, page_id: str) -> dict[str, Any]:
        blocks = self._blocks.get(page_id, [])
        return {
            "object": "list",
            "results": [copy.deepcopy(b) for b in blocks],
            "has_more": False,
            "next_cursor": None,
        }

    def _append_children(self, page_id: str, body: dict[str, Any]) -> dict[str, Any]:
        created = []
        for child in body.get("children", []):
            self._next_block_id += 1
            block_id = f"blk-new{self._next_block_id}"
            block = {"object": "block", "id": block_id, **child}
            self._blocks.setdefault(page_id, []).append(block)
            self._block_owner[block_id] = page_id
            created.append(copy.deepcopy(block))
        return {"object": "list", "results": created}

    def _patch_block(self, block_id: str, body: dict[str, Any]) -> dict[str, Any]:
        page_id = self._block_owner.get(block_id)
        if page_id is None:
            return {"object": "error", "status": 404, "code": "object_not_found"}
        for block in self._blocks[page_id]:
            if block["id"] == block_id:
                block.update(body)
                return copy.deepcopy(block)
        return {"object": "error", "status": 404, "code": "object_not_found"}


@pytest.fixture()
def transport() -> FakeNotionTransport:
    return FakeNotionTransport()


@pytest.fixture()
def adapter(transport: FakeNotionTransport) -> NotionAdapter:
    return NotionAdapter(token="fake-token", transport=transport)


WRITE_TARGET = {
    "operation": "set_property",
    "target": {"page_id": "nt001"},
    "payload": {"property": "Status", "value": "Disputed"},
}


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


def test_notion_adapter_conforms(adapter: NotionAdapter) -> None:
    report = run_conformance(adapter, write_target=WRITE_TARGET)
    assert report.ok, report.render()


# ---------------------------------------------------------------------------
# _plain() — every property type the adapter is supposed to understand
# ---------------------------------------------------------------------------


def test_plain_title() -> None:
    prop = {"type": "title", "title": [{"plain_text": "Export v2"}, {"plain_text": " Spec"}]}
    assert _plain(prop) == "Export v2 Spec"


def test_plain_rich_text() -> None:
    prop = {"type": "rich_text", "rich_text": [{"plain_text": "hello "}, {"plain_text": "world"}]}
    assert _plain(prop) == "hello world"


def test_plain_select() -> None:
    prop = {"type": "select", "select": {"name": "Engineering"}}
    assert _plain(prop) == "Engineering"


def test_plain_status() -> None:
    prop = {"type": "status", "status": {"name": "Shipped"}}
    assert _plain(prop) == "Shipped"


def test_plain_date() -> None:
    assert _plain({"type": "date", "date": {"start": "2026-09-05"}}) == "2026-09-05"
    ranged = {"type": "date", "date": {"start": "2026-09-01", "end": "2026-09-05"}}
    assert _plain(ranged) == "2026-09-01 \u2192 2026-09-05"


def test_plain_handles_empty_and_none() -> None:
    assert _plain(None) == ""
    assert _plain({"type": "select", "select": None}) == ""
    assert _plain({"type": "rich_text", "rich_text": []}) == ""


def test_plain_people_and_multi_select() -> None:
    people = {"type": "people", "people": [{"id": "u1", "name": "Marcus Chen"}]}
    assert _plain(people) == "Marcus Chen"
    multi = {"type": "multi_select", "multi_select": [{"name": "a"}, {"name": "b"}]}
    assert _plain(multi) == "a, b"


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def test_hash_stable_across_repeated_fetch(adapter: NotionAdapter) -> None:
    first = {e.id: e for e in adapter.fetch(scope="db1")}
    second = {e.id: e for e in adapter.fetch(scope="db1")}
    assert first["nt001"].pointer.content_hash == second["nt001"].pointer.content_hash


def test_hash_changes_when_page_is_edited(adapter: NotionAdapter, transport: FakeNotionTransport) -> None:
    before = adapter.resolve(SourcePointer(app="notion", resource_uri="x://y", locator={"id": "nt001"}, content_hash="0" * 64))
    assert before is not None

    action = Action(
        app="notion",
        operation="set_property",
        target={"page_id": "nt001"},
        payload={"property": "Status", "value": "Disputed"},
        justified_by=("test:evidence",),
    )
    adapter.act(action)

    after = adapter.resolve(before.pointer)
    assert after is not None
    assert after.pointer.content_hash != before.pointer.content_hash


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


def test_resolve_missing_page_returns_none(adapter: NotionAdapter) -> None:
    ghost = SourcePointer(
        app="notion",
        resource_uri="https://example.invalid/nope",
        locator={"id": "does-not-exist"},
        content_hash="0" * 64,
    )
    assert adapter.resolve(ghost) is None


def test_resolve_archived_page_returns_none(adapter: NotionAdapter, transport: FakeNotionTransport) -> None:
    transport._pages["nt001"]["archived"] = True
    pointer = SourcePointer(app="notion", resource_uri="x://y", locator={"id": "nt001"}, content_hash="0" * 64)
    assert adapter.resolve(pointer) is None


# ---------------------------------------------------------------------------
# set_property: read-before-write, and undo restores the prior value
# ---------------------------------------------------------------------------


def test_set_property_captures_prior_value(adapter: NotionAdapter) -> None:
    action = Action(
        app="notion",
        operation="set_property",
        target={"page_id": "nt001"},
        payload={"property": "Status", "value": "Disputed"},
        justified_by=("test:evidence",),
    )
    receipt = adapter.act(action)
    assert receipt.prior_state == {"property": "Status", "type": "status", "value": "Shipped"}

    fetched = adapter.resolve(SourcePointer(app="notion", resource_uri="x", locator={"id": "nt001"}, content_hash="0" * 64))
    assert fetched is not None
    assert "Status: Disputed" in fetched.text


def test_undo_set_property_restores_prior_value(adapter: NotionAdapter) -> None:
    action = Action(
        app="notion",
        operation="set_property",
        target={"page_id": "nt001"},
        payload={"property": "Status", "value": "Disputed"},
        justified_by=("test:evidence",),
    )
    receipt = adapter.act(action)
    undone = adapter.undo(receipt)
    assert undone.is_undone

    fetched = adapter.resolve(SourcePointer(app="notion", resource_uri="x", locator={"id": "nt001"}, content_hash="0" * 64))
    assert fetched is not None
    assert "Status: Shipped" in fetched.text


# ---------------------------------------------------------------------------
# append_block: additive write, undo retracts rather than erases
# ---------------------------------------------------------------------------


def test_append_block_is_additive_and_undo_retracts(adapter: NotionAdapter, transport: FakeNotionTransport) -> None:
    action = Action(
        app="notion",
        operation="append_block",
        target={"page_id": "nt002"},
        payload={"text": "Disputed by Walnut: see ENG-412."},
        justified_by=("test:evidence",),
    )
    receipt = adapter.act(action)
    assert receipt.prior_state is None
    block_id = receipt.result["block_id"]
    assert transport._block_owner[block_id] == "nt002"

    undone = adapter.undo(receipt)
    assert undone.is_undone
    patched_block = next(b for b in transport._blocks["nt002"] if b["id"] == block_id)
    rich_text = patched_block["callout"]["rich_text"][0]
    assert "[retracted by Walnut" in rich_text["text"]["content"]
    assert rich_text["annotations"]["strikethrough"] is True


# ---------------------------------------------------------------------------
# create_page: additive write, undo archives rather than deletes
# ---------------------------------------------------------------------------


def test_create_page_and_undo_archives(adapter: NotionAdapter, transport: FakeNotionTransport) -> None:
    action = Action(
        app="notion",
        operation="create_page",
        target={"database_id": "db1"},
        payload={"properties": {"Name": {"title": [{"type": "text", "text": {"content": "New doc"}}]}}},
        justified_by=("test:evidence",),
    )
    receipt = adapter.act(action)
    assert receipt.prior_state is None
    page_id = receipt.result["page_id"]
    assert page_id in transport._pages
    assert transport._pages[page_id]["archived"] is False

    undone = adapter.undo(receipt)
    assert undone.is_undone
    assert transport._pages[page_id]["archived"] is True


# ---------------------------------------------------------------------------
# Transport contract: Notion-Version header
# ---------------------------------------------------------------------------


def test_default_transport_always_sends_notion_version_header() -> None:
    built = NotionAdapter._build_default_transport("secret-token")
    assert built.headers["Notion-Version"] == "2022-06-28"
    assert built.headers["Authorization"] == "Bearer secret-token"


def test_default_transport_omits_authorization_without_token() -> None:
    built = NotionAdapter._build_default_transport(None)
    assert built.headers["Notion-Version"] == "2022-06-28"
    assert "Authorization" not in built.headers
