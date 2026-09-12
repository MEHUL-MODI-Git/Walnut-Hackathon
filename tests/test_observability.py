"""Tests for `walnut.observability`.

Three things must hold for this module to be trustworthy in a demo:

1. With no Lemma credentials configured, everything still runs — the no-op
   path is what most judges will actually exercise, since nobody is going to
   hand out `LEMMA_API_KEY` for a laptop demo.
2. When a client *is* wired up, `Tracer.tool` / `.generation` / `.decision`
   route to the right Lemma primitives with the right shapes — verified here
   against a fake client/handle pair standing in for the real SDK.
3. A traced function that raises still raises. Tracing must never change the
   caller-visible behaviour of the code it observes.

`uselemma-tracing` is deliberately NOT installed in this environment (see the
task note in `walnut/observability.py`): these tests prove the no-op path is
what actually runs when the package or the key is missing, not merely that it
would run if given the chance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from walnut.observability import Tracer, traced


# ---------------------------------------------------------------------------
# A fake Lemma client/handle pair — a test double for the real SDK, built to
# the same structural protocol `Tracer` expects (see `_LemmaClient` /
# `_LemmaHandle` in walnut/observability.py).
# ---------------------------------------------------------------------------


@dataclass
class FakeHandle:
    tools: list[dict[str, Any]] = field(default_factory=list)
    generations: list[dict[str, Any]] = field(default_factory=list)

    def record_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def record_generation(self, **kwargs: Any) -> None:
        self.generations.append(kwargs)


@dataclass
class FakeClient:
    """Mimics Lemma's documented contract: run the callback, and if it raises,
    mark the trace failed (recorded here rather than shipped anywhere) before
    re-raising the original exception unchanged."""

    handle: FakeHandle = field(default_factory=FakeHandle)
    traces: list[dict[str, Any]] = field(default_factory=list)
    failures: list[BaseException] = field(default_factory=list)

    def trace(
        self,
        name: str,
        fn: Callable[[FakeHandle], Any],
        *,
        input: Any = None,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> Any:
        self.traces.append(
            {"name": name, "input": input, "thread_id": thread_id, "user_id": user_id}
        )
        try:
            return fn(self.handle)
        except Exception as exc:
            self.failures.append(exc)
            raise


# ---------------------------------------------------------------------------
# (a) No credentials at all -> the no-op path, and it must not crash anything.
# ---------------------------------------------------------------------------


def test_no_op_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMMA_API_KEY", raising=False)
    monkeypatch.delenv("LEMMA_PROJECT_ID", raising=False)

    tracer = Tracer()
    assert tracer.enabled is False

    def do_work() -> str:
        # Calling every recording method inside a no-op trace must be silent
        # and side-effect-free, never raise, and never require a real client.
        tracer.tool("fetch_evidence", {"query": "x"}, {"count": 3})
        tracer.generation("draft", {"prompt": "hi"}, "hello", model="local-llm")
        tracer.decision("gate:publish", "approved", 0.92, ["ev-1", "ev-2"])
        return "ok"

    result = tracer.run("demo-agent", do_work, input="ticket text")
    assert result == "ok"


def test_no_op_recording_outside_a_trace_is_also_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEMMA_API_KEY", raising=False)
    tracer = Tracer()
    # No `run()` in progress at all — adapters may legitimately call tool()
    # from code paths that aren't always wrapped in a trace.
    tracer.tool("standalone_call", {"a": 1}, {"b": 2})
    tracer.generation("standalone_gen", "in", "out", model="m")
    tracer.decision("standalone_decision", "refused", 0.1, [])


def test_missing_package_falls_back_to_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with a key set, an ImportError on the SDK must not crash us.

    `uselemma-tracing` is not installed in this environment, so setting the
    key is enough to exercise the real fallback branch (`_build_client`'s
    `except ImportError`) rather than merely asserting it exists.
    """
    monkeypatch.setenv("LEMMA_API_KEY", "lma_fake_for_test")
    monkeypatch.setenv("LEMMA_PROJECT_ID", "proj_fake_for_test")

    tracer = Tracer()
    assert tracer.enabled is False  # package genuinely absent from this venv

    assert tracer.run("demo-agent", lambda: 42) == 42


# ---------------------------------------------------------------------------
# (b) With a client wired up, calls land in the right places with the right
#     shapes.
# ---------------------------------------------------------------------------


def test_records_tool_generation_and_decision_into_fake_sink() -> None:
    client = FakeClient()
    tracer = Tracer(client=client)
    assert tracer.enabled is True

    def do_work() -> str:
        tracer.tool("fetch_evidence", {"query": "renewal date"}, {"count": 2})
        tracer.generation(
            "draft-reply", {"prompt": "hi"}, "hello there", model="gpt-4o"
        )
        tracer.decision("gate:publish", "approved", 0.87, ["ev-1", "ev-2"])
        return "done"

    result = tracer.run(
        "demo-agent", do_work, input="ticket text", thread_id="t-1", user_id="u-1"
    )

    assert result == "done"
    assert client.traces == [
        {"name": "demo-agent", "input": "ticket text", "thread_id": "t-1", "user_id": "u-1"}
    ]

    assert len(client.handle.tools) == 2  # fetch_evidence + the decision
    fetch_call = client.handle.tools[0]
    assert fetch_call["name"] == "fetch_evidence"
    assert fetch_call["input"] == {"query": "renewal date"}
    assert fetch_call["output"] == {"count": 2}
    assert fetch_call["error"] is None

    decision_call = client.handle.tools[1]
    assert decision_call["name"] == "decision:gate:publish"
    assert decision_call["output"] == {
        "outcome": "approved",
        "confidence": 0.87,
        "evidence_ids": ["ev-1", "ev-2"],
    }

    assert len(client.handle.generations) == 1
    gen_call = client.handle.generations[0]
    assert gen_call["name"] == "draft-reply"
    assert gen_call["model"] == "gpt-4o"
    assert gen_call["output"] == "hello there"


def test_traced_decorator_records_on_instance_tracer() -> None:
    client = FakeClient()

    class FakeAdapter:
        name = "slack"

        def __init__(self) -> None:
            self.tracer = Tracer(client=client)

        @traced("slack.fetch")
        def fetch(self, scope: str) -> list[str]:
            return [f"msg-in-{scope}"]

    adapter = FakeAdapter()

    def run_it() -> list[str]:
        return adapter.fetch("general")

    out = adapter.tracer.run("wrapper-trace", run_it)
    assert out == ["msg-in-general"]

    assert len(client.handle.tools) == 1
    call = client.handle.tools[0]
    assert call["name"] == "slack.fetch"
    assert call["output"] == ["msg-in-general"]
    assert call["error"] is None


# ---------------------------------------------------------------------------
# (c) A failing traced function still propagates its exception, and the
#     failure is recorded on the way out.
# ---------------------------------------------------------------------------


def test_failing_function_propagates_and_records_error() -> None:
    client = FakeClient()
    tracer = Tracer(client=client)

    class AdapterBoom(RuntimeError):
        pass

    def do_work() -> None:
        try:
            raise AdapterBoom("upstream API returned 500")
        except AdapterBoom as exc:
            tracer.tool("call_upstream", {"id": 1}, None, error=exc)
            raise

    with pytest.raises(AdapterBoom, match="upstream API returned 500"):
        tracer.run("demo-agent", do_work)

    # The trace-level failure was observed by the (fake) client...
    assert len(client.failures) == 1
    assert isinstance(client.failures[0], AdapterBoom)

    # ...and the tool call itself was recorded as an error, not silently
    # dropped.
    assert len(client.handle.tools) == 1
    call = client.handle.tools[0]
    assert call["name"] == "call_upstream"
    assert call["status"] == "ERROR"
    assert call["error"] is client.failures[0]


def test_failing_function_propagates_even_in_no_op_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEMMA_API_KEY", raising=False)
    tracer = Tracer()

    def do_work() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        tracer.run("demo-agent", do_work)
