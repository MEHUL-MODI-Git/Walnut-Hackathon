# Narration — written to the recorded beats

`scripts/narrate.py` parses this file. The parser is deliberately dumb, so keep the shape:

- a segment starts at a `## NN · title` heading
- `- start:` and `- duration:` are seconds (floats)
- `- note:` lines are commentary and are never spoken
- every line beginning `> ` is **spoken text**; a segment with no `>` lines is silent

Run it plainly — **no `--fit`**. Every start below is copied from
`runs/demo/timeline.json`, which `scripts/record_demo.py` writes as it records: the
wall-clock second each beat actually began. Summing the `beat()` values instead is
wrong by however long the page loads take, and wrong cumulatively, because the
navigations fall between beats rather than at the end. Scaling proportionally to
correct for it pushed the closing line past the end of the video.

If you re-record, re-copy these numbers from the new `timeline.json`.

---

## Who this is written for

Someone who has never heard of this product, watching for two minutes, with no idea
what "a company brain" is supposed to mean. So: no vocabulary that has to be
explained, no architecture, and the medical example carried all the way through
rather than gestured at. The words earn their place by making the next thing on
screen make sense.

Three things it has to land, in this order, because each one depends on the last:

1. **The problem is that information is split up.** Not that it is missing —
   everything in the story is written down somewhere, by someone doing their job
   properly. It is just written down in four different places, and nobody reads all
   four.
2. **You ask once and get one answer.** This is the product. Everything else is
   consequence.
3. **It can act, and it stops where it should.** Shown, not claimed.

The custom connector belongs in the first ten seconds, not as a feature note at the
end: the reason a hospital story works at all is that the hospital's own database and
its own pharmacy are in the answer, and those are not apps anyone ships a connector
for.

## What it deliberately does not say

No tiers, no taint rule, no conformance suite, no adapter contract. Every one of them
is real and load-bearing, and every one of them costs more seconds to explain than it
buys in a two-minute video. The audit screen shows the receipts; the README explains
the mechanisms. The narration stays on what a nurse would notice.

## Timing

The take measures **1:54.8**. Read at roughly 150 words per minute, the script below
is ~235 words ≈ 94 s of speech in 116 s of picture — which leaves silence in the
right places rather than filling every frame.

The contradiction segment is deliberately the slowest in the video: 48 words given 22
seconds, about two-thirds of normal pace. It is the one moment where a viewer has to
hold three facts at once, and rushing it loses the whole demo.

---

## 01 · A dozen separate systems
- start: 3.54
- duration: 11.21
- note: Connectors screen. Five built-in apps above, three of the clinic's own below.

> A clinic's information lives in a dozen separate systems. Five here are ordinary
> apps. Three are the clinic's own — patient database, labs, and pharmacy.

## 02 · One question
- start: 14.75
- duration: 8.16
- note: Typing "Ankusha Rao" into the search box, then the answer loading.

> Those three were added with configuration, not new code. Now — one question.

## 03 · One page
- start: 22.91
- duration: 6.22
- note: Scrolling the briefing: identity, conditions, medications, allergies, results.

> Everything the organisation knows about her. One page.

## 04 · Where it came from
- start: 29.13
- duration: 3.53
- note: A citation opens, showing the underlying record.

> Each line shows its source.

## 05 · The thing nobody saw
- start: 32.66
- duration: 21.02
- note: THE BEAT. Read slowly. Never cut. Three facts, and the viewer needs all three.

> Here is why this matters. Her chart says: allergies — none recorded. But twelve days
> ago, a nurse wrote in a team chat that as a child, Ankusha came out in a rash after
> amoxicillin. It never reached her chart. And she is currently prescribed
> co-amoxiclav. Co-amoxiclav is amoxicillin.

## 06 · What it does with that
- start: 53.68
- duration: 12.54
- note: The coverage table, including the source that was searched and held nothing.

> Walnut doesn't decide which one is true — it shows both, with sources. And it names
> every system it searched, including the one that held nothing. That's an answer, not
> a gap.

## 07 · It can act
- start: 66.22
- duration: 5.54
- note: The conflict on the Knowledge screen; the propose click.

> From here, it can act — across every connected system.

## 08 · Across all of them
- start: 71.76
- duration: 12.53
- note: The activity list: linear, slack, notion, dispensary hold, email refused.

> It opens a task to reconcile the record, replies in the care-team channel, flags the
> chart — and places a hold on the dose the pharmacy was about to hand over.

## 09 · Where it stops
- start: 84.29
- duration: 12.52
- note: The approvals queue. The asymmetry is the point: safe direction versus unsafe.

> It can stop a dose by itself, because stopping one is the safe direction. It cannot
> release one, and it cannot email outside the clinic, unless a person approves.

## 10 · Nothing lost, nothing invented
- start: 96.81
- duration: 13.42
- note: The audit trail: identity merges held for a human, then the decision ledger.
- note: An earlier draft closed on the injected-document refusal. That refusal is real
- note: and tested, but it is not reachable from the browser, so the line described
- note: something the viewer could not see. The closing line now names what is on
- note: screen — which is the same claim the whole product is making.

> Every action carries the evidence that caused it. And where the systems disagree
> about who someone even is, it says so, instead of guessing. Nothing is lost — and
> nothing is invented.
