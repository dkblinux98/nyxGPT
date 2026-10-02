"""Bounds for subprocesses a request thread can end up waiting on (#3858).

FastAPI runs a plain `def` handler in Starlette's AnyIO threadpool -- 40
workers by default, and nothing in `app.py` raises that. A subprocess with no
timeout holds its worker for as long as the command blocks, so an unreachable
dependency that *blackholes* rather than refuses (a kubeconfig context aimed at
a torn-down cluster, a Docker socket hop to a host that stopped answering)
turns every polling dashboard request into a permanently held worker. Enough of
them and every sync endpoint queues behind the exhausted pool, `/health`
included: a whole-API outage caused by a dependency the running deployment may
not even use.

This module is the one place that says what "bounded" means, so a new caller
cannot reinvent a subtly different answer:

* `PROBE_TIMEOUT_SECONDS` -- the bound for a *status probe*: a read-only call
  made to answer a polled endpoint. Short by design; a probe that has not
  answered in five seconds has already failed as far as a dashboard poll is
  concerned.
* `TIMEOUT_RETURNCODE` (124, GNU `timeout(1)`'s convention) -- how a timeout is
  reported to a caller that expects a `CompletedProcess`. A timeout is a
  *result*, not a traceback: `subprocess.TimeoutExpired` reaching a handler is
  a 500 on a status endpoint, which is exactly the honest-degraded-reading
  failure this bound exists to prevent.
* `bounded_argv` -- adds the tool's *own* dial bound where it offers one
  (`kubectl --request-timeout`). Deliberately both bounds: the flag makes the
  tool give up with its own clean message, and the Python `timeout=` catches
  everything the flag does not (a wedged TLS handshake, a hung DNS lookup, a
  binary that ignores the flag entirely). The flag is *not* added when we are
  running inside a Pod, because there it breaks kubectl outright -- see
  `bounded_argv`. The Python bound, which carries the whole safety property
  above, applies everywhere unconditionally.
* `kubectl_env` -- hands a `kubectl` child the kubeconfig kubectl's own default
  resolution would have used. Not a bound; it lives here because it needs the
  same in-a-Pod exception, and two copies of that exception would be free to
  disagree (#3956). See its docstring for why `kubectl` on a k3s node is not
  kubectl.

Enumeration of every `subprocess.run`/`subprocess.Popen` in `src/nyxgpt/`, as
of #3858 -- **18 call sites, 10 of them reachable from an HTTP handler**. Kept
here because the enumeration is what stops the next helper from being written
unbounded; re-check it when a new subprocess is added.

Reachable from a handler, *polled* (bounded -- these are the trap):

1. `canary._run` (`canary.py`) -- `/canary/status`, `/admin/overview`, and the
   canary action endpoints. Bounded here (#3858).
2. `ops._run` (`ops.py`) -- `/infra/status`, `/self-heal/*`, `/admin/overview`,
   `/monitoring`. Bounded here (#3858).
3. `self_heal._run` (`self_heal.py`) -- `/self-heal/status`, `/admin/overview`.
   Already carried a 30s `timeout=`, but let `TimeoutExpired` escape to the
   handler; converted to a 124 result here (#3858).
4. `cloud_artifact_smoke._run` (`cloud_artifact_smoke.py`) -- `/ops/cloud-artifact-smoke`.
   Already bounded (mandatory `timeout=`, 124 on expiry); the model this
   module generalizes.
5. `rag.embeddings` `nvidia-smi` probe -- GPU facts behind the resource
   endpoints. Already bounded (`timeout=2`).

Reachable from a handler, *long-running mutation* (deliberately unbounded):

6. `cloud_infra.ensure_terraform_binary` (`brew install terraform`)
7. `cloud_infra._run_terraform` (`terraform apply`/`destroy`)
8. `cloud_deploy.run_remote` (SSH; optional `timeout=`, set by its probe callers)
9. `cloud_deploy.provision_remote` (`Popen`, SSH `bash -s` install stream)
10. `cloud_deploy.open_tunnel` background `Popen` (detached, outlives the request)

    Each sits behind an explicit `POST` that an operator triggered and that
    takes minutes by contract (`/cloud/infra/apply`, `/cloud/infra/destroy`,
    `/cloud/deploy`). A five-second bound would break the operation itself, and
    nothing polls them, so they cannot exhaust the pool the way a dashboard
    poll can. They are listed, not fixed: the distinction is the point.

Not reachable from a handler (CLI-only, no bound needed):

11-16. `ops` install/dev-mode plumbing -- `npm ci`/`npm run build` for the web
    artifact, `_run_npm` (x2), the MCP dep install, `kubectl port-forward`
    (`nyxgpt ops port-forward`, foreground by contract), and `doctor`'s
    `node -p require.resolve` probe. Reached only from `install()`/`doctor()`,
    which no endpoint calls (`ops.up` is a CLI entrypoint; `self_heal` only
    names it in prose).
17. `cloud_deploy.open_tunnel` foreground `Popen` -- `background=False` is a
    CLI-only path that blocks until the operator interrupts it.
18. `workflow_log_store` `gh` invocation -- retrospective tooling, not imported
    by anything the API serves.
"""

from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

# What a status probe reachable from a polled HTTP endpoint is allowed to cost.
# Sized for a call that dials something (kubectl to an API server, docker to a
# daemon): a dial that has not answered in five seconds is the failure mode
# this exists for, and a healthy one answers in milliseconds.
PROBE_TIMEOUT_SECONDS = 5.0

# The same idea for a polled probe that asks the *local* machine instead --
# `brew services list`, `systemctl --user is-active`, `launchctl list`. These
# cannot blackhole on a network, but a cold `brew services list` on a loaded
# machine genuinely takes seconds, so bounding it at the dial timeout would
# invent failures. Still bounded: a wedged service manager must not be able to
# hold a request thread indefinitely either.
LOCAL_PROBE_TIMEOUT_SECONDS = 15.0

# GNU `timeout(1)`'s exit code for "the command was killed for running too
# long". Callers distinguish a timeout from any other failure with `timed_out`
# rather than by string-matching stderr.
TIMEOUT_RETURNCODE = 124

# kubectl subcommands that stream or watch. `--request-timeout` bounds every
# request kubectl makes, including the watch these hold open, so adding it here
# would cut the command off mid-stream -- exactly the behavior these callers
# are asking for the opposite of. They carry their own `--timeout` instead.
_KUBECTL_STREAMING_SUBCOMMANDS = frozenset(
    {"attach", "exec", "logs", "port-forward", "proxy", "rollout", "wait"}
)

# kubectl global flags that consume the following argv token as their value.
# Needed so `kubectl get pods -n logs` (a namespace named "logs") is not read
# as the streaming `logs` subcommand. Only the value-taking ones matter: an
# unlisted boolean flag simply doesn't hide the token after it, and an
# unlisted value-taking flag can at worst make a positional out of its value,
# which errs toward *skipping* the flag rather than adding it to a watch.
_KUBECTL_VALUE_FLAGS = frozenset(
    {
        "-n",
        "--namespace",
        "--context",
        "--cluster",
        "--kubeconfig",
        "--user",
        "-s",
        "--server",
        "--token",
        "--as",
        "--as-group",
        "--cache-dir",
        "--certificate-authority",
        "--client-certificate",
        "--client-key",
        "--tls-server-name",
        "-o",
        "--output",
        "-v",
        "--v",
    }
)


# Set by the kubelet in every Pod, and the same variable client-go's own
# in-cluster detection keys on (`rest.InClusterConfig`). Its presence is the
# cheapest honest answer to "would kubectl reach the API server through the
# mounted service account rather than a kubeconfig file?".
_IN_CLUSTER_ENV_VAR = "KUBERNETES_SERVICE_HOST"


def _running_in_cluster() -> bool:
    """True when this process is running inside a Kubernetes Pod.

    Read at call time, not import time: a test (and a long-lived process whose
    environment is rewritten) must be able to change the answer.
    """
    return bool(os.environ.get(_IN_CLUSTER_ENV_VAR))


def _positional_args(args: list[str]) -> list[str]:
    """The argv tokens that are subcommands/operands rather than flags or flag values."""
    positionals: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            # `--namespace=logs` carries its own value; `--namespace logs` eats
            # the next token.
            skip_next = "=" not in arg and arg in _KUBECTL_VALUE_FLAGS
            continue
        positionals.append(arg)
    return positionals


def timeout_message(timeout: float) -> str:
    """The one wording for an expired bound, so every surface reads the same."""
    return f"timed out after {timeout:.0f}s"


def timed_out(result: subprocess.CompletedProcess[str]) -> bool:
    """True when `result` came from an expired bound rather than the command itself.

    A command *could* exit 124 on its own; treating that as a timeout reports
    "timed out" for a command that failed some other way, which is a strictly
    better failure than the reverse (reporting a hang as a normal non-zero exit
    and leaving the operator to guess).
    """
    return result.returncode == TIMEOUT_RETURNCODE


def timeout_result(
    cmd: list[str], exc: subprocess.TimeoutExpired, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Turn an expired bound into the `CompletedProcess` shape every caller already handles.

    Whatever the command managed to emit before it was killed is preserved --
    it is often the whole diagnosis (#3783) -- and decoded defensively because
    `TimeoutExpired` carries bytes or str depending on how the run was
    configured.
    """
    captured = "".join(
        part.decode("utf-8", "replace") if isinstance(part, bytes) else (part or "")
        for part in (exc.stdout, exc.stderr)
    )
    return subprocess.CompletedProcess(cmd, TIMEOUT_RETURNCODE, captured, timeout_message(timeout))


# Where kubectl's own default resolution looks when `$KUBECONFIG` is unset.
# Named here rather than left implicit because the whole point of
# `kubectl_env` below is that on some machines kubectl does NOT look here.
_DEFAULT_KUBECONFIG_RELPATH = Path(".kube") / "config"


def default_kubeconfig() -> Path | None:
    """The kubeconfig kubectl would read by default, if it exists.

    A plain `Path.home()/".kube"/"config"` existence check, resolved at call
    time so a test (and a service whose HOME changes) gets the answer for the
    environment it is actually in.
    """
    try:
        candidate = Path.home() / _DEFAULT_KUBECONFIG_RELPATH
    except RuntimeError:  # pragma: no cover - no home directory resolvable
        return None
    return candidate if candidate.is_file() else None


def kubectl_env(cmd: list[str], env: dict[str, str] | None) -> dict[str, str] | None:
    """Name the default kubeconfig explicitly for a `kubectl` child process (#3956).

    **`kubectl` is not always kubectl.** On a k3s node -- which is what
    `nyxgpt cloud deploy --kubernetes` creates -- `/usr/local/bin/kubectl` is a
    symlink to the `k3s` binary, and k3s's kubectl shim defaults `$KUBECONFIG`
    to `/etc/rancher/k3s/k3s.yaml` when the variable is unset. That file is
    mode 0600 and root-owned, so every nyxGPT kubectl call made as the login
    user failed with `permission denied` -- and `~/.kube/config`, which the
    deploy writes for exactly this purpose, was never consulted because k3s had
    already answered the question kubectl's own default would have answered.

    The owner's 2026-08-26 acceptance round is what that cost: on a running
    k3s cluster, `nyxgpt cloud canary status` reported *"this process is
    currently running in native mode"* and pointed at `ops install
    --kubernetes` -- the capability #3506 chose the substrate for, reporting
    itself absent. Self-heal's Pod watchdog was blind on the same box for the
    same reason.

    So the fix is not "export KUBECONFIG in one more place" (the provisioning
    script already does, and a later `ssh host nyxgpt ...` inherits none of
    it): every kubectl child this codebase spawns is handed the kubeconfig
    kubectl's *own* default resolution would have used, which makes the call
    independent of whose kubectl is on PATH. On a machine with real kubectl
    this is a no-op by construction -- it names the file kubectl was going to
    read anyway.

    Three cases are left alone, each deliberately:

    * **`$KUBECONFIG` already set** -- the operator (or the bridge unit) has
      chosen, and a product default must not overrule a stated choice.
    * **inside a Pod** -- the same trap `bounded_argv` documents at length:
      kubectl only falls back to the mounted service account while the merged
      config is still the built-in default, so setting `KUBECONFIG` there
      sends it to `http://localhost:8080` instead of the API server. In-cluster
      is the one place a kubeconfig file is *not* how kubectl reaches the
      cluster.
    * **no `~/.kube/config`** -- there is nothing to name, and inventing a
      path would turn "no cluster configured here" into "a kubeconfig that
      does not exist", which reads as a broken deployment rather than none.

    Returns `env` unchanged in those cases, so a caller can pass the result
    straight through to `subprocess.run(env=...)`.
    """
    if not cmd or Path(cmd[0]).name != "kubectl":
        return env
    if _running_in_cluster():
        return env
    base = env if env is not None else dict(os.environ)
    if base.get("KUBECONFIG", "").strip():
        return env
    kubeconfig = default_kubeconfig()
    if kubeconfig is None:
        return env
    return {**base, "KUBECONFIG": str(kubeconfig)}


def bounded_argv(cmd: list[str], timeout: float | None) -> list[str]:
    """Add the tool's own dial bound to `cmd` when it offers one and `timeout` is set.

    Only `kubectl` does today (`--request-timeout`), which is the tool the
    polled canary/infra probes actually dial a network with. An argv that
    already sets the flag is left alone, as is any streaming subcommand (see
    `_KUBECTL_STREAMING_SUBCOMMANDS`) and any unbounded call.

    The streaming check reads *positional* tokens only, so a flag value that
    happens to spell a streaming subcommand (`-n logs`) doesn't suppress the
    bound. It scans every positional rather than just the first because an
    unlisted value-taking global flag would shift which token that is, and
    wrongly bounding a watch is the worse of the two mistakes: it cuts a
    healthy slow rollout short and reports it as a failure, whereas wrongly
    skipping the flag still leaves the Python `timeout=` in force.

    **The flag is never added from inside a Pod, because there it does not
    bound kubectl -- it breaks it.** kubectl only falls back to the mounted
    service account when the kubeconfig it merged is *identical* to the
    built-in default (client-go's `DeferredLoadingClientConfig` consults
    `IsDefaultConfig` before `InClusterConfig`). `--request-timeout` lands in
    the client-config overrides, so the merged config stops comparing equal,
    the fallback is skipped, and kubectl dials the no-config default server
    `http://localhost:8080` -- connection refused, every call, on the one
    deployment mode canary and the Pod probes exist for. Proven on a live kind
    cluster: `canary-track-metrics-smoke` failed with
    `Get "http://localhost:8080/api?timeout=5s": connection refused` (the
    injected bound visible in the query string) on two consecutive heads while
    the same job was green without the flag. A workstation with a real
    kubeconfig is unaffected, which is why only a cluster test can see this.
    Losing the flag in-cluster costs the clean message only; the Python
    `timeout=` still bounds the identical hang, and it is the half that carries
    the pool-exhaustion property.
    """
    if timeout is None or not cmd:
        return cmd
    if Path(cmd[0]).name != "kubectl":
        return cmd
    if _running_in_cluster():
        return cmd
    if any(arg.startswith("--request-timeout") for arg in cmd[1:]):
        return cmd
    if any(arg in _KUBECTL_STREAMING_SUBCOMMANDS for arg in _positional_args(cmd[1:])):
        return cmd
    # Rounded *up*, and never below 1: kubectl reads `--request-timeout=0s` as
    # "no timeout", so a sub-second bound formatted naively would silently
    # remove the very bound it was asked to add.
    return [cmd[0], f"--request-timeout={max(1, math.ceil(timeout))}s", *cmd[1:]]
