"""Start the user's own Chrome with a local remote-debugging port.

Chrome started this way carries no automation flags, so sites that reject automated browsers
(Google sign-in) accept it. Since Chrome 136 remote debugging needs a non-default profile
directory, so we always pass our own --user-data-dir.
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

DEFAULT_PORT = 9333  # not 9222: that one is commonly taken (e.g. by vendor apps embedding WebView2)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def candidate_paths(platform: str | None = None, env=None, home: Path | None = None) -> list[Path]:
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = Path(home) if home else Path.home()
    if platform == "darwin":
        inner = Path("Google Chrome.app") / "Contents" / "MacOS" / "Google Chrome"
        return [Path("/Applications") / inner, home / "Applications" / inner]
    if platform.startswith("win"):
        roots = [env.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        return [Path(r) / "Google" / "Chrome" / "Application" / "chrome.exe" for r in roots if r]
    return []


def detach_options(platform: str | None = None) -> dict:
    """Popen options that let Chrome keep running after this tool exits."""
    if (platform or sys.platform).startswith("win"):
        return {"creationflags": _DETACHED}
    return {"start_new_session": True}


def find_chrome(candidates=None) -> Path | None:
    for path in candidate_paths() if candidates is None else candidates:
        if Path(path).is_file():
            return Path(path)
    return None


def build_args(chrome: Path, port: int, profile_dir: Path, start_url: str | None = None) -> list[str]:
    args = [
        str(chrome),
        f"--remote-debugging-port={port}",  # Chrome binds this to 127.0.0.1 only
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if start_url:
        args.append(start_url)
    return args


def browser_info(port: int, timeout: float = 1.0) -> dict | None:
    """What the DevTools endpoint on this port says about itself, or None if nothing DevTools-like answers."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as resp:
            data = json.load(resp)
    except (OSError, http.client.HTTPException, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_chrome(info: dict) -> bool:
    """True only for Chrome. Other programs also expose DevTools ports (e.g. Edge WebView2 inside
    vendor apps), and we must never attach to those by accident."""
    return str(info.get("Browser", "")).startswith(("Chrome/", "HeadlessChrome/"))


def is_debug_port_open(port: int, timeout: float = 1.0) -> bool:
    return browser_info(port, timeout) is not None


def launch_chrome(port: int, profile_dir: Path, chrome: Path | None = None,
                  start_url: str | None = None) -> subprocess.Popen:
    """Start Chrome detached, so closing this tool does not close the browser."""
    exe = chrome or find_chrome()
    if exe is None:
        raise FileNotFoundError("Google Chrome was not found")
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        build_args(exe, port, Path(profile_dir), start_url),
        **detach_options(),
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
