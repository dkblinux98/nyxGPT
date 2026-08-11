"""Unit tests for release-candidate publishing to PyPI (#3727).

Nothing here reaches PyPI or GitHub: the published-version lookup and the
workflow dispatch are replaced with recorders, so the tests assert on the
part that actually has to be right -- the version arithmetic, the
guardrails, and the fact that an RC can never affect a plain
`pip install nyxgpt`.

The last section is the acceptance criterion this feature exists for: a
pinned pre-release must survive the whole repo-less provisioning path
(`nyxgpt cloud user-data`, `nyxgpt cloud deploy`) unchanged, because an RC
nobody can install on a clean machine would be useless.
"""

import argparse
import json

import pytest

from nyxgpt import release_candidate as rc

pytestmark = pytest.mark.unit


PUBLISHED = ("2.1.0", "3.0.0rc1", "3.0.0rc2", "1.0.0")


def _args(**overrides) -> argparse.Namespace:
    base = {"branch": "v3.0.0", "publish": False, "rc_number": None, "json": False}
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


@pytest.mark.parametrize("version", ["3.0.0rc1", "3.0.0a1", "3.0.0b2", "3.0.0.dev4"])
def test_prereleases_are_recognized(version):
    assert rc.is_prerelease(version) is True


@pytest.mark.parametrize("version", ["3.0.0", "2.1.0", "10.0.1"])
def test_stable_releases_are_not_prereleases(version):
    assert rc.is_prerelease(version) is False


def test_release_line_strips_the_rc_suffix():
    assert rc.release_line("3.0.0rc7") == "3.0.0"
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


def test_rc_version_rejects_a_non_release_base():
    with pytest.raises(rc.ReleaseCandidateError, match="not a release version"):
        rc.rc_version("3.0", 1)


def test_rc_version_rejects_a_zero_or_negative_number():
    with pytest.raises(rc.ReleaseCandidateError, match="1 or greater"):
        rc.rc_version("3.0.0", 0)


def test_every_version_this_module_publishes_is_a_prerelease():
    """The load-bearing guarantee: `pip install nyxgpt` can never resolve to an RC."""
    for number in (1, 2, 17):
        assert rc.is_prerelease(rc.rc_version("3.0.0", number))


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


def test_pin_version_result_is_still_parseable_toml():
    import tomllib

    data = tomllib.loads(rc.pin_version(PYPROJECT, "3.0.0rc3"))

    assert data["project"]["version"] == "3.0.0rc3"
    assert data["tool"]["something"]["version"] == "9.9.9"


def test_pin_version_refuses_a_version_that_is_not_a_release_or_rc():
    with pytest.raises(rc.ReleaseCandidateError, match="unrecognized version"):
        rc.pin_version(PYPROJECT, "not-a-version")


def test_pin_version_reports_a_pyproject_with_no_project_version():
    with pytest.raises(rc.ReleaseCandidateError, match=r"\[project\]"):
        rc.pin_version('[project]\nname = "nyxGPT"\n', "3.0.0rc1")


def test_the_real_pyproject_can_be_pinned():
    """The shipped pyproject.toml is what the publish workflow rewrites."""
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    pinned = rc.pin_version(pyproject.read_text(encoding="utf-8"), "3.0.0rc1")

    import tomllib

    assert tomllib.loads(pinned)["project"]["version"] == "3.0.0rc1"


# --- The plan -------------------------------------------------------------


def test_plan_from_the_release_branch_is_publishable():
    plan = rc.plan("v3.0.0", published=PUBLISHED)

    assert plan["publishable"] is True
    assert plan["blockers"] == []
    assert plan["release"] == "3.0.0"
    assert plan["next_rc_version"] == "3.0.0rc3"
    assert plan["published_rcs"] == ["3.0.0rc1", "3.0.0rc2"]
    assert plan["is_prerelease"] is True
    assert plan["workflow"] == rc.RC_WORKFLOW_FILE


def test_plan_refuses_a_non_release_branch():
    plan = rc.plan("feat/3727-something", published=PUBLISHED)

    assert plan["publishable"] is False
    assert any("not a release branch" in blocker for blocker in plan["blockers"])


def test_plan_refuses_a_branch_that_disagrees_with_the_declared_version():
    """`v2.9.0` while pyproject says 3.0.0 would mislabel which line the RC came from."""
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
    assert any("next RC number is unknown" in blocker for blocker in plan["blockers"])


def test_plan_commands_pin_the_candidate_exactly():
    commands = rc.plan("v3.0.0", published=PUBLISHED)["commands"]

    assert commands["install"] == "pip install nyxgpt==3.0.0rc3"
    assert commands["user_data"] == "nyxgpt cloud user-data --os linux --version 3.0.0rc3"
    assert commands["deploy"] == "nyxgpt cloud deploy --version 3.0.0rc3"
    # Never a bare `pip install nyxgpt`: that resolves to the last stable
    # release, which is precisely what an RC exists to get past.
    assert "nyxgpt==" in commands["install"]


def test_plan_states_its_guardrails():
    guardrails = " ".join(rc.plan("v3.0.0", published=PUBLISHED)["guardrails"]).lower()

    assert "dispatch-only" in guardrails
    assert "release branches only" in guardrails
    assert "pre-release" in guardrails
    assert "acceptance only" in guardrails


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
    result = rc.dispatch("v3.0.0", rc_number=4)

    url, kwargs = calls[0]
    assert url.endswith(f"/actions/workflows/{rc.RC_WORKFLOW_FILE}/dispatches")
    assert kwargs["json"] == {"ref": "v3.0.0", "inputs": {"rc_number": "4"}}
    assert kwargs["headers"]["Authorization"] == "Bearer ghp_x"
    assert result["dispatched"] is True


def test_dispatch_omits_the_rc_number_when_it_should_be_auto(monkeypatch):
    import httpx

    _stub_config(monkeypatch)
    calls = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: (calls.append(kw), _Response(204))[1])

    rc.dispatch("v3.0.0")

    assert calls[0]["json"]["inputs"] == {}


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


# --- `nyxgpt release rc` --------------------------------------------------


def test_cli_reports_the_plan(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.release_rc(_args()) == 0

    out = capsys.readouterr().out
    assert "3.0.0rc3" in out
    assert "pip install nyxgpt==3.0.0rc3" in out


def test_cli_json_output_is_the_same_payload(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.release_rc(_args(json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["next_rc_version"] == "3.0.0rc3"
    assert payload["publishable"] is True


def test_cli_exits_non_zero_from_a_branch_that_cannot_be_published(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.release_rc(_args(branch="feat/x")) == 1
    assert "NOT publishable" in capsys.readouterr().out


def test_cli_publish_dispatches_the_workflow(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))
    dispatched = []
    monkeypatch.setattr(
        rc,
        "dispatch",
        lambda branch, number=None: (
            dispatched.append((branch, number)),
            {"dispatched": True, "runs_url": "https://github.com/x/y/actions"},
        )[1],
    )

    assert rc.release_rc(_args(publish=True)) == 0

    assert dispatched == [("v3.0.0", None)]
    assert "3.0.0rc3" in capsys.readouterr().out


def test_cli_publish_refuses_when_the_plan_is_blocked(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    def explode(*a, **k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("must not dispatch from a non-release branch")

    monkeypatch.setattr(rc, "dispatch", explode)

    assert rc.release_rc(_args(branch="master", publish=True)) == 1
    assert "refusing to publish" in capsys.readouterr().err


# --- `python -m nyxgpt.release_candidate` (what the workflow runs) --------


def test_main_prints_the_resolved_version(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "v3.0.0"]) == 0
    assert capsys.readouterr().out.strip() == "3.0.0rc3"


def test_main_honours_an_explicit_rc_number(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "v3.0.0", "--rc-number", "7"]) == 0
    assert capsys.readouterr().out.strip() == "3.0.0rc7"


def test_main_treats_a_blank_rc_number_as_auto(monkeypatch, capsys):
    """The workflow input defaults to an empty string, not an unset value."""
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "v3.0.0", "--rc-number", ""]) == 0
    assert capsys.readouterr().out.strip() == "3.0.0rc3"


def test_main_refuses_an_rc_number_pypi_already_serves(monkeypatch, capsys):
    monkeypatch.setattr(rc, "fetch_published_versions", lambda *a, **k: list(PUBLISHED))

    assert rc.main(["--branch", "v3.0.0", "--rc-number", "2"]) == 1
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


# --- The RC has to survive the repo-less provisioning path ----------------


def test_user_data_pins_an_rc_exactly():
    """`nyxgpt cloud user-data --version 3.0.0rcN` -- the EC2 bootstrap half."""
    from nyxgpt import cloud_provision

    rendered = cloud_provision.render_user_data("linux", "3.0.0rc3")

    assert 'NYXGPT_VERSION="3.0.0rc3"' in rendered
    assert cloud_provision.VERSION_PLACEHOLDER not in rendered
    # An exact `==` pin is what makes a pre-release installable at all: pip
    # excludes pre-releases from an unpinned requirement.
    assert 'PIP_SPEC="nyxgpt==${NYXGPT_VERSION}"' in rendered


def test_deploy_accepts_an_rc_version_and_splices_it_into_the_remote_script(tmp_path, monkeypatch):
    """`nyxgpt cloud deploy --version 3.0.0rcN` -- the one-command half."""
    from nyxgpt import cloud_deploy

    monkeypatch.setattr(cloud_deploy, "DEPLOY_STATE_FILE", tmp_path / "deploy.json")
    args = argparse.Namespace(version="3.0.0rc3", skip_observability=True)
    plan = cloud_deploy.resolve_plan(args)
    script = cloud_deploy.render_provision_script(plan)

    assert plan.version == "3.0.0rc3"
    assert 'NYXGPT_VERSION="3.0.0rc3"' in script


# --- The publish workflow itself ------------------------------------------


def _publish_workflow() -> dict:
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / rc.RC_WORKFLOW_FILE
    assert path.is_file(), f"missing publish workflow: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_publish_workflow_is_dispatch_only():
    """No push/tag/release trigger: an RC is never cut by accident."""
    # PyYAML parses the unquoted `on:` key as the boolean True.
    triggers = _publish_workflow()[True]

    assert set(triggers) == {"workflow_dispatch"}


def test_publish_workflow_delegates_the_guardrail_to_the_tested_module():
    """The branch check and the version arithmetic must not be re-implemented in YAML."""
    steps = _publish_workflow()["jobs"]["publish-rc"]["steps"]
    run_steps = " ".join(step.get("run", "") for step in steps)

    assert "python -m nyxgpt.release_candidate" in run_steps
    assert '--branch "$GITHUB_REF_NAME"' in run_steps


def test_publish_workflow_can_mint_an_oidc_token_for_trusted_publishing():
    job = _publish_workflow()["jobs"]["publish-rc"]

    assert job["permissions"]["id-token"] == "write"
    assert job["permissions"]["contents"] == "read"
