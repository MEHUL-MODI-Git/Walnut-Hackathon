"""A generic SQL adapter: point Walnut at a database you already own.

Slack, Linear, GitHub, Notion and Email are all *someone else's* API, reached over
HTTP with someone else's rate limits and someone else's auth model. A support-ticket
table, an orders table, an internal CRM export are different in kind: they are the
company's own data, already sitting in a database the company already runs. This
adapter exists so "connect your own internal systems" is a real story rather than a
promise that only ever gets demonstrated with the five apps we happened to ship.

The design constraint that shapes everything below: **the user supplies the query.**
That is the whole value proposition — nobody has to write five lines of ORM mapping
to expose a bespoke internal table, they write the `SELECT` they'd write anyway. It
is also the whole security problem, because a string the user controls is about to
sit next to SQL string interpolation. The rule this file exists to enforce: the
*query* is trusted (the operator who configures the adapter chose it, the same way
they choose `uri_template`), but every *value* that flows through this adapter after
that point — an id coming back from `SourcePointer.locator`, a scope string handed to
`fetch()` — is untrusted and is bound as a parameter, never interpolated. See
`resolve()` for where this actually matters: it wraps the user's query as a subquery
and filters it with a placeholder, exactly the shape `cursor.execute(sql, params)`
was invented for.

Three moving parts:

1. **DSN dispatch.** `sqlite://` works out of the box via the stdlib — no driver to
   install, no daemon to run, which matters because the demo and the test suite both
   need a SQL source that exists with zero setup. Postgres and MySQL DSNs attempt an
   optional driver import (`psycopg2`, `pymysql`) and fail loudly, naming the missing
   package, rather than silently doing nothing — see `_connect_for_scheme()`.

2. **Identifier hygiene.** `id_column` and every column name this adapter touches
   go through an allow-list regex before they are ever embedded in SQL text (values
   never are — only column *names*, which DBAPI parameter binding cannot cover,
   go through string formatting). This is the same reason ORMs quote identifiers:
   a parameter placeholder binds a *value* into a query, it cannot bind a *column
   name*, so column names have to be validated instead of parameterised.

3. **Pointer synthesis.** `Evidence.pointer.resource_uri` must be a URI a human (or
   `resolve()`) can act on, and the contract requires it start with `http://`,
   `https://` or `imap://` — see `SourcePointer.__post_init__` in `contract.py`. A
   raw database row has no such thing by default, so when the caller supplies no
   `uri_template` this adapter synthesises one. That synthesised URI is **not** a
   network-resolvable link — the whole point of this adapter is that the row's data
   never has to leave the customer's database — but it is a stable, unique locator
   shaped like the others, and it round-trips through `resolve()` exactly like a
   real permalink does. See `_default_resource_uri()` for the exact form.

Read-only by default. The only write this adapter performs is `annotate`, and it
never touches the customer's own table: it appends to a companion
`walnut_annotations` table that this adapter creates the first time it is needed.
**Walnut never writes to, updates, or deletes a row in the table the user pointed us
at.** That is not a policy choice this file makes lightly, it is the only way a
"connect your own database" adapter can be handed a `SELECT` written against a
production schema without becoming the scariest line in this codebase.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Sequence
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

__all__ = ["SQLAdapter", "SQLAdapterError"]


class SQLAdapterError(ValueError):
    """Raised for configuration mistakes: a bad DSN, an unsafe identifier, a missing
    optional driver. Always a mistake the caller made while wiring the adapter up,
    never a data-shaped surprise from a live query — those propagate as whatever the
    underlying DBAPI driver raises, because masking them would hide real bugs."""


# An identifier we are willing to splice into SQL text. Values are always bound as
# parameters; only column *names* go through this allow-list, because parameter
# binding has no way to bind a column name (see module docstring, point 2).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_ANNOTATIONS_TABLE = "walnut_annotations"

# scheme -> (pip package name to suggest, import name to attempt)
_OPTIONAL_DRIVERS: dict[str, tuple[str, str]] = {
    "postgres": ("psycopg2-binary", "psycopg2"),
    "postgresql": ("psycopg2-binary", "psycopg2"),
    "mysql": ("pymysql", "pymysql"),
}

_KNOWN_SCHEMES = {"sqlite", *_OPTIONAL_DRIVERS}

_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?", re.IGNORECASE)


def _validate_identifier(name: str, *, what: str) -> str:
    """Every column name that reaches SQL text, not a bound parameter, comes through
    here first. Raises rather than quietly stripping, because a silently-dropped
    character in a column name is a worse failure mode than a loud one."""
    if not _IDENTIFIER_RE.match(name):
        raise SQLAdapterError(
            f"{what} {name!r} is not a safe SQL identifier (must match "
            f"{_IDENTIFIER_RE.pattern!r}). Refusing to interpolate it into a query."
        )
    return name


def _parse_sqlite_path(dsn: str) -> str:
    """`sqlite:///relative.db`, `sqlite:////abs/path.db`, `sqlite://:memory:` and
    bare `sqlite://` (also memory) — the same three-slash-means-relative,
    four-slash-means-absolute convention every sqlite DSN parser in the wild uses,
    plus the `:memory:` shorthand the task spec asks for explicitly."""
    prefix = "sqlite://"
    tail = dsn[len(prefix):]
    if tail in ("", ":memory:"):
        return ":memory:"
    if tail.startswith("/"):
        rest = tail[1:]
        return ":memory:" if rest == ":memory:" else rest
    return tail


def _detect_paramstyle(conn: Any) -> str:
    """DBAPI modules declare `paramstyle` at module level (PEP 249). sqlite3 uses
    `qmark` (`?`); psycopg2 and pymysql both use `pyformat`/`format` (`%s`). Detecting
    this from the live connection, rather than hard-coding it per DSN scheme, means an
    injected test `connect` callable is free to hand back whatever it wants and the
    adapter still binds parameters the way that object expects."""
    module_name = type(conn).__module__.split(".")[0]
    module = sys.modules.get(module_name)
    style = getattr(module, "paramstyle", None)
    return style or "qmark"


def _rowdict(cols: Sequence[str], row: Any) -> dict[str, Any]:
    """Normalise one fetched row to a plain dict keyed by column name, whether the
    driver handed back a tuple (stdlib sqlite3's default), a `sqlite3.Row`, or a
    dict-like row (e.g. psycopg2's `RealDictCursor`)."""
    if isinstance(row, dict):
        return dict(row)
    return {col: row[i] for i, col in enumerate(cols)}


def _parse_timestamp(value: Any) -> datetime | None:
    """Best-effort, never-raise timestamp parsing. Most DBAPI drivers hand SQLite
    timestamps back as plain strings (SQLite has no native datetime type); Postgres
    and MySQL drivers typically hand back real `datetime` objects already. A column
    that doesn't parse is not a fetch failure — `Evidence.occurred_at` is optional."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _discover_tables(query: str) -> tuple[str, ...]:
    """Naive `FROM`/`JOIN` scrape for `probe()`'s scope list. It does not parse SQL —
    it does not need to. It only has to give a human approving the connection a
    reasonable idea of what tables the query touches; the query itself, not this
    list, is what actually runs."""
    found: list[str] = []
    for match in _TABLE_RE.finditer(query):
        name = match.group(1)
        if name not in found:
            found.append(name)
    return tuple(found)


class SQLAdapter:
    """Six contract methods over an arbitrary user-supplied `SELECT`.

    `query` defines the evidence set: it is run as a subquery everywhere this
    adapter needs one, so it can be as simple as `SELECT * FROM tickets` or as
    involved as a multi-table join with a `WHERE` clause the operator already
    trusts. `id_column` must be a column the query actually projects, and must
    uniquely identify a row within the query's result set — `resolve()` depends
    on that uniqueness to re-fetch exactly one row.
    """

    def __init__(
        self,
        name: str,
        dsn: str,
        query: str,
        id_column: str,
        text_columns: list[str],
        author_column: str | None = None,
        timestamp_column: str | None = None,
        uri_template: str | None = None,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        if not name:
            raise SQLAdapterError("SQLAdapter requires a non-empty name")
        if not query or not query.strip():
            raise SQLAdapterError("SQLAdapter requires a non-empty query")
        if not text_columns:
            raise SQLAdapterError(
                "SQLAdapter requires at least one text_column — an Evidence with no "
                "text is not evidence"
            )

        self.name = name
        self._dsn = dsn
        # Strip a trailing semicolon/whitespace so the query embeds cleanly as a
        # subquery: `SELECT * FROM (<query>) AS _w ...`. A trailing `;` would break
        # that wrapping outright.
        self._query = query.strip().rstrip(";").strip()
        self._id_column = _validate_identifier(id_column, what="id_column")
        self._text_columns = tuple(
            _validate_identifier(c, what="text_columns entry") for c in text_columns
        )
        self._author_column = (
            _validate_identifier(author_column, what="author_column")
            if author_column is not None
            else None
        )
        self._timestamp_column = (
            _validate_identifier(timestamp_column, what="timestamp_column")
            if timestamp_column is not None
            else None
        )
        self._uri_template = uri_template
        self._connect_fn = connect

        self._scheme = dsn.split("://", 1)[0].lower() if "://" in dsn else ""
        if connect is None:
            # Validate eagerly — a bad DSN scheme should fail at construction, not
            # three calls later on the first fetch(). If the caller supplied their
            # own `connect`, they own the DSN's meaning entirely and we defer to it
            # (this is also how tests point a "postgres://" DSN at a fake).
            if not self._scheme:
                raise SQLAdapterError(
                    f"DSN {dsn!r} has no scheme. Expected e.g. "
                    "'sqlite:///path/to.db' or 'sqlite://:memory:'."
                )
            if self._scheme not in _KNOWN_SCHEMES:
                raise SQLAdapterError(
                    f"Unknown DSN scheme {self._scheme!r} in {dsn!r}. Supported: "
                    f"{sorted(_KNOWN_SCHEMES)}. Pass an explicit `connect` callable "
                    "to use a driver this adapter does not know about."
                )

        # MySQL's CAST() has no TEXT type — it wants CHAR. Every other scheme this
        # adapter knows accepts CAST(x AS TEXT).
        self._cast_type = "CHAR" if self._scheme.startswith("mysql") else "TEXT"

        self._conn: Any | None = None
        self._paramstyle = "qmark"

    # -- connection -----------------------------------------------------------

    def _connect_for_scheme(self) -> Any:
        if self._scheme == "sqlite":
            return sqlite3.connect(_parse_sqlite_path(self._dsn))

        package, module_name = _OPTIONAL_DRIVERS[self._scheme]
        try:
            driver = __import__(module_name)
        except ImportError as exc:
            raise SQLAdapterError(
                f"DSN scheme {self._scheme!r} requires the {package!r} package, "
                f"which is not installed. Install it (`pip install {package}`) to "
                f"use a {self._scheme} source, or pass an explicit `connect` "
                "callable if you already manage the connection yourself."
            ) from exc
        return driver.connect(self._dsn)

    def _get_conn(self) -> Any:
        """Open (or reuse) the single connection this adapter holds.

        Lazy and cached, not reopened per call, for a reason that matters more than
        efficiency: `sqlite:///:memory:` is a fresh, empty database on every new
        connection. Opening once and holding it is what makes an in-memory SQLite
        source usable at all rather than one that forgets every row between calls.
        """
        if self._conn is None:
            self._conn = self._connect_fn() if self._connect_fn is not None else self._connect_for_scheme()
            self._paramstyle = _detect_paramstyle(self._conn)
        return self._conn

    @property
    def _placeholder(self) -> str:
        return "?" if self._paramstyle == "qmark" else "%s"

    # -- read -------------------------------------------------------------------

    def probe(self) -> SourceProfile:
        """List the tables the query touches (or, failing that, the adapter's own
        name) as scopes, plus a cheap row-count estimate via `COUNT(*)`."""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM ({self._query}) AS _w")
        row = cur.fetchone()
        count = row[0] if row is not None else None
        tables = _discover_tables(self._query)
        return SourceProfile(
            app=self.name,
            display_name=f"SQL: {self.name}",
            scopes=tables or (self.name,),
            record_count_estimate=int(count) if count is not None else None,
            detail={"dsn_scheme": self._scheme or "custom", "query": self._query},
        )

    def _row_to_evidence(self, row: dict[str, Any]) -> Evidence:
        if self._id_column not in row:
            raise SQLAdapterError(
                f"id_column {self._id_column!r} is not a column the query "
                f"projects. Columns returned: {sorted(row)}"
            )
        row_id = str(row[self._id_column])
        text = "\n\n".join(
            str(row[col]) for col in self._text_columns if row.get(col) is not None
        )
        author = None
        if self._author_column is not None:
            raw_author = row.get(self._author_column)
            author = str(raw_author) if raw_author is not None else None
        occurred_at = (
            _parse_timestamp(row.get(self._timestamp_column))
            if self._timestamp_column is not None
            else None
        )
        return Evidence(
            id=row_id,
            pointer=SourcePointer(
                app=self.name,
                resource_uri=self._resource_uri(row_id, row),
                locator={"id": row_id},
                # Hashed over every column the query returned, not just the text
                # columns: a status flip or an author reassignment is a real change
                # to the evidence even if the ticket body text never moved, and
                # drift detection should catch it.
                content_hash=content_hash(row),
            ),
            text=text,
            author=author,
            occurred_at=occurred_at,
            raw=dict(row),
        )

    def _resource_uri(self, row_id: str, row: dict[str, Any]) -> str:
        if self._uri_template is not None:
            format_vars = dict(row)
            format_vars["id"] = row_id
            try:
                return self._uri_template.format(**format_vars)
            except (KeyError, IndexError) as exc:
                raise SQLAdapterError(
                    f"uri_template {self._uri_template!r} references a placeholder "
                    f"not present in the row (or as {{id}}): {exc}"
                ) from exc
        return self._default_resource_uri(row_id)

    def _default_resource_uri(self, row_id: str) -> str:
        """The synthesised pointer used when the caller supplies no `uri_template`.

        Form: `https://sql.internal.walnut/<adapter-name>/<id-column>/<id-value>`,
        each segment percent-encoded. This is **not** a network-resolvable link —
        nothing is listening at `sql.internal.walnut`, and that is deliberate: the
        entire premise of this adapter is that a company's own database rows never
        have to leave that database to become cited evidence. What this URI has to
        be is *stable* (the same row always produces the same URI, so citations
        stay valid across fetches) and *unique* (no two rows in this adapter's
        result set collide), and it has to satisfy `SourcePointer`'s requirement
        that every pointer be `http(s)://` or `imap://` shaped — see
        `contract.py`'s `SourcePointer.__post_init__`. Pass `uri_template` (e.g.
        `"https://internal/tickets/{id}"`) to point at a real internal UI instead.
        """
        return (
            f"https://sql.internal.walnut/{quote(self.name, safe='')}"
            f"/{quote(self._id_column, safe='')}"
            f"/{quote(row_id, safe='')}"
        )

    def fetch(self, scope: str | None = None, limit: int = 100) -> list[Evidence]:
        """Run the configured query and return up to `limit` rows as `Evidence`.

        `scope` is accepted for interface parity with the other adapters, but a
        single arbitrary `SELECT` has no general notion of "channels" or "repos" to
        narrow into — the query itself is already the full, fixed projection the
        operator chose. It is not used to filter here; a future version could
        support named sub-scopes by keying multiple queries off `scope`, but that is
        a different adapter design, not a silent behaviour this one should fake.
        """
        conn = self._get_conn()
        cur = conn.cursor()
        placeholder = self._placeholder
        cur.execute(f"SELECT * FROM ({self._query}) AS _w LIMIT {placeholder}", (int(limit),))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return [self._row_to_evidence(_rowdict(cols, r)) for r in rows]

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        """Re-query the one row `pointer` names, by `id_column`, and return it — or
        `None` if it is gone. Never raises for a missing row.

        The security-sensitive line: the user's query is wrapped as a subquery and
        filtered with a bound parameter, never string-interpolated. `id_column` was
        already validated against an identifier allow-list at construction time, so
        the only thing touching this SQL text unvalidated is the query the operator
        configured — the *value* being searched for is always a bind parameter.
        """
        row_id = pointer.locator.get("id")
        if row_id is None:
            return None
        conn = self._get_conn()
        cur = conn.cursor()
        placeholder = self._placeholder
        sql = (
            f"SELECT * FROM ({self._query}) AS _w "
            f"WHERE CAST({self._id_column} AS {self._cast_type}) = {placeholder}"
        )
        cur.execute(sql, (str(row_id),))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return self._row_to_evidence(_rowdict(cols, row))

    # -- write --------------------------------------------------------------

    def capabilities(self) -> ActionCapabilities:
        """A SQL source is read-only by default. The one declared operation,
        `annotate`, never writes to the caller's own table — see `act()`."""
        return ActionCapabilities(app=self.name, operations={"annotate": ActionTier.TRIVIAL})

    def _ensure_annotations_table(self, conn: Any) -> None:
        """Create the companion annotations table on first use. This table belongs
        to Walnut, not to the customer's schema — it is additive, it is reversible
        (`undo()` deletes exactly the row `act()` inserted), and it is the only
        thing this adapter ever writes. **Walnut never mutates a row in the table
        the operator's `query` reads from.**"""
        cur = conn.cursor()
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {_ANNOTATIONS_TABLE} ("
            "annotation_id TEXT PRIMARY KEY, "
            "app TEXT NOT NULL, "
            "row_id TEXT NOT NULL, "
            "note TEXT, "
            "created_at TEXT NOT NULL"
            ")"
        )
        conn.commit()

    def act(self, action: Action) -> ActionReceipt:
        """Insert one annotation row. Purely additive — `prior_state=None` — so
        `undo()` is a delete of exactly this row, not a restoration of some prior
        value (there is no prior value; the row did not exist)."""
        if action.operation != "annotate":
            raise SQLAdapterError(
                f"SQLAdapter supports only the 'annotate' operation, got "
                f"{action.operation!r}. Declared operations: "
                f"{sorted(self.capabilities().operations)}"
            )
        row_id = str(action.target.get("id", ""))
        if not row_id:
            raise SQLAdapterError("annotate requires target={'id': <row id>}")

        conn = self._get_conn()
        self._ensure_annotations_table(conn)
        annotation_id = f"{self.name}-annot-{uuid.uuid4().hex}"
        note = str(action.payload.get("note", ""))
        created_at = utcnow().isoformat()

        placeholder = self._placeholder
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO {_ANNOTATIONS_TABLE} "
            f"(annotation_id, app, row_id, note, created_at) "
            f"VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
            (annotation_id, self.name, row_id, note, created_at),
        )
        conn.commit()

        return ActionReceipt(
            action_id=annotation_id,
            action=action,
            result={"annotation_id": annotation_id, "row_id": row_id, "note": note},
            prior_state=None,
        )

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        """Delete exactly the annotation row `act()` inserted. Still never touches
        the customer's own table — undo of an additive write is a delete of what was
        added, not a retraction dressed up as one."""
        conn = self._get_conn()
        self._ensure_annotations_table(conn)
        placeholder = self._placeholder
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM {_ANNOTATIONS_TABLE} WHERE annotation_id = {placeholder}",
            (receipt.action_id,),
        )
        conn.commit()

        return ActionReceipt(
            action_id=receipt.action_id,
            action=receipt.action,
            result=receipt.result,
            prior_state=receipt.prior_state,
            executed_at=receipt.executed_at,
            undone_at=utcnow(),
        )
