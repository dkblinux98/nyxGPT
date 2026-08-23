"""`brew` exiting non-zero is not the same claim as "the install failed" (#3861).

Owner acceptance, 2026-08-21: `nyxgpt up` reported `native api service` and
`native web service` failed, and left the machine with no listener on :8000 or
:3000 -- while both kegs were complete, both `opt` links were intact and `brew
linkage` was clean. The nonzero exit was real; what it reported was not the
install.

Homebrew's post-build phase uses `ofail`, not `odie`
(`Library/Homebrew/formula_installer.rb`: `Failed to create <opt_prefix>`,
three `brew link` wordings, `Failed to install service files`, `Failed to fix
install linkage`). `ofail` sets `Homebrew.failed` and *returns*; the installer
finishes the keg, prints the caveats and the `==> Summary` line, and only
`brew.rb`'s closing `exit Homebrew.failed? ? 1 : 0` turns the flag into a
status of 1. So a complete keg and an exit code of 1 are not in tension --
that is the documented shape of the thing.

These tests pin the two halves of the fix that follows from that: a nonzero
exit is resolved against the **keg**, and the tolerance is not blanket -- it
requires both a recognised soft-failure wording and a keg that verifies.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from nyxgpt import ops


def _fake_brew_tree(tmp_path: Path, formula: str, version: str, *, entrypoint: str) -> Path:
    """Build a Cellar/opt/bin layout `_verify_brew_keg` can be pointed at.

    A real keg is exactly this much, as far as the verification is concerned:
    a Cellar directory at the version, a parseable `INSTALL_RECEIPT.json`, an
    `opt` symlink resolving to it, and an executable entry point.
    """
    keg = tmp_path / "Cellar" / formula / version
    (keg / "bin").mkdir(parents=True)
    exe = keg / "bin" / entrypoint
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    (keg / "INSTALL_RECEIPT.json").write_text(
        json.dumps({"time": int(time.time())}), encoding="utf-8"
    )
    opt = tmp_path / "opt" / formula
    opt.parent.mkdir(parents=True, exist_ok=True)
    opt.symlink_to(keg)
    (tmp_path / "bin").mkdir(exist_ok=True)
    return keg


def _brew_env(monkeypatch, tmp_path: Path, *, responses=None, linkage_rc: int = 0, calls=None):
    """Monkeypatch `ops._run` so `brew --cellar`/`--prefix`/`linkage` answer from `tmp_path`."""
    responses = responses or {}

    def fake_run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        if cmd[:2] == ["brew", "--cellar"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{tmp_path / 'Cellar'}\n")
        if cmd[:2] == ["brew", "--prefix"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{tmp_path}\n")
        if cmd[:2] == ["brew", "linkage"]:
            return subprocess.CompletedProcess(cmd, linkage_rc, stdout="", stderr="")
        for prefix, result in responses.items():
            if cmd[: len(prefix)] == list(prefix):
                return result
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")


# The tail of the owner's 2026-08-21 `brew install --overwrite
# dkblinux98/nyxgpt-local/nyxgpt-api` output, as ops captured it. The `Error:`
# line is the whole of what brew said about the failure on stderr.
_LINKAGE_OFAIL_OUTPUT = """\
==> Fetching downloads for: nyxgpt-api
==> Summary
🍺  /opt/homebrew/Cellar/nyxgpt-api/3.0.0: 5,918 files, 147MB, built in 15 seconds
Error: Failed to fix install linkage
"""

# The same class, a different member: the owner's 2026-08-17 run, where the
# candidate keg already owned /opt/homebrew/bin/nyxgpt-api.
_LINK_OFAIL_OUTPUT = """\
==> Summary
🍺  /opt/homebrew/Cellar/nyxgpt-api/2.1.0: 5,918 files, 147MB, built in 15 seconds
Error: The `brew link` step did not complete successfully
"""


# --- _brew_soft_failure_reason ---


@pytest.mark.unit
@pytest.mark.parametrize(
    "output,expected",
    [
        (_LINKAGE_OFAIL_OUTPUT, "Failed to fix install linkage"),
        (_LINK_OFAIL_OUTPUT, "The `brew link` step did not complete successfully"),
        ("Error: Failed to install service files\n", "Failed to install service files"),
        (
            "Error: Failed to create /opt/homebrew/opt/nyxgpt-api\n",
            "Failed to create /opt/homebrew/opt/nyxgpt-api",
        ),
    ],
)
def test_recognises_each_homebrew_post_install_ofail(output, expected):
    assert ops._brew_soft_failure_reason(output) == expected


@pytest.mark.unit
def test_a_real_build_failure_is_not_a_soft_failure():
    """The wordings above are the *whole* list, and nothing else joins it.

    A pip refusal, a missing interpreter, a sha256 mismatch -- every failure
    that happens *before* Homebrew has a keg -- must stay fatal, because there
    is nothing installed for the keg check to verify and reporting it as
    tolerable would be the false success this function exists to avoid.
    """
    assert ops._brew_soft_failure_reason("Error: nyxgpt-api: SHA256 mismatch\n") is None
    assert ops._brew_soft_failure_reason("Error: Failure while executing; `pip` exited 1\n") is None


@pytest.mark.unit
def test_the_marker_must_be_on_an_error_line_brew_wrote():
    """Prose that merely mentions the wording is not brew reporting it.

    The formula's own caveats are echoed into the same captured output, and a
    keg whose caveats discussed linkage would otherwise make every failure of
    that install look tolerable.
    """
    caveats = (
        "==> Caveats\n"
        "If you see Failed to fix install linkage, see docs/homebrew.md\n"
        "Error: Failure while executing; `npm ci` exited 1\n"
    )
    assert ops._brew_soft_failure_reason(caveats) is None


# --- _verify_brew_keg ---


@pytest.mark.unit
def test_verify_accepts_a_complete_keg(monkeypatch, tmp_path):
    _fake_brew_tree(tmp_path, "nyxgpt-api", "3.0.0", entrypoint="nyxgpt-api")
    _brew_env(monkeypatch, tmp_path)
    ok, detail = ops._verify_brew_keg(
        "nyxgpt-api", version="3.0.0", spec="tap/nyxgpt-api", entrypoint="nyxgpt-api"
    )
    assert ok, detail
    assert "is complete" in detail


@pytest.mark.unit
def test_verify_rejects_a_keg_brew_restored_from_backup(monkeypatch, tmp_path):
    """`brew reinstall` puts the old keg back when the build dies.

    The directory survives and so does its version, so those two checks pass
    on a genuinely failed reinstall. The install receipt's timestamp is what
    tells a restored keg from a freshly built one -- without this check the
    tolerance would report a failed rebuild as installed and (worse) record
    the source checksum, so the next run would skip the retry.
    """
    keg = _fake_brew_tree(tmp_path, "nyxgpt-api", "3.0.0", entrypoint="nyxgpt-api")
    (keg / "INSTALL_RECEIPT.json").write_text(
        json.dumps({"time": int(time.time()) - 86400}), encoding="utf-8"
    )
    _brew_env(monkeypatch, tmp_path)
    ok, detail = ops._verify_brew_keg(
        "nyxgpt-api",
        version="3.0.0",
        spec="tap/nyxgpt-api",
        entrypoint="nyxgpt-api",
        installed_after=time.time(),
    )
    assert not ok
    assert "predates this install" in detail


@pytest.mark.unit
def test_verify_rejects_a_keg_with_broken_linkage(monkeypatch, tmp_path):
    """A linkage step that failed *and* left something missing is a real failure.

    This is the check that separates the two: `Failed to fix install linkage`
    on a keg whose libraries all resolve is cosmetic (an `@rpath` id on a
    `dlopen`-ed Python extension is correct), and the same message on a keg
    with missing libraries is not.
    """
    _fake_brew_tree(tmp_path, "nyxgpt-api", "3.0.0", entrypoint="nyxgpt-api")
    _brew_env(monkeypatch, tmp_path, linkage_rc=1)
    ok, detail = ops._verify_brew_keg(
        "nyxgpt-api", version="3.0.0", spec="tap/nyxgpt-api", entrypoint="nyxgpt-api"
    )
    assert not ok
    assert "missing libraries" in detail


@pytest.mark.unit
def test_verify_reports_but_does_not_fail_on_an_unmade_bin_link(monkeypatch, tmp_path):
    """A `brew link` ofail leaves `<prefix>/bin/<name>` unmade -- reported, not fatal.

    The state that produces it here is two kegs of one component competing for
    the same link (the owner's 2026-08-17 run). The command still runs, from
    the other keg, and the *service* execs the `opt` path either way -- so the
    fact belongs in the warning, not in the verdict.
    """
    _fake_brew_tree(tmp_path, "nyxgpt-api", "3.0.0", entrypoint="nyxgpt-api")
    _brew_env(monkeypatch, tmp_path)
    ok, detail = ops._verify_brew_keg(
        "nyxgpt-api", version="3.0.0", spec="tap/nyxgpt-api", entrypoint="nyxgpt-api"
    )
    assert ok
    assert "is NOT linked" in detail


# --- _brew_install_or_reinstall ---


def _install_responses(rc: int, output: str):
    return {
        ("brew", "list", "--versions"): subprocess.CompletedProcess([], 1),
        ("brew", "install"): subprocess.CompletedProcess([], rc, stdout="", stderr=output),
    }


@pytest.mark.unit
def test_install_survives_a_post_install_soft_failure_on_a_verified_keg(monkeypatch, tmp_path):
    """The acceptance failure itself: exit 1, complete keg, install reported done."""
    prefix = tmp_path / "brew"
    _fake_brew_tree(prefix, "nyxgpt-api", "3.0.0", entrypoint="nyxgpt-api")
    _brew_env(monkeypatch, prefix, responses=_install_responses(1, _LINKAGE_OFAIL_OUTPUT))
    marker_dir = tmp_path / "dist"

    outcome = ops._brew_install_or_reinstall(
        "tap/nyxgpt-api",
        "nyxgpt-api",
        sha256="deadbeef",
        marker_dir=marker_dir,
        version="3.0.0",
    )

    assert outcome.decision == "installed"
    assert "Failed to fix install linkage" in outcome.warning
    assert "ofail" in outcome.warning
    # The checksum IS recorded: the keg was verified against this exact source,
    # so rebuilding it on the next run would burn a couple of minutes to
    # reproduce the same cosmetic failure.
    assert (marker_dir / ".nyxgpt-api.sha256").read_text(encoding="utf-8") == "deadbeef"


@pytest.mark.unit
def test_install_still_fails_when_the_soft_failure_left_no_keg(monkeypatch, tmp_path):
    """Half of the tolerance is the keg. Without one, the exit code stands.

    The wording alone proves nothing: `Failed to create <opt_prefix>` is an
    `ofail` too, and a machine where that step failed has no usable component
    at all.
    """
    prefix = tmp_path / "brew"
    (prefix / "Cellar").mkdir(parents=True)
    (prefix / "bin").mkdir(parents=True)
    _brew_env(monkeypatch, prefix, responses=_install_responses(1, _LINKAGE_OFAIL_OUTPUT))
    marker_dir = tmp_path / "dist"

    with pytest.raises(RuntimeError, match="brew install nyxgpt-api failed"):
        ops._brew_install_or_reinstall(
            "tap/nyxgpt-api",
            "nyxgpt-api",
            sha256="deadbeef",
            marker_dir=marker_dir,
            version="3.0.0",
        )
    assert not (marker_dir / ".nyxgpt-api.sha256").exists()


@pytest.mark.unit
def test_install_still_fails_on_an_unrecognised_error_even_with_a_keg(monkeypatch, tmp_path):
    """The other half. A complete keg does not license ignoring any exit code.

    A stale keg from a previous run is present on almost every machine, so
    "the keg looks fine" on its own would tolerate a build that never ran.
    """
    prefix = tmp_path / "brew"
    _fake_brew_tree(prefix, "nyxgpt-api", "3.0.0", entrypoint="nyxgpt-api")
    _brew_env(
        monkeypatch,
        prefix,
        responses=_install_responses(1, "Error: Failure while executing; `pip` exited 1\n"),
    )
    marker_dir = tmp_path / "dist"

    with pytest.raises(RuntimeError, match="brew install nyxgpt-api failed"):
        ops._brew_install_or_reinstall(
            "tap/nyxgpt-api",
            "nyxgpt-api",
            sha256="deadbeef",
            marker_dir=marker_dir,
            version="3.0.0",
        )
    assert not (marker_dir / ".nyxgpt-api.sha256").exists()


# --- _installed_keg_version / _recover_after_failed_native_install ---


@pytest.mark.unit
def test_installed_keg_version_prefers_the_opt_linked_one(monkeypatch, tmp_path):
    _fake_brew_tree(tmp_path, "nyxgpt-api", "2.1.0", entrypoint="nyxgpt-api")
    (tmp_path / "Cellar" / "nyxgpt-api" / "3.0.0").mkdir(parents=True)
    _brew_env(monkeypatch, tmp_path)
    # 3.0.0 sorts highest, but 2.1.0 is what `opt` -- and therefore the
    # service's plist -- actually points at.
    assert ops._installed_keg_version("nyxgpt-api") == "2.1.0"


@pytest.mark.unit
def test_recovery_starts_the_installed_keg_and_names_only_wrapped_commands(monkeypatch, tmp_path):
    """#3861's second finding: a failed install step must not leave the stack down.

    The owner's run stopped all four registrations, failed the two install
    steps, and never reached the `brew services restart` that follows them --
    so the only way back to a serving stack was raw `brew services start`.
    """
    _fake_brew_tree(tmp_path, "nyxgpt-api", "3.0.0", entrypoint="nyxgpt-api")
    _brew_env(monkeypatch, tmp_path)
    restarted: list[str] = []
    monkeypatch.setattr(
        ops,
        "_restart_brew_service",
        lambda name: restarted.append(name) or [ops.OpsResult(True, f"Restarted {name}")],
    )

    results = ops._recover_after_failed_native_install(
        "nyxgpt-api", component="api", entrypoint="nyxgpt-api"
    )

    assert restarted == ["nyxgpt-api"]
    recovery = [r for r in results if r.message.startswith("Recovery:")]
    assert recovery and recovery[0].ok
    text = "\n".join(f"{r.message}\n{r.details}" for r in results)
    assert "nyxgpt ops restart api" in text
    for raw in ("brew services", "brew install", "docker compose", "kubectl"):
        assert raw not in text


@pytest.mark.unit
def test_recovery_starts_nothing_when_there_is_no_usable_keg(monkeypatch, tmp_path):
    """Starting a half-written keg trades "down" for "crash-looping"."""
    (tmp_path / "Cellar").mkdir(parents=True)
    _brew_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ops,
        "_restart_brew_service",
        lambda name: pytest.fail(f"nothing should have been started, got {name}"),
    )

    results = ops._recover_after_failed_native_install(
        "nyxgpt-api", component="api", entrypoint="nyxgpt-api"
    )

    assert results and not results[0].ok
    assert "nyxgpt ops doctor" in results[0].details
    assert "brew services" not in results[0].details


@pytest.mark.unit
def test_failed_api_install_reports_the_failure_and_recovers(monkeypatch, tmp_path):
    """`_install_homebrew_api` turns the raise into a FAIL result plus a recovery pass."""
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(ops, "_homebrew_formula_template", lambda name: tmp_path / "template.rb")
    (tmp_path / "template.rb").write_text('version "__VERSION__"\n', encoding="utf-8")
    monkeypatch.setattr(ops, "_tap_repo", lambda tap: tmp_path / "tap")
    monkeypatch.setattr(ops, "_read_project_version", lambda: "3.0.0")
    monkeypatch.setattr(ops, "_create_dist_tarball", lambda *a, **k: tmp_path / "x.tar.gz")
    monkeypatch.setattr(ops, "_sha256_file", lambda p: "sha")
    monkeypatch.setattr(
        ops,
        "_brew_install_or_reinstall",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("brew install nyxgpt-api failed: boom")),
    )
    recovered: list[str] = []
    monkeypatch.setattr(
        ops,
        "_recover_after_failed_native_install",
        lambda formula, **k: recovered.append(formula)
        or [ops.OpsResult(True, "Recovery: api is running", "", status="NOTE")],
    )

    results = ops._install_homebrew_api()

    assert recovered == ["nyxgpt-api"]
    assert any(not r.ok and r.message == "Failed to install nyxgpt-api" for r in results)
    assert any(r.message.startswith("Recovery:") for r in results)


# --- _bounded_output ---


@pytest.mark.unit
def test_bounded_output_keeps_the_error_lines_from_the_elided_middle():
    """The elision is what cost two diagnosis rounds on this very issue.

    Homebrew prints the *cause* of a `fix_dynamic_linkage` failure in the
    middle of stdout (`puts e`) and its caveats afterwards, so a head+tail
    window keeps the "Error: Failed to fix install linkage" headline and drops
    the line that says why. The 2026-07-29 instance of the same failure -- from
    before this bounding existed -- named it outright, and that line is the
    shape asserted here.
    """
    cause = (
        "Error: Failed changing dylib ID of "
        "/opt/homebrew/Cellar/nyxgpt-api/2.0.0/libexec/venv/lib/python3.12/"
        "site-packages/tiktoken/_tiktoken.cpython-312-darwin.so"
    )
    lines = (
        ["==> Installing nyxgpt-api"] * 5
        + ["installing something"] * 30
        + [cause]
        + ["more build noise"] * 30
        + ["==> Caveats"] * 20
    )
    excerpt = ops._bounded_output("\n".join(lines))

    assert cause in excerpt
    assert "lines omitted" in excerpt
    # Still bounded: head + marker + rescued lines + tail, not the whole log.
    assert len(excerpt.splitlines()) < len(lines)


@pytest.mark.unit
def test_bounded_output_caps_how_many_error_lines_it_rescues():
    """An `npm ci` failure can emit thousands of `error` lines; the bound holds."""
    lines = ["head"] * 5 + [f"error: {i}" for i in range(500)] + ["tail"] * 20
    excerpt = ops._bounded_output("\n".join(lines))
    rescued = [line for line in excerpt.splitlines() if line.startswith("error: ")]
    assert len(rescued) == ops._OUTPUT_EXCERPT_SALIENT_LINES
    # The ones nearest the failure, not the ones nearest the start.
    assert rescued[-1] == "error: 499"


@pytest.mark.unit
def test_bounded_output_keeps_a_failed_brew_install_whole():
    """The measurement that sets the verbatim floor (#3861).

    Homebrew prints the cause of a post-install failure with `puts e` -- a
    bare exception message, no `Error:` prefix -- so no pattern rescues it by
    shape. What separates it from real log spam is size: across the owner's
    `cli.log.2` every failing `brew install`/`reinstall` stdout was 63-67
    lines, while `npm ci` and a pip backtrack elided 375 and 475. Below the
    floor nothing is elided at all, which is why the excerpt that cost two
    diagnosis rounds cannot recur at this size.
    """
    lines = ["==> Installing nyxgpt-api"] * 5
    lines += ["==> some build step"] * 40
    lines += ["Updating load commands would exceed header padding"]
    lines += ["==> Caveats"] * 21
    assert len(lines) == 67  # the owner's actual stdout length

    excerpt = ops._bounded_output("\n".join(lines))

    assert "lines omitted" not in excerpt
    assert "Updating load commands would exceed header padding" in excerpt


@pytest.mark.unit
def test_bounded_output_still_elides_the_outputs_the_bound_was_written_for():
    """An `npm ci` or a pip backtrack is still bounded -- the floor is a floor."""
    lines = ["step"] * (ops._OUTPUT_EXCERPT_VERBATIM_MAX_LINES + 400)
    excerpt = ops._bounded_output("\n".join(lines))
    assert "lines omitted" in excerpt
    assert len(excerpt.splitlines()) < ops._OUTPUT_EXCERPT_VERBATIM_MAX_LINES


@pytest.mark.unit
def test_recovery_does_not_ask_brew_to_resolve_an_ambiguous_name(monkeypatch, tmp_path):
    """The recovery runs on the machine state that produced #3861: two taps.

    `brew linkage` is the only check in `_verify_brew_keg` that makes brew
    *resolve a formula*, and the recovery has no tap-qualified spec to give it
    -- a bare name errors when two taps provide it, which would make the
    recovery refuse to start a keg that is perfectly good, for the wrong
    reason. Checks 1-4 read the Cellar layout directly and cannot be
    ambiguous.
    """
    _fake_brew_tree(tmp_path, "nyxgpt-api", "3.0.0", entrypoint="nyxgpt-api")
    calls: list[list[str]] = []
    _brew_env(monkeypatch, tmp_path, calls=calls)
    monkeypatch.setattr(
        ops, "_restart_brew_service", lambda name: [ops.OpsResult(True, f"Restarted {name}")]
    )

    results = ops._recover_after_failed_native_install(
        "nyxgpt-api", component="api", entrypoint="nyxgpt-api"
    )

    assert any(r.message.startswith("Recovery:") and r.ok for r in results)
    assert not any(cmd[:2] == ["brew", "linkage"] for cmd in calls), (
        "the recovery asked brew to resolve a bare formula name, which errors "
        "on a machine carrying both taps"
    )
