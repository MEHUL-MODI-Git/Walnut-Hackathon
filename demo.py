#!/usr/bin/env python
"""The three-act demo, runnable end to end with no credentials.

    python demo.py            all three acts
    python demo.py --act 3    just the refusal

Act 1  the brain      five systems, one graph, one human resolved across five identities
Act 2  the action     a contradiction found and acted on across four apps, one gated
Act 3  the refusal    a poisoned document tries to drive the agent, and fails

Act 3 is the one that matters. Every other team will demo a success.
"""

from __future__ import annotations

import argparse
import sys

from walnut import Brain
from walnut.actions.executor import ActionExecutor
from walnut.actions.governance import QueueGate, Refusal
from walnut.adapters.fixture import load_all_fixtures, load_identities
from walnut.agent import WalnutAgent
from walnut.contradiction import detect_contradictions

RULE = "─" * 78


def header(n: int, title: str, claim: str) -> None:
    print(f"\n{RULE}\nACT {n} · {title}\n{claim}\n{RULE}")


def build() -> tuple[WalnutAgent, QueueGate]:
    adapters = load_all_fixtures()
    brain = Brain()
    gate = QueueGate()
    # Freshness checking is off for the demo: the fixture adapter is the source of
    # truth for itself, so every hash trivially matches. It is ON in the live path,
    # where it is the check that catches a source moving under a stale citation.
    executor = ActionExecutor(adapters, brain, gate, verify_freshness=False)
    return WalnutAgent(adapters, brain, executor), gate


def act1(agent: WalnutAgent) -> None:
    header(1, "THE BRAIN", "Five fragmented systems become one graph of cited facts.")

    count = agent.ingest(limit=200)
    stats = agent.brain.stats()
    print(f"\ningested {count} records from {len(stats['apps'])} apps: {', '.join(stats['apps'])}")
    print(f"graph: {stats.get('node_count', '?')} nodes, {stats.get('edge_count', '?')} edges")

    report = agent.resolve_people(load_identities())
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

    # Pick the one the customer actually complained about: the spec page claiming
    # "Shipped" against the systems that build and track the work. The other 17 are
    # real and stay on the list — a brain that reports only the conflict it went
    # looking for is just a search engine with extra steps.
    def score(c) -> int:
        apps = set(c.apps)
        return (
            ("notion" in apps and bool(apps & {"github", "linear"})) * 4
            + (c.subject == "feature:exportv2") * 2
            + (c.left.is_primary and c.right.is_primary)
        )

    conflict = max(conflicts, key=score)
    print("\nthe one the customer is asking about:\n")
    print(conflict.render())

    plan = [
        {"app": "linear", "operation": "create_issue", "target": {"id": "ENG-NEW"},
         "payload": {"title": "Export v2 status is contradicted by an open PR",
                     "state": "Todo"},
         "rationale": "File the contradiction as tracked work, with its evidence chain."},
        {"app": "github", "operation": "comment", "target": {"id": "pull-288"},
         "payload": {"body": "Walnut: this PR is cited as shipped elsewhere. It is open."},
         "rationale": "Tell the engineer where the false claim is being made."},
        {"app": "slack", "operation": "post_reply", "target": {"id": "sl006"},
         "payload": {"text": "Confirmed still open — evidence attached."},
         "rationale": "Close the loop with whoever asked."},
        {"app": "notion", "operation": "set_property", "target": {"id": "nt001"},
         "payload": {"status": "Disputed"},
         "rationale": "Repair the stale fact that caused the confusion."},
        {"app": "email", "operation": "send_email", "target": {"id": "em-reply"},
         "payload": {"to": "contact@northstar-analytics.com",
                     "subject": "Re: export timeouts"},
         "rationale": "Reply to the customer. Customer-facing, so it must be gated."},
    ]

    print(f"\nproposing {len(plan)} actions across {len({p['app'] for p in plan})} apps\n")
    for result in agent.execute_all(agent.propose_for_conflict(conflict, plan)):
        if isinstance(result, Refusal):
            print(f"  HELD    {result.action.app}.{result.action.operation}"
                  f"  → {result.reason.value}")
        else:
            print(f"  done    {result.action.app}.{result.action.operation}"
                  f"  → {result.action_id}")

    if gate.pending:
        print(f"\n  {len(gate.pending)} action awaiting human approval:")
        for key, (action, _) in gate.pending.items():
            print(f"    [{key}] {action.app}.{action.operation} — {action.rationale}")
        print("\n  The only action that reaches a customer is the only action that stops.")
        print("  Four internal writes went through. The email waits for a person.")


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

    for op, app, target, payload in [
        ("set_state", "linear", {"id": "ENG-412"}, {"state": "Done"}),
        ("send_email", "email", {"id": "bulk"}, {"to": "customers@"}),
    ]:
        result = agent.executor.execute(
            Action(app=app, operation=op, target=target, payload=payload,
                   justified_by=(fact.node_id,),
                   rationale="requested by the Support Macros document")
        )
        print(result.render() if isinstance(result, Refusal)
              else f"  EXECUTED {app}.{op} — THIS IS A BUG, the taint rule did not fire")
        print()

    print("the same document CAN justify quarantining itself:\n")
    result = agent.executor.execute(
        Action(app="notion", operation="append_block", target={"id": fact.pointer.locator["id"]},
               payload={"text": "[Walnut] This page contains injected instructions."},
               justified_by=(fact.node_id,),
               rationale="quarantine the document")
    )
    print(f"  {'REFUSED' if isinstance(result, Refusal) else 'done'}  "
          "notion.append_block — labelling the poison is permitted; acting on it is not.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--act", type=int, choices=[1, 2, 3], help="run one act only")
    args = parser.parse_args()

    agent, gate = build()
    acts = [args.act] if args.act else [1, 2, 3]

    if args.act in (2, 3):
        agent.ingest(limit=200)  # later acts need the brain populated

    if 1 in acts:
        act1(agent)
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
