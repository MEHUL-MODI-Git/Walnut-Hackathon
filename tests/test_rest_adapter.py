"""Conformance and behavioural tests for the generic REST/JSON adapter.

No internal API exists in this environment to point `RESTAdapter` at, so
`FakeWikiTransport` below is not a mocking convenience — it is the only evidence this
adapter behaves correctly at all, exactly as `tests/test_github_adapter.py` explains
for the GitHub adapter's fake. It stands in for a small internal-wiki API: a
collection of articles, each annotatable, each deletable-by-annotation-id.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

import pytest

from walnut.adapters.rest import RESTAdapter, RESTNotFound, _dig
from walnut.conformance import run_conformance
from walnut.contract import Action, ActionTier, SourcePointer

_BASE_URL = "https://wiki.internal.example"


class FakeWikiTransport:
    """A minimal, stateful stand-in for an internal wiki's REST API.

    Records every call it receives (`self.calls`) so tests can assert on request
    shape and, crucially, prove a rejected malicious id never reached the network at
    all. `wrap_records` toggles between the two response shapes the adapter must
    handle: a bare JSON array, and an object with the array nested under a
    `records_key` path (`data.items` here).
    """

    def __init__(self, wrap_records: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.wrap_records = wrap_records

        self.articles: dict[str, dict[str, Any]] = {
            "art-1": {
                "id": "art-1",
                "title": "Deploying the export worker",
                "body": "Run `make deploy-export` from the release branch. Holding "
                "for QA sign-off before promoting to prod.",
                "author": {"name": "priya"},
                "updated_at": "2026-09-01T10:00:00Z",
                "url": "https://wiki.internal.example/articles/art-1",
            },
            "art-2": {
                "id": "art-2",
                "title": "On-call rotation",
                "body": "The on-call rotation runs weekly, handoff Mondays at 10am.",
                "author": {"name": "amir"},
                "updated_at": "2026-09-05T08:30:00Z",
                # deliberately no "url" field: exercises the uri_template fallback
            },
        }
        self._next_annotation_id = 500
        self.annotations: dict[int, dict[str, Any]] = {}

    def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url, kwargs))
        assert url.startswith(_BASE_URL), f"transport received a non-base url: {url!r}"
        path = url[len(_BASE_URL) :]
        parts = [p for p in path.split("/") if p]

        if method == "GET" and path == "/api/articles":
            records = list(self.articles.values())
            if self.wrap_records:
                return {"data": {"items": records}, "page": 1}
            return records

        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "articles"]:
            article_id = unquote(parts[2])
            if article_id not in self.articles:
                raise RESTNotFound(url)
            return self.articles[article_id]

        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ["api", "articles"]
            and parts[3] == "annotate"
        ):
            article_id = unquote(parts[2])
            if article_id not in self.articles:
                raise RESTNotFound(url)
            annotation_id = self._next_annotation_id
            self._next_annotation_id += 1
            body = kwargs.get("json", {})
            self.annotations[annotation_id] = {"article_id": article_id, **body}
            return {"id": annotation_id, "article_id": article_id}

        if method == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "annotations"]:
            annotation_id = int(unquote(parts[2]))
            if annotation_id not in self.annotations:
                raise RESTNotFound(url)
            del self.annotations[annotation_id]
            return {}

        raise AssertionError(f"FakeWikiTransport has no route for {method} {path}")


# -- fixtures -----------------------------------------------------------------


@pytest.fixture
def transport() -> FakeWikiTransport:
    return FakeWikiTransport()


def _make_adapter(transport: FakeWikiTransport, **overrides: Any) -> RESTAdapter:
    config: dict[str, Any] = dict(
        name="internal_wiki",
        base_url=_BASE_URL,
        list_path="/api/articles",
        item_path="/api/articles/{id}",
        records_key=None,
        id_field="id",
        text_fields=["title", "body"],
        author_field="author.name",
        timestamp_field="updated_at",
        uri_field="url",
        uri_template="https://wiki.internal.example/articles/{id}",
        annotate_path="/api/articles/{id}/annotate",
        delete_path="/api/annotations/{id}",
        transport=transport,
    )
    config.update(overrides)
    return RESTAdapter(**config)


@pytest.fixture
def adapter(transport: FakeWikiTransport) -> RESTAdapter:
    return _make_adapter(transport)


# -- conformance ----------------------------------------------------------------


def test_rest_adapter_conforms(adapter: RESTAdapter) -> None:
    report = run_conformance(
        adapter,
        write_target={
            "operation": "annotate",
            "target": {"id": "art-1"},
            "payload": {"note": "walnut conformance"},
            # Annotating is additive (a new annotation, not an overwrite of the
            # article), so there is no prior_state to capture or restore.
            "overwrites": False,
        },
    )
    assert report.ok, report.render()


# -- _dig -----------------------------------------------------------------------


def test_dig_nested_and_missing_paths() -> None:
    obj = {"data": {"items": [1, 2, 3], "author": {"name": "priya"}}}
    assert _dig(obj, "data.items") == [1, 2, 3]
    assert _dig(obj, "data.author.name") == "priya"
    # missing at the leaf
    assert _dig(obj, "data.author.email") is None
    # missing partway through
    assert _dig(obj, "data.missing.deeper") is None
    # missing at the root
    assert _dig(obj, "nope") is None
    # walking through a non-dict (a list) rather than raising
    assert _dig(obj, "data.items.0") is None
    # empty object
    assert _dig({}, "a.b.c") is None


# -- response shapes --------------------------------------------------------------


def test_fetch_handles_bare_array_response(transport: FakeWikiTransport) -> None:
    adapter = _make_adapter(transport, records_key=None)
    records = adapter.fetch(limit=10)
    ids = {e.id for e in records}
    assert ids == {"internal_wiki:art-1", "internal_wiki:art-2"}


def test_fetch_handles_nested_records_key_response() -> None:
    transport = FakeWikiTransport(wrap_records=True)
    adapter = _make_adapter(transport, records_key="data.items")
    records = adapter.fetch(limit=10)
    ids = {e.id for e in records}
    assert ids == {"internal_wiki:art-1", "internal_wiki:art-2"}


def test_fetch_honours_limit(adapter: RESTAdapter) -> None:
    assert len(adapter.fetch(limit=1)) == 1


# -- evidence shape -----------------------------------------------------------


def test_evidence_carries_text_author_timestamp_and_uri(adapter: RESTAdapter) -> None:
    records = {e.id: e for e in adapter.fetch()}
    art1 = records["internal_wiki:art-1"]
    assert "Deploying the export worker" in art1.text
    assert "make deploy-export" in art1.text
    assert art1.author == "priya"
    assert art1.occurred_at is not None
    assert art1.occurred_at.year == 2026
    # uri_field is present on art-1, so it wins over the uri_template fallback
    assert art1.pointer.resource_uri == "https://wiki.internal.example/articles/art-1"

    art2 = records["internal_wiki:art-2"]
    # art-2 has no "url" field, so resolution falls through to uri_template
    assert art2.pointer.resource_uri == "https://wiki.internal.example/articles/art-2"
    assert art2.pointer.resource_uri.startswith("https://")


def test_resource_uri_synthesized_when_no_uri_field_or_template(
    transport: FakeWikiTransport,
) -> None:
    adapter = _make_adapter(transport, uri_field=None, uri_template=None)
    records = {e.id: e for e in adapter.fetch()}
    art1 = records["internal_wiki:art-1"]
    # Falls all the way back to base_url + item_path with the id substituted.
    assert art1.pointer.resource_uri == "https://wiki.internal.example/api/articles/art-1"
    assert art1.pointer.resource_uri.startswith("https://")


def test_text_falls_back_to_full_record_json_when_no_text_fields_configured(
    transport: FakeWikiTransport,
) -> None:
    adapter = _make_adapter(transport, text_fields=None)
    records = {e.id: e for e in adapter.fetch()}
    art1 = records["internal_wiki:art-1"]
    assert art1.text is not None
    assert "Deploying the export worker" in art1.text  # present in the raw record


# -- hashing --------------------------------------------------------------------


def test_content_hash_is_stable_across_repeated_fetches(adapter: RESTAdapter) -> None:
    first = {e.id: e for e in adapter.fetch()}
    second = {e.id: e for e in adapter.fetch()}
    assert first.keys() == second.keys()
    for evidence_id in first:
        assert (
            first[evidence_id].pointer.content_hash
            == second[evidence_id].pointer.content_hash
        )


def test_content_hash_changes_when_the_record_is_edited(
    adapter: RESTAdapter, transport: FakeWikiTransport
) -> None:
    before = adapter.resolve(
        SourcePointer(
            app=adapter.name,
            resource_uri=f"{_BASE_URL}/api/articles/art-1",
            locator={"id": "art-1"},
            content_hash="0" * 64,
        )
    )
    assert before is not None

    transport.articles["art-1"]["body"] = "Completely rewritten deployment steps."

    after = adapter.resolve(
        SourcePointer(
            app=adapter.name,
            resource_uri=f"{_BASE_URL}/api/articles/art-1",
            locator={"id": "art-1"},
            content_hash="0" * 64,
        )
    )
    assert after is not None
    assert after.pointer.content_hash != before.pointer.content_hash


# -- resolve() --------------------------------------------------------------------


def test_resolve_returns_none_on_404(adapter: RESTAdapter) -> None:
    ghost = SourcePointer(
        app=adapter.name,
        resource_uri=f"{_BASE_URL}/api/articles/does-not-exist",
        locator={"id": "does-not-exist"},
        content_hash="0" * 64,
    )
    assert adapter.resolve(ghost) is None


def test_resolve_returns_none_without_item_path(transport: FakeWikiTransport) -> None:
    adapter = _make_adapter(transport, item_path=None)
    pointer = SourcePointer(
        app=adapter.name,
        resource_uri=f"{_BASE_URL}/api/articles/art-1",
        locator={"id": "art-1"},
        content_hash="0" * 64,
    )
    assert adapter.resolve(pointer) is None


# -- path traversal / id safety ----------------------------------------------------


def test_safe_id_segment_rejects_path_traversal(adapter: RESTAdapter) -> None:
    with pytest.raises(ValueError, match="traversal|escape"):
        adapter._safe_id_segment("../../admin")
    with pytest.raises(ValueError, match="traversal|escape"):
        adapter._safe_id_segment("/etc/passwd")


def test_resolve_with_traversal_id_returns_none_and_never_calls_transport(
    adapter: RESTAdapter, transport: FakeWikiTransport
) -> None:
    malicious = SourcePointer(
        app=adapter.name,
        resource_uri=f"{_BASE_URL}/api/articles/art-1",  # a plausible-looking pointer
        locator={"id": "../../admin"},
        content_hash="0" * 64,
    )
    calls_before = len(transport.calls)
    assert adapter.resolve(malicious) is None
    # The id was rejected before it ever became part of a URL handed to the
    # transport — proving the rejection happens before any network call, not just
    # that the (possibly permissive) fake happened to 404 it.
    assert len(transport.calls) == calls_before


def test_normal_ids_with_special_characters_are_percent_encoded(
    transport: FakeWikiTransport,
) -> None:
    transport.articles["a b/c"] = {
        "id": "a b/c",
        "title": "weird id",
        "body": "x",
        "author": {"name": "n"},
        "updated_at": "2026-09-01T00:00:00Z",
    }
    adapter = _make_adapter(transport)
    pointer = SourcePointer(
        app=adapter.name,
        resource_uri=f"{_BASE_URL}/api/articles/a-b-c",
        locator={"id": "a b/c"},
        content_hash="0" * 64,
    )
    evidence = adapter.resolve(pointer)
    assert evidence is not None
    method, url, _ = transport.calls[-1]
    assert method == "GET"
    # space and "/" are both percent-encoded, so "a b/c" cannot be read as two path
    # segments — it stays one opaque, safely-encoded id.
    assert "a%20b%2Fc" in url


# -- capabilities / act / undo ---------------------------------------------------


def test_capabilities_declares_annotate_as_trivial(adapter: RESTAdapter) -> None:
    caps = adapter.capabilities()
    assert caps.app == adapter.name
    assert caps.operations == {"annotate": ActionTier.TRIVIAL}


def test_annotate_and_undo_roundtrip(adapter: RESTAdapter, transport: FakeWikiTransport) -> None:
    action = Action(
        app=adapter.name,
        operation="annotate",
        target={"id": "art-1"},
        payload={"note": "flagged by walnut"},
        justified_by=("internal_wiki:art-1",),
    )
    receipt = adapter.act(action)
    assert receipt.result["annotation_id"] in transport.annotations
    assert receipt.prior_state is None  # additive write, nothing to overwrite

    undone = adapter.undo(receipt)
    assert undone.is_undone
    assert receipt.result["annotation_id"] not in transport.annotations


def test_read_only_adapter_raises_clear_error_on_act(transport: FakeWikiTransport) -> None:
    read_only = _make_adapter(transport, annotate_path=None)
    action = Action(
        app=read_only.name,
        operation="annotate",
        target={"id": "art-1"},
        payload={"note": "x"},
        justified_by=("internal_wiki:art-1",),
    )
    with pytest.raises(RuntimeError, match="read-only"):
        read_only.act(action)


def test_no_delete_path_raises_clear_error_on_undo(
    transport: FakeWikiTransport,
) -> None:
    no_undo = _make_adapter(transport, delete_path=None)
    action = Action(
        app=no_undo.name,
        operation="annotate",
        target={"id": "art-1"},
        payload={"note": "x"},
        justified_by=("internal_wiki:art-1",),
    )
    receipt = no_undo.act(action)
    with pytest.raises(RuntimeError, match="does not support undo"):
        no_undo.undo(receipt)


def test_act_rejects_unknown_operation(adapter: RESTAdapter) -> None:
    action = Action(
        app=adapter.name,
        operation="delete_everything",
        target={"id": "art-1"},
        payload={},
        justified_by=("internal_wiki:art-1",),
    )
    with pytest.raises(KeyError):
        adapter.act(action)
