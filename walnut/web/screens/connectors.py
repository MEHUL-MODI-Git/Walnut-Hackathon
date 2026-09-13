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
from ...contract import ActionCapabilities
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


def _builtin_row(c: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
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
    return (name, pill(word, kind), scope, _num(c["record_estimate"]),
            esc(_fmt_time(c["connected_at"])), note)


def _custom_row(s: CustomSource) -> tuple[str, str, str, str, str, str]:
    word, kind = _CUSTOM_STATUS.get(s.status(), (s.status().title(), "quiet"))
    name = f'<a href="/connectors/{esc(s.name)}">{esc(s.name)}</a>'
    return (name, pill(word, kind), esc(s.kind), '<span class="num">—</span>', "—",
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

    builtin_table = table(_COLUMNS, [_builtin_row(c) for c in connections],
                          empty_text="No built-in apps configured.")
    custom_table = table(_COLUMNS, [_custom_row(s) for s in sources],
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


def _header_panel(
    app_id: str, display_name: str, word: str, kind: str,
    conn: dict[str, Any] | None,
) -> str:
    """(a) Is it live, and when did we last check."""
    lines = []
    if conn is not None:
        lines.append(
            f'<p><span class="lbl">Connected</span> '
            f'{esc(_fmt_time(conn["connected_at"]))}</p>'
        )
    else:
        lines.append('<p><span class="lbl">Connected</span> —</p>')

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

    recent_html = "".join(
        rail(_text_of(r), _cite_of(r)) for r in recent[:8]
    ) or empty("Nothing ingested yet.")

    body = (
        "<h2>Scopes</h2>" + scope_block + counts + gap + span
        + '<h2 style="margin-top:var(--s5)">Recently ingested</h2>' + recent_html
    )
    return panel("What it can see", body)


def _capabilities_panel(app_id: str, capabilities: ActionCapabilities | None) -> str:
    """(c), built-in case — what it can write, and what a poisoned document could
    actually cause here, per operation rather than in a paragraph."""
    if capabilities is None or not capabilities.operations:
        return panel("What it can write", empty("No capabilities reported."))

    rows = []
    for op in sorted(capabilities.operations):
        tier = capabilities.operations[op]
        reversible = is_reversible(app_id, op)
        quarantine_safe = is_quarantine_safe(app_id, op)
        rows.append((
            f"<code>{esc(op)}</code>",
            meter(tier.name),
            pill("yes", "ok") if reversible else pill("no", "stop"),
            pill("yes", "attention") if quarantine_safe else pill("no", "ok"),
        ))

    return panel(
        "What it can write",
        table(("Operation", "Tier", "Reversible", "May ingested content trigger it?"), rows),
    )


def _conformance_panel(source: CustomSource) -> str:
    """(c), custom-source case — the conformance report replaces the capability table.

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
    """Four stacked panels in the order an admin's questions arrive: is it live, what
    can it see, what can it write, what is wrong and what do I do about it.

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

    header_panel = _header_panel(app_id, display_name, word, kind, conn)
    seeing_panel = _seeing_panel(conn, source, ingested_count, oldest, newest, recent)

    if is_custom:
        writing_panel = _conformance_panel(source)  # type: ignore[arg-type]
    else:
        writing_panel = _capabilities_panel(app_id, capabilities)

    guidance_panel = _guidance_panel(conn, spec, source)

    connect_panel = ""
    if conn is not None and spec is not None and conn["state"] != "connected":
        connect_panel = _connect_card(conn, spec, ingested_count)

    return (
        page_header + header_panel + seeing_panel + writing_panel + guidance_panel
        + connect_panel
    )
