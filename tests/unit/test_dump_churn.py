"""Unit tests for scripts/retrospective/dump_churn.py (#3776).

Covers the pure log-parsing, split-methodology, rollup, pricing and incident
validation logic; `collect()` is exercised with injected fake `gh`-calling
functions so no subprocess or GitHub API access is required.
"""

from __future__ import annotations

import importlib.util
import json
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
def dump_churn():
    return _load("dump_churn", RETRO_DIR / "dump_churn.py")


def _log(*entries):
    """Build a timestamped job log from (timestamp, body) pairs."""
    return "\n".join(f"{ts} {body}" for ts, body in entries)


class TestStepClassification:
    def test_only_real_action_invocations_count_as_rounds(self, dump_churn):
        assert dump_churn.is_claude_step(
            {"name": "Run Claude Code to implement issue (Initial)", "conclusion": "success"}
        )
        assert dump_churn.is_claude_step(
            {"name": "Claude Fix Issues (Attempt 2)", "conclusion": "failure"}
        )
        # mentions Claude but spends no tokens
        assert not dump_churn.is_claude_step(
            {"name": "Check Claude progress completion", "conclusion": "success"}
        )
        assert not dump_churn.is_claude_step(
            {"name": "Read Claude analysis result (Phase 3)", "conclusion": "success"}
        )

    def test_skipped_steps_are_not_rounds(self, dump_churn):
        assert not dump_churn.is_claude_step(
            {"name": "Run Claude Code to fix review issues (Review Fix)", "conclusion": "skipped"}
        )
        assert not dump_churn.is_claude_step({"name": "Run Claude Code", "conclusion": None})

    @pytest.mark.parametrize(
        "name,kind",
        [
            ("Run Claude Code to implement issue (Initial)", "implement"),
            ("Run Claude Code to fix review issues (Review Fix)", "review-fix"),
            ("Run Claude Code to fix acceptance failure", "acceptance-fix"),
            ("Claude Fix Issues (Attempt 2)", "self-heal"),
            ("Run Claude Code Review", "review"),
            ("Run Claude Code", "session"),
        ],
    )
    def test_round_kind_mapping(self, dump_churn, name, kind):
        assert dump_churn.round_kind(name) == kind


class TestSplitLogBySteps:
    STEPS = [
        {
            "name": "step-a",
            "started_at": "2026-08-14T10:00:00Z",
            "completed_at": "2026-08-14T10:05:00Z",
        },
        {
            "name": "step-b",
            "started_at": "2026-08-14T10:06:00Z",
            "completed_at": "2026-08-14T10:09:00Z",
        },
    ]

    def test_lines_land_in_the_step_whose_window_contains_them(self, dump_churn):
        log = _log(
            ("2026-08-14T10:00:01.1234567Z", "alpha"),
            ("2026-08-14T10:04:59.0000000Z", "beta"),
            ("2026-08-14T10:07:00.0000000Z", "gamma"),
        )
        segments = dump_churn.split_log_by_steps(log, self.STEPS)
        assert segments["step-a"] == ["alpha", "beta"]
        assert segments["step-b"] == ["gamma"]

    def test_lines_outside_every_window_are_dropped(self, dump_churn):
        log = _log(("2026-08-14T09:59:00.0000000Z", "setup noise"))
        assert dump_churn.split_log_by_steps(log, self.STEPS) == {}

    def test_untimestamped_continuation_stays_with_the_current_step(self, dump_churn):
        log = _log(("2026-08-14T10:00:01.0000000Z", "alpha")) + "\n    continued"
        assert dump_churn.split_log_by_steps(log, self.STEPS)["step-a"] == [
            "alpha",
            "    continued",
        ]

    def test_step_with_no_start_time_is_ignored(self, dump_churn):
        segments = dump_churn.split_log_by_steps(
            _log(("2026-08-14T10:00:01.0000000Z", "alpha")),
            [{"name": "queued", "started_at": None, "completed_at": None}],
        )
        assert segments == {}


class TestParseUsage:
    def test_terminal_result_event_wins_over_per_message_usage(self, dump_churn):
        usage = dump_churn.parse_usage(
            [
                '{"type":"assistant","message":{"usage":{"input_tokens":10,"output_tokens":5}}}',
                '{"type":"assistant","message":{"usage":{"input_tokens":20,"output_tokens":7}}}',
                '{"type":"result","total_cost_usd":0.42,"usage":{"input_tokens":30,'
                '"output_tokens":12,"cache_creation_input_tokens":100,"cache_read_input_tokens":900}}',
            ]
        )
        assert usage["tokens"] == {
            "input": 30,
            "output": 12,
            "cache_creation": 100,
            "cache_read": 900,
            "total": 1042,
        }
        assert usage["cost_usd_reported"] == pytest.approx(0.42)

    def test_without_a_terminal_event_per_message_usage_is_summed(self, dump_churn):
        usage = dump_churn.parse_usage(
            [
                '{"type":"assistant","message":{"usage":{"input_tokens":10,"output_tokens":5}}}',
                '{"type":"assistant","message":{"usage":{"input_tokens":20,"output_tokens":7}}}',
            ]
        )
        assert usage["tokens"]["input"] == 30
        assert usage["tokens"]["total"] == 42

    def test_no_usage_at_all_is_an_honest_unknown_not_a_zero(self, dump_churn):
        usage = dump_churn.parse_usage(["Run anthropics/claude-code-action@v1", "log expired"])
        assert usage["tokens"] is None
        assert usage["turns"] == 0

    def test_turns_and_first_mutation_are_tracked_in_order(self, dump_churn):
        usage = dump_churn.parse_usage(
            [
                '{"type":"assistant"} tool_use {"name":"Read"}',
                '{"type":"assistant"} tool_use {"name":"Grep"}',
                '{"type":"assistant"} tool_use {"name":"Edit"}',
                '{"type":"assistant"} tool_use {"name":"Bash"}',
            ]
        )
        assert usage["turns"] == 4
        assert usage["first_mutation_turn"] == 3

    def test_model_is_taken_from_the_log(self, dump_churn):
        usage = dump_churn.parse_usage(['{"model":"claude-opus-5","type":"assistant"}'])
        assert usage["model"] == "claude-opus-5"

    def test_thousands_separators_are_parsed(self, dump_churn):
        usage = dump_churn.parse_usage(
            ['total_cost_usd=1.5 input_tokens: "1,234" output_tokens: 6']
        )
        assert usage["tokens"]["input"] == 1234


class TestSplitTokens:
    def test_pro_rata_split_by_turn_ratio(self, dump_churn):
        split = dump_churn.split_tokens(
            {"tokens": {"total": 1000}, "turns": 10, "first_mutation_turn": 5}
        )
        # turns 1-4 are context, 5-10 production
        assert split["context_turns"] == 4
        assert split["production_turns"] == 6
        assert split["context_tokens"] == 400
        assert split["production_tokens"] == 600
        assert split["source"] == "turns"

    def test_step_that_never_modified_a_file_is_all_context(self, dump_churn):
        split = dump_churn.split_tokens(
            {"tokens": {"total": 500}, "turns": 3, "first_mutation_turn": None}
        )
        assert (split["context_tokens"], split["production_tokens"]) == (500, 0)
        assert split["source"] == "no-change"

    def test_mutation_on_the_first_turn_leaves_no_context_tokens(self, dump_churn):
        split = dump_churn.split_tokens(
            {"tokens": {"total": 800}, "turns": 4, "first_mutation_turn": 1}
        )
        assert split["context_tokens"] == 0
        assert split["production_tokens"] == 800

    def test_no_turn_markers_yields_an_unknown_split(self, dump_churn):
        split = dump_churn.split_tokens({"tokens": None, "turns": 0, "first_mutation_turn": None})
        assert split["source"] == "unknown"
        assert split["context_tokens"] is None
        assert split["production_tokens"] is None


class TestCollect:
    RUN = {
        "id": 900,
        "status": "completed",
        "head_branch": "feat/3753-formula",
        "run_started_at": "2026-08-14T10:00:00Z",
        "path": ".github/workflows/developer_auto_implement.yml",
    }
    JOB = {
        "id": 91,
        "steps": [
            {
                "name": "Run Claude Code to implement issue (Initial)",
                "conclusion": "success",
                "started_at": "2026-08-14T10:00:00Z",
                "completed_at": "2026-08-14T10:05:00Z",
            },
            {
                "name": "Check Claude progress completion",
                "conclusion": "success",
                "started_at": "2026-08-14T10:05:01Z",
                "completed_at": "2026-08-14T10:05:02Z",
            },
        ],
    }
    LOG = _log(
        (
            "2026-08-14T10:00:10.0000000Z",
            '{"type":"assistant","model":"claude-opus-5"} {"name":"Read"}',
        ),
        ("2026-08-14T10:01:10.0000000Z", '{"type":"assistant"} {"name":"Edit"}'),
        (
            "2026-08-14T10:04:10.0000000Z",
            '{"type":"result","total_cost_usd":0.5,"usage":{"input_tokens":600,"output_tokens":400}}',
        ),
        ("2026-08-14T10:05:01.5000000Z", "not a claude step"),
    )

    def _fakes(self, runs_by_workflow, jobs, log):
        return (
            lambda repo, wf: runs_by_workflow.get(wf, []),
            lambda repo, run_id: jobs,
            lambda repo, job_id: log,
        )

    def test_walks_claude_steps_and_attributes_them_to_an_issue(self, dump_churn):
        since = dump_churn.parse_ts("2026-08-01T00:00:00Z")
        list_runs_fn, jobs_fn, log_fn = self._fakes(
            {"developer_auto_implement.yml": [self.RUN]}, [self.JOB], self.LOG
        )
        rounds = dump_churn.collect(
            "owner/repo", since, list_runs_fn=list_runs_fn, jobs_fn=jobs_fn, job_log_fn=log_fn
        )
        assert len(rounds) == 1  # the non-action "Check Claude progress" step is not a round
        r = rounds[0]
        assert (r["issue"], r["kind"], r["workflow"]) == (
            3753,
            "implement",
            "developer_auto_implement.yml",
        )
        assert r["tokens"]["total"] == 1000
        assert r["model"] == "claude-opus-5"
        assert r["split"]["context_tokens"] == 500  # 1 of 2 turns before the Edit

    def test_runs_outside_the_window_are_skipped(self, dump_churn):
        since = dump_churn.parse_ts("2026-08-20T00:00:00Z")
        list_runs_fn, jobs_fn, log_fn = self._fakes(
            {"developer_auto_implement.yml": [self.RUN]}, [self.JOB], self.LOG
        )
        assert (
            dump_churn.collect(
                "owner/repo", since, list_runs_fn=list_runs_fn, jobs_fn=jobs_fn, job_log_fn=log_fn
            )
            == []
        )

    def test_incomplete_runs_are_skipped(self, dump_churn):
        run = {**self.RUN, "status": "in_progress"}
        since = dump_churn.parse_ts("2026-08-01T00:00:00Z")
        list_runs_fn, jobs_fn, log_fn = self._fakes(
            {"developer_auto_implement.yml": [run]}, [self.JOB], self.LOG
        )
        assert (
            dump_churn.collect(
                "owner/repo", since, list_runs_fn=list_runs_fn, jobs_fn=jobs_fn, job_log_fn=log_fn
            )
            == []
        )

    def test_expired_log_still_records_the_round_with_unknown_tokens(self, dump_churn):
        since = dump_churn.parse_ts("2026-08-01T00:00:00Z")
        list_runs_fn, jobs_fn, log_fn = self._fakes(
            {"developer_auto_implement.yml": [self.RUN]}, [self.JOB], ""
        )
        rounds = dump_churn.collect(
            "owner/repo", since, list_runs_fn=list_runs_fn, jobs_fn=jobs_fn, job_log_fn=log_fn
        )
        assert rounds[0]["tokens"] is None
        assert rounds[0]["split"]["source"] == "unknown"


class TestMergeAndNumberRounds:
    def _round(self, job_id, step, started, issue=1, kind="implement", total=100, ctx=40):
        return {
            "issue": issue,
            "job_id": job_id,
            "step": step,
            "kind": kind,
            "started_at": started,
            "tokens": {
                "input": total,
                "output": 0,
                "cache_creation": 0,
                "cache_read": 0,
                "total": total,
            },
            "split": {
                "context_tokens": ctx,
                "production_tokens": total - ctx,
                "source": "turns",
            },
        }

    def test_merge_keeps_history_and_refreshes_rewalked_rounds(self, dump_churn):
        old = [self._round(1, "a", "2026-07-01T00:00:00Z", total=10, ctx=1)]
        fresh = [
            self._round(1, "a", "2026-07-01T00:00:00Z", total=99, ctx=9),
            self._round(2, "b", "2026-08-01T00:00:00Z"),
        ]
        merged = dump_churn.merge_rounds(old, fresh)
        assert len(merged) == 2
        assert merged[0]["tokens"]["total"] == 99  # re-walk wins over the stored copy
        assert [r["job_id"] for r in merged] == [1, 2]  # chronological

    def test_rounds_are_numbered_per_issue_and_per_kind(self, dump_churn):
        rounds = [
            self._round(1, "a", "2026-08-01T00:00:00Z", kind="implement"),
            self._round(2, "b", "2026-08-02T00:00:00Z", kind="review-fix"),
            self._round(3, "c", "2026-08-03T00:00:00Z", kind="review-fix"),
            self._round(4, "d", "2026-08-04T00:00:00Z", issue=2, kind="implement"),
        ]
        dump_churn.number_rounds(rounds)
        assert [(r["round"], r["kind_round"]) for r in rounds] == [(1, 1), (2, 1), (3, 2), (1, 1)]

    def test_unattributed_rounds_get_no_ordinal(self, dump_churn):
        rounds = [self._round(1, "a", "2026-08-01T00:00:00Z", issue=None)]
        dump_churn.number_rounds(rounds)
        assert rounds[0]["round"] is None


class TestRollupAndTotals:
    def _round(self, issue, started, ctx, prod, round_no=None, kind="implement"):
        total = ctx + prod
        return {
            "issue": issue,
            "job_id": hash((issue, started)) % 1000,
            "step": started,
            "kind": kind,
            "started_at": started,
            "round": round_no,
            "model": "claude-opus-5",
            "tokens": {
                "input": total,
                "output": 0,
                "cache_creation": 0,
                "cache_read": 0,
                "total": total,
            },
            "split": {"context_tokens": ctx, "production_tokens": prod, "source": "turns"},
        }

    def test_repeat_round_context_is_the_onboarding_tax(self, dump_churn):
        rounds = [
            self._round(3753, "2026-08-10T00:00:00Z", 400, 600, round_no=1),
            self._round(3753, "2026-08-11T00:00:00Z", 900, 100, round_no=2, kind="review-fix"),
        ]
        issues = dump_churn.rollup_issues(rounds)
        entry = issues[3753]
        assert entry["rounds"] == 2
        assert entry["tokens"]["total"] == 2000
        assert entry["context_tokens"] == 1300
        assert entry["production_tokens"] == 700
        assert entry["context_pct"] == 65.0
        # only round 2's context is tax a persistent memory would have saved
        assert entry["repeat_context_tokens"] == 900
        assert entry["kinds"] == {"implement": 1, "review-fix": 1}
        assert (entry["first_round_at"], entry["last_round_at"]) == (
            "2026-08-10T00:00:00Z",
            "2026-08-11T00:00:00Z",
        )

    def test_unknown_split_rounds_count_tokens_but_not_the_split(self, dump_churn):
        rounds = [
            self._round(7, "2026-08-10T00:00:00Z", 100, 100, round_no=1),
            {
                "issue": 7,
                "job_id": 5,
                "step": "s",
                "kind": "implement",
                "started_at": "2026-08-11T00:00:00Z",
                "round": 2,
                "tokens": {
                    "input": 50,
                    "output": 0,
                    "cache_creation": 0,
                    "cache_read": 0,
                    "total": 50,
                },
                "split": {"context_tokens": None, "production_tokens": None, "source": "unknown"},
            },
        ]
        entry = dump_churn.rollup_issues(rounds)[7]
        assert entry["tokens"]["total"] == 250
        assert entry["context_tokens"] + entry["production_tokens"] == 200
        assert entry["unknown_split_rounds"] == 1

    def test_unattributed_rounds_are_excluded_from_the_rollup(self, dump_churn):
        rounds = [self._round(None, "2026-08-10T00:00:00Z", 10, 10, round_no=None)]
        assert dump_churn.rollup_issues(rounds) == {}

    def test_totals_aggregate_across_issues(self, dump_churn):
        rounds = [
            self._round(1, "2026-08-10T00:00:00Z", 100, 100, round_no=1),
            self._round(1, "2026-08-11T00:00:00Z", 300, 100, round_no=2),
            self._round(2, "2026-08-12T00:00:00Z", 50, 150, round_no=1),
        ]
        issues = dump_churn.rollup_issues(rounds)
        totals = dump_churn.totals_of(rounds, issues)
        assert totals["rounds"] == 3
        assert totals["issues"] == 2
        assert totals["tokens"]["total"] == 800
        assert totals["context_tokens"] == 450
        assert totals["repeat_context_tokens"] == 300
        assert totals["multi_round_issues"] == 1
        assert totals["context_pct"] == 56.2  # 450/800, banker's rounding
        assert totals["priced"] is False
        assert totals["cost_usd"] is None


class TestPricing:
    SHEET = {
        "default": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3},
        "models": {"claude-opus-5": {"input": 6.0, "output": 30.0, "cache_read": 0.6}},
    }

    def test_no_sheet_means_no_dollars(self, dump_churn):
        assert dump_churn.price_tokens({"input": 1_000_000}, "claude-opus-5", None) is None

    def test_model_rates_win_over_the_default(self, dump_churn):
        tokens = {"input": 1_000_000, "output": 0, "cache_creation": 0, "cache_read": 0}
        assert dump_churn.price_tokens(tokens, "claude-opus-5", self.SHEET) == pytest.approx(6.0)
        assert dump_churn.price_tokens(tokens, "some-other-model", self.SHEET) == pytest.approx(3.0)

    def test_cache_write_falls_back_to_the_input_rate(self, dump_churn):
        tokens = {"input": 0, "output": 0, "cache_creation": 1_000_000, "cache_read": 0}
        # the opus entry has no cache_write key
        assert dump_churn.price_tokens(tokens, "claude-opus-5", self.SHEET) == pytest.approx(6.0)

    def test_issue_cost_sums_its_rounds(self, dump_churn):
        rounds = [
            {
                "issue": 5,
                "kind": "implement",
                "round": 1,
                "started_at": "2026-08-10T00:00:00Z",
                "model": "claude-opus-5",
                "tokens": {
                    "input": 1_000_000,
                    "output": 100_000,
                    "cache_creation": 0,
                    "cache_read": 0,
                    "total": 1_100_000,
                },
                "split": {"context_tokens": 100, "production_tokens": 100, "source": "turns"},
            }
        ]
        issues = dump_churn.rollup_issues(rounds, self.SHEET)
        assert issues[5]["cost_usd"] == pytest.approx(9.0)  # 6.00 input + 3.00 output
        assert dump_churn.totals_of(rounds, issues, self.SHEET)["priced"] is True


class TestIncidents:
    def _doc(self, **overrides):
        doc = {
            "kinds": {"stale-lane-state": "…"},
            "incidents": [
                {
                    "id": "2026-08-14-x",
                    "date": "2026-08-14",
                    "kind": "stale-lane-state",
                    "title": "t",
                    "summary": "s",
                    "recordedIn": 3776,
                }
            ],
        }
        doc.update(overrides)
        return doc

    def test_valid_document_passes(self, dump_churn):
        assert dump_churn.validate_incidents(self._doc())["incidents"]

    @pytest.mark.parametrize("field", ["id", "date", "kind", "title", "summary", "recordedIn"])
    def test_missing_required_field_is_rejected(self, dump_churn, field):
        doc = self._doc()
        doc["incidents"][0].pop(field)
        with pytest.raises(ValueError):
            dump_churn.validate_incidents(doc)

    def test_bad_date_and_unknown_kind_and_duplicate_id_are_rejected(self, dump_churn):
        bad_date = self._doc()
        bad_date["incidents"][0]["date"] = "14/08/2026"
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            dump_churn.validate_incidents(bad_date)

        bad_kind = self._doc()
        bad_kind["incidents"][0]["kind"] = "invented-kind"
        with pytest.raises(ValueError, match="unknown kind"):
            dump_churn.validate_incidents(bad_kind)

        dupe = self._doc()
        dupe["incidents"].append(dict(dupe["incidents"][0]))
        with pytest.raises(ValueError, match="duplicate"):
            dump_churn.validate_incidents(dupe)

    def test_incidents_must_be_a_list(self, dump_churn):
        with pytest.raises(ValueError, match="must be a list"):
            dump_churn.validate_incidents({"incidents": {}})

    def test_tally_groups_by_kind_and_month(self, dump_churn):
        doc = self._doc()
        doc["kinds"]["stale-run-state"] = "…"
        doc["incidents"].append(
            {
                "id": "2026-07-01-y",
                "date": "2026-07-01",
                "kind": "stale-run-state",
                "title": "t2",
                "summary": "s2",
                "recordedIn": 1,
            }
        )
        tally = dump_churn.incident_tally(dump_churn.validate_incidents(doc))
        assert tally["total"] == 2
        assert tally["byKind"] == {"stale-lane-state": 1, "stale-run-state": 1}
        assert tally["byMonth"] == {"2026-07": 1, "2026-08": 1}
        assert [i["date"] for i in tally["incidents"]] == ["2026-08-14", "2026-07-01"]

    def test_absent_file_is_tolerated(self, dump_churn, tmp_path):
        assert dump_churn.load_incidents(tmp_path / "nope.json") is None
        assert dump_churn.incident_tally(None) is None

    def test_committed_seed_file_is_valid(self, dump_churn):
        doc = dump_churn.load_incidents(RETRO_DIR / "data" / "stale_context_incidents.json")
        tally = dump_churn.incident_tally(doc)
        assert tally["total"] >= 3  # seeded with the 2026-08-14 incidents
        assert doc["howToRecord"]

    def test_committed_price_sheet_example_is_not_loaded_as_a_live_sheet(
        self, dump_churn, tmp_path
    ):
        # prices are owner-configured; only price_sheet.json counts
        assert dump_churn.load_price_sheet(tmp_path / "price_sheet.json") is None
        example = json.loads((RETRO_DIR / "data" / "price_sheet.example.json").read_text())
        assert set(example["default"]) == {"input", "output", "cache_write", "cache_read"}


class TestBuildSnapshot:
    def test_snapshot_shape(self, dump_churn):
        rounds = [
            {
                "issue": 42,
                "job_id": 1,
                "step": "Run Claude Code to implement issue (Initial)",
                "kind": "implement",
                "started_at": "2026-08-10T00:00:00Z",
                "model": None,
                "tokens": {
                    "input": 10,
                    "output": 10,
                    "cache_creation": 0,
                    "cache_read": 0,
                    "total": 20,
                },
                "split": {"context_tokens": 5, "production_tokens": 15, "source": "turns"},
            }
        ]
        snapshot = dump_churn.build_snapshot(rounds)
        assert set(snapshot) == {
            "generated_at",
            "methodology",
            "rounds",
            "issues",
            "totals",
            "staleContextIncidents",
        }
        assert set(snapshot["issues"]) == {"42"}
        assert snapshot["rounds"][0]["round"] == 1
        assert "first file-modifying tool use" in snapshot["methodology"]["split"]
        assert snapshot["staleContextIncidents"] is None
