from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urlparse

from playwright.async_api import Error as BrowserError, async_playwright

from .config import Config
from .dedupe import fingerprints
from .state import Session, utc_now

JOIN_URL = "https://www.classpoint.app/"
READ_STATE = Path(__file__).with_name("slide.js").read_text()
MIN_EVENT_CHECK_SECONDS = 0.25
LOG = logging.getLogger(__name__)
OBSERVE = """(() => {
  window.__classpointSlideRevision = 0;
  let pending = false;
  const changed = () => {
    window.__classpointSlideRevision++;
    if (pending || !window.__classpointRequestCheck) return;
    pending = true;
    setTimeout(() => {
      pending = false;
      window.__classpointRequestCheck().catch(() => {});
    }, 100);
  };
  const inSlide = node => (node.nodeType === 1 ? node : node.parentElement)?.closest('.presenter_img');
  new MutationObserver(records => {
    if (records.some(record => inSlide(record.target) || (record.type === 'childList' &&
      [...record.addedNodes, ...record.removedNodes].some(node => node.nodeType === 1 &&
        (node.matches('.presenter_img') || node.querySelector('.presenter_img'))))))
      changed();
  }).observe(document, {subtree:true, attributes:true, childList:true, characterData:true});
  document.addEventListener('load', event => {
    if (event.target.matches?.('.presenter_img img')) changed();
  }, true);
})();"""


async def pause_or_stop(stop: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def wait_join_step(page, stop, predicate, invalid_selector, error_message):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if stop.is_set():
            raise InterruptedError("Stopped before joining.")
        if await page.locator(invalid_selector).count():
            raise ValueError(error_message)
        if await page.locator(predicate).count():
            return
        await pause_or_stop(stop, 0.2)
    raise TimeoutError("ClassPoint did not respond to the join form within 30 seconds.")


async def join_class(page, code: str, name: str, stop: asyncio.Event):
    if not re.fullmatch(r"[A-Za-z0-9]{1,8}", code) or not 1 <= len(name.strip()) <= 25:
        raise ValueError("Use a code of 1-8 letters/numbers and a name of 1-25 characters.")
    await page.goto(JOIN_URL, wait_until="domcontentloaded", timeout=45_000)
    field = page.locator('input[placeholder="Class code"]')
    await field.wait_for(state="visible", timeout=30_000)
    if stop.is_set():
        raise InterruptedError("Stopped before joining.")
    await field.fill(code)
    await page.locator('button:has(svg[data-testid="ArrowForwardIcon"])').click()
    await wait_join_step(page, stop, 'input[placeholder="Your name"], .classroom',
                         'input[placeholder="Class code"][aria-invalid="true"]',
                         "Class code is invalid or no longer in use.")
    name_field = page.locator('input[placeholder="Your name"]')
    if await name_field.count():
        await name_field.fill(name)
        await page.get_by_role("button", name=re.compile(r"let.s go|join|enter", re.I)).first.click()
        await wait_join_step(page, stop, ".classroom",
                             'input[placeholder="Your name"][aria-invalid="true"]',
                             "ClassPoint rejected the name (it may already be taken). Try another name.")


@dataclass
class EndDetector:
    end_grace: float
    disconnect_grace: float
    ended_since: float | None = None
    disconnected_since: float | None = None

    def update(self, state: dict, now: float) -> str | None:
        # A presenter can pause the slideshow without ending the class.
        end_signal = state.get("ended") or (state.get("join") and not state.get("classroom"))
        if end_signal:
            if self.ended_since is None:
                self.ended_since = now
            if now - self.ended_since >= self.end_grace:
                return "ClassPoint session ended (or the presenter removed the runner)."
        else:
            self.ended_since = None
        disconnected = state.get("disconnected") or (not state.get("classroom") and not end_signal)
        if disconnected:
            if self.disconnected_since is None:
                self.disconnected_since = now
            if now - self.disconnected_since >= self.disconnect_grace:
                return "Connection was lost for too long; these are the slides captured before disconnection."
        else:
            self.disconnected_since = None
        return None


class SlideRecorder:
    def __init__(self, config: Config, session: Session):
        self.config = config
        self.session = session
        self.candidate = None
        self.candidate_since = 0.0
        self.candidate_crop = None
        self.pending_confirmation = False
        self.check_requested = asyncio.Event()
        self.socket = None

    def observe_socket(self, socket):
        # Track the classroom WebSocket for connection loss.
        parsed = urlparse(socket.url)
        host = parsed.hostname or ""
        if parsed.path != "/classsession" or not (host == "classpoint.app" or host.endswith(".classpoint.app")):
            return
        self.socket = socket
        self.check_requested.set()
        socket.on("close", lambda _: self.check_requested.set())

    async def capture_once(self, page, cdp, now: float | None = None, before=None):
        self.pending_confirmation = False
        if before is None:
            before = await page.evaluate(READ_STATE)
        slide = before["slide"]
        if not slide or before["ended"] or before["disconnected"]:
            self.candidate = None
            return False
        clip = slide["clip"].copy()
        source_size = slide.get("source_size")
        # Density comes from the painted image before object-fit clipping.
        # Chromium paints the crop at this density, including visible ink.
        clip["scale"] = min(max(1.0, slide.get("source_density", 1.0)), self.config.max_capture_scale)
        result = await cdp.send("Page.captureScreenshot", {
            "format": "png", "fromSurface": True, "captureBeyondViewport": True,
            "clip": clip,
        })
        observed_at = time.monotonic() if now is None else now
        after = await page.evaluate(READ_STATE)
        if after["slide"] != slide or after["ended"] or after["disconnected"]:
            self.candidate = None
            return False
        png = base64.b64decode(result["data"])
        exact, digest = await asyncio.to_thread(fingerprints, png)
        if digest in self.session.comparison_hashes or exact in self.session.hashes:
            self.candidate = None
            return False
        crop = (clip, source_size)
        if digest != self.candidate or crop != self.candidate_crop:
            self.candidate, self.candidate_since = digest, observed_at
            self.candidate_crop = crop
        if observed_at - self.candidate_since < self.config.settle_seconds:
            self.pending_confirmation = True
            return False
        if len(self.session.data["slides"]) >= self.config.max_slides:
            raise RuntimeError("Slide safety limit reached; recorded slides are preserved.")
        if shutil.disk_usage(self.session.directory).free < self.config.min_free_mb * 1024 * 1024:
            raise RuntimeError("Low disk space; recorded slides are preserved.")
        return self.session.add_slide(png, exact, digest, source_size=source_size,
                                      capture_scale=clip.get("scale", 1.0))

    async def wait_for_check(self, stop: asyncio.Event):
        ready_at = time.monotonic() + min(MIN_EVENT_CHECK_SECONDS, self.config.poll_seconds)
        delay = self.config.poll_seconds
        if self.pending_confirmation:
            remaining = self.candidate_since + self.config.settle_seconds - time.monotonic()
            delay = min(delay, max(0.1, remaining))
        tasks = [asyncio.create_task(event.wait()) for event in (stop, self.check_requested)]
        try:
            await asyncio.wait(tasks, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        # Continuous DOM activity can otherwise request a full PNG at 10 Hz.
        remaining = ready_at - time.monotonic()
        if remaining > 0 and not stop.is_set():
            await pause_or_stop(stop, remaining)

    async def watch(self, page, stop: asyncio.Event, on_joined=None):
        cdp = await page.context.new_cdp_session(page)
        detector = EndDetector(self.config.end_grace_seconds, self.config.disconnect_grace_seconds)
        started = time.monotonic()
        error_since = None
        async def notify_joined():
            try:
                await on_joined()
            except Exception:
                LOG.exception("Failed to announce that class recording started")

        notification = asyncio.create_task(notify_joined()) if on_joined else None
        try:
            while not stop.is_set():
                self.check_requested.clear()
                now = time.monotonic()
                if now - started >= self.config.max_session_hours * 3600:
                    return "Maximum recording duration reached; captured slides are preserved."
                if page.is_closed():
                    return "Browser closed unexpectedly; captured slides are preserved."
                try:
                    state = await page.evaluate(READ_STATE)
                    # Quiet classrooms may have no slide traffic for minutes.
                    if self.socket and self.socket.is_closed():
                        state["disconnected"] = True
                    self.session.data.update(last_checked_at=utc_now(), connection_state=(
                        "reconnecting" if state["disconnected"] else "classroom connected" if state["classroom"] else "outside classroom"))
                    reason = detector.update(state, now)
                    if reason:
                        return reason
                    if not state["disconnected"]:
                        await self.capture_once(page, cdp, before=state)
                    else:
                        self.candidate = None
                        self.pending_confirmation = False
                    phase = ("reconnecting" if state["disconnected"] else
                             "recording slides" if state["slide"] else "waiting for presenter/slides")
                    if self.session.data["phase"] != phase:
                        self.session.data["phase"] = phase
                        self.session.save()
                    error_since = None
                except BrowserError:
                    self.session.data.update(connection_state="browser error", phase="reconnecting")
                    self.candidate = None
                    self.pending_confirmation = False
                    error_since = now if error_since is None else error_since
                    if now - error_since >= self.config.disconnect_grace_seconds:
                        return "Browser connection failed; captured slides are preserved."
                await self.wait_for_check(stop)
            return "Recording stopped by request."
        finally:
            if notification:
                notification.cancel()
                await asyncio.gather(notification, return_exceptions=True)
            try:
                await cdp.detach()
            except BrowserError:
                pass

    async def run(self, stop: asyncio.Event, on_joined=None):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
                await context.add_init_script(OBSERVE)
                page = await context.new_page()
                await page.expose_function("__classpointRequestCheck", self.check_requested.set)
                page.on("websocket", self.observe_socket)
                await join_class(page, self.session.data["code"], self.session.data["name"], stop)
                return await self.watch(page, stop, on_joined)
            finally:
                await browser.close()
