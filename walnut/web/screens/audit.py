"""The Audit screen: how do you know it works?

Not a claim that the system works — a record of what it actually did, and an
honest account of what that record does and does not prove.

Identity resolution is promoted to the top of this page rather than buried
under the decision ledger, because it is the best evidence in the console that
scattering was undone: five apps' worth of handles collapsed into one person,
with the band that says how sure Walnut is and the evidence for each merge. A
decision ledger with counts in it is easy to render; a table that shows
`@sarah`, `sarah-k`, and `Sarah Kim` are the same human, sourced from three
different systems, is the harder claim, and the more persuasive one.
"""

from __future__ import annotations

from typing import Any

from ..design import empty, esc, pill, rail, table

__all__ = ["page_audit"]

_BAND_PILL: dict[str, str] = {
    "certain": "ok",
    "likely": "quiet",
    "uncertain": "attention",
}


def _identity_section(identities: Any) -> str:
    """Cross-app people, promoted above the decision ledger.

    `identities` is a `ResolutionReport | None` — None when the caller has
    nothing to resolve yet (a fresh brain, or a demo running with one app
    connected). The section is skipped rather than faked in that case; an
    empty stat line dressed up as data would be exactly the kind of confident
    overstatement this page exists to avoid.
    """
    if identities is None:
        return ""

    s = identities.summary()
    stats = (
        '<div class="row" style="gap:var(--s6);margin-bottom:var(--s4)">'
        f'<div><b class="num" style="font-size:20px">{s.get("people", 0)}</b>'
        '<div class="lbl">people</div></div>'
        f'<div><b class="num" style="font-size:20px">{s.get("identities", 0)}</b>'
        '<div class="lbl">identities</div></div>'
        f'<div><b class="num" style="font-size:20px">{s.get("cross_app", 0)}</b>'
        '<div class="lbl">cross-app</div></div>'
        f'<div><b class="num" style="font-size:20px">{s.get("needs_human_review", 0)}</b>'
        '<div class="lbl">needs human review</div></div>'
        "</div>"
    )

    rows = []
    for person in identities.resolved:
        band = person.band.value
        handles = ", ".join(
            f'<span class="mono">{esc(i.app)}:{esc(i.handle)}</span>'
            for i in sorted(person.identities, key=lambda i: i.app)
        )
        rows.append(
            (
                esc(person.canonical_name),
                pill(band, _BAND_PILL.get(band, "")),
                handles,
            )
        )
    people_table = table(("canonical name", "band", "handles"), rows)

    review_rows = [
        rail(
            f"{a.app}:{a.handle} ({a.display_name or 'no name'}) vs "
            f"{b.app}:{b.handle} ({b.display_name or 'no name'})",
            reason,
        )
        for a, b, reason in identities.needs_review
    ]
    review_body = "".join(review_rows) if review_rows else empty(
        "Nothing held for review."
    )

    return f"""<h2>Identity resolution</h2>
{stats}
{people_table}
<h2>Held for a human</h2>
{review_body}"""


def _decision_ledger(decisions: dict[str, Any], ledger: dict[str, Any]) -> str:
    categories = decisions.get("categories") or {}
    outcomes = decisions.get("outcomes") or {}

    cat_rows = [
        (f'<span class="mono">{esc(cat)}</span>', f'<span class="num">{count}</span>')
        for cat, count in sorted(categories.items(), key=lambda kv: -kv[1])
    ]
    cat_table = table(("category", "count"), cat_rows, empty_text="No decisions recorded yet.")

    executed = outcomes.get("executed", 0)
    refused = outcomes.get("refused", 0)
    undone = ledger.get("undone", 0)
    split = (
        '<div class="row" style="gap:var(--s6);margin:var(--s3) 0 var(--s4)">'
        f'<div><b class="num" style="font-size:20px">{executed}</b>'
        '<div class="lbl">executed</div></div>'
        f'<div><b class="num" style="font-size:20px">{refused}</b>'
        '<div class="lbl">refused</div></div>'
        f'<div><b class="num" style="font-size:20px">{undone}</b>'
        '<div class="lbl">undone</div></div>'
        "</div>"
    )

    return f"""<h2>Decision ledger</h2>
{split}
{cat_table}"""


def _refusals_section(refusal_reasons: list[str]) -> str:
    if not refusal_reasons:
        body = empty("No refusals recorded on this run.")
    else:
        body = "".join(pill(reason, "attention") + " " for reason in refusal_reasons)
        body = f'<div class="row">{body}</div>'
    return f"""<h2>Refusals</h2>
{body}"""


_WHAT_THIS_DOES_NOT_PROVE = (
    "<h2>What this does not prove</h2>\n"
    '<p style="color:var(--muted);max-width:70ch">Stated because a reliability '
    "page that only lists successes is not one. Taint detection is "
    "a tripwire, not a perimeter &mdash; it is safe to rely on only because "
    "ingested content can never justify a state-changing action regardless of "
    "what it says, so a missed pattern costs the explanation, not the outcome. "
    "Contradiction detection is lexical and will miss anything phrased without "
    "status vocabulary. Identity resolution is deterministic, not calibrated. "
    "And no live third-party API was necessarily called to produce this page "
    "&mdash; seeded and demo data can look identical to it.</p>"
)


def page_audit(
    decisions: dict[str, Any],
    ledger: dict[str, Any],
    identities: Any,
    refusal_reasons: list[str],
) -> str:
    """Every decision this system made, and the evidence behind it."""
    header = """<div class="ph">
  <h1>Audit</h1>
  <p>The answer to "how do you know it works" &mdash; not a claim that it
  does, a record of what it actually did.</p>
</div>"""

    return (
        header
        + _identity_section(identities)
        + _decision_ledger(decisions, ledger)
        + _refusals_section(refusal_reasons)
        + _WHAT_THIS_DOES_NOT_PROVE
    )
