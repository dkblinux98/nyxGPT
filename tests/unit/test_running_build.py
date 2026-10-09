"""Unit tests for `nyxgpt.running_build` -- the running-vs-installed comparison (#4133).

The defect these pin: a `brew upgrade` on a host with the stack running left
the api serving from the *previous* version's virtualenv (a python3.11 venv
under `~/.nyxGPT/opt/nyxgpt-api/venv` that the upgrade had emptied), while
`nyxgpt ops install` reported 56/56 steps `[OK]` and every version surface
reported the new keg. Nothing available to the operator distinguished that
state from a correct one.

So the invariants under test are mostly about what this module must REFUSE to
say. A missing report, a missing expectation and a malformed `runtime` block
must each read as "could not determine" -- the one answer that is never
acceptable is a confident `match` the evidence does not support, because that
is exactly the `[OK]`-over-a-mismatch being fixed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nyxgpt.running_build import (
    BUILD_MATCH,
    BUILD_MISMATCH,
    BUILD_UNDETERMINED,
    RuntimeBuild,
    classify,
    local_runtime_build,
    same_tree,
)

pytestmark = pytest.mark.unit


def _build(prefix: str, *, exists: bool = True, pid: int = 4133, version: str = "3.0.0rc14"):
    return RuntimeBuild(
        executable=f"{prefix}/bin/python3",
        prefix=prefix,
        python="3.11.9",
        pid=pid,
        version=version,
        prefix_exists=exists,
    )


class TestLocalRuntimeBuild:
    """Self-description: the process reports its own interpreter, not a probe."""

    def test_reports_this_process(self):
        build = local_runtime_build()
        assert build.prefix == sys.prefix
        assert build.executable == sys.executable
        assert build.pid == os.getpid()
        assert build.python == ".".join(str(n) for n in sys.version_info[:3])

    def test_prefix_exists_is_true_for_a_live_interpreter(self):
        """The acute form of #4133 is `False` here, so `True` must be real."""
        assert local_runtime_build().prefix_exists is True

    def test_round_trips_through_its_dict_form(self):
        build = local_runtime_build()
        assert RuntimeBuild.from_dict(build.to_dict()) == build


class TestSameTree:
    """Both sides are realpath-ed, because the Homebrew side is a symlink by design."""

    def test_identical_paths_match(self, tmp_path):
        assert same_tree(str(tmp_path), str(tmp_path))

    def test_symlinked_opt_path_matches_the_cellar_it_resolves_to(self, tmp_path):
        """A keg service execs `<prefix>/opt/<formula>/libexec/venv`; the process
        reports the resolved Cellar path. Comparing the literals would report
        drift on every correct brew install."""
        cellar = tmp_path / "Cellar" / "nyxgpt-api@3.0.0rc" / "3.0.0rc17" / "libexec" / "venv"
        cellar.mkdir(parents=True)
        opt = tmp_path / "opt" / "nyxgpt-api@3.0.0rc"
        opt.parent.mkdir(parents=True)
        opt.symlink_to(cellar.parent.parent)

        assert same_tree(str(cellar), str(opt / "libexec" / "venv"))

    def test_a_path_inside_the_expected_root_matches(self, tmp_path):
        inner = tmp_path / "libexec" / "venv"
        inner.mkdir(parents=True)
        assert same_tree(str(inner), str(tmp_path))

    def test_two_versions_of_the_same_keg_do_not_match(self, tmp_path):
        """#4133's shape: same formula, different version directory."""
        old = tmp_path / "Cellar" / "nyxgpt-api@3.0.0rc" / "3.0.0rc14" / "libexec" / "venv"
        new = tmp_path / "Cellar" / "nyxgpt-api@3.0.0rc" / "3.0.0rc17" / "libexec" / "venv"
        assert not same_tree(str(old), str(new))

    def test_a_nonexistent_running_prefix_is_compared_rather_than_raising(self, tmp_path):
        """A deleted venv is the state worth reporting, so the comparison must
        survive it -- `Path.resolve(strict=True)` would raise instead."""
        gone = tmp_path / "deleted" / "venv"
        assert not same_tree(str(gone), str(tmp_path / "keg" / "venv"))
        assert same_tree(str(gone), str(gone))

    def test_empty_sides_never_match(self, tmp_path):
        assert not same_tree("", str(tmp_path))
        assert not same_tree(str(tmp_path), "")


class TestFromDict:
    """Tolerant parsing: `ops` reads this off an api process from another build."""

    def test_parses_a_full_block(self):
        build = RuntimeBuild.from_dict(
            {
                "executable": "/keg/venv/bin/python3",
                "prefix": "/keg/venv",
                "python": "3.12.4",
                "pid": 77,
                "version": "3.0.0rc17",
                "prefix_exists": True,
            }
        )
        assert build is not None
        assert build.prefix == "/keg/venv"
        assert build.pid == 77

    @pytest.mark.parametrize("payload", [None, {}, "runtime", [], {"prefix": ""}])
    def test_anything_without_a_prefix_is_unparseable(self, payload):
        """An api predating this field answers with no runtime block at all.
        That must read as "cannot determine", never as a match."""
        assert RuntimeBuild.from_dict(payload) is None

    def test_a_malformed_pid_does_not_lose_the_prefix(self):
        build = RuntimeBuild.from_dict({"prefix": "/keg/venv", "pid": "not-a-pid"})
        assert build is not None
        assert build.prefix == "/keg/venv"
        assert build.pid == 0

    def test_a_missing_prefix_exists_defaults_to_present(self):
        """Absent means "this build did not report it", not "the venv is gone" --
        the deleted-venv claim is only made when a process actually makes it."""
        build = RuntimeBuild.from_dict({"prefix": "/keg/venv"})
        assert build is not None
        assert build.prefix_exists is True


class TestClassify:
    """The four states, and which evidence each one requires."""

    def test_match_when_the_process_runs_the_installed_venv(self, tmp_path):
        venv = tmp_path / "libexec" / "venv"
        venv.mkdir(parents=True)
        drift = classify(_build(str(venv)), str(venv))
        assert drift.state == BUILD_MATCH
        assert not drift.mismatched

    def test_mismatch_names_both_paths(self, tmp_path):
        drift = classify(
            _build(str(tmp_path / "old" / "venv")),
            str(tmp_path / "new" / "venv"),
            remediation="nyxgpt ops restart api",
        )
        assert drift.state == BUILD_MISMATCH
        assert drift.mismatched
        assert str(tmp_path / "old" / "venv") in drift.summary()
        assert str(tmp_path / "new" / "venv") in drift.summary()
        assert "MISMATCH" in drift.summary()

    def test_mismatch_summary_is_not_a_version_string(self, tmp_path):
        """AC3: a mismatch is stated as a mismatch. The summary must not be
        satisfiable by printing the version a stale process reports."""
        drift = classify(
            _build(str(tmp_path / "old" / "venv"), version="3.0.0rc17"),
            str(tmp_path / "new" / "venv"),
        )
        assert drift.summary() != "version 3.0.0rc17"
        assert "3.0.0rc17" not in drift.summary()

    def test_a_deleted_running_prefix_says_the_next_restart_will_fail(self, tmp_path):
        drift = classify(
            _build(str(tmp_path / "gone" / "venv"), exists=False),
            str(tmp_path / "new" / "venv"),
        )
        assert drift.state == BUILD_MISMATCH
        assert "no longer exists" in drift.detail
        assert "next restart" in drift.detail

    def test_a_live_but_different_prefix_does_not_claim_deletion(self, tmp_path):
        drift = classify(
            _build(str(tmp_path / "other" / "venv"), exists=True),
            str(tmp_path / "new" / "venv"),
        )
        assert drift.state == BUILD_MISMATCH
        assert "no longer exists" not in drift.detail

    def test_the_deleted_path_is_named_and_it_is_the_running_one(self, tmp_path):
        """The acute sentence must name its subject (#4182).

        It used to read "... the installed service execs <installed>. That
        path no longer exists", where "that path" is the installed one by
        every rule of English and the running one in the code. On an upgraded
        machine the installed venv is the one path here that certainly DOES
        exist, so the sentence asserted the opposite of the truth about it --
        this issue's class exactly. Asserted on the string because the string
        is the defect.
        """
        gone = tmp_path / "gone" / "venv"
        installed = tmp_path / "new" / "venv"
        installed.mkdir(parents=True)
        drift = classify(_build(str(gone), exists=False), str(installed))
        assert f"{gone} no longer exists" in drift.detail
        assert f"{installed} no longer exists" not in drift.detail
        assert "That path no longer exists" not in drift.detail

    def test_a_symlinked_expectation_reports_what_it_resolves_to(self, tmp_path):
        """`opt` is the path that runs; the keg behind it is which build it is.

        macOS's expectation is `<prefix>/opt/<formula>/libexec/venv` by
        design, and that path reads identically before and after an upgrade.
        A surface that printed only it answered "which build is installed?"
        with a string that cannot distinguish two answers (#4182).
        """
        keg = tmp_path / "Cellar" / "nyxgpt-api@3.0.0rc" / "3.0.0rc1" / "libexec" / "venv"
        keg.mkdir(parents=True)
        opt = tmp_path / "opt" / "nyxgpt-api@3.0.0rc"
        opt.parent.mkdir(parents=True)
        opt.symlink_to(keg.parent.parent)
        expected = opt / "libexec" / "venv"

        drift = classify(_build(str(tmp_path / "old" / "venv")), str(expected))
        assert drift.state == BUILD_MISMATCH
        assert drift.expected_prefix == str(expected)
        assert drift.expected_resolved == str(keg)
        assert str(keg) in drift.detail
        assert drift.to_dict()["expected_resolved"] == str(keg)

    def test_an_unsymlinked_expectation_reports_no_second_path(self, tmp_path):
        """No symlink, nothing to resolve -- and no duplicate path printed.

        Linux's expectation is a real directory, so repeating it as "now
        <same path>" would be noise on every Linux host.
        """
        venv = tmp_path / "opt" / "nyxgpt-api" / "venv"
        venv.mkdir(parents=True)
        drift = classify(_build(str(tmp_path / "old")), str(venv))
        assert drift.expected_resolved == ""
        assert "now" not in drift.detail

    def test_no_report_is_undetermined_not_a_match(self, tmp_path):
        drift = classify(None, str(tmp_path / "venv"))
        assert drift.state == BUILD_UNDETERMINED
        assert not drift.mismatched
        assert drift.running_prefix == ""

    def test_no_expectation_is_undetermined_not_a_mismatch(self, tmp_path):
        """Failing to locate the installed venv must not send an operator to
        restart a service that is fine."""
        drift = classify(_build(str(tmp_path / "venv")), None)
        assert drift.state == BUILD_UNDETERMINED
        assert not drift.mismatched

    def test_undetermined_detail_is_the_callers_reason_when_given(self):
        drift = classify(None, "/keg/venv", undetermined_detail="connection refused")
        assert "connection refused" in drift.summary()

    def test_remediation_is_carried_on_a_mismatch_and_dropped_on_a_match(self, tmp_path):
        venv = tmp_path / "venv"
        venv.mkdir()
        assert classify(_build(str(venv)), str(venv), remediation="fix-me").remediation == ""
        assert (
            classify(_build("/elsewhere"), str(venv), remediation="fix-me").remediation == "fix-me"
        )

    def test_to_dict_carries_the_summary_for_the_dashboard(self, tmp_path):
        drift = classify(
            _build(str(tmp_path / "old")),
            str(tmp_path / "new"),
            remediation="nyxgpt ops restart api",
        )
        payload = drift.to_dict()
        assert payload["state"] == BUILD_MISMATCH
        assert payload["running"]["prefix"] == str(tmp_path / "old")
        assert payload["expected_prefix"] == str(tmp_path / "new")
        assert payload["remediation"] == "nyxgpt ops restart api"
        assert payload["summary"] == drift.summary()

    def test_to_dict_tolerates_no_running_report(self):
        assert classify(None, "/keg/venv").to_dict()["running"] is None


class TestRealWorldPaths:
    """The two concrete paths from #4133's report, asserted as a pair."""

    def test_the_owners_stale_path_mismatches_the_rc17_keg(self):
        running = RuntimeBuild(
            executable=str(Path.home() / ".nyxGPT/opt/nyxgpt-api/venv/bin/python3"),
            prefix=str(Path.home() / ".nyxGPT/opt/nyxgpt-api/venv"),
            python="3.11.9",
            pid=512,
            version="3.0.0rc17",
            prefix_exists=False,
        )
        expected = "/opt/homebrew/Cellar/nyxgpt-api@3.0.0rc/3.0.0rc17/libexec/venv"
        drift = classify(running, expected, remediation="nyxgpt ops restart api")

        # The version the stale process reports is plausible and useless: it
        # is the same string the keg carries. Only the prefix separates them.
        assert running.version == "3.0.0rc17"
        assert drift.state == BUILD_MISMATCH
        assert drift.remediation == "nyxgpt ops restart api"
