"""Unit tests for ops' running-build reconcile, status block and doctor finding (#4133).

The acceptance criteria these pin, in the issue's own order:

* **AC2** -- `nyxgpt ops install` after a `brew upgrade` either restarts the
  services onto the new venv or reports plainly that the running process is
  stale and names the command that fixes it; it does not report `[OK]` over
  the mismatch (`_reconcile_running_api_build`).
* **AC3** -- `nyxgpt ops status` does not report `version 3.0.0rcNN` for a
  process running some other version's venv; a mismatch is stated as a
  mismatch (`_print_running_api_build`).

AC1 and AC5 are runtime claims about a real Homebrew upgrade and are proved by
execution, not here: `.github/workflows/macos-brew-smoke.yml`'s
`candidate-upgrade` job. What unit tests CAN establish is that the comparison
is made at all, that it is sited after the service restart, and that every
"cannot tell" stays out of the `[OK]` column -- which is the half of the
defect that let a 56/56 install ship over a dead venv.

Conventions follow test_ops_dev_mode.py: `_run`/`_which`/`httpx` are mocked,
`platform.system()` is pinned per test, and nothing reads or writes the
developer's real `~/.nyxGPT`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nyxgpt import ops
from nyxgpt.running_build import (
    BUILD_MATCH,
    BUILD_MISMATCH,
    BUILD_NOT_APPLICABLE,
    BUILD_UNDETERMINED,
    BuildDrift,
    RuntimeBuild,
)

pytestmark = pytest.mark.unit


def _build(prefix: str, *, exists: bool = True, pid: int = 4133, version: str = "3.0.0rc17"):
    return RuntimeBuild(
        executable=f"{prefix}/bin/python3",
        prefix=prefix,
        python="3.11.9",
        pid=pid,
        version=version,
        prefix_exists=exists,
    )


def _drift(state: str, *, running=None, expected="", detail="", remediation=""):
    return BuildDrift(
        state=state,
        running=running,
        expected_prefix=expected,
        expected_source="",
        detail=detail,
        remediation=remediation,
    )


class _Resp:
    """Stand-in for `httpx.get(...)`'s response."""

    def __init__(self, status_code=200, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class TestExpectedNativeApiVenv:
    """Where the installed api service's interpreter lives, per layout."""

    def test_linux_answers_the_ops_managed_venv(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
        venv = tmp_path / ".nyxGPT" / "opt" / "nyxgpt-api" / "venv"
        venv.mkdir(parents=True)
        with patch.object(ops.platform, "system", return_value="Linux"):
            prefix, source = ops._expected_native_api_venv()
        assert prefix == str(venv)
        assert source

    def test_linux_without_the_venv_answers_empty(self, tmp_path, monkeypatch):
        """An absent venv means no native api is installed here, which is what
        scopes the whole check away from Compose/Kubernetes-only hosts."""
        monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
        with patch.object(ops.platform, "system", return_value="Linux"):
            prefix, source = ops._expected_native_api_venv()
        assert prefix == ""
        assert "does not exist" in source

    def test_macos_dev_mode_answers_the_ops_managed_venv(self, tmp_path, monkeypatch):
        """Dev mode's LaunchAgent wrapper execs the editable venv, not a keg --
        and `~/.nyxGPT/opt/nyxgpt-api/venv` is the exact path #4133's stale
        process was running from."""
        monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
        venv = tmp_path / ".nyxGPT" / "opt" / "nyxgpt-api" / "venv"
        venv.mkdir(parents=True)
        with (
            patch.object(ops.platform, "system", return_value="Darwin"),
            patch.object(ops, "read_install_mode") as read_mode,
        ):
            read_mode.return_value.is_dev = True
            prefix, _ = ops._expected_native_api_venv()
        assert prefix == str(venv)

    def test_macos_artifact_answers_the_kegs_libexec_venv_via_opt(self, tmp_path, monkeypatch):
        """Read through `<prefix>/opt/<formula>` -- the symlink the service's
        plist resolves at every start -- so an upgrade moves the answer."""
        monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
        keg_venv = tmp_path / "opt" / "nyxgpt-api@3.0.0rc" / "libexec" / "venv"
        keg_venv.mkdir(parents=True)
        with (
            patch.object(ops.platform, "system", return_value="Darwin"),
            patch.object(ops, "read_install_mode") as read_mode,
            patch.object(ops, "_native_artifact_service_name", return_value="nyxgpt-api@3.0.0rc"),
            patch.object(ops, "_brew_path", return_value=tmp_path),
        ):
            read_mode.return_value.is_dev = False
            prefix, source = ops._expected_native_api_venv()
        assert prefix == str(keg_venv)
        assert "nyxgpt-api@3.0.0rc" in source

    def test_macos_artifact_falls_back_to_the_cellar(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
        cellar = tmp_path / "Cellar"
        keg_venv = cellar / "nyxgpt-api@3.0.0rc" / "3.0.0rc17" / "libexec" / "venv"
        keg_venv.mkdir(parents=True)

        def _brew_path(flag):
            # No `opt` symlink on this machine, which is the state the
            # fallback exists for.
            return tmp_path if flag == "--prefix" else cellar

        with (
            patch.object(ops.platform, "system", return_value="Darwin"),
            patch.object(ops, "read_install_mode") as read_mode,
            patch.object(ops, "_native_artifact_service_name", return_value="nyxgpt-api@3.0.0rc"),
            patch.object(ops, "_brew_path", side_effect=_brew_path),
            patch.object(ops, "_installed_keg_version", return_value="3.0.0rc17"),
        ):
            read_mode.return_value.is_dev = False
            prefix, source = ops._expected_native_api_venv()
        assert prefix == str(keg_venv)
        assert "3.0.0rc17" in source

    def test_no_keg_answers_empty_with_a_reason_rather_than_guessing(self, tmp_path, monkeypatch):
        """An expectation that cannot be located must produce `undetermined`,
        never a mismatch -- a wrong expectation would send an operator to
        restart a service that is fine."""
        monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
        with (
            patch.object(ops.platform, "system", return_value="Darwin"),
            patch.object(ops, "read_install_mode") as read_mode,
            patch.object(ops, "_native_artifact_service_name", return_value="nyxgpt-api@3.0.0rc"),
            patch.object(ops, "_brew_path", return_value=None),
            patch.object(ops, "_which", return_value="/opt/homebrew/bin/brew"),
            patch.object(ops, "_installed_keg_version", return_value=None),
        ):
            read_mode.return_value.is_dev = False
            prefix, source = ops._expected_native_api_venv()
        assert prefix == ""
        assert "nyxgpt-api@3.0.0rc" in source

    def test_unsupported_os_answers_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
        with (
            patch.object(ops.platform, "system", return_value="Windows"),
            patch.object(ops, "read_install_mode") as read_mode,
        ):
            read_mode.return_value.is_dev = False
            prefix, source = ops._expected_native_api_venv()
        assert prefix == ""
        assert "Windows" in source


class TestProbeRunningApiRuntime:
    """The authoritative read: ask the process what it is executing."""

    def _cfg(self, **auth):
        cfg = ops.ConfigParser()
        cfg.add_section("api")
        cfg.set("api", "port", "8000")
        if auth:
            cfg.add_section("auth")
            for key, value in auth.items():
                cfg.set("auth", key, value)
        return cfg

    def test_parses_the_runtime_block(self):
        payload = {"release_version": "3.0.0rc17", "runtime": _build("/keg/venv").to_dict()}
        with (
            patch.object(ops, "load_config", return_value=self._cfg()),
            patch.object(ops.httpx, "get", return_value=_Resp(payload=payload)) as get,
        ):
            build, why_not = ops._probe_running_api_runtime()
        assert why_not == ""
        assert build is not None and build.prefix == "/keg/venv"
        assert get.call_args.args[0] == "http://127.0.0.1:8000/api/v1/info"

    def test_uses_the_configured_port(self):
        cfg = self._cfg()
        cfg.set("api", "port", "9001")
        with (
            patch.object(ops, "load_config", return_value=cfg),
            patch.object(
                ops.httpx, "get", return_value=_Resp(payload={"runtime": _build("/v").to_dict()})
            ) as get,
        ):
            ops._probe_running_api_runtime()
        assert ":9001/" in get.call_args.args[0]

    def test_sends_the_api_key_when_auth_is_enabled(self):
        """`/api/v1/info` is behind the API-key middleware; an operator who
        hardened their install must not lose this check as a side effect."""
        cfg = self._cfg(enabled="true", header="X-Custom-Key", api_key="s3cret")
        with (
            patch.object(ops, "load_config", return_value=cfg),
            patch.object(
                ops.httpx, "get", return_value=_Resp(payload={"runtime": _build("/v").to_dict()})
            ) as get,
        ):
            ops._probe_running_api_runtime()
        assert get.call_args.kwargs["headers"] == {"X-Custom-Key": "s3cret"}

    def test_sends_no_header_when_auth_is_disabled(self):
        with (
            patch.object(ops, "load_config", return_value=self._cfg(enabled="false")),
            patch.object(
                ops.httpx, "get", return_value=_Resp(payload={"runtime": _build("/v").to_dict()})
            ) as get,
        ):
            ops._probe_running_api_runtime()
        assert get.call_args.kwargs["headers"] == {}

    def test_connection_failure_reports_the_reason(self):
        with (
            patch.object(ops, "load_config", return_value=self._cfg()),
            patch.object(ops.httpx, "get", side_effect=OSError("connection refused")),
        ):
            build, why_not = ops._probe_running_api_runtime()
        assert build is None
        assert "connection refused" in why_not

    def test_non_200_reports_the_status(self):
        with (
            patch.object(ops, "load_config", return_value=self._cfg()),
            patch.object(ops.httpx, "get", return_value=_Resp(status_code=503)),
        ):
            build, why_not = ops._probe_running_api_runtime()
        assert build is None
        assert "503" in why_not

    def test_non_json_body_reports_the_reason(self):
        with (
            patch.object(ops, "load_config", return_value=self._cfg()),
            patch.object(ops.httpx, "get", return_value=_Resp(raise_json=True)),
        ):
            build, why_not = ops._probe_running_api_runtime()
        assert build is None
        assert "no JSON body" in why_not

    def test_an_api_predating_the_field_says_so(self):
        """A candidate without a `runtime` block cannot answer the question.
        Saying so is the only honest outcome -- treating it as a pass is the
        `[OK]`-over-a-mismatch this issue is about."""
        with (
            patch.object(ops, "load_config", return_value=self._cfg()),
            patch.object(
                ops.httpx, "get", return_value=_Resp(payload={"release_version": "2.1.0"})
            ),
        ):
            build, why_not = ops._probe_running_api_runtime()
        assert build is None
        assert "predates the running-build check" in why_not


class TestNativeApiBuildDrift:
    """Vantage-point gating: the one way this check could accuse a correct stack."""

    def _mode(self, *, native_api="started", compose=None, terraform=None):
        return ops.DeploymentMode(
            native={"api": native_api, "web": native_api},
            compose=compose or {},
            conflicts=set(),
            terraform=terraform or {},
            terraform_conflicts=set(),
        )

    def test_nothing_answering_settles_it_before_any_other_work(self):
        """The probe runs first, and that is a cost decision as much as a
        logical one: `detect_deployment_mode` runs `docker compose ps` and the
        macOS expectation costs two `brew` calls, and neither can change the
        answer once nothing is serving."""
        with (
            patch.object(ops, "_probe_running_api_runtime", return_value=(None, "did not answer")),
            patch.object(ops, "_expected_native_api_venv") as expected,
            patch.object(ops, "detect_deployment_mode") as mode,
        ):
            drift = ops._native_api_build_drift()
        assert drift.state == BUILD_UNDETERMINED
        assert "did not answer" in drift.detail
        expected.assert_not_called()
        mode.assert_not_called()

    def test_no_native_api_venv_is_not_applicable(self):
        with (
            patch.object(
                ops, "_probe_running_api_runtime", return_value=(_build("/some/venv"), "")
            ),
            patch.object(ops, "_expected_native_api_venv", return_value=("", "no venv here")),
            patch.object(ops, "detect_deployment_mode") as mode,
        ):
            drift = ops._native_api_build_drift()
        assert drift.state == BUILD_NOT_APPLICABLE
        assert "no venv here" in drift.detail
        # Nothing is surveyed once there is no native install: the question
        # has no subject, and the survey costs a `docker compose ps`.
        mode.assert_not_called()

    def test_a_containerised_api_is_not_applicable(self):
        """A Compose api's `sys.prefix` lives in its image; comparing it to a
        host keg path would report drift on a correct deployment."""
        mode = self._mode(native_api="none", compose={"api": "running", "web": "running"})
        with (
            patch.object(
                ops, "_probe_running_api_runtime", return_value=(_build("/usr/local"), "")
            ),
            patch.object(ops, "_expected_native_api_venv", return_value=("/keg/venv", "the keg")),
            patch.object(ops, "detect_deployment_mode", return_value=mode),
            patch.object(ops, "compose_core_components", return_value=["api"]),
        ):
            drift = ops._native_api_build_drift()
        assert drift.state == BUILD_NOT_APPLICABLE
        assert "container/cluster" in drift.detail
        # The container's own prefix is still reported -- the page says what is
        # running, it just does not call it drift.
        assert drift.running_prefix == "/usr/local"

    def test_a_kubernetes_host_access_bridge_is_not_applicable(self):
        """The k3s bridge binds :8000 on the host, so the api answering there
        is a Pod's -- not the native keg this host may also carry."""
        with (
            patch.object(
                ops, "_probe_running_api_runtime", return_value=(_build("/usr/local"), "")
            ),
            patch.object(ops, "_expected_native_api_venv", return_value=("/keg/venv", "the keg")),
            patch.object(ops, "detect_deployment_mode", return_value=self._mode()),
            patch.object(ops, "compose_core_components", return_value=[]),
            patch.object(ops, "_k8s_access_bridge_owns_host_ports", return_value=True),
        ):
            drift = ops._native_api_build_drift()
        assert drift.state == BUILD_NOT_APPLICABLE

    def test_a_native_api_is_compared(self):
        with (
            patch.object(ops, "detect_deployment_mode", return_value=self._mode()),
            patch.object(ops, "compose_core_components", return_value=[]),
            patch.object(ops, "_k8s_access_bridge_owns_host_ports", return_value=False),
            patch.object(ops, "_expected_native_api_venv", return_value=("/keg/venv", "the keg")),
            patch.object(ops, "_probe_running_api_runtime", return_value=(_build("/keg/venv"), "")),
        ):
            drift = ops._native_api_build_drift()
        assert drift.state == BUILD_MATCH

    def test_a_stopped_native_service_is_still_compared(self):
        """Deliberately NOT gated on the service running: a surviving
        pre-upgrade process is exactly why `brew services list` could not see
        the owner's api, so skipping the comparison there would blind the
        check in the one case it exists for."""
        with (
            patch.object(ops, "detect_deployment_mode", return_value=self._mode(native_api="none")),
            patch.object(ops, "compose_core_components", return_value=[]),
            patch.object(ops, "_k8s_access_bridge_owns_host_ports", return_value=False),
            patch.object(ops, "_expected_native_api_venv", return_value=("/new/venv", "the keg")),
            patch.object(ops, "_probe_running_api_runtime", return_value=(_build("/old/venv"), "")),
        ):
            drift = ops._native_api_build_drift()
        assert drift.state == BUILD_MISMATCH

    def test_a_native_api_on_another_venv_is_a_mismatch(self):
        with (
            patch.object(ops, "detect_deployment_mode", return_value=self._mode()),
            patch.object(ops, "compose_core_components", return_value=[]),
            patch.object(ops, "_k8s_access_bridge_owns_host_ports", return_value=False),
            patch.object(ops, "_expected_native_api_venv", return_value=("/new/venv", "the keg")),
            patch.object(
                ops,
                "_probe_running_api_runtime",
                return_value=(_build("/old/venv", exists=False), ""),
            ),
        ):
            drift = ops._native_api_build_drift()
        assert drift.state == BUILD_MISMATCH
        assert drift.remediation == ops._RUNNING_BUILD_REMEDIATION

    def test_an_unreachable_api_is_undetermined(self):
        with (
            patch.object(ops, "detect_deployment_mode", return_value=self._mode()),
            patch.object(ops, "compose_core_components", return_value=[]),
            patch.object(ops, "_k8s_access_bridge_owns_host_ports", return_value=False),
            patch.object(ops, "_expected_native_api_venv", return_value=("/new/venv", "the keg")),
            patch.object(ops, "_probe_running_api_runtime", return_value=(None, "did not answer")),
        ):
            drift = ops._native_api_build_drift()
        assert drift.state == BUILD_UNDETERMINED
        assert "did not answer" in drift.detail


class TestStopStaleApiProcess:
    """Killing a process is the strongest action here, so the gate is narrow."""

    def test_nothing_is_signalled_without_a_confirmed_mismatch(self):
        for state in (BUILD_MATCH, BUILD_UNDETERMINED, BUILD_NOT_APPLICABLE):
            with patch.object(ops.os, "kill") as kill:
                assert ops._stop_stale_api_process(_drift(state, running=_build("/old"))) == []
            kill.assert_not_called()

    def test_nothing_is_signalled_without_a_pid(self):
        drift = _drift(BUILD_MISMATCH, running=_build("/old", pid=0))
        with patch.object(ops.os, "kill") as kill:
            assert ops._stop_stale_api_process(drift) == []
        kill.assert_not_called()

    def test_this_process_is_never_signalled(self):
        drift = _drift(BUILD_MISMATCH, running=_build("/old", pid=ops.os.getpid()))
        with patch.object(ops.os, "kill") as kill:
            assert ops._stop_stale_api_process(drift) == []
        kill.assert_not_called()

    def test_sigterm_then_reports_the_exit(self):
        drift = _drift(BUILD_MISMATCH, running=_build("/old", pid=991))
        calls = []

        def _kill(pid, sig):
            calls.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError
            return None

        with patch.object(ops.os, "kill", side_effect=_kill):
            results = ops._stop_stale_api_process(drift)
        assert calls[0] == (991, ops.signal.SIGTERM)
        assert len(results) == 1 and results[0].ok
        assert "Stopped the stale api process" in results[0].message

    def test_an_already_gone_process_is_reported_as_such(self):
        drift = _drift(BUILD_MISMATCH, running=_build("/old", pid=992))
        with patch.object(ops.os, "kill", side_effect=ProcessLookupError):
            results = ops._stop_stale_api_process(drift)
        assert results[0].ok
        assert "already exited" in results[0].message

    def test_permission_denied_names_the_manual_route(self):
        drift = _drift(BUILD_MISMATCH, running=_build("/old", pid=993))
        with patch.object(ops.os, "kill", side_effect=PermissionError("not permitted")):
            results = ops._stop_stale_api_process(drift)
        assert not results[0].ok
        assert ops._RUNNING_BUILD_REMEDIATION in results[0].details

    def test_sigkill_follows_a_process_that_ignores_sigterm(self):
        """A process whose own venv was deleted can fail partway through its
        shutdown path; leaving it holding :8000 after reporting it stopped
        would be a worse report than not having tried."""
        drift = _drift(BUILD_MISMATCH, running=_build("/old", pid=994))
        sent = []

        def _kill(pid, sig):
            sent.append(sig)
            return None

        with (
            patch.object(ops.os, "kill", side_effect=_kill),
            patch.object(ops.time, "sleep"),
            patch.object(ops.time, "time", side_effect=[0.0, 100.0, 200.0]),
        ):
            results = ops._stop_stale_api_process(drift)
        assert ops.signal.SIGKILL in sent
        assert results[0].ok
        assert "ignored SIGTERM" in results[0].message


class TestReconcileRunningApiBuild:
    """AC2: install repairs the mismatch, or fails naming the fix. Never `[OK]`."""

    def test_a_match_is_a_passing_step(self):
        with patch.object(
            ops, "_native_api_build_drift", return_value=_drift(BUILD_MATCH, expected="/keg/venv")
        ):
            results = ops._reconcile_running_api_build()
        assert [r.ok for r in results] == [True]
        assert results[0].status != "WARN"

    def test_not_applicable_passes_quietly(self):
        with patch.object(
            ops,
            "_native_api_build_drift",
            return_value=_drift(BUILD_NOT_APPLICABLE, detail="no native api"),
        ):
            results = ops._reconcile_running_api_build()
        assert results[0].ok
        assert "not applicable" in results[0].message

    def test_undetermined_warns_rather_than_claiming_a_match(self):
        """A fresh machine's api may not be up yet, so this cannot fail the
        install -- but it must not be reported as verified either."""
        with patch.object(
            ops,
            "_native_api_build_drift",
            return_value=_drift(BUILD_UNDETERMINED, detail="did not answer"),
        ):
            results = ops._reconcile_running_api_build()
        assert results[0].ok
        assert results[0].status == "WARN"
        assert "Could not verify" in results[0].message

    def test_a_mismatch_stops_the_stale_process_and_restarts_the_service(self):
        stale = _drift(
            BUILD_MISMATCH,
            running=_build("/old/venv", exists=False, pid=777),
            expected="/new/venv",
            detail="pid 777 is running /old/venv",
            remediation=ops._RUNNING_BUILD_REMEDIATION,
        )
        repaired = _drift(BUILD_MATCH, running=_build("/new/venv"), expected="/new/venv")
        with (
            patch.object(ops, "_native_api_build_drift", side_effect=[stale, repaired]),
            patch.object(
                ops, "_stop_stale_api_process", return_value=[ops.OpsResult(True, "Stopped")]
            ) as stop,
            patch.object(
                ops, "_restart_native_service", return_value=[ops.OpsResult(True, "Restarted")]
            ) as restart,
            patch.object(ops.time, "sleep"),
        ):
            results = ops._reconcile_running_api_build()
        stop.assert_called_once_with(stale)
        restart.assert_called_once_with("api")
        assert all(r.ok for r in results)
        assert any("now executes the installed build" in r.message for r in results)

    def test_a_mismatch_that_survives_the_repair_fails_the_step(self):
        """The whole of this issue: an install that cannot make the running
        build be the installed build must not report success over it."""
        stale = _drift(
            BUILD_MISMATCH,
            running=_build("/old/venv", pid=778),
            expected="/new/venv",
            detail="pid 778 is running /old/venv",
            remediation=ops._RUNNING_BUILD_REMEDIATION,
        )
        with (
            patch.object(ops, "_native_api_build_drift", return_value=stale),
            patch.object(ops, "_stop_stale_api_process", return_value=[]),
            patch.object(ops, "_restart_native_service", return_value=[]),
            patch.object(ops.time, "sleep"),
            patch.object(ops.time, "time", side_effect=[0.0, 1.0, 1000.0]),
        ):
            results = ops._reconcile_running_api_build()
        failures = [r for r in results if not r.ok]
        assert len(failures) == 1
        assert ops._RUNNING_BUILD_REMEDIATION in failures[0].details
        assert "still not executing the installed build" in failures[0].message

    def test_the_step_runs_after_the_native_api_step(self, tmp_path, monkeypatch):
        """Siting is the point: the api step reports the keg it installed and
        the service it restarted, and neither is evidence about what answers
        on :8000 afterwards."""
        monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
        captured: list[str] = []

        def _run_steps(_name, steps, **_kwargs):
            captured.extend(name for name, _fn in steps)
            return [], []

        with (
            patch.object(ops, "_run_steps", side_effect=_run_steps),
            patch.object(ops, "_print_slow_steps_summary"),
            patch.object(ops, "_ops_action_outcome", return_value=("ok", "")),
            patch.object(ops, "_record_ops_action"),
        ):
            ops.install(type("Args", (), {"dev": False, "quiet": True})())

        assert "running api build" in captured
        assert captured.index("running api build") > captured.index("native api service")
        assert captured.index("running api build") > captured.index("native web service")


class TestPrintRunningApiBuild:
    """AC3: a mismatch is stated as a mismatch, never as a version."""

    def test_a_mismatch_names_both_paths_and_the_repair(self, capsys):
        ops._print_running_api_build(
            _drift(
                BUILD_MISMATCH,
                running=_build("/old/venv", exists=False, pid=42),
                expected="/new/venv",
                detail="pid 42 is running python 3.11.9 from /old/venv",
                remediation=ops._RUNNING_BUILD_REMEDIATION,
            )
        )
        out = capsys.readouterr().out
        assert "MISMATCH" in out
        assert "/old/venv" in out
        assert "/new/venv" in out
        assert ops._RUNNING_BUILD_REMEDIATION in out

    def test_a_mismatch_disowns_the_version_lines_above_it(self, capsys):
        """The surface being fixed printed the installed version for a process
        running some other venv. The block has to say which one it describes."""
        ops._print_running_api_build(
            _drift(
                BUILD_MISMATCH,
                running=_build("/old/venv", version="3.0.0rc17"),
                expected="/new/venv",
                detail="d",
            )
        )
        out = capsys.readouterr().out
        assert "describe what is INSTALLED" in out
        assert "not a statement about this process" in out

    def test_a_match_says_which_venv_is_running(self, capsys):
        ops._print_running_api_build(
            _drift(BUILD_MATCH, expected="/new/venv", running=_build("/new/venv"))
        )
        out = capsys.readouterr().out
        assert "OK" in out
        assert "/new/venv" in out
        assert "MISMATCH" not in out

    def test_undetermined_is_rendered_as_cannot_determine(self, capsys):
        ops._print_running_api_build(_drift(BUILD_UNDETERMINED, detail="did not answer"))
        out = capsys.readouterr().out
        assert "CANNOT DETERMINE" in out
        assert "did not answer" in out
        assert "OK" not in out

    def test_not_applicable_prints_nothing(self, capsys):
        """A permanent row on every Compose/Kubernetes host is what teaches an
        operator to skip the block that matters."""
        ops._print_running_api_build(_drift(BUILD_NOT_APPLICABLE, detail="no native api"))
        assert capsys.readouterr().out == ""


class TestDoctorFinding:
    """`doctor` lists things to fix, so only the confirmed mismatch is one."""

    def test_a_mismatch_is_a_finding_that_names_the_fix(self):
        drift = _drift(
            BUILD_MISMATCH,
            running=_build("/old/venv"),
            expected="/new/venv",
            detail="pid 1 is running /old/venv",
            remediation=ops._RUNNING_BUILD_REMEDIATION,
        )
        with patch.object(ops, "_native_api_build_drift", return_value=drift):
            issues = ops._running_api_build_doctor_issues()
        assert len(issues) == 1
        assert ops._RUNNING_BUILD_REMEDIATION in issues[0]
        assert "/old/venv" in issues[0]

    @pytest.mark.parametrize("state", [BUILD_MATCH, BUILD_UNDETERMINED, BUILD_NOT_APPLICABLE])
    def test_nothing_else_is_a_finding(self, state):
        """`doctor` runs on machines whose api is deliberately down; a finding
        there would train operators to ignore the list."""
        with patch.object(ops, "_native_api_build_drift", return_value=_drift(state)):
            assert ops._running_api_build_doctor_issues() == []


class TestInfraRunningBuild:
    """The dashboard's read: the api describing itself, scoped to its vantage point."""

    def test_in_cluster_is_out_of_scope(self):
        drift = ops._infra_running_build(in_cluster=True, running_mode="kubernetes")
        assert drift.state == BUILD_NOT_APPLICABLE
        assert "Kubernetes Pod" in drift.detail

    def test_a_non_native_serving_mode_is_out_of_scope(self):
        drift = ops._infra_running_build(in_cluster=False, running_mode="compose")
        assert drift.state == BUILD_NOT_APPLICABLE
        assert "compose" in drift.detail

    def test_a_native_mode_compares_this_process(self):
        with patch.object(
            ops, "_expected_native_api_venv", return_value=(ops.sys.prefix, "the keg")
        ):
            drift = ops._infra_running_build(in_cluster=False, running_mode="native")
        assert drift.state == BUILD_MATCH

    def test_a_native_mode_on_another_venv_is_a_mismatch(self):
        with patch.object(
            ops, "_expected_native_api_venv", return_value=("/some/other/venv", "the keg")
        ):
            drift = ops._infra_running_build(in_cluster=False, running_mode="native")
        assert drift.state == BUILD_MISMATCH
        assert drift.remediation == ops._RUNNING_BUILD_REMEDIATION

    def test_infra_status_carries_it_under_install_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
        expected = _drift(BUILD_MISMATCH, running=_build("/old"), expected="/new", detail="d")
        with (
            patch.object(ops, "_infra_running_build", return_value=expected),
            patch.object(ops, "_which", return_value=None),
            patch.object(ops, "detect_deployment_mode") as mode,
            patch.object(ops.self_heal, "compose_probe") as probe,
            patch.object(ops, "terraform_stack_state", return_value={}),
            patch.object(ops, "_serving_status", return_value={}),
            patch.object(ops, "_in_cluster", return_value=False),
        ):
            mode.return_value = ops.DeploymentMode(
                native={"api": "started"},
                compose={},
                conflicts=set(),
                terraform={},
                terraform_conflicts=set(),
            )
            probe.return_value.available = True
            probe.return_value.reason = ""
            probe.return_value.statuses = []
            payload = ops.infra_status()

        running_build = payload["install_mode"]["running_build"]
        assert running_build["state"] == BUILD_MISMATCH
        assert running_build["running"]["prefix"] == "/old"
        assert running_build["expected_prefix"] == "/new"


class TestLegacyStalePathIsReachable:
    """The path #4133 observed is the one this code is pointed at.

    Not a tautology: `~/.nyxGPT/opt/nyxgpt-api/venv` is the macOS *dev*-mode
    and Linux layout, so a macOS *artifact* install -- the acceptance channel
    -- expects a keg venv and a process on that legacy path is a mismatch by
    construction. That is what makes the owner's state detectable at all.
    """

    def test_the_legacy_ops_venv_mismatches_a_keg_expectation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
        legacy = tmp_path / ".nyxGPT" / "opt" / "nyxgpt-api" / "venv"
        keg_venv = tmp_path / "opt" / "nyxgpt-api@3.0.0rc" / "libexec" / "venv"
        keg_venv.mkdir(parents=True)

        mode = ops.DeploymentMode(
            native={"api": "started", "web": "started"},
            compose={},
            conflicts=set(),
            terraform={},
            terraform_conflicts=set(),
        )
        with (
            patch.object(ops.platform, "system", return_value="Darwin"),
            patch.object(ops, "read_install_mode") as read_mode,
            patch.object(ops, "_native_artifact_service_name", return_value="nyxgpt-api@3.0.0rc"),
            patch.object(ops, "_brew_path", return_value=tmp_path),
            patch.object(ops, "detect_deployment_mode", return_value=mode),
            patch.object(
                ops,
                "_probe_running_api_runtime",
                # python3.11, the legacy path, and the *new* version string --
                # every field except the prefix looks correct.
                return_value=(
                    RuntimeBuild(
                        executable=str(legacy / "bin" / "python3"),
                        prefix=str(legacy),
                        python="3.11.9",
                        pid=4133,
                        version="3.0.0rc17",
                        prefix_exists=False,
                    ),
                    "",
                ),
            ),
        ):
            read_mode.return_value.is_dev = False
            drift = ops._native_api_build_drift()

        assert drift.state == BUILD_MISMATCH
        assert str(legacy) in drift.summary()
        assert str(keg_venv) in drift.summary()
        assert "next restart" in drift.detail


def test_the_stale_path_from_the_issue_is_not_referenced_as_a_macos_artifact_venv():
    """Guard for the inverse-claims half: the keg is self-contained.

    #4133's technical notes record that the rc17 keg wrapper already execs
    `libexec/venv/bin/python3` and that `~/.nyxGPT/opt/nyxgpt-*/venv` is not
    the macOS artifact layout. If an artifact-mode macOS install ever starts
    answering with the `~/.nyxGPT` venv again, the comparison above silently
    stops discriminating -- so the routing predicate is pinned here rather
    than only in the per-layout tests.
    """
    with (
        patch.object(ops.platform, "system", return_value="Darwin"),
        patch.object(ops, "read_install_mode") as read_mode,
        patch.object(ops, "_native_artifact_service_name", return_value="nyxgpt-api@3.0.0rc"),
        patch.object(ops, "_brew_path", return_value=None),
        patch.object(ops, "_which", return_value="/opt/homebrew/bin/brew"),
        patch.object(ops, "_installed_keg_version", return_value=None),
    ):
        read_mode.return_value.is_dev = False
        prefix, _ = ops._expected_native_api_venv()
    assert ".nyxGPT" not in prefix
    assert prefix == ""


def test_running_build_module_does_not_import_ops():
    """`running_build` sits below both callers, like `brew_services` (D-022)."""
    source = (Path(ops.__file__).parent / "running_build.py").read_text(encoding="utf-8")
    assert "import ops" not in source
    assert "from nyxgpt.ops" not in source
    assert "from nyxgpt.self_heal" not in source
