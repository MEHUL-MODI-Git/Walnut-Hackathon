"""Retrieval — ask about anything, get an answer.

The first version of this screen grouped records by which app they came from, which is
how the system thinks, not how a person asks. Nobody wants twelve rows sorted by source.
They want the answer, organised by meaning, with the receipts available if they care.

So: a briefing. Identity, what the record says, what the instruments say, what needs
attention, what happened recently. **Every line carries its citation folded away behind
a marker** — always there, never in the way. That is the whole design argument: a
citation you must read is noise, and a citation you cannot reach is a claim. It has to
be one keystroke away and no closer.

Coverage moves below the answer. It is the proof, and proof belongs after the claim —
but it stays on every result, including the sources that held nothing, because a search
that quietly skipped four systems must never look like one that read them all.
"""

from __future__ import annotations

from typing import Any

from ..design import empty, esc, panel, table

__all__ = ["page_retrieval"]

EXAMPLES = ("Ankusha Rao", "MR-4417", "Priya Patel")


def _cite(fact: Any) -> str:
    """A citation marker that opens. Closed by default — the receipt, not the content."""
    if fact is None:
        return ""
    return (f'<details class="cite"><summary>{esc(fact.app)}</summary>'
            f'<div class="cite-body">{esc(fact.text[:600])}'
            f'<div class="c">{esc(fact.cite())}</div></div></details>')


def _line(label: str, value: str, fact: Any, flag: str = "") -> str:
    return (f'<div class="ln{" warn" if flag else ""}">'
            f'<div class="ln-l">{esc(label)}</div>'
            f'<div class="ln-v">{esc(value)}{_cite(fact)}</div></div>')


def _conflict_block(conflict: Any) -> str:
    """Both sides quoted down to the clause that actually disagrees.

    Rendering the whole record here put three hundred characters of demographics on
    screen with the disputed word buried inside, which is how an alert that fires
    correctly still gets missed.
    """
    left, right = conflict.left.fact, conflict.right.fact
    return f"""<div class="alert">
      <div class="alert-h">Conflicting information · {esc(conflict.subject)}</div>
      <div class="alert-b">
        <div class="ln"><div class="ln-l">{esc(left.app)} says</div>
          <div class="ln-v">{esc(conflict.left.quote())}{_cite(left)}</div></div>
        <div class="ln"><div class="ln-l">{esc(right.app)} says</div>
          <div class="ln-v">{esc(conflict.right.quote())}{_cite(right)}</div></div>
      </div>
      <div class="alert-f">Not ranked automatically — recency and source authority are
        heuristics, not evidence. <a href="/knowledge#conflicts">Review →</a></div>
    </div>"""


def _coverage(assembly: Any, unsearchable: list[tuple[str, str]]) -> str:
    rows = []
    for cov in assembly.coverage:
        if cov.state == "found":
            rows.append([f'<b>{esc(cov.app)}</b>',
                         '<span class="dot found"></span>found',
                         f'<b class="num">{cov.hits}</b>', ""])
        else:
            rows.append([f'<b>{esc(cov.app)}</b>',
                         '<span class="dot nothing"></span>nothing',
                         '<span class="num">0</span>',
                         '<span class="pill quiet">searched · no match</span>'])
    for app, reason in unsearchable:
        rows.append([f'<b>{esc(app)}</b>',
                     '<span class="dot miss"></span>not searched', "—",
                     f'{esc(reason[:60])} · <a href="/connectors/{esc(app)}">fix</a>'])

    searched = len(assembly.coverage)
    found = len(assembly.apps_with_hits)
    lead = (f'<p class="cov-lead">Looked in <b>{searched + len(unsearchable)}</b> '
            f'sources. <b>{found}</b> had something. '
            f'{searched - found} had nothing — which is a result, not a gap.</p>')
    return lead + table(["source", "", "found", ""], rows)


def page_retrieval(
    query: str,
    assembly: Any = None,
    *,
    briefing: Any = None,
    unsearchable: list[tuple[str, str]] | None = None,
    held: dict[str, int] | None = None,
) -> str:
    unsearchable = unsearchable or []
    held = held or {}

    ask = f"""<form method="get" action="/" class="ask">
      <input id="q" name="q" class="big" value="{esc(query)}"
             placeholder="Ask about anyone or anything — a patient, a colleague, a record"
             autofocus aria-label="Ask about anyone or anything">
      <button class="btn">Ask</button></form>"""

    # ---- nothing asked yet ----------------------------------------------
    if assembly is None or not query.strip():
        chips = "".join(f'<a class="chip" href="/?q={esc(e)}">{esc(e)}</a>'
                        for e in EXAMPLES)
        rows = [[f"<b>{esc(a)}</b>", f'<span class="num">{n}</span>']
                for a, n in sorted(held.items())]
        return (f'<div class="ph"><h1>What would you like to know?</h1>'
                f'<p>One question, answered from every system at once. Every line shows '
                f'where it came from.</p></div>{ask}'
                f'<div class="row" style="margin-top:14px">{chips}</div>'
                + panel("Connected", table(["source", "records"], rows,
                                           empty_text="Nothing connected yet.")))

    # ---- nothing found ---------------------------------------------------
    if assembly.total == 0:
        return (f'<div class="ph"><h1>Nothing about “{esc(query)}”</h1>'
                f'<p>Here is where we looked.</p></div>{ask}'
                + panel("Coverage", _coverage(assembly, unsearchable))
                + f'<div class="note"><b>Every source was searched.</b> None of them '
                  f'hold anything about “{esc(query)}”.</div>')

    # ---- an answer -------------------------------------------------------
    head = [f'<div class="ph"><h1>{esc(query)}</h1>']
    if briefing is not None and briefing.headline:
        head.append(f'<p class="ident">{esc(briefing.headline)}'
                    f'{_cite(briefing.headline_fact)}</p>')
    head.append(f'<p class="src">{assembly.total} records · '
                f'{len(assembly.apps_with_hits)} of '
                f'{len(assembly.coverage) + len(unsearchable)} sources'
                + (f' · known as {", ".join(esc(a) for a in assembly.aliases[:5])}'
                   if assembly.aliases else "")
                + "</p></div>")
    body = ["".join(head), ask]

    if briefing is not None:
        body.extend(_conflict_block(c) for c in briefing.conflicts[:2])

        for section in briefing.sections:
            rows = "".join(_line(l.label, l.value, l.fact, l.flag) for l in section.lines)
            body.append(panel(section.title, rows))

        if briefing.timeline:
            rows = "".join(_line(l.label, l.value, l.fact, l.flag)
                           for l in briefing.timeline)
            body.append(panel("Recently", rows))

    if assembly.related_subjects:
        chips = "".join(f'<a class="chip" href="/?q={esc(s)}">{esc(s)}</a>'
                        for s in assembly.related_subjects)
        body.append(panel("Also mentioned", chips))

    body.append(panel("Where this came from", _coverage(assembly, unsearchable)))
    body.append('<div class="note">Act on this in '
                '<a href="/actions?tab=activity">Actions</a> — every action must cite '
                'the evidence that justified it.</div>')
    return "".join(body)
