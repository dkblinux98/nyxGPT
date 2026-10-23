"""What `ops install` tells the operator when it could not reach a registry (#4205).

The criterion this file covers: *a transient fetch failure is distinguishable
from a real build failure in what `ops install` reports -- it says the build
could not reach the registry, not that nyxGPT is broken.*

Both directions matter and the second is the one that is easy to get wrong. A
build that is genuinely broken must keep reporting as broken: "registry
unreachable, retry" aimed at a TypeScript error sends the operator round a
loop that cannot terminate, and they would have been better off with the
unhelpful message they had before.
"""

from __future__ import annotations

import subprocess

import pytest

from nyxgpt import build_fetch_policy as policy
from nyxgpt import ops

_NETWORK_BUILD_OUTPUT = (
    "#1 [internal] load build definition from Dockerfile\n"
    + "#5 some very ordinary progress line\n" * 200
    + "#8 23.97 npm error code EAI_AGAIN\n"
    + "#8 23.97 npm error network In most cases you are behind a proxy\n"
    + 'ERROR: failed to build: failed to solve: process "/bin/sh -c npm ci" '
    "did not complete successfully: exit code: 152\n"
)

#: The same failure, with the cause buried: a multi-stage build keeps printing
#: progress for the stages that were already in flight, so the npm lines land
#: in the middle of the output rather than at the end.
#:
#: This fixture was written against `_bounded_output` as it stood at #4205 --
#: `_OUTPUT_EXCERPT_SALIENT_RE` was then anchored at line start, so BuildKit's
#: `#8 23.97 ` prefix put the cause beyond any rescue. #4192's round 4 made
#: that pattern `search` rather than `match` (ledger D-076) for exactly that
#: reason, so the old shape no longer buries anything, and a test whose
#: premise has quietly become false is the defect both issues are about.
#:
#: What buries a line *by construction*, and will keep burying it however the
#: pattern is spelled, is the CAP: the rescue carries at most
#: `_OUTPUT_EXCERPT_SALIENT_LINES` (12) lines and keeps the LAST of them, the
#: ones nearest the failure. So the cause is followed here by more than twelve
#: later error-labelled lines -- which is what a failing multi-stage build
#: really prints, the other stages giving up after the one that broke.
_NETWORK_BUILD_OUTPUT_CAUSE_BURIED = (
    "#1 [internal] load build definition from Dockerfile\n"
    + "#5 some very ordinary progress line\n" * 200
    + "#8 23.97 npm error code EAI_AGAIN\n"
    + "#8 23.97 npm error network In most cases you are behind a proxy\n"
    + "#9 12.01 error: stage cancelled because another stage failed\n" * 30
    + "#9 some other stage still reporting progress\n" * 200
    + "ERROR: failed to build: failed to solve: exit code: 152\n"
)

_REAL_BUILD_OUTPUT = (
    "#1 [internal] load build definition from Dockerfile\n"
    "./src/app/page.tsx:12:5\n"
    "Type error: Property 'nope' does not exist on type 'Props'.\n"
    'ERROR: failed to build: failed to solve: process "/bin/sh -c npm run build" '
    "did not complete successfully: exit code: 1\n"
)


def _fake_build(output: str):
    """A `_run` stand-in whose `docker build` fails with `output` on stdout."""

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 1)
        if cmd[:2] == ["docker", "build"]:
            return subprocess.CompletedProcess(cmd, 1, stdout=output, stderr="")
        return subprocess.CompletedProcess(cmd, 0)

    return fake_run


def _build(monkeypatch, tmp_path, output: str):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(ops, "_run", _fake_build(output))
    with pytest.raises(ops.ImageBuildFailure) as excinfo:
        ops._docker_build_if_needed(
            "nyxgpt-web:local",
            src,
            fingerprint_paths=[src],
            marker_dir=tmp_path / "markers",
        )
    return excinfo.value


@pytest.mark.unit
def test_image_build_failure_carries_the_network_attribution(monkeypatch, tmp_path):
    exc = _build(monkeypatch, tmp_path, _NETWORK_BUILD_OUTPUT)
    assert exc.fetch is not None
    assert exc.fetch.tool == "npm"
    assert exc.fetch.code == "EAI_AGAIN"


@pytest.mark.unit
def test_image_build_failure_classifies_the_full_output_not_the_excerpt(monkeypatch, tmp_path):
    """The trap this reporting had to avoid (#4192, D-076).

    `exc.detail` is a bounded excerpt, and no bound can promise to contain the
    cause: the salient-line rescue carries at most
    `_OUTPUT_EXCERPT_SALIENT_LINES` of them and keeps the last, so a cause
    followed by enough later error lines is outside the record the operator is
    shown. Classifying that excerpt would therefore see nothing at all, on an
    output that says `EAI_AGAIN` twice.

    This asserts the gap is real -- measured on this fixture, not assumed from
    how the pattern happens to be spelled this week -- and that classification
    does not fall into it.
    """
    exc = _build(monkeypatch, tmp_path, _NETWORK_BUILD_OUTPUT_CAUSE_BURIED)
    # The cause is genuinely absent from the bounded excerpt...
    assert "lines omitted" in exc.detail
    assert "EAI_AGAIN" not in exc.detail
    # ...and was still attributed, because the classifier saw all of it. It is
    # then put back in front of the operator by the reported result, below.
    assert exc.fetch is not None and exc.fetch.code == "EAI_AGAIN"
    result = ops._image_build_failure_result("nyxgpt-web:local", exc)
    assert "EAI_AGAIN" in (result.details or "")


@pytest.mark.unit
def test_network_build_failure_is_reported_as_a_registry_failure(monkeypatch, tmp_path):
    exc = _build(monkeypatch, tmp_path, _NETWORK_BUILD_OUTPUT)
    result = ops._image_build_failure_result("nyxgpt-web:local", exc)
    assert not result.ok
    assert "Could not reach the package registry" in result.message
    assert "nyxgpt-web:local" in result.message
    # The evidence and the one remediation, so the operator is not left to
    # guess whether to retry or to file a bug.
    assert "EAI_AGAIN" in (result.details or "")
    assert "not a nyxGPT defect" in (result.details or "")
    assert "nyxgpt ops install" in (result.details or "")


@pytest.mark.unit
def test_real_build_failure_still_reports_as_a_build_failure(monkeypatch, tmp_path):
    exc = _build(monkeypatch, tmp_path, _REAL_BUILD_OUTPUT)
    assert exc.fetch is None
    result = ops._image_build_failure_result("nyxgpt-web:local", exc)
    assert not result.ok
    assert result.message == "docker build nyxgpt-web:local failed"
    assert "package registry" not in result.message
    assert "Type error" in (result.details or "")


@pytest.mark.unit
def test_image_build_failure_result_accepts_a_plain_runtime_error():
    """A caller's `except RuntimeError` routes anything here, not just ours."""
    result = ops._image_build_failure_result("nyxgpt-api:local", RuntimeError("boom"))
    assert result.message == "docker build nyxgpt-api:local failed"
    assert "boom" in (result.details or "")


@pytest.mark.unit
def test_failed_network_build_records_no_fingerprint(monkeypatch, tmp_path):
    """So the retry the remediation recommends actually rebuilds.

    Telling an operator to re-run `ops install` would be wrong advice if the
    failed build had recorded its fingerprint -- the next run would skip the
    rebuild and the image would stay broken.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    marker_dir = tmp_path / "markers"
    monkeypatch.setattr(ops, "_run", _fake_build(_NETWORK_BUILD_OUTPUT))
    with pytest.raises(ops.ImageBuildFailure):
        ops._docker_build_if_needed(
            "nyxgpt-web:local", src, fingerprint_paths=[src], marker_dir=marker_dir
        )
    assert not (marker_dir / ".nyxgpt-web_local.sha256").exists()


# --- The native (non-image) fetch sites -------------------------------------


@pytest.mark.unit
def test_run_hands_the_policy_to_a_fetching_child(monkeypatch):
    """`ops._run` applies the policy itself, so no call site can forget it."""
    seen: dict[str, object] = {}

    def fake_subprocess_run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    ops._run(["/tmp/venv/bin/pip", "install", "nyxgpt"], check=False)
    env = seen["env"]
    assert isinstance(env, dict)
    for name, value in policy.ENV_POLICY.items():
        assert env[name] == value


@pytest.mark.unit
def test_run_leaves_a_non_fetching_child_alone(monkeypatch):
    seen: dict[str, object] = {}

    def fake_subprocess_run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    ops._run(["docker", "image", "inspect", "x"], check=False)
    assert seen["env"] is None


@pytest.mark.unit
def test_package_fetch_failure_reports_a_registry_failure():
    cp = subprocess.CompletedProcess(
        ["npm", "ci"], 1, stdout="", stderr="npm error code ECONNRESET\n"
    )
    result = ops._package_fetch_failure(
        cp, step="npm ci in /x/web", failure_message="Failed to install web deps via npm ci"
    )
    assert result.message == "Could not reach the package registry while running npm ci in /x/web"
    assert "ECONNRESET" in (result.details or "")


@pytest.mark.unit
def test_package_fetch_failure_keeps_the_callers_wording_for_a_real_failure():
    cp = subprocess.CompletedProcess(
        ["npm", "ci"],
        1,
        stdout="",
        stderr=(
            "npm error `npm ci` can only install packages when your package.json "
            "and package-lock.json are in sync\n"
        ),
    )
    result = ops._package_fetch_failure(
        cp, step="npm ci in /x/web", failure_message="Failed to install web deps via npm ci"
    )
    assert result.message == "Failed to install web deps via npm ci"
    assert "in sync" in (result.details or "")


@pytest.mark.unit
def test_package_fetch_failure_carries_a_post_condition_verdict():
    """The exit-0 case: the tool said nothing, so the caller's verdict must."""
    cp = subprocess.CompletedProcess(["npm", "ci"], 0, stdout="added 0 packages\n", stderr="")
    result = ops._package_fetch_failure(
        cp,
        step="npm ci in /x/web",
        failure_message="Web deps installed but undici still missing",
        extra_output=policy.incomplete_fetch_message("npm ci", 0),
    )
    assert "Could not reach the package registry" in result.message
    assert policy.INCOMPLETE_FETCH_MARKER in (result.details or "")


@pytest.mark.unit
def test_install_packages_from_sets_applies_the_retry_flags(monkeypatch):
    """The distro-install chokepoint, where apt/dnf get their inline policy."""
    calls: list[list[str]] = []

    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")

    def fake_privileged_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_privileged_run", fake_privileged_run)
    status, packages = ops._install_packages_from_sets(
        (("apt-get", ["apt-get", "install", "-y"], ["docker.io"]),)
    )
    assert status == "installed" and packages == ["docker.io"]
    assert calls == [
        ["apt-get", "-o", "Acquire::Retries=5", "update"],
        ["apt-get", "-o", "Acquire::Retries=5", "install", "-y", "docker.io"],
    ]


@pytest.mark.unit
def test_install_packages_from_sets_applies_dnf_flags(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}" if tool == "dnf" else None)
    monkeypatch.setattr(
        ops,
        "_privileged_run",
        lambda cmd, **k: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0))[1],
    )
    status, _ = ops._install_packages_from_sets(
        (
            ("apt-get", ["apt-get", "install", "-y"], ["docker.io"]),
            ("dnf", ["dnf", "install", "-y"], ["docker"]),
        )
    )
    assert status == "installed"
    assert calls == [
        ["dnf", "--setopt=retries=5", "--setopt=timeout=30", "install", "-y", "docker"]
    ]
