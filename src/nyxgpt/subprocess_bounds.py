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
  binary that ignores the flag entirely).

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


def bounded_argv(cmd: list[str], timeout: float | None) -> list[str]:
    """Add the tool's own dial bound to `cmd` when it offers one and `timeout` is set.

    Only `kubectl` does today (`--request-timeout`), which is the tool the
    polled canary/infra probes actually dial a network with. An argv that
    already sets the flag is left alone, as is any streaming subcommand (see
    `_KUBECTL_STREAMING_SUBCOMMANDS`) and any unbounded call.
    """
    if timeout is None or not cmd:
        return cmd
    if Path(cmd[0]).name != "kubectl":
        return cmd
    if any(arg.startswith("--request-timeout") for arg in cmd[1:]):
        return cmd
    if any(arg in _KUBECTL_STREAMING_SUBCOMMANDS for arg in cmd[1:]):
        return cmd
    return [cmd[0], f"--request-timeout={timeout:.0f}s", *cmd[1:]]
