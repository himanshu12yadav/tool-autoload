"""`AutoReload.exe --selftest <result-file>`: a no-window check that a packaged build really works.

It opens the GUI toolkit (so missing theme files show up), then drives a headless browser through the
engine: open a page, reload it once. The outcome is written to <result-file> because a windowed
exe has no console. Exit code 0 = everything worked.
"""
from __future__ import annotations

import queue
import sys
import tempfile
import time
import traceback
from pathlib import Path


def run(result_file: str) -> int:
    lines: list[str] = []
    ok = False
    try:
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        root.update()
        root.destroy()
        lines.append("gui toolkit: ok")

        from .engine import Engine, JobEvent, LogEvent
        from .job import Job

        events: queue.Queue = queue.Queue()
        engine = Engine(events, headless=True, profile_dir=Path(tempfile.mkdtemp()))
        try:
            engine.start_job(Job(url="about:blank", interval_s=1, count=1))
            deadline = time.time() + 90
            while time.time() < deadline:
                try:
                    ev = events.get(timeout=0.5)
                except queue.Empty:
                    continue
                if isinstance(ev, LogEvent):
                    lines.append(ev.text)
                elif isinstance(ev, JobEvent) and ev.status == "finished":
                    ok = True
                    break
                elif isinstance(ev, JobEvent) and ev.status == "stopped":
                    break
        finally:
            engine.shutdown()
        lines.append("engine: " + ("ok" if ok else "FAILED (job did not finish)"))
    except Exception:
        lines.append("EXCEPTION:\n" + traceback.format_exc())
    lines.append("SELFTEST " + ("OK" if ok else "FAIL"))
    Path(result_file).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1]))
