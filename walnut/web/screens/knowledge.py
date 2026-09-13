"""The Knowledge screen: the data layer, browsable.

Replaces the old flat `/evidence` dump. Two things live here and nowhere else,
because both are properties of the data layer rather than of any one query:

* **Source distribution.** How lopsided is the evidence base? A brain that is 90%
  Slack and 2% GitHub is not five systems' worth of company memory, it is one
  chat log with four garnishes — and a bar-in-a-table shows that at a glance in a
  way a pie chart, which does not sort and barely scans, does not.
* **Contradictions.** Two sources disagreeing is a fact about the corpus, true
  independent of whatever a user happens to be searching for right now. It stays
  visible even when a filter narrows the fact list to nothing.

Every fact keeps its citation. This is the console's evidence: not a claim about
what Walnut knows, but the receipts for it.
"""

from __future__ import annotations

from typing import Any

from ..design import empty, esc, panel, rail, table

__all__ = ["page_knowledge"]


def _source_distribution(by_app_counts: dict[str, int]) -> str:
    """A table with a bar drawn inside its own cell — sorts, scans, and shows
    dominance at once, which a pie chart does none of."""
    total = sum(by_app_counts.values())
    if not total:
        return empty("No sources ingested yet.")

    rows = []
    for app, count in sorted(by_app_counts.items(), key=lambda kv: -kv[1]):
        share = count / total
        bar = (
            f'<div style="display:flex;align-items:center;gap:8px">'
            f'<div style="flex:1;height:8px;background:var(--rule);border-radius:2px;'
            f'overflow:hidden"><div style="width:{share * 100:.1f}%;height:100%;'
            f'background:var(--walnut)"></div></div>'
            f'<span class="mono" style="min-width:3.5em;text-align:right">{share:.0%}</span>'
            f"</div>"
        )
        rows.append((esc(app), f'<span class="num">{count}</span>', bar))
    return table(("source", "records", "share"), rows)


def _conflict_row(conflict: Any) -> str:
    subject = esc(conflict.subject)
    left_app, right_app = conflict.apps
    header = (
        f"<b>{subject}</b> · {esc(left_app)} vs {esc(right_app)}"
        f'<div style="color:var(--muted);margin-top:2px">{esc(conflict.explanation)}</div>'
    )
    sides = rail(conflict.left.fact.text, conflict.left.fact.cite()) + rail(
        conflict.right.fact.text, conflict.right.fact.cite()
    )
    form = (
        f'<form method="post" action="/act">'
        f'<input type="hidden" name="conflict_id" value="{esc(conflict.id)}">'
        f'<button class="btn">Propose actions</button></form>'
    )
    return f'<div style="margin-bottom:var(--s5)">{header}{sides}{form}</div>'


def page_knowledge(
    stats: dict[str, Any],
    facts: list[Any],
    q: str,
    conflicts: list[Any],
    by_app_counts: dict[str, int],
) -> str:
    """The data layer, browsable: what Walnut has ingested, where it disagrees
    with itself, and a way to search the evidence directly."""
    fact_count = stats.get("facts_indexed", 0)
    source_count = len(stats.get("apps", []))
    edge_count = stats.get("edge_count", 0)

    header = f"""<div class="ph">
  <h1>Knowledge base</h1>
  <p>{fact_count} facts · {source_count} sources · {edge_count} edges.
  Every claim held here carries the evidence it was built from — nothing enters
  the brain without a resolvable pointer back to its source.</p>
</div>
<form method="post" action="/ingest" class="row" style="margin-bottom:var(--s5)">
  <button class="btn ghost">Re-ingest</button>
</form>"""

    dist = panel("Source distribution", _source_distribution(by_app_counts))

    filter_form = f"""<form method="get" action="/knowledge" class="row" style="margin-bottom:var(--s4)">
  <input name="q" value="{esc(q)}" placeholder="filter evidence&hellip;"
         style="flex:1;min-width:220px">
  <button class="btn">Filter</button>
</form>"""

    shown = facts[:50]
    remainder = len(facts) - len(shown)
    if not facts:
        if q:
            fact_body = empty(
                f"No fact matches '{q}'. {fact_count} facts held across {source_count} sources."
            )
        else:
            fact_body = empty("Nothing ingested yet.")
    else:
        rows = []
        for f in shown:
            cite = (
                f'{esc(f.cite())} · <span class="pill quiet">{esc(f.app)}</span> '
                f'· <span class="mono">{esc(f.node_id)}</span>'
            )
            rows.append(rail(f.text, cite))
        more = (
            f'<p style="color:var(--muted);margin-top:var(--s2)">'
            f"and {remainder} more fact(s) not shown.</p>"
            if remainder > 0
            else ""
        )
        fact_body = "".join(rows) + more

    fact_section = f"""<h2>Evidence</h2>
{filter_form}
{fact_body}"""

    if conflicts:
        conflict_body = "".join(_conflict_row(c) for c in conflicts)
    else:
        conflict_body = empty("No conflicts detected between sources.")
    conflict_panel = panel(f"Conflicts between sources — {len(conflicts)}", conflict_body)

    return header + dist + fact_section + conflict_panel
