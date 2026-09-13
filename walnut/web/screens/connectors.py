"""Connectors screen: five built-in apps, unlimited custom sources, one honest table.

**Why a table, not a card grid.** `design.py`'s own rule: a table is for rows that are
comparable on identical axes and the job is a column scan. A dozen connectors are
exactly that — the reader's question is "which of these is actually live," and a card
grid makes that a dozen separate reading acts instead of one glance down a column.

**Why "Sample data," not "demo."** `ConnectionState.DEMO` is the same enum value either
way, but the word a buyer reads is not neutral. "Demo" says the *product* is a demo.
"Sample data" says this one connector has not been pointed at a real account yet, and
is working perfectly on Meridian Health's seeded data in the meantime. Same state,
opposite claim — so the rename happens here, at the rendering boundary, and nowhere
else needs to change.

**The detail page's shape follows the order an admin's questions actually arrive:**
is it live, what can it see, what can it write, and — only once something has gone
wrong — why, and what do I do about it. The one thing this page fixes from the old
`page_connections`/`page_sources` split: `spec.gotcha` used to render only on the
*disconnected* credential form, which is backwards — the gotcha is exactly what you
need re-reading once a connection has already failed, so it is now a persistent note
on the errors panel regardless of connection state.

Read-only rendering. This module never touches `ConnectionManager`, `SourceRegistry`,
or an adapter directly — it is handed already-redacted dicts and dataclasses, and the
caller owns every POST route it renders a `<form>` toward.
"""

from __future__ import annotations

from typing import Any

from ...actions.governance import is_quarantine_safe, is_reversible
from ...connections import CredentialSpec
from ...contract import ActionCapabilities, ActionTier
from ...plugins import CustomSource
from ..design import empty, esc, meter, panel, pill, rail, table

__all__ = ["page_connectors", "page_connector_detail"]

# Built-in connection state -> (label, pill kind). "Sample data" is the deliberate
# rename documented above; "demo" never reaches the page.
_BUILTIN_STATUS: dict[str, tuple[str, str]] = {
    "connected": ("Live", "ok"),
    "demo": ("Sample data", "quiet"),
    "error": ("Error", "stop"),
}

# CustomSource.status() -> (label, pill kind). "ready" means the conformance suite
# passed and the source is actually contributing evidence, which is what "Live" means
# for a built-in too — the same word for the same fact, whichever kind of source it is.
_CUSTOM_STATUS: dict[str, tuple[str, str]] = {
    "ready": ("Live", "ok"),
    "failing": ("Failing", "attention"),
    "error": ("Error", "stop"),
    "unchecked": ("Unchecked", "quiet"),
}

_COLUMNS = ("Connector", "Status", "Scope", "Records", "Last sync", "Note")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fmt_time(value: str | None) -> str:
    """An ISO timestamp trimmed to something a human reads in one glance, or a dash."""
    if not value:
        return "—"
    return value.replace("T", " ")[:19]


def _num(value: int | None) -> str:
    return f'<span class="num">{value}</span>' if value is not None else '<span class="num">—</span>'


def _text_of(item: Any) -> str:
    """Recent evidence, handled whether it arrives as an `Evidence` or a plain dict."""
    if hasattr(item, "text"):
        return str(item.text)
    if isinstance(item, dict):
        return str(item.get("text", ""))
    return str(item)


def _preview_of(item: Any) -> str:
    """One ingested record as one readable line.

    Adapters join their configured text fields with blank lines, and `.rail .t` is
    `white-space:pre-wrap` — so a prescription arrived as seven lonely lines, one word
    each, and eight of them turned this panel into the tallest thing on the page. The
    fields are re-joined with a separator here, at the rendering boundary: the stored
    evidence is untouched, and what an admin scans is a row per record rather than a
    column per field.
    """
    return " · ".join(part.strip() for part in _text_of(item).splitlines() if part.strip())


def _cite_of(item: Any) -> str:
    cite = getattr(item, "cite", None)
    if callable(cite):
        return str(cite())
    if isinstance(item, dict):
        return str(item.get("cite", ""))
    return ""


# ---------------------------------------------------------------------------
# page_connectors — the list
# ---------------------------------------------------------------------------


def _rows_and_sync(
    app: str, ingested: dict[str, int], last_seen: dict[str, Any]
) -> tuple[str, str]:
    """What this source actually put in the brain, and when.

    Falls back to an em dash only when the source really has contributed nothing —
    which is then a true statement about the source rather than a gap in this screen.
    """
    shown = _num(ingested.get(app) or 0)
    stamp = last_seen.get(app)
    # `_fmt_time` reads ISO strings; the brain holds datetimes. Normalise here rather
    # than widening the formatter, so it keeps one input shape.
    if stamp is None:
        return shown, "—"
    text = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp)
    return shown, esc(_fmt_time(text))


def _builtin_row(
    c: dict[str, Any], ingested: dict[str, int], last_seen: dict[str, Any]
) -> tuple[str, str, str, str, str, str]:
    word, kind = _BUILTIN_STATUS.get(c["state"], (c["state"].title(), "quiet"))
    name = f'<a href="/connectors/{esc(c["app"])}">{esc(c["display_name"])}</a>'

    if c["state"] == "connected":
        shown = c["scopes"][:3]
        more = f" +{len(c['scopes']) - 3} more" if len(c["scopes"]) > 3 else ""
        scope = esc(", ".join(shown) + more) if shown else "—"
    elif c["state"] == "demo":
        scope = "Meridian Health sample data"
    else:
        scope = "—"

    note = esc(c["error"][:140]) if c["state"] == "error" and c["error"] else ""
    records, synced = _rows_and_sync(c["app"], ingested, last_seen)
    return (name, pill(word, kind), scope, records, synced, note)


def _custom_row(
    s: CustomSource, ingested: dict[str, int], last_seen: dict[str, Any]
) -> tuple[str, str, str, str, str, str]:
    word, kind = _CUSTOM_STATUS.get(s.status(), (s.status().title(), "quiet"))
    name = f'<a href="/connectors/{esc(s.name)}">{esc(s.name)}</a>'
    records, synced = _rows_and_sync(s.name, ingested, last_seen)
    return (name, pill(word, kind), esc(s.kind), records, synced,
            esc(s.summary()[:140]))


def _add_source_intro(plugin_dir: str) -> str:
    """The three ways in, and the trust rule that governs all of them."""
    return (
        "<p>Three ways in, in increasing effort: point Walnut at "
        '<a href="#add-sql">a database with one SQL query</a>, describe '
        '<a href="#add-rest">a JSON API</a>, or drop '
        '<a href="#add-plugin">a Python file</a> into '
        f'<code>{esc(plugin_dir)}/</code> implementing the six-method adapter contract — '
        "any source at all, with no first-party integration required.</p>"
        "<p><b>A custom source is not trusted, it is tested.</b> Registering one runs "
        "the same behavioural conformance suite the five built-in adapters pass, and "
        "reports exactly which guarantees hold — before anything it returns is allowed "
        "into the brain.</p>"
    )


def _sql_form() -> str:
    body = (
        '<form method="post" action="/sources/add">'
        '<input type="hidden" name="kind" value="sql">'
        "<label>Name</label>"
        '<input name="name" placeholder="support_db">'
        "<label>Connection string</label>"
        '<input name="dsn" placeholder="sqlite:///./support.db">'
        '<p class="lbl">SQLite works with no setup. Postgres and MySQL need their '
        "driver installed.</p>"
        "<label>Query</label>"
        '<input name="query" '
        'placeholder="SELECT id, subject, body, author, created_at FROM tickets">'
        "<label>ID column</label>"
        '<input name="id_column" placeholder="id">'
        "<label>Text columns (comma separated)</label>"
        '<input name="text_columns" placeholder="subject, body">'
        "<label>Link template (optional)</label>"
        '<input name="uri_template" placeholder="https://support.internal/ticket/{id}">'
        '<div class="row" style="margin-top:var(--s4)">'
        '<button class="btn" type="submit">Add and validate</button></div>'
        "</form>"
        '<div class="note"><b>Walnut never writes to your tables.</b> A SQL source is '
        "read-only; annotations go to a separate companion table it creates itself.</div>"
    )
    return f'<div id="add-sql">{panel("Add a database", body)}</div>'


def _rest_form() -> str:
    body = (
        '<form method="post" action="/sources/add">'
        '<input type="hidden" name="kind" value="rest">'
        "<label>Name</label>"
        '<input name="name" placeholder="internal_wiki">'
        "<label>Base URL</label>"
        '<input name="base_url" placeholder="https://wiki.internal">'
        "<label>List path</label>"
        '<input name="list_path" placeholder="/api/articles">'
        "<label>Item path (optional)</label>"
        '<input name="item_path" placeholder="/api/articles/{id}">'
        "<label>Records key (optional)</label>"
        '<input name="records_key" placeholder="data.items">'
        "<label>ID field</label>"
        '<input name="id_field" placeholder="id">'
        "<label>Text fields (comma separated)</label>"
        '<input name="text_fields" placeholder="title, body">'
        "<label>Auth header (optional)</label>"
        '<input name="auth_header" placeholder="Bearer …">'
        '<div class="row" style="margin-top:var(--s4)">'
        '<button class="btn" type="submit">Add and validate</button></div>'
        "</form>"
    )
    return f'<div id="add-rest">{panel("Add an internal API", body)}</div>'


def _plugin_form(plugin_dir: str) -> str:
    body = (
        f"<p>For a source with no SQL or HTTP surface, drop a Python file into "
        f"<code>{esc(plugin_dir)}/</code> implementing the six-method contract, "
        "exposing either <code>build()</code> or <code>ADAPTER</code>. It is "
        "discovered, validated against the same conformance suite, and reported "
        "above.</p>"
        '<p class="lbl">Loading a plugin executes that file — the same trust model as '
        "a pytest conftest. Point the directory only at code you would run yourself.</p>"
        '<form method="post" action="/sources/rescan">'
        '<div class="row"><button class="btn ghost" type="submit">'
        "Rescan plugin directory</button></div></form>"
    )
    return f'<div id="add-plugin">{panel("Add anything else", body)}</div>'


def page_connectors(
    connections: list[dict[str, Any]],
    specs: dict[str, CredentialSpec],
    sources: list[CustomSource],
    plugin_dir: str,
    ingested: dict[str, int] | None = None,
    last_seen: dict[str, Any] | None = None,
) -> str:
    """One table, two sections. `specs` is accepted (and part of the contract the
    caller relies on) even though this listing itself only needs the redacted
    connection dicts — the detail page is where a `CredentialSpec` earns its keep."""
    live = sum(1 for c in connections if c["state"] == "connected")
    sample = sum(1 for c in connections if c["state"] == "demo")
    err = sum(1 for c in connections if c["state"] == "error")
    failing = sum(1 for s in sources if s.status() in ("failing", "error"))

    header = (
        '<div class="ph"><h1>Connectors</h1>'
        "<p>Five built-in apps, plus anything you connect yourself. Every one works on "
        "seeded data before you connect anything — connecting an account swaps the live "
        "adapter in behind the same six-method contract, and nothing downstream, not "
        "the brain and not the agent, knows or cares which is in play.</p></div>"
    )

    line = [f"{live} live", f"{sample} sample data", f"{err} error"]
    if sources:
        line.append(f"{len(sources)} custom source{'s' if len(sources) != 1 else ''}")
        if failing:
            line[-1] += f", {failing} failing"
    summary = f'<p class="lbl">{" · ".join(line)}</p>'

    ingested = ingested or {}
    last_seen = last_seen or {}
    builtin_table = table(
        _COLUMNS, [_builtin_row(c, ingested, last_seen) for c in connections],
        empty_text="No built-in apps configured.")
    custom_table = table(
        _COLUMNS, [_custom_row(s, ingested, last_seen) for s in sources],
        empty_text="No custom sources yet — add one below.")

    listing = panel(
        "Connectors",
        summary
        + "<h2>Built-in</h2>" + builtin_table
        + '<h2 style="margin-top:var(--s5)">Custom</h2>' + custom_table,
    )

    return (
        header
        + listing
        + panel("Add a custom source", _add_source_intro(plugin_dir))
        + _sql_form()
        + _rest_form()
        + _plugin_form(plugin_dir)
    )


# ---------------------------------------------------------------------------
# page_connector_detail — one connector, in full
# ---------------------------------------------------------------------------


def _status_of(
    conn: dict[str, Any] | None, source: CustomSource | None
) -> tuple[str, str]:
    if conn is not None:
        return _BUILTIN_STATUS.get(conn["state"], (conn["state"].title(), "quiet"))
    if source is not None:
        return _CUSTOM_STATUS.get(source.status(), (source.status().title(), "quiet"))
    return ("Unknown", "quiet")


def _source_endpoint(source: CustomSource) -> str:
    """The one configuration value worth showing an admin, per kind of source.

    A REST source's `base_url` is the whole answer to "where is this pointed?" — it is
    also what a reader needs when the source is unreachable and they are about to go
    and check whether the service is up. A SQL source's DSN is deliberately NOT shown:
    a connection string routinely carries a password inline, and this panel has no
    business being the place that leaks one.
    """
    if source.kind == "rest":
        return str(source.spec.get("base_url") or "")
    return ""


def _header_lines(conn: dict[str, Any] | None, source: CustomSource | None) -> str:
    """Facts about the connection, and only the ones that have a value.

    `Connected —` was rendered for every source that had no timestamp, which is every
    custom source and every built-in still on sample data. A label with a dash beside
    it reads as a field that failed to load; the honest rendering of "there is no
    connection date because nothing has been connected" is the sample-data sentence,
    or nothing at all.
    """
    rows: list[tuple[str, str]] = []
    if conn is not None:
        if conn["connected_at"]:
            rows.append(("Connected", esc(_fmt_time(conn["connected_at"]))))
        elif conn["state"] == "demo":
            rows.append(("Serving", "Meridian Health sample data — no account connected yet"))
    if source is not None:
        endpoint = _source_endpoint(source)
        if endpoint:
            rows.append(("Endpoint", f'<span class="mono">{esc(endpoint)}</span>'))
        rows.append(("Registered as", f'a custom {esc(source.kind)} source'))

    return "".join(
        f'<p><span class="lbl">{esc(label)}</span> {value}</p>' for label, value in rows
    )


def _header_panel(
    app_id: str, display_name: str, word: str, kind: str,
    conn: dict[str, Any] | None, source: CustomSource | None = None,
) -> str:
    """(a) Is it live, where is it pointed, and when did we last check."""
    lines = [_header_lines(conn, source)]

    controls = (
        f'<form method="post" action="/connectors/{esc(app_id)}/test">'
        '<button class="btn ghost" type="submit">Test connection</button></form>'
    )
    if conn is not None and conn["state"] == "connected":
        controls += (
            f'<form method="post" action="/connections/{esc(app_id)}/disconnect">'
            '<button class="btn danger" type="submit">Disconnect</button></form>'
        )
    lines.append(f'<div class="row" style="margin-top:var(--s3)">{controls}</div>')

    return panel(display_name, "".join(lines), pill(word, kind))


def _seeing_panel(
    conn: dict[str, Any] | None, source: CustomSource | None, ingested_count: int,
    oldest: str, newest: str, recent: list[Any],
) -> str:
    """(b) What it can see — the token's own claim, next to what actually landed.

    The two counts are read side by side on purpose. A live token can report five
    scopes and Walnut can still have ingested nothing from three of them — pagination
    that silently stopped, a scope the fetch code never wired up, a filter that is
    stricter than it looks. That gap is where every connector bug in this codebase has
    actually lived, so it gets a sentence of its own rather than two numbers a reader
    has to notice disagree.
    """
    if conn is not None:
        scopes = conn["scopes"]
        chips = "".join(f'<span class="chip"><b>{esc(s)}</b></span>' for s in scopes)
        token_n: int | None = conn["record_estimate"]
    elif source is not None:
        chips = f'<span class="chip"><b>{esc(source.kind)} source</b></span>'
        token_n = None
    else:
        chips = ""
        token_n = None

    scope_block = chips or empty("No scopes reported.")

    counts = (
        '<div class="row" style="margin-top:var(--s4)">'
        f'<div><div class="lbl">Token reports (record_estimate)</div>{_num(token_n)}</div>'
        f'<div><div class="lbl">Actually ingested</div>{_num(ingested_count)}</div>'
        "</div>"
    )

    gap = ""
    if token_n is not None and token_n != ingested_count:
        gap = (
            '<div class="note"><b>Gap.</b> The credential reports '
            f"{token_n} record{'s' if token_n != 1 else ''}, but Walnut has actually "
            f"ingested {ingested_count}. If that is not expected — a scope with 0 "
            "ingested rows, or a token claiming far more than landed — this is where "
            "a connector bug shows up first.</div>"
        )
    elif token_n is not None:
        gap = '<div class="note"><b>Matches.</b> Everything the token can see has been ingested.</div>'

    span = (
        f"<p>Evidence spans {esc(oldest)} → {esc(newest)}.</p>"
        if (oldest or newest) else ""
    )

    # The empty state has to agree with the count two inches above it. This panel
    # spent a release asserting "15 ingested" and "Nothing ingested yet" in the same
    # breath, because the caller was handing it the action ledger instead of the
    # facts. The caller is fixed; this branch makes the page structurally unable to
    # tell those two lies at once again, whatever it is handed next.
    shown = recent[:6]
    recent_html = "".join(rail(_preview_of(r), _cite_of(r)) for r in shown)
    caption = ""
    if not recent_html:
        recent_html = empty(
            "Nothing ingested yet." if not ingested_count else
            f"{ingested_count} record{'s' if ingested_count != 1 else ''} ingested, but "
            "none could be listed here. That is a display fault, not an empty source — "
            "the knowledge base still holds them."
        )
    elif ingested_count > len(shown):
        # Said out loud, because "6 records" under a heading that reports 15 is the
        # same kind of apparent contradiction this panel was just fixed for.
        caption = (f'<p class="lbl">Newest {len(shown)} of {ingested_count}.</p>')

    body = (
        "<h2>Scopes</h2>" + scope_block + counts + gap + span
        + '<h2 style="margin-top:var(--s5)">Recently ingested</h2>' + caption + recent_html
    )
    return panel("What it can see", body)


# `ActionTier.GATED` is the agent's vocabulary, not a clinic administrator's. The word
# in the table answers the question actually being asked — "will this happen without
# me?" — and the machine's own name stays beside it, small and mono, because the person
# who has to read an audit row or argue with a vendor needs the exact enum too. TRIVIAL
# and INTERNAL share a word on purpose: to the person signing off, both mean "runs on
# its own", and the meter still separates them by consequence.
_TIER_WORDS: dict[ActionTier, str] = {
    ActionTier.TRIVIAL: "Automatic",
    ActionTier.INTERNAL: "Automatic",
    ActionTier.GATED: "Needs approval",
    ActionTier.FORBIDDEN: "Never permitted",
}

# `.lbl` uppercases; a URL path and a prose clause must not be shouted.
_SUB = 'class="lbl" style="margin-top:3px;text-transform:none"'
_SUB_MONO = 'class="lbl mono" style="margin-top:3px;text-transform:none"'


def _tier_cell(tier: ActionTier) -> str:
    word = _TIER_WORDS.get(tier, tier.name.title())
    return (
        f"<div>{esc(word)}</div>"
        f'<div class="lbl mono" style="margin-top:3px">{meter(tier.name)}</div>'
    )


def _trigger_cell(app_id: str, operation: str) -> str:
    """Whether a document Walnut has read may cause this write.

    Not the tier, and not a restatement of it: `email.flag` is TRIVIAL and still must
    not be triggerable by a poisoned message. `QUARANTINE_SAFE` is the single list
    that answers this, so the page reads it rather than inferring.
    """
    if is_quarantine_safe(app_id, operation):
        return (
            pill("Yes", "attention")
            + f"<div {_SUB}>additive and reversible, so ingested text may justify it</div>"
        )
    return pill("No", "ok") + f"<div {_SUB}>only a person can ask for this</div>"


def _declared_operations(source: CustomSource | None) -> dict[str, Any]:
    """The `operations` block the customer wrote, exactly as they configured it.

    Read from the source's own spec rather than from `capabilities()`, because this is
    the one thing `capabilities()` cannot say: the tier is in there, but *who chose it*
    is not — and that authorship is the entire extensibility claim.
    """
    if source is None:
        return {}
    declared = source.spec.get("operations")
    if not isinstance(declared, dict):
        return {}
    return {op: cfg for op, cfg in declared.items() if isinstance(cfg, dict)}


def _endpoint_of(operation: str, config: dict[str, Any] | None,
                 source: CustomSource | None) -> str:
    """`POST /api/holds`, if the configuration says so. Never guessed."""
    if config is not None and config.get("path"):
        return f"{str(config.get('method', '')).upper()} {config['path']}".strip()
    if operation == "annotate" and source is not None:
        path = source.spec.get("annotate_path")
        if path:
            return f"POST {path}"
    return ""


def _reversible_cell(app_id: str, operation: str, config: dict[str, Any] | None,
                     source: CustomSource | None) -> str:
    """Reversible means Walnut can actually undo it, not that undo exists in theory.

    Two different facts, both honest: `IRREVERSIBLE` is Walnut's own central list (a
    sent email cannot be recalled), and a declared operation is reversible only if its
    configuration gave an undo path — `RESTAdapter._undo_declared` raises otherwise,
    so claiming "yes" here would be a promise the adapter refuses to keep.
    """
    if not is_reversible(app_id, operation):
        return pill("No", "stop") + f"<div {_SUB}>Walnut records this as impossible to undo</div>"

    if config is not None:
        undo = config.get("undo")
        if isinstance(undo, dict) and undo.get("path"):
            endpoint = f"{str(undo.get('method', 'DELETE')).upper()} {undo['path']}"
            return pill("Yes", "ok") + f"<div {_SUB_MONO}>{esc(endpoint)}</div>"
        return (
            pill("No", "stop")
            + f"<div {_SUB}>the configuration declares no undo — reversing it is a "
              "manual job in the source system</div>"
        )

    if operation == "annotate" and source is not None and source.kind == "rest":
        path = source.spec.get("delete_path")
        if path:
            return pill("Yes", "ok") + f"<div {_SUB_MONO}>DELETE {esc(path)}</div>"
        return pill("No", "stop") + f"<div {_SUB}>no delete path configured</div>"

    return pill("Yes", "ok")


def _capabilities_panel(
    app_id: str,
    capabilities: ActionCapabilities | None,
    source: CustomSource | None = None,
) -> str:
    """(c) What it can write — one row per operation the adapter actually reports.

    Nothing here is authored by this page. The operations and their tiers come from
    `adapter.capabilities()`, quarantine-safety from `QUARANTINE_SAFE`, reversibility
    from `IRREVERSIBLE` and the source's own undo configuration. An adapter that
    reports nothing gets a sentence saying so, never an empty table — a blank table
    reads as "we checked and it is fine", which is the opposite of what it means.
    """
    if capabilities is None:
        return panel("What it can write", empty(
            "This connector did not report what it can write. Until it does, every "
            "write to it is refused as an unknown operation."
        ))
    if not capabilities.operations:
        return panel("What it can write", empty(
            "This connector reports no write operations. It is read-only — Walnut can "
            "cite it and cannot change it."
        ))

    declared = _declared_operations(source)
    endpoints = {
        op: _endpoint_of(op, declared.get(op), source)
        for op in capabilities.operations
    }
    show_endpoint = source is not None and any(endpoints.values())

    rows = []
    for op in sorted(capabilities.operations):
        config = declared.get(op)
        # `td` carries overflow-wrap:anywhere, which broke `release_hold` across two
        # lines once the endpoint column crowded it. An operation name is an
        # identifier — it is quoted in refusals and the audit log, and half of it on
        # each line is not the same string to the person matching them up.
        cells = [f'<code style="white-space:nowrap">{esc(op)}</code>']
        if show_endpoint:
            cells.append(
                f'<span class="mono">{esc(endpoints[op])}</span>' if endpoints[op]
                else '<span class="lbl">—</span>'
            )
            # Which of the two authors put this row here. The distinction is the
            # product claim: `place_hold` exists because the dispensary said so, and
            # `annotate` exists because every REST source gets it.
            cells.append(
                f"<b>{esc(source.name)}</b>'s config" if config is not None  # type: ignore[union-attr]
                else f"Walnut<div {_SUB}>every {esc(source.kind)} source gets this</div>"  # type: ignore[union-attr]
            )
        cells.extend([
            _tier_cell(capabilities.operations[op]),
            _trigger_cell(app_id, op),
            _reversible_cell(app_id, op, config, source),
        ])
        rows.append(tuple(cells))

    headers = ["Operation"]
    if show_endpoint:
        headers += ["Calls", "Declared by"]
    headers += ["What Walnut may do", "May ingested content trigger it?", "Reversible"]

    body = ""
    if declared:
        body += (
            f"<p><b>Walnut hardcodes none of this.</b> {esc(source.name)} declared "  # type: ignore[union-attr]
            f"{len(declared)} operation{'s' if len(declared) != 1 else ''} — the HTTP "
            "method, the path, and the tier — in its own configuration, and Walnut "
            "validated that declaration when it connected: a write with no explicit "
            "method, no path, or no tier is a refused connection, and there is no "
            "default tier. Walnut enforces the tier; it did not choose it. Nothing "
            "about a URL tells Walnut whether it stops a dose or starts one — only "
            "the people who run the system know that.</p>"
        )

    body += table(headers, rows)
    body += ('<p class="lbl" style="margin-top:var(--s3)">Any operation not listed '
             "here is refused as an unknown operation.</p>")

    aside = pill("Declared by the source", "quiet") if declared else ""
    return panel("What it can write", body, aside)


def _conformance_panel(source: CustomSource) -> str:
    """(c bis), custom-source case — the conformance report, below the capabilities.

    It used to *replace* the capability table, which quietly hid the most interesting
    thing about a customer-connected system: a source that declares its own writes was
    the one connector whose "what it can write" section had nothing in it.

    Consequence first: a reader should not have to parse a pass/fail table to learn
    whether this source is doing anything yet.
    """
    report = source.report
    if report is None:
        return panel(
            "Conformance",
            empty(source.error or "Not yet validated — registration did not complete."),
        )

    n_failed = len(report.failed)
    if n_failed:
        lead = (
            f'<p><b>This source contributes no evidence until {n_failed} check'
            f'{"s" if n_failed != 1 else ""} pass{"es" if n_failed == 1 else ""}.</b> '
            "A source that returns uncited evidence, or invents records instead of "
            "returning nothing, is not wired into the brain until it stops.</p>"
        )
        fail_table = table(
            ("Check", "Why it failed"),
            [(f"<code>{esc(name)}</code>", esc(why)) for name, why in report.failed],
        )
    else:
        lead = "<p><b>All checks pass.</b> This source contributes evidence to the brain.</p>"
        fail_table = ""

    counts = (
        f'<p class="lbl">{len(report.passed)} passed · {n_failed} failed · '
        f"{len(report.skipped)} skipped</p>"
    )
    return panel("Conformance", lead + fail_table + counts)


def _guidance_panel(
    conn: dict[str, Any] | None, spec: CredentialSpec | None, source: CustomSource | None,
) -> str:
    """(d) The connection error verbatim, and `spec.gotcha` as a persistent note.

    Previously `gotcha` rendered only on the disconnected credential form — exactly
    backwards, since the moment you most need to be told about the known mistake is
    after the connection has already failed because of it. It renders here regardless
    of connection state.
    """
    parts = []
    error = (conn or {}).get("error") if conn is not None else (source.error if source else "")
    if error:
        parts.append(rail(error, kind="stop"))
    elif source is not None and source.report is not None and source.report.failed:
        # `source.error` is set only when REGISTRATION itself raised. A source that
        # registered fine and then failed conformance — an unreachable service is the
        # common one — left this panel saying "No errors recorded" directly beneath a
        # conformance table reporting a refused connection. Same contradiction as the
        # ingest count; same fix. The reason is named here rather than restated in
        # full, because the panel above already carries the detail.
        why = source.report.failed[0][1]
        parts.append(rail(
            f"This source registered, then failed {len(source.report.failed)} "
            f"conformance check{'s' if len(source.report.failed) != 1 else ''}: {why}",
            kind="stop",
        ))
    else:
        parts.append(empty("No errors recorded."))

    if spec is not None and spec.gotcha:
        parts.append(f'<div class="note"><b>Known gotcha.</b> {esc(spec.gotcha)}</div>')

    return panel("Errors & guidance", "".join(parts))


def _connect_card(conn: dict[str, Any], spec: CredentialSpec, ingested_count: int) -> str:
    """The credential form — a card, because it is one thing with its own controls,
    one of exactly two places in this console that earns that treatment."""
    fields = "".join(
        f'<label>{esc(f.label)}{" (optional)" if f.optional else ""}</label>'
        f'<input name="{esc(f.key)}" type="{"password" if f.secret else "text"}" '
        f'placeholder="{esc(f.placeholder)}" autocomplete="off">'
        f'<p class="lbl">{esc(f.help)}</p>'
        for f in spec.fields
    )
    n = ingested_count
    body = (
        f'<form method="post" action="/connections/{esc(conn["app"])}/connect">'
        f'{fields}'
        '<div class="row" style="margin-top:var(--s4)">'
        f'<button class="btn" type="submit">Connect {esc(spec.display_name)}</button>'
        "</div></form>"
        f'<p class="lbl" style="margin-top:var(--s3)"><b>Where:</b> {esc(spec.where)}</p>'
        + (f'<div class="note"><b>Known gotcha.</b> {esc(spec.gotcha)}</div>' if spec.gotcha else "")
        + f'<p style="margin-top:var(--s3)">Currently serving {n} sample record'
          f'{"s" if n != 1 else ""}; connecting replaces them.</p>'
    )
    return panel(f"Connect {spec.display_name}", body)


def page_connector_detail(
    conn: dict[str, Any] | None,
    spec: CredentialSpec | None,
    source: CustomSource | None,
    capabilities: ActionCapabilities | None,
    ingested_count: int,
    oldest: str,
    newest: str,
    recent: list[Any],
) -> str:
    """Stacked panels in the order an admin's questions arrive: is it live, what can
    it see, what can it write, what is wrong and what do I do about it.

    A custom source gets the same "what it can write" table as a built-in — the tiers
    are the customer's own declaration rather than Walnut's, which is precisely why it
    is worth showing — followed by its conformance report.

    `conn`/`spec` describe a built-in app; `source` describes a custom one. Exactly one
    of `conn` and `source` is expected to be set — the caller picks which by app id.
    """
    is_custom = source is not None
    if conn is not None:
        app_id = conn["app"]
        display_name = conn["display_name"]
        blurb = spec.blurb if spec is not None else ""
    elif source is not None:
        app_id = source.name
        display_name = source.name
        blurb = f"Custom {source.kind} source."
    else:
        app_id, display_name, blurb = "unknown", "Unknown connector", ""

    word, kind = _status_of(conn, source)

    page_header = (
        f'<div class="ph"><h1>{esc(display_name)}</h1>'
        + (f"<p>{esc(blurb)}</p>" if blurb else "")
        + "</div>"
    )

    header_panel = _header_panel(app_id, display_name, word, kind, conn, source)
    seeing_panel = _seeing_panel(conn, source, ingested_count, oldest, newest, recent)

    writing_panel = _capabilities_panel(app_id, capabilities, source)
    if is_custom:
        writing_panel += _conformance_panel(source)  # type: ignore[arg-type]

    guidance_panel = _guidance_panel(conn, spec, source)

    connect_panel = ""
    if conn is not None and spec is not None and conn["state"] != "connected":
        connect_panel = _connect_card(conn, spec, ingested_count)

    return (
        page_header + header_panel + seeing_panel + writing_panel + guidance_panel
        + connect_panel
    )
