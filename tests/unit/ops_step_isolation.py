"""Shared step-isolation helpers for the `ops.install()` unit tests (#3789).

`ops.install()` and `ops._install_terraform_steps()` are step lists of
machine-mutating functions: they build Docker images, write the real
`~/.nyxGPT` (service units, LaunchAgents, `docker-compose.yml`, the Grafana
Slack secret), install Homebrew formulas and run `terraform apply`. Their unit
tests neutralize them by patching each step **by name**, which only works while
every name is listed -- and the lists have grown since the tests were written,
so steps went unpatched and ran for real: the suite built the 567MB
`nyxgpt-api:local` and 1.16GB `nyxgpt-web:local` images mid-run, wrote the real
`~/.nyxGPT/secrets/slack-webhook-url`, and cleared the machine's real
intentional-stop markers. It also made the suite pass or fail by what the
machine happened to have -- `test_install_terraform_steps_records_success` and
eight `ops.install()` tests fail deterministically with no `docker` on PATH,
which is how this surfaced (a failed gate pytest run on #3789/PR #3791).

The lists below are the fix's single source of truth, kept honest by
`tests/unit/test_ops_step_isolation.py`, which parses the real step lists out
of `src/nyxgpt/ops.py` and fails if a step is added without being added here.
That closes the enumeration pattern ledger entry V-015 records as open, rather
than patching the three names that happened to bite.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable, Iterable, Sequence
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

from nyxgpt import ops

# Every function `ops.install()` runs as a step, in step order (the
# observability tail runs unless `--skip-observability` is passed).
INSTALL_STEP_FUNCS: tuple[str, ...] = (
    "_sync_packaged_resources",
    "_clear_intentional_stops",
    "_install_config",
    # Added to `ops.install()` by #3859 and not to this list, which is what
    # this list's own guard test exists to catch: an unlisted step is not
    # patched out, so it runs for real in every test that patches "all" steps.
    # Here that step shells out to `launchctl` to find jobs a previous install
    # left loaded. It is inert off macOS, which is why the isolation tests
    # passed on the runner without it -- but on a developer's Mac an unpatched
    # step queries real launchd, which is the #3789 hazard this list closes.
    "_report_orphaned_launchd_jobs",
    "_reconcile_install_mode",
    "_ensure_docker_engine",
    "migrate_legacy_volumes",
    "_reconcile_phantom_compose_app_containers",
    "_ensure_web_deps",
    "_ensure_mcp_deps",
    "_ensure_cassandra_container",
    "_install_cassandra_log_follower_service",
    "_install_ollama_log_follower_service",
    "_install_ollama_env_agent",
    "_install_native_api",
    "_install_native_web",
    "_ensure_native_ollama_service",
    "_cleanup_stale_log_symlinks",
    "sync_env_from_config",
    "_generate_compose_config",
    "_ensure_required_models",
    "_ensure_glitchtip_secrets_dir",
    "_sync_grafana_slack_webhook_secret",
    "_reconcile_grafana_provisioning",
    "_provision_glitchtip",
)

# Every function `ops._install_kubernetes_steps()` runs as a step, plus the
# health call it makes when no step fails.
K8S_INSTALL_STEP_FUNCS: tuple[str, ...] = (
    "_clear_intentional_stops",
    "_ensure_kubectl_and_cluster",
    "_build_and_load_k8s_api_image",
    "_build_and_load_k8s_web_image",
    "_ensure_k8s_secret",
    "_kubectl_apply_kustomization",
    "_wait_for_k8s_data_tier",
    "_sync_packaged_resources",
    "_apply_k8s_observability",
    "_record_k8s_install_mode",
    "_wait_for_k8s_app_tier",
    "_wait_for_k8s_observability",
    "_k8s_stack_health",
    "_preflight_k8s_capacity",
    "_k8s_observability_health",
)

# Every function `ops._install_terraform_steps()` runs as a step, plus the
# health call it makes when no step fails.
TERRAFORM_INSTALL_STEP_FUNCS: tuple[str, ...] = (
    "_sync_packaged_resources",
    "_clear_intentional_stops",
    "migrate_legacy_volumes",
    "_ensure_terraform_binary",
    "_ensure_terraform_tfvars",
    "_generate_compose_config",
    "_ensure_required_models",
    "_record_terraform_install_mode",
    "_resolve_terraform_images",
    "_terraform_init_plan_apply",
    "_ensure_glitchtip_secrets_dir",
    "_sync_grafana_slack_webhook_secret",
    "_start_observability_stack_terraform",
    "_provision_glitchtip",
    "_sync_local_terraform_config",
    "_terraform_stack_health",
)


def _step_tuples(tree: ast.AST) -> Iterable[ast.expr]:
    """Yield each `(name, fn)` step tuple in a `steps = [...]` / `steps.append(...)` body."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == "steps" for t in targets):
                yield from getattr(node.value, "elts", [])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "steps"
        ):
            yield from node.args


def step_funcs_from_source(func: Callable[..., Any], extra: Sequence[str] = ()) -> set[str]:
    """Return the step functions `func`'s step list actually dispatches through.

    Reads the source rather than trusting a hand-kept list: each step is
    `(label, fn)` where `fn` is either the function itself or a lambda wrapping
    a call to it. `extra` names functions called outside the step list (the
    terraform path's `_terraform_stack_health`); each must really appear as a
    call in the source, so a stale entry there fails too.
    """
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    # Steps defined INSIDE the function under test are locals, not module
    # attributes, so no caller can patch them by name -- enumerating them would
    # only produce a list entry that `patch.object(ops, ...)` raises on. They
    # are a real hole in this isolation (a nested step still runs for real),
    # but a hole this guard cannot close: closing it means lifting the step to
    # module scope. The terraform path's `_resolve_images` was exactly that
    # case, until #3863 lifted it to module scope so it could be patched; any
    # future nested step lands here again and is surfaced, not silently run.
    local_defs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } - {func.__name__}
    found: set[str] = set()
    for element in _step_tuples(tree):
        fn = element.elts[1] if isinstance(element, ast.Tuple) else element
        if isinstance(fn, ast.Name):
            found.add(fn.id)
        else:  # a lambda wrapping the call, e.g. `lambda: _install_native_api(dev=dev)`
            found.update(
                call.func.id
                for call in ast.walk(fn)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            )
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for name in extra:
        assert name in called, f"{name} is no longer called by {func.__name__}"
        found.add(name)
    return found - local_defs


def patch_steps(
    stack: ExitStack,
    names: Sequence[str],
    results: list[ops.OpsResult] | None = None,
    skip: Sequence[str] = (),
) -> dict[str, MagicMock]:
    """Patch every step in `names` (except `skip`) to return `results`.

    Enter this first so a test's own, more specific patch of a single step
    still wins. Returns the `{name: MagicMock}` map for per-step assertions;
    `skip` is for tests that deliberately exercise one real step (and for the
    fault-injection proof that this patching is load-bearing).
    """
    ok = results if results is not None else [ops.OpsResult(True, "ok")]
    return {
        name: stack.enter_context(patch.object(ops, name, return_value=ok))
        for name in names
        if name not in skip
    }
