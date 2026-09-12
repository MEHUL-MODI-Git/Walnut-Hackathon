"""Conformance and behavioural tests for the generic SQL adapter.

Everything here runs against a real in-memory SQLite database — no mocked cursor, no
fake DBAPI. `sqlite3` is stdlib, so this is also the proof that "SQLite works with
zero setup" actually holds: nothing here installs a driver or spins up a service.

One connection is shared between the adapter (via the injectable `connect=` factory)
and the test itself, because `sqlite3.connect(":memory:")` opens a fresh, empty
database every time it is called — holding one connection open for the life of both
the adapter and the test is what lets the test mutate rows out from under the adapter
(to prove drift detection) and inspect the database directly (to prove an injection
attempt left the schema untouched).
"""

from __future__ import annotations

import sqlite3

import pytest

from walnut.adapters.sql import SQLAdapter, SQLAdapterError
from walnut.conformance import run_conformance
from walnut.contract import SourcePointer

_SCHEMA = """
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    requester_email TEXT,
    assigned_to TEXT,
    created_at TEXT NOT NULL
)
"""

_ROWS = [
    (1, "VPN drops every morning", "Reconnects fine but it's happening daily around 9am.",
     "open", "priya@acme.test", "alex", "2026-08-01T09:03:00+00:00"),
    (2, "Invoice #4471 shows wrong tax", "Tax line is 8% but our region is 6%.",
     "open", "morgan@acme.test", "jamie", "2026-08-03T14:22:00+00:00"),
    (3, "Export to CSV times out", "Any export over 10k rows just spins forever.",
     "closed", "sam@acme.test", "alex", "2026-08-05T11:47:00+00:00"),
]


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(_SCHEMA)
    conn.executemany(
        "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?)", _ROWS
    )
    conn.commit()
    return conn


@pytest.fixture()
def conn() -> sqlite3.Connection:
    return _make_db()


@pytest.fixture()
def adapter(conn: sqlite3.Connection) -> SQLAdapter:
    return SQLAdapter(
        name="support_db",
        dsn="sqlite:///unused-because-connect-is-injected.db",
        query="SELECT id, subject, body, status, requester_email, assigned_to, "
        "created_at FROM tickets",
        id_column="id",
        text_columns=["subject", "body"],
        author_column="assigned_to",
        timestamp_column="created_at",
        uri_template="https://internal/tickets/{id}",
        connect=lambda: conn,
    )


# -- conformance --------------------------------------------------------------


def test_sql_adapter_conforms(adapter: SQLAdapter) -> None:
    write_target = {
        "operation": "annotate",
        "target": {"id": "1"},
        "payload": {"note": "walnut conformance annotation"},
        "overwrites": False,  # annotate is purely additive: insert, then delete on undo
    }
    report = run_conformance(adapter, write_target=write_target)
    assert report.ok, report.render()


# -- probe --------------------------------------------------------------------


def test_probe_lists_the_table_as_a_scope(adapter: SQLAdapter) -> None:
    profile = adapter.probe()
    assert profile.app == "support_db"
    assert "tickets" in profile.scopes
    assert profile.record_count_estimate == 3


def test_probe_scopes_are_never_empty_even_with_no_from_clause(
    conn: sqlite3.Connection,
) -> None:
    # A query with no discoverable table name (subselect-free VALUES clause) still
    # must report a non-empty scope list — falls back to the adapter's own name.
    weird = SQLAdapter(
        name="values_source",
        dsn="sqlite://:memory:",
        query="SELECT 1 AS id, 'hello' AS body",
        id_column="id",
        text_columns=["body"],
        connect=lambda: conn,
    )
    profile = weird.probe()
    assert profile.scopes == ("values_source",)


# -- content hashing ------------------------------------------------------------


def test_content_hash_is_a_real_sha256(adapter: SQLAdapter) -> None:
    evidence = adapter.fetch(limit=5)[0]
    assert len(evidence.pointer.content_hash) == 64
    int(evidence.pointer.content_hash, 16)  # raises if not hex


def test_content_hash_is_stable_across_repeated_fetches(adapter: SQLAdapter) -> None:
    first = {e.id: e.pointer.content_hash for e in adapter.fetch(limit=5)}
    second = {e.id: e.pointer.content_hash for e in adapter.fetch(limit=5)}
    assert first == second


def test_content_hash_changes_when_the_row_is_updated(
    adapter: SQLAdapter, conn: sqlite3.Connection
) -> None:
    before = next(e for e in adapter.fetch(limit=5) if e.id == "2")
    conn.execute("UPDATE tickets SET status = ? WHERE id = 2", ("closed",))
    conn.commit()
    after = adapter.resolve(before.pointer)
    assert after is not None
    assert after.pointer.content_hash != before.pointer.content_hash


# -- resolve --------------------------------------------------------------------


def test_resolve_round_trips_an_existing_row(adapter: SQLAdapter) -> None:
    original = adapter.fetch(limit=5)[0]
    again = adapter.resolve(original.pointer)
    assert again is not None
    assert again.id == original.id
    assert again.pointer.content_hash == original.pointer.content_hash


def test_resolve_returns_none_for_a_deleted_row(
    adapter: SQLAdapter, conn: sqlite3.Connection
) -> None:
    victim = next(e for e in adapter.fetch(limit=5) if e.id == "3")
    conn.execute("DELETE FROM tickets WHERE id = 3")
    conn.commit()
    assert adapter.resolve(victim.pointer) is None


def test_resolve_never_raises_for_a_nonexistent_id(adapter: SQLAdapter) -> None:
    ghost = SourcePointer(
        app="support_db",
        resource_uri="https://internal/tickets/999999",
        locator={"id": "999999"},
        content_hash="0" * 64,
    )
    assert adapter.resolve(ghost) is None


# -- SQL injection --------------------------------------------------------------


def test_id_containing_a_drop_table_payload_is_safely_parameterized(
    adapter: SQLAdapter, conn: sqlite3.Connection
) -> None:
    malicious = SourcePointer(
        app="support_db",
        resource_uri="https://internal/tickets/malicious",
        locator={"id": "1'; DROP TABLE tickets; --"},
        content_hash="0" * 64,
    )
    # Must not raise, and must not match any row — the payload is a value, not SQL.
    assert adapter.resolve(malicious) is None

    # The table must still exist and be fully intact: a real DROP TABLE would have
    # made this raise sqlite3.OperationalError instead of returning cleanly above.
    cur = conn.execute("SELECT COUNT(*) FROM tickets")
    assert cur.fetchone()[0] == 3


def test_id_column_itself_is_validated_not_interpolated_blindly() -> None:
    with pytest.raises(SQLAdapterError, match="id_column"):
        SQLAdapter(
            name="bad",
            dsn="sqlite://:memory:",
            query="SELECT 1 AS id",
            id_column="id; DROP TABLE tickets; --",
            text_columns=["id"],
        )


# -- resource_uri -----------------------------------------------------------------


def test_uri_template_is_used_when_supplied(adapter: SQLAdapter) -> None:
    evidence = next(e for e in adapter.fetch(limit=5) if e.id == "1")
    assert evidence.pointer.resource_uri == "https://internal/tickets/1"


def test_uri_is_synthesised_when_no_template_supplied(conn: sqlite3.Connection) -> None:
    bare = SQLAdapter(
        name="support_db",
        dsn="sqlite://:memory:",
        query="SELECT id, subject, body FROM tickets",
        id_column="id",
        text_columns=["subject", "body"],
        connect=lambda: conn,
    )
    evidence = next(e for e in bare.fetch(limit=5) if e.id == "1")
    uri = evidence.pointer.resource_uri
    assert uri.startswith("https://")
    # Stable and unique: the adapter name, the id column, and the row id all appear.
    assert uri == "https://sql.internal.walnut/support_db/id/1"

    # Synthesised URIs round-trip through resolve() exactly like a real permalink.
    again = bare.resolve(evidence.pointer)
    assert again is not None
    assert again.pointer.resource_uri == uri


# -- DSN handling -----------------------------------------------------------------


def test_sqlite_memory_dsn_works_with_zero_setup() -> None:
    real = SQLAdapter(
        name="zero_setup",
        dsn="sqlite://:memory:",
        query="SELECT 1 AS id, 'hello world' AS body",
        id_column="id",
        text_columns=["body"],
    )
    evidence = real.fetch(limit=5)
    assert len(evidence) == 1
    assert evidence[0].text == "hello world"


def test_unknown_dsn_scheme_raises_a_clear_error() -> None:
    with pytest.raises(SQLAdapterError, match="Unknown DSN scheme"):
        SQLAdapter(
            name="mongo_oops",
            dsn="mongodb://localhost:27017/mydb",
            query="SELECT 1",
            id_column="id",
            text_columns=["id"],
        )


def test_postgres_dsn_without_driver_names_the_missing_package() -> None:
    adapter = SQLAdapter(
        name="pg_source",
        dsn="postgresql://user:pass@localhost/mydb",
        query="SELECT 1 AS id",
        id_column="id",
        text_columns=["id"],
    )
    with pytest.raises(SQLAdapterError, match="psycopg2"):
        adapter.probe()


def test_mysql_dsn_without_driver_names_the_missing_package() -> None:
    adapter = SQLAdapter(
        name="mysql_source",
        dsn="mysql://user:pass@localhost/mydb",
        query="SELECT 1 AS id",
        id_column="id",
        text_columns=["id"],
    )
    with pytest.raises(SQLAdapterError, match="pymysql"):
        adapter.probe()


# -- capabilities -----------------------------------------------------------------


def test_capabilities_declares_annotate_as_trivial(adapter: SQLAdapter) -> None:
    from walnut.contract import ActionTier

    caps = adapter.capabilities()
    assert caps.tier_of("annotate") is ActionTier.TRIVIAL


def test_act_rejects_any_operation_other_than_annotate(adapter: SQLAdapter) -> None:
    from walnut.contract import Action

    action = Action(
        app="support_db",
        operation="delete_ticket",
        target={"id": "1"},
        payload={},
        justified_by=("conformance:synthetic",),
    )
    with pytest.raises(SQLAdapterError, match="annotate"):
        adapter.act(action)


# -- annotate never touches the customer's table -----------------------------------


def test_annotate_writes_only_to_the_companion_table_never_the_source_table(
    adapter: SQLAdapter, conn: sqlite3.Connection
) -> None:
    from walnut.contract import Action

    def _read_ticket_1() -> dict[str, object]:
        cur = conn.execute("SELECT * FROM tickets WHERE id = 1")
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, cur.fetchone()))

    before = _read_ticket_1()

    action = Action(
        app="support_db",
        operation="annotate",
        target={"id": "1"},
        payload={"note": "customer confirmed by phone"},
        justified_by=("support_db:1",),
    )
    receipt = adapter.act(action)

    after = _read_ticket_1()
    assert before == after  # the customer's own row is byte-for-byte untouched

    rows = conn.execute(
        "SELECT app, row_id, note FROM walnut_annotations WHERE annotation_id = ?",
        (receipt.action_id,),
    ).fetchall()
    assert rows == [("support_db", "1", "customer confirmed by phone")]

    adapter.undo(receipt)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM walnut_annotations WHERE annotation_id = ?",
        (receipt.action_id,),
    ).fetchone()[0]
    assert remaining == 0
