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
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

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
