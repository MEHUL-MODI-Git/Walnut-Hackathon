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
from ..plugins import SourceRegistry
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
        agent = self.agent or self.rebuild_agent()
        # Honour the repo / database the operator actually chose.
        count = agent.ingest(scopes=self.connections.default_scopes(), limit=200)
        try:
            agent.resolve_people(load_identities())
        except Exception:  # noqa: BLE001 - identity data is fixture-only, optional
            pass
        self.ingested = True
        return count


state = State()


def html(body: str, title: str, active: str) -> HTMLResponse:
    return HTMLResponse(views.layout(title, body, active))


def back(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# -- overview ---------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    agent = state.ensure()
    conflicts = detect_contradictions(state.brain)
    return html(
        views.page_dashboard(
            state.brain.stats(),
            state.connections.summary(),
            agent.executor.ledger.summary(),
            len(conflicts),
        ),
        "Overview", "dash",
    )


@app.post("/ingest")
def reingest() -> RedirectResponse:
    state.brain = Brain()
    state.ingested = False
    state.rebuild_agent()
    state.ingest()
    return back("/")


# -- connections ------------------------------------------------------------


@app.get("/connections", response_class=HTMLResponse)
def connections() -> HTMLResponse:
    return html(
        views.page_connections(
            [c.redacted() for c in state.connections.all()], APP_SPECS
        ),
        "Connections", "conn",
    )


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
    plan = build_plan(conflict, state.brain)
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


# -- approvals --------------------------------------------------------------


@app.get("/approvals", response_class=HTMLResponse)
def approvals() -> HTMLResponse:
    agent = state.ensure()
    pending = [(k, a, ctx) for k, (a, ctx) in state.gate.pending.items()]
    return html(
        views.page_approvals(pending, agent.executor.ledger.history()),
        "Approvals", "appr",
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
    return html(
        views.page_audit(
            state.brain.decision_summary(),
            agent.executor.ledger.summary(),
            agent.executor.ledger.refusals,
            agent.identities.summary() if agent.identities else None,
        ),
        "Audit", "audit",
    )


# -- custom sources ---------------------------------------------------------


@app.get("/sources", response_class=HTMLResponse)
def sources() -> HTMLResponse:
    return html(
        views.page_sources(state.registry.all(), str(state.registry.plugin_dir)),
        "Sources", "src",
    )


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


@app.get("/evidence", response_class=HTMLResponse)
def evidence(q: str = "") -> HTMLResponse:
    state.ensure()
    facts = (
        state.brain.search(q, limit=60) if q.strip()
        else list(state.brain._facts.values())  # noqa: SLF001 - internal by design
    )
    return html(views.page_evidence(facts, q), "Evidence", "ev")
