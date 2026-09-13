"""The Walnut web app.

    uvicorn walnut.web.app:app --reload --port 8000

One process, one in-memory brain. There is no database and no session store: this is a
demo surface for a hackathon, and every piece of state it holds is either rebuildable
from the connected apps in seconds or is deliberately ephemeral (credentials).

The app never calls an adapter directly. It goes through `ConnectionManager.adapters()`,
which hands back live adapters where an account is connected and fixture adapters
everywhere else — so every screen works identically whether or not anything is plugged
in. That is what makes the product demonstrable to someone who has not yet set up five
API tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..actions.executor import ActionExecutor
from ..actions.governance import QueueGate
from ..adapters.fixture import load_identities
from ..agent import WalnutAgent
from ..brain import Brain
from ..connections import APP_SPECS, ConnectionManager
from ..contradiction import detect_contradictions
from ..playbook import build_plan
from ..observability import Tracer
from ..intent import parse_intent
from ..clinical_sources import register_clinical_sources
from ..internal_systems import register_internal_systems
from ..plugins import SourceRegistry
from ..briefing import build_briefing
from ..retrieval import assemble, link_by_subject
from . import design
from . import views

app = FastAPI(title="Walnut", docs_url=None, redoc_url=None)


@dataclass
class State:
    """Everything the app holds between requests."""

    connections: ConnectionManager = field(default_factory=ConnectionManager)
    brain: Brain = field(default_factory=Brain)
    gate: QueueGate = field(default_factory=QueueGate)
    tracer: Tracer = field(default_factory=Tracer)
    registry: SourceRegistry = field(default_factory=SourceRegistry)
    agent: WalnutAgent | None = None
    last_question: str = "is the dosing engine v2 fix actually shipped?"
    last_answer: Any = None
    last_results: list[Any] = field(default_factory=list)
    last_intent: Any = None
    ingested: bool = False
    edges: int = 0
    _internals_checked: bool = False

    def rebuild_agent(self) -> WalnutAgent:
        """Rebuild against whatever is currently connected.

        Called after every connect/disconnect so the agent always reflects the live
        set of adapters rather than the set that existed at process start.
        """
        adapters = self.all_adapters()
        executor = ActionExecutor(
            adapters, self.brain, self.gate,
            verify_freshness=False, tracer=self.tracer,
        )
        self.agent = WalnutAgent(adapters, self.brain, executor, tracer=self.tracer)
        return self.agent

    def all_adapters(self) -> dict[str, Any]:
        """The five built-ins plus every custom source that PASSED conformance.

        A failing custom source stays visible on the sources page so it can be fixed,
        but it is not wired in — a company brain assembled from connectors that break
        the guarantees is worse than one without them.
        """
        return {**self.connections.adapters(), **self.registry.usable_adapters()}

    def ensure(self) -> WalnutAgent:
        if self.agent is None:
            self.rebuild_agent()
        assert self.agent is not None
        if not self.ingested:
            self.ingest()
        return self.agent

    def ingest(self) -> int:
        # The customer's own internal systems join the brain like any other source.
        # Unreachable ones are registered as failures the console can show, never
        # silently skipped — that silence is the failure the coverage table exists for.
        if not self._internals_checked:
            self._internals_checked = True
            try:
                register_clinical_sources(self.registry)
                register_internal_systems(self.registry)
                self.rebuild_agent()
            except Exception:  # noqa: BLE001 - an optional system must never block boot
                pass

        agent = self.agent or self.rebuild_agent()
        # Honour the repo / database the operator actually chose.
        count = agent.ingest(scopes=self.connections.default_scopes(), limit=200)
        try:
            agent.resolve_people(load_identities())
        except Exception:  # noqa: BLE001 - identity data is fixture-only, optional
            pass
        # A brain with no edges is a database with extra steps. Linking on specific
        # shared referents is what makes traversal and "what else mentions this"
        # answerable at all.
        try:
            self.edges = link_by_subject(self.brain)
        except Exception:  # noqa: BLE001 - linking is an enhancement, never a blocker
            self.edges = 0
        self.ingested = True
        return count

    def unsearchable(self) -> list[tuple[str, str]]:
        """Sources that exist but could NOT be searched.

        Without this the coverage table silently omits a broken connector, which makes
        an honest search indistinguishable from one that skipped four systems.
        """
        out: list[tuple[str, str]] = []
        for conn in self.connections.all():
            if conn.state.value == "error":
                out.append((conn.app, conn.error or "connection error"))
        for src in self.registry.all():
            if not src.usable:
                out.append((src.name, src.summary() or "not conforming"))
        return out

    def held(self) -> dict[str, int]:
        return {app: len(self.brain.by_app(app)) for app in self.brain.stats()["apps"]}

    def last_seen(self) -> dict[str, Any]:
        """When each source last handed us a record.

        The connectors table had a Records column and a Last sync column, and every
        row of both read "—" — including the rows for sources that had just supplied
        a hundred and thirteen records between them. Two empty columns on the first
        screen anyone opens say the product is unfinished, when what was missing was
        the wiring to the numbers it already had.
        """
        out: dict[str, Any] = {}
        for app in self.brain.stats()["apps"]:
            stamps = [f.pointer.retrieved_at for f in self.brain.by_app(app)
                      if f.pointer.retrieved_at]
            if stamps:
                out[app] = max(stamps)
        return out

    def shell(self) -> dict[str, Any]:
        """Badges and the sidebar status strip."""
        faults = len(self.unsearchable())
        stats = self.brain.stats()
        return {
            "badges": {
                "approvals": len(self.gate.pending) or None,
                "connectors": faults or None,
            },
            "strip": (f"{stats.get('facts_indexed', 0)} facts · "
                      f"{len(stats.get('apps', []))} sources<br>"
                      f"{getattr(self, 'edges', 0)} links"
                      + (f" · {len(self.gate.pending)} pending" if self.gate.pending else "")),
        }


state = State()


def html(body: str, title: str, active: str) -> HTMLResponse:
    """Render through the new sidebar shell."""
    ctx = state.shell()
    return HTMLResponse(
        design.layout(title, body, active, badges=ctx["badges"], strip=ctx["strip"])
    )


def back(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# -- overview ---------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def retrieval(q: str = "") -> HTMLResponse:
    """The hero: everything the company knows about one subject."""
    from .screens.retrieval import page_retrieval

    agent = state.ensure()
    found = assemble(state.brain, q, identities=agent.identities) if q.strip() else None
    brief = (
        build_briefing(found, detect_contradictions(state.brain))
        if found is not None and found.total
        else None
    )
    return html(
        page_retrieval(q, found, briefing=brief, unsearchable=state.unsearchable(),
                       held=state.held()),
        "Ask", "retrieval",
    )


@app.post("/ingest")
def reingest() -> RedirectResponse:
    state.brain = Brain()
    state.ingested = False
    state.rebuild_agent()
    state.ingest()
    return back("/")


# -- connections ------------------------------------------------------------


@app.get("/connectors", response_class=HTMLResponse)
def connectors() -> HTMLResponse:
    """Built-in connectors and the customer's own systems, in one table.

    To an admin these are the same object: a thing that puts records into the brain.
    The split between a first-party adapter and a registered custom source is an
    implementation detail, and a customer who connects their own database does not
    think of it as second-class.
    """
    from .screens.connectors import page_connectors

    state.ensure()
    return html(
        page_connectors([c.redacted() for c in state.connections.all()], APP_SPECS,
                        state.registry.all(), str(state.registry.plugin_dir),
                        ingested=state.held(), last_seen=state.last_seen()),
        "Connectors", "connectors",
    )


@app.get("/connectors/{app_name}", response_class=HTMLResponse)
def connector_detail(app_name: str) -> HTMLResponse:
    """One connector: what it can see, what it can write, and why it is failing."""
    from .screens.connectors import page_connector_detail

    # Called for the ingest it guarantees, not for the agent it returns — this page
    # no longer reads the action ledger, so there is nothing here to name it for.
    state.ensure()
    conn = next((c for c in state.connections.all() if c.app == app_name), None)
    source = next((s for s in state.registry.all() if s.name == app_name), None)
    adapter = state.all_adapters().get(app_name)

    caps = None
    if adapter is not None:
        try:
            caps = adapter.capabilities()
        except Exception:  # noqa: BLE001 - an adapter that cannot describe itself
            caps = None

    facts = state.brain.by_app(app_name)
    stamps = sorted(f.occurred_at or f.pointer.retrieved_at for f in facts) if facts else []

    # "Recently ingested" means ingested. This was handed `executor.ledger.history()`
    # — the record of writes Walnut has PERFORMED — so every connector page reported a
    # count of ingested records and then, an inch below it, that nothing had been
    # ingested. Both cannot be true, and on a product whose entire claim is that it
    # does not assert things it cannot show you, that particular contradiction is the
    # worst one available. Newest first, sorted on the same key as `stamps`.
    recent = sorted(facts, key=lambda f: f.occurred_at or f.pointer.retrieved_at)[-8:][::-1]

    return html(
        page_connector_detail(
            conn.redacted() if conn else None,
            APP_SPECS.get(app_name), source, caps, len(facts),
            stamps[0].strftime("%Y-%m-%d") if stamps else "—",
            stamps[-1].strftime("%Y-%m-%d") if stamps else "—",
            recent,
        ),
        f"{app_name} · Connectors", "connectors",
    )


@app.get("/connections", response_class=HTMLResponse)
def connections_legacy() -> HTMLResponse:
    return connectors()


@app.get("/sources", response_class=HTMLResponse)
def sources_legacy() -> HTMLResponse:
    return connectors()


@app.post("/connectors/{app_name}/test")
def test_connection(app_name: str) -> RedirectResponse:
    """Re-probe a live credential. Validation is using it, not checking its shape."""
    conn = next((c for c in state.connections.all() if c.app == app_name), None)
    if conn is not None and conn.credentials:
        state.connections.connect(app_name, dict(conn.credentials))
    return back(f"/connectors/{app_name}")


@app.post("/connections/{app_name}/connect")
async def connect(app_name: str, request: Request) -> RedirectResponse:
    """Validate the credential by using it, then swap the live adapter in."""
    form = await request.form()
    creds = {k: str(v) for k, v in form.items()}
    state.connections.connect(app_name, creds)
    # Re-ingest so the change is visible immediately rather than after a manual step.
    state.brain = Brain()
    state.ingested = False
    state.rebuild_agent()
    return back("/connections")


@app.post("/connections/{app_name}/disconnect")
def disconnect(app_name: str) -> RedirectResponse:
    state.connections.disconnect(app_name)
    state.brain = Brain()
    state.ingested = False
    state.rebuild_agent()
    return back("/connections")


# -- investigate ------------------------------------------------------------


@app.get("/investigate", response_class=HTMLResponse)
def investigate_get() -> HTMLResponse:
    state.ensure()
    return html(
        views.page_investigate(
            state.last_question, state.last_answer,
            detect_contradictions(state.brain), state.last_results, state.last_intent,
        ),
        "Investigate", "inv",
    )


@app.post("/investigate")
def investigate_post(question: str = Form("")) -> RedirectResponse:
    agent = state.ensure()
    state.last_question = question or state.last_question
    inv = agent.investigate(state.last_question)
    state.last_answer = inv.answer
    state.last_results = []
    return back("/investigate")


@app.post("/act")
def act(conflict_id: str = Form("")) -> RedirectResponse:
    """Propose and execute the standard five-app response to ONE specific contradiction.

    Addressed by conflict id rather than by subject. Several conflicts commonly share a
    subject, so subject-addressing silently substituted a different one — writing an
    evidence chain into four external systems that did not match the one the human read.
    If the id no longer resolves (the brain was re-ingested), do nothing rather than
    guess at a replacement.
    """
    agent = state.ensure()
    conflicts = [c for c in detect_contradictions(state.brain) if c.id == conflict_id]
    if not conflicts:
        state.last_results = []
        return back("/investigate")

    conflict = conflicts[0]
    plan = build_plan(conflict, state.brain, adapters=state.all_adapters())
    state.last_results = agent.execute_all(agent.propose_for_conflict(conflict, plan))
    return back("/investigate")


@app.post("/request")
def propose_from_request(request_text: str = Form("", alias="request")) -> RedirectResponse:
    """Turn a sentence into proposed actions — or into a question."""
    agent = state.ensure()
    # Prefer the evidence already on screen: "file an issue about this" names nothing
    # retrievable, and the user plainly means what they are looking at.
    hint = [facts[0] for _, facts in (state.last_answer.facts if state.last_answer else [])]
    state.last_intent = parse_intent(
        request_text, state.brain, state.all_adapters(), evidence_hint=hint or None
    )
    state.last_results = []
    return back("/investigate")


@app.post("/request/execute")
def execute_request() -> RedirectResponse:
    """Run the proposed actions through the same choke point as everything else."""
    agent = state.ensure()
    if state.last_intent is None or not state.last_intent.proposed:
        return back("/investigate")
    state.last_results = agent.execute_all(state.last_intent.actions)
    state.last_intent = None
    return back("/investigate")


@app.get("/actions", response_class=HTMLResponse)
def actions(tab: str = "catalogue") -> HTMLResponse:
    """What the agent can do, per connected app — and what it has done."""
    from .screens.actions import page_actions

    agent = state.ensure()
    return html(
        page_actions(tab, state.all_adapters(), agent.executor.ledger,
                     state.last_intent, state.last_results),
        "Actions", "actions",
    )


@app.get("/investigate", response_class=HTMLResponse)
def investigate_legacy() -> RedirectResponse:
    """Investigate dissolved into Retrieval (asking) and Actions (doing)."""
    return RedirectResponse("/actions?tab=activity", status_code=307)


# -- approvals --------------------------------------------------------------


@app.get("/approvals", response_class=HTMLResponse)
def approvals() -> HTMLResponse:
    from .screens.approvals import page_approvals

    agent = state.ensure()
    pending = [(k, a, ctx) for k, (a, ctx) in state.gate.pending.items()]
    gated = sum(
        1
        for ad in state.all_adapters().values()
        for tier in ad.capabilities().operations.values()
        if int(tier) >= 2
    )
    return html(
        page_approvals(pending, gated, agent.executor.ledger.summary()["executed"]),
        "Approvals", "approvals",
    )


@app.post("/approvals/{key}/approve")
def approve(key: str) -> RedirectResponse:
    """Approve, then re-run the action so the decision is actually carried out.

    The gate records the answer and the action is re-submitted through the same
    executor — it is not waved past the other checks. An action approved by a human is
    still refused if its evidence has gone stale in the meantime.
    """
    agent = state.ensure()
    entry = state.gate.pending.get(key)
    state.gate.resolve(key, approved=True, note="approved in console")
    if entry is not None:
        state.last_results = [agent.executor.execute(entry[0])]
    return back("/approvals")


@app.post("/approvals/{key}/deny")
def deny(key: str) -> RedirectResponse:
    state.gate.resolve(key, approved=False, note="denied in console")
    return back("/approvals")


@app.post("/undo/{action_id}")
def undo(action_id: str) -> RedirectResponse:
    agent = state.ensure()
    try:
        agent.executor.undo(action_id)
    except Exception:  # noqa: BLE001 - an adapter refusing to undo is an answer
        # A sent email cannot be recalled, and the email adapter says so by raising.
        # Turning that into a 500 would present an honest refusal as a crash.
        pass
    return back("/approvals")


@app.get("/audit", response_class=HTMLResponse)
def audit() -> HTMLResponse:
    agent = state.ensure()
    from .screens.audit import page_audit

    ledger = agent.executor.ledger
    return html(
        page_audit(state.brain.decision_summary(), ledger.summary(),
                   agent.identities, ledger.summary()["refusal_reasons"]),
        "Audit", "audit",
    )


# -- custom sources ---------------------------------------------------------


@app.post("/sources/add")
async def add_source(request: Request) -> RedirectResponse:
    """Register a declarative source and validate it against the conformance suite."""
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    spec: dict[str, Any] = {k: v for k, v in form.items() if v}
    for listish in ("text_columns", "text_fields"):
        if listish in spec:
            spec[listish] = [p.strip() for p in spec[listish].split(",") if p.strip()]
    if auth := spec.pop("auth_header", None):
        spec["headers"] = {"Authorization": auth}
    if spec.get("name"):
        state.registry.add_spec(spec)
        state.ingested = False
        state.rebuild_agent()
    return back("/sources")


@app.post("/sources/rescan")
def rescan_sources() -> RedirectResponse:
    state.registry.discover_plugins()
    state.ingested = False
    state.rebuild_agent()
    return back("/sources")


@app.post("/sources/{name}/remove")
def remove_source(name: str) -> RedirectResponse:
    state.registry.remove(name)
    state.ingested = False
    state.rebuild_agent()
    return back("/sources")


# -- evidence ---------------------------------------------------------------


@app.get("/knowledge", response_class=HTMLResponse)
def knowledge(q: str = "") -> HTMLResponse:
    """The data layer, browsable — what the brain actually holds."""
    from .screens.knowledge import page_knowledge

    state.ensure()
    facts = (
        state.brain.search(q, limit=60) if q.strip()
        else list(state.brain._facts.values())  # noqa: SLF001 - internal by design
    )
    stats = dict(state.brain.stats())
    stats["edge_count"] = state.edges
    return html(
        page_knowledge(stats, facts, q, detect_contradictions(state.brain), state.held()),
        "Knowledge base", "knowledge",
    )


@app.get("/evidence", response_class=HTMLResponse)
def evidence_legacy(q: str = "") -> HTMLResponse:
    """Kept so existing links and docs do not break."""
    return knowledge(q)
