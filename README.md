# Walnut

**A company brain that reads five fragmented systems, and an agent that acts across
them only on cited evidence — and refuses when it can't.**

Built for the Multi-App AI Agent Hackathon (Lemma × Comma Capital).

> *"Build one useful, multi-step AI agent. Connect it to at least three external apps.
> Show how you know it works."*

---

## The problem

Every company's truth is smeared across Slack, Linear, GitHub, Notion and email. No
human reads all five at once, so they drift apart. Then an agent is pointed at them,
and it does the thing agents do when they cannot tell what is true: it acts
confidently, produces the wrong result, throws no error, and reports success.

Walnut is built around the opposite assumption — that the interesting question is not
*can the agent act*, but *can the agent tell when it must not*.

## Three guarantees, enforced by constructors rather than prompts

```python
>>> SourcePointer(app="slack", resource_uri="https://…", content_hash="")
ValueError: content_hash must be a sha256 hex digest, got ''.
           Empty or placeholder hashes make citation verification silently vacuous.

>>> Action(app="linear", operation="create_issue", justified_by=())
ValueError: Action linear.create_issue has no justifying evidence. Every action must
           cite the evidence that motivated it — this is the rule the whole system
           exists to enforce.
```

1. **No claim without a resolvable source.** Evidence cannot be constructed without a
   pointer carrying a real content hash. Drift is detectable because the hash moves.
2. **No action without justifying evidence.** An unjustified action is not rejected at
   runtime; it cannot be built.
3. **No deletion.** Every write is reversible and undo *retracts* rather than erases —
   a Slack message is edited to `[retracted by Walnut …]`, a Linear issue is archived,
   a Notion property is restored with the audit note left in place.

## The demo

```bash
python demo.py             # all three acts
python demo.py --act 3     # just the refusal
```

Runs entirely offline against fixture data. No API tokens required.

**Act 1 — the brain.** 88 records from five apps become one graph. Sarah Kim resolves
across five identities (`@sarah`, `sarah-k`, `Sarah Kim`, `S. Kim`,
`sarah.kim@meridian.dev`). Ten uncertain matches are held for a human rather than
guessed, because over-merging two real people is a data-protection incident.

**Act 2 — the action.** The brain finds that the Notion spec claiming
`Status: Shipped` is contradicted by GitHub PR #288, still open with zero approvals.
It files a Linear issue carrying the evidence chain, comments on the PR, replies in
Slack, corrects the Notion status — and **stops** at the customer email.

```
done    linear.create_issue   → linear-1
done    github.comment        → github-1
done    slack.post_reply      → slack-1
done    notion.set_property   → notion-1
HELD    email.send_email      → gate_timeout

  The only action that reaches a customer is the only action that stops.
```

**Act 3 — the refusal.** A Notion page carrying injected instructions asks the agent to
mark all issues resolved and email the customer list. Both are operations it can
perform. It refuses both, and shows why:

```
REFUSED: email.send_email
  reason: tainted_instruction
  Content read from a connected app is evidence about the world, never a command
  addressed to this agent. Quarantining or labelling it is permitted; acting on it
  is not.
  taint path:
    1. notion · https://notion.so/meridian/nt002 · matched 'ignore previous instruction'
```

The same document *can* justify quarantining itself. You may label the poison; you may
not act on it.

## Architecture

```
Slack   Linear   GitHub   Notion   Email          five apps, read AND write
  └───────┴────────┴────────┴────────┘
         Adapter contract — six methods
   probe · fetch · resolve │ capabilities · act · undo
                    │
              Brain facade  ← swap seam
                    │
      Semantica: graph, PROV-O provenance, decision chains,
                 temporal state_at(), retraction tombstones
                    │
   ActionExecutor — the ONE choke point for every write
   unknown? forbidden? justified? tainted? stale? gated?
                    │
           Lemma tracing throughout
```

**Why the checks are ordered that way.** Taint is checked *before* the human gate. If
injected content could reach an approval prompt, the attack becomes social engineering
with extra steps — a plausible request rubber-stamped by a tired reviewer at 3am. The
agent refuses rather than delegating the decision.

**The agent loop is model-optional.** Ingestion, identity resolution, contradiction
detection and action proposal are all deterministic Python. A language model can phrase
the customer reply, but is never in the path of a decision about what is true or what
may be written. The run is reproducible.

## Reliability

```bash
pytest -q          # 135 passed
```

- **Every adapter passes one behavioural conformance suite** (`walnut/conformance.py`).
  A `Protocol` constrains signatures only; a structurally valid adapter can still
  return uncited evidence or mislabel a customer-facing write as internal. The suite is
  what "the adapter is done" actually means — and it caught a real duplicate-evidence
  bug in the Slack adapter during the build.
- **Tests run with no network and no credentials**, so they are also the regression
  suite during the hackathon window.
- **`FixtureAdapter` is held to the same suite as the live adapters**, which makes it a
  diagnostic instrument as well as an offline fallback: if it passes and a live adapter
  does not, the bug is in the live adapter.

### Bugs the build actually found

Recorded because "show how you know it works" should include how you know it *didn't*:

| Found by | Bug |
|---|---|
| Conformance suite | Slack adapter double-counted thread replies |
| First live demo run | Semantica silently swallowed every recorded decision — its `metadata` is splatted as kwargs and `recorded_at` collided with its own parameter, raising inside a bare `except` |
| First live demo run | Contradiction detector reported **303** conflicts on an 88-record corpus: unversioned feature words matched as shared referents, and the cartesian product was emitted undeduplicated. Now 18, deduplicated per subject and app-pair |
| Demo output review | Dedup kept the *newest* claim per pair rather than the most *authoritative*, so an all-hands agenda outranked the spec page. Primacy now beats recency |
| Demo output review | `regression` matched "regression pass", reading a completed QA issue as broken |

## Layout

```
walnut/
  contract.py        six-method adapter contract; the two enforced rules
  conformance.py     the behavioural suite every adapter must pass
  brain.py           Semantica facade — the swap seam
  identity.py        cross-app identity resolution, three match bands
  contradiction.py   deterministic conflict detection
  render.py          the grounding wall
  agent.py           ingest → investigate → propose → execute
  observability.py   Lemma tracing, no-ops without credentials
  actions/
    governance.py    refusal types, taint detection, human gates
    executor.py      the one write choke point
  adapters/          slack · linear · github · notion · email · fixture
fixtures/            the Meridian company as CSV
demo.py              the three acts
```

## Setup

```bash
uv venv && uv pip install -e ".[dev]"
python demo.py
```

Live connectors read credentials from `.env` (see `.env.example`). Absent credentials,
everything falls back to fixtures and the full demo still runs.
