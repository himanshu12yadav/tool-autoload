"""Load/save the job list and form defaults as JSON. Never raises on bad files."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .job import Job, validate

DEFAULT_FORM = {
    "url": "",
    "interval_s": "30",
    "jitter_s": "0",
    "mode": "normal",
    "count": "10",
    "tabs": "1",
    "duration": "",  # "Run for" text, e.g. "2h"; empty = no time limit
    "infinite": True,
    "browser_mode": "own",  # "own" = this tool's window, "mine" = attach to the user's Chrome
    "debug_port": "9333",
}


def app_dir(platform: str | None = None, env=None, home: Path | None = None) -> Path:
    """Where settings, logins and the Chrome profile live. The arguments exist so each platform's
    answer can be tested from any machine."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = Path(home) if home else Path.home()
    if platform == "darwin":
        return home / "Library" / "Application Support" / "AutoReload"
    if platform.startswith("win"):
        return Path(env.get("APPDATA") or home) / "AutoReload"
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "AutoReload"


def default_path() -> Path:
    return app_dir() / "settings.json"


@dataclass
class Settings:
    jobs: list[Job] = field(default_factory=list)
    form: dict = field(default_factory=lambda: dict(DEFAULT_FORM))


def load(path: Path) -> Settings:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()

    jobs: list[Job] = []
    raw_jobs = data.get("jobs")
    for entry in raw_jobs if isinstance(raw_jobs, list) else []:
        try:
            job = Job.from_dict(entry)
            validate(job)
        except (KeyError, TypeError, ValueError):
            continue
        jobs.append(job)

    form = dict(DEFAULT_FORM)
    raw_form = data.get("form")
    if isinstance(raw_form, dict):
        form.update({k: v for k, v in raw_form.items() if k in DEFAULT_FORM})
    return Settings(jobs=jobs, form=form)


def save(path: Path, settings: Settings) -> None:
    """Atomic write (temp file + replace) so a crash never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"jobs": [j.to_dict() for j in settings.jobs], "form": settings.form}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
