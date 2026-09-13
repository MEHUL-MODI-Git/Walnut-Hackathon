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


# -- declared operations -----------------------------------------------------


class FakeOpsTransport:
    """A stand-in for an internal service that has a real write surface.

    `FakeWikiTransport` models the one write shape this adapter can assume for an
    unknown API (annotate). This one models the shape a source has to *tell* Walnut
    about: holds that can be placed and released, and tickets whose created handle is
    not called `id`. Like the wiki fake it records every call, so a test can prove
    both what went over the wire and — for a rejected id — that nothing did.

    `POST /api/tickets` deliberately returns a decoy `id` alongside `ticket_ref`. An
    adapter that reached for the default field name instead of the configured
    `result_id_field` would address its undo at the decoy, and the assertions below
    would see it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.holds: dict[str, dict[str, Any]] = {}
        self.tickets: dict[str, dict[str, Any]] = {}
        self._next_hold = 7
        self._next_ticket = 9

    def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url, kwargs))
        assert url.startswith(_BASE_URL), f"transport received a non-base url: {url!r}"
        path = url[len(_BASE_URL) :]
        parts = [p for p in path.split("/") if p]

        if method == "POST" and path == "/api/holds":
            hold_id = f"hold-{self._next_hold}"
            self._next_hold += 1
            self.holds[hold_id] = dict(kwargs.get("json") or {})
            return {"id": hold_id, "status": "held"}

        if method == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "holds"]:
            hold_id = unquote(parts[2])
            if hold_id not in self.holds:
                raise RESTNotFound(url)
            del self.holds[hold_id]
            return {}

        if method == "POST" and path == "/api/tickets":
            ticket_ref = f"T-{self._next_ticket}"
            self._next_ticket += 1
            self.tickets[ticket_ref] = dict(kwargs.get("json") or {})
            return {"ticket_ref": ticket_ref, "id": "decoy-not-the-handle"}

        if method == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "tickets"]:
            ticket_ref = unquote(parts[2])
            if ticket_ref not in self.tickets:
                raise RESTNotFound(url)
            del self.tickets[ticket_ref]
            return {}

        raise AssertionError(f"FakeOpsTransport has no route for {method} {path}")


_DECLARED_OPERATIONS: dict[str, dict[str, Any]] = {
    "place_hold": {
        "method": "POST",
        "path": "/api/holds",
        "tier": "INTERNAL",
        "undo": {"method": "DELETE", "path": "/api/holds/{id}"},
        "result_id_field": "id",
    },
    "release_hold": {
        "method": "DELETE",
        "path": "/api/holds/{id}",
        "tier": "GATED",
    },
    "open_ticket": {
        "method": "POST",
        "path": "/api/tickets",
        "tier": "INTERNAL",
        "undo": {"method": "DELETE", "path": "/api/tickets/{id}"},
        "result_id_field": "ticket_ref",
    },
}


@pytest.fixture
def ops_transport() -> FakeOpsTransport:
    return FakeOpsTransport()


@pytest.fixture
def ops_adapter(ops_transport: FakeOpsTransport) -> RESTAdapter:
    return _make_adapter(ops_transport, operations=_DECLARED_OPERATIONS)


def _ops_action(adapter: RESTAdapter, operation: str, **kw: Any) -> Action:
    return Action(
        app=adapter.name,
        operation=operation,
        target=kw.get("target", {}),
        payload=kw.get("payload", {}),
        justified_by=("internal_wiki:art-1",),
    )


def test_declared_operations_appear_in_capabilities_beside_annotate(
    ops_adapter: RESTAdapter,
) -> None:
    """Guards the case where a declared operation is executable but invisible.

    The executor decides what needs approval from `capabilities()`. An operation
    missing from that map, or present at a tier the config did not choose, is an
    action taken under permission nobody granted it.
    """
    caps = ops_adapter.capabilities()
    assert caps.operations == {
        "annotate": ActionTier.TRIVIAL,
        "place_hold": ActionTier.INTERNAL,
        "release_hold": ActionTier.GATED,
        "open_ticket": ActionTier.INTERNAL,
    }
    assert caps.tier_of("release_hold") is ActionTier.GATED


def test_declared_tier_follows_the_config_and_is_not_inferred_from_the_method(
    ops_transport: FakeOpsTransport,
) -> None:
    """Guards against the tier being derived from the HTTP verb.

    `POST /api/holds` is INTERNAL at one customer and GATED at the next — the verb
    carries none of that. The same operation declared differently must report
    differently, or the declaration is decoration.
    """
    stricter = _make_adapter(
        ops_transport,
        operations={
            "place_hold": {"method": "POST", "path": "/api/holds", "tier": "GATED"},
        },
    )
    assert stricter.capabilities().operations["place_hold"] is ActionTier.GATED


def test_declared_act_sends_the_payload_as_json_to_the_configured_path(
    ops_adapter: RESTAdapter, ops_transport: FakeOpsTransport
) -> None:
    """Guards the wire shape of a declared write: verb, URL, and body together.

    A write that reaches the right URL by the wrong verb, or arrives with the body
    dropped, fails in the source system rather than here — after the ledger has
    already recorded that Walnut did it.
    """
    receipt = ops_adapter.act(
        _ops_action(ops_adapter, "place_hold", payload={"reason": "awaiting pharmacist review"})
    )

    method, url, kwargs = ops_transport.calls[-1]
    assert (method, url) == ("POST", f"{_BASE_URL}/api/holds")
    assert kwargs["json"] == {"reason": "awaiting pharmacist review"}
    assert ops_transport.holds["hold-7"] == {"reason": "awaiting pharmacist review"}
    assert receipt.result["id"] == "hold-7"
    assert receipt.prior_state is None


def test_declared_delete_addresses_the_record_and_sends_no_body(
    ops_adapter: RESTAdapter, ops_transport: FakeOpsTransport
) -> None:
    """Guards against a body riding along on a DELETE.

    Several real HTTP stacks and proxies reject or silently drop a DELETE with a
    body, so a `release_hold` that carried one would appear to succeed here and fail
    intermittently in front of a customer.
    """
    ops_adapter.act(_ops_action(ops_adapter, "place_hold", payload={"reason": "x"}))
    ops_adapter.act(
        _ops_action(
            ops_adapter,
            "release_hold",
            target={"id": "hold-7"},
            payload={"reason": "cleared by pharmacist"},
        )
    )

    method, url, kwargs = ops_transport.calls[-1]
    assert (method, url) == ("DELETE", f"{_BASE_URL}/api/holds/hold-7")
    assert kwargs == {}
    assert "hold-7" not in ops_transport.holds


def test_declared_operation_with_an_id_placeholder_refuses_a_target_without_an_id(
    ops_adapter: RESTAdapter, ops_transport: FakeOpsTransport
) -> None:
    """Guards against `/api/holds/{id}` being sent with the placeholder unfilled.

    Formatting a missing id would either raise deep inside the transport or, worse,
    produce a URL that addresses the collection — releasing every hold instead of one.
    """
    with pytest.raises(ValueError, match="must include 'id'"):
        ops_adapter.act(_ops_action(ops_adapter, "release_hold"))
    assert ops_transport.calls == []


def test_declared_operation_without_an_id_placeholder_needs_no_target(
    ops_adapter: RESTAdapter, ops_transport: FakeOpsTransport
) -> None:
    """Guards against a blanket id requirement on every declared write.

    Creating a hold has nothing to address yet — demanding a target id would make
    the whole create-shaped half of the mechanism unusable.
    """
    receipt = ops_adapter.act(_ops_action(ops_adapter, "place_hold", payload={"reason": "x"}))

    method, url, _ = ops_transport.calls[-1]
    assert (method, url) == ("POST", f"{_BASE_URL}/api/holds")
    assert "record_id" not in receipt.result
    assert receipt.action_id == "internal_wiki:place_hold:hold-7"


def test_undo_of_a_declared_operation_issues_the_configured_reversal(
    ops_adapter: RESTAdapter, ops_transport: FakeOpsTransport
) -> None:
    """Guards the promise `capabilities()` makes on behalf of a reversible write.

    An INTERNAL action is auto-executed on the understanding that it can be taken
    back. An undo that no-ops, or that reports success without reaching the source,
    turns that understanding into a false one.
    """
    receipt = ops_adapter.act(_ops_action(ops_adapter, "place_hold", payload={"reason": "x"}))
    assert "hold-7" in ops_transport.holds

    undone = ops_adapter.undo(receipt)

    method, url, kwargs = ops_transport.calls[-1]
    assert (method, url) == ("DELETE", f"{_BASE_URL}/api/holds/hold-7")
    assert kwargs == {}
    assert "hold-7" not in ops_transport.holds
    assert undone.is_undone
    assert undone.executed_at == receipt.executed_at


def test_undo_addresses_the_handle_named_by_result_id_field(
    ops_adapter: RESTAdapter, ops_transport: FakeOpsTransport
) -> None:
    """Guards against the undo reaching for `id` when the source names it otherwise.

    The ticket API returns both `ticket_ref` (the real handle) and an `id` that
    addresses nothing. Defaulting to `id` would send a DELETE that either 404s or,
    at a source where that id is meaningful, deletes the wrong record.
    """
    receipt = ops_adapter.act(_ops_action(ops_adapter, "open_ticket", payload={"summary": "s"}))
    assert receipt.result["ticket_ref"] == "T-9"

    ops_adapter.undo(receipt)

    method, url, _ = ops_transport.calls[-1]
    assert (method, url) == ("DELETE", f"{_BASE_URL}/api/tickets/T-9")
    assert ops_transport.tickets == {}


def test_undo_of_a_declared_operation_with_no_undo_config_says_so_plainly(
    ops_adapter: RESTAdapter, ops_transport: FakeOpsTransport
) -> None:
    """Guards against an irreversible write being quietly marked undone.

    `release_hold` declares no reversal. The failure mode worth preventing is a
    receipt that comes back `is_undone` while the hold is still released — a steward
    reading the audit trail would believe the system had put it back.
    """
    ops_adapter.act(_ops_action(ops_adapter, "place_hold", payload={"reason": "x"}))
    receipt = ops_adapter.act(
        _ops_action(ops_adapter, "release_hold", target={"id": "hold-7"})
    )
    calls_before = len(ops_transport.calls)

    with pytest.raises(RuntimeError, match="by hand"):
        ops_adapter.undo(receipt)

    assert not receipt.is_undone
    assert len(ops_transport.calls) == calls_before


def test_declared_operation_rejects_a_traversal_id_before_anything_is_sent(
    ops_adapter: RESTAdapter, ops_transport: FakeOpsTransport
) -> None:
    """Guards the traversal chokepoint on the path that did not exist when it was written.

    `_safe_id_segment` was introduced for `resolve()` and `annotate`. Declared
    operations are a second, later way for an id from an unknown system to become a
    URL segment — and this one is attached to a DELETE.
    """
    with pytest.raises(ValueError, match="traversal|escape"):
        ops_adapter.act(
            _ops_action(ops_adapter, "release_hold", target={"id": "../../etc/passwd"})
        )
    assert ops_transport.calls == []


# -- declared-operation validation, at construction ---------------------------


def test_unknown_tier_is_rejected_at_construction_and_names_the_valid_tiers(
    ops_transport: FakeOpsTransport,
) -> None:
    """Guards against a typo'd tier becoming a silent default.

    A misspelled `"INTERNALL"` that fell back to TRIVIAL would auto-execute a write
    that the customer meant to gate — and nothing downstream could tell.
    """
    with pytest.raises(ValueError, match="not one of") as excinfo:
        _make_adapter(
            ops_transport,
            operations={
                "place_hold": {"method": "POST", "path": "/api/holds", "tier": "INTERNALL"},
            },
        )
    message = str(excinfo.value)
    assert all(tier in message for tier in ("TRIVIAL", "INTERNAL", "GATED", "FORBIDDEN"))


def test_declaring_a_read_method_as_an_operation_is_rejected_at_construction(
    ops_transport: FakeOpsTransport,
) -> None:
    """Guards against a GET entering the action vocabulary.

    An operation is something the ledger records as a change to a source system. A
    read declared as one would produce an approval request, a receipt, and an undo
    for an event that never happened.
    """
    with pytest.raises(ValueError, match="explicit write method"):
        _make_adapter(
            ops_transport,
            operations={
                "read_holds": {"method": "GET", "path": "/api/holds", "tier": "TRIVIAL"},
            },
        )


def test_redeclaring_annotate_is_rejected_at_construction(
    ops_transport: FakeOpsTransport,
) -> None:
    """Guards against two definitions of one operation name.

    `act()` checks declared operations first, so a redeclared `annotate` would
    shadow the built-in one — and `capabilities()` would still report it at the
    built-in TRIVIAL tier while a different endpoint was being called.
    """
    with pytest.raises(ValueError, match="built in and cannot be redeclared"):
        _make_adapter(
            ops_transport,
            operations={
                "annotate": {"method": "POST", "path": "/api/notes", "tier": "GATED"},
            },
        )


def test_operation_without_a_path_is_rejected_at_construction(
    ops_transport: FakeOpsTransport,
) -> None:
    """Guards against a pathless operation surviving until someone invokes it.

    It would appear in `capabilities()`, be approved by a human, and only then fail —
    the failure landing on the steward who approved it rather than on the config.
    """
    with pytest.raises(ValueError, match="non-empty 'path'"):
        _make_adapter(
            ops_transport,
            operations={"place_hold": {"method": "POST", "tier": "INTERNAL"}},
        )


def test_undo_declared_without_a_path_is_rejected_at_construction(
    ops_transport: FakeOpsTransport,
) -> None:
    """Guards against an undo that exists in the config but cannot be performed.

    The presence of an `undo` key is what makes the operation look reversible. A
    half-written one is worse than none: it promises reversibility at approval time
    and discovers it has no URL only when someone tries to take the action back.
    """
    with pytest.raises(ValueError, match="'undo' with no path"):
        _make_adapter(
            ops_transport,
            operations={
                "place_hold": {
                    "method": "POST",
                    "path": "/api/holds",
                    "tier": "INTERNAL",
                    "undo": {"method": "DELETE"},
                },
            },
        )


# -- no-operations regression --------------------------------------------------


def test_annotate_is_untouched_when_no_operations_are_declared(
    adapter: RESTAdapter, transport: FakeWikiTransport
) -> None:
    """Guards the adapters already in production against the new code path.

    Every existing REST source was configured before declared operations existed and
    passes `operations=None`. The two branches added to `act()`, `undo()`, and
    `capabilities()` must leave the wire calls those sources make byte-identical.
    """
    assert adapter.capabilities().operations == {"annotate": ActionTier.TRIVIAL}

    receipt = adapter.act(
        Action(
            app=adapter.name,
            operation="annotate",
            target={"id": "art-1"},
            payload={"note": "flagged by walnut"},
            justified_by=("internal_wiki:art-1",),
        )
    )
    method, url, kwargs = transport.calls[-1]
    assert (method, url) == ("POST", f"{_BASE_URL}/api/articles/art-1/annotate")
    assert kwargs == {"json": {"note": "flagged by walnut"}}
    assert receipt.result["record_id"] == "art-1"
    assert receipt.action_id == "internal_wiki:annotate:art-1:500"

    adapter.undo(receipt)
    method, url, kwargs = transport.calls[-1]
    assert (method, url) == ("DELETE", f"{_BASE_URL}/api/annotations/500")
    assert kwargs == {}
    assert transport.annotations == {}
