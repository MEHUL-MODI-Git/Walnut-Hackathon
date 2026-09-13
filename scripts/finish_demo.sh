#!/usr/bin/env bash
#
# Mux the silent screen recording and the narration track into one shippable mp4.
#
#     bash scripts/finish_demo.sh
#     bash scripts/finish_demo.sh path/to/video.webm path/to/audio.mp3 path/to/out.mp4
#
# H.264 / CRF 20 / yuv420p because that is the combination every judge's laptop, every
# browser and every submission form will actually play. The Playwright .webm is VP8,
# which Safari and a good many upload pipelines will not touch.
#
# The three arguments exist for testing the pipeline against something other than the
# real take; day to day, run it with none.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

VIDEO="${1:-$ROOT/runs/demo/walnut-demo.webm}"
AUDIO="${2:-$ROOT/runs/demo/narration.mp3}"
OUT="${3:-$ROOT/runs/demo/walnut-demo.mp4}"

fail() { printf '\n  %s\n\n' "$1" >&2; exit 1; }

command -v ffmpeg  >/dev/null 2>&1 || fail "ffmpeg is not on PATH. brew install ffmpeg"
command -v ffprobe >/dev/null 2>&1 || fail "ffprobe is not on PATH. brew install ffmpeg"

[ -f "$VIDEO" ] || fail "no video at $VIDEO
  Record one first:  ./.venv/bin/python scripts/record_demo.py"

[ -f "$AUDIO" ] || fail "no narration at $AUDIO
  Make one first:    ./.venv/bin/python scripts/narrate.py
  (or --dry-run for silence, to check the mux)"

probe() { ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$1"; }

V_DUR="$(probe "$VIDEO")"
A_DUR="$(probe "$AUDIO")"

printf '  video  %s  (%.2fs)\n' "${VIDEO##*/}" "$V_DUR"
printf '  audio  %s  (%.2fs)\n' "${AUDIO##*/}" "$A_DUR"

# Warn rather than fail. A narration longer than the picture is a real problem, but it
# is the operator's problem to judge — refusing to produce a file at 5am would be worse
# than producing one and saying what is wrong with it. `-af apad -shortest` makes the
# output exactly as long as the video either way: short audio is padded with silence,
# long audio is cut off at the final frame.
OVER="$(awk -v a="$A_DUR" -v v="$V_DUR" 'BEGIN{ d = a - v; printf "%.2f", (d > 0 ? d : 0) }')"
if awk -v o="$OVER" 'BEGIN{ exit !(o > 0.5) }'; then
  printf '\n  WARNING: the narration is %ss longer than the picture.\n' "$OVER"
  printf '  The tail will be cut at the last frame. See demo/narration.md.\n\n'
fi

mkdir -p "$(dirname "$OUT")"

ffmpeg -y -v error -stats \
  -i "$VIDEO" -i "$AUDIO" \
  -map 0:v:0 -map 1:a:0 \
  -c:v libx264 -crf 20 -preset medium -pix_fmt yuv420p \
  -c:a aac -b:a 192k -ar 44100 \
  -af apad -shortest \
  -movflags +faststart \
  "$OUT"

[ -f "$OUT" ] || fail "ffmpeg reported success but wrote no file at $OUT"

OUT_DUR="$(probe "$OUT")"
OUT_BYTES="$(wc -c < "$OUT" | tr -d ' ')"
OUT_MB="$(awk -v b="$OUT_BYTES" 'BEGIN{ printf "%.1f", b / 1000000 }')"

printf '\n  %s\n' "$OUT"
printf '  %.2fs · %s MB · H.264 CRF 20 · AAC 192k · faststart\n\n' "$OUT_DUR" "$OUT_MB"
