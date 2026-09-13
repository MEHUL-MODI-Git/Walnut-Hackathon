"""The console's design system: tokens, shell, and the component vocabulary.

Every screen imports from here and nothing else. One stylesheet, one layout, one set of
components — so a change to how a status pill looks changes it everywhere, and two
screens cannot quietly diverge.

**Direction: clinical instrument.** Warm paper rather than slate, one accent named after
the product, semantic colour reserved strictly for state. The reference points are lab
software and a well-set document, not a developer-tool landing page — this console shows
a clinic's patient data to the people responsible for it, and it should feel precise and
calm rather than exciting.

**The component rule, which is load-bearing:**

* **Table** when rows are comparable on identical axes and the job is a column scan.
  This is the default. Connectors, coverage, action catalogues, activity.
* **Rail list** when items are heterogeneous prose carrying a citation, read in
  sequence. Evidence, refusals, timeline entries. A 2px left rail and text — not a
  bordered box, because sixty rounded boxes is sixty competing rectangles.
* **Card** only when an item contains its own controls and there are few of them.
  Exactly two uses in the whole console: an approval request, and a credential form.

Everything is server-rendered f-strings. No build step and no JavaScript beyond
`<details>` — a toolchain that breaks at 3am is unrecoverable, and nothing here needs
one.
"""

from __future__ import annotations

import html
from typing import Any, Iterable

__all__ = [
    "CSS", "empty", "esc", "layout", "meter", "panel", "pill", "rail", "table",
]


def esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


CSS = """
:root{
  --paper:#FBFAF7; --surface:#FFFFFF; --ink:#1A1D1A; --rule:#E4E1D9;
  --muted:#63675F; --walnut:#7A4B2A;
  --ok:#2F6B4F; --attention:#8A5C0E; --stop:#9B3A2E; --quiet:#756F62;
  --rule-soft:#EDEAE2;
  --sidebar:228px; --content:1100px;
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:24px; --s6:32px; --s7:48px; --s8:64px;
  --r-sm:6px; --r-md:9px;
  --shadow-1:0 1px 2px rgba(30,26,20,.05), 0 1px 1px rgba(30,26,20,.03);
  --ease:180ms cubic-bezier(.2,.6,.2,1);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#171614; --surface:#1E1D1A; --ink:#EDEAE3; --rule:#34322D;
  --muted:#9B978D; --walnut:#C58B5E;
  --ok:#5FAE86; --attention:#D6A84A; --stop:#E0776A; --quiet:#8B8F84;
  --rule-soft:#2B2925;
  --shadow-1:0 1px 2px rgba(0,0,0,.28), 0 1px 1px rgba(0,0,0,.2);
}}
:root[data-theme="dark"]{
  --paper:#171614; --surface:#1E1D1A; --ink:#EDEAE3; --rule:#34322D;
  --muted:#9B978D; --walnut:#C58B5E;
  --ok:#5FAE86; --attention:#D6A84A; --stop:#E0776A; --quiet:#8B8F84;
  --rule-soft:#2B2925;
  --shadow-1:0 1px 2px rgba(0,0,0,.28), 0 1px 1px rgba(0,0,0,.2);
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:var(--paper);color:var(--ink);
  font:14px/1.6 "IBM Plex Sans",ui-sans-serif,-apple-system,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  display:flex;min-height:100vh}
a{color:var(--walnut);text-decoration:none}
a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--walnut);outline-offset:2px;border-radius:2px}
.mono,code{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:.93em;
  letter-spacing:-.01em}
.num{font-variant-numeric:tabular-nums}

/* ---- sidebar ---- */
aside{width:var(--sidebar);flex:0 0 var(--sidebar);background:var(--surface);
  border-right:1px solid var(--rule);display:flex;flex-direction:column;
  position:sticky;top:0;height:100vh}
.brand{padding:var(--s5) var(--s5) var(--s4)}
.brand b{display:block;font-size:16px;font-weight:600;letter-spacing:-.01em}
.brand span{display:block;font-size:11.5px;color:var(--muted);margin-top:3px;
  letter-spacing:.01em}
nav{padding:var(--s3) var(--s3);flex:1;overflow-y:auto}
nav .grp{font-size:10.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  color:var(--quiet);padding:var(--s5) var(--s3) var(--s2)}
nav .grp:first-child{padding-top:var(--s2)}
nav a{display:flex;align-items:center;justify-content:space-between;gap:var(--s2);
  padding:8px var(--s3);border-radius:var(--r-sm);color:var(--ink);font-size:13.5px;
  font-weight:450;transition:background-color var(--ease),color var(--ease)}
nav a:hover{background:var(--paper);text-decoration:none}
nav a.on{background:color-mix(in srgb,var(--walnut) 9%,var(--paper));
  color:var(--walnut);font-weight:600;box-shadow:inset 2px 0 0 var(--walnut)}
nav .badge{font-family:"IBM Plex Mono",monospace;font-size:10.5px;font-weight:500;
  padding:1.5px 6.5px;border-radius:999px;
  background:color-mix(in srgb,var(--attention) 16%,transparent);color:var(--attention)}
nav .badge.stop{background:color-mix(in srgb,var(--stop) 14%,transparent);color:var(--stop)}
.strip{border-top:1px solid var(--rule);padding:var(--s4) var(--s5);
  font-size:11px;color:var(--muted);line-height:1.7;letter-spacing:.01em}

/* ---- content ---- */
main{flex:1;min-width:0;padding:var(--s7) clamp(16px,3vw,48px) var(--s8);
  max-width:calc(var(--content) + 80px)}
.ph{margin-bottom:var(--s6)}
.ph h1{font-size:23px;font-weight:600;letter-spacing:-.017em;text-wrap:balance;
  line-height:1.3}
.ph p{font-size:13.5px;color:var(--muted);margin-top:var(--s2);max-width:66ch;
  line-height:1.6}
h2{font-size:15px;font-weight:600;letter-spacing:-.005em;margin:var(--s7) 0 var(--s4);
  text-wrap:balance}
h2:first-child{margin-top:0}
.lbl{font-size:10.5px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted)}

/* ---- panel ---- */
.panel{background:var(--surface);border:1px solid var(--rule);border-radius:var(--r-md);
  margin-bottom:var(--s5);box-shadow:var(--shadow-1)}
.panel>header{padding:var(--s4) var(--s5);border-bottom:1px solid var(--rule);
  display:flex;align-items:center;justify-content:space-between;gap:var(--s3)}
.panel>header h3{font-size:13.5px;font-weight:600;letter-spacing:-.003em}
.panel>.body{padding:var(--s5)}
.panel>.body>:last-child{margin-bottom:0}

/* ---- table ---- */
.tw{overflow-x:auto;border-radius:var(--r-md)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:10.5px;font-weight:600;letter-spacing:.07em;
  text-transform:uppercase;color:var(--muted);padding:var(--s3) var(--s4);
  border-bottom:1px solid var(--rule);white-space:nowrap;background:var(--surface)}
td{padding:var(--s3) var(--s4);border-bottom:1px solid var(--rule-soft);
  vertical-align:top;overflow-wrap:anywhere;line-height:1.55}
tbody tr{transition:background-color var(--ease)}
tbody tr:hover{background:var(--paper)}
tr:last-child td{border-bottom:0}
td.n{text-align:right;font-variant-numeric:tabular-nums;font-family:"IBM Plex Mono",monospace;
  font-size:.94em}

/* ---- pills & dots ---- */
.pill{display:inline-block;font-size:10.5px;font-weight:500;padding:2.5px 8px;
  border-radius:999px;white-space:nowrap;letter-spacing:.01em;
  background:color-mix(in srgb,var(--muted) 14%,transparent);color:var(--muted)}
.pill.ok{background:color-mix(in srgb,var(--ok) 15%,transparent);color:var(--ok)}
.pill.attention{background:color-mix(in srgb,var(--attention) 16%,transparent);color:var(--attention)}
.pill.stop{background:color-mix(in srgb,var(--stop) 14%,transparent);color:var(--stop)}
.pill.quiet{background:color-mix(in srgb,var(--quiet) 15%,transparent);color:var(--quiet)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;
  vertical-align:baseline}
.dot.found{background:var(--ok)}
.dot.nothing{background:transparent;box-shadow:inset 0 0 0 1.5px var(--quiet)}
.dot.miss{background:var(--attention);border-radius:1px;
  clip-path:polygon(50% 0,100% 100%,0 100%);width:8px;height:8px}

/* ---- tier meter (never colour alone) ---- */
.meter{display:inline-flex;gap:2px;vertical-align:middle;margin-right:6px}
.meter i{width:5px;height:9px;border-radius:1px;background:var(--rule);display:block}
.meter i.f{background:var(--ink)}
.meter.gated i.f{background:var(--attention)}
.meter.forbidden i.f{background:var(--stop)}

/* ---- rail list ---- */
.rail{border-left:2px solid var(--rule);padding:var(--s2) 0 var(--s2) var(--s4);
  margin-bottom:var(--s4)}
.rail.ok{border-color:var(--ok)} .rail.stop{border-color:var(--stop)}
.rail.attention{border-color:var(--attention)} .rail.quiet{border-color:var(--quiet)}
.rail .t{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.55}
.rail .c{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--muted);
  margin-top:var(--s2);overflow-wrap:anywhere;letter-spacing:-.005em}

/* ---- the briefing: built to be read at a glance, under pressure ---- */
.ask{display:flex;gap:var(--s2);margin-bottom:var(--s5)}
.ask input{flex:1;min-width:220px}
.ph .ident{font-size:16px;color:var(--ink);margin-top:var(--s2);font-weight:450;
  display:flex;align-items:baseline;gap:var(--s2);flex-wrap:wrap}
.ph .src{font-size:12.5px;color:var(--muted);margin-top:var(--s2);
  font-variant-numeric:tabular-nums}

/* one statement per row: label left, value right, citation folded away */
.ln{display:flex;gap:var(--s4);padding:11px 0;border-bottom:1px solid var(--rule-soft);
  align-items:baseline}
.ln:last-child{border-bottom:0}
.ln:first-child{padding-top:0}
.ln-l{flex:0 0 170px;font-size:12.5px;color:var(--muted);line-height:1.5}
.ln-v{flex:1;min-width:0;font-size:15px;line-height:1.55;overflow-wrap:anywhere}
.ln.warn .ln-v{font-weight:500}
.ln.warn .ln-l{color:var(--attention);font-weight:600}
@media(max-width:620px){.ln{display:block}.ln-l{margin-bottom:2px}}

/* the citation. Closed by default — a receipt you can reach, never noise you must read */
/* Closed, the chip sits inline at the end of the line it belongs to. Open, it becomes
   a block BELOW that line. An inline-block that grows is laid out on its baseline, so
   an opened receipt lifted itself over the row above and covered the very statement it
   was citing — the one arrangement a citation must never take. */
details.cite{display:inline-block;margin-left:var(--s2);vertical-align:baseline}
details.cite[open]{display:block;margin:var(--s2) 0 0}
details.cite>summary{display:inline-block;list-style:none;cursor:pointer;
  font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.02em;
  color:var(--muted);background:var(--paper);border:1px solid var(--rule);
  padding:1px 7px;border-radius:999px;transition:all var(--ease)}
details.cite>summary::-webkit-details-marker{display:none}
details.cite>summary:hover{color:var(--walnut);border-color:var(--walnut)}
details.cite[open]>summary{color:var(--walnut);border-color:var(--walnut)}
.cite-body{margin-top:var(--s2);padding:var(--s3);background:var(--paper);
  border-left:2px solid var(--walnut);border-radius:0 var(--r-sm) var(--r-sm) 0;
  font-size:13px;line-height:1.55;white-space:pre-wrap;overflow-wrap:anywhere;
  max-height:18em;overflow-y:auto}
.cite-body .c{margin-top:var(--s2);font-family:"IBM Plex Mono",monospace;
  font-size:11px;color:var(--muted);white-space:normal}

/* the thing a clinician must not miss */
.alert{border:1px solid color-mix(in srgb,var(--attention) 40%,var(--rule));
  background:color-mix(in srgb,var(--attention) 6%,var(--surface));
  border-radius:var(--r-md);margin-bottom:var(--s5);box-shadow:var(--shadow-1)}
.alert-h{padding:var(--s4) var(--s5);font-weight:600;font-size:14.5px;
  color:var(--attention);border-bottom:1px solid color-mix(in srgb,var(--attention) 22%,transparent)}
.alert-b{padding:var(--s3) var(--s5)}
.alert-f{padding:var(--s3) var(--s5) var(--s4);font-size:12.5px;color:var(--muted);
  border-top:1px solid color-mix(in srgb,var(--attention) 18%,transparent)}
.cov-lead{font-size:13.5px;margin-bottom:var(--s4);color:var(--ink)}
.cov-lead b{font-variant-numeric:tabular-nums}

/* ---- forms ---- */
label{display:block;font-size:11.5px;color:var(--muted);margin:var(--s4) 0 var(--s1)}
input,select{width:100%;background:var(--surface);border:1px solid var(--rule);
  color:var(--ink);padding:8px 10px;border-radius:var(--r-sm);font:inherit;
  transition:border-color var(--ease)}
input:focus{outline:none;border-color:var(--walnut)}
input.big{font-size:16px;padding:12px 14px;border-radius:var(--r-md)}
.btn{display:inline-block;background:var(--walnut);color:var(--surface);border:0;
  padding:8px 14px;border-radius:var(--r-sm);font:inherit;font-weight:500;cursor:pointer;
  letter-spacing:.002em;transition:filter var(--ease),background-color var(--ease)}
.btn:hover{filter:brightness(1.08)}
.btn.ghost{background:transparent;border:1px solid var(--rule);color:var(--ink)}
.btn.ghost:hover{background:var(--paper);filter:none}
.btn.danger{background:transparent;border:1px solid var(--stop);color:var(--stop)}
.btn.danger:hover{background:color-mix(in srgb,var(--stop) 8%,transparent);filter:none}
.row{display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap}
.chip{display:inline-block;font-size:11.5px;padding:3.5px 10px;border:1px solid var(--rule);
  border-radius:999px;background:var(--surface);margin:0 var(--s1) var(--s1) 0}
.chip b{font-weight:500}
.chip span{color:var(--muted);font-size:10.5px;margin-left:5px}
.empty{color:var(--muted);padding:var(--s7) var(--s4);text-align:center;font-size:13px;
  border:1px dashed var(--rule);border-radius:var(--r-md)}
.note{border-left:2px solid var(--attention);padding:var(--s3) var(--s4);
  font-size:12.5px;color:var(--muted);margin-top:var(--s4);line-height:1.55}
.note b{color:var(--ink);font-weight:600}
details>summary{cursor:pointer;color:var(--muted);font-size:12.5px;padding:var(--s2) 0}
@media(max-width:860px){
  body{flex-direction:column}
  aside{width:100%;flex:none;position:static;height:auto;
    border-right:0;border-bottom:1px solid var(--rule)}
  nav{display:flex;flex-wrap:wrap;gap:2px;padding:var(--s2)}
  nav .grp,.strip{display:none}
  main{padding:var(--s5) var(--s4) var(--s7)}
  .ph h1{font-size:20px}
}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

NAV = [
    ("KNOW", [
        ("/", "retrieval", "Retrieval"),
        ("/knowledge", "knowledge", "Knowledge base"),
        ("/connectors", "connectors", "Connectors"),
    ]),
    ("ACT", [
        ("/actions", "actions", "Actions"),
        ("/approvals", "approvals", "Approvals"),
        ("/audit", "audit", "Audit"),
    ]),
]


def layout(title: str, body: str, active: str = "", *, badges: dict[str, Any] | None = None,
           strip: str = "") -> str:
    """The shell. `badges` maps a nav key to a count; a badge means someone is blocked.

    Deliberately sparse: a badge is a count of things needing a human, or a fault. Never
    a vanity metric — if three things badge, none of them mean anything.
    """
    badges = badges or {}
    out = []
    for group, items in NAV:
        out.append(f'<div class="grp">{esc(group)}</div>')
        for href, key, label in items:
            badge = badges.get(key)
            kind = "stop" if key == "connectors" else ""
            chip = f'<span class="badge {kind}">{esc(badge)}</span>' if badge else ""
            on = " on" if active == key else ""
            out.append(f'<a href="{href}" class="{on.strip()}">{esc(label)}{chip}</a>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · Walnut</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style></head><body>
<aside>
  <div class="brand"><b>Walnut</b><span>Meridian Health</span></div>
  <nav>{"".join(out)}</nav>
  <div class="strip">{strip}</div>
</aside>
<main>{body}</main></body></html>"""


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


def panel(title: str, body: str, aside_html: str = "") -> str:
    return (f'<section class="panel"><header><h3>{esc(title)}</h3>{aside_html}</header>'
            f'<div class="body">{body}</div></section>')


def table(headers: Iterable[str], rows: Iterable[Iterable[str]],
          *, empty_text: str = "Nothing here.") -> str:
    """Rows already rendered as HTML cells. The default component, by design."""
    body = "".join(f"<tr>{''.join(f'<td>{c}</td>' for c in r)}</tr>" for r in rows)
    if not body:
        return empty(empty_text)
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    return f'<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def pill(text: str, kind: str = "") -> str:
    return f'<span class="pill {kind}">{esc(text)}</span>'


def rail(text: str, cite: str = "", kind: str = "", *, limit: int = 420) -> str:
    """Heterogeneous prose carrying a citation. Not a card — there are always many."""
    body = f'<div class="t">{esc(text[:limit])}</div>'
    if cite:
        body += f'<div class="c">{esc(cite)}</div>'
    return f'<div class="rail {kind}">{body}</div>'


def meter(tier_name: str) -> str:
    """Consequence as a 4-segment meter plus a word.

    Never colour alone: this is precisely the information that must not be misread,
    and roughly one reader in twelve cannot rely on hue to tell amber from green.
    """
    levels = {"TRIVIAL": 1, "INTERNAL": 2, "GATED": 3, "FORBIDDEN": 4}
    filled = levels.get(tier_name.upper(), 0)
    cls = {"GATED": "gated", "FORBIDDEN": "forbidden"}.get(tier_name.upper(), "")
    bars = "".join(f'<i class="{"f" if i < filled else ""}"></i>' for i in range(4))
    return f'<span class="meter {cls}">{bars}</span>{esc(tier_name.title())}'


def empty(text: str) -> str:
    return f'<div class="empty">{esc(text)}</div>'
