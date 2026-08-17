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
