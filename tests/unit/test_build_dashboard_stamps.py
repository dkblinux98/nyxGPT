"""Unit tests for build_dashboard.py's build/as-of provenance (#3807).

Covers load_issues() (the corpus is read stamped or bare), parse_stamp() and
source_stamps() — the per-source freshness block the page renders as its build
timestamp, per-section "as of" lines and staleness callouts.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
RETRO_DIR = REPO_ROOT / "scripts" / "retrospective"

NOW = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build_dashboard():
    return _load("build_dashboard", RETRO_DIR / "build_dashboard.py")


ISSUES = [
    {"n": 1, "title": "t", "labels": [], "milestone": None, "created": "2026-08-01T00:00:00Z"}
]


def test_load_issues_reads_the_historical_bare_list(build_dashboard, tmp_path):
    """A corpus written before #3807 is a list and carries no stamp."""
    path = tmp_path / "all_issues.json"
    path.write_text(json.dumps(ISSUES))
    issues, generated_at = build_dashboard.load_issues(path)
    assert issues == ISSUES
    assert generated_at is None


def test_load_issues_reads_the_stamped_object_shape(build_dashboard, tmp_path):
    """A stamped refresh yields the same issues plus its as-of time."""
    path = tmp_path / "all_issues.json"
    path.write_text(json.dumps({"generated_at": "2026-08-18T08:00:00Z", "issues": ISSUES}))
    issues, generated_at = build_dashboard.load_issues(path)
    assert issues == ISSUES
    assert generated_at == "2026-08-18T08:00:00Z"


def test_load_issues_tolerates_a_stamped_file_with_no_issues(build_dashboard, tmp_path):
    path = tmp_path / "all_issues.json"
    path.write_text(json.dumps({"generated_at": "2026-08-18T08:00:00Z"}))
    assert build_dashboard.load_issues(path) == ([], "2026-08-18T08:00:00Z")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-08-17T06:02:42Z", datetime(2026, 8, 17, 6, 2, 42, tzinfo=UTC)),
        ("2026-08-17T06:02:42+00:00", datetime(2026, 8, 17, 6, 2, 42, tzinfo=UTC)),
        # A naive stamp is read as UTC rather than crashing the whole build.
        ("2026-08-17T06:02:42", datetime(2026, 8, 17, 6, 2, 42, tzinfo=UTC)),
        (None, None),
        ("", None),
        ("last Tuesday", None),
    ],
)
def test_parse_stamp(build_dashboard, raw, expected):
    assert build_dashboard.parse_stamp(raw) == expected


def _stamps(build_dashboard, generated_at, present=True):
    return build_dashboard.source_stamps(
        NOW,
        [("spend", "Spend telemetry", "spend.json", present, generated_at, "retro_spend_dump.yml")],
    )["spend"]


def test_fresh_source_is_not_stale(build_dashboard):
    entry = _stamps(build_dashboard, (NOW - timedelta(hours=2)).isoformat())
    assert entry["stale"] is False
    assert entry["ageDays"] == pytest.approx(2 / 24, abs=0.01)
    assert entry["generatedAt"].startswith("2026-08-18T07:00")


def test_source_materially_older_than_the_build_is_flagged(build_dashboard):
    """A stale dump must not be able to hide behind a fresh build."""
    entry = _stamps(build_dashboard, (NOW - timedelta(days=9)).isoformat())
    assert entry["stale"] is True
    assert entry["ageDays"] == pytest.approx(9.0)


def test_stale_boundary_is_the_documented_threshold(build_dashboard):
    just_under = _stamps(build_dashboard, (NOW - timedelta(hours=23)).isoformat())
    just_over = _stamps(build_dashboard, (NOW - timedelta(hours=25)).isoformat())
    assert (just_under["stale"], just_over["stale"]) == (False, True)
    assert build_dashboard.STALE_SOURCE_DAYS == 1.0


def test_unstamped_source_is_reported_as_unknown_not_omitted(build_dashboard):
    """No generated_at means the page says so — silence would read as fresh."""
    entry = _stamps(build_dashboard, None)
    assert entry["present"] is True
    assert entry["generatedAt"] is None
    assert entry["ageDays"] is None
    assert entry["stale"] is False
    assert entry["file"] == "spend.json"
    assert entry["label"] == "Spend telemetry"


def test_absent_file_is_marked_not_present(build_dashboard):
    """An optional dump that was never produced: the page must not claim its
    data is merely unknown."""
    assert _stamps(build_dashboard, None, present=False)["present"] is False


def test_every_source_names_the_dump_that_owes_it(build_dashboard):
    """#3808: an absent section has to say which run to go and read, so the
    producing workflow travels with the stamp."""
    assert _stamps(build_dashboard, None, present=False)["workflow"] == "retro_spend_dump.yml"


def _seed_data_dir(tmp_path, *, issues_stamped=False):
    data = tmp_path / "data"
    data.mkdir()
    corpus = (
        {"generated_at": NOW.isoformat(), "issues": ISSUES}
        if issues_stamped
        else list(ISSUES)  # type: ignore[assignment]
    )
    (data / "all_issues.json").write_text(json.dumps(corpus))
    (data / "dashboard_data.json").write_text(
        json.dumps({"generated_at": NOW.isoformat(), "modules": {}, "issues": [], "days": {}})
    )
    (data / "relationships.json").write_text(json.dumps({"generated_at": "2026-07-01T00:00:00Z"}))
    return data


def _build(build_dashboard, monkeypatch, data, out, *extra):
    monkeypatch.setattr(
        sys, "argv", ["build_dashboard.py", "--data-dir", str(data), "--out", str(out), *extra]
    )
    return build_dashboard.main()


def test_build_emits_a_stamp_for_every_source(build_dashboard, tmp_path, monkeypatch):
    """End to end: the built HTML carries a build time and one entry per source."""
    data = _seed_data_dir(tmp_path, issues_stamped=True)
    out = tmp_path / "retro.html"
    _build(build_dashboard, monkeypatch, data, out)

    embedded = json.loads(
        out.read_text().split("const QDATA = ", 1)[1].split(";\nconst DATA", 1)[0]
    )
    build = embedded["build"]
    assert build["staleAfterDays"] == 1.0
    assert build["at"] and datetime.fromisoformat(build["at"]).tzinfo is not None
    assert set(build["sources"]) == {
        "issues",
        "relationships",
        "reviews",
        "projectFields",
        "spend",
        "churn",
    }
    # Present-but-unrefreshed vs absent are different states on the page.
    assert build["sources"]["issues"]["present"] is True
    assert build["sources"]["issues"]["generatedAt"].startswith("2026-08-18T09:00")
    assert build["sources"]["relationships"]["stale"] is True
    assert build["sources"]["spend"]["present"] is False
    assert build["sources"]["churn"]["present"] is False
    # Every panel's freshness line is wired to a source key that exists.
    html = out.read_text()
    for keys in {"issues relationships projectFields", "issues reviews", "spend", "churn"}:
        assert f'data-asof="{keys}"' in html
        assert all(k in build["sources"] for k in keys.split(" "))


def test_unstamped_corpus_survives_the_build(build_dashboard, tmp_path, monkeypatch):
    """The bare-list corpus still builds; its as-of time is simply unknown."""
    data = _seed_data_dir(tmp_path, issues_stamped=False)
    out = tmp_path / "retro.html"
    _build(build_dashboard, monkeypatch, data, out)
    embedded = json.loads(
        out.read_text().split("const QDATA = ", 1)[1].split(";\nconst DATA", 1)[0]
    )
    assert embedded["build"]["sources"]["issues"] == {
        "label": "Issue corpus",
        "file": "all_issues.json",
        "generatedAt": None,
        "ageDays": None,
        "stale": False,
        "present": True,
        # Written by the refresh session by hand, so no dump owes it.
        "workflow": None,
    }


# --- #3808: a dump that did not land must be impossible to publish quietly ---


def test_build_fails_when_a_dump_did_not_land(build_dashboard, tmp_path, monkeypatch, capsys):
    """The churn dump had failed on every run it ever had and the refresh still
    reported success, because a missing input cost the build nothing."""
    data = _seed_data_dir(tmp_path, issues_stamped=True)
    out = tmp_path / "retro.html"

    assert _build(build_dashboard, monkeypatch, data, out) == build_dashboard.MISSING_SOURCE_EXIT

    printed = capsys.readouterr().out
    assert "missing sources:" in printed
    # Named and actionable: which file, and which dump owes it.
    for token in ("spend.json", "retro_spend_dump.yml", "churn.json", "retro_churn_dump.yml"):
        assert token in printed
    # The page is still written -- it is publishable, deliberately, not silently.
    assert out.exists()


def test_missing_sources_can_be_published_deliberately(build_dashboard, tmp_path, monkeypatch):
    """--allow-missing-sources is the conscious override, not the default."""
    data = _seed_data_dir(tmp_path, issues_stamped=True)
    out = tmp_path / "retro.html"
    assert _build(build_dashboard, monkeypatch, data, out, "--allow-missing-sources") == 0


def test_build_succeeds_when_every_source_landed(build_dashboard, tmp_path, monkeypatch):
    data = _seed_data_dir(tmp_path, issues_stamped=True)
    stamp = {"generated_at": NOW.isoformat()}
    (data / "project_fields.json").write_text(json.dumps({**stamp, "issues": {}}))
    empty_bucket = {"claude_steps": 0, "runs": 0, "runner_minutes": 0.0, "retry_cycles": 0}
    (data / "spend.json").write_text(
        json.dumps({**stamp, "issues": {}, "unattributed": empty_bucket})
    )
    (data / "churn.json").write_text(json.dumps({**stamp, "issues": {}, "rounds": []}))
    out = tmp_path / "retro.html"
    assert _build(build_dashboard, monkeypatch, data, out) == 0


def test_absent_section_is_rendered_unavailable_not_hidden(build_dashboard, tmp_path, monkeypatch):
    """The page has to say a section is missing data. Omitting it is what made
    a never-delivered churn view indistinguishable from a complete dashboard."""
    data = _seed_data_dir(tmp_path, issues_stamped=True)
    out = tmp_path / "retro.html"
    _build(build_dashboard, monkeypatch, data, out)
    html = out.read_text()

    # Both optional panels are wired to the unavailable renderer, and neither
    # section element is hidden outright any more.
    for key, body, notice in (
        ("spend", "spendbody", "spendunavail"),
        ("churn", "churnbody", "churnunavail"),
    ):
        assert f"renderUnavailable('{key}', '{body}', '{notice}')" in html
        assert f'<div class="unavail" id="{notice}" hidden></div>' in html
    assert 'id="spendSection" hidden' not in html
    assert 'id="churnSection" hidden' not in html
    # The notice needs the producing workflow to point at a run to read.
    embedded = json.loads(html.split("const QDATA = ", 1)[1].split(";\nconst DATA", 1)[0])
    assert embedded["build"]["sources"]["churn"]["workflow"] == "retro_churn_dump.yml"
    assert embedded["build"]["sources"]["spend"]["workflow"] == "retro_spend_dump.yml"
