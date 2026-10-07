"""Connect-to-my-Chrome mode: the engine attaches to a browser it did not start."""
import json
import queue
import time
import urllib.request

import pytest

from autoreload import chrome
from autoreload.engine import Engine, JobEvent, LogEvent
from autoreload.job import Job

from conftest import free_port, reload_requests


@pytest.fixture
def engine(tmp_path):
    q = queue.Queue()
    eng = Engine(q, headless=True, profile_dir=tmp_path / "own-profile", channels=(None,))
    eng.q = q
    yield eng
    eng.shutdown()


def pump(engine, until, timeout=30.0):
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


def endpoint(port):
    return f"http://127.0.0.1:{port}"


def open_tabs(port):
    with urllib.request.urlopen(f"{endpoint(port)}/json", timeout=5) as r:
        return [t["url"] for t in json.load(r) if t["type"] == "page"]


def open_users_tab(port, url):
    """Open a tab the way a person would, from outside the tool (a headless Chromium does not
    load a URL given on its command line, so use the DevTools HTTP endpoint instead)."""
    req = urllib.request.Request(f"{endpoint(port)}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as resp:
        target = json.load(resp)
    # bring it to the front like a tab a person is looking at; a background tab in a headless
    # browser can sit unloaded for a long time
    urllib.request.urlopen(f"{endpoint(port)}/json/activate/{target['id']}", timeout=10).read()


def page_url(server, name="page"):
    return f"http://127.0.0.1:{server.server_port}/{name}"


def wait_hits(server, n, timeout=15):
    deadline = time.time() + timeout
    while len(server.hits) < n:
        assert time.time() < deadline, f"expected {n} hits, got {len(server.hits)}"
        time.sleep(0.1)


def test_reloads_through_connected_browser_hard_mode(engine, server, start_browser):
    port = start_browser()
    engine.set_connect_url(endpoint(port))
    job = Job(url=page_url(server), interval_s=1, mode="hard", count=2)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))

    assert [e for e in events if isinstance(e, LogEvent) and "Connected to your Chrome" in e.text]
    assert len(server.hits) == 3  # open + 2 reloads
    for _, headers in server.hits[1:]:
        assert headers.get("Cache-Control") == "no-cache"
        assert headers.get("Pragma") == "no-cache"
    assert page_url(server) in open_tabs(port)


def test_attaches_to_existing_tab_without_renavigating(engine, server, start_browser):
    port = start_browser()
    open_users_tab(port, page_url(server))
    wait_hits(server, 1)  # the user's tab loaded the page once

    engine.set_connect_url(endpoint(port))
    job = Job(url=page_url(server), interval_s=1, mode="normal", count=2)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))

    assert [e for e in events if isinstance(e, LogEvent) and "attached" in e.text]
    assert len(server.hits) == 3  # 1 from the browser + 2 reloads, no extra navigation
    assert open_tabs(port).count(page_url(server)) == 1  # no duplicate tab opened


def test_removing_an_attached_job_leaves_the_users_tab_open(engine, server, start_browser):
    port = start_browser()
    open_users_tab(port, page_url(server))
    wait_hits(server, 1)
    engine.set_connect_url(endpoint(port))
    job = Job(url=page_url(server), interval_s=1, count=1)
    engine.start_job(job)
    pump(engine, status(job.id, "finished"))

    engine.remove_job(job.id)
    time.sleep(1.0)
    assert page_url(server) in open_tabs(port)


def test_several_tabs_adopt_the_existing_one_open_the_rest_and_remove_closes_only_its_own(
        engine, server, start_browser):
    port = start_browser()
    open_users_tab(port, page_url(server))
    wait_hits(server, 1)
    engine.set_connect_url(endpoint(port))
    job = Job(url=page_url(server), interval_s=1, count=1, tabs=3)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))

    attached = [e for e in events if isinstance(e, LogEvent) and "attached" in e.text]
    assert len(attached) == 1  # only the user's one existing tab is adopted
    assert open_tabs(port).count(page_url(server)) == 3
    assert len(reload_requests(server)) == 3  # all three tabs, the adopted one included, reloaded once

    engine.remove_job(job.id)
    time.sleep(1.5)
    assert open_tabs(port).count(page_url(server)) == 1  # the two it opened are gone, the user's stays


def test_shutdown_disconnects_but_does_not_quit_the_browser(tmp_path, server, start_browser):
    port = start_browser()
    q = queue.Queue()
    eng = Engine(q, headless=True, profile_dir=tmp_path / "p", channels=(None,))
    eng.q = q
    eng.set_connect_url(endpoint(port))
    job = Job(url=page_url(server), interval_s=1, count=1)
    eng.start_job(job)
    pump(eng, status(job.id, "finished"))

    eng.shutdown()
    assert chrome.is_debug_port_open(port)
    assert page_url(server) in open_tabs(port)  # tabs we opened are left as they were


def test_unreachable_chrome_gives_actionable_message(engine):
    engine.set_connect_url(endpoint(free_port()))
    job = Job(url="https://example.com", interval_s=1, count=1)
    engine.start_job(job)
    events = pump(engine, status(job.id, "stopped"))
    logs = " ".join(e.text for e in events if isinstance(e, LogEvent))
    assert "Open Chrome" in logs


def test_refuses_to_attach_to_a_program_that_is_not_chrome(engine, fake_devtools):
    port = fake_devtools("Edg/154.0.4258.53")  # like Lenovo's WebView2 squatting on port 9222
    engine.set_connect_url(endpoint(port))
    job = Job(url="https://example.com", interval_s=1, count=1)
    engine.start_job(job)
    events = pump(engine, status(job.id, "stopped"))
    logs = " ".join(e.text for e in events if isinstance(e, LogEvent))
    assert "another program" in logs and "Edg/154" in logs
    assert "Connected to your Chrome" not in logs


def test_can_switch_back_to_own_window(engine, server, start_browser):
    port = start_browser()
    engine.set_connect_url(endpoint(port))
    engine.set_connect_url(None)
    job = Job(url=page_url(server), interval_s=1, count=1)
    engine.start_job(job)
    events = pump(engine, status(job.id, "finished"))
    assert [e for e in events if isinstance(e, LogEvent) and "Browser started" in e.text]
