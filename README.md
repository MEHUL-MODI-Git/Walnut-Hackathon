# Walnut

**A company brain. Every system a company runs, in one place — so nothing is ever lost,
no matter how scattered it was. Then an agent that can act on it.**

Multi-App AI Agent Hackathon · Lemma × Comma Capital

> 🎥 **Demo video:** _paste link here before submitting_
> 💻 **Repository:** this repo. `make stack` runs the whole thing.

---

## The problem

Every company holds an enormous amount of useful information and can't get at any of it.
It's spread across a dozen tools that don't talk to each other, so answering even a
simple question means opening five tabs, and real analysis mostly just doesn't happen.

Worse, the most important fact is often in the one place nobody thinks to look. A nurse
messages a colleague that a patient reacted badly to a drug as a child. It never reaches
the chart. The chart still says *"Allergies: none recorded."* The pharmacy still has the
antibiotic queued.

Nothing was hidden. Everything was written down. It was just written down **somewhere
else**.

## What Walnut does

Connects all of it into one graph of cited facts, so you can ask about any person,
patient, customer or record and get **everything, from everywhere, with its source
attached**. Then it acts back into those systems — under governance, because it's acting
on a company's real data.

```
> Ankusha Rao

  12 records · 5 of 8 sources · resolved through 4 identities

  ehr      MR-4417 · CKD stage 2 · co-amoxiclav 625mg TDS · Allergies: None recorded
  slack    "she came out in a rash all over as a kid after being given amoxicillin.
            that is NOT showing anywhere in her chart" — 12 days ago
  notion   visit note, filed · referral protocol
  email    specialist referral thread
  labs     eGFR 78 → 71 → 64 → 58  (latest flagged Low)
  github   searched · no match

  ⚠ CONTRADICTION · MR-4417
    ehr says absent — slack says present
```

## The apps we work with

**Five built-in connectors**, each read *and* write:

| App | Contributes | Can write |
|---|---|---|
| **Slack** | care-team coordination, the informal channel | reply in thread, post, react, DM |
| **Linear** | operations task board | create issue, comment, set state, assign, label |
| **GitHub** | internal tooling | comment, create issue, label, commit status |
| **Notion** | protocols, visit notes, the written record | set property, append block, create page |
| **Email** | correspondence that crosses the org boundary | draft, flag, move, **send (gated)** |

**Plus the customer's own systems** — because the sources that matter most to a company
never ship as a first-party integration. Three ways in, no forked code:

| Route | Example in this repo |
|---|---|
| **SQL** — point at a database with a query | `ehr` and `labs` — the clinic's record system |
| **REST** — describe an internal API's shape | `dispensary` — a live service in `services/dispensary/` |
| **Python** — one file implementing six methods | `walnut_plugins/example_csv_source.py` |

`services/dispensary/` is a real FastAPI service standing in for a customer's internal
system. Walnut's **unmodified** REST adapter passes full conformance against it over
real HTTP. See [ADAPTERS.md](ADAPTERS.md).

## How we know it works

The hackathon asks you to show this, so here is exactly what is checked and how.

```bash
make check     # tests green · brief all-PASS · demo clean · working tree clean
```

### 1. A behavioural conformance suite every connector must pass

`walnut/conformance.py` — 12 checks that a type signature cannot catch. Real content
hashes, followable URIs, unique ids, stable hashes across re-fetches, `resolve()`
returning `None` for a missing record rather than inventing one, prior state captured
before a write, external operations gated.

**Every adapter is held to it, including custom ones.** A custom source that fails is
shown on the Connectors screen and **is not wired in** — it contributes no evidence until
it conforms. It caught two real bugs during this build: the Slack adapter double-counting
thread replies, and the `labs` source keying evidence on a non-unique column, which would
have silently collapsed a six-month lab trend into a single record.

### 2. A self-generating reliability brief

```bash
python -m walnut.brief > BRIEF.md
```

Ten checks, each read from a **live object** — it executes the system and reports what
happened, rather than restating claims from memory. It reports its own failures, and
`make check` refuses to pass if any check fails, so you cannot record a demo against a
broken build.

### 3. Coverage — the part that makes "nothing is lost" checkable

A search that quietly read four of six sources looks identical to one that read all six.
So every result carries **every source**, in one of three states:

| | |
|---|---|
| **found** | it had something |
| **nothing** | *searched · no match* — a quiet neutral, **never red, never hidden**. A searched-and-empty source is the promise working |
| **not searched** | the only failure state — with the reason and a link to fix it |

A zero-hit query doesn't say "0 results". It shows the full table and says *"Every source
was searched. None of them hold anything about X."* A negative that proves the guarantee.

### 4. A control condition

`walnut/baseline.py` is this same codebase with the governance layer removed — same
adapters, same corpus, same task. It executes far more actions, including ones taken
verbatim from a document carrying injected instructions, and reports every one as a
success. A test asserts the control is *not* secretly governed, because a flattering
control is worse than none.

### 5. 328 tests, no network, no credentials

Green on both graph backends — `WALNUT_GRAPH=simple` runs a dependency-free fallback, so
a broken graph library costs the provenance features, not the product.

## Governance — why it's safe to point at real systems

Actions are tiered by the **direction of risk**, not by the kind of write:

| | |
|---|---|
| **auto** | place a hold on a queued prescription — *stopping a dose fails safe* |
| **auto** | post to the care team · create a task · append a cited note · annotate the record |
| **GATED** | email the referring clinician · release a hold — *these make things less safe* |
| **FORBIDDEN** | write the allergy field · alter a prescription — never, under any approval |

**The agent can stop a dose going out. It cannot start one.**

Three guarantees are enforced by constructors, not by prompts:

```python
>>> Action(app="linear", operation="create_issue", justified_by=())
ValueError: Action linear.create_issue has no justifying evidence. Every action must
           cite the evidence that motivated it.
```

No claim without a resolvable source · no action without the evidence justifying it ·
no deletion — every action is reversible, and undo *retracts* rather than erases.

And content read from a connected app is **evidence about the world, never a command**.
A document carrying injected instructions is refused with its taint path shown — while
quarantining that same document is still permitted.

## Run it

```bash
make install
make stack      # console on :8000, mock internal hospital system on :8900
make demo       # the three-act walkthrough in the terminal
make check      # everything that must be green before recording
```

Works with **zero credentials** — every source runs on seeded data. `.env.example` lists
what to paste to connect real accounts, including the two gotchas that bite people
(Linear wants a bare `Authorization` header, not `Bearer`; Notion sees nothing until you
share pages with the integration).

## The demo corpus

**Meridian Health**, a fictional nine-site clinic group. ~113 facts across seven sources,
ten staff each filed under a different handle per system, one planted clinical
contradiction, and one adversarial document. All synthetic, all `.example` domains, no
real people or patients. See [fixtures/README.md](fixtures/README.md).

## Layout

```
walnut/
  retrieval.py       assemble everything about a subject + source coverage
  brain.py           the graph of cited facts (swappable backend)
  identity.py        cross-app identity resolution, three match bands
  contradiction.py   deterministic conflict detection
  intent.py          natural language → proposed actions, or a question
  conformance.py     the suite every connector must pass
  actions/           tiers, human gates, typed refusals, the one write choke point
  adapters/          slack · linear · github · notion · email · sql · rest · fixture
  web/               the console: design system + one module per screen
services/dispensary/ a mock internal hospital system, for the custom-adapter path
fixtures/            Meridian Health
```

## Bugs this build actually found

Recorded because "show how you know it works" should include how you learned it didn't.
Full list in [BRIEF.md](BRIEF.md); the ones worth naming:

| Found by | Bug |
|---|---|
| Running the product | **Asking about a person returned nothing.** Identity resolution was built and never wired into retrieval, so 71 of 88 facts rendered an author of `p01` |
| Running the product | **A customer appearing 15 times returned 0 records** — exact-substring matching, so `st anne` never matched `St. Anne's` |
| Running the product | The graph had **zero edges**. Facts were ingested and never linked — a bag of records, not a graph |
| Re-reading the code | **One approval became standing authority.** A fix for the gate stored the answer permanently, so the single write that reaches a customer could re-execute forever |
| Adversarial review | The taint rule exempted tier TRIVIAL — which included setting an arbitrary IMAP flag, so a poisoned document could mark a real message `\Deleted` |
| Conformance suite | `labs` keyed evidence on a non-unique column, collapsing a six-month trend into one record |

## What this does not prove

Stated because a reliability section that only lists successes is not one.

- **Taint detection is a tripwire, not a perimeter.** Pattern matching over adversarial
  text is false-negative-prone. It's safe to rely on only because it isn't what keeps the
  system safe — ingested content can never justify a state-changing action regardless of
  what it says, so a missed pattern costs the explanation, not the outcome.
- **Contradiction detection is lexical.** It finds status and absence/presence conflicts
  on shared specific referents. No semantic entailment.
- **Identity resolution is deterministic, not calibrated.** The uncertain band goes to a
  human rather than to a threshold.
- **Live third-party APIs**: adapters are verified against recorded-shape transports and,
  for the dispensary, a real running service. The demo video states which accounts were
  connected live.
