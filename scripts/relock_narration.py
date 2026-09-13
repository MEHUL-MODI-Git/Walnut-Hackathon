"""Re-point demo/narration.md at the timeline the last recording actually produced.

Every re-record shifts the beats by a second or two — a page loads faster, Chromium
launches slower. Copying the new numbers across by hand is the kind of chore that gets
skipped once, and the failure is silent: the voice keeps talking about the screen it
was written for while the picture has moved on, and nothing in the pipeline complains.

So the mapping is mechanical. Each narration segment owns one named beat and runs until
the next segment's beat begins, which means the words and the frames cannot drift apart
without someone editing this table.

    python scripts/relock_narration.py        # after every re-record
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMELINE = ROOT / "runs" / "demo" / "timeline.json"
NARRATION = ROOT / "demo" / "narration.md"

# segment number -> the beat it speaks over. Order is the running order.
SEGMENTS: tuple[tuple[str, str], ...] = (
    ("01", "intro"),
    ("02", "connectors"),
    ("03", "typed"),
    ("04", "briefing"),
    ("05", "citation"),
    ("06", "contradiction"),
    ("07", "coverage"),
    ("08", "conflicts"),
    ("09", "acted"),
    ("10", "linear"),
    ("11", "dispensary"),
    ("12", "approvals"),
    ("13", "audit"),
)
# The title and closing cards are deliberately silent: a card is read, not narrated,
# and a voice over it is one thing too many.
SILENT_TAIL = "close"


def main() -> int:
    if not TIMELINE.exists():
        print(f"no {TIMELINE} — record first", file=sys.stderr)
        return 1

    data = json.loads(TIMELINE.read_text())
    first: dict[str, float] = {}
    for mark in data["marks"]:
        first.setdefault(str(mark["beat"]), float(mark["at"]))

    missing = [beat for _, beat in SEGMENTS if beat not in first]
    if missing:
        print(f"the recording has no beat named: {', '.join(missing)}", file=sys.stderr)
        return 1

    bounds: dict[str, tuple[float, float]] = {}
    for index, (number, beat) in enumerate(SEGMENTS):
        start = first[beat]
        if index + 1 < len(SEGMENTS):
            end = first[SEGMENTS[index + 1][1]]
        else:
            end = first.get(SILENT_TAIL, float(data["total"]))
        bounds[number] = (start, end - start)

    text = NARRATION.read_text()

    def rewrite(match: re.Match[str]) -> str:
        start, duration = bounds[match.group(1)]
        return (f"## {match.group(1)} ·{match.group(2)}\n"
                f"- start: {start:.2f}\n- duration: {duration:.2f}")

    text, count = re.subn(
        r"## (\d\d) ·(.+?)\n- start: [\d.]+\n- duration: [\d.]+", rewrite, text)
    if count != len(SEGMENTS):
        print(f"matched {count} segments, expected {len(SEGMENTS)}", file=sys.stderr)
        return 1

    total = float(data["total"])
    text = re.sub(r"The take measures \*\*[\d:.]+\*\*",
                  f"The take measures **{int(total) // 60}:{total % 60:04.1f}**", text)
    NARRATION.write_text(text)

    for number, beat in SEGMENTS:
        start, duration = bounds[number]
        print(f"  {number}  {start:7.2f}s  for {duration:6.2f}s   ({beat})")
    print(f"\n  {NARRATION} re-locked to a {total:.1f}s take")
    return 0


if __name__ == "__main__":
    sys.exit(main())
