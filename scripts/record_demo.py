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

The whole thing must also come in under two minutes, which is a hard submission cap.
Time was taken out of the beats the script has slack in — never out of the
contradiction or the coverage table, because those are the two nobody can say faster.

**Why a browser and not a screen recorder.** A scripted run is reproducible. If a take
is wrong, fix the script and re-run rather than re-performing it — at 5am that is the
difference between shipping and not.
"""

from __future__ import annotations

import argparse
import contextlib
import json
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
VIEWPORT = {"width": 1280, "height": 800}
"""Deliberately smaller than the design's comfortable width: fewer CSS
pixels across the frame means larger text in the finished video, which is
the constraint that actually applies when this is watched in a browser tab
among a dozen other submissions."""
PATIENT = "Ankusha Rao"
SUBJECT = "MR-4417"  # her MRN — the key every system stores her under


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError("ffmpeg is needed to trim the recording's dead head")
    return path


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _spawn(module: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--port", str(port),
         "--log-level", "error"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _await(port: int, what: str, health: str | None = None) -> None:
    """Wait until the service ANSWERS, not merely until the port is busy.

    A port check passes against the previous run's instance while it is still
    releasing the socket. The recorder then started, the new service never bound, and
    the take came out with `dispensary.place_hold` refused for connection refused —
    a video of the product failing, produced by the recorder, indistinguishable at a
    glance from a video of the product working.
    """
    import httpx

    for _ in range(120):
        if not _free(port):
            if health is None:
                return
            try:
                if httpx.get(health, timeout=2.0).status_code == 200:
                    return
            except Exception:  # noqa: BLE001 - not up yet is not an error
                pass
        time.sleep(0.25)
    raise RuntimeError(f"{what} never answered on :{port}")


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


def _require_dispensary() -> None:
    """Refuse to record the action beat against a dead pharmacy."""
    import httpx

    try:
        ok = httpx.get(f"http://127.0.0.1:{DISPENSARY_PORT}/healthz",
                       timeout=3.0).status_code == 200
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        raise RuntimeError(
            "the dispensary stopped answering before the action beat. The take would "
            "show place_hold refused for connection refused — the product working "
            "correctly on a broken rig, which is the one failure a viewer cannot tell "
            "apart from the product being broken."
        )


def _warm() -> None:
    """Make the first request before the camera rolls, not during the first shot.

    Walnut ingests lazily on the first page view — every connector, the clinic
    database, the pharmacy. That took nine and a half seconds of the opening frame,
    so the video began with a blank page loading. Doing it here costs the same nine
    seconds off camera and gives them back to the demo.
    """
    try:
        import httpx

        for path in ("/", "/connectors", "/knowledge"):
            httpx.get(f"http://127.0.0.1:{PORT}{path}", timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"the app did not answer a warm-up request: {exc}") from exc


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
        _await(DISPENSARY_PORT, "the dispensary service",
               f"http://127.0.0.1:{DISPENSARY_PORT}/healthz")
        _reset_dispensary()
        web = _spawn("walnut.web.app:app", PORT)
        try:
            _await(PORT, "the app")
            _warm()
            yield
        finally:
            web.terminate()
            with contextlib.suppress(Exception):
                web.wait(timeout=5)
    finally:
        dispensary.terminate()
        with contextlib.suppress(Exception):
            dispensary.wait(timeout=5)



# A pointer the viewer can follow, and scrolling that does not jump.
#
# This is stagecraft, and it belongs ONLY to the recording: it is injected into the
# browser session, never into the product, which still ships without a line of
# JavaScript. It adds no information and changes no data — without a visible pointer
# a click is an unexplained cut, and a wheel event that teleports the page is a
# reader losing their place.
_CURSOR = """
(() => {
  const dot = document.createElement('div');
  dot.id = '__cursor';
  dot.style.cssText = `position:fixed;z-index:2147483647;width:18px;height:18px;
    margin:-9px 0 0 -9px;border-radius:50%;pointer-events:none;
    background:rgba(122,75,42,.30);border:1.5px solid rgba(122,75,42,.85);
    transition:transform .08s ease-out;left:-100px;top:-100px`;
  const add = () => document.body && document.body.appendChild(dot);
  document.readyState === 'loading'
    ? document.addEventListener('DOMContentLoaded', add) : add();
  addEventListener('mousemove', e => {
    dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px';
  }, true);
  addEventListener('mousedown', () => {
    dot.style.transform = 'scale(2.1)';
    dot.style.background = 'rgba(122,75,42,.55)';
  }, true);
  addEventListener('mouseup', () => {
    dot.style.transform = 'scale(1)';
    dot.style.background = 'rgba(122,75,42,.30)';
  }, true);
})();
"""

_CARD = """<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap');
  html,body{height:100%%;margin:0}
  body{background:#FBFAF7;color:#1A1D1A;display:grid;place-items:center;
       font-family:'IBM Plex Sans',-apple-system,system-ui,sans-serif}
  .c{text-align:center;max-width:44rem;padding:0 2rem}
  h1{font-size:3.4rem;font-weight:600;letter-spacing:-.02em;margin:0 0 .6rem}
  .m{color:#7A4B2A;font-size:1.05rem;font-weight:500;letter-spacing:.08em;
     text-transform:uppercase;margin:0 0 2.2rem}
  p{font-size:1.45rem;line-height:1.5;color:#3A3E39;margin:0}
  .r{width:64px;height:2px;background:#7A4B2A;margin:2.2rem auto 0}
</style>
<div class="c"><div class="m">%s</div><h1>%s</h1><p>%s</p><div class="r"></div></div>"""



# The only emphasis in the whole recording, spent on the one frame everything else
# exists to set up. A soft lift and a ring — no zoom, no motion, nothing that would
# read as an edit. If the viewer takes one thing away from two minutes, it is this
# box, and a flat page gives them no reason to look at it rather than anywhere else.
_EMPHASIS = """
(() => {
  const el = document.querySelector('.alert');
  if (!el) return;
  el.style.transition = 'box-shadow .5s ease, transform .5s ease';
  el.style.boxShadow = '0 0 0 3px rgba(154,106,23,.28), 0 10px 34px rgba(26,29,26,.10)';
  el.style.transform = 'translateY(-2px)';
  el.scrollIntoView({block: 'center', behavior: 'smooth'});
})();
"""

def _card(page, kicker: str, title: str, line: str) -> None:
    page.set_content(_CARD % (kicker, title, line))
    page.wait_for_timeout(400)


def _glide(page, distance: int, steps: int = 26) -> None:
    """Scroll the way a person does: continuously, so the eye keeps its place."""
    step = distance / steps
    for _ in range(steps):
        page.mouse.wheel(0, step)
        page.wait_for_timeout(16)


def _point_at(page, locator) -> None:
    """Move the pointer onto a thing before clicking it, so the click reads."""
    try:
        box = locator.bounding_box()
        if box:
            page.mouse.move(box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2, steps=18)
            page.wait_for_timeout(220)
    except Exception:  # noqa: BLE001 - a pointer flourish must never fail a take
        pass


def record(fast: bool = False) -> Path:
    from playwright.sync_api import sync_playwright

    scale = 0.25 if fast else 1.0

    marks: list[dict[str, float | str]] = []
    # Set properly once the browser context exists. The video starts when the context
    # does, not when this function does — timing the beats from here counted Chromium's
    # launch as part of the recording, so every mark was seconds later than the frame
    # it named and the head trim cut into the title card.
    clock = [time.monotonic()]

    def beat(seconds: float, name: str = "") -> None:
        """Hold the frame for as long as the narration line takes to say.

        Each beat records the wall-clock second it actually began. Computing those
        starts by summing the beats is wrong by however long the page loads took, and
        wrong CUMULATIVELY: the navigations fall between beats, not at the end, so a
        proportional correction pushes every later line further out of step than the
        one before it. The last line ended up starting after the video had finished.

        Measuring costs nothing and cannot drift.
        """
        at = time.monotonic() - clock[0]
        marks.append({"at": round(at, 2), "for": seconds * scale, "beat": name})
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
        ctx.add_init_script(_CURSOR)
        page = ctx.new_page()
        clock[0] = time.monotonic()  # the camera is rolling from here
        base = f"http://127.0.0.1:{PORT}"

        # ---- title --------------------------------------------------------
        _card(page, "Meridian Health", "Walnut",
              "One question. Every system at once.")
        beat(2, "title")

        # ---- 0:00 the estate ------------------------------------------------
        page.goto(f"{base}/connectors", wait_until="networkidle")
        beat(7, "connectors")

        # ---- 0:15 ask about the patient -------------------------------------
        page.goto(base, wait_until="networkidle")
        beat(2, "ask")
        _point_at(page, page.locator("#q"))
        page.click("#q")
        page.type("#q", PATIENT, delay=110 * scale)
        beat(1, "typed")
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle")
        beat(6.5, "answer")

        # ---- 0:25 the briefing ----------------------------------------------
        for _ in range(3):
            _glide(page, 260)
            beat(1.5, "briefing")

        # open one citation — the receipt is one click away, never in the way
        cites = page.locator("details.cite > summary")
        if need(cites, "a citation to open"):
            target = cites.nth(min(3, cites.count() - 1))
            _point_at(page, target)
            target.click()
            beat(3, "citation")

        # ---- 0:45 the contradiction -----------------------------------------
        alert = need(page.locator(".alert"), "the contradiction — the demo's climax")
        if alert is not None:
            alert.scroll_into_view_if_needed()
            page.evaluate(_EMPHASIS)
            page.wait_for_timeout(500)
            beat(21, "contradiction")

        # ---- 1:05 coverage — including what held nothing --------------------
        page.keyboard.press("End")
        beat(12, "coverage")

        # ---- 1:15 it acts ---------------------------------------------------
        page.goto(f"{base}/knowledge", wait_until="networkidle")
        page.keyboard.press("End")
        beat(3, "conflicts")
        # Address the clinical conflict by its own subject, not by position. An
        # earlier take matched an enclosing div and clicked the LAST propose button
        # on the page — so the video showed a patient's unrecorded drug reaction and
        # then approved an email about a rota sync. It looked entirely fine.
        # The hold is the beat the whole custom-connector story turns on, and the
        # only one that depends on a second process still being alive 75 seconds in.
        # Checked here rather than discovered in the footage.
        _require_dispensary()

        propose = need(
            page.locator(f'.conflict[data-subject="{SUBJECT}"] form[action="/act"] button'),
            f"the propose-actions button for {SUBJECT}",
        )
        if propose is not None:
            propose.scroll_into_view_if_needed()
            _point_at(page, propose)
            beat(1, "propose")
            propose.click()
            page.wait_for_load_state("networkidle")
        page.goto(f"{base}/actions?tab=activity", wait_until="networkidle")
        beat(12, "acted")

        # ---- 1:30 where it stops --------------------------------------------
        page.goto(f"{base}/approvals", wait_until="networkidle")
        beat(12, "approvals")

        # ---- 1:45 what it refuses -------------------------------------------
        page.goto(f"{base}/audit", wait_until="networkidle")
        beat(3, "audit")
        _glide(page, 700, steps=40)
        beat(9, "refusal")

        # ---- close --------------------------------------------------------
        _card(page, "Walnut", "Nothing is lost.",
              "Every answer cited. Every action justified.")
        beat(3, "close")

        ctx.close()
        browser.close()

    if missing:
        print("\n  this take is missing:", file=sys.stderr)
        for what in missing:
            print(f"    - {what}", file=sys.stderr)

    total = round(time.monotonic() - clock[0], 2)

    video = next(OUT.glob("*.webm"), None)
    if video is None:
        raise RuntimeError("playwright produced no video")
    final = OUT / "walnut-demo.webm"
    video.rename(final)

    # Chromium spends the better part of ten seconds launching, and the recording
    # starts the moment the context does — so every take opened on nine seconds of
    # blank frame. In a two-minute submission that is eight percent of the budget
    # spent on a browser starting up. Cut it here, and re-base the timeline by the
    # same amount, so the narration and the picture stay one consistent thing.
    head = max(0.0, (marks[0]["at"] if marks else 0.0) - 0.8)
    if head > 0.5:
        trimmed = OUT / "trimmed.webm"
        subprocess.run(
            [_ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{head:.2f}",
             "-i", str(final), "-c", "copy", str(trimmed)], check=True)
        trimmed.replace(final)
        for mark in marks:
            mark["at"] = round(mark["at"] - head, 2)
        total = round(total - head, 2)

    (OUT / "timeline.json").write_text(
        json.dumps({"total": total, "head_trimmed": round(head, 2),
                    "marks": marks}, indent=1))
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
    print(f"  {OUT / 'timeline.json'}  — when each beat actually began")
    print("\n  Narration: demo/narration.md, placed on the measured timeline.")
    print("  To get an mp4:  ffmpeg -i walnut-demo.webm -c:v libx264 -crf 20 walnut-demo.mp4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
