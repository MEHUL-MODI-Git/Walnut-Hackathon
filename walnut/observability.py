"""A thin tracing wrapper around Lemma (uselemma-tracing).

Judges read these traces, so the instrumentation has to be honest and legible —
but the rest of Walnut cannot be allowed to depend on it. Lemma is a hackathon
sponsor integration, not infrastructure: if `LEMMA_API_KEY` is unset, the package
is missing, or the sponsor's service is down, every adapter and the brain itself
must keep working exactly as if this module did not exist.

That is enforced with a null-object pattern rather than scattered
`try/except ImportError` calls: `Tracer` always holds *something* that satisfies
the same small client protocol — either the real `uselemma_tracing.Lemma` client
or `_NullClient`, which runs the traced callback directly and records nothing.
Callers never branch on whether tracing is live; they call `tracer.tool(...)`
and it is either shipped to Lemma or thrown away, identically from the caller's
point of view.

Confirmed against docs.uselemma.ai (tracing/instrumentation/{setup,concepts,
traces,tool-calls,generations}, guides/building-high-quality-traces), 2026-09-12:

    pip install uselemma-tracing
    export LEMMA_API_KEY="lma_..."
    export LEMMA_PROJECT_ID="proj_..."

    from uselemma_tracing import Lemma
    lemma = Lemma()

    def run(trace):
        trace.record_tool(name=..., input=..., output=..., duration_ms=..., error=...)
        trace.record_generation(name=..., input=..., output=..., model=..., error=...)
        return result

    answer = lemma.trace("agent-name", run, input=..., thread_id=..., user_id=...)

One agent execution is one trace; the root callback receives a trace object and
every tool call / generation recorded on it becomes a child span. If the callback
raises, Lemma marks the trace failed, ships it, and re-raises the original
exception unchanged — tracing must never change what the caller observes.

Where the docs differed from the brief this module was built against: `trace.tool`
does not exist as a bare verb — the real methods are `record_tool` /
`start_tool().end()`, and there is no `record_decision` primitive at all (see
`Tracer.decision` below for how that gap is closed). `record_tool` and
`record_generation` both also accept `duration_ms` and `status`, which this
wrapper forwards when supplied but does not fabricate.
"""

from __future__ import annotations

import functools
import logging
import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence, TypeVar

__all__ = ["Tracer", "traced", "default_tracer"]

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# The client protocol both the real SDK and the null object satisfy
# ---------------------------------------------------------------------------


class _LemmaHandle(Protocol):
    """What `Tracer` needs from the object Lemma hands into a trace callback.

    Kept minimal and structural (a `Protocol`, not a base class) so the real
    `uselemma_tracing` trace object satisfies it by duck typing — this module
    never imports Lemma's types, only calls methods it is documented to have.
    """

    def record_tool(
        self,
        *,
        name: str,
        input: Any,
        output: Any = None,
        duration_ms: int | None = None,
        status: str | None = None,
        error: BaseException | None = None,
    ) -> None: ...

    def record_generation(
        self,
        *,
        name: str,
        input: Any,
        output: Any = None,
        model: str,
        duration_ms: int | None = None,
        status: str | None = None,
        error: BaseException | None = None,
    ) -> None: ...


class _LemmaClient(Protocol):
    """What `Tracer` needs from the top-level client (`Lemma()` or a stand-in)."""

    def trace(
        self,
        name: str,
        fn: Callable[[_LemmaHandle], T],
        *,
        input: Any = None,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> T: ...


class _NullHandle:
    """The trace object handed to callbacks when tracing is off.

    Every record_* call is a deliberate no-op rather than a raised
    `NotImplementedError` — a hackathon demo with no Lemma key configured must
    behave identically to one with a key, minus the traces landing anywhere.
    """

    def record_tool(self, **_kwargs: Any) -> None:
        return None

    def record_generation(self, **_kwargs: Any) -> None:
        return None


class _NullClient:
    """Stands in for `uselemma_tracing.Lemma` with zero network dependency.

    `trace()` runs the callback synchronously against a `_NullHandle` and does
    not swallow exceptions: a failing traced function must fail exactly as it
    would if this module were never imported. This is the one place the
    real SDK's documented "catch, mark failed, ship, re-raise" behaviour is
    reproduced minus the "ship" step, since there is nowhere to ship to.
    """

    def trace(
        self,
        name: str,
        fn: Callable[[_LemmaHandle], T],
        *,
        input: Any = None,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> T:
        return fn(_NullHandle())


def _build_client() -> tuple[_LemmaClient, bool]:
    """Resolve the real Lemma client if and only if it can plausibly work.

    Three independent reasons to fall back, all treated the same way: the
    package is not installed (hackathon judges may run this without the
    optional `obs` extra), the credentials are not configured (local dev,
    CI, or a laptop with no sponsor account), or the SDK's own constructor
    raises (a bad key, a network probe at init, an SDK bug). None of these
    are this application's problem to surface as a crash.
    """
    api_key = os.environ.get("LEMMA_API_KEY", "").strip()
    if not api_key:
        return _NullClient(), False

    try:
        from uselemma_tracing import Lemma  # type: ignore[import-not-found]
    except ImportError:
        logger.info(
            "LEMMA_API_KEY is set but uselemma-tracing is not installed "
            "(pip install 'walnut[obs]'); tracing is disabled."
        )
        return _NullClient(), False

    try:
        client = Lemma()
    except Exception:
        logger.warning("Lemma() failed to initialise; tracing is disabled.", exc_info=True)
        return _NullClient(), False

    return client, True


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------


class Tracer:
    """Records one agent execution and everything inside it, or records nothing.

    Usage mirrors Lemma's own shape closely on purpose, so a trace produced by
    this wrapper reads the same in the dashboard as one instrumented directly
    against the SDK:

        tracer = Tracer()
        tracer.run("triage-agent", lambda: do_the_work(), input=ticket.text)

    Inside the wrapped callable, `tracer.tool(...)`, `tracer.generation(...)`,
    and `tracer.decision(...)` attach child spans to whichever trace is
    currently running — the callable does not need to accept or thread through
    a trace object itself. That plumbing is a `ContextVar` rather than an
    instance attribute so nested or concurrent `run()` calls (e.g. two adapter
    calls dispatched with `asyncio.gather`) each see their own trace and never
    cross-attribute spans between them.
    """

    def __init__(self, *, client: _LemmaClient | None = None) -> None:
        if client is not None:
            self._client: _LemmaClient = client
            self._enabled = True
        else:
            self._client, self._enabled = _build_client()
        self._current: ContextVar[_LemmaHandle | None] = ContextVar(
            f"walnut_lemma_trace_{id(self)}", default=None
        )

    @property
    def enabled(self) -> bool:
        """True only when a real Lemma client is wired up.

        Exposed mainly for tests and for a startup log line — application code
        should never need to branch on this, that is the entire point of the
        null-object pattern above.
        """
        return self._enabled

    # -- the trace itself -----------------------------------------------

    def run(
        self,
        name: str,
        fn: Callable[[], T],
        *,
        input: Any = None,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> T:
        """Wrap one agent execution as a single Lemma trace.

        `fn` takes no arguments — unlike Lemma's raw `lemma.trace(name, run,
        ...)`, where `run` receives the trace object, this wrapper stashes the
        active trace in a context variable so `tool()` / `generation()` /
        `decision()` can be called from anywhere in the call stack underneath
        `fn`, including inside adapter methods several frames down that have
        no idea a trace is even running. Kept as a zero-arg callable (use a
        closure or `functools.partial`) rather than `*args, **kwargs` so the
        call site stays unambiguous about what is trace metadata (`input`,
        `thread_id`, `user_id`) and what is the work itself.

        Exceptions are never caught here: they propagate to the caller exactly
        as `fn()` raised them, after Lemma (or `_NullClient`) has had a chance
        to mark the trace failed.
        """

        def _callback(handle: _LemmaHandle) -> T:
            token: Token[_LemmaHandle | None] = self._current.set(handle)
            try:
                return fn()
            finally:
                self._current.reset(token)

        return self._client.trace(
            name, _callback, input=input, thread_id=thread_id, user_id=user_id
        )

    # -- child spans ------------------------------------------------------

    def tool(
        self,
        name: str,
        input: Any,
        output: Any = None,
        *,
        error: BaseException | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Record one adapter/tool call as a child span of the active trace.

        A no-op if called outside `run()` (no active trace) — adapters should
        not have to know or care whether the call they are part of happens to
        be traced this time. `error` maps to Lemma's `status="ERROR"` plus
        `error=`, matching the documented pattern for a tool call that raised.
        """
        handle = self._current.get()
        if handle is None:
            return
        handle.record_tool(
            name=name,
            input=input,
            output=output,
            duration_ms=duration_ms,
            status="ERROR" if error is not None else None,
            error=error,
        )

    def generation(
        self,
        name: str,
        input: Any,
        output: Any,
        model: str,
        *,
        error: BaseException | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Record one LLM call as a child span of the active trace.

        Same no-op-outside-a-trace behaviour as `tool()`. `model` is required
        (not defaulted) because an unlabelled generation is close to useless
        in the dashboard — you cannot tell cost or latency regressions apart
        by model without it.
        """
        handle = self._current.get()
        if handle is None:
            return
        handle.record_generation(
            name=name,
            input=input,
            output=output,
            model=model,
            duration_ms=duration_ms,
            status="ERROR" if error is not None else None,
            error=error,
        )

    def decision(
        self,
        name: str,
        outcome: str,
        confidence: float,
        evidence_ids: Sequence[str],
    ) -> None:
        """Record one governance decision — a gate, a refusal, an executed action.

        Lemma has exactly two span primitives: a "tool call" (a deterministic
        or side-effecting invocation) and a "generation" (a sampled LLM call).
        A governance decision is neither, but it is much closer to the first:
        Walnut's gates and the harness's refusal logic
        (`HARNESS-OPERATING-DOCTRINE.md` invariant 11 — never assume, verify or
        refuse) are meant to be *deterministic given their inputs*, the same
        way a tool is. Modelling a decision as a "generation" would misleadingly
        imply it was sampled from a model and is therefore non-reproducible,
        which is precisely the property this system is built to avoid. So a
        decision is recorded as `record_tool(name=f"decision:{name}", ...)`
        with a structured output — `{"outcome", "confidence", "evidence_ids"}`
        — rather than free text, deliberately echoing the shape of
        `Brain.record_decision()` in `walnut/brain.py` so the same event reads
        consistently whether you are looking at the graph's decision chain or
        at a Lemma trace.

        A no-op outside `run()`, like `tool()` and `generation()`.
        """
        handle = self._current.get()
        if handle is None:
            return
        handle.record_tool(
            name=f"decision:{name}",
            input={"evidence_ids": list(evidence_ids)},
            output={
                "outcome": outcome,
                "confidence": confidence,
                "evidence_ids": list(evidence_ids),
            },
        )


# ---------------------------------------------------------------------------
# Convenience decorator
# ---------------------------------------------------------------------------

_default_tracer: Tracer | None = None


def default_tracer() -> Tracer:
    """A process-wide `Tracer`, built lazily from the environment.

    Exists only so `@traced` works unadorned on a free function or a method
    whose class has no `tracer` attribute of its own. Anything that already
    owns a `Tracer` (the brain, an agent runner wired up at startup) should
    pass it explicitly rather than relying on this global — a singleton is a
    convenience for the decorator, not a recommended way to hold state.
    """
    global _default_tracer
    if _default_tracer is None:
        _default_tracer = Tracer()
    return _default_tracer


@dataclass(frozen=True, slots=True)
class _CallInput:
    """What gets sent to Lemma as a traced call's `input`.

    A dataclass rather than a bare dict so the shape is self-documenting in
    the dashboard: positional and keyword arguments are kept distinguishable
    instead of being flattened into one ambiguous blob.
    """

    args: tuple[Any, ...]
    kwargs: dict[str, Any]


def traced(
    name: str | None = None, *, tracer: Tracer | None = None
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorate an adapter method so every call is recorded as a tool span.

    Resolution order for which `Tracer` to use, cheapest and most specific
    first:

    1. `tracer=` passed to the decorator itself.
    2. `self.tracer` on the bound instance, if the wrapped callable is a
       method and its instance carries one — this is the common case, since
       adapters are expected to hold the same `Tracer` the brain was built
       with.
    3. `default_tracer()`, so the decorator still does something sane on a
       bare function or an instance with no tracer wired up.

    The wrapped call's exception, if any, is recorded via `Tracer.tool`'s
    `error=` and then always re-raised unchanged — this decorator observes,
    it never suppresses.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        span_name = name or fn.__qualname__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            active = tracer
            if active is None and args:
                candidate = getattr(args[0], "tracer", None)
                if isinstance(candidate, Tracer):
                    active = candidate
            if active is None:
                active = default_tracer()

            call_input = _CallInput(args=args[1:] if args else args, kwargs=kwargs)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - re-raised unchanged below
                active.tool(span_name, call_input, None, error=exc)
                raise
            active.tool(span_name, call_input, result)
            return result

        return wrapper

    return decorator
