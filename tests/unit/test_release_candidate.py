"""Unit tests for the single PyPI publish pipeline -- dev, rc, stable (#3727).

Nothing here reaches PyPI or GitHub: the published-version lookup, the run
history lookup and the workflow dispatch are replaced with recorders, so the
tests assert on the part that actually has to be right -- the version
arithmetic, the guardrails, the nightly's no-op condition, and the fact that
a dev or rc build can never affect a plain `pip install nyxgpt`.

Two later sections are the acceptance criteria this feature exists for: a
pinned pre-release must survive the whole repo-less provisioning path
(`nyxgpt cloud user-data`, `nyxgpt cloud deploy`) unchanged, because a build
nobody can install on a clean machine would be useless; and there must be
exactly ONE publish pipeline -- the ceremony delegates its PyPI phase to the
same workflow rather than uploading on its own.
"""

import argparse
import io
import json
from pathlib import Path

import pytest

from nyxgpt import release_candidate as rc

pytestmark = pytest.mark.unit


PUBLISHED = ("2.1.0", "3.0.0rc1", "3.0.0rc2", "1.0.0", "3.0.0.dev5")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _args(**overrides) -> argparse.Namespace:
    base = {
        "branch": "v3.0.0",
        "channel": "rc",
        "publish": False,
        "number": None,
        "json": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --- Version arithmetic ---------------------------------------------------


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("v3.0.0", "3.0.0"),
        ("v10.2.13", "10.2.13"),
        ("  v3.0.0  ", "3.0.0"),
        ("master", None),
        ("main", None),
        ("feat/3727-release-candidate", None),
        ("v3.0", None),
        ("v3.0.0-hotfix", None),
        ("", None),
    ],
)
def test_release_branch_version_recognizes_only_release_branches(branch, expected):
    assert rc.release_branch_version(branch) == expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [("3.0.0rc1", ("3.0.0", 1)), ("3.0.0rc12", ("3.0.0", 12)), ("3.0.0", None), ("3.0.0b1", None)],
)
def test_parse_rc_version(version, expected):
    assert rc.parse_rc_version(version) == expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [("3.0.0.dev1", ("3.0.0", 1)), ("3.0.0.dev41", ("3.0.0", 41)), ("3.0.0rc1", None)],
)
def test_parse_dev_version(version, expected):
    assert rc.parse_dev_version(version) == expected


@pytest.mark.parametrize("version", ["3.0.0rc1", "3.0.0a1", "3.0.0b2", "3.0.0.dev4"])
def test_prereleases_are_recognized(version):
    assert rc.is_prerelease(version) is True


@pytest.mark.parametrize("version", ["3.0.0", "2.1.0", "10.0.1"])
def test_stable_releases_are_not_prereleases(version):
    assert rc.is_prerelease(version) is False


def test_release_line_strips_every_prerelease_suffix():
    assert rc.release_line("3.0.0rc7") == "3.0.0"
    assert rc.release_line("3.0.0.dev7") == "3.0.0"
    assert rc.release_line("3.0.0") == "3.0.0"


def test_next_rc_number_continues_after_the_highest_published():
    assert rc.next_rc_number("3.0.0", PUBLISHED) == 3
    assert rc.next_rc_version("3.0.0", PUBLISHED) == "3.0.0rc3"


def test_next_rc_number_starts_at_one_for_an_unstarted_line():
    assert rc.next_rc_number("3.1.0", PUBLISHED) == 1
    assert rc.next_rc_version("3.1.0", PUBLISHED) == "3.1.0rc1"


def test_next_rc_number_ignores_other_release_lines():
    """A `2.1.0rc9` must not push the 3.0.0 line's numbering."""
    assert rc.next_rc_number("3.0.0", ("2.1.0rc9", "3.0.0rc1")) == 2


def test_next_rc_number_is_not_fooled_by_gaps_or_ordering():
    """Lexical sorting would call rc9 the highest; the number is what counts."""
    assert rc.next_rc_number("3.0.0", ("3.0.0rc9", "3.0.0rc10")) == 11


def test_dev_and_rc_numbering_are_independent():
    """A published `3.0.0.dev5` must not consume an RC number, or vice versa."""
    assert rc.next_dev_number("3.0.0", PUBLISHED) == 6
    assert rc.next_dev_version("3.0.0", PUBLISHED) == "3.0.0.dev6"
    assert rc.next_rc_number("3.0.0", PUBLISHED) == 3


def test_rc_version_rejects_a_non_release_base():
    with pytest.raises(rc.ReleaseCandidateError, match="not a release version"):
        rc.rc_version("3.0", 1)


def test_rc_version_rejects_a_zero_or_negative_number():
    with pytest.raises(rc.ReleaseCandidateError, match="1 or greater"):
        rc.rc_version("3.0.0", 0)


def test_dev_version_rejects_a_zero_number():
    with pytest.raises(rc.ReleaseCandidateError, match="1 or greater"):
        rc.dev_version("3.0.0", 0)


def test_pep440_orders_dev_below_rc_below_the_release():
    """The ordering guarantee: a nightly can never shadow an RC or the release."""
    from packaging.version import Version

    assert Version("3.0.0.dev99") < Version("3.0.0rc1") < Version("3.0.0rc2") < Version("3.0.0")


def test_channel_version_composes_each_channel():
    assert rc.channel_version("dev", "3.0.0", 7) == "3.0.0.dev7"
    assert rc.channel_version("rc", "3.0.0", 7) == "3.0.0rc7"
    assert rc.channel_version("stable", "3.0.0") == "3.0.0"


def test_channel_version_refuses_a_number_on_the_stable_channel():
    """There is no "release 3.0.0, build 4" -- dropping the number silently
    would let `--number 4` look honoured while publishing something else."""
    with pytest.raises(rc.ReleaseCandidateError, match="takes no build number"):
        rc.channel_version("stable", "3.0.0", 4)


def test_channel_version_rejects_an_unknown_channel():
    with pytest.raises(rc.ReleaseCandidateError, match="Unknown channel"):
        rc.channel_version("nightly-ish", "3.0.0", 1)


def test_a_dev_or_rc_build_is_always_a_prerelease():
    """The load-bearing guarantee: `pip install nyxgpt` can never resolve to one."""
    for number in (1, 2, 17):
        assert rc.is_prerelease(rc.channel_version("rc", "3.0.0", number))
        assert rc.is_prerelease(rc.channel_version("dev", "3.0.0", number))
    # ...and the stable channel is, by construction, not one.
    assert rc.is_prerelease(rc.channel_version("stable", "3.0.0")) is False


def test_resolve_dev_number_uses_the_run_number_so_every_nightly_is_unique():
    """PyPI versions are immutable -- two nightlies must never claim one version."""
    assert rc.resolve_dev_number("3.0.0", PUBLISHED, run_number=42) == 42


def test_resolve_dev_number_never_reuses_a_published_build():
    """A fresh workflow file restarts its run counter at 1; PyPI already has dev5."""
    assert rc.resolve_dev_number("3.0.0", PUBLISHED, run_number=1) == 6


def test_resolve_dev_number_falls_back_to_the_next_free_number():
    assert rc.resolve_dev_number("3.0.0", PUBLISHED) == 6


# --- pyproject pinning ----------------------------------------------------


PYPROJECT = """[build-system]
requires = ["setuptools>=68"]

[project]
name = "nyxGPT"
version = "3.0.0"
description = "Local ChatGPT-style system"

[tool.something]
version = "9.9.9"
"""


def test_pin_version_rewrites_only_the_project_table():
    pinned = rc.pin_version(PYPROJECT, "3.0.0rc3")

    assert 'version = "3.0.0rc3"' in pinned
    # The unrelated tool table keeps its own version.
    assert 'version = "9.9.9"' in pinned
    assert 'version = "3.0.0"\n' not in pinned
    # Everything else survives untouched.
    assert 'name = "nyxGPT"' in pinned
    assert pinned.count("\n") == PYPROJECT.count("\n")


def test_pin_version_accepts_a_dev_build():
    import tomllib

    assert tomllib.loads(rc.pin_version(PYPROJECT, "3.0.0.dev9"))["project"]["version"] == (
        "3.0.0.dev9"
    )


def test_pin_version_result_is_still_parseable_toml():
    import tomllib

    data = tomllib.loads(rc.pin_version(PYPROJECT, "3.0.0rc3"))

    assert data["project"]["version"] == "3.0.0rc3"
    assert data["tool"]["something"]["version"] == "9.9.9"


def test_pin_version_refuses_a_version_that_is_not_a_release_rc_or_dev():
    with pytest.raises(rc.ReleaseCandidateError, match="unrecognized version"):
        rc.pin_version(PYPROJECT, "not-a-version")


def test_pin_version_reports_a_pyproject_with_no_project_version():
    with pytest.raises(rc.ReleaseCandidateError, match=r"\[project\]"):
        rc.pin_version('[project]\nname = "nyxGPT"\n', "3.0.0rc1")


def test_the_real_pyproject_can_be_pinned():
    """The shipped pyproject.toml is what the publish workflow rewrites."""
    import tomllib

    pyproject = REPO_ROOT / "pyproject.toml"
    pinned = rc.pin_version(pyproject.read_text(encoding="utf-8"), "3.0.0rc1")

    assert tomllib.loads(pinned)["project"]["version"] == "3.0.0rc1"


# --- The plan -------------------------------------------------------------


def test_plan_from_the_release_branch_is_publishable():
    plan = rc.plan("v3.0.0", published=PUBLISHED)

    assert plan["publishable"] is True
    assert plan["blockers"] == []
    assert plan["release"] == "3.0.0"
    assert plan["channel"] == "rc"
    assert plan["version"] == "3.0.0rc3"
    assert plan["next_rc_version"] == "3.0.0rc3"
    assert plan["published_rcs"] == ["3.0.0rc1", "3.0.0rc2"]
    assert plan["published_dev_builds"] == ["3.0.0.dev5"]
    assert plan["is_prerelease"] is True
    assert plan["workflow"] == rc.PUBLISH_WORKFLOW_FILE


def test_plan_for_the_dev_channel_uses_the_run_number():
    plan = rc.plan("v3.0.0", "dev", published=PUBLISHED, run_number=31)

    assert plan["channel"] == "dev"
    assert plan["version"] == "3.0.0.dev31"
    assert plan["is_prerelease"] is True
    assert plan["commands"]["install"] == "pip install nyxgpt==3.0.0.dev31"


def test_plan_for_the_stable_channel_is_the_bare_release():
    plan = rc.plan("v3.0.0", "stable", published=PUBLISHED)

    assert plan["version"] == "3.0.0"
    assert plan["is_prerelease"] is False
    assert plan["publishable"] is True


def test_plan_refuses_a_version_pypi_already_serves():
    """PyPI versions are immutable -- publishing 3.0.0 twice cannot work."""
    plan = rc.plan("v3.0.0", "stable", published=(*PUBLISHED, "3.0.0"))

    assert plan["publishable"] is False
    assert any("immutable" in blocker for blocker in plan["blockers"])


def test_plan_refuses_an_unknown_channel():
    with pytest.raises(rc.ReleaseCandidateError, match="Unknown channel"):
        rc.plan("v3.0.0", "nightly", published=PUBLISHED)


def test_plan_refuses_a_non_release_branch():
    plan = rc.plan("feat/3727-something", published=PUBLISHED)

    assert plan["publishable"] is False
    assert any("not a release branch" in blocker for blocker in plan["blockers"])


def test_plan_refuses_a_branch_that_disagrees_with_the_declared_version():
    """`v2.9.0` while pyproject says 3.0.0 would mislabel which line the build came from."""
    plan = rc.plan("v2.9.0", published=PUBLISHED)

    assert plan["publishable"] is False
    assert any("pyproject.toml declares" in blocker for blocker in plan["blockers"])


def test_plan_treats_a_failed_pypi_lookup_as_blocking(monkeypatch):
    """Guessing rc1 after a failed lookup would collide with a published rc1."""

    def explode(*args, **kwargs):
        raise rc.ReleaseCandidateError("Could not reach PyPI at https://pypi.org/...: boom")

    monkeypatch.setattr(rc, "fetch_published_versions", explode)
    plan = rc.plan("v3.0.0")

    assert plan["publishable"] is False
    assert "Could not reach PyPI" in plan["pypi_lookup_error"]
    assert any("is unknown" in blocker for blocker in plan["blockers"])


def test_plan_commands_pin_the_candidate_exactly():
    commands = rc.plan("v3.0.0", published=PUBLISHED)["commands"]

    assert commands["install"] == "pip install nyxgpt==3.0.0rc3"
    assert commands["user_data"] == "nyxgpt cloud user-data --os linux --version 3.0.0rc3"
    assert commands["deploy"] == "nyxgpt cloud deploy --version 3.0.0rc3"
    # Never a bare `pip install nyxgpt`: that resolves to the last stable
    # release, which is precisely what these builds exist to get past.
    assert "nyxgpt==" in commands["install"]


def test_plan_states_its_guardrails():
    guardrails = " ".join(rc.plan("v3.0.0", published=PUBLISHED)["guardrails"]).lower()

    assert "dispatch triggers only" in guardrails
    assert "release branches only" in guardrails
    assert "pre-release" in guardrails
    assert "acceptance only" in guardrails
    assert "trusted publishing" in guardrails


def test_plan_states_the_stable_channel_is_ceremony_only():
    guardrails = " ".join(rc.plan("v3.0.0", "stable", published=PUBLISHED)["guardrails"]).lower()

    assert "ceremony only" in guardrails
    assert "release_ceremony.sh" in guardrails


def test_stable_plan_renders_a_command_the_cli_actually_accepts():
    """The CLI's --channel offers dev|rc only; rendering `--channel stable`
    would hand an operator a line that fails with an argparse error."""
    commands = rc.plan("v3.0.0", "stable", published=PUBLISHED)["commands"]

    assert commands["publish"] == "scripts/release_ceremony.sh"
    assert commands["plan"] == "scripts/release_ceremony.sh --dry-run"
    assert "--channel stable" not in " ".join(commands.values())


def test_rc_plan_renders_the_macos_acceptance_install():
    """An rc also stamps the -rc formulas, so the plan has to say how to install one."""
    commands = rc.plan("v3.0.0", "rc", published=PUBLISHED)["commands"]

    assert commands["brew"] == (
        "brew tap dkblinux98/nyxgpt && brew install nyxgpt-api-rc nyxgpt-web-rc"
    )
    assert "nyxgpt-api " not in commands["brew"], "an rc must never install the stable formula"


def test_dev_plan_offers_no_brew_command_because_the_nightly_is_pypi_only():
    commands = rc.plan("v3.0.0", "dev", published=PUBLISHED, run_number=31)["commands"]

    assert "brew" not in commands


def test_rc_guardrails_state_the_stable_formulas_are_untouched():
    guardrails = " ".join(rc.plan("v3.0.0", "rc", published=PUBLISHED)["guardrails"]).lower()

    assert "nyxgpt-api-rc" in guardrails
    assert "brew install nyxgpt-api` still resolves to the latest stable" in guardrails
    assert "prerelease" in guardrails


def test_rc_guardrails_explain_the_automatic_candidate(monkeypatch):
    """The dashboard renders these verbatim, so the autopilot's behaviour has a
    surface there too -- an owner reading the panel learns candidates cut
    themselves at agentic-work-complete, and why a repeat cuts none."""
    guardrails = " ".join(rc.plan("v3.0.0", "rc", published=PUBLISHED)["guardrails"]).lower()

    assert "agentic-work-complete" in guardrails
    assert "no duplicate candidates" in guardrails


def test_dev_guardrails_do_not_claim_the_autopilot_cuts_nightlies():
    """The autopilot publishes candidates only -- the nightly is the schedule's."""
    guardrails = " ".join(
        rc.plan("v3.0.0", "dev", published=PUBLISHED, run_number=31)["guardrails"]
    ).lower()

    assert "agentic-work-complete" not in guardrails


def test_dev_guardrails_state_the_nightly_never_touches_the_tap():
    guardrails = " ".join(
        rc.plan("v3.0.0", "dev", published=PUBLISHED, run_number=31)["guardrails"]
    ).lower()

    assert "pypi only" in guardrails
    assert "never touches the homebrew tap" in guardrails


# --- PyPI lookup ----------------------------------------------------------


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_fetch_published_versions_reads_every_release(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: _Response(200, {"releases": {"3.0.0rc1": [], "2.1.0": []}}),
    )

    assert rc.fetch_published_versions() == ["2.1.0", "3.0.0rc1"]


def test_fetch_published_versions_treats_an_unpublished_project_as_empty(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(404))

    assert rc.fetch_published_versions("nyxgpt-does-not-exist") == []


def test_fetch_published_versions_raises_on_a_bad_status(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(503))

    with pytest.raises(rc.ReleaseCandidateError, match="HTTP 503"):
        rc.fetch_published_versions()


def test_fetch_published_versions_raises_on_a_transport_error(monkeypatch):
    import httpx

    def explode(*a, **k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "get", explode)

    with pytest.raises(rc.ReleaseCandidateError, match="Could not reach PyPI"):
        rc.fetch_published_versions()


# --- The nightly's no-op condition ----------------------------------------


def _runs(*entries) -> dict:
    return {"workflow_runs": list(entries)}


def _run(run_id, sha, created_at, event="schedule", conclusion="success") -> dict:
    return {
        "id": run_id,
        "head_sha": sha,
        "created_at": created_at,
        "event": event,
        "conclusion": conclusion,
    }


def test_select_last_scheduled_sha_takes_the_newest_successful_nightly():
    payload = _runs(
        _run(1, "aaa", "2026-08-09T07:27:00Z"),
        _run(3, "ccc", "2026-08-11T07:27:00Z"),
        _run(2, "bbb", "2026-08-10T07:27:00Z"),
    )

    assert rc.select_last_scheduled_sha(payload) == "ccc"


def test_select_last_scheduled_sha_ignores_dispatched_and_failed_runs():
    """An rc dispatch or a failed nightly says nothing about what was published."""
    payload = _runs(
        _run(9, "dispatched", "2026-08-11T09:00:00Z", event="workflow_dispatch"),
        _run(8, "failed", "2026-08-11T08:00:00Z", conclusion="failure"),
        _run(7, "published", "2026-08-10T07:27:00Z"),
    )

    assert rc.select_last_scheduled_sha(payload) == "published"


def test_select_last_scheduled_sha_ignores_the_callers_own_run():
    payload = _runs(
        _run(5, "mine", "2026-08-11T07:27:00Z"), _run(4, "prev", "2026-08-10T07:27:00Z")
    )

    assert rc.select_last_scheduled_sha(payload, exclude_run_id="5") == "prev"


def test_select_last_scheduled_sha_is_empty_before_the_first_nightly():
    assert rc.select_last_scheduled_sha(_runs()) == ""


def test_fetch_last_scheduled_sha_asks_github_for_successful_scheduled_runs(monkeypatch):
    import httpx

    calls = []

    def record(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(200, _runs(_run(1, "abc123", "2026-08-10T07:27:00Z")))

    monkeypatch.setattr(httpx, "get", record)

    assert rc.fetch_last_scheduled_sha("dkblinux98/nyxGPT", token="ghs_x") == "abc123"

    url, kwargs = calls[0]
    assert f"/actions/workflows/{rc.PUBLISH_WORKFLOW_FILE}/runs" in url
    assert "event=schedule" in url and "status=success" in url
    assert kwargs["headers"]["Authorization"] == "Bearer ghs_x"


def test_fetch_last_scheduled_sha_treats_a_workflow_with_no_history_as_empty(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(404))

    assert rc.fetch_last_scheduled_sha("dkblinux98/nyxGPT") == ""


def test_fetch_last_scheduled_sha_raises_rather_than_guessing(monkeypatch):
    """A GitHub outage must not read as "nothing published yet" -- that republishes nightly."""
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(500))

    with pytest.raises(rc.ReleaseCandidateError, match="HTTP 500"):
        rc.fetch_last_scheduled_sha("dkblinux98/nyxGPT")


# --- The rc channel's no-op condition (#3729) -----------------------------
#
# The sprint autopilot dispatches a candidate every time the sprint parks at
# agentic-work-complete, so "has the tip moved since the last candidate?" has
# to be answerable for rc exactly as it is for the nightly -- otherwise a
# second observation of the same parked state cuts a duplicate rcN.


def _dispatch_run(run_id, sha, created_at, title="publish rc from v3.0.0", conclusion="success"):
    return {
        "id": run_id,
        "head_sha": sha,
        "created_at": created_at,
        "event": "workflow_dispatch",
        "conclusion": conclusion,
        "display_title": title,
    }


def test_select_last_published_sha_rc_takes_the_newest_successful_candidate():
    payload = _runs(
        _dispatch_run(1, "aaa", "2026-08-09T12:00:00Z"),
        _dispatch_run(3, "ccc", "2026-08-11T12:00:00Z"),
        _dispatch_run(2, "bbb", "2026-08-10T12:00:00Z"),
    )

    assert rc.select_last_published_sha(payload, "rc") == "ccc"


def test_select_last_published_sha_rc_ignores_other_channels_dispatches():
    """dev and stable arrive on the same trigger -- only the title tells them apart."""
    payload = _runs(
        _dispatch_run(9, "stable", "2026-08-11T15:00:00Z", title="publish stable from v3.0.0"),
        _dispatch_run(8, "dev", "2026-08-11T14:00:00Z", title="publish dev from v3.0.0"),
        _dispatch_run(7, "candidate", "2026-08-10T12:00:00Z"),
    )

    assert rc.select_last_published_sha(payload, "rc") == "candidate"


def test_select_last_published_sha_rc_ignores_a_dry_run():
    """A dry run uploads nothing, so counting it would suppress the next real candidate."""
    payload = _runs(
        _dispatch_run(9, "dry", "2026-08-11T15:00:00Z", title="publish rc [dry run] from v3.0.0"),
        _dispatch_run(7, "candidate", "2026-08-10T12:00:00Z"),
    )

    assert rc.select_last_published_sha(payload, "rc") == "candidate"


def test_select_last_published_sha_rc_ignores_failed_and_scheduled_runs():
    payload = _runs(
        _dispatch_run(9, "failed", "2026-08-11T15:00:00Z", conclusion="failure"),
        _run(8, "nightly", "2026-08-11T07:27:00Z"),
        _dispatch_run(7, "candidate", "2026-08-10T12:00:00Z"),
    )

    assert rc.select_last_published_sha(payload, "rc") == "candidate"


def test_select_last_published_sha_rc_ignores_the_callers_own_run():
    payload = _runs(
        _dispatch_run(5, "mine", "2026-08-11T12:00:00Z"),
        _dispatch_run(4, "prev", "2026-08-10T12:00:00Z"),
    )

    assert rc.select_last_published_sha(payload, "rc", exclude_run_id="5") == "prev"


def test_select_last_published_sha_is_empty_before_the_first_candidate():
    assert rc.select_last_published_sha(_runs(), "rc") == ""


def test_select_last_published_sha_dev_is_unmoved_by_rc_dispatches():
    """The two channels' histories are independent: an rc must not skip a nightly."""
    payload = _runs(
        _dispatch_run(9, "candidate", "2026-08-11T15:00:00Z"),
        _run(7, "nightly", "2026-08-10T07:27:00Z"),
    )

    assert rc.select_last_published_sha(payload, "dev") == "nightly"


@pytest.mark.parametrize(
    "title,expected",
    [
        ("publish rc from v3.0.0", True),
        ("PUBLISH RC FROM v3.0.0", True),
        ("publish rc [dry run] from v3.0.0", False),
        ("publish dev from v3.0.0", False),
        ("publish stable from v3.0.0", False),
        ("", False),
    ],
)
def test_run_published_channel_reads_the_workflow_run_name(title, expected):
    run = _dispatch_run(1, "sha", "2026-08-11T12:00:00Z", title=title)

    assert rc.run_published_channel(run, "rc") is expected


def test_run_published_channel_dev_needs_no_title_because_only_the_nightly_is_scheduled():
    assert rc.run_published_channel(_run(1, "sha", "2026-08-11T07:27:00Z"), "dev") is True


def test_fetch_last_published_sha_asks_github_for_rc_dispatches(monkeypatch):
    import httpx

    calls = []

    def record(url, **kwargs):
        calls.append(url)
        return _Response(200, _runs(_dispatch_run(1, "abc123", "2026-08-10T12:00:00Z")))

    monkeypatch.setattr(httpx, "get", record)

    assert rc.fetch_last_published_sha("dkblinux98/nyxGPT", "rc") == "abc123"
    assert "event=workflow_dispatch" in calls[0] and "status=success" in calls[0]


# --- The autopilot's preflight (#3729) ------------------------------------


def test_autopilot_preflight_dispatches_the_next_candidate_when_the_tip_moved():
    decision = rc.autopilot_rc_preflight(
        "v3.0.0",
        ["3.0.0rc1", "2.9.0"],
        _runs(_dispatch_run(1, "oldsha", "2026-08-10T12:00:00Z")),
        head_sha="newsha",
    )

    assert decision["dispatch"] is True
    assert decision["version"] == "3.0.0rc2"
    assert decision["last_sha"] == "oldsha"


def test_autopilot_preflight_is_a_noop_on_an_unchanged_tip():
    """Repeat observations of the same parked state must not cut a duplicate candidate."""
    decision = rc.autopilot_rc_preflight(
        "v3.0.0",
        ["3.0.0rc1"],
        _runs(_dispatch_run(1, "samesha", "2026-08-10T12:00:00Z")),
        head_sha="samesha",
    )

    assert decision["dispatch"] is False
    # It still names what the acceptance round should install.
    assert decision["version"] == "3.0.0rc1"
    assert "unchanged" in decision["reason"]


def test_autopilot_preflight_publishes_the_first_candidate_of_a_release_line():
    decision = rc.autopilot_rc_preflight("v3.0.0", ["2.9.0"], _runs(), head_sha="newsha")

    assert decision["dispatch"] is True
    assert decision["version"] == "3.0.0rc1"
    assert decision["last_sha"] == ""


def test_autopilot_preflight_dispatches_when_the_tip_is_unknown():
    """An unreadable tip is "unknown", never "no" -- the pipeline's guard still decides."""
    decision = rc.autopilot_rc_preflight(
        "v3.0.0",
        ["3.0.0rc1"],
        _runs(_dispatch_run(1, "samesha", "2026-08-10T12:00:00Z")),
        head_sha="",
    )

    assert decision["dispatch"] is True
    assert decision["version"] == "3.0.0rc2"


def test_autopilot_preflight_refuses_a_non_release_branch():
    with pytest.raises(rc.ReleaseCandidateError, match="release branches"):
        rc.autopilot_rc_preflight("feat/3729-something", ["3.0.0rc1"], _runs(), head_sha="sha")


def test_autopilot_preflight_agrees_with_the_pipelines_own_guard():
    """Preflight and pipeline must never disagree about the same tip: one implementation."""
    runs = _runs(_dispatch_run(1, "samesha", "2026-08-10T12:00:00Z"))
    decision = rc.autopilot_rc_preflight("v3.0.0", ["3.0.0rc1"], runs, head_sha="samesha")

    assert decision["dispatch"] is False
    assert rc.select_last_published_sha(runs, "rc") == "samesha"


def test_main_autopilot_preflight_prints_the_decision_as_json(monkeypatch, capsys):
    """The shell library reads this on stdout; it makes no network call of its own."""
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "published": ["3.0.0rc1"],
                    "runs": _runs(_dispatch_run(1, "oldsha", "2026-08-10T12:00:00Z")),
                }
            )
        ),
    )

    def explode(*a, **k):  # pragma: no cover - asserted by not being called
        raise AssertionError("the preflight must not reach the network")

    monkeypatch.setattr(rc, "fetch_published_versions", explode)

    exit_code = rc.main(["--branch", "v3.0.0", "--autopilot-preflight", "--head-sha", "newsha"])

    assert exit_code == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision == {
        "dispatch": True,
        "version": "3.0.0rc2",
        "last_sha": "oldsha",
        "release": "3.0.0",
        "reason": "v3.0.0 has moved since the last published candidate",
    }


def test_main_autopilot_preflight_reports_a_broken_payload(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))

    assert rc.main(["--branch", "v3.0.0", "--autopilot-preflight"]) == 1
    assert "not JSON" in capsys.readouterr().err


def test_main_rc_skips_when_the_tip_is_unchanged_since_the_last_candidate(
    monkeypatch, capsys, tmp_path
):
    """The idempotency the autopilot relies on: same tip -> no second candidate."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    monkeypatch.setattr(rc, "fetch_last_published_sha", lambda *a, **k: "deadbeef")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT, encoding="utf-8")

    exit_code = rc.main(
        [
            "--branch",
            "v3.0.0",
            "--channel",
            "rc",
            "--skip-if-unchanged",
            "--head-sha",
            "deadbeef",
            "--repo",
            "dkblinux98/nyxGPT",
            "--pin",
            str(pyproject),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == rc.SKIP_SENTINEL
    assert pyproject.read_text(encoding="utf-8") == PYPROJECT


def test_main_rc_publishes_when_the_tip_has_moved(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    monkeypatch.setattr(rc, "fetch_last_published_sha", lambda *a, **k: "oldsha")

    exit_code = rc.main(
        [
            "--branch",
            "v3.0.0",
            "--channel",
            "rc",
            "--skip-if-unchanged",
            "--head-sha",
            "newsha",
            "--repo",
            "dkblinux98/nyxGPT",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == rc.next_rc_version("3.0.0", list(PUBLISHED))


def test_main_rc_asks_github_for_rc_history_not_the_nightlys(monkeypatch, capsys):
    """A candidate must not be skipped because a *nightly* built the same commit."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    seen = {}

    def record(repo, channel="dev", **kwargs):
        seen["channel"] = channel
        return ""

    monkeypatch.setattr(rc, "fetch_last_published_sha", record)

    rc.main(
        [
            "--branch",
            "v3.0.0",
            "--channel",
            "rc",
            "--skip-if-unchanged",
            "--head-sha",
            "newsha",
            "--repo",
            "dkblinux98/nyxGPT",
        ]
    )

    assert seen["channel"] == "rc"


def test_main_refuses_the_tip_guard_on_the_stable_channel(monkeypatch, capsys):
    """A release publishes exactly what the ceremony tagged -- it never no-ops."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    exit_code = rc.main(
        [
            "--branch",
            "v3.0.0",
            "--channel",
            "stable",
            "--confirm",
            rc.STABLE_CONFIRMATION,
            "--tags-at-head",
            "3.0.0",
            "--skip-if-unchanged",
            "--head-sha",
            "newsha",
            "--repo",
            "dkblinux98/nyxGPT",
        ]
    )

    assert exit_code == 1
    assert "ceremony-only" in capsys.readouterr().err


# --- Dispatch -------------------------------------------------------------


def _stub_config(monkeypatch, pat="ghp_x", owner="dkblinux98", repo="nyxGPT"):
    from nyxgpt import config

    monkeypatch.setattr(config, "load_config", lambda *a, **k: object())
    monkeypatch.setattr(config, "get_github_pat", lambda cfg: pat)
    monkeypatch.setattr(config, "get_github_repo_owner", lambda cfg: owner)
    monkeypatch.setattr(config, "get_github_repo_name", lambda cfg: repo)


def test_dispatch_posts_the_workflow_dispatch(monkeypatch):
    import httpx

    _stub_config(monkeypatch)
    calls = []

    def record(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(204)

    monkeypatch.setattr(httpx, "post", record)
    result = rc.dispatch("v3.0.0", "rc", number=4)

    url, kwargs = calls[0]
    assert url.endswith(f"/actions/workflows/{rc.PUBLISH_WORKFLOW_FILE}/dispatches")
    assert kwargs["json"] == {"ref": "v3.0.0", "inputs": {"channel": "rc", "number": "4"}}
    assert kwargs["headers"]["Authorization"] == "Bearer ghp_x"
    assert result["dispatched"] is True
    assert result["channel"] == "rc"


def test_dispatch_can_ask_for_an_immediate_dev_build(monkeypatch):
    import httpx

    _stub_config(monkeypatch)
    calls = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: (calls.append(kw), _Response(204))[1])

    rc.dispatch("v3.0.0", "dev")

    assert calls[0]["json"]["inputs"] == {"channel": "dev"}


def test_dispatch_refuses_the_stable_channel(monkeypatch):
    """A release is published by the ceremony, which supplies tag + confirmation."""
    import httpx

    def explode(*a, **k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("dispatch must not ask GitHub for a stable publish")

    monkeypatch.setattr(httpx, "post", explode)

    with pytest.raises(rc.ReleaseCandidateError, match="release_ceremony.sh"):
        rc.dispatch("v3.0.0", "stable")


def test_dispatch_refuses_a_non_release_branch(monkeypatch):
    """The guardrail holds even before any credential is read."""
    import httpx

    def explode(*a, **k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("dispatch must not call GitHub for a non-release branch")

    monkeypatch.setattr(httpx, "post", explode)

    with pytest.raises(rc.ReleaseCandidateError, match="release branches"):
        rc.dispatch("feat/whatever")


def test_dispatch_without_credentials_says_which_ones_are_missing(monkeypatch):
    _stub_config(monkeypatch, pat="", owner="", repo="nyxGPT")

    with pytest.raises(rc.ReleaseCandidateError, match=r"\[github\] pat.*repo_owner"):
        rc.dispatch("v3.0.0")


def test_dispatch_reports_a_github_refusal(monkeypatch):
    import httpx

    _stub_config(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response(403, text="forbidden"))

    with pytest.raises(rc.ReleaseCandidateError, match="HTTP 403"):
        rc.dispatch("v3.0.0")


# --- `nyxgpt release publish` / `nyxgpt release rc` -----------------------


def test_cli_reports_the_plan(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.release_publish(_args()) == 0

    out = capsys.readouterr().out
    assert "3.0.0rc3" in out
    assert "pip install nyxgpt==3.0.0rc3" in out


def test_cli_reports_the_dev_channel_plan(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.release_publish(_args(channel="dev")) == 0

    out = capsys.readouterr().out
    assert "3.0.0.dev6" in out
    assert "dev channel" in out


def test_release_rc_is_the_rc_channel(monkeypatch, capsys):
    """The original spelling keeps working, and means exactly `--channel rc`."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    args = argparse.Namespace(
        branch="v3.0.0", publish=False, rc_number=None, json=True, channel=None
    )

    assert rc.release_rc(args) == 0
    assert json.loads(capsys.readouterr().out)["channel"] == "rc"


def test_cli_json_output_is_the_same_payload(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.release_publish(_args(json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["next_rc_version"] == "3.0.0rc3"
    assert payload["publishable"] is True


def test_cli_exits_non_zero_from_a_branch_that_cannot_be_published(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.release_publish(_args(branch="feat/x")) == 1
    assert "NOT publishable" in capsys.readouterr().out


def test_cli_publish_dispatches_the_workflow(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    dispatched = []
    monkeypatch.setattr(
        rc,
        "dispatch",
        lambda branch, channel="rc", number=None: (
            dispatched.append((branch, channel, number)),
            {"dispatched": True, "runs_url": "https://github.com/x/y/actions"},
        )[1],
    )

    assert rc.release_publish(_args(publish=True)) == 0

    assert dispatched == [("v3.0.0", "rc", None)]
    assert "3.0.0rc3" in capsys.readouterr().out


def test_cli_refuses_a_number_already_on_pypi_before_dispatching(monkeypatch, capsys):
    """Fail fast locally instead of burning a run the workflow will reject."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    def explode(*a, **k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("must not dispatch a number PyPI already serves")

    monkeypatch.setattr(rc, "dispatch", explode)

    assert rc.release_publish(_args(number=2, publish=True)) == 1
    assert "already on PyPI" in capsys.readouterr().err


def test_cli_refuses_a_zero_number_instead_of_dispatching_a_doomed_run(monkeypatch, capsys):
    """`--number 0` used to dispatch a run that the module's guardrail then failed."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    def explode(*a, **k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("must not dispatch an invalid number")

    monkeypatch.setattr(rc, "dispatch", explode)

    assert rc.release_publish(_args(number=0, publish=True)) == 1
    assert "1 or greater" in capsys.readouterr().err


def test_cli_reports_the_number_it_actually_dispatches(monkeypatch, capsys):
    """The message must name the requested build, not the auto-resolved one."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    monkeypatch.setattr(
        rc,
        "dispatch",
        lambda branch, channel="rc", number=None: {
            "dispatched": True,
            "runs_url": "https://github.com/x/y/actions",
        },
    )

    assert rc.release_publish(_args(number=9, publish=True)) == 0

    out = capsys.readouterr().out
    assert "3.0.0rc9" in out
    assert "3.0.0rc3" not in out


def test_cli_warns_that_a_dispatched_dev_build_can_no_op(monkeypatch, capsys):
    """The workflow runs the nightly's unchanged-tip check for every dev run,
    dispatched or scheduled -- so say so rather than let SKIPPED look broken."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    monkeypatch.setattr(
        rc,
        "dispatch",
        lambda branch, channel="rc", number=None: {
            "dispatched": True,
            "runs_url": "https://github.com/x/y/actions",
        },
    )

    assert rc.release_publish(_args(channel="dev", publish=True)) == 0

    out = capsys.readouterr().out
    assert "publishes nothing when the tip has not moved" in out


def test_cli_warns_that_an_rc_no_ops_on_an_unchanged_tip(monkeypatch, capsys):
    """Since #3729 the rc channel carries the same guard -- say so, or the
    operator reads "SKIPPED" off the run and thinks something broke."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    monkeypatch.setattr(
        rc,
        "dispatch",
        lambda branch, channel="rc", number=None: {
            "dispatched": True,
            "runs_url": "https://github.com/x/y/actions",
        },
    )

    assert rc.release_publish(_args(publish=True)) == 0
    assert "publishes nothing when the tip has not moved" in capsys.readouterr().out


def test_cli_does_not_warn_about_a_no_op_for_an_explicitly_numbered_rc(monkeypatch, capsys):
    """An explicit number is deliberate intent, and the workflow drops the
    guard for it -- promising a no-op there would be wrong."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    monkeypatch.setattr(
        rc,
        "dispatch",
        lambda branch, channel="rc", number=None: {
            "dispatched": True,
            "runs_url": "https://github.com/x/y/actions",
        },
    )

    assert rc.release_publish(_args(publish=True, number=9)) == 0
    assert "publishes nothing" not in capsys.readouterr().out


def test_cli_publish_refuses_when_the_plan_is_blocked(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    def explode(*a, **k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("must not dispatch from a non-release branch")

    monkeypatch.setattr(rc, "dispatch", explode)

    assert rc.release_publish(_args(branch="master", publish=True)) == 1
    assert "refusing to publish" in capsys.readouterr().err


def test_cli_reports_a_dispatch_failure(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    def explode(*a, **k):
        raise rc.ReleaseCandidateError("GitHub refused the workflow dispatch (HTTP 403)")

    monkeypatch.setattr(rc, "dispatch", explode)

    assert rc.release_publish(_args(publish=True)) == 1
    assert "HTTP 403" in capsys.readouterr().err


# --- `python -m nyxgpt.release_candidate` (what the workflow runs) --------


def test_main_prints_the_resolved_version(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "v3.0.0"]) == 0
    assert capsys.readouterr().out.strip() == "3.0.0rc3"


def test_main_honours_an_explicit_number(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "v3.0.0", "--number", "7"]) == 0
    assert capsys.readouterr().out.strip() == "3.0.0rc7"


def test_main_treats_a_blank_number_as_auto(monkeypatch, capsys):
    """The workflow input defaults to an empty string, not an unset value."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "v3.0.0", "--number", ""]) == 0
    assert capsys.readouterr().out.strip() == "3.0.0rc3"


def test_main_rejects_a_non_numeric_number(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "v3.0.0", "--number", "two"]) == 1
    assert "must be a number" in capsys.readouterr().err


def test_main_refuses_a_number_pypi_already_serves(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "v3.0.0", "--number", "2"]) == 1
    assert "already on PyPI" in capsys.readouterr().err


def test_main_refuses_a_non_release_branch(monkeypatch, capsys):
    """This is the workflow's release-branch guardrail, enforced in one place."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "feat/3727-x"]) == 1
    assert "not a release branch" in capsys.readouterr().err


def test_main_pins_the_pyproject_it_is_given(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT, encoding="utf-8")

    assert rc.main(["--branch", "v3.0.0", "--pin", str(pyproject)]) == 0

    assert 'version = "3.0.0rc3"' in pyproject.read_text(encoding="utf-8")
    assert capsys.readouterr().out.strip() == "3.0.0rc3"


def test_main_does_not_pin_anything_when_the_guardrail_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT, encoding="utf-8")

    assert rc.main(["--branch", "master", "--pin", str(pyproject)]) == 1
    assert pyproject.read_text(encoding="utf-8") == PYPROJECT


def test_main_prints_the_json_plan_on_stderr(monkeypatch, capsys):
    """stdout stays the bare version so the workflow can use `$(...)` directly."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "v3.0.0", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "3.0.0rc3"
    assert json.loads(captured.err)["version"] == "3.0.0rc3"


# --- the nightly dev channel, end to end through main() -------------------


def test_main_dev_uses_the_run_number_for_a_unique_version(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "v3.0.0", "--channel", "dev", "--run-number", "88"]) == 0
    assert capsys.readouterr().out.strip() == "3.0.0.dev88"


def test_main_dev_skips_when_the_tip_is_unchanged(monkeypatch, capsys, tmp_path):
    """The owner's no-op requirement: an unchanged tip publishes nothing."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    monkeypatch.setattr(rc, "fetch_last_published_sha", lambda *a, **k: "deadbeef")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT, encoding="utf-8")

    exit_code = rc.main(
        [
            "--branch",
            "v3.0.0",
            "--channel",
            "dev",
            "--run-number",
            "88",
            "--skip-if-unchanged",
            "--head-sha",
            "deadbeef",
            "--repo",
            "dkblinux98/nyxGPT",
            "--pin",
            str(pyproject),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == rc.SKIP_SENTINEL
    # Nothing was pinned: the run stops before it builds anything.
    assert pyproject.read_text(encoding="utf-8") == PYPROJECT


def test_main_dev_publishes_when_the_tip_has_moved(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    monkeypatch.setattr(rc, "fetch_last_published_sha", lambda *a, **k: "oldsha")

    exit_code = rc.main(
        [
            "--branch",
            "v3.0.0",
            "--channel",
            "dev",
            "--run-number",
            "88",
            "--skip-if-unchanged",
            "--head-sha",
            "newsha",
            "--repo",
            "dkblinux98/nyxGPT",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "3.0.0.dev88"


def test_main_dev_publishes_the_first_nightly(monkeypatch, capsys):
    """No previous successful nightly -> nothing to compare against -> publish."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    monkeypatch.setattr(rc, "fetch_last_published_sha", lambda *a, **k: "")

    assert (
        rc.main(
            [
                "--branch",
                "v3.0.0",
                "--channel",
                "dev",
                "--skip-if-unchanged",
                "--head-sha",
                "newsha",
                "--repo",
                "dkblinux98/nyxGPT",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "3.0.0.dev6"


def test_main_dev_fails_rather_than_publishing_blind_on_a_github_error(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    def explode(*a, **k):
        raise rc.ReleaseCandidateError("Could not reach the GitHub API: boom")

    monkeypatch.setattr(rc, "fetch_last_published_sha", explode)

    assert (
        rc.main(
            [
                "--branch",
                "v3.0.0",
                "--channel",
                "dev",
                "--skip-if-unchanged",
                "--head-sha",
                "newsha",
                "--repo",
                "dkblinux98/nyxGPT",
            ]
        )
        == 1
    )
    assert "Could not reach the GitHub API" in capsys.readouterr().err


# --- the stable channel is ceremony-only ---------------------------------


STABLE_ARGS = ["--branch", "v3.0.0", "--channel", "stable"]


def test_main_stable_refuses_without_the_ceremony_confirmation(monkeypatch, capsys):
    """Dispatching `channel=stable` by hand must not publish a release."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main([*STABLE_ARGS, "--tags-at-head", "3.0.0"]) == 1
    assert "ceremony-only" in capsys.readouterr().err


def test_main_stable_refuses_when_the_release_tag_is_not_at_the_tip(monkeypatch, capsys):
    """Phase 1 of the ceremony creates the tag; without it this is not the release."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main([*STABLE_ARGS, "--confirm", "ceremony", "--tags-at-head", "2.1.0"]) == 1
    assert "is not at the built commit" in capsys.readouterr().err


def test_main_stable_publishes_the_release_for_the_ceremony(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    exit_code = rc.main([*STABLE_ARGS, "--confirm", "ceremony", "--tags-at-head", "3.0.0 v3.0.0"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "3.0.0"


# --- A build has to survive the repo-less provisioning path ---------------


@pytest.mark.parametrize("version", ["3.0.0rc3", "3.0.0.dev12"])
def test_user_data_pins_a_prerelease_exactly(version):
    """`nyxgpt cloud user-data --version <dev-or-rc>` -- the EC2 bootstrap half."""
    from nyxgpt import cloud_provision

    rendered = cloud_provision.render_user_data("linux", version)

    assert f'NYXGPT_VERSION="{version}"' in rendered
    assert cloud_provision.VERSION_PLACEHOLDER not in rendered
    # An exact `==` pin is what makes a pre-release installable at all: pip
    # excludes pre-releases from an unpinned requirement.
    assert 'PIP_SPEC="nyxgpt==${NYXGPT_VERSION}"' in rendered


@pytest.mark.parametrize("version", ["3.0.0rc3", "3.0.0.dev12"])
def test_deploy_accepts_a_prerelease_and_splices_it_into_the_remote_script(
    tmp_path, monkeypatch, version
):
    """`nyxgpt cloud deploy --version <dev-or-rc>` -- the one-command half."""
    from nyxgpt import cloud_deploy

    monkeypatch.setattr(cloud_deploy, "DEPLOY_STATE_FILE", tmp_path / "deploy.json")
    args = argparse.Namespace(version=version, skip_observability=True)
    plan = cloud_deploy.resolve_plan(args)
    script = cloud_deploy.render_provision_script(plan)

    assert plan.version == version
    assert f'NYXGPT_VERSION="{version}"' in script


# --- The publish workflow itself ------------------------------------------


def _publish_workflow() -> dict:
    import yaml

    path = REPO_ROOT / ".github" / "workflows" / rc.PUBLISH_WORKFLOW_FILE
    assert path.is_file(), f"missing publish workflow: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _publish_workflow_text() -> str:
    return (REPO_ROOT / ".github" / "workflows" / rc.PUBLISH_WORKFLOW_FILE).read_text(
        encoding="utf-8"
    )


def test_publish_workflow_runs_on_a_schedule_and_on_dispatch_only():
    """No push/tag/release trigger: a build is never cut by accident."""
    # PyYAML parses the unquoted `on:` key as the boolean True.
    triggers = _publish_workflow()[True]

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"], "the nightly dev build needs a cron entry"


def test_publish_workflow_offers_all_three_channels():
    channels = _publish_workflow()[True]["workflow_dispatch"]["inputs"]["channel"]["options"]

    assert set(channels) == set(rc.CHANNELS)


def test_publish_workflow_delegates_the_guardrail_to_the_tested_module():
    """The branch check and the version arithmetic must not be re-implemented in YAML."""
    steps = _publish_workflow()["jobs"]["publish"]["steps"]
    run_steps = " ".join(step.get("run", "") for step in steps)

    assert "python -m nyxgpt.release_candidate" in run_steps
    assert '--branch "$GITHUB_REF_NAME"' in run_steps
    assert "--skip-if-unchanged" in run_steps


def test_publish_workflow_defaults_a_scheduled_run_to_the_dev_channel():
    """`inputs.channel` is empty on a schedule -- the nightly must still be a dev build."""
    steps = _publish_workflow()["jobs"]["publish"]["steps"]
    version_step = next(step for step in steps if step.get("id") == "version")

    assert version_step["env"]["CHANNEL"] == "${{ inputs.channel || 'dev' }}"


def test_publish_workflow_can_mint_an_oidc_token_for_trusted_publishing():
    job = _publish_workflow()["jobs"]["publish"]

    assert job["permissions"]["id-token"] == "write"
    assert job["permissions"]["contents"] == "read"
    # The nightly no-op check reads this workflow's own run history.
    assert job["permissions"]["actions"] == "read"


def test_publish_workflow_stores_no_pypi_token():
    """Owner decision 2026-08-11: the stored-token path is retired for OIDC."""
    text = _publish_workflow_text()

    assert "PYPI_API_TOKEN" not in text
    assert "PYPI_TOKEN" not in text
    assert "password:" not in text


def test_publish_workflow_publishes_with_the_official_pypa_action():
    steps = _publish_workflow()["jobs"]["publish"]["steps"]
    publishers = [s for s in steps if "pypa/gh-action-pypi-publish" in str(s.get("uses", ""))]

    assert len(publishers) == 1, "exactly one publish step -- one pipeline, not two"
    assert "!inputs.dry_run" in publishers[0]["if"]


# --- The rc channel's Homebrew tap step (#3727, owner decision 17:44Z) ----


def _rc_tap_job() -> dict:
    jobs = _publish_workflow()["jobs"]
    assert "homebrew-tap-rc" in jobs, "the rc channel must also stamp the tap"
    return jobs["homebrew-tap-rc"]


def test_only_the_rc_channel_can_reach_the_tap_job():
    """Structural, not conditional: dev and stable never enter this job."""
    job = _rc_tap_job()
    condition = job["if"]

    assert job["needs"] == "publish"
    assert "needs.publish.outputs.channel == 'rc'" in condition
    # A nightly that no-ops publishes nothing, so it must stamp nothing.
    assert "needs.publish.outputs.publish == 'true'" in condition
    assert "!inputs.dry_run" in condition


def test_the_publish_job_exports_what_the_tap_job_gates_on():
    """The gate above is only real if the channel actually reaches it."""
    outputs = _publish_workflow()["jobs"]["publish"]["outputs"]

    assert outputs["channel"] == "${{ steps.version.outputs.channel }}"
    assert outputs["version"] == "${{ steps.version.outputs.version }}"
    assert outputs["publish"] == "${{ steps.version.outputs.publish }}"


def test_rc_tap_job_reuses_the_existing_formula_machinery():
    """Owner decision: reuse build_homebrew_artifacts.py, not a parallel copy."""
    run_steps = " ".join(step.get("run", "") for step in _rc_tap_job()["steps"])

    assert "scripts/build_homebrew_artifacts.py" in run_steps
    assert "--channel rc" in run_steps


def test_rc_tap_job_never_writes_a_stable_formula():
    """Clobber-safety: `brew install nyxgpt-api` must stay on the latest stable."""
    steps = _rc_tap_job()["steps"]
    run_steps = "\n".join(step.get("run", "") for step in steps)

    for stable_formula in ("nyxgpt-api.rb", "nyxgpt-web.rb"):
        # The only mentions allowed are the assertions that refuse to write it.
        for line in run_steps.splitlines():
            if stable_formula in line and "cp " in line:
                raise AssertionError(f"rc tap job copies the stable formula: {line.strip()}")
    for rc_formula in rc.RC_FORMULAS:
        assert f"{rc_formula}.rb" in run_steps

    # ...and it says so out loud rather than relying on the file names alone.
    assert any("Assert no stable formula" in str(step.get("name", "")) for step in steps)


def test_rc_tap_job_cuts_a_prerelease_that_is_never_latest():
    run_steps = "\n".join(step.get("run", "") for step in _rc_tap_job()["steps"])

    assert "gh release create" in run_steps
    assert "--prerelease" in run_steps
    assert "--latest=false" in run_steps
    # Verifying the mutation is the house rule, not an extra.
    assert "isPrerelease" in run_steps


def test_rc_tap_job_can_write_releases_but_not_mint_a_pypi_token():
    job = _rc_tap_job()

    assert job["permissions"]["contents"] == "write"
    assert "id-token" not in job["permissions"]


def test_the_stable_tap_workflow_is_not_reachable_from_a_prerelease():
    """release-artifacts.yml owns the stable formulas; `prereleased` must not start it."""
    import yaml

    path = REPO_ROOT / ".github" / "workflows" / "release-artifacts.yml"
    triggers = yaml.safe_load(path.read_text(encoding="utf-8"))[True]

    assert triggers["release"]["types"] == ["released"]


# --- ONE pipeline: the ceremony delegates rather than duplicating ---------


def _ceremony() -> str:
    return (REPO_ROOT / "scripts" / "release_ceremony.sh").read_text(encoding="utf-8")


def test_ceremony_delegates_its_pypi_phase_to_the_publish_workflow():
    """Owner decision 2026-08-11: one build/publish core, three entry points."""
    text = _ceremony()

    assert rc.PUBLISH_WORKFLOW_FILE in text
    assert "channel=stable" in text
    assert f"confirm={rc.STABLE_CONFIRMATION}" in text


def test_ceremony_no_longer_uploads_or_stores_a_pypi_token():
    text = _ceremony()

    assert "twine upload" not in text
    assert "TWINE_PASSWORD" not in text
    assert "PYPI_TOKEN" not in text


def test_ceremony_still_verifies_pypi_serves_the_release():
    """Delegation must not lose the house rule: verify every mutation afterwards."""
    text = _ceremony()

    assert "pypi.org/pypi/nyxgpt/${VERSION}/json" in text
    assert "does not serve nyxgpt ${VERSION}" in text


def test_ceremony_keeps_its_ceremony_exclusive_steps():
    """Master merge, tag, GitHub Release and the repoint stay owner-run."""
    text = _ceremony()

    assert "refs/heads/master" in text
    assert "releases/${DRAFT_ID}" in text
    assert "RELEASE_BRANCH" in text


# --- The autopilot's rc dispatch, as a shape (#3729) ----------------------
#
# The behaviour lives in scripts/agents/lib/gh_project.sh (exercised by
# tests/test_sprint_autopilot_lib.sh). What is asserted here is the shape the
# owner decision fixes: the kick path publishes candidates and *only*
# candidates, and the pipeline it dispatches carries the tip guard that makes
# a repeat firing a no-op.


def _autopilot_lib() -> str:
    return (REPO_ROOT / "scripts" / "agents" / "lib" / "gh_project.sh").read_text(encoding="utf-8")


def _publish_rc_function() -> str:
    """The body of _autopilot_publish_rc -- the whole kick-to-publish path."""
    text = _autopilot_lib()
    start = text.index("_autopilot_publish_rc() {")
    end = text.index("\n}\n", start)
    return text[start:end]


def test_the_kick_path_can_only_dispatch_the_rc_channel():
    """A release is cut by the ceremony (tag + confirmation), never by the loop."""
    body = _publish_rc_function()

    assert 'channel="$AUTOPILOT_RC_CHANNEL"' in body
    assert 'AUTOPILOT_RC_CHANNEL="rc"' in _autopilot_lib()
    # Every other channel is refused before the dispatch, not silently coerced.
    assert '[[ "$AUTOPILOT_RC_CHANNEL" != "rc" ]]' in body
    for forbidden in ("stable", "dev", rc.STABLE_CONFIRMATION):
        assert f"channel={forbidden}" not in body


def test_the_kick_path_dispatches_the_one_publish_pipeline():
    """No parallel release workflow: it dispatches #3727's pipeline by name."""
    text = _autopilot_lib()

    assert f'AUTOPILOT_PUBLISH_WORKFLOW="{rc.PUBLISH_WORKFLOW_FILE}"' in text
    assert 'gh workflow run "$AUTOPILOT_PUBLISH_WORKFLOW"' in _publish_rc_function()


def test_the_kick_path_publishes_only_at_agentic_work_complete():
    """It reuses #3709's park state rather than deciding completion itself."""
    text = _autopilot_lib()

    assert 'AUTOPILOT_RC_PARK_STATE="awaiting_acceptance"' in text
    assert '[[ "$state" != "$AUTOPILOT_RC_PARK_STATE" ]]' in _publish_rc_function()


def test_the_kick_path_asks_the_tested_module_what_it_would_publish():
    """The preflight is the same implementation the pipeline's guard uses."""
    text = _autopilot_lib()

    assert "python3 -m nyxgpt.release_candidate" in text
    assert "--autopilot-preflight" in text


def test_the_publish_workflow_guards_the_rc_channel_against_a_repeat_firing():
    """The idempotency the autopilot relies on lives in the pipeline, per the
    owner decision -- so a duplicate cannot be cut even by a hand dispatch."""
    steps = _publish_workflow()["jobs"]["publish"]["steps"]
    version_step = next(step for step in steps if step.get("id") == "version")
    run = version_step["run"]

    assert '[ "${CHANNEL}" = "rc" ]' in run
    assert "--skip-if-unchanged" in run
    # An explicit number and a dry run are deliberate acts, so they opt out.
    assert '[ -z "${NUMBER}" ]' in run
    assert '[ "${DRY_RUN}" != "true" ]' in run
    assert version_step["env"]["DRY_RUN"] == "${{ inputs.dry_run }}"


def test_the_publish_workflow_run_name_stays_readable_by_the_rc_tip_guard():
    """`run-name` is how a finished rc run is told apart from a dev or stable
    dispatch (they share the workflow_dispatch event) -- keep them in sync."""
    workflow = _publish_workflow()
    run_name = " ".join(workflow["run-name"].split())

    assert run_name.startswith("publish ${{ inputs.channel || 'dev' }}")
    # What GitHub renders for a real rc dispatch, and for a dry run.
    rendered = run_name.replace("${{ inputs.channel || 'dev' }}", "rc").replace(
        "${{ inputs.dry_run && ' [dry run]' || '' }}", ""
    )
    rendered = rendered.replace("${{ github.ref_name }}", "v3.0.0")
    assert rc._RC_RUN_TITLE_RE.match(rendered), rendered
    assert not rc._RC_RUN_TITLE_RE.match(rendered.replace("rc from", "rc [dry run] from"))
