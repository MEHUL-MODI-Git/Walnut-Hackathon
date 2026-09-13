# Narration — written to the recorded beats

`scripts/narrate.py` parses this file. The parser is deliberately dumb, so keep the shape:

- a segment starts at a `## NN · title` heading
- `- start:` and `- duration:` are seconds (floats)
- `- note:` lines are commentary and are never spoken
- every line beginning `> ` is **spoken text**; a segment with no `>` lines is silent

Run it plainly — **no `--fit`**. Every start below is written by
`scripts/relock_narration.py` from `runs/demo/timeline.json`, which the recorder
produces as it records: the wall-clock second each beat actually began. Summing the
`beat()` values instead is wrong by however long the page loads take, and wrong
cumulatively, because navigations fall between beats rather than at the end.

After every re-record: `python scripts/relock_narration.py`, then narrate.

---

## Who this is written for

Someone who has never heard of this product, watching for two minutes among many other
demos. No vocabulary that has to be explained, no architecture, and the medical example
carried all the way through rather than gestured at.

The order matters because each point depends on the one before:

1. **The problem is that information is split up.** Not missing — written down, by
   people doing their jobs properly, in five different places nobody reads together.
2. **You ask once and get one answer, with every fact's source.** This is the product.
3. **It can act, and here is the proof in the other systems' own screens.** Shown, not
   claimed. A judge for a multi-app agent hackathon needs to see the write land.
4. **It stops where it should.**

The custom connector is stated in the first thirty seconds, because the reason a
hospital story works at all is that the hospital's own database and pharmacy are in the
answer — and nobody ships a connector for those.

## What it deliberately does not say

No tiers, no taint rule, no conformance suite, no adapter contract. All real, all
load-bearing, all more expensive to explain than they buy in two minutes.

## What runs live and what is seeded, stated once

Linear is a real workspace: the issue on screen was created by the agent during the
take. The pharmacy is a real running service. Slack, Notion and email run on seeded
clinic data, and the narration does not claim otherwise.

---

## 01 · The problem
- start: 3.10
- duration: 24.21
- note: The opening animation — scattered sources drawing together into one graph.
- note: The slowest segment after the contradiction. Let the picture do half the work.

> Every company's knowledge is scattered — across documents, email, chat, databases,
> and systems built years ago. No one person can see all of it. In healthcare that is
> dangerous: every fact matters, and the one that matters most is usually in the one
> place nobody looks. Walnut connects every source into one company brain — a
> knowledge graph where each fact keeps its source — and lets an agent act on it.

## 02 · One clinic's systems
- start: 27.31
- duration: 9.88
- note: Connectors screen. Five built-in apps above, three of the clinic's own below.

> This is one clinic group. Five ordinary apps — and three systems of its own: patient
> records, labs, and the pharmacy, connected with configuration, not code.

## 03 · One question
- start: 37.19
- duration: 6.86
- note: Typing "Ankusha Rao", then the answer loading.

> One question, about one patient.

## 04 · One page
- start: 44.05
- duration: 5.86
- note: Scrolling the briefing.

> Everything the organisation knows about her. One page.

## 05 · Where it came from
- start: 49.91
- duration: 3.03
- note: A citation opens.

> Each line shows its source.

## 06 · The thing nobody saw
- start: 52.94
- duration: 18.51
- note: THE BEAT. Read slowly. Never cut. Three facts, and the viewer needs all three.

> Here is why this matters. Her chart says: allergies — none recorded. But twelve days
> ago, a nurse wrote in a team chat that as a child, Ankusha came out in a rash after
> amoxicillin. It never reached her chart. And she is currently prescribed
> co-amoxiclav. Co-amoxiclav is amoxicillin.

## 07 · What it does with that
- start: 71.45
- duration: 11.05
- note: The coverage table, including the source that was searched and held nothing.

> Walnut doesn't decide which one is true — it shows both, with sources. And it names
> every system it searched, including the one that held nothing. That's an answer, not
> a gap.

## 08 · It can act
- start: 82.50
- duration: 5.36
- note: The conflict on the Knowledge screen; the propose click.

> From here, it can act — across every connected system.

## 09 · Across all of them
- start: 87.86
- duration: 10.03
- note: The activity list: Linear (with the real issue URL), Slack, Notion, the
- note: pharmacy hold, and the email held for approval.

> It opens a task in Linear, replies in the care-team channel, flags the chart — and
> places a hold on the dose the pharmacy was about to hand over.

## 10 · In Linear itself
- start: 97.89
- duration: 6.08
- note: The issue MEH-n, open in Linear's own interface. Real workspace, real write.

> That task is real — created by the agent, in Linear, just now.

## 11 · In the pharmacy's own system
- start: 103.97
- duration: 7.03
- note: The dispensary's own queue page. The co-amoxiclav row reads HELD with the reason.

> And this is the pharmacy's own system. The dose is on hold, with Walnut's reason
> beside it.

## 12 · Where it stops
- start: 111.00
- duration: 10.52
- note: The approvals queue. Safe direction versus unsafe.

> It can stop a dose by itself, because stopping one is the safe direction. It cannot
> release one, and it cannot email outside the clinic, unless a person approves.

## 13 · Nothing lost, nothing invented
- start: 121.52
- duration: 13.32
- note: The audit trail: identity merges held for a human, then the decision ledger.

> Every action carries the evidence that caused it. And where the systems disagree
> about who someone even is, it says so, instead of guessing. Nothing is lost — and
> nothing is invented.
