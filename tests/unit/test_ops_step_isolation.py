"""The unit suite must not reconcile the machine it runs on (#3789).

`ops.install()` and `ops._install_terraform_steps()` are lists of
machine-mutating steps; their tests neutralize them by patching each step by
name. That convention is only safe while the name lists are complete, and they
had drifted -- `_build_terraform_docker_images` ran a real `docker build`
mid-suite, `_sync_grafana_slack_webhook_secret` wrote the real
`~/.nyxGPT/secrets/slack-webhook-url`, `_clear_intentional_stops` cleared the
machine's real intentional-stop markers, and nine tests failed deterministically
on any machine with no `docker` on PATH.

Two guards live here:

* the enumeration guards, which read the real step lists out of `ops.py` and
  fail if a step is added without being added to `ops_step_isolation`; and
* a guard/fault-injection pair (the #3753 template) proving the patching is
  load-bearing -- the same install with one step left real does reach the
  filesystem, so the guard above cannot pass vacuously.

`ops.env_sync()` is a step list too, and its tests never adopted that
convention: they call it directly with a `tmp_path` config, which isolates
nothing about the two machine-facing things its "sync grafana slack webhook
secret" step does (#3947). Those two are neutralized for the whole unit suite
by `_isolate_grafana_secret_files` / `real_restart_grafana_if_running` in
`conftest.py`; the guards at the bottom of this file are what keeps that true.
"""

from __future__ import annotations

import subprocess
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from ops_step_isolation import (
    INSTALL_STEP_FUNCS,
    K8S_INSTALL_STEP_FUNCS,
    TERRAFORM_INSTALL_STEP_FUNCS,
    patch_steps,
    step_funcs_from_source,
)

from nyxgpt import ops

pytestmark = pytest.mark.unit


def test_install_step_list_is_fully_enumerated():
    """Adding a step to `ops.install()` must add it to `INSTALL_STEP_FUNCS`.

    Otherwise the new step runs for real in every test that patches "all" the
    steps -- which is how `_install_native_api`, `_generate_compose_config` and
    the observability tail came to execute against the runner.
    """
    assert step_funcs_from_source(ops.install) == set(INSTALL_STEP_FUNCS)


def test_terraform_install_step_list_is_fully_enumerated():
    """Same guard for the Terraform bring-up list."""
    assert step_funcs_from_source(
        ops._install_terraform_steps, extra=("_terraform_stack_health",)
    ) == set(TERRAFORM_INSTALL_STEP_FUNCS)


def test_kubernetes_install_step_list_is_fully_enumerated():
    """Same guard for the Kubernetes bring-up list."""
    assert step_funcs_from_source(
        ops._install_kubernetes_steps, extra=("_k8s_stack_health", "_k8s_observability_health")
    ) == set(K8S_INSTALL_STEP_FUNCS)


def test_patched_kubernetes_steps_leave_the_real_machine_alone(monkeypatch, tmp_path):
    """With every step patched, the Kubernetes bring-up touches no filesystem state."""
    monkeypatch.setattr(ops.self_heal.Path, "home", staticmethod(lambda: tmp_path))

    with ExitStack() as stack:
        stack.enter_context(patch.object(ops, "_refuse_port_collision", return_value=None))
        patch_steps(stack, K8S_INSTALL_STEP_FUNCS)
        results = ops.install_kubernetes_local(api_key="k")

    assert all(r.ok for r in results)
    assert list(tmp_path.iterdir()) == []


def test_an_unpatched_kubernetes_step_really_would_reach_the_machine(monkeypatch, tmp_path):
    """Fault injection for the Kubernetes list, same pattern as above."""
    monkeypatch.setattr(ops.self_heal.Path, "home", staticmethod(lambda: tmp_path))

    with ExitStack() as stack:
        stack.enter_context(patch.object(ops, "_refuse_port_collision", return_value=None))
        patch_steps(stack, K8S_INSTALL_STEP_FUNCS, skip=("_clear_intentional_stops",))
        results = ops.install_kubernetes_local(api_key="k")

    assert all(r.ok for r in results)
    assert (tmp_path / ".nyxGPT" / "self_heal_state.json").exists()


def test_patched_install_steps_leave_the_real_machine_alone(monkeypatch, tmp_path):
    """With every step patched, `ops.install()` touches no filesystem state."""
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops.self_heal.Path, "home", staticmethod(lambda: tmp_path))

    with ExitStack() as stack:
        patch_steps(stack, INSTALL_STEP_FUNCS)
        rc = ops.install(
            SimpleNamespace(dev=False, skip_observability=False, terraform=False, kubernetes=False)
        )

    assert rc == 0
    assert list(tmp_path.iterdir()) == []


def test_an_unpatched_install_step_really_would_reach_the_machine(monkeypatch, tmp_path):
    """Fault injection: prove the guard above is not vacuously true (#3753).

    The same install with `_clear_intentional_stops` left real writes
    `~/.nyxGPT/self_heal_state.json` for real -- on a developer's machine that
    clears the intentional-stop markers `nyxgpt ops stop` set, so self-heal
    resumes restarting components the owner deliberately stopped. If this test
    ever stops passing, the steps became inert and the convention can be
    retired.
    """
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops.self_heal.Path, "home", staticmethod(lambda: tmp_path))

    with ExitStack() as stack:
        patch_steps(stack, INSTALL_STEP_FUNCS, skip=("_clear_intentional_stops",))
        rc = ops.install(
            SimpleNamespace(dev=False, skip_observability=False, terraform=False, kubernetes=False)
        )

    assert rc == 0
    assert (tmp_path / ".nyxGPT" / "self_heal_state.json").exists()


def test_patched_terraform_steps_leave_the_real_machine_alone(monkeypatch, tmp_path):
    """With every step patched, the Terraform bring-up touches no filesystem state."""
    monkeypatch.setattr(ops.self_heal.Path, "home", staticmethod(lambda: tmp_path))

    with ExitStack() as stack:
        stack.enter_context(patch.object(ops, "_refuse_port_collision", return_value=None))
        patch_steps(stack, TERRAFORM_INSTALL_STEP_FUNCS)
        results = ops.install_terraform_local(api_key="k")

    assert all(r.ok for r in results)
    assert list(tmp_path.iterdir()) == []


def test_an_unpatched_terraform_step_really_would_reach_the_machine(monkeypatch, tmp_path):
    """Fault injection for the Terraform list, same pattern as above."""
    monkeypatch.setattr(ops.self_heal.Path, "home", staticmethod(lambda: tmp_path))

    with ExitStack() as stack:
        stack.enter_context(patch.object(ops, "_refuse_port_collision", return_value=None))
        patch_steps(stack, TERRAFORM_INSTALL_STEP_FUNCS, skip=("_clear_intentional_stops",))
        results = ops.install_terraform_local(api_key="k")

    assert all(r.ok for r in results)
    assert (tmp_path / ".nyxGPT" / "self_heal_state.json").exists()


def _env_sync_invocation(tmp_path, monkeypatch) -> MagicMock:
    """A `nyxgpt ops env-sync` whose every *declared* path is under `tmp_path`.

    Which is the whole point: `env_sync` takes `--config` and `--env-file` and
    nothing else, so a test can look fully isolated and still have the slack
    webhook step write the invoking machine's `~/.nyxGPT/secrets` -- the path
    for that is derived from `Path.home()`, not from anything passed in.
    `_sync_packaged_resources` is patched out because it resolves the *other*
    real-home constant (`ops.NYXGPT_HOME`); it is not what these two guards are
    about.
    """
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[monitoring]\nenabled = true\n"
        "slack_webhook_url = https://hooks.slack.com/services/T000/B000/isolated\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", tmp_path / "config.docker.ini")
    monkeypatch.setattr(ops, "_sync_packaged_resources", lambda: [ops.OpsResult(True, "synced")])

    args = MagicMock()
    args.config = str(cfg_path)
    args.env_file = str(tmp_path / ".env")
    args.quiet = True
    return args


def test_env_sync_writes_the_grafana_slack_secret_outside_the_real_home(tmp_path, monkeypatch):
    """`env_sync` must not write the running machine's Slack webhook secret (#3947).

    Running the unit suite on a machine with a webhook configured used to
    overwrite `~/.nyxGPT/secrets/slack-webhook-url` -- with the *unconfigured*
    placeholder, whenever the test's config carried no webhook -- and the value
    is not recoverable from Slack, which shows an incoming-webhook URL once.

    The second assertion is what keeps the first from passing vacuously: the
    step really does perform the write, it just lands in `tmp_path` now.
    """
    args = _env_sync_invocation(tmp_path, monkeypatch)
    secret_path = ops._slack_webhook_secret_path()

    assert Path.home() not in secret_path.parents
    assert ops.env_sync(args) == 0
    assert (
        secret_path.read_text(encoding="utf-8").strip()
        == "https://hooks.slack.com/services/T000/B000/isolated"
    )


def test_env_sync_never_restarts_the_machines_own_grafana(tmp_path, monkeypatch):
    """`env_sync` must not reach the live Docker daemon (#3947).

    Injects the exact condition that made this class visible -- a `grafana`
    container present and never coming back healthy. Unstubbed,
    `_sync_grafana_slack_webhook_secret` restarts it and polls for two minutes
    before returning 2, so every `env_sync` test on that machine fails
    `assert rc == 0` for a reason none of them is about. That is what cost
    #3947 three verification runs, under two different-looking symptoms.
    """
    args = _env_sync_invocation(tmp_path, monkeypatch)
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "restarting"})
    monkeypatch.setattr(ops, "_wait_for_grafana_healthy", lambda: False)
    monkeypatch.setattr(ops, "_run", fake_run)

    assert ops.env_sync(args) == 0
    assert [c for c in commands if c[-2:] == ["restart", "grafana"]] == []
