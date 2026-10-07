import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from autoreload import chrome


def test_find_chrome_returns_first_existing(tmp_path):
    missing = tmp_path / "nope" / "chrome.exe"
    real = tmp_path / "chrome.exe"
    real.write_text("x")
    assert chrome.find_chrome([missing, real]) == real


def test_find_chrome_none_when_absent(tmp_path):
    assert chrome.find_chrome([tmp_path / "a.exe", tmp_path / "b.exe"]) is None


def test_build_args_has_port_profile_and_no_automation_flags():
    args = chrome.build_args(Path("C:/c/chrome.exe"), 9333, Path("C:/prof"), "https://x.com")
    assert "--remote-debugging-port=9333" in args
    assert any(a.startswith("--user-data-dir=") and a.endswith("prof") for a in args)
    assert args[-1] == "https://x.com"
    assert not any(flag in args for flag in ("--enable-automation", "--no-sandbox"))


def test_candidate_paths_on_macos_point_inside_the_app_bundle(tmp_path):
    inner = Path("Contents") / "MacOS" / "Google Chrome"
    paths = chrome.candidate_paths("darwin", {}, tmp_path)
    assert Path("/Applications/Google Chrome.app") / inner in paths
    assert tmp_path / "Applications" / "Google Chrome.app" / inner in paths  # per-user install


def test_candidate_paths_on_windows_use_program_files_and_localappdata():
    env = {"PROGRAMFILES": "C:/PF", "PROGRAMFILES(X86)": "C:/PF86", "LOCALAPPDATA": "C:/LA"}
    paths = chrome.candidate_paths("win32", env, Path("C:/home"))
    assert Path("C:/PF/Google/Chrome/Application/chrome.exe") in paths
    assert Path("C:/PF86/Google/Chrome/Application/chrome.exe") in paths
    assert Path("C:/LA/Google/Chrome/Application/chrome.exe") in paths


def test_detach_options_per_platform():
    win = chrome.detach_options("win32")
    assert win.get("creationflags", 0) != 0 or sys.platform != "win32"  # real flags only exist on Windows
    assert "start_new_session" not in win
    mac = chrome.detach_options("darwin")
    assert mac == {"start_new_session": True}  # Chrome must outlive this app on a Mac too


def test_launch_without_chrome_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome, "find_chrome", lambda candidates=None: None)
    with pytest.raises(FileNotFoundError):
        chrome.launch_chrome(9222, tmp_path)


class _VersionHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"Browser": "Chrome/test"}'
        self.send_response(200 if self.path == "/json/version" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_is_debug_port_open_true_for_devtools_like_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _VersionHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert chrome.is_debug_port_open(srv.server_port)
    finally:
        srv.shutdown()
        srv.server_close()


def test_browser_info_returns_version_dict(fake_devtools):
    port = fake_devtools("Chrome/153.0.1.2")
    assert chrome.browser_info(port) == {"Browser": "Chrome/153.0.1.2"}


@pytest.mark.parametrize("browser, expected", [
    ("Chrome/153.0.7000.1", True),
    ("HeadlessChrome/153.0.7000.1", True),
    ("Edg/154.0.4258.53", False),   # what Lenovo's embedded WebView2 reports on port 9222
    ("", False),
])
def test_is_chrome_only_accepts_chrome(browser, expected):
    assert chrome.is_chrome({"Browser": browser}) is expected


def test_is_chrome_handles_missing_field():
    assert chrome.is_chrome({}) is False


def test_non_json_answer_is_not_a_debug_port():
    class Html(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html>hello</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Html)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert chrome.browser_info(srv.server_port) is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_default_port_avoids_9222():
    assert chrome.DEFAULT_PORT != 9222


def test_is_debug_port_open_false_when_nothing_listens():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _VersionHandler)
    port = srv.server_port
    srv.server_close()
    assert not chrome.is_debug_port_open(port, timeout=0.5)
