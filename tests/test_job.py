import random

import pytest

from autoreload.job import (Job, format_duration, is_finished, next_delay, normalize_url,
                            parse_duration, validate)


class FixedRng:
    """rng stub: uniform() always returns a chosen fraction of the requested range."""

    def __init__(self, fraction):
        self.fraction = fraction

    def uniform(self, a, b):
        return a + (b - a) * self.fraction


# --- normalize_url -----------------------------------------------------------

def test_normalize_adds_https_when_scheme_missing():
    assert normalize_url("example.com/page") == "https://example.com/page"


def test_normalize_keeps_existing_scheme():
    assert normalize_url("http://localhost:8000/x") == "http://localhost:8000/x"


def test_normalize_strips_whitespace():
    assert normalize_url("  https://example.com  ") == "https://example.com"


@pytest.mark.parametrize("bad", ["", "   ", "https://", "ftp://example.com", "http://"])
def test_normalize_rejects_invalid(bad):
    with pytest.raises(ValueError):
        normalize_url(bad)


# --- validate ----------------------------------------------------------------

def make(**kw):
    base = dict(url="https://example.com", interval_s=10, jitter_s=0, mode="normal", count=None)
    base.update(kw)
    return Job(**base)


def test_validate_accepts_good_job():
    validate(make())
    validate(make(count=5, jitter_s=3, mode="hard"))


def test_validate_rejects_short_interval():
    with pytest.raises(ValueError, match="interval"):
        validate(make(interval_s=0.5))


def test_validate_rejects_negative_jitter():
    with pytest.raises(ValueError, match="jitter"):
        validate(make(jitter_s=-1))


def test_validate_rejects_jitter_not_below_interval():
    with pytest.raises(ValueError, match="jitter"):
        validate(make(interval_s=5, jitter_s=5))


@pytest.mark.parametrize("count", [0, -3])
def test_validate_rejects_bad_count(count):
    with pytest.raises(ValueError, match="count"):
        validate(make(count=count))


def test_validate_rejects_unknown_mode():
    with pytest.raises(ValueError, match="mode"):
        validate(make(mode="turbo"))


@pytest.mark.parametrize("tabs", [1, 3, 20])
def test_validate_accepts_tab_counts(tabs):
    validate(make(tabs=tabs))


@pytest.mark.parametrize("tabs", [0, -1, 21, 500])
def test_validate_rejects_bad_tab_counts(tabs):
    with pytest.raises(ValueError, match="tabs"):
        validate(make(tabs=tabs))


def test_tabs_default_to_one():
    assert make().tabs == 1


def test_tabs_survive_round_trip():
    assert Job.from_dict(make(tabs=4).to_dict()).tabs == 4


def test_job_saved_before_tabs_existed_loads_with_one_tab():
    old = make().to_dict()
    del old["tabs"]
    assert Job.from_dict(old).tabs == 1


# --- run-for duration --------------------------------------------------------

@pytest.mark.parametrize("text, seconds", [
    ("90s", 90),
    ("30m", 1800),
    ("2h", 7200),
    ("1h30m", 5400),
    ("1h 30m", 5400),
    ("1d 6h", 108000),
    ("1D 6H", 108000),       # case does not matter
    ("1.5h", 5400),
    ("  45 s  ", 45),
    ("1d2h3m4s", 93784),
])
def test_parse_duration_accepts(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "   ", None])
def test_parse_duration_empty_means_no_limit(text):
    assert parse_duration(text) is None


def test_parse_duration_bare_number_asks_for_a_unit():
    with pytest.raises(ValueError, match="unit"):
        parse_duration("30")


@pytest.mark.parametrize("text", ["abc", "-5m", "5x", "h", "1h garbage", "1h,", "m30"])
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(ValueError, match="Run for"):
        parse_duration(text)


@pytest.mark.parametrize("text", ["0m", "0s", "0h 0m"])
def test_parse_duration_rejects_zero(text):
    with pytest.raises(ValueError, match="greater than zero"):
        parse_duration(text)


@pytest.mark.parametrize("seconds, text", [
    (45, "45s"), (90, "1m 30s"), (1800, "30m"), (5400, "1h 30m"), (93600, "1d 2h"), (93784, "1d 2h 3m 4s"),
])
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


def test_format_then_parse_round_trips():
    for seconds in (1, 59, 60, 3599, 3600, 86399, 86400, 200000):
        assert parse_duration(format_duration(seconds)) == seconds


def test_format_duration_rounds_fractions():
    assert format_duration(2.6) == "3s"


def test_validate_accepts_no_limit_and_a_long_enough_limit():
    validate(make(duration_s=None))
    validate(make(interval_s=10, duration_s=10))
    validate(make(interval_s=10, duration_s=3600))


def test_validate_rejects_limit_shorter_than_one_interval():
    with pytest.raises(ValueError, match="Run for"):
        validate(make(interval_s=30, duration_s=10))


def test_duration_defaults_to_no_limit():
    assert make().duration_s is None


def test_duration_survives_round_trip():
    assert Job.from_dict(make(duration_s=7200).to_dict()).duration_s == 7200


def test_job_saved_before_duration_existed_loads_with_no_limit():
    old = make().to_dict()
    del old["duration_s"]
    assert Job.from_dict(old).duration_s is None


# --- next_delay --------------------------------------------------------------

def test_next_delay_without_jitter_is_interval():
    assert next_delay(make(interval_s=10, jitter_s=0)) == 10


def test_next_delay_stays_within_jitter_bounds():
    job = make(interval_s=10, jitter_s=3)
    rng = random.Random(1234)
    for _ in range(500):
        d = next_delay(job, rng)
        assert 7 <= d <= 13


def test_next_delay_hits_both_extremes():
    job = make(interval_s=10, jitter_s=3)
    assert next_delay(job, FixedRng(0.0)) == 7
    assert next_delay(job, FixedRng(1.0)) == 13


def test_next_delay_never_below_one_second():
    job = make(interval_s=1, jitter_s=0.9)
    assert next_delay(job, FixedRng(0.0)) >= 1.0


# --- is_finished -------------------------------------------------------------

def test_infinite_job_never_finishes():
    job = make(count=None)
    job.done = 10_000
    assert not is_finished(job)


def test_counted_job_finishes_when_done_reaches_count():
    job = make(count=3)
    job.done = 2
    assert not is_finished(job)
    job.done = 3
    assert is_finished(job)


# --- serialization -----------------------------------------------------------

def test_dict_round_trip_drops_runtime_state():
    job = make(count=4, jitter_s=2, mode="hard")
    job.done = 3
    job.status = "running"
    restored = Job.from_dict(job.to_dict())
    assert restored.id == job.id
    assert (restored.url, restored.interval_s, restored.jitter_s, restored.mode, restored.count) == (
        job.url, job.interval_s, job.jitter_s, job.mode, job.count)
    assert restored.done == 0
    assert restored.status == "idle"
