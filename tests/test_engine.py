"""Integration tests: real headless Chromium against a local HTTP server."""
import asyncio
import queue
import time

import pytest

from autoreload.engine import Engine, JobEvent, LogEvent
from autoreload.job import Job

from conftest import reload_requests


@pytest.fixture
def engine(tmp_path):
    q = queue.Queue()
    eng = Engine(q, headless=True, profile_dir=tmp_path / "profile", channels=(None,))
    eng.q = q
    yield eng
    eng.shutdown()


def url(server, name="page"):
    return f"http://127.0.0.1:{server.server_port}/{name}"


def pump(engine, until, timeout=30.0):
    """Drain the event queue until `until(event)` is true; returns all events seen."""
    seen = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ev = engine.q.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(ev)
        if until(ev):
            return seen
    raise AssertionError(f"timed out; events: {seen[-8:]}")


def status(job_id, st):
    return lambda ev: isinstance(ev, JobEvent) and ev.job_id == job_id and ev.status == st


def test_normal_reload_runs_count_then_finishes(engine, server):
    job = Job(url=url(server), interval_s=1, mode="normal", count=3)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))

    final = [e for e in events if isinstance(e, JobEvent) and e.status == "finished"][-1]
    assert final.done == 3
    # initial load + 3 reloads reach the server; a normal reload revalidates (max-age=0)
    assert len(server.hits) == 4
    for _, headers in server.hits[1:]:
        assert headers.get("Cache-Control") == "max-age=0"
        assert "Pragma" not in headers


def test_hard_reload_bypasses_cache(engine, server):
    job = Job(url=url(server), interval_s=1, mode="hard", count=3)
    engine.start_job(job)
    pump(engine, status(job.id, "finished"))

    assert len(server.hits) == 4
    for _, headers in server.hits[1:]:
        assert headers.get("Cache-Control") == "no-cache"
        assert headers.get("Pragma") == "no-cache"
        assert "If-None-Match" not in headers


def test_infinite_job_stops_on_request_and_stays_stopped(engine, server):
    job = Job(url=url(server), interval_s=1, mode="normal", count=None)
    engine.start_job(job)
    pump(engine, lambda e: isinstance(e, JobEvent) and e.done >= 1)
    engine.stop_job(job.id)
    pump(engine, status(job.id, "stopped"))

    hits_after_stop = len(server.hits)
    time.sleep(2.5)
    assert len(server.hits) == hits_after_stop


def test_jobs_run_independently(engine, server):
    fast = Job(url=url(server, "page?fast"), interval_s=1, count=3)
    slow = Job(url=url(server, "page?slow"), interval_s=1, count=None)
    engine.start_job(fast)
    engine.start_job(slow)
    pump(engine, status(fast.id, "finished"))
    engine.stop_job(slow.id)
    pump(engine, status(slow.id, "stopped"))

    fast_hits = [p for p, _ in server.hits if p.endswith("fast")]
    assert len(fast_hits) == 4


def test_closing_the_tab_stops_the_job(engine, server):
    job = Job(url=url(server), interval_s=2, count=None)
    engine.start_job(job)
    pump(engine, status(job.id, "running"))  # emitted only after the page is open

    page = engine._pages[job.id][0]
    asyncio.run_coroutine_threadsafe(page.close(), engine._loop).result(10)

    events = pump(engine, status(job.id, "stopped"))
    assert events


def test_job_with_several_tabs_reloads_every_tab_each_cycle(engine, server):
    job = Job(url=url(server), interval_s=1, mode="hard", count=2, tabs=3)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))

    final = [e for e in events if isinstance(e, JobEvent) and e.status == "finished"][-1]
    assert final.done == 2  # counts cycles, not individual tab reloads
    reloads = reload_requests(server)
    assert len(reloads) == 3 * 2  # every one of the 3 tabs reloaded in both cycles
    assert all(h.get("Cache-Control") == "no-cache" for h in reloads)  # all hard reloads


# Timing note for the time-limit tests: with interval 1 s and no jitter, reloads fall at about
# 1 s, 2 s, 3 s (plus a few ms each), so a 3.7 s limit allows exactly three and a 2.5 s limit two.

def finished_event(events, job_id):
    return [e for e in events if isinstance(e, JobEvent) and e.job_id == job_id and e.status == "finished"][-1]


def log_text(events):
    return " ".join(e.text for e in events if isinstance(e, LogEvent))


def test_time_limit_stops_an_infinite_job_and_does_not_reload_after_the_end(engine, server):
    job = Job(url=url(server), interval_s=1, count=None, duration_s=3.7)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))

    assert finished_event(events, job.id).done == 3
    assert "time limit reached" in log_text(events)
    assert len(reload_requests(server)) == 3
    time.sleep(1.5)
    assert len(reload_requests(server)) == 3  # nothing happens after the limit


def test_count_wins_when_it_comes_first(engine, server):
    job = Job(url=url(server), interval_s=1, count=2, duration_s=60)
    started = time.time()
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))

    assert finished_event(events, job.id).done == 2
    assert time.time() - started < 30  # finished by count long before the 60 s limit
    assert "time limit reached" not in log_text(events)


def test_time_wins_when_it_comes_before_the_count(engine, server):
    job = Job(url=url(server), interval_s=1, count=50, duration_s=3.7)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))

    assert finished_event(events, job.id).done == 3
    assert "time limit reached" in log_text(events)


def test_events_report_time_left_counting_down(engine, server):
    job = Job(url=url(server), interval_s=1, count=None, duration_s=3.7)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))

    left = [e.limit_remaining for e in events if isinstance(e, JobEvent) and e.limit_remaining is not None]
    assert left and left[0] <= 3.7 + 0.01  # tolerance: time.time() has ~0.2 microsecond resolution
    assert left == sorted(left, reverse=True)  # never goes up
    assert left[-1] < 1.5


def test_job_without_a_limit_reports_no_time_left(engine, server):
    job = Job(url=url(server), interval_s=1, count=1)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))
    assert all(e.limit_remaining is None for e in events if isinstance(e, JobEvent))


def test_time_limit_belongs_to_the_job_not_each_tab(engine, server):
    job = Job(url=url(server), interval_s=1, mode="hard", count=None, tabs=3, duration_s=3.7)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))

    assert finished_event(events, job.id).done == 3
    assert len(reload_requests(server)) == 3 * 3  # 3 cycles x 3 tabs


def test_starting_again_restarts_the_clock(engine, server):
    job = Job(url=url(server), interval_s=1, count=None, duration_s=2.5)
    engine.start_job(job)
    first = pump(engine, status(job.id, "finished"))
    assert finished_event(first, job.id).done == 2

    engine.start_job(job)
    second = pump(engine, status(job.id, "finished"))
    assert finished_event(second, job.id).done == 2  # a full new run, not an instant finish


def test_closing_some_tabs_keeps_the_job_going_until_none_are_left(engine, server):
    job = Job(url=url(server), interval_s=1, count=None, tabs=3)
    engine.start_job(job)
    pump(engine, status(job.id, "running"))

    def close(page):
        asyncio.run_coroutine_threadsafe(page.close(), engine._loop).result(10)

    pages = list(engine._pages[job.id])
    assert len(pages) == 3
    close(pages[0])
    events = pump(engine, lambda e: isinstance(e, JobEvent) and e.status == "running" and e.done >= 2)
    assert not [e for e in events if isinstance(e, JobEvent) and e.status == "stopped"]

    close(pages[1])
    close(pages[2])
    pump(engine, status(job.id, "stopped"))


def test_unreachable_url_is_logged_and_job_keeps_going(engine):
    job = Job(url="http://127.0.0.1:9/nothing", interval_s=1, count=2)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"), timeout=60)
    logs = [e.text for e in events if isinstance(e, LogEvent)]
    assert any("failed" in t for t in logs)
