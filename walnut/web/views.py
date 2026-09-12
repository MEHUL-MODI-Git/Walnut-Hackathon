"""HTML rendering. Server-side f-strings, deliberately.

No React, no build step, no npm. At 3am during a hackathon window the failure mode of a
JavaScript toolchain is that it stops working for a reason unrelated to your product,
and you cannot ship. A Python function that returns a string cannot do that.

Everything is one stylesheet and a handful of render functions. The visual language is
borrowed from the domain: connection states are pills, evidence is always shown with
its citation attached, and refusals are styled as prominently as successes rather than
tucked away in red small print — a refusal is a correct outcome here, not an error.
"""

from __future__ import annotations

import html
from typing import Any

__all__ = ["layout", "page_approvals", "page_audit", "page_connections", "page_dashboard",
           "page_evidence", "page_investigate"]


def esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0c0e12; --panel:#14171d; --panel2:#1a1e26; --line:#262b35;
  --fg:#e6e9ef; --dim:#8a93a6; --faint:#5b6376;
  --ok:#3fb950; --warn:#d29922; --bad:#f85149; --accent:#6e9fff; --demo:#a371f7;
}
body{background:var(--bg);color:var(--fg);
  font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
  padding-block:0;min-height:100vh}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
code,pre,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
header{border-bottom:1px solid var(--line);background:var(--panel);
  padding:14px 20px;display:flex;align-items:center;gap:22px;flex-wrap:wrap}
header .brand{font-weight:650;letter-spacing:-.2px;font-size:15px}
header .brand span{color:var(--dim);font-weight:400;margin-left:8px;font-size:12.5px}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{color:var(--dim);padding:6px 11px;border-radius:7px;font-size:13px}
nav a:hover{background:var(--panel2);color:var(--fg);text-decoration:none}
nav a.on{background:var(--panel2);color:var(--fg)}
main{max-width:1080px;margin:0 auto;padding:26px 20px 80px}
h1{font-size:21px;font-weight:640;letter-spacing:-.3px;margin-bottom:6px}
h2{font-size:15px;font-weight:600;margin:30px 0 12px}
.sub{color:var(--dim);margin-bottom:22px;max-width:70ch}
.grid{display:grid;gap:13px;grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:16px}
.card h3{font-size:14px;font-weight:600;display:flex;align-items:center;
  justify-content:space-between;gap:10px;margin-bottom:5px}
.card p{color:var(--dim);font-size:13px;margin-bottom:12px}
.pill{font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px;
  text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}
.pill.connected{background:rgba(63,185,80,.13);color:var(--ok)}
.pill.demo{background:rgba(163,113,247,.13);color:var(--demo)}
.pill.error{background:rgba(248,81,73,.13);color:var(--bad)}
.stat{display:flex;gap:26px;flex-wrap:wrap;margin:18px 0 26px}
.stat div{min-width:96px}
.stat b{display:block;font-size:25px;font-weight:640;letter-spacing:-.5px}
.stat small{color:var(--dim);font-size:12px}
label{display:block;font-size:12.5px;color:var(--dim);margin:11px 0 4px}
input{width:100%;background:var(--bg);border:1px solid var(--line);color:var(--fg);
  padding:9px 11px;border-radius:7px;font-size:13px;font-family:inherit}
input:focus{outline:none;border-color:var(--accent)}
.btn{background:var(--accent);color:#05070c;border:0;padding:9px 15px;border-radius:7px;
  font-weight:600;font-size:13px;cursor:pointer;font-family:inherit}
.btn:hover{filter:brightness(1.1)}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
.btn.danger{background:transparent;border:1px solid var(--bad);color:var(--bad)}
.btn.ok{background:var(--ok);color:#04120a}
.row{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:14px}
.note{background:var(--panel2);border-left:2px solid var(--warn);padding:10px 13px;
  border-radius:0 7px 7px 0;font-size:12.5px;color:var(--dim);margin-top:12px}
.note b{color:var(--warn);font-weight:600}
.where{font-size:12px;color:var(--faint);margin-top:9px}
.evidence{background:var(--panel2);border:1px solid var(--line);border-radius:9px;
  padding:12px 14px;margin-bottom:9px}
.evidence .txt{margin-bottom:7px;white-space:pre-wrap;overflow-wrap:anywhere}
.cite{font-size:11.5px;color:var(--faint);font-family:ui-monospace,monospace;
  overflow-wrap:anywhere}
.cite b{color:var(--dim);font-weight:600}
.conflict{border:1px solid rgba(210,153,34,.35);background:rgba(210,153,34,.05);
  border-radius:11px;padding:15px;margin-bottom:13px}
.conflict .hd{color:var(--warn);font-weight:640;font-size:13px;margin-bottom:8px;
  display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.refusal{border:1px solid rgba(248,81,73,.35);background:rgba(248,81,73,.05);
  border-radius:11px;padding:15px;margin-bottom:13px}
.refusal .hd{color:var(--bad);font-weight:640;font-size:13px;margin-bottom:7px}
.taint{margin-top:10px;padding-top:10px;border-top:1px solid rgba(248,81,73,.2)}
.taint div{font-family:ui-monospace,monospace;font-size:11.5px;color:var(--dim);
  overflow-wrap:anywhere;margin-top:3px}
.done{border:1px solid rgba(63,185,80,.3);background:rgba(63,185,80,.05);
  border-radius:9px;padding:11px 14px;margin-bottom:8px;font-size:13px}
.done b{color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:600;font-size:11.5px;
  text-transform:uppercase;letter-spacing:.4px;padding:7px 10px;
  border-bottom:1px solid var(--line)}
td{padding:9px 10px;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.empty{color:var(--faint);text-align:center;padding:44px 20px;
  border:1px dashed var(--line);border-radius:11px}
.tablewrap{overflow-x:auto}
@media(max-width:520px){main{padding:18px 14px 60px}.stat{gap:16px}}
"""


def layout(title: str, body: str, active: str = "") -> str:
    def nav(href: str, key: str, label: str) -> str:
        return f'<a href="{href}" class="{"on" if active == key else ""}">{label}</a>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · Walnut</title><style>{CSS}</style></head><body>
<header>
  <div class="brand">Walnut<span>company brain · Meridian Health</span></div>
  <nav>
    {nav("/", "dash", "Overview")}
    {nav("/connections", "conn", "Connections")}
    {nav("/sources", "src", "Sources")}
    {nav("/investigate", "inv", "Investigate")}
    {nav("/approvals", "appr", "Approvals")}
    {nav("/evidence", "ev", "Evidence")}
    {nav("/audit", "audit", "Audit")}
  </nav>
</header>
<main>{body}</main></body></html>"""


# ---------------------------------------------------------------------------


def _conn_card(c: dict[str, Any], spec: Any) -> str:
    state = c["state"]
    detail = ""
    if state == "connected":
        scopes = ", ".join(c["scopes"][:4]) or "—"
        more = f" +{len(c['scopes']) - 4} more" if len(c["scopes"]) > 4 else ""
        detail = (f'<p style="color:var(--ok)">Live · sees {esc(scopes)}{more}</p>')
    elif state == "error":
        detail = f'<p style="color:var(--bad)">{esc(c["error"])}</p>'
    else:
        detail = '<p>Using seeded Meridian Health data. Fully functional.</p>'

    if state == "connected":
        action = (f'<form method="post" action="/connections/{c["app"]}/disconnect">'
                  f'<button class="btn danger">Disconnect</button></form>')
    else:
        fields = "".join(
            f'<label>{esc(f.label)}'
            f'{" <span style=\'color:var(--faint)\'>(optional)</span>" if f.optional else ""}'
            f'</label>'
            f'<input name="{esc(f.key)}" type="{"password" if f.secret else "text"}" '
            f'placeholder="{esc(f.placeholder)}" autocomplete="off">'
            f'<div class="where">{esc(f.help)}</div>'
            for f in spec.fields
        )
        action = (
            f'<form method="post" action="/connections/{c["app"]}/connect">{fields}'
            f'<div class="row"><button class="btn">Connect {esc(spec.display_name)}</button></div>'
            f'</form>'
            f'<div class="where" style="margin-top:11px"><b>Where:</b> {esc(spec.where)}</div>'
            + (f'<div class="note"><b>Gotcha.</b> {esc(spec.gotcha)}</div>' if spec.gotcha else "")
        )

    return f"""<div class="card">
      <h3>{esc(spec.display_name)}<span class="pill {state}">{esc(state)}</span></h3>
      <p>{esc(spec.blurb)}</p>{detail}{action}</div>"""


def page_connections(connections: list[dict[str, Any]], specs: dict[str, Any]) -> str:
    cards = "".join(_conn_card(c, specs[c["app"]]) for c in connections)
    return f"""<h1>Connections</h1>
<p class="sub">Five systems, each read <em>and</em> write. Every one works on seeded data
out of the box — connect a real account and the live adapter swaps in behind the same
contract. Credentials are held in memory for this process only, never written to disk
and never logged.</p>
<p class="sub" style="color:var(--faint)">A credential is validated by <em>using</em> it:
Walnut calls <code>probe()</code> and shows you what the token can actually see. A token
that parses but reads nothing is reported as an error, not a success.</p>
<div class="grid">{cards}</div>"""


def page_dashboard(stats: dict[str, Any], conn_summary: dict[str, int],
                   ledger: dict[str, Any], conflicts: int) -> str:
    apps = ", ".join(stats.get("apps", [])) or "none yet"
    return f"""<h1>Overview</h1>
<p class="sub">One graph of cited facts drawn from five fragmented systems, and an agent
that may act on them only with evidence.</p>
<div class="stat">
  <div><b>{stats.get("facts_indexed", 0)}</b><small>facts</small></div>
  <div><b>{len(stats.get("apps", []))}</b><small>apps ingested</small></div>
  <div><b>{conflicts}</b><small>contradictions</small></div>
  <div><b>{ledger.get("executed", 0)}</b><small>actions taken</small></div>
  <div><b>{ledger.get("refused", 0)}</b><small>refused</small></div>
  <div><b>{conn_summary.get("connected", 0)}/{conn_summary.get("total", 5)}</b>
       <small>live accounts</small></div>
</div>
<div class="card">
  <h3>Ingested from</h3><p>{esc(apps)}</p>
  <form method="post" action="/ingest"><div class="row">
    <button class="btn">Re-ingest all apps</button>
    <a class="btn ghost" href="/investigate">Investigate →</a>
  </div></form>
</div>
<h2>What this proves</h2>
<div class="grid">
  <div class="card"><h3>No claim without a source</h3>
    <p>Evidence cannot be constructed without a resolvable pointer and a real sha256
    content hash. When the source moves, the hash moves, and the citation stops
    verifying.</p></div>
  <div class="card"><h3>No action without evidence</h3>
    <p>An action carrying no justifying evidence raises at construction. It is not a
    check that can be skipped — the object cannot be built.</p></div>
  <div class="card"><h3>No deletion</h3>
    <p>Every write is reversible, and undo retracts rather than erases: messages are
    edited to show withdrawal, issues archived, properties restored with the note left
    in place.</p></div>
</div>"""


def _evidence_block(text: str, cite: str) -> str:
    return (f'<div class="evidence"><div class="txt">{esc(text[:420])}</div>'
            f'<div class="cite">{esc(cite)}</div></div>')


def page_investigate(question: str, answer: Any, conflicts: list[Any],
                     results: list[Any] | None, intent: Any = None) -> str:
    body = [f"""<h1>Investigate</h1>
<p class="sub">Ask across all five systems. Every claim renders with its citation, or it
does not render at all.</p>
<form method="post" action="/investigate"><div class="row" style="margin-top:0">
  <input name="question" value="{esc(question)}" style="flex:1;min-width:260px"
         placeholder="e.g. is the dosing engine v2 fix actually shipped?">
  <button class="btn">Ask</button></div></form>"""]

    body.append("""<h2>Or tell it what to do</h2>
<form method="post" action="/request"><div class="row" style="margin-top:0">
  <input name="request" style="flex:1;min-width:260px"
         placeholder="e.g. file a Linear issue about MED-412 and comment on the PR">
  <button class="btn">Propose</button></div></form>
<p class="sub" style="margin-top:9px;color:var(--faint)">Proposed actions still go
through the same checks as everything else — a misread sentence produces a refused
action, never a wrong write. Ambiguity becomes a question rather than a guess.</p>""")

    if intent is not None:
        if intent.proposed:
            body.append("<h2>Proposed</h2>")
            for prop in intent.proposed:
                body.append(f"""<div class="card" style="margin-bottom:10px">
                  <h3>{esc(prop.action.app)}.{esc(prop.action.operation)}
                    <span class="pill demo">from &ldquo;{esc(prop.matched_phrase)}&rdquo;</span></h3>
                  <p>Justified by {esc(", ".join(f.node_id for f in prop.evidence))}</p>
                  {"".join(_evidence_block(f.text, f.cite()) for f in prop.evidence)}
                </div>""")
            body.append("""<form method="post" action="/request/execute">
              <div class="row"><button class="btn">Execute all proposed</button></div>
              </form>""")
        for question in intent.clarifications:
            body.append(f'<div class="refusal"><div class="hd">Needs clarification</div>'
                        f'<div style="color:var(--dim);font-size:12.5px">{esc(question)}</div></div>')

    if answer is not None:
        if answer.facts:
            body.append("<h2>Facts</h2>")
            body.extend(_evidence_block(c.text, f[0].cite()) for c, f in answer.facts)
        if answer.analysis:
            body.append(f'<div class="note" style="border-left-color:var(--accent)">'
                        f'<b>Analysis.</b> {esc(answer.analysis)}</div>')
        if answer.refusals:
            body.append("<h2>Refused</h2>")
            body.extend(f'<div class="refusal"><div class="hd">{esc(w)}</div>'
                        f'<div style="color:var(--dim);font-size:12.5px">{esc(why)}</div></div>'
                        for w, why in answer.refusals)

    if conflicts:
        body.append(f"<h2>Contradictions ({len(conflicts)})</h2>")
        for c in conflicts[:6]:
            body.append(f"""<div class="conflict">
              <div class="hd">{esc(c.subject)}
                <span class="pill demo">{esc(c.apps[0])} vs {esc(c.apps[1])}</span></div>
              <div style="color:var(--dim);font-size:12.5px;margin-bottom:11px">
                {esc(c.explanation)}</div>
              {_evidence_block(c.left.fact.text, c.left.fact.cite())}
              {_evidence_block(c.right.fact.text, c.right.fact.cite())}
              <form method="post" action="/act">
                <input type="hidden" name="conflict_id" value="{esc(c.id)}">
                <button class="btn">Propose actions across all five apps</button></form>
            </div>""")

    if results:
        body.append("<h2>Result</h2>")
        for r in results:
            if hasattr(r, "reason"):
                taint = ""
                if getattr(r, "taint_path", ()):
                    taint = ('<div class="taint">taint path'
                             + "".join(f"<div>{esc(p)}</div>" for p in r.taint_path)
                             + "</div>")
                body.append(f"""<div class="refusal">
                  <div class="hd">REFUSED · {esc(r.action.app)}.{esc(r.action.operation)}
                    — {esc(r.reason.value)}</div>
                  <div style="color:var(--dim);font-size:12.5px">{esc(r.explanation)}</div>
                  {taint}</div>""")
            else:
                body.append(f'<div class="done"><b>done</b> &nbsp;'
                            f'{esc(r.action.app)}.{esc(r.action.operation)} '
                            f'<span class="cite">→ {esc(r.action_id)}</span></div>')
    return "".join(body)


def _undo_cell(receipt: Any) -> str:
    """Undo button, or an honest explanation of why there isn't one."""
    from ..actions.governance import is_reversible

    if receipt.is_undone:
        return ""
    if not is_reversible(receipt.action.app, receipt.action.operation):
        return '<span class="cite">cannot be undone</span>'
    return (f'<form method=post action=/undo/{esc(receipt.action_id)}>'
            f'<button class="btn ghost">Undo</button></form>')


def page_approvals(pending: list[tuple[str, Any, str]], history: list[Any]) -> str:
    body = ["""<h1>Approvals</h1>
<p class="sub">Anything customer-facing, bulk or destructive stops here. An unanswered
request is a refusal, never an approval — the gate fails closed.</p>"""]

    if not pending:
        body.append('<div class="empty">Nothing awaiting approval.</div>')
    for key, action, context in pending:
        body.append(f"""<div class="card" style="margin-bottom:13px">
          <h3>{esc(action.app)}.{esc(action.operation)}
            <span class="pill error">gated</span></h3>
          <p>{esc(action.rationale)}</p>
          <div class="evidence"><div class="txt">{esc(context)}</div></div>
          <div class="row">
            <form method="post" action="/approvals/{esc(key)}/approve">
              <button class="btn ok">Approve and execute</button></form>
            <form method="post" action="/approvals/{esc(key)}/deny">
              <button class="btn danger">Deny</button></form>
          </div></div>""")

    if history:
        rows = "".join(
            f"<tr><td class='mono'>{esc(r.action_id)}</td>"
            f"<td>{esc(r.action.app)}.{esc(r.action.operation)}</td>"
            f"<td>{'undone' if r.is_undone else 'live'}</td>"
            f"<td>{_undo_cell(r)}</td></tr>"
            for r in history
        )
        body.append(f"""<h2>Action ledger</h2><div class="tablewrap"><table>
          <tr><th>id</th><th>action</th><th>state</th><th></th></tr>{rows}
          </table></div>""")
    return "".join(body)


def page_evidence(facts: list[Any], q: str) -> str:
    rows = "".join(
        f'<div class="evidence"><div class="txt">{esc(f.text[:300])}</div>'
        f'<div class="cite"><b>{esc(f.node_id)}</b> · {esc(f.cite())}</div></div>'
        for f in facts[:60]
    )
    return f"""<h1>Evidence</h1>
<p class="sub">Everything the brain holds, each item with the pointer it came from.
Nothing enters without provenance.</p>
<form method="get" action="/evidence"><div class="row" style="margin-top:0">
  <input name="q" value="{esc(q)}" placeholder="filter…" style="flex:1;min-width:220px">
  <button class="btn">Search</button></div></form>
<h2>{len(facts)} fact(s)</h2>{rows or '<div class="empty">Nothing ingested yet.</div>'}"""


def page_audit(decisions: dict[str, Any], ledger: dict[str, Any],
               refusals: list[Any], identities: dict[str, Any] | None) -> str:
    """Everything the system decided and why — the answer to 'how do you know?'."""
    outcomes = decisions.get("outcomes", {}) or {}
    cats = decisions.get("categories", {}) or {}

    cat_rows = "".join(
        f"<tr><td class='mono'>{esc(k)}</td><td>{esc(v)}</td></tr>"
        for k, v in sorted(cats.items(), key=lambda kv: -kv[1])[:14]
    ) or "<tr><td colspan=2>nothing yet</td></tr>"

    ref_blocks = "".join(
        f"""<div class="refusal">
          <div class="hd">{esc(r.action.app)}.{esc(r.action.operation)}
            — {esc(r.reason.value)}</div>
          <div style="color:var(--dim);font-size:12.5px">{esc(r.explanation)}</div>
          {'<div class="taint">' + "".join(f"<div>{esc(p)}</div>" for p in r.taint_path)
           + "</div>" if getattr(r, "taint_path", ()) else ""}
        </div>"""
        for r in refusals[:8]
    ) or '<div class="empty">No refusals recorded on this run.</div>'

    ident = ""
    if identities:
        ident = f"""<h2>Identity resolution</h2>
        <div class="stat">
          <div><b>{identities.get('people', 0)}</b><small>people</small></div>
          <div><b>{identities.get('identities', 0)}</b><small>identities</small></div>
          <div><b>{identities.get('cross_app', 0)}</b><small>cross-app</small></div>
          <div><b>{identities.get('needs_human_review', 0)}</b><small>held for a human</small></div>
        </div>
        <div class="note"><b>Uncertain matches are never merged.</b> Fusing two
        different real people is not a degraded answer, it is a data-protection
        incident — so the uncertain band routes to a person rather than to a
        threshold.</div>"""

    return f"""<h1>Audit</h1>
<p class="sub">Every decision this system made, and the evidence behind it. This page is
the answer to "how do you know it works" — not a claim that it does, a record of what
it actually did.</p>
<div class="stat">
  <div><b>{decisions.get('total_decisions', 0)}</b><small>decisions</small></div>
  <div><b>{outcomes.get('executed', 0)}</b><small>executed</small></div>
  <div><b>{outcomes.get('refused', 0)}</b><small>refused</small></div>
  <div><b>{ledger.get('undone', 0)}</b><small>undone</small></div>
</div>
<h2>Refusals</h2>{ref_blocks}
<h2>Decisions by category</h2>
<div class="tablewrap"><table><tr><th>category</th><th>count</th></tr>{cat_rows}</table></div>
{ident}
<h2>What this does not prove</h2>
<div class="note"><b>Stated because a reliability page that only lists successes is not
one.</b> Taint detection is a tripwire, not a perimeter — it is safe to rely on only
because ingested content can never justify a state-changing action regardless of what
it says, so a missed pattern costs the explanation, not the outcome. Contradiction
detection is lexical and will miss anything phrased without status vocabulary. Identity
resolution is deterministic, not calibrated.</div>"""


def page_sources(sources: list[Any], plugin_dir: str) -> str:
    """Custom sources: bring your own database, API, or Python adapter."""
    cards = ""
    for s in sources:
        status = s.status()
        pill = {"ready": "connected", "failing": "error",
                "error": "error", "unchecked": "demo"}[status]
        detail = f'<p>{esc(s.summary())}</p>'
        if s.report and s.report.failed:
            detail += ('<div class="note"><b>Not yet conforming.</b><br>'
                       + "<br>".join(f"{esc(n)}: {esc(w)}" for n, w in s.report.failed[:5])
                       + "</div>")
        if s.error:
            detail += f'<div class="note"><b>Error.</b> {esc(s.error[:400])}</div>'
        cards += f"""<div class="card">
          <h3>{esc(s.name)} <span class="pill {pill}">{esc(status)}</span></h3>
          <p style="color:var(--faint)">{esc(s.kind)} source</p>{detail}
          <form method="post" action="/sources/{esc(s.name)}/remove">
            <button class="btn danger">Remove</button></form></div>"""

    return f"""<h1>Custom sources</h1>
<p class="sub">The five built-in connectors are a starting point, not the product. A
company's real estate is its own databases, its internal wiki, and a ticketing service
somebody wrote years ago — none of which will ever ship as a first-party integration.</p>
<p class="sub"><b>A custom source is not trusted, it is tested.</b> Registering one runs
the same behavioural conformance suite the five built-in adapters pass, and reports
exactly which guarantees hold. A source that returns uncited evidence, or invents
records instead of returning nothing, is reported as failing before it can put anything
into the brain.</p>

<div class="grid">{cards or '<div class="empty">No custom sources yet.</div>'}</div>

<h2>Add a database</h2>
<div class="card"><form method="post" action="/sources/add">
  <input type="hidden" name="kind" value="sql">
  <label>Name</label><input name="name" placeholder="support_db">
  <label>Connection string</label>
  <input name="dsn" placeholder="sqlite:///./support.db">
  <div class="where">SQLite works with no setup. Postgres and MySQL need their driver.</div>
  <label>Query</label>
  <input name="query" placeholder="SELECT id, subject, body, author, created_at FROM tickets">
  <label>ID column</label><input name="id_column" placeholder="id">
  <label>Text columns (comma separated)</label>
  <input name="text_columns" placeholder="subject, body">
  <label>Link template (optional)</label>
  <input name="uri_template" placeholder="https://support.internal/ticket/{{id}}">
  <div class="row"><button class="btn">Add and validate</button></div>
</form>
<div class="note"><b>Walnut never writes to your tables.</b> A SQL source is read-only;
annotations go to a separate companion table it creates itself.</div></div>

<h2>Add an internal API</h2>
<div class="card"><form method="post" action="/sources/add">
  <input type="hidden" name="kind" value="rest">
  <label>Name</label><input name="name" placeholder="internal_wiki">
  <label>Base URL</label><input name="base_url" placeholder="https://wiki.internal">
  <label>List path</label><input name="list_path" placeholder="/api/articles">
  <label>Item path (optional)</label>
  <input name="item_path" placeholder="/api/articles/{{id}}">
  <label>Records key (optional)</label>
  <input name="records_key" placeholder="data.items">
  <label>ID field</label><input name="id_field" placeholder="id">
  <label>Text fields (comma separated)</label>
  <input name="text_fields" placeholder="title, body">
  <label>Auth header (optional)</label>
  <input name="auth_header" placeholder="Bearer …">
  <div class="row"><button class="btn">Add and validate</button></div>
</form></div>

<h2>Add anything else</h2>
<div class="card">
  <p>For a source with no SQL or HTTP surface, drop a Python file into
  <code>{esc(plugin_dir)}/</code> implementing the six-method contract, exposing either
  <code>build()</code> or <code>ADAPTER</code>. It is discovered, validated against the
  same suite, and reported here.</p>
  <p style="color:var(--faint)">Loading a plugin executes that file — the same trust
  model as a pytest conftest. Point the directory only at code you would run yourself.</p>
  <form method="post" action="/sources/rescan">
    <div class="row"><button class="btn ghost">Rescan plugin directory</button></div>
  </form>
</div>"""
