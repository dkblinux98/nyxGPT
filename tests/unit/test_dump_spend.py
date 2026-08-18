"""Unit tests for scripts/retrospective/dump_spend.py (#3696).

Only covers the pure branch-attribution and aggregation logic; `collect()` is
exercised with injected fake `gh`-calling functions so no subprocess or
GitHub API access is required.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
RETRO_DIR = REPO_ROOT / "scripts" / "retrospective"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dump_spend():
    return _load("dump_spend", RETRO_DIR / "dump_spend.py")


class TestIssueOf:
    def test_matches_developer_branch_kinds(self, dump_spend):
        assert dump_spend.issue_of("feat/3696-spend-telemetry") == 3696
        assert dump_spend.issue_of("fix/42-null-pointer") == 42
        assert dump_spend.issue_of("chore/7-cleanup") == 7
        assert dump_spend.issue_of("test/100-coverage") == 100
        assert dump_spend.issue_of("docs/5-readme") == 5
        assert dump_spend.issue_of("refactor/9-tidy") == 9

    def test_matches_claude_issue_session_branch(self, dump_spend):
        assert dump_spend.issue_of("claude/issue-3696-20260809-2333") == 3696

    def test_no_match_for_release_or_unrelated_branches(self, dump_spend):
        assert dump_spend.issue_of("v3.0.0") is None
        assert dump_spend.issue_of("main") is None
        assert dump_spend.issue_of("claude/retro-data") is None
        assert dump_spend.issue_of(None) is None
        assert dump_spend.issue_of("") is None

    def test_requires_numeric_prefix_immediately_after_kind(self, dump_spend):
        assert dump_spend.issue_of("feat/no-issue-number") is None


class TestCollect:
    def _fake_apis(self, dump_spend, runs_by_workflow, minutes_by_run, claude_steps_by_run):
        def list_runs_fn(repo, workflow_file):
            return runs_by_workflow.get(workflow_file, [])

        def run_minutes_fn(repo, run_id):
            return minutes_by_run.get(run_id, 0.0)

        def claude_steps_fn(repo, run_id):
            return claude_steps_by_run.get(run_id, 0)

        return list_runs_fn, run_minutes_fn, claude_steps_fn

    def test_attributes_runs_by_branch_and_sums_runner_minutes(self, dump_spend):
        runs_by_workflow = {
            "claude.yml": [
                {"id": 1, "status": "completed", "head_branch": "claude/issue-3696-20260809-2333"},
            ],
            "ci-tests.yml": [
                {"id": 2, "status": "completed", "head_branch": "feat/3696-spend-telemetry"},
            ],
        }
        list_runs_fn, run_minutes_fn, claude_steps_fn = self._fake_apis(
            dump_spend, runs_by_workflow, {1: 2.5, 2: 4.0}, {}
        )
        issues, unattributed = dump_spend.collect(
            "owner/repo",
            list_runs_fn=list_runs_fn,
            run_minutes_fn=run_minutes_fn,
            claude_steps_fn=claude_steps_fn,
        )
        bucket = issues[3696]
        assert bucket["runs"] == 2
        assert bucket["runner_minutes"] == pytest.approx(6.5)
        assert bucket["claude_steps"] == 1  # static count for claude.yml
        assert bucket["workflows"] == {"claude.yml": 1, "ci-tests.yml": 1}
        assert unattributed["runs"] == 0

    def test_unattributable_branch_rolls_up_to_unattributed_bucket(self, dump_spend):
        runs_by_workflow = {
            "security-scan.yml": [
                {"id": 3, "status": "completed", "head_branch": "v3.0.0"},
            ],
        }
        list_runs_fn, run_minutes_fn, claude_steps_fn = self._fake_apis(
            dump_spend, runs_by_workflow, {3: 1.0}, {}
        )
        issues, unattributed = dump_spend.collect(
            "owner/repo",
            list_runs_fn=list_runs_fn,
            run_minutes_fn=run_minutes_fn,
            claude_steps_fn=claude_steps_fn,
        )
        assert issues == {}
        assert unattributed["runs"] == 1
        assert unattributed["runner_minutes"] == pytest.approx(1.0)

    def test_incomplete_runs_are_skipped(self, dump_spend):
        runs_by_workflow = {
            "claude.yml": [
                {"id": 4, "status": "in_progress", "head_branch": "feat/1-wip"},
            ],
        }
        list_runs_fn, run_minutes_fn, claude_steps_fn = self._fake_apis(
            dump_spend, runs_by_workflow, {4: 99.0}, {}
        )
        issues, unattributed = dump_spend.collect(
            "owner/repo",
            list_runs_fn=list_runs_fn,
            run_minutes_fn=run_minutes_fn,
            claude_steps_fn=claude_steps_fn,
        )
        assert issues == {}
        assert unattributed["runs"] == 0

    def test_dynamic_workflow_derives_claude_steps_and_retry_cycles(self, dump_spend):
        runs_by_workflow = {
            dump_spend.CLAUDE_WORKFLOW_DYNAMIC: [
                {"id": 5, "status": "completed", "head_branch": "feat/8-thing"},
            ],
        }
        list_runs_fn, run_minutes_fn, claude_steps_fn = self._fake_apis(
            dump_spend, runs_by_workflow, {5: 10.0}, {5: 3}
        )
        issues, _ = dump_spend.collect(
            "owner/repo",
            list_runs_fn=list_runs_fn,
            run_minutes_fn=run_minutes_fn,
            claude_steps_fn=claude_steps_fn,
        )
        bucket = issues[8]
        assert bucket["claude_steps"] == 3
        # first Claude step is the real work; the rest are self-heal retries
        assert bucket["retry_cycles"] == 2

    def test_dynamic_workflow_with_single_claude_step_has_no_retries(self, dump_spend):
        runs_by_workflow = {
            dump_spend.CLAUDE_WORKFLOW_DYNAMIC: [
                {"id": 6, "status": "completed", "head_branch": "feat/9-thing"},
            ],
        }
        list_runs_fn, run_minutes_fn, claude_steps_fn = self._fake_apis(
            dump_spend, runs_by_workflow, {6: 5.0}, {6: 1}
        )
        issues, _ = dump_spend.collect(
            "owner/repo",
            list_runs_fn=list_runs_fn,
            run_minutes_fn=run_minutes_fn,
            claude_steps_fn=claude_steps_fn,
        )
        assert issues[9]["retry_cycles"] == 0


class TestBuildSnapshot:
    def test_finalizes_buckets_and_stringifies_issue_keys(self, dump_spend):
        issues = {
            3696: {
                "claude_steps": 2,
                "runs": 3,
                "runner_minutes": 4.567,
                "retry_cycles": 1,
                "workflows": {"claude.yml": 3},
            }
        }
        unattributed = dump_spend.empty_bucket()
        snapshot = dump_spend.build_snapshot(issues, unattributed)
        assert set(snapshot) == {
            "generated_at",
            "issues",
            "unattributed",
            # Provenance of the walk, so a consumer can tell a real zero from
            # an out-of-window one and a clean dump from a degraded one.
            "window_start",
            "window_days",
            "degraded",
            "minutes_source",
        }
        assert snapshot["issues"] == {
            "3696": {
                "claude_steps": 2,
                "runs": 3,
                "runner_minutes": 4.57,
                "retry_cycles": 1,
                "workflows": {"claude.yml": 3},
            }
        }
        assert snapshot["unattributed"]["runs"] == 0


class TestWindowAndDegradation:
    """#3808 round two: fixing the pagination bug exposed an unbounded walk.

    `collect()` makes one `/timing` call per completed run. This repo holds
    ~36k runs, so unbounded the dump exhausts the agent token's hourly REST
    budget mid-walk; `gh(check=True)` then raised and threw away 22 minutes of
    work (run 32001662234). The walk is now date-bounded, and a single failed
    per-run call degrades to zero and is counted rather than aborting.
    """

    def test_window_start_defaults_to_thirty_days(self, dump_spend, monkeypatch):
        monkeypatch.delenv("SPEND_WINDOW_DAYS", raising=False)
        since = dump_spend.window_start()
        assert since is not None
        expected = (
            dump_spend.datetime.now(dump_spend.UTC) - dump_spend.timedelta(days=30)
        ).strftime("%Y-%m-%d")
        assert since == expected

    def test_zero_means_all_history(self, dump_spend, monkeypatch):
        monkeypatch.setenv("SPEND_WINDOW_DAYS", "0")
        assert dump_spend.window_start() is None

    def test_bad_value_falls_back_to_default(self, dump_spend, monkeypatch):
        monkeypatch.setenv("SPEND_WINDOW_DAYS", "not-a-number")
        assert dump_spend.window_start() is not None

    def test_list_runs_sends_the_created_filter(self, dump_spend, monkeypatch):
        seen = {}

        def fake_gh(*args):
            seen["args"] = args
            return '{"workflow_runs": []}'

        monkeypatch.setattr(dump_spend, "gh", fake_gh)
        dump_spend.list_runs("o/r", "ci-tests.yml", since="2026-07-18")
        assert "created=>=2026-07-18" in seen["args"]

    def test_list_runs_omits_the_filter_for_full_history(self, dump_spend, monkeypatch):
        seen = {}

        def fake_gh(*args):
            seen["args"] = args
            return '{"workflow_runs": []}'

        monkeypatch.setattr(dump_spend, "gh", fake_gh)
        dump_spend.list_runs("o/r", "ci-tests.yml", since="")
        assert not any(str(a).startswith("created=") for a in seen["args"])

    def test_failed_timing_call_is_counted_not_fatal(self, dump_spend, monkeypatch):
        import subprocess as sp

        def boom(*args):
            raise sp.CalledProcessError(1, ["gh", *args])

        monkeypatch.setattr(dump_spend, "gh", boom)
        dump_spend.DEGRADED["timing"] = 0
        assert dump_spend.run_minutes("o/r", 123) == 0.0
        assert dump_spend.DEGRADED["timing"] == 1

    def test_failed_jobs_call_is_counted_not_fatal(self, dump_spend, monkeypatch):
        import subprocess as sp

        def boom(*args):
            raise sp.CalledProcessError(1, ["gh", *args])

        monkeypatch.setattr(dump_spend, "gh", boom)
        dump_spend.DEGRADED["claude_steps"] = 0
        assert dump_spend.claude_steps_dynamic("o/r", 123) == 0
        assert dump_spend.DEGRADED["claude_steps"] == 1

    def test_snapshot_reports_window_and_degradation(self, dump_spend, monkeypatch):
        monkeypatch.setenv("SPEND_WINDOW_DAYS", "30")
        dump_spend.DEGRADED["timing"] = 2
        snap = dump_spend.build_snapshot({}, dump_spend.empty_bucket())
        assert snap["window_start"] is not None
        assert snap["window_days"] == 30
        assert snap["degraded"]["timing"] == 2, "a degraded dump must say so in its own output"


class TestSkippedRunsAreExcluded:
    """A skipped run did nothing: no billable minutes, no executed step.

    Counting them was wrong twice over -- it inflated `runs`, and for the
    static workflows it credited a Claude step that never executed
    (claude.yml: 3,969 runs, all 3,969 skipped). It also spent one /timing
    call per no-op run, which is what put the walk beyond the token's hourly
    REST budget: 21,637 of 23,963 tracked runs in this repo are skipped.
    """

    def _run(self, rid, branch, conclusion="success"):
        return {
            "id": rid,
            "status": "completed",
            "conclusion": conclusion,
            "head_branch": branch,
        }

    def test_skipped_run_is_not_counted_at_all(self, dump_spend):
        runs = [self._run(1, "feat/3696-x", "skipped")]
        called = []
        issues, unattributed = dump_spend.collect(
            "o/r",
            list_runs_fn=lambda repo, wf: runs if wf == "claude.yml" else [],
            run_minutes_fn=lambda repo, rid: called.append(rid) or 5.0,
            claude_steps_fn=lambda repo, rid: 1,
        )
        assert issues == {}, "a skipped run must not create an issue bucket"
        assert called == [], "no /timing call may be spent on a skipped run"

    def test_cancelled_run_is_not_counted(self, dump_spend):
        runs = [self._run(1, "feat/3696-x", "cancelled")]
        issues, _ = dump_spend.collect(
            "o/r",
            list_runs_fn=lambda repo, wf: runs if wf == "claude.yml" else [],
            run_minutes_fn=lambda repo, rid: 5.0,
            claude_steps_fn=lambda repo, rid: 1,
        )
        assert issues == {}

    def test_skipped_static_run_credits_no_claude_step(self, dump_spend):
        # The correctness half: claude.yml is CLAUDE_WORKFLOWS_STATIC, so a
        # counted skipped run would add a phantom Claude step.
        runs = [self._run(1, "feat/3696-x", "skipped"), self._run(2, "feat/3696-x", "success")]
        issues, _ = dump_spend.collect(
            "o/r",
            list_runs_fn=lambda repo, wf: runs if wf == "claude.yml" else [],
            run_minutes_fn=lambda repo, rid: 1.0,
            claude_steps_fn=lambda repo, rid: 0,
        )
        assert issues[3696]["claude_steps"] == 1, "only the executed run counts"
        assert issues[3696]["runs"] == 1

    def test_successful_and_failed_runs_still_count(self, dump_spend):
        runs = [self._run(1, "feat/3696-x", "success"), self._run(2, "feat/3696-x", "failure")]
        issues, _ = dump_spend.collect(
            "o/r",
            list_runs_fn=lambda repo, wf: runs if wf == "ci-tests.yml" else [],
            run_minutes_fn=lambda repo, rid: 2.0,
            claude_steps_fn=lambda repo, rid: 0,
        )
        assert issues[3696]["runs"] == 2, "a failed run still burned runner minutes"
        assert issues[3696]["runner_minutes"] == 4.0


class TestMinutesSource:
    """#3808: nyxGPT is a public repo, so Actions minutes are free and the
    API's `billable` block is all zeros. Reading only `billable` produced a
    spend panel of zeros. `run_duration_ms` is the populated fallback, and
    the snapshot records which measure each figure came from."""

    def _timing(self, dump_spend, monkeypatch, payload):
        monkeypatch.setattr(dump_spend, "gh", lambda *a: __import__("json").dumps(payload))

    def test_falls_back_to_run_duration_when_unbilled(self, dump_spend, monkeypatch):
        dump_spend.MINUTES_SOURCE.update(billable=0, duration=0)
        self._timing(
            dump_spend,
            monkeypatch,
            {"billable": {"UBUNTU": {"total_ms": 0}}, "run_duration_ms": 120000},
        )
        assert dump_spend.run_minutes("o/r", 1) == 2.0
        assert dump_spend.MINUTES_SOURCE["duration"] == 1
        assert dump_spend.MINUTES_SOURCE["billable"] == 0

    def test_prefers_billable_when_the_repo_is_billed(self, dump_spend, monkeypatch):
        dump_spend.MINUTES_SOURCE.update(billable=0, duration=0)
        self._timing(
            dump_spend,
            monkeypatch,
            {"billable": {"UBUNTU": {"total_ms": 60000}}, "run_duration_ms": 999999},
        )
        assert dump_spend.run_minutes("o/r", 1) == 1.0
        assert dump_spend.MINUTES_SOURCE["billable"] == 1

    def test_zero_everywhere_is_still_zero(self, dump_spend, monkeypatch):
        self._timing(dump_spend, monkeypatch, {"billable": {}, "run_duration_ms": 0})
        assert dump_spend.run_minutes("o/r", 1) == 0.0
