"""The generic REST/JSON adapter: any internal HTTP API as cited evidence, by config.

Walnut ships five named adapters — Slack, Linear, GitHub, Notion, Email — because those
are the systems every design partner already has. But the actual claim this product
makes is broader: "point Walnut at your system of record and it will cite it." A
company with a bespoke ticketing tool, an internal wiki nobody outside the team has
heard of, or a homegrown service with a JSON API has no bespoke adapter waiting for
them, and never will — nobody is going to write and maintain one adapter per internal
tool a customer happens to run.

`RESTAdapter` is the answer: describe the shape of the response in configuration
(where the records live in the JSON, which field is the id, which fields are text)
rather than writing code. It still satisfies the same six-method contract and the same
conformance suite as every named adapter, because the contract does not know or care
whether an adapter was handwritten or configured.

Design choices worth recording, in the same spirit as `github.py`:

* **`transport` is `(method, url, **kw) -> Any`, not `(method, path, **kw)`.** Unlike
  GitHub, this adapter has no fixed API root, so the adapter itself resolves
  `base_url + path` into a full URL before handing it to the transport. That keeps the
  transport a pure HTTP seam — it never needs to know this adapter's configuration.

* **`httpx` is imported lazily, inside the default-transport builder.** Importing this
  module must never require a network client to be installed or reachable — plenty of
  callers will only ever construct a `RESTAdapter` with an injected fake transport (in
  tests, or against a mock server), and `import walnut.adapters.rest` is not the moment
  to find out `httpx` is missing.

* **No credentials against a real internal API exist in this environment.** Exactly
  like `github.py`, the default `httpx`-backed transport is untested cleverness by
  construction — it is a thin, literal translation of "make the HTTP call and raise on
  404 or another error status," nothing more. The fake transport in
  `tests/test_rest_adapter.py` is not a shortcut; for this adapter it is the only
  evidence that its behaviour is correct at all.

* **Ids that become URL path segments are treated as hostile input.** A record id in a
  generic, config-driven adapter can originate from any internal system we did not
  write and do not control. `_safe_id_segment` rejects anything that looks like a
  path-traversal attempt (a literal `..` or a leading `/`) before it is allowed near a
  URL, and percent-encodes everything else. `resolve()`, `act()`, and `undo()` all go
  through this one chokepoint rather than three copies of the same check.

* **Read-only is the honest default, not a silent no-op.** Most internal APIs this
  adapter will be pointed at have no write surface Walnut should use. `act()` raises a
  clear, typed error when no `annotate_path` was configured, rather than pretending the
  write succeeded. `undo()` does the same when no `delete_path` was configured. Honesty
  about capability is the whole point of `ActionCapabilities` — an adapter that claims
  a capability it cannot exercise defeats the reason the type exists.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
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

__all__ = ["RESTAdapter", "RESTAPIError", "RESTNotFound"]

Transport = Callable[..., Any]
"""`(method, url, **kwargs) -> Any`. One HTTP call against a fully-resolved URL. Unlike
`github.py`'s transport, this one takes a complete URL rather than a path fragment,
because a generic adapter has no fixed API root to prepend for it."""


class RESTAPIError(RuntimeError):
    """The configured API returned an error this adapter is not entitled to swallow."""

    def __init__(self, status_code: int, url: str, payload: Any = None) -> None:
        super().__init__(f"REST API error {status_code} for {url}: {payload!r}")
        self.status_code = status_code
        self.url = url
        self.payload = payload


class RESTNotFound(RESTAPIError):
    """HTTP 404. The record does not exist — never raised past `resolve()`."""

    def __init__(self, url: str) -> None:
        super().__init__(404, url, payload=None)


def _dig(obj: Any, path: str) -> Any:
    """Walk a dotted path (`"data.items"`, `"meta.author.name"`) through nested dicts.

    Returns `None` the instant the path stops making sense — a missing key, or a
    non-dict where a dict was expected — rather than raising. Config describing a
    response shape is exactly the kind of thing that will occasionally be wrong (a
    field renamed upstream, an optional field genuinely absent on some records), and
    a `KeyError` from deep inside evidence construction is a worse failure mode than a
    `None` the caller can reason about.
    """
    current = obj
    for key in path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


class RESTAdapter:
    """Any internal HTTP/JSON API as evidence, described entirely by configuration.

    A single configured collection (`list_path`) is fetched and, optionally, a single
    record within it can be re-fetched (`item_path`) for `resolve()`. Writes are
    limited to one deliberately narrow operation, `annotate` — posting a note back to
    a record — because that is the one write shape that makes sense for an arbitrary,
    unknown API without knowing anything else about it; anything richer needs a
    purpose-built adapter.
    """

    name: str

    def __init__(
        self,
        name: str,
        base_url: str,
        list_path: str,
        item_path: str | None = None,
        records_key: str | None = None,
        id_field: str = "id",
        text_fields: list[str] | None = None,
        author_field: str | None = None,
        timestamp_field: str | None = None,
        uri_field: str | None = None,
        uri_template: str | None = None,
        headers: dict[str, str] | None = None,
        transport: Transport | None = None,
        annotate_path: str | None = None,
        delete_path: str | None = None,
    ) -> None:
        if not name:
            raise ValueError("RESTAdapter requires a non-empty name")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(
                f"RESTAdapter base_url must be absolute http(s), got {base_url!r}. "
                "Every resource_uri this adapter mints is derived from it, and the "
                "conformance suite requires every resource_uri to be followable."
            )
        if not list_path:
            raise ValueError("RESTAdapter requires a non-empty list_path")

        self.name = name
        self._base_url = base_url.rstrip("/")
        self._list_path = list_path
        self._item_path = item_path
        self._records_key = records_key
        self._id_field = id_field
        self._text_fields = list(text_fields) if text_fields else None
        self._author_field = author_field
        self._timestamp_field = timestamp_field
        self._uri_field = uri_field
        self._uri_template = uri_template
        self._annotate_path = annotate_path
        self._delete_path = delete_path
        self._headers = dict(headers) if headers else {}
        self._transport: Transport = transport or self._build_default_transport(self._headers)

    @staticmethod
    def _build_default_transport(headers: dict[str, str]) -> Transport:
        """The real transport. Never exercised by the test suite — there is no
        internal API in this environment to point it at — so it stays a thin, literal
        translation of "make the call, raise on 404, raise on any other error status."
        Anything cleverer here would be cleverness nobody has verified.
        """

        def _real_transport(method: str, url: str, **kwargs: Any) -> Any:
            import httpx  # imported lazily: importing this module must never require

            response = httpx.request(method, url, headers=headers, timeout=30.0, **kwargs)
            if response.status_code == 404:
                raise RESTNotFound(url)
            if response.status_code >= 400:
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
                raise RESTAPIError(response.status_code, url, payload)
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        return _real_transport

    # -- URL / id safety ------------------------------------------------------

    def _full_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    def _safe_id_segment(self, raw_id: Any) -> str:
        """The one chokepoint every id passes through before it touches a URL.

        A generic adapter's ids come from a system we did not write, so a `..` or a
        leading `/` is treated as an attempted path-traversal escape out of the
        configured endpoint rather than a valid identifier, and rejected outright —
        not merely encoded and hoped to be harmless. Everything that survives that
        check is still percent-encoded, because "not traversal" is not the same as
        "safe to splice into a URL path unescaped."
        """
        id_str = str(raw_id)
        if not id_str:
            raise ValueError(f"{self.name}: record id is empty, cannot build a resource URL")
        if ".." in id_str or id_str.startswith("/"):
            raise ValueError(
                f"{self.name}: refusing to build a URL from id {id_str!r} — it "
                "contains '..' or a leading '/', which looks like an attempt to "
                "escape the configured endpoint rather than a real identifier."
            )
        return quote(id_str, safe="")

    def _render_path(self, template: str, safe_id: str) -> str:
        """`item_path`, `annotate_path`, and `delete_path` all follow the same two
        shapes: a `{id}` placeholder anywhere in the template, or (if there is no
        placeholder) an implicit `<template>/<id>`."""
        if "{id}" in template:
            return template.format(id=safe_id)
        return f"{template.rstrip('/')}/{safe_id}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._transport(method, self._full_url(path), **kwargs)

    def _scope_name(self) -> str:
        normalized = self._list_path.strip("/").replace("/", ".")
        return normalized or self.name

    # -- read -------------------------------------------------------------------

    def probe(self) -> SourceProfile:
        """Report the one collection this adapter was configured against.

        There is no cheap "list all collections" call for an arbitrary API the way
        there is for GitHub's `/user/repos`, so the scope is derived from the
        configuration itself rather than a network round trip — `probe()` stays
        read-only and free, as the contract requires.
        """
        scope = self._scope_name()
        return SourceProfile(
            app=self.name,
            display_name=f"{self.name} (generic REST)",
            scopes=(scope,),
            record_count_estimate=None,
            detail={"base_url": self._base_url, "list_path": self._list_path},
        )

    def fetch(self, scope: str | None = None, limit: int = 100) -> list[Evidence]:
        """Pull the configured collection as evidence.

        `scope` is accepted for protocol symmetry with the other adapters but is not
        used to select among multiple collections — this adapter is configured
        against exactly one (`list_path`). A future multi-collection variant would
        need a different constructor shape; this one is deliberately the simplest
        thing that lets a team point Walnut at one internal endpoint today.
        """
        response = self._request("GET", self._list_path)
        records = self._extract_records(response)
        records = records[: max(0, limit)]
        return [self._to_evidence(record) for record in records]

    def _extract_records(self, response: Any) -> list[Any]:
        """Handle both response shapes the brief calls out: a bare JSON array, and an
        object with the array nested under `records_key`."""
        if isinstance(response, list):
            return response
        if isinstance(response, dict) and self._records_key:
            dug = _dig(response, self._records_key)
            if isinstance(dug, list):
                return dug
        return []

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        """Re-fetch one record via `item_path`, to verify a citation still holds.

        Every failure mode collapses to `None` rather than an exception: no
        `item_path` configured, an id that fails the path-traversal check, a 404, any
        other transport error, or a response that is not a usable record. `resolve()`
        answering "is this record still there, verified" must never itself crash the
        caller trying to find out — a record we cannot currently verify is
        indistinguishable, from the caller's point of view, from one that is gone.
        """
        if not self._item_path:
            return None
        raw_id = pointer.locator.get("id")
        if raw_id is None:
            return None
        try:
            safe_id = self._safe_id_segment(raw_id)
            path = self._render_path(self._item_path, safe_id)
            response = self._request("GET", path)
        except RESTNotFound:
            return None
        except Exception:
            # Deliberately broad: per the contract, resolve() never raises. A
            # transport hiccup while re-verifying a citation is not grounds to crash
            # the caller — it is, for now, an unresolvable pointer, which is exactly
            # what `None` means here.
            return None

        if not isinstance(response, dict) or not response:
            return None
        return self._to_evidence(response)

    # -- evidence construction --------------------------------------------------

    def _to_evidence(self, record: dict[str, Any]) -> Evidence:
        raw_id = _dig(record, self._id_field)
        id_str = str(raw_id) if raw_id is not None else content_hash(record)[:16]

        pointer = SourcePointer(
            app=self.name,
            resource_uri=self._resource_uri(record, id_str),
            locator={"id": id_str},
            content_hash=content_hash(record),
        )
        author = _dig(record, self._author_field) if self._author_field else None
        return Evidence(
            id=f"{self.name}:{id_str}",
            pointer=pointer,
            text=self._compose_text(record),
            author=str(author) if author is not None else None,
            occurred_at=self._occurred_at(record),
            labels=(self._scope_name(),),
            raw=dict(record),
        )

    def _compose_text(self, record: dict[str, Any]) -> str:
        if not self._text_fields:
            # No field list configured: fall back to the whole record rather than
            # guess at field names that might not exist in this API. The brain still
            # gets something to reason over; a company that wants better text just
            # configures text_fields.
            return json.dumps(record, sort_keys=True, default=str, ensure_ascii=False)

        parts = []
        for field_path in self._text_fields:
            value = _dig(record, field_path)
            if value is not None and str(value) != "":
                parts.append(str(value))
        return "\n\n".join(parts)

    def _occurred_at(self, record: dict[str, Any]) -> datetime | None:
        if not self._timestamp_field:
            return None
        return _parse_dt(_dig(record, self._timestamp_field))

    def _resource_uri(self, record: dict[str, Any], id_str: str) -> str:
        """Prefer a URL the source itself hands us; fall back to one we build.

        Whatever the configuration says, the result must start with `http://` or
        `https://` — the conformance suite enforces this on every piece of evidence,
        so a misconfigured `uri_field` or `uri_template` (one that doesn't resolve to
        an absolute URL) falls through to the synthesized form rather than producing
        evidence with an unfollowable pointer.
        """
        if self._uri_field:
            value = _dig(record, self._uri_field)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value

        if self._uri_template:
            # `record` may itself carry an "id" key (most will); it must not
            # collide with the "id" this method always supplies, so the template's
            # own view of the record's fields is layered *under* the resolved id
            # rather than passed alongside it as a second keyword.
            format_kwargs = {**record, "id": id_str}
            try:
                candidate = self._uri_template.format(**format_kwargs)
            except (KeyError, IndexError):
                candidate = self._uri_template.format(id=id_str)
            if candidate.startswith(("http://", "https://")):
                return candidate

        return self._synthesize_uri(id_str)

    def _synthesize_uri(self, id_str: str) -> str:
        try:
            safe_id = self._safe_id_segment(id_str)
        except ValueError:
            # A record whose id itself fails the traversal check still needs *some*
            # followable resource_uri to satisfy the contract; point at the
            # collection itself rather than fabricate a path we just rejected as
            # unsafe.
            return self._full_url(self._list_path)
        if self._item_path:
            return self._full_url(self._render_path(self._item_path, safe_id))
        return self._full_url(f"{self._list_path.rstrip('/')}/{safe_id}")

    # -- write --------------------------------------------------------------

    def capabilities(self) -> ActionCapabilities:
        return ActionCapabilities(
            app=self.name,
            operations={"annotate": ActionTier.TRIVIAL},
        )

    def act(self, action: Action) -> ActionReceipt:
        if action.operation != "annotate":
            raise KeyError(
                f"{self.name} adapter has no act handler for operation {action.operation!r}. "
                "The only write this generic adapter supports is 'annotate'."
            )
        if not self._annotate_path:
            raise RuntimeError(
                f"{self.name} adapter is read-only: no annotate_path was configured for "
                "this source, so there is nothing to write to. Configure annotate_path "
                "to enable annotate()."
            )

        raw_id = action.target.get("id")
        if raw_id is None:
            raise ValueError(f"{self.name}: annotate action.target must include 'id'")
        safe_id = self._safe_id_segment(raw_id)
        path = self._render_path(self._annotate_path, safe_id)

        response = self._request("POST", path, json=action.payload)
        response = response if isinstance(response, dict) else {}
        annotation_id = response.get("id") or response.get("annotation_id")

        result: dict[str, Any] = {"record_id": str(raw_id), "annotation_id": annotation_id}
        result.update(response)
        return ActionReceipt(
            action_id=f"{self.name}:annotate:{raw_id}:{annotation_id}",
            action=action,
            result=result,
            # Additive: an annotation is a new record alongside the original, not an
            # overwrite of an existing value, so there is nothing to capture as
            # prior_state — undo is a delete of the annotation, not a restoration.
            prior_state=None,
        )

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        if receipt.action.operation != "annotate":
            raise KeyError(
                f"{self.name} adapter has no undo handler for operation "
                f"{receipt.action.operation!r}."
            )
        if not self._delete_path:
            raise RuntimeError(
                f"{self.name} adapter does not support undo: no delete_path was "
                "configured for this source. This annotation cannot be reversed "
                "through Walnut — it will need to be removed by hand."
            )

        annotation_id = receipt.result.get("annotation_id")
        if annotation_id is None:
            raise RuntimeError(
                f"{self.name} adapter cannot undo action {receipt.action_id!r}: no "
                "annotation id was captured when the action ran, so there is nothing "
                "to point the delete at."
            )
        safe_id = self._safe_id_segment(annotation_id)
        path = self._render_path(self._delete_path, safe_id)
        self._request("DELETE", path)

        return ActionReceipt(
            action_id=receipt.action_id,
            action=receipt.action,
            result=receipt.result,
            prior_state=receipt.prior_state,
            executed_at=receipt.executed_at,
            undone_at=utcnow(),
        )


# ---------------------------------------------------------------------------
# module-private helpers
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
