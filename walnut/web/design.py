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
  --paper:#FBFAF7; --surface:#FFFFFF; --ink:#1A1D1A; --rule:#E3E0D8;
  --muted:#6B6F6A; --walnut:#7A4B2A;
  --ok:#2F6B4F; --attention:#9A6A17; --stop:#9B3A2E; --quiet:#8C9088;
  --sidebar:216px; --content:1100px;
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:24px; --s6:32px; --s7:48px;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#171614; --surface:#1E1D1A; --ink:#EDEAE3; --rule:#34322D;
  --muted:#9B978D; --walnut:#C58B5E;
  --ok:#5FAE86; --attention:#D6A84A; --stop:#E0776A; --quiet:#7E837B;
}}
:root[data-theme="dark"]{
  --paper:#171614; --surface:#1E1D1A; --ink:#EDEAE3; --rule:#34322D;
  --muted:#9B978D; --walnut:#C58B5E;
  --ok:#5FAE86; --attention:#D6A84A; --stop:#E0776A; --quiet:#7E837B;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--paper);color:var(--ink);
  font:13px/1.5 "IBM Plex Sans",ui-sans-serif,-apple-system,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased;display:flex;min-height:100vh}
a{color:var(--walnut);text-decoration:none}
a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--walnut);outline-offset:2px}
.mono,code{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:.92em}
.num{font-variant-numeric:tabular-nums}

/* ---- sidebar ---- */
aside{width:var(--sidebar);flex:0 0 var(--sidebar);background:var(--surface);
  border-right:1px solid var(--rule);display:flex;flex-direction:column;
  position:sticky;top:0;height:100vh}
.brand{padding:var(--s5) var(--s4) var(--s4);border-bottom:1px solid var(--rule)}
.brand b{display:block;font-size:15px;font-weight:600;letter-spacing:-.01em}
.brand span{display:block;font-size:11.5px;color:var(--muted);margin-top:2px}
nav{padding:var(--s4) var(--s2);flex:1;overflow-y:auto}
nav .grp{font-size:10.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);padding:var(--s3) var(--s3) var(--s2)}
nav a{display:flex;align-items:center;justify-content:space-between;gap:var(--s2);
  padding:7px var(--s3);border-radius:3px;color:var(--ink);font-size:13px}
nav a:hover{background:var(--paper);text-decoration:none}
nav a.on{background:var(--paper);color:var(--walnut);font-weight:500;
  box-shadow:inset 2px 0 0 var(--walnut)}
nav .badge{font-family:"IBM Plex Mono",monospace;font-size:10.5px;font-weight:500;
  padding:1px 6px;border-radius:999px;background:color-mix(in srgb,var(--attention) 16%,transparent);
  color:var(--attention)}
nav .badge.stop{background:color-mix(in srgb,var(--stop) 14%,transparent);color:var(--stop)}
.strip{border-top:1px solid var(--rule);padding:var(--s3) var(--s4);
  font-size:11px;color:var(--muted);line-height:1.55}

/* ---- content ---- */
main{flex:1;min-width:0;padding:var(--s6) clamp(16px,3vw,40px) var(--s7);
  max-width:calc(var(--content) + 80px)}
.ph{margin-bottom:var(--s5)}
.ph h1{font-size:20px;font-weight:600;letter-spacing:-.015em}
.ph p{color:var(--muted);margin-top:var(--s1);max-width:70ch}
h2{font-size:13px;font-weight:600;letter-spacing:.02em;margin:var(--s6) 0 var(--s3)}
h2:first-child{margin-top:0}
.lbl{font-size:10.5px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted)}

/* ---- panel ---- */
.panel{background:var(--surface);border:1px solid var(--rule);border-radius:4px;
  margin-bottom:var(--s4)}
.panel>header{padding:var(--s3) var(--s4);border-bottom:1px solid var(--rule);
  display:flex;align-items:center;justify-content:space-between;gap:var(--s3)}
.panel>header h3{font-size:12.5px;font-weight:600}
.panel>.body{padding:var(--s4)}
.panel>.body>:last-child{margin-bottom:0}

/* ---- table ---- */
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-size:10.5px;font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted);padding:var(--s2) var(--s3);
  border-bottom:1px solid var(--rule);white-space:nowrap}
td{padding:var(--s2) var(--s3);border-bottom:1px solid var(--rule);
  vertical-align:top;overflow-wrap:anywhere}
tr:last-child td{border-bottom:0}
td.n{text-align:right;font-variant-numeric:tabular-nums;font-family:"IBM Plex Mono",monospace}

/* ---- pills & dots ---- */
.pill{display:inline-block;font-size:10.5px;font-weight:500;padding:2px 8px;
  border-radius:999px;white-space:nowrap;
  background:color-mix(in srgb,var(--muted) 14%,transparent);color:var(--muted)}
.pill.ok{background:color-mix(in srgb,var(--ok) 15%,transparent);color:var(--ok)}
.pill.attention{background:color-mix(in srgb,var(--attention) 17%,transparent);color:var(--attention)}
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
.rail{border-left:2px solid var(--rule);padding:var(--s1) 0 var(--s1) var(--s3);
  margin-bottom:var(--s3)}
.rail.ok{border-color:var(--ok)} .rail.stop{border-color:var(--stop)}
.rail.attention{border-color:var(--attention)} .rail.quiet{border-color:var(--quiet)}
.rail .t{white-space:pre-wrap;overflow-wrap:anywhere}
.rail .c{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--muted);
  margin-top:var(--s1);overflow-wrap:anywhere}

/* ---- forms ---- */
label{display:block;font-size:11.5px;color:var(--muted);margin:var(--s3) 0 var(--s1)}
input,select{width:100%;background:var(--paper);border:1px solid var(--rule);
  color:var(--ink);padding:7px 9px;border-radius:3px;font:inherit}
input:focus{outline:none;border-color:var(--walnut)}
input.big{font-size:16px;padding:11px 13px}
.btn{display:inline-block;background:var(--walnut);color:var(--surface);border:0;
  padding:7px 13px;border-radius:3px;font:inherit;font-weight:500;cursor:pointer}
.btn:hover{filter:brightness(1.08)}
.btn.ghost{background:transparent;border:1px solid var(--rule);color:var(--ink)}
.btn.danger{background:transparent;border:1px solid var(--stop);color:var(--stop)}
.row{display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap}
.chip{display:inline-block;font-size:11.5px;padding:3px 9px;border:1px solid var(--rule);
  border-radius:999px;background:var(--surface);margin:0 var(--s1) var(--s1) 0}
.chip b{font-weight:500}
.chip span{color:var(--muted);font-size:10.5px;margin-left:5px}
.empty{color:var(--muted);padding:var(--s6) var(--s4);text-align:center;
  border:1px dashed var(--rule);border-radius:4px}
.note{border-left:2px solid var(--attention);padding:var(--s2) var(--s3);
  font-size:12px;color:var(--muted);margin-top:var(--s3)}
.note b{color:var(--ink);font-weight:600}
details>summary{cursor:pointer;color:var(--muted);font-size:12px;padding:var(--s2) 0}
@media(max-width:860px){
  body{flex-direction:column}
  aside{width:100%;flex:none;position:static;height:auto;
    border-right:0;border-bottom:1px solid var(--rule)}
  nav{display:flex;flex-wrap:wrap;gap:2px;padding:var(--s2)}
  nav .grp,.strip{display:none}
  main{padding:var(--s4) var(--s4) var(--s7)}
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
