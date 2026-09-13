"""Turn demo/narration.md into a voiceover track timed to the recorded beats.

    ELEVENLABS_API_KEY=... ./.venv/bin/python scripts/narrate.py
    ./.venv/bin/python scripts/narrate.py --dry-run      # no key needed, no API calls
    ./.venv/bin/python scripts/narrate.py --cuts --fit runs/demo/walnut-demo.webm

Writes `runs/demo/audio/NN.mp3` per segment and one `runs/demo/narration.mp3` in which
each segment begins at its target start time.

**Why silence-padding rather than trusting the TTS.** A text-to-speech call returns
however long it returns; asking it to land on a 12.000-second mark is asking it to do
something it has no mechanism for. So each segment is synthesised at its natural length
and then *placed* — delayed by exactly its target start and mixed onto a common
timeline. The gaps between segments are silence, which is free and exact. The picture
and the voice therefore agree by construction rather than by luck, and re-synthesising
one changed sentence cannot shift every line that follows it.

**Why mix rather than concatenate.** Concatenation would make every segment's start
depend on the measured length of all the segments before it — one long take and the
whole track walks. `adelay` + `amix` pins each segment to an absolute timestamp instead.
It also means an overrun is *audible as an overlap* rather than silently pushing the
demo's climax past the frame it belongs to. That is the honest failure mode: the script
tells you, loudly, which segments are too long for their beats.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NARRATION = ROOT / "demo" / "narration.md"
OUT = ROOT / "runs" / "demo"
VIDEO = OUT / "walnut-demo.webm"

# The synthesis cache deliberately lives OUTSIDE runs/demo. `record_demo.py` opens with
# `shutil.rmtree(OUT)`, so anything cached under runs/ is destroyed by the next take —
# and re-running the recorder is the most ordinary thing in the world at this stage.
# ElevenLabs calls cost credits; losing them to a re-record would be a self-inflicted
# wound. runs/ is already gitignored, and so is this.
CACHE = ROOT / "demo" / ".cache"

API = "https://api.elevenlabs.io/v1/text-to-speech"
MODEL = "eleven_multilingual_v2"

# ElevenLabs "Rachel" — the oldest and most widely-available stock voice, so it is the
# one most likely to exist on whatever account the key belongs to. Any account with a
# different roster should override it: --voice, or ELEVENLABS_VOICE_ID.
DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"

# Planning rate used for --dry-run placeholders and for the overrun warnings. ~156 wpm
# is an unhurried demo-narration pace; ElevenLabs multilingual v2 sits near it.
WORDS_PER_SECOND = 2.6

# Total beat time in record_demo.py, used to sanity-check a --fit target. Kept as a
# constant rather than re-derived, because this script must not import the recorder.
BEAT_TOTAL = 89.81


def die(msg: str) -> None:
    print(f"\n  {msg}\n", file=sys.stderr)
    raise SystemExit(1)


def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        die("ffmpeg is not on PATH. brew install ffmpeg")
    return exe


def ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        die("ffprobe is not on PATH. brew install ffmpeg")
    return exe


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
        die(f"command failed: {' '.join(cmd[:3])} …\n\n{tail}")


def duration_of(path: Path) -> float:
    proc = subprocess.run(
        [ffprobe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        die(f"ffprobe could not read {path}")
    try:
        return float(proc.stdout.strip())
    except ValueError:
        die(f"ffprobe returned no duration for {path}")
        return 0.0  # unreachable; keeps type checkers quiet


# ---------------------------------------------------------------- the narration file

@dataclass
class Segment:
    number: str
    title: str
    start: float
    duration: float
    cut: str = "no"          # "no" | "yes" | "partial"
    lines: list[str] = field(default_factory=list)       # spoken, in order
    cut_lines: list[bool] = field(default_factory=list)  # parallel: is this a `~` line?

    def text(self, apply_cuts: bool) -> str:
        keep = [
            line for line, is_cut in zip(self.lines, self.cut_lines)
            if not (apply_cuts and is_cut)
        ]
        return " ".join(keep).strip()

    @property
    def words(self) -> int:
        return len(self.text(False).split())


HEAD = re.compile(r"^##\s+(\d+)\s*[·.\-]\s*(.+?)\s*$")
META = re.compile(r"^-\s+(start|duration|cut)\s*:\s*(.+?)\s*$")


def parse(path: Path) -> list[Segment]:
    """Read narration.md. The format is documented at the top of that file.

    Anything that is not a `## NN · title` heading, a `- key: value` line or a `> `
    quote is commentary — the intro prose, the timing tables, the `- note:` lines — and
    is skipped. That is deliberate: the file has to stay readable by a human holding a
    microphone, so the machine-readable part is a small island inside ordinary Markdown.
    """
    if not path.exists():
        die(f"no narration file at {path}")
    segments: list[Segment] = []
    current: Segment | None = None
    in_preamble = True

    for raw in path.read_text(encoding="utf-8").splitlines():
        head = HEAD.match(raw)
        if head:
            # Headings before the first one carrying `- start:` belong to the prose
            # intro ("## How the timeline was derived"); those never match HEAD because
            # they are not numbered, so reaching here always means a real segment.
            current = Segment(number=head.group(1), title=head.group(2),
                              start=-1.0, duration=-1.0)
            segments.append(current)
            in_preamble = False
            continue
        if current is None or in_preamble:
            continue
        meta = META.match(raw)
        if meta:
            key, value = meta.group(1), meta.group(2)
            if key == "cut":
                current.cut = value.strip().lower()
            else:
                try:
                    setattr(current, key, float(value))
                except ValueError:
                    die(f"segment {current.number}: `{key}: {value}` is not a number")
            continue
        if raw.startswith(">"):
            line = raw[1:].strip()
            if not line:
                continue
            is_cut = line.startswith("~")
            if is_cut:
                line = line[1:].strip()
            current.lines.append(line)
            current.cut_lines.append(is_cut)

    if not segments:
        die(f"{path} parsed to zero segments — has its format changed?")
    for seg in segments:
        if seg.start < 0 or seg.duration <= 0:
            die(f"segment {seg.number} ({seg.title}) is missing start or duration")
    # Starts must be non-decreasing, or "place at absolute time" means nothing.
    for a, b in zip(segments, segments[1:]):
        if b.start < a.start:
            die(f"segment {b.number} starts before {a.number} — the table is out of order")
    return segments


# ---------------------------------------------------------------------- synthesis

def synth(text: str, voice: str, key: str) -> bytes:
    import httpx

    body = {
        "text": text,
        "model_id": MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    try:
        resp = httpx.post(
            f"{API}/{voice}",
            headers={"xi-api-key": key, "accept": "audio/mpeg",
                     "content-type": "application/json"},
            json=body, timeout=120.0,
        )
    except httpx.HTTPError as exc:
        die(f"ElevenLabs request failed: {exc}")
    if resp.status_code == 401:
        die("ElevenLabs rejected the key (401). Check ELEVENLABS_API_KEY.")
    if resp.status_code == 404:
        die(f"ElevenLabs has no voice {voice} on this account (404).\n"
            f"  List yours at https://api.elevenlabs.io/v1/voices and pass --voice.")
    if resp.status_code != 200:
        die(f"ElevenLabs returned {resp.status_code}: {resp.text[:400]}")
    return resp.content


def placeholder(path: Path, seconds: float) -> None:
    """A silent mp3 of the length the real take is predicted to be.

    This is what makes --dry-run worth having: the placement, the mixing, the overlap
    warnings and the mux downstream all exercise real files of realistic length, so the
    only untested step on the day the key arrives is the HTTP call itself.
    """
    run([ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{seconds:.3f}",
         "-c:a", "libmp3lame", "-q:a", "4", str(path)])


def retempo(src: Path, dst: Path, tempo: float) -> None:
    """Speed a segment up slightly so it fits its beat, without changing its pitch.

    Used only to absorb a small overrun. Anything past ~1.2× stops sounding like a
    person reading and starts sounding like a person who has been told they are over
    time, so the caller caps it and warns rather than compressing harder.
    """
    run([ffmpeg(), "-y", "-v", "error", "-i", str(src),
         "-filter:a", f"atempo={tempo:.4f}", "-c:a", "libmp3lame", "-q:a", "4", str(dst)])


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--narration", type=Path, default=NARRATION)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--voice", default=os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE),
                    help=f"ElevenLabs voice id (default {DEFAULT_VOICE}, 'Rachel')")
    ap.add_argument("--dry-run", action="store_true",
                    help="no API calls and no key needed: silent placeholders of the "
                         "predicted length, so the whole pipeline can be validated")
    ap.add_argument("--cuts", action="store_true",
                    help="apply DEMO.md's cut list (segments marked `cut: yes`, and the "
                         "`~`-marked lines of `cut: partial` segments)")
    ap.add_argument("--fit", nargs="?", const=str(VIDEO), default=None, metavar="VIDEO",
                    help="stretch the gaps between segments so the timeline spans the "
                         "real video's duration (speech is never stretched)")
    ap.add_argument("--max-tempo", type=float, default=1.15,
                    help="speed a segment up by at most this to fit its slot (1.0 = off)")
    ap.add_argument("--force", action="store_true", help="ignore the cache, re-synthesise")
    args = ap.parse_args()

    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not args.dry_run and not key:
        die("ELEVENLABS_API_KEY is not set.\n\n"
            "  Get a key at https://elevenlabs.io/app/settings/api-keys, then:\n\n"
            "      export ELEVENLABS_API_KEY='sk_…'\n"
            "      ./.venv/bin/python scripts/narrate.py\n\n"
            "  Or validate the whole pipeline without one:\n\n"
            "      ./.venv/bin/python scripts/narrate.py --dry-run")

    segments = parse(args.narration)
    if args.cuts:
        segments = [s for s in segments if s.cut != "yes"]

    # --- the timeline ----------------------------------------------------------
    # Beat-derived starts are the authority. --fit scales them onto the real take,
    # which is longer than the beat total by however long the page navigations took.
    scale = 1.0
    if args.fit:
        video = Path(args.fit)
        if not video.exists():
            die(f"--fit was given {video}, which does not exist")
        measured = duration_of(video)
        scale = measured / BEAT_TOTAL
        if scale < 0.9:
            die(f"{video.name} is {measured:.2f}s against {BEAT_TOTAL:.2f}s of beats.\n"
                f"  That is almost certainly a `record_demo.py --fast` check take, not a\n"
                f"  shipping recording. Fitting the narration to it would compress the\n"
                f"  whole script. Re-record without --fast, or drop --fit.")
        print(f"  fitting to {video.name}: {measured:.2f}s / {BEAT_TOTAL:.2f}s "
              f"of beats = ×{scale:.3f} on every start time")

    cache_dir = CACHE
    cache_dir.mkdir(parents=True, exist_ok=True)

    placed: list[tuple[Segment, Path, float, float]] = []  # seg, mp3, start, length
    warnings: list[str] = []

    for index, seg in enumerate(segments):
        text = seg.text(args.cuts)
        start = seg.start * scale
        # A segment's slot runs to the next segment's start, not just its own beat —
        # a silent following beat is usable room, and segment 05 exists to provide it.
        nxt = segments[index + 1].start * scale if index + 1 < len(segments) else None
        slot = (nxt - start) if nxt is not None else seg.duration * scale

        target = args.out / "audio" / f"{seg.number}.mp3"
        target.parent.mkdir(parents=True, exist_ok=True)

        if not text:
            print(f"  {seg.number}  {start:6.2f}s  (silent) {seg.title}")
            continue

        digest = hashlib.sha256(
            f"{args.voice}\x00{MODEL}\x00{'dry' if args.dry_run else 'live'}\x00{text}"
            .encode()).hexdigest()[:16]
        cached = cache_dir / f"{seg.number}-{digest}.mp3"

        if cached.exists() and not args.force:
            state = "cached"
        else:
            if args.dry_run:
                placeholder(cached, max(0.4, len(text.split()) / WORDS_PER_SECOND))
            else:
                cached.write_bytes(synth(text, args.voice, key))
            state = "synthesised"
            # Drop stale cache entries for this segment so the directory reflects the
            # current script rather than accumulating every draft of it.
            for old in cache_dir.glob(f"{seg.number}-*.mp3"):
                if old != cached:
                    old.unlink()

        length = duration_of(cached)
        source = cached

        if args.max_tempo > 1.0 and length > slot + 0.05:
            tempo = min(args.max_tempo, length / slot)
            sped = cache_dir / f"{seg.number}-{digest}-x{tempo:.3f}.mp3"
            if not sped.exists() or args.force:
                retempo(cached, sped, tempo)
            source, length = sped, duration_of(sped)
            state += f", ×{tempo:.2f}"

        shutil.copyfile(source, target)
        placed.append((seg, target, start, length))

        over = length - slot
        flag = ""
        if over > 0.05:
            flag = f"  OVER by {over:5.2f}s"
            warnings.append(
                f"{seg.number} {seg.title}: {length:.2f}s of speech in a {slot:.2f}s slot "
                f"(+{over:.2f}s) — {seg.words} words")
        print(f"  {seg.number}  {start:6.2f}s  {length:5.2f}s/{slot:5.2f}s  "
              f"{seg.title} [{state}]{flag}")

    if not placed:
        die("nothing to synthesise — every segment is silent or cut")

    # --- place everything on one timeline ---------------------------------------
    # adelay pins each segment to an absolute millisecond; amix sums them. normalize=0
    # keeps each segment at the level it was synthesised at — with it on, ffmpeg divides
    # by the input count and the whole track goes quiet wherever segments do not overlap.
    total = max(start + length for _, _, start, length in placed)
    if args.fit:
        total = max(total, duration_of(Path(args.fit)))

    cmd = [ffmpeg(), "-y", "-v", "error"]
    for _, path, _, _ in placed:
        cmd += ["-i", str(path)]
    chains = []
    for i, (_, _, start, _) in enumerate(placed):
        ms = int(round(start * 1000))
        chains.append(
            f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"adelay={ms}|{ms}[d{i}]")
    mix = "".join(f"[d{i}]" for i in range(len(placed)))
    chains.append(f"{mix}amix=inputs={len(placed)}:normalize=0:dropout_transition=0[mixed]")
    chains.append(f"[mixed]apad,atrim=0:{total:.3f},asetpts=N/SR/TB[out]")
    cmd += ["-filter_complex", ";".join(chains), "-map", "[out]",
            "-c:a", "libmp3lame", "-q:a", "2", str(args.out / "narration.mp3")]
    run(cmd)

    final = args.out / "narration.mp3"
    print(f"\n  {final}  ({duration_of(final):.2f}s"
          f"{', silent placeholders' if args.dry_run else ''})")

    if warnings:
        print(f"\n  {len(warnings)} segment(s) do not fit their beat — they will overlap "
              f"the next line:")
        for w in warnings:
            print(f"    - {w}")
        print("  demo/narration.md explains each one and what the fix is.")

    if args.dry_run:
        print("\n  This is silence. Set ELEVENLABS_API_KEY and re-run without --dry-run.")
    print(f"  Then:  bash scripts/finish_demo.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
