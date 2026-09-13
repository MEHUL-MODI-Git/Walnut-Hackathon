"""Actions screen: what the agent can do, and what it did.

Two tabs, one table-first vocabulary, no primary button.

**Catalogue** ("what it can do") is a reference, not a call to action. Its job is to
answer, for every operation on every connected app: how consequential is this, can it
be undone, and — the row that actually matters when a document is hostile — could
ingested content alone trigger it. Most rows are TRIVIAL and boring, and the summary
line says so up front, because the honest shape of an action catalogue is mostly
harmless things with a few loud exceptions, not the other way round.

**Activity** ("what it did") is the run history, and it deliberately does not have a
separate audit page for refusals: a refusal is what happened when someone asked and
the answer was no, and hiding that on a different screen than the successes would be
an act of PR, not of evidence. `done / held / refused / undone` all live in one
`outcome` column for the same reason `.rail` refusals sit inline in a Slack thread
rather than in a separate moderation log — the reader wants the sequence of events,
not a curated subset of it.

Everything here is a read of `adapters`, `ledger`, `intent`, and `results` — this
module never calls an adapter or executes anything itself. Wiring `/request` and
`/request/execute` and `/undo/{id}` to `ActionExecutor` is the caller's job.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...actions.executor import ActionLedger
from ...actions.governance import Refusal, is_quarantine_safe, is_reversible
from ...contract import Action, ActionReceipt, ActionTier, Adapter
from ...intent import Intent
from ..design import empty, esc, meter, pill, rail, table

__all__ = ["page_actions"]

# Quoted verbatim from `ActionTier`'s own docstrings in walnut/contract.py — this is
# the tier vocabulary as the type actually defines it, not a paraphrase that could
# drift from the source of truth.
_TIER_DEFINITIONS: tuple[tuple[ActionTier, str], ...] = (
    (ActionTier.TRIVIAL, "Reversible, internal, low-consequence: labels, flags, "
                          "reactions, links."),
    (ActionTier.INTERNAL, "Real internal writes. Auto-executed, logged, reversible."),
    (ActionTier.GATED, "External-facing, bulk, or destructive. Blocks on human "
                        "approval."),
    (ActionTier.FORBIDDEN, "Never executed by the agent under any approval. Present "
                            "so the vocabulary can express it and the refusal is "
                            "typed rather than improvised."),
)


def page_actions(
    tab: str,
    adapters: dict[str, Adapter],
    ledger: ActionLedger,
    intent: Intent | None,
    results: list[Any],
) -> str:
    """Render the Actions screen body for `tab` ("catalogue" or "activity").

    `adapters` drives the catalogue directly — a custom source is just another entry
    in this dict, so connecting one visibly appends a row with no change here. This
    is the whole "actions vary per connected app" story made honest rather than
    asserted.
    """
    active_tab = tab if tab in ("catalogue", "activity") else "catalogue"
    body = [_tabs(active_tab)]
    if active_tab == "catalogue":
        body.append(_catalogue(adapters))
    else:
        body.append(_activity(adapters, ledger, intent, results))
    return "".join(body)


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _tabs(active: str) -> str:
    """Two plain links. No JS, no build step — `?tab=` is the entire router."""

    def link(key: str, label: str) -> str:
        if active == key:
            return f"<b>{esc(label)}</b>"
        return f'<a href="/actions?tab={key}">{esc(label)}</a>'

    return (
        '<div class="ph"><h1>Actions</h1>'
        f'<p class="lbl">{link("catalogue", "What it can do")} · '
        f'{link("activity", "What it did")}</p></div>'
    )


def _tier_counts(adapters: dict[str, Adapter]) -> tuple[int, int, dict[ActionTier, int]]:
    """Total operations, total apps, and a per-tier count — the catalogue's headline."""
    counts: dict[ActionTier, int] = {t: 0 for t in ActionTier}
    total = 0
    for adapter in adapters.values():
        try:
            caps = adapter.capabilities()
        except Exception:  # noqa: BLE001 - an adapter that can't describe itself has no row
            continue
        for tier in caps.operations.values():
            counts[tier] += 1
            total += 1
    return total, len(adapters), counts


def _when(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def _target_text(target: dict[str, Any]) -> str:
    if not target:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in target.items())


# ---------------------------------------------------------------------------
# Tab 1 — catalogue
# ---------------------------------------------------------------------------


def _catalogue(adapters: dict[str, Adapter]) -> str:
    total, n_apps, counts = _tier_counts(adapters)
    legend_rows = [
        (meter(tier.name), esc(text))
        for tier, text in _TIER_DEFINITIONS
    ]
    legend = table(("Tier", "Definition"), legend_rows)

    summary = (
        f"<p>{total} operation{'s' if total != 1 else ''} across {n_apps} app"
        f"{'s' if n_apps != 1 else ''} · {counts[ActionTier.TRIVIAL]} trivial · "
        f"{counts[ActionTier.INTERNAL]} internal · {counts[ActionTier.GATED]} gated · "
        f"{counts[ActionTier.FORBIDDEN]} forbidden</p>"
    )

    out = [
        "<h2>Tier legend</h2>", legend,
        "<h2>What every connected app can do</h2>", summary,
    ]

    if not adapters:
        out.append(empty("No apps connected. Nothing to act on yet."))
        return "".join(out)

    for app_name in sorted(adapters):
        adapter = adapters[app_name]
        try:
            caps = adapter.capabilities()
        except Exception:  # noqa: BLE001 - surfaced, not hidden
            out.append(f"<h2>{esc(app_name)}</h2>")
            out.append(empty(f"{app_name} could not describe its capabilities."))
            continue

        rows = []
        for operation in sorted(caps.operations):
            tier = caps.operations[operation]
            reversible = is_reversible(app_name, operation)
            quarantine_safe = is_quarantine_safe(app_name, operation)
            rows.append((
                f'<code>{esc(operation)}</code>',
                meter(tier.name),
                pill("yes", "ok") if reversible else pill("no", "stop"),
                pill("yes", "attention") if quarantine_safe else pill("no", "ok"),
            ))

        out.append(f"<h2>{esc(app_name)}</h2>")
        out.append(table(
            ("Operation", "Tier", "Reversible", "Ingested content may trigger it"),
            rows,
        ))

    return "".join(out)


# ---------------------------------------------------------------------------
# Tab 2 — activity
# ---------------------------------------------------------------------------


def _activity(
    adapters: dict[str, Adapter],
    ledger: ActionLedger,
    intent: Intent | None,
    results: list[Any],
) -> str:
    out = [_request_box()]

    if intent is not None:
        out.append(_intent_block(intent))

    if results:
        out.append(_results_block(results))

    out.append(_history(adapters, ledger))
    return "".join(out)


def _request_box() -> str:
    return (
        '<h2>Ask it to do something</h2>'
        '<form method="post" action="/request">'
        '<label>Request</label>'
        '<input class="big" name="request" placeholder='
        '"e.g. file a Linear issue about MED-412 and comment on the PR">'
        '<div class="row"><button class="btn" type="submit">Propose</button></div>'
        '</form>'
        '<p class="lbl">Proposals go through the same checks as everything else — a '
        'misread sentence produces a refused action, never a wrong write.</p>'
    )


def _intent_block(intent: Intent) -> str:
    out: list[str] = []

    if intent.proposed:
        out.append("<h2>Proposed</h2>")
        for prop in intent.proposed:
            dotted = f"{prop.action.app}.{prop.action.operation}"
            out.append(
                f'<section class="panel"><header><h3>{esc(dotted)}</h3>'
                f'{pill(f"from &ldquo;{esc(prop.matched_phrase)}&rdquo;", "quiet")}'
                f'</header><div class="body">'
            )
            for fact in prop.evidence:
                out.append(rail(fact.text, fact.cite()))
            out.append("</div></section>")
        out.append(
            '<form method="post" action="/request/execute">'
            '<div class="row"><button class="btn" type="submit">'
            "Execute proposed actions</button></div></form>"
        )

    if intent.clarifications:
        out.append("<h2>Needs clarification</h2>")
        for question in intent.clarifications:
            out.append(rail(question, kind="attention"))

    return "".join(out)


def _results_block(results: list[Any]) -> str:
    out = ["<h2>Just now</h2>"]
    for r in results:
        if isinstance(r, Refusal):
            dotted = f"{r.action.app}.{r.action.operation}"
            out.append(rail(f"REFUSED · {dotted} — {r.explanation}",
                             r.reason.value, kind="stop"))
        else:
            dotted = f"{r.action.app}.{r.action.operation}"
            out.append(rail(f"done · {dotted}", _landed_at(r) or r.action_id, kind="ok"))
    return "".join(out)


def _landed_at(receipt: ActionReceipt) -> str:
    """The record this write became, in the words of the system that took it.

    "done · linear.create_issue" says Walnut believes it wrote something. A live
    adapter's receipt says what: the issue's own identifier and URL, as the API
    returned them. Showing that turns the line from an assertion into something a
    reader can go and check — which is the only kind of "done" this product should
    be in the business of reporting. Fixture receipts carry no URL and fall back to
    the action id as before.
    """
    result = receipt.result or {}
    for key in ("issue", "comment", "page", "message", "record"):
        inner = result.get(key)
        if isinstance(inner, dict):
            url = inner.get("url") or inner.get("permalink")
            ident = inner.get("identifier") or inner.get("id") or ""
            if url:
                return f"{ident} · {url}".strip(" ·")
    url = result.get("url") or result.get("permalink")
    return f"{result.get('identifier') or ''} · {url}".strip(" ·") if url else ""


def _undo_cell(receipt: ActionReceipt) -> str:
    """A working Undo button, or the plain, credible truth that there isn't one."""
    if receipt.is_undone:
        return "—"
    if not is_reversible(receipt.action.app, receipt.action.operation):
        return "cannot be undone"
    return (
        f'<form method="post" action="/undo/{esc(receipt.action_id)}">'
        '<button class="btn ghost" type="submit">Undo</button></form>'
    )


def _refusal_details(r: Refusal) -> str:
    body = [f"<div><b>reason:</b> {esc(r.reason.value)}</div>",
            f"<div>{esc(r.explanation)}</div>"]
    if r.taint_path:
        body.append("<div><b>taint path:</b></div>")
        body.extend(f"<div>{i + 1}. {esc(p)}</div>" for i, p in enumerate(r.taint_path))
    return f"<details><summary>details</summary>{''.join(body)}</details>"


def _tier_for(adapters: dict[str, Adapter], action: Action) -> str:
    adapter = adapters.get(action.app)
    if adapter is None:
        return "—"
    try:
        return meter(adapter.capabilities().tier_of(action.operation).name)
    except Exception:  # noqa: BLE001 - operation may no longer be declared
        return "—"


def _history(adapters: dict[str, Adapter], ledger: ActionLedger) -> str:
    receipts = ledger.history()
    refusals = ledger.refusals
    out = ["<h2>Run history</h2>"]

    if not receipts and not refusals:
        total, _n_apps, _counts = _tier_counts(adapters)
        out.append(empty(
            f"No actions taken yet. {total} operation{'s' if total != 1 else ''} "
            "are available — nothing runs without evidence, and nothing that "
            "leaves the organisation runs without you."
        ))
        return "".join(out)

    entries: list[tuple[datetime, ActionReceipt | Refusal]] = []
    entries.extend((r.executed_at, r) for r in receipts)
    entries.extend((r.refused_at, r) for r in refusals)  # type: ignore[misc]
    entries.sort(key=lambda e: e[0], reverse=True)

    rows = []
    for when, entry in entries:
        action = entry.action
        dotted = f"{action.app}.{action.operation}"
        evidence = ", ".join(action.justified_by) or "—"

        if isinstance(entry, Refusal):
            outcome_word = "held" if entry.reason.value == "gate_timeout" else "refused"
            outcome = pill(outcome_word, "attention" if outcome_word == "held" else "stop")
            outcome_cell = outcome + _refusal_details(entry)
            undo = "—"
        else:
            outcome_word = "undone" if entry.is_undone else "done"
            outcome_cell = pill(outcome_word, "quiet" if outcome_word == "undone" else "ok")
            undo = _undo_cell(entry)

        rows.append((
            esc(_when(when)),
            f"<code>{esc(dotted)}</code>",
            esc(_target_text(action.target)),
            _tier_for(adapters, action),
            outcome_cell,
            f'<span class="mono">{esc(evidence)}</span>',
            undo,
        ))

    out.append(table(
        ("Time", "Action", "Target", "Tier", "Outcome", "Evidence", "Undo"),
        rows,
    ))
    return "".join(out)
