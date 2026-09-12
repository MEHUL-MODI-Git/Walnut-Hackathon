# Walnut — Build Plan

**Multi-App AI Agent Hackathon** · Lemma × Comma Capital · judged by the founders of
Arga Labs and Userlens.

> *"Build one useful, multi-step AI agent. Connect it to at least three external apps.
> Show how you know it works."*

| | |
|---|---|
| **Window** | Mon 14 Sep, **00:00–07:00 SGT** (Sun 13 Sep, 09:00–16:00 PT) |
| **Code freeze** | **05:30 SGT.** Non-negotiable. |
| **Judging** | technical execution 30 · **reliability & evaluation 25** · usefulness 20 · originality 15 · demo clarity 10 |
| **Apps** | Slack · Linear · GitHub · Notion · Email (IMAP/SMTP) — five, three required |

## The product in one line

A company brain that reads five fragmented systems, answers only with cited evidence,
takes governed action across all five — and **refuses** when the evidence does not hold.

## The three-act demo

1. **Brain** — ingest 5 apps, resolve one human across 5 identities, answer with citations.
2. **Action** — customer escalation → contradiction found → Linear issue + GitHub PR
   comment + Slack reply + Notion status fix + a **gated** customer email.
3. **Refusal** — a poisoned Notion page tries to drive bulk destructive action. The agent
   refuses with a typed reason and shows the taint path. Then the same run with the
   evidence layer off silently does the wrong thing.

Act 3 is what wins. Every other team will demo a success.

---

## Phases

Phases 1–6 need **no credentials** and can be built ahead of the window. Phases 7–9 are
the window itself.

### Phase 1 — Contract and brain ✅ DONE
The 6-method adapter contract (read×3 / write×3) and the Semantica-backed brain facade.
Two rules enforced by constructors rather than convention: no evidence without a
resolvable pointer and a real content hash; **no action without justifying evidence**.
*16 tests green, no network, no credentials.*

### Phase 2 — Governance core (no creds)
The part that earns the 25%.
- `ActionTier` ladder: TRIVIAL / INTERNAL / **GATED** / FORBIDDEN
- `ActionExecutor` — routes by tier, blocks GATED on human approval
- Undo ledger — every write reversible; **retract, never erase**
- Typed refusals with reason codes
- Taint tracking: instruction-from-human vs instruction-found-in-data

### Phase 3 — Conformance suite (no creds)
One behavioural suite every adapter must pass. A Protocol constrains signatures only;
a structurally-conforming adapter can still violate every rule. This is what stops five
parallel adapter implementations from drifting.

### Phase 4 — Fixtures (no creds)
The Meridian company as CSV: 5 apps, ~40 people, one planted contradiction, one
cross-app identity chain, one adversarial page. Seed input, not agent input.

### Phase 5 — Adapters ×5 (no creds to write, creds to verify)
Slack · Linear · GitHub · Notion · Email. Written against documented APIs, verified
against the conformance suite with recorded fixtures. Live smoke test on Sunday.

### Phase 6 — Intelligence + observability (no creds)
- Cross-app identity resolution
- Contradiction detection
- Cited renderer — **no value renders without a citation**
- Lemma tracing throughout

### Phase 7 — Seed and smoke test (Sunday, needs creds)
Push fixtures into the five real apps. One "hello world" write per app to prove every
token actually has the scope it claims. **This is the step that most often fails at 03:00
if skipped.**

### Phase 8 — The window (00:00–05:30 SGT)
Agent loop, the three-act scenario, human-gate console, the refusal path.

### Phase 9 — Ship (05:30–07:00 SGT)
Freeze. Record the 2-minute demo. Write the reliability brief. Submit.

---

## Parallelisation

| Work | Who | Why |
|---|---|---|
| Governance core, agent loop, refusal logic | main (strong) | judgement; this is the product's whole argument |
| Fixture authoring | sonnet | mechanical, well-specified |
| Adapters ×5 | sonnet ×5, parallel | five independent files against one fixed contract |
| Observability wrapper | sonnet | mechanical, documented API |
| Review of every returned file | main (strong) | nothing merges unreviewed |

## Standing rules

1. **No credentials in the repo.** `.env` is gitignored; `.env.example` carries the shape.
2. **Nothing merges unreviewed**, regardless of which model wrote it.
3. **Tests run with no network and no credentials.** If a test needs a live API it is a
   smoke test, not a unit test, and it lives behind a marker.
4. **Every adapter passes the conformance suite** before it is considered done.
5. **Cut order when behind:** Notion drops first (four apps still clears the bar), then
   undo, then the commit-status flourish. **The refusal demo is never cut** — it is the
   submission's entire differentiator.
