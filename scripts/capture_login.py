"""Log into an external app once, in a window this tool opens, and keep the session.

    python scripts/capture_login.py linear

The demo recorder runs its own clean Chromium, which knows nothing about the browser you
are logged into. To show a real Linear issue inside Linear's own interface, the
recorder needs a session — so this opens a visible window on linear.app, waits while
you sign in, and saves the resulting cookies and local storage to
`runs/auth/<app>.json`. `runs/` is gitignored; the file never leaves this machine.

Nothing here reads or stores your password. The browser handles the login; this only
keeps what the browser would keep.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTH = ROOT / "runs" / "auth"

SITES = {
    "linear": "https://linear.app/login",
    "notion": "https://www.notion.so/login",
    "slack": "https://slack.com/signin",
}


def _looks_signed_in(app: str, url: str) -> bool:
    """A workspace URL, not a login or auth page."""
    lowered = url.lower()
    if any(word in lowered for word in ("login", "signin", "sign-in", "auth", "oauth",
                                        "accounts.google", "verify", "magic")):
        return False
    if app == "linear":
        # linear.app/<workspace>/... once signed in
        return "linear.app/" in lowered and len(lowered.split("linear.app/", 1)[1]) > 1
    if app == "notion":
        return "notion.so/" in lowered
    if app == "slack":
        return ".slack.com/" in lowered
    return False


def main() -> int:
    app = sys.argv[1] if len(sys.argv) > 1 else "linear"
    if app not in SITES:
        print(f"unknown app {app!r}; one of {sorted(SITES)}", file=sys.stderr)
        return 1

    from playwright.sync_api import sync_playwright

    AUTH.mkdir(parents=True, exist_ok=True)
    target = AUTH / f"{app}.json"

    with sync_playwright() as pw:
        # The stock "Chrome for Testing" build plus the automation flag is exactly
        # what login pages screen for — Google refused outright, Linear said "we were
        # unable to verify it's you". Real Chrome with the automation signal removed
        # is an ordinary browser as far as a sign-in page can tell.
        browser = pw.chromium.launch(
            headless=False, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/128.0.0.0 Safari/537.36"),
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = ctx.new_page()
        page.goto(SITES[app])
        print(f"\n  A browser window is open on {SITES[app]}.")
        print("  Sign in there. This saves the session by itself the moment your")
        print("  workspace appears — nothing to press. (Up to ten minutes.)")

        # No `input()`: run from a session without a keyboard attached, that raised
        # EOFError the instant the window opened. Watch the browser instead.
        deadline = time.monotonic() + 600
        signed_in = False
        while time.monotonic() < deadline:
            try:
                open_pages = [pg for pg in ctx.pages if not pg.is_closed()]
                if not open_pages:
                    break
                # Google sign-in opens a second tab; the workspace may land in either.
                if any(_looks_signed_in(app, pg.url) for pg in open_pages):
                    time.sleep(2.5)  # let the app finish writing its own session
                    signed_in = True
                    break
            except Exception:  # noqa: BLE001 - a page can vanish mid-poll
                break
            time.sleep(1.0)

        if not signed_in:
            print("\n  No signed-in workspace was seen. Nothing saved.", file=sys.stderr)
            with contextlib.suppress(Exception):
                browser.close()
            return 1

        ctx.storage_state(path=str(target))
        browser.close()

    print(f"\n  saved → {target}")
    print("  The recorder will use it automatically on the next take.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
