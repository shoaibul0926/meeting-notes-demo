"""Joins a Google Meet call as a silent, cameraless bot so the existing
system-audio loopback capture (recorder.py) picks up the call's audio
exactly like it does for an in-person meeting. No new audio pipeline -
the bot just needs to be let into the call and keep playing its audio
through this machine's speakers."""
import re
import threading
import time

from playwright.sync_api import sync_playwright

BOT_NAME = "Meeting Notes Bot"
JOIN_TIMEOUT_S = 120


class MeetBot(threading.Thread):
    def __init__(self, meet_url, name=BOT_NAME):
        super().__init__(daemon=True)
        self.meet_url = meet_url
        self.name = name
        self.status = "starting"  # starting -> waiting_for_admission -> in_meeting -> ended/error/left
        self.error = None
        self._leave_requested = threading.Event()

    def request_leave(self):
        self._leave_requested.set()
        self.join(timeout=30)

    def run(self):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=False,
                    args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
                )
                context = browser.new_context(permissions=[])
                page = context.new_page()
                try:
                    self._join(page)
                    self.status = "in_meeting"
                    self._wait_until_left_or_removed(page)
                finally:
                    try:
                        if page.locator('button[aria-label*="Leave call" i]').count():
                            page.locator('button[aria-label*="Leave call" i]').first.click(timeout=3000)
                    except Exception:
                        pass
                    context.close()
                    browser.close()
        except Exception as exc:
            self.error = exc
            self.status = "error"

    def _join(self, page):
        page.goto(self.meet_url, wait_until="domcontentloaded", timeout=30000)
        self._dismiss_popup(page, "Dismiss")
        self._dismiss_popup(page, "Got it")

        name_box = page.locator('input[type="text"][aria-label*="name" i]').first
        try:
            name_box.wait_for(timeout=10000)
            name_box.fill(self.name)
        except Exception:
            pass  # already signed in / name not requested

        self._turn_off(page, "camera")
        self._turn_off(page, "microphone")

        ask = page.get_by_role("button", name=re.compile("ask to join|join now", re.I))
        ask.first.click(timeout=15000)

        self.status = "waiting_for_admission"
        deadline = time.time() + JOIN_TIMEOUT_S
        while time.time() < deadline:
            if page.locator('button[aria-label*="Leave call" i]').count():
                return
            if page.get_by_text(re.compile("denied|can.t join|no longer", re.I)).count():
                raise RuntimeError("Host denied or the meeting ended before the bot was admitted.")
            time.sleep(1)
        raise RuntimeError(f"Not admitted within {JOIN_TIMEOUT_S}s - ask the host to let the bot in.")

    def _turn_off(self, page, device):
        try:
            btn = page.locator(f'button[aria-label*="Turn off {device}" i]').first
            if btn.count():
                btn.click(timeout=3000)
        except Exception:
            pass

    def _dismiss_popup(self, page, label):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count():
                btn.first.click(timeout=2000)
        except Exception:
            pass

    def _wait_until_left_or_removed(self, page):
        while not self._leave_requested.is_set():
            if not page.locator('button[aria-label*="Leave call" i]').count():
                self.status = "ended"
                return
            time.sleep(2)
        self.status = "left"
