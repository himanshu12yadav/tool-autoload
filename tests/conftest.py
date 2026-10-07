import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import sync_playwright

from autoreload import chrome

PAGE = b"<html><body>hello</body></html>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/page"):
            self.send_response(404)
            self.end_headers()
            return
        self.server.hits.append((self.path, dict(self.headers)))
        if self.headers.get("If-None-Match") == '"v1"':
            self.send_response(304)
            self.send_header("ETag", '"v1"')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("ETag", '"v1"')
        self.send_header("Cache-Control", "max-age=3600")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def chromium_exe():
    with sync_playwright() as p:
        return p.chromium.executable_path


@pytest.fixture
def start_browser(chromium_exe, tmp_path):
    """Factory: start a real headless Chromium with a debug port (stands in for the user's Chrome)
    and return its port. Browsers are stopped by exact PID at teardown."""
    procs = []

    def start():
        port = free_port()
        args = chrome.build_args(chromium_exe, port, tmp_path / f"prof{port}", "about:blank")
        procs.append(subprocess.Popen(args + ["--headless=new"], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL))
        deadline = time.time() + 20
        while not chrome.is_debug_port_open(port):
            assert time.time() < deadline, "test browser did not start"
            time.sleep(0.2)
        return port

    yield start
    for p in procs:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)


@pytest.fixture
def fake_devtools():
    """Factory: a tiny server that answers /json/version like a DevTools endpoint claiming to be
    `browser` (e.g. 'Edg/154.0'). Returns the port; servers are stopped at teardown."""
    servers = []

    def start(browser):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/json/version":
                    body = ('{"Browser": "%s"}' % browser).encode()
                    self.send_response(200)
                else:
                    body = b"{}"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv.server_port

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def reload_requests(server):
    """Requests that came from a reload. Reloads carry a Cache-Control request header; plain
    navigations do not. Opening several tabs on one URL at once is not counted: Chromium fetches
    it once and serves the other tabs from its cache."""
    return [headers for _, headers in server.hits if "Cache-Control" in headers]


@pytest.fixture
def server():
    """Local HTTP server; `server.hits` is a list of (path, request-headers) per /page* request."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.hits = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()
