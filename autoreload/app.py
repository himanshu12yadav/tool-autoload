"""CustomTkinter front end. Talks to Engine only through its thread-safe API and event queue."""
from __future__ import annotations

import math
import queue
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

from . import chrome, settings
from .engine import Engine, JobEvent, LogEvent
from .job import Job, format_duration, normalize_url, parse_duration, validate

MODE_LABELS = {"normal": "Normal reload", "hard": "Hard reload"}
LABEL_TO_MODE = {v: k for k, v in MODE_LABELS.items()}
ACTIVE = ("starting", "running")
STATUS_COLORS = {
    "running": ("#1a7f37", "#3fb950"),
    "starting": ("#9a6700", "#d29922"),
    "finished": ("#0969da", "#58a6ff"),
    "stopped": ("gray40", "gray60"),
    "idle": ("gray40", "gray60"),
}
MAX_LOG_LINES = 1000
# "Run for" boxes: (key, label, dropdown choices). Typing any whole number also works.
DURATION_FIELDS = (
    ("d", "days", [str(n) for n in (0, 1, 2, 3, 5, 7, 14, 30)]),
    ("h", "hours", [str(n) for n in (0, 1, 2, 3, 4, 6, 8, 12, 24)]),
    ("m", "min", [str(n) for n in (0, 1, 5, 10, 15, 20, 30, 45)]),
    ("s", "sec", [str(n) for n in (0, 5, 10, 15, 30, 45)]),
)
_DURATION_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _fmt_seconds(value: float) -> str:
    return f"{value:g}s"


class JobRow:
    def __init__(self, parent, job: Job, on_start, on_stop, on_remove):
        self.job = job
        self.remaining: float | None = None
        self.time_left: float | None = None  # until the job's "Run for" limit ends it

        self.frame = ctk.CTkFrame(parent)
        self.frame.pack(fill="x", padx=4, pady=3)
        self.frame.grid_columnconfigure(0, weight=1)

        shown = job.url if len(job.url) <= 70 else job.url[:67] + "..."
        ctk.CTkLabel(self.frame, text=shown, anchor="w", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=10, pady=(6, 0))
        self.meta = ctk.CTkLabel(self.frame, anchor="w", text_color=("gray35", "gray65"))
        self.meta.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))

        self.state = ctk.CTkLabel(self.frame, width=170, anchor="e")
        self.state.grid(row=0, column=1, rowspan=2, padx=6)

        self.start_btn = ctk.CTkButton(self.frame, text="Start", width=64, command=lambda: on_start(self))
        self.start_btn.grid(row=0, column=2, rowspan=2, padx=3)
        self.stop_btn = ctk.CTkButton(self.frame, text="Stop", width=64, command=lambda: on_stop(self),
                                      fg_color="#b62324", hover_color="#8e1a1b")
        self.stop_btn.grid(row=0, column=3, rowspan=2, padx=3)
        self.remove_btn = ctk.CTkButton(self.frame, text="Remove", width=70, command=lambda: on_remove(self),
                                        fg_color="gray45", hover_color="gray30")
        self.remove_btn.grid(row=0, column=4, rowspan=2, padx=(3, 10))
        self.refresh()

    def refresh(self) -> None:
        job = self.job
        total = "∞" if job.count is None else str(job.count)
        jitter = f" ± {_fmt_seconds(job.jitter_s)}" if job.jitter_s else ""
        tabs = f"  ·  {job.tabs} tabs" if job.tabs > 1 else ""
        limit = f"  ·  for {format_duration(job.duration_s)}" if job.duration_s else ""
        self.meta.configure(
            text=f"{MODE_LABELS[job.mode]}  ·  every {_fmt_seconds(job.interval_s)}{jitter}{tabs}{limit}"
                 f"  ·  reloads {job.done}/{total}")

        text = job.status
        if job.status == "running" and self.remaining is not None:
            text += f"  ·  next in {math.ceil(self.remaining)}s"
        if job.status == "running" and self.time_left is not None:
            text += f"  ·  {format_duration(math.ceil(self.time_left))} left"
        self.state.configure(text=text, text_color=STATUS_COLORS.get(job.status, STATUS_COLORS["idle"]))

        active = job.status in ACTIVE
        self.start_btn.configure(state="disabled" if active else "normal")
        self.stop_btn.configure(state="normal" if active else "disabled")

    def destroy(self) -> None:
        self.frame.destroy()


class App(ctk.CTk):
    def __init__(self, settings_path: Path | None = None, engine_factory=Engine):
        super().__init__()
        self.title("Auto Reload")
        self._fit_to_screen(920, 720)
        self.minsize(700, 520)

        self._settings_path = Path(settings_path) if settings_path else settings.default_path()
        self._closed = False
        self.events: queue.Queue = queue.Queue()
        self.engine = engine_factory(self.events)
        self.rows: dict[str, JobRow] = {}
        self._applied_url: str | None = None  # browser mode the engine is currently set to

        loaded = settings.load(self._settings_path)
        self._build_form(loaded.form)
        self._build_jobs_and_log()
        for job in loaded.jobs:
            self._add_row(job)
        self._apply_browser_mode()

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self._poll)

    def _fit_to_screen(self, width: int, height: int) -> None:
        """Open centred, shrunk to fit small or display-scaled screens (CTk geometry is unscaled)."""
        scale = ctk.ScalingTracker.get_window_scaling(self)
        avail_w = int(self.winfo_screenwidth() / scale)
        avail_h = int(self.winfo_screenheight() / scale)
        w = min(width, avail_w - 40)
        h = min(height, avail_h - 120)  # leave room for the taskbar and title bar
        x = max(0, (avail_w - w) // 2)
        y = max(0, (avail_h - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{x}+{y}")

    # ---- layout --------------------------------------------------------------------

    def _build_form(self, form: dict) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=3)
        self.grid_rowconfigure(3, weight=2)

        box = ctk.CTkFrame(self)
        box.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        box.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(box, text="URL").grid(row=0, column=0, padx=(12, 6), pady=(12, 6), sticky="w")
        self.url_var = ctk.StringVar(value=form["url"])
        self.url_entry = ctk.CTkEntry(box, textvariable=self.url_var, placeholder_text="https://example.com/page")
        self.url_entry.grid(row=0, column=1, columnspan=5, padx=(0, 12), pady=(12, 6), sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self.add_job())
        self.tabs_var = ctk.StringVar(value=str(form["tabs"]))
        ctk.CTkLabel(box, text="Tabs").grid(row=0, column=6, padx=(0, 6), pady=(12, 6), sticky="e")
        ctk.CTkEntry(box, textvariable=self.tabs_var, width=60).grid(row=0, column=7, padx=(0, 12),
                                                                     pady=(12, 6), sticky="w")

        self.interval_var = ctk.StringVar(value=str(form["interval_s"]))
        self.jitter_var = ctk.StringVar(value=str(form["jitter_s"]))
        self.count_var = ctk.StringVar(value=str(form["count"]))
        self.infinite_var = ctk.BooleanVar(value=bool(form["infinite"]))
        self.mode_var = ctk.StringVar(value=MODE_LABELS.get(form["mode"], MODE_LABELS["normal"]))

        ctk.CTkLabel(box, text="Every (s)").grid(row=1, column=0, padx=(12, 6), pady=(0, 12), sticky="w")
        ctk.CTkEntry(box, textvariable=self.interval_var, width=70).grid(row=1, column=1, pady=(0, 6), sticky="w")
        ctk.CTkLabel(box, text="± Jitter (s)").grid(row=1, column=2, padx=(12, 6), pady=(0, 6))
        ctk.CTkEntry(box, textvariable=self.jitter_var, width=60).grid(row=1, column=3, pady=(0, 6))
        ctk.CTkOptionMenu(box, values=list(MODE_LABELS.values()), variable=self.mode_var, width=130).grid(
            row=1, column=4, padx=12, pady=(0, 6))
        ctk.CTkLabel(box, text="Reloads").grid(row=1, column=5, padx=(0, 6), pady=(0, 6))
        self.count_entry = ctk.CTkEntry(box, textvariable=self.count_var, width=60)
        self.count_entry.grid(row=1, column=6, pady=(0, 6))
        ctk.CTkCheckBox(box, text="Infinite", variable=self.infinite_var, command=self._sync_count_state,
                        width=20).grid(row=1, column=7, padx=12, pady=(0, 6))

        ctk.CTkLabel(box, text="Run for").grid(row=2, column=0, padx=(12, 6), pady=(0, 6), sticky="w")
        runfor = ctk.CTkFrame(box, fg_color="transparent")
        runfor.grid(row=2, column=1, columnspan=7, pady=(0, 6), sticky="w")
        self.duration_vars: dict[str, ctk.StringVar] = {}
        self.duration_boxes: list[ctk.CTkComboBox] = []
        for key, label, choices in DURATION_FIELDS:
            var = ctk.StringVar(value="0")
            self.duration_vars[key] = var
            box_ = ctk.CTkComboBox(runfor, values=choices, variable=var, width=66)
            box_.pack(side="left")
            self.duration_boxes.append(box_)
            ctk.CTkLabel(runfor, text=label).pack(side="left", padx=(4, 12))
        ctk.CTkLabel(runfor, text="all 0 = no limit · disabled while Reloads is above 0",
                     text_color=("gray35", "gray65")).pack(side="left", padx=(4, 0))
        try:
            self._set_duration_fields(parse_duration(form["duration"]))
        except ValueError:
            pass  # unreadable saved text: leave the fields at 0 (no limit)
        for var in (self.count_var, self.infinite_var):
            var.trace_add("write", lambda *_: self._sync_count_state())
        self._sync_count_state()

        self.mine_var = ctk.BooleanVar(value=form["browser_mode"] == "mine")
        self.port_var = ctk.StringVar(value=str(form["debug_port"]))
        ctk.CTkCheckBox(box, text="Use my own Chrome (needed for Google login)", variable=self.mine_var,
                        command=self._on_browser_mode).grid(row=3, column=0, columnspan=4, padx=12,
                                                            pady=(0, 12), sticky="w")
        ctk.CTkLabel(box, text="Port").grid(row=3, column=4, padx=(12, 6), pady=(0, 12), sticky="e")
        ctk.CTkEntry(box, textvariable=self.port_var, width=60).grid(row=3, column=5, columnspan=2,
                                                                     pady=(0, 12), sticky="w")
        ctk.CTkButton(box, text="Open Chrome", width=110, command=self.open_chrome).grid(
            row=3, column=7, padx=12, pady=(0, 12))

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=12, pady=2)
        ctk.CTkButton(bar, text="Add job", width=90, command=self.add_job).pack(side="left")
        ctk.CTkButton(bar, text="Start all", width=90, command=self.start_all,
                      fg_color="#1a7f37", hover_color="#116329").pack(side="left", padx=6)
        ctk.CTkButton(bar, text="Stop all", width=90, command=self.stop_all,
                      fg_color="#b62324", hover_color="#8e1a1b").pack(side="left")
        self.error_label = ctk.CTkLabel(bar, text="", text_color=("#b62324", "#ff7b72"), anchor="w")
        self.error_label.pack(side="left", padx=12)

    def _build_jobs_and_log(self) -> None:
        self.jobs_frame = ctk.CTkScrollableFrame(self, label_text="Jobs")
        self.jobs_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=6)

        self.log = ctk.CTkTextbox(self, state="disabled", wrap="none")
        self.log.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))

    def _count_limits_run(self) -> bool:
        """True when a Reloads count above 0 is in charge, so the Run for timer is switched off."""
        raw = self.count_var.get().strip()
        return not self.infinite_var.get() and raw.isdigit() and int(raw) > 0

    def _sync_count_state(self) -> None:
        self.count_entry.configure(state="disabled" if self.infinite_var.get() else "normal")
        state = "disabled" if self._count_limits_run() else "normal"
        for box in self.duration_boxes:
            box.configure(state=state)

    # ---- actions -------------------------------------------------------------------

    def add_job(self) -> None:
        try:
            job = self._job_from_form()
        except ValueError as e:
            self.error_label.configure(text=str(e))
            return
        self.error_label.configure(text="")
        self._add_row(job)
        self._save()

    def _job_from_form(self) -> Job:
        url = normalize_url(self.url_var.get())
        interval = _parse_number(self.interval_var.get(), "Interval")
        jitter = _parse_number(self.jitter_var.get() or "0", "Jitter")
        duration = None if self._count_limits_run() else self._duration_seconds()
        count = None
        if not self.infinite_var.get():
            raw = self.count_var.get().strip()
            if not raw.isdigit():
                raise ValueError("Reloads must be a whole number")
            count = int(raw)
            if count == 0 and duration:
                count = None  # Reloads 0 + a Run for time: the timer alone ends the job
        raw_tabs = self.tabs_var.get().strip() or "1"
        if not raw_tabs.isdigit():
            raise ValueError("Tabs must be a whole number")
        job = Job(url=url, interval_s=interval, jitter_s=jitter,
                  mode=LABEL_TO_MODE[self.mode_var.get()], count=count, tabs=int(raw_tabs),
                  duration_s=duration)
        validate(job)
        return job

    def _duration_seconds(self) -> float | None:
        """Total of the days/hours/min/sec boxes in seconds; None when they are all 0 (no limit)."""
        total = 0
        for key, _label, _choices in DURATION_FIELDS:
            raw = self.duration_vars[key].get().strip() or "0"
            if not raw.isdigit():
                raise ValueError("Run for: days, hours, min and sec must be whole numbers")
            total += int(raw) * _DURATION_UNIT_SECONDS[key]
        return float(total) if total else None

    def _duration_text(self) -> str:
        """The boxes as saved text ("2h 30m"), or "" if empty or not yet valid."""
        try:
            seconds = self._duration_seconds()
        except ValueError:
            return ""
        return format_duration(seconds) if seconds else ""

    def _set_duration_fields(self, seconds: float | None) -> None:
        left = int(round(seconds or 0))
        for key, _label, _choices in DURATION_FIELDS:
            count, left = divmod(left, _DURATION_UNIT_SECONDS[key])
            self.duration_vars[key].set(str(count))

    def _add_row(self, job: Job) -> None:
        self.rows[job.id] = JobRow(self.jobs_frame, job, self._start_row, self._stop_row, self._remove_row)

    # ---- browser mode (own window vs. the user's Chrome) ---------------------------

    def _parse_port(self) -> int:
        raw = self.port_var.get().strip()
        if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
            raise ValueError("Port must be a number between 1024 and 65535")
        return int(raw)

    def _browser_url(self) -> str | None:
        return f"http://127.0.0.1:{self._parse_port()}" if self.mine_var.get() else None

    def _apply_browser_mode(self) -> bool:
        """Push the chosen browser mode to the engine. False (with a message) if it can't be applied."""
        try:
            url = self._browser_url()
        except ValueError as e:
            self.error_label.configure(text=str(e))
            return False
        if url != self._applied_url:
            if any(r.job.status in ACTIVE for r in self.rows.values()):
                self.mine_var.set(self._applied_url is not None)
                self.error_label.configure(text="Stop all jobs before changing the browser mode")
                return False
            self.engine.set_connect_url(url)
            self._applied_url = url
        return True

    def _on_browser_mode(self) -> None:
        if self._apply_browser_mode():
            self.error_label.configure(text="")
            self._save()

    def open_chrome(self) -> None:
        try:
            port = self._parse_port()
        except ValueError as e:
            self.error_label.configure(text=str(e))
            return
        self.error_label.configure(text="")
        info = chrome.browser_info(port)
        if info is not None:
            if chrome.is_chrome(info):
                self._note(f"Chrome is already running on port {port}. Tick 'Use my own Chrome' and press Start.")
            else:
                self.error_label.configure(
                    text=f"Port {port} is used by another program ({info.get('Browser', 'unknown')}). "
                         "Pick a different port.")
            return
        try:
            chrome.launch_chrome(port, settings.app_dir() / "chrome-profile")
        except (FileNotFoundError, OSError) as e:
            self.error_label.configure(text=f"Could not start Chrome: {e}")
            return
        self._note("Opened Chrome. Log in to your sites there (Google sign-in works), "
                   "keep it open, then press Start.")

    def _note(self, text: str) -> None:
        self._append_log(f"{datetime.now():%H:%M:%S} {text}")

    # ---- job actions ---------------------------------------------------------------

    def _start_row(self, row: JobRow) -> None:
        if row.job.status in ACTIVE:
            return
        if not self._apply_browser_mode():
            return
        row.job.done = 0
        row.job.status = "starting"
        row.remaining = None
        row.time_left = None
        row.refresh()
        self.engine.start_job(row.job)

    def _stop_row(self, row: JobRow) -> None:
        self.engine.stop_job(row.job.id)

    def _remove_row(self, row: JobRow) -> None:
        self.engine.remove_job(row.job.id)
        self.rows.pop(row.job.id, None)
        row.destroy()
        self._save()

    def start_all(self) -> None:
        for row in list(self.rows.values()):
            self._start_row(row)

    def stop_all(self) -> None:
        self.engine.stop_all()

    # ---- event pump ----------------------------------------------------------------

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            while True:
                event = self.events.get_nowait()
                try:
                    self._handle(event)
                except Exception as e:  # one bad event must never freeze every row for good
                    self._append_log(f"UI error while handling an event: {e!r}")
        except queue.Empty:
            pass
        finally:
            if not self._closed:
                self.after(100, self._poll)

    def _handle(self, ev) -> None:
        if isinstance(ev, LogEvent):
            self._append_log(ev.text)
        elif isinstance(ev, JobEvent):
            row = self.rows.get(ev.job_id)
            if row is None:
                return
            row.job.status = ev.status
            row.job.done = ev.done
            row.remaining = ev.remaining
            row.time_left = ev.limit_remaining
            row.refresh()

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---- persistence / shutdown ----------------------------------------------------

    def _form_state(self) -> dict:
        return {
            "url": self.url_var.get(),
            "interval_s": self.interval_var.get(),
            "jitter_s": self.jitter_var.get(),
            "mode": LABEL_TO_MODE.get(self.mode_var.get(), "normal"),
            "count": self.count_var.get(),
            "tabs": self.tabs_var.get(),
            "duration": self._duration_text(),
            "infinite": bool(self.infinite_var.get()),
            "browser_mode": "mine" if self.mine_var.get() else "own",
            "debug_port": self.port_var.get(),
        }

    def _save(self) -> None:
        try:
            settings.save(self._settings_path,
                          settings.Settings(jobs=[r.job for r in self.rows.values()], form=self._form_state()))
        except OSError as e:
            self._append_log(f"Could not save settings: {e}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._save()
        self.engine.shutdown()
        self.destroy()


def _parse_number(raw: str, label: str) -> float:
    try:
        return float(raw.strip())
    except ValueError:
        raise ValueError(f"{label} must be a number") from None
