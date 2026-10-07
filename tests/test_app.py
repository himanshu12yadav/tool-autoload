"""GUI tests: real CustomTkinter window wired to a headless engine."""
import time

import pytest

tk = pytest.importorskip("tkinter")

from autoreload import settings  # noqa: E402
from autoreload.app import App  # noqa: E402
from autoreload.engine import Engine  # noqa: E402
from autoreload.job import parse_duration  # noqa: E402

from conftest import reload_requests  # noqa: E402


def make_app(settings_path, factory):
    # No retry on purpose: Tk start-up failures were caused by pytest's fd capture (see pytest.ini),
    # so a failure here should be loud, not papered over.
    return App(settings_path=settings_path, engine_factory=factory)


@pytest.fixture
def app(tmp_path):
    def factory(events):
        return Engine(events, headless=True, profile_dir=tmp_path / "profile", channels=(None,))

    a = make_app(tmp_path / "settings.json", factory)
    a.withdraw()
    a.settings_file = tmp_path / "settings.json"
    yield a
    if not a._closed:
        a.close()


def fill(app, **kw):
    app.url_var.set(kw.get("url", "https://example.com"))
    app.interval_var.set(str(kw.get("interval", 5)))
    app.jitter_var.set(str(kw.get("jitter", 0)))
    app.count_var.set(str(kw.get("count", 3)))
    app.tabs_var.set(str(kw.get("tabs", 1)))
    app._set_duration_fields(parse_duration(str(kw.get("duration", ""))))  # e.g. "2h", "90s", ""
    app.infinite_var.set(kw.get("infinite", False))
    app.mode_var.set(kw.get("mode", "Normal reload"))


def pump(app, until, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.update()
        if until():
            return
        time.sleep(0.05)
    states = [(r.job.url, r.job.status, r.job.done) for r in app.rows.values()]
    log_tail = app.log.get("1.0", "end").strip().splitlines()[-8:]
    raise AssertionError(f"timed out waiting for GUI state; jobs={states}; log tail={log_tail}")


def test_add_job_creates_row_and_persists(app):
    fill(app, url="example.com/x", interval=7, jitter=2, count=4, mode="Hard reload")
    app.add_job()

    assert len(app.rows) == 1
    job = next(iter(app.rows.values())).job
    assert (job.url, job.interval_s, job.jitter_s, job.count, job.mode) == (
        "https://example.com/x", 7, 2, 4, "hard")
    assert app.error_label.cget("text") == ""

    saved = settings.load(app.settings_file)
    assert [j.url for j in saved.jobs] == ["https://example.com/x"]
    assert saved.form["mode"] == "hard"


@pytest.mark.parametrize("kw, fragment", [
    ({"url": ""}, "URL"),
    ({"interval": "abc"}, "Interval"),
    ({"interval": 0.2}, "interval"),
    ({"jitter": 10, "interval": 5}, "jitter"),
    ({"count": "x"}, "Reloads"),
    ({"count": 0}, "count"),
    ({"tabs": "x"}, "Tabs"),
    ({"tabs": 0}, "tabs"),
    ({"tabs": 99}, "tabs"),
    ({"duration": "5s", "interval": 30}, "Run for"),  # shorter than one interval
])
def test_bad_input_shows_error_and_adds_nothing(app, kw, fragment):
    fill(app, **kw)
    app.add_job()
    assert not app.rows
    assert fragment in app.error_label.cget("text")


def test_tabs_field_is_stored_shown_and_persisted(app):
    fill(app, tabs=4)
    app.add_job()
    row = next(iter(app.rows.values()))
    assert row.job.tabs == 4
    assert "4 tabs" in row.meta.cget("text")
    assert settings.load(app.settings_file).jobs[0].tabs == 4
    assert settings.load(app.settings_file).form["tabs"] == "4"  # the form remembers the last typed value


def test_a_bad_event_does_not_stop_the_event_pump(app, monkeypatch):
    handled, scheduled = [], []

    def flaky(ev):
        if ev == "boom":
            raise RuntimeError("bad event")
        handled.append(ev)

    monkeypatch.setattr(app, "_handle", flaky)
    monkeypatch.setattr(app, "after", lambda ms, fn: scheduled.append((ms, fn)))
    app.events.put("boom")
    app.events.put("fine")
    app._poll()

    assert handled == ["fine"]                 # the event after the bad one was still handled
    assert scheduled and scheduled[0][0] == 100  # and polling carries on
    assert "bad event" in app.log.get("1.0", "end")  # the problem is visible, not silent


def test_run_for_is_stored_shown_and_persisted(app):
    fill(app, interval=5, duration="2h")
    app.add_job()
    row = next(iter(app.rows.values()))
    assert row.job.duration_s == 7200
    assert "for 2h" in row.meta.cget("text")
    saved = settings.load(app.settings_file)
    assert saved.jobs[0].duration_s == 7200
    assert saved.form["duration"] == "2h"


@pytest.mark.parametrize("key, value", [("h", "abc"), ("m", "-5"), ("s", "1.5"), ("d", "2x")])
def test_run_for_boxes_must_hold_whole_numbers(app, key, value):
    fill(app)
    app.duration_vars[key].set(value)
    app.add_job()
    assert not app.rows
    assert "whole numbers" in app.error_label.cget("text")


def test_run_for_boxes_add_up(app):
    app.duration_vars["d"].set("1")
    app.duration_vars["h"].set("2")
    app.duration_vars["m"].set("3")
    app.duration_vars["s"].set("4")
    assert app._duration_seconds() == 93784
    assert app._duration_text() == "1d 2h 3m 4s"


def test_run_for_boxes_all_zero_or_blank_mean_no_limit(app):
    for key in "dhms":
        app.duration_vars[key].set("0")
    assert app._duration_seconds() is None
    for key in "dhms":
        app.duration_vars[key].set("")
    assert app._duration_seconds() is None
    assert app._duration_text() == ""


def test_run_for_boxes_come_back_after_restart(app, tmp_path):
    fill(app, url="https://keep.me", interval=5, duration="1d 2h")
    app.add_job()
    app.close()

    second = make_app(tmp_path / "settings.json",
                      lambda q: Engine(q, headless=True, profile_dir=tmp_path / "p3", channels=(None,)))
    try:
        second.withdraw()
        assert {k: v.get() for k, v in second.duration_vars.items()} == {"d": "1", "h": "2", "m": "0", "s": "0"}
    finally:
        second.close()


def test_job_without_a_limit_does_not_mention_one(app):
    fill(app, duration="")
    app.add_job()
    row = next(iter(app.rows.values()))
    assert row.job.duration_s is None
    assert " for " not in row.meta.cget("text")


def test_row_shows_time_left_while_running(app):
    from autoreload.engine import JobEvent
    fill(app, interval=5, duration="2h")
    app.add_job()
    row = next(iter(app.rows.values()))

    app._handle(JobEvent(row.job.id, "running", 1, 4.2, 3724.5))
    text = row.state.cget("text")
    assert "next in 5s" in text
    assert "1h 2m 5s left" in text

    app._handle(JobEvent(row.job.id, "finished", 3, None, 0.0))
    assert "left" not in row.state.cget("text")


def test_job_with_a_time_limit_finishes_by_itself_through_the_gui(app, server):
    fill(app, url=f"http://127.0.0.1:{server.server_port}/page", interval=1, infinite=True, duration="3s")
    app.add_job()
    row = next(iter(app.rows.values()))
    app._start_row(row)
    pump(app, lambda: row.job.status == "finished")

    assert row.job.done == 2  # reloads at ~1s and ~2s; the third would fall at or after the 3s limit
    assert "time limit reached" in app.log.get("1.0", "end")
    assert str(row.start_btn.cget("state")) == "normal"


def test_single_tab_job_does_not_mention_tabs(app):
    fill(app, tabs=1)
    app.add_job()
    assert "tab" not in next(iter(app.rows.values())).meta.cget("text")


def test_job_with_two_tabs_runs_through_the_gui(app, server):
    fill(app, url=f"http://127.0.0.1:{server.server_port}/page", interval=1, count=2, tabs=2)
    app.add_job()
    row = next(iter(app.rows.values()))
    app._start_row(row)
    pump(app, lambda: row.job.status == "finished")
    assert row.job.done == 2
    assert len(reload_requests(server)) == 2 * 2  # two tabs, reloaded in both cycles


def test_infinite_ignores_count_field(app):
    fill(app, count="garbage", infinite=True)
    app.add_job()
    assert next(iter(app.rows.values())).job.count is None


def test_remove_row_updates_settings(app):
    fill(app)
    app.add_job()
    row = next(iter(app.rows.values()))
    app._remove_row(row)
    assert not app.rows
    assert settings.load(app.settings_file).jobs == []


def test_settings_restored_on_next_launch(app, tmp_path):
    fill(app, url="https://keep.me", count=9)
    app.add_job()
    app.close()

    second = make_app(tmp_path / "settings.json",
                      lambda q: Engine(q, headless=True, profile_dir=tmp_path / "p2", channels=(None,)))
    try:
        second.withdraw()
        assert [r.job.url for r in second.rows.values()] == ["https://keep.me"]
        assert second.url_var.get() == "https://keep.me"
    finally:
        second.close()


def test_browser_mode_is_applied_and_persisted(app):
    app.mine_var.set(True)
    app.port_var.set("9333")
    app._on_browser_mode()

    assert app._applied_url == "http://127.0.0.1:9333"
    saved = settings.load(app.settings_file)
    assert saved.form["browser_mode"] == "mine"
    assert saved.form["debug_port"] == "9333"


@pytest.mark.parametrize("port", ["abc", "80", "99999", ""])
def test_bad_port_is_rejected(app, port):
    app.mine_var.set(True)
    app.port_var.set(port)
    app._on_browser_mode()
    assert "Port" in app.error_label.cget("text")
    assert app._applied_url is None


def test_browser_mode_cannot_change_while_a_job_runs(app):
    fill(app)
    app.add_job()
    next(iter(app.rows.values())).job.status = "running"

    app.mine_var.set(True)
    app._on_browser_mode()

    assert app.mine_var.get() is False  # reverted
    assert "Stop all jobs" in app.error_label.cget("text")
    assert app._applied_url is None


def test_open_chrome_launches_with_dedicated_profile(app, monkeypatch):
    calls = []
    monkeypatch.setattr("autoreload.app.chrome.browser_info", lambda port, timeout=1.0: None)
    monkeypatch.setattr("autoreload.app.chrome.launch_chrome",
                        lambda port, profile_dir, *a, **k: calls.append((port, profile_dir)))
    app.port_var.set("9444")
    app.open_chrome()

    assert len(calls) == 1
    port, profile = calls[0]
    assert port == 9444
    assert profile.name == "chrome-profile"
    assert "Opened Chrome" in app.log.get("1.0", "end")


def test_open_chrome_does_nothing_if_chrome_already_running(app, monkeypatch, fake_devtools):
    calls = []
    monkeypatch.setattr("autoreload.app.chrome.launch_chrome", lambda *a, **k: calls.append(a))
    app.port_var.set(str(fake_devtools("Chrome/153.0.1.2")))
    app.open_chrome()
    assert calls == []
    assert "already running" in app.log.get("1.0", "end")


def test_open_chrome_warns_when_another_program_owns_the_port(app, monkeypatch, fake_devtools):
    calls = []
    monkeypatch.setattr("autoreload.app.chrome.launch_chrome", lambda *a, **k: calls.append(a))
    app.port_var.set(str(fake_devtools("Edg/154.0.4258.53")))
    app.open_chrome()
    assert calls == []
    text = app.error_label.cget("text")
    assert "another program" in text and "Edg/154" in text


def test_open_chrome_reports_missing_chrome(app, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("Google Chrome was not found")

    monkeypatch.setattr("autoreload.app.chrome.browser_info", lambda port, timeout=1.0: None)
    monkeypatch.setattr("autoreload.app.chrome.launch_chrome", boom)
    app.open_chrome()
    assert "Chrome was not found" in app.error_label.cget("text")


def test_job_runs_in_users_chrome_through_the_gui(app, server, start_browser):
    port = start_browser()
    app.mine_var.set(True)
    app.port_var.set(str(port))
    fill(app, url=f"http://127.0.0.1:{server.server_port}/page", interval=1, count=2, mode="Hard reload")
    app.add_job()
    row = next(iter(app.rows.values()))

    app._start_row(row)
    pump(app, lambda: row.job.status == "finished")

    assert app._applied_url == f"http://127.0.0.1:{port}"
    assert row.job.done == 2
    assert "Connected to your Chrome" in app.log.get("1.0", "end")
    assert len(server.hits) == 3
    assert all(h.get("Cache-Control") == "no-cache" for _, h in server.hits[1:])


def test_start_runs_job_to_completion_through_the_gui(app, server):
    fill(app, url=f"http://127.0.0.1:{server.server_port}/page", interval=1, count=2, mode="Hard reload")
    app.add_job()
    row = next(iter(app.rows.values()))

    app._start_row(row)
    assert row.job.status == "starting"
    pump(app, lambda: row.job.status == "finished")

    assert row.job.done == 2
    assert len(server.hits) == 3
    assert "reloads 2/2" in row.meta.cget("text")
    assert "reload #2 ok" in app.log.get("1.0", "end")
    assert str(row.start_btn.cget("state")) == "normal"
    assert str(row.stop_btn.cget("state")) == "disabled"


def test_stop_button_stops_infinite_job(app, server):
    fill(app, url=f"http://127.0.0.1:{server.server_port}/page", interval=1, infinite=True)
    app.add_job()
    row = next(iter(app.rows.values()))

    app._start_row(row)
    pump(app, lambda: row.job.status == "running" and row.job.done >= 1)
    app._stop_row(row)
    pump(app, lambda: row.job.status == "stopped")
    assert str(row.start_btn.cget("state")) == "normal"
