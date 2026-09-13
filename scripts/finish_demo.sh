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

# An operator-recorded clip laid over the "linear" slot. Linear's sign-in refuses
# automated browsers, so the real issue is shown from a screen recording made in the
# operator's own browser: scaled to the frame, shown for exactly the slot the recorder
# left, trimmed never stretched, last frame held if the clip runs short.
#     LINEAR_CLIP=path/to/clip.mov bash scripts/finish_demo.sh
LINEAR_CLIP="${LINEAR_CLIP:-}"
INPUTS=(-i "$VIDEO" -i "$AUDIO")
VIDEO_ARGS=(-map 0:v:0)
if [ -n "$LINEAR_CLIP" ]; then
  [ -f "$LINEAR_CLIP" ] || fail "no clip at $LINEAR_CLIP"
  TL="$ROOT/runs/demo/timeline.json"
  [ -f "$TL" ] || fail "no timeline.json — record first"
  SLOT="$(python3 "$ROOT/scripts/_slot.py" "$TL" linear)"
  [ -n "$SLOT" ] || fail "the recording has no 'linear' slot"
  SLOT_AT="${SLOT% *}"; SLOT_FOR="${SLOT#* }"
  SLOT_END="$(awk -v a="$SLOT_AT" -v f="$SLOT_FOR" 'BEGIN{printf "%.2f", a+f}')"
  printf '  clip   %s  → over the Linear slot at %ss for %ss\n' "${LINEAR_CLIP##*/}" "$SLOT_AT" "$SLOT_FOR"
  INPUTS+=(-i "$LINEAR_CLIP")
  FILTER="[2:v]trim=0:${SLOT_FOR},setpts=PTS-STARTPTS,fps=25,"
  FILTER+="scale=1280:800:force_original_aspect_ratio=decrease:flags=lanczos,"
  FILTER+="pad=1280:800:(ow-iw)/2:(oh-ih)/2:color=#FBFAF7,"
  FILTER+="tpad=stop_mode=clone:stop_duration=${SLOT_FOR},setpts=PTS+${SLOT_AT}/TB[clip];"
  FILTER+="[0:v][clip]overlay=eof_action=pass:enable='between(t,${SLOT_AT},${SLOT_END})'[v]"
  VIDEO_ARGS=(-filter_complex "$FILTER" -map "[v]")
fi

ffmpeg -y -v error -stats \
  "${INPUTS[@]}" \
  "${VIDEO_ARGS[@]}" -map 1:a:0 \
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
