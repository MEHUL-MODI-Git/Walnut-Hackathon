"""The Approvals screen: a work queue, and nothing else.

Every action at tier `GATED` or above blocks here until a human answers. The
screen shows only what is *currently* pending — the executed/refused history
belongs to Actions, not here. A queue screen with a forty-row ledger sitting
under it reads as "nothing here is urgent"; a queue screen that is *just* the
queue reads as exactly what it is.

Each pending item is a card, not a table row, because it carries its own two
controls (approve, deny) and there are never many of them at once — the two
conditions `design.py` reserves cards for. The payload is rendered as labelled
fields rather than a JSON blob: an approver who cannot read what they are
approving is rubber-stamping, which is the exact failure this gate exists to
prevent.
"""

from __future__ import annotations

from typing import Any

from ..design import empty, esc, panel, pill, rail

__all__ = ["page_approvals"]


def _reach_tag(app: str) -> str:
    """Is this write about to leave the building?

    Email is the one connected app that speaks to people outside the organisation.
    Everything else lands in an internal system of record, however consequential it
    is otherwise — the distinction an approver needs first is "does this leave the
    building," not "how big is the diff."
    """
    if app == "email":
        return pill("leaves the organisation", "stop")
    return pill("internal", "quiet")


def _payload_fields(payload: dict[str, Any]) -> str:
    """Labelled fields, never a JSON blob — the whole point of this screen."""
    if not payload:
        return '<p style="color:var(--muted)">No payload fields.</p>'
    rows = []
    for key, value in payload.items():
        rows.append(
            f'<div style="margin-bottom:var(--s2)">'
            f'<div class="lbl">{esc(key)}</div>'
            f'<div style="white-space:pre-wrap;overflow-wrap:anywhere">{esc(value)}</div>'
            f"</div>"
        )
    return "".join(rows)


def _pending_card(key: str, action: Any, context: str) -> str:
    header_pills = pill("Gated", "attention") + " " + _reach_tag(action.app)
    body = f"""<p>{esc(action.rationale) or "No rationale given."}</p>
<h2 style="margin-top:var(--s4)">What will be written</h2>
{_payload_fields(action.payload)}
<h2 style="margin-top:var(--s4)">Why</h2>
{rail(context)}
<div class="row" style="margin-top:var(--s4)">
  <form method="post" action="/approvals/{esc(key)}/approve">
    <button class="btn">Approve and execute</button></form>
  <form method="post" action="/approvals/{esc(key)}/deny">
    <button class="btn danger">Deny</button></form>
</div>
<p class="note" style="border-left-color:var(--quiet)">An unanswered request is
a refusal.</p>"""
    return panel(f"{action.app}.{action.operation}", body, header_pills)


def page_approvals(
    pending: list[tuple[str, Any, str]], gated_ops: int, decided_today: int
) -> str:
    """A work queue. An empty one should look like proof the gate works, not
    like a broken page."""
    header = """<div class="ph">
  <h1>Approvals</h1>
  <p>Anything that leaves the organisation, changes many records at once, or cannot
  be taken back stops here before it runs. A gated action carries no default — it
  executes only once a human says yes, and an unanswered request is a refusal.</p>
</div>"""

    if not pending:
        body = empty("Nothing awaiting approval.") + (
            f'<p style="color:var(--muted);text-align:center;margin-top:var(--s2)">'
            f"{gated_ops} gated operations exist across your connectors · "
            f"{decided_today} decided today</p>"
        )
        return header + body

    cards = "".join(_pending_card(key, action, context) for key, action, context in pending)
    return header + cards
