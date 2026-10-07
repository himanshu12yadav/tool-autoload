import json

from autoreload.job import Job
from autoreload.settings import DEFAULT_FORM, Settings, app_dir, load, save


def test_app_dir_on_macos_uses_application_support(tmp_path):
    assert app_dir("darwin", {}, tmp_path) == tmp_path / "Library" / "Application Support" / "AutoReload"


def test_app_dir_on_windows_uses_appdata_or_home(tmp_path):
    assert app_dir("win32", {"APPDATA": str(tmp_path / "Roaming")}, tmp_path) == tmp_path / "Roaming" / "AutoReload"
    assert app_dir("win32", {}, tmp_path) == tmp_path / "AutoReload"


def test_app_dir_on_linux_follows_xdg(tmp_path):
    assert app_dir("linux", {}, tmp_path) == tmp_path / ".config" / "AutoReload"
    assert app_dir("linux", {"XDG_CONFIG_HOME": str(tmp_path / "x")}, tmp_path) == tmp_path / "x" / "AutoReload"


def test_missing_file_gives_defaults(tmp_path):
    s = load(tmp_path / "nope.json")
    assert s.jobs == []
    assert s.form == DEFAULT_FORM


def test_round_trip(tmp_path):
    path = tmp_path / "s.json"
    jobs = [
        Job(url="https://a.com", interval_s=5, jitter_s=1, mode="hard", count=3),
        Job(url="https://b.com", interval_s=60),
    ]
    form = {**DEFAULT_FORM, "interval_s": "15", "mode": "hard"}
    save(path, Settings(jobs=jobs, form=form))

    loaded = load(path)
    assert [j.to_dict() for j in loaded.jobs] == [j.to_dict() for j in jobs]
    assert loaded.form["interval_s"] == "15"
    assert loaded.form["mode"] == "hard"


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "deep" / "dir" / "s.json"
    save(path, Settings(jobs=[], form=dict(DEFAULT_FORM)))
    assert path.exists()


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    s = load(path)
    assert s.jobs == []
    assert s.form == DEFAULT_FORM


def test_wrong_top_level_type_falls_back(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load(path).jobs == []


def test_bad_job_entries_are_skipped_good_ones_kept(tmp_path):
    good = Job(url="https://ok.com", interval_s=10).to_dict()
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"jobs": [{"url": "x"}, good, "junk"], "form": {}}), encoding="utf-8")
    s = load(path)
    assert [j.url for j in s.jobs] == ["https://ok.com"]


def test_invalid_job_values_are_skipped(tmp_path):
    bad = Job(url="https://bad.com", interval_s=0.1).to_dict()  # fails validate()
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"jobs": [bad], "form": {}}), encoding="utf-8")
    assert load(path).jobs == []


def test_duration_is_saved_with_the_job_and_the_form(tmp_path):
    path = tmp_path / "s.json"
    job = Job(url="https://a.com", interval_s=5, duration_s=7200)
    save(path, Settings(jobs=[job], form={**DEFAULT_FORM, "duration": "2h"}))
    loaded = load(path)
    assert loaded.jobs[0].duration_s == 7200
    assert loaded.form["duration"] == "2h"


def test_default_form_has_empty_duration():
    assert DEFAULT_FORM["duration"] == ""


def test_job_with_nonsense_duration_is_skipped(tmp_path):
    bad = Job(url="https://bad.com", interval_s=30).to_dict()
    bad["duration_s"] = "soon"
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"jobs": [bad], "form": {}}), encoding="utf-8")
    assert load(path).jobs == []


def test_unknown_form_keys_ignored_missing_keys_defaulted(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"jobs": [], "form": {"mode": "hard", "bogus": 1}}), encoding="utf-8")
    s = load(path)
    assert s.form["mode"] == "hard"
    assert "bogus" not in s.form
    assert s.form["interval_s"] == DEFAULT_FORM["interval_s"]
