"""Record the two-minute demo by driving the real console in a real browser.

    python scripts/record_demo.py            # ~112s, writes runs/demo/*.webm
    python scripts/record_demo.py --fast     # same beats, quarter timings, for checking

Nothing here is faked or re-created. It starts the actual app, opens Chromium, types
into the actual search box and clicks the actual buttons, so what lands in the video is
what the product does. If a beat silently stops working, the recording shows it rather
than papering over it — which is the only property that makes an automated recording
trustworthy.

Voice is laid over afterwards. Each `beat()` holds the frame for as long as the
corresponding narration line takes to say, so the two line up without re-cutting.

The timings were measured against the script rather than estimated from it. The first
cut ran 90 seconds of picture under 137 seconds of speech — every line would have
landed on the wrong screen, and the contradiction beat, the one the whole demo exists
for, gave a 51-word passage 12 seconds. `demo/narration.md` holds the segment table;
if a line and its frame drift apart, that table and these numbers are the two halves
to reconcile.

**Why a browser and not a screen recorder.** A scripted run is reproducible. If a take
is wrong, fix the script and re-run rather than re-performing it — at 5am that is the
difference between shipping and not.
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runs" / "demo"
PORT = 8321
DISPENSARY_PORT = 8900
VIEWPORT = {"width": 1440, "height": 900}
PATIENT = "Ankusha Rao"
SUBJECT = "MR-4417"  # her MRN — the key every system stores her under


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _spawn(module: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--port", str(port),
         "--log-level", "error"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _await(port: int, what: str) -> None:
    for _ in range(80):
        if not _free(port):
            return
        time.sleep(0.25)
    raise RuntimeError(f"{what} did not come up on :{port}")


def _reset_dispensary() -> None:
    """Put the pharmacy back to its seeded state before the camera rolls.

    The demo's climax is the agent placing a hold on a dose that is queued to go out.
    Placing that hold CHANGES the prescription to "held" — so a second take finds
    nothing queued, the plan silently drops the step, and the best beat in the video
    is simply absent with no error anywhere. A recording you cannot re-take
    identically is a recording you get one chance at.
    """
    try:
        import httpx

        httpx.post(f"http://127.0.0.1:{DISPENSARY_PORT}/api/_reset", timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"could not reset the dispensary before recording: {exc}. Recording now "
            "would risk a take with no hold in it."
        ) from exc


@contextlib.contextmanager
def serve():
    """Run the whole stack for the duration of the recording.

    The dispensary starts FIRST and is waited on, because Walnut registers it at
    startup only if it answers. Recording against a half-started stack produces a
    take where the custom internal system reads "not searched" — honest, but not the
    demo: the whole point of that connector is a system Walnut had never heard of
    until the customer plugged it in.
    """
    dispensary = _spawn("services.dispensary.app:app", DISPENSARY_PORT)
    try:
        _await(DISPENSARY_PORT, "the dispensary service")
        _reset_dispensary()
        web = _spawn("walnut.web.app:app", PORT)
        try:
            _await(PORT, "the app")
            time.sleep(1.5)  # let the first ingest finish before the camera rolls
            yield
        finally:
            web.terminate()
            with contextlib.suppress(Exception):
                web.wait(timeout=5)
    finally:
        dispensary.terminate()
        with contextlib.suppress(Exception):
            dispensary.wait(timeout=5)


def record(fast: bool = False) -> Path:
    from playwright.sync_api import sync_playwright

    scale = 0.25 if fast else 1.0

    def beat(seconds: float) -> None:
        """Hold the frame for as long as the narration line takes to say."""
        time.sleep(seconds * scale)

    missing: list[str] = []

    def need(locator, what: str):
        """A beat that silently found nothing is a beat that records a blank frame.

        Rather than skip it quietly, note it — `main()` prints the list, so a take
        that lost its climax says so instead of looking fine."""
        if not locator.count():
            missing.append(what)
            return None
        return locator.first

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    with serve(), sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(
            viewport=VIEWPORT, record_video_dir=str(OUT),
            record_video_size=VIEWPORT, device_scale_factor=2,
            color_scheme="light",  # the console's light theme is the designed one
        )
        page = ctx.new_page()
        base = f"http://127.0.0.1:{PORT}"

        # ---- 0:00 the estate ------------------------------------------------
        page.goto(f"{base}/connectors", wait_until="networkidle")
        beat(11)

        # ---- 0:15 ask about the patient -------------------------------------
        page.goto(base, wait_until="networkidle")
        beat(2)
        page.click("#q")
        page.type("#q", PATIENT, delay=110 * scale)
        beat(1)
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle")
        beat(8)

        # ---- 0:25 the briefing ----------------------------------------------
        for _ in range(3):
            page.mouse.wheel(0, 260)
            beat(2.2)

        # open one citation — the receipt is one click away, never in the way
        cites = page.locator("details.cite > summary")
        if need(cites, "a citation to open"):
            cites.nth(min(3, cites.count() - 1)).click()
            beat(5)

        # ---- 0:45 the contradiction -----------------------------------------
        alert = need(page.locator(".alert"), "the contradiction — the demo's climax")
        if alert is not None:
            alert.scroll_into_view_if_needed()
            beat(22)

        # ---- 1:05 coverage — including what held nothing --------------------
        page.keyboard.press("End")
        beat(13)

        # ---- 1:15 it acts ---------------------------------------------------
        page.goto(f"{base}/knowledge", wait_until="networkidle")
        page.keyboard.press("End")
        beat(3)
        # Address the clinical conflict by its own subject, not by position. An
        # earlier take matched an enclosing div and clicked the LAST propose button
        # on the page — so the video showed a patient's unrecorded drug reaction and
        # then approved an email about a rota sync. It looked entirely fine.
        propose = need(
            page.locator(f'.conflict[data-subject="{SUBJECT}"] form[action="/act"] button'),
            f"the propose-actions button for {SUBJECT}",
        )
        if propose is not None:
            propose.scroll_into_view_if_needed()
            beat(1)
            propose.click()
            page.wait_for_load_state("networkidle")
        page.goto(f"{base}/actions?tab=activity", wait_until="networkidle")
        beat(14)

        # ---- 1:30 where it stops --------------------------------------------
        page.goto(f"{base}/approvals", wait_until="networkidle")
        beat(13)

        # ---- 1:45 what it refuses -------------------------------------------
        page.goto(f"{base}/audit", wait_until="networkidle")
        beat(4)
        page.mouse.wheel(0, 700)
        beat(12)

        ctx.close()
        browser.close()

    if missing:
        print("\n  this take is missing:", file=sys.stderr)
        for what in missing:
            print(f"    - {what}", file=sys.stderr)

    video = next(OUT.glob("*.webm"), None)
    if video is None:
        raise RuntimeError("playwright produced no video")
    final = OUT / "walnut-demo.webm"
    video.rename(final)
    return final


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fast", action="store_true",
                    help="quarter timings — to check the beats, not to ship")
    args = ap.parse_args()

    print(f"recording{' (fast check)' if args.fast else ''} — driving the real app…")
    path = record(fast=args.fast)
    size = path.stat().st_size / 1_000_000
    print(f"\n  {path}  ({size:.1f} MB)")
    print("\n  Lay the narration from DEMO.md over this; the beats are timed to it.")
    print("  To get an mp4:  ffmpeg -i walnut-demo.webm -c:v libx264 -crf 20 walnut-demo.mp4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
