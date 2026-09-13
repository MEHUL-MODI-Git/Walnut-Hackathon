# Walnut

**One place that holds everything a company knows, so nothing gets lost — and an agent
that can act on it across every app.**

Built for the Multi-App AI Agent Hackathon · Lemma × Comma Capital

| | |
|---|---|
| 🎥 **Demo video (2:18)** | **https://youtu.be/YiiNclHpecY** |
| 💻 **Run it** | `make install && make stack` — no credentials needed |
| ✅ **Tests** | 395, passing on two graph backends |

---

## 1. Project overview

### The problem

A company's information is split across a dozen tools that don't talk to each other.
Answering a simple question means opening five tabs, so most of the time nobody bothers.

The real damage is worse than wasted time. **The fact that matters most is usually in the
one place nobody thinks to look.**

Here is the case this project is built around:

- A nurse sends a message to her team: a patient told her that as a child she came out
  in a rash after taking amoxicillin.
- That message never reaches the patient's chart.
- The chart still says **"Allergies: none recorded."**
- The pharmacy still has **co-amoxiclav** — which contains amoxicillin — queued to hand
  over today.

Nothing was hidden. Nobody did anything wrong. Everything was written down. It was just
written down **somewhere else**, and no single person reads all four systems at once.

This shape is not special to hospitals. A customer told support about a bug that never
reached the issue tracker. A contract clause agreed over email that never reached the
signed document. Same problem, different industry.

### The solution

Walnut connects every system a company uses and builds one searchable memory out of
them. You ask about a person, a patient, a customer or a record — and you get back
everything, from everywhere, with the source attached to each line.

Then it acts back into those systems, with rules about what it may do alone and what
needs a person.

Ask about the patient above, and you get one page:

```
> Ankusha Rao

  13 records · 6 of 7 sources searched had something

  ⚠ CONFLICTING INFORMATION · MR-4417
      ehr says     Allergies: None recorded
      slack says   "she came out in a rash all over as a kid after being
                    given amoxicillin. that is NOT showing anywhere in
                    her chart"   — 12 days ago

  On record
    Conditions     Chronic kidney disease stage 2; hypertension
    Medications    co-amoxiclav 625mg TDS
    Allergies      None recorded                                  ⚠

  Where this came from
    ehr         found   1     dispensary  found   1
    slack       found   2     linear      found   1
    notion      found   1     email       found   7
    labs        nothing 0     searched · no match
```

Three things are happening here that are easy to miss:

**1. It found the message nobody linked to the chart.** The nurse's message and the
hospital database share no common field except the patient's MRN buried in the text.
Walnut links them anyway, and flags that the two disagree.

**2. It does not decide who is right.** It shows both sides with their sources and leaves
the judgement to a clinician. Ranking by "most recent" or "most official" would be a
guess, and a confident guess about a drug allergy is exactly the wrong thing to build.

**3. It says what it searched and found nothing in.** `labs` is keyed on the MRN, not the
name, so it genuinely holds nothing filed under "Ankusha Rao". That is reported as a
result, not hidden. A search that quietly skipped four systems must never look the same
as one that read them all.

Then it acts. From that one contradiction it:

- opens a task in **Linear** to reconcile the record,
- replies in the **Slack** care-team thread,
- flags the visit note in **Notion**,
- **places a hold on the queued prescription** in the hospital's own pharmacy system,
- and drafts an **email** to the referring clinician — which it does *not* send, because
  that leaves the organisation and needs a person to approve it.

Every one of those actions carries the two records that caused it. An action that cannot
cite its evidence cannot be built at all — the code refuses to construct it.

---

## 2. External apps used

### Five built-in connectors

Each one can read *and* write.

| App | What it holds in this demo | What the agent can write |
|---|---|---|
| **Slack** | team coordination — the informal channel where things get said first | reply in thread, post, react, DM |
| **Linear** | the operations task board | create issue, comment, set state, assign, label |
| **Notion** | protocols, visit notes, the written record | set property, append block, create page |
| **Email** | correspondence that leaves the organisation | draft, flag, move, **send (needs approval)** |
| **GitHub** | built and tested, but a clinic doesn't use one — it holds no records in this corpus | comment, create issue, label, commit status |

**Verified against real accounts.** Notion and Linear were run against live API keys
during this build and pass 12 of 15 conformance checks each (the other 3 need a write
target, and are reported as *skipped*, never quietly passed).

**In the demo video, Linear is live.** The issue on screen was created in a real Linear
workspace by the agent during the recording, and the video then cuts into Linear itself
to show it. Slack, Notion and email run on seeded clinic data in the video, on purpose:
connecting the live Notion account would replace the clinic's visit notes with the one
unrelated page that integration can see, and then write to it.

### Plus your own systems — any number of them

**This is the part that matters most, and it is why the app count is not five.**

The systems a company depends on most are usually the ones nobody ships an integration
for: an internal database, a service somebody wrote six years ago, a tool with 40 users
worldwide. Walnut treats these as first-class. There are three ways in, and **none of
them require changing Walnut's code**:

| Route | What you provide | Working example in this repo |
|---|---|---|
| **SQL** | a connection string and one query | `ehr` and `labs` — the clinic's record system |
| **REST** | a short config describing your API's shape | `dispensary` — a live service on `:8900` |
| **Python** | one file implementing six methods | `walnut_plugins/example_csv_source.py` |

So the number of apps is **not fixed at five**. It is five built-in, plus as many custom
sources as you care to add — and a custom source is not second-class. It appears in the
same search results, contributes to the same coverage table, and can be acted on like
any other app.

**Any external tool, app or MCP server can be connected the same way.** If it speaks
HTTP and JSON, the REST route needs a short config and nothing else. If it doesn't — an
MCP server over stdio, a proprietary SDK, a file share — a Python plugin of six methods
(`probe`, `fetch`, `resolve`, `capabilities`, `act`, `undo`) wraps it and it becomes a
source and an action target like any other. There is no MCP-specific adapter in this
repo; the point is that one isn't needed, because the contract is the same for every
kind of system.

**A worked example is in the repo.** `services/dispensary/` is a real FastAPI service
that behaves like a hospital's internal pharmacy system. Walnut connects to it with its
**unmodified** REST adapter, through a config file — no adapter code was written for it.
It passes all 17 conformance checks over a real HTTP socket.

That service also declares its own write operations, and their risk level, in its own
config:

```python
"operations": {
    "place_hold":   {"method": "POST",   "path": "/api/holds",      "tier": "INTERNAL"},
    "release_hold": {"method": "DELETE", "path": "/api/holds/{id}", "tier": "GATED"},
}
```

Walnut hardcodes none of that. Nothing about a URL tells you whether it stops a dose or
starts one — only the people who run the system know that, so they declare it, and
Walnut enforces it.

**Apps the agent writes to in the demo: five** — Slack, Linear, Notion and the custom
pharmacy service directly, plus an email drafted and held for a person to send. The
clinic's own database is read, never written: the EHR connector declares one write
(`annotate`, to a companion table) and the plan does not use it.

---

## 3. Setup instructions

Works with **zero credentials**. Nothing is called over the internet unless you add keys
yourself.

```bash
git clone https://github.com/MEHUL-MODI-Git/Walnut-Hackathon.git
cd Walnut-Hackathon

make install          # creates .venv with uv and installs
make stack            # console on :8000, mock pharmacy service on :8900
```

`make install` uses [uv](https://docs.astral.sh/uv/) (`brew install uv`, or
`pip install uv`). Without it, the same thing by hand:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev,web]"
make stack
```

Open **http://localhost:8000** and type `Ankusha Rao`.

Other commands:

```bash
make test             # the full test suite
make check            # everything that must be green: tests + brief + demo
make web              # console only, without the pharmacy service
make demo             # three-act terminal walkthrough, incl. the injection refusal
```

**If the pharmacy service isn't running**, the console still works. That source then
reports itself as *not searched*, with the reason and a link to fix it — which is the
honest failure mode, and is worth seeing once.

### Connecting real accounts (optional)

Copy `.env.example` to `.env` and fill in what you have. **You must export it yourself**
— nothing in this repo reads `.env` automatically:

```bash
set -a; source .env; set +a
make smoke            # tests the credentials before anything else
make stack
```

Two gotchas that catch people, both noted in `.env.example`: Linear wants a bare
`Authorization` header (not `Bearer`), and **Notion returns nothing until you explicitly
share pages with your integration** — the token is valid, it just cannot see anything.

### Requirements

Python 3.12 and `uv` (or plain `pip`, as above). No database to set up, no Docker, no
build step, no API keys. `ffmpeg` is needed only to rebuild the demo video.

---

## 4. Reliability testing

One command runs every gate:

```bash
make check     # tests green · reliability brief all-PASS · demo runs clean
```

If any of the three fails, `make check` fails. Here is what each part actually checks.

### a. Every connector must pass a behavioural test suite

`walnut/conformance.py` holds **17 checks** that a type signature cannot catch: content
hashes are real, URIs are followable, record ids are unique, hashes stay stable when you
fetch the same record twice, `resolve()` returns `None` for a record that is gone instead
of inventing one, and operations that reach outside the company are gated.

Twelve run with read access alone. The three write checks (prior state captured before a
write, undo restoring it, an unjustified action failing to construct) run when you supply
a write target, and are reported as **skipped** rather than silently passed when you
don't.

**Custom sources are held to the same suite.** A custom source that fails is shown on the
Connectors screen and **is not connected** — it contributes nothing to the brain until it
conforms. This is the answer to the obvious objection about letting anyone plug anything
in: a custom source is not trusted, it is tested.

It caught two real bugs during this build — Slack double-counting thread replies, and the
`labs` source keying records on a column that wasn't unique, which would have silently
collapsed a six-month trend into one data point.

### b. A reliability brief the system writes about itself

```bash
python -m walnut.brief > BRIEF.md
```

Eleven checks, each one reading a **live object** — it runs the system and reports what
happened, rather than restating claims from memory. It reports its own failures, and
`make check` refuses to pass if any check fails. You cannot record a demo against a
broken build.

### c. Coverage — what makes "nothing is lost" checkable

Every search reports **every source**, in one of three states:

| State | Meaning |
|---|---|
| **found** | it had something |
| **nothing** | searched, held nothing. Shown in neutral grey — **never red, never hidden**. A searched-and-empty source is the promise working |
| **not searched** | the only real failure — shown with the reason and a link to fix it |

Ask about something nobody has ever written down and you don't get "0 results". You get
the full table and the sentence *"Every source was searched. None of them hold anything
about X."* That is a negative result that proves the guarantee.

### d. A control condition

`walnut/baseline.py` is this same codebase with the safety layer removed — same
connectors, same data, same task. It executes far more actions, including ones taken
word-for-word from a document containing hidden instructions, and reports every one as a
success. Nothing crashes. That is the point: the failure it prevents is a quiet one.

A test asserts the control is *not* secretly governed, because a flattering control is
worse than no control.

### e. The full suite, offline

```bash
make test                      # 395 tests
WALNUT_GRAPH=simple make test  # again, on the dependency-free graph backend
```

No network, no credentials, no fixtures downloaded at runtime. Green on both backends, so
a broken graph library costs the provenance features, not the product.

### Safety rules, enforced by code rather than by prompting

Two rules are enforced in constructors, so breaking them is impossible rather than
discouraged:

```python
>>> Action(app="linear", operation="create_issue", target={"id": "MED-412"},
...        payload={"body": "…"}, justified_by=())
ValueError: Action linear.create_issue has no justifying evidence. Every action must
cite the evidence that motivated it — this is the rule the whole system exists to
enforce.
```

No claim without a source you can follow. No action without the evidence that caused it.

Actions are ranked by **which direction the risk points**, not by how big the write is:

| | |
|---|---|
| **Runs on its own** | place a hold on a queued prescription — *stopping a dose fails safe* |
| **Runs on its own** | post to the team · create a task · add a cited note |
| **Needs a person** | email outside the organisation · release a hold — *these make things less safe* |

**The agent can stop a dose going out. It cannot start one.** That holds through both
doors: undoing a hold asks for the same approval as releasing one, because it is the same
act. It did not hold at first — undo skipped the check entirely — and that was found by a
test written against what the product claims rather than against what the code did.

Finally, anything read from a connected app is **evidence about the world, never an
instruction**. A document containing hidden commands is refused with the reason shown,
while quarantining that same document is still allowed — the document can justify
labelling itself, just not acting on itself. Run `make demo` and read act 3:

```
linear.set_state (tier INTERNAL) is justified by ingested content containing
embedded instructions [override (1 match)]. Content read from a connected app is
evidence about the world, never a command addressed to this agent.
```

### What this does **not** prove

Listed because a reliability section that only lists successes isn't one.

- **The hidden-instruction detector is a tripwire, not a wall.** Pattern matching over
  adversarial text misses things. It is safe to rely on only because it is not what keeps
  the system safe: content read from an app can never justify an action regardless of
  what it says, so a missed pattern costs the explanation, not the outcome.
- **Contradiction detection is lexical, not semantic.** It finds status and
  present/absent conflicts on shared identifiers. It does not reason about meaning.
- **Identity matching is deterministic, not calibrated.** Uncertain matches go to a
  person, not to a confidence threshold.
- **Three of the five built-in apps are tested against recorded API shapes, not live
  accounts.** Linear is used live in the demo and Notion was verified live; Slack,
  GitHub and email were not.

### Bugs this build actually found

Recorded because "show how you know it works" should include how you learned it didn't.

| Found by | Bug |
|---|---|
| Running the product | **Asking about a person returned nothing.** Identity matching was built and never connected to search — the single most obvious question anyone would ask |
| Running the product | **Every record showed `p01` as its author** instead of the person's name in that app |
| Running the product | **Punctuation lost records.** `co-amoxiclav` and `co amoxiclav` reached different results |
| Running the product | The graph had **zero links** — records were stored and never connected to each other |
| Watching the demo recording | **The contradiction detector read "no new symptoms reported today" as a report of symptoms** — an absence read as its opposite, ranked above the real finding |
| Re-reading the code | **One approval became permanent authority**, so the single action that reaches outside the company could re-run forever |
| Adversarial review | The instruction-injection rule exempted low-risk writes — which included setting an arbitrary email flag, so a poisoned document could mark a real message deleted |
| A test written against the claim | **Undo bypassed the approval gate**, so a held dose could be released in two steps that both looked routine |
| Conformance suite | `labs` keyed records on a non-unique column, collapsing a six-month trend into one record |
| Connecting a live system | **The agent cited itself.** It filed a Linear issue summarising the allergy contradiction; the next ingest read that issue back as a fresh claim from Linear and ranked it above the nurse's message it summarised. Every write now carries a signature, and a signed record is never treated as evidence |

---

## 5. Demo video

**🎥 https://youtu.be/YiiNclHpecY** — 2 min 18 sec.

The video is not a mock-up or a slideshow. It was recorded by driving the running
product in a real browser (`scripts/record_demo.py`), so every number on screen came out
of the live system during the take. If a feature had broken, the recording would show it.

It covers, in order: why scattered information is dangerous · the connected systems
including the clinic's own · one question about one patient · the contradiction between
the chart and the nurse's message · the coverage table including the source that held
nothing · four writes across four systems · **the issue inside Linear, and the hold
inside the pharmacy's own system** · the email stopped for a human · the audit trail.

Two parts of the video come from outside the recorder, and are labelled here so nothing
is passed off as more than it is: the opening animation is a designed sequence
(`demo/intro-v2.html`), and the cut into Linear is a screen recording made in a normal
browser, because Linear's sign-in refuses automated ones. The issue it shows is real
and was created by the agent.

---

## Appendix

### The demo data

**Meridian Health**, a fictional nine-site clinic group. 113 records across seven sources
with the pharmacy service running, 98 across six without it. Ten staff members, each
filed under a different handle in every system. One planted clinical contradiction, one
adversarial document. All invented, all `.example` domains, no real people or patients.
See [fixtures/README.md](fixtures/README.md).

### Repository layout

```
walnut/
  retrieval.py       gather everything about a subject, and report coverage
  briefing.py        organise it the way a person reads, not the way it was stored
  brain.py           the linked store of cited facts
  identity.py        matching the same person across apps
  contradiction.py   finding claims that cannot both be true
  intent.py          plain English → proposed actions, or a clarifying question
  conformance.py     the suite every connector must pass
  actions/           risk tiers, approval gates, typed refusals, one write path
  adapters/          slack · linear · github · notion · email · sql · rest
  web/               the console — no JavaScript, no build step
services/dispensary/ a mock internal hospital system, for the custom-source path
scripts/             the demo recorder, narration, and credential smoke test
fixtures/            Meridian Health
```

### A note on `make demo`

`make demo` is a terminal walkthrough of the same clinic story in three acts: it ingests
128 records from 8 sources, links 397 connections between them, finds the allergy
contradiction, acts on it across six apps including the pharmacy hold — and then, in act
3, shows the one thing the console has no screen for: a document containing hidden
instructions being refused, with the reason shown.

It also prints the control comparison side by side. **The console is the product; the
terminal walkthrough is where you can see the refusal.**

More detail on writing your own connector: [ADAPTERS.md](ADAPTERS.md).
