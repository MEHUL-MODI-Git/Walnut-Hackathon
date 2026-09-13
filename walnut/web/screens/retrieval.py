"""Retrieval — everything the company knows about one thing.

This is the product. A person asks about a patient, a colleague, or a customer, and gets
back everything, from every connected system, with its source attached — no matter how
scattered it was or what name each system filed it under.

Two moments carry the screen, and both are cheap because the data already exists:

**The aliases.** You asked for one name; here are the five it is stored under. A Slack
message written by `priya`, a GitHub commit by `priya-p` and a Notion page by `P. Patel`
are one person, and saying so out loud is the clearest possible demonstration that the
scattering was undone.

**The coverage table.** Every source is listed on every result, including the ones that
had nothing. This is the part most systems omit, and it is the part that makes the
promise checkable: a search that quietly read four of six sources looks identical to one
that read all six, so a source that was searched and came back empty must be *shown*
coming back empty. That is the promise working, not a gap in it — which is why the
"nothing" state is rendered in a quiet neutral and never in red.

Only one state on this screen is a failure: a source that could **not** be searched.
"""

from __future__ import annotations

from typing import Any

from ..design import empty, esc, meter, panel, pill, rail, table

__all__ = ["page_retrieval"]

EXAMPLES = ("Ankusha Rao", "Priya Patel", "co-amoxiclav")


def _coverage_table(assembly: Any, unsearchable: list[tuple[str, str]]) -> str:
    """Three states, deliberately not a red/green binary.

    `unsearchable` carries connectors that errored or custom sources failing
    conformance. Without unioning those in, a broken connector simply *vanishes* from
    the table — and an honest search would be indistinguishable from one that skipped
    four systems. That union is the honesty of the entire screen.
    """
    rows = []
    for cov in assembly.coverage:
        if cov.state == "found":
            mark = f'<span class="dot found"></span>found'
            hits = f'<b class="num">{cov.hits}</b>'
            note = ""
        else:
            mark = f'<span class="dot nothing"></span>nothing'
            hits = '<span class="num">0</span>'
            note = '<span class="pill quiet">searched · no match</span>'
        rows.append([
            f'<b>{esc(cov.app)}</b>', mark, hits,
            f'<span class="num">{cov.total_held}</span>', note,
        ])

    for app, reason in unsearchable:
        rows.append([
            f'<b>{esc(app)}</b>',
            '<span class="dot miss"></span>not searched',
            "—", "—",
            f'{esc(reason)} · <a href="/connectors/{esc(app)}">fix</a>',
        ])

    searched = len(assembly.coverage)
    found = len(assembly.apps_with_hits)
    lead = (
        f"<p class='ph'><b>{searched} source{'s' if searched != 1 else ''} searched. "
        f"{found} had something. {searched - found} had nothing — and that is a result, "
        f"not a gap.</b></p>"
    )
    if unsearchable:
        lead += (f'<div class="note"><b>{len(unsearchable)} source could not be '
                 f'searched.</b> Its records are not represented in this answer.</div>')

    return lead + table(
        ["source", "", "found", "held", ""], rows,
        empty_text="No sources connected yet.",
    )


def _results(assembly: Any, mode: str) -> str:
    if mode == "time":
        rows = "".join(
            f'<div class="rail"><div class="t">'
            f'<span class="pill quiet">{esc(f.app)}</span> '
            f'{esc((f.occurred_at or f.pointer.retrieved_at).strftime("%Y-%m-%d"))} — '
            f'{esc(f.text[:260])}</div>'
            f'<div class="c">{esc(f.cite())}</div></div>'
            for f in assembly.timeline()
        )
        return rows or empty("Nothing to show in order.")

    out = []
    for app, facts in sorted(assembly.by_app.items(), key=lambda kv: -len(kv[1])):
        head = (f'<h2>{esc(app)} <span class="pill quiet">{len(facts)} '
                f'record{"s" if len(facts) != 1 else ""}</span></h2>')
        shown = "".join(rail(f.text, f.cite()) for f in facts[:3])
        rest = ""
        if len(facts) > 3:
            more = "".join(rail(f.text, f.cite()) for f in facts[3:])
            rest = (f"<details><summary>show {len(facts) - 3} more from "
                    f"{esc(app)}</summary>{more}</details>")
        out.append(head + shown + rest)
    return "".join(out)


def page_retrieval(
    query: str,
    assembly: Any = None,
    *,
    unsearchable: list[tuple[str, str]] | None = None,
    held: dict[str, int] | None = None,
    mode: str = "source",
) -> str:
    """The hero screen. `assembly` is `walnut.retrieval.assemble()`'s result."""
    unsearchable = unsearchable or []
    held = held or {}

    search = f"""<form method="get" action="/">
      <label for="q" class="lbl">Ask about any person, patient, customer or record</label>
      <div class="row" style="margin-top:8px">
        <input id="q" name="q" class="big" value="{esc(query)}"
               placeholder="e.g. Ankusha Rao" autofocus style="flex:1;min-width:260px">
        <button class="btn">Search everything</button>
      </div></form>"""

    if assembly is None or not query.strip():
        chips = "".join(
            f'<a class="chip" href="/?q={esc(e)}"><b>{esc(e)}</b></a>' for e in EXAMPLES
        )
        strip = table(
            ["source", "records held"],
            [[f"<b>{esc(a)}</b>", f'<span class="num">{n}</span>']
             for a, n in sorted(held.items())],
            empty_text="No sources connected yet.",
        )
        return (f'<div class="ph"><h1>Retrieval</h1><p>One question, answered from every '
                f'connected system, with every claim carrying its source.</p></div>'
                f'{search}<div style="margin-top:16px">{chips}</div>'
                + panel("What is connected", strip)
                + '<div class="note">Ask about a person and you get every system they '
                  'appear in, under every name they are filed under.</div>')

    # ---- an answer -------------------------------------------------------

    aliases = ""
    if assembly.aliases:
        chips = "".join(
            f'<span class="chip"><b>{esc(a)}</b></span>' for a in assembly.aliases
        )
        aliases = panel(
            "Known as",
            chips + '<div class="note"><b>You asked for one name.</b> These are the '
                    f'{len(assembly.aliases)} it is stored under across your systems.</div>',
        )

    toggle = (f'<div class="row"><a class="chip" href="/?q={esc(query)}&mode=source">'
              f'{"<b>By source</b>" if mode != "time" else "By source"}</a>'
              f'<a class="chip" href="/?q={esc(query)}&mode=time">'
              f'{"<b>By time</b>" if mode == "time" else "By time"}</a></div>')

    related = ""
    if assembly.related_subjects:
        chips = "".join(
            f'<a class="chip" href="/?q={esc(s)}">{esc(s)}</a>'
            for s in assembly.related_subjects
        )
        related = panel("These records also mention", chips)

    if assembly.total == 0:
        # A negative that proves the guarantee, rather than a bare "0 results".
        return (f'<div class="ph"><h1>Everything about “{esc(query)}”</h1>'
                f'<p>Nothing found — and here is where we looked.</p></div>{search}'
                + panel("Coverage", _coverage_table(assembly, unsearchable))
                + f'<div class="note"><b>Every source was searched. None of them hold '
                  f'anything about “{esc(query)}”.</b></div>')

    return (
        f'<div class="ph"><h1>Everything about “{esc(query)}”</h1>'
        f'<p class="num">{assembly.total} records · {len(assembly.apps_with_hits)} of '
        f'{len(assembly.coverage) + len(unsearchable)} sources'
        + (f' · resolved through {len(assembly.aliases)} identities' if assembly.aliases else "")
        + f'</p></div>{search}'
        + aliases
        + panel("Coverage", _coverage_table(assembly, unsearchable))
        + panel("Records", toggle + _results(assembly, mode))
        + related
        + '<div class="note">Act on this in <a href="/actions?tab=activity">Actions</a> — '
          'every action must cite the evidence that justified it.</div>'
    )
