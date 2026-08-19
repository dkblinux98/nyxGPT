"""Unit tests for `nyxgpt cloud smoke` (P6-17, #3515).

Nothing here touches AWS, SSH or HTTP: `cloud_deploy`'s deploy/destroy/tunnel
functions and `cloud_smoke._http` are replaced with recorders, so the tests
assert on the thing that actually matters about this command -- that the
verification phases run in the right order against the tunneled API, and that
the teardown happens on *every* exit path, including the ones an exception
takes. A regression there is a recurring AWS bill, which is why most of the
cases below are failure cases.
"""

import argparse
import json
import os
import subprocess

import pytest

from nyxgpt import cloud_deploy, cloud_infra, cloud_smoke
from nyxgpt.cloud import CloudCommandError

MARKER = f"XYZZY-NYXGPT-CLOUD-{os.getpid()}"


@pytest.fixture(autouse=True)
def _isolated_cloud_home(tmp_path, monkeypatch):
    """Point every path the modules read or write at a temp dir."""
    cloud_dir = tmp_path / ".nyxGPT" / "cloud"
    cloud_dir.mkdir(parents=True)
    monkeypatch.setattr(cloud_deploy, "CLOUD_DIR", cloud_dir)
    monkeypatch.setattr(cloud_deploy, "DEPLOY_STATE_FILE", cloud_dir / "deploy.json")
    monkeypatch.setattr(cloud_deploy, "DEPLOY_HISTORY_FILE", cloud_dir / "history.jsonl")
    monkeypatch.setattr(cloud_deploy, "TUNNEL_STATE_FILE", cloud_dir / "tunnel.json")
    monkeypatch.setattr(cloud_deploy, "TUNNEL_LOG_FILE", cloud_dir / "tunnel.log")
    monkeypatch.setattr(cloud_infra, "CLOUD_STATE_FILE", cloud_dir / "state.json")
    monkeypatch.setattr(cloud_infra, "SETTINGS_FILE", cloud_dir / "infra.json")
    # A substrate exists unless a test says otherwise: that is the state the
    # teardown has real work to do in.
    tfstate = cloud_dir / "terraform.tfstate"
    tfstate.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cloud_infra, "TFSTATE_FILE", tfstate)
    return cloud_dir


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """Collapse the phases' settle/poll delays so the tests run instantly."""
    monkeypatch.setattr(cloud_smoke.time, "sleep", lambda *_args: None)


class FakeHttp:
    """Stand-in for `cloud_smoke._http` that records calls and canned answers.

    Answers a healthy deployment by default; `responses` overrides individual
    endpoints, keyed by path for the API and by full URL for an observability
    UI on its own port.
    """

    def __init__(self, responses: dict[str, tuple[int, str]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        path: str,
        *,
        payload: dict | None = None,
        api_key: str = "",
        timeout: float = 30.0,
        base: str = cloud_smoke.API_BASE,
    ) -> tuple[int, str]:
        """Record the call and return the canned (or default) response."""
        self.calls.append({"path": path, "payload": payload, "api_key": api_key, "base": base})
        for key in (f"{base}{path}", path):
            if key in self.responses:
                return self.responses[key]
        if base != cloud_smoke.API_BASE:
            return 200, "ok"
        if path == "/models":
            return 200, json.dumps({"models": ["llama3.1:8b"]})
        if path == "/models/required":
            return 200, json.dumps(
                {
                    "base_url": "http://127.0.0.1:11434",
                    "reachable": True,
                    "error": "",
                    "ready": True,
                    "remediation": "",
                    "models": [
                        {
                            "role": "chat",
                            "model": "llama3.1:8b",
                            "setting": "[nyxgpt] default_model",
                            "present": True,
                        },
                        {
                            "role": "embedding",
                            "model": "nomic-embed-text",
                            "setting": "[rag] embedding_model",
                            "present": True,
                        },
                    ],
                }
            )
        if path == "/rag/query":
            return 200, json.dumps({"results": [{"text": f"the phrase is {MARKER}."}]})
        if path == "/chat":
            return 200, json.dumps({"reply": "OK"})
        return 200, "{}"

    def paths(self) -> list[str]:
        """Return just the API paths that were called, in order."""
        return [str(c["path"]) for c in self.calls if c["base"] == cloud_smoke.API_BASE]


class FakeDeploy:
    """Recorder for `cloud_deploy.deploy`/`destroy`."""

    def __init__(
        self, *, profiles: list[str] | None = None, error: Exception | None = None
    ) -> None:
        self.profiles = (
            ["monitoring", "logging", "tracing", "errors"] if profiles is None else profiles
        )
        self.error = error
        self.deploys = 0
        self.destroys = 0
        self.destroy_error: Exception | None = None

    def deploy(self, _args) -> dict:
        """Pretend to deploy, or raise the configured error."""
        self.deploys += 1
        if self.error:
            raise self.error
        return {
            "action": "deploy",
            "plan": {"version": "3.0.0", "profiles": list(self.profiles)},
            "target": {
                "host": "198.51.100.10",
                "user": "ec2-user",
                "identity_file": "",
                "region": "us-east-1",
                "instance_id": "i-0abc",
                "security_group_id": "sg-0abc",
            },
        }

    def destroy(self, _args) -> dict:
        """Record the teardown, or raise the configured teardown error."""
        self.destroys += 1
        if self.destroy_error:
            raise self.destroy_error
        return {"action": "destroy", "tunnel": {}, "settings": {"aws_region": "us-east-1"}}


def _install(monkeypatch, *, deploy: FakeDeploy, http: FakeHttp, tunnel_running: bool = True):
    """Replace every side effect `run_smoke` has with a recorder."""
    monkeypatch.setattr(cloud_deploy, "deploy", deploy.deploy)
    monkeypatch.setattr(cloud_deploy, "destroy", deploy.destroy)
    monkeypatch.setattr(cloud_smoke, "_http", http)
    monkeypatch.setattr(
        cloud_deploy,
        "tunnel_status",
        lambda: {"running": tunnel_running, "pid": 4242, "host": "198.51.100.10", "urls": {}},
    )
    monkeypatch.setattr(cloud_deploy, "start_tunnel", lambda *a, **k: {"running": True, "pid": 1})
    monkeypatch.setattr(
        cloud_deploy,
        "wait_for_health",
        lambda *a, **k: {
            "healthy": True,
            "url": "http://localhost:8000/health",
            "status": 200,
            "waited": 1.0,
        },
    )
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=json.dumps({"api_key": "secret-key", "default_model": "llama3.1:8b"}) + "\n",
            stderr="",
        ),
    )


def _args(**overrides) -> argparse.Namespace:
    base = {
        "host": None,
        "ssh_user": None,
        "identity_file": None,
        "version": "3.0.0",
        "skip_observability": False,
        "skip_deploy": False,
        "keep": False,
        "yes": False,
        "api_key": None,
        "health_timeout": None,
        "ssh_timeout": None,
        "model_timeout": None,
        "chat_timeout": None,
        "rag_timeout": None,
        "observability_timeout": None,
        "json": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _steps(result: dict) -> list[str]:
    return [step["step"] for step in result["steps"]]


# --- The happy path ------------------------------------------------------


def test_smoke_deploys_verifies_and_tears_down(monkeypatch):
    deploy, http = FakeDeploy(), FakeHttp()
    _install(monkeypatch, deploy=deploy, http=http)

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is True
    assert result["failure"] == ""
    assert _steps(result) == [
        "deploy",
        "access",
        "model",
        "chat",
        "rag",
        "observability",
        "teardown",
    ]
    assert deploy.deploys == 1
    assert deploy.destroys == 1
    assert result["teardown"]["destroyed"] is True
    # Chat and RAG are verified in that order, through the tunneled API.
    assert http.paths() == ["/models/required", "/chat", "/rag/ingest", "/rag/query"]


def test_verification_goes_through_the_tunnel_with_the_instances_api_key(monkeypatch):
    deploy, http = FakeDeploy(), FakeHttp()
    _install(monkeypatch, deploy=deploy, http=http)

    cloud_smoke.run_smoke(_args())

    chat = next(c for c in http.calls if c["path"] == "/chat")
    # localhost, because the instance publishes nothing off loopback -- the
    # tunnel is the only path in, so verifying the app verifies that path.
    assert cloud_smoke.API_BASE == "http://localhost:8000/api/v1"
    assert chat["base"] == cloud_smoke.API_BASE
    assert chat["api_key"] == "secret-key"


def test_an_explicit_api_key_beats_the_instances_own(monkeypatch):
    deploy, http = FakeDeploy(), FakeHttp()
    _install(monkeypatch, deploy=deploy, http=http)

    cloud_smoke.run_smoke(_args(api_key="from-the-flag"))

    assert all(
        c["api_key"] == "from-the-flag" for c in http.calls if c["base"] == cloud_smoke.API_BASE
    )


def test_the_run_is_recorded_in_the_deploy_history(monkeypatch, _isolated_cloud_home):
    _install(monkeypatch, deploy=FakeDeploy(), http=FakeHttp())

    cloud_smoke.run_smoke(_args())

    history = cloud_deploy.deploy_history()
    assert history[0]["action"] == "smoke"
    assert history[0]["outcome"] == "succeeded"
    assert history[0]["instance_id"] == "i-0abc"


# --- Teardown always happens --------------------------------------------


def test_teardown_runs_when_chat_fails(monkeypatch):
    deploy = FakeDeploy()
    http = FakeHttp({"/chat": (500, '{"detail": "ollama exploded"}')})
    _install(monkeypatch, deploy=deploy, http=http)

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is False
    assert "chat request failed" in result["failure"]
    assert deploy.destroys == 1
    assert result["teardown"]["destroyed"] is True
    # The failing phase records no step, and RAG/observability are never
    # attempted -- but the teardown still is.
    assert _steps(result) == ["deploy", "access", "model", "teardown"]


def test_teardown_runs_when_rag_does_not_return_the_ingested_marker(monkeypatch):
    deploy = FakeDeploy()
    http = FakeHttp({"/rag/query": (200, json.dumps({"results": []}))})
    _install(monkeypatch, deploy=deploy, http=http)

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is False
    assert "did not surface the phrase" in result["failure"]
    assert deploy.destroys == 1


def test_teardown_runs_when_the_deploy_itself_fails(monkeypatch):
    deploy = FakeDeploy(error=CloudCommandError("terraform apply failed"))
    _install(monkeypatch, deploy=deploy, http=FakeHttp())

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is False
    assert "terraform apply failed" in result["failure"]
    # A half-applied substrate is exactly the case that must not be left billing.
    assert deploy.destroys == 1


def test_teardown_runs_after_an_unexpected_exception(monkeypatch):
    deploy = FakeDeploy(error=RuntimeError("boto3 blew up"))
    _install(monkeypatch, deploy=deploy, http=FakeHttp())

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is False
    assert "unexpected error: RuntimeError: boto3 blew up" in result["failure"]
    assert deploy.destroys == 1


def test_teardown_runs_after_a_keyboard_interrupt(monkeypatch):
    deploy = FakeDeploy(error=KeyboardInterrupt())
    _install(monkeypatch, deploy=deploy, http=FakeHttp())

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is False
    assert "interrupted" in result["failure"]
    assert deploy.destroys == 1


def test_a_failed_teardown_fails_the_run_even_when_every_check_passed(monkeypatch):
    deploy = FakeDeploy()
    deploy.destroy_error = CloudCommandError("terraform destroy failed")
    _install(monkeypatch, deploy=deploy, http=FakeHttp())

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is False
    assert "terraform destroy failed" in result["teardown"]["error"]
    assert "still be running and billing" in result["teardown"]["reason"]


def test_teardown_with_nothing_provisioned_is_not_a_failure(monkeypatch):
    cloud_infra.TFSTATE_FILE.unlink()
    deploy = FakeDeploy(error=CloudCommandError("apply failed before creating anything"))
    _install(monkeypatch, deploy=deploy, http=FakeHttp())

    result = cloud_smoke.run_smoke(_args())

    assert deploy.destroys == 0
    assert result["teardown"]["error"] == ""
    assert result["teardown"]["reason"] == "nothing was provisioned"
    # The verification failure still fails the run; only the teardown is clean.
    assert result["passed"] is False


def test_keep_leaves_the_deployment_up_and_says_so(monkeypatch):
    deploy = FakeDeploy()
    _install(monkeypatch, deploy=deploy, http=FakeHttp())

    result = cloud_smoke.run_smoke(_args(keep=True))

    assert result["passed"] is True
    assert deploy.destroys == 0
    assert result["teardown"]["kept"] is True
    assert "still up" in result["teardown"]["reason"]


# --- Individual phases ---------------------------------------------------


def test_access_opens_a_tunnel_when_none_is_running(monkeypatch):
    deploy, http = FakeDeploy(), FakeHttp()
    _install(monkeypatch, deploy=deploy, http=http, tunnel_running=False)
    opened: list[str] = []
    monkeypatch.setattr(
        cloud_deploy, "start_tunnel", lambda target, *a, **k: opened.append(target.host) or {}
    )

    result = cloud_smoke.run_smoke(_args())

    assert opened == ["198.51.100.10"]
    assert result["steps"][1] == {
        "step": "access",
        "tunnel_opened": True,
        "url": "http://localhost:8000/health",
        "waited_seconds": 1.0,
    }


def test_access_fails_when_the_stack_never_answers_through_the_tunnel(monkeypatch):
    deploy = FakeDeploy()
    _install(monkeypatch, deploy=deploy, http=FakeHttp())
    monkeypatch.setattr(
        cloud_deploy,
        "wait_for_health",
        lambda *a, **k: {
            "healthy": False,
            "url": "http://localhost:8000/health",
            "status": 0,
            "waited": 9.0,
        },
    )

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is False
    assert "did not answer 200" in result["failure"]
    assert deploy.destroys == 1


def test_the_smoke_never_pulls_a_model_itself(monkeypatch):
    """V-017: a smoke that pulls the model cannot catch an install that didn't.

    The instance's own `nyxgpt ops install` pulls both required models
    (#3824), so this run must only ever read readiness.
    """
    http = FakeHttp()
    _install(monkeypatch, deploy=FakeDeploy(), http=http)

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is True
    assert "/models/pull" not in http.paths()
    assert "/models/required" in http.paths()


def test_a_missing_required_model_fails_the_run(monkeypatch):
    """The defect this issue exists to catch: healthy stack, no chat model."""
    missing = json.dumps(
        {
            "base_url": "http://127.0.0.1:11434",
            "reachable": True,
            "error": "",
            "ready": False,
            "remediation": "Re-run `nyxgpt ops install`",
            "models": [
                {
                    "role": "chat",
                    "model": "llama3.1:8b",
                    "setting": "[nyxgpt] default_model",
                    "present": False,
                }
            ],
        }
    )
    http = FakeHttp({"/models/required": (200, missing)})
    deploy = FakeDeploy()
    _install(monkeypatch, deploy=deploy, http=http)

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is False
    assert "missing required model(s) llama3.1:8b" in result["failure"]
    assert deploy.destroys == 1


def test_an_unreachable_ollama_fails_the_run(monkeypatch):
    unreachable = json.dumps(
        {
            "base_url": "http://127.0.0.1:11434",
            "reachable": False,
            "error": "RuntimeError: connection refused",
            "ready": False,
            "remediation": "",
            "models": [
                {
                    "role": "chat",
                    "model": "llama3.1:8b",
                    "setting": "[nyxgpt] default_model",
                    "present": None,
                }
            ],
        }
    )
    http = FakeHttp({"/models/required": (200, unreachable)})
    _install(monkeypatch, deploy=FakeDeploy(), http=http)

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is False
    assert "Ollama did not answer" in result["failure"]


def test_every_forwarded_observability_ui_is_probed(monkeypatch):
    http = FakeHttp()
    _install(monkeypatch, deploy=FakeDeploy(), http=http)

    result = cloud_smoke.run_smoke(_args())

    probed = {
        str(c["base"]) + str(c["path"]) for c in http.calls if c["base"] != cloud_smoke.API_BASE
    }
    assert probed == {
        "http://localhost:3001/api/health",
        "http://localhost:9090/-/ready",
        "http://localhost:16686/",
        "http://localhost:8080/",
    }
    observability = next(s for s in result["steps"] if s["step"] == "observability")
    assert observability["services"] == {
        "grafana": 200,
        "prometheus": 200,
        "jaeger": 200,
        "glitchtip": 200,
    }


def test_an_unreachable_observability_ui_fails_the_run(monkeypatch):
    http = FakeHttp({"http://localhost:16686/": (0, "connection refused")})
    deploy = FakeDeploy()
    _install(monkeypatch, deploy=deploy, http=http)

    result = cloud_smoke.run_smoke(_args(observability_timeout=1))

    assert result["passed"] is False
    assert "jaeger (localhost:16686)" in result["failure"]
    assert deploy.destroys == 1


def test_a_login_redirect_or_401_still_counts_as_reachable(monkeypatch):
    http = FakeHttp(
        {
            "http://localhost:3001/api/health": (401, "unauthorized"),
            "http://localhost:8080/": (302, ""),
        }
    )
    _install(monkeypatch, deploy=FakeDeploy(), http=http)

    result = cloud_smoke.run_smoke(_args())

    assert result["passed"] is True


def test_observability_is_skipped_when_no_profile_is_deployed(monkeypatch):
    http = FakeHttp()
    _install(monkeypatch, deploy=FakeDeploy(profiles=[]), http=http)

    result = cloud_smoke.run_smoke(_args(skip_observability=True))

    assert result["passed"] is True
    observability = next(s for s in result["steps"] if s["step"] == "observability")
    assert observability["skipped"] is True
    assert not [c for c in http.calls if c["base"] != cloud_smoke.API_BASE]


def test_remote_settings_returns_nothing_when_the_instance_cannot_be_read(monkeypatch):
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["ssh"], returncode=255, stdout="", stderr="x"
        ),
    )
    assert cloud_smoke.remote_settings(cloud_deploy.DeployTarget(host="h")) == {}


def test_remote_settings_reads_the_key_and_model_off_the_instance(monkeypatch):
    monkeypatch.setattr(
        cloud_deploy,
        "run_remote",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout="warning: noise\n" + json.dumps({"api_key": "k", "default_model": "m"}),
            stderr="",
        ),
    )
    assert cloud_smoke.remote_settings(cloud_deploy.DeployTarget(host="h")) == {
        "api_key": "k",
        "default_model": "m",
    }


# --- `--skip-deploy` -----------------------------------------------------


def test_skip_deploy_verifies_the_existing_deployment(monkeypatch, _isolated_cloud_home):
    (_isolated_cloud_home / "state.json").write_text(
        json.dumps(
            {
                "region": "us-east-1",
                "instance_id": "i-0abc",
                "public_ip": "198.51.100.10",
                "security_group_id": "sg-0abc",
            }
        ),
        encoding="utf-8",
    )
    (_isolated_cloud_home / "deploy.json").write_text(
        json.dumps({"version": "3.0.0", "profiles": ["monitoring"], "host": "198.51.100.10"}),
        encoding="utf-8",
    )
    deploy = FakeDeploy()
    _install(monkeypatch, deploy=deploy, http=FakeHttp())

    result = cloud_smoke.run_smoke(_args(skip_deploy=True, yes=True))

    assert deploy.deploys == 0
    assert result["steps"][0] == {"step": "deploy", "skipped": True, "host": "198.51.100.10"}
    assert result["passed"] is True
    assert deploy.destroys == 1


def test_skip_deploy_without_confirmation_refuses_to_destroy_someone_elses_deployment(
    monkeypatch, capsys
):
    deploy = FakeDeploy()
    _install(monkeypatch, deploy=deploy, http=FakeHttp())

    exit_code = cloud_smoke.smoke_command(_args(skip_deploy=True))

    assert exit_code == 2
    assert deploy.deploys == 0 and deploy.destroys == 0
    assert "Re-run with --yes" in capsys.readouterr().err


# --- The command wrapper -------------------------------------------------


def test_smoke_command_exits_zero_on_a_pass(monkeypatch, capsys):
    _install(monkeypatch, deploy=FakeDeploy(), http=FakeHttp())

    assert cloud_smoke.smoke_command(_args()) == 0
    assert "PASSED" in capsys.readouterr().out


def test_smoke_command_exits_nonzero_on_a_failure(monkeypatch, capsys):
    _install(monkeypatch, deploy=FakeDeploy(), http=FakeHttp({"/chat": (500, "boom")}))

    assert cloud_smoke.smoke_command(_args()) == 1
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "chat request failed" in err


def test_smoke_command_can_print_the_whole_record_as_json(monkeypatch, capsys):
    _install(monkeypatch, deploy=FakeDeploy(), http=FakeHttp())

    assert cloud_smoke.smoke_command(_args(json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "smoke"
    assert payload["passed"] is True
    assert [s["step"] for s in payload["steps"]][-1] == "teardown"


def test_keep_warns_that_the_deployment_is_still_billing(monkeypatch, capsys):
    _install(monkeypatch, deploy=FakeDeploy(), http=FakeHttp())

    assert cloud_smoke.smoke_command(_args(keep=True)) == 0
    assert "still billing" in capsys.readouterr().err


# --- Wiring --------------------------------------------------------------


def test_the_dashboard_pointer_for_the_smoke_test_comes_from_the_backend():
    # The admin Cloud page renders LIFECYCLE_COMMANDS rather than its own
    # strings, so this is what puts `nyxgpt cloud smoke` on that surface.
    assert cloud_deploy.LIFECYCLE_COMMANDS["smoke"] == "nyxgpt cloud smoke"


def test_cli_routes_cloud_smoke_to_the_smoke_command(monkeypatch):
    from nyxgpt import cli

    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(cli.cloud_smoke_mod, "smoke_command", lambda args: seen.append(args) or 0)

    assert cli.cli(["cloud", "smoke", "--keep", "--skip-observability"]) == 0
    assert seen[0].keep is True
    assert seen[0].skip_observability is True
