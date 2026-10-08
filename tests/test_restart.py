"""Start -> Stop -> Start again, in your-own-Chrome mode: the second run must work like the first."""
import queue
import time

import pytest

from autoreload.engine import Engine, JobEvent
from autoreload.job import Job

from conftest import reload_requests
from test_engine_connect import endpoint, open_tabs, page_url, pump, status


@pytest.fixture
def engine(tmp_path):
    q = queue.Queue()
    eng = Engine(q, headless=True, profile_dir=tmp_path / "own-profile", channels=(None,))
    eng.q = q
    yield eng
    eng.shutdown()


def wait_reloads(server, n, timeout=20):
    deadline = time.time() + timeout
    while len(reload_requests(server)) < n:
        assert time.time() < deadline, f"expected {n} reloads, got {len(reload_requests(server))}"
        time.sleep(0.1)


def running_with_a_reload(job_id):
    return lambda ev: isinstance(ev, JobEvent) and ev.job_id == job_id and ev.status == "running" and ev.done >= 1


@pytest.mark.parametrize("mode, tabs", [("normal", 1), ("hard", 1), ("normal", 3), ("hard", 3)])
def test_restart_after_stop_reloads_again(engine, server, start_browser, mode, tabs):
    engine.set_connect_url(endpoint(start_browser()))
    job = Job(url=page_url(server), interval_s=1, mode=mode, tabs=tabs)  # infinite
    engine.start_job(job)
    pump(engine, running_with_a_reload(job.id))
    engine.stop_job(job.id)
    pump(engine, status(job.id, "stopped"))
    before = len(reload_requests(server))

    engine.start_job(job)
    pump(engine, running_with_a_reload(job.id))
    wait_reloads(server, before + tabs)  # the second run really reloads the page, every tab


def test_restart_with_a_reload_count_counts_from_zero(engine, server, start_browser):
    engine.set_connect_url(endpoint(start_browser()))
    job = Job(url=page_url(server), interval_s=1, count=3)
    engine.start_job(job)
    pump(engine, running_with_a_reload(job.id))
    engine.stop_job(job.id)
    pump(engine, status(job.id, "stopped"))
    before = len(reload_requests(server))

    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))
    assert [e.done for e in events if isinstance(e, JobEvent) and e.status == "finished"] == [3]
    assert len(reload_requests(server)) - before == 3


def test_restart_with_a_run_for_timer_gets_a_fresh_clock(engine, server, start_browser):
    engine.set_connect_url(endpoint(start_browser()))
    job = Job(url=page_url(server), interval_s=1, duration_s=4)
    engine.start_job(job)
    pump(engine, running_with_a_reload(job.id))
    engine.stop_job(job.id)
    pump(engine, status(job.id, "stopped"))

    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))
    left = [e.limit_remaining for e in events if isinstance(e, JobEvent) and e.status == "running"]
    assert left and max(left) > 2  # the second run started with (nearly) the full 4s, not what was left


def test_start_right_after_stop_is_not_swallowed(engine, server, start_browser):
    engine.set_connect_url(endpoint(start_browser()))
    job = Job(url=page_url(server), interval_s=1)
    engine.start_job(job)
    pump(engine, running_with_a_reload(job.id))
    engine.stop_job(job.id)
    engine.start_job(job)  # no waiting for "stopped"
    events = pump(engine, lambda ev: isinstance(ev, JobEvent) and ev.status == "running" and ev.done >= 1)
    assert events  # the job is running again after the stop settled
    engine.stop_job(job.id)
    pump(engine, status(job.id, "stopped"))


def test_stop_twice_then_start_twice(engine, server, start_browser):
    engine.set_connect_url(endpoint(start_browser()))
    job = Job(url=page_url(server), interval_s=1)
    engine.start_job(job)
    pump(engine, status(job.id, "running"))
    engine.stop_job(job.id)
    engine.stop_job(job.id)
    pump(engine, status(job.id, "stopped"))
    engine.start_job(job)
    engine.start_job(job)  # second one must be ignored, not start a second loop
    pump(engine, running_with_a_reload(job.id))
    before = len(reload_requests(server))
    time.sleep(3.2)
    gained = len(reload_requests(server)) - before
    assert 2 <= gained <= 4  # ~1 reload a second: one loop, not two


def test_restart_after_the_tab_was_closed_in_chrome(engine, server, start_browser):
    port = start_browser()
    engine.set_connect_url(endpoint(port))
    job = Job(url=page_url(server), interval_s=1)
    engine.start_job(job)
    pump(engine, running_with_a_reload(job.id))
    engine.stop_job(job.id)
    pump(engine, status(job.id, "stopped"))
    # the person closes the job's tab while it is stopped
    import json
    import urllib.request
    with urllib.request.urlopen(f"{endpoint(port)}/json", timeout=5) as r:
        for t in json.load(r):
            if t["type"] == "page" and t["url"] == page_url(server):
                urllib.request.urlopen(f"{endpoint(port)}/json/close/{t['id']}", timeout=5).read()
    time.sleep(0.5)
    before = len(reload_requests(server))

    engine.start_job(job)
    pump(engine, running_with_a_reload(job.id))
    assert page_url(server) in open_tabs(port)
    assert len(reload_requests(server)) > before


# ---- through the real window (buttons, row state), "Use my own Chrome" ticked --------------------

from test_app import app, fill, pump as pump_gui  # noqa: E402,F401  (app is a fixture)


def _own_chrome_row(app, port, **kw):
    app.mine_var.set(True)
    app.port_var.set(str(port))
    fill(app, **kw)
    app.add_job()
    return next(iter(app.rows.values()))


@pytest.mark.parametrize("mode", ["Normal reload", "Hard reload"])
def test_gui_start_stop_start_again_in_own_chrome(app, server, start_browser, mode):
    row = _own_chrome_row(app, start_browser(), url=page_url(server), interval=1, infinite=True, mode=mode)

    app._start_row(row)
    pump_gui(app, lambda: row.job.status == "running" and row.job.done >= 1)
    app._stop_row(row)
    pump_gui(app, lambda: row.job.status == "stopped")
    assert row.start_btn.cget("state") == "normal" and row.stop_btn.cget("state") == "disabled"
    before = len(reload_requests(server))

    app._start_row(row)
    assert row.job.done == 0 and row.job.status == "starting"
    pump_gui(app, lambda: row.job.status == "running" and row.job.done >= 1)
    assert len(reload_requests(server)) > before
    assert row.stop_btn.cget("state") == "normal" and row.start_btn.cget("state") == "disabled"
