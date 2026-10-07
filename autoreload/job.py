"""Pure job model and scheduling rules. No Playwright or Tk imports."""
from __future__ import annotations

import random
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlparse

MODES = ("normal", "hard")
MIN_INTERVAL_S = 1.0
MAX_TABS = 20
_ALLOWED_SCHEMES = ("http", "https", "file")


@dataclass
class Job:
    url: str
    interval_s: float = 30.0
    jitter_s: float = 0.0
    mode: str = "normal"
    count: int | None = None  # None = infinite
    tabs: int = 1  # how many tabs open this URL; all are reloaded together each cycle
    duration_s: float | None = None  # stop by itself after this long; None = no time limit
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    # runtime state, never persisted
    done: int = 0
    status: str = "idle"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "interval_s": self.interval_s,
            "jitter_s": self.jitter_s,
            "mode": self.mode,
            "count": self.count,
            "tabs": self.tabs,
            "duration_s": self.duration_s,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        return cls(
            id=data["id"],
            url=data["url"],
            interval_s=data["interval_s"],
            jitter_s=data["jitter_s"],
            mode=data["mode"],
            count=data["count"],
            tabs=data.get("tabs", 1),  # files saved before the tabs field existed
            duration_s=data.get("duration_s"),  # ...or before the time limit existed
        )


def normalize_url(raw: str) -> str:
    """Trim, default to https://, and reject anything that is not a usable web URL."""
    url = (raw or "").strip()
    if not url:
        raise ValueError("URL is empty")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r}")
    if parsed.scheme != "file" and not parsed.netloc:
        raise ValueError("URL has no host")
    return url


def validate(job: Job) -> None:
    if job.mode not in MODES:
        raise ValueError(f"Unknown mode: {job.mode!r}")
    if job.interval_s < MIN_INTERVAL_S:
        raise ValueError(f"interval must be at least {MIN_INTERVAL_S:g}s")
    if job.jitter_s < 0:
        raise ValueError("jitter cannot be negative")
    if job.jitter_s >= job.interval_s:
        raise ValueError("jitter must be smaller than the interval")
    if job.count is not None and job.count < 1:
        raise ValueError("count must be at least 1 (or infinite)")
    if not 1 <= job.tabs <= MAX_TABS:
        raise ValueError(f"tabs must be between 1 and {MAX_TABS}")
    if job.duration_s is not None and job.duration_s < job.interval_s:
        raise ValueError("Run for must be at least one interval")


_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_PART = r"\s*(\d+(?:\.\d+)?)\s*([dhms])"


def parse_duration(text: str | None) -> float | None:
    """'30m', '2h', '1h 30m', '1d 6h', '90s', '1.5h' -> seconds. Empty -> None (no limit)."""
    s = (text or "").strip().lower()
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        raise ValueError("Run for needs a unit, e.g. 30m, 2h or 1d")
    if not re.fullmatch(rf"(?:{_PART})+\s*", s):
        raise ValueError("Run for must look like 30m, 2h or 1d 6h")
    total = sum(float(n) * _UNIT_SECONDS[u] for n, u in re.findall(_PART, s))
    if total <= 0:
        raise ValueError("Run for must be greater than zero")
    return total


def format_duration(seconds: float) -> str:
    """90 -> '1m 30s', 5400 -> '1h 30m'. Rounded to whole seconds; parse_duration reads it back."""
    left = max(0, int(round(seconds)))
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, left = divmod(left, size)
        if count:
            parts.append(f"{count}{unit}")
    return " ".join(parts) or "0s"


def next_delay(job: Job, rng=random) -> float:
    """Seconds to wait before the next reload: interval +/- uniform jitter, floored at 1s."""
    delay = job.interval_s
    if job.jitter_s:
        delay += rng.uniform(-job.jitter_s, job.jitter_s)
    return max(MIN_INTERVAL_S, delay)


def is_finished(job: Job) -> bool:
    return job.count is not None and job.done >= job.count
