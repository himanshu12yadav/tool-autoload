"""Background engine: one worker thread owns an asyncio loop + Playwright.

The GUI thread never touches Playwright. It calls the thread-safe methods below
(start_job / stop_job / ...) and reads LogEvent / JobEvent objects from the queue.
The engine works on private copies of Job objects, so no state is shared across threads.
"""
from __future__ import annotations

import asyncio
import dataclasses
import queue
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from . import chrome, settings
from .job import Job, is_finished, next_delay

LOAD_TIMEOUT_MS = 30_000
DEFAULT_CHANNELS = ("chrome", "msedge", None)  # None = Playwright's bundled Chromium


@dataclass
class LogEvent:
    text: str  # already timestamped


@dataclass
class JobEvent:
    job_id: str
    status: str  # starting | running | finished | stopped
    done: int
    remaining: float | None = None  # seconds until next reload, when known
    limit_remaining: float | None = None  # seconds until the "Run for" limit ends the job, if it has one


class Engine:
    def __init__(
        self,
        events: queue.Queue,
        *,
        headless: bool = False,
        profile_dir: Path | None = None,
        channels=DEFAULT_CHANNELS,
        rng=random,
    ):
        self.events = events
        self._headless = headless
        self._profile_dir = Path(profile_dir) if profile_dir else settings.app_dir() / "profile"
        self._channels = tuple(channels)
        self._rng = rng

        # Everything below is only touched from the loop thread.
        self._pw = None
        self._context = None
        self._browser = None  # set only when attached to the user's own Chrome
        self._connect_url: str | None = None  # e.g. http://127.0.0.1:9222; None = launch our own window
        self._adopted: set = set()  # pages that were already open in the user's Chrome (never closed by us)
        self._closing = False
        self._launch_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task] = {}
        self._pages: dict[str, list] = {}  # job id -> its tabs
        self._cdp: dict = {}  # page -> CDP session (used for hard reloads)
        self._blank_pages: list = []

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="autoreload-engine", daemon=True)
        self._thread.start()

    # ---- thread-safe public API ------------------------------------------------

    def start_job(self, job: Job) -> None:
        self._submit(self._start_job(dataclasses.replace(job, done=0, status="starting")))

    def stop_job(self, job_id: str) -> None:
        self._submit(self._stop_job(job_id))

    def stop_all(self) -> None:
        self._submit(self._stop_all())

    def remove_job(self, job_id: str) -> None:
        """Stop the job and close the tabs it opened (tabs that were already open in your Chrome stay)."""
        self._submit(self._remove_job(job_id))

    def set_connect_url(self, url: str | None) -> None:
        """Attach to the user's own Chrome (remote-debugging URL) or, with None, use our own window.
        Stops running jobs and drops the current browser connection when the mode changes."""
        self._submit(self._set_connect_url(url))

    def shutdown(self, timeout: float = 15.0) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    # ---- internals ---------------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def _submit(self, coro) -> None:
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _report(f):
            if not f.cancelled() and f.exception():
                self._log(f"Internal error: {f.exception()!r}")

        fut.add_done_callback(_report)

    def _log(self, text: str) -> None:
        self.events.put(LogEvent(f"{datetime.now():%H:%M:%S} {text}"))

    def _emit(self, job: Job, status: str, remaining: float | None = None,
              limit_remaining: float | None = None) -> None:
        job.status = status
        self.events.put(JobEvent(job.id, status, job.done, remaining, limit_remaining))

    async def _start_job(self, job: Job) -> None:
        existing = self._tasks.get(job.id)
        if existing and not existing.done():
            return
        self._tasks[job.id] = asyncio.create_task(self._run_job(job))

    async def _stop_job(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _stop_all(self) -> None:
        for job_id in list(self._tasks):
            await self._stop_job(job_id)

    async def _remove_job(self, job_id: str) -> None:
        await self._stop_job(job_id)
        for page in self._pages.pop(job_id, []):
            self._cdp.pop(page, None)
            if page in self._adopted:  # never close the user's own tab
                self._adopted.discard(page)
            elif not page.is_closed():
                try:
                    await page.close()
                except PlaywrightError:
                    pass

    async def _set_connect_url(self, url: str | None) -> None:
        async with self._launch_lock:
            if url == self._connect_url:
                return
            await self._stop_all()
            await self._close_context()
            self._connect_url = url
            self._log("Browser mode: " + ("your own Chrome" if url else "this tool's own window"))

    async def _close_context(self) -> None:
        """Drop the current browser. An attached Chrome is only disconnected, never quit."""
        ctx, browser = self._context, self._browser
        self._context = None
        self._browser = None
        self._pages.clear()
        self._cdp.clear()
        self._adopted.clear()
        self._blank_pages.clear()
        try:
            if browser is not None:
                await browser.close()  # for connect_over_cdp this disconnects
            elif ctx is not None:
                await ctx.close()
        except PlaywrightError:
            pass

    async def _shutdown(self) -> None:
        self._closing = True
        await self._stop_all()
        await self._close_context()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def _ensure_context(self):
        async with self._launch_lock:
            if self._context is not None:
                return self._context
            if self._pw is None:
                self._pw = await async_playwright().start()
            if self._connect_url:
                return await self._attach_to_chrome(self._connect_url)
            last_error = None
            for channel in self._channels:
                name = channel or "chromium"
                try:
                    ctx = await self._pw.chromium.launch_persistent_context(
                        str(self._profile_dir / name),
                        channel=channel,
                        headless=self._headless,
                        no_viewport=True,
                    )
                except PlaywrightError as e:
                    last_error = e
                    continue
                self._context = ctx
                self._blank_pages = list(ctx.pages)
                ctx.on("close", lambda *_a, c=ctx: self._on_context_closed(c))
                self._log(f"Browser started ({name})")
                return ctx
            raise RuntimeError(
                "Could not start a browser. Install Chrome/Edge, or run "
                f"'playwright install chromium'. Last error: {last_error}"
            )

    async def _attach_to_chrome(self, url: str):
        port = urlparse(url).port
        info = await asyncio.to_thread(chrome.browser_info, port) if port else None
        if info is None:
            raise RuntimeError(f"Could not reach Chrome at {url}. Click 'Open Chrome' first and keep that "
                               "window open.")
        if not chrome.is_chrome(info):
            raise RuntimeError(f"Port {port} belongs to another program ({info.get('Browser', 'unknown')}), "
                               "not Chrome. Choose a different port, then click 'Open Chrome'.")
        try:
            browser = await self._pw.chromium.connect_over_cdp(url)
        except PlaywrightError as e:
            raise RuntimeError(
                f"Could not reach Chrome at {url}. Click 'Open Chrome' first and keep that window open. "
                f"({_short(e)})"
            ) from None
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        self._browser = browser
        self._context = ctx
        self._blank_pages = []  # in this mode every tab belongs to the user; never reuse one blindly
        browser.on("disconnected", lambda *_a, c=ctx: self._on_context_closed(c))
        self._log("Connected to your Chrome")
        return ctx

    def _on_context_closed(self, ctx) -> None:
        if self._context is not ctx:
            return
        self._context = None
        self._browser = None
        self._pages.clear()
        self._cdp.clear()
        self._adopted.clear()
        self._blank_pages.clear()
        if not self._closing:
            self._log("Browser was closed")

    def _find_open_tab(self, ctx, url: str):
        """An already-open tab showing exactly this URL (ignoring #fragment and trailing /)
        that no job is using yet."""
        taken = {p for pages in self._pages.values() for p in pages}
        want = _canonical(url)
        for page in ctx.pages:
            if not page.is_closed() and page not in taken and _canonical(page.url) == want:
                return page
        return None

    async def _open_pages(self, job: Job, ctx) -> list:
        """The job's tabs: reuse the ones it already has (a restart), adopt matching tabs the user
        already has open (own-Chrome mode), and open new ones for the rest."""
        pages = self._pages.setdefault(job.id, [])
        pages[:] = [p for p in pages if not p.is_closed()]
        fresh = []  # tabs we still have to navigate

        while len(pages) < job.tabs:
            page = None
            if self._connect_url:
                page = self._find_open_tab(ctx, job.url)
                if page is not None:
                    self._adopted.add(page)
                    pages.append(page)
                    self._log(f"[{job.mode}] attached to your open tab {job.url}")
                    continue
            else:
                while self._blank_pages and page is None:
                    spare = self._blank_pages.pop()
                    if not spare.is_closed():
                        page = spare
            if page is None:
                page = await ctx.new_page()
            pages.append(page)
            fresh.append(page)

        async def load(page):
            try:
                await page.goto(job.url, timeout=LOAD_TIMEOUT_MS)
                self._log(f"[{job.mode}] opened {job.url}")
            except PlaywrightError as e:
                if not page.is_closed():
                    self._log(f"[{job.mode}] open failed for {job.url}: {_short(e)}")

        await asyncio.gather(*(load(p) for p in fresh))
        return pages

    async def _run_job(self, job: Job) -> None:
        try:
            self._emit(job, "starting")
            ctx = await self._ensure_context()
            pages = await self._open_pages(job, ctx)
            # The "Run for" clock starts now that the tabs are open. Wall clock, so a PC that sleeps
            # still stops the job after the real time asked for.
            deadline = time.time() + job.duration_s if job.duration_s else None
            self._emit(job, "running", None, _time_left(deadline))
            timed_out = False
            while not is_finished(job):
                outcome = await self._wait(job, pages, next_delay(job, self._rng), deadline)
                if outcome == "closed":
                    return
                if outcome == "time_up":
                    timed_out = True
                    break
                await self._reload_all(job, pages)
                job.done += 1
                self._emit(job, "running", None, _time_left(deadline))
            why = "finished: time limit reached" if timed_out else "finished"
            self._log(f"[{job.mode}] {job.url} {why} after {job.done} reloads")  # reason first, then status
            self._emit(job, "finished", None, 0.0 if timed_out else _time_left(deadline))
        except asyncio.CancelledError:
            self._emit(job, "stopped")
            self._log(f"[{job.mode}] {job.url} stopped")
            raise
        except Exception as e:
            self._log(f"[{job.mode}] {job.url} failed: {_short(e)}")  # reason first, then the status change
            self._emit(job, "stopped")
        finally:
            if self._tasks.get(job.id) is asyncio.current_task():
                del self._tasks[job.id]

    async def _wait(self, job: Job, pages: list, delay: float, deadline: float | None = None) -> str:
        """Count down to the next reload, ticking once a second.

        Returns "reload" when it is time to reload, "time_up" when the job's "Run for" limit
        (`deadline`, wall-clock time) arrives first (no reload is done then; an exact tie also
        counts as time up), or "closed" when every tab has been closed by the user. Tabs closed
        one by one are just dropped."""
        loop = asyncio.get_running_loop()
        reload_at = loop.time() + delay
        while True:
            alive = [p for p in pages if not p.is_closed()]
            if len(alive) < len(pages):
                self._log(f"[{job.mode}] {job.url} {len(pages) - len(alive)} tab(s) closed, "
                          f"{len(alive)} left")
                pages[:] = alive
            if not pages:
                self._emit(job, "stopped")
                self._log(f"[{job.mode}] {job.url} all tabs were closed, job stopped")
                return "closed"
            until_reload = reload_at - loop.time()
            until_end = None if deadline is None else deadline - time.time()
            if until_end is not None and until_end <= until_reload:
                if until_end <= 0:
                    return "time_up"
                self._emit(job, "running", None, until_end)  # the end comes first: no "next in"
                await asyncio.sleep(min(1.0, until_end))
                continue
            if until_reload <= 0:
                return "reload"
            self._emit(job, "running", until_reload, None if until_end is None else max(0.0, until_end))
            await asyncio.sleep(min(1.0, until_reload))

    async def _reload_all(self, job: Job, pages: list) -> None:
        """Reload every tab at the same time; one slow or failing tab does not hold up the others."""
        n = job.done + 1
        total = len(pages)
        await asyncio.gather(*(self._reload(job, page, n, i + 1, total) for i, page in enumerate(pages)))

    async def _reload(self, job: Job, page, n: int, index: int, total: int) -> None:
        tab = f" tab {index}/{total}" if total > 1 else ""
        try:
            if page.url.startswith("chrome-error://"):
                await page.goto(job.url, timeout=LOAD_TIMEOUT_MS)  # last load failed; retry the URL
            elif job.mode == "hard":
                cdp = await self._cdp_session(page)
                async with page.expect_event("load", timeout=LOAD_TIMEOUT_MS) as info:
                    await cdp.send("Page.reload", {"ignoreCache": True})
                await info.value
            else:
                await page.reload(timeout=LOAD_TIMEOUT_MS)
            self._log(f"[{job.mode}] {job.url}{tab} reload #{n} ok")
        except PlaywrightError as e:
            if not page.is_closed():
                self._log(f"[{job.mode}] {job.url}{tab} reload #{n} failed: {_short(e)}")

    async def _cdp_session(self, page):
        cdp = self._cdp.get(page)
        if cdp is None:
            cdp = await page.context.new_cdp_session(page)
            self._cdp[page] = cdp
        return cdp


def _time_left(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.time())


def _canonical(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def _short(e: Exception) -> str:
    return str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
