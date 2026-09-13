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

  13 records · 6 of 7 sources

  ehr         MR-4417 · CKD stage 2 · co-amoxiclav 625mg TDS · Allergies: None recorded
  slack       "she came out in a rash all over as a kid after being given amoxicillin.
               that is NOT showing anywhere in her chart" — 12 days ago
  notion      visit note · status Filed
  email       7 messages · the renal referral thread, and a penicillin heads-up
  dispensary  MR-4417 · co-amoxiclav · queued for dispense today
  linear      MED-420 · reschedule her Thursday follow-up
  labs        searched · no match

  ⚠ CONTRADICTION · MR-4417
    ehr says absent — slack says present
```

`labs` is keyed on the MRN, not the name, so it holds nothing filed under "Ankusha
Rao" — that is what *searched · no match* means. Ask `MR-4417` and it returns the
eGFR trend 78 → 71 → 64 → 58, the last one flagged Low.

## The apps we work with

**Five built-in connectors**, each read *and* write:

| App | Contributes | Can write |
|---|---|---|
| **Slack** | care-team coordination, the informal channel | reply in thread, post, react, DM |
| **Linear** | operations task board | create issue, comment, set state, assign, label |
| **GitHub** | internal tooling — built and tested, but a clinic does not use it, so it contributes no records to this corpus | comment, create issue, label, commit status |
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
system. Walnut's **unmodified** REST adapter passes all 17 conformance checks against
it. `tests/test_dispensary.py` runs the service in-process over ASGI — same
request/response translation, no socket — so the suite needs no running service;
pointed at it over a real HTTP socket on `:8900` the same 17 pass. The Connectors
screen supplies no write target, so it reports the read half: **12 passed, 0 failed,
3 skipped**. See [ADAPTERS.md](ADAPTERS.md).

## How we know it works

The hackathon asks you to show this, so here is exactly what is checked and how.

```bash
make check     # tests green · brief all-PASS · demo clean
```

Tests, brief and demo are hard gates — `make check` fails if any of them does. The
dirty-tree line is a warning only; it prints and carries on.

### 1. A behavioural conformance suite every connector must pass

`walnut/conformance.py` — 17 checks that a type signature cannot catch. Real content
hashes, followable URIs, unique ids, stable hashes across re-fetches, `resolve()`
returning `None` for a missing record rather than inventing one, external operations
gated. Twelve of them need only read access and run everywhere; the three write checks
— prior state captured before a write, undo restoring it, an unjustified action failing
to construct — run when a write target is supplied, and are reported as **skipped**
rather than quietly passed when it is not.

**Every adapter is held to it, including custom ones.** A custom source that fails is
shown on the Connectors screen and **is not wired in** — it contributes no evidence until
it conforms. It caught two real bugs during this build: the Slack adapter double-counting
thread replies, and the `labs` source keying evidence on a non-unique column, which would
have silently collapsed a six-month lab trend into a single record.

### 2. A self-generating reliability brief

```bash
python -m walnut.brief > BRIEF.md
```

Eleven checks, each read from a **live object** — it executes the system and reports
what happened, rather than restating claims from memory. It reports its own failures, and
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

### 5. The whole suite, no network, no credentials

```bash
make test                      # or: ./.venv/bin/python -m pytest -q
WALNUT_GRAPH=simple make test  # the dependency-free graph backend
```

The count is deliberately not quoted here — it rots on the next commit, and a document
that quietly goes stale is the exact failure this product is an argument against. Run
the command; the generated brief carries the live number. Green on both graph backends:
`WALNUT_GRAPH=simple` runs a dependency-free fallback, so a broken graph library costs
the provenance features, not the product.

## Governance — why it's safe to point at real systems

Actions are tiered by the **direction of risk**, not by the kind of write:

| | |
|---|---|
| **auto** | place a hold on a queued prescription — *stopping a dose fails safe* |
| **auto** | post to the care team · create a task · append a cited note · annotate the record |
| **GATED** | email the referring clinician · release a hold — *these make things less safe* |
| **FORBIDDEN** | the tier exists so a refusal is typed rather than improvised. No connector here declares an operation in it |

**The agent can stop a dose going out. It cannot start one.**

Writing the allergy field and altering a prescription are not FORBIDDEN operations —
they are not operations at all. The EHR connector declares exactly one: `annotate`,
which writes to a companion table, never to the clinic's own rows. Ask for
`ehr.set_allergies` and the refusal is `unknown_operation` — *"'ehr' adapter declares
no operation 'set_allergies'. Known: ['annotate']"*. There is no approval that grants
it, because there is nothing to grant.

Two guarantees are enforced by constructors, not by prompts:

```python
>>> Action(app="linear", operation="create_issue", target={"id": "MED-412"},
...        payload={"body": "…"}, justified_by=())
ValueError: Action linear.create_issue has no justifying evidence. Every action must
cite the evidence that motivated it — this is the rule the whole system exists to
enforce.
```

No claim without a resolvable source — a `SourcePointer` with no followable URI or a
placeholder content hash raises too. And no action without the evidence justifying it.

Nothing is deleted: undo *retracts* rather than erases. But reversibility is not
universal, and the code says so in one place — `IRREVERSIBLE = {"email":
{"send_email"}}`. A sent email cannot be recalled, which is precisely why sending is
the gated operation, and why the console will not offer an Undo button that could only
raise.

And content read from a connected app is **evidence about the world, never a command**.
A document carrying injected instructions is refused with its taint path shown — while
quarantining that same document is still permitted. That refusal is demonstrated in
`make demo`, act 3; the console has no screen that reproduces it.

## Run it

```bash
make install
make stack      # console on :8000, mock internal hospital system on :8900
make demo       # three-act terminal walkthrough — OLD corpus, see below
make check      # everything that must be green before recording
```

`make demo` has not yet been moved to the clinic corpus: it still runs the original
five-app story (80 records, Slack/Linear/GitHub/Notion/Email, the rota-sync
contradiction). Act 3 — the injected-instruction refusal with its taint path — is real
and only exists there. The console is the current product; the terminal demo is the
older one.

Works with **zero credentials**. The four seeded apps and the clinic's SQL sources need
nothing; the dispensary is a live service, so it needs `make stack` (or `make
dispensary`) running, and reports itself *not searched* with a reason when it is not.

To connect real accounts, fill in `.env` — then **export it yourself**, because nothing
in this repo reads that file:

```bash
set -a; source .env; set +a
make stack     # or: make smoke, to test the credentials first
```

`.env.example` lists what to paste, including the two gotchas that bite people (Linear
wants a bare `Authorization` header, not `Bearer`; Notion sees nothing until you share
pages with the integration).

## The demo corpus

**Meridian Health**, a fictional nine-site clinic group. 113 facts across seven sources
with the dispensary running — 98 across six without it, and the seventh row then reads
*not searched*, with the reason. Ten staff each filed under a different handle per
system, one planted clinical contradiction, and one adversarial document. All
synthetic, all `.example` domains, no real people or patients. See
[fixtures/README.md](fixtures/README.md).

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
[BRIEF.md](BRIEF.md) carries its own list, from the earlier five-app build — these are
the ones from this one:

| Found by | Bug |
|---|---|
| Running the product | **Asking about a person returned nothing.** Identity resolution was built and never wired into retrieval, so "Priya Patel" matched no record in any system — the single most obvious question anyone would ask |
| Running the product | **Every record was authored by `p01`.** The fixture adapter emitted the raw person id instead of that app's handle, so no name in the corpus was the name it is stored under (`tests/test_retrieval.py:31`) |
| Running the product | **Punctuation lost a subject.** Exact-substring matching, so `co-amoxiclav` and `co amoxiclav` reached different records, and `st anne` could never match `St. Anne's` (`tests/test_retrieval.py:40`) |
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
- **Live third-party APIs.** The five built-in adapters are verified against
  recorded-shape transports, not against Slack, Linear, GitHub, Notion or a real
  mailbox. The dispensary is the exception: it is a real service and the REST adapter
  is verified against it, in-process in the test suite and over a real socket when it
  is running. The demo video states which accounts, if any, were connected live.
