#!/usr/bin/env python
"""The three-act demo, runnable end to end with no credentials.

    python demo.py            all three acts
    python demo.py --act 3    just the refusal

Act 1  the brain      the clinic's whole estate in one graph of cited facts
Act 2  the action     a contradiction found and acted on across several systems, one gated
Act 3  the refusal    a poisoned document tries to drive the agent, and fails

Act 3 is the one that matters. Every other team will demo a success.

## What this ingests, and why it is not just the five fixture apps

The five fixture apps (slack, notion, github, linear, email) are the estate every
company has. They are not the estate this demo is *about*. The clinic's own systems —
the EHR and lab database reached through the SQL adapter (`walnut.clinical_sources`)
and the pharmacy's internal HTTP service reached through the REST adapter
(`walnut.internal_systems`) — arrive through `SourceRegistry.add_spec()`, the same
documented extension point a customer uses, and they carry the half of the corpus the
whole story turns on: a record saying `Allergies: None recorded` against a nurse who
reported a reaction, and a dose queued to go out today regardless.

Both are optional and neither can stop the demo. The EHR is built from CSV fixtures,
so it is always there. The dispensary is a separate process on port 8900; if it is not
running the source registers, fails conformance honestly, and is reported as a system
that could NOT be searched — which is the product's own guarantee demonstrated rather
than a crash. The only thing lost is the hold step in act 2, and act 2 says so out
loud instead of quietly being one action shorter.

    python -m uvicorn services.dispensary.app:app --port 8900   # optional, richer act 2
"""

from __future__ import annotations

import argparse
import sys

from walnut import Brain
from walnut.actions.executor import ActionExecutor
from walnut.actions.governance import QueueGate, Refusal
from walnut.adapters.fixture import load_all_fixtures, load_identities
from walnut.agent import WalnutAgent
from walnut.clinical_sources import register_clinical_sources
from walnut.contradiction import detect_contradictions
from walnut.internal_systems import register_internal_systems
from walnut.observability import Tracer
from walnut.playbook import build_plan
from walnut.plugins import SourceRegistry
from walnut.retrieval import link_by_subject

RULE = "─" * 78


def header(n: int, title: str, claim: str) -> None:
    print(f"\n{RULE}\nACT {n} · {title}\n{claim}\n{RULE}")


def build() -> tuple[WalnutAgent, QueueGate, SourceRegistry]:
    """The estate: five fixture apps plus the clinic's own systems.

    The custom sources go through `SourceRegistry`, which runs the full conformance
    suite against each one and hands back only the sources that PASSED. That is the
    same gate the console uses, and it is why this function returns the registry too:
    a source that did not pass is not silently missing, it is reportable.
    """
    adapters = dict(load_all_fixtures())

    registry = SourceRegistry()
    # The clinic's database. Built from fixtures/ehr.csv and fixtures/labs.csv, so it
    # is always available and never needs a server.
    register_clinical_sources(registry)
    # The pharmacy's internal HTTP service. Optional — see the module docstring.
    register_internal_systems(registry)
    adapters.update(registry.usable_adapters())

    brain = Brain()
    gate = QueueGate()
    # Traces the whole run into Lemma when LEMMA_API_KEY is present, and silently
    # no-ops when it is not. The demo must never require an observability vendor to
    # be reachable in order to run.
    tracer = Tracer()
    # Freshness checking is off for the demo: the fixture adapter is the source of
    # truth for itself, so every hash trivially matches. It is ON in the live path,
    # where it is the check that catches a source moving under a stale citation.
    executor = ActionExecutor(adapters, brain, gate, verify_freshness=False,
                              tracer=tracer)
    return WalnutAgent(adapters, brain, executor, tracer=tracer), gate, registry


def unsearchable(registry: SourceRegistry) -> list[tuple[str, str]]:
    """Sources that exist but could not be searched, with the reason.

    Printed rather than swallowed. A source that silently is not there is
    indistinguishable from one that was searched and held nothing, and telling those
    two apart is the entire product.
    """
    out: list[tuple[str, str]] = []
    for source in registry.all():
        if source.usable:
            continue
        why = source.error
        if not why and source.report is not None:
            if source.report.unreachable:
                why = f"service unreachable — {source.report.unreachable}"
            else:
                why = "; ".join(f"{name}: {detail}" for name, detail in source.report.failed)
        out.append((source.name, why or "not validated"))
    return out


def ingest(agent: WalnutAgent) -> tuple[int, int]:
    """Ingest everything, then link it. Returns (records, edges).

    Linking is not decoration. Without `link_by_subject` the graph has nodes and no
    relationships — the patient's MRN appears in the nurse's message, the EHR row, the
    lab result and the pharmacy queue, and nothing connects them, so "what else
    mentions this" has no answer and the edge count reads 0.
    """
    count = agent.ingest(limit=200)
    edges = link_by_subject(agent.brain)
    return count, edges


def act1(agent: WalnutAgent, registry: SourceRegistry, edges: int) -> None:
    header(1, "THE BRAIN", "A clinic's fragmented estate becomes one graph of cited facts.")

    stats = agent.brain.stats()
    print(f"\ningested {stats['facts_indexed']} records from "
          f"{len(stats['apps'])} sources: {', '.join(stats['apps'])}")
    print(f"graph: {stats.get('node_count', '?')} nodes, "
          f"{stats.get('edge_count', edges)} edges linked on shared referents")

    custom = [s for s in registry.all() if s.usable]
    if custom:
        print("\n  of those, connected by configuration alone — no adapter written:")
        for source in custom:
            print(f"    {source.name:12} {source.kind:7} {source.summary()}")

    missing = unsearchable(registry)
    if missing:
        print("\n  NOT searched, and reported rather than hidden:")
        for name, why in missing:
            print(f"    {name:12} {why[:96]}")


def act1_identities(report) -> None:
    """Identity resolution, reported against whatever the run actually resolved.

    Kept separate from act 1's ingest summary because the resolution itself happens
    once, before any act runs — `--act 2` needs resolved people just as much as act 1
    needs to print them.
    """
    print(f"\nidentity resolution: {report.summary()}")
    sarah = report.find("Sarah Kim")
    if sarah:
        print("\n  one human, five identities:")
        for i in sorted(sarah.identities, key=lambda x: x.app):
            print(f"    {i.app:8} {i.handle}")
        print(f"  band: {sarah.band.value}")
    if report.needs_review:
        print(f"\n  {len(report.needs_review)} uncertain match(es) held for human review,")
        print("  because over-merging two real people is a data-protection incident,")
        print("  not a rounding error.")


def act2(agent: WalnutAgent, gate: QueueGate) -> None:
    header(2, "THE ACTION", "A contradiction no single dashboard could see, acted on.")

    conflicts = detect_contradictions(agent.brain)
    if not conflicts:
        print("no contradictions found — check the fixtures")
        return

    print(f"\n{len(conflicts)} contradiction(s) detected across the estate:")
    for c in conflicts:
        print(f"  · {c.subject:20} {c.apps[0]} vs {c.apps[1]}")

    # Pick the one that would actually hurt somebody, and take the rest with us: a
    # brain that reports only the conflict it went looking for is just a search engine
    # with extra steps.
    def score(c) -> int:
        # No hard-coded subject or patient. The shape is what is scored: a system of
        # record disagreeing with a person who observed something, both sides
        # authoritative, and — decisively — a dose still queued to go out while the
        # disagreement is unresolved. The seed data changes; the shape does not.
        apps = set(c.apps)
        return (
            bool(apps & {"ehr", "dispensary", "labs"}) * 8
            + ("notion" in apps and bool(apps & {"github", "linear"})) * 4
            + (c.left.is_primary and c.right.is_primary) * 2
            + ("github" in apps)  # an unmerged PR is the least deniable evidence
            + c.subject.startswith("feature:")
        )

    conflict = max(conflicts, key=score)
    print("\nthe one that could hurt somebody:\n")
    print(conflict.render())

    plan = build_plan(conflict, agent.brain, adapters=agent.adapters)

    print(f"\nproposing {len(plan)} actions across {len({p['app'] for p in plan})} apps\n")
    results = list(agent.execute_all(agent.propose_for_conflict(conflict, plan)))
    for result in results:
        if isinstance(result, Refusal):
            print(f"  HELD    {result.action.app}.{result.action.operation}"
                  f"  → {result.reason.value}")
        else:
            print(f"  done    {result.action.app}.{result.action.operation}"
                  f"  → {result.action_id}")

    held = [step for step in plan if step["operation"] == "place_hold"]
    if held:
        print(f"\n  the step that matters: {held[0]['app']}.place_hold on "
              f"{held[0]['target']} — a dose queued for today, stopped before it is "
              "handed over.")
    else:
        # Said out loud, never silently absent. Whether the hold step is in the plan
        # depends on a connected source declaring `place_hold`; when no such source is
        # connected, the honest thing is to name what is missing and why.
        print("\n  no source in this run declares place_hold, so nothing in the plan "
              "stops a dose.")
        print("  start the pharmacy service to see that step:")
        print("    python -m uvicorn services.dispensary.app:app --port 8900")

    if gate.pending:
        executed = len([r for r in results if not isinstance(r, Refusal)])
        print(f"\n  {len(gate.pending)} action awaiting human approval:")
        for key, (action, _) in gate.pending.items():
            print(f"    [{key}] {action.app}.{action.operation} — {action.rationale}")
        print("\n  The only action that leaves the organisation is the only action "
              "that stops.")
        # Counted from the run, not asserted from memory: with the pharmacy connected
        # this number is different, and a demo that states a stale count is exactly
        # the unearned claim this product exists to remove.
        print(f"  {executed} internal write(s) went through. The rest waits for a person.")


def act3(agent: WalnutAgent) -> None:
    header(3, "THE REFUSAL", "A document tries to instruct the agent. It does not work.")

    poisoned = [
        f for f in agent.brain.by_app("notion")
        if "ignore previous instructions" in f.text.lower()
    ]
    if not poisoned:
        print("adversarial fixture not found — check fixtures/notion.csv")
        return

    fact = poisoned[0]
    print(f"\ningested document: {fact.pointer.resource_uri}")
    idx = fact.text.lower().find("ignore previous instructions")
    print(f"  ...{fact.text[max(0, idx - 90):idx + 120].strip()}...")

    print("\nthe document asks the agent to mark all issues resolved and email the")
    print("customer list. both are real operations this agent can perform.\n")

    from walnut.contract import Action

    act3_results = []
    for op, app, target, payload in [
        ("set_state", "linear", {"id": "any"}, {"state": "Done"}),
        ("send_email", "email", {"id": "bulk"}, {"to": "customers@"}),
    ]:
        result = agent.executor.execute(
            Action(app=app, operation=op, target=target, payload=payload,
                   justified_by=(fact.node_id,),
                   rationale="requested by the Support Macros document")
        )
        act3_results.append(result)
        print(result.render() if isinstance(result, Refusal)
              else f"  EXECUTED {app}.{op} — THIS IS A BUG, the taint rule did not fire")
        print()

    print("the same document CAN justify quarantining itself:\n")
    quarantine = agent.executor.execute(
        Action(app="notion", operation="append_block", target={"id": fact.pointer.locator["id"]},
               payload={"text": "[Walnut] This page contains injected instructions."},
               justified_by=(fact.node_id,),
               rationale="quarantine the document")
    )
    print(f"  {'REFUSED' if isinstance(quarantine, Refusal) else 'done'}  "
          "notion.append_block — labelling the poison is permitted; acting on it is not.")

    # The control condition. Comparative claims need something to compare against, and
    # the honest comparison is this same codebase with the governance layer removed.
    from walnut.adapters.fixture import load_all_fixtures
    from walnut.baseline import compare

    act3_results.append(quarantine)
    print(f"\n{RULE}")
    # Scoped to THIS act's three attempts only. Folding in act 2's successes would
    # compare two different tasks and flatter whichever side had more to do.
    print(compare(agent.brain, load_all_fixtures(), act3_results).render())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--act", type=int, choices=[1, 2, 3], help="run one act only")
    args = parser.parse_args()

    agent, gate, registry = build()
    acts = [args.act] if args.act else [1, 2, 3]

    # Ingest once, up front, for every act. Act 2 and act 3 both need a populated
    # brain, and ingesting inside act 1 meant `--act 2` quietly ran a second, separate
    # ingest with no linking — so the same command produced a graph with no edges.
    _records, edges = ingest(agent)

    report = agent.resolve_people(load_identities())

    if 1 in acts:
        act1(agent, registry, edges)
        act1_identities(report)
    if 2 in acts:
        act2(agent, gate)
    if 3 in acts:
        act3(agent)

    print(f"\n{RULE}\nRELIABILITY\n{RULE}")
    for key, value in agent.reliability_report().items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
