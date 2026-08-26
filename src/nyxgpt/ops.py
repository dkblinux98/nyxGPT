"""Operational commands for `nyxgpt ops`: install, status, doctor, restart, logs, env-sync.

Wraps the native (Homebrew services + LaunchAgents) and Docker-managed
(Cassandra container, Docker Compose stack) pieces of a local nyxGPT
deployment behind a single CLI surface, so operators never need to run raw
`brew`/`docker`/`launchctl` commands themselves. Also cross-checks for a
Compose deployment running alongside the native one so `status`/`restart`
can warn about -- and refuse to create -- port collisions between the two.
"""

from __future__ import annotations

import base64
import configparser
import contextlib
import getpass
import hashlib
import importlib.metadata
import importlib.resources
import ipaddress
import json
import logging
import os
import platform
import plistlib
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Container, Iterator, Mapping, Sequence
from configparser import ConfigParser
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from nacl import public as nacl_public

from nyxgpt import (
    brew_services,
    docker_access,
    model_bootstrap,
    release_tarball,
    restart_state,
    self_heal,
    tracing,
)
from nyxgpt import metrics as prom_metrics
from nyxgpt import verify as verify_mod
from nyxgpt.config import (
    VALID_SESSION_BACKENDS,
    describe_config_parse_error,
    get_error_tracking_config,
    get_error_tracking_enabled,
    get_log_aggregation_enabled,
    get_monitoring_config,
    get_monitoring_slack_webhook_url,
    get_session_backend,
    get_tracing_config,
    get_tracing_enabled,
    grafana_admin_password_path,
    read_grafana_admin_password,
    resolve_grafana_admin_password,
)
from nyxgpt.install_mode import (
    CHANNEL_CANDIDATE,
    CHANNEL_DEV,
    CHANNEL_STABLE,
    DEV_LAUNCHD_LABELS,
    INSTALL_MODE_ARTIFACT,
    INSTALL_MODE_DEV,
    MANAGER_BREW,
    MANAGER_LAUNCHD,
    MANAGER_SYSTEMD,
    MANAGER_UNKNOWN,
    SUBSTRATE_KUBERNETES,
    SUBSTRATE_TERRAFORM,
    InstallIdentity,
    InstallModeState,
    clear_install_mode,
    install_mode_file,
    read_install_mode,
    write_install_mode,
)
from nyxgpt.k8s_pod_state import classify_pod
from nyxgpt.logging import get_correlation_id

# The vendored-source tarball builder lives in its own stdlib-only module
# (#3741) so release tooling -- `scripts/build_homebrew_artifacts.py`, run by
# CI jobs that only check the repo out -- can import it without dragging in
# this module's runtime dependencies (httpx, pynacl, the metrics/tracing
# stack). Re-exported here because the local file:// tap install path below
# has always called these by their `ops.` names.
from nyxgpt.release_tarball import (  # noqa: F401
    _WEB_VENDOR_EXCLUDES,
    _sha256_file,
    _vendor_tree,
    build_release_dist_tarball,
)
from nyxgpt.subprocess_bounds import (
    LOCAL_PROBE_TIMEOUT_SECONDS,
    PROBE_TIMEOUT_SECONDS,
    bounded_argv,
    timed_out,
    timeout_message,
    timeout_result,
)

logger = logging.getLogger(__name__)

# Repo root: .../nyxGPT/src/nyxgpt/ops.py -> parents[2] is repo root.
#
# #3621 retired every REPO_ROOT-relative lookup the `nyxgpt ops
# install`/`up` local-stack reconciliation path depended on (Compose file,
# config/provisioning templates, launchd/systemd unit templates, helper
# scripts -- see `_sync_packaged_resources`) in favor of package data
# resolved via `importlib.resources`. REPO_ROOT itself stays, scoped to
# operations that are inherently repo-checkout-dependent regardless of
# Python packaging: building distributable artifacts FROM source (Homebrew
# tap tarball vendoring, the self-contained Linux venv build, Docker image
# builds for Terraform/Kubernetes local deploy), Terraform/Kubernetes local
# deploy itself (terraform/*.tf and k8s/*.yaml are files on disk, not
# importable package data), the `web/` npm project (its own build/packaging
# concern, not shipped as Python package data), and dev-checkout-only
# doctor/version diagnostics that no-op cleanly when REPO_ROOT doesn't
# exist. `tests/unit/test_repo_root_allowlist.py` guards this boundary: it
# fails if a new REPO_ROOT-relative lookup appears anywhere it isn't already
# on the allowlist.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Maps a logical component to the *stable* Homebrew formula its native-mode
# service is named after. Cassandra has no native brew service -- per
# product_management/PHASE_6_PLAN.md it stays the one ops-managed Docker
# container even under native-first, so it's tracked via
# `_docker_container_state` instead (see `detect_deployment_mode`).
#
# Imported rather than restated: `self_heal.py` needs the same answer, and the
# two hand-maintained copies agreeing by convention is what shipped #3853 (see
# `nyxgpt/brew_services.py`). Never look a component up by this name directly
# on macOS -- resolve it against a live `brew services list` snapshot with
# `brew_services.resolve`, because a candidate install registers
# `nyxgpt-api@<line>rc`.
NATIVE_BREW_SERVICES = brew_services.NATIVE_BREW_SERVICES

# Linux twin of NATIVE_BREW_SERVICES: maps a logical component to its
# systemd --user unit name (see ops/systemd/*.service, #3508). "ollama" gets
# its own nyxgpt-ollama.service so it's managed the same operator-facing way
# as api/web -- including when a distro-installed system-wide `ollama.service`
# (e.g. from the official installer) already holds port 11434, which install
# stops and disables to take the port over (#3632; see
# `_system_ollama_service_conflicts`/`_takeover_system_ollama_service`).
NATIVE_SYSTEMD_SERVICES: dict[str, str] = {
    "api": "nyxgpt-api",
    "web": "nyxgpt-web",
    "ollama": "nyxgpt-ollama",
}


def _is_macos() -> bool:
    """True if running on macOS -- the Homebrew services + launchd native path."""
    return platform.system() == "Darwin"


def _is_linux() -> bool:
    """True if running on Linux -- the systemd --user native path (#3508)."""
    return platform.system() == "Linux"


def _unsupported_os_result(action: str) -> list[OpsResult]:
    """Standard failure for a native-mode step on an OS with no dispatch branch.

    Native install is supported on macOS (Homebrew/launchd) and Linux
    (systemd) only -- Compose/Terraform/Kubernetes deployment modes are
    unaffected since they never touch host service managers.
    """
    return [
        OpsResult(
            False,
            f"{action}: unsupported OS for native mode ({platform.system()})",
            "Native install (nyxgpt ops install, no --terraform/--kubernetes) supports "
            "macOS and Linux only. Use --terraform or --kubernetes on other platforms.",
        )
    ]


# Host port each component binds to under Docker Compose (see docker-compose.yml).
# Used only for collision messaging -- detection itself is state-based.
COMPOSE_COMPONENT_PORTS: dict[str, int] = {
    "api": 8000,
    "web": 3000,
    "ollama": 11434,
    "cassandra": 9042,
}

# Local web UI URL once the stack is up. EVERY local mode binds `web` to this
# same host port (COMPOSE_COMPONENT_PORTS above, app.py's CORS allowlist),
# Kubernetes included since #3986: the provisioned kind cluster publishes the
# web NodePort here, and a cluster whose host ports nyxGPT cannot map gets a
# managed background forward onto it instead (`_ensure_k8s_host_access`). It
# used to be the one exception -- ClusterIP Services and a hand-run
# `kubectl port-forward` -- which is the defect that issue removed.
WEB_URL = "http://127.0.0.1:3000"

NATIVE_CONFIG_HINT = "~/.nyxGPT/config.ini"
COMPOSE_CONFIG_HINT = (
    "~/.nyxGPT/docker/config.docker.ini (mounted into the Compose 'api' container)"
)

# Runtime data the ops layer needs -- the Compose file, its config/provisioning
# templates, launchd/systemd unit templates, and a handful of helper scripts
# -- ships inside the installed package under `nyxgpt.resources` (see
# pyproject.toml's package-data and `src/nyxgpt/resources/`) instead of being
# resolved relative to REPO_ROOT: an installed, non-editable build has no
# repo checkout alongside it for REPO_ROOT-relative lookups to find (#3621).
# `_sync_packaged_resources` (called by `install()`) copies that packaged
# tree into this fixed, writable, ops-managed location once per install;
# every lookup below just reads from here afterwards, uniformly across a dev
# checkout and an installed package.
NYXGPT_HOME = Path.home() / ".nyxGPT"
OPS_COMPOSE_FILE = NYXGPT_HOME / "docker-compose.yml"
OPS_DOCKER_DIR = NYXGPT_HOME / "docker"
OPS_LAUNCHAGENTS_DIR = NYXGPT_HOME / "ops" / "launchagents"
OPS_SYSTEMD_TEMPLATES_DIR = NYXGPT_HOME / "ops" / "systemd"
OPS_SCRIPTS_SRC_DIR = NYXGPT_HOME / "scripts"

# Where nyxgpt drops CLI tools it installs on the operator's behalf when no
# package manager can supply them (currently `kind`/`kubectl` for
# `nyxgpt ops install --kubernetes --local` -- see `_ensure_cli_tool`, #3724).
# Kept inside the ops-managed home rather than a system location so no step
# ever needs sudo; `_ensure_nyxgpt_bin_on_path` puts it on PATH for the
# running process so the very same run can use what it just installed.
NYXGPT_BIN_DIR = NYXGPT_HOME / "bin"

# Install mode (artifact vs dev checkout) lives in `nyxgpt.install_mode` --
# see that module for what the two modes are and why the choice is recorded
# rather than re-derived (#3789).

# The container api-config is a derived, per-machine artifact (like .env):
# it's bind-mounted into the containerized api and gets its DSN filled in at
# runtime by `nyxgpt ops glitchtip-init`, so it isn't part of the packaged
# resources synced by `_sync_packaged_resources`. `nyxgpt ops
# install`/`env-sync` regenerates it from the native `~/.nyxGPT/config.ini`
# (the single source of truth) via `_generate_compose_config`.
COMPOSE_CONFIG_FILE = OPS_DOCKER_DIR / "config.docker.ini"

# Compose override that attaches the observability profiles to the
# terraform-managed network (`nyxgpt-terraform`) so they interoperate with the
# terraform-managed core containers -- used by `install --terraform --local`.
TERRAFORM_NET_OVERRIDE = OPS_DOCKER_DIR / "docker-compose.terraform-net.yml"

# Placeholder substituted with the installing user's home directory when a
# LaunchAgent plist template is copied into ~/Library/LaunchAgents -- the
# templates in ops/launchagents/ must never hard-code a real account's home
# directory (see #3276 acceptance failure: the merged plists hard-coded the
# original author's `/Users/darlabaker`, so the installed LaunchAgent pointed
# at a nonexistent script path for every other user).
LAUNCHAGENT_HOME_PLACEHOLDER = "__NYXGPT_HOME__"

# Container path promtail's docker-compose.yml service binds to native-mode
# host logs (~/.nyxGPT/logs). `_log_aggregation_wiring_issue` checks the
# running promtail container's actual mounts (via `docker inspect`) for this
# destination to catch a regression (see #3277, #3349) where that bind mount
# is dropped and native-mode logs silently stop reaching Loki.
PROMTAIL_NATIVE_LOG_MOUNT_MARKER = "/var/log/nyxgpt-native/logs"

# --- Container data layout (see docs/docker-compose.md#volumes, issue #3346) ---
#
# All container data lives in plain host bind mounts under ~/.nyxGPT/volumes/
# instead of opaque named Docker volumes -- visible/attributable on the host
# filesystem and, for the three "shared" directories below, reused as-is by
# every deployment mode that mounts them (rather than each mode keeping its
# own duplicate copy). `VOLUME_DIR_NAMES` maps a logical component name to
# its directory under ~/.nyxGPT/volumes/; `volume_dir()` resolves the full
# path. Kept in this module (rather than config.py) since it's purely a
# Docker/ops concern -- native processes' own state (~/.nyxGPT/config.ini,
# ~/.nyxGPT/logs, ...) is unrelated and untouched by any of this.
VOLUME_DIR_NAMES: dict[str, str] = {
    # Shared across every mode that mounts them: the native `nyxgpt-cassandra`
    # container (`_ensure_cassandra_container` below), docker-compose.yml, and
    # terraform/main.tf all bind to the *same* ~/.nyxGPT/volumes/cassandra and
    # ~/.nyxGPT/volumes/nyxgpt-data directories -- ollama is now shared by
    # native mode too (see #3431): native Ollama isn't containerized, but
    # `_ensure_ollama_service` points its `OLLAMA_MODELS` env var at
    # ~/.nyxGPT/volumes/ollama/models, the same directory Compose/Terraform's
    # `ollama` container bind-mounts (as /root/.ollama), instead of native
    # Ollama's own default ~/.ollama/models store.
    "cassandra": "cassandra",
    "ollama": "ollama",
    "nyxgpt-data": "nyxgpt-data",
    # Compose-only today (no native/Terraform equivalent), so no per-mode
    # duplication to worry about -- see OBSERVABILITY_PROFILES.
    "prometheus": "prometheus",
    "grafana": "grafana",
    "loki": "loki",
    "glitchtip-postgres": "glitchtip-postgres",
    "glitchtip-uploads": "glitchtip-uploads",
}


def volume_dir(component: str) -> Path:
    """Return `~/.nyxGPT/volumes/<component>`'s host bind-mount path, creating it if needed."""
    p = Path.home() / ".nyxGPT" / "volumes" / VOLUME_DIR_NAMES[component]
    _ensure_dir(p)
    return p


@dataclass(frozen=True)
class DeploymentMode:
    """Snapshot of what's actually running, native vs. Docker Compose vs. Terraform.

    `native`/`compose`/`terraform` map component name -> a state string
    ("started"/"running"/"none"/"absent"/...); `conflicts` lists components
    reported live in both native and Compose (a same-port phantom-backend
    conflict). `terraform_conflicts` lists components reported live under
    Terraform *and* under native or Compose at once -- a whole second core
    stack left running after an incomplete mode switch (#3565: `nyxgpt ops
    down` without `--terraform` followed by `nyxgpt ops install` left both
    the native and Terraform stacks up, each answering on its own network,
    while this dataclass's pre-fix `conflicts` field -- native-vs-Compose
    only -- had no way to represent it and logged `conflicts=[]`). `terraform`
    defaults to `{}` and `terraform_conflicts` to `[]` so every existing
    positional/keyword construction (tests included) stays valid without
    populating them.
    """

    native: dict[str, str]
    compose: dict[str, str]
    conflicts: list[str]
    terraform: dict[str, str] = field(default_factory=dict)
    terraform_conflicts: list[str] = field(default_factory=list)
    # Why a container read came back `unknown`, when one did (#4022). Empty
    # whenever every read was answered -- so a caller can print the cause of a
    # `DOCKER_STATE_UNKNOWN` in `native`/`terraform` instead of leaving the
    # operator to guess at it. Defaulted, so every existing construction
    # (tests included) stays valid.
    docker_probe_reason: str = ""


@dataclass(frozen=True)
class OpsResult:
    """Outcome of a single ops step: whether it succeeded, plus human-readable detail.

    `status` overrides the stdout label this result prints under (see
    `_result_status_label`) without touching `ok`, which stays the sole input
    to exit codes and to `_ops_action_outcome`. It exists for the states that
    are genuinely neither success nor failure -- a Pod that is still starting
    is not a healthy Pod, but calling it `[FAIL]` reports a mid-rollout
    snapshot as a broken stack (#3827).
    """

    ok: bool
    message: str
    details: str = ""
    status: str = ""


# Prefix a step uses to mark an attempt that failed but that a later fallback
# in the *same* step already made moot -- see `_superseded_attempts`.
_SUPERSEDED_PREFIX = "Superseded"


def _result_status_label(r: OpsResult) -> str:
    """Return the stdout status label for `r`: "OK", "FAIL", "SKIP", "NOTE" or `r.status`.

    A skip is still `ok=True` (accounting/exit-code logic is unaffected) but
    reads misleadingly as a plain "OK" -- results whose message starts with
    "Skipped" (the existing convention for a deliberate no-op, e.g. "no
    docker found") print as SKIP instead (#3558).

    "NOTE" is the same idea for the other direction: an attempt that *failed*
    but that the step then recovered from is not a success and not a failure,
    and printing it as either misreports the step (#3762).

    An explicit `status` wins over all of it, including over `ok=False`: a
    caller that has already classified its own result ("PENDING" for a Pod
    still pulling its image) knows more about it than these heuristics do.
    """
    if r.status:
        return r.status.upper()
    if not r.ok:
        return "FAIL"
    if r.message.strip().lower().startswith("skip"):
        return "SKIP"
    if r.message.strip().startswith(_SUPERSEDED_PREFIX):
        return "NOTE"
    return "OK"


def _superseded_attempts(results: list[OpsResult]) -> list[OpsResult]:
    """Rewrite failed attempts in `results` that a later fallback already made moot.

    A step that tries one route, fails, and then succeeds by another route
    used to print `[FAIL] <first route>` followed by `[OK] <second route>`,
    which reads as a step contradicting itself -- the owner's 2026-08-14
    cloud acceptance run saw exactly that under `[4/23] docker engine`
    (#3762). The attempt genuinely failed, so it is not relabelled "OK";
    it becomes a `[NOTE]` whose details keep the original diagnostic, and
    the step's verdict is left to the result that actually settled it.

    Successful results pass through untouched, so callers can hand a whole
    sub-step's output here without filtering first.
    """
    settled: list[OpsResult] = []
    for r in results:
        if r.ok:
            settled.append(r)
            continue
        settled.append(
            OpsResult(
                True,
                f"{_SUPERSEDED_PREFIX}: {r.message} (recovered below -- not a failure of this step)",
                r.details,
            )
        )
    return settled


def _emit_results(action: str, results: list[OpsResult]) -> bool:
    """Print and structured-log each OpsResult from an ops step, returning overall success.

    Preserves the `[OK]`/`[FAIL]`/`[SKIP]` stdout lines every CLI entrypoint
    already printed (plus any label a result set for itself -- `[PENDING]`,
    see `_result_status_label`), and additionally logs one INFO/WARNING record per
    result (service/action/result plus any subprocess failure detail in
    `details`) so `nyxgpt ops` activity lands in the log files instead of
    only stdout.

    A *failing* result's details are inlined into the WARNING message itself,
    bounded by `_bounded_output` (#3783). They were previously carried only in
    the structured `extra`, so the step-failure line an operator actually reads
    ("ops: install failed: Failed to pip install nyxgpt-api") named the step
    and dropped the reason.
    """
    ok = True
    for r in results:
        print(f"[{_result_status_label(r)}] {r.message}")
        if r.details:
            print(f"  {r.details}")
        log = logger.info if r.ok else logger.warning
        detail_excerpt = "" if r.ok else _bounded_output(r.details)
        log(
            "ops: %s %s: %s",
            action,
            "ok" if r.ok else "failed",
            f"{r.message}\n{detail_excerpt}" if detail_excerpt else r.message,
            extra={
                "component": "ops",
                "action": action,
                "ok": r.ok,
                "result_message": r.message,
                "details": r.details,
            },
        )
        ok = ok and r.ok
    return ok


# --- Live step progress (#3558) ---
#
# `install()`/`down()`/`restart()`/`stop()`/`observability()`/`env_sync()`/
# `glitchtip_init()` used to build up a `results` list across every step and
# print it all at once via a single `_emit_results()` call at the end --
# meaning a ~52-step `nyxgpt ops install` run stayed completely silent until
# the very last step finished, with no way to tell a slow step from a hung
# one. `_run_steps()` below is the shared replacement: it announces each
# step before running it, prints that step's own outcome immediately after
# (so output streams live), and runs a background heartbeat while a step is
# still in flight. `--quiet` (per CLI entrypoint) suppresses the
# announcements/heartbeat/summary and falls back to the old terse
# OK/FAIL-only-per-result output, for scripting.

# Seconds between "still running" heartbeat lines for an in-flight step.
_STEP_HEARTBEAT_INTERVAL_S = 5.0

# Elapsed seconds at/over which a step is called out in the final "slow
# steps" summary.
_SLOW_STEP_THRESHOLD_S = 3.0

_STEP_FAILURE_HINT = (
    "Run `nyxgpt ops doctor` for diagnostics or `nyxgpt ops logs <service>` to inspect "
    "recent logs, then retry."
)


class _StepHeartbeat:
    """Background thread printing periodic elapsed-time lines for an in-flight step.

    Started right before a step's function runs and stopped right after, so
    an operator watching a long silent step (brew install, image pull,
    Cassandra readiness wait) can tell "still working" from "hung" (#3558).
    """

    def __init__(self, step_name: str) -> None:
        """Prepare (but don't yet start) a heartbeat for the step named `step_name`."""
        self._step_name = step_name
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        """Start the background heartbeat thread."""
        self._thread.start()

    def _run(self) -> None:
        """Print a "still running" line every `_STEP_HEARTBEAT_INTERVAL_S` until stopped."""
        elapsed = 0.0
        while not self._stop_event.wait(_STEP_HEARTBEAT_INTERVAL_S):
            elapsed += _STEP_HEARTBEAT_INTERVAL_S
            print(f"    ... still running ({elapsed:.0f}s): {self._step_name}")

    def stop(self) -> None:
        """Signal the heartbeat thread to stop and wait for it to exit."""
        self._stop_event.set()
        self._thread.join(timeout=_STEP_HEARTBEAT_INTERVAL_S)


def _run_steps(
    action: str,
    steps: list[tuple[str, Callable[[], list[OpsResult]]]],
    *,
    quiet: bool = False,
) -> tuple[list[OpsResult], list[tuple[str, float]]]:
    """Run `steps` in order, streaming live progress instead of buffering to the end (#3558).

    Before each step (unless `quiet`): prints "[n/m] step_name..." and
    starts a background heartbeat (`_StepHeartbeat`). After each step: prints
    its OK/FAIL/SKIP outcome via `_emit_results` right away, so a long
    `nyxgpt ops install` run shows progress as it happens. A step whose
    function raises is turned into a single FAIL result naming the step, the
    error, and a remediation hint -- exactly like a returned failure -- so
    one bad step doesn't abort the rest (matches the pre-existing
    best-effort contract of these step loops).

    Returns the combined results plus `(step_name, elapsed_seconds)` for
    every step at/over `_SLOW_STEP_THRESHOLD_S`, for the caller's final
    slow-step summary.
    """
    results: list[OpsResult] = []
    slow_steps: list[tuple[str, float]] = []
    total = len(steps)
    for i, (step_name, fn) in enumerate(steps, start=1):
        if not quiet:
            print(f"[{i}/{total}] {step_name}...")
        heartbeat = None if quiet else _StepHeartbeat(step_name)
        start_time = time.monotonic()
        if heartbeat is not None:
            heartbeat.start()
        try:
            step_results = fn()
        except Exception as e:
            logger.error(
                "ops: %s step %s raised %s: %s",
                action,
                step_name,
                type(e).__name__,
                e,
                extra={"component": "ops", "action": action, "step": step_name},
                exc_info=True,
            )
            # `str(CalledProcessError)` is only "Command '...' returned
            # non-zero exit status 1." -- the output that says *why* is on the
            # exception, so put it in the step's own failure detail rather
            # than making the reader go find the preceding `_run` warning
            # (#3783).
            cause = f"{type(e).__name__}: {e}"
            if isinstance(e, subprocess.CalledProcessError):
                excerpt = _combined_output_excerpt(e.stdout, e.stderr)
                if excerpt:
                    cause = f"{cause}\n{excerpt}"
            step_results = [
                OpsResult(
                    False,
                    f"ops {action} failed: {step_name}",
                    f"{cause}. {_STEP_FAILURE_HINT}",
                )
            ]
        finally:
            if heartbeat is not None:
                heartbeat.stop()
        elapsed = time.monotonic() - start_time
        if elapsed >= _SLOW_STEP_THRESHOLD_S:
            slow_steps.append((step_name, elapsed))
        _emit_results(action, step_results)
        _emit_step_verdict(action, step_name, i, total, step_results)
        results.extend(step_results)
    return results, slow_steps


def _emit_step_verdict(
    action: str,
    step_name: str,
    index: int,
    total: int,
    results: list[OpsResult],
) -> None:
    """Print one closing verdict for a step whose results were mixed (#3762).

    Each `OpsResult` is true on its own, but a step that emits a `[FAIL]`
    and then `[OK]` lines for its other checks has no single answer to "did
    step 4 pass?" -- the owner's 2026-08-14 cloud acceptance run read that
    output as self-contradictory. The step's last word is added here so the
    answer is always the last line about it.

    Only mixed steps get a verdict: an all-OK or all-FAIL step already reads
    coherently, and a failure the step recovered from is a `[NOTE]`
    (`_superseded_attempts`), not a failure, so it doesn't trigger one either.
    """
    failures = [r for r in results if not r.ok]
    if not failures or len(failures) == len(results):
        return
    summary = "; ".join(r.message for r in failures)
    print(
        f"[FAIL] step {index}/{total} {step_name!r} did not fully succeed: "
        f"{len(failures)} of {len(results)} checks failed ({summary})"
    )
    logger.warning(
        "ops: %s step %s did not fully succeed: %s",
        action,
        step_name,
        summary,
        extra={
            "component": "ops",
            "action": action,
            "step": step_name,
            "ok": False,
            "failed_checks": len(failures),
            "total_checks": len(results),
        },
    )


def _print_slow_steps_summary(slow_steps: list[tuple[str, float]]) -> None:
    """Print the final "slow steps" summary for a live-progress step run (#3558)."""
    if not slow_steps:
        return
    print(f"\nSlow steps (over {_SLOW_STEP_THRESHOLD_S:.0f}s):")
    for step_name, elapsed in slow_steps:
        print(f"  {step_name}: {elapsed:.1f}s")


def _ops_action_outcome(results: list[OpsResult]) -> tuple[str, str]:
    """Reduce a block of `OpsResult`s to a single (result, message) pair.

    `result` is "success" if every result ok'd, "refused" if any failing
    result's message starts with "Refusing" (the existing convention used by
    `_compose_conflict_result`/`_ensure_cassandra_container`'s port-collision
    refusals), else "failure".
    """
    if not results:
        return "success", ""
    failed = [r for r in results if not r.ok]
    if not failed:
        return "success", "; ".join(r.message for r in results)
    if any(r.message.startswith("Refusing") for r in failed):
        return "refused", "; ".join(r.message for r in failed)
    return "failure", "; ".join(r.message for r in failed)


def _record_ops_action(command: str, service: str, result: str, message: str = "") -> None:
    """Record one operator-initiated lifecycle action: a `nyxgpt_ops_actions_total`
    increment plus a structured log event.

    Kept entirely separate from self-heal's own `nyxgpt_selfheal_restarts_total`
    counter and heal-event log (see #3390 -- the self-heal counter answers "how
    often did the system recover itself"; folding operator-driven `nyxgpt ops`
    actions into it would make autonomous heals indistinguishable from manual
    restarts). This is the one place both CLI (`nyxgpt ops ...`) and the
    SRE/admin dashboard's equivalent API calls funnel through, so a metrics gap
    can be attributed to a deliberate `ops down` rather than reading as an
    unexplained outage.

    `correlation_id` (#3430) is `get_correlation_id()`'s resolution: the
    active HTTP request's `request_id` for dashboard-triggered actions, or
    `NYXGPT_CORRELATION_ID` from the environment for CLI-triggered ones (see
    `mint_correlation_id`) -- lets an operator join this event to the
    subprocess it drove, or the request that triggered it.
    """
    correlation_id = get_correlation_id()
    prom_metrics.OPS_ACTIONS_TOTAL.labels(command=command, service=service, result=result).inc()
    log = logger.info if result == "success" else logger.warning
    log(
        "ops: lifecycle action command=%s service=%s result=%s correlation_id=%s%s",
        command,
        service,
        result,
        correlation_id,
        f": {message}" if message else "",
        extra={
            "component": "ops",
            "event": "ops_lifecycle_action",
            "command": command,
            "service": service,
            "result": result,
            "result_message": message,
            "correlation_id": correlation_id,
        },
    )


def record_manual_restart(service: str, ok: bool, message: str = "") -> None:
    """Record a dashboard-triggered "Heal Now" restart as an ops lifecycle action.

    Called by the `/api/v1/self-heal/heal` endpoint for each component it
    restarts -- a manual heal-now click is an operator-initiated restart just
    like `nyxgpt ops restart <service>`, so it's recorded the same way (see
    #3390). This does not touch `nyxgpt_selfheal_restarts_total`, which
    self_heal.heal_now() already increments for this same restart -- that
    counter's accounting of "this component was restarted" is unaffected;
    this only adds the separate operator-action signal.
    """
    _record_ops_action("restart", service, "success" if ok else "failure", message)


def record_canary_action(
    action: str, result: str, message: str = "", *, component: str = "api"
) -> None:
    """Record a canary lifecycle action (deploy/start/promote/rollback/reset) per #3390.

    `canary.py` funnels every rollout action through here rather than calling
    `_record_ops_action` directly, keeping the "canary-<action>" command
    naming convention (mirroring "install"/"restart"/"down") in one place.
    `service` is `component` -- "api" by default (unchanged from before
    #3419), or "web" for the web canary pair (see canary.py's `COMPONENTS`).
    """
    _record_ops_action(f"canary-{action}", component, result, message)


# The Docker socket hop this process routes its Docker calls through when it
# cannot reach the socket directly -- the invoking session predates its
# `docker` group membership. Either "sg" (re-run the command under the docker
# group) or "sudo" (passwordless sudo), whichever is available *and* proven to
# carry this process's environment through unchanged. It is applied centrally
# in `_run` rather than at the ~40 `["docker", ...]` call sites in this module,
# so no Docker call path can forget it and re-introduce #3760's mid-deploy
# "permission denied ... /var/run/docker.sock".
#
# The mechanism lives in `docker_access.py`, shared with `self_heal.py`
# (#4022): the two modules cannot import each other's copy (ops imports
# self_heal), they each grew one, and the copies diverged on the answer they
# exist to give identically -- self-heal's retried a denied read through
# `sg docker` and reported "unknown", while `_docker_container_state` did
# neither and rendered a running Cassandra as `absent` on the dashboard.
#
# **Two entry points, two policies.** `_enable_docker_socket_hop` is the
# *eager* one: it is reached only from an interactive `nyxgpt ops install`,
# already a privileged operation, so it may escalate to `sudo`. Everything
# else here -- crucially `_docker_container_state`, which `/infra/status`
# polls from inside the public API process -- goes through `_DOCKER_HOP.run`,
# whose candidates are `sg` only, the same bar `self_heal` holds itself to.
# `sg` claims a group the invoking user already holds and so grants no
# authority the operator did not already give this account; `sudo` from a
# web-reachable process would.
_DOCKER_SOCKET_HOP_LABELS = docker_access.HOP_LABELS


def _hop_runner(
    cmd: list[str],
    *,
    timeout: float,
    expected: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """`_run` in the shape `docker_access` asks for (keyword-only, never raising).

    Looked up through the module globals rather than bound, so a test that
    stubs `ops._run` stubs the hop probes with it.
    """
    return _run(cmd, check=False, expected=expected, env=env, timeout=timeout)


_DOCKER_HOP = docker_access.DockerSocketHop(
    runner=_hop_runner,
    which=lambda name: _which(name),
    # The safe default, for every lazy retry (see the note above). The install
    # path passes its wider candidate list explicitly.
    candidates=("sg",),
    on_probe_error=lambda hop, e: logger.debug(
        "ops: %s docker probe failed: %s: %s", hop, type(e).__name__, e
    ),
)


def _docker_socket_hop() -> str | None:
    """The hop ops is currently routing its Docker calls through, if any."""
    return _DOCKER_HOP.active


def _docker_socket_hop_active() -> bool:
    """True while ops is reaching the Docker socket through a hop."""
    return _DOCKER_HOP.active is not None


def _wrap_docker_hop(cmd: list[str], hop: str | None) -> list[str]:
    """Rewrite `cmd` to run through `hop`, preserving the environment it sees.

    See `docker_access.hop_argv` for why both forms are environment-preserving
    by construction and what each one costs.
    """
    return docker_access.hop_argv(cmd, hop)


def _apply_docker_socket_hop(cmd: list[str]) -> list[str]:
    """Route a bare `docker ...` argv through the active hop.

    Only rewrites argv whose *first* element is `docker`, so an argv that is
    already privileged (`_privileged_run` puts `sudo` in front) is never
    double-wrapped, and no non-Docker command is touched.
    """
    return _DOCKER_HOP.apply(cmd)


# The backstop bound for an ops subprocess: half an hour is far longer than
# any real step (the slowest observed are `terraform apply` against AWS and a
# cold `npm ci`, both minutes), and far shorter than "forever", which is what
# every one of these calls was before #3858. It exists to stop a wedged
# process from holding a thread for the life of the API, not to police
# latency; callers on a polled endpoint pass `PROBE_TIMEOUT_SECONDS` instead,
# and a call that blocks by contract passes `timeout=None` deliberately.
DEFAULT_RUN_TIMEOUT_SECONDS = 1800.0


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    expected: bool = False,
    expected_returncodes: Container[int] | None = None,
    expected_message: str | None = None,
    input: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = DEFAULT_RUN_TIMEOUT_SECONDS,
    stream_stdout: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run `cmd`, capturing stdout/stderr as text.

    Pass `stream_stdout=True` to leave stdout attached to the terminal while
    still capturing stderr -- the discipline `cloud_infra.py`'s Terraform
    runner documents: long-running commands with meaningful progress output
    (`terraform apply` streaming per-resource "Still creating..." lines) are
    indistinguishable from a hang when fully captured, and Terraform puts its
    failure diagnostics on stderr, so a failure still carries its reason. In
    this mode `result.stdout` is None; every consumer of it in this module
    already tolerates that (`_cp_details`, `_log_nonzero_exit`).

    Raises `subprocess.CalledProcessError` on non-zero exit unless `check=False`.
    Non-zero exits are always logged with the command and a stderr tail first,
    so the evidence reaches Loki even when a caller catches the exception (or
    passes `check=False`) without logging it itself (#3415 gap 5).
    Pass `expected=True` for read-only probes where any non-zero exit is a
    normal outcome (e.g. "not found"/"not running") to log at DEBUG instead
    of WARNING.
    Pass `expected_returncodes={1, ...}` when only specific non-zero exit
    codes are a known success/no-op path (e.g. `createsuperuser --noinput`
    exiting 1 because the account already exists) -- a matching exit logs at
    INFO with `expected_message` (or a generic "expected exit" message)
    instead of WARNING, while any other non-zero exit still logs at WARNING
    exactly as today (#3574).
    A bare `docker ...` argv is routed through the active Docker socket hop
    (`_apply_docker_socket_hop`, see `_enable_docker_socket_hop`). Logging
    keeps the *unwrapped* argv: `_redact_cmd` masks secrets element by
    element, and `sg -c` collapses argv into one shell string it could not
    then see into.
    Pass `input` to feed the subprocess's stdin (e.g. a secret a caller
    doesn't want to put on `cmd`'s argv at all -- argv is visible to `ps`,
    shell history, and this function's own non-zero-exit logging, while
    stdin is not (#3644, CodeQL py/clear-text-logging-sensitive-data)).
    Pass `env` to run with a modified environment -- the other secret-safe
    channel: `docker compose exec -e VAR` (bare, no `=value`) forwards VAR
    from this process's environment into the container without the value
    ever appearing on argv (CodeQL #105/#106).
    Pass `timeout` to change the bound on how long this call may block, or
    `None` to remove it (#3858). The default is a wedged-process backstop, not
    a latency budget: `install`/`up` run `terraform apply`, `brew install` and
    `docker pull` through here and those legitimately take minutes. Anything
    reachable from a *polled* endpoint passes `PROBE_TIMEOUT_SECONDS` instead
    -- see `subprocess_bounds` for why. An expired bound is reported the same
    way any other failure is: `check=False` returns a `TIMEOUT_RETURNCODE`
    result (see `timed_out`), `check=True` raises `CalledProcessError` with
    that code, so no caller has to learn about `TimeoutExpired` and no handler
    can be hit by one.
    """
    try:
        # Streaming mode swaps capture_output for an explicit piped stderr
        # (stdout inherits the terminal, stderr still carries the failure
        # diagnostic). Built conditionally rather than passing stderr=None in
        # captured mode, so the default path's call signature is unchanged --
        # test_run_invokes_subprocess_with_expected_kwargs pins it.
        output_kwargs: dict[str, Any] = (
            {"capture_output": False, "stderr": subprocess.PIPE}
            if stream_stdout
            else {"capture_output": True}
        )
        result = subprocess.run(
            bounded_argv(_apply_docker_socket_hop(cmd), timeout),
            check=check,
            text=True,
            input=input,
            env=env,
            timeout=timeout,
            **output_kwargs,
        )
    except subprocess.CalledProcessError as e:
        _log_nonzero_exit(
            cmd, e.returncode, e.stdout, e.stderr, expected, expected_returncodes, expected_message
        )
        raise
    except subprocess.TimeoutExpired as exc:
        # Only a set bound can expire, but read the value back off the
        # exception rather than asserting it: `assert` is stripped under
        # `python -O`, and the narrowing has to hold in that build too.
        expired = timeout if timeout is not None else exc.timeout
        timed = timeout_result(cmd, exc, expired)
        logger.warning(
            f"Subprocess {timeout_message(expired)}: {' '.join(_redact_cmd(cmd))}",
            extra={
                "component": "ops",
                "cmd": _redact_cmd(cmd),
                "timeout_seconds": expired,
                "stdout_tail": (timed.stdout or "")[-2000:],
            },
        )
        if check:
            raise subprocess.CalledProcessError(
                timed.returncode, cmd, timed.stdout, timed.stderr
            ) from exc
        return timed
    if result.returncode != 0:
        _log_nonzero_exit(
            cmd,
            result.returncode,
            result.stdout,
            result.stderr,
            expected,
            expected_returncodes,
            expected_message,
        )
    return result


# --- Bounded subprocess output excerpts (#3783) ---
#
# A failed subprocess's *own* output is almost always the whole diagnosis --
# `pip`'s "requires a different Python: 3.9.x not in '>=3.11'" refusal during
# the rc9 cloud round said exactly what was wrong, and was thrown away because
# ops logged only the exit code and the argv. The owner had to SSH to the
# instance and re-run the command by hand to read it. So every failure path
# below carries the output with it -- but bounded, because an `npm ci` or a
# resolver backtrack can emit thousands of lines and unbounded log spam is its
# own failure of reporting. Head plus tail with an elision marker keeps both
# ends that matter: what the command was starting to do, and how it died.
_OUTPUT_EXCERPT_HEAD_LINES = 5
_OUTPUT_EXCERPT_TAIL_LINES = 20
_OUTPUT_EXCERPT_MAX_LINE_CHARS = 500

# How many *salient* lines to rescue out of the elided middle (#3861), and
# what makes a line salient.
#
# The head+tail bound above threw away the one thing worth keeping, twice, on
# the same defect. Homebrew's post-install failure path prints its diagnosis
# in the MIDDLE of stdout -- `FormulaInstaller#fix_dynamic_linkage` does
# `ofail "Failed to fix install linkage"` to stderr and then `puts e` (the
# actual exception) plus "The formula built, but ..." to stdout, before the
# caveats and the `==> Summary` line that the tail then keeps. So the excerpt
# in `~/.nyxGPT/logs/cli.log` for the 2026-08-21 acceptance failure ended at a
# bare "Error: Failed to fix install linkage" with the cause inside
# `... [42 lines omitted] ...`, and two diagnosis rounds were spent guessing
# at it. (The 2026-07-29 instance of the *same* failure, logged before this
# bounding existed, named its cause outright: `Error: Failed changing dylib ID
# of .../tiktoken/_tiktoken.cpython-312-darwin.so from @rpath/... to
# /opt/homebrew/opt/nyxgpt-api/...`.)
#
# A head+tail window is the wrong shape for a tool that reports progress at
# both ends and failures in between. The fix is not a bigger window -- an
# `npm ci` or a pip backtrack would blow any window -- it is to keep the lines
# that say something went wrong, wherever they are, and to stay bounded by
# capping how many of them are kept.
_OUTPUT_EXCERPT_SALIENT_LINES = 12
_OUTPUT_EXCERPT_SALIENT_RE = re.compile(
    r"^\s*(?:error|warning|fatal|failed|exception|traceback)\b[: ]",
    re.IGNORECASE,
)

# ...and the other half, which is the half that would actually have saved the
# 2026-08-21 round. Homebrew's `puts e` prints a *bare* exception message
# ("Updating load commands would exceed header padding") with no `Error:`
# prefix, so no pattern short of "keep everything" would rescue it by shape.
# What separates it from real log spam is length: measured across the owner's
# `cli.log.2`, every failing `brew install`/`reinstall` stdout was 63-67 lines
# (elisions of 38, 39, 41, 42), while the genuinely large ones -- `npm ci`, a
# pip resolver backtrack -- elided 375 and 475. A verbatim floor of 80 lines
# therefore keeps the whole of a failed brew install, including whatever it
# printed mid-stream, and still bounds the outputs the original bound was
# written for. It is the cheapest correct answer: the reason a head+tail
# window failed here is that the output was never big enough to need one.
_OUTPUT_EXCERPT_VERBATIM_MAX_LINES = 80


def _bounded_output(text: str | None) -> str:
    """Return `text` reduced to a bounded head+tail excerpt, keeping error lines.

    Empty/None input yields `""`. Short output is returned verbatim (stripped),
    so the common case reads exactly as it did before this bounding existed.
    Longer output keeps the first `_OUTPUT_EXCERPT_HEAD_LINES` and the last
    `_OUTPUT_EXCERPT_TAIL_LINES` lines with an explicit `... [N lines omitted]
    ...` marker between them -- never silently truncated, and never zero. Any
    single line over `_OUTPUT_EXCERPT_MAX_LINE_CHARS` is clipped too, so one
    pathological line (a minified stack trace, a base64 blob) can't blow the
    bound on its own.

    Output no longer than `_OUTPUT_EXCERPT_VERBATIM_MAX_LINES` is kept whole,
    and up to `_OUTPUT_EXCERPT_SALIENT_LINES` lines from the elided middle of
    anything longer that looks like an error/warning are carried out with the
    marker (the *last* such lines, which sit nearest the failure). The rescued
    lines are labelled as coming from the omitted region and are still shown
    in their original order, so nothing here can be misread as the end of the
    output.
    """
    if not text or not text.strip():
        return ""
    lines = [
        (
            line
            if len(line) <= _OUTPUT_EXCERPT_MAX_LINE_CHARS
            else line[:_OUTPUT_EXCERPT_MAX_LINE_CHARS] + " ... [line truncated]"
        )
        for line in text.strip().splitlines()
    ]
    head_and_tail = _OUTPUT_EXCERPT_HEAD_LINES + _OUTPUT_EXCERPT_TAIL_LINES
    if len(lines) <= max(head_and_tail, _OUTPUT_EXCERPT_VERBATIM_MAX_LINES):
        return "\n".join(lines)
    omitted = len(lines) - head_and_tail
    middle = lines[_OUTPUT_EXCERPT_HEAD_LINES : len(lines) - _OUTPUT_EXCERPT_TAIL_LINES]
    salient = [line for line in middle if _OUTPUT_EXCERPT_SALIENT_RE.match(line)]
    salient = salient[-_OUTPUT_EXCERPT_SALIENT_LINES:] if salient else []
    marker = f"... [{omitted} lines omitted] ..."
    if salient:
        marker = (
            f"... [{omitted} lines omitted; the {len(salient)} error/warning "
            "line(s) among them follow] ..."
        )
    return "\n".join(
        [
            *lines[:_OUTPUT_EXCERPT_HEAD_LINES],
            marker,
            *salient,
            *lines[-_OUTPUT_EXCERPT_TAIL_LINES:],
        ]
    )


def _combined_output_excerpt(stdout: str | None, stderr: str | None) -> str:
    """Bounded stdout+stderr of a finished subprocess, in that order.

    Each stream is bounded *separately* rather than concatenated first: `pip`
    and `npm` write volumes of progress to stdout and the one-line reason for
    the failure to stderr, so a tail taken over the concatenation could drop
    the only line worth reading.
    """
    parts = [_bounded_output(stdout), _bounded_output(stderr)]
    return "\n".join(p for p in parts if p)


def _output_excerpt(cp: subprocess.CompletedProcess[str]) -> str:
    """Bounded combined output of `cp`, for an `OpsResult`'s failure details."""
    return _combined_output_excerpt(cp.stdout, cp.stderr)


# Argument names (and inline `--flag=value` forms) whose *value* may carry a
# secret. When we log a failed command we mask those values so an API key,
# password, or DSN can never reach Loki in clear text (CodeQL
# py/clear-text-logging-sensitive-data). Matching is case-insensitive and
# substring-based so `--api-key`, `--admin-password`, and `--glitchtip-dsn`
# are all covered.
_SECRET_ARG_HINTS = ("key", "token", "secret", "password", "passwd", "dsn", "credential")


def _redact_cmd(cmd: list[str]) -> list[str]:
    """Return a copy of `cmd` with any secret-bearing argument value masked.

    Two shapes are redacted: a value in the position *after* a secret-named
    flag (`--api-key VALUE` -> `--api-key ***`), and an inline `NAME=value`
    where the name looks sensitive -- with or without a leading dash, so
    both `--dsn=...` and an env-style `DJANGO_SUPERUSER_PASSWORD=...`
    (docker `-e NAME=value` forwarding) are masked. The returned list is
    freshly built so the sensitive value never flows on to the logging sink.
    """
    redacted: list[str] = []
    mask_next = False
    for arg in cmd:
        if mask_next:
            redacted.append("***")
            mask_next = False
            continue
        low = arg.lower()
        if "=" in arg:
            name, _, _value = arg.partition("=")
            if any(h in name.lower() for h in _SECRET_ARG_HINTS):
                redacted.append(f"{name}=***")
                continue
        if arg.startswith("-") and any(h in low for h in _SECRET_ARG_HINTS):
            redacted.append(arg)
            mask_next = True
            continue
        redacted.append(arg)
    return redacted


def _log_nonzero_exit(
    cmd: list[str],
    returncode: int,
    stdout: str | None,
    stderr: str | None,
    expected: bool,
    expected_returncodes: Container[int] | None,
    expected_message: str | None,
) -> None:
    """Log a subprocess non-zero exit at the appropriate level (helper for `_run`).

    A returncode declared via `expected_returncodes` logs at INFO -- it is
    useful information, not a warning sign -- with wording that explicitly
    says the exit was expected, so it never reads as scary to the user
    (#3574). Everything else keeps the pre-existing WARNING/DEBUG split.

    An unexpected exit carries a bounded excerpt of the subprocess's own
    combined output *in the log message*, not only in the structured `extra`
    (#3783): the message is what reaches a terminal, a `journalctl` tail and
    `nyxgpt ops logs`, and it was the only thing the owner could see when an
    rc9 install died on a `pip` refusal whose text ops had thrown away.
    """
    safe_cmd = _redact_cmd(cmd)
    safe_cmd_str = " ".join(safe_cmd)
    output = _combined_output_excerpt(stdout, stderr)
    if expected_returncodes is not None and returncode in expected_returncodes:
        level = logging.INFO
        message = expected_message or (
            f"Subprocess exited with expected rc={returncode}, treated as "
            f"success: {safe_cmd_str}"
        )
    else:
        level = logging.DEBUG if expected else logging.WARNING
        message = f"Subprocess exited non-zero (rc={returncode}): {safe_cmd_str}"
        if output:
            message += f"\n--- subprocess output ---\n{output}\n--- end subprocess output ---"
    # CodeQL alerts #105/#106 (py/clear-text-logging-sensitive-data) flagged
    # `message`/`extra` below, and were REAL twice over: a bare positional
    # secret (`_reset_grafana_admin_password`, fixed in #3644 by piping it
    # over stdin), and an env-style `-e DJANGO_SUPERUSER_PASSWORD=value`
    # argv element (`_glitchtip_ensure_superuser`) that slipped past
    # `_redact_cmd`'s dash-prefixed flag masking and reached this log's
    # `extra` at INFO on every idempotent glitchtip-init re-run. That call
    # site now forwards the secret via the process environment (bare `-e
    # VAR` + `_run(env=...)`) so it never appears on argv, and `_redact_cmd`
    # also masks dash-less `NAME=value` elements as defense-in-depth.
    # Keep secrets off argv entirely (stdin or env) at any new call site --
    # that kills the flow at the source instead of trying to convince
    # CodeQL's taint tracker that `_redact_cmd` is a sanitizer. Inline
    # suppression comments (`codeql[...]`, then `lgtm[...]`) were tried in
    # earlier rounds and neither took effect against this repo's CodeQL
    # default-setup configuration -- don't reintroduce them.
    # `test_run_redacts_secret_cmd_values_on_nonzero_exit` in
    # `tests/unit/test_ops.py` covers the masking that remains as
    # defense-in-depth for any other secret-bearing flag.
    logger.log(
        level,
        message,
        extra={
            "component": "ops",
            "cmd": safe_cmd,
            "returncode": returncode,
            "stderr_tail": stderr[-2000:] if stderr else "",
            "output_excerpt": output,
        },
    )


def _which(prog: str) -> str | None:
    """Return the absolute path to `prog` on PATH, or None if it isn't found."""
    return shutil.which(prog)


def _running_as_root() -> bool:
    """True if this process already has root privileges (no sudo needed)."""
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _privileged_run(
    cmd: list[str], *, expected: bool = False
) -> subprocess.CompletedProcess[str] | None:
    """Run `cmd` with root privileges, never prompting for a password.

    A handful of install steps genuinely need root on Linux and cannot be
    expressed any other way: stopping/disabling the *system-scope*
    `ollama.service` the official Ollama installer enables so nyxGPT's own
    `nyxgpt-ollama.service` can hold port 11434 (#3632), installing the
    Docker engine/Compose plugin from the distro's package manager, and
    chowning a Docker-created root-owned bind-mount directory to the uid its
    container runs as. All of them are reconciliation `nyxgpt ops install`
    is expected to perform on its own -- an operator who has to hand-run
    three `sudo` commands before `install` can finish is exactly the
    friction #3632 was filed about.

    `sudo -n` is the safety property that makes this acceptable: it *never*
    prompts. On a host with passwordless sudo (the default on Ubuntu cloud
    images and GitHub Actions runners) the command runs; anywhere else it
    fails instantly and the caller reports the exact command for the
    operator to run themselves, rather than hanging on a TTY prompt inside a
    non-interactive install. Returns None when root is unreachable at all
    (no `sudo` on PATH and not already root) so callers can distinguish
    "couldn't even try" from "tried and was refused".
    """
    if _running_as_root():
        return _run(cmd, check=False, expected=expected)
    if _which("sudo") is None:
        return None
    return _run(["sudo", "-n", *cmd], check=False, expected=expected)


def _read_project_version() -> str:
    """Return the project version from pyproject.toml, or "0.0.0" if unreadable.

    The "0.0.0" fallback is deliberately implausible: a brew formula stamped
    with it signals "version could not be determined" instead of silently
    masquerading as a real release.
    """
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return "0.0.0"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(data.get("project", {}).get("version", "0.0.0"))


def project_version() -> str:
    """Public wrapper around `_read_project_version()`, for cross-module version stamping.

    Used by `canary.py` to stamp the versioned image tag `nyxgpt ops deploy
    --kubernetes` builds (see #3409) -- `_read_project_version` stays private
    since every other caller is internal to this module.
    """
    return _read_project_version()


def _ensure_dir(p: Path) -> None:
    """Create directory `p` (and any missing parents) if it doesn't already exist."""
    p.mkdir(parents=True, exist_ok=True)


def _copy_file(src: Path, dst: Path, *, mode: int | None = None) -> None:
    """Copy `src` to `dst`, creating `dst`'s parent directory and optionally chmod'ing it.

    Args:
        src: Source file path.
        dst: Destination file path.
        mode: If given, permission bits applied to `dst` after copying (e.g. 0o755).
    """
    _ensure_dir(dst.parent)
    shutil.copy2(src, dst)
    if mode is not None:
        os.chmod(dst, mode)


def _packaged_resources_root() -> Path:
    """Return the filesystem path backing the `nyxgpt.resources` package data.

    Resolves via `importlib.resources`, so this works identically whether
    nyxGPT is running from an editable dev checkout (`src/nyxgpt/resources/`
    holds symlinks back to the canonical `docker/`, `docker-compose.yml`,
    `ops/`, `.env.example`, `scripts/*.sh`) or an installed, non-editable
    wheel (real copies of the same files, bundled at build time -- see
    pyproject.toml's `[tool.setuptools.package-data]`).
    """
    return Path(str(importlib.resources.files("nyxgpt.resources")))


def _sync_packaged_resources() -> list[OpsResult]:
    """Copy the packaged Compose/config/provisioning/unit-template/script
    tree into `NYXGPT_HOME` so every other ops step reads from one fixed,
    writable location regardless of whether nyxGPT is running from a source
    checkout or an installed package (#3621) -- see `_packaged_resources_root`.

    Idempotent and safe to re-run: overwrites the synced copies (so a
    `nyxgpt ops install` after an upgrade always ships the current
    templates) without touching anything else already under `NYXGPT_HOME`,
    notably `docker/config.docker.ini` (a separately generated, git-ignored
    artifact -- see `_generate_compose_config` -- never part of the packaged
    resources), `k8s/secret.yaml` (generated by `_ensure_k8s_secret`, carries
    the API key, likewise never packaged) and `config.ini`.

    `k8s` joined the list in #3834: the Kubernetes manifests were the last
    runtime data still resolved under `REPO_ROOT`, so `--kubernetes` could
    only ever run from a checkout.
    """
    src_root = _packaged_resources_root()
    try:
        _copy_file(src_root / "docker-compose.yml", OPS_COMPOSE_FILE)
        _copy_file(src_root / ".env.example", NYXGPT_HOME / ".env.example")
        for subdir in ("docker", "ops", "scripts", "k8s"):
            shutil.copytree(src_root / subdir, NYXGPT_HOME / subdir, dirs_exist_ok=True)
        for script in OPS_SCRIPTS_SRC_DIR.glob("*.sh"):
            os.chmod(script, 0o755)
    except OSError as e:
        return [
            OpsResult(
                False,
                "Failed to sync packaged ops resources",
                f"{type(e).__name__}: {e}",
            )
        ]
    return [OpsResult(True, f"Synced packaged ops resources to {NYXGPT_HOME}")]


def _tap_repo(tap: str) -> Path:
    """Return the local checkout path of Homebrew tap `tap` (`brew --repo <tap>`)."""
    cp = _run(["brew", "--repo", tap])
    return Path((cp.stdout or "").strip())


# --- Deployment mode detection ---


def _brew_services_snapshot() -> dict[str, str]:
    """Return {brew_service_name: state} parsed from `brew services list`.

    Keyed by the literal formula name Homebrew prints, which on a candidate
    install is `nyxgpt-api@<line>rc` -- resolve a component to its entry with
    `brew_services.resolve`, never by indexing `NATIVE_BREW_SERVICES` (#3853).
    """
    if _which("brew") is None:
        return {}
    cp = _run(
        ["brew", "services", "list"],
        check=False,
        expected=True,
        timeout=LOCAL_PROBE_TIMEOUT_SECONDS,
    )
    return brew_services.parse_services_list(cp.stdout or "")


def _resolved_brew_service(component: str, snapshot: Mapping[str, str] | None = None) -> str:
    """The brew service name this machine actually registers for `component`.

    One call site's worth of convenience over `brew_services.resolve`: takes
    the `brew services list` snapshot when the caller already has one, and
    reads a fresh one when it does not.
    """
    if snapshot is None:
        snapshot = _brew_services_snapshot()
    return brew_services.resolve(component, NATIVE_BREW_SERVICES[component], snapshot)


def _brew_service_registration(name: str) -> tuple[str, Path | None]:
    """Return `(state, plist)` for brew service `name` from `brew services list`.

    `state` is the Status column (`started`, `error`, `none`, ...), or
    `"none"` when brew does not list the formula at all. `plist` is the File
    column -- and that file *is* the registration: a plist sitting in
    ~/Library/LaunchAgents is what launchd starts again at the next login,
    which is why "did the stop take?" is a question about this path and not
    about an exit code (#3861).

    The File column is read rather than derived from the formula name on
    purpose: brew has used more than one label scheme, and the column is
    what this machine's brew actually wrote. `_brew_services_snapshot` stays
    the cheap name->state map its many callers want; this is the one caller
    that needs the file too.

    This is the one place outside `brew_services.parse_services_list` that
    reads brew's rows directly, so it strips escapes the same way: brew
    colours the Status column and the escapes survive a pipe (#3861). The
    path is matched as "the rest of the line from its leading `/` or `~`"
    rather than taken as the last whitespace-separated field, so a home
    directory containing a space still yields the whole plist path.
    """
    if _which("brew") is None:
        return "none", None
    cp = _run(
        ["brew", "services", "list"],
        check=False,
        expected=True,
        timeout=LOCAL_PROBE_TIMEOUT_SECONDS,
    )
    for line in brew_services.strip_ansi(cp.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == name:
            path_match = re.search(r"([/~].*\.plist)\s*$", line)
            plist = Path(path_match.group(1)).expanduser() if path_match else None
            return parts[1], plist
    return "none", None


def _brew_service_will_restart(name: str, plist: Path | None = None) -> bool:
    """Whether anything will start brew service `name` again.

    The question belongs to launchd, not to brew: a plist in
    ~/Library/LaunchAgents is what gets loaded at the next login, and a loaded
    job is what is holding a port right now. Either one means the service is
    still registered; neither means it is not, whatever brew says about it.

    `brew services list`'s Status column deliberately does **not** decide it.
    On two real runners a column-based read reported the service registered
    while launchd said it was gone (`macos-brew-smoke.yml` runs 32222041921
    and 32228088507 -- in the latter, the escalation below found no plist to
    remove and no loaded job, and the read still reported it). Run 32233162053
    then captured brew's rows verbatim and identified the mechanism: every
    state token is ANSI-wrapped (`ESC[39mnoneESC[0m`), so an unstripped state
    matches no literal, and `!= "none"` reads a de-registered service as
    registered. It is *not* that the column goes stale -- the same capture
    shows the just-retired service reading `none` with an empty File field
    within the second. The escapes are now stripped at the parser
    (`brew_services.strip_ansi`).

    The column still does not decide registration here, for a reason that
    outlives that bug: a state word answers "is it running", and `error`,
    `stopped` and `scheduled` are all *registered*. Reading it as
    de-registration is the mirror image of reading `brew services stop`'s exit
    code as "de-registered" -- both consult a signal that is about something
    else -- and it cost a *false* failure (a successful retire reported as
    one) where the exit code cost a false success.

    `plist` may be passed when the caller already has brew's File column, to
    honour a label scheme other than `homebrew.mxcl.<name>` without paying a
    second `brew services list`.
    """
    for candidate in (plist, _launchagents_dir() / f"homebrew.mxcl.{name}.plist"):
        if candidate is not None and candidate.exists():
            return True
    if not _is_macos() or _which("launchctl") is None:
        # `brew services` drives systemd --user on Linux, where there is no
        # plist and no gui domain to ask; the systemd path answers there.
        return False
    label = plist.stem if plist is not None else f"homebrew.mxcl.{name}"
    try:
        cp = _run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            check=False,
            expected=True,
            timeout=LOCAL_PROBE_TIMEOUT_SECONDS,
        )
    except Exception:
        # A probe that cannot run has not found a registration. Reporting one
        # here would fail a retire that succeeded.
        return False
    return cp.returncode == 0


def _brew_service_is_registered(name: str) -> bool:
    """Whether brew service `name` will still be started by launchd.

    The plist at brew's conventional path settles it without asking brew
    anything -- a file that exists is a registration, and this is the cheap
    check. Only when it is absent is brew asked which path it actually chose,
    since the label scheme is brew's to change; see
    `_brew_service_will_restart` for why the Status column is not the signal
    in either case.
    """
    if (_launchagents_dir() / f"homebrew.mxcl.{name}.plist").exists():
        return True
    _state, plist = _brew_service_registration(name)
    return _brew_service_will_restart(name, plist)


# The state string a container read carries when the read could not be made
# at all -- as distinct from `absent`, which asserts Docker answered and there
# is no such container (#4022). Every operator-facing surface that consumes a
# container state has to keep the two apart: on the owner's EC2 instance the
# API process's `systemd --user` session predated its `docker` group, so every
# read was denied, and rendering that denial as `absent` told the dashboard
# Cassandra was gone while it was running.
DOCKER_STATE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContainerProbe:
    """One container read: what it says, and whether it is an answer at all.

    `known=False` means the read did not happen -- `state` is then
    `DOCKER_STATE_UNKNOWN` and `reason` carries Docker's own words for why, so
    the Infrastructure card and `ops status` can print the cause instead of
    sending the operator to a log for it (the pattern `self_heal.ComposeProbe`
    already set for the Compose survey, #3812).
    """

    state: str
    known: bool = True
    reason: str = ""


def _container_deployed(state: str) -> bool:
    """True if `state` is a *determined* reading that something exists.

    `absent` is a determined negative and `unknown` is not a reading at all,
    so neither counts as deployed. Used wherever a stack's existence is
    inferred from its containers -- an unknown read must not promote a denied
    session into "Terraform is deployed here" any more than into "Cassandra is
    absent".
    """
    return state not in ("absent", DOCKER_STATE_UNKNOWN)


def _docker_container_probe(name: str) -> ContainerProbe:
    """Read one container's docker state, honestly (#4022).

    `docker ps -a --filter name=^<name>$` has no "no such container" failure
    mode: a container that is not there is exit 0 with empty output. So *any*
    non-zero exit here means the read did not happen -- a denied socket, an
    unreachable daemon, an expired bound -- and the only honest rendering of
    that is `unknown` with the reason, never `absent`. The pre-#4022 code
    returned `absent` for all of them, which is the whole of this defect.

    The call goes through `_DOCKER_HOP.run`, so a session whose group set
    predates its `docker` membership -- the `systemd --user` case this fault
    keeps arriving as -- is retried through `sg docker` and usually answers
    for real rather than degrading at all. Only when no hop is available does
    the unknown reading survive to the surface.
    """
    if _which("docker") is None:
        # A determined negative: with no Docker CLI there is no container.
        return ContainerProbe("absent")
    cp = _DOCKER_HOP.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}"],
        expected=True,
        # Polled by `/infra/status` on every dashboard refresh, and the one
        # call here that can dial a daemon that is not answering (a socket
        # hop to a host that stopped responding). Bounded tightly (#3858).
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if cp.returncode != 0:
        return ContainerProbe(
            DOCKER_STATE_UNKNOWN,
            known=False,
            reason=_docker_probe_reason(cp),
        )
    out = (cp.stdout or "").strip()
    return ContainerProbe(out.splitlines()[0].strip() if out else "absent")


def _docker_probe_reason(cp: subprocess.CompletedProcess[str]) -> str:
    """A one-line operator-facing cause for a failed container read.

    Docker's own stderr, trimmed -- the same shape `self_heal.compose_probe`
    puts on the Self-Heal panel, so an operator reading either surface sees
    the same sentence rather than having to correlate two vocabularies.
    """
    detail = (cp.stderr or cp.stdout or "").strip().splitlines()
    first = detail[0].strip() if detail else "no output"
    if len(first) > 300:
        first = first[:297] + "..."
    return f"`docker ps` exited {cp.returncode}: {first}"


def _docker_container_state(name: str) -> str:
    """The docker state ('running', 'exited', ...) for a container, 'absent', or 'unknown'.

    The string-only view of `_docker_container_probe`, for the callers that
    only branch on it. Anything rendering the answer to an operator should
    use the probe instead, so it can print *why* a read is unknown.
    """
    return _docker_container_probe(name).state


def _compose_stack_snapshot() -> dict[str, str]:
    """Return {service: state} for the docker-compose.yml stack, if any is running.

    Reuses self_heal.list_component_status(), which already knows how to resolve
    and query the project's docker-compose.yml via `docker compose ps -a`. That
    call returns a combined view -- Compose services plus native components plus
    absent-desired entries -- so this filters to `source == "compose"` only;
    otherwise a native component (e.g. the native `nyxgpt-cassandra` container)
    would fold into this "compose" snapshot and collide with the native reading
    of the very same container in `detect_deployment_mode()`, producing a false
    phantom-backend conflict.
    """
    try:
        statuses = self_heal.list_component_status()
    except Exception as e:
        logger.warning(
            "list_component_status() failed, treating compose stack as empty: %s",
            e,
            extra={"component": "ops"},
        )
        return {}
    # `known=False` rows are excluded (#3812): this map's values are docker
    # states, compared against "running"/"absent" by its callers, and an
    # undetermined component has no docker state to put here. Reporting one
    # would push a guess into `detect_deployment_mode()`'s conflict checks;
    # the "can't determine" signal belongs to `compose_probe`, which
    # `infra_status` reports alongside this map.
    return {s.service: s.state for s in statuses if s.source == "compose" and s.known}


def _terraform_or_kubernetes_managed_components() -> set[str]:
    """Service names `list_component_status()` currently reports as Terraform/Kubernetes-managed.

    `ops down`/`stop` (no `--terraform`/`--kubernetes` flag) mark api/web/
    ollama/cassandra intentionally stopped by service name, but only ever
    stop the native/Compose form of those services. If a Terraform/
    Kubernetes deployment is what's actually running, marking would flag it
    `desired=False` in `list_component_status()` even though neither this
    call nor its `_stop_*` helpers touched it -- silently blinding self-heal
    to a component that never went down. Returns an empty set (never
    raises) if the status lookup fails, so a lookup error never blocks the
    marking it would otherwise guard.
    """
    try:
        statuses = self_heal.list_component_status()
    except Exception as e:
        logger.warning(
            "list_component_status() failed, treating no components as "
            "Terraform/Kubernetes-managed: %s",
            e,
            extra={"component": "ops"},
        )
        return set()
    return {s.service for s in statuses if s.source in ("terraform", "kubernetes")}


def detect_deployment_mode() -> DeploymentMode:
    """Detect which deployment(s) are actually running: native vs. Docker Compose.

    `ops.py` otherwise assumes native-only (Homebrew services + one ops-managed
    Cassandra container). This cross-checks the Compose stack so `status`/`restart`
    can see -- and avoid colliding with -- a Compose deployment left running.

    Cassandra's entry can be `DOCKER_STATE_UNKNOWN` (#4022): it is the one
    native component read out of Docker, and a session that may not talk to
    the daemon has no answer about it. `docker_probe_reason` carries the cause
    so `status`/`infra_status` can say so. The conflict checks below are
    unaffected by construction -- they ask whether a component is *running*,
    and an unknown reading is not one.
    """
    native = _native_services_snapshot()
    cassandra = _docker_container_probe(CASSANDRA_CONTAINER_NAME)
    native["cassandra"] = cassandra.state

    compose = _compose_stack_snapshot()
    terraform = terraform_stack_state()
    docker_probe_reason = cassandra.reason

    conflicts = [
        component
        for component in COMPOSE_COMPONENT_PORTS
        if native.get(component) in ("started", "running") and compose.get(component) == "running"
    ]

    terraform_conflicts = [
        component
        for component in TERRAFORM_CONTAINERS
        if terraform.get(component) == "running"
        and (native.get(component) in ("started", "running") or compose.get(component) == "running")
    ]

    logger.debug(
        "ops: detected deployment mode (native=%s, compose=%s, terraform=%s, conflicts=%s, "
        "terraform_conflicts=%s)",
        native,
        compose,
        terraform,
        conflicts,
        terraform_conflicts,
        extra={
            "component": "ops",
            "action": "detect_deployment_mode",
            "native": native,
            "compose": compose,
            "terraform": terraform,
            "conflicts": conflicts,
            "terraform_conflicts": terraform_conflicts,
        },
    )
    if conflicts:
        logger.warning(
            "ops: native/Compose deployment conflict on %s -- both report running on the "
            "shared port",
            ", ".join(sorted(conflicts)),
            extra={"component": "ops", "action": "detect_deployment_mode", "conflicts": conflicts},
        )
    if terraform_conflicts:
        logger.warning(
            "ops: native/Compose and Terraform deployment stacks both running on %s -- an "
            "incomplete mode switch left two core stacks up",
            ", ".join(sorted(terraform_conflicts)),
            extra={
                "component": "ops",
                "action": "detect_deployment_mode",
                "terraform_conflicts": terraform_conflicts,
            },
        )

    return DeploymentMode(
        native=native,
        compose=compose,
        conflicts=conflicts,
        terraform=terraform,
        terraform_conflicts=terraform_conflicts,
        docker_probe_reason=docker_probe_reason,
    )


def compose_core_components(mode: DeploymentMode) -> list[str]:
    """The *core* app components (api/web/ollama/cassandra) this Compose snapshot manages.

    `mode.compose` is every Compose-sourced service the survey saw --
    observability containers included, and on a native install those are the
    only Compose entries there are (`install()` starts the observability
    stack unless `--skip-observability` is passed, so the correctly
    configured native install always has ten of them running). Asking
    "is this deployment Compose-managed?" by testing that dict for
    truthiness therefore answers yes on every native install: the defect
    #3855 was filed for, where the Infrastructure page labelled a Homebrew
    stack "Docker Compose" while the Self-Heal page -- which filters to core
    services first -- correctly called the same host native.

    This exists so callers ask the core question by name instead of
    rediscovering the filter, and so it is scoped by `CORE_APP_SERVICES`,
    the same constant `self_heal.detected_mode()` uses, rather than by a
    third list of core component names.

    Presence, not `state == "running"`, matching `detected_mode`: a core
    component only reaches this snapshot at all if Compose won
    `self_heal._resolve_core_component_conflicts` for it, and any *running*
    native or Terraform entry beats a non-running Compose one there. So a
    core name appearing here already is the authoritative reading of that
    component, whatever state it is in.
    """
    return sorted(set(mode.compose) & CORE_APP_SERVICES)


# --- Restart helpers ---


def _brew_cellar() -> Path | None:
    """Homebrew's Cellar directory, or None when it cannot be located.

    Delegates to `brew_services` (D-022): `self_heal.py` names formulas too and
    `ops.py` imports it, so the answer to "which tap owns this keg" has to sit
    below both rather than be copied into each.
    """
    return brew_services.cellar(_which)


def _brew_formula_spec(name: str) -> str:
    """`<tap>/<name>` for an installed keg, else `name` unchanged (#3861).

    Thin delegation, kept as a name because seven call sites use it. The
    reasoning lives with the implementation in `brew_services.formula_spec`.
    """
    return brew_services.formula_spec(name, _which)


def _restart_brew_service(name: str) -> list[OpsResult]:
    """Restart Homebrew service `name` via `brew services restart`.

    Returns a single-element list: an OpsResult reporting brew missing, the
    restart command's success, or its failure with captured stdout/stderr.
    """
    if _which("brew") is None:
        return [OpsResult(False, f"brew not found; cannot restart {name}")]
    try:
        cp = _run(["brew", "services", "restart", _brew_formula_spec(name)], check=False)
        if cp.returncode == 0:
            return [OpsResult(True, f"Restarted brew service: {name}")]
        details = _output_excerpt(cp)
        return [OpsResult(False, f"Failed to restart brew service: {name}", details.strip())]
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to restart brew service: {name}",
                f"{type(e).__name__}: {e}",
            )
        ]


def _restart_docker_container(name: str) -> list[OpsResult]:
    """Restart Docker container `name` via `docker restart`, with a start-back-up recovery attempt.

    If the container was running before the restart and `docker restart`
    leaves it stopped (e.g. a port collision on the start half), this makes
    one `docker start` attempt to bring it back up rather than silently
    leaving a previously healthy container down.

    Returns a single-element list of OpsResult describing success, a
    recovered restart failure, or an unrecovered "DOWN" failure.
    """
    if _which("docker") is None:
        return [OpsResult(False, f"docker not found; cannot restart {name}")]

    was_running = _docker_container_state(name) == "running"

    try:
        cp = _run(["docker", "restart", name], check=False)
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to restart docker container: {name}",
                f"{type(e).__name__}: {e}",
            )
        ]

    if cp.returncode == 0:
        return [OpsResult(True, f"Restarted docker container: {name}")]

    details = _output_excerpt(cp)

    if was_running and _docker_container_state(name) != "running":
        # `docker restart` stops then starts the container -- if the start half fails
        # (e.g. a port collision) the container is left stopped even though it was
        # healthy before. Try once to bring it back up rather than silently leaving a
        # previously-running container down with only a generic failure message.
        recovery = _run(["docker", "start", name], check=False)
        if recovery.returncode == 0 and _docker_container_state(name) == "running":
            return [
                OpsResult(
                    False,
                    f"Restart of {name} failed but the previously running container was recovered",
                    details.strip(),
                )
            ]
        return [
            OpsResult(
                False,
                f"DOWN: {name} failed to restart and is now STOPPED (recovery attempt also failed)",
                details.strip(),
            )
        ]

    return [OpsResult(False, f"Failed to restart docker container: {name}", details.strip())]


def _restart_launchagent(label: str) -> list[OpsResult]:
    """Restart the LaunchAgent `label` via `launchctl kickstart -k` in the current GUI domain.

    Returns a single-element list of OpsResult reporting success or failure
    (including any exception raised while invoking launchctl).
    """
    # Prefer a kickstart (restart) in the current GUI domain.
    domain = f"gui/{os.getuid()}/{label}"
    try:
        cp = _run(["launchctl", "kickstart", "-k", domain], check=False)
        if cp.returncode == 0:
            return [OpsResult(True, f"Restarted LaunchAgent: {label}")]
        details = _output_excerpt(cp)
        return [OpsResult(False, f"Failed to restart LaunchAgent: {label}", details.strip())]
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to restart LaunchAgent: {label}",
                f"{type(e).__name__}: {e}",
            )
        ]


def _find_launchagent_template(
    name: str = "com.nyxgpt.cassandra-logs.plist",
) -> tuple[Path | None, list[Path]]:
    """
    Locate a log-follower LaunchAgent template (by plist filename) among the
    packaged resources `_sync_packaged_resources` synced to
    `OPS_LAUNCHAGENTS_DIR`. Returns (path_or_none, candidates_checked).
    """
    candidates = [OPS_LAUNCHAGENTS_DIR / name]
    for p in candidates:
        try:
            if p.exists():
                return p, candidates
        except Exception as e:
            # If something odd happens (permissions, broken symlink), keep searching.
            logger.warning(
                "Could not check candidate path %s, skipping: %s",
                p,
                e,
                extra={"component": "ops"},
            )
            continue
    return None, candidates


def _install_launchagent_from_template(tpl: Path, dst: Path) -> None:
    """Render a LaunchAgent plist template to `dst`, substituting
    `LAUNCHAGENT_HOME_PLACEHOLDER` with the installing user's actual home
    directory so the installed plist works for whichever account runs
    `nyxgpt ops install`, not just the template's original author.
    """
    _ensure_dir(dst.parent)
    text = tpl.read_text(encoding="utf-8")
    text = text.replace(LAUNCHAGENT_HOME_PLACEHOLDER, str(Path.home()))
    dst.write_text(text, encoding="utf-8")


def _install_cassandra_launchagent() -> list[OpsResult]:
    """Install and (re)load the Cassandra log-follower LaunchAgent.

    Locates the synced plist template (see `_find_launchagent_template`), copies it into
    ~/Library/LaunchAgents, then boots it out and back in via `launchctl
    bootout`/`bootstrap`/`kickstart` so a stale prior load doesn't linger.
    Returns a single-element list of OpsResult; fails if the template can't
    be found among the candidate paths.
    """
    results: list[OpsResult] = []
    tpl, checked = _find_launchagent_template()
    if tpl is None:
        details = (
            "Tried:\n"
            + "\n".join(str(p) for p in checked)
            + "\nRun `nyxgpt ops install` first to sync packaged templates into "
            f"{NYXGPT_HOME}."
        )
        results.append(OpsResult(False, "Missing Cassandra logs LaunchAgent template", details))
        return results
    la_dir = Path.home() / "Library" / "LaunchAgents"
    _ensure_dir(la_dir)
    dst = la_dir / tpl.name
    _install_launchagent_from_template(tpl, dst)

    label = "com.nyxgpt.cassandra-logs"
    domain = f"gui/{os.getuid()}"

    _run(["launchctl", "bootout", domain, str(dst)], check=False, expected=True)
    _run(["launchctl", "bootstrap", domain, str(dst)], check=False)
    _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)

    results.append(OpsResult(True, "Installed Cassandra logs LaunchAgent", str(dst)))
    return results


def _install_ollama_launchagent() -> list[OpsResult]:
    """Install and (re)load the Ollama container-logs LaunchAgent (Compose mode).

    Mirrors `_install_cassandra_launchagent`: locates the plist template,
    copies it into ~/Library/LaunchAgents, then boots it out and back in via
    `launchctl bootout`/`bootstrap`/`kickstart`. Installed unconditionally by
    `nyxgpt ops install` regardless of deployment mode, same as the Cassandra
    LaunchAgent -- `follow-ollama-logs.sh` (which this LaunchAgent runs)
    handles both Compose mode (follows the `nyxgpt-ollama` container's
    `docker logs`) and native mode (tails Homebrew's own ollama.log
    directly), switching between them on its own (see #3441). Returns a
    single-element list of OpsResult; fails if the template can't be found
    among the candidate paths.
    """
    results: list[OpsResult] = []
    tpl, checked = _find_launchagent_template("com.nyxgpt.ollama-logs.plist")
    if tpl is None:
        details = (
            "Tried:\n"
            + "\n".join(str(p) for p in checked)
            + "\nRun `nyxgpt ops install` first to sync packaged templates into "
            f"{NYXGPT_HOME}."
        )
        results.append(OpsResult(False, "Missing Ollama logs LaunchAgent template", details))
        return results
    la_dir = Path.home() / "Library" / "LaunchAgents"
    _ensure_dir(la_dir)
    dst = la_dir / tpl.name
    _install_launchagent_from_template(tpl, dst)

    label = "com.nyxgpt.ollama-logs"
    domain = f"gui/{os.getuid()}"

    _run(["launchctl", "bootout", domain, str(dst)], check=False, expected=True)
    _run(["launchctl", "bootstrap", domain, str(dst)], check=False)
    _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)

    results.append(OpsResult(True, "Installed Ollama logs LaunchAgent", str(dst)))
    return results


def _install_ollama_env_launchagent() -> list[OpsResult]:
    """Install and (re)load the Ollama shared-model-store env LaunchAgent (#3431).

    Mirrors `_install_ollama_launchagent`/`_install_cassandra_launchagent`:
    locates the `com.nyxgpt.ollama-env.plist` template, copies it into
    ~/Library/LaunchAgents, then boots it out and back in. That LaunchAgent
    runs `scripts/set-ollama-models-env.sh` (`launchctl setenv OLLAMA_MODELS
    ...`) at every login, since a bare `launchctl setenv` call (as done
    immediately by `_ensure_ollama_service`) only applies to the current
    session and would otherwise be lost on reboot. Returns a single-element
    list of OpsResult; fails if the template can't be found among the
    candidate paths.
    """
    results: list[OpsResult] = []
    tpl, checked = _find_launchagent_template("com.nyxgpt.ollama-env.plist")
    if tpl is None:
        details = (
            "Tried:\n"
            + "\n".join(str(p) for p in checked)
            + "\nRun `nyxgpt ops install` first to sync packaged templates into "
            f"{NYXGPT_HOME}."
        )
        results.append(OpsResult(False, "Missing Ollama env LaunchAgent template", details))
        return results
    la_dir = Path.home() / "Library" / "LaunchAgents"
    _ensure_dir(la_dir)
    dst = la_dir / tpl.name
    _install_launchagent_from_template(tpl, dst)

    label = "com.nyxgpt.ollama-env"
    domain = f"gui/{os.getuid()}"

    _run(["launchctl", "bootout", domain, str(dst)], check=False, expected=True)
    _run(["launchctl", "bootstrap", domain, str(dst)], check=False)
    _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)

    results.append(OpsResult(True, "Installed Ollama env LaunchAgent", str(dst)))
    return results


_STALE_LOG_SYMLINK_NAMES: tuple[str, ...] = (
    "nyxgpt-api.log",
    "nyxgpt-api.err.log",
    "nyxgpt-web.log",
    "nyxgpt-web.err.log",
)


def _cleanup_stale_log_symlinks() -> list[OpsResult]:
    """Remove leftover ~/.nyxGPT/logs symlinks from the pre-#3441 symlink mechanism.

    A prior nyxgpt version's `_ensure_log_symlinks` pointed these names at
    Homebrew's var/log dir. That target lives outside ~/.nyxGPT/logs, which
    is all promtail's container actually bind-mounts, so those symlinks were
    never reachable from inside it and never shipped a single line to Loki
    (#3441) -- nyxgpt-api/nyxgpt-web's own structured logs already land
    directly in ~/.nyxGPT/logs as real files (`api.log`, see
    `nyxgpt.logging.configure_logging`), and Ollama's log now reaches
    ~/.nyxGPT/logs/ollama.log as a real file too, written by
    `follow-ollama-logs.sh` itself. Reconciles on every `nyxgpt ops install`
    so a leftover symlink from before this fix doesn't linger.

    Returns one OpsResult per name considered.
    """
    results: list[OpsResult] = []
    home_logs = Path.home() / ".nyxGPT" / "logs"
    for name in _STALE_LOG_SYMLINK_NAMES:
        dst = home_logs / name
        try:
            if dst.is_symlink():
                dst.unlink()
                results.append(OpsResult(True, f"Removed stale log symlink {name}", str(dst)))
            else:
                results.append(OpsResult(True, f"No stale log symlink for {name}"))
        except Exception as e:
            results.append(OpsResult(False, f"Failed to remove stale log symlink {name}", str(e)))
    return results


def _create_dist_tarball(
    tap_dir: Path, name: str, version: str, source_root: Path | None = None
) -> Path:
    """`nyxgpt.release_tarball._create_dist_tarball`, defaulted to *this* module's REPO_ROOT.

    The builder itself moved to the stdlib-only `nyxgpt.release_tarball`
    module (#3741) so release tooling can import it without this module's
    third-party dependencies. Every local install path here still calls it
    by its `ops.` name and expects "vendor from the checkout `ops.REPO_ROOT`
    points at", so the default is resolved here rather than there.
    """
    return release_tarball._create_dist_tarball(
        tap_dir, name, version, REPO_ROOT if source_root is None else source_root
    )


# --- Artifact install: the same service tarballs, published (#3759) -----
#
# The native installers below build each service from a
# `<name>-<version>.tar.gz` that `_create_dist_tarball` vendors out of the
# checkout. An artifact install (`pip install nyxgpt`, the repo-less
# portability requirement -- CLAUDE.md 2026-08-01) has no checkout to vendor
# from: `REPO_ROOT` resolves *inside* the installed venv
# (.../venv/lib/python3.11), where `src/nyxgpt`, `web/` and `pyproject.toml`
# do not exist. That is what broke `nyxgpt cloud deploy` on EC2 (#3759): the
# api install step died on `FileNotFoundError: .../lib/python3.11/src/nyxgpt`.
#
# The fix is not a second install recipe -- it is a second *source* for the
# very same tarball. Every release and release candidate attaches
# `nyxgpt-api-<version>.tar.gz` and `nyxgpt-web-<version>.tar.gz` to its
# GitHub Release (release-artifacts.yml's homebrew-tap job for a release,
# release-publish-pypi.yml's rc job for a candidate) -- byte-for-byte what
# `_create_dist_tarball` produces locally, and exactly what the published
# Homebrew formulas install from. So `_service_source_tarball` vendors from
# the checkout when there is one and downloads the published asset when
# there isn't; everything downstream (venv, pip install, npm build, wrapper,
# unit) is unchanged and shared by both paths.
#
# `{tag}` is the release the assets are *published on*, which is not always
# the release named by the version (#3763): a candidate carries its own
# tarballs, but a stable release is published before they are built and can
# never gain an asset afterwards, so its tarballs are served from the
# `<version>-homebrew` sidecar release. `_release_asset_urls` tries both, in
# that order, from `release_tarball.homebrew_asset_release_tag` -- the same
# definition `scripts/build_homebrew_artifacts.py` stamps the Homebrew
# formulas' `url` with.
RELEASE_ASSET_URL = (
    "https://github.com/dkblinux98/nyxGPT/releases/download/{tag}/{name}-{version}.tar.gz"
)


def _release_asset_urls(name: str, version: str) -> list[str]:
    """Every URL `<name>-<version>.tar.gz` may be published at, best first.

    The version's own release first -- where a release candidate's tarballs
    are, and where releases published before #3763 carry theirs -- then the
    `<version>-homebrew` sidecar the stable tarballs are published on now.
    """
    tags = [version, release_tarball.homebrew_asset_release_tag(version)]
    return [RELEASE_ASSET_URL.format(tag=tag, name=name, version=version) for tag in tags]


# The published Homebrew tap, macOS's artifact channel (docs/homebrew.md).
# Used when `nyxgpt ops install` runs on macOS from an artifact install: the
# local `file://` tap is built from a checkout, the remote one isn't.
REMOTE_TAP = "dkblinux98/nyxgpt"


def _has_vendorable_source(name: str) -> bool:
    """True when `REPO_ROOT` holds the source `_create_dist_tarball` vendors for `name`.

    Per service rather than one repo-wide "is this a checkout" flag,
    because that is exactly the granularity of the failure: the api tarball
    needs `pyproject.toml` + `src/nyxgpt/` + `example.config.ini`, the web
    tarball needs `web/`, and each installer only cares about its own.
    False means "artifact install" for that service -- see
    `_service_source_tarball`.
    """
    if name == "nyxgpt-web":
        return (REPO_ROOT / "web" / "package.json").is_file()
    return (REPO_ROOT / "src" / "nyxgpt").is_dir() and (REPO_ROOT / "pyproject.toml").is_file()


def _dev_checkout_root() -> Path | None:
    """Return the source checkout dev mode would build from, or None.

    Dev mode is checkout-only by definition: it needs `pyproject.toml` +
    `src/nyxgpt/` for the editable api install and `web/` for the Next dev
    server. `REPO_ROOT` points inside the installed package on an artifact
    install, where neither is present -- so this answers None there and
    `install()` refuses `--dev` with that fact rather than half-installing.
    """
    if _has_vendorable_source("nyxgpt-api") and _has_vendorable_source("nyxgpt-web"):
        return REPO_ROOT
    return None


def dev_checkout_root() -> Path | None:
    """Public name for `_dev_checkout_root`, for modules outside `ops` (#3950).

    `nyxgpt cloud deploy --dev` has to answer the same question this module's
    own install paths do -- *is there a working tree to build from at all?* --
    and it must answer it the same way, or the cloud path would refuse (or
    accept) a checkout the local path disagrees about. Delegating rather than
    re-implementing is the point: `cloud_deploy` gets one call, and this stays
    the only definition of what a dev-mode source tree is.

    Deliberately a thin forward to the private name rather than a rename: every
    caller inside this module, and the tests that monkeypatch
    `ops._dev_checkout_root`, keep working unchanged.
    """
    return _dev_checkout_root()


def _installed_distribution_version() -> str | None:
    """Version of the installed `nyxgpt` distribution, or None if it isn't installed."""
    try:
        return importlib.metadata.version("nyxgpt")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - source tree only
        return None


def _native_service_version() -> str:
    """The version the native `api`/`web` services are installed at.

    A dev checkout answers with the checkout's declared `pyproject.toml`
    version, so a version bump is picked up with no reinstall (the reason
    `_read_project_version` reads the file rather than package metadata).
    An artifact install has no `pyproject.toml` above the package -- where
    `_read_project_version` would answer its deliberately implausible
    "0.0.0" -- so the installed distribution's metadata version answers
    instead. That is the release whose published tarballs
    `_download_release_tarball` fetches, which keeps the services on the
    same release as the `nyxgpt` that installed them.
    """
    if (REPO_ROOT / "pyproject.toml").is_file():
        return _read_project_version()
    return _installed_distribution_version() or _read_project_version()


def _download_release_tarball(dest_dir: Path, name: str, version: str) -> Path:
    """Download the published `<name>-<version>.tar.gz` release asset into `dest_dir/dist`.

    Mirrors `_create_dist_tarball`'s contract -- same filename, same
    `dist/` location, same returned path -- so the callers can't tell which
    source produced the tarball. Downloads to a temp path and `replace()`s
    it into position, so an interrupted download can never leave a
    truncated archive behind for the next run to install from (the same
    reason `_download_tool_binary` does it).

    Tries every release the asset may be published on (`_release_asset_urls`
    -- the version's own release, then its `<version>-homebrew` sidecar), so
    a stable version whose release could not take the assets still installs.

    Raises RuntimeError with the URLs tried and the last underlying error
    when the asset can't be fetched anywhere -- the caller turns that into an
    OpsResult rather than letting a traceback stand in for a diagnosis.
    """
    dist_dir = dest_dir / "dist"
    _ensure_dir(dist_dir)
    tar_path = dist_dir / f"{name}-{version}.tar.gz"
    tmp = dist_dir / f".{name}-{version}.download"
    urls = _release_asset_urls(name, version)
    last_error: Exception | None = None
    for url in urls:
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=300.0) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
            tmp.replace(tar_path)
        except (httpx.HTTPError, OSError) as e:
            # Never leave a truncated archive behind for the next candidate
            # URL -- or the next run -- to install from.
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            last_error = e
            continue
        return tar_path
    raise RuntimeError(
        f"Could not download the published {name} {version} artifact from "
        f"{' or '.join(urls)} ({type(last_error).__name__}: {last_error}). Check network "
        f"access to github.com, and that {version} is a published release or release "
        "candidate."
    ) from last_error


# Escape hatch for an artifact install of a version no release serves:
# `NYXGPT_ARTIFACT_DIR` names a directory already holding the very same
# `<name>-<version>.tar.gz` assets, and they are used instead of being
# downloaded. Two callers need it and neither is a real instance:
#
#   - `nyxgpt cloud smoke --container --wheel` (#3784), which installs a wheel
#     built from the tree under test. That wheel's version is the checkout's
#     in-development one (e.g. `3.0.0`), which has no GitHub Release and never
#     will until the release ceremony cuts one -- so the CLI installs and then
#     `ops install` 404s on `nyxgpt-api-<version>.tar.gz`. Staging the tarballs
#     built from the same tree is what makes a branch's own `ops.py` testable
#     on the artifact path at all; without it the job can only ever install a
#     *published* rc, i.e. not the code under review.
#   - an air-gapped install, which has the assets but no route to github.com.
#
# It is the service-tarball sibling of the bootstrap's `NYXGPT_PIP_SPEC`
# (scripts/cloud/ec2-user-data-linux.sh.tmpl), and unset on a real instance.
LOCAL_ARTIFACT_DIR_ENV = "NYXGPT_ARTIFACT_DIR"


def _staged_service_tarball(dest_dir: Path, name: str, version: str) -> Path | None:
    """Copy `<name>-<version>.tar.gz` out of `$NYXGPT_ARTIFACT_DIR`, or None if unset.

    Mirrors `_download_release_tarball`'s contract -- same filename, same
    `dist/` location, same returned path -- so callers can't tell which of
    the three sources produced the tarball.

    A directory that is set but does not hold the asset raises rather than
    falling through to the download: the caller asked for *this* artifact,
    and silently installing a different one (or 404ing against a URL they
    never chose) is how a staging bug reads as a network failure.
    """
    directory = os.environ.get(LOCAL_ARTIFACT_DIR_ENV, "").strip()
    if not directory:
        return None
    staged = Path(directory).expanduser() / f"{name}-{version}.tar.gz"
    if not staged.is_file():
        raise RuntimeError(
            f"{LOCAL_ARTIFACT_DIR_ENV}={directory} is set, so {staged.name} was expected "
            f"there, but {staged} does not exist. Stage the asset or unset "
            f"{LOCAL_ARTIFACT_DIR_ENV} to download it from the {version} release instead."
        )
    dist_dir = dest_dir / "dist"
    _ensure_dir(dist_dir)
    tar_path = dist_dir / staged.name
    shutil.copy2(staged, tar_path)
    return tar_path


def _service_source_tarball(dest_dir: Path, name: str, version: str) -> Path:
    """Return the `<name>-<version>.tar.gz` a native installer builds `name` from.

    Vendored from the checkout when one is present (the dev path, byte-for-
    byte as before), taken from `$NYXGPT_ARTIFACT_DIR` when the operator
    staged it, and otherwise downloaded from the published GitHub Release
    asset (the artifact path, #3759). Raises RuntimeError if the artifact
    can't be obtained.
    """
    if _has_vendorable_source(name):
        return _create_dist_tarball(dest_dir, name, version)
    staged = _staged_service_tarball(dest_dir, name, version)
    if staged is not None:
        return staged
    return _download_release_tarball(dest_dir, name, version)


def _homebrew_formula_template(name: str) -> Path | None:
    """The checkout's formula template for `name`, or None on an artifact install.

    Single-sourced because two callers must never disagree about it
    (#3861): `_install_homebrew_api`/`_web` use its presence to decide
    between building a local `file://` tap and installing from the published
    tap, and `_native_install_identity` uses it to name the service that
    decision will register. If identity detection guessed the routing
    separately, a reconcile would compare against a formula name no install
    ever used -- which is the same class of bug as recording no name at all.
    """
    template = REPO_ROOT / "homebrew" / f"{name}.rb"
    return template if template.exists() else None


def _remote_tap_formula(name: str, version: str) -> str:
    """The published tap's formula name for `name` at `version`.

    Stable releases keep the plain name (`nyxgpt-api`); a release candidate
    is published as its release line's candidate formula
    (`3.0.0rc5` -> `nyxgpt-api@3.0.0rc`), so the two channels can coexist in
    one tap without `brew install nyxgpt-api` ever resolving to a candidate
    -- see docs/homebrew.md#candidate-channel and
    `scripts/build_homebrew_artifacts.py`'s `formula_name`.
    """
    line, marker, _candidate = version.partition("rc")
    return f"{name}@{line}rc" if marker else name


def _install_from_remote_tap(name: str) -> list[OpsResult]:
    """Install `name` from the published Homebrew tap and (re)start its service.

    macOS's artifact-install path (#3759): `_install_homebrew_api`/`_web`
    build a local `file://` tap out of the checkout, which an artifact
    install doesn't have. The remote tap carries the same formulas built
    from the same tarballs, so this is the identical install recipe with a
    published source -- the documented macOS install
    (docs/homebrew.md#install), run for the operator instead of handed to
    them.
    """
    if _which("brew") is None:
        return [OpsResult(False, "Homebrew not found", "")]

    version = _native_service_version()
    formula = _remote_tap_formula(name, version)
    spec = f"{REMOTE_TAP}/{formula}"

    cp = _run(["brew", "tap", REMOTE_TAP], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, f"Failed to tap {REMOTE_TAP}", _cp_details(cp))]
    # Homebrew gates formulas from third-party taps: without trust, `brew
    # install` stops instead of installing (#3752). Trust the tap, never one
    # formula -- installing a candidate resolves its `conflicts_with` against
    # the stable formula, which brew then has to load too, and a per-formula
    # grant does not cover it (#3770). The subcommand is spelled `tap-trust`
    # on some Homebrews and `trust` on others, so try both. Tolerated on
    # failure -- Homebrew builds predating the trust gate have nothing to
    # trust.
    if _run(["brew", "tap-trust", REMOTE_TAP], check=False, expected=True).returncode != 0:
        _run(["brew", "trust", REMOTE_TAP], check=False, expected=True)

    started_at = time.time()
    cp = _run(["brew", "install", spec], check=False)
    warning = ""
    if cp.returncode != 0:
        # Two different nonzero exits arrive here and they are not the same
        # thing (#3861).
        #
        # The first is a Homebrew post-install *soft* failure -- the install
        # completed, an `ofail` step after it did not, and brew's exit status
        # reports the latter. Resolve it against the keg, exactly as
        # `_brew_install_or_reinstall` does; the two paths used to disagree
        # about this class purely by accident, because the `brew upgrade`
        # fallback below happens to exit 0 on an already-installed formula and
        # so reported `ok` for the very failure the other path called fatal.
        #
        # The second is a genuine "already installed, this is an upgrade"
        # refusal, which is what that fallback was written for -- and which is
        # now decided on the keg too, because `brew upgrade` also exits 0 when
        # it does nothing at all.
        soft_failure = _brew_soft_failure_reason(_cp_details(cp))
        verified, evidence = (
            _verify_installed_brew_keg(
                formula, spec=spec, entrypoint=name, installed_after=started_at
            )
            if soft_failure
            else (False, "brew reported no post-install soft failure")
        )
        if soft_failure and verified:
            warning = (
                f"Homebrew reported a post-install soft failure and exited "
                f"{cp.returncode}: {soft_failure}. The keg was still installed -- "
                f"verified here, not assumed: {evidence}."
            )
        else:
            cp_upgrade = _run(["brew", "upgrade", spec], check=False)
            verified, evidence = _verify_installed_brew_keg(formula, spec=spec, entrypoint=name)
            if not verified:
                return [
                    OpsResult(
                        False,
                        f"Failed to install {spec} from the published tap",
                        f"{_cp_details(cp)}\n{_cp_details(cp_upgrade)}\n{evidence}",
                    )
                ]
            if cp_upgrade.returncode != 0:
                warning = (
                    f"`brew upgrade {spec}` exited {cp_upgrade.returncode}, but the keg "
                    f"verifies as installed and usable: {evidence}."
                )

    detail = f"version {version}"
    if warning:
        detail = f"{detail}\n{warning}"
    if formula != name:
        # brew names a service after its formula, so a candidate keg's
        # service is `nyxgpt-api@3.0.0rc`, not `nyxgpt-api`.
        #
        # This used to warn that `ops status` would therefore report the
        # component as not running. It no longer does: the probe resolves the
        # service from `brew services list` instead of asserting the stable
        # name (#3853, `nyxgpt/brew_services.py`). Keeping the note as a
        # statement of fact is still worth the line -- the operator sees a
        # versioned name in `brew services list` and needs to know it is
        # theirs -- but a caveat printed once at install time was never a
        # substitute for `status` being right, and while it stood it also
        # hid the half nobody had accounted for: `nyxgpt up` gates on the
        # same probe, so it waited its full timeout and exited 2 on a stack
        # that was entirely healthy.
        detail += (
            f"\nCandidate channel: the service is named {formula} after its formula "
            f"(not {name}), which is what `brew services list` shows and what "
            "`nyxgpt ops status`, `nyxgpt up` and self-heal resolve it by. "
            "See docs/homebrew.md#candidate-channel."
        )
    results = [
        OpsResult(
            True,
            f"Installed {formula} from {REMOTE_TAP}",
            detail,
            status="WARN" if warning else "",
        )
    ]
    results.extend(_restart_brew_service(formula))
    return results


# Homebrew's post-build *soft* failures, verbatim from its own source
# (`Library/Homebrew/formula_installer.rb`, lines 1214/1242/1250/1259/1313/1324
# in the Homebrew this project runs against). Each of these is an `ofail`, not
# an `odie`: the formula is already built, the keg is already in the Cellar and
# `FormulaInstaller#finish` carries on past it -- it only sets `Homebrew.failed`,
# which `brew.rb`'s `exit Homebrew.failed? ? 1 : 0` turns into an exit status of
# 1 at the very end. So "brew exited 1" and "the install failed" are different
# claims, and #3861's acceptance failure is what happens when ops treats them as
# the same one.
#
# The failure the owner hit was `Failed to fix install linkage`. Its cause is
# structural, not environmental: for a source build Homebrew runs
# `fix_dynamic_linkage` unconditionally (`formula_installer.rb:999` -- only a
# *poured bottle* with `skip_relocation` may skip it, and this project ships no
# bottles), and that step rewrites the `LC_ID_DYLIB` install name of every
# Mach-O dylib in the keg to the keg's full `opt` path. `tiktoken`'s
# maturin-built `_tiktoken.cpython-312-darwin.so` is such a dylib, its ID is the
# 38-byte `@rpath/_tiktoken.cpython-312-darwin.so`, and the replacement
# (`/opt/homebrew/opt/nyxgpt-api/libexec/venv/lib/python3.12/site-packages/
# tiktoken/_tiktoken.cpython-312-darwin.so`) is ~111 bytes, which does not fit
# the load-command padding the wheel was linked with -- ruby-macho raises and
# `Keg#change_dylib_id` re-raises. That is exactly what the 2026-07-29 log
# recorded, in full, before ops' output bounding started eliding it.
#
# Nothing is actually broken by that failure: an `@rpath` ID on a Python
# extension module is correct, because CPython `dlopen`s it by path and nothing
# ever links against its ID -- which is why `brew linkage` reported the keg
# clean while `brew` had exited 1.
#
# There is no formula-level setting that can prevent it, so ops cannot make the
# nonzero exit go away. What it can do is stop using an ambiguous signal: the
# exit code says brew ran and something in it failed, not *what*. So a nonzero
# exit is resolved against the keg itself (`_verify_brew_keg`) -- the same move
# `_stop_brew_service` already makes in the other direction, where brew exits 0
# without having de-registered anything.
_BREW_SOFT_FAILURE_MARKERS: tuple[str, ...] = (
    "Failed to fix install linkage",
    "The `brew link` step did not complete successfully",
    "An unexpected error occurred during the `brew link` step",
    "Failed to install service files",
    "Failed to create ",
)


def _brew_soft_failure_reason(output: str) -> str | None:
    """The Homebrew post-install soft failure `output` reports, or None.

    Matches only the `ofail` wordings listed in `_BREW_SOFT_FAILURE_MARKERS`,
    and only on a line brew itself marked as an error (`Error: ...`), so an
    incidental mention in a caveats block or in a formula's own build log
    cannot be read as one. If Homebrew rewords one of these, this returns None
    and the caller falls back to treating the exit as fatal -- the safe
    direction, and a visible one, because the operator then sees the raw
    Homebrew text in the failure detail.
    """
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Error:"):
            continue
        detail = stripped[len("Error:") :].strip()
        for marker in _BREW_SOFT_FAILURE_MARKERS:
            if detail.startswith(marker):
                return detail
    return None


def _brew_path(flag: str) -> Path | None:
    """`brew --prefix`/`--cellar` (no formula argument), or None if brew can't say.

    Deliberately called without a formula name: with two taps carrying the same
    formula names -- every machine that has tested the published tap alongside
    the local one -- `brew --prefix <bare name>` is ambiguous and errors, while
    the bare directory query never is (#3861).
    """
    if _which("brew") is None:
        return None
    cp = _run(["brew", flag], check=False, expected=True, timeout=60)
    if cp.returncode != 0 or not (cp.stdout or "").strip():
        return None
    return Path((cp.stdout or "").strip().splitlines()[0])


def _verify_brew_keg(
    formula: str,
    *,
    version: str,
    spec: str | None,
    entrypoint: str,
    installed_after: float | None = None,
) -> tuple[bool, str]:
    """Is the keg `formula`@`version` really installed, linked and usable?

    This is the observation that replaces trusting `brew`'s exit code (see
    `_BREW_SOFT_FAILURE_MARKERS`). Every check is against the machine, none
    against what brew said:

    1. the keg directory exists in the Cellar at exactly `version`;
    2. its `INSTALL_RECEIPT.json` parses, and -- when `installed_after` is
       given -- was written at or after that moment. This is the check that
       stops a *restored backup* from reading as a successful reinstall:
       `brew reinstall` moves the old keg aside and puts it back if the build
       dies, so the directory and the version both survive a genuine failure
       and only the receipt's timestamp tells the two apart;
    3. `<prefix>/opt/<formula>` resolves to that keg -- the `opt` path is what
       the generated LaunchAgent plist execs, so a keg with no `opt` link is
       not runnable however complete it looks;
    4. the keg's `bin/<entrypoint>` exists and is executable;
    5. `brew linkage --test <spec>` finds no missing libraries -- skipped when
       `spec` is None, because that check is the only one here that makes brew
       *resolve a formula*, and a bare formula name is ambiguous on a machine
       carrying two taps that both provide it. A caller that has no
       tap-qualified spec passes None rather than a name that would fail for
       the wrong reason; checks 1-4 read the Cellar layout directly and are
       never ambiguous.

    Returns `(ok, detail)`, where `detail` names the first check that failed
    (or lists what passed) so a failure is never reported as a bare "not
    verified".
    """
    cellar = _brew_path("--cellar")
    prefix = _brew_path("--prefix")
    if cellar is None or prefix is None:
        return False, "could not read `brew --cellar`/`brew --prefix`"

    keg = cellar / formula / version
    if not keg.is_dir():
        return False, f"no keg at {keg}"

    receipt = keg / "INSTALL_RECEIPT.json"
    try:
        receipt_data = json.loads(receipt.read_text(encoding="utf-8"))
        receipt_time = float(receipt_data.get("time", 0))
    except (OSError, ValueError, TypeError):
        return False, f"unreadable install receipt at {receipt}"
    if installed_after is not None and receipt_time + 5 < installed_after:
        return False, (
            f"the keg at {keg} predates this install "
            f"(receipt time {receipt_time:.0f} < {installed_after:.0f}) -- brew restored "
            "the previous keg rather than completing this one"
        )

    opt = prefix / "opt" / formula
    try:
        opt_ok = opt.resolve() == keg.resolve()
    except OSError:
        opt_ok = False
    if not opt_ok:
        return False, f"{opt} does not resolve to {keg}"

    exe = keg / "bin" / entrypoint
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        return False, f"no executable {exe}"

    if spec is None:
        linkage_state = "`brew linkage` not run (no tap-qualified spec to ask about)"
    else:
        linkage = _run(["brew", "linkage", "--test", spec], check=False, expected=True, timeout=300)
        if linkage.returncode != 0:
            return False, (
                f"`brew linkage --test {spec}` reports missing libraries:\n"
                f"{_output_excerpt(linkage)}"
            )
        linkage_state = "`brew linkage --test` is clean"

    # Reported, deliberately NOT gated on. `<prefix>/bin/<entrypoint>` is the
    # PATH-visible symlink, and it is exactly what Homebrew's `brew link`
    # `ofail` leaves unmade -- but the case that produces it here is two kegs
    # of the same component owning one link (the owner's 2026-08-17 run, where
    # `nyxgpt-api@3.0.0rc` held `/opt/homebrew/bin/nyxgpt-api`), and in that
    # state the command still runs, from the other keg. Failing the whole
    # install on it would turn a benign collision into a hard stop, so the
    # answer is carried into the warning instead of into the verdict. The
    # *service* does not depend on it either way: the plist execs the `opt`
    # path checked above.
    link = prefix / "bin" / entrypoint
    if not link.exists():
        link_state = f"{link} is NOT linked (run `nyxgpt ops doctor` to see what owns it)"
    elif link.resolve() == exe.resolve():
        link_state = f"{link} points at this keg"
    else:
        link_state = f"{link} points at {link.resolve()}, not this keg"

    return True, f"keg {keg} is complete, linked at {opt}, {linkage_state}; {link_state}"


def _verify_installed_brew_keg(
    formula: str,
    *,
    spec: str,
    entrypoint: str,
    installed_after: float | None = None,
) -> tuple[bool, str]:
    """`_verify_brew_keg` against whatever version of `formula` is installed now.

    For the published tap the expected version is a moving target -- the tap
    can carry a newer release candidate than the running artifact reports --
    so pinning the check to a version this process computed would fail a
    perfectly good install. What matters there is that *a* complete keg of
    this formula is installed; the freshness half of the check
    (`installed_after`) still says whether this run produced it.
    """
    version = _installed_keg_version(formula)
    if version is None:
        return False, f"no installed keg for {formula}"
    return _verify_brew_keg(
        formula,
        version=version,
        spec=spec,
        entrypoint=entrypoint,
        installed_after=installed_after,
    )


@dataclass(frozen=True)
class _BrewInstallOutcome:
    """What `_brew_install_or_reinstall` decided, plus any soft failure it tolerated.

    `warning` is empty on a clean install. When brew reported a post-install
    soft failure that the keg verification then contradicted, it carries the
    Homebrew wording and the evidence -- so an operator is told the install
    step did not fully succeed even though the component is usable, rather
    than being shown a bare "installed" that hides it.
    """

    decision: str
    warning: str = ""


def _brew_install_or_reinstall(
    spec: str, name: str, *, sha256: str, marker_dir: Path, version: str, entrypoint: str = ""
) -> _BrewInstallOutcome:
    """`brew install`/`reinstall` formula `name`, skipping the work when unchanged.

    Compares the just-built tarball's `sha256` against the one recorded the
    last time this formula was actually installed (`marker_dir/.<name>.sha256`).
    If the formula is already installed and the checksum matches, the source
    hasn't changed since the last install: skip the `brew fetch`/`install`
    round-trip entirely. Otherwise (not installed yet, or the checksum
    changed -- a new/edited source tree), fetch and install/reinstall, then
    record the new checksum. The `fetch --force` refreshes brew's download
    cache first: the tarball URL and version are constant across runs, so
    without it brew would reinstall from a stale cached tarball and fail the
    formula's sha256 check.

    Returns a `_BrewInstallOutcome`: which of the three decisions was made
    ("installed" / "reinstalled (source changed)" / "already up to date
    (skipped)"), plus any Homebrew post-install soft failure that was
    tolerated because the keg verified complete anyway.

    A nonzero exit from brew is NOT taken at face value (#3861). If it names
    one of Homebrew's post-build `ofail` steps *and* `_verify_brew_keg` finds
    the keg complete, linked and clean, the install is reported as done with
    that warning attached; anything else still raises. See
    `_BREW_SOFT_FAILURE_MARKERS` for why both halves are required and why
    neither alone would do.
    """
    entrypoint = entrypoint or name
    installed = (
        _run(
            ["brew", "list", "--versions", _brew_formula_spec(name)], check=False, expected=True
        ).returncode
        == 0
    )
    marker = marker_dir / f".{name}.sha256"
    previous_sha256 = marker.read_text(encoding="utf-8").strip() if marker.exists() else None

    if installed and previous_sha256 == sha256:
        return _BrewInstallOutcome("already up to date (skipped reinstall)")

    _run(["brew", "fetch", "--force", spec], check=False)
    started_at = time.time()
    if installed:
        cp = _run(["brew", "reinstall", spec], check=False)
        action, decision = "reinstall", "reinstalled (source changed since last install)"
    else:
        cp = _run(["brew", "install", "--overwrite", spec], check=False)
        action, decision = "install", "installed"

    warning = ""
    if cp.returncode != 0:
        detail = _output_excerpt(cp)
        soft_failure = _brew_soft_failure_reason(_cp_details(cp))
        verified, evidence = (
            _verify_brew_keg(
                name,
                version=version,
                spec=spec,
                entrypoint=entrypoint,
                installed_after=started_at,
            )
            if soft_failure
            else (False, "brew reported no post-install soft failure")
        )
        if not (soft_failure and verified):
            # Surface the failure instead of reporting a false success, and do
            # NOT record the checksum: writing the marker on a failed build
            # would make the next run see a matching checksum and skip, so the
            # broken install would never be retried (the bug that let a failed
            # api keg rebuild report "reinstalled" and then stick as a stale
            # wrapper).
            raise RuntimeError(f"brew {action} {name} failed: {detail}\n{evidence}")
        warning = (
            f"Homebrew reported a post-install soft failure and exited "
            f"{cp.returncode}: {soft_failure}. The keg was still built and "
            f"installed -- verified here, not assumed: {evidence}. This is an "
            "`ofail` in Homebrew's own post-install phase (it sets the exit "
            "status without stopping the install), so the component is usable; "
            "nothing to retry."
        )
        logger.warning(
            "ops: brew %s %s exited %d on a post-install soft failure (%s); keg verified: %s",
            action,
            name,
            cp.returncode,
            soft_failure,
            evidence,
            extra={"component": "ops", "formula": name, "soft_failure": soft_failure},
        )

    _ensure_dir(marker_dir)
    marker.write_text(sha256, encoding="utf-8")
    return _BrewInstallOutcome(decision, warning)


# Where `_docker_build_if_needed` records the sha256 fingerprint of the source
# an image was last built from (`.<image>.sha256`, image name/tag sanitized) --
# the Docker-image analogue of `_tap_repo(tap) / "dist"` for the Homebrew
# markers `_brew_install_or_reinstall` reads/writes.
DOCKER_IMAGE_MARKER_DIR = Path.home() / ".nyxGPT" / "docker-images"


def _hash_paths(paths: list[Path], *, excludes: frozenset[str] = frozenset()) -> str:
    """sha256 fingerprint over the file contents under `paths` (files or directories).

    Walks each directory deterministically (sorted, skipping any dir named in
    `excludes` -- e.g. `_WEB_VENDOR_EXCLUDES`), hashing every file's path
    relative to its base plus its bytes, so the digest changes iff the
    content or layout under `paths` actually changed. Used by
    `_docker_build_if_needed` to detect source changes for the app the
    image is built from -- the Docker-image equivalent of `_sha256_file`
    over `_create_dist_tarball`'s vendored tarball.
    """
    digest = hashlib.sha256()
    for base in sorted(paths, key=str):
        if base.is_file():
            digest.update(f"{base.name}\0".encode())
            digest.update(base.read_bytes())
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(n for n in dirnames if n not in excludes)
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                rel = fpath.relative_to(base)
                digest.update(f"{base.name}/{rel}\0".encode())
                digest.update(fpath.read_bytes())
    return digest.hexdigest()


def _docker_build_if_needed(
    image: str,
    context: Path,
    *,
    fingerprint_paths: list[Path],
    excludes: frozenset[str] = frozenset(),
    build_args: dict[str, str] | None = None,
    marker_dir: Path,
) -> str:
    """`docker build -t image context`, skipping the work when the source hasn't changed.

    Mirrors `_brew_install_or_reinstall` (#3406) for the Docker/terraform
    image build sites (#3414). Compares a sha256 fingerprint of
    `fingerprint_paths` -- the actual app source the image is built from
    (e.g. `src/nyxgpt/` + `pyproject.toml` for `nyxgpt-api`, `web/` for
    `nyxgpt-web`), not the whole build context -- against the one recorded
    the last time `image` was actually built (`marker_dir/.<image>.sha256`,
    name/tag sanitized). If `image` already exists locally and the
    fingerprint matches, the source hasn't changed since the last build:
    skip `docker build` entirely. Otherwise (image missing, or the
    fingerprint changed -- a new/edited source tree), run the build and
    record the new fingerprint.

    Returns a short human-readable string describing which of the three
    decisions was made ("built" / "rebuilt (source changed since last
    build)" / "already up to date (skipped rebuild)"), for the caller to
    report.
    """
    marker_name = re.sub(r"[^A-Za-z0-9_.-]", "_", image)
    marker = marker_dir / f".{marker_name}.sha256"
    previous_fingerprint = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
    fingerprint = _hash_paths(fingerprint_paths, excludes=excludes)

    image_exists = (
        _run(["docker", "image", "inspect", image], check=False, expected=True).returncode == 0
    )
    if image_exists and previous_fingerprint == fingerprint:
        return "already up to date (skipped rebuild)"

    cmd = ["docker", "build", "-t", image]
    for arg_name, arg_value in (build_args or {}).items():
        cmd += ["--build-arg", f"{arg_name}={arg_value}"]
    cmd.append(str(context))
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        # As with `_brew_install_or_reinstall`: surface the failure and do NOT
        # record the fingerprint, so a broken build doesn't get skipped (and
        # so stick as a stale image) on the next run.
        detail = _output_excerpt(cp)
        raise RuntimeError(f"docker build {image} failed: {detail}")

    _ensure_dir(marker_dir)
    marker.write_text(fingerprint, encoding="utf-8")
    return "built" if previous_fingerprint is None else "rebuilt (source changed since last build)"


# The app source `nyxgpt-api`'s image is built from -- COPY'd into the
# Dockerfile (pyproject.toml, src/nyxgpt/, example.config.ini) plus the
# entrypoint script it also COPYs -- not the whole build context, so an
# unrelated repo-root change (docs, terraform/, etc.) doesn't force a rebuild.
#
# Kept as paths *relative to the build context* because there are now two
# contexts (#3834, #3985): the checkout, and the staged copy of the published
# `nyxgpt-api` artifact the Kubernetes and Terraform artifact paths build from
# (`_stage_artifact_build_context`). Identical layout, identical fingerprint --
# so identical content skips the rebuild in either mode.
_API_IMAGE_FINGERPRINT_RELPATHS = (
    Path("src") / "nyxgpt",
    Path("pyproject.toml"),
    Path("example.config.ini"),
    Path("docker") / "entrypoint.sh",
)
_API_IMAGE_FINGERPRINT_PATHS = [REPO_ROOT / rel for rel in _API_IMAGE_FINGERPRINT_RELPATHS]


def _installed_keg_version(formula: str) -> str | None:
    """The version of the currently installed `formula` keg, or None if there is none.

    Read off the Cellar directory rather than `brew list --versions <name>`,
    for the reason `_brew_path` records: with two taps carrying the same
    formula names a bare name is ambiguous to brew, and the Cellar layout
    never is. When several versions are present the one `<prefix>/opt/<formula>`
    resolves to wins -- that is the keg the service's plist actually execs --
    falling back to the highest-sorting directory name.
    """
    cellar = _brew_path("--cellar")
    if cellar is None:
        return None
    versions = sorted(p.name for p in (cellar / formula).glob("*") if p.is_dir())
    if not versions:
        return None
    prefix = _brew_path("--prefix")
    if prefix is not None:
        opt = prefix / "opt" / formula
        try:
            linked = opt.resolve().name
        except OSError:
            linked = ""
        if linked in versions:
            return linked
    return versions[-1]


def _recover_after_failed_native_install(
    formula: str, *, component: str, entrypoint: str
) -> list[OpsResult]:
    """Leave a running stack -- or a wrapped way back to one -- after a failed install step.

    #3861's second finding, and a violation in its own right: when the api and
    web install steps failed, ops never reached their `brew services restart`
    calls, so a machine that had been serving on :8000/:3000 was left with
    every registration stopped and no listener at all. The only route back was
    raw `brew services start` plus `brew services info`, which is exactly what
    CLAUDE.md's Operational Command Wrapping rule says an operator must never
    need.

    So a failed install no longer ends the step. If a *complete* keg for the
    component is still installed -- verified against the machine by
    `_verify_brew_keg`, not assumed from the fact that a directory exists --
    its service is (re)started here, and the operator is told which build is
    now serving. If there is no usable keg, nothing is started (starting a
    half-written keg would trade "down" for "crash-looping") and the result
    names the wrapped commands that diagnose and retry.

    Either way the returned results carry `nyxgpt`-wrapped remediation only.
    The install's own failure result is the caller's to append; nothing here
    reports the install as having succeeded.
    """
    remediation = (
        f"Retry with `nyxgpt up`; inspect with `nyxgpt ops doctor`, "
        f"`nyxgpt ops status` and `nyxgpt ops logs {component}`. "
        f"`nyxgpt ops restart {component}` restarts just this component."
    )
    version = _installed_keg_version(formula)
    if version is None:
        return [
            OpsResult(
                False,
                f"No previously installed {formula} keg to fall back to; {component} stays down",
                remediation,
            )
        ]

    verified, evidence = _verify_brew_keg(
        formula,
        version=version,
        # No tap-qualified spec here: the recovery does not know which tap the
        # keg came from, and a bare name is ambiguous on a two-tap machine --
        # `brew linkage` would fail for that reason and this would refuse to
        # start a keg that is perfectly good. Checks 1-4 are what "can this
        # keg serve?" actually needs.
        spec=None,
        entrypoint=entrypoint,
    )
    if not verified:
        return [
            OpsResult(
                False,
                f"The installed {formula} {version} keg is not usable; {component} stays down",
                f"{evidence}. {remediation}",
            )
        ]

    results = _restart_brew_service(formula)
    if all(r.ok for r in results):
        results.append(
            OpsResult(
                True,
                f"Recovery: {component} is running from the installed {formula} {version} keg",
                f"The install step above failed, so this is the previous build, not a new one. "
                f"{evidence}. {remediation}",
                status="NOTE",
            )
        )
    else:
        results.append(
            OpsResult(
                False,
                f"Recovery: could not start {component} from the installed "
                f"{formula} {version} keg",
                remediation,
            )
        )
    return results


def _install_homebrew_api(tap: str = "dkblinux98/nyxgpt-local") -> list[OpsResult]:
    """Build and install the `nyxgpt-api` Homebrew formula into `tap`, then (re)start the service.

    Generates a dist tarball vendoring `pyproject.toml` + `src/nyxgpt/`,
    patches the formula template's `sha256` to match it, writes the formula
    into the tap's Formula/ dir, and installs/reinstalls it only if the
    vendored source actually changed since the last install (see
    `_brew_install_or_reinstall`). The formula itself builds a self-contained
    venv inside the Cellar keg from that vendored source -- the installed app
    never depends on the repo checkout or an editable `.venv` (#3406).

    When a rebuild actually happened (`decision` is "installed" or
    "reinstalled ..."), this restarts the service (`brew services restart`)
    instead of just starting it: `brew services start` on an already-running
    service is a no-op, so a code fix landing while the API is still running
    would otherwise leave the *old* in-memory process serving requests
    against the new keg's on-disk source forever -- silently undoing the fix
    (#3472, mirrors the `nyxgpt-web` fix in #3445). When nothing changed, a
    plain `start` is used so an already-up-to-date install doesn't bounce a
    healthy running service. Returns a list of OpsResult; fails early if brew
    isn't installed or the formula template is missing.
    """
    results: list[OpsResult] = []
    if _which("brew") is None:
        return [OpsResult(False, "Homebrew not found", "")]

    template = _homebrew_formula_template("nyxgpt-api")
    if template is None:
        # No checkout to build a local tap from -- an artifact install
        # (`pip install nyxgpt`) installs the published formula instead of
        # failing on a missing template (#3759).
        return _install_from_remote_tap("nyxgpt-api")

    tap_dir = _tap_repo(tap)
    version = _read_project_version()

    tar = _create_dist_tarball(tap_dir, "nyxgpt-api", version)
    sha = _sha256_file(tar)

    content = template.read_text(encoding="utf-8")
    # Stamp the template's __VERSION__/__SHA256__ placeholders with the real
    # values for the tarball just generated. The regex substitutions remain as
    # a safety net for any formula copy that still carries concrete (stale)
    # values instead of placeholders.
    content = content.replace("__VERSION__", version)
    content = content.replace("__SHA256__", sha)
    content = re.sub(r'sha256 "[a-f0-9]+"', f'sha256 "{sha}"', content)
    content = re.sub(r'version "[^"]+"', f'version "{version}"', content)
    content = re.sub(r"nyxgpt-api-[^\"/]+\.tar\.gz", f"nyxgpt-api-{version}.tar.gz", content)

    formula_dir = tap_dir / "Formula"
    _ensure_dir(formula_dir)
    dst = formula_dir / "nyxgpt-api.rb"
    dst.write_text(content, encoding="utf-8")
    results.append(OpsResult(True, "Installed nyxgpt-api formula", str(dst)))

    try:
        outcome = _brew_install_or_reinstall(
            f"{tap}/nyxgpt-api",
            "nyxgpt-api",
            sha256=sha,
            marker_dir=tap_dir / "dist",
            version=version,
        )
    except RuntimeError as exc:
        # The install really failed (not one of Homebrew's post-install soft
        # failures -- `_brew_install_or_reinstall` has already resolved those
        # against the keg). Report it, then leave the operator a stack rather
        # than a hole: #3861.
        results.append(OpsResult(False, "Failed to install nyxgpt-api", str(exc)))
        results.extend(
            _recover_after_failed_native_install(
                "nyxgpt-api", component="api", entrypoint="nyxgpt-api"
            )
        )
        return results

    decision = outcome.decision
    if decision == "already up to date (skipped reinstall)":
        _run(["brew", "services", "start", _brew_formula_spec("nyxgpt-api")], check=False)
        results.append(OpsResult(True, f"nyxgpt-api: {decision}; requested service start", ""))
    else:
        # A new keg was just installed -- restart, not start, so the running
        # process actually picks it up instead of continuing to serve the
        # old build's code (#3472).
        results.append(
            OpsResult(
                True,
                f"nyxgpt-api: {decision}",
                outcome.warning,
                status="WARN" if outcome.warning else "",
            )
        )
        results.extend(_restart_brew_service("nyxgpt-api"))

    return results


def _install_homebrew_web(tap: str = "dkblinux98/nyxgpt-local") -> list[OpsResult]:
    """Build and install the `nyxgpt-web` Homebrew formula into `tap`, then (re)start the service.

    Generates a dist tarball vendoring the `web/` source tree (minus
    gitignored build artifacts -- see `_WEB_VENDOR_EXCLUDES`), substitutes
    its `file://` URL and sha256 into the formula template, writes the
    formula into the tap's Formula/ dir, and installs/reinstalls it only if
    the vendored source actually changed since the last install (see
    `_brew_install_or_reinstall`). The formula itself runs `npm ci`/`npm run
    build` inside the Cellar keg from that vendored source -- the installed
    app never depends on the repo checkout (#3406).

    When a rebuild actually happened (`decision` is "installed" or
    "reinstalled ..."), this restarts the service (`brew services restart`)
    instead of just starting it: `brew services start` on an already-running
    service is a no-op, so a rebuild-while-serving would otherwise leave the
    old `next start` process running against the *old* in-memory build
    manifest while the on-disk `.next` chunks are already the new build's --
    every served page then references chunk hashes that no longer exist,
    404ing (#3445). When nothing changed, a plain `start` is used so an
    already-up-to-date install doesn't bounce a healthy running service.

    Returns a list of OpsResult; fails early if brew isn't installed or the
    formula template is missing.
    """
    results: list[OpsResult] = []
    if _which("brew") is None:
        return [OpsResult(False, "Homebrew not found", "")]

    template = _homebrew_formula_template("nyxgpt-web")
    if template is None:
        # Artifact install -- see `_install_homebrew_api` above (#3759).
        return _install_from_remote_tap("nyxgpt-web")

    tap_dir = _tap_repo(tap)
    version = _read_project_version()

    tar = _create_dist_tarball(tap_dir, "nyxgpt-web", version)
    sha = _sha256_file(tar)
    url = f"file://{tar}"

    content = template.read_text(encoding="utf-8")
    content = content.replace("__NYXGPT_WEB_URL__", url)
    content = content.replace("__NYXGPT_WEB_SHA256__", sha)
    content = content.replace("__VERSION__", version)
    content = re.sub(r'version "[^"]+"', f'version "{version}"', content)

    formula_dir = tap_dir / "Formula"
    _ensure_dir(formula_dir)
    dst = formula_dir / "nyxgpt-web.rb"
    dst.write_text(content, encoding="utf-8")
    results.append(OpsResult(True, "Installed nyxgpt-web formula", str(dst)))

    try:
        outcome = _brew_install_or_reinstall(
            f"{tap}/nyxgpt-web",
            "nyxgpt-web",
            sha256=sha,
            marker_dir=tap_dir / "dist",
            version=version,
        )
    except RuntimeError as exc:
        # See `_install_homebrew_api` -- same contract, same reason (#3861).
        results.append(OpsResult(False, "Failed to install nyxgpt-web", str(exc)))
        results.extend(
            _recover_after_failed_native_install(
                "nyxgpt-web", component="web", entrypoint="nyxgpt-web"
            )
        )
        return results

    decision = outcome.decision
    if decision == "already up to date (skipped reinstall)":
        _run(["brew", "services", "start", _brew_formula_spec("nyxgpt-web")], check=False)
        results.append(OpsResult(True, f"nyxgpt-web: {decision}; requested service start", ""))
    else:
        # A new keg (and new `.next` build output) was just installed --
        # restart, not start, so the running process actually picks it up
        # instead of continuing to serve the old build's chunk manifest.
        results.append(
            OpsResult(
                True,
                f"nyxgpt-web: {decision}",
                outcome.warning,
                status="WARN" if outcome.warning else "",
            )
        )
        results.extend(_restart_brew_service("nyxgpt-web"))

    return results


# Keys in the derived container api-config that must be rewritten from their
# native (localhost) values to container-network values. Everything else --
# models, RAG tuning, secrets, and the browser-facing *_ui_url values -- is
# copied verbatim from ~/.nyxGPT/config.ini (the single source of truth); only
# service-to-service endpoints change inside the container network. Terraform
# gives its containers matching network aliases (cassandra/ollama/...) so these
# same hostnames resolve under `nyxgpt ops install --terraform --local`.
_COMPOSE_CONFIG_OVERRIDES: tuple[tuple[str, str, str], ...] = (
    ("nyxgpt", "sessions_dir", "/root/.nyxGPT/sessions"),
    ("nyxgpt", "vectorstore_dir", "/root/.nyxGPT/vectorstore"),
    ("logging", "dir", "/root/.nyxGPT/logs"),
    ("ollama", "base_url", "http://ollama:11434"),
    ("rag", "cassandra_hosts", "cassandra"),
    ("tracing", "otlp_endpoint", "http://otel-collector:4318/v1/traces"),
    ("api", "host", "0.0.0.0"),
    # NOTE: auth is intentionally NOT overridden. The `--local` deploy is
    # loopback-only (NYXGPT_BIND_ADDR defaults to 127.0.0.1) and container
    # traffic stays on the private docker network, so it mirrors the native
    # local-first setup -- auth follows the real config. If the user has
    # `[auth] enabled = true` + an api_key natively, that carries over; if it's
    # disabled (the local default), forcing it on would just reject the web's
    # own requests (it has no matching key), which is the 401/500 we hit.
)


def _generate_compose_config() -> list[OpsResult]:
    """Generate the git-ignored `docker/config.docker.ini` from the native
    `~/.nyxGPT/config.ini` (the single source of truth).

    The containerized deploys -- the observability Compose profiles and the
    terraform/kubernetes `--local` core stack -- bind-mount this file as the
    api container's `config.ini`. Rather than maintain a separate hand-edited
    file, derive it from the real local config so the container runs the
    user's actual settings, rewriting only the service-network endpoints that
    must differ inside the container network (`_COMPOSE_CONFIG_OVERRIDES`). The
    browser-facing `*_ui_url` values stay `localhost` -- they're opened from
    the host, not container-internal.

    Regenerated on every `nyxgpt ops install`/`env-sync` so native edits
    propagate. Comments and unlisted keys survive verbatim (the rewrite is
    line-based via `_patch_ini_value`). The DSN `nyxgpt ops glitchtip-init`
    writes into the native config also carries over, but -- unlike every
    other unlisted key -- it isn't copied verbatim: its host:port is rewritten
    for the container network the same way `_COMPOSE_CONFIG_OVERRIDES` rewrites
    `ollama`/`cassandra` (see `_containerized_error_tracking_dsn`), because a
    containerized api using the native, browser-facing `localhost` DSN
    silently drops every event (#3565). It can't be a static entry in
    `_COMPOSE_CONFIG_OVERRIDES` because the value carries a per-install secret
    key, not a fixed constant.

    No-ops (successfully) before `nyxgpt wizard` has created the native config.
    """
    native = Path.home() / ".nyxGPT" / "config.ini"
    if not native.exists():
        return [
            OpsResult(
                True,
                f"Skipped compose config (no {native})",
                "Run `nyxgpt wizard` to create config.ini, then re-run `nyxgpt ops install`.",
            )
        ]
    try:
        text = native.read_text(encoding="utf-8")
        for section, key, value in _COMPOSE_CONFIG_OVERRIDES:
            text = _patch_ini_value(text, section, key, value)
        try:
            dsn_parser = ConfigParser()
            dsn_parser.optionxform = str  # type: ignore[assignment]
            dsn_parser.read_string(text)
            native_dsn = dsn_parser.get("error_tracking", "dsn", fallback="").strip()
        except Exception as e:
            logger.warning(
                "Failed to parse %s while rewriting the error-tracking DSN for "
                "the container network, leaving it unrewritten: %s",
                native,
                e,
                extra={"component": "ops"},
            )
            native_dsn = ""
        if native_dsn:
            text = _patch_ini_value(
                text, "error_tracking", "dsn", _containerized_error_tracking_dsn(native_dsn)
            )
        COMPOSE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        COMPOSE_CONFIG_FILE.write_text(text, encoding="utf-8")
        # Copied verbatim from native config.ini above (see docstring): carries
        # [auth] api_key / [monitoring] grafana_admin_password when set, same
        # secret-bearing content as the native file, so it gets the same 0600.
        os.chmod(COMPOSE_CONFIG_FILE, 0o600)
    except OSError as e:
        return [
            OpsResult(
                False,
                f"Failed to generate {COMPOSE_CONFIG_FILE}",
                f"{type(e).__name__}: {e}",
            )
        ]
    return [OpsResult(True, f"Generated {COMPOSE_CONFIG_FILE} from {native}")]


def _shared_ollama_models_dir() -> Path:
    """Path native Ollama's `OLLAMA_MODELS` must point at to read the same
    models as Compose/Terraform (#3431).

    Ollama's `OLLAMA_MODELS` env var names a directory holding `blobs/` and
    `manifests/` directly (its default is `~/.ollama/models`, which has that
    layout). The shared `~/.nyxGPT/volumes/ollama` dir is bind-mounted to
    `/root/.ollama` in the Compose/Terraform `ollama` containers, which -- not
    having `OLLAMA_MODELS` overridden themselves -- store models at the
    container-default `/root/.ollama/models`, i.e. host
    `~/.nyxGPT/volumes/ollama/models`. Pointing native `OLLAMA_MODELS` at that
    same subdirectory (rather than its `~/.nyxGPT/volumes/ollama` parent)
    is what actually unifies the two stores.
    """
    d = volume_dir("ollama") / "models"
    _ensure_dir(d)
    return d


def _ollama_migration_state_path(name: str) -> Path:
    """Path to a `~/.nyxGPT/.migration-state/` marker for a one-time Ollama
    unification step (#3431) -- mirrors `_migration_marker_path`'s pattern so
    later `nyxgpt ops install` runs skip work already done rather than
    re-touching it (merged models, an already-applied env restart, ...).
    """
    state_dir = Path.home() / ".nyxGPT" / ".migration-state"
    _ensure_dir(state_dir)
    return state_dir / name


def _migrate_native_ollama_models(dest_models_dir: Path) -> list[OpsResult]:
    """One-time merge of native Ollama's own store (`~/.ollama/models`) into
    the shared `dest_models_dir`, so pointing native Ollama at the shared
    store doesn't silently orphan models already pulled natively before this
    unification (#3431).

    Additive merge, not overwrite: blobs are content-addressed by sha256
    digest (so an existing blob at the destination is always identical to
    the source one) and manifests take the destination's copy as
    authoritative on conflict -- the shared store is what Compose/Terraform
    already use, so it wins. Idempotent via a marker file, mirroring
    `migrate_legacy_volumes`. Merges via hardlink (falling back to a real
    copy if source and dest are on different filesystems), since blobs can
    be multi-GB and both paths are normally under the same `$HOME`.
    """
    marker = _ollama_migration_state_path("ollama-native-models.migrated")
    if marker.exists():
        return [OpsResult(True, "Native Ollama models: already reconciled (nothing to migrate)")]

    src_dir = Path.home() / ".ollama" / "models"
    if not src_dir.exists():
        marker.touch()
        return [OpsResult(True, "No native Ollama model store found (nothing to migrate)")]

    copied = 0
    for src_file in src_dir.rglob("*"):
        if not src_file.is_file():
            continue
        dst_file = dest_models_dir / src_file.relative_to(src_dir)
        if dst_file.exists():
            continue
        _ensure_dir(dst_file.parent)
        try:
            # Source and dest are normally both under $HOME on the same
            # filesystem, so a hardlink is instant and uses no extra disk
            # for what can be multi-GB blobs -- falls back to a real copy
            # for the (e.g. custom OLLAMA volume elsewhere) cross-device case.
            os.link(src_file, dst_file)
        except OSError:
            _copy_file(src_file, dst_file)
        copied += 1

    marker.touch()
    if copied:
        return [
            OpsResult(
                True,
                f"Merged {copied} native Ollama model file(s) into the shared store",
                f"Source: {src_dir}\nDest: {dest_models_dir}\n"
                f"The old {src_dir} files were left in place -- safe to remove by hand "
                "once you've confirmed the shared store has everything you need.",
            )
        ]
    return [OpsResult(True, "Native Ollama model store already matched the shared store")]


def _set_native_ollama_models_env(models_dir: Path) -> OpsResult:
    """Point native Ollama at `models_dir` via `launchctl setenv OLLAMA_MODELS`
    -- never a symlink (owner constraint, #3431) -- so `brew services
    start`/`restart ollama` picks it up in the user's launchd GUI session.

    `launchctl setenv` only applies to the current login session, which is
    why `nyxgpt ops install` also installs a RunAtLoad LaunchAgent
    (`_install_ollama_env_launchagent`) that reapplies it at every login.
    """
    cp = _run(["launchctl", "setenv", "OLLAMA_MODELS", str(models_dir)], check=False)
    if cp.returncode == 0:
        return OpsResult(True, f"Set OLLAMA_MODELS={models_dir} for native Ollama")
    details = _output_excerpt(cp)
    return OpsResult(False, "Failed to set OLLAMA_MODELS via launchctl setenv", details.strip())


def _ensure_ollama_service() -> list[OpsResult]:
    """Ensure the native Ollama Homebrew service is installed, running, and
    pointed at the shared model store.

    Reconciles to the intended state like `_ensure_cassandra_container`:
    already started (and already pointed at the shared store) -> no-op;
    installed but stopped -> `brew services start`; formula absent -> `brew
    install ollama` first, then start. Without this step, `ops install` only
    set up the Ollama *logs* LaunchAgent and never started Ollama itself, so
    chat/embeddings stayed down after an `ops down` until a manual `ops
    restart ollama`.

    Also unifies the native model store with Compose/Terraform's shared one
    (#3431): merges any models already pulled natively into
    `~/.nyxGPT/volumes/ollama/models`, then points native Ollama at it via
    `launchctl setenv OLLAMA_MODELS` (see `_set_native_ollama_models_env`).
    An already-running service doesn't pick up a `launchctl setenv` change on
    its own, so the first time this runs against a live service it issues a
    one-time `brew services restart` -- gated by a marker so later runs stay
    a no-op, same as every other reconciler here.
    """
    if _which("brew") is None:
        return [OpsResult(False, "Homebrew not found; cannot ensure ollama service", "")]

    results: list[OpsResult] = []
    models_dir = _shared_ollama_models_dir()
    results.extend(_migrate_native_ollama_models(models_dir))

    env_marker = _ollama_migration_state_path("ollama-native-env.configured")
    env_already_applied = env_marker.exists()
    env_result = _set_native_ollama_models_env(models_dir)
    results.append(env_result)
    if env_result.ok:
        env_marker.touch()

    state = _brew_services_snapshot().get("ollama")

    if state == "started":
        if env_already_applied:
            results.append(OpsResult(True, "Ollama brew service already running"))
            return results
        cp = _run(["brew", "services", "restart", "ollama"], check=False)
        if cp.returncode == 0:
            results.append(
                OpsResult(True, "Restarted brew service: ollama (to pick up shared OLLAMA_MODELS)")
            )
        else:
            details = _output_excerpt(cp)
            results.append(
                OpsResult(False, "Failed to restart brew service: ollama", details.strip())
            )
        return results

    if state is None:
        cp = _run(["brew", "install", "ollama"], check=False)
        if cp.returncode != 0:
            details = _output_excerpt(cp)
            results.append(OpsResult(False, "Failed to brew install ollama", details.strip()))
            return results
        results.append(OpsResult(True, "Installed ollama formula"))

    cp = _run(["brew", "services", "start", "ollama"], check=False)
    if cp.returncode == 0:
        results.append(OpsResult(True, "Started brew service: ollama"))
    else:
        details = _output_excerpt(cp)
        results.append(OpsResult(False, "Failed to start brew service: ollama", details.strip()))
    return results


# --- Linux native (systemd) install (#3508) ---
#
# Linux twin of the Homebrew-services + launchd section above. There's no
# Homebrew Cellar to build inside, so `_install_native_api_systemd`/
# `_install_native_web_systemd` create their own self-contained install roots
# under ~/.nyxGPT/opt/<component> (a plain venv for the api, a built `web/`
# tree for the web UI) instead -- reusing `_create_dist_tarball` (already
# OS-agnostic: it just vendors source into a tarball) rather than duplicating
# it. `ollama` is managed as its own systemd --user unit (`nyxgpt-ollama.service`)
# so every native component is reachable through the same `systemctl --user`
# surface -- including on a host where the distro's system-wide
# `ollama.service` holds port 11434, which install disables to free it (#3632).


def _systemd_user_dir() -> Path:
    """Return `~/.config/systemd/user`, the systemd --user unit search path."""
    return Path.home() / ".config" / "systemd" / "user"


def _find_systemd_unit_template(name: str) -> tuple[Path | None, list[Path]]:
    """Locate a systemd unit template (by filename) among the packaged
    resources `_sync_packaged_resources` synced to `OPS_SYSTEMD_TEMPLATES_DIR`.

    Mirrors `_find_launchagent_template`'s search strategy. Returns
    (path_or_none, candidates_checked).
    """
    candidates = [OPS_SYSTEMD_TEMPLATES_DIR / name]
    for p in candidates:
        try:
            if p.exists():
                return p, candidates
        except Exception as e:
            logger.warning(
                "Could not check candidate path %s, skipping: %s",
                p,
                e,
                extra={"component": "ops"},
            )
            continue
    return None, candidates


def _install_systemd_unit_from_template(
    tpl: Path, dst: Path, *, substitutions: dict[str, str] | None = None
) -> None:
    """Render a systemd unit template to `dst`.

    Substitutes `LAUNCHAGENT_HOME_PLACEHOLDER` (the same `__NYXGPT_HOME__`
    placeholder the launchd plist templates use) with the installing user's
    actual home directory, plus any unit-specific `substitutions` (e.g. the
    resolved `ollama` binary path for nyxgpt-ollama.service).
    """
    _ensure_dir(dst.parent)
    text = tpl.read_text(encoding="utf-8")
    text = text.replace(LAUNCHAGENT_HOME_PLACEHOLDER, str(Path.home()))
    for placeholder, value in (substitutions or {}).items():
        text = text.replace(placeholder, value)
    dst.write_text(text, encoding="utf-8")


def _reload_and_activate_systemd_unit(unit: str) -> list[OpsResult]:
    """`daemon-reload`, enable, then restart (or start) a systemd --user unit.

    Mirrors the launchd installers' bootout+bootstrap+kickstart pattern: a
    changed unit file must be reloaded before systemd picks it up, `enable`
    makes it start at every login (the launchd RunAtLoad equivalent), and
    `restart` (rather than `start`) ensures a just-rebuilt venv/web bundle is
    actually picked up instead of leaving an already-running process serving
    stale code -- `systemctl restart` on a unit that isn't running yet is
    equivalent to `start` (mirrors the brew-service restart-vs-start fix from
    #3472/#3445).
    """
    results: list[OpsResult] = []
    cp = _run(["systemctl", "--user", "daemon-reload"], check=False)
    if cp.returncode != 0:
        details = _output_excerpt(cp)
        results.append(OpsResult(False, "systemctl --user daemon-reload failed", details.strip()))
        return results

    _run(["systemctl", "--user", "enable", unit], check=False, expected=True)

    cp = _run(["systemctl", "--user", "restart", unit], check=False)
    if cp.returncode == 0:
        results.append(OpsResult(True, f"Started/restarted systemd unit: {unit}"))
    else:
        details = _output_excerpt(cp)
        results.append(OpsResult(False, f"Failed to start systemd unit: {unit}", details.strip()))
    return results


def _install_cassandra_logs_systemd_unit() -> list[OpsResult]:
    """Install and (re)start the Cassandra log-follower systemd --user unit.

    Linux twin of `_install_cassandra_launchagent`: locates the unit template
    in the repo, copies it into ~/.config/systemd/user/, then reloads and
    (re)starts it. Returns a single-element list of OpsResult (plus the
    reload/activate results); fails if the template can't be found.
    """
    results: list[OpsResult] = []
    tpl, checked = _find_systemd_unit_template("nyxgpt-cassandra-logs.service")
    if tpl is None:
        details = (
            "Tried:\n"
            + "\n".join(str(p) for p in checked)
            + "\nRun `nyxgpt ops install` first to sync packaged templates into "
            f"{NYXGPT_HOME}."
        )
        results.append(OpsResult(False, "Missing Cassandra logs systemd unit template", details))
        return results
    dst = _systemd_user_dir() / tpl.name
    _install_systemd_unit_from_template(tpl, dst)
    results.append(OpsResult(True, "Installed Cassandra logs systemd unit", str(dst)))
    results.extend(_reload_and_activate_systemd_unit("nyxgpt-cassandra-logs.service"))
    return results


def _install_ollama_logs_systemd_unit() -> list[OpsResult]:
    """Install and (re)start the Ollama log-follower systemd --user unit.

    Linux twin of `_install_ollama_launchagent`. Installed unconditionally by
    `nyxgpt ops install` regardless of deployment mode, same as the Cassandra
    unit -- `follow-ollama-logs.sh` (which this unit runs) handles both
    Compose mode (follows the `nyxgpt-ollama` container) and native mode
    (tails ~/.nyxGPT/logs/ollama-native.log, nyxgpt-ollama.service's own
    stdout) on its own (see #3441).
    """
    results: list[OpsResult] = []
    tpl, checked = _find_systemd_unit_template("nyxgpt-ollama-logs.service")
    if tpl is None:
        details = (
            "Tried:\n"
            + "\n".join(str(p) for p in checked)
            + "\nRun `nyxgpt ops install` first to sync packaged templates into "
            f"{NYXGPT_HOME}."
        )
        results.append(OpsResult(False, "Missing Ollama logs systemd unit template", details))
        return results
    dst = _systemd_user_dir() / tpl.name
    _install_systemd_unit_from_template(tpl, dst)
    results.append(OpsResult(True, "Installed Ollama logs systemd unit", str(dst)))
    results.extend(_reload_and_activate_systemd_unit("nyxgpt-ollama-logs.service"))
    return results


def _native_install_root(component: str) -> Path:
    """Return `~/.nyxGPT/opt/<component>`, creating it if needed.

    The install root used in place of a Homebrew Cellar keg -- holds the
    component's venv/build plus the wrapper script its systemd unit (Linux)
    or launchd agent (macOS dev mode) execs. Linux native mode uses it for
    both install modes; macOS uses it only in dev mode, since artifact mode
    there builds inside a keg (#3789).
    """
    p = Path.home() / ".nyxGPT" / "opt" / component
    _ensure_dir(p)
    return p


def _venv_site_packages(venv_dir: Path) -> Path | None:
    """Return a venv's `site-packages` directory, or None if the venv wasn't created."""
    matches = sorted((venv_dir / "lib").glob("python3.*/site-packages"))
    return matches[0] if matches else None


# nyxGPT's `requires-python` floor, as a literal: a service venv has to be
# built *before* anything is installed into it, so ops cannot read the
# metadata of the package it is about to install.
# `tests/unit/test_ops_service_python.py` asserts this stays in step with
# pyproject.toml's `requires-python`.
_MIN_SERVICE_PYTHON = (3, 11)

# Explicitly-versioned interpreter names to look for on PATH, newest first,
# when the interpreter ops itself runs under is below the floor. Bare
# `python3` is tried last and only if it satisfies the floor: on Amazon Linux
# 2023 it is 3.9, which is exactly how #3782 shipped a service venv that pip
# then refused ("requires a different Python: 3.9.16 not in '>=3.11'").
_SERVICE_PYTHON_NAMES = ("python3.13", "python3.12", "python3.11", "python3")


def _interpreter_version(exe: str) -> tuple[int, int] | None:
    """Return `exe`'s `(major, minor)`, or None if it cannot be run.

    Asks the interpreter itself rather than parsing its filename: a
    `python3.11` on PATH may be a wrapper, a symlink to something else, or
    not executable at all.
    """
    cp = _run(
        [exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        check=False,
        expected=True,
    )
    if cp.returncode != 0:
        return None
    parts = (cp.stdout or "").strip().split(".")
    if len(parts) != 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def _running_interpreter() -> tuple[str, tuple[int, int]]:
    """Return the interpreter ops itself runs under, and its `(major, minor)`.

    Read from `sys` rather than by running a subprocess -- and factored out
    so a smoke/test can stand in a different interpreter for it.
    """
    return (sys.executable, (sys.version_info[0], sys.version_info[1]))


def _service_python_candidates() -> list[tuple[str, tuple[int, int]]]:
    """Return the interpreters that could build a service venv, best first.

    Order:

    1. `sys.executable` -- the interpreter ops itself runs under. It is
       already known to satisfy `requires-python` (it is running nyxGPT), and
       on a cloud instance it is the one provisioning installed for exactly
       that reason. Its version is read from `sys.version_info` rather than a
       subprocess, so selection costs nothing in the common case.
    2. `python3.13`/`python3.12`/`python3.11` from PATH, newest first.
    3. Bare `python3`, last -- it is the distro's choice, not nyxGPT's.

    Every entry carries the version actually reported by that interpreter;
    entries below the floor are kept (the caller names them in its failure
    message) and duplicates of the same real path are dropped.
    """
    candidates: list[tuple[str, tuple[int, int]]] = []
    seen: set[str] = set()

    def _add(exe: str | None, version: tuple[int, int] | None) -> None:
        if not exe:
            return
        try:
            key = os.path.realpath(exe)
        except OSError:  # pragma: no cover - realpath on a live path
            key = exe
        if key in seen:
            return
        seen.add(key)
        if version is None:
            version = _interpreter_version(exe)
        if version is None:
            return
        candidates.append((exe, version))

    _add(*_running_interpreter())
    for name in _SERVICE_PYTHON_NAMES:
        _add(_which(name), None)
    return candidates


def _format_python_version(version: tuple[int, int]) -> str:
    """Render a `(major, minor)` interpreter version for an operator-facing message."""
    return f"{version[0]}.{version[1]}"


def _no_service_python_details(candidates: list[tuple[str, tuple[int, int]]]) -> str:
    """Explain a failed interpreter search, naming found and required versions."""
    floor = _format_python_version(_MIN_SERVICE_PYTHON)
    if candidates:
        found = "\n".join(f"  {exe} (Python {_format_python_version(v)})" for exe, v in candidates)
    else:
        found = "  (no working Python interpreter found on PATH)"
    return (
        f"nyxGPT requires Python >= {floor}; none of the interpreters on this machine qualify.\n"
        f"Found:\n{found}\n"
        f"Install a Python >= {floor} and re-run `nyxgpt ops install`:\n"
        "  Amazon Linux / Fedora / RHEL: sudo dnf install -y python3.11 python3.11-pip\n"
        "  Debian / Ubuntu:              sudo apt-get install -y python3.11 python3.11-venv"
    )


def _create_service_venv(venv_dir: Path, service: str) -> OpsResult:
    """Create `venv_dir` with an interpreter that satisfies `requires-python`.

    Bare `python3` is never assumed sufficient (#3782): Amazon Linux 2023's
    is 3.9, so `python3 -m venv` there produced a venv pip then refused to
    install nyxGPT into -- a failure that surfaced as an opaque `pip install
    ... (rc=1)` at the end of a cloud deploy.

    Candidates are tried in `_service_python_candidates` order and the first
    one that both satisfies the floor *and* successfully creates the venv
    wins -- so a `python3.11` that is present but has no working `venv`/
    `ensurepip` (Debian's split `python3.11-venv` package) falls through to
    the next candidate instead of failing the install.
    """
    candidates = _service_python_candidates()
    usable = [(exe, v) for exe, v in candidates if v >= _MIN_SERVICE_PYTHON]
    if not usable:
        return OpsResult(
            False,
            f"No Python >= {_format_python_version(_MIN_SERVICE_PYTHON)} "
            f"available to create the {service} venv",
            _no_service_python_details(candidates),
        )

    failures: list[str] = []
    for exe, version in usable:
        cp = _run([exe, "-m", "venv", str(venv_dir)], check=False)
        if cp.returncode == 0:
            return OpsResult(
                True,
                f"Created {service} venv with Python {_format_python_version(version)}",
                f"{exe} -> {venv_dir}",
            )
        failures.append(
            f"{exe} (Python {_format_python_version(version)}):\n{_output_excerpt(cp).strip()}"
        )

    return OpsResult(False, f"Failed to create {service} venv", "\n".join(failures))


def _write_executable(path: Path, content: str) -> None:
    """Write `content` to `path` and mark it executable (0o755)."""
    _ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o755)


# Wrapper script templates for the Linux native services -- plain-string
# (not f-string) templates so the embedded Python heredocs' own `{...}`
# formatting doesn't need brace-escaping; `__NYXGPT_API_VENV__`/
# `__NYXGPT_WEB_ROOT__` are substituted via `str.replace` at install time.
# Mirror homebrew/nyxgpt-api.rb's and nyxgpt-web.rb's `bin/nyxgpt-*` wrapper
# scripts: read host/port (and, for web, the api_base_url override) from
# ~/.nyxGPT/config.ini, then exec the real process.
_NATIVE_API_WRAPPER_TEMPLATE = """#!/bin/bash
set -euo pipefail

CONFIG_FILE="$HOME/.nyxGPT/config.ini"
SYS_PY="$(command -v python3 || echo /usr/bin/python3)"

HOST="127.0.0.1"
PORT="8000"

if [ -f "$CONFIG_FILE" ]; then
  IFS=$'\\t' read -r HOST PORT < <("$SYS_PY" - <<'PY'
import configparser
import os

cfg = configparser.ConfigParser()
cfg.read(os.path.expanduser('~/.nyxGPT/config.ini'), encoding='utf-8')

host = cfg.get('api', 'host', fallback='127.0.0.1')
try:
    port = str(cfg.getint('api', 'port', fallback=8000))
except Exception:
    port = '8000'

print(f"{host}\\t{port}")
PY
)
fi

echo "nyxgpt-api starting (__NYXGPT_API_MODE__)" >&2
echo "  host: $HOST" >&2
echo "  port: $PORT" >&2

exec "__NYXGPT_API_VENV__/bin/python3" -m uvicorn nyxgpt.app:app --host "$HOST" --port "$PORT"
"""

_NATIVE_WEB_WRAPPER_TEMPLATE = """#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE="$HOME/.nyxGPT/config.ini"
SYS_PY="$(command -v python3 || echo /usr/bin/python3)"

HOST="127.0.0.1"
PORT="3000"
API_BASE=""
AUTH_KEY=""

if [ -f "$CONFIG_FILE" ]; then
  IFS=$'\\t' read -r HOST PORT API_BASE AUTH_KEY < <("$SYS_PY" - <<'PY'
import configparser
import os

cfg = configparser.ConfigParser()
cfg.read(os.path.expanduser('~/.nyxGPT/config.ini'), encoding='utf-8')

host = cfg.get('web', 'host', fallback='127.0.0.1')
try:
    port = str(cfg.getint('web', 'port', fallback=3000))
except Exception:
    port = '3000'
api_base = cfg.get('web', 'api_base_url', fallback='')
# The proxy in web/src/lib/apiProxy.ts attaches X-API-Key from
# NYXGPT_AUTH_API_KEY; without it every proxied call 401s the moment
# [auth] enabled is turned on (#3632).
try:
    auth_on = cfg.getboolean('auth', 'enabled', fallback=False)
except Exception:
    auth_on = False
api_key = cfg.get('auth', 'api_key', fallback='').strip() if auth_on else ''

print(f"{host}\\t{port}\\t{api_base}\\t{api_key}")
PY
)
fi

export HOST="$HOST"
export PORT="$PORT"
if [ -n "$API_BASE" ]; then
  export NEXT_PUBLIC_API_BASE="$API_BASE"
fi
if [ -n "$AUTH_KEY" ]; then
  export NYXGPT_AUTH_API_KEY="$AUTH_KEY"
fi

echo "nyxgpt-web starting (__NYXGPT_WEB_MODE__)" >&2
echo "  host: $HOST" >&2
echo "  port: $PORT" >&2

cd "__NYXGPT_WEB_ROOT__"
exec __NYXGPT_WEB_START_CMD__
"""

# `npm run dev` rather than `npm run start`: dev mode's whole point is that
# the running web UI is the working tree, so it serves through Next's dev
# server (which compiles from `<checkout>/web` on demand) instead of a
# production bundle that would have to be rebuilt to see an edit.
#
# BOTH commands pass host/port explicitly, and that is load-bearing rather
# than tidy: *neither* `next dev` nor `next start` reads the `HOST` env var
# this wrapper exports (Next reads `HOSTNAME`, and for `next start` the
# documented control is `-H/--hostname`). An earlier version passed them only
# on the dev command, and the comment here named the hazard while fixing one
# caller of it. The consequence was not cosmetic: on the artifact path -- the
# repo-less default, i.e. every real install -- `next start` fell back to its
# own default and bound `0.0.0.0`, so `[web] host = 127.0.0.1` was read from
# config, exported, and silently discarded.
#
# That contradicted `DECISION_PRIVATE_ACCESS_MECHANISM.md` in its own words
# ("Nothing is ever listening on a non-loopback address on the deployments"),
# and it removed a defence-in-depth layer the decision deliberately chose over
# the network-restricted-public-bind alternative it compared against. On a
# cloud instance the security group still fronted it (TCP 22 only); on a local
# native install nothing did, and `[auth] enabled` defaults to false. Found by
# owner acceptance testing on 2026-08-26 (`ss -lntp` on the EC2 instance showed
# `*:3000` against the deploy's own claim of a loopback bind).
#
# The guard is
# `tests/unit/test_ops_dev_mode.py::test_both_web_start_commands_bind_the_configured_host`,
# which asserts the flags per mode rather than on dev alone.
_WEB_START_HOST_ARGS = '-- --hostname "$HOST" --port "$PORT"'
_DEV_WEB_START_CMD = f"npm run dev {_WEB_START_HOST_ARGS}"
_ARTIFACT_WEB_START_CMD = f"npm run start {_WEB_START_HOST_ARGS}"


def _write_native_api_wrapper(root: Path, venv_dir: Path, *, dev: bool) -> Path:
    """Write the `nyxgpt-api` wrapper script the systemd unit / launchd agent execs.

    Same script either way -- it execs uvicorn from `venv_dir` -- since the
    difference between the modes lives in what that venv contains: a
    non-editable install of the vendored/published source (artifact mode) or
    an editable install pointing at the checkout (dev mode). Only the
    startup banner differs, so `nyxgpt ops logs api` says which one is
    running. Returns the wrapper path.
    """
    wrapper = root / "bin" / "nyxgpt-api"
    content = _NATIVE_API_WRAPPER_TEMPLATE.replace("__NYXGPT_API_VENV__", str(venv_dir)).replace(
        "__NYXGPT_API_MODE__",
        "dev mode: editable venv" if dev else "self-contained venv",
    )
    _write_executable(wrapper, content)
    return wrapper


def _write_native_web_wrapper(root: Path, web_root: Path, *, dev: bool) -> Path:
    """Write the `nyxgpt-web` wrapper script the systemd unit / launchd agent execs.

    `web_root` is the built bundle in artifact mode (`npm run start`) and the
    checkout's `web/` directory in dev mode (`npm run dev`). Both commands
    carry `--hostname`/`--port` from config.ini -- see `_WEB_START_HOST_ARGS`
    for why that is required on each and not just on dev. Returns the wrapper
    path.
    """
    wrapper = root / "bin" / "nyxgpt-web"
    content = (
        _NATIVE_WEB_WRAPPER_TEMPLATE.replace("__NYXGPT_WEB_ROOT__", str(web_root))
        .replace(
            "__NYXGPT_WEB_MODE__",
            "dev mode: Next dev server on the checkout" if dev else "self-contained build",
        )
        .replace(
            "__NYXGPT_WEB_START_CMD__",
            _DEV_WEB_START_CMD if dev else _ARTIFACT_WEB_START_CMD,
        )
    )
    _write_executable(wrapper, content)
    return wrapper


def _install_and_activate_native_systemd_unit(service: str) -> list[OpsResult]:
    """Render `<service>.service` into ~/.config/systemd/user, then (re)start it.

    Shared by the artifact and dev install paths (#3789): the unit file is
    identical in both modes -- it execs
    `~/.nyxGPT/opt/<service>/bin/<service>`, and only the *contents* of that
    wrapper differ -- so switching modes on Linux is a wrapper rewrite plus a
    restart, with no unit churn. Returns a list of OpsResult; the first is a
    failure if the packaged template hasn't been synced into `NYXGPT_HOME`.
    """
    unit_file = f"{service}.service"
    tpl, checked = _find_systemd_unit_template(unit_file)
    if tpl is None:
        details = (
            "Tried:\n"
            + "\n".join(str(p) for p in checked)
            + "\nRun `nyxgpt ops install` first to sync packaged templates into "
            f"{NYXGPT_HOME}."
        )
        return [OpsResult(False, f"Missing {service} systemd unit template", details)]
    dst = _systemd_user_dir() / tpl.name
    _ensure_dir(Path.home() / ".nyxGPT" / "logs")
    _install_systemd_unit_from_template(tpl, dst)
    results = [OpsResult(True, f"Installed {service} systemd unit", str(dst))]
    results.extend(_reload_and_activate_systemd_unit(unit_file))
    return results


def _install_native_api_systemd() -> list[OpsResult]:
    """Build and install a self-contained venv for `nyxgpt-api` on Linux, then
    (re)start its systemd --user unit.

    Linux twin of `_install_homebrew_api`: takes the `nyxgpt-api` source
    tarball (`pyproject.toml` + `src/nyxgpt/` + `example.config.ini`) into
    `~/.nyxGPT/opt/nyxgpt-api`, creates a venv there with an interpreter that
    satisfies nyxGPT's `requires-python` (`_create_service_venv`, #3782 --
    the distro's bare `python3` is 3.9 on Amazon Linux 2023 and pip refuses
    the artifact in a venv built from it), `pip install`s
    that tarball into it, copies `example.config.ini` next to the installed
    package (`config_wizard`'s schema-source resolution finds it there with
    no repo root above the venv, mirroring the brew formula's own fix,
    #3406 -- an artifact install carries it as package data instead, so
    there is nothing to copy), writes the wrapper script the systemd unit
    execs, then installs/reloads the unit.

    The tarball is vendored from the checkout on a dev machine and
    downloaded from the published release asset on an artifact install
    (`_service_source_tarball`, #3759) -- the install recipe below is the
    same either way.

    Unlike `_install_homebrew_api`'s sha256-gated skip-if-unchanged
    (`_brew_install_or_reinstall`), this always rebuilds -- a slower but
    simpler first Linux implementation; `nyxgpt ops install` is not a hot
    path. Returns a list of OpsResult.
    """
    results: list[OpsResult] = []
    root = _native_install_root("nyxgpt-api")
    version = _native_service_version()
    try:
        tar = _service_source_tarball(root, "nyxgpt-api", version)
    except RuntimeError as e:
        return [OpsResult(False, "Could not obtain the nyxgpt-api artifact", str(e))]
    venv_dir = root / "venv"

    venv_result = _create_service_venv(venv_dir, "nyxgpt-api")
    results.append(venv_result)
    if not venv_result.ok:
        return results

    pip = str(venv_dir / "bin" / "pip")
    _run([pip, "install", "--upgrade", "pip"], check=False)
    cp = _run([pip, "install", str(tar)], check=False)
    if cp.returncode != 0:
        details = _output_excerpt(cp)
        results.append(OpsResult(False, "Failed to pip install nyxgpt-api", details.strip()))
        return results
    results.append(
        OpsResult(True, "Installed nyxgpt-api into a self-contained venv", str(venv_dir))
    )

    site_packages = _venv_site_packages(venv_dir)
    example_config = REPO_ROOT / "example.config.ini"
    if site_packages is not None and example_config.exists():
        nyxgpt_pkg_dir = site_packages / "nyxgpt"
        if nyxgpt_pkg_dir.exists():
            _copy_file(example_config, nyxgpt_pkg_dir / "example.config.ini")

    wrapper = _write_native_api_wrapper(root, venv_dir, dev=False)
    results.append(OpsResult(True, "Installed nyxgpt-api wrapper script", str(wrapper)))

    results.extend(_install_and_activate_native_systemd_unit("nyxgpt-api"))
    return results


def _install_native_web_systemd() -> list[OpsResult]:
    """Build and install a self-contained web build for `nyxgpt-web` on Linux,
    then (re)start its systemd --user unit.

    Linux twin of `_install_homebrew_web`: takes the `nyxgpt-web` source
    tarball (the `web/` tree minus its gitignored build artifacts, see
    `_WEB_VENDOR_EXCLUDES`) -- vendored from the checkout on a dev machine,
    downloaded from the published release asset on an artifact install
    (`_service_source_tarball`, #3759) -- extracts it into a staging
    directory, runs `npm ci`/`npm run build` inside it, then -- only once that build has
    succeeded -- swaps it into `~/.nyxGPT/opt/nyxgpt-web/build` in place of
    the previous one, writes the wrapper script the systemd unit execs, and
    installs/reloads the unit. Always rebuilds -- see
    `_install_native_api_systemd`'s docstring for why this doesn't do
    `_install_homebrew_web`'s sha256-gated skip.

    The build/swap is staged rather than done in place so a failed `npm ci`/
    `npm run build` (transient registry issue, disk pressure, OOM, ...)
    leaves the previous, still-running build untouched -- the live
    `nyxgpt-web.service` wrapper keeps `cd`ing into a build that still
    exists instead of a path a failed rebuild rmtree'd out from under it (a
    real outage-on-restart bug found in review, #3508).

    Returns a list of OpsResult; fails early if `npm` isn't on PATH.
    """
    if _which("npm") is None:
        return [OpsResult(False, "npm not found; cannot install nyxgpt-web", "")]

    results: list[OpsResult] = []
    root = _native_install_root("nyxgpt-web")
    version = _native_service_version()
    try:
        tar = _service_source_tarball(root, "nyxgpt-web", version)
    except RuntimeError as e:
        return [OpsResult(False, "Could not obtain the nyxgpt-web artifact", str(e))]

    build_dir = root / "build"
    staging_dir = root / "build.staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    _ensure_dir(staging_dir)
    with tarfile.open(tar) as tf:
        # Trusted input: `tar` was just built by `_create_dist_tarball` above
        # from this repo's own `web/` tree, not from an external/user source.
        tf.extractall(staging_dir, filter="data")
    staged_extracted = staging_dir / f"nyxgpt-web-{version}"

    npm = _which("npm") or "npm"
    for step_name, npm_args in (("npm ci", ["ci"]), ("npm run build", ["run", "build"])):
        try:
            cp = subprocess.run(
                [npm, *npm_args],
                cwd=str(staged_extracted),
                text=True,
                capture_output=True,
                check=False,
            )
        except Exception as e:
            shutil.rmtree(staging_dir, ignore_errors=True)
            results.append(
                OpsResult(
                    False, f"Failed to run {step_name} for nyxgpt-web", f"{type(e).__name__}: {e}"
                )
            )
            return results
        if cp.returncode != 0:
            shutil.rmtree(staging_dir, ignore_errors=True)
            results.append(
                OpsResult(
                    False,
                    f"{step_name} failed for nyxgpt-web",
                    _output_excerpt(cp),
                )
            )
            return results

    # The new build is known-good -- swap it into place. Two renames (both
    # fast, same-filesystem) instead of an rmtree-then-build so the previous
    # build is never gone from `build_dir` for longer than the swap itself.
    old_dir = root / "build.old"
    if old_dir.exists():
        shutil.rmtree(old_dir)
    if build_dir.exists():
        build_dir.rename(old_dir)
    staging_dir.rename(build_dir)
    if old_dir.exists():
        shutil.rmtree(old_dir, ignore_errors=True)
    extracted = build_dir / f"nyxgpt-web-{version}"
    results.append(OpsResult(True, "Built nyxgpt-web production bundle", str(extracted)))

    wrapper = _write_native_web_wrapper(root, extracted, dev=False)
    results.append(OpsResult(True, "Installed nyxgpt-web wrapper script", str(wrapper)))

    results.extend(_install_and_activate_native_systemd_unit("nyxgpt-web"))
    return results


def _system_ollama_service_active() -> bool:
    """True if a distro-managed system-wide `ollama.service` is currently active.

    The official Ollama Linux installer (`curl -fsSL https://ollama.com/install.sh
    | sh`) auto-enables and starts this unit, bound to 127.0.0.1:11434 -- the
    same port `nyxgpt-ollama.service` (nyxGPT's own systemd --user unit)
    needs, so the two can never both hold the port at once (#3632: observed
    in CI as `nyxgpt-ollama.service` crash-looping, "ollama serve" exiting
    immediately with the port already taken). Checked with plain `systemctl
    is-active` (system scope, no `--user`), since the installer's unit runs
    system-wide, not per-user. False (never raises) if systemctl is missing
    or the query fails -- an unknown/absent system unit is not a conflict.
    """
    if _which("systemctl") is None:
        return False
    cp = _run(["systemctl", "is-active", "ollama.service"], check=False, expected=True)
    return (cp.stdout or "").strip() == "active"


def _system_ollama_service_enabled() -> bool:
    """True if the system-wide `ollama.service` is enabled to start at boot.

    Checked alongside `_system_ollama_service_active()` because a merely
    *enabled* (currently stopped) system unit is just as much of a conflict:
    it would grab port 11434 back from `nyxgpt-ollama.service` at the next
    boot. `is-enabled` also reports "enabled" for a unit that is masked-free
    but stopped, which is precisely the state we still want to disable.

    "static" is deliberately *not* treated as a conflict: a static unit has no
    `[Install]` section, so `systemctl disable` on it is a no-op -- reporting
    it would leave `ops doctor` flagging a conflict nothing can ever clear.
    A static unit is also not started at boot on its own, so it only matters
    while it is running, which `_system_ollama_service_active()` already
    covers.
    """
    if _which("systemctl") is None:
        return False
    cp = _run(["systemctl", "is-enabled", "ollama.service"], check=False, expected=True)
    return (cp.stdout or "").strip() in {"enabled", "enabled-runtime"}


def _system_ollama_service_conflicts() -> bool:
    """True if a distro-managed system-wide `ollama.service` would contend for port 11434."""
    return _system_ollama_service_active() or _system_ollama_service_enabled()


def _takeover_system_ollama_service() -> tuple[bool, list[OpsResult]]:
    """Stop and disable a pre-existing system-wide `ollama.service` so
    `nyxgpt-ollama.service` can own port 11434.

    Chosen reconciliation for #3632 (documented in docs/systemd.md): nyxGPT
    manages Ollama itself, the same operator-facing way it manages api/web,
    rather than deferring to whatever the official Ollama installer
    (`curl -fsSL https://ollama.com/install.sh | sh`) left enabled. An
    earlier round of this fix *adopted* the system unit instead; that was
    rejected in acceptance testing because it leaves the machine in a split
    arrangement nyxgpt neither owns nor can restart, and leaves
    `OLLAMA_MODELS` pointed at Ollama's own store rather than the shared
    `~/.nyxGPT/volumes/ollama/models` one every other mode bind-mounts.

    Stopping a *system*-scope unit needs root, which is done via
    `_privileged_run`'s never-prompting `sudo -n` (see its docstring).
    Returns `(port_free, results)`: `port_free` is False when the system
    unit is still holding the port -- the caller must then NOT install or
    start `nyxgpt-ollama.service`, since it could only crash-loop, which is
    the exact failure this issue was filed for.
    """
    results: list[OpsResult] = []
    cp = _privileged_run(["systemctl", "disable", "--now", "ollama.service"])
    if cp is None or cp.returncode != 0:
        details = ""
        if cp is not None:
            details = _output_excerpt(cp)
        return False, [
            OpsResult(
                False,
                "System-wide ollama.service holds port 11434 and could not be disabled",
                "nyxgpt manages Ollama itself via nyxgpt-ollama.service, which cannot "
                "start while the distro's system unit owns the port. Root was not "
                "available without a password prompt, so free the port by hand and "
                "re-run install:\n"
                "  sudo systemctl disable --now ollama.service && nyxgpt ops install"
                + (f"\n{details}" if details else ""),
            )
        ]
    results.append(
        OpsResult(
            True,
            "Stopped and disabled system-wide ollama.service",
            "Freed 127.0.0.1:11434 for nyxgpt-ollama.service, which nyxgpt manages "
            "itself (pointed at the shared ~/.nyxGPT/volumes/ollama/models store). "
            "See docs/systemd.md#ollama.",
        )
    )
    # `disable --now` returns as soon as systemd accepts the job; the socket
    # can still be held for a moment while `ollama serve` shuts down, and
    # nyxgpt-ollama.service would lose the race and crash-loop exactly as
    # before. Wait for the port to actually come free.
    ollama_port = COMPOSE_COMPONENT_PORTS["ollama"]
    if not _wait_for_port_free("127.0.0.1", ollama_port):
        return False, [
            *results,
            OpsResult(
                False,
                f"Port {ollama_port} is still in use after disabling system ollama.service",
                "Something else is serving on it (another Ollama process, a container). "
                "Free it, then re-run `nyxgpt ops install`.",
            ),
        ]
    return True, results


def _wait_for_port_free(host: str, port: int, timeout: float = 15.0) -> bool:
    """Poll until nothing accepts TCP connections on `host:port`, or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex((host, port)) != 0:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


# --- Ollama bootstrap (Linux) ---
#
# Ollama's only supported Linux distribution channel is the install script it
# publishes at this URL (the same one docs/systemd.md used to hand the
# operator to paste). ops fetches and runs it rather than printing it: on
# macOS `_ensure_ollama_service` already runs `brew install ollama` for the
# operator, so a Linux path that stops and says "install it first" is not the
# "same or equivalent commands" the platform is specified to offer (#3508
# acceptance), and it is the same defect #3724 was filed for on kind.
OLLAMA_INSTALL_SCRIPT_URL = "https://ollama.com/install.sh"

_OLLAMA_MANUAL_INSTALL_HINT = (
    "Install Ollama yourself, then re-run `nyxgpt ops install`:\n"
    "  curl -fsSL https://ollama.com/install.sh | sh\n"
    "Other options (rootless tarball, distro packages): "
    "https://ollama.com/download/linux"
)


def _install_linux_ollama() -> list[OpsResult]:
    """Install Ollama on Linux with its official installer, for the operator.

    The Linux counterpart of the `brew install ollama` that
    `_ensure_ollama_service` already performs on macOS. Mirrors
    `_ensure_docker_engine`'s contract for the same reason: a missing
    prerequisite `nyxgpt ops install` can resolve on its own is ops's job,
    not a command handed back to the operator (CLAUDE.md's Operational
    Command Wrapping rule, and #3724's precedent).

    The installer writes to `/usr/local/bin` and registers a system unit, so
    it needs root -- taken through `_privileged_run`'s never-prompting `sudo
    -n`, exactly like the Docker engine bootstrap. Root that isn't available
    without a password is reported with the command to run by hand rather
    than hung on a TTY prompt.

    The installer also enables a *system-wide* `ollama.service` bound to port
    11434. That is expected and handled: the caller runs the
    `_takeover_system_ollama_service` reconciliation afterwards, so nyxGPT's
    own `nyxgpt-ollama.service` ends up owning the port and the shared model
    store (#3632).

    Never fatal to the rest of install: a host that genuinely can't have
    Ollama still gets its api/web/cassandra pieces reconciled.
    """
    try:
        resp = httpx.get(OLLAMA_INSTALL_SCRIPT_URL, follow_redirects=True, timeout=60.0)
        resp.raise_for_status()
        script = resp.text
    except httpx.HTTPError as e:
        return [
            OpsResult(
                False,
                "Could not download the Ollama installer",
                f"{OLLAMA_INSTALL_SCRIPT_URL}: {type(e).__name__}: {e}\n"
                f"{_OLLAMA_MANUAL_INSTALL_HINT}",
            )
        ]

    with tempfile.TemporaryDirectory(prefix="nyxgpt-ollama-install-") as tmpdir:
        script_path = Path(tmpdir) / "install.sh"
        script_path.write_text(script, encoding="utf-8")
        cp = _privileged_run(["sh", str(script_path)])

    if cp is None:
        return [
            OpsResult(
                False,
                "Ollama is not installed and root is not available without a password",
                _OLLAMA_MANUAL_INSTALL_HINT,
            )
        ]
    if cp.returncode != 0:
        return [
            OpsResult(
                False,
                "Could not install Ollama automatically",
                (_output_excerpt(cp) + "\n" + _OLLAMA_MANUAL_INSTALL_HINT).strip(),
            )
        ]
    # The installer puts the binary in /usr/local/bin, which a minimal
    # non-login shell's PATH does not always carry -- and a successful
    # install this process still can't invoke is not a success.
    if _which("ollama") is None:
        return [
            OpsResult(
                False,
                "Ollama installer succeeded but `ollama` is still not on PATH",
                "Expected it in /usr/local/bin. Add that directory to PATH and re-run "
                "`nyxgpt ops install`.",
            )
        ]
    return [OpsResult(True, "Installed Ollama", _cp_details(cp))]


def _ensure_ollama_installed() -> list[OpsResult]:
    """Ensure the `ollama` binary exists before the native Ollama service needs it.

    No-op when it is already on PATH (the common case, and every re-run).
    Linux installs it (`_install_linux_ollama`); macOS does not reach here at
    all, since `_ensure_ollama_service` installs the formula as part of its
    own reconcile.
    """
    if _which("ollama") is not None:
        return []
    if not _is_linux():
        return [
            OpsResult(
                False,
                "ollama not found on PATH",
                "nyxgpt cannot install Ollama automatically on "
                f"{platform.system()}. See https://ollama.com/download.",
            )
        ]
    return _install_linux_ollama()


def _install_native_ollama_systemd() -> list[OpsResult]:
    """Ensure native Ollama is installed as the `nyxgpt-ollama.service` systemd
    --user unit, pointed at the shared model store.

    Linux twin of `_ensure_ollama_service`, including its install half: a
    missing `ollama` binary is installed with the official installer
    (`_ensure_ollama_installed`) the same way macOS's twin runs `brew install
    ollama`, rather than stopping to tell the operator to run it themselves
    (#3508 acceptance). That runs *before* the port-conflict reconciliation
    below, because the installer itself enables the system-wide
    `ollama.service` whose port this unit then has to take over. Migrates any models already pulled into Ollama's
    own default store into the shared one (`_migrate_native_ollama_models`,
    OS-agnostic, #3431), then installs/reloads a unit whose `Environment=`
    already bakes in `OLLAMA_MODELS` -- no separate env-refresh unit is
    needed the way launchd needs a RunAtLoad LaunchAgent (`Environment=`
    applies on every unit start, unlike `launchctl setenv`'s per-session
    scope; see ops/systemd/nyxgpt-ollama.service).

    If a system-wide `ollama.service` already holds port 11434, it is stopped
    and disabled first so this unit can take the port over (#3632; see
    `_takeover_system_ollama_service`). When that takeover can't be
    completed, the nyxgpt unit is deliberately NOT installed or started --
    it could only crash-loop against the port -- and the failure is reported
    with the command that frees it.
    """
    # Install first, take the port over second: the official installer
    # enables a system-wide `ollama.service` on the very port this unit
    # needs, so a takeover performed before the install would check a
    # conflict that does not exist yet and miss the one it creates.
    results: list[OpsResult] = _ensure_ollama_installed()
    if any(not r.ok for r in results):
        return results

    if _system_ollama_service_conflicts():
        port_free, takeover_results = _takeover_system_ollama_service()
        results.extend(takeover_results)
        if not port_free:
            return results

    ollama_bin = _which("ollama")
    if ollama_bin is None:
        return [
            *results,
            OpsResult(False, "ollama not found on PATH", _OLLAMA_MANUAL_INSTALL_HINT),
        ]

    models_dir = _shared_ollama_models_dir()
    results.extend(_migrate_native_ollama_models(models_dir))

    tpl, checked = _find_systemd_unit_template("nyxgpt-ollama.service")
    if tpl is None:
        details = (
            "Tried:\n"
            + "\n".join(str(p) for p in checked)
            + "\nRun `nyxgpt ops install` first to sync packaged templates into "
            f"{NYXGPT_HOME}."
        )
        results.append(OpsResult(False, "Missing nyxgpt-ollama systemd unit template", details))
        return results
    dst = _systemd_user_dir() / tpl.name
    _ensure_dir(Path.home() / ".nyxGPT" / "logs")
    _install_systemd_unit_from_template(
        tpl, dst, substitutions={"__NYXGPT_OLLAMA_BIN__": ollama_bin}
    )
    results.append(OpsResult(True, "Installed nyxgpt-ollama systemd unit", str(dst)))
    results.extend(_reload_and_activate_systemd_unit("nyxgpt-ollama.service"))
    return results


def _systemd_services_snapshot() -> dict[str, str]:
    """Return {unit_name: state} for nyxGPT-managed systemd --user units.

    Mirrors `_brew_services_snapshot()`'s vocabulary ("started" for a live
    unit) *and* its "not in this dict" == "not installed" contract: a unit
    is only included if its file actually exists in
    `~/.config/systemd/user/` -- `systemctl --user is-active` on a
    never-installed unit name reports "inactive"/"unknown" the same way it
    would for an installed-but-stopped one, so checking activity alone would
    misreport a fresh machine that's never run `nyxgpt ops install` as
    having every native component "down" instead of simply absent. Empty on
    any failure (systemctl not on PATH, no session bus reachable).
    """
    if _which("systemctl") is None:
        return {}
    unit_dir = _systemd_user_dir()
    snapshot: dict[str, str] = {}
    for unit in NATIVE_SYSTEMD_SERVICES.values():
        if not (unit_dir / f"{unit}.service").exists():
            continue
        cp = _run(
            ["systemctl", "--user", "is-active", f"{unit}.service"],
            check=False,
            expected=True,
            timeout=LOCAL_PROBE_TIMEOUT_SECONDS,
        )
        state = (cp.stdout or "").strip()
        snapshot[unit] = "started" if state == "active" else "none"
    return snapshot


def _native_services_snapshot() -> dict[str, str]:
    """{component: state} for the OS-appropriate native service manager.

    Used by `detect_deployment_mode()` in place of a direct
    `_brew_services_snapshot()` call so it reflects whichever native path
    (Homebrew/launchd or systemd) actually applies on this host.

    The macOS branch resolves each component's service name against what
    `brew services list` actually reports rather than indexing the stable
    name, so `nyxgpt ops status` reports the candidate channel's
    `nyxgpt-api@<line>rc` service under `api` instead of the `none` it used
    to print on every rc install (#3853). Before this, the only place that
    behavior was ever mentioned was a line `ops install` printed once, which
    an operator running `status` days later never saw.
    """
    if _is_macos():
        brew_snapshot = _brew_services_snapshot()
        snapshot = {
            component: brew_snapshot.get(brew_name, "none")
            for component, brew_name in brew_services.resolve_all(
                NATIVE_BREW_SERVICES, brew_snapshot
            ).items()
        }
        if read_install_mode().is_dev:
            # A dev install runs api/web under its own LaunchAgents, not
            # `brew services` -- reading brew here would report "none" (or,
            # worse, a leftover keg's state) for the processes actually
            # serving (#3789). ollama is unchanged: it is a brew service in
            # both modes.
            for component, label in DEV_LAUNCHD_LABELS.items():
                snapshot[component] = "started" if _launchd_agent_loaded(label) else "none"
        return snapshot
    if _is_linux():
        systemd_snapshot = _systemd_services_snapshot()
        return {
            component: systemd_snapshot.get(unit, "none")
            for component, unit in NATIVE_SYSTEMD_SERVICES.items()
        }
    return dict.fromkeys(NATIVE_BREW_SERVICES, "none")


def _restart_systemd_service(unit: str) -> list[OpsResult]:
    """Restart systemd --user unit `unit` via `systemctl --user restart`.

    Returns a single-element list: an OpsResult reporting systemctl missing,
    the restart command's success, or its failure with captured stdout/stderr.
    """
    if _which("systemctl") is None:
        return [OpsResult(False, f"systemctl not found; cannot restart {unit}")]
    try:
        cp = _run(["systemctl", "--user", "restart", f"{unit}.service"], check=False)
        if cp.returncode == 0:
            return [OpsResult(True, f"Restarted systemd unit: {unit}")]
        details = _output_excerpt(cp)
        return [OpsResult(False, f"Failed to restart systemd unit: {unit}", details.strip())]
    except Exception as e:
        return [
            OpsResult(False, f"Failed to restart systemd unit: {unit}", f"{type(e).__name__}: {e}")
        ]


def _stop_systemd_service(unit: str) -> list[OpsResult]:
    """Stop systemd --user unit `unit` via `systemctl --user stop`.

    Returns a single-element list: an OpsResult reporting systemctl missing,
    the stop command's success, or its failure with captured stdout/stderr.
    """
    if _which("systemctl") is None:
        return [OpsResult(False, f"systemctl not found; cannot stop {unit}")]
    try:
        cp = _run(["systemctl", "--user", "stop", f"{unit}.service"], check=False)
        if cp.returncode == 0:
            return [OpsResult(True, f"Stopped systemd unit: {unit}")]
        details = _output_excerpt(cp)
        return [OpsResult(False, f"Failed to stop systemd unit: {unit}", details.strip())]
    except Exception as e:
        return [
            OpsResult(False, f"Failed to stop systemd unit: {unit}", f"{type(e).__name__}: {e}")
        ]


# --- Dev-mode native install (#3789) ---
#
# Dev mode installs the same stack topology as the artifact path -- native
# api/web service wrappers, ollama, the Cassandra container, observability --
# and differs in exactly two places: what the api venv contains (an editable
# install of the checkout instead of a vendored/published tarball) and what
# the web wrapper runs (`npm run dev` in `<checkout>/web` instead of `npm run
# start` against a built bundle). Everything below is the machinery for
# those two differences plus the macOS service manager they need, since a
# dev install has no keg for `brew services` to attach to.


def _dev_mode_unavailable(component: str) -> OpsResult:
    """The failure returned when `--dev` is used without a source checkout."""
    return OpsResult(
        False,
        f"Dev mode needs a source checkout; cannot install {component}",
        "`--dev` builds the api and web UI from a checkout's working tree, and this "
        f"nyxgpt is running from an installed package ({REPO_ROOT} has no "
        "pyproject.toml/src/nyxgpt/web). Run `nyxgpt up --dev` from a clone, or drop "
        "`--dev` to install the published artifacts.",
    )


def _install_dev_launchagent(component: str) -> list[OpsResult]:
    """Install and (re)load the dev-mode launchd agent for `api`/`web` on macOS.

    macOS's artifact path runs api/web as `brew services`, which exist only
    because the keg's formula declares them -- dev mode has no keg, so it
    runs the wrapper scripts under its own LaunchAgents instead (the same
    mechanism the Cassandra/Ollama log followers already use). The plists
    are packaged templates like every other ops resource, so
    `_sync_packaged_resources` has already put them where
    `_find_launchagent_template` looks.
    """
    label = DEV_LAUNCHD_LABELS[component]
    results: list[OpsResult] = []
    tpl, checked = _find_launchagent_template(f"{label}.plist")
    if tpl is None:
        details = (
            "Tried:\n"
            + "\n".join(str(p) for p in checked)
            + "\nRun `nyxgpt ops install` first to sync packaged templates into "
            f"{NYXGPT_HOME}."
        )
        return [OpsResult(False, f"Missing {label} LaunchAgent template", details)]

    la_dir = Path.home() / "Library" / "LaunchAgents"
    _ensure_dir(la_dir)
    _ensure_dir(NYXGPT_HOME / "logs")
    dst = la_dir / tpl.name
    _install_launchagent_from_template(tpl, dst)

    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", domain, str(dst)], check=False, expected=True)
    _run(["launchctl", "bootstrap", domain, str(dst)], check=False)
    # kickstart -k, not just bootstrap: an agent that was already loaded from
    # a previous dev install must actually restart to pick up the rebuilt
    # venv / current working tree, the same reason the artifact path
    # restarts rather than starts its brew service (#3472).
    cp = _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)
    if cp.returncode != 0:
        results.append(
            OpsResult(False, f"Failed to start LaunchAgent: {label}", _output_excerpt(cp).strip())
        )
        return results
    results.append(OpsResult(True, f"Installed and started LaunchAgent: {label}", str(dst)))
    return results


def _activate_native_dev_service(component: str) -> list[OpsResult]:
    """(Re)start the dev-mode `api`/`web` service via the OS-appropriate manager."""
    if _is_macos():
        return _install_dev_launchagent(component)
    if _is_linux():
        return _install_and_activate_native_systemd_unit(NATIVE_SYSTEMD_SERVICES[component])
    return _unsupported_os_result(f"native {component} dev install")


def _install_native_api_dev() -> list[OpsResult]:
    """Install the `api` service as an editable venv on the checkout, then (re)start it.

    Creates (or reuses) the venv under `~/.nyxGPT/opt/nyxgpt-api/venv` and
    `pip install -e <checkout>` into it, so the running service imports
    `nyxgpt` straight out of `src/nyxgpt/` -- a `git pull` plus a restart is
    the whole update path, with no tarball, no formula and no keg build in
    between (#3789). The wrapper script and service unit/agent are the same
    ones the artifact path installs; only the venv's contents differ.

    The venv is built through `_create_service_venv`, the same interpreter
    selection the artifact path uses (#3782): `pip install -e` honours
    `requires-python` exactly as installing the tarball does, so a bare
    `python3` that is below the floor fails a dev install the same way it
    failed a cloud deploy.

    Returns a list of OpsResult.
    """
    checkout = _dev_checkout_root()
    if checkout is None:
        return [_dev_mode_unavailable("nyxgpt-api")]

    results: list[OpsResult] = []
    root = _native_install_root("nyxgpt-api")
    venv_dir = root / "venv"

    venv_result = _create_service_venv(venv_dir, "nyxgpt-api")
    results.append(venv_result)
    if not venv_result.ok:
        return results

    pip = str(venv_dir / "bin" / "pip")
    _run([pip, "install", "--upgrade", "pip"], check=False)
    cp = _run([pip, "install", "-e", str(checkout)], check=False)
    if cp.returncode != 0:
        results.append(
            OpsResult(
                False,
                "Failed to pip install -e the nyxgpt checkout",
                _output_excerpt(cp).strip(),
            )
        )
        return results
    results.append(
        OpsResult(True, f"Installed nyxgpt-api (editable) from {checkout}", str(venv_dir))
    )

    wrapper = _write_native_api_wrapper(root, venv_dir, dev=True)
    results.append(OpsResult(True, "Installed nyxgpt-api wrapper script (dev mode)", str(wrapper)))
    results.extend(_activate_native_dev_service("api"))
    return results


def _install_native_web_dev() -> list[OpsResult]:
    """Point the `web` service at the checkout's Next dev server, then (re)start it.

    No vendoring, no `npm run build`, no keg: the wrapper `cd`s into
    `<checkout>/web` and runs `npm run dev`, so the served UI is the working
    tree and an edit shows up without reinstalling anything (#3789).
    `node_modules` is primed earlier in the same install by
    `_ensure_web_deps` (which runs `npm ci` in the checkout's `web/`), so a
    missing one here means that step failed and is reported as such rather
    than silently starting a dev server that cannot boot.

    Returns a list of OpsResult.
    """
    checkout = _dev_checkout_root()
    if checkout is None:
        return [_dev_mode_unavailable("nyxgpt-web")]
    if _which("npm") is None:
        return [OpsResult(False, "npm not found; cannot install nyxgpt-web", "")]

    web_dir = checkout / "web"
    if not (web_dir / "node_modules").is_dir():
        return [
            OpsResult(
                False,
                "Web dependencies are missing; cannot start the dev server",
                f"{web_dir / 'node_modules'} does not exist -- the `web deps` install step "
                "(npm ci) must succeed before the dev-mode web service can run.",
            )
        ]

    results: list[OpsResult] = []
    root = _native_install_root("nyxgpt-web")
    wrapper = _write_native_web_wrapper(root, web_dir, dev=True)
    results.append(
        OpsResult(True, f"Installed nyxgpt-web wrapper script (dev mode, {web_dir})", str(wrapper))
    )
    results.extend(_activate_native_dev_service("web"))
    return results


def _launchagents_dir() -> Path:
    """Return `~/Library/LaunchAgents`, where every user LaunchAgent lives.

    Both nyxGPT's own `com.nyxgpt.*` plists and Homebrew's
    `homebrew.mxcl.*` service plists land here, which is why the uninstall
    teardown can clear a brew service whose formula brew can no longer even
    resolve (#3859).
    """
    return Path.home() / "Library" / "LaunchAgents"


def _remove_launchagents(labels: dict[str, str], kind: str) -> list[OpsResult]:
    """Unload and delete each LaunchAgent in `labels` (macOS).

    Unload *and* delete, in that order: a plist left in
    ~/Library/LaunchAgents is reloaded at the next login, so a teardown that
    only boots the job out reinstates it the next time the operator logs in.
    `kind` names the population in the result lines ("dev-mode", "log/env")
    so a run that clears several of them says which agent came from where.

    Idempotent by construction: `_stop_launchagent` reports an unloaded job
    as success, and an absent plist is simply not unlinked -- teardown runs
    routinely against half-removed machines.
    """
    results: list[OpsResult] = []
    la_dir = _launchagents_dir()
    for component, label in labels.items():
        plist = la_dir / f"{label}.plist"
        results.extend(_stop_launchagent(label))
        if plist.exists():
            try:
                plist.unlink()
                results.append(
                    OpsResult(True, f"Removed {kind} LaunchAgent for {component}", str(plist))
                )
            except OSError as e:
                results.append(
                    OpsResult(
                        False,
                        f"Failed to remove {kind} LaunchAgent for {component}",
                        f"{type(e).__name__}: {e}",
                    )
                )
    return results


def _remove_dev_launchagents() -> list[OpsResult]:
    """Unload and delete the dev-mode api/web LaunchAgents (macOS).

    Teardown only (#3859): `uninstall` clears these because a still-loaded
    agent with `KeepAlive` outlives the install it belonged to and holds
    ports 8000/3000 against whatever is installed next.

    It is *not* the mode-switch cleanup any more (#3861). Until then this
    function and `_stop_artifact_brew_services` were the two hand-written
    halves of one transition pair (dev <-> artifact), and their existence as
    a *pair* is what made artifact-to-artifact invisible: there was no third
    function, so there was no third case. `_retire_previous_identity` now
    subtracts the target's own services from everything registered, which
    covers all three directions and any future one; the brew half was
    deleted with the gate that called it, and this half survives only for
    the teardown caller it also had.
    """
    return _remove_launchagents(DEV_LAUNCHD_LABELS, "dev-mode")


def _remove_support_launchagents() -> list[OpsResult]:
    """Unload and delete the log-follower and Ollama-env LaunchAgents (macOS).

    The population `brew uninstall` structurally cannot reach: Homebrew never
    knew these existed, because `nyxgpt ops install` wrote them itself
    (`SUPPORT_LAUNCHD_LABELS`). Until #3859 nothing removed them at all, in
    any mode, which is why `com.nyxgpt.ollama-logs` outlived the owner's
    complete uninstall.
    """
    return _remove_launchagents(SUPPORT_LAUNCHD_LABELS, "log/env")


def _target_brew_formula(name: str) -> str:
    """The formula this machine's next native install of `name` will register.

    The same branch `_install_homebrew_api`/`_install_homebrew_web` take: a
    checkout carrying `homebrew/<name>.rb` builds a local `file://` tap and
    installs the plain stable formula; without one they fall through to
    `_install_from_remote_tap`, which installs the published channel's
    formula for the running version -- `nyxgpt-api@3.0.0rc` on a candidate.

    Derived from the installer's own condition rather than guessed, because
    `_stop_superseded_brew_services` uses it to decide what to stop: a wrong
    answer here stops the service the install is about to start.
    """
    if (REPO_ROOT / "homebrew" / f"{name}.rb").exists():
        return name
    return _remote_tap_formula(name, _native_service_version())


def _stop_superseded_brew_services() -> list[OpsResult]:
    """Stop api/web brew services from a *different* formula than this install's.

    The competing-install half of #3853. A prior release's `nyxgpt-api` keg
    from 2.1.0 was still installed, with its service still registered, when
    the candidate install registered `nyxgpt-api@3.0.0rc` beside it. Homebrew
    treats a differently-named formula as unrelated software, so nothing
    removed it, and both formulas declare `keep_alive true`: launchd
    relaunched the loser of the :8000 race every few seconds, indefinitely,
    filing `[Errno 48] address already in use` into the error tracker the
    whole time.

    It is a step of its own rather than part of `_reconcile_install_mode`
    because of **when** it runs, not what it knows. Since #3861 the reconcile
    subtracts the target's own services from everything registered, so it
    sees this formula change too -- the two overlap by design, and stopping
    an already-stopped service is a no-op. But the reconcile records the new
    identity as part of the install, while this runs *ahead* of the api/web
    install steps, which is where the port has to be free. (Before #3861 the
    reconcile really was blind here: it gated on the install **mode**
    changing, and the owner's machine never changed mode. That gate is gone;
    this step no longer depends on its absence.)

    Runs *before* the api/web install steps, so the port is free when the
    service this install owns starts. Only the service is stopped -- the keg
    is left installed, because removing software the operator installed is
    not an install's call to make, and `nyxgpt ops uninstall` is the command
    that does it.

    Packaging is the first line of defence here, not this: since #3853 both
    channels' published formulas declare `conflicts_with` each other, so brew
    refuses the second install outright (`scripts/build_homebrew_artifacts.py`).
    This is the fallback for machines already in the bad state, and for the
    local `file://` tap path, whose checked-in formulas are not stamped by
    that script.

    A stop that fails is reported but does not fail the install: the very
    next steps install and start the service this run owns, and *they* say
    whether the port was actually free. Failing here instead would block an
    install on a leftover the operator may already have removed by hand --
    a diagnostic that fails an install is a diagnostic that gets skipped.
    """
    if not _is_macos() or _which("brew") is None:
        return []
    if read_install_mode().is_dev:
        # A dev install owns no brew service at all, so "the one this install
        # owns" has no referent and `keep` would be a formula name nothing
        # registered. The `install mode` step above already retired every
        # api/web brew service for this target (#3861); re-deriving a
        # superseded set from a keep that does not exist would only invent
        # findings about services it has just stopped.
        return []

    snapshot = _brew_services_snapshot()
    results: list[OpsResult] = []
    for component, service in (("api", "nyxgpt-api"), ("web", "nyxgpt-web")):
        keep = _target_brew_formula(service)
        stale = brew_services.superseded(service, snapshot, keep=keep)
        if not stale:
            continue
        results.append(
            OpsResult(
                True,
                f"{component}: {len(stale)} superseded brew service(s) registered besides {keep}",
                f"{brew_services.format_variants(service, snapshot)}\n"
                f"Stopping {', '.join(stale)}: a service from another formula holds this "
                "component's port and launchd restarts it (keep_alive), so leaving it "
                "registered makes both builds fight for it (#3853). The keg itself is "
                "left installed -- `nyxgpt ops uninstall` removes it.",
            )
        )
        for name in stale:
            for stopped in _stop_brew_service(name):
                results.append(
                    stopped
                    if stopped.ok
                    else OpsResult(True, stopped.message, stopped.details, status="NOTE")
                )
    return results


def _drop_stale_api_venv() -> list[OpsResult]:
    """Delete `~/.nyxGPT/opt/nyxgpt-api/venv` so the new mode builds it clean.

    Both modes install into the same venv path, and installing one over the
    other in place leaves the previous mode's `nyxgpt` distribution in
    site-packages (an editable `.pth`/finder, or a plain copy) racing the new
    one for the import. Rebuilding from empty is a few seconds and removes
    the whole class of "which nyxgpt is the service actually running?"
    ambiguity.
    """
    venv_dir = _native_install_root("nyxgpt-api") / "venv"
    if not venv_dir.exists():
        return []
    try:
        shutil.rmtree(venv_dir)
    except OSError as e:
        return [
            OpsResult(
                False,
                "Failed to remove the previous mode's nyxgpt-api venv",
                f"{venv_dir}: {type(e).__name__}: {e}",
            )
        ]
    return [OpsResult(True, "Removed the previous mode's nyxgpt-api venv", str(venv_dir))]


def _native_artifact_service_name(name: str) -> str:
    """The service name the artifact path will register `name` under.

    macOS: brew names a service after its formula, and the formula depends on
    which tap the install uses -- a checkout builds the local `file://` tap,
    which always carries the plain `nyxgpt-api`/`nyxgpt-web`, while an
    artifact install resolves the published tap's channel formula, where a
    candidate is `nyxgpt-api@3.0.0rc` (`_remote_tap_formula`). Both branches
    are read from the same predicate the installer branches on
    (`_homebrew_formula_template`), never re-derived.
    """
    if _homebrew_formula_template(name) is not None:
        return name
    return _remote_tap_formula(name, _native_service_version())


def _native_install_identity(dev: bool) -> InstallIdentity:
    """The identity `install(dev=...)` is about to put on this machine (#3861).

    Identity detection lives here rather than in `nyxgpt.install_mode`
    because it needs the tap/formula/version routing above, and that module
    must stay import-free of `ops` (`ops` imports it, and `self_heal` reads
    it without importing `ops`).

    The per-OS shapes are genuinely different, and flattening them is what
    made this defect possible in the first place:

    - **macOS artifact** -- `brew`, with the formula name per component, so
      `nyxgpt-api` and `nyxgpt-api@3.0.0rc` are different identities.
    - **macOS dev** -- `launchd`, with the `com.nyxgpt.*` labels.
    - **Linux (both modes)** -- `systemd`, and the units are the *same*
      `nyxgpt-api`/`nyxgpt-web` in either mode, so there the manager and the
      service names do not disambiguate anything and the version/channel
      fields carry the whole signal.
    """
    version = _native_service_version()
    if dev:
        services = dict(DEV_LAUNCHD_LABELS) if _is_macos() else _linux_api_web_units()
        return InstallIdentity.build(
            mode=INSTALL_MODE_DEV,
            manager=MANAGER_LAUNCHD if _is_macos() else MANAGER_SYSTEMD,
            services=services,
            version=version,
            channel=CHANNEL_DEV,
            checkout=_dev_checkout_root(),
        )
    channel = CHANNEL_CANDIDATE if "rc" in version else CHANNEL_STABLE
    if _is_macos():
        manager = MANAGER_BREW
        services = {
            component: _native_artifact_service_name(NATIVE_BREW_SERVICES[component])
            for component in ("api", "web")
        }
    elif _is_linux():
        manager, services = MANAGER_SYSTEMD, _linux_api_web_units()
    else:
        # No native service manager on this OS -- `_unsupported_os_result`
        # will say so. Record what is still true (mode, version, channel) and
        # name no services rather than inventing macOS's.
        manager, services = MANAGER_UNKNOWN, {}
    return InstallIdentity.build(
        mode=INSTALL_MODE_ARTIFACT,
        manager=manager,
        services=services,
        version=version,
        channel=channel,
    )


def _linux_api_web_units() -> dict[str, str]:
    """The systemd --user units carrying api/web (identical in both modes)."""
    return {component: NATIVE_SYSTEMD_SERVICES[component] for component in ("api", "web")}


def _discover_native_services() -> list[tuple[str, str]]:
    """`(manager, service name)` for every nyxGPT api/web service on this machine.

    The answer to an *unknown* previous identity (#3861): a machine with no
    marker, or one written before identities were recorded, may still be
    carrying services from an install nothing described -- which is exactly
    the owner's Mac, where two keg pairs and a LaunchAgent set were all
    registered at once. Reading the managers is the only way to find them.

    macOS only looks like the general case: `brew services list` names every
    keg's service including a candidate's `nyxgpt-api@3.0.0rc`, and dev's
    LaunchAgents are found as plists on disk rather than as loaded jobs,
    because an unloaded plist with `KeepAlive` is reloaded at the next login
    and is therefore still a live claim on the port. Linux contributes
    nothing: both modes drive the same two unit names, so anything found
    there is already the target's own and would be filtered out below.
    """
    if not _is_macos():
        return []
    found: list[tuple[str, str]] = [
        (MANAGER_BREW, name)
        # `nyxgpt-` covers both channels' formulas for both components and
        # excludes `ollama`, which is the same brew service in every mode.
        # `brew services list` lists every keg that *has* a service file,
        # registered or not, so a name here is a candidate and
        # `_brew_row_is_a_live_registration` decides.
        for name, state in sorted(_brew_services_snapshot().items())
        if name.startswith("nyxgpt-") and _brew_row_is_a_live_registration(name, state)
    ]
    la_dir = _launchagents_dir()
    found.extend(
        (MANAGER_LAUNCHD, label)
        for label in sorted(DEV_LAUNCHD_LABELS.values())
        if (la_dir / f"{label}.plist").exists()
    )
    return found


def _brew_row_is_a_live_registration(name: str, state: str) -> bool:
    """Whether a `brew services list` row is a live claim on this component's port.

    `started` is decisive on its own: brew is reporting a running job. `none`
    with no plist at brew's conventional path is decisive the other way --
    brew says nothing is registered and there is no file for launchd to load,
    so a keg that is merely installed is not reported as if it were running
    and nothing is asked of launchd.

    Everything between them (`error <code>`, `stopped`, `scheduled`,
    `unknown`) is the ambiguity #3861 tripped on, twice and in both
    directions. `error 3` is the crash-looping keg the owner's Mac had, and it
    is a *registered* service: the state word answers "is it running", never
    "will launchd start it again". (A column-based read also reported services
    launchd had already forgotten, runs 32222041921 and 32228088507; run
    32233162053 traced that to ANSI-coloured state text rather than to a stale
    column, and the escapes are stripped at the parser now --
    `_brew_service_will_restart`.) So those states are settled at the launchd
    level rather than read off the column:
    otherwise `doctor` names a service the last `nyxgpt up` retired and
    prescribes re-running the retire that already worked.
    """
    if state == "started":
        return True
    if (_launchagents_dir() / f"homebrew.mxcl.{name}.plist").exists():
        return True
    if state == "none":
        return False
    return _brew_service_is_registered(name)


def _retire_service(manager: str, name: str) -> list[OpsResult]:
    """Stop `name` under `manager` so it stops competing for ports 8000/3000.

    Stop and de-register, never uninstall: a keg or unit left in place is
    harmless once nothing starts it, and removing it is a teardown decision
    (#3859), not a reconcile one. What is *not* harmless is a registered
    service -- `brew services` and launchd's `KeepAlive` both restart their
    job at login, which is how the owner's machine kept two apis fighting
    over port 8000 for seven hours.
    """
    if manager == MANAGER_BREW:
        return _stop_brew_service(name)
    if manager == MANAGER_LAUNCHD:
        results = _stop_launchagent(name)
        plist = _launchagents_dir() / f"{name}.plist"
        if plist.exists():
            try:
                plist.unlink()
                results.append(OpsResult(True, f"Removed LaunchAgent: {name}", str(plist)))
            except OSError as e:
                results.append(
                    OpsResult(
                        False,
                        f"Failed to remove LaunchAgent: {name}",
                        f"{type(e).__name__}: {e}",
                    )
                )
        return results
    if manager == MANAGER_SYSTEMD:
        return _stop_systemd_service(name)
    return [
        OpsResult(
            True,
            f"Previous install registered {name} under an unknown service manager",
            "Not stopped -- nyxGPT does not know how. Stop it by hand if it holds a port.",
        )
    ]


def _retire_previous_identity(
    previous: InstallIdentity, target: InstallIdentity
) -> list[OpsResult]:
    """Stop whatever the previous identity registered that the target will not.

    One rule, not a table of transition pairs (the acceptance criterion is
    explicit about this, and a pair list is what produced the defect): take
    everything registered that is not the target's own service, and stop it.
    That single subtraction covers dev -> artifact (launchd labels retire),
    artifact -> dev (brew services retire), stable -> candidate
    (`nyxgpt-api` retires while `nyxgpt-api@3.0.0rc` starts), candidate ->
    stable, and any future artifact form, because none of them is a special
    case of anything.

    "Everything registered" is the recorded previous identity **union** what
    the service managers actually report, and the union is load-bearing
    rather than belt-and-braces. Each half alone misses a real machine: the
    marker alone misses an install nothing recorded -- the owner's Mac
    carried *four* identities and one marker -- and the managers alone miss
    a previous install whose services are currently stopped but still
    installed, which `brew services` restarts at the next login. The
    target's own services are excluded from both halves, so an install never
    retires what it is about to start.

    Called on every install, including one whose recorded identity already
    matches the target: the discovery half is the only thing that can see a
    service no marker ever described, and that population does not appear
    only on the runs where the identity changed.
    """
    keep = set(target.service_names)
    stale = dict.fromkeys(
        [(previous.manager, name) for name in previous.service_names] + _discover_native_services()
    )
    results: list[OpsResult] = []
    for manager, name in stale:
        if name in keep:
            continue
        results.extend(_retire_service(manager, name))
    return results


def _reconcile_install_mode(dev: bool) -> list[OpsResult]:
    """Record the install identity this run targets, retiring the previous one.

    Runs before the api/web install steps so those steps -- and every
    `restart`/`stop`/`status` afterwards -- see the identity the machine is
    actually being reconciled to.

    The gate is a whole-identity comparison, not `previous.mode != target`
    (#3861). That older gate could not see an artifact-to-artifact switch at
    all: installing `nyxgpt-api@3.0.0rc` over `nyxgpt-api` 2.1.0 wrote
    `artifact` where `artifact` already stood, both kegs kept their
    `keep_alive` services registered on ports 8000/3000, and the resulting
    crash loop was invisible to every layer that consults this marker. It
    was not a lax check -- it was the strongest check a two-value model can
    support, which is why the model changed rather than the condition.

    An *unknown* previous identity (no marker, or one written before #3861)
    is treated as a possible mismatch and reconciled defensively, never as
    "the same": reading unknown as unchanged is today's failure exactly.

    What the comparison gates is the *reporting* of the change and the venv
    rebuild it implies. The retire itself is unconditional, because a marker
    that matches the target says nothing about what is actually registered
    beside it -- see the comment on the `_retire_previous_identity` call.
    """
    previous = read_install_mode()
    target_identity = _native_install_identity(dev)
    target = INSTALL_MODE_DEV if dev else INSTALL_MODE_ARTIFACT
    checkout = _dev_checkout_root() if dev else None
    results: list[OpsResult] = []

    differences = previous.identity.differences(target_identity)
    if differences:
        results.append(
            OpsResult(
                True,
                f"Install identity changing: {previous.identity.detail()} "
                f"-> {target_identity.detail()}",
                "\n".join(differences),
            )
        )
    # Subtracting the target's own services from everything registered runs
    # on *every* install, not only when the recorded identity changed. A
    # matching marker is not evidence that the machine matches it: a retire
    # that failed last time, a keg's service started by hand, an install made
    # outside `nyxgpt ops` at all -- each leaves a foreign service registered
    # under a marker that already says "this is what is installed". Gating
    # the subtraction on `differences` made `doctor`'s own remedy ("re-run
    # `nyxgpt up` to retire the ones that are not this install's") a no-op in
    # exactly the states doctor fires in. The cost is one `brew services
    # list` per install, and on a machine with nothing foreign registered the
    # loop retires nothing.
    results.extend(_retire_previous_identity(previous.identity, target_identity))
    if differences and _identity_change_invalidates_api_venv(previous.identity, target_identity):
        results.extend(_drop_stale_api_venv())

    state = InstallModeState(
        mode=target,
        checkout=str(checkout) if checkout else None,
        recorded=True,
        identity=target_identity,
    )
    marker = write_install_mode(target, checkout, identity=target_identity)
    results.append(OpsResult(True, f"Install mode: {state.label()}", str(marker)))
    return results


def _identity_change_invalidates_api_venv(
    previous: InstallIdentity, target: InstallIdentity
) -> bool:
    """Whether `~/.nyxGPT/opt/nyxgpt-api/venv` must be rebuilt from empty.

    The venv is shared between modes at one path, so a mode, manager or
    service-name change can leave the previous install's `nyxgpt`
    distribution in site-packages racing the new one for the import
    (`_drop_stale_api_venv`). A *version* change within the same names is not
    that hazard: it is the ordinary upgrade, which
    `_install_native_api_systemd` performs by pip-installing the new tarball
    into that same venv. Rebuilding there would add minutes to every Linux
    upgrade to remove a race that cannot happen.

    An unknown previous identity gets the rebuild, because it might be any of
    the above -- and it costs nothing on a machine with no venv, which is
    every genuinely fresh install.
    """
    if not previous.known:
        return True
    return (
        previous.mode != target.mode
        or previous.manager != target.manager
        or previous.service_names != target.service_names
    )


def _install_native_api(dev: bool = False) -> list[OpsResult]:
    """Install/update the native `api` service via the OS-appropriate mechanism.

    `dev=True` installs the editable-checkout variant on either OS instead of
    the artifact path's Homebrew keg (macOS) / tarball venv (Linux) -- see
    `_install_native_api_dev` (#3789).
    """
    if dev:
        return _install_native_api_dev()
    if _is_macos():
        return _install_homebrew_api()
    if _is_linux():
        return _install_native_api_systemd()
    return _unsupported_os_result("native api install")


def _install_native_web(dev: bool = False) -> list[OpsResult]:
    """Install/update the native `web` service via the OS-appropriate mechanism.

    `dev=True` runs the checkout's Next dev server instead of a built bundle
    -- see `_install_native_web_dev` (#3789).
    """
    if dev:
        return _install_native_web_dev()
    if _is_macos():
        return _install_homebrew_web()
    if _is_linux():
        return _install_native_web_systemd()
    return _unsupported_os_result("native web install")


def _ensure_native_ollama_service() -> list[OpsResult]:
    """Ensure the native `ollama` service is installed/running via the OS-appropriate mechanism."""
    if _is_macos():
        return _ensure_ollama_service()
    if _is_linux():
        return _install_native_ollama_systemd()
    return _unsupported_os_result("native ollama service")


def _ensure_required_models(
    base_url: str | None = None, *, wait_for_server_s: float = 180.0
) -> list[OpsResult]:
    """Pull the configured chat and embedding models into Ollama (#3824).

    Until this ran, `nyxgpt ops install` reported every service healthy on a
    machine whose Ollama had never downloaded a model, and the user's first
    chat message failed. The models come from configuration -- `[nyxgpt]
    default_model` and `[rag] embedding_model`, resolved by
    `nyxgpt.model_bootstrap.required_models` -- not from literals here, so
    pointing the config at a different model changes what the install pulls.

    Both models, always: RAG is a per-session toggle, so "RAG is off" is not a
    reason to leave the embedding model unpulled and make the first RAG-enabled
    message block on a download.

    Unconditional by design: there is no flag to skip it. An install already
    needs network egress for the CLI, the service tarballs and Ollama's own
    installer, so a skip flag would only create a supported way for this
    command to report success while leaving chat broken.

    Idempotent: a model already in the Ollama store is reported as such and
    nothing is downloaded, so a re-install over a warm machine adds one
    `/api/tags` request.
    """
    outcomes = model_bootstrap.ensure_required_models(
        base_url=base_url, wait_for_server_s=wait_for_server_s
    )
    if not outcomes:
        return [
            OpsResult(
                True,
                "Skipped required-model pull (no models configured)",
                "Set [nyxgpt] default_model in ~/.nyxGPT/config.ini.",
            )
        ]
    results: list[OpsResult] = []
    for outcome in outcomes:
        model = outcome.model
        if not outcome.ok:
            results.append(
                OpsResult(
                    False,
                    f"Required {model.role} model '{model.name}' is not installed",
                    outcome.detail
                    + "\nThe stack cannot serve "
                    + ("chat" if model.role == model_bootstrap.CHAT_ROLE else "RAG")
                    + " without it -- fix the cause and re-run `nyxgpt ops install`.",
                )
            )
        elif outcome.already_present:
            results.append(
                OpsResult(True, f"{model.role.capitalize()} model present", outcome.detail)
            )
        else:
            results.append(
                OpsResult(True, f"Pulled {model.role} model '{model.name}'", outcome.detail)
            )
    return results


def _install_cassandra_log_follower_service() -> list[OpsResult]:
    """Install the Cassandra log-follower agent via the OS-appropriate mechanism."""
    if _is_macos():
        return _install_cassandra_launchagent()
    if _is_linux():
        return _install_cassandra_logs_systemd_unit()
    return _unsupported_os_result("cassandra log follower")


def _install_ollama_log_follower_service() -> list[OpsResult]:
    """Install the Ollama log-follower agent via the OS-appropriate mechanism."""
    if _is_macos():
        return _install_ollama_launchagent()
    if _is_linux():
        return _install_ollama_logs_systemd_unit()
    return _unsupported_os_result("ollama log follower")


def _install_ollama_env_agent() -> list[OpsResult]:
    """Install the Ollama shared-model-store env agent, on the OS that still needs one.

    macOS needs a RunAtLoad LaunchAgent that reapplies `launchctl setenv
    OLLAMA_MODELS` every login (#3431); Linux's `nyxgpt-ollama.service`
    already bakes `Environment=OLLAMA_MODELS=...` into the unit file, which
    applies on every unit start with no companion agent needed.
    """
    if _is_macos():
        return _install_ollama_env_launchagent()
    if _is_linux():
        return [
            OpsResult(
                True,
                "Ollama env agent: not needed on Linux",
                "nyxgpt-ollama.service's Environment= directive sets OLLAMA_MODELS "
                "directly -- unlike launchctl setenv, it doesn't need a RunAtLoad "
                "companion to survive a reboot.",
            )
        ]
    return _unsupported_os_result("ollama env agent")


def _launchd_agent_loaded(label: str) -> bool:
    """True if LaunchAgent `label` is currently loaded (`launchctl list`).

    False on any failure (launchctl missing, non-zero exit, or the call
    itself raising): this feeds `status`/`detect_deployment_mode`, which
    report state rather than act on it, so an unreadable launchd is "not
    known to be running", never an exception out of a status command.
    """
    if _which("launchctl") is None:
        return False
    try:
        cp = _run(
            ["launchctl", "list"], check=False, expected=True, timeout=LOCAL_PROBE_TIMEOUT_SECONDS
        )
    except Exception as e:
        logger.warning(
            "Could not query launchctl list for %s: %s", label, e, extra={"component": "ops"}
        )
        return False
    if cp.returncode != 0:
        return False
    for line in (cp.stdout or "").splitlines():
        parts = line.split()
        if parts and parts[-1] == label:
            return True
    return False


def _dev_launchd_label(component: str) -> str | None:
    """The dev-mode launchd label to drive for `component`, or None.

    None means "use this OS's normal service manager": every component on
    Linux (both modes drive the same systemd units), `ollama` on macOS (a
    brew service in both modes), and all of macOS when the machine is on the
    artifact path.
    """
    if not _is_macos() or component not in DEV_LAUNCHD_LABELS:
        return None
    if not read_install_mode().is_dev:
        return None
    return DEV_LAUNCHD_LABELS[component]


def _restart_native_service(component: str) -> list[OpsResult]:
    """Restart the OS-appropriate native service for `component` ("api"/"web"/"ollama")."""
    if _is_macos():
        label = _dev_launchd_label(component)
        if label is not None:
            return _restart_launchagent(label)
        # Resolved, not indexed: on a candidate install the running service
        # is `nyxgpt-api@<line>rc` and restarting `nyxgpt-api` would act on
        # something else entirely -- an older release's keg, or nothing (#3853).
        return _restart_brew_service(_resolved_brew_service(component))
    if _is_linux():
        return _restart_systemd_service(NATIVE_SYSTEMD_SERVICES[component])
    return _unsupported_os_result(f"restart {component}")


def _stop_native_service(component: str) -> list[OpsResult]:
    """Stop the OS-appropriate native service for `component` ("api"/"web"/"ollama").

    On macOS this stops **every** registered brew service variant for the
    component, not just the resolved one. "Stop the stack" has to mean the
    ports are free afterwards, and both channels' formulas declare
    `keep_alive true`: leaving a superseded `nyxgpt-api` registered while
    stopping `nyxgpt-api@3.0.0rc` hands :8000 straight back to launchd, which
    is the `[Errno 48] address already in use` restart loop from #3853.
    """
    if _is_macos():
        label = _dev_launchd_label(component)
        if label is not None:
            return _stop_launchagent(label)
        snapshot = _brew_services_snapshot()
        resolved = _resolved_brew_service(component, snapshot)
        results = _stop_brew_service(resolved)
        for stale in brew_services.superseded(
            NATIVE_BREW_SERVICES[component], snapshot, keep=resolved
        ):
            results.extend(_stop_brew_service(stale))
        return results
    if _is_linux():
        return _stop_systemd_service(NATIVE_SYSTEMD_SERVICES[component])
    return _unsupported_os_result(f"stop {component}")


# Every LaunchAgent `nyxgpt ops install` writes into ~/Library/LaunchAgents
# regardless of install mode (#3859): the two log followers plus the Ollama
# env agent. Deliberately *not* part of `DEV_LAUNCHD_LABELS` -- that map is
# dev mode's substitute for the api/web brew services, while these three are
# installed unconditionally, in both modes, by
# `_install_cassandra_launchagent`, `_install_ollama_log_follower_service`
# and `_install_ollama_env_agent`.
#
# One map, because the same three agents used to be enumerated in three
# places and every list was different: removal knew only the dev pair,
# `status` reported `com.nyxgpt.cassandra-logs` alone, and nothing at all
# knew about `com.nyxgpt.ollama-env`. So `com.nyxgpt.ollama-logs` was still
# running at PID 58068 after the owner's complete `brew uninstall` + `brew
# untap`, and invisible to the command they would have checked with.
# `_remove_support_launchagents` (teardown) and `status` (reporting) both
# read this.
SUPPORT_LAUNCHD_LABELS: dict[str, str] = {
    "cassandra-logs": "com.nyxgpt.cassandra-logs",
    "ollama-logs": "com.nyxgpt.ollama-logs",
    "ollama-env": "com.nyxgpt.ollama-env",
}

# Linux twin of SUPPORT_LAUNCHD_LABELS. There is no `ollama-env` unit: the
# systemd path puts OLLAMA_MODELS in the service unit's own `Environment=`,
# which applies on every start and needs no login-time agent (see
# `_install_ollama_env_agent`'s Linux branch).
SUPPORT_SYSTEMD_UNITS: dict[str, str] = {
    "cassandra-logs": "nyxgpt-cassandra-logs",
    "ollama-logs": "nyxgpt-ollama-logs",
}

# The log-follower agents `restart`/`stop`/`down` drive, and the single source
# of that *set*. The env agent is deliberately absent: it has no log to follow.
#
# Every command that drives a follower iterates this tuple instead of naming a
# member, and the CLI builds its `restart`/`stop` target choices from it, so
# the commands and the targets they accept cannot drift apart. #3859 unified
# the label maps below for teardown and `status`, but left each caller
# hand-enumerating `cassandra-logs` alone -- so `ollama-logs` had a helper
# wired to the right label and nothing calling it, and `launchctl bootout` was
# the only way to stop the ollama watcher (#4033). Adding a fourth follower
# now reaches every command that stops one, rather than three that must each
# be remembered.
NATIVE_LOG_FOLLOWERS: tuple[str, ...] = ("cassandra-logs", "ollama-logs")

# Follower identifiers mapped to each OS's native name for that agent -- a
# view of the two maps above rather than a third list.
_NATIVE_LOG_FOLLOWER_LAUNCHD_LABELS: dict[str, str] = {
    name: SUPPORT_LAUNCHD_LABELS[name] for name in NATIVE_LOG_FOLLOWERS
}
_NATIVE_LOG_FOLLOWER_SYSTEMD_UNITS: dict[str, str] = {
    name: SUPPORT_SYSTEMD_UNITS[name] for name in NATIVE_LOG_FOLLOWERS
}


def _restart_native_log_follower(name: str) -> list[OpsResult]:
    """Restart the OS-appropriate log-follower agent ("cassandra-logs"/"ollama-logs")."""
    if _is_macos():
        return _restart_launchagent(_NATIVE_LOG_FOLLOWER_LAUNCHD_LABELS[name])
    if _is_linux():
        return _restart_systemd_service(_NATIVE_LOG_FOLLOWER_SYSTEMD_UNITS[name])
    return _unsupported_os_result(f"restart {name}")


def _stop_native_log_follower(name: str) -> list[OpsResult]:
    """Stop the OS-appropriate log-follower agent ("cassandra-logs"/"ollama-logs")."""
    if _is_macos():
        return _stop_launchagent(_NATIVE_LOG_FOLLOWER_LAUNCHD_LABELS[name])
    if _is_linux():
        return _stop_systemd_service(_NATIVE_LOG_FOLLOWER_SYSTEMD_UNITS[name])
    return _unsupported_os_result(f"stop {name}")


def _ensure_web_deps() -> list[OpsResult]:
    """Ensure web/node_modules is present by running npm ci/install in ./web.

    This is intentionally part of ops install so users don't have to run `npm install` manually.
    """
    results: list[OpsResult] = []
    web_dir = REPO_ROOT / "web"
    if not web_dir.exists():
        # Artifact install: there is no checkout, so there is no `web/` npm
        # project to prime -- and reporting the path REPO_ROOT resolved to
        # inside the installed venv only reads as a bug (#3759). The
        # nyxgpt-web service installs its own dependencies from the
        # published web artifact (`_install_native_web`).
        return [
            OpsResult(
                True,
                "Web deps: not applicable to an artifact install (no repo checkout)",
                "The nyxgpt-web service installs its own dependencies from the published "
                "web artifact; web/node_modules is a dev-checkout concern.",
            )
        ]

    if _which("node") is None:
        return [
            OpsResult(
                False,
                "node not found; cannot install web deps",
                "Install Node.js and ensure `node` is on PATH",
            )
        ]
    if _which("npm") is None:
        return [
            OpsResult(
                False,
                "npm not found; cannot install web deps",
                "Install Node.js/npm and ensure `npm` is on PATH",
            )
        ]

    node_modules = web_dir / "node_modules"
    lockfile = web_dir / "package-lock.json"

    def _can_resolve(pkg: str) -> bool:
        try:
            cp = subprocess.run(
                ["node", "-p", f"require.resolve('{pkg}')"],
                cwd=str(web_dir),
                text=True,
                capture_output=True,
            )
            return cp.returncode == 0
        except Exception as e:
            logger.warning(
                "Failed to resolve node package %r, assuming missing: %s",
                pkg,
                e,
                extra={"component": "ops"},
            )
            return False

    # Check if node_modules exists and undici can be resolved
    if node_modules.exists() and _can_resolve("undici"):
        results.append(OpsResult(True, "Web deps already installed (undici OK)", str(node_modules)))
        return results

    def _run_npm(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(cmd, cwd=str(web_dir), text=True, capture_output=True)

    # Prefer npm ci when a lockfile exists, but if the lockfile is out of sync
    # (common after editing package.json), fall back to npm install so the lock
    # is updated and dependencies actually get installed.
    try:
        if lockfile.exists():
            cp = _run_npm(["npm", "ci"])
            if cp.returncode != 0:
                stderr = cp.stderr or ""
                # npm uses EUSAGE + a specific message when package-lock is out of sync
                if (
                    "can only install packages when your package.json and package-lock.json"
                    in stderr
                ):
                    cp2 = _run_npm(["npm", "install"])
                    if cp2.returncode != 0:
                        details = _output_excerpt(cp2)
                        results.append(
                            OpsResult(
                                False,
                                "Failed to install web deps via npm install (after npm ci mismatch)",
                                details.strip(),
                            )
                        )
                        return results
                    # npm install succeeded (and likely updated package-lock.json)
                    if not _can_resolve("undici"):
                        details = _output_excerpt(cp2)
                        results.append(
                            OpsResult(
                                False,
                                "Web deps installed but undici still missing",
                                details.strip(),
                            )
                        )
                        return results
                    results.append(
                        OpsResult(
                            True,
                            "Installed web deps via npm install (lockfile was out of sync; package-lock.json updated)",
                            str(node_modules),
                        )
                    )
                    return results

                # Other npm ci failure
                details = _output_excerpt(cp)
                results.append(
                    OpsResult(False, "Failed to install web deps via npm ci", details.strip())
                )
                return results

            # npm ci succeeded
            if not _can_resolve("undici"):
                details = _output_excerpt(cp)
                results.append(
                    OpsResult(
                        False,
                        "Web deps installed but undici still missing",
                        details.strip(),
                    )
                )
                return results
            results.append(OpsResult(True, "Installed web deps via npm ci", str(node_modules)))
            return results

        # No lockfile: use npm install
        cp = _run_npm(["npm", "install"])
        if cp.returncode == 0:
            if not _can_resolve("undici"):
                details = _output_excerpt(cp)
                results.append(
                    OpsResult(
                        False,
                        "Web deps installed but undici still missing",
                        details.strip(),
                    )
                )
                return results
            results.append(OpsResult(True, "Installed web deps via npm install", str(node_modules)))
            return results

        details = _output_excerpt(cp)
        results.append(
            OpsResult(False, "Failed to install web deps via npm install", details.strip())
        )
        return results

    except Exception as e:
        results.append(OpsResult(False, "Failed to install web deps", f"{type(e).__name__}: {e}"))
        return results


def _ensure_mcp_deps() -> list[OpsResult]:
    """Ensure root node_modules are present for Claude Code MCP servers."""
    results: list[OpsResult] = []
    root_dir = REPO_ROOT
    pkg_json = root_dir / "package.json"

    if not pkg_json.exists():
        # Artifact install -- same reasoning as `_ensure_web_deps` (#3759).
        # The MCP servers are repo-local dev tooling, never part of an
        # installed deployment.
        return [
            OpsResult(
                True,
                "MCP deps: not applicable to an artifact install (no repo checkout)",
                "The Claude Code MCP servers are repo-local dev tooling, not a runtime "
                "dependency of an installed nyxGPT.",
            )
        ]

    if _which("npm") is None:
        return [
            OpsResult(
                False,
                "npm not found; cannot install MCP deps",
                "Install Node.js/npm and ensure `npm` is on PATH",
            )
        ]

    sentinel = root_dir / "node_modules" / "@modelcontextprotocol" / "server-github"
    if sentinel.exists():
        results.append(
            OpsResult(True, "MCP deps already installed", str(root_dir / "node_modules"))
        )
        return results

    lockfile = root_dir / "package-lock.json"
    use_ci = lockfile.exists()
    npm_cmd = ["npm", "ci"] if use_ci else ["npm", "install"]

    try:
        cp = subprocess.run(npm_cmd, cwd=str(root_dir), text=True, capture_output=True)
        if cp.returncode == 0:
            results.append(
                OpsResult(
                    True,
                    f"Installed MCP deps via {' '.join(npm_cmd)}",
                    str(root_dir / "node_modules"),
                )
            )
        else:
            details = _output_excerpt(cp)
            results.append(OpsResult(False, "Failed to install MCP deps", details.strip()))
    except Exception as e:
        results.append(OpsResult(False, "Failed to install MCP deps", f"{type(e).__name__}: {e}"))

    return results


# --- Docker engine bootstrap (Linux) ---

# Distro package sets that provide the Docker *engine*, tried in order. Each
# entry is (package-manager argv prefix, packages).
#
# Engine and Compose plugin are installed as two separate transactions
# (`_LINUX_COMPOSE_PACKAGE_SETS` below) because the two are not packaged
# together everywhere. Amazon Linux 2023 carries `docker` but ships no compose
# package in its repos at all, so the combined `dnf install -y docker
# docker-compose-plugin` this used to run failed as a unit -- taking the engine
# down with the plugin and reporting "Could not install Docker automatically"
# on a host where the engine alone would have installed fine (#3760).
_LINUX_DOCKER_ENGINE_PACKAGE_SETS: tuple[tuple[str, list[str], list[str]], ...] = (
    ("apt-get", ["apt-get", "install", "-y"], ["docker.io"]),
    ("dnf", ["dnf", "install", "-y"], ["docker"]),
    ("yum", ["yum", "install", "-y"], ["docker"]),
)

# Distro package sets that provide the Compose v2 CLI plugin. The Debian/Ubuntu
# plugin has been packaged under two names across releases
# (`docker-compose-v2` on recent Ubuntu, `docker-compose-plugin` where Docker's
# own repo is configured), so both are attempted before giving up. Distros that
# package nothing at all (Amazon Linux 2023) fall through to
# `_install_compose_plugin_binary`.
_LINUX_COMPOSE_PACKAGE_SETS: tuple[tuple[str, list[str], list[str]], ...] = (
    ("apt-get", ["apt-get", "install", "-y"], ["docker-compose-v2"]),
    ("apt-get", ["apt-get", "install", "-y"], ["docker-compose-plugin"]),
    ("dnf", ["dnf", "install", "-y"], ["docker-compose-plugin"]),
    ("yum", ["yum", "install", "-y"], ["docker-compose-plugin"]),
)

# Where a manually fetched Compose v2 plugin goes. The Docker CLI scans this
# directory for `docker-<subcommand>` plugins for *every* user on the host,
# which is what a per-user `~/.docker/cli-plugins` install would not do -- and
# ops may end up invoking Docker as root (`_enable_docker_socket_hop`).
COMPOSE_PLUGIN_DIR = Path("/usr/local/lib/docker/cli-plugins")

# Docker publishes one static plugin binary per platform with every Compose
# release. `releases/latest/download/...` is GitHub's own redirect to the
# newest release's asset, so this URL keeps resolving without a version bump
# here -- and the fetch is verified by actually running `docker compose
# version` afterwards rather than trusted blind.
_COMPOSE_PLUGIN_URL = (
    "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-{arch}"
)

# `platform.machine()` -> the architecture suffix in Compose's release assets.
_COMPOSE_PLUGIN_ARCHES: dict[str, str] = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "armv6l": "armv6",
    "armv7l": "armv7",
    "ppc64le": "ppc64le",
    "riscv64": "riscv64",
    "s390x": "s390x",
}

_DOCKER_MANUAL_INSTALL_HINT = (
    "Install Docker yourself, then re-run `nyxgpt ops install`:\n"
    "  Debian/Ubuntu:     sudo apt-get update && sudo apt-get install -y docker.io "
    "docker-compose-v2\n"
    "  Fedora/RHEL:       sudo dnf install -y docker docker-compose-plugin\n"
    "  Amazon Linux 2023: sudo dnf install -y docker  (its repos carry no compose\n"
    "                     package, so fetch the plugin binary as well:\n"
    f"                     sudo mkdir -p {COMPOSE_PLUGIN_DIR} && sudo curl -fsSL -o "
    f"{COMPOSE_PLUGIN_DIR / 'docker-compose'} \\\n"
    "                     https://github.com/docker/compose/releases/latest/download/"
    "docker-compose-linux-$(uname -m) \\\n"
    f"                     && sudo chmod 0755 {COMPOSE_PLUGIN_DIR / 'docker-compose'})\n"
    "  then:              sudo systemctl enable --now docker && sudo usermod -aG docker "
    "$(whoami)"
)


def _docker_daemon_reachable() -> bool:
    """True if `docker info` succeeds -- i.e. the daemon is up AND this user may talk to it.

    Never raises: this runs inside `doctor` and inside install steps, where a
    docker binary that can't even be executed is one more thing to report,
    not a reason to abort the surrounding check.
    """
    if _which("docker") is None:
        return False
    try:
        cp = _run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False, expected=True)
    except Exception as e:
        logger.debug("ops: docker info failed: %s: %s", type(e).__name__, e)
        return False
    return cp.returncode == 0


def _install_packages_from_sets(
    package_sets: tuple[tuple[str, list[str], list[str]], ...],
    *,
    expected_failure: bool = False,
) -> tuple[str, list[str]]:
    """Install the first of `package_sets` whose package manager exists and succeeds.

    Returns `("installed", packages)` on the first successful transaction,
    `("no-root", [])` when root isn't reachable at all (so the caller can say
    so rather than blaming the distro), or `("failed", [])` when every
    matching set was attempted without success -- including the case where no
    package manager on this host matched any set.

    `expected_failure=True` logs a non-zero exit at DEBUG instead of WARNING,
    for sets that are *routinely* absent (no distro packages the Compose
    plugin under every name, and trying is how we find out).
    """
    refreshed_apt = False
    for tool, install_cmd, packages in package_sets:
        if _which(tool) is None:
            continue
        if tool == "apt-get" and not refreshed_apt:
            # Without a package index a fresh cloud image's `install` just
            # reports "Unable to locate package".
            refreshed_apt = True
            _privileged_run(["apt-get", "update"], expected=True)
        cp = _privileged_run([*install_cmd, *packages], expected=expected_failure)
        if cp is None:
            return ("no-root", [])
        if cp.returncode == 0:
            return ("installed", packages)
    return ("failed", [])


def _install_linux_docker_engine() -> list[OpsResult]:
    """Install the Docker engine from the distro's package manager."""
    status, packages = _install_packages_from_sets(_LINUX_DOCKER_ENGINE_PACKAGE_SETS)
    if status == "installed":
        return [OpsResult(True, f"Installed Docker engine packages: {', '.join(packages)}")]
    if status == "no-root":
        return [
            OpsResult(
                False,
                "Docker is not installed and root is not available without a password",
                _DOCKER_MANUAL_INSTALL_HINT,
            )
        ]
    return [
        OpsResult(
            False,
            "Could not install Docker automatically",
            _DOCKER_MANUAL_INSTALL_HINT,
        )
    ]


def _install_compose_plugin_binary() -> list[OpsResult]:
    """Install Docker's own Compose v2 plugin binary into `COMPOSE_PLUGIN_DIR`.

    The escape hatch for distros that package no compose at all. Amazon Linux
    2023 is the case that forced it: `dnf install docker` gives a perfectly
    healthy engine, but there is no `docker-compose-plugin` in its repos, so
    every Compose-backed step of `ops install` -- the observability stack, the
    Grafana admin credential reconcile -- skipped or failed on a host that
    could run them fine (#3760).
    """
    machine = platform.machine()
    arch = _COMPOSE_PLUGIN_ARCHES.get(machine.lower())
    if arch is None:
        return [
            OpsResult(
                False,
                f"Docker publishes no Compose plugin build for architecture {machine!r}",
                _DOCKER_MANUAL_INSTALL_HINT,
            )
        ]
    if _which("curl") is None:
        return [
            OpsResult(
                False,
                "Could not install the Docker Compose plugin (curl not found on PATH)",
                _DOCKER_MANUAL_INSTALL_HINT,
            )
        ]

    dest = COMPOSE_PLUGIN_DIR / "docker-compose"
    url = _COMPOSE_PLUGIN_URL.format(arch=arch)
    for cmd in (
        ["mkdir", "-p", str(COMPOSE_PLUGIN_DIR)],
        ["curl", "-fsSL", "--retry", "3", "-o", str(dest), url],
        ["chmod", "0755", str(dest)],
    ):
        cp = _privileged_run(cmd)
        if cp is None:
            # Root is unreachable (no passwordless sudo) -- name that, rather
            # than reporting the generic "could not install" the engine path
            # distinguishes too.
            return [
                OpsResult(
                    False,
                    f"Could not install the Docker Compose plugin (root required for `{cmd[0]}`)",
                    _DOCKER_MANUAL_INSTALL_HINT,
                )
            ]
        if cp.returncode != 0:
            return [
                OpsResult(
                    False,
                    "Could not install the Docker Compose plugin automatically",
                    _DOCKER_MANUAL_INSTALL_HINT,
                )
            ]

    if not _compose_available():
        return [
            OpsResult(
                False,
                f"Fetched {dest} but `docker compose` is still not usable",
                _DOCKER_MANUAL_INSTALL_HINT,
            )
        ]
    return [
        OpsResult(
            True,
            f"Installed the Docker Compose plugin to {dest}",
            f"Fetched from {url} -- this distro's repos carry no compose package.",
        )
    ]


def _install_linux_compose_plugin() -> list[OpsResult]:
    """Ensure `docker compose` works: distro package first, release binary second."""
    status, packages = _install_packages_from_sets(
        _LINUX_COMPOSE_PACKAGE_SETS, expected_failure=True
    )
    if status == "installed" and _compose_available():
        return [OpsResult(True, f"Installed Docker Compose packages: {', '.join(packages)}")]
    return _install_compose_plugin_binary()


def _ensure_docker_group_membership() -> list[OpsResult]:
    """Add the invoking user to the `docker` group so non-root `docker` calls work.

    The daemon's socket is root:docker 0660, so a fresh `apt-get install
    docker.io` leaves every `docker` call failing with "permission denied
    while trying to connect to the Docker daemon socket" until the user is in
    that group -- and, crucially, until their *login session* is recreated:
    group membership is stamped into a session's credentials at login, so an
    already-running `systemd --user` manager (and every service it spawned,
    including `nyxgpt-api`) keeps the old group set. That is exactly the
    #3632 finding where `nyxgpt ops status` saw Cassandra running from a
    fresh shell while the API-backed web UI reported it absent: the CLI had
    the group, the long-lived API process didn't. Re-login guidance is
    therefore part of the result, not an afterthought.
    """
    user = getpass.getuser()
    if _running_as_root():
        return [OpsResult(True, "Running as root; docker group membership not needed")]
    cp = _privileged_run(["usermod", "-aG", "docker", user])
    if cp is None or cp.returncode != 0:
        return [
            OpsResult(
                False,
                f"Could not add {user!r} to the 'docker' group",
                f"Run: sudo usermod -aG docker {user} && sudo loginctl terminate-user {user}, "
                "reconnect, then re-run `nyxgpt ops install`.",
            )
        ]
    return [
        OpsResult(
            True,
            f"Added {user!r} to the 'docker' group",
            "Group membership only applies to *new* login sessions, so this shell (and "
            "any already-running systemd --user services) still lack it. If docker "
            f"commands still fail: sudo loginctl terminate-user {user}, then reconnect.",
        )
    ]


# Name of the throwaway variable the hop's environment probe passes through a
# candidate hop. Re-exported from `docker_access` so this module's tests and
# call sites keep one name for it.
_HOP_ENV_PROBE_VAR = docker_access.ENV_PROBE_VAR


def _docker_hop_preserves_env(hop: str) -> bool:
    """True if `hop` hands this process's environment through unchanged.

    See `docker_access.hop_preserves_env`: `HOME` (Compose bind-mount
    interpolation) and an `env=`-forwarded variable (the `docker compose exec
    -e VAR` secret channel) are both checked, because both fail *silently*
    rather than loudly if the hop resets the environment.
    """
    return docker_access.hop_preserves_env(hop, _hop_runner)


def _docker_hop_reaches_daemon(hop: str) -> bool:
    """True if a `docker` call made through `hop` can talk to the daemon."""
    return docker_access.hop_reaches_daemon(hop, _hop_runner)


def _enable_docker_socket_hop() -> bool:
    """Reach the Docker socket through `sg docker`/`sudo` for the rest of this process.

    `usermod -aG docker` cannot reach an already-open login session: group
    membership is stamped into a session's credentials when it is created. In
    a `nyxgpt cloud deploy` that meant the deploy's second pass died on
    "permission denied while trying to connect to the Docker daemon socket"
    moments after the first pass had created the Cassandra container (#3760).
    Telling the operator to reconnect and start the deploy over is a poor
    answer when ops already holds the privilege it needs.

    `sg docker` is tried first: it applies the group membership just added,
    needs no sudoers assumptions at all, preserves the environment by
    construction, and is the same mechanism the cloud provisioning script
    already validates with `sg docker -c true`. Passwordless sudo
    (`sudo -n --preserve-env`) is the second choice, for a host where the
    group change itself could not be made. Either way the hop must prove it
    preserves the environment before it is used -- see
    `docker_access.hop_preserves_env` for why an env-resetting hop is worse
    than no hop.

    **This is the one place `sudo` is on the list** (#4022). The candidate
    list is spelled here rather than defaulted on `_DOCKER_HOP`, because the
    justification for it is local: this function is reached only from
    `_ensure_docker_engine`, i.e. an interactive `nyxgpt ops install`, which
    is already a privileged operation. The lazy retries in
    `_DOCKER_HOP.run` -- reachable from the polled `/infra/status` inside the
    public API process -- keep the module default of `sg` only. A future
    caller therefore has to ask for `sudo` deliberately; it cannot inherit it.

    Returns True if a hop is now active. The group membership is still added
    either way, so a later login needs no hop at all. Deliberately a last
    resort, tried only *after* the real group change has been attempted and
    found not to have taken effect -- never as a way to skip it.
    """
    if _DOCKER_HOP.active is not None:
        return True
    if _running_as_root() or _which("docker") is None:
        return False
    return _DOCKER_HOP.adopt(("sg", "sudo")) is not None


def _ensure_docker_engine() -> list[OpsResult]:
    """Ensure a usable Docker engine + Compose plugin exist before anything needs them.

    `nyxgpt ops install` brings up Cassandra and the whole observability
    stack in containers, so "install Docker first" was an undocumented
    prerequisite the operator had to satisfy by hand -- #3632's finding that
    `install` on a clean Linux box "doesn't install missing docker components
    for observability", and the frictionless-install ask behind it. This step
    reconciles that the same way every other install step reconciles its
    piece: install the packages if missing, start/enable the daemon, and put
    the invoking user in the `docker` group.

    Engine and Compose plugin are reconciled independently, because a distro
    that has one need not have the other: Amazon Linux 2023 packages the
    engine and no compose at all, so its plugin comes from Docker's own
    release binary (#3760). If the group change turns out not to reach this
    already-open session, ops falls back to `sudo -n docker` for the rest of
    the run rather than failing every later Docker step.

    Linux only. macOS's Docker Desktop is a GUI application with a license
    prompt -- installing it unattended is not something `ops install` should
    do behind the operator's back, so there it only reports what's missing.
    Every failure is actionable (the exact command to run by hand) rather
    than fatal to the rest of install: a host that genuinely can't have
    Docker still gets its native api/web/ollama services reconciled.
    """
    if not _is_linux():
        if _which("docker") is None:
            return [
                OpsResult(
                    False,
                    "Docker not found on PATH",
                    "Install Docker Desktop (https://docs.docker.com/desktop/install/mac-install/) "
                    "and start it, then re-run `nyxgpt ops install`.",
                )
            ]
        return [OpsResult(True, "Docker is available")]

    results: list[OpsResult] = []
    if _which("docker") is None:
        results.extend(_install_linux_docker_engine())
        if not any(r.ok for r in results):
            return results

    if _which("docker") is None:
        return [
            *results,
            OpsResult(False, "Docker still not found on PATH", _DOCKER_MANUAL_INSTALL_HINT),
        ]

    if not _compose_available():
        results.extend(_install_linux_compose_plugin())

    # A distro `docker.io` install leaves the unit enabled but, on a
    # container/minimal image, not necessarily started.
    cp = _privileged_run(["systemctl", "enable", "--now", "docker"], expected=True)
    if cp is not None and cp.returncode == 0:
        results.append(OpsResult(True, "Docker daemon enabled and started"))

    if _docker_daemon_reachable():
        results.append(OpsResult(True, "Docker daemon is reachable"))
        return results

    # The group-membership attempt is reported *after* we know whether the
    # daemon ended up reachable: a `usermod` that failed on a host ops then
    # reached through `sudo -n docker` is history, not this step's verdict
    # (#3762). Only when nothing worked does it stay a `[FAIL]`.
    group_results = _ensure_docker_group_membership()
    if _docker_daemon_reachable():
        results.extend(_superseded_attempts(group_results))
        results.append(OpsResult(True, "Docker daemon is reachable"))
    elif _enable_docker_socket_hop():
        hop = _DOCKER_SOCKET_HOP_LABELS[str(_docker_socket_hop())]
        results.extend(_superseded_attempts(group_results))
        results.append(
            OpsResult(
                True,
                f"Docker daemon is reachable via {hop} for the rest of this run",
                "A 'docker' group membership only applies to new login "
                f"sessions, so ops runs Docker through {hop} for the rest of this "
                "process instead of failing every container step. The hop preserves "
                "this session's environment, so Compose bind mounts still resolve "
                "under this user's home. Reconnect (or run: sudo loginctl "
                f"terminate-user {getpass.getuser()}) to drop it.",
            )
        )
    else:
        # Nothing recovered it, so the failed attempt keeps its `[FAIL]`:
        # it is part of why this step failed, not noise to hide.
        results.extend(group_results)
        results.append(
            OpsResult(
                False,
                "Docker is installed but this process cannot reach the daemon",
                "Usually a group-membership change that hasn't reached this login "
                f"session yet. Run: sudo loginctl terminate-user {getpass.getuser()}, "
                "reconnect, then re-run `nyxgpt ops install`.",
            )
        )
    return results


def _docker_access_doctor_issue() -> str | None:
    """`nyxgpt ops doctor` check for a Docker daemon this process can't talk to.

    Distinct from doctor's existing "Missing tool in PATH: docker": here the
    binary *is* present and the daemon may well be running -- the invoking
    user just isn't in the `docker` group, or their session predates the
    membership. That produced #3632's most confusing symptom, `nyxgpt ops
    status` and the web UI disagreeing about whether Cassandra was running,
    because they ran from sessions with different group sets.
    """
    if _which("docker") is None:
        return None
    if _docker_daemon_reachable():
        return None
    hint = ""
    if _is_linux():
        hint = (
            f" -- if you were just added to the 'docker' group, the change only applies to "
            f"new login sessions (run: sudo loginctl terminate-user {getpass.getuser()}, "
            "then reconnect)"
        )
    return (
        "Docker is installed but the daemon is unreachable from this process "
        f"(run: nyxgpt ops install){hint}"
    )


# --- Local Cassandra container lifecycle ---

# Canonical definition of the one ops-managed Docker container in a native-mode
# local deployment (api/web/ollama run natively via Homebrew; see docs/ops.md).
# Mirrors the `cassandra` service in docker-compose.yml so the native and
# Compose paths agree on image/port/volume -- but this container is created and
# managed via plain `docker run`/`docker start`, entirely separate from the
# Compose "cloud/server" stack, so its lifecycle never requires (or pulls in)
# the rest of docker-compose.yml.
CASSANDRA_CONTAINER_NAME = "nyxgpt-cassandra"
# Keep this pin identical to the `cassandra` image tag in docker-compose.yml,
# terraform/main.tf (docker_image.cassandra) and k8s/statefulset-cassandra.yaml
# -- see docs/docker-compose.md for the image-pinning policy and how to bump
# all four together.
CASSANDRA_IMAGE = "cassandra:5.0.8"
# Must match the cluster name stamped into the Cassandra data directory's
# system keyspace: Cassandra refuses to start when the saved cluster name
# differs from the configured one, so a recreated container without this env
# crash-loops with "Saved cluster name nyxgpt != configured name Test Cluster".
CASSANDRA_CLUSTER_NAME = "nyxgpt"


def _ensure_cassandra_container() -> list[OpsResult]:
    """Ensure the local `nyxgpt-cassandra` Docker container exists and is running.

    Reconciles to the intended state rather than only adding:
    - running: nothing to do.
    - present but not running (exited/created/paused/...): `docker start` it.
    - absent: `docker run` a fresh container from `CASSANDRA_IMAGE`, bind-mounted to
      `volume_dir("cassandra")`, bound to `${NYXGPT_BIND_ADDR:-127.0.0.1}:${CASSANDRA_PORT:-9042}`.

    This is what `nyxgpt ops install` was missing entirely: without it, no
    `nyxgpt` command ever created the container in the first place, so
    `nyxgpt ops restart cassandra` (a plain `docker restart`) had nothing to
    restart on a fresh machine or after the container was removed.
    """
    if _which("docker") is None:
        return [
            OpsResult(
                False,
                "docker not found; cannot ensure local Cassandra container",
                "Install Docker Desktop (or the docker CLI) -- Cassandra is the one "
                "Docker-managed piece of a native-mode local install.",
            )
        ]

    # ~/.nyxGPT/volumes/cassandra is the same host directory docker-compose.yml
    # and terraform/main.tf bind-mount their `cassandra` service/container to --
    # two Cassandra processes writing to it concurrently would corrupt the data
    # directory, so refuse rather than create a second writer (see #3346).
    if terraform_stack_state().get("cassandra") == "running":
        return [
            OpsResult(
                False,
                "Refusing to start native Cassandra: the Terraform-managed Cassandra "
                f"container ({TERRAFORM_CONTAINERS['cassandra']}) is already running and "
                "shares the same ~/.nyxGPT/volumes/cassandra data directory",
                "Run `nyxgpt ops down --terraform` first, or skip native Cassandra.",
            )
        ]

    probe = _docker_container_probe(CASSANDRA_CONTAINER_NAME)
    state = probe.state

    if state == "running":
        return [OpsResult(True, f"Cassandra container already running: {CASSANDRA_CONTAINER_NAME}")]

    if not probe.known:
        # Refuse rather than fall through to `docker run --name
        # nyxgpt-cassandra` (#4022). Before this, a denied read reported
        # `absent` and this step tried to *create* a container that may well
        # already exist -- a second writer to ~/.nyxGPT/volumes/cassandra is
        # the exact corruption #3346 refuses elsewhere in this function, and
        # here it would have been reached on the strength of a read that never
        # happened.
        user = getpass.getuser()
        return [
            OpsResult(
                False,
                "Cannot determine whether the Cassandra container exists: " f"{probe.reason}",
                "This session may not talk to the Docker daemon and no `sg docker` hop was "
                f"available, so ops will not create a container that may already be running. "
                f"Repair: sudo usermod -aG docker {user}, then "
                f"sudo loginctl terminate-user {user} (or reboot), and re-run.",
            )
        ]

    if state != "absent":
        cp = _run(["docker", "start", CASSANDRA_CONTAINER_NAME], check=False)
        if cp.returncode == 0:
            return [
                OpsResult(True, f"Started existing Cassandra container: {CASSANDRA_CONTAINER_NAME}")
            ]
        details = _output_excerpt(cp)
        return [
            OpsResult(
                False,
                f"Failed to start existing Cassandra container: {CASSANDRA_CONTAINER_NAME}",
                details.strip(),
            )
        ]

    bind_addr = os.environ.get("NYXGPT_BIND_ADDR", "127.0.0.1")
    port = os.environ.get("CASSANDRA_PORT", "9042")
    data_dir = volume_dir("cassandra")
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        CASSANDRA_CONTAINER_NAME,
        "--restart",
        "unless-stopped",
        "-p",
        f"{bind_addr}:{port}:9042",
        "-v",
        f"{data_dir}:/var/lib/cassandra",
        "-e",
        f"CASSANDRA_CLUSTER_NAME={CASSANDRA_CLUSTER_NAME}",
        CASSANDRA_IMAGE,
    ]
    cp = _run(cmd, check=False)
    if cp.returncode == 0:
        return [
            OpsResult(
                True,
                f"Created Cassandra container: {CASSANDRA_CONTAINER_NAME} ({CASSANDRA_IMAGE})",
                f"Bound to {bind_addr}:{port}, data persisted in {data_dir}",
            )
        ]
    details = _output_excerpt(cp)
    return [
        OpsResult(
            False,
            f"Failed to create Cassandra container: {CASSANDRA_CONTAINER_NAME}",
            details.strip(),
        )
    ]


# --- Legacy named-volume migration (issue #3346) ---
#
# Before #3346, container data lived in named Docker volumes rather than
# ~/.nyxGPT/volumes/ bind mounts: `nyxgpt_cassandra_data` (native, and -- by
# coincidence of docker-compose.yml's explicit `name: nyxgpt` project name --
# also Compose's `cassandra_data`), `nyxgpt_ollama_data`/`nyxgpt_data` (Compose),
# and `nyxgpt_tf_ollama_data`/`nyxgpt_tf_cassandra_data`/`nyxgpt_tf_nyxgpt_data`
# (Terraform). This copies that data into the new bind-mount directories on
# upgrade so it isn't silently lost. On macOS/Docker Desktop a named volume's
# files live inside the Docker VM, not directly reachable from the host
# filesystem, so the copy runs through a throwaway container rather than a
# plain filesystem copy.

# Pinned to a specific version (no floating/untagged reference), same policy
# as every other third-party image in this file -- see
# docs/docker-compose.md#image-pinning. This one is just a disposable `cp`
# runner, not a long-lived service, so it isn't part of that doc's
# pinned-images table.
MIGRATION_HELPER_IMAGE = "alpine:3.20.3"

# dest volume_dir() component -> legacy Docker volume names to check, in
# priority order (first one found with data wins; see
# `migrate_legacy_volumes`). Cassandra/Ollama/nyxgpt-data can have both a
# Compose-era and a Terraform-era candidate now that both modes share one
# destination directory; only one can be migrated in since merging two
# independent Cassandra data directories isn't safe.
LEGACY_VOLUME_SOURCES: dict[str, list[str]] = {
    "cassandra": ["nyxgpt_cassandra_data", "nyxgpt_tf_cassandra_data"],
    "ollama": ["nyxgpt_ollama_data", "nyxgpt_tf_ollama_data"],
    # Compose's volume key was literally `nyxgpt_data:`, which Compose prefixes
    # with the project name (`name: nyxgpt` in docker-compose.yml) to get
    # `nyxgpt_nyxgpt_data` -- not `nyxgpt_data`.
    "nyxgpt-data": ["nyxgpt_nyxgpt_data", "nyxgpt_tf_nyxgpt_data"],
    "prometheus": ["nyxgpt_prometheus_data"],
    "grafana": ["nyxgpt_grafana_data"],
    "loki": ["nyxgpt_loki_data"],
    "glitchtip-postgres": ["nyxgpt_glitchtip_postgres_data"],
    "glitchtip-uploads": ["nyxgpt_glitchtip_uploads"],
}


def _docker_volume_exists(name: str) -> bool:
    """Return whether a Docker volume named `name` currently exists."""
    if _which("docker") is None:
        return False
    cp = _run(["docker", "volume", "inspect", name], check=False, expected=True)
    return cp.returncode == 0


def _migrate_docker_volume_to_bind_dir(
    volume_name: str, dest_dir: Path, *, label: str
) -> OpsResult:
    """Copy `volume_name`'s contents into `dest_dir` via a throwaway container, then remove it.

    `dest_dir` is assumed already created and empty (checked by the caller,
    `migrate_legacy_volumes`, so this can be tested/called standalone without
    re-deriving that check). Removal is best-effort -- a volume still attached
    to some other container is left behind with a note rather than failing
    the whole migration.
    """
    cp = _run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume_name}:/from:ro",
            "-v",
            f"{dest_dir}:/to",
            MIGRATION_HELPER_IMAGE,
            "sh",
            "-c",
            "cp -a /from/. /to/",
        ],
        check=False,
    )
    if cp.returncode != 0:
        return OpsResult(
            False,
            f"Failed to migrate {label} data from legacy volume '{volume_name}'",
            _cp_details(cp),
        )

    rm = _run(["docker", "volume", "rm", volume_name], check=False, expected=True)
    if rm.returncode == 0:
        return OpsResult(
            True,
            f"Migrated {label} data from legacy volume '{volume_name}' into {dest_dir}",
            "Old volume removed.",
        )
    return OpsResult(
        True,
        f"Migrated {label} data from legacy volume '{volume_name}' into {dest_dir}",
        f"Could not remove the old volume (still in use?): {_cp_details(rm)}",
    )


def _migration_marker_path(dest_name: str) -> Path:
    """Path to `migrate_legacy_volumes`'s per-component "already reconciled" marker.

    Deliberately stored outside `~/.nyxGPT/volumes/<component>/` rather than
    inferred from that directory being non-empty: a freshly-started container
    populates its empty bind mount with new files within seconds of `docker
    compose up`, which an emptiness check can't tell apart from "an earlier
    run already migrated the legacy volume in here" -- that ambiguity is what
    let pre-#3346 data get silently stranded. A marker only appears once this
    function has itself made that determination.
    """
    state_dir = Path.home() / ".nyxGPT" / ".migration-state"
    _ensure_dir(state_dir)
    return state_dir / f"{dest_name}.migrated"


def migrate_legacy_volumes() -> list[OpsResult]:
    """Copy pre-#3346 named-volume data into ~/.nyxGPT/volumes/, then drop the old volumes.

    Idempotent and safe to run on every `nyxgpt ops install` (native and
    `--terraform --local` both do): once a component has been reconciled
    (migrated, or confirmed to have no legacy volume) that's recorded in a
    marker file under `~/.nyxGPT/.migration-state/`, and later runs skip it
    without re-touching `~/.nyxGPT/volumes/<component>/`. Also exposed
    standalone as `nyxgpt ops migrate-volumes` for Compose-only users who
    never run `nyxgpt ops install`.

    If a legacy volume is still found for a component whose destination
    directory is *not yet marked reconciled* but is already non-empty (e.g.
    the new bind-mounted stack was brought up before this ran), migration is
    refused with a loud, actionable failure rather than silently reporting
    success -- auto-merging risks silently overwriting or shadowing whichever
    side holds the data the user actually wants.
    """
    if _which("docker") is None:
        return [OpsResult(True, "Skipped legacy volume migration (docker not found)")]

    results: list[OpsResult] = []
    for dest_name, candidates in LEGACY_VOLUME_SOURCES.items():
        dest_dir = volume_dir(dest_name)
        marker = _migration_marker_path(dest_name)
        if marker.exists():
            results.append(OpsResult(True, f"{dest_name}: already reconciled (nothing to migrate)"))
            continue

        existing = [v for v in candidates if _docker_volume_exists(v)]
        if not existing:
            marker.touch()
            results.append(
                OpsResult(True, f"No legacy volume found for {dest_name} (nothing to migrate)")
            )
            continue

        found, *unmigrated = existing
        if any(dest_dir.iterdir()):
            results.append(
                OpsResult(
                    False,
                    f"{dest_name}: legacy volume '{found}' still holds pre-#3346 data but "
                    f"{dest_dir} is already non-empty -- refusing to auto-migrate. This can "
                    "happen if the new bind-mounted stack was started before running "
                    "`nyxgpt ops migrate-volumes`, which would otherwise silently strand the "
                    "old data.",
                    f"Stop the stack, inspect both {dest_dir} and the legacy volume "
                    f"(docker run --rm -v {found}:/legacy alpine ls -la /legacy), manually merge "
                    f"whichever side holds the data you need, then `docker volume rm {found}` "
                    "once done -- this warning repeats on every run until that volume is gone.",
                )
            )
            continue

        result = _migrate_docker_volume_to_bind_dir(found, dest_dir, label=dest_name)
        if unmigrated:
            note = (
                f"Note: legacy volume(s) {unmigrated} for {dest_name} were also found but not "
                "migrated (only one source can be merged in safely) -- inspect and remove "
                "manually if they hold data you still need."
            )
            result = OpsResult(
                result.ok,
                result.message,
                f"{result.details}\n{note}" if result.details else note,
            )
        if result.ok:
            marker.touch()
        results.append(result)

    return results


def migrate_volumes_cmd(_args) -> int:
    """CLI entrypoint for `nyxgpt ops migrate-volumes`.

    A standalone escape hatch for migrating pre-#3346 named-volume data
    without running the rest of `nyxgpt ops install` -- e.g. for a
    Compose-only user who never runs `install` (that's the native-mode
    reconciler). Safe to re-run; see `migrate_legacy_volumes`.
    """
    results = migrate_legacy_volumes()
    ok = _emit_results("migrate-volumes", results)
    return 0 if ok else 2


# --- Phantom Compose app-tier reconciliation ---


def _detect_phantom_compose_app_containers() -> dict[str, str]:
    """Return {service: state} for app-tier services currently running under Compose.

    In the native mode `nyxgpt ops install` targets, `api`/`web`/`ollama` run
    natively and `cassandra` runs as the one ops-managed Docker container
    (`_ensure_cassandra_container`) -- none of `CORE_APP_SERVICES` should also be
    running as Docker Compose services. A prior raw `docker compose up` (or the
    pre-#3231 observability bring-up, which started every profile-less default
    service too) can leave these running alongside the native ones, colliding on
    the same ports.
    """
    compose = _compose_stack_snapshot()
    return {
        service: state
        for service, state in compose.items()
        if service in CORE_APP_SERVICES and state == "running"
    }


def _reconcile_phantom_compose_app_containers() -> list[OpsResult]:
    """Stop any leaked Compose app-tier containers (api/web/ollama/cassandra).

    Uses `docker compose stop <service>` (not `down`) so containers/volumes are
    preserved -- consistent with `nyxgpt ops` avoiding destructive actions by
    default. This is what makes `nyxgpt ops install` a reconciler instead of an
    additive-only installer: re-running it now cleans up a mixed-mode mess left
    by an earlier run (or a raw `docker compose up`) instead of adding to it.
    """
    if _which("docker") is None:
        return []

    phantoms = _detect_phantom_compose_app_containers()
    if not phantoms:
        return [OpsResult(True, "No phantom Docker Compose app-tier containers detected")]

    results: list[OpsResult] = []
    for service in sorted(phantoms):
        cp = _run(
            ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "stop", service],
            check=False,
        )
        if cp.returncode == 0:
            results.append(
                OpsResult(
                    True,
                    f"Stopped phantom Compose container for {service} (was running "
                    "alongside the native/local deployment)",
                )
            )
        else:
            details = _output_excerpt(cp)
            results.append(
                OpsResult(
                    False,
                    f"Failed to stop phantom Compose container for {service}",
                    details.strip(),
                )
            )
    return results


def _cp_details(cp: subprocess.CompletedProcess[str]) -> str:
    """Concatenate stdout+stderr from a CompletedProcess into one details string."""
    stdout = (cp.stdout or "").strip()
    stderr = (cp.stderr or "").strip()
    return (stdout + ("\n" + stderr if stderr else "")).strip()


# The single statement of what deploys where, shared by `_resolve_locality`'s
# rejection below and by the `--cloud` help text in `nyxgpt.cli` so the two
# cannot drift (#3948). It says what `--cloud` is NOT ("this flag") and names
# the commands that do the job, because the previous wording ("not yet
# implemented -- --local is the precursor") read as "nyxGPT cannot deploy to a
# cloud target at all", which is false: `nyxgpt cloud infra apply` provisions
# the AWS substrate (with Terraform) and `nyxgpt cloud deploy` deploys this
# stack onto it. The unimplemented part is narrowly this flag.
CLOUD_DEPLOY_POINTER = (
    "cloud deployment is `nyxgpt cloud infra apply` to provision the AWS substrate "
    "and `nyxgpt cloud deploy` to deploy this stack onto it -- add --kubernetes there "
    "for a single-node k3s cluster running these same k8s/*.yaml manifests (#3956); "
    "see docs/cloud.md and docs/kubernetes.md"
)


def _resolve_locality(args) -> str | None:
    """Resolve the `--local`/`--cloud` locality shared by `--terraform`/`--kubernetes`.

    **Local is the default** (#3948). `--local` was previously required and
    was also the only accepted value, which made the user type the one legal
    answer to a question with no alternative; it is still accepted, as an
    explicit no-op, so existing scripts and docs keep working.

    `--cloud` is accepted by the CLI surface (so it doesn't need a redesign
    later) but rejected: *this flag* deploys to the local machine. That is a
    limit of the flag, not of the product -- see `CLOUD_DEPLOY_POINTER` for
    the commands that do deploy to a cloud target.

    The cloud target is not a second mode of this command: it is
    `nyxgpt cloud deploy`, which provisions the substrate, reaches the
    instance over the #3503 SSH path, and runs this very command *there*
    (#3956 -- `--kubernetes` on that command puts a single-node k3s cluster on
    the box and then runs `ops install --kubernetes` on it, which is #3506's
    "no new deployment code path" in one sentence). That is why the rejection
    points at a command rather than promising a future one: the older wording
    ("not yet implemented -- --local is the precursor to a future cloud
    target") was an expiry-dated world-state claim of exactly the kind #3744
    forbids, and by 2026-08-19 it had expired twice over -- `nyxgpt cloud
    deploy` shipped in #3513 and its Kubernetes mode in #3956.

    Returns "local", or None (having already printed an error) if `--cloud`
    was asked for.
    """
    if getattr(args, "cloud", False):
        print(
            "ERROR: --cloud is not implemented for `ops install --terraform/--kubernetes`, "
            f"which deploys to the local machine; {CLOUD_DEPLOY_POINTER}.",
            file=sys.stderr,
        )
        return None
    return "local"


def _resolve_api_key(explicit: str | None) -> str:
    """Resolve the auth API key for a Terraform/Kubernetes bootstrap: explicit, prompted, or random.

    Prefers an explicitly-passed `--api-key`. Otherwise, when stdin is a TTY,
    prompts for one (hidden input, never echoed or logged) so an operator can
    set a memorable key; if left blank -- or stdin isn't interactive at all
    (e.g. driven from the SRE/admin dashboard or a test) -- falls back to a
    random one so the command still completes non-interactively.
    """
    if explicit:
        return explicit
    if sys.stdin.isatty():
        try:
            entered = getpass.getpass(
                "API key for the deployed stack's [auth] section (blank to auto-generate): "
            )
        except Exception as e:
            logger.warning(
                "Interactive API key prompt failed, auto-generating one instead: %s",
                e,
                extra={"component": "ops"},
            )
            entered = ""
        if entered:
            return entered
    return secrets.token_hex(32)


def _refuse_port_collision(components: list[str]) -> OpsResult | None:
    """Refuse to bring up a new deployment on ports already bound by native/Compose.

    Terraform's containers and the native/Compose stack all default to the
    same host ports (8000/3000/11434/9042). Returns a failing `OpsResult`
    naming the conflicting components -- and what to run instead -- if any of
    `components` are already live natively or via Compose, else None.
    """
    mode = detect_deployment_mode()
    bound = [
        c
        for c in components
        if mode.native.get(c) in ("started", "running") or mode.compose.get(c) == "running"
    ]
    if not bound:
        return None
    return OpsResult(
        False,
        "Refusing to start: port collision with a running native/Compose stack",
        f"{', '.join(sorted(bound))} already serving via native/Compose on the same host "
        "port(s) -- run `nyxgpt ops down` (or stop the conflicting components) before "
        "bringing up the Terraform/Kubernetes deployment.",
    )


# --- Terraform local deployment (`nyxgpt ops install/down --terraform --local`) ---

# The Terraform working directory, materialized from the packaged
# `nyxgpt.resources/terraform/local` configuration (#3835) rather than read
# out of a checkout's `terraform/`: `--terraform --local` has to work on a
# machine that has no repository (CLAUDE.md's repo-less portability
# requirement), and package data is not a directory `terraform -chdir=` can
# be pointed at on every install (a wheel's data may be read-only, and
# `terraform init` writes a provider cache into the config directory). Same
# shape `nyxgpt cloud infra` already uses for `terraform/aws`
# (`cloud_infra.sync_terraform_config`), with one difference: state stays
# inside this directory, because `versions.tf`'s local backend path is
# relative to it and pre-#3835 installs kept their state next to the config
# too -- `_migrate_repo_terraform_state` moves exactly that file in.
TERRAFORM_DIR = NYXGPT_HOME / "terraform"

# Container names terraform/main.tf creates for the core stack (see docs/terraform.md).
TERRAFORM_CONTAINERS: dict[str, str] = {
    "ollama": "nyxgpt-tf-ollama",
    "cassandra": "nyxgpt-tf-cassandra",
    "api": "nyxgpt-tf-api",
    "web": "nyxgpt-tf-web",
}

# Terraform was pulled from homebrew-core after HashiCorp's 2023 BUSL relicense,
# so `brew install terraform` fails -- install from the official tap instead.
HASHICORP_TAP = "hashicorp/tap"

# Dev mode's image refs (`--terraform --dev`, #3835): built from the
# checkout's working tree and never pushed anywhere, so the tag says so.
# `_build_terraform_docker_images` builds them before `terraform apply`, and
# since #3984 that is the ONLY build in the dev path -- terraform/main.tf has
# no `build {}` block in any mode and simply consumes these tags.
TF_API_IMAGE = "nyxgpt-api:local"
TF_WEB_IMAGE = "nyxgpt-web:local"


# --- the artifact path's images (#3985) ---
#
# The Terraform artifact path builds its two images LOCALLY, from the
# published `nyxgpt-api-<version>.tar.gz` / `nyxgpt-web-<version>.tar.gz`
# source tarballs -- the same artifact channel the native install and the
# Kubernetes artifact path already use, and the same staging helper
# (`_stage_artifact_build_context`).
#
# It used to pull `ghcr.io/dkblinux98/nyxgpt-{api,web}` instead, with a
# `latest` fallback, and that could not install the builds acceptance testing
# actually runs, for two independent reasons (owner acceptance 2026-08-21,
# both re-verified 2026-08-22):
#
#   1. A RELEASE CANDIDATE PUBLISHES NO IMAGES. release-artifacts.yml's
#      `container-images` job triggers on `release: released`, and an rc is a
#      prerelease, which fires `prereleased`. So `ghcr.io/.../nyxgpt-api:
#      3.0.0rc13` is `manifest unknown`, the resolution fell back to `latest`
#      -- the previous *stable* release -- and the operator got a 2.1.0 stack
#      while believing they were testing 3.0.0rc13. The tarballs an rc does
#      publish need no new release machinery at all.
#   2. THE PUBLISHED IMAGES ARE amd64-ONLY, so the fallback could not even be
#      pulled on Apple Silicon ("no matching manifest for linux/arm64/v8").
#      release-artifacts.yml now builds `linux/amd64,linux/arm64` for the
#      consumers that do run published images (Docker/Compose --  see
#      docs/portability-matrix.md), but a locally built image is the host's
#      own architecture by construction, so this path no longer depends on
#      that being right.
#
# The tag is version-qualified AND namespaced to this path, and both halves
# are load-bearing:
#   - the version is what lets `ops status` name the build a Terraform
#     deployment is running (the install-mode marker records these refs, and
#     `InstallModeState._terraform_label` prints them);
#   - `artifact-` keeps it out of two tag namespaces already in use on the
#     same daemon: `nyxgpt-api:local` (dev mode here, and the Kubernetes
#     install's `K8S_IMAGE`) and `nyxgpt-api:<version>` (what `nyxgpt canary
#     deploy` builds -- `canary.IMAGE_REPOSITORY`). Sharing a tag would let
#     one path silently overwrite another's image.
def _terraform_artifact_image_ref(component: str, version: str) -> str:
    """The local tag the Terraform artifact path builds `component` at."""
    return f"nyxgpt-{component}:artifact-{version}"


# {component: (published service artifact, staged build-context directory
# name)}. The directory name is load-bearing: `_hash_paths` keys its
# fingerprint on the base directory's *name*, so staging the web tree as
# `web/` -- what the dev build's context is called -- lets identical content
# skip the rebuild when the operator toggles between `--dev` and the artifact
# path on an unchanged tree. Shared with the Kubernetes artifact path
# (`K8S_IMAGE_ARTIFACTS`), which builds the same two images from the same two
# tarballs; defined here because this is the earlier of the two in the file.
ARTIFACT_IMAGE_SOURCES: dict[str, tuple[str, str]] = {
    "api": ("nyxgpt-api", "context"),
    "web": ("nyxgpt-web", "web"),
}

# Where the Terraform artifact path unpacks those tarballs into build
# contexts. Deliberately not `K8S_BUILD_DIR`: a machine can carry both
# deployments, and a shared staging root would let one install's `rmtree`
# land in the middle of the other's build.
TF_BUILD_DIR = NYXGPT_HOME / "build" / "terraform"

# Operator/CI override for the artifact path's image refs, mirroring
# `NYXGPT_ARTIFACT_DIR` for the native tarballs (`_staged_service_tarball`):
# it names images already present in (or pullable by) the local Docker
# daemon, so an operator can deploy a specific published or hand-built image
# without going through the tarball build at all.
TF_IMAGE_ENV_OVERRIDES: dict[str, str] = {
    "api": "NYXGPT_TF_API_IMAGE",
    "web": "NYXGPT_TF_WEB_IMAGE",
}

# Baked into the web UI's client bundle at build time (Next.js NEXT_PUBLIC_*
# semantics) by every build of that image -- the Terraform dev and artifact
# builds here, and `_build_and_load_k8s_web_image`/`canary.deploy` for the
# cluster. A container's bundle is only ever loaded by a browser on the
# operator's own machine, so the host-local default is right for all of them.
TF_WEB_API_BASE_URL_DEFAULT = "http://localhost:8000"


def _ensure_terraform_binary() -> list[OpsResult]:
    """Ensure `terraform` is on PATH, installing it via the HashiCorp tap if missing."""
    if _which("terraform") is not None:
        return [OpsResult(True, "terraform already installed")]
    if _which("brew") is None:
        return [
            OpsResult(
                False,
                "terraform not found and Homebrew is unavailable to install it",
                "Install Terraform >= 1.5.0 manually: "
                "https://developer.hashicorp.com/terraform/install",
            )
        ]
    cp = _run(["brew", "tap", HASHICORP_TAP], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, f"brew tap {HASHICORP_TAP} failed", _cp_details(cp))]
    cp = _run(["brew", "install", f"{HASHICORP_TAP}/terraform"], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, f"brew install {HASHICORP_TAP}/terraform failed", _cp_details(cp))]
    if _which("terraform") is None:
        return [OpsResult(False, "terraform install reported success but binary still not on PATH")]
    return [OpsResult(True, f"Installed terraform via {HASHICORP_TAP}")]


def _packaged_local_terraform_dir() -> Path:
    """Path to the packaged local-stack Terraform configuration inside `nyxgpt.resources`."""
    return _packaged_resources_root() / "terraform" / "local"


def _migrate_repo_terraform_state() -> list[OpsResult]:
    """Adopt a pre-#3835 checkout-resident deployment's state and tfvars.

    Before #3835 the working directory *was* `<checkout>/terraform`, so an
    existing deployment's state file lives there. Leaving it behind would
    orphan a live stack: `terraform apply` from the new directory would plan
    to create the four `nyxgpt-tf-*` containers that already exist (name
    conflicts), and `nyxgpt ops down --terraform` would destroy nothing while
    reporting success.

    The tfvars comes with it, for a quieter but worse reason: it carries the
    deployment's `auth_api_key`, and `_ensure_terraform_tfvars` generates a
    fresh random one when there is no file. Without this the first apply
    after an upgrade would rotate the key of a stack the operator is already
    using, with nothing on screen to say so.

    Copies (never moves), so the old directory stays readable if anything
    about this needs checking afterwards, and only into a location that has
    none of its own -- anything already written here is authoritative.
    """
    old_dir = REPO_ROOT / "terraform"
    results: list[OpsResult] = []
    new_state = TERRAFORM_DIR / "terraform.tfstate"
    old_state = old_dir / "terraform.tfstate"
    if not new_state.exists() and old_state.is_file():
        try:
            data = json.loads(old_state.read_text(encoding="utf-8"))
            has_resources = bool(data.get("resources"))
        except (OSError, ValueError):
            has_resources = False
        # A post-destroy (empty) state records nothing worth carrying over --
        # the fresh directory is equivalent, and copying it would only make
        # this look like a migration happened.
        if has_resources:
            try:
                _ensure_dir(TERRAFORM_DIR)
                shutil.copy2(old_state, new_state)
                backup = old_state.with_suffix(".tfstate.backup")
                if backup.is_file():
                    shutil.copy2(backup, new_state.with_suffix(".tfstate.backup"))
            except OSError as e:
                return [
                    OpsResult(
                        False,
                        "Failed to migrate the checkout's Terraform state into the "
                        "ops-managed directory",
                        f"{old_state} -> {new_state}: {type(e).__name__}: {e}",
                    )
                ]
            results.append(
                OpsResult(
                    True,
                    "Migrated the checkout's Terraform state into the ops-managed directory",
                    f"{old_state} -> {new_state}",
                )
            )

    new_tfvars = TERRAFORM_DIR / "terraform.tfvars"
    old_tfvars = old_dir / "terraform.tfvars"
    if not new_tfvars.exists() and old_tfvars.is_file():
        try:
            _ensure_dir(TERRAFORM_DIR)
            shutil.copy2(old_tfvars, new_tfvars)
            os.chmod(new_tfvars, 0o600)
        except OSError as e:
            return results + [
                OpsResult(
                    False,
                    "Failed to migrate the checkout's terraform.tfvars into the "
                    "ops-managed directory",
                    f"{old_tfvars} -> {new_tfvars}: {type(e).__name__}: {e}",
                )
            ]
        results.append(
            OpsResult(
                True,
                "Migrated the checkout's terraform.tfvars (keeping the deployment's "
                "auth key) into the ops-managed directory",
                f"{old_tfvars} -> {new_tfvars}",
            )
        )
    return results


def _sync_local_terraform_config() -> list[OpsResult]:
    """Materialize the packaged local-stack Terraform configuration into `TERRAFORM_DIR`.

    Idempotent: overwrites the `.tf` sources (so an upgraded nyxGPT always
    applies its own configuration) while leaving this working directory's
    `.terraform/` provider cache, `terraform.tfvars` and state alone. Runs
    the pre-#3835 state migration first so a machine that deployed from a
    checkout keeps operating on the same stack (see
    `_migrate_repo_terraform_state`).
    """
    source = _packaged_local_terraform_dir()
    if not source.is_dir():
        return [
            OpsResult(
                False,
                f"Packaged Terraform configuration not found at {source}",
                "This nyxGPT installation is incomplete -- reinstall the package.",
            )
        ]
    results = _migrate_repo_terraform_state()
    try:
        _ensure_dir(TERRAFORM_DIR)
        shutil.copytree(
            source,
            TERRAFORM_DIR,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".terraform", "*.tfstate", "*.tfstate.*", "*.tfvars"),
        )
    except OSError as e:
        return results + [
            OpsResult(
                False,
                f"Failed to materialize the Terraform configuration in {TERRAFORM_DIR}",
                f"{type(e).__name__}: {e}",
            )
        ]
    return results + [OpsResult(True, f"Synced the Terraform configuration to {TERRAFORM_DIR}")]


def _ensure_terraform_tfvars(api_key: str | None) -> list[OpsResult]:
    """Bootstrap `~/.nyxGPT/terraform/terraform.tfvars` from the example, once.

    Deliberately never rewrites an existing file: without `--api-key` the key
    is auto-generated (`_resolve_api_key`), so regenerating tfvars on every
    install would rotate the deployed stack's auth key behind the operator's
    back. Everything that varies per run -- the install mode, the images, the
    dev-mode build context -- is passed as `-var` instead of stored here (see
    `_terraform_init_plan_apply`).
    """
    tfvars = TERRAFORM_DIR / "terraform.tfvars"
    if tfvars.exists():
        return [OpsResult(True, f"{tfvars} already exists")]
    example = TERRAFORM_DIR / "terraform.tfvars.example"
    if not example.exists():
        return [OpsResult(False, f"Missing {example} to bootstrap tfvars from")]
    key = _resolve_api_key(api_key)
    text = example.read_text(encoding="utf-8")
    text = re.sub(r'auth_api_key\s*=\s*".*"', lambda _m: f'auth_api_key = "{key}"', text)
    tfvars.write_text(text, encoding="utf-8")
    os.chmod(tfvars, 0o600)
    return [OpsResult(True, f"Bootstrapped {tfvars} from terraform.tfvars.example")]


def _terraform_image_vars(images: dict[str, str]) -> list[str]:
    """The `-var` arguments that put `images` into the plan.

    Passed per run rather than persisted in tfvars so switching between
    `--dev` and the artifact path is a single command with no leftover state:
    the image refs are the only thing that differs between the two modes in
    the plan, so the next apply simply replaces them.

    Since #3984 this is the *whole* mode-dependent surface. It used to also
    carry `build_from_source` and `repo_path`, which drove a `dynamic "build"`
    block in terraform/main.tf; that block is gone, because ops has already
    built or staged both images by the time this runs and the provider's own
    build cannot complete on Docker 29.x with the containerd image store. See
    `docker_image.api` in terraform/main.tf.
    """
    return [
        f"-var=api_image={images['api']}",
        f"-var=web_image={images['web']}",
    ]


def _terraform_init_plan_apply(images: dict[str, str] | None = None) -> list[OpsResult]:
    """Run `terraform init` -> `plan` -> `apply`, stopping at the first failure."""
    var_args = _terraform_image_vars(images or {"api": TF_API_IMAGE, "web": TF_WEB_IMAGE})
    chdir = f"-chdir={TERRAFORM_DIR}"
    cp = _run(["terraform", chdir, "init", "-input=false"], check=False, stream_stdout=True)
    if cp.returncode != 0:
        return [OpsResult(False, "terraform init failed", _cp_details(cp))]
    results = [OpsResult(True, "terraform init")]

    cp = _run(
        ["terraform", chdir, "plan", "-input=false", *var_args, "-out=tfplan"],
        check=False,
        stream_stdout=True,
    )
    if cp.returncode != 0:
        results.append(OpsResult(False, "terraform plan failed", _cp_details(cp)))
        return results
    results.append(OpsResult(True, "terraform plan"))

    cp = _run(
        ["terraform", chdir, "apply", "-input=false", "-auto-approve", "tfplan"],
        check=False,
        stream_stdout=True,
    )
    if cp.returncode != 0:
        results.append(OpsResult(False, "terraform apply failed", _cp_details(cp)))
        return results
    results.append(OpsResult(True, "terraform apply"))
    return results


def _terraform_override_image(component: str) -> tuple[str | None, list[OpsResult]]:
    """Resolve `component` from its `NYXGPT_TF_{API,WEB}_IMAGE` override, if set.

    Returns `(None, [])` -- not a failure -- when the variable is unset, which
    is the signal to the caller to take the normal artifact build. When it IS
    set, the image must be present locally or pullable; anything else is a
    failure, because an operator who named a specific image must never be
    given a different one (#3985).
    """
    override = os.environ.get(TF_IMAGE_ENV_OVERRIDES[component], "").strip()
    if not override:
        return None, []
    cp = _run(["docker", "image", "inspect", override], check=False, expected=True)
    if cp.returncode == 0:
        return override, [
            OpsResult(
                True,
                f"terraform {component} image: {override} "
                f"(staged via {TF_IMAGE_ENV_OVERRIDES[component]})",
            )
        ]
    cp = _run(["docker", "pull", override], check=False)
    if cp.returncode == 0:
        return override, [
            OpsResult(
                True,
                f"terraform {component} image: pulled {override} "
                f"(named by {TF_IMAGE_ENV_OVERRIDES[component]})",
            )
        ]
    return None, [
        OpsResult(
            False,
            f"{TF_IMAGE_ENV_OVERRIDES[component]}={override} is neither present locally "
            "nor pullable",
            _cp_details(cp),
        )
    ]


def _build_terraform_artifact_image(
    component: str, version: str
) -> tuple[str | None, list[OpsResult]]:
    """Build `component`'s image from its published source tarball at `version`.

    The artifact path's unit of work (#3985). Stages the published
    `nyxgpt-{api,web}-<version>.tar.gz` into a docker build context
    (`_stage_artifact_build_context` -- vendored from a checkout when there is
    one, taken from `$NYXGPT_ARTIFACT_DIR` when staged, else downloaded from
    the GitHub Release) and builds it into
    `_terraform_artifact_image_ref(component, version)`.

    `ref` is None on failure, and the results name `version` when they say so:
    there is deliberately NO fallback to another release. Substituting one was
    the defect -- an operator asked for 3.0.0rc13 and got 2.1.0 -- so a version
    whose artifacts cannot be resolved fails the install instead.
    """
    ref = _terraform_artifact_image_ref(component, version)
    service, context_name = ARTIFACT_IMAGE_SOURCES[component]
    try:
        context = _stage_artifact_build_context(service, context_name, TF_BUILD_DIR)
    except (RuntimeError, OSError, tarfile.TarError) as e:
        return None, [
            OpsResult(
                False,
                f"Could not stage the published {service} {version} artifact to build the "
                f"terraform {component} image from",
                f"{type(e).__name__}: {e}\n"
                f"Stage it in $NYXGPT_ARTIFACT_DIR, set "
                f"{TF_IMAGE_ENV_OVERRIDES[component]} to an image this machine can reach, "
                "or run with --dev from a checkout to build the working tree.",
            )
        ]

    if component == "web":
        # Mirrors `_build_terraform_docker_images`' web build: fingerprint the
        # tree itself rather than the whole context, skipping the gitignored
        # vendor/build output, and bake in the API base URL the bundle calls.
        build_kwargs: dict[str, Any] = {
            "fingerprint_paths": [context],
            "excludes": _WEB_VENDOR_EXCLUDES,
            "build_args": {"NEXT_PUBLIC_API_BASE_URL": TF_WEB_API_BASE_URL_DEFAULT},
        }
    else:
        build_kwargs = {
            "fingerprint_paths": [context / rel for rel in _API_IMAGE_FINGERPRINT_RELPATHS],
        }
    try:
        decision = _docker_build_if_needed(
            ref, context, marker_dir=DOCKER_IMAGE_MARKER_DIR, **build_kwargs
        )
    except RuntimeError as e:
        return None, [OpsResult(False, f"docker build {ref} failed", str(e))]
    return ref, [
        OpsResult(
            True,
            f"terraform {component} image: {ref} ({decision})",
            f"built from the published {service}-{version}.tar.gz",
        )
    ]


def _build_terraform_artifact_images(images: dict[str, str]) -> list[OpsResult]:
    """Resolve both api/web images the artifact-path deploy runs (#3835, #3985).

    The artifact path is the default and the only one a machine with no
    checkout can take: nothing here reads `REPO_ROOT`, and `terraform apply`
    gets finished image refs rather than a build context (see
    `_terraform_image_vars`). Fills `images` in place -- the shared dict
    `_install_terraform_steps` threads through its steps.

    Two sources per component, each reported so the operator can see which one
    answered: the `NYXGPT_TF_{API,WEB}_IMAGE` override (an image already in,
    or pullable by, the local daemon), and otherwise a local build of the
    published source tarball at THIS nyxGPT's own version. Stops at the first
    failure rather than deploying a half-resolved pair.
    """
    if _which("docker") is None:
        return [
            OpsResult(False, "docker not found on PATH -- cannot build the nyxgpt api/web images")
        ]
    version = _native_service_version()
    results: list[OpsResult] = []
    for component in ("api", "web"):
        ref, component_results = _terraform_override_image(component)
        if not component_results:
            ref, component_results = _build_terraform_artifact_image(component, version)
        results += component_results
        if ref is None:
            return results
        images[component] = ref
    return results


def _build_terraform_docker_images() -> list[OpsResult]:
    """Build the `nyxgpt-api`/`nyxgpt-web` images the Terraform `--local --dev`
    deploy consumes, skipping each build the app source hasn't changed since (#3414).

    Dev mode only (#3835): the artifact path builds the same two images from
    the published source tarballs instead (`_build_terraform_artifact_images`)
    and never needs a checkout. Runs before `terraform init/plan/apply` so
    `docker_image.api`/`.web` in terraform/main.tf (the `local` tags, matching
    `TF_API_IMAGE`/`TF_WEB_IMAGE` here) already exist locally when the plan
    resolves them: unchanged source means `_docker_build_if_needed` skips the
    rebuild entirely (reported below, mirroring the Homebrew
    `_install_homebrew_api`/`_web` decision output); changed source means it
    rebuilds now, and the new image id is what the next apply rolls the
    containers onto. Terraform itself builds nothing (#3984).
    """
    if _which("docker") is None:
        return [OpsResult(False, "docker not found on PATH -- cannot build nyxgpt-api/nyxgpt-web")]

    results: list[OpsResult] = []
    try:
        decision = _docker_build_if_needed(
            TF_API_IMAGE,
            REPO_ROOT,
            fingerprint_paths=_API_IMAGE_FINGERPRINT_PATHS,
            marker_dir=DOCKER_IMAGE_MARKER_DIR,
        )
        results.append(OpsResult(True, f"{TF_API_IMAGE}: {decision}"))
    except RuntimeError as e:
        results.append(OpsResult(False, f"docker build {TF_API_IMAGE} failed", str(e)))

    try:
        decision = _docker_build_if_needed(
            TF_WEB_IMAGE,
            REPO_ROOT / "web",
            fingerprint_paths=[REPO_ROOT / "web"],
            excludes=_WEB_VENDOR_EXCLUDES,
            build_args={"NEXT_PUBLIC_API_BASE_URL": TF_WEB_API_BASE_URL_DEFAULT},
            marker_dir=DOCKER_IMAGE_MARKER_DIR,
        )
        results.append(OpsResult(True, f"{TF_WEB_IMAGE}: {decision}"))
    except RuntimeError as e:
        results.append(OpsResult(False, f"docker build {TF_WEB_IMAGE} failed", str(e)))

    return results


def terraform_stack_state() -> dict[str, str]:
    """{component: docker state} for the Terraform-managed containers (used by status/doctor).

    A value may be `DOCKER_STATE_UNKNOWN` (#4022): these reads sit on exactly
    the same denied-socket path as the native Cassandra one -- the same
    `docker ps` against the same daemon -- so they got the same fix rather
    than being left as the next sighting of it. Use `_container_deployed`
    rather than `!= "absent"` when inferring whether a Terraform stack exists
    here: an unreadable container is evidence of neither presence nor absence.

    The *reason* an entry is unknown is not carried here, because there is
    only ever one: this process cannot reach the Docker daemon. Callers read
    it from `DeploymentMode.docker_probe_reason`, which the native Cassandra
    read on the same daemon has already recorded.
    """
    return {
        component: _docker_container_state(name) for component, name in TERRAFORM_CONTAINERS.items()
    }


def _terraform_state_has_resources() -> bool:
    """True if terraform.tfstate records any managed resources.

    ``terraform destroy`` (what ``nyxgpt ops down --terraform`` runs) always
    leaves ``terraform.tfstate`` in place with an empty ``resources`` list --
    that's a clean post-destroy state, not stale state. Only a tfstate that
    still records resources (with no matching containers running) is stale.
    """
    tfstate = TERRAFORM_DIR / "terraform.tfstate"
    try:
        data = json.loads(tfstate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return bool(data.get("resources"))


def _terraform_stack_health() -> list[OpsResult]:
    """Report each Terraform-managed container's state, plus the stack's output URLs.

    An unreadable container is reported as unreadable, not as down (#4022): a
    denied Docker socket says nothing about whether the container is running,
    and `[FAIL] terraform cassandra: absent` for a healthy stack is the same
    false negative on the CLI that the dashboard was showing.
    """
    results = [
        OpsResult(
            state == "running",
            f"terraform {component}: {state}"
            + (
                " (this session cannot read Docker container state)"
                if state == DOCKER_STATE_UNKNOWN
                else ""
            ),
            TERRAFORM_CONTAINERS[component],
            status="UNKNOWN" if state == DOCKER_STATE_UNKNOWN else "",
        )
        for component, state in terraform_stack_state().items()
    ]
    cp = _run(["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-json"], check=False)
    if cp.returncode == 0 and (cp.stdout or "").strip():
        try:
            outputs = json.loads(cp.stdout)
            urls = ", ".join(f"{k}={v.get('value')}" for k, v in outputs.items())
            results.append(OpsResult(True, f"terraform outputs: {urls}"))
        except (ValueError, AttributeError):
            pass
    return results


def _record_terraform_install_mode(dev: bool, images: dict[str, str]) -> list[OpsResult]:
    """Record which mode -- and which images -- this Terraform deployment runs (#3835).

    Its own marker, never the native one (`nyxgpt.install_mode`): the two
    deployments are installed independently, and the native marker also
    decides whether `restart api` drives launchd or `brew services`, so
    writing it here would break the native services this deploy does not
    touch. Written before `terraform apply` so a failed apply still leaves
    `ops status`/`doctor` reporting the mode that was attempted rather than
    the previous deployment's.
    """
    checkout = REPO_ROOT if dev else None
    target = INSTALL_MODE_DEV if dev else INSTALL_MODE_ARTIFACT
    marker = write_install_mode(target, checkout, substrate=SUBSTRATE_TERRAFORM, images=images)
    state = InstallModeState(
        mode=target,
        checkout=str(checkout) if checkout else None,
        substrate=SUBSTRATE_TERRAFORM,
        images=images,
        # The marker was just written, so this state is recorded by
        # construction -- not the artifact default (#3835).
        recorded=True,
    )
    return [OpsResult(True, f"Terraform install mode: {state.label()}", str(marker))]


def _resolve_terraform_images(dev: bool, images: dict[str, str]) -> list[OpsResult]:
    """Resolve the api/web images the terraform plan references.

    Module scope, not nested inside `_install_terraform_steps`, because a
    nested step cannot be patched by name: `patch.object(ops, ...)` only
    reaches module attributes. While this was a local, the step-isolation
    guard could not neutralize it, so every unit test that patched "all" the
    install steps still ran a real `docker pull` against the runner -- six
    minutes of network I/O per suite run, failing on any machine without a
    docker daemon. Behavior is unchanged; only the binding moved, with `dev`
    and the shared `images` dict now passed explicitly.
    """
    if dev:
        return _build_terraform_docker_images()
    return _build_terraform_artifact_images(images)


def _install_terraform_steps(api_key: str | None, dev: bool = False) -> list[OpsResult]:
    """Run the Terraform bring-up steps and return structured results (no printing).

    Ensures terraform is present (installing via the hashicorp tap if
    missing), materializes the packaged Terraform configuration into
    `TERRAFORM_DIR`, bootstraps terraform.tfvars from the example if absent,
    resolves the api/web images for this mode, runs init -> plan -> apply,
    and reports the resulting stack health. Stops at the first failing step
    since each depends on the last (installing the binary before generating
    tfvars before running init, etc.) -- unlike `install()`'s native steps,
    which are independent and best-effort.

    `dev=True` builds the api/web images from `REPO_ROOT`'s working tree
    (the pre-#3835 behavior, now opt-in); the default artifact path builds
    them from the published source tarballs at this nyxGPT's own version and
    touches no checkout at all (#3985).

    Shared by the `nyxgpt ops install --terraform --local` CLI entrypoint
    (`_install_terraform`) and `install_terraform_local`, the SRE/admin
    dashboard API's structured equivalent.
    """
    collision = _refuse_port_collision(["api", "web", "ollama", "cassandra"])
    if collision is not None:
        _record_ops_action("install", "terraform", "refused", collision.message)
        return [collision]

    logger.info(
        "ops: install --terraform --local starting (mode=%s)",
        INSTALL_MODE_DEV if dev else INSTALL_MODE_ARTIFACT,
        extra={
            "component": "ops",
            "action": "install-terraform",
            "install_mode": INSTALL_MODE_DEV if dev else INSTALL_MODE_ARTIFACT,
        },
    )

    # Filled in by the image step below and read by the two steps after it.
    # A dict rather than a return value because every step in this list has
    # the same `() -> list[OpsResult]` shape.
    images: dict[str, str] = {"api": TF_API_IMAGE, "web": TF_WEB_IMAGE} if dev else {}

    results: list[OpsResult] = []
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        # Must run first: `_start_observability_stack_terraform` targets
        # self_heal.COMPOSE_FILE and TERRAFORM_NET_OVERRIDE, both of which
        # live under NYXGPT_HOME once synced from the packaged resources
        # (#3621).
        ("sync packaged ops resources", _sync_packaged_resources),
        (
            "clear intentional-stop markers",
            lambda: _clear_intentional_stops(["api", "web", "ollama", "cassandra"]),
        ),
        # Must run before terraform apply: main.tf no longer declares the
        # `nyxgpt_tf_*` docker_volume resources (#3346), so apply would
        # otherwise destroy them -- along with any not-yet-migrated data --
        # as part of reconciling state to the new host-bind-mount config.
        ("migrate legacy volumes", migrate_legacy_volumes),
        ("terraform binary", _ensure_terraform_binary),
        # Must run before tfvars and apply: it puts the .tf sources (and any
        # state migrated out of a pre-#3835 checkout) in the working
        # directory both of them operate on (#3835).
        ("terraform configuration", _sync_local_terraform_config),
        ("terraform tfvars", lambda: _ensure_terraform_tfvars(api_key)),
        # Must run before apply: terraform/main.tf bind-mounts
        # docker/config.docker.ini into the api container (same pattern as
        # docker-compose.yml), so the derived file has to exist first or
        # Docker creates an empty directory in its place.
        ("compose config (derive from native)", _generate_compose_config),
        # Must run before apply: builds the api/web images the plan
        # references, since terraform builds nothing itself (#3984). In dev
        # mode the source is the working tree; on the artifact path it is the
        # published `nyxgpt-{api,web}-<version>.tar.gz` source tarballs, which
        # is what makes this deploy possible with no checkout at all (#3835)
        # AND at a release candidate's own version (#3985). Either way an
        # unchanged source skips the rebuild (#3414).
        ("api/web images", lambda: _resolve_terraform_images(dev, images)),
        # After the images are known, before apply: the marker is what `ops
        # status`/`doctor` read to report this deployment's mode instead of
        # the native services' (#3835).
        ("terraform install mode", lambda: _record_terraform_install_mode(dev, images)),
        ("terraform init/plan/apply", lambda: _terraform_init_plan_apply(images)),
        # Must run after apply (the ollama container has to exist) and before
        # the stack is called up: `terraform apply` returns as soon as the
        # container is created, so the pull waits for the server to answer on
        # the host-published port first. Same required models, same config
        # keys, as every other run mode (#3824).
        ("required models", _ensure_required_models),
        # Must run before the observability stack starts: Grafana's Compose
        # bind-mount auto-creates a missing ~/.nyxGPT/secrets root-owned on
        # Linux (#3432), which then blocks the token write below.
        ("glitchtip secrets dir", _ensure_glitchtip_secrets_dir),
        # Must run before the observability stack starts: Grafana's alerting
        # provisioning (docker/grafana/provisioning/alerting/contact-points.yml)
        # unconditionally reads $__file{/etc/nyxgpt-secrets/slack-webhook-url}
        # and refuses to boot if it's missing -- the native install() path
        # writes this placeholder-or-real secret via the same function before
        # starting its observability stack; the terraform path must too (#3588).
        ("slack webhook secret", _sync_grafana_slack_webhook_secret),
        # After apply (network + core containers exist): bring the observability
        # stack up on the terraform network and auto-provision GlitchTip, so the
        # terraform deploy has the same full SRE view as the native install.
        ("observability stack (terraform network)", _start_observability_stack_terraform),
        ("glitchtip auto-provisioning", _provision_glitchtip),
    ]
    for step_name, fn in steps:
        try:
            step_results = fn()
        except Exception as e:
            results.append(
                OpsResult(False, f"terraform {step_name} raised", f"{type(e).__name__}: {e}")
            )
            step_results = []
        results += step_results
        if step_results and not all(r.ok for r in step_results):
            break
    else:
        results += _terraform_stack_health()

    result, message = _ops_action_outcome(results)
    _record_ops_action("install", "terraform", result, message)
    return results


def install_terraform_local(api_key: str | None = None, dev: bool = False) -> list[OpsResult]:
    """Structured (non-printing) Terraform local bring-up, for the SRE/admin dashboard API.

    Runs the same steps as `nyxgpt ops install --terraform --local` --
    locality is implicitly "local" here since that's the only target this
    endpoint offers (see `_resolve_locality`) -- and returns the OpsResult
    list directly instead of routing it through `_emit_results`, so a
    FastAPI endpoint can translate it straight to JSON. `dev` defaults to
    False: the dashboard has no checkout to build from and must never
    silently deploy one.
    """
    return _install_terraform_steps(api_key, dev=dev)


def _install_terraform(args) -> int:
    """`nyxgpt ops install --terraform --local`: the full Terraform bring-up in one command."""
    if _resolve_locality(args) is None:
        return 2
    dev = bool(getattr(args, "dev", False))
    if dev and _dev_checkout_root() is None:
        # Checkout-only by definition -- dev mode builds the api/web images
        # from the working tree. Say so up front rather than failing at the
        # image build after terraform is already installed (#3835, mirroring
        # `install()`'s native refusal).
        print(
            "ERROR: --dev needs a source checkout, and this nyxgpt is running from an "
            f"installed package ({REPO_ROOT} has no pyproject.toml/src/nyxgpt/web).\n"
            "       Run `nyxgpt up --terraform --dev` from a clone of the "
            "repository, or drop --dev to deploy the published images.",
            file=sys.stderr,
        )
        return 2
    results = _install_terraform_steps(getattr(args, "api_key", None), dev=dev)
    ok = _emit_results("install --terraform", results)
    return 0 if ok else 2


def _down_terraform_steps() -> list[OpsResult]:
    """Tear down the Terraform-managed stack and return structured results.

    Brings the observability Compose stack down FIRST: `install --terraform
    --local` starts it on the `nyxgpt-terraform` network, and `terraform
    destroy` can't remove that network while those containers are still
    attached (it times out on the network delete). Then runs `terraform
    destroy` for the core stack.

    Syncs the packaged configuration into `TERRAFORM_DIR` first (#3835): a
    machine upgrading from a pre-#3835 install has its state in the
    checkout's `terraform/` and nothing in the ops-managed directory, and
    destroying from an empty directory would report success while leaving
    the whole stack running. The image refs are still passed (they are
    required variables of the plan being destroyed) but nothing builds or
    pulls them: since #3984 the configuration has no `build {}` block in any
    mode, so a dev-mode deployment whose checkout has since been deleted --
    or one whose images have been removed -- is still destroyable.
    """
    if _which("terraform") is None:
        results = [OpsResult(False, "terraform not found on PATH -- nothing to destroy")]
    else:
        results = _sync_local_terraform_config()
        results += _stop_observability_stack_terraform()
        recorded = read_install_mode(substrate=SUBSTRATE_TERRAFORM)
        images = {
            "api": recorded.images.get("api", TF_API_IMAGE),
            "web": recorded.images.get("web", TF_WEB_IMAGE),
        }
        cp = _run(
            [
                "terraform",
                f"-chdir={TERRAFORM_DIR}",
                "destroy",
                "-input=false",
                *_terraform_image_vars(images),
                "-auto-approve",
            ],
            check=False,
        )
        if cp.returncode == 0:
            results.append(OpsResult(True, "terraform destroy", _cp_details(cp)))
        else:
            results.append(OpsResult(False, "terraform destroy failed", _cp_details(cp)))

    # The `--terraform --local` deploy builds the nyxgpt-api/web images via
    # `docker build`, whose BuildKit layer cache balloons across repeated
    # deploys (17GB+ observed). Reclaim it on teardown -- "down" means we're
    # done, so the cache has served its purpose and the next deploy rebuilds it
    # as needed. NOTE: Docker's build cache is host-global, not per-project, so
    # this also clears cache for any other local Docker builds; that's an
    # acceptable trade on a dev/ops box and is what keeps disk from creeping up.
    # Best-effort: a prune failure never fails the teardown.
    if _which("docker") is not None:
        cp = _run(["docker", "builder", "prune", "-f"], check=False)
        reclaimed = next(
            (ln.strip() for ln in (cp.stdout or "").splitlines() if "reclaimed" in ln.lower()),
            "",
        )
        if cp.returncode == 0:
            results.append(
                OpsResult(
                    True, "docker build cache pruned" + (f" -- {reclaimed}" if reclaimed else "")
                )
            )
        else:
            results.append(OpsResult(True, f"docker build cache prune skipped ({_cp_details(cp)})"))

    result, message = _ops_action_outcome(results)
    _record_ops_action("down", "terraform", result, message)
    return results


def down_terraform() -> list[OpsResult]:
    """Structured (non-printing) `terraform destroy`, for the SRE/admin dashboard API."""
    return _down_terraform_steps()


def _down_terraform(_args) -> int:
    """`nyxgpt ops down --terraform`: `terraform destroy` the Terraform-managed stack."""
    results = _down_terraform_steps()
    ok = _emit_results("down --terraform", results)
    return 0 if ok else 2


# --- Kubernetes local deployment (`nyxgpt ops install/down --kubernetes --local`) ---

# The manifests `kubectl apply -k` applies, read from the ops-managed home
# rather than from a checkout (#3834). `k8s/` ships as package data
# (`nyxgpt.resources.k8s`) and `_sync_packaged_resources` copies it here, the
# same treatment #3621 gave the Compose file and the unit templates -- a
# machine with no checkout has no `<repo>/k8s` for `kubectl apply -k` to read,
# which is why `--kubernetes` could not run there at all. It also has to be a
# *writable* location: `_ensure_k8s_secret` writes `secret.yaml` next to the
# kustomization that references it, and site-packages is neither writable nor
# a place an API key belongs.
K8S_DIR = NYXGPT_HOME / "k8s"
K8S_NAMESPACE = "nyxgpt"

# Pod-name prefixes of the two *app* workloads, as distinct from the
# observability Pods that share the namespace (see `_k8s_app_pods_present`).
K8S_APP_POD_PREFIXES = ("nyxgpt-api-", "nyxgpt-web-")
K8S_IMAGE = "nyxgpt-api:local"

# The workload that serves this deployment's LLM, and the URL its clients use
# (#3987). `k8s/configmap.yaml` gives the api Pods `[ollama] base_url =
# http://ollama:11434`, so that -- not the host's `127.0.0.1:11434` -- is the
# Ollama a Kubernetes deployment's model readiness is a question about. The
# fully-qualified name is what gets *printed*: an operator reading a status
# report on the host has to be able to tell the two apart at a glance, and
# `http://ollama:11434` alone resolves to nothing there.
K8S_OLLAMA_WORKLOAD = "statefulset/ollama"
K8S_OLLAMA_BASE_URL = f"http://ollama.{K8S_NAMESPACE}.svc.cluster.local:11434"

# The deployment's data/LLM tier (#3786): the in-cluster Cassandra that holds
# chat sessions for every api replica and the in-cluster Ollama that answers
# them (k8s/statefulset-cassandra.yaml, k8s/statefulset-ollama.yaml). The
# install waits for both to report Ready, because "the api Pods are Running"
# is not the same thing as "a chat works" -- without this wait the command
# returns while Cassandra is still bootstrapping and Ollama is still pulling
# the default model, and the first thing the operator sees in the web UI is a
# failed session list.
#
# The timeouts are per-workload and generous on purpose: they cover a cold
# first boot (Cassandra initializing an empty data directory, Ollama pulling
# the default model over whatever link the workstation has), not a steady-state
# restart. Exceeding one is reported as a failure with the workload named, not
# silently ignored.
K8S_DATA_TIER_WORKLOADS: tuple[tuple[str, str, int], ...] = (
    ("statefulset/cassandra", "Cassandra (chat session store)", 600),
    ("statefulset/ollama", "Ollama (LLM, including the first default-model pull)", 900),
)

# The app tier's own rollout (#3827). The canary halves of both pairs ship at
# zero replicas by design (`nyxgpt canary start` scales them up), so only the
# stable Deployments are waited on -- a wait on a deliberately-empty Deployment
# would be a wait for Pods nobody asked for.
K8S_APP_TIER_WORKLOADS: tuple[tuple[str, str], ...] = (
    ("deploy/nyxgpt-api-stable", "nyxGPT API"),
    ("deploy/nyxgpt-web-stable", "nyxGPT web UI"),
)

# Shorter than the data tier's budgets on purpose: both images are built
# locally and side-loaded into the cluster by the install itself
# (`_build_and_load_k8s_image`), so there is no registry pull to absorb -- this
# covers scheduling and the readiness probes, not a download.
K8S_APP_TIER_ROLLOUT_TIMEOUT_S = 600

# The local cluster `nyxgpt ops install --kubernetes --local` provisions via `kind`
# when kubectl's current context has no reachable cluster (#3596, owner decision
# 2026-08-03). The name is reserved for nyxgpt: `nyxgpt ops down --kubernetes` only
# ever deletes a kind cluster with this exact name, which is what lets it tell a
# cluster it provisioned apart from a bring-your-own one (minikube, Docker Desktop,
# an operator's own differently-named kind cluster) without needing separate state.
KIND_CLUSTER_NAME = "nyxgpt-local"
KIND_CONTEXT = f"kind-{KIND_CLUSTER_NAME}"

# What the provisioned kind node publishes to the host, as
# `host port -> Service NodePort` (#3986). These are the two NodePorts pinned
# in `k8s/service-web.yaml` and `k8s/service.yaml`, mapped onto the same host
# ports every other local deployment mode binds (COMPOSE_COMPONENT_PORTS), so
# `http://127.0.0.1:3000` means the same thing in Kubernetes mode as it does
# natively.
#
# This is the half of #3986 that makes an install *finish* usable. A kind
# cluster created with no config publishes nothing at all, so the previous
# `kind create cluster --name nyxgpt-local --wait 60s` guaranteed that a
# successful install left no reachable UI -- the operator had to keep a
# foreground `kubectl port-forward` alive in a spare terminal, and that
# forward died with the Pod it attached to.
#
# Mapping has to be declared when the cluster is created (a running kind node
# is a container; its published ports cannot be added later), which is why the
# NodePort numbers are pinned in the manifests rather than allocated.
KIND_HOST_PORT_MAPPINGS: tuple[tuple[int, int], ...] = (
    (3000, 30300),
    (8000, 30800),
)

# Which Service each mapping publishes, and on which of its ports:
# `host port -> (Service, Service port, node port)`. Derived from
# KIND_HOST_PORT_MAPPINGS so the cluster's mapping and the Service patch
# cannot drift apart -- they are worthless independently.
#
# The NodePort is applied by `_publish_k8s_app_tier_nodeports` rather than
# declared in `k8s/service*.yaml`, and that is a security decision, not a
# stylistic one: those manifests are applied by the AWS k3s deployment too,
# whose invariant is that nothing but port 22 exists on the instance (#3503,
# docs/security.md). A NodePort in the base manifest would bind on that node's
# interfaces as well. Patching it on only where nyxGPT created the cluster AND
# mapped the ports to loopback keeps the base posture ClusterIP everywhere
# else -- including a bring-your-own local cluster, where opening node ports
# on someone else's cluster is not nyxGPT's call.
K8S_HOST_PUBLISHED_SERVICES: dict[int, tuple[str, int, int]] = {
    3000: ("nyxgpt-web", 3000, 30300),
    8000: ("nyxgpt-api", 8000, 30800),
}

# Where the generated kind cluster config is written. Under the ops-managed
# home, not a temp file, so an operator (or a support transcript) can see
# exactly what the cluster they are running was created from.
KIND_CLUSTER_CONFIG_FILE = NYXGPT_HOME / "k8s" / "kind-cluster.yaml"

# Official download endpoints for the two CLI tools the local Kubernetes path
# needs. Both are "latest/stable" aliases rather than pinned versions, so a
# clean machine gets a currently-supported binary without nyxGPT having to
# ship (and age out) a version table. kind publishes per-platform release
# assets under GitHub's `releases/latest/download` alias; kubectl publishes
# its current stable version number at `stable.txt`, which then names the
# binary path (see `_kubectl_download_url`).
KIND_DOWNLOAD_URL = (
    "https://github.com/kubernetes-sigs/kind/releases/latest/download/kind-{os}-{arch}"
)
# How long `_k3s_import_image` will wait for one image to land in k3s's
# containerd store. Generous: the api image is multi-GB and the import is a
# decompress-and-write of the whole thing onto the instance's root volume, on
# hardware an operator chose for running nyxGPT rather than for build
# throughput. Bounded all the same (D-027) -- `install_kubernetes_local` is
# reachable from an HTTP handler, and an unbounded pipe there is a worker held
# forever.
K3S_IMAGE_IMPORT_TIMEOUT_SECONDS = 900

KUBECTL_STABLE_URL = "https://dl.k8s.io/release/stable.txt"
KUBECTL_DOWNLOAD_URL = "https://dl.k8s.io/release/{version}/bin/{os}/{arch}/kubectl"


def _tool_platform() -> tuple[str, str] | None:
    """Return (os, arch) slugs for kind/kubectl release assets, or None if unsupported.

    Both projects use the same Go-style naming (`darwin`/`linux` x
    `amd64`/`arm64`), which covers every platform nyxGPT targets (macOS and
    Linux on Intel or ARM). Anything else falls back to an actionable
    "install it yourself" error rather than guessing at an asset name.
    """
    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(platform.machine().lower())
    if system is None or arch is None:
        return None
    return system, arch


def _ensure_nyxgpt_bin_on_path() -> Path:
    """Put `~/.nyxGPT/bin` on this process's PATH (idempotent) and return it.

    Anything `_ensure_cli_tool` downloads lands there, and every later
    `_which`/`_run` in the same process has to be able to find it -- a tool
    installed mid-run is useless if the run that installed it can't invoke
    it. Prepending to `os.environ["PATH"]` covers the current process and
    every subprocess it spawns; `_ensure_kubectl_and_cluster` and
    `_down_kubernetes_steps` call this first so a later `nyxgpt ops` run
    finds the tools again even if the operator never touched their shell
    profile. Creating the directory is left to `_download_tool_binary` (the
    only thing that writes into it) -- a PATH entry that doesn't exist yet is
    simply skipped by the lookup.
    """
    entry = str(NYXGPT_BIN_DIR)
    current = os.environ.get("PATH", "")
    if entry not in current.split(os.pathsep):
        os.environ["PATH"] = f"{entry}{os.pathsep}{current}" if current else entry
    return NYXGPT_BIN_DIR


def _download_tool_binary(name: str, url: str) -> tuple[bool, str]:
    """Download a single-file CLI binary from `url` into `~/.nyxGPT/bin/<name>`.

    Writes to a temp path and `replace()`s it into position so an interrupted
    download can never leave a truncated, executable binary behind. Returns
    (ok, details) rather than an OpsResult so callers can fold the details
    into whatever message fits their context.
    """
    bin_dir = _ensure_nyxgpt_bin_on_path()
    _ensure_dir(bin_dir)
    dest = bin_dir / name
    tmp = dest.with_name(f".{name}.download")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=300.0) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        tmp.chmod(0o755)
        tmp.replace(dest)
    except (httpx.HTTPError, OSError) as e:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return False, f"{url}: {type(e).__name__}: {e}"
    return True, f"downloaded {url} -> {dest}"


def _kind_download_url(system: str, arch: str) -> str:
    """Release-asset URL for the current kind build on this platform."""
    return KIND_DOWNLOAD_URL.format(os=system, arch=arch)


def _kubectl_download_url(system: str, arch: str) -> str:
    """Release URL for the current *stable* kubectl on this platform.

    kubectl has no "latest" alias, so the stable version number is fetched
    first (a few bytes of plain text) and interpolated into the binary path.
    """
    resp = httpx.get(KUBECTL_STABLE_URL, follow_redirects=True, timeout=30.0)
    resp.raise_for_status()
    version = resp.text.strip()
    return KUBECTL_DOWNLOAD_URL.format(version=version, os=system, arch=arch)


def _ensure_cli_tool(
    name: str,
    *,
    brew_formula: str,
    url_for: Callable[[str, str], str],
    manual_url: str,
) -> list[OpsResult]:
    """Ensure `name` is on PATH, installing it for the operator if it isn't (#3724).

    `nyxgpt ops install --kubernetes --local` is specified to bring a local
    Kubernetes deployment up on a machine that has nothing set up yet, so a
    missing `kind`/`kubectl` is nyxGPT's job to resolve, not a prompt to hand
    back to the operator (#3724: telling the user to go install kind was an
    acceptance failure against #3596). Two acquisition paths, in order:

    1. Homebrew, when present -- it's already the native-install prerequisite
       on macOS, and it keeps the tool upgradable through the operator's own
       package manager.
    2. A direct download of the official release binary into
       `~/.nyxGPT/bin` -- the Linux/no-brew path, and the fallback when a
       brew install fails (e.g. no formula for the platform). Needs no root.

    Only if both are unavailable does this fail, and then with a link the
    operator can follow -- never a raw command to paste (CLAUDE.md's
    Operational Command Wrapping rule).
    """
    _ensure_nyxgpt_bin_on_path()
    found = _which(name)
    if found is not None:
        return [OpsResult(True, f"{name} already installed", f"path: {found}")]

    notes: list[str] = []
    if _which("brew") is not None:
        cp = _run(["brew", "install", brew_formula], check=False)
        if cp.returncode == 0 and _which(name) is not None:
            return [OpsResult(True, f"Installed {name} via Homebrew", _cp_details(cp))]
        notes.append(
            f"Homebrew could not supply {name} (falling back to a direct download): "
            + (_cp_details(cp) or "install reported success but the binary was still missing")
        )

    target = _tool_platform()
    if target is None:
        notes.append(
            f"No prebuilt {name} for {platform.system()}/{platform.machine()}. "
            f"Install it manually: {manual_url}"
        )
        return [
            OpsResult(
                False, f"{name} is missing and nyxgpt cannot install it here", "\n".join(notes)
            )
        ]

    try:
        url = url_for(*target)
    except httpx.HTTPError as e:
        notes.append(f"Could not resolve the {name} download URL: {type(e).__name__}: {e}")
        return [
            OpsResult(
                False,
                f"{name} is missing and nyxgpt could not download it",
                "\n".join([*notes, f"Install it manually: {manual_url}"]),
            )
        ]

    ok, details = _download_tool_binary(name, url)
    notes.append(details)
    if not ok:
        notes.append(f"Install it manually: {manual_url}")
        return [
            OpsResult(
                False, f"{name} is missing and nyxgpt could not download it", "\n".join(notes)
            )
        ]
    if _which(name) is None:
        notes.append(f"Install it manually: {manual_url}")
        return [OpsResult(False, f"Installed {name} but it is still not on PATH", "\n".join(notes))]
    return [OpsResult(True, f"Installed {name} into {NYXGPT_BIN_DIR}", "\n".join(notes))]


def _ensure_kind_binary() -> list[OpsResult]:
    """Install `kind` if it's missing so a local cluster can be provisioned (#3724)."""
    return _ensure_cli_tool(
        "kind",
        brew_formula="kind",
        url_for=_kind_download_url,
        manual_url="https://kind.sigs.k8s.io/#installation",
    )


def _ensure_kubectl_binary() -> list[OpsResult]:
    """Install `kubectl` if it's missing -- same rationale as `_ensure_kind_binary` (#3724)."""
    return _ensure_cli_tool(
        "kubectl",
        brew_formula="kubernetes-cli",
        url_for=_kubectl_download_url,
        manual_url="https://kubernetes.io/docs/tasks/tools/",
    )


def _kind_cluster_exists(name: str = KIND_CLUSTER_NAME) -> bool:
    """Return whether a kind cluster named `name` already exists."""
    cp = _run(["kind", "get", "clusters"], check=False, expected=True)
    if cp.returncode != 0:
        return False
    return name in (cp.stdout or "").split()


def _kind_cluster_config() -> str:
    """Render the kind cluster config the provisioned local cluster is created from (#3986).

    One control-plane node carrying an `extraPortMappings` entry per
    `KIND_HOST_PORT_MAPPINGS`, which is what publishes the app tier's
    NodePorts on the host. Bound to `127.0.0.1` rather than `0.0.0.0`: this
    is a developer workstation cluster holding an api key, and #3195 already
    settled that nyxGPT's local surfaces are loopback-only.
    """
    lines = [
        "# Generated by `nyxgpt ops install --kubernetes` -- do not edit by hand.",
        "# Publishes the app tier's NodePorts on the host so the web UI is reachable",
        "# with no port-forward, and stays reachable across Pod replacement (#3986).",
        "kind: Cluster",
        "apiVersion: kind.x-k8s.io/v1alpha4",
        "nodes:",
        "  - role: control-plane",
        "    extraPortMappings:",
    ]
    for host_port, node_port in KIND_HOST_PORT_MAPPINGS:
        lines += [
            f"      - containerPort: {node_port}",
            f"        hostPort: {host_port}",
            '        listenAddress: "127.0.0.1"',
            "        protocol: TCP",
        ]
    return "\n".join(lines) + "\n"


def _create_kind_cluster(name: str = KIND_CLUSTER_NAME) -> list[OpsResult]:
    """Create the local `kind` cluster nyxgpt provisions when no cluster is reachable.

    Created **with a config** (#3986): `kind create cluster` with no config
    publishes no host ports, and every Service in `k8s/` used to be
    ClusterIP, so the cluster this produced had no host-reachable surface at
    all. See `KIND_HOST_PORT_MAPPINGS`.
    """
    try:
        KIND_CLUSTER_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        KIND_CLUSTER_CONFIG_FILE.write_text(_kind_cluster_config(), encoding="utf-8")
    except OSError as e:
        return [
            OpsResult(
                False,
                f"Could not write the kind cluster config ({KIND_CLUSTER_CONFIG_FILE})",
                f"{type(e).__name__}: {e}",
            )
        ]
    cp = _run(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            name,
            "--config",
            str(KIND_CLUSTER_CONFIG_FILE),
            "--wait",
            "60s",
        ],
        check=False,
    )
    if cp.returncode != 0:
        return [OpsResult(False, f"kind create cluster --name {name} failed", _cp_details(cp))]
    ports = ", ".join(str(host) for host, _node in KIND_HOST_PORT_MAPPINGS)
    return [
        OpsResult(
            True,
            f"Created local kind cluster: {name} (publishing host ports {ports})",
            _cp_details(cp),
        )
    ]


def _kind_node_container(name: str = KIND_CLUSTER_NAME) -> str:
    """Name of the kind cluster's control-plane node container (`kind`'s own convention)."""
    return f"{name}-control-plane"


def _kind_cluster_publishes_host_ports(name: str = KIND_CLUSTER_NAME) -> bool:
    """True if the running kind cluster publishes every `KIND_HOST_PORT_MAPPINGS` host port.

    Asked of the node **container**, not of the config file: an operator may
    be reusing a `nyxgpt-local` cluster created by an older nyxGPT (or by
    hand) that has no mappings, and a cluster's published ports cannot be
    changed after creation. A False here is what routes the install to the
    managed background forward instead of promising a URL that will not
    answer.
    """
    if _which("docker") is None:
        return False
    cp = _run(
        ["docker", "port", _kind_node_container(name)],
        check=False,
        expected=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if cp.returncode != 0:
        return False
    published = cp.stdout or ""
    return all(f":{host_port}" in published for host_port, _node in KIND_HOST_PORT_MAPPINGS)


def _delete_kind_cluster(name: str = KIND_CLUSTER_NAME) -> list[OpsResult]:
    """Delete the named kind cluster if it exists; no-op (not a failure) if it doesn't."""
    if not _kind_cluster_exists(name):
        return [OpsResult(True, f"kind cluster {name} already absent -- nothing to delete")]
    cp = _run(["kind", "delete", "cluster", "--name", name], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, f"kind delete cluster --name {name} failed", _cp_details(cp))]
    return [OpsResult(True, f"Deleted local kind cluster: {name}", _cp_details(cp))]


def _ensure_kubectl_and_cluster() -> list[OpsResult]:
    """Check `kubectl` is on PATH and a cluster is reachable, provisioning one if not.

    Bring-your-own-cluster stays supported unchanged: if kubectl's current context
    already reaches a cluster (minikube, Docker Desktop, an existing kind cluster,
    a remote context, ...) that cluster is used as-is and nothing is provisioned.
    Only when no cluster is reachable at all does this fall back to `kind` (#3596,
    owner decision 2026-08-03: kind is the provisioned local substrate) --
    reusing the `nyxgpt-local` cluster from a previous run if one is already there,
    or creating it fresh otherwise. `kubectl` and `kind` are installed here when
    they're missing (`_ensure_cli_tool`, #3724) rather than being handed back to
    the operator as homework: `--local` is meant to work on a machine with
    nothing set up. Docker stays a genuine external prerequisite (it needs a
    privileged system install / Docker Desktop), so a missing one produces an
    actionable error naming where to get it rather than a raw command to run.
    """
    results: list[OpsResult] = []
    _ensure_nyxgpt_bin_on_path()
    if _which("kubectl") is None:
        results += _ensure_kubectl_binary()
        if not all(r.ok for r in results):
            return results

    cp = _run(["kubectl", "cluster-info"], check=False, expected=True)
    if cp.returncode == 0:
        return results + [
            OpsResult(True, "Kubernetes cluster reachable", f"context: {_kubectl_context()}")
        ]

    if _which("kind") is None:
        results += _ensure_kind_binary()
        if not all(r.ok for r in results):
            return results
    if _which("docker") is None:
        return results + [
            OpsResult(
                False,
                "No reachable Kubernetes cluster, and kind needs Docker to create one",
                "Install/start Docker so `nyxgpt ops install --kubernetes` can "
                "provision a local kind cluster.",
            )
        ]

    if _kind_cluster_exists():
        cp = _run(["kubectl", "config", "use-context", KIND_CONTEXT], check=False)
        if cp.returncode != 0:
            return results + [
                OpsResult(
                    False, f"kubectl config use-context {KIND_CONTEXT} failed", _cp_details(cp)
                )
            ]
        results.append(OpsResult(True, f"Reusing existing kind cluster: {KIND_CLUSTER_NAME}"))
    else:
        results += _create_kind_cluster()
        if not all(r.ok for r in results):
            return results

    cp = _run(["kubectl", "cluster-info"], check=False)
    if cp.returncode != 0:
        return results + [
            OpsResult(False, "Provisioned kind cluster is not reachable", _cp_details(cp))
        ]
    return results + [
        OpsResult(True, "Kubernetes cluster reachable", f"context: {_kubectl_context()}")
    ]


# Where a Pod's ServiceAccount credentials are projected, and the environment
# variable the kubelet injects for the API server. Together they are what
# "running inside Kubernetes" means to a process (#3988): in-cluster
# authentication uses these, NOT a kubeconfig context, which is why the api
# Pod's `kubectl config current-context` is empty while `kubectl get pods` in
# that same Pod works.
K8S_SERVICEACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
K8S_IN_CLUSTER_ENV = "KUBERNETES_SERVICE_HOST"

# What `infra_status` reports as the Kubernetes "context" when the process is
# authenticated in-cluster. There is no context name to report -- saying so is
# more useful to an operator than an empty string that reads like "none".
K8S_IN_CLUSTER_CONTEXT_LABEL = "in-cluster (ServiceAccount)"


def _in_cluster() -> bool:
    """True when this process is running inside a Kubernetes Pod (#3988).

    Both signals, not either: the environment variable alone can be inherited
    by a shell an operator exported it in, and the token directory alone can
    be a stale mount. Requiring the pair is what keeps this from claiming
    in-cluster on a workstation.

    This exists because the Infrastructure page, served BY the api Pod, used
    to report its own cluster as NOT DEPLOYED -- the detection gate asked
    `kubectl config current-context`, and a Pod has no context.
    """
    if not os.environ.get(K8S_IN_CLUSTER_ENV):
        return False
    return (K8S_SERVICEACCOUNT_DIR / "token").exists()


def _kubectl_context() -> str:
    """Return kubectl's current context name (e.g. `kind-nyxgpt`, `docker-desktop`), or "" if unset.

    An empty string does NOT mean "no cluster" -- see `_in_cluster`. A process
    running in a Pod has no kubeconfig context and full API access; callers
    deciding whether a cluster is reachable must consider both.
    """
    cp = _run(
        ["kubectl", "config", "current-context"],
        check=False,
        expected=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    return (cp.stdout or "").strip()


def _build_and_load_k8s_image(
    image: str = K8S_IMAGE,
    *,
    context: Path = REPO_ROOT,
    fingerprint_paths: list[Path] | None = None,
    excludes: frozenset[str] = frozenset(),
    build_args: dict[str, str] | None = None,
) -> list[OpsResult]:
    """Build `image` from `context` and load it into the current cluster's image cache.

    Docker Desktop's built-in cluster shares the host's image cache, so a
    build alone is enough there. kind/minikube/k3s each need an explicit
    load step; an unrecognized cluster type is treated the same way the
    documented manual flow would be -- skip the load and tell the operator
    to do it themselves if their cluster doesn't share the host cache.

    k3s is the branch `nyxgpt cloud deploy --kubernetes` lands on (#3956):
    it runs its own containerd, so a `docker build` on the same box produces
    an image the cluster cannot see, and every `:local` tag in `k8s/*.yaml`
    would `ImagePullBackOff`. It is detected by the `k3s` binary rather than
    by the context name, because k3s's default context is called `default` --
    a name that says nothing. Ordered last of the three so a machine that has
    k3s installed but is currently pointed at a kind cluster still takes the
    kind branch.

    `image` defaults to the mutable `nyxgpt-api:local` tag `nyxgpt ops
    install --kubernetes` uses; `nyxgpt ops deploy --kubernetes` (via
    `canary.deploy`) passes a versioned tag instead (see
    `build_and_load_k8s_image` / #3409). `context`/`fingerprint_paths`/
    `excludes`/`build_args` default to the `nyxgpt-api` image's build (repo
    root, `_API_IMAGE_FINGERPRINT_PATHS`); `canary.deploy` overrides them for
    the `web` component to build `web/` with `_WEB_VENDOR_EXCLUDES` and the
    `NEXT_PUBLIC_API_BASE_URL` build arg (#3419), mirroring
    `_build_terraform_docker_images`'s web build. Either way the build
    itself is gated by `_docker_build_if_needed` (#3414): a versioned tag is
    always missing locally the first time (so it always builds), while the
    repeated `:local` tag skips the rebuild once the source stops changing
    between installs, mirroring the Homebrew reinstall-if-needed behavior
    from #3406.
    """
    if _which("docker") is None:
        return [OpsResult(False, f"docker not found on PATH -- cannot build the {image} image")]
    try:
        decision = _docker_build_if_needed(
            image,
            context,
            fingerprint_paths=fingerprint_paths or _API_IMAGE_FINGERPRINT_PATHS,
            excludes=excludes,
            build_args=build_args,
            marker_dir=DOCKER_IMAGE_MARKER_DIR,
        )
    except RuntimeError as e:
        return [OpsResult(False, "docker build failed", str(e))]
    results = [OpsResult(True, f"{image}: {decision}")]

    cluster_context = _kubectl_context()
    if "docker-desktop" in cluster_context:
        results.append(
            OpsResult(True, "Docker Desktop cluster shares the host image cache -- skipped load")
        )
        return results
    if cluster_context.startswith("kind-") and _which("kind") is not None:
        cluster_name = cluster_context.removeprefix("kind-")
        cp = _run(["kind", "load", "docker-image", image, "--name", cluster_name], check=False)
        if cp.returncode != 0:
            results.append(OpsResult(False, "kind load docker-image failed", _cp_details(cp)))
        else:
            results.append(OpsResult(True, f"Loaded {image} into kind cluster {cluster_name}"))
        return results
    if _which("minikube") is not None:
        cp = _run(["minikube", "image", "load", image], check=False)
        if cp.returncode != 0:
            results.append(OpsResult(False, "minikube image load failed", _cp_details(cp)))
        else:
            results.append(OpsResult(True, f"Loaded {image} into minikube"))
        return results
    if _which("k3s") is not None:
        results += _k3s_import_image(image)
        return results
    results.append(
        OpsResult(
            True,
            f"Unrecognized cluster context {cluster_context!r} -- skipped image load",
            "If this cluster doesn't share the host's image cache, load "
            f"{image} into it manually before the Pods can start.",
        )
    )
    return results


def _k3s_import_image(image: str) -> list[OpsResult]:
    """Import a locally-built docker image into k3s's containerd store (#3956).

    k3s does not use docker: it runs its own containerd, with its own image
    store, so an image `docker build` just produced is invisible to it. Every
    `k8s/*.yaml` Deployment pins `imagePullPolicy: IfNotPresent` against a
    `:local` tag that exists in no registry, so without this step the apply
    succeeds, the Pods are created, and every one of them sits in
    `ErrImagePull`/`ImagePullBackOff` forever.

    That failure mode is why this is an `OpsResult(False)` on error rather
    than the `True` the unrecognized-cluster branch above returns: there, the
    operator was told to load the image themselves and a bring-your-own
    cluster may genuinely share the host cache; here we know it does not, so
    a failed import is a failed install, reported at the step that caused it
    instead of eight minutes later as an unexplained Pending stack.

    The image itself is **piped**, not staged through a temp file: the two
    images together are several GB, and a cloud instance's root volume is
    sized for the stack, not for a second copy of it. `docker save`'s
    **stderr**, on the other hand, goes to a temp file rather than to a second
    pipe, and that asymmetry is deliberate. Nothing reads a stderr pipe until
    after the import returns, so a `docker save` that filled the 64KiB stderr
    buffer would block on the write, stop feeding the import, and leave the
    two processes waiting on each other until the timeout below expired. The
    diagnostic is a line or two in practice, which is exactly why the deadlock
    would never show up in testing and would surface as a fifteen-minute hang
    on somebody's deploy.

    `sudo` unless already root -- containerd's socket
    (`/run/k3s/containerd/containerd.sock`) is root-owned, and the login user
    a cloud deploy runs as is not.
    """
    argv = [*_k3s_sudo_prefix(), "k3s", "ctr", "images", "import", "-"]
    try:
        with (
            tempfile.TemporaryFile(mode="w+") as save_errors,
            subprocess.Popen(  # nosec B603
                ["docker", "save", image], stdout=subprocess.PIPE, stderr=save_errors
            ) as save,
        ):
            cp = subprocess.run(  # nosec B603 - fixed argv, no shell
                argv,
                stdin=save.stdout,
                capture_output=True,
                text=True,
                check=False,
                timeout=K3S_IMAGE_IMPORT_TIMEOUT_SECONDS,
            )
            # Close our handle so `docker save` sees EPIPE and exits if the
            # import died early, instead of blocking the `with` on a full pipe.
            if save.stdout is not None:
                save.stdout.close()
            save.wait(timeout=K3S_IMAGE_IMPORT_TIMEOUT_SECONDS)
            save_errors.seek(0)
            save_stderr = save_errors.read()
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(
            "ops: k3s image import failed for %s: %s",
            image,
            e,
            extra={"component": "ops", "action": "k3s-image-import", "image": image},
        )
        return [OpsResult(False, f"Could not import {image} into k3s", f"{type(e).__name__}: {e}")]

    if save.returncode != 0:
        logger.warning(
            "ops: docker save %s exited %s: %s",
            image,
            save.returncode,
            save_stderr.strip(),
            extra={"component": "ops", "action": "k3s-image-import", "image": image},
        )
        return [
            OpsResult(
                False,
                f"docker save {image} failed -- nothing to import into k3s",
                save_stderr.strip(),
            )
        ]
    if cp.returncode != 0:
        logger.warning(
            "ops: k3s ctr images import exited %s for %s: %s",
            cp.returncode,
            image,
            (cp.stderr or "").strip(),
            extra={"component": "ops", "action": "k3s-image-import", "image": image},
        )
        return [OpsResult(False, f"k3s ctr images import failed for {image}", _cp_details(cp))]
    return [OpsResult(True, f"Imported {image} into k3s's containerd", _cp_details(cp))]


def _k3s_sudo_prefix() -> list[str]:
    """`["sudo", "-n"]` unless this process is already root (or sudo is absent)."""
    if os.geteuid() == 0 or _which("sudo") is None:
        return []
    return ["sudo", "-n"]


def build_and_load_k8s_image(
    image: str,
    *,
    context: Path | None = None,
    fingerprint_paths: list[Path] | None = None,
    excludes: frozenset[str] = frozenset(),
    build_args: dict[str, str] | None = None,
) -> list[OpsResult]:
    """Build and load a specific, caller-chosen image tag (used by `nyxgpt ops deploy --kubernetes`).

    Public wrapper around `_build_and_load_k8s_image` for cross-module use
    (`canary.deploy`) -- the mutable-`:local`-tag install flow keeps calling
    the private function directly with its default. `canary.deploy` calls
    this with no extra kwargs for the `api` component (forwarding only
    `image`, identical to the original single-argument call) and with
    `web/`'s context/fingerprint/build-args for the `web` component (#3419)
    -- only kwargs the caller actually supplied are forwarded, so the `api`
    call path is unchanged.
    """
    kwargs: dict[str, Any] = {}
    if context is not None:
        kwargs["context"] = context
    if fingerprint_paths is not None:
        kwargs["fingerprint_paths"] = fingerprint_paths
    if excludes:
        kwargs["excludes"] = excludes
    if build_args is not None:
        kwargs["build_args"] = build_args
    return _build_and_load_k8s_image(image, **kwargs)


# Matches one `  key: "value"` entry of a Secret manifest's `stringData`
# block -- the only shape either template uses, and the shape
# `_write_k8s_secret_value` rewrites.
_K8S_SECRET_ENTRY_RE = re.compile(r'^(\s+)([A-Za-z0-9._-]+):\s*"(.*)"\s*$')


def _reconcile_k8s_secret_keys(secret_path: Path, example: Path) -> list[OpsResult]:
    """Add keys a NEWER template declares to an already-bootstrapped Secret (#3990).

    Both Secret manifests are written once and then left alone, so that an
    upgrade never rotates a credential out from under the data stored against
    it. That rule is right, and on its own it is also a trap: a release that
    adds a key (here `error-tracking-dsn`, which every app Deployment now
    references with `secretKeyRef`) would apply a Secret WITHOUT it on every
    machine that had ever installed before, and a `secretKeyRef` to a missing
    key does not degrade -- it leaves every api and web Pod in
    `CreateContainerConfigError`. So missing keys are added, carrying the
    template's own value, and existing keys are never touched.

    Text-append rather than a YAML round-trip: the comments in both templates
    are load-bearing documentation (#3538's why-not-empty note among them),
    and `stringData` is the last block in each, so appending is a faithful
    edit.
    """
    if not example.exists():
        # Nothing to reconcile against; the existing file is what it is.
        return [OpsResult(True, f"{secret_path} already exists")]

    def entries(text: str) -> list[re.Match[str]]:
        matched = (_K8S_SECRET_ENTRY_RE.match(line) for line in text.splitlines())
        return [m for m in matched if m is not None]

    existing = secret_path.read_text(encoding="utf-8")
    present = {m.group(2) for m in entries(existing)}
    missing = [m for m in entries(example.read_text(encoding="utf-8")) if m.group(2) not in present]
    if not missing:
        return [OpsResult(True, f"{secret_path} already exists")]

    added = "\n".join(f'{m.group(1)}{m.group(2)}: "{m.group(3)}"' for m in missing)
    secret_path.write_text(
        existing + ("" if existing.endswith("\n") else "\n") + added + "\n", encoding="utf-8"
    )
    os.chmod(secret_path, 0o600)
    names = ", ".join(sorted(m.group(2) for m in missing))
    return [OpsResult(True, f"Added {names} to the existing {secret_path} from its template")]


def _ensure_k8s_secret(api_key: str | None) -> list[OpsResult]:
    """Bootstrap k8s/secret.yaml from the example if it doesn't exist yet (never committed).

    An existing file keeps every value it has -- rotating the API key under a
    running deployment is not this step's job -- but gains any key the current
    template has added since (`_reconcile_k8s_secret_keys`).
    """
    secret_path = K8S_DIR / "secret.yaml"
    example = K8S_DIR / "secret.example.yaml"
    if secret_path.exists():
        return _reconcile_k8s_secret_keys(secret_path, example)
    if not example.exists():
        return [OpsResult(False, f"Missing {example} to bootstrap the secret from")]
    key = _resolve_api_key(api_key)
    text = example.read_text(encoding="utf-8")
    text = re.sub(r'api-key:\s*".*"', lambda _m: f'api-key: "{key}"', text)
    secret_path.write_text(text, encoding="utf-8")
    os.chmod(secret_path, 0o600)
    return [OpsResult(True, f"Bootstrapped {secret_path} from secret.example.yaml")]


def _kubectl_apply_kustomization() -> list[OpsResult]:
    """Apply `k8s/`'s kustomization (namespace, RBAC, ConfigMap, Secret, Deployments, Service)."""
    cp = _run(["kubectl", "apply", "-k", str(K8S_DIR)], check=False)
    if cp.returncode != 0:
        return [OpsResult(False, "kubectl apply -k k8s/ failed", _cp_details(cp))]
    return [OpsResult(True, "kubectl apply -k k8s/", _cp_details(cp))]


# --- In-cluster observability layer (#3787) ---
#
# Kubernetes mode used to deploy the api/web tier and nothing else: the
# Compose observability profiles are unreachable from a cluster (they scrape
# `host.docker.internal` and resolve Compose service names on a Docker
# network), so a `--kubernetes --local` deployment had no metrics, no logs,
# no traces and no error tracking, and the admin dashboard's observability
# tiles pointed at nothing. `k8s/observability/` is the in-cluster answer;
# everything below applies, inspects and tears it down through `nyxgpt ops`
# so no operator ever types kubectl.

K8S_OBSERVABILITY_DIR = K8S_DIR / "observability"

# The workloads `k8s/observability/` ships. Listed explicitly rather than
# discovered with a label selector so a Deployment that failed to apply
# reports as missing instead of silently dropping out of the report.
K8S_OBSERVABILITY_DEPLOYMENTS = (
    "prometheus",
    "grafana",
    "loki",
    "otel-collector",
    "jaeger",
    "glitchtip-postgres",
    "glitchtip-redis",
    "glitchtip",
    "glitchtip-worker",
)
K8S_OBSERVABILITY_DAEMONSETS = ("promtail",)

# How long `install --kubernetes` / `ops observability --kubernetes` will wait
# for the ten workloads above to roll out, in total (#3826).
#
# A *shared* budget, not a per-workload timeout: the ten Pods pull their
# images concurrently, so the first `rollout status` absorbs nearly all of the
# real wait and the rest return immediately. A per-workload timeout would
# multiply the worst case by ten for no extra coverage.
#
# The wait itself exists because `_k8s_stack_health` reads Pod *phase*, and a
# Pod still pulling grafana/loki/glitchtip is `Pending` -- so without it the
# default (observability-on) install reports a fistful of failed Pods on a
# perfectly healthy cluster, which is why the k8s smoke was passing
# `--skip-observability` and testing a configuration no user runs.
K8S_OBSERVABILITY_ROLLOUT_BUDGET_S = 900

# Grafana's provisioning, mounted by k8s/observability/grafana.yaml: ConfigMap
# name -> path under the synced `docker/grafana` tree it is generated from.
# Generated here rather than committed as k8s manifests because kustomize
# cannot read files above its own root, and a second copy of the dashboards
# would drift from the Compose/native one within a release.
K8S_GRAFANA_CONFIGMAPS: dict[str, tuple[str, ...]] = {
    "grafana-datasources": ("provisioning", "datasources"),
    "grafana-dashboard-providers": ("provisioning", "dashboards"),
    "grafana-alerting": ("provisioning", "alerting"),
    "grafana-dashboards": ("dashboards",),
}


def _k8s_observability_secret_values() -> dict[str, str]:
    """Resolve the values `k8s/observability/secret.yaml` is bootstrapped with.

    Same sources the Compose path uses, so an operator's config.ini drives
    Grafana identically in either mode: the ops-managed Grafana admin
    password (`resolve_grafana_admin_password`) and `[monitoring]
    slack_webhook_url`. An unset webhook resolves to
    `GRAFANA_SLACK_WEBHOOK_PLACEHOLDER_URL` rather than an empty string for
    the #3538 reason -- Grafana's alerting validator crash-loops the Pod on
    an empty contact-point URL, it does not degrade gracefully.

    Reading config is best-effort: a machine with no config.ini yet still
    gets a bootable Grafana (placeholders), which is what the example file
    already carries.
    """
    values = {
        "glitchtip-grafana-token": GRAFANA_GLITCHTIP_TOKEN_PLACEHOLDER,
        "slack-webhook-url": GRAFANA_SLACK_WEBHOOK_PLACEHOLDER_URL,
    }
    from nyxgpt.config import load_config

    try:
        cfg = load_config()
    except Exception:
        return values
    with contextlib.suppress(Exception):
        values["grafana-admin-password"] = _grafana_admin_password(cfg)
    with contextlib.suppress(Exception):
        webhook = get_monitoring_slack_webhook_url(cfg).strip()
        if webhook:
            values["slack-webhook-url"] = webhook
    return values


def _ensure_k8s_observability_secret() -> list[OpsResult]:
    """Bootstrap `k8s/observability/secret.yaml` from its example (never committed).

    Mirrors `_ensure_k8s_secret`: written once, left alone afterwards, so a
    re-install never rotates GlitchTip's Django SECRET_KEY or its Postgres
    password out from under the data already stored against them -- except
    for keys the template has ADDED since, which are appended rather than
    left missing (`_reconcile_k8s_secret_keys`, and see its docstring for why
    a missing key is worse than a stale one). Delete the file to re-bootstrap
    it from current config.
    """
    secret_path = K8S_OBSERVABILITY_DIR / "secret.yaml"
    example = K8S_OBSERVABILITY_DIR / "secret.example.yaml"
    if secret_path.exists():
        return _reconcile_k8s_secret_keys(secret_path, example)
    if not example.exists():
        return [OpsResult(False, f"Missing {example} to bootstrap the observability secret from")]

    text = example.read_text(encoding="utf-8")
    for key, value in _k8s_observability_secret_values().items():
        # Only the placeholder line for this key is rewritten; the YAML
        # shape (and every explanatory comment above it) is preserved.
        text = re.sub(
            rf'^(\s*{re.escape(key)}:\s*)".*"$',
            lambda m, v=value: f'{m.group(1)}"{v}"',  # type: ignore[misc]
            text,
            flags=re.MULTILINE,
        )
    secret_path.write_text(text, encoding="utf-8")
    os.chmod(secret_path, 0o600)
    return [OpsResult(True, f"Bootstrapped {secret_path} from secret.example.yaml")]


def _kubectl_apply_stdin(manifest: str, what: str) -> OpsResult:
    """`kubectl apply -f -` a manifest built in-process (no temp files)."""
    cp = _run(["kubectl", "apply", "-f", "-"], input=manifest, check=False)
    if cp.returncode != 0:
        return OpsResult(False, f"kubectl apply {what} failed", _cp_details(cp))
    return OpsResult(True, f"kubectl apply {what}", _cp_details(cp))


def _apply_k8s_grafana_provisioning() -> list[OpsResult]:
    """Generate Grafana's provisioning ConfigMaps from `~/.nyxGPT/docker/grafana`.

    The datasources, dashboard provider, alerting rules and dashboard JSONs
    are the SAME files Compose and native mode use -- synced into
    `NYXGPT_HOME` by `_sync_packaged_resources` (#3621), so this works from
    an installed package as well as a checkout. `kubectl create configmap
    --dry-run=client -o yaml` renders each directory, and the result is
    applied through stdin: server-side create/replace in one idempotent step.

    Restarts Grafana only when an apply actually *changed* something
    (kubectl says "configured" rather than "unchanged"/"created"). Grafana
    reads provisioning at startup only, so an edited dashboard or datasource
    is otherwise invisible until something else restarts the Pod -- and
    restarting unconditionally would bounce Grafana on every install.
    """
    grafana_dir = OPS_DOCKER_DIR / "grafana"
    if not grafana_dir.is_dir():
        return [
            OpsResult(
                False,
                f"Missing {grafana_dir} -- cannot provision Grafana in-cluster",
                "Run `nyxgpt ops install` (or `nyxgpt ops observability`) first: it syncs the "
                "packaged Grafana provisioning into ~/.nyxGPT/docker.",
            )
        ]

    results: list[OpsResult] = []
    changed = False
    for name, parts in K8S_GRAFANA_CONFIGMAPS.items():
        source = grafana_dir.joinpath(*parts)
        if not source.is_dir():
            results.append(OpsResult(False, f"Missing {source} for ConfigMap {name}"))
            continue
        cp = _run(
            [
                "kubectl",
                "-n",
                K8S_NAMESPACE,
                "create",
                "configmap",
                name,
                f"--from-file={source}",
                "--dry-run=client",
                "-o",
                "yaml",
            ],
            check=False,
        )
        if cp.returncode != 0:
            results.append(OpsResult(False, f"Could not render ConfigMap {name}", _cp_details(cp)))
            continue
        applied = _kubectl_apply_stdin(cp.stdout, f"configmap/{name}")
        results.append(applied)
        if applied.ok and "configured" in (applied.details or ""):
            changed = True

    if changed and all(r.ok for r in results):
        results.append(_restart_k8s_grafana())
    return results


def _restart_k8s_grafana() -> OpsResult:
    """Roll the Grafana Deployment so it re-reads changed provisioning."""
    cp = _run(
        ["kubectl", "-n", K8S_NAMESPACE, "rollout", "restart", "deployment/grafana"],
        check=False,
    )
    if cp.returncode != 0:
        return OpsResult(
            False, "Could not restart Grafana to pick up provisioning", _cp_details(cp)
        )
    return OpsResult(True, "Restarted Grafana to pick up changed provisioning", _cp_details(cp))


def _apply_k8s_observability() -> list[OpsResult]:
    """Apply the in-cluster observability layer (`k8s/observability/`).

    Order matters: the overlay creates the namespace and the workloads, then
    Grafana's provisioning ConfigMaps are applied into that namespace. A
    Grafana Pod scheduled before its ConfigMaps exist simply waits for them
    (kubelet retries the mount), so this ordering costs nothing and avoids
    needing a separate namespace-bootstrap step.
    """
    results = _ensure_k8s_observability_secret()
    if not all(r.ok for r in results):
        return results

    cp = _run(["kubectl", "apply", "-k", str(K8S_OBSERVABILITY_DIR)], check=False)
    if cp.returncode != 0:
        results.append(
            OpsResult(False, "kubectl apply -k k8s/observability/ failed", _cp_details(cp))
        )
        return results
    results.append(OpsResult(True, "kubectl apply -k k8s/observability/", _cp_details(cp)))
    results += _apply_k8s_grafana_provisioning()
    return results


def _delete_k8s_observability() -> list[OpsResult]:
    """Remove the in-cluster observability layer, if it was ever bootstrapped.

    A never-installed cluster has no `k8s/observability/secret.yaml`, which
    the kustomization references -- `kubectl delete -k` would fail on the
    missing file rather than on anything about the cluster. Nothing to
    delete is a success, not an error.
    """
    if not (K8S_OBSERVABILITY_DIR / "secret.yaml").exists():
        return [OpsResult(True, "No observability layer bootstrapped -- nothing to delete")]
    cp = _run(
        ["kubectl", "delete", "-k", str(K8S_OBSERVABILITY_DIR), "--ignore-not-found"],
        check=False,
    )
    if cp.returncode != 0:
        return [OpsResult(False, "kubectl delete -k k8s/observability/ failed", _cp_details(cp))]
    return [OpsResult(True, "kubectl delete -k k8s/observability/", _cp_details(cp))]


# --- One shared readiness vocabulary for Kubernetes workloads (#3827) ---
#
# `_k8s_stack_health` used to score a Pod on its `phase` alone -- anything but
# `Running` was `[FAIL]` -- while `_k8s_observability_health` reported a
# workload with zero ready replicas as `[OK] observability grafana: 0/1
# ready`. One command printed both, so a single `--kubernetes --local` install
# gave two contradictory verdicts on the same condition, and the ten `[FAIL]
# pod ...: Pending` lines it emitted while kind was still pulling images
# buried the one Pod that was genuinely broken (prometheus, `Insufficient
# memory`).
#
# Everything that reports on a Kubernetes workload now classifies it into
# exactly one of these three states, and the distinction that matters is the
# middle one: PENDING is not a failure. A Pod pulling a multi-hundred-megabyte
# image is doing what it is supposed to; only a wait that runs out of budget
# (`_await_k8s_rollout`), or a condition that will never resolve on its own,
# turns into FAILED.
K8S_STATE_READY = "ready"
K8S_STATE_PENDING = "pending"
K8S_STATE_FAILED = "failed"

# The summary `_classify_k8s_pod` gives the one FAILED case whose remedy is a
# bigger cluster, not a fix to the workload: no node would take the Pod. Named
# because the Infrastructure page reports that population separately, with the
# command that resolves it (#3825) -- and must read it from this classification
# rather than probing `.spec.nodeName` on its own, or the page's own badges and
# its "could not be scheduled" list can disagree about the same Pod.
K8S_SUMMARY_UNSCHEDULABLE = "Pending: unschedulable"

# Container waiting reasons that a Pod does not recover from by waiting
# longer: the image cannot be fetched or is misnamed, the container config
# references a missing ConfigMap/Secret key, or the process keeps dying.
# kubelet retries these forever, so the Pod sits in a state that *looks*
# like startup and never leaves it -- reporting them as "still starting"
# would be the same lie in the other direction.
K8S_BLOCKED_WAITING_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "CreateContainerConfigError",
        "CreateContainerError",
        "ErrImageNeverPull",
        "ErrImagePull",
        "ImageInspectError",
        "ImagePullBackOff",
        "InvalidImageName",
        "RunContainerError",
    }
)


@dataclass(frozen=True)
class K8sWorkloadState:
    """One workload's (or Pod's) state in the shared vocabulary above.

    `summary` is the operator-facing phrase -- it carries *why*, which is the
    whole point of separating "Pending: pulling images" from "Pending:
    unschedulable (0/1 nodes are available: Insufficient memory)".
    """

    name: str
    state: str
    summary: str
    details: str = ""
    # The cluster's own one-word reason, when there is one
    # (`CrashLoopBackOff`, `ImagePullBackOff`, ...), kept apart from the
    # rendered `summary` so callers can key on it without parsing prose --
    # `_k8s_blocked_confirmations` needs to tell one blocked reason from
    # another.
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Whether this state should count against an ops command's exit status.

        Pending counts as ok: it is a transient state, and the caller that
        can actually decide whether it settled is the wait, not the snapshot.
        """
        return self.state != K8S_STATE_FAILED

    @property
    def label(self) -> str:
        """The stdout label (`OK`/`PENDING`/`FAIL`) this state prints under."""
        return {
            K8S_STATE_READY: "OK",
            K8S_STATE_PENDING: "PENDING",
            K8S_STATE_FAILED: "FAIL",
        }[self.state]

    def as_result(self, prefix: str = "") -> OpsResult:
        """Render as an `OpsResult` carrying the label, not just a boolean."""
        return OpsResult(
            self.ok, f"{prefix}{self.name}: {self.summary}", self.details, status=self.label
        )


def _k8s_container_block(status: dict[str, Any]) -> tuple[str, str] | None:
    """Return `(reason, message)` for the first container stuck in a blocked state.

    Looks at init containers too: an init container in `ImagePullBackOff`
    holds the whole Pod in `Pending` with nothing wrong in the main
    container's status.
    """
    statuses = list(status.get("initContainerStatuses") or []) + list(
        status.get("containerStatuses") or []
    )
    for cs in statuses:
        waiting = ((cs.get("state") or {}).get("waiting")) or {}
        reason = str(waiting.get("reason") or "")
        if reason in K8S_BLOCKED_WAITING_REASONS:
            return reason, str(waiting.get("message") or "").strip()
    return None


def _classify_k8s_pod(pod: dict[str, Any]) -> K8sWorkloadState:
    """Classify one Pod (as `kubectl get pods -o json` returns it) into the vocabulary.

    The *reading* of the Pod -- phase, readiness, whether the scheduler
    refused it, and the cluster's own words for why it is not serving -- comes
    from `nyxgpt.k8s_pod_state.classify_pod`, the one place `self_heal.py` also
    reads Pods from (#3832). What stays here is this module's *policy* on that
    reading: which states count against an ops command's exit status, and the
    phrases the install report prints. Two classifiers that agreed by
    convention is exactly how the watchdog and the install report came to
    disagree about a Pending Pod in the first place.

    The classification an operator needs, rather than the one the phase field
    happens to offer:

    * `Running` with its `Ready` condition true, or `Succeeded`, is READY.
    * `Pending` while images pull or containers are created is PENDING --
      the state this whole section exists to stop reporting as a failure.
    * `Pending` with `PodScheduled` not true -- the scheduler cannot place the
      Pod (`Unschedulable`, what a `FailedScheduling` event leaves behind, or
      `SchedulingGated`) -- is FAILED and says so, because no amount of
      waiting fixes a node that cannot fit it.
    * A blocked container state (see `K8S_BLOCKED_WAITING_REASONS`) is FAILED
      whatever the phase says, including the `Running` Pod whose container is
      in `CrashLoopBackOff`. This is the one place the two callers *do*
      differ, and deliberately: an install must not report a crash-looping Pod
      as healthy, while self-heal answers that same Pod by restarting it.
    """
    state = classify_pod(pod)
    name = state.name or "?"
    status = pod.get("status") or {}
    phase = state.phase or "Unknown"

    blocked = _k8s_container_block(status)
    if blocked is not None:
        reason, message = blocked
        return K8sWorkloadState(name, K8S_STATE_FAILED, f"{phase}: {reason}", message, reason)

    if state.unschedulable:
        return K8sWorkloadState(name, K8S_STATE_FAILED, K8S_SUMMARY_UNSCHEDULABLE, state.detail)

    if state.pending:
        return K8sWorkloadState(
            name, K8S_STATE_PENDING, f"Pending: {state.reason or 'being scheduled'}"
        )

    if state.running:
        if state.ready:
            return K8sWorkloadState(name, K8S_STATE_READY, "Running")
        return K8sWorkloadState(
            name,
            K8S_STATE_PENDING,
            f"Running: {state.reason or 'containers not ready yet'}",
        )

    if phase == "Succeeded":
        return K8sWorkloadState(name, K8S_STATE_READY, "Succeeded")

    # `Failed`, `Unknown`, and anything a future Kubernetes adds: not ready,
    # and not something waiting resolves.
    return K8sWorkloadState(name, K8S_STATE_FAILED, phase, str(status.get("message") or "").strip())


def _k8s_pod_states(
    namespace: str = "", *, selector: str = "", expected: bool = False
) -> tuple[list[K8sWorkloadState], OpsResult | None]:
    """Classify every Pod in the namespace; returns `(states, read_failure)`.

    `read_failure` is non-None only when the Pod list could not be read at
    all -- which is a real failure (an unreachable cluster is not "pending"),
    kept separate so callers do not have to invent a fake state for it.

    `selector` narrows the read to one workload's Pods (`-l app=x,track=y`).
    `expected=True` for the read-only probes (`infra_status`) where an
    unreachable cluster is a normal answer rather than something to warn about.
    """
    cp = _run(
        ["kubectl", "-n", namespace or K8S_NAMESPACE, "get", "pods", "-o", "json"]
        + (["-l", selector] if selector else []),
        check=False,
        expected=expected,
        # `/infra/status` polls this, so it must not be able to hold a
        # threadpool worker on a configured-but-unreachable cluster (#3858).
        # A single `get pods` against a cluster that *is* answering is far
        # inside this bound, install-time waits included.
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if timed_out(cp):
        # Reported as its own failure so the page says "the cluster did not
        # answer" rather than leaving an operator to infer it from an empty
        # pod list -- this is the "cannot determine" case (#3468), not
        # "nothing is deployed".
        return [], OpsResult(
            False,
            f"Could not read pod status: the cluster {timeout_message(PROBE_TIMEOUT_SECONDS)}",
            _cp_details(cp),
        )
    if cp.returncode != 0:
        return [], OpsResult(False, "Could not read pod status", _cp_details(cp))
    try:
        payload = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError as e:
        return [], OpsResult(False, "Could not parse pod status", f"{e}\n{_cp_details(cp)}")
    items = payload.get("items") or []
    return [_classify_k8s_pod(p) for p in items if isinstance(p, dict)], None


def _k8s_app_pods_present(pod_states: Sequence[K8sWorkloadState]) -> bool:
    """Whether any api/web Pod is actually in the namespace.

    `_k8s_pod_states` reads *every* Pod in the `nyxgpt` namespace, which
    includes the in-cluster observability workloads. A namespace holding only
    those has no api/web to describe, so the install-mode follow-up ("the Pods
    run images built from that working tree") would be asserting a deployment
    that is not there -- the Kubernetes twin of the Terraform defect in #3989.
    The app Deployments are `nyxgpt-api-{stable,canary}` and
    `nyxgpt-web-{stable,canary}` (k8s/deployment*.yaml), so their Pods are the
    ones whose names begin with these prefixes.
    """
    return any(name.startswith(K8S_APP_POD_PREFIXES) for name in (s.name for s in pod_states))


def _k8s_workload_selector(ref: str) -> str:
    """`app=x,track=y` for a `deploy/…`-style ref -- how to find *its* Pods.

    Read from the workload's own `.spec.selector.matchLabels` rather than
    guessed from the name, so it stays correct if a manifest relabels. An
    unreadable or selector-less workload returns "", and the caller then
    declines to attribute any Pod to it (see `_k8s_blocked_pods`) -- a wait
    must never invent a failure out of a Pod belonging to something else.
    """
    cp = _run(
        [
            "kubectl",
            "-n",
            K8S_NAMESPACE,
            "get",
            ref,
            "-o",
            "jsonpath={.spec.selector.matchLabels}",
        ],
        check=False,
        expected=True,
    )
    if cp.returncode != 0:
        return ""
    try:
        labels = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(labels, dict) or not labels:
        return ""
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


@dataclass(frozen=True)
class K8sDeploymentProbe:
    """One read of the nyxGPT namespace: what is deployed, or why that is unknown.

    `determined` is the #3468 distinction the CLI was missing entirely: a
    *configured* cluster whose Pod list could not be read is CANNOT DETERMINE,
    and reporting it as "nothing deployed" is the false negative that issue
    removed from the Infrastructure page. A machine with no kubectl, or a
    kubectl with no current context, is not that case -- nothing there was
    ever pointed at a cluster, so "not detected" is a fact rather than a guess.
    """

    pods: list[K8sWorkloadState]
    reason: str = ""
    determined: bool = True

    @property
    def deployed(self) -> bool:
        """Whether a deployment is there -- Pods, not merely a reachable cluster.

        Deliberately not the inverse of `determined`: an undetermined read is
        not deployed *and* not "nothing deployed", which is why callers that
        report to an operator ask both (see `summary`).
        """
        return bool(self.pods)

    @property
    def summary(self) -> str:
        """The one-line rendering of this probe for a status report."""
        if self.pods:
            ready = sum(1 for p in self.pods if p.state == K8S_STATE_READY)
            return f"{K8S_NAMESPACE} namespace: {ready}/{len(self.pods)} pod(s) ready"
        if not self.determined:
            return f"cannot determine ({self.reason})"
        return f"not detected ({self.reason})"


def _k8s_deployment_probe() -> K8sDeploymentProbe:
    """Read the nyxGPT namespace once, for every block that needs to know (#3987).

    `ops status` used to reach the cluster only in its own dedicated section at
    the bottom, so every block above it -- the "Deployment mode" summary and
    the required-model check -- described the native machine as though nothing
    else could be serving, and the owner's Kubernetes acceptance run read as
    "nothing is deployed" above 14 running Pods. Those blocks now take this
    probe rather than each growing one of its own, which also means they
    cannot disagree with each other about whether a deployment exists.

    The current-context check comes before the Pod read on purpose: it is the
    cheaper question, it is the one that is true on most machines running this
    command, and it is what separates "no cluster here" from "a cluster that
    did not answer" (see `K8sDeploymentProbe.determined`, and `infra_status`,
    which classifies the dashboard's copy of this the same way).
    """
    if _which("kubectl") is None:
        return K8sDeploymentProbe([], "kubectl not found")
    if not _kubectl_context():
        return K8sDeploymentProbe([], "no cluster configured -- kubectl has no current context")
    states, read_failure = _k8s_pod_states(expected=True)
    if read_failure is not None:
        return K8sDeploymentProbe([], read_failure.message, determined=False)
    return K8sDeploymentProbe(states, f"no pods in the {K8S_NAMESPACE} namespace")


def _k8s_installed_model_names() -> tuple[set[str] | None, str, str]:
    """The models the *in-cluster* Ollama holds; `(names, error_class, detail)`.

    `names` is None when the cluster's Ollama could not be asked at all, and
    `error_class` then names the failure in one classifier-style word. The
    split is the #3837 contract: `required_models_status`'s dict is returned
    verbatim by `GET /models/required`, so only `error_class` may travel with
    it -- `detail` carries kubectl's own words (cluster names, node addresses,
    proxy state) and goes to the log.

    Read with `kubectl exec ... -- ollama list` rather than over HTTP because
    the Service is a ClusterIP: from the host there is no route to
    `http://ollama:11434` without a port-forward, and standing one up to
    answer a status question would be a heavier side effect than the question
    deserves. `statefulset/ollama` (not `ollama-0`) so the ref stays correct
    if the StatefulSet is ever scaled or renamed by ordinal.
    """
    if _which("kubectl") is None:
        return None, "KubectlNotFound", "kubectl is not on PATH"
    cp = _run(
        ["kubectl", "-n", K8S_NAMESPACE, "exec", K8S_OLLAMA_WORKLOAD, "--", "ollama", "list"],
        check=False,
        expected=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if timed_out(cp):
        return None, "KubectlTimeout", f"the cluster {timeout_message(PROBE_TIMEOUT_SECONDS)}"
    if cp.returncode != 0:
        return None, "KubectlExecFailed", _cp_details(cp)
    names: set[str] = set()
    for line in (cp.stdout or "").splitlines():
        fields = line.split()
        # `ollama list` prints a NAME/ID/SIZE/MODIFIED table; the first column
        # already carries the explicit tag, so `normalize_model_name` is a
        # no-op on it and stays only so this set is comparable with the
        # HTTP-path one without either caller knowing which produced it.
        if not fields or fields[0].upper() == "NAME":
            continue
        names.add(model_bootstrap.normalize_model_name(fields[0]))
    return names, "", ""


def _k8s_blocked_pods(namespace: str = "", *, selector: str = "") -> list[K8sWorkloadState]:
    """The Pods that are FAILED right now -- what a wait fast-fails on (#3827).

    A read that itself fails returns nothing: an unreachable cluster is a
    reason to keep waiting for the rollout, not to declare a Pod broken.
    """
    states, _ = _k8s_pod_states(namespace, selector=selector)
    return [s for s in states if s.state == K8S_STATE_FAILED]


def _k8s_observability_workload_state() -> dict[str, str]:
    """Map every observability workload to `ready/N`-style state, or "absent".

    One `kubectl get` per kind (not per workload) so this stays cheap enough
    for `ops status` and the admin dashboard's infrastructure poll.
    """
    state: dict[str, str] = dict.fromkeys(
        (*K8S_OBSERVABILITY_DEPLOYMENTS, *K8S_OBSERVABILITY_DAEMONSETS), "absent"
    )
    queries = (
        ("deploy", "{.status.readyReplicas}", "{.spec.replicas}"),
        ("daemonset", "{.status.numberReady}", "{.status.desiredNumberScheduled}"),
    )
    for kind, ready_field, desired_field in queries:
        cp = _run(
            [
                "kubectl",
                "-n",
                K8S_NAMESPACE,
                "get",
                kind,
                "-o",
                f"jsonpath={{range .items[*]}}{{.metadata.name}}={ready_field}/{desired_field};{{end}}",
            ],
            check=False,
            expected=True,
        )
        if cp.returncode != 0:
            continue
        for entry in (e for e in (cp.stdout or "").split(";") if e):
            name, _, counts = entry.partition("=")
            if name not in state:
                continue
            # A workload with no ready Pods yet omits the field entirely,
            # rendering as "/1" -- report that as an explicit 0.
            ready, _, desired = counts.partition("/")
            state[name] = f"{ready or '0'}/{desired or '?'} ready"
    return state


def _classify_k8s_observability_workload(name: str, value: str) -> K8sWorkloadState:
    """Classify one `_k8s_observability_workload_state` entry into the shared vocabulary.

    "absent" is FAILED -- the workload was applied and is not there. A
    `0/1 ready` counts as PENDING, not as the `[OK] observability grafana:
    0/1 ready` it used to print (#3827): zero ready replicas is exactly the
    condition `_k8s_stack_health` was simultaneously calling a failure, and
    the two halves of one command must not disagree about it.
    """
    if value == "absent":
        return K8sWorkloadState(
            name,
            K8S_STATE_FAILED,
            "absent",
            "Re-run `nyxgpt ops observability --kubernetes`.",
        )
    ready, _, desired = value.partition("/")
    desired_count = desired.split()[0] if desired else ""
    if ready.strip().isdigit() and ready.strip() == desired_count and ready.strip() != "0":
        return K8sWorkloadState(name, K8S_STATE_READY, value)
    return K8sWorkloadState(name, K8S_STATE_PENDING, value)


def _k8s_observability_health() -> list[OpsResult]:
    """Snapshot of the observability workloads right after the rollout wait.

    Uses the same three-state vocabulary as `_k8s_stack_health`
    (`_classify_k8s_observability_workload`): ready, still-rolling-out
    (`[PENDING]`, not a failure and not a green tick), or absent. The install
    waits for the layer first (`_wait_for_k8s_observability`), so a workload
    reported PENDING here is one that reached readiness during the wait and
    lost a replica since -- worth showing, not worth failing on, since the
    wait is what already ruled on whether the layer settled. `nyxgpt ops
    status` re-reads it.
    """
    state = _k8s_observability_workload_state()
    states = [_classify_k8s_observability_workload(name, value) for name, value in state.items()]
    results = [s.as_result(prefix="observability ") for s in states]
    missing = [s for s in states if s.summary == "absent"]
    if missing:
        results.append(
            OpsResult(
                False,
                f"{len(missing)} observability workload(s) missing from the cluster",
                "Re-run `nyxgpt ops observability --kubernetes`.",
            )
        )
    results += _k8s_observability_data_flow(state)
    return results


# --- Running is not receiving (#3990) ---
#
# Every readiness signal this module had answered "is the process up". Ten
# observability workloads reported `1/1 ready` on a cluster where the api and
# web Pods were exporting spans to `localhost:4318` -- their own Pods -- and
# reporting no errors at all, so the tier that installed perfectly observed
# nothing. Readiness structurally cannot catch that: a collector with no
# clients is as ready as a collector with a thousand. It is the same shape as
# #3812, and the reason a green install reached owner acceptance blind.
#
# So the report asks each backend what it has RECEIVED, not merely whether it
# is up. A backend that is up but empty prints `[NO DATA]`: not `[OK]`, and
# not a failure either -- a stack nobody has chatted with yet legitimately has
# no spans, and failing the install for that would just teach operators to
# ignore the line. `ok` stays True for exactly that reason; the line carries
# the remedy instead (see `OpsResult.status`, #3827).
K8S_NO_DATA_LABEL = "NO DATA"

# The Pod every probe below runs FROM. Grafana is the natural choice and not
# an arbitrary one: it is the tier's own consumer (these are the queries its
# panels make), it already mounts the GlitchTip token the errors probe needs,
# and its image carries busybox wget -- so one `kubectl exec` answers all four
# questions without scheduling a Pod of our own on a node that #3825 showed is
# already tight.
K8S_TELEMETRY_PROBE_DEPLOYMENT = "grafana"

# Per-request budget for those in-cluster fetches. Short on purpose: these are
# Service-to-Service hops inside one node, and this probe runs at the end of
# an install an operator is waiting on.
K8S_TELEMETRY_PROBE_TIMEOUT_S = 10

# Where Grafana's Deployment mounts the GlitchTip bearer token
# (k8s/observability/grafana.yaml -> the `glitchtip-grafana-token` key of the
# nyxgpt-observability-secrets Secret). The errors probe reads the token from
# the same file Grafana's Infinity datasource expands with `$__file{}`, so it
# is asking GlitchTip the same question, with the same credential, that the
# SRE Home panels ask -- which is what makes a green line here mean those
# panels will not 401.
K8S_GRAFANA_GLITCHTIP_TOKEN_MOUNT = "/etc/nyxgpt-secrets/glitchtip-grafana-token"


def _k8s_incluster_get(url: str, *, bearer_token_file: str = "") -> tuple[bool, str]:
    """GET `url` from inside the cluster through the Grafana Pod; `(ok, body)`.

    `kubectl exec` rather than a port-forward: these are the *in-cluster*
    addresses (`http://jaeger:16686`), and reaching them the way the tier's
    own consumer reaches them is the whole point -- a probe run from the
    workstation through a tunnel would answer a different question than the
    one an operator is asking ("does Grafana see data?").

    `bearer_token_file` names a path INSIDE that Pod whose contents become an
    `Authorization: Bearer` header. The token is read in the container and
    never crosses this process's argv or logs (`_run` logs the command it
    ran), which is why it is passed as a filename rather than a value.
    """
    # `$0` is the shell's own name, so the caller's arguments start at `$1`.
    script = (
        'url="$1"; token_file="$2"; timeout="$3"; '
        'if [ -n "$token_file" ]; then '
        'wget -q -O - -T "$timeout" '
        '--header="Authorization: Bearer $(cat "$token_file")" "$url"; '
        'else wget -q -O - -T "$timeout" "$url"; fi'
    )
    cp = _run(
        [
            "kubectl",
            "-n",
            K8S_NAMESPACE,
            "exec",
            f"deploy/{K8S_TELEMETRY_PROBE_DEPLOYMENT}",
            "--",
            "sh",
            "-c",
            script,
            "sh",
            url,
            bearer_token_file,
            str(K8S_TELEMETRY_PROBE_TIMEOUT_S),
        ],
        check=False,
        # A backend that answers 401/404, or a Grafana Pod that is not up, is
        # a normal outcome for a probe -- it is the finding, not an error to
        # warn about in the log.
        expected=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    return cp.returncode == 0, cp.stdout or ""


def _no_data(message: str, details: str = "") -> OpsResult:
    """An `[NO DATA]` line: the backend is up, nothing has arrived (see above)."""
    return OpsResult(True, message, details, status=K8S_NO_DATA_LABEL)


def _k8s_traces_flow_result() -> OpsResult:
    """Does Jaeger hold spans from nyxGPT, or only its own?

    THE exact symptom of #3990: `/api/services` returned
    `{"data":["jaeger-all-in-one"]}` -- Jaeger's own self-traces, which read
    in the UI as "traces are working" while every nyxGPT span was being
    posted to the app Pod's own localhost. Anything named `nyxgpt-*` here
    (`nyxgpt-api` from `[tracing] service_name`, `nyxgpt-web` from
    web/src/instrumentation.ts) can only have arrived through the collector.
    """
    ok, body = _k8s_incluster_get("http://jaeger:16686/api/services")
    if not ok:
        return _no_data(
            "observability traces: could not ask Jaeger what it has received",
            "Jaeger did not answer /api/services from inside the cluster; re-check with "
            "`nyxgpt ops status`.",
        )
    try:
        services = [str(s) for s in (json.loads(body).get("data") or [])]
    except (ValueError, AttributeError):
        return _no_data("observability traces: Jaeger's service list was unreadable", body[:200])

    nyxgpt_services = sorted(s for s in services if s.startswith("nyxgpt"))
    if nyxgpt_services:
        return OpsResult(True, f"observability traces: Jaeger has {', '.join(nyxgpt_services)}")
    return _no_data(
        "observability traces: Jaeger has no nyxGPT spans yet",
        "Nothing has been traced since the collector came up. Send one chat message and "
        "re-check with `nyxgpt ops status`; if it stays empty, the api/web Pods are not "
        "exporting to `otel-collector` (see [tracing] otlp_endpoint in the nyxgpt-config "
        "ConfigMap and NYXGPT_OTLP_ENDPOINT on the web Deployments).",
    )


def _k8s_metrics_flow_result() -> OpsResult:
    """Is Prometheus actually scraping, or merely running?

    The metrics half of the same question. A scrape failure is exactly as
    silent as a dropped span: Prometheus stays healthy and green and every
    dashboard renders empty (the native twin of this check is
    `_prometheus_api_scrape_issue`, #3721).
    """
    ok, body = _k8s_incluster_get("http://prometheus:9090/api/v1/targets?state=active")
    if not ok:
        return _no_data("observability metrics: could not ask Prometheus for its targets")
    try:
        targets = json.loads(body)["data"]["activeTargets"]
    except (ValueError, KeyError, TypeError):
        return _no_data(
            "observability metrics: Prometheus's target list was unreadable", body[:200]
        )

    up = [t for t in targets if t.get("health") == "up"]
    if up:
        return OpsResult(
            True, f"observability metrics: {len(up)}/{len(targets)} scrape target(s) up"
        )
    return _no_data(
        "observability metrics: no scrape target is up",
        "Prometheus is running and collecting nothing; every Grafana metrics panel will be "
        "empty. Check the api Service is reachable from the prometheus Pod.",
    )


def _k8s_logs_flow_result() -> OpsResult:
    """Has anything been shipped into Loki?

    promtail can be `1/1 ready` and discovering nothing at all -- the labels
    the dashboards query on come from its relabel rules, so an empty label
    set means the log panels are blank whatever the DaemonSet reports.
    """
    ok, body = _k8s_incluster_get("http://loki:3100/loki/api/v1/label/job/values")
    if not ok:
        return _no_data("observability logs: could not ask Loki which jobs it holds")
    try:
        jobs = [str(j) for j in (json.loads(body).get("data") or [])]
    except (ValueError, AttributeError):
        return _no_data("observability logs: Loki's label list was unreadable", body[:200])

    if jobs:
        return OpsResult(True, f"observability logs: Loki holds {len(jobs)} job label(s)")
    return _no_data(
        "observability logs: Loki has received nothing",
        "promtail is running but no Pod logs have landed; the log panels will be empty.",
    )


def _k8s_errors_flow_result() -> OpsResult:
    """Will Grafana's GlitchTip panels authenticate, or 401?

    The second symptom #3990 reported. Grafana's Infinity datasource sends
    the token at `K8S_GRAFANA_GLITCHTIP_TOKEN_MOUNT` as a bearer credential;
    the manifest ships a deliberately non-empty PLACEHOLDER there (an empty
    value crash-loops Grafana's alerting validator, #3538), and before this
    fix nothing ever replaced it -- so every SRE Home GlitchTip panel
    answered `401 Unauthorized` on every Kubernetes deployment.

    Asked in two steps because the two answers have different remedies: a
    placeholder means provisioning never ran, while a real token that is
    refused means it ran and the credential has since been invalidated (the
    Kubernetes shape of the #3565 drift `ops doctor` already checks for
    natively).
    """
    cp = _run(
        [
            "kubectl",
            "-n",
            K8S_NAMESPACE,
            "exec",
            f"deploy/{K8S_TELEMETRY_PROBE_DEPLOYMENT}",
            "--",
            "cat",
            K8S_GRAFANA_GLITCHTIP_TOKEN_MOUNT,
        ],
        check=False,
        expected=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if cp.returncode != 0:
        return _no_data(
            "observability errors: Grafana has no GlitchTip token mounted",
            f"{K8S_GRAFANA_GLITCHTIP_TOKEN_MOUNT} is not readable in the grafana Pod; the "
            "SRE Home GlitchTip panels cannot authenticate.",
        )
    if (cp.stdout or "").strip() == GRAFANA_GLITCHTIP_TOKEN_PLACEHOLDER:
        return _no_data(
            "observability errors: Grafana's GlitchTip token is still the placeholder",
            "The SRE Home GlitchTip panels will answer 401 Unauthorized. Provision a real "
            "token with `nyxgpt ops glitchtip-init --kubernetes`.",
        )

    ok, _body = _k8s_incluster_get(
        f"http://{GLITCHTIP_CONTAINER_HOST}:{GLITCHTIP_CONTAINER_PORT}/api/0/organizations/",
        bearer_token_file=K8S_GRAFANA_GLITCHTIP_TOKEN_MOUNT,
    )
    if not ok:
        return _no_data(
            "observability errors: GlitchTip rejected Grafana's token",
            "The token is not the placeholder but GlitchTip will not accept it -- its "
            "project data was probably re-minted underneath it (#3565). Re-run "
            "`nyxgpt ops glitchtip-init --kubernetes`.",
        )
    return OpsResult(True, "observability errors: GlitchTip accepts Grafana's token")


def _k8s_observability_data_flow(state: dict[str, str] | None = None) -> list[OpsResult]:
    """Ask the four telemetry backends what they have RECEIVED (#3990).

    Reported alongside the readiness lines rather than instead of them: the
    two answer different questions, and the install that shipped this defect
    is the proof that the readiness answer alone is not enough. Skipped
    entirely when the probe Pod is not there (nothing to ask from), so a
    `--skip-observability` cluster or a half-rolled-out layer reports its
    readiness failure without four confusing follow-on lines.

    `state` is the workload map the caller has already read
    (`_k8s_observability_workload_state`); passed in rather than re-read
    because that is a second `kubectl get` for an answer the only caller is
    holding already.
    """
    if _which("kubectl") is None:
        return []
    workloads = _k8s_observability_workload_state() if state is None else state
    probe_state = workloads.get(K8S_TELEMETRY_PROBE_DEPLOYMENT, "absent")
    # Only ask through a Grafana that is actually serving. A Pod still
    # starting cannot answer, and four `[NO DATA]` lines about a tier that is
    # mid-rollout would be exactly the wall of false negatives #3827 removed
    # from the readiness half of this report.
    if _classify_k8s_observability_workload(K8S_TELEMETRY_PROBE_DEPLOYMENT, probe_state).state != (
        K8S_STATE_READY
    ):
        return []
    return [
        _k8s_traces_flow_result(),
        _k8s_metrics_flow_result(),
        _k8s_logs_flow_result(),
        _k8s_errors_flow_result(),
    ]


# A rollout wait checks for blocked Pods every slice rather than blocking on
# one long `kubectl rollout status`: the point is to notice a Pod that will
# never start (`Insufficient memory`, a bad image) in the first minute instead
# of at the end of a 900s budget, which is what made #3827's one real failure
# arrive last and buried among nine false ones.
K8S_ROLLOUT_POLL_SLICE_S = 30

# ...but only after the same Pod has been seen blocked on two consecutive
# slices. `ImagePullBackOff` can follow a single registry hiccup that the next
# kubelet retry clears, and a Pod can be briefly `Unschedulable` while a
# cluster autoscaler adds the node it needs. One confirmation costs ~30s and
# removes the whole class of "aborted a healthy rollout" failures.
K8S_BLOCKED_CONFIRMATIONS = 2

# `CrashLoopBackOff` gets longer, because it is the one blocked reason a
# *healthy* bring-up passes through: a Pod whose dependency is not up yet
# fails its probe, restarts, and kubelet escalates the restart delay
# (10s, 20s, 40s ... capped at 5min) -- leaving the reason visible for
# minutes after the attempt that will succeed has been scheduled. Two slices
# (60s) is well inside that window, so the shorter count would abort a rollout
# that was about to come up. The states that genuinely never resolve
# (`Unschedulable`, a bad image, a missing ConfigMap key) keep the fast count:
# there is nothing to wait for.
K8S_CRASHLOOP_CONFIRMATIONS = 4
_K8S_CRASHLOOP_REASON = "CrashLoopBackOff"


def _k8s_blocked_confirmations(state: K8sWorkloadState) -> int:
    """How many consecutive slices `state` must persist before a wait fails on it."""
    return (
        K8S_CRASHLOOP_CONFIRMATIONS
        if state.reason == _K8S_CRASHLOOP_REASON
        else K8S_BLOCKED_CONFIRMATIONS
    )


# What `kubectl rollout status` prints when it hits its own `--timeout`, as
# opposed to failing for a reason waiting will not fix (object not found,
# cluster unreachable, a paused Deployment).
_K8S_ROLLOUT_TIMEOUT_MARKER = "timed out waiting for the condition"


def _k8s_rollout_timed_out(cp: subprocess.CompletedProcess[str]) -> bool:
    """Whether a failed `rollout status` merely ran out of its slice."""
    return _K8S_ROLLOUT_TIMEOUT_MARKER in ((cp.stdout or "") + (cp.stderr or ""))


def _wait_for_k8s_rollouts(
    workloads: list[tuple[str, str, float]],
    *,
    remedy: str,
    shared_deadline: float | None = None,
) -> list[OpsResult]:
    """Wait for each `(ref, label, budget_s)` to roll out, failing fast on blocked Pods.

    Each workload's budget is its *own*, and its deadline is stamped when that
    workload's wait starts -- not when the whole step started. The workloads
    are waited on one after another, so a single `now + budget` stamped up
    front would silently spend the slow ones' budgets on the ones before them:
    Ollama's 900s allowance for a cold default-model pull became "900s minus
    however long Cassandra took to bootstrap", and an install on a slow link
    then reported the false failure this issue exists to remove. Pass
    `shared_deadline` for the one case where a single pooled budget is
    deliberate (the observability layer, whose workloads roll out together).

    The one wait every Kubernetes bring-up step uses (#3827), so that "ready"
    means the same thing everywhere and the three ways a wait can end are
    distinguished:

    * the workload rolled out -> `[OK] <label> ready`;
    * one of *this workload's own* Pods is in a state waiting does not fix
      (`_k8s_blocked_pods` under the workload's label selector, confirmed over
      `K8S_BLOCKED_CONFIRMATIONS` slices) -> a failure that names that Pod and
      its reason, raised immediately rather than after the whole budget
      drains;
    * the deadline passed -> a failure naming the workload still being waited
      on.

    The selector matters: every tier shares the `nyxgpt` namespace, and the api
    Pods restart against their liveness probe while Cassandra is still
    bootstrapping. Scanning the whole namespace would let that transient
    CrashLoopBackOff abort the *data tier's* wait and report Cassandra as
    broken -- a false failure of exactly the kind this issue is about. A
    workload whose selector cannot be read simply gets no fast-fail (it waits
    out its budget); the wait never invents a failure from a Pod it cannot
    attribute.

    Stops at the first failure: the remaining workloads' verdicts would be
    about a cluster that is already known to be broken.
    """
    results: list[OpsResult] = []
    for ref, label, budget_s in workloads:
        # Stamped here, per workload -- see the docstring. `shared_deadline`
        # is the deliberate exception, not the default.
        deadline = shared_deadline if shared_deadline is not None else time.monotonic() + budget_s
        selector = _k8s_workload_selector(ref)
        # Per workload, not per wait: a Pod confirmed blocked for one workload
        # says nothing about the next one's Pods.
        blocked_seen: dict[str, int] = {}
        while True:
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                results.append(OpsResult(False, f"{label} did not become ready in time", remedy))
                return results
            cp = _run(
                [
                    "kubectl",
                    "-n",
                    K8S_NAMESPACE,
                    "rollout",
                    "status",
                    ref,
                    f"--timeout={max(1, min(remaining, K8S_ROLLOUT_POLL_SLICE_S))}s",
                ],
                check=False,
            )
            if cp.returncode == 0:
                results.append(OpsResult(True, f"{label} ready", _cp_details(cp)))
                break
            if not _k8s_rollout_timed_out(cp):
                # Not a slow rollout: `rollout status` itself could not run
                # (no such object, unreachable cluster, paused Deployment).
                results.append(
                    OpsResult(
                        False,
                        f"{label}: could not check rollout",
                        f"{_cp_details(cp)}\n{remedy}".strip(),
                    )
                )
                return results
            blocked = _k8s_blocked_pods(selector=selector) if selector else []
            names = {s.name for s in blocked}
            blocked_seen = {n: c + 1 for n, c in blocked_seen.items() if n in names}
            for name in names - set(blocked_seen):
                blocked_seen[name] = 1
            confirmed = [
                s for s in blocked if blocked_seen.get(s.name, 0) >= _k8s_blocked_confirmations(s)
            ]
            if confirmed:
                results.append(
                    OpsResult(
                        False,
                        f"{label} cannot start: "
                        + "; ".join(f"pod {s.name}: {s.summary}" for s in confirmed),
                        "\n".join(f"{s.name}: {s.details}" for s in confirmed if s.details)
                        + ("\n" if any(s.details for s in confirmed) else "")
                        + remedy,
                    )
                )
                return results
    return results


def _wait_for_k8s_observability(
    budget_s: int = K8S_OBSERVABILITY_ROLLOUT_BUDGET_S,
) -> list[OpsResult]:
    """Block until every observability workload has rolled out (#3826).

    The observability counterpart of `_wait_for_k8s_data_tier`, and it exists
    for the same reason: `kubectl apply -k` returns as soon as the objects are
    accepted, so everything downstream of it -- `_k8s_stack_health`'s Pod
    states, `_k8s_observability_health`'s ready counts, the operator's first
    look at the SRE dashboard -- reads a cluster whose Pods are still pulling
    multi-hundred-megabyte images, and reports a mid-rollout snapshot as the
    install's verdict.

    One shared deadline across the workloads (see
    `K8S_OBSERVABILITY_ROLLOUT_BUDGET_S`), and a workload that does not make it
    is a failure naming that workload -- an operator told "installed" by a
    command that left Prometheus Pending has been told the wrong thing. This
    is the deliberate exception to `_wait_for_k8s_rollouts`' per-workload
    budgets, so it is passed explicitly rather than being an accident of the
    call's shape: the layer is one unit of a dozen small workloads sharing a
    node's spare capacity, and budgeting each separately would multiply one
    pooled allowance by twelve.
    """
    refs = [f"deploy/{name}" for name in K8S_OBSERVABILITY_DEPLOYMENTS]
    refs += [f"daemonset/{name}" for name in K8S_OBSERVABILITY_DAEMONSETS]
    return _wait_for_k8s_rollouts(
        [(ref, ref, budget_s) for ref in refs],
        shared_deadline=time.monotonic() + budget_s,
        remedy=(
            f"The observability layer had {budget_s}s in total to roll out. Check "
            "`nyxgpt ops status` for the workload's Pods; a Pod stuck Pending usually means "
            "the node cannot fit the stack's resource requests.\nThe layer is part of the "
            "default install; re-run with `--skip-observability` only if you deliberately "
            "want the app tier alone."
        ),
    )


def _k8s_stack_health() -> list[OpsResult]:
    """Snapshot of Pod/Service health in the `nyxgpt` namespace, after the waits.

    A one-shot snapshot, not a wait-until-ready loop -- the install's rollout
    waits (`_wait_for_k8s_data_tier`, `_wait_for_k8s_app_tier`,
    `_wait_for_k8s_observability`) are what decide whether the stack settled,
    and this reports the state they left behind. So a Pod that is still
    starting prints `[PENDING]` and does not fail the command, while a Pod
    that cannot start (unschedulable, image it cannot pull, container in
    CrashLoopBackOff) prints `[FAIL]` with the reason -- one vocabulary,
    shared with `_k8s_observability_health` (#3827). Re-check with `nyxgpt ops
    status`.

    No HPA check here -- the stable/canary Deployments deliberately have none
    (autoscaling would fight canary.py's replica-count-based traffic split;
    see #3409).
    """
    results: list[OpsResult] = []

    states, read_failure = _k8s_pod_states()
    if read_failure is not None:
        results.append(read_failure)
    else:
        if not states:
            results.append(OpsResult(False, f"No pods found in namespace {K8S_NAMESPACE}"))
        results += [s.as_result(prefix="pod ") for s in states]

    # `cassandra`/`ollama` are the data/LLM tier's Services (#3786) -- the
    # hostnames k8s/configmap.yaml points the api at. A missing one is the
    # exact shape of the failure this list exists to catch: api/web Pods
    # Running, nothing to chat with.
    for svc in ("nyxgpt-api", "nyxgpt-web", "cassandra", "ollama"):
        cp = _run(
            ["kubectl", "-n", K8S_NAMESPACE, "get", "svc", svc, "--no-headers"],
            check=False,
            expected=True,
        )
        results.append(
            OpsResult(
                cp.returncode == 0,
                f"Service {svc}" + (" found" if cp.returncode == 0 else " not found"),
            )
        )
    return results


def _wait_for_k8s_data_tier() -> list[OpsResult]:
    """Block until the in-cluster Cassandra and Ollama are Ready (#3786).

    `kubectl apply -k` returns as soon as the objects are accepted, so
    without this the install reports success while Cassandra is still
    bootstrapping its keyspace directory and Ollama is still pulling the
    default model -- and the operator's first chat attempt fails against a
    stack the command just called healthy. `kubectl rollout status` on each
    StatefulSet waits for exactly the condition that matters: the Pod passing
    its readiness probe, which for Cassandra means CQL answers and for Ollama
    means the configured default model is present (see the probes in
    k8s/statefulset-*.yaml), not merely that a port is open.

    A timeout is a failure, not a warning: a stack whose data tier never came
    up cannot chat, and saying otherwise is what produced this issue. The
    failure names the workload so the operator knows which half to look at.
    """
    return _wait_for_k8s_rollouts(
        list(K8S_DATA_TIER_WORKLOADS),
        remedy=(
            "The stack cannot serve chat without it. Check `nyxgpt ops status` for the "
            "workload's Pod state."
        ),
    )


def _wait_for_k8s_app_tier() -> list[OpsResult]:
    """Block until the api and web Deployments have rolled out (#3827).

    The app tier had no wait at all: `kubectl apply -k` returned, and the
    install snapshotted health while the api and web Pods were still being
    created. Whatever that snapshot said was a statement about the first few
    seconds of a rollout, not about the stack -- which is the defect this
    issue is, seen from the app tier's side rather than observability's.

    Only the *stable* Deployments: the canary pair ships at zero replicas by
    design (`nyxgpt canary start` scales it up), so waiting on it would be
    waiting for Pods nobody asked for.
    """
    return _wait_for_k8s_rollouts(
        [(ref, label, K8S_APP_TIER_ROLLOUT_TIMEOUT_S) for ref, label in K8S_APP_TIER_WORKLOADS],
        remedy=(
            "The stack serves neither chat nor the UI without it. Check "
            "`nyxgpt ops status` for the workload's Pod state."
        ),
    )


def _reconcile_k8s_canary_resting() -> list[OpsResult]:
    """Assert the canary Deployments' declared resting state after an install (#3991).

    `kubectl apply -k` sets the manifests' `replicas: 0` on both canary
    Deployments -- that much is verified, and it means the apply itself never
    scales them up. What the install did NOT do is *check*: it applied, waited
    for the stable Deployments, and reported the stack healthy without ever
    asking whether the canary pair was where its own manifests say it rests.
    A cluster whose canary track was left carrying replicas by an interrupted
    rollout (see `canary.reset` for the two routes) therefore came out of a
    fresh install still off-contract, with two idle canary Pods holding live
    Service endpoints outside any rollout.

    Reconciling here rather than trusting the apply is the difference between
    a command that declares a resting state and one that establishes it. It
    is deliberately the same wrapped surface an operator has
    (`nyxgpt canary reset`), not a private `kubectl scale`: one code path, so
    the refusal that protects a live rollout protects the install too.
    """
    from nyxgpt import canary as canary_module

    results: list[OpsResult] = []
    for component, spec in canary_module.COMPONENTS.items():
        # A component with no canary track cannot be off-contract, so there is
        # nothing here to reconcile. `ollama` is the one today (see
        # `canary.OLLAMA_UNSUPPORTED_REASON`), and asking it to reset returns
        # that documented refusal -- which this loop then reported as an
        # install failure, reddening every k8s smoke. Keyed on the capability
        # the spec already declares rather than on the component's name, so a
        # future unsupported component is covered without editing this loop.
        if not spec.supported:
            continue
        result = canary_module.reset(K8S_NAMESPACE, component=component)
        # A refusal because a rollout is genuinely in progress is not an
        # install failure: the operator started it deliberately, and an
        # install must not end their rollout behind their back.
        ok = result.ok or "rollout is in progress" in result.message
        results.append(OpsResult(ok, f"canary {component}: {result.message}", result.details))
    return results


# How long `_ensure_k8s_host_access` gives the host URL to start answering
# before it reports the access path as unverified. The Pods are already Ready
# by the time it runs (`_wait_for_k8s_app_tier`), so this covers only kube-proxy
# programming the NodePort or the freshly-started forward binding its socket --
# seconds, not a rollout.
K8S_HOST_ACCESS_PROBE_BUDGET_S = 60.0


def _probe_web_url(url: str, budget_s: float = K8S_HOST_ACCESS_PROBE_BUDGET_S) -> str | None:
    """Poll `url` until it answers, returning None on success or the last error.

    "Answers" means an HTTP response of any status: the point is that
    something on the host is listening and reaching the cluster, not that the
    web UI likes the request. A 404 from Next.js proves the path end to end
    just as well as a 200.
    """
    deadline = time.monotonic() + budget_s
    last = "no response"
    while True:
        try:
            httpx.get(url, timeout=5.0, follow_redirects=False)
            return None
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
        if time.monotonic() >= deadline:
            return last
        time.sleep(2.0)


def _k8s_access_bridge_owns_host_ports() -> bool:
    """True where the systemd access bridge is (or is about to be) holding 3000/8000.

    The k3s deployment `nyxgpt cloud deploy --kubernetes` provisions reaches
    its cluster through systemd `--user` units running `nyxgpt ops
    port-forward` (docs/cloud.md), which bind exactly the two loopback ports
    `_ensure_k8s_host_access` would otherwise claim -- and the install runs
    BEFORE the provisioning script installs those units, so a forward started
    here would win the bind race and leave every bridge unit restarting
    forever.

    Detected by the substrate (`k3s` on PATH) as well as by the unit template,
    because on a first deploy the template does not exist yet. k3s is the
    right signal: it is the substrate the bridge exists for, and ops already
    branches on it for image import (`_k3s_import_image`).
    """
    if not _is_linux():
        return False
    if _which("k3s") is not None:
        return True
    return (_systemd_user_dir() / f"{K8S_ACCESS_BRIDGE_UNIT}.service").exists()


def _publish_k8s_app_tier_nodeports() -> list[OpsResult]:
    """Patch the api/web Services onto the node ports the cluster maps (#3986).

    Only reached for a cluster nyxGPT provisioned and whose node publishes
    every `KIND_HOST_PORT_MAPPINGS` host port -- see
    `K8S_HOST_PUBLISHED_SERVICES` for why this is a patch rather than a line
    in `k8s/service*.yaml`.

    Idempotent, and it has to be: `kubectl apply -k` sets every field its
    config declares, so the base manifest's `type: ClusterIP` is re-asserted
    on each install and this step re-publishes afterwards. That ordering is
    the reason it lives in the host-access step rather than next to the apply.
    """
    results: list[OpsResult] = []
    for _host_port, (service, port, node_port) in sorted(K8S_HOST_PUBLISHED_SERVICES.items()):
        patch = json.dumps(
            {
                "spec": {
                    "type": "NodePort",
                    "ports": [
                        {
                            "name": "http",
                            "port": port,
                            "targetPort": "http",
                            "nodePort": node_port,
                        }
                    ],
                }
            }
        )
        cp = _run(
            ["kubectl", "-n", K8S_NAMESPACE, "patch", "svc", service, "-p", patch],
            check=False,
        )
        if cp.returncode != 0:
            results.append(
                OpsResult(
                    False, f"Could not publish {service} on node port {node_port}", _cp_details(cp)
                )
            )
            return results
        results.append(OpsResult(True, f"{service} published on node port {node_port}"))
    return results


def _ensure_k8s_host_access() -> list[OpsResult]:
    """Leave the web UI reachable from the browser when the install returns (#3986).

    The install used to end with every Pod Ready and NOTHING listening on the
    host: `k8s/`'s Services were all ClusterIP and the provisioned kind
    cluster published no ports, so the operator had to start a foreground
    `kubectl port-forward` in a spare terminal before the product could be
    used -- a forward that then died with the next Pod replacement. Both
    halves were nyxGPT's own choices, and this step is where they are undone.

    Two paths, because only one of them is nyxGPT's to arrange:

    * **A cluster nyxGPT provisioned** publishes the app tier's NodePorts on
      the host (`KIND_HOST_PORT_MAPPINGS`). Nothing to start: the mapping is
      a property of the node and the NodePort a property of the Service, so
      it survives a canary rollout, a self-heal restart and an image change.
      This verifies the URL rather than asserting it -- the whole complaint
      in #3986 is an install that reported success over an unreachable UI.
    * **Anything else** -- a bring-your-own cluster, or a `nyxgpt-local`
      created by an older nyxGPT with no port mappings -- gets the managed
      background forward, in the shape `nyxgpt cloud tunnel --background`
      established: detached, supervised across Pod replacement, pid recorded
      so `nyxgpt ops port-forward --status/--stop` and `ops down` can find
      it. Still not as good as a mapped port, but it is established BY the
      install rather than left as homework.

    A URL that does not answer is a **warning-shaped failure**, not a silent
    pass: the stack is up, but the thing the operator asked for is not usable
    yet, and saying so is the entire point of the issue.
    """
    context = _kubectl_context()
    provisioned = context == KIND_CONTEXT and _kind_cluster_publishes_host_ports()
    if provisioned:
        results = _publish_k8s_app_tier_nodeports()
        if not all(r.ok for r in results):
            return results
        failure = _probe_web_url(WEB_URL)
        if failure is not None:
            return results + [
                OpsResult(
                    False,
                    f"The cluster publishes {WEB_URL} but it did not answer",
                    f"{failure}\nCheck `nyxgpt ops status` for the web Pods; if the cluster "
                    "was created by an older nyxGPT, `nyxgpt ops down --kubernetes` and "
                    "re-install to recreate it with host port mappings.",
                )
            ]
        return results + [
            OpsResult(
                True,
                f"Web UI reachable at {WEB_URL} (NodePort published by the cluster -- no "
                "port-forward needed, and it survives Pod replacement)",
            )
        ]

    if _k8s_access_bridge_owns_host_ports():
        # A k3s deployment holds 127.0.0.1:3000/:8000 with systemd `--user`
        # units running `nyxgpt ops port-forward` (docs/cloud.md's access
        # bridge, `_k8s_access_bridge_issues`), and `nyxgpt cloud tunnel`
        # forwards the workstation onto them. Starting a second forward here
        # would win the bind race -- this step runs BEFORE the provisioning
        # script installs those units -- and leave every bridge unit
        # restarting forever against a port it can never have. Report the
        # arrangement instead of competing with it.
        return [
            OpsResult(
                True,
                f"{WEB_URL} is held by the Kubernetes access bridge on this instance, not by "
                "a forward started here",
                "systemd --user `nyxgpt-k8s-bridge@{api,web}` run the forward; reach it from "
                "your workstation with `nyxgpt cloud tunnel`. `nyxgpt ops doctor` reports "
                "the units' state.",
            )
        ]

    results = start_port_forward_background("app")
    if not all(r.ok for r in results):
        return results
    failure = _probe_web_url(WEB_URL)
    if failure is not None:
        return results + [
            OpsResult(
                False,
                f"Started a background port-forward but {WEB_URL} did not answer",
                f"{failure}\nSee {K8S_PORT_FORWARD_LOG_FILE} for what the forward reported, "
                "and `nyxgpt ops port-forward --status`.",
            )
        ]
    return results + [
        OpsResult(
            True,
            f"Web UI reachable at {WEB_URL} (managed background port-forward -- "
            "`nyxgpt ops port-forward --status` / `--stop`)",
        )
    ]


# --- Checkout-free image builds for the Kubernetes path (#3834) ---
#
# `--kubernetes` used to build both images from `REPO_ROOT` unconditionally,
# which made it a dev deployment permanently -- one that never said so -- and
# made it unrunnable on the machine the repo-less portability requirement
# actually targets: one with no checkout.
#
# The artifact path builds the same two images from the same published
# artifacts the native artifact path installs: `nyxgpt-api-<version>.tar.gz`
# and `nyxgpt-web-<version>.tar.gz`, resolved by `_service_source_tarball`
# (vendored from a checkout when there is one, taken from
# `$NYXGPT_ARTIFACT_DIR` when staged, else downloaded from the GitHub
# Release). One artifact channel for every local install mode is what keeps a
# *release candidate* installable here: an rc publishes those tarballs but no
# container image, so a path that could only pull `ghcr.io` images would work
# for stable releases and fail for exactly the builds acceptance testing runs.
#
# The staging half below is shared, not Kubernetes-specific: #3985 moved the
# Terraform artifact path onto this same channel, for exactly the reason the
# paragraph above gives, and it calls `_stage_artifact_build_context` with its
# own staging root (`TF_BUILD_DIR`).
#
# `--dev` keeps the old behavior, now explicit and recorded: build from the
# working tree.
K8S_BUILD_DIR = NYXGPT_HOME / "build" / "kubernetes"

# The published artifact each Kubernetes image is built from, and the
# directory name its staged context gets -- the same two pairs the Terraform
# artifact path uses, keyed by this path's image tags. See
# `ARTIFACT_IMAGE_SOURCES` for why the context directory name is load-bearing.
K8S_IMAGE_ARTIFACTS: dict[str, tuple[str, str]] = {
    K8S_IMAGE: ARTIFACT_IMAGE_SOURCES["api"],
    TF_WEB_IMAGE: ARTIFACT_IMAGE_SOURCES["web"],
}


def _stage_api_build_files(context: Path) -> None:
    """Add the two files the api image's Dockerfile COPYs that the tarball has no copy of.

    The `nyxgpt-api` artifact vendors what the *service* needs (pyproject.toml,
    `src/nyxgpt/`, example.config.ini) -- it is the same tarball the Homebrew
    formula builds a venv from, and it carries no Dockerfile. Both missing
    pieces come from the running package's own resources rather than from the
    tarball, which is what lets an already-published artifact (an rc cut
    before this code existed) still be built into an image:

    - the Dockerfile, packaged as `nyxgpt-api.Dockerfile` -- a link to the
      repository's root `Dockerfile`, so there is one Dockerfile, not two that
      drift;
    - `docker/entrypoint.sh`, which the unpacked tarball does carry (under
      `src/nyxgpt/resources/docker/`, since the vendored tree includes the
      package's own resources), just not at the path the Dockerfile COPYs it
      from.

    Raises RuntimeError naming the missing file if either is absent.
    """
    dockerfile = _packaged_resources_root() / "docker" / "nyxgpt-api.Dockerfile"
    if not dockerfile.is_file():
        raise RuntimeError(
            f"the packaged api Dockerfile is missing ({dockerfile}) -- this nyxgpt "
            "installation's package data is incomplete; reinstall it"
        )
    _copy_file(dockerfile, context / "Dockerfile")
    entrypoint = context / "src" / "nyxgpt" / "resources" / "docker" / "entrypoint.sh"
    if not entrypoint.is_file():
        raise RuntimeError(
            f"the staged nyxgpt-api artifact has no {entrypoint.name} at "
            f"{entrypoint} -- it is not a nyxgpt-api tarball this version can build"
        )
    _copy_file(entrypoint, context / "docker" / "entrypoint.sh", mode=0o755)


def _stage_k8s_artifact_context(image: str) -> Path:
    """`_stage_artifact_build_context` for a Kubernetes `image` tag."""
    service, context_name = K8S_IMAGE_ARTIFACTS[image]
    return _stage_artifact_build_context(service, context_name, K8S_BUILD_DIR)


def _stage_artifact_build_context(service: str, context_name: str, root_dir: Path) -> Path:
    """Unpack `service`'s published source tarball into a docker build context.

    `service` is `nyxgpt-api` or `nyxgpt-web`; `context_name` is the directory
    name the context gets (load-bearing for the build fingerprint -- see
    `ARTIFACT_IMAGE_SOURCES`); `root_dir` is the caller's staging root, which
    differs per substrate so two installs on one machine cannot collide
    (`K8S_BUILD_DIR` vs `TF_BUILD_DIR`).

    The context is rebuilt from the artifact on every install rather than
    updated in place, so a half-unpacked or hand-edited leftover can never be
    what gets built. Raises RuntimeError with a diagnosis the caller turns
    into an OpsResult -- the artifact may be missing, undownloadable, or not
    the tarball this version knows how to build.
    """
    version = _native_service_version()
    root = root_dir / service
    _ensure_dir(root)
    tarball = _service_source_tarball(root, service, version)

    unpacked = root / "unpacked"
    if unpacked.exists():
        shutil.rmtree(unpacked)
    with tarfile.open(tarball) as tf:
        # Trusted input in the same sense as `_install_native_web_systemd`'s
        # extract: this is nyxGPT's own release artifact (or the checkout's
        # own vendored copy of it), not user-supplied.
        tf.extractall(unpacked, filter="data")
    extracted = unpacked / f"{service}-{version}"
    if not extracted.is_dir():
        raise RuntimeError(
            f"{tarball} does not contain the expected {service}-{version}/ directory"
        )

    context = root / context_name
    if context.exists():
        shutil.rmtree(context)
    extracted.replace(context)
    shutil.rmtree(unpacked, ignore_errors=True)

    if service == "nyxgpt-api":
        _stage_api_build_files(context)
    elif not (context / "Dockerfile").is_file():
        raise RuntimeError(
            f"the staged {service} artifact has no Dockerfile at {context / 'Dockerfile'} "
            "-- it is not a nyxgpt-web tarball this version can build"
        )
    return context


def _build_and_load_k8s_api_image(dev: bool = False) -> list[OpsResult]:
    """Build/load `nyxgpt-api:local` from the working tree (`dev`) or the published artifact.

    Artifact mode (the default) stages the published `nyxgpt-api` tarball and
    builds that; dev mode builds the checkout exactly as every install did
    before #3834. Both produce the same tag from the same Dockerfile -- what
    differs is *which source* is in the image, which is why the mode is
    recorded and reported rather than left to be guessed at.
    """
    if dev:
        return _build_and_load_k8s_image()
    try:
        context = _stage_k8s_artifact_context(K8S_IMAGE)
    except (RuntimeError, OSError, tarfile.TarError) as e:
        return [
            OpsResult(
                False,
                "Could not stage the published nyxgpt-api artifact to build the image from",
                f"{type(e).__name__}: {e}",
            )
        ]
    return _build_and_load_k8s_image(
        K8S_IMAGE,
        context=context,
        fingerprint_paths=[context / rel for rel in _API_IMAGE_FINGERPRINT_RELPATHS],
    )


def _build_and_load_k8s_web_image(dev: bool = False) -> list[OpsResult]:
    """Build/load `nyxgpt-web:local`, the web canary pair's image (#3419).

    Mirrors `_build_terraform_docker_images`'s web build: the context is the
    web tree (not the repo root), fingerprinted on that tree itself (excluding
    `_WEB_VENDOR_EXCLUDES` build artifacts) rather than on the whole build
    context, with the same `NEXT_PUBLIC_API_BASE_URL` build arg default
    Terraform's local deploy uses -- this is inlined into the browser bundle
    at build time, and like Terraform's containers, the deployment is reached
    from the operator's own workstation at `127.0.0.1`, so the same host-local
    default applies. (Nothing under `web/src` reads that variable today: every
    browser call is a relative `/api/...` served by a Next.js route handler,
    which reaches the api in-cluster. It is passed for parity with the
    Terraform build rather than because a browser needs it -- see #3986.)

    That tree is `<checkout>/web` in dev mode and the staged published
    `nyxgpt-web` artifact otherwise (#3834); the artifact carries its own
    Dockerfile, since `web/Dockerfile` is part of the vendored tree.
    """
    if dev:
        context = REPO_ROOT / "web"
    else:
        try:
            context = _stage_k8s_artifact_context(TF_WEB_IMAGE)
        except (RuntimeError, OSError, tarfile.TarError) as e:
            return [
                OpsResult(
                    False,
                    "Could not stage the published nyxgpt-web artifact to build the image from",
                    f"{type(e).__name__}: {e}",
                )
            ]
    return _build_and_load_k8s_image(
        TF_WEB_IMAGE,
        context=context,
        fingerprint_paths=[context],
        excludes=_WEB_VENDOR_EXCLUDES,
        build_args={"NEXT_PUBLIC_API_BASE_URL": TF_WEB_API_BASE_URL_DEFAULT},
    )


# --- Node capacity preflight (#3825) ---
#
# A Pod's REQUEST reserves node capacity at schedule time; its LIMIT caps the
# peak. Apply a stack whose requests exceed the node and kubectl still
# succeeds -- the objects are accepted, the Deployments report progressing,
# and one Pod simply sits `Pending / FailedScheduling: Insufficient memory`
# forever. That is how #3825 presented: the install said it was done,
# prometheus was never scheduled, and the operator's later `nyxgpt canary
# start` failed the same way and looked like a broken canary.
#
# Checked for BOTH memory and cpu, not just the resource the issue named:
# right-sizing the memory alone moved the wall rather than removing it -- on
# a 4-core VM the canary Pod then failed with `Insufficient cpu` instead, an
# identical failure with a different word in it.
#
# The manifests were right-sized in the same change, but sizing alone is not
# a fix: the operator's node is whatever their Docker Desktop VM was given,
# and a stack that fits 8Gi/4 cores does not fit 4Gi/2. So the install
# measures the node it is about to fill and says so BEFORE applying anything,
# instead of leaving Pods Pending for the operator to diagnose.

# The two resources the scheduler will refuse a Pod over here. (Ephemeral
# storage is a third in principle; nothing in this stack requests any.)
_K8S_PREFLIGHT_RESOURCES = ("memory", "cpu")

# Kubernetes resource-quantity suffixes (binary and decimal), per
# k8s.io/apimachinery/pkg/api/resource.
_K8S_QUANTITY_MULTIPLIERS: dict[str, float] = {
    "": 1,
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}
# `K` is matched as well as `k` because the binary suffixes capitalise it
# ("Ki" is what a node reports its allocatable memory in) while the decimal
# kilo is lower-case. A bare "K" is not a legal suffix and falls out as
# unparseable at the lookup below.
_K8S_QUANTITY_RE = re.compile(r"^(\d+(?:\.\d+)?)([kKMGTPE]i?|m)?$")

_MIB = 1024**2


def _parse_k8s_quantity(value: object) -> int | None:
    """Parse a Kubernetes memory quantity ("512Mi", "2Gi", "1000M") into bytes.

    Returns None for anything unparseable rather than guessing -- the
    preflight downgrades itself to a skip when it cannot read a figure,
    which is the safe direction: never block an install on a number we did
    not understand.
    """
    if not isinstance(value, str):
        return None
    match = _K8S_QUANTITY_RE.match(value.strip())
    if match is None:
        return None
    amount = float(match.group(1))
    suffix = match.group(2) or ""
    if suffix == "m":
        return int(amount / 1000)
    multiplier = _K8S_QUANTITY_MULTIPLIERS.get(suffix)
    if multiplier is None:
        return None
    return int(amount * multiplier)


def _parse_k8s_cpu(value: object) -> int | None:
    """Parse a Kubernetes CPU quantity ("250m", "2", "1.5") into millicores.

    A separate unit from memory on purpose: CPU's "m" suffix is the normal
    way to write it, and rounding 250m to "0 bytes" the way the memory
    parser would makes every comparison meaningless.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _K8S_QUANTITY_RE.match(text)
    if match is None:
        return None
    amount = float(match.group(1))
    suffix = match.group(2) or ""
    if suffix == "m":
        return int(amount)
    if suffix != "":
        return None
    return int(amount * 1000)


def _parse_k8s_resource(value: object, resource: str) -> int | None:
    """Parse a quantity in the unit that resource is compared in."""
    if resource == "cpu":
        return _parse_k8s_cpu(value)
    return _parse_k8s_quantity(value)


def _format_k8s_resource(value: int, resource: str) -> str:
    """Render a parsed figure back in the unit the manifests are written in."""
    if resource == "cpu":
        return f"{value}m"
    return f"{value // _MIB}Mi"


def _container_resource_request(container: dict[str, Any], resource: str) -> object:
    """`resources.requests.<resource>` of one container, or None if it sets none."""
    resources = container.get("resources") or {}
    requests = resources.get("requests") or {}
    return requests.get(resource)


def _pod_resource_request(pod_spec: dict[str, Any], resource: str = "memory") -> int:
    """Effective request of one Pod for `resource`, in that resource's unit.

    The scheduler charges `max(sum of the regular containers, the largest
    initContainer)`: init containers run to completion one at a time before
    the regular ones start, so they set a floor rather than adding. (Native
    sidecars -- initContainers with `restartPolicy: Always` -- do add, and
    are summed with the regular containers here for that reason.)
    """
    total = 0
    floor = 0
    for container in pod_spec.get("containers") or []:
        total += (
            _parse_k8s_resource(_container_resource_request(container, resource), resource) or 0
        )
    for init in pod_spec.get("initContainers") or []:
        request = _parse_k8s_resource(_container_resource_request(init, resource), resource) or 0
        if init.get("restartPolicy") == "Always":
            total += request
        else:
            floor = max(floor, request)
    return max(total, floor)


def _workload_resource_requests(
    objects: list[dict[str, Any]],
    *,
    node_count: int,
    resource: str = "memory",
    canary_pool_ceiling: int = 1,
) -> tuple[int, int, list[tuple[str, int]]]:
    """Total what a set of rendered manifests will reserve of `resource`.

    Returns `(scheduled, standby, breakdown)`:

    * `scheduled` -- what applying these manifests reserves right away:
      per-Pod request x replicas, and x `node_count` for a DaemonSet, which
      places one Pod on every node.
    * `standby` -- what a workload deliberately parked at `replicas: 0` will
      ask for the moment something scales it up. That is exactly the canary
      pair (`nyxgpt canary start`), so this is the headroom a rollout needs
      and the reason a 99%-full node reads as "installed fine" right up
      until the operator starts a canary (#3825). Derived from the manifests
      rather than hardcoded, so a new parked workload is counted for free.
    * `breakdown` -- `(name, amount)` per workload, largest first, for the
      operator-facing detail.

    `canary_pool_ceiling` is `[canary] total_replicas`. Since #3833 a rollout
    does not carve its split out of a standing pool -- it GROWS the track to
    at most that many Pods and gives them back on promote/rollback -- so the
    headroom a rollout needs is the whole difference between the ceiling and
    the stable Deployment's resting count, not the single parked Pod that
    used to be the only thing scaled up. Counting one Pod here would let an
    install pass this preflight and still strand a rollout, which is exactly
    #3825's defect one step later. Left at 1 (the pre-#3833 meaning) when the
    caller has no config to read.
    """
    scheduled = 0
    standby = 0
    breakdown: list[tuple[str, int]] = []
    # Resting counts first: a parked canary is charged against the count its
    # own stable track rests at, and kustomize may render either one first.
    resting: dict[str, int] = {}
    for obj in objects:
        if obj.get("kind") != "Deployment":
            continue
        declared = (obj.get("spec") or {}).get("replicas")
        name = ((obj.get("metadata") or {}).get("name")) or ""
        if name:
            resting[name] = 1 if declared is None else int(declared)
    for obj in objects:
        kind = obj.get("kind")
        if kind not in ("Deployment", "StatefulSet", "DaemonSet"):
            continue
        spec = obj.get("spec") or {}
        pod_spec = ((spec.get("template") or {}).get("spec")) or {}
        per_pod = _pod_resource_request(pod_spec, resource)
        if kind == "DaemonSet":
            replicas = max(node_count, 1)
        else:
            declared = spec.get("replicas")
            replicas = 1 if declared is None else int(declared)
        name = ((obj.get("metadata") or {}).get("name")) or kind.lower()
        if replicas == 0:
            # The Pods a rollout of this track adds to the node: the canary
            # itself, plus every stable replica the pool has to borrow to
            # express the weight. Never fewer than one -- a parked workload
            # with no stable partner is still one Pod when something scales
            # it up.
            partner = f"{name.removesuffix('-canary')}-stable"
            borrowed = max(1, canary_pool_ceiling - resting.get(partner, canary_pool_ceiling - 1))
            standby += per_pod * borrowed
            continue
        scheduled += per_pod * replicas
        breakdown.append((f"{name} x{replicas}", per_pod * replicas))
    breakdown.sort(key=lambda item: item[1], reverse=True)
    return scheduled, standby, breakdown


# `_k8s_unschedulable_pods()` used to live here (#3825): a second `kubectl`
# read that named the Pods with an empty `.spec.nodeName`. It is gone as of
# the #3827 merge, not because the operator stopped needing that list -- the
# Infrastructure page still shows it, and `infra_status` still fills
# `kubernetes.unschedulable` -- but because it is now taken from
# `_classify_k8s_pod` (`K8S_SUMMARY_UNSCHEDULABLE`) like every other verdict
# in this module. The nodeName heuristic also disagreed with the classifier
# for a moment on every install: a Pod the scheduler has accepted but not yet
# bound has no `.spec.nodeName` either, so it was named "could not be
# scheduled" on the same page that badged it PENDING.


def _k8s_render_kustomization(directory: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Render a kustomization to objects without applying it.

    `--dry-run=client` builds the objects locally and prints them; nothing
    reaches the cluster, which is the whole point of a preflight. Returns
    `([], reason)` when the render fails -- callers skip rather than block.
    """
    cp = _run(
        [
            "kubectl",
            "apply",
            "-k",
            str(directory),
            "--dry-run=client",
            "--validate=false",
            "-o",
            "json",
        ],
        check=False,
    )
    if cp.returncode != 0:
        return [], f"could not render {directory}: {(cp.stderr or '').strip()[:200]}"
    try:
        rendered = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        return [], f"could not parse the rendered {directory}: {e}"
    if rendered.get("kind") == "List":
        return list(rendered.get("items") or []), None
    return [rendered], None


def _k8s_node_allocatable() -> tuple[dict[str, int], int, str | None]:
    """Allocatable capacity across schedulable nodes: `(per-resource, count, error)`.

    Allocatable, not capacity: the kubelet's reserved slice is already
    subtracted there, and it is what the scheduler actually compares
    requests against. Cordoned nodes are excluded -- nothing new will land
    on them. One `kubectl get` for both resources.
    """
    cp = _run(["kubectl", "get", "nodes", "-o", "json"], check=False)
    if cp.returncode != 0:
        return {}, 0, f"could not read node capacity: {(cp.stderr or '').strip()[:200]}"
    try:
        payload = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        return {}, 0, f"could not parse node capacity: {e}"
    totals = dict.fromkeys(_K8S_PREFLIGHT_RESOURCES, 0)
    count = 0
    for node in payload.get("items") or []:
        if ((node.get("spec") or {}).get("unschedulable")) is True:
            continue
        allocatable = ((node.get("status") or {}).get("allocatable")) or {}
        parsed = {
            resource: _parse_k8s_resource(allocatable.get(resource), resource)
            for resource in _K8S_PREFLIGHT_RESOURCES
        }
        if any(value is None for value in parsed.values()):
            continue
        for resource, value in parsed.items():
            totals[resource] += value or 0
        count += 1
    if count == 0:
        return {}, 0, "no schedulable node reported allocatable capacity"
    return totals, count, None


def _k8s_committed_requests(exclude_namespace: str) -> tuple[dict[str, int], str | None]:
    """What Pods outside `exclude_namespace` have already reserved on the nodes.

    kube-system alone accounts for a few hundred MiB and most of a core on a
    kind node, and it is charged against the same allocatable pool the stack
    is about to draw from -- comparing the stack against raw allocatable
    would overstate what is free by exactly that much. Our own namespace is
    excluded because this install is what defines its contents: counting the
    previous revision's Pods would double-charge a re-install.
    """
    cp = _run(["kubectl", "get", "pods", "--all-namespaces", "-o", "json"], check=False)
    if cp.returncode != 0:
        return {}, f"could not read scheduled Pods: {(cp.stderr or '').strip()[:200]}"
    try:
        payload = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        return {}, f"could not parse scheduled Pods: {e}"
    totals = dict.fromkeys(_K8S_PREFLIGHT_RESOURCES, 0)
    for pod in payload.get("items") or []:
        metadata = pod.get("metadata") or {}
        if metadata.get("namespace") == exclude_namespace:
            continue
        spec = pod.get("spec") or {}
        if not spec.get("nodeName"):
            # Not scheduled, so not holding capacity.
            continue
        if ((pod.get("status") or {}).get("phase")) in ("Succeeded", "Failed"):
            continue
        for resource in _K8S_PREFLIGHT_RESOURCES:
            totals[resource] += _pod_resource_request(spec, resource)
    return totals, None


def _evaluate_k8s_capacity(
    objects: list[dict[str, Any]],
    *,
    resource: str,
    allocatable: int,
    node_count: int,
    committed: int,
    skip_observability: bool,
    canary_pool_ceiling: int = 1,
    observability_objects: list[dict[str, Any]] | None = None,
) -> OpsResult:
    """Compare one resource's requests against what the cluster has left.

    Three outcomes, all of them reported rather than left for the operator
    to find in `kubectl describe`:

    * does not fit -> a failing result naming the shortfall and what to do
      about it. The install stops there, which is strictly better than the
      pre-#3825 behaviour of applying anyway and leaving a Pod Pending.
    * fits, but not with the canary pair's headroom -> a passing result that
      says so, so "start a canary later" is a known constraint rather than a
      surprise failure.
    * fits with headroom -> a passing result with the figures.

    On a multi-node cluster the comparison is against the SUM of the nodes,
    which no single Pod can draw on, so a shortfall there is reported as a
    warning rather than a refusal -- summed capacity proves a stack cannot
    fit, but never that it can.

    `observability_objects` is the subset of `objects` the `--skip-observability`
    flag would drop. It is what decides whether the refusal may offer that
    flag: advice the operator can follow and still be refused is worse than no
    advice, because they spend a second install finding that out (#3825).
    """
    requested, standby, breakdown = _workload_resource_requests(
        objects,
        node_count=node_count,
        resource=resource,
        canary_pool_ceiling=canary_pool_ceiling,
    )
    free = allocatable - committed

    def show(value: int) -> str:
        return _format_k8s_resource(value, resource)

    detail = (
        f"node allocatable {show(allocatable)}, already reserved by other namespaces "
        f"{show(committed)}, free {show(free)}; this stack requests {show(requested)}"
        f" across {node_count} node(s).\n"
        + "\n".join(f"  {name}: {show(size)}" for name, size in breakdown)
    )

    if requested > free:
        shortfall = show(requested - free)
        knob = "Memory" if resource == "memory" else "CPUs"
        remedy = (
            f"Give the cluster VM at least {shortfall} more {resource} (Docker Desktop: "
            f"Settings -> Resources -> {knob}, then `nyxgpt ops down --kubernetes` and "
            "re-run this install)"
        )
        if not skip_observability:
            # Only when dropping the layer actually closes the gap. Offering
            # it unconditionally sent an operator whose shortfall is larger
            # than the layer into a second install that refuses them again --
            # and reads as "nyxGPT told me to do this and it did not work".
            layer, _, _ = _workload_resource_requests(
                observability_objects or [],
                node_count=node_count,
                resource=resource,
                canary_pool_ceiling=canary_pool_ceiling,
            )
            if layer and requested - layer <= free:
                remedy += (
                    f", or install without the observability layer, which is {show(layer)} "
                    "of the above: `nyxgpt ops install --kubernetes "
                    "--skip-observability`"
                )
        message = (
            f"Not enough node {resource}: the stack requests {show(requested)} but only "
            f"{show(free)} is free"
        )
        if node_count > 1:
            # Summed capacity cannot prove a per-node placement is possible,
            # so it must not be used to refuse one.
            return OpsResult(True, f"Warning: {message}", f"{detail}\n{remedy}")
        return OpsResult(False, message, f"{detail}\n{remedy}\nNothing was applied.")

    if requested + standby > free:
        return OpsResult(
            True,
            f"{resource.capitalize()} is tight: {show(free - requested)} free after install, "
            f"and a canary rollout needs {show(standby)}",
            f"{detail}\n`nyxgpt canary start` will leave its Pod Pending until the "
            f"cluster VM has more {resource}.",
        )

    return OpsResult(
        True,
        f"Node {resource} is sufficient: {show(requested)} requested, {show(free)} free "
        f"({show(standby)} of that reserved for a canary rollout)",
        detail,
    )


def _preflight_k8s_capacity(*, skip_observability: bool = False) -> list[OpsResult]:
    """Refuse to fill a node the stack does not fit on, before applying anything (#3825).

    Renders the manifests that are about to be applied and evaluates their
    memory and cpu requests against the cluster (see
    `_evaluate_k8s_capacity` for what each outcome means). Anything it cannot
    measure -- a render that fails, a node that reports no allocatable
    capacity -- is a skip, never a block: the preflight exists to catch a
    known-bad arithmetic result, not to become a new way for the install to
    refuse.
    """
    # Read the node first: a preflight that is about to skip itself must not
    # leave a bootstrapped Secret behind as a side effect.
    allocatable, node_count, error = _k8s_node_allocatable()
    if error is not None:
        return [OpsResult(True, "Skipped capacity preflight", error)]

    # Both tiers land in the same namespace and draw on the same node, so
    # the figure that matters is their union. The app tier is included only
    # once its Secret exists, which is also the marker for "an app tier was
    # bootstrapped at all" (`_down_kubernetes_steps` uses the same one) --
    # `nyxgpt ops observability --kubernetes --local` on a cluster that has
    # never had one must not be measured as though it did.
    directories = [K8S_DIR] if (K8S_DIR / "secret.yaml").exists() else []
    if not skip_observability:
        # The observability kustomization references its Secret, so it can
        # only be rendered once that has been bootstrapped. Idempotent: the
        # apply step later finds the same file and leaves it alone.
        secret_results = _ensure_k8s_observability_secret()
        if not all(r.ok for r in secret_results):
            return secret_results
        directories.append(K8S_OBSERVABILITY_DIR)

    objects: list[dict[str, Any]] = []
    # Kept apart as well as summed: a refusal may only name
    # `--skip-observability` as a way out once it knows this subset is big
    # enough to be one.
    observability_objects: list[dict[str, Any]] = []
    for directory in directories:
        rendered, error = _k8s_render_kustomization(directory)
        if error is not None:
            return [OpsResult(True, "Skipped capacity preflight", error)]
        objects += rendered
        if directory == K8S_OBSERVABILITY_DIR:
            observability_objects += rendered

    committed, error = _k8s_committed_requests(K8S_NAMESPACE)
    if error is not None:
        return [OpsResult(True, "Skipped capacity preflight", error)]

    # How far `nyxgpt canary start` may grow a track (#3833) -- the pool is
    # borrowed for the rollout, so this is the headroom the node has to keep
    # free, not a count of standing Pods. An unreadable config falls back to
    # the shipped default rather than to "one Pod", which would understate it.
    from nyxgpt.config import get_canary_total_replicas, load_config

    try:
        ceiling = get_canary_total_replicas(load_config())
    except Exception:  # pragma: no cover - config is best-effort here
        ceiling = 4

    return [
        _evaluate_k8s_capacity(
            objects,
            resource=resource,
            allocatable=allocatable.get(resource, 0),
            node_count=node_count,
            committed=committed.get(resource, 0),
            skip_observability=skip_observability,
            canary_pool_ceiling=ceiling,
            observability_objects=observability_objects,
        )
        for resource in _K8S_PREFLIGHT_RESOURCES
    ]


# The app tier's Deployments, in the order a rollout should touch them.
# Named explicitly rather than discovered by label so a Deployment that failed
# to apply reports as missing instead of dropping silently out of a restart.
K8S_APP_TIER_DEPLOYMENTS = (
    "nyxgpt-api-stable",
    "nyxgpt-api-canary",
    "nyxgpt-web-stable",
    "nyxgpt-web-canary",
)

# How long each app-tier Deployment gets to finish an install-mode *restart*
# rollout (`_restart_k8s_app_tier`, #3834). Distinct from
# `K8S_APP_TIER_ROLLOUT_TIMEOUT_S`, the budget the *install's* app-tier wait
# uses: this one rolls Pods whose image is already on the node, so it needs no
# allowance for a build or a load. The two were both named
# `K8S_APP_TIER_ROLLOUT_TIMEOUT_S` -- landed by two PRs open at the same time,
# and neither ruff nor mypy flags a rebound module constant -- so this later
# assignment silently halved the install wait #3827 added, reinstating the
# false `[FAIL]`-on-a-still-starting-Pod that issue exists to remove.
K8S_APP_TIER_RESTART_TIMEOUT_S = 300


def _restart_k8s_app_tier() -> list[OpsResult]:
    """Roll the app-tier Deployments so they pick up a just-rebuilt `:local` image.

    Needed only when the install mode *changed* (#3834). Both modes produce
    the same two mutable tags, so the Deployment specs are byte-identical
    across a switch and `kubectl apply -k` has nothing to update -- the Pods
    would keep running the previous mode's image while the marker, `ops
    status` and the dashboard all said the new one. That is precisely the
    class of lie this issue exists to remove, so the switch forces the
    rollout and waits for it rather than reporting a mode the cluster is not
    running yet.

    A Deployment that isn't there is not a failure: `nyxgpt ops observability
    --kubernetes` can create the namespace without an app tier.
    """
    results: list[OpsResult] = []
    for deployment in K8S_APP_TIER_DEPLOYMENTS:
        ref = f"deploy/{deployment}"
        exists = _run(
            ["kubectl", "-n", K8S_NAMESPACE, "get", ref], check=False, expected=True
        ).returncode
        if exists != 0:
            results.append(OpsResult(True, f"{ref} not deployed -- nothing to roll"))
            continue
        cp = _run(["kubectl", "-n", K8S_NAMESPACE, "rollout", "restart", ref], check=False)
        if cp.returncode != 0:
            results.append(
                OpsResult(False, f"kubectl rollout restart {ref} failed", _cp_details(cp))
            )
            return results
        cp = _run(
            [
                "kubectl",
                "-n",
                K8S_NAMESPACE,
                "rollout",
                "status",
                ref,
                f"--timeout={K8S_APP_TIER_RESTART_TIMEOUT_S}s",
            ],
            check=False,
        )
        if cp.returncode != 0:
            results.append(
                OpsResult(
                    False,
                    f"{ref} did not finish rolling onto the new install mode's image",
                    _cp_details(cp),
                )
            )
            return results
        results.append(OpsResult(True, f"Rolled {ref} onto the new install mode's image"))
    return results


# The systemd --user template a `nyxgpt cloud deploy --kubernetes` installs to
# bridge the instance's loopback into the ClusterIP-only Services (#3956). Named
# here so `doctor` can report it: on a cloud Kubernetes deployment this unit,
# not the API process, is what holds 127.0.0.1:8000, so it is a distinct thing
# that can be down while every Pod is Running -- and an operator told only "the
# API did not answer" goes looking in the cluster, where nothing is wrong.
K8S_ACCESS_BRIDGE_UNIT = "nyxgpt-k8s-bridge@"
K8S_ACCESS_BRIDGE_TARGETS: tuple[str, ...] = ("api", "web", "observability")


def _k8s_access_bridge_issues() -> list[str]:
    """Report the access bridge's units, returning `doctor` issues for any down.

    Silent where no bridge exists: the template is installed only by a cloud
    `--kubernetes` deploy, so a local kind/minikube cluster (where the install
    publishes the NodePorts on the host, or manages its own background
    forward -- #3986) has no units to report and gets no output. Reads only
    `is-active`; `doctor` never starts anything.
    """
    if not _is_linux():
        return []
    unit_dir = _systemd_user_dir()
    if not (unit_dir / f"{K8S_ACCESS_BRIDGE_UNIT}.service").exists():
        return []
    issues: list[str] = []
    for target in K8S_ACCESS_BRIDGE_TARGETS:
        unit = f"{K8S_ACCESS_BRIDGE_UNIT}{target}.service"
        enabled = _run(
            ["systemctl", "--user", "is-enabled", unit],
            check=False,
            expected=True,
            timeout=LOCAL_PROBE_TIMEOUT_SECONDS,
        )
        if (enabled.stdout or "").strip() != "enabled":
            # Not enabled is not a fault: `--skip-observability` deliberately
            # leaves the observability bridge alone.
            continue
        active = _run(
            ["systemctl", "--user", "is-active", unit],
            check=False,
            expected=True,
            timeout=LOCAL_PROBE_TIMEOUT_SECONDS,
        )
        state = (active.stdout or "").strip() or "unknown"
        print(f"Kubernetes access bridge ({target}): {state}")
        if state != "active":
            issues.append(
                f"The Kubernetes access bridge for {target} is {state}. That unit, not "
                f"the Pod, is what holds this host's loopback port, so the stack is "
                f"unreachable through the SSH tunnel even with every Pod Running. "
                f"Re-run `nyxgpt cloud deploy` to reconcile it."
            )
    return issues


def _record_k8s_install_mode(dev: bool) -> list[OpsResult]:
    """Record the mode the Kubernetes deployment was just built in (#3834).

    Runs after the kustomization is applied, not before it: the marker is a
    statement about a deployment that exists, so an install that died at the
    image build must leave the previous deployment's record alone rather than
    relabel it. On a *change* of mode it also rolls the app tier, so what the
    cluster runs and what the marker says can't diverge.

    Its own marker, separate from the native one: a machine can run a native
    dev install and a Kubernetes artifact deployment at the same time, and one
    machine-wide answer is what made `ops status` report a native `dev` mode
    for a pure-Kubernetes deployment.
    """
    previous = read_install_mode(substrate=SUBSTRATE_KUBERNETES)
    target = INSTALL_MODE_DEV if dev else INSTALL_MODE_ARTIFACT
    checkout = _dev_checkout_root() if dev else None
    results: list[OpsResult] = []

    if previous.recorded and previous.mode != target:
        results.append(
            OpsResult(True, f"Kubernetes install mode changing: {previous.mode} -> {target}")
        )
        results += _restart_k8s_app_tier()
        if not all(r.ok for r in results):
            return results

    state = InstallModeState(
        mode=target,
        checkout=str(checkout) if checkout else None,
        substrate=SUBSTRATE_KUBERNETES,
        recorded=True,
    )
    marker = write_install_mode(target, checkout, substrate=SUBSTRATE_KUBERNETES)
    results.append(OpsResult(True, f"Kubernetes install mode: {state.label()}", str(marker)))
    return results


def _refuse_k8s_dev_without_checkout() -> OpsResult:
    """The failure returned when `--kubernetes --dev` is used without a source checkout."""
    return OpsResult(
        False,
        "Dev mode needs a source checkout; cannot install --kubernetes --dev",
        "`--dev` builds the api and web images from a checkout's working tree, and this "
        f"nyxgpt is running from an installed package ({REPO_ROOT} has no "
        "pyproject.toml/src/nyxgpt/web). Run it from a clone, or drop `--dev` to build "
        "the images from the published artifacts.",
    )


def _install_kubernetes_steps(
    api_key: str | None, *, skip_observability: bool = False, dev: bool = False
) -> list[OpsResult]:
    """Run the Kubernetes bring-up steps and return structured results (no printing).

    Prereq checks (cluster reachable, kubectl present), builds and loads
    `nyxgpt-api:local` and `nyxgpt-web:local`, bootstraps the deployment's
    secret.yaml (prompting for the API key, never committing it), applies the
    kustomization (which now includes the web stable/canary pair -- #3419),
    records the install mode, brings up the in-cluster observability layer
    (#3787), and snapshots Pod/Service health. Stops at the first failing
    step, same rationale as `_install_terraform_steps`.

    `skip_observability` mirrors the native/Compose install's
    `--skip-observability`: the same flag now means the same thing in
    Kubernetes mode instead of being silently ignored there.

    `dev` builds both images from the current checkout instead of from the
    published artifacts (#3834). It is checkout-only, so it is refused up
    front on an installed package rather than failing at the build; without it
    the artifact path runs, which needs no checkout at all.

    Shared by the `nyxgpt ops install --kubernetes --local` CLI entrypoint
    (`_install_kubernetes`) and `install_kubernetes_local`, the SRE/admin
    dashboard API's structured equivalent.
    """
    if dev and _dev_checkout_root() is None:
        refusal = _refuse_k8s_dev_without_checkout()
        _record_ops_action("install", "kubernetes", "refused", refusal.message)
        return [refusal]

    collision = _refuse_port_collision(["api", "web"])
    if collision is not None:
        _record_ops_action("install", "kubernetes", "refused", collision.message)
        return [collision]

    logger.info(
        "ops: install --kubernetes --local starting (mode=%s)",
        INSTALL_MODE_DEV if dev else INSTALL_MODE_ARTIFACT,
        extra={
            "component": "ops",
            "action": "install-kubernetes",
            "install_mode": INSTALL_MODE_DEV if dev else INSTALL_MODE_ARTIFACT,
        },
    )
    results: list[OpsResult] = []
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        ("clear intentional-stop markers", lambda: _clear_intentional_stops(["api", "web"])),
        # Must run first, and unconditionally (#3834): the manifests
        # `kubectl apply -k` applies are packaged resources synced to
        # `K8S_DIR`, so on a machine with no checkout there is nothing to
        # apply until this has run. It also puts the Grafana provisioning
        # under ~/.nyxGPT/docker for `_apply_k8s_grafana_provisioning` --
        # which is why it used to run here only when observability was on.
        ("sync packaged resources", _sync_packaged_resources),
        ("cluster prerequisites", _ensure_kubectl_and_cluster),
        # BEFORE THE IMAGE BUILDS, not merely before the first apply (#3825).
        # The preflight needs a node to measure, so it cannot precede the step
        # above; it needs nothing at all from the two builds below, and those
        # are the expensive half of this command -- a from-scratch
        # `nyxgpt ops install --kubernetes --local` spends ~20 minutes there.
        # Sitting behind them meant an operator whose VM cannot hold the stack
        # paid for two container images and a provisioned cluster before being
        # told so, which is the "refuse before provisioning" this issue asked
        # for in name only. Now the refusal costs seconds and leaves no built
        # image behind.
        #
        # The secret bootstrap moves up with it, because the render the
        # preflight does resolves the Secret both kustomizations reference.
        # Moving it earlier is independently better: when it has no `--api-key`
        # to use it PROMPTS, and a prompt is worth more before a 20-minute
        # build than after one.
        ("secret bootstrap", lambda: _ensure_k8s_secret(api_key)),
        (
            "node capacity preflight",
            lambda: _preflight_k8s_capacity(skip_observability=skip_observability),
        ),
        ("build/load api image", lambda: _build_and_load_k8s_api_image(dev=dev)),
        ("build/load web image", lambda: _build_and_load_k8s_web_image(dev=dev)),
        ("apply kustomization", _kubectl_apply_kustomization),
        ("record install mode", lambda: _record_k8s_install_mode(dev)),
        ("wait for data/LLM tier", _wait_for_k8s_data_tier),
        # The api/web Pods depend on the tier above for their readiness
        # probes, so they are waited on after it -- and they ARE waited on
        # (#3827): without this the health snapshot below described a
        # rollout a few seconds old rather than the stack the operator was
        # about to be handed.
        ("wait for app tier", _wait_for_k8s_app_tier),
        # After the app tier is up, so the reset acts on a settled cluster
        # rather than racing the rollout it would be reading (#3991).
        ("canary resting state", _reconcile_k8s_canary_resting),
        # The step that makes the install's promise true (#3986): a completed
        # install leaves the web UI reachable from the browser, with no
        # follow-up command. Before the observability layer, because this is
        # what the operator is waiting for and `--skip-observability` must not
        # be able to skip it.
        ("host access", _ensure_k8s_host_access),
    ]
    if not skip_observability:
        # After the app tier: Prometheus's scrape target and promtail's
        # discovery both point at Pods the kustomization above creates.
        steps.append(("observability layer", _apply_k8s_observability))
        # ...and wait for it, before `_k8s_stack_health` below reads Pod
        # phases: those Pods are in the same namespace, and a Pod still
        # pulling its image is `Pending`, which that snapshot scores as a
        # failure (#3826).
        steps.append(("wait for observability layer", _wait_for_k8s_observability))
        # After that wait, because it talks to GlitchTip's REST API and needs
        # the Pod actually serving (#3990). This is the step the Kubernetes
        # install never had: without it the api reports no errors at all and
        # Grafana authenticates to GlitchTip with the manifest's placeholder
        # token, which is what put `401 Unauthorized` on the SRE Home panels.
        steps.append(("glitchtip provisioning", _k8s_provision_glitchtip))
    for step_name, fn in steps:
        try:
            step_results = fn()
        except Exception as e:
            results.append(
                OpsResult(False, f"kubernetes {step_name} raised", f"{type(e).__name__}: {e}")
            )
            step_results = []
        results += step_results
        if step_results and not all(r.ok for r in step_results):
            break
    else:
        results += _k8s_stack_health()
        if not skip_observability:
            results += _k8s_observability_health()

    result, message = _ops_action_outcome(results)
    _record_ops_action("install", "kubernetes", result, message)
    return results


def install_kubernetes_local(
    api_key: str | None = None, *, skip_observability: bool = False
) -> list[OpsResult]:
    """Structured (non-printing) Kubernetes local bring-up, for the SRE/admin dashboard API.

    Runs the same steps as `nyxgpt ops install --kubernetes --local` --
    locality is implicitly "local" here since that's the only target this
    endpoint offers (see `_resolve_locality`) -- and returns the OpsResult
    list directly instead of routing it through `_emit_results`, so a
    FastAPI endpoint can translate it straight to JSON.

    Always the artifact path: dev mode is a checkout-only choice an operator
    makes at a terminal in the checkout they want to run (#3834), and the api
    process serving this call has no business assuming one is there.
    """
    return _install_kubernetes_steps(api_key, skip_observability=skip_observability)


def observability_kubernetes() -> list[OpsResult]:
    """Structured (non-printing) in-cluster observability bring-up (#3787).

    The Kubernetes counterpart of `reconcile_observability(True)`: applies
    `k8s/observability/` on its own, without touching the app tier, for the
    SRE/admin dashboard's observability controls and for
    `nyxgpt ops observability --kubernetes --local`.
    """
    results = _ensure_kubectl_and_cluster()
    if not all(r.ok for r in results):
        return results
    # Before the preflight, not after it (#3834): the manifests the preflight
    # renders are packaged resources that only exist under `K8S_DIR` once this
    # has run, so on a machine with no checkout the preflight would find
    # nothing to render and skip itself -- silently reporting "skipped" for a
    # node it could in fact have measured.
    results += _sync_packaged_resources()
    if not all(r.ok for r in results):
        return results
    # The layer this adds is what tipped an 8Gi node over in #3825, and it is
    # added here to a cluster that is usually already running the app tier --
    # so the same preflight applies, measuring both tiers together.
    results += _preflight_k8s_capacity()
    if not all(r.ok for r in results):
        return results
    results += _apply_k8s_observability()
    if not all(r.ok for r in results):
        return results
    # Same reason as the install path (#3826): reporting readiness from a
    # snapshot taken seconds after `kubectl apply` reports whatever the image
    # pulls happen to have finished, not whether the layer works.
    results += _wait_for_k8s_observability()
    if all(r.ok for r in results):
        # The layer is not wired until GlitchTip has been provisioned (#3990)
        # -- deploying it without this leaves Grafana holding the placeholder
        # token and the api with no DSN, which is a tier that runs and
        # observes nothing.
        results += _k8s_provision_glitchtip()
        results += _k8s_observability_health()
    return results


def _install_kubernetes(args) -> int:
    """`nyxgpt ops install --kubernetes --local`: the full k8s bring-up in one command.

    `--dev` means the same thing here as it does natively (#3834): build from
    the checkout's working tree. Without it the images come from the published
    artifacts, so this command needs no checkout at all.
    """
    if _resolve_locality(args) is None:
        return 2
    results = _install_kubernetes_steps(
        getattr(args, "api_key", None),
        skip_observability=bool(getattr(args, "skip_observability", False)),
        dev=bool(getattr(args, "dev", False)),
    )
    ok = _emit_results("install --kubernetes", results)
    return 0 if ok else 2


def _down_kubernetes_steps() -> list[OpsResult]:
    """Remove the `nyxgpt` namespace's Kubernetes resources and return structured results.

    Also tears down the local `kind` cluster (#3596) *iff* kubectl's current context is
    the `nyxgpt-local` cluster nyxgpt reserves for itself (see `_ensure_kubectl_and_cluster`)
    -- a bring-your-own cluster (minikube, Docker Desktop, an operator's own differently
    named kind cluster) is never destroyed, only the deployment inside it is. This is what
    keeps "never destroys a cluster nyxgpt did not create" true without needing a separate
    flag or state file: the reserved name is the only signal, and it's authoritative by
    construction -- `_ensure_kubectl_and_cluster` is the only code path that ever creates a
    cluster by that name.

    Puts `~/.nyxGPT/bin` on PATH first so a `kubectl`/`kind` that a previous
    `install --kubernetes --local` downloaded there (#3724) is still found by
    the teardown that has to undo it, even from a shell that never had that
    directory on its own PATH.
    """
    _ensure_nyxgpt_bin_on_path()
    # First, and unconditionally: `down` must release whatever access path the
    # install established (#3986). A supervised background forward outlives
    # the cluster it points at -- it would sit there restarting a `kubectl
    # port-forward` against a deleted namespace, holding host ports 3000/8000
    # against the next install. Nothing here needs kubectl, so it runs even on
    # the paths below that have none.
    results = stop_port_forward()
    if _which("kubectl") is None:
        results += [OpsResult(False, "kubectl not found on PATH -- nothing to tear down")]
    elif not (K8S_DIR / "secret.yaml").exists():
        # The app-tier kustomization *references* secret.yaml, so `kubectl
        # delete -k k8s/` on a cluster that never had an app tier fails on
        # the missing FILE, before it ever reaches the cluster. That is now a
        # reachable state: `nyxgpt ops observability --kubernetes --local`
        # deploys the observability layer on its own (#3787), and its
        # teardown must not be blocked by an app tier that was never
        # installed.
        results += [OpsResult(True, "No app tier bootstrapped -- skipped kubectl delete -k k8s/")]
    else:
        cp = _run(["kubectl", "delete", "-k", str(K8S_DIR), "--ignore-not-found"], check=False)
        if cp.returncode == 0:
            results += [
                OpsResult(
                    True, "kubectl delete -k k8s/ (namespace and all resources)", _cp_details(cp)
                )
            ]
        else:
            results += [OpsResult(False, "kubectl delete -k k8s/ failed", _cp_details(cp))]

    if results[-1].ok and _which("kubectl") is not None:
        # After the base delete, not before: the base kustomization owns the
        # namespace, so deleting it already cascades the observability layer
        # away and this call then finds nothing (`--ignore-not-found`), which
        # is a success. When only the observability layer was ever deployed,
        # this is the delete that does the work.
        results += _delete_k8s_observability()

    if results[-1].ok and _kubectl_context() == KIND_CONTEXT and _which("kind") is not None:
        results += _delete_kind_cluster()

    if all(r.ok for r in results):
        # The marker describes a deployment that no longer exists (#3834).
        # Leaving it behind is how `ops status` ends up reporting an install
        # mode for something that was torn down two modes ago -- the exact
        # stale-record failure this issue was filed for, one substrate over.
        marker = clear_install_mode(substrate=SUBSTRATE_KUBERNETES)
        results.append(OpsResult(True, "Cleared the Kubernetes install-mode record", str(marker)))

    result, message = _ops_action_outcome(results)
    _record_ops_action("down", "kubernetes", result, message)
    return results


def down_kubernetes() -> list[OpsResult]:
    """Structured (non-printing) `kubectl delete -k k8s/`, for the SRE/admin dashboard API."""
    return _down_kubernetes_steps()


def _down_kubernetes(_args) -> int:
    """`nyxgpt ops down --kubernetes`: remove the `nyxgpt` namespace's Kubernetes resources."""
    results = _down_kubernetes_steps()
    ok = _emit_results("down --kubernetes", results)
    return 0 if ok else 2


# `nyxgpt ops port-forward --target X`: target -> (Service, local port, Service port).
#
# The local ports are NOT arbitrary: each one is the port that mode's UI is
# published on everywhere else (Compose's `GRAFANA_UI_PORT` 3001, Jaeger
# 16686, GlitchTip 8080, Prometheus 9090). Matching them is what makes the
# admin dashboard's observability links -- built from `[monitoring]
# grafana_ui_url` and friends, which default to those same localhost ports --
# resolve in Kubernetes mode without a second, mode-specific configuration
# (#3787). kubectl binds these to 127.0.0.1 only, per #3195.
K8S_PORT_FORWARD_TARGETS: dict[str, tuple[str, int, int]] = {
    "web": ("nyxgpt-web", 3000, 3000),
    "api": ("nyxgpt-api", 8000, 8000),
    "grafana": ("grafana", 3001, 3000),
    "prometheus": ("prometheus", 9090, 9090),
    "jaeger": ("jaeger", 16686, 16686),
    "glitchtip": ("glitchtip", 8080, 8080),
}

# What `--target observability` expands to: every observability UI at once,
# so one command makes the whole SRE surface reachable in Kubernetes mode.
K8S_OBSERVABILITY_PORT_FORWARD_TARGETS = ("grafana", "prometheus", "jaeger", "glitchtip")

# ...and what `--target app` expands to (#3986). docs/kubernetes.md used to
# tell operators to "forward both at once" and then show a command whose
# default target forwards only `web`; this is the target that combination
# actually needs, in the shape `observability` already had. It is also what
# the install starts in the background on a cluster whose host ports nyxGPT
# cannot map (see `_ensure_k8s_host_access`).
K8S_APP_PORT_FORWARD_TARGETS = ("web", "api")

# Every `--target` value, including the two that expand to several forwards.
# Single source for the CLI's `choices` and for `_port_forward_plan`, so the
# two cannot disagree about what is accepted.
K8S_PORT_FORWARD_TARGET_NAMES: tuple[str, ...] = (
    *K8S_PORT_FORWARD_TARGETS,
    "app",
    "observability",
)

# The managed background forward's pid and plan, so `--status`/`--stop` (and
# `ops down --kubernetes`) can find a forward started by an earlier process
# -- the same treatment `nyxgpt cloud tunnel --background` gives its SSH
# tunnel (`cloud_deploy.TUNNEL_STATE_FILE`), which is the precedent #3986
# names.
K8S_PORT_FORWARD_STATE_FILE = NYXGPT_HOME / "k8s" / "port-forward.json"

# Where the detached supervisor's own output goes. A background child
# outlives the CLI process that started it, so its stderr cannot stay on a
# pipe nobody will read.
K8S_PORT_FORWARD_LOG_FILE = NYXGPT_HOME / "k8s" / "port-forward.log"

# How long the supervisor waits before restarting a forward that exited.
# `kubectl port-forward` dies when the Pod it attached to is replaced
# (docs/kubernetes.md), which is exactly the case this supervision exists to
# survive -- a canary rollout or a self-heal restart must not silently take
# the UI down. Short enough that the gap is not noticeable, long enough that
# a genuinely unsatisfiable forward (port already bound) does not spin.
K8S_PORT_FORWARD_RESTART_DELAY_S = 2.0


def _port_forward_plan(args) -> list[tuple[str, str, int, int]] | None:
    """Resolve `port-forward`'s args into (target, service, local, remote) rows.

    `--port` overrides the local port, but only for a single target -- with
    `--target observability` there are four of them and one override cannot
    mean anything sensible, so it is rejected rather than silently ignored.
    Returns None (after printing why) on a bad combination.
    """
    target = getattr(args, "target", "web") or "web"
    port_override = getattr(args, "port", None)

    if target in ("observability", "app"):
        if port_override is not None:
            print(
                f"ERROR: --port cannot be combined with --target {target} "
                "(it forwards several Services; pass a single --target to override one port)",
                file=sys.stderr,
            )
            return None
        names = list(
            K8S_APP_PORT_FORWARD_TARGETS
            if target == "app"
            else K8S_OBSERVABILITY_PORT_FORWARD_TARGETS
        )
    elif target in K8S_PORT_FORWARD_TARGETS:
        names = [target]
    else:
        known = ", ".join(sorted(K8S_PORT_FORWARD_TARGET_NAMES))
        print(f"ERROR: unknown --target {target!r} (known targets: {known})", file=sys.stderr)
        return None

    plan = []
    for name in names:
        service, local_port, remote_port = K8S_PORT_FORWARD_TARGETS[name]
        if port_override is not None:
            local_port = port_override
        plan.append((name, service, local_port, remote_port))
    return plan


@dataclass
class _PortForwardArgs:
    """The two fields `_port_forward_plan` reads, for in-process callers.

    `port_forward` is an argparse entrypoint; the install and the background
    starter need the same plan without inventing a Namespace.
    """

    target: str = "web"
    port: int | None = None


def _port_forward_argv(service: str, local_port: int, remote_port: int) -> list[str]:
    """The `kubectl port-forward` invocation for one Service, bound to loopback (#3195)."""
    return [
        "kubectl",
        "-n",
        K8S_NAMESPACE,
        "port-forward",
        f"svc/{service}",
        f"{local_port}:{remote_port}",
    ]


def _process_alive(pid: int) -> bool:
    """True if `pid` names a live process this user can signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def port_forward_status() -> dict[str, Any]:
    """Report the managed background forward: whether it runs, and what it publishes.

    Self-healing in the same way `cloud_deploy.tunnel_status` is: a recorded
    pid that is no longer alive (a reboot, an external `kill`) reads as "not
    running" rather than as a forward that is still there.
    """
    record: dict[str, Any] = {}
    if K8S_PORT_FORWARD_STATE_FILE.exists():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            loaded = json.loads(K8S_PORT_FORWARD_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                record = loaded
    pid = int(record.get("pid") or 0)
    running = _process_alive(pid)
    return {
        "running": running,
        "pid": pid if running else 0,
        "targets": list(record.get("targets") or []),
        "urls": list(record.get("urls") or []),
    }


def stop_port_forward() -> list[OpsResult]:
    """Stop the managed background forward, if one is running.

    Signals the whole process group: the supervisor is detached with
    `start_new_session=True`, so its `kubectl` children share its group and a
    group signal is what stops them together. A `kubectl` left behind would
    keep the local port bound and make the next `--background` fail for a
    reason that has nothing to do with the cluster.
    """
    status = port_forward_status()
    if not status["running"]:
        K8S_PORT_FORWARD_STATE_FILE.unlink(missing_ok=True)
        return [OpsResult(True, "No managed background port-forward is running")]
    pid = int(status["pid"])
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        # Racing an external kill, or a supervisor that is no longer its own
        # group leader: fall back to the process itself.
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    K8S_PORT_FORWARD_STATE_FILE.unlink(missing_ok=True)
    return [
        OpsResult(
            True,
            f"Stopped the managed background port-forward (pid {pid})",
            ", ".join(status["targets"]),
        )
    ]


def _supervise_port_forward(plan: list[tuple[str, str, int, int]]) -> int:
    """Keep every forward in `plan` alive until this process is asked to stop (#3986).

    This is the body of the detached child `--background` starts, and the
    reason the background forward is not merely a detached `kubectl`:
    `kubectl port-forward` attaches to ONE Pod and exits when that Pod is
    replaced, so an unsupervised forward silently takes the UI down on the
    first canary rollout or self-heal restart -- the failure mode #3986
    reports against the manual workaround. Restarting it is the whole job.

    Runs until SIGTERM/SIGINT (what `--stop` sends), then terminates every
    child before returning.
    """
    stopping = threading.Event()

    def _request_stop(_signum: int, _frame: Any) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    children: dict[str, subprocess.Popen[bytes]] = {}
    try:
        while not stopping.is_set():
            for name, service, local_port, remote_port in plan:
                proc = children.get(name)
                if proc is not None and proc.poll() is None:
                    continue
                if proc is not None:
                    print(
                        f"[{name}] forward exited ({proc.returncode}) -- restarting",
                        flush=True,
                    )
                children[name] = subprocess.Popen(
                    _port_forward_argv(service, local_port, remote_port)
                )
                print(
                    f"[{name}] forwarding http://127.0.0.1:{local_port} -> "
                    f"{K8S_NAMESPACE}/svc/{service}:{remote_port}",
                    flush=True,
                )
            stopping.wait(K8S_PORT_FORWARD_RESTART_DELAY_S)
    finally:
        for proc in children.values():
            with contextlib.suppress(Exception):
                proc.terminate()
        for proc in children.values():
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
    return 0


def _supervisor_argv(target: str) -> list[str]:
    """Command line for the detached supervisor child.

    Re-enters this same CLI through the *running interpreter* rather than
    through a `nyxgpt` on PATH: the api process that may start this (and a
    venv-installed CLI whose bin directory is not exported) can both be
    relied on to have the package importable, and neither can be relied on to
    have the console script findable.
    """
    return [
        sys.executable,
        "-c",
        "import sys; from nyxgpt.cli import cli; sys.exit(cli())",
        "ops",
        "port-forward",
        "--target",
        target,
        "--supervise",
    ]


def start_port_forward_background(target: str = "app") -> list[OpsResult]:
    """Start (or report) the managed background forward for `target` (#3986).

    Modelled on `nyxgpt cloud tunnel --background`, which #3986 names as the
    existing precedent: a detached child in its own process group, its pid
    recorded so `--status`/`--stop`/`ops down --kubernetes` can find it from
    another process, and its output in a log file rather than on a pipe
    nobody will read.

    Idempotent: an already-running forward is reported, not duplicated --
    starting a second one would only fail on the bound local port.
    """
    if _which("kubectl") is None:
        return [OpsResult(False, "kubectl not found on PATH -- cannot start a port-forward")]

    existing = port_forward_status()
    if existing["running"]:
        return [
            OpsResult(
                True,
                f"Background port-forward already running (pid {existing['pid']})",
                ", ".join(existing["urls"]),
            )
        ]

    plan = _port_forward_plan(_PortForwardArgs(target=target))
    if plan is None:
        return [OpsResult(False, f"Unknown port-forward target {target!r}")]

    try:
        K8S_PORT_FORWARD_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log = open(K8S_PORT_FORWARD_LOG_FILE, "w", encoding="utf-8")  # noqa: SIM115
    except OSError as e:
        return [
            OpsResult(
                False,
                f"Could not open the port-forward log ({K8S_PORT_FORWARD_LOG_FILE})",
                f"{type(e).__name__}: {e}",
            )
        ]
    with log:
        process = subprocess.Popen(
            _supervisor_argv(target),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    urls = [f"http://127.0.0.1:{local}" for _name, _svc, local, _remote in plan]
    record = {
        "pid": process.pid,
        "target": target,
        "targets": [name for name, _svc, _local, _remote in plan],
        "urls": urls,
    }
    K8S_PORT_FORWARD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    K8S_PORT_FORWARD_STATE_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return [
        OpsResult(
            True,
            f"Background port-forward started (pid {process.pid}); it is restarted "
            "automatically when a Pod is replaced",
            ", ".join(urls),
        )
    ]


def port_forward(args) -> int:
    """`nyxgpt ops port-forward`: forward a Kubernetes Service to localhost.

    The app tier's own Services are NodePorts since #3986, and on the kind
    cluster nyxGPT provisions those are published on the host, so reaching
    the web UI needs no forward at all. This command remains the way to reach
    a cluster whose host ports nyxGPT cannot map (a bring-your-own cluster,
    or a `nyxgpt-local` created by an older nyxGPT), and the only way to
    reach the observability UIs, whose Services stay ClusterIP. It wraps
    `kubectl port-forward` so operators never type the raw command
    themselves, per CLAUDE.md's Operational Command Wrapping requirement.

    `--target` selects what to forward (default `web`, unchanged).
    `--target app` forwards web and api together -- the combination
    docs/kubernetes.md used to ask for while showing a command that forwarded
    one -- and `--target observability` forwards Grafana, Prometheus, Jaeger
    and GlitchTip at once on the ports the admin dashboard already expects.

    `--background` hands the forward to a supervised, detached child instead
    (pid recorded, `--status`/`--stop` to inspect and end it), which is what
    the install establishes on a cluster it cannot map host ports for. The
    supervision is the point: a plain `kubectl port-forward` dies with the
    Pod it attached to, so an unsupervised background forward would take the
    UI down on the first canary rollout.

    Without `--background` it runs in the foreground until interrupted
    (Ctrl-C), same as `kubectl port-forward` itself -- there's no "done"
    state to return early from.
    """
    if getattr(args, "status", False):
        status = port_forward_status()
        if status["running"]:
            print(
                f"Background port-forward running (pid {status['pid']}): "
                + ", ".join(status["urls"])
            )
        else:
            print("No managed background port-forward is running.")
        return 0
    if getattr(args, "stop", False):
        return 0 if _emit_results("port-forward --stop", stop_port_forward()) else 2

    if _which("kubectl") is None:
        print("[FAIL] kubectl not found on PATH", file=sys.stderr)
        return 2

    if getattr(args, "background", False):
        target = getattr(args, "target", "web") or "web"
        results = start_port_forward_background(target)
        return 0 if _emit_results("port-forward --background", results) else 2

    plan = _port_forward_plan(args)
    if plan is None:
        return 2

    if getattr(args, "supervise", False):
        # The detached child's own mode -- keeps every forward in the plan
        # alive across Pod replacement. Not something an operator runs
        # directly; `--background` is the surface.
        return _supervise_port_forward(plan)

    logger.info(
        "ops: port-forward starting",
        extra={
            "component": "ops",
            "action": "port-forward",
            "targets": ",".join(name for name, _svc, _local, _remote in plan),
        },
    )

    procs: list[subprocess.Popen[bytes]] = []
    try:
        for name, service, local_port, remote_port in plan:
            # flush: this runs in the foreground indefinitely, so a buffered
            # stdout (any non-TTY: a log file, a CI step) would hold these
            # lines back for the entire session.
            print(
                f"Forwarding http://127.0.0.1:{local_port} -> "
                f"{K8S_NAMESPACE}/svc/{service}:{remote_port} ({name})",
                flush=True,
            )
            procs.append(subprocess.Popen(_port_forward_argv(service, local_port, remote_port)))
        print("Ctrl-C to stop.", flush=True)
        # Any forward exiting on its own (Service deleted, port already
        # bound) ends the command: a partially-working set of tunnels is
        # worse than an obvious failure the operator can re-run.
        returncode = 0
        while procs:
            for proc in list(procs):
                try:
                    returncode = proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    continue
                procs.remove(proc)
                break
            else:
                continue
            break
    except KeyboardInterrupt:
        returncode = 0
    finally:
        for proc in procs:
            with contextlib.suppress(Exception):
                proc.terminate()
        for proc in procs:
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
    return returncode


def infra_status() -> dict[str, Any]:
    """Honest, status-only deployment status for the Infrastructure admin page (see #3410).

    Reports which mode is actually running (native/compose/terraform/
    kubernetes/none) and each mode's component state, mirroring the
    sections `nyxgpt ops status` prints (see `status`) as JSON. Distinguishes
    a probe that couldn't be run at all from this vantage point --
    `probe_available: False` -- from a probe that ran and found nothing, so
    a caller (the web UI) can render "cannot determine from this deployment
    mode" instead of a false "NOT DEPLOYED" when e.g. the API process has no
    docker socket. This page has no install/destroy actions: those are
    `nyxgpt ops` CLI-only (see docs/terraform.md / docs/kubernetes.md). Also
    reports `serving`, which instance/service is currently handling traffic
    (see `_serving_status`) -- traffic *control* stays on the canary page.

    Kubernetes refines this further (#3468): `configured` reports whether
    this process has a cluster to talk to at all -- a kubeconfig
    current-context, **or** in-cluster ServiceAccount credentials (#3988;
    kubectl missing counts as not configured either way). No cluster means
    there was never anything to be unreachable -- that's a
    confidently-determined NOT DEPLOYED, not CANNOT DETERMINE. The latter is
    reserved for a *configured* cluster the probe couldn't reach (timeout,
    connection refused to a cluster that's meant to exist, auth failure),
    preserving #3410's original false-NOT-DEPLOYED protection for that case.

    The in-cluster half of that is #3988's whole subject: served from the api
    Pod, this function used to gate on `kubectl config current-context`
    alone, which is empty in a Pod -- so the page reported the very cluster
    it was running in as NOT DEPLOYED. `in_cluster` (top level, and on the
    `kubernetes` section) says when the answer comes from inside the
    deployment being described; the Compose survey and the native install
    identity are then reported as **out of scope** rather than answered from
    the container's own filesystem.

    `compose_probe_available` extends the same "can't determine" distinction
    to the `compose` section (#3588): `False` means `docker compose ps`
    couldn't be queried from this vantage point at all, so an empty `compose`
    dict must not be read as "nothing running". It is answered by *running*
    the survey rather than by checking that a binary and a file exist
    (#3812) -- on the rc12 cloud install both existed and every call still
    exited 125 against an unreachable daemon, so the flag said "available"
    while the survey returned nothing. `compose_probe_reason` carries the
    cause to the page ("`docker compose ps` exited 125: permission denied
    ...") so the operator does not have to go looking in a log for it --
    see `self_heal.compose_probe`.

    `native_probe_available`/`native_probe_reason` are the same pair for the
    **native** card, and `terraform.probe_available` was rebuilt on the same
    rule (#4022). Native Cassandra is the one native component read out of
    Docker, and until #4022 a read this process was not allowed to make came
    back as the flat string `absent` -- so on the owner's EC2 instance, whose
    API process runs under a `systemd --user` manager whose group set predates
    `usermod -aG docker`, the card reported Cassandra gone while it was
    serving. The read now retries through `sg docker` first and, when even
    that cannot be had, says `unknown` with Docker's own words. The Terraform
    card's flag stopped being "is there a docker CLI" for the same reason
    #3812 took that shape away from Compose: on that instance the CLI existed
    and every call was denied.
    """
    mode_info = detect_deployment_mode()
    compose_probe = self_heal.compose_probe()

    docker_available = _which("docker") is not None
    tf_state = terraform_stack_state()
    # `probe_available` was "is there a docker CLI", which is the same
    # existence-check-instead-of-a-run mistake #3812 removed from
    # `compose_probe_available`: on the owner's instance the CLI existed and
    # every call was denied, so the card said "available" over reads it could
    # not make. It is now answered by whether the reads *happened* (#4022).
    tf_probe_available = docker_available and DOCKER_STATE_UNKNOWN not in tf_state.values()
    # One daemon, one denial: the reason the native Cassandra read could not be
    # made is the reason these could not either.
    tf_probe_reason = "" if tf_probe_available else mode_info.docker_probe_reason
    # The Terraform deployment's own install mode (#3835) -- never the native
    # marker, which describes a different deployment that may well be in the
    # other mode.
    tf_install_mode_state = read_install_mode(substrate=SUBSTRATE_TERRAFORM)
    # `_container_deployed`, not `!= "absent"`: an unreadable container is not
    # evidence of a deployment any more than it is evidence of an absent one.
    tf_deployed = docker_available and any(_container_deployed(s) for s in tf_state.values())
    terraform = {
        "probe_available": tf_probe_available,
        "probe_reason": tf_probe_reason,
        "deployed": tf_deployed,
        "containers": tf_state,
        "install_mode": {
            "mode": tf_install_mode_state.mode,
            "checkout": tf_install_mode_state.checkout,
            # The label is the deployment-aware one: a running stack with no
            # marker is reported as unrecorded, never as the artifact default
            # it did not earn (#3835).
            "label": tf_install_mode_state.label(deployed=tf_deployed),
            "images": tf_install_mode_state.images,
            "recorded": tf_install_mode_state.recorded,
        },
    }

    kubectl_available = _which("kubectl") is not None
    in_cluster = _in_cluster()
    kubernetes_context = _kubectl_context() if kubectl_available else ""
    if not kubernetes_context and in_cluster:
        # #3988: the page was being served BY a Pod in the cluster it called
        # NOT DEPLOYED. In-cluster auth uses the mounted ServiceAccount token
        # and `KUBERNETES_SERVICE_HOST`, never a kubeconfig context, so the
        # old `bool(current-context)` gate was structurally incapable of
        # seeing the deployment it was running in.
        kubernetes_context = K8S_IN_CLUSTER_CONTEXT_LABEL
    kubernetes_configured = bool(kubernetes_context)
    pods: list[str] = []
    pod_states: list[dict[str, str]] = []
    unschedulable: list[str] = []
    # No kubeconfig context AND no in-cluster credentials means no cluster was
    # ever configured here -- that's a confidently-determined NOT DEPLOYED
    # (#3468), not the CANNOT DETERMINE state reserved for a *configured*
    # cluster the probe couldn't reach (kubectl missing entirely is folded
    # into "not configured" too, since there's nothing to talk to it with).
    kubernetes_probe_available = not kubernetes_configured
    if kubernetes_configured:
        # Classified rather than dumped (#3827): the raw `kubectl get pods`
        # line says `Pending` for a Pod pulling an image and for one the node
        # cannot fit, and an operator reading this page cannot tell which is
        # which -- the same conflation the install used to print. One read
        # answers both `pods` (the display lines) and `pod_states` (the
        # states the page badges each line with).
        states, read_failure = _k8s_pod_states(expected=True)
        kubernetes_probe_available = read_failure is None
        if kubernetes_probe_available:
            pods = [f"{s.name}   {s.summary}" for s in states]
            pod_states = [
                {"name": s.name, "state": s.state, "summary": s.summary, "details": s.details}
                for s in states
            ]
            # Pods the scheduler could not place (#3825), named separately so
            # the page can print the remedy -- a badge says the Pod will not
            # start, but not that the cure is a bigger cluster VM. Derived from
            # the same classification as the badges above rather than from a
            # second `.spec.nodeName` probe (#3827): two independent notions of
            # "unschedulable" on one screen is how a page ends up contradicting
            # itself, which is the defect this issue exists to remove. A Pod
            # that has simply not been placed *yet* is PENDING, and is not
            # named here.
            unschedulable = [s.name for s in states if s.summary == K8S_SUMMARY_UNSCHEDULABLE]
    # The in-cluster observability layer (#3787), reported per workload so the
    # Infrastructure page can say *which* piece is missing rather than just
    # "observability: no". Only probed when the cluster answered at all --
    # otherwise this would report a confident "absent" for a cluster nobody
    # could reach, exactly the false-negative #3468 removed elsewhere.
    observability_workloads: dict[str, str] = {}
    if kubernetes_configured and kubernetes_probe_available:
        observability_workloads = _k8s_observability_workload_state()
    k8s_install_mode = read_install_mode(substrate=SUBSTRATE_KUBERNETES)
    kubernetes = {
        "available": kubectl_available,
        "configured": kubernetes_configured,
        "probe_available": kubernetes_probe_available,
        "deployed": bool(pods),
        "namespace": K8S_NAMESPACE,
        "pods": pods,
        # Per-Pod ready/pending/failed, so the page can badge a Pod that is
        # still starting differently from one that will never start, and show
        # the scheduler's reason for the latter (#3827).
        "pod_states": pod_states,
        # Names only: the remedy is a CLI one (give the cluster VM more
        # memory/CPU and re-run `nyxgpt ops install --kubernetes --local`,
        # which refuses up front rather than repeating this), so the page
        # reports the state and names the command (#3825).
        "unschedulable": unschedulable,
        # (#3596) which cluster is configured, and whether it's the local `kind`
        # cluster nyxgpt provisions when nothing else is reachable, vs. a
        # bring-your-own cluster (minikube, Docker Desktop, a remote context, ...).
        "context": kubernetes_context,
        # False in-cluster: a Pod cannot see which kind cluster (if any) its
        # nodes belong to, and guessing would be the same class of confident
        # wrong answer this issue removed.
        "provisioned": kubernetes_context == KIND_CONTEXT,
        # Whether this answer comes from inside the cluster being described
        # (#3988). The page uses it to say so, and to scope the rows below
        # that a Pod cannot honestly answer.
        "in_cluster": in_cluster,
        # What the two images in this cluster were built from (#3834): the
        # published artifacts, or a checkout's working tree via
        # `--dev`. `recorded: False` means no marker -- deployed before nyxGPT
        # recorded one, or from another machine -- which the page must show as
        # unknown rather than as the artifact default, since here that default
        # would be a guess about someone else's deployment.
        "install_mode": {
            "mode": k8s_install_mode.mode,
            "checkout": k8s_install_mode.checkout,
            "label": k8s_install_mode.label(),
            "recorded": k8s_install_mode.recorded,
        },
        "observability": {
            "probe_available": kubernetes_probe_available,
            "deployed": any(state != "absent" for state in observability_workloads.values()),
            "workloads": observability_workloads,
            # The same three-state classification `pod_states` carries, for
            # the same reason (#3827): the raw `workloads` map is `"0/1
            # ready"`-style prose, which the card could only render as
            # undifferentiated grey -- a healthy workload, one still rolling
            # out and one that never deployed all looked alike, on the same
            # card that badges every Pod READY/PENDING/FAILED.
            "workload_states": [
                {
                    "name": s.name,
                    "state": s.state,
                    "summary": s.summary,
                    "details": s.details,
                }
                for s in (
                    _classify_k8s_observability_workload(name, value)
                    for name, value in observability_workloads.items()
                )
            ],
            # How the operator reaches the UIs above from their own machine
            # -- a `nyxgpt` command, never a raw kubectl invocation.
            "port_forward_command": "nyxgpt ops port-forward --target observability",
        },
    }

    native_running = any(state in ("started", "running") for state in mode_info.native.values())
    # Compose mode means a *core* component is Compose-managed, not merely
    # that something Compose-sourced is up (#3855) -- see
    # `compose_core_components` for why the whole-snapshot truthiness test
    # this replaces labelled every native install "Docker Compose", ahead of
    # `native_running` ever being evaluated.
    compose_core = compose_core_components(mode_info)
    if terraform["deployed"]:
        running_mode = "terraform"
    elif kubernetes["deployed"]:
        running_mode = "kubernetes"
    elif compose_core:
        running_mode = "compose"
    elif native_running:
        running_mode = "native"
    else:
        running_mode = "none"

    # Which *install* mode the native api/web are on -- artifact (published/
    # vendored builds) or dev (this checkout's working tree, #3789). Reported
    # alongside the deployment mode for the same reason `ops status` prints
    # it: a dashboard showing a healthy native stack must not let a dev-mode
    # install be read as a verdict on the artifact path.
    install_mode_state = read_install_mode()
    install_mode: dict[str, Any] = {
        "mode": install_mode_state.mode,
        "checkout": install_mode_state.checkout,
        "label": install_mode_state.label(),
        "components": sorted(DEV_LAUNCHD_LABELS),
        # Which build, not merely which mode (#3861). `known: false` is the
        # honest answer for a machine whose marker predates identities -- the
        # page says so rather than presenting the mode as if it identified
        # the install.
        "identity": {
            "known": install_mode_state.identity.known,
            "manager": install_mode_state.identity.manager,
            "services": install_mode_state.identity.service_map,
            "version": install_mode_state.identity.version,
            "channel": install_mode_state.identity.channel,
            "detail": install_mode_state.identity.detail(),
        },
    }

    compose_probe_available = compose_probe.available
    compose_probe_reason = compose_probe.reason
    if in_cluster:
        # A Compose survey run from inside a Pod is not a question with an
        # answer (#3988). The container has no host filesystem and no Docker
        # socket, so the probe was reporting its own `/root/.nyxGPT/...` path
        # to the operator as the reason -- a container-internal path, about a
        # machine the page cannot see. Replaced by a scope statement: this
        # vantage point does not cover Compose at all.
        compose_probe_available = False
        compose_probe_reason = (
            "Not in scope from here: this API is running inside a Kubernetes Pod, which has "
            "no host filesystem and no Docker socket. Run `nyxgpt ops status` on the host to "
            "survey a Docker Compose deployment there."
        )
        # Same for the native install identity: whatever marker the container
        # image happens to carry describes a different machine, and the
        # remedies the card offers (`nyxgpt up`, `nyxgpt ops doctor`) are
        # aimed at a host this process cannot reach.
        install_mode["in_scope"] = False
        install_mode["out_of_scope_reason"] = (
            "Not in scope from here: this API is running inside a Kubernetes Pod. The "
            "Kubernetes card above describes this deployment; a native install is a "
            "separate one, on a host this process cannot see."
        )
    else:
        install_mode["in_scope"] = True
        install_mode["out_of_scope_reason"] = ""

    return {
        "mode": running_mode,
        # Where this answer was computed (#3988). `in_cluster` means the page
        # is describing the deployment it is itself being served from, which
        # is what makes the Compose/native rows above out of scope rather
        # than merely unlucky.
        "in_cluster": in_cluster,
        "install_mode": install_mode,
        "native": mode_info.native,
        # The native card's own "can't determine" signal (#4022). Cassandra is
        # the one native component read out of Docker, so a denied socket
        # leaves exactly that entry unreadable; the card renders it as
        # unreadable rather than as `absent`, with the cause beside it.
        "native_probe_available": mode_info.native.get("cassandra") != DOCKER_STATE_UNKNOWN,
        "native_probe_reason": mode_info.docker_probe_reason,
        "compose": mode_info.compose,
        "compose_probe_available": compose_probe_available,
        "compose_probe_reason": compose_probe_reason,
        "conflicts": sorted(mode_info.conflicts),
        "terraform": terraform,
        "kubernetes": kubernetes,
        "serving": _serving_status(running_mode),
    }


def _serving_status(running_mode: str) -> dict[str, Any]:
    """Which instance/service is currently serving traffic (#3410's third duty for this page).

    Traffic splitting only exists in Kubernetes mode -- native/Compose/
    Terraform each run exactly one instance of every component, so it
    serves 100% of traffic by construction. Only Kubernetes mode delegates
    to `canary.status()` for the stable/canary weight and per-track health;
    everywhere else this reports the single-instance fact directly rather
    than duplicating canary's Kubernetes-only probing. Traffic *control*
    stays on the canary page (#3409) -- this only reports the current split.

    Reports every canary-capable component (api, web -- see #3419) under
    `components`, plus the `api` fields spread at the top level unchanged
    for backward compatibility with existing callers/tests that only knew
    about a single component.
    """
    if running_mode != "kubernetes":
        return {
            "supported": False,
            "message": (
                "Single instance serving 100% of traffic -- traffic splitting is a "
                "Kubernetes-mode feature (see the Canary page)."
            ),
        }

    from nyxgpt import canary as canary_module

    components: dict[str, Any] = {}
    for key in ("api", "web"):
        component_status = canary_module.status(component=key)
        components[key] = {
            "active": component_status["active"],
            "weight_percent": component_status["weight_percent"],
            "stable": component_status["stable"],
            "canary": component_status["canary"],
        }

    return {
        "supported": True,
        "components": components,
        **components["api"],
    }


def _install_config() -> list[OpsResult]:
    """Ensure ``~/.nyxGPT/config.ini`` exists, running the setup wizard if missing.

    First-run experience: on a fresh machine `nyxgpt ops install` has no
    config to install services against, so this step launches the interactive
    wizard (the same one behind `nyxgpt wizard`) to create it before any other
    step runs. When stdin is not a TTY (CI, scripted installs) the wizard
    cannot prompt, so the step fails with instructions instead of hanging.
    """
    dst = Path.home() / ".nyxGPT" / "config.ini"
    if dst.exists():
        return [OpsResult(True, "Config already exists", str(dst))]

    if not sys.stdin.isatty():
        return [
            OpsResult(
                False,
                "No config.ini found and stdin is not a TTY -- run `nyxgpt wizard` first",
                str(dst),
            )
        ]

    from nyxgpt.wizard import run_wizard

    print("\nNo config.ini found. Launching setup wizard...\n")
    if run_wizard(output_path=dst) != 0:
        return [OpsResult(False, "Wizard did not complete", str(dst))]
    return [OpsResult(True, "Created config.ini via wizard", str(dst))]


def install(args) -> int:
    """CLI entrypoint for `nyxgpt ops install`.

    Reconciles the local machine to the intended native-mode topology (see
    docs/ops.md): first ensuring ~/.nyxGPT/config.ini exists (launching the
    interactive setup wizard on a fresh machine -- see `_install_config`),
    then migrating any pre-#3346 named-volume data into
    ~/.nyxGPT/volumes/ (see `migrate_legacy_volumes`), then stopping any
    phantom Docker Compose app-tier containers leaked from an earlier run or
    a raw `docker compose up`, then ensuring the
    local Cassandra container plus every other install step (scripts, web deps,
    MCP deps, Cassandra LaunchAgent, Ollama logs LaunchAgent, Ollama env
    LaunchAgent, Homebrew formulas, the native Ollama service (including
    pointing it at the shared model store -- see `_ensure_ollama_service`,
    #3431), log symlinks, env sync from config.ini, the observability stack)
    -- printing an OK/FAIL line per result. A failure in one step doesn't
    stop the rest from running.

    The observability step (Grafana/Loki/Jaeger/GlitchTip) runs by default so
    a fresh install comes up with the full SRE view already populated --
    pass `--skip-observability` to opt out (e.g. on a host with no Docker,
    or to keep those Compose profiles stopped for resource reasons).

    `--dev` installs the api/web services from the current checkout instead
    of from artifacts (see `nyxgpt.install_mode` and `_install_native_api_dev`
    /`_install_native_web_dev`, #3789): same topology, same steps, no keg or
    tarball build. It is checkout-only and returns 2 immediately when run
    from an installed package; without it the artifact path is unchanged and
    remains the default.

    Returns 0 if every step succeeded, else 2.
    """
    if getattr(args, "terraform", False) and getattr(args, "kubernetes", False):
        print("ERROR: --terraform and --kubernetes are mutually exclusive", file=sys.stderr)
        return 2
    if getattr(args, "terraform", False):
        return _install_terraform(args)
    if getattr(args, "kubernetes", False):
        return _install_kubernetes(args)

    dev = bool(getattr(args, "dev", False))
    if dev and _dev_checkout_root() is None:
        # Checkout-only by definition -- say so up front rather than
        # reconciling half a stack and failing at the api step (#3789).
        print(
            "ERROR: --dev needs a source checkout, and this nyxgpt is running from an "
            f"installed package ({REPO_ROOT} has no pyproject.toml/src/nyxgpt/web).\n"
            "       Run `nyxgpt up --dev` from a clone of the repository, or drop --dev "
            "to install the published artifacts.",
            file=sys.stderr,
        )
        return 2

    logger.info(
        "ops: install starting (mode=%s)",
        INSTALL_MODE_DEV if dev else INSTALL_MODE_ARTIFACT,
        extra={
            "component": "ops",
            "action": "install",
            "install_mode": INSTALL_MODE_DEV if dev else INSTALL_MODE_ARTIFACT,
        },
    )

    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        # Must run first: every step below that reads a Compose file, config
        # template, launchd/systemd unit template, or helper script assumes
        # the packaged copies are already synced to NYXGPT_HOME (#3621).
        ("sync packaged ops resources", _sync_packaged_resources),
        (
            "clear intentional-stop markers",
            lambda: _clear_intentional_stops(["api", "web", "ollama", "cassandra"]),
        ),
        ("config", _install_config),
        # Homebrew has no uninstall hook, so a keg removed without `nyxgpt ops
        # uninstall` first leaves its service loaded and pointing at deleted
        # files. Install is that condition reached from the other direction:
        # report it before anything below tries to bind :8000/:3000 against
        # it (#3859, #3853).
        ("orphaned launchd jobs", _report_orphaned_launchd_jobs),
        # Must run before the api/web steps: it records the install identity
        # they (and every later restart/stop/status) act in, and retires
        # every api/web service that identity does not own -- the previous
        # mode's, the previous formula's, or anything registered that no
        # marker recorded (#3789, #3861).
        ("install mode", lambda: _reconcile_install_mode(dev)),
        # Must run before the api/web steps and after the identity is
        # recorded: it frees :8000/:3000 from a *different formula's* service
        # for the same component -- the leftover `nyxgpt-api` keg a candidate
        # install has no reason to touch and that launchd keeps restarting
        # (#3853). It overlaps `install mode` since #3861 rather than
        # covering a case that step cannot see; what it adds is a second stop
        # attempt sited immediately before the install, reported per
        # component with the variants brew lists.
        ("superseded brew services", _stop_superseded_brew_services),
        # Everything container-backed below (Cassandra, the observability
        # stack) needs a working engine, and requiring the operator to
        # install Docker by hand first was itself an acceptance failure
        # (#3632) -- so reconcile it here rather than assuming it.
        ("docker engine", _ensure_docker_engine),
        ("migrate legacy volumes", migrate_legacy_volumes),
        ("phantom compose reconciliation", _reconcile_phantom_compose_app_containers),
        ("web deps", _ensure_web_deps),
        ("mcp deps", _ensure_mcp_deps),
        ("cassandra container", _ensure_cassandra_container),
        ("cassandra log follower service", _install_cassandra_log_follower_service),
        ("ollama logs follower service", _install_ollama_log_follower_service),
        ("ollama env agent", _install_ollama_env_agent),
        ("native api service", lambda: _install_native_api(dev=dev)),
        ("native web service", lambda: _install_native_web(dev=dev)),
        ("ollama service", _ensure_native_ollama_service),
        # Must run after the ollama service step: it pulls into the server
        # that step just started. Without it the install reported every
        # component healthy on a machine with no chat model, and the user's
        # first message failed (#3824).
        ("required models", _ensure_required_models),
        ("stale log symlink cleanup", _cleanup_stale_log_symlinks),
        ("env sync", sync_env_from_config),
        ("compose config (derive from native)", _generate_compose_config),
    ]
    if not getattr(args, "skip_observability", False):
        # The bind-mount ownership reconcile (#3632) used to be its own step
        # here. It now runs inside `_reconcile_grafana_provisioning` -- the
        # "observability stack" step below -- because `install` was never the
        # only path that starts the stack: `nyxgpt ops observability` and the
        # dashboard's observability toggle both bypass this list, and so came
        # up on root-owned volumes (#3721). Its per-directory OK/FAIL lines
        # still print, under that step.
        steps.append(("glitchtip secrets dir", _ensure_glitchtip_secrets_dir))
        steps.append(("slack webhook secret", _sync_grafana_slack_webhook_secret))
        steps.append(("observability stack", _reconcile_grafana_provisioning))
        steps.append(("glitchtip auto-provisioning", _provision_glitchtip))

    quiet = bool(getattr(args, "quiet", False))
    results, slow_steps = _run_steps("install", steps, quiet=quiet)
    ok = all(r.ok for r in results)
    if not quiet:
        _print_slow_steps_summary(slow_steps)
    logger.info(
        "ops: install %s (%d/%d steps ok)",
        "succeeded" if ok else "failed",
        sum(1 for r in results if r.ok),
        len(results),
        extra={"component": "ops", "action": "install", "ok": ok, "steps": len(results)},
    )
    result, message = _ops_action_outcome(results)
    _record_ops_action("install", "all", result, message)

    return 0 if ok else 2


def _pending_components(excluded: set[str]) -> list[str]:
    """Desired components not yet reporting healthy, minus `excluded`.

    Shared by the health-wait's poll and its timeout message so the two can't
    disagree about what "pending" means -- the timeout used to name nothing at
    all, which cost a full CI cycle to work out (#3508).
    """
    return sorted(
        s.service
        for s in self_heal.list_component_status()
        if s.desired and not s.healthy and s.service not in excluded
    )


def _wait_for_stack_healthy(
    timeout: float = 180.0,
    poll_interval: float = 3.0,
    *,
    skip_observability: bool = False,
) -> bool:
    """Poll every desired component's health until all report healthy, or `timeout` elapses.

    Reuses `self_heal.list_component_status()` -- the same cross-mode
    (native/Compose/Terraform/Kubernetes) probe set `nyxgpt self-heal status`
    and the automatic heal loop already rely on -- so `up`'s health-wait can't
    drift from what the rest of the system considers "healthy". A component
    with `desired=False` (an operator-disabled observability profile, or one
    marked intentionally stopped) is excluded, same as `heal_now`'s automatic
    pass.

    `skip_observability` additionally excludes the observability Compose
    services, because `--skip-observability` deliberately does not start them
    while leaving their config.ini feature flags on -- so self-heal keeps
    reporting them `desired=True, state="absent"` (that's `_absent_desired_
    statuses`, and it is the correct answer to "what does the operator want
    running"). Without this, `nyxgpt up --skip-observability` could never
    return 0: it would wait the full timeout for containers the same command
    chose not to start, and exit 2 on a perfectly healthy stack (#3508).
    """
    excluded = self_heal.observability_services() if skip_observability else set()
    deadline = time.monotonic() + timeout
    while True:
        if not _pending_components(excluded):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def up(args) -> int:
    """CLI entrypoint for `nyxgpt up` -- a thin alias for `nyxgpt ops install`.

    Runs the exact same reconciliation `install()` does -- mode flags
    (`--terraform`, `--kubernetes`, `--local`, `--skip-observability`, etc.)
    pass straight through, no forked behavior -- then waits for every desired
    component to report healthy via `_wait_for_stack_healthy` and prints the
    web UI URL once it's reachable. Idempotent, same as `install()` itself:
    re-running it just reconciles and re-waits.

    Pass `--no-wait` to return as soon as `install()` finishes, without
    waiting for component health; `--timeout` controls how long to wait
    before giving up (default 180s). `--skip-observability` is honored by the
    wait as well as the install -- see `_wait_for_stack_healthy`.

    Returns whatever `install()` returned if it failed or `--no-wait` was
    passed; 2 if the health-wait times out; 0 once every desired component is
    healthy.
    """
    rc = install(args)
    if rc != 0 or getattr(args, "no_wait", False):
        return rc

    skip_observability = bool(getattr(args, "skip_observability", False))
    print("\nWaiting for components to report healthy...")
    if not _wait_for_stack_healthy(
        timeout=getattr(args, "timeout", 180.0),
        skip_observability=skip_observability,
    ):
        pending = _pending_components(
            self_heal.observability_services() if skip_observability else set()
        )
        still = f" Still unhealthy: {', '.join(pending)}." if pending else ""
        print(
            "WARNING: not every component reported healthy within the timeout --"
            f"{still} run `nyxgpt ops status` or `nyxgpt self-heal status` for details.",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "kubernetes", False):
        # No second terminal (#3986): the install establishes the access path
        # -- a published NodePort on the cluster nyxGPT provisions, or a
        # managed background forward on one whose host ports it cannot map --
        # so this is a URL, not homework.
        print(f"nyxGPT is up: {WEB_URL}")
        if not skip_observability:
            print(
                "Observability (Grafana, Prometheus, Jaeger, GlitchTip) runs in the cluster "
                "too -- `nyxgpt ops port-forward --target observability` publishes all four "
                "on the ports the admin dashboard links to."
            )
    else:
        print(f"nyxGPT is up: {WEB_URL}")
    return 0


def required_models_status(
    cfg: ConfigParser | None = None,
    cfg_path: Path | None = None,
    *,
    kubernetes: bool = False,
) -> dict[str, Any]:
    """Report whether Ollama holds every model this install requires (#3824).

    Shared by `nyxgpt ops status`, which prints it, and the SRE/admin
    dashboard's model-readiness panel, which renders it -- so the terminal and
    the dashboard can never disagree about what "ready" means.

    `reachable` is False when Ollama could not be asked at all; `present` is
    then None per model rather than False, because "cannot tell" is not
    "missing".

    `kubernetes=True` asks the Ollama *in the cluster* instead of the one this
    machine's config names (#3987). It is a parameter rather than a probe
    because only the caller knows which deployment the question is about: the
    owner's Kubernetes acceptance run got `UNKNOWN (Ollama unreachable)` for
    two models that `kubectl -n nyxgpt exec ollama-0 -- ollama list` showed
    were both present, because the host has no Ollama on `127.0.0.1:11434`
    and is not supposed to -- the install stops it. `ops status`/`ops doctor`
    pass True when they have seen Pods; the API surface never does, and needs
    not to: an api Pod's own config already points at `http://ollama:11434`,
    which is the same Ollama by the route that actually exists from there.

    The two sources are deliberately *not* merged or fallen back between. A
    host that has both a native Ollama and a Kubernetes deployment has two
    model stores that can legitimately differ, and answering "is this
    deployment ready" out of the other one's store is the same class of false
    report this parameter exists to end.
    """
    from nyxgpt.config import get_ollama_base_url, load_config

    if cfg is None:
        cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
        # An absent config reports against the shipped defaults, it does not
        # raise: `load_config(None)` means "the default *path*", not "the
        # defaults", so passing None here routed the no-config case straight
        # into the FileNotFoundError the `exists()` check had just ruled out.
        # `ops status` is the diagnostic a user runs on a not-yet-configured
        # machine, and its contract is to always return 0 -- an empty parser
        # makes every value fall through to its code default and the block
        # degrades to reporting those as MISSING/UNKNOWN, which is the honest
        # answer when there is no config to read.
        cfg = load_config(cfg_path) if cfg_path.exists() else ConfigParser()
    base_url = K8S_OLLAMA_BASE_URL if kubernetes else get_ollama_base_url(cfg)
    wanted = model_bootstrap.required_models(cfg)

    installed: set[str] | None
    error = ""
    if kubernetes:
        installed, error, detail = _k8s_installed_model_names()
        if installed is None:
            logger.warning(
                "Ollama model lookup failed in the %s namespace (%s): %s",
                K8S_NAMESPACE,
                error,
                detail,
            )
    else:
        try:
            installed = model_bootstrap.installed_model_names(base_url=base_url)
        except Exception as e:
            installed = None
            # #3837 (CodeQL #129, py/stack-trace-exposure). Same fault class as
            # #123 one file over, and found by the same sweep: this dict is
            # returned straight out of `GET /models/required` (`app.py`), so a
            # caught exception's message reaching it reaches a browser. The bare
            # `except Exception` is the point -- whatever `installed_model_names`
            # raises against an unreachable Ollama is an httpx transport error,
            # and its string names the base URL's resolution failure and the
            # host's proxy. The class is what the dashboard renders ("Ollama did
            # not answer (ConnectError)"); the message goes to the log.
            #
            # Logged as one formatted line, not `exc_info=e` (#3987). The
            # #3837 constraint is about what reaches the *browser* and is
            # unchanged -- `error` is still the class name alone -- but the
            # traceback was ~50 lines of urllib frames on the terminal of
            # every `nyxgpt ops status`, printed two lines above the graceful
            # "UNKNOWN (Ollama unreachable)" verdict it duplicated. An
            # unreachable dependency is a normal outcome of a status probe,
            # and the stack of a caught, handled exception says nothing an
            # operator can act on that the class and message do not.
            logger.warning(
                "Ollama model lookup failed at %s: %s: %s", base_url, type(e).__name__, e
            )
            error = type(e).__name__

    missing = (
        []
        if installed is None
        else [m for m in wanted if model_bootstrap.normalize_model_name(m.name) not in installed]
    )
    models = [
        {
            "role": m.role,
            "model": m.name,
            "setting": m.setting,
            "present": None if installed is None else m not in missing,
        }
        for m in wanted
    ]
    return {
        "base_url": base_url,
        "reachable": installed is not None,
        "error": error,
        "models": models,
        "ready": installed is not None and not missing,
        # Built here rather than in each caller so the terminal, the dashboard
        # and doctor all offer the same nyxgpt-wrapped remediation.
        "remediation": model_bootstrap.missing_models_hint(missing) if missing else "",
    }


def _print_required_models_status(
    *, kubernetes: bool = False, cluster_unreadable: str = ""
) -> None:
    """Print the required-model readiness block of `nyxgpt ops status`.

    `kubernetes=True` reports against the deployment's in-cluster Ollama --
    see `required_models_status` for why the caller decides that and not this
    function (#3987).

    `cluster_unreadable` is the third state, and the one this block would
    otherwise report dishonestly: a cluster is configured but its Pod list did
    not come back, so nobody can say whether a deployment is there. The block
    falls back to this host's Ollama -- there is nothing else left to ask --
    but says so, because "PRESENT" printed under a Deployment mode block that
    just said `cannot determine` reads as a statement about the deployment,
    which is the class of false report this whole change exists to end.
    """
    info = required_models_status(kubernetes=kubernetes)
    where = " (in-cluster)" if kubernetes else ""
    print(f"\nRequired models (Ollama at {info['base_url']}{where}):")
    if cluster_unreadable and not kubernetes:
        print(
            f"  Note: the {K8S_NAMESPACE} namespace could not be read ({cluster_unreadable}), "
            "so these lines describe this host's Ollama only -- not any Kubernetes "
            "deployment that may be running."
        )
    # Reachable, though only one way: `required_models` falls back to the code
    # default when the key is *absent*, so this branch is not the no-config
    # case -- it is a config.ini that sets `default_model =` (and
    # `embedding_model =`) to the empty string, which asks for no model at all.
    if not info["models"]:
        print("  none configured -- set [nyxgpt] default_model in ~/.nyxGPT/config.ini")
        return
    if not info["reachable"]:
        for m in info["models"]:
            print(f"  {m['role']}: {m['model']} -- UNKNOWN (Ollama unreachable)")
        # Named for the deployment being reported on: telling an operator to
        # bring "the ollama service" up when the Ollama in question is a Pod
        # points at the wrong machine (#3987).
        next_step = (
            f"check `kubectl -n {K8S_NAMESPACE} get pods -l app=ollama` -- the Pod that "
            "serves this deployment is not answering"
            if kubernetes
            else "run `nyxgpt ops status` again once the ollama service is up"
        )
        print(f"  Ollama did not answer ({info['error']}) -- {next_step}.")
        return
    for m in info["models"]:
        print(f"  {m['role']}: {m['model']} -- {'PRESENT' if m['present'] else 'MISSING'}")
    if not info["ready"]:
        print(f"  {info['remediation']}")
        if kubernetes:
            # `missing_models_hint` is written for the machine it runs on, and
            # both commands it names act on this host's Ollama -- neither one
            # reaches the Pod that is actually short of the model (#3987).
            print(
                "  Those commands pull into this host's Ollama, which is not what serves "
                "this deployment -- re-run `nyxgpt ops install --kubernetes` to pull into "
                "the cluster."
            )


def status(_args) -> int:
    """CLI entrypoint for `nyxgpt ops status`.

    Prints the detected deployment mode (native vs. Compose per component),
    a native/Compose port-conflict warning if both are live, Homebrew
    service states, the Cassandra log-follower LaunchAgent's load state,
    whether the ops-managed Cassandra Docker container is running, and (in
    Kubernetes mode, when pods are present) each canary-capable component's
    stable/canary rollout state via `_serving_status` (see #3419).

    Where a Kubernetes deployment is present it is named in the "Deployment
    mode" block and the required-model check is asked of the *in-cluster*
    Ollama rather than this host's (#3987) -- see `_k8s_deployment_pods`.

    Always returns 0.
    """
    logger.info("ops: status starting", extra={"component": "ops", "action": "status"})
    print("nyxGPT ops status")

    mode = detect_deployment_mode()
    # Read once, up front, and passed to every block below that would
    # otherwise assume the native machine is the whole story (#3987): the
    # "Deployment mode" summary and the required-model check both used to be
    # printed before this command ever looked at the cluster, so a fully
    # running 14-Pod deployment was reported as "nothing detected" with its
    # models UNKNOWN. The dedicated Kubernetes section further down reuses
    # this same probe rather than paying for a second `kubectl get pods`.
    k8s = _k8s_deployment_probe()
    k8s_pods = k8s.pods
    k8s_deployed = k8s.deployed

    # Printed before the component states, and per component below, so a
    # dev-mode pass can never be read as an artifact-path pass (#3789).
    #
    # Attributed to the *native* api/web, and only asserted when there are
    # any (#3834): this marker says nothing about a Terraform or Kubernetes
    # deployment, and reporting it unqualified is what let a pure-Kubernetes
    # deployment be labelled `dev (editable checkout at ...)` from a native
    # dev install that had long since been torn down. The Terraform line
    # below and the Kubernetes section further down report those deployments'
    # own recorded modes (#3835, #3834).
    install_mode = read_install_mode()
    native_installed = any(
        mode.native.get(component, "none") != "none" for component in DEV_LAUNCHD_LABELS
    )
    print(f"\nInstall mode (native api/web): {install_mode.label()}")
    if not native_installed:
        print(
            "  No native api/web on this machine -- that is a record of the last native "
            "install, not a statement about whatever is serving now."
        )
    elif install_mode.is_dev:
        checkout = Path(install_mode.checkout) if install_mode.checkout else None
        if checkout is not None and not checkout.exists():
            print(
                f"  WARNING: that checkout no longer exists ({checkout}) -- api/web are "
                "running code that may be gone. Re-run `nyxgpt up --dev` from a checkout, "
                "or `nyxgpt up` to return to the artifact path."
            )
        print(
            "  api/web run the working tree (editable venv + Next dev server); "
            "restart a service to pick up new code. Artifact-path behavior "
            "(published tap/tarball) is NOT what is being exercised here."
        )

    terraform_deployed = any(_container_deployed(state) for state in mode.terraform.values())
    terraform_install_mode = read_install_mode(substrate=SUBSTRATE_TERRAFORM)
    if terraform_deployed or install_mode_file(SUBSTRATE_TERRAFORM).exists():
        # Attributed the same way as the native line above, and printed only
        # when there is a Terraform deployment to describe (#3835). `deployed`
        # matters here: a running stack with no marker is reported as not
        # recorded rather than as the artifact default, which for Terraform
        # would assert the opposite of the truth.
        print(
            f"Install mode (terraform): {terraform_install_mode.label(deployed=terraform_deployed)}"
        )
        if not terraform_deployed:
            # The marker alone is enough to print the line above, so this
            # branch is reached whenever a Terraform install ran on this
            # machine at some point -- including one that failed, or one that
            # has since been torn down. Saying so is the whole point (#3989):
            # the dev follow-up below used to be printed unconditionally and
            # asserted, in the present tense, that api/web containers exist
            # and describes what they were built from, on a machine where
            # `docker cassandra: absent` and every Terraform component
            # `absent` appeared three lines later. Mirrors the native block
            # above, which has always drawn this distinction.
            print(
                "  No Terraform deployment on this machine -- that is a record of the "
                "last Terraform install, not a statement about whatever is serving now."
            )
        elif terraform_install_mode.is_dev:
            print(
                "  the api/web containers were built from that working tree, not from "
                "published images -- artifact-path behavior is NOT what is being "
                "exercised here."
            )

    print("\nDeployment mode:")
    for component in ("api", "web", "ollama"):
        state = mode.native.get(component, "none")
        suffix = ""
        # Never stamp a mode on a component that isn't installed (#3834): a
        # `native api: none  [dev]` line described the install mode of
        # something that was not running at all.
        if component in DEV_LAUNCHD_LABELS and state != "none":
            suffix = f"  [{INSTALL_MODE_DEV if install_mode.is_dev else INSTALL_MODE_ARTIFACT}]"
        print(f"  native  {component}: {state}{suffix}")
    # Cassandra is the one Docker-managed piece of a local-first install --
    # labeling it "native" here misstated the topology.
    cassandra_state = mode.native.get("cassandra", "absent")
    if cassandra_state == DOCKER_STATE_UNKNOWN:
        # Never printed as `absent` (#4022): this session could not make the
        # read, which is a different fact from the container not being there.
        # The operator gets the cause and the permanent repair instead of a
        # confident wrong answer -- and `nyxgpt ops status` and the web UI's
        # Infrastructure page now say the same thing here, which before this
        # fix they could not (docs/systemd.md documented the disagreement).
        print(f"  docker  cassandra: {cassandra_state} (cannot determine from this session)")
        if mode.docker_probe_reason:
            print(f"      {mode.docker_probe_reason}")
        user = getpass.getuser()
        print(
            "      This session may not talk to the Docker daemon, and no `sg docker` hop "
            f"was available. Repair: sudo usermod -aG docker {user}, then "
            f"sudo loginctl terminate-user {user} (or reboot)."
        )
    else:
        print(f"  docker  cassandra: {cassandra_state}")
    if mode.compose:
        for component, state in sorted(mode.compose.items()):
            print(f"  compose {component}: {state}")
    else:
        print("  compose: not detected (no Docker Compose stack running)")
    # Kubernetes belongs in the block *titled* "Deployment mode" (#3987). The
    # Pods were already described further down in their own section, but a
    # block that enumerates native/docker/compose/terraform and stops reads as
    # "nothing is deployed" printed immediately above a running cluster --
    # which is what the owner's Kubernetes acceptance run saw. This is the CLI
    # cousin of #3828's "Detected mode: Nothing detected running" on the
    # dashboard, on a different code path: `status`'s own structure, not
    # `self_heal.detect_deployment_mode`, so that fix could not reach here.
    pointer = " -- see the Kubernetes section below" if k8s_deployed else ""
    print(f"  kubernetes: {k8s.summary}{pointer}")
    running_terraform = {c: s for c, s in mode.terraform.items() if _container_deployed(s)}
    if running_terraform:
        # Tri-state, matching the deployment line above: dev, artifact, or
        # unrecorded when something is running that no install recorded.
        tf_mode_label = terraform_install_mode.short_label(deployed=True)
        for component, state in sorted(running_terraform.items()):
            # Only api/web have an install mode -- ollama/cassandra are
            # pinned third-party images in both modes.
            suffix = f"  [{tf_mode_label}]" if component in ("api", "web") else ""
            print(f"  terraform {component}: {state}{suffix}")

    if mode.conflicts:
        stop_examples = ", ".join(f"nyxgpt ops stop {c}" for c in sorted(mode.conflicts))
        print(
            "\nWARNING: "
            + ", ".join(sorted(mode.conflicts))
            + " reported running in BOTH native and Docker Compose. Only one is actually "
            "serving traffic on the shared port -- the other is a phantom backend. Config "
            f"edits to {NATIVE_CONFIG_HINT} only reach the native process; if Compose is the "
            f"one answering, edit {COMPOSE_CONFIG_HINT} instead. Run `{stop_examples}` to stop "
            "both and pick one, or `nyxgpt ops down --app-only` to drop the Compose app tier "
            "entirely."
        )
    else:
        config_hint = NATIVE_CONFIG_HINT
        # The same core-vs-any question as `infra_status`'s mode selection
        # (#3855): `COMPOSE_CONFIG_HINT` names the config file mounted into
        # the Compose *api* container, so it is only in use when a core
        # component runs under Compose. The observability containers a
        # native install runs by default read no nyxGPT config at all, and
        # testing the whole snapshot told every such install to go edit a
        # file nothing on the host was reading.
        if compose_core_components(mode):
            config_hint += f" (native components) / {COMPOSE_CONFIG_HINT} (Compose components)"
        print(f"\nConfig in use: {config_hint}")

    if mode.terraform_conflicts:
        print(
            "\nWARNING: "
            + ", ".join(sorted(mode.terraform_conflicts))
            + " reported running under BOTH native/Compose and Terraform -- an incomplete "
            "mode switch (e.g. `nyxgpt ops down` without `--terraform` before `nyxgpt ops "
            "install`) left two whole core stacks up at once, each answering on its own "
            "network. Run `nyxgpt ops down --terraform` to tear down the Terraform stack, "
            "then `nyxgpt ops install` to reconcile the native/Compose one -- or the reverse "
            "if Terraform is the mode you want to keep."
        )

    if _is_linux():
        if _which("systemctl"):
            cp = _run(
                [
                    "systemctl",
                    "--user",
                    "list-units",
                    "--all",
                    "--type=service",
                    "--no-legend",
                    "nyxgpt-*.service",
                ],
                check=False,
                expected=True,
            )
            print("\nsystemd --user services:\n" + (cp.stdout or "").strip())
        else:
            print("\nsystemd --user services: systemctl not found")

        # Every support unit nyxGPT installs, from the same map the removal
        # path uses -- the Linux twin of the LaunchAgent block below, which
        # #3859 widened past `cassandra-logs` while this branch kept reporting
        # that one alone (#4033). `nyxgpt ops status` is the command an
        # operator checks a teardown with, so a follower it cannot name is a
        # follower that survives one unnoticed.
        for unit_base in SUPPORT_SYSTEMD_UNITS.values():
            unit = f"{unit_base}.service"
            try:
                cp = _run(["systemctl", "--user", "is-active", unit], check=False, expected=True)
                state = (cp.stdout or "").strip() or "inactive"
                print(f"\nsystemd unit {unit}: {state.upper()}")
            except Exception as e:
                print(f"\nsystemd unit {unit}: ERROR ({e})")
    else:
        if _which("brew"):
            cp = _run(["brew", "services", "list"], check=False, expected=True)
            print("\nHomebrew services:\n" + (cp.stdout or "").strip())
        else:
            print("\nHomebrew services: brew not found")

        # Every agent nyxGPT installs, from the map the removal path uses
        # (#3859). It used to be `com.nyxgpt.cassandra-logs` alone, so the two
        # agents this command is most needed for -- `ollama-logs` and
        # `ollama-env`, which no `brew uninstall` can remove -- were invisible
        # to the one command an operator would check a teardown with.
        labels = list(SUPPORT_LAUNCHD_LABELS.values())
        if install_mode.is_dev:
            # Dev mode's api/web run as LaunchAgents, not brew services --
            # the "Homebrew services" block above says nothing about them.
            labels.extend(DEV_LAUNCHD_LABELS[c] for c in ("api", "web"))
        try:
            cp = _run(["launchctl", "list"], check=False, expected=True)
            listed = cp.stdout or ""
            for label in labels:
                print(f"\nLaunchAgent {label}: {'LOADED' if label in listed else 'NOT LOADED'}")
        except Exception as e:
            for label in labels:
                print(f"\nLaunchAgent {label}: ERROR ({e})")

    if _which("docker") is None:
        print("\nDocker: docker not found")

    _print_required_models_status(
        kubernetes=k8s_deployed,
        cluster_unreadable="" if k8s.determined else k8s.reason,
    )

    tf_state = terraform_stack_state()
    if any(_container_deployed(state) for state in tf_state.values()):
        print("\nTerraform-managed stack (nyxgpt ops down --terraform to tear down):")
        for component, state in sorted(tf_state.items()):
            print(f"  terraform {component}: {state}")

    if k8s_deployed:
        # Classified, not `kubectl get pods --no-headers` echoed verbatim
        # (#3827). That raw table renders a Pod pulling an image and a Pod no
        # node will ever take identically -- both just say `Pending` -- which
        # is the confusion this issue is about, and `nyxgpt ops status` is the
        # command every failure message here tells the operator to run next.
        # Same classifier, same three labels, same reasons as the install's
        # own report.
        #
        # The Pods come from the single read taken at the top of this command
        # (#3987) -- the blocks above report on the same deployment and must
        # not be able to disagree with this section about whether it exists.
        pod_states = k8s_pods
        if pod_states:
            context = _kubectl_context()
            if context == KIND_CONTEXT:
                cluster_note = (
                    " (kind cluster nyxgpt provisioned -- torn down together on "
                    "nyxgpt ops down --kubernetes)"
                )
            elif not context and _in_cluster():
                # Running in a Pod: there is no context to name, and calling
                # this a "bring-your-own cluster, context: " with an empty
                # tail was the CLI's version of #3988's false negative.
                cluster_note = f" ({K8S_IN_CLUSTER_CONTEXT_LABEL} -- this process's own cluster)"
            else:
                cluster_note = f" (bring-your-own cluster, context: {context})"
            print(
                f"\nKubernetes ({K8S_NAMESPACE} namespace, nyxgpt ops down --kubernetes to "
                f"tear down): {len(pod_states)} pod(s){cluster_note}"
            )
            # The deployment's OWN install mode (#3834) -- what the two images
            # in this cluster were built from, not what the native services on
            # this host were installed from.
            k8s_install_mode = read_install_mode(substrate=SUBSTRATE_KUBERNETES)
            print(f"  Install mode: {k8s_install_mode.label()}")
            if not _k8s_app_pods_present(pod_states):
                # Same distinction the native and Terraform lines draw
                # (#3989): the marker records what the last install built,
                # and a namespace with no api/web Pods in it is not running
                # any of it.
                print(
                    "  No nyxGPT api/web Pods in this namespace -- that is a record of the "
                    "last Kubernetes install, not a statement about whatever is serving now."
                )
            elif k8s_install_mode.is_dev:
                checkout = Path(k8s_install_mode.checkout) if k8s_install_mode.checkout else None
                if checkout is not None and not checkout.exists():
                    print(
                        f"  WARNING: that checkout no longer exists ({checkout}) -- the "
                        "images in this cluster were built from a tree that is gone."
                    )
                print(
                    "  The Pods run images built from that working tree as it was at install "
                    "time; re-run `nyxgpt ops install --kubernetes --dev` to pick up "
                    "new code, or drop --dev to deploy the published artifacts."
                )
            for pod_state in pod_states:
                print(f"  [{pod_state.label}] pod {pod_state.name}: {pod_state.summary}")
                if pod_state.details:
                    print(f"      {pod_state.details}")

            observability_state = _k8s_observability_workload_state()
            if any(state != "absent" for state in observability_state.values()):
                print(
                    "\nKubernetes observability (in-cluster -- reach the UIs with "
                    "`nyxgpt ops port-forward --target observability`):"
                )
                # `0/1 ready` is PENDING here too, not a bare count the
                # operator has to interpret against the labelled Pod lines
                # printed just above it (#3827).
                for workload, state in observability_state.items():
                    w = _classify_k8s_observability_workload(workload, state)
                    print(f"  [{w.label}] {w.name}: {w.summary}")
            else:
                print(
                    "\nKubernetes observability: not deployed "
                    "(`nyxgpt ops observability --kubernetes` deploys it)"
                )

            serving = _serving_status("kubernetes")
            if serving["supported"]:
                print("\nCanary (per component -- see the Canary page in the web admin):")
                for component, c in serving["components"].items():
                    rollout = f"active -- {c['weight_percent']}%" if c["active"] else "idle"
                    print(
                        f"  {component}: rollout {rollout} | "
                        f"stable={c['stable']['state']} ({c['stable']['version'] or 'n/a'}) | "
                        f"canary={c['canary']['state']} ({c['canary']['version'] or 'n/a'})"
                    )

    print(
        "\nCleanup: `nyxgpt ops stop <target>` stops one component (native and/or Compose), "
        "`nyxgpt ops down` tears down the whole stack."
    )

    logger.info(
        "ops: status complete (native=%s, compose=%s, conflicts=%s)",
        mode.native,
        mode.compose,
        mode.conflicts,
        extra={
            "component": "ops",
            "action": "status",
            "native": mode.native,
            "compose": mode.compose,
            "conflicts": mode.conflicts,
        },
    )

    return 0


def _promtail_container_id() -> str | None:
    """Return the running promtail container's ID, or None if it isn't up."""
    cp = _run(
        ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "ps", "-q", "promtail"],
        check=False,
        expected=True,
    )
    container_id = (cp.stdout or "").strip().splitlines()[0] if cp.stdout else ""
    return container_id or None


def _promtail_native_mount_missing(container_id: str) -> bool:
    """Return True if the running promtail container has no native-logs bind mount.

    Inspects the actual container's mounts via `docker inspect` rather than
    grepping docker-compose.yml's text for the mount marker -- a promtail
    container created before a compose-file edit (or otherwise missing the
    bind for any reason) has a stale mount list that still passes a text-only
    check, even though the running container can't see native-mode logs.
    """
    cp = _run(
        ["docker", "inspect", "--format", "{{json .Mounts}}", container_id],
        check=False,
        expected=True,
    )
    if cp.returncode != 0:
        return True
    try:
        mounts = json.loads(cp.stdout or "[]")
    except Exception as e:
        logger.warning(
            "Failed to parse `docker inspect` mounts for %s, assuming misconfigured: %s",
            container_id,
            e,
            extra={"component": "ops"},
        )
        return True
    return not any(m.get("Destination") == PROMTAIL_NATIVE_LOG_MOUNT_MARKER for m in mounts)


def _log_aggregation_wiring_issue(cfg_path: Path | None = None) -> str | None:
    """Detect the #3277 failure mode: native-mode logs never reaching Loki.

    promtail always runs as a Compose container regardless of whether the
    core app is deployed native or Compose (see `OBSERVABILITY_PROFILES`).
    In native mode, api/self-heal/ops write logs to the host `~/.nyxGPT/logs`
    directly -- a plain directory, not the `nyxgpt_data` Docker-managed
    volume promtail otherwise mounts for Compose-mode logs. If the running
    promtail container ever loses its host bind mount for that directory,
    native logs silently stop reaching Loki -- Grafana just shows nothing
    rather than erroring, so this needs an explicit check rather than
    relying on someone noticing an empty dashboard.

    Only reports an issue when there's something to actually verify: log
    aggregation is enabled, promtail is confirmed running, and native-mode
    log files actually exist to be missed. Returns None otherwise (nothing
    to check yet, or the wiring is intact).
    """
    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return None

    parser = ConfigParser()
    try:
        parser.read(cfg_path)
    except Exception as e:
        logger.warning(
            "Failed to parse %s, skipping promtail wiring check: %s",
            cfg_path,
            e,
            extra={"component": "ops"},
        )
        return None
    if not get_log_aggregation_enabled(parser):
        return None

    if _compose_stack_snapshot().get("promtail") != "running":
        return None

    native_log_dir = Path.home() / ".nyxGPT" / "logs"
    if not native_log_dir.exists() or not any(native_log_dir.glob("*.log*")):
        return None

    container_id = _promtail_container_id()
    if container_id is None or not _promtail_native_mount_missing(container_id):
        return None

    return (
        f"Log aggregation is enabled and native-mode logs exist under {native_log_dir}, "
        "but the running promtail container has no bind mount for them -- "
        "native logs are not reaching Loki. See docs/docker-compose.md#log-aggregation."
    )


def _tracing_wiring_issue(cfg_path: Path | None = None) -> str | None:
    """Detect the #3350 failure mode: native-mode spans never reaching the collector.

    The api's OTLPSpanExporter is a fire-and-forget HTTP client: when nothing
    listens on `[tracing] otlp_endpoint`, it silently drops every span after
    retrying, rather than failing the request or raising anywhere visible.
    That leaves the Distributed Tracing panel reporting "active" (it only
    checks that `init_tracing` ran, not that the collector is reachable)
    while Jaeger's store stays permanently empty. This does a real TCP
    connect (via `tracing.otlp_endpoint_reachable`) to the endpoint's
    host/port so `doctor` catches the gap, e.g. the otel-collector Compose
    service missing its host `ports:` mapping in native mode.

    Only reports an issue when tracing is actually enabled. Returns None
    otherwise (nothing to check yet, or the collector is reachable).
    """
    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return None

    parser = ConfigParser()
    try:
        parser.read(cfg_path)
    except Exception as e:
        logger.warning(
            "Failed to parse %s, skipping tracing wiring check: %s",
            cfg_path,
            e,
            extra={"component": "ops"},
        )
        return None
    if not get_tracing_enabled(parser):
        return None

    tracing_config = get_tracing_config(parser)
    endpoint = tracing_config["otlp_endpoint"]
    if tracing.otlp_endpoint_reachable(endpoint):
        return None

    return (
        f"Tracing is enabled ([tracing] otlp_endpoint={endpoint}) but nothing is "
        "listening there -- spans are being silently dropped and Jaeger will stay "
        "empty. Confirm the otel-collector Compose service (tracing profile) is "
        "running and publishes that port to the host (nyxgpt ops observability). "
        "See docs/docker-compose.md#distributed-tracing."
    )


def _prometheus_api_scrape_issue(cfg_path: Path | None = None) -> str | None:
    """Detect the #3721 failure mode: prometheus can't reach the native API.

    Twin of `_tracing_wiring_issue` for the metrics half of the stack. A failed
    scrape is just as silent: prometheus itself stays healthy and green, the
    Monitoring panel reports "enabled" (it only checks the flag), and every
    Grafana dashboard renders with no data at all. The classic cause is a plain
    Linux docker engine, where `host.docker.internal` resolves to the bridge
    gateway and a loopback-bound native uvicorn isn't listening there -- which
    is what `host-api-relay` exists to bridge.

    Asks prometheus for its own view of the `nyxgpt-api` target rather than
    re-deriving reachability, so it reports the same `lastError` an operator
    would see on prometheus's /targets page. Only runs when monitoring is
    enabled and prometheus is actually up; any transport/parse failure returns
    None (prometheus not reachable is the existing "stack is down" story, not
    this check's finding).
    """
    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return None

    parser = ConfigParser()
    try:
        parser.read(cfg_path)
    except Exception as e:
        logger.warning(
            "Failed to parse %s, skipping prometheus scrape check: %s",
            cfg_path,
            e,
            extra={"component": "ops"},
        )
        return None

    monitoring_config = get_monitoring_config(parser)
    if not monitoring_config["enabled"]:
        return None

    base_url = str(monitoring_config["prometheus_ui_url"]).rstrip("/")
    try:
        resp = httpx.get(f"{base_url}/api/v1/targets", params={"state": "active"}, timeout=5.0)
        resp.raise_for_status()
        active = resp.json()["data"]["activeTargets"]
    except Exception as e:
        logger.debug(
            "Prometheus targets unavailable at %s, skipping scrape check: %s",
            base_url,
            e,
            extra={"component": "ops"},
        )
        return None

    api_targets = [t for t in active if t.get("labels", {}).get("job") == "nyxgpt-api"]
    if not api_targets or any(t.get("health") == "up" for t in api_targets):
        return None

    last_error = next(
        (t.get("lastError") for t in api_targets if t.get("lastError")),
        "no error reported",
    )
    hint = ""
    if _is_linux():
        hint = (
            " On Linux this is usually the container->host-loopback gap (#3721): re-run "
            "`nyxgpt ops observability` to (re)enable the host-api-relay service, and check "
            "`nyxgpt ops logs host-api-relay`."
        )
    return (
        "Prometheus cannot scrape the API's /metrics endpoint "
        f"(job nyxgpt-api is down: {last_error}) -- every Grafana dashboard will render "
        f"empty even though the stack looks healthy.{hint} "
        "See docs/troubleshooting.md#grafana-dashboards-are-empty-on-linux."
    )


# Remediation for a host still carrying the pre-#3721 `[api] host` widening.
# Shared verbatim by the doctor check and `_sync_host_relay_env`'s disabled
# outcome so the two surfaces can't drift into telling different stories.
HOST_RELAY_REVERT_REMEDIATION = (
    "The host-api-relay service (#3721) now gives Prometheus a route to a "
    "loopback-bound API, so widening the bind is no longer necessary for "
    "observability: set `[api] host = 127.0.0.1` in ~/.nyxGPT/config.ini, then run "
    "`nyxgpt ops env-sync && nyxgpt ops observability && nyxgpt ops restart api`. "
    "See docs/troubleshooting.md#grafana-dashboards-are-empty-on-linux."
)


def _insecure_api_bind_issue(cfg_path: Path | None = None) -> str | None:
    """Detect the #3721 *workaround* still in place: `[api] host` past loopback.

    The other half of `_prometheus_api_scrape_issue`. That check catches a host
    that never got the relay; this one catches a host that worked around the
    missing relay by hand and was then never migrated back -- the exact state
    #3509's reporter is in ("I had to enable auth and set host to 0.0.0.0 to get
    observability working on a linux native installation").

    That state is invisible to every existing check. The scrape is *up*, so
    `_prometheus_api_scrape_issue` stays quiet; `_host_relay_decision` reads the
    widened bind and disables the relay with a reason that sounds like an
    approval; and each `nyxgpt ops observability` faithfully reconciles the
    insecure posture back in. Nothing ever says the workaround is now
    unnecessary, so the API keeps listening on every interface -- which is
    precisely what P6-4 ("no 0.0.0.0/0 ingress anywhere") forbids.

    Deliberately narrow, so it flags the workaround rather than nagging every
    non-loopback bind. All four must hold:

    * Linux -- Docker Desktop never had the gap, so the workaround isn't a
      plausible reason for a widened bind on macOS.
    * `[api] host` is outside `LOOPBACK_API_HOSTS`.
    * Monitoring is enabled -- the widening is only attributable to the
      observability gap if observability is actually in use.
    * The docker bridge gateway resolves -- i.e. the relay would really work
      here. Without that, reverting would trade a bind-posture finding for
      genuinely broken dashboards, so this stays silent.
    """
    if not _is_linux():
        return None

    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return None

    api_host = _native_api_host(cfg_path)
    if api_host.lower() in LOOPBACK_API_HOSTS:
        return None

    parser = ConfigParser()
    try:
        parser.read(cfg_path)
    except Exception as e:
        logger.warning(
            "Failed to parse %s, skipping API bind posture check: %s",
            cfg_path,
            e,
            extra={"component": "ops"},
        )
        return None
    if not get_monitoring_config(parser)["enabled"]:
        return None

    if _docker_bridge_gateway_ip() is None:
        return None

    return (
        f"The API is bound to `{api_host}`, publishing it on every interface "
        "instead of loopback only -- the pre-#3721 workaround for Prometheus not "
        f"being able to reach a native API from a container. {HOST_RELAY_REVERT_REMEDIATION}"
    )


def _tracing_packages_doctor_issue(cfg_path: Path | None = None) -> str | None:
    """Detect the #3484 failure mode: a venv predating the #3430 OTel backbone
    (or missing a package `pip uninstall`'d since) is missing an
    `opentelemetry-instrumentation-*`/exporter package.

    `nyxgpt.tracing` no longer crashes on import when a package is missing
    (that's the #3484 fix), but if tracing is enabled in config the operator
    is silently running degraded -- missing instrumentors are skipped, or
    tracing is disabled outright if the exporter itself is missing -- until
    they reinstall. Only reports an issue when tracing is actually enabled,
    same gating as `_tracing_wiring_issue`.
    """
    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return None

    parser = ConfigParser()
    try:
        parser.read(cfg_path)
    except Exception as e:
        logger.warning(
            "Failed to parse %s, skipping tracing packages check: %s",
            cfg_path,
            e,
            extra={"component": "ops"},
        )
        return None
    if not get_tracing_enabled(parser):
        return None

    missing = tracing.missing_tracing_packages()
    if not missing:
        return None

    return (
        "Tracing is enabled but these OTel packages are missing from this venv: "
        f"{', '.join(missing)}. Tracing is running degraded (missing instrumentors "
        "are skipped, or tracing is disabled if the exporter itself is missing) "
        "until you run: nyxgpt ops install."
    )


def _ollama_env_drift_issue() -> str | None:
    """Detect the #3431 boot-race failure mode: native Ollama's OLLAMA_MODELS
    env drifting back to Ollama's default `~/.ollama/models` store.

    `launchctl setenv` only applies to the launchd GUI session it's run in,
    and the `com.nyxgpt.ollama-env` LaunchAgent that reapplies it is a
    *separate* RunAtLoad agent from Homebrew's own `ollama` one -- launchd
    doesn't guarantee their relative ordering. If Homebrew's agent wins that
    race on a given login, `ollama serve` starts before OLLAMA_MODELS is set
    and silently falls back to the default store for the rest of the
    session, reproducing the split-library bug this issue exists to fix.
    `set-ollama-models-env.sh` now force-restarts the brew service every
    login to close that race, but this check surfaces drift here too in case
    something else (a manual `launchctl unsetenv`, a non-nyxgpt Ollama
    install) undoes it between doctor runs.

    Only reports an issue once the shared store has actually been configured
    (`nyxgpt ops install` has run at least once) and there's a `launchctl` to
    check (macOS). Returns None otherwise.
    """
    if _which("launchctl") is None:
        return None
    marker = _ollama_migration_state_path("ollama-native-env.configured")
    if not marker.exists():
        return None

    expected = _shared_ollama_models_dir()
    cp = _run(["launchctl", "getenv", "OLLAMA_MODELS"], check=False, expected=True)
    actual = (cp.stdout or "").strip()
    if cp.returncode != 0 or not actual:
        return (
            f"Native Ollama's OLLAMA_MODELS env is not set for this login session "
            f"(expected {expected}) -- models pulled/read natively may split from the "
            "shared store again (run: nyxgpt ops install)"
        )
    if Path(actual) != expected:
        return (
            f"Native Ollama's OLLAMA_MODELS is set to {actual}, not the shared store "
            f"{expected} -- models pulled/read natively may split from the shared store "
            "again (run: nyxgpt ops install)"
        )
    return None


def _missing_required_models_issue(
    cfg_path: Path | None = None, *, kubernetes: bool = False
) -> str | None:
    """Report a configured chat/embedding model Ollama does not have (#3824).

    `nyxgpt ops install` pulls both, so a machine missing one has either not
    been installed since the models were configured, had the model deleted, or
    is pointing at an Ollama whose store is not the one the install filled.
    Whichever it is, the first chat message will fail against it -- so doctor
    calls it a problem and names the `nyxgpt` command that fixes it.

    Silent when Ollama is unreachable: that is the ollama service's own
    failure, already reported by `nyxgpt ops status`/self-heal, and guessing
    "model missing" from it would misname the fault.

    `kubernetes=True` asks the deployment's in-cluster Ollama instead of the
    one this machine's config names, for the same reason `ops status` does
    (#3987). Doctor's native-machine reading was not a false *report* here --
    an unreachable host Ollama makes it silent rather than wrong -- but it was
    a false *silence*: on a Kubernetes deployment the check never ran at all,
    so the one fault it exists to catch, a Pod short of a model the config
    requires, could not be found by the command an operator runs to find
    faults.
    """
    from nyxgpt.config import get_ollama_base_url, load_config

    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return None
    try:
        cfg = load_config(cfg_path)
        if kubernetes:
            installed, error, detail = _k8s_installed_model_names()
            if installed is None:
                # The same silence as an unreachable native Ollama, and for
                # the same reason: an Ollama that cannot be asked is a fault
                # of its own, reported by the Pod lines in `ops status`, and
                # inferring "model missing" from it would misname it.
                logger.info(
                    "ops: skipping the required-model check, the in-cluster Ollama did "
                    "not answer: %s: %s",
                    error,
                    detail,
                    extra={"component": "ops", "action": "doctor"},
                )
                return None
            missing = [
                m
                for m in model_bootstrap.required_models(cfg)
                if model_bootstrap.normalize_model_name(m.name) not in installed
            ]
            if not missing:
                return None
            return (
                f"{model_bootstrap.missing_models_hint(missing)} That Ollama is the one "
                f"in the {K8S_NAMESPACE} namespace, not this host's -- re-run "
                "`nyxgpt ops install --kubernetes` to pull into the cluster."
            )
        missing = model_bootstrap.missing_required_models(
            base_url=get_ollama_base_url(cfg), cfg=cfg
        )
    except Exception as e:
        logger.info(
            "ops: skipping the required-model check, Ollama did not answer: %s: %s",
            type(e).__name__,
            e,
            extra={"component": "ops", "action": "doctor"},
        )
        return None
    if not missing:
        return None
    return model_bootstrap.missing_models_hint(missing)


def _linux_ollama_port_conflict_issue() -> str | None:
    """Detect the #3632 port-11434 conflict: a system-wide `ollama.service`
    contending with the `nyxgpt-ollama.service` nyxgpt owns.

    Fires whenever the distro's system unit is active or enabled on Linux --
    it either already holds the port (so `nyxgpt-ollama.service` crash-loops)
    or will grab it back at the next boot. `nyxgpt ops install` reconciles
    this itself now (`_takeover_system_ollama_service` stops and disables the
    system unit), so re-running install is the first remediation offered,
    with the explicit `sudo` command for a host where install couldn't get
    root without a prompt. Linux only; returns None on macOS/other or when
    nothing conflicts.
    """
    if not _is_linux():
        return None
    if not _system_ollama_service_conflicts():
        return None
    return (
        "System-wide ollama.service is active/enabled and contends for port 11434 with "
        "nyxgpt-ollama.service, which nyxgpt manages (run: nyxgpt ops install, or if it "
        "reported it could not get root: sudo systemctl disable --now ollama.service && "
        "nyxgpt ops install)"
    )


# Curated per-component loggers surfaced in the Log Aggregation panel and
# the "Operational Logs" Grafana dashboard (see app._loki_curated_queries).
# `_loki_recent_volume_by_logger` reports each one's recent line count so
# `doctor` output can distinguish "pipeline healthy, component just idle"
# (e.g. deploy/canary legitimately silent on a native install that's never
# run a k8s operation) from "no logs reaching Loki at all" (every logger at
# zero, including ones that just definitely logged something) -- see #3349.
LOKI_CURATED_LOGGERS: dict[str, str] = {
    "self_heal": "nyxgpt.self_heal",
    "deploy": "nyxgpt.deploy",
    "canary": "nyxgpt.canary",
    "chat": "nyxgpt.chat",
    "rag": "nyxgpt.rag.rag",
}


def _grafana_doctor_token_path() -> Path:
    """Where the Grafana service-account token for doctor's Loki log-volume
    check is written -- see `_provision_grafana_doctor_token`.

    A dedicated Viewer-scoped service account, not the shared `admin`
    credential the rest of this module's Grafana calls use: that password
    lives in config.ini and can drift from whatever a long-running Grafana
    container's own database actually has (a regenerated config.ini, a
    hand-edited `.env`, ...), which is what let the datasource-proxy query
    below 401 on an otherwise-healthy stack (#3438). A token minted once and
    read straight off disk sidesteps that drift entirely -- same pattern as
    `_glitchtip_grafana_token_path`'s token file, just consumed by this
    process instead of by Grafana itself.
    """
    return Path.home() / ".nyxGPT" / "secrets" / "grafana-doctor-token"


def _loki_recent_volume_by_logger(
    grafana_ui_url: str, *, hours: int = 24
) -> tuple[dict[str, int] | None, str | None]:
    """Query Loki (via Grafana's provisioned datasource proxy, authenticated
    with the service-account token `_provision_grafana_doctor_token` mints)
    for each curated logger's line count over the last `hours`.

    Returns `(volumes, issue)`. `volumes` is None when nothing could be
    queried. `issue` is set to an actionable `doctor` finding only for the
    fixable auth failure modes (token file missing, or Grafana rejects the
    token) -- #3438 was specifically about that case degrading into a
    debug-level log line no operator would see. Any other failure (Grafana
    or Loki simply unreachable) still returns `issue=None`: that's not
    itself a failure here, since the rest of `doctor` already covers overall
    stack reachability.
    """
    token_path = _grafana_doctor_token_path()
    token = token_path.read_text().strip() if token_path.exists() else ""
    if not token:
        return None, (
            f"Missing Grafana service-account token at {token_path} -- the Loki "
            "log-volume check can't authenticate (run: nyxgpt ops install)"
        )

    volumes: dict[str, int] = {}
    try:
        with httpx.Client(
            base_url=grafana_ui_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        ) as client:
            for name, logger_name in LOKI_CURATED_LOGGERS.items():
                query = f'sum(count_over_time({{job="nyxgpt", logger="{logger_name}"}}[{hours}h]))'
                resp = client.get(
                    "/api/datasources/proxy/uid/loki/loki/api/v1/query",
                    params={"query": query},
                )
                if resp.status_code == 401:
                    return None, (
                        f"Grafana rejected the doctor service-account token at {token_path} "
                        "(401) -- delete the file and re-run `nyxgpt ops install` to mint a "
                        "fresh one"
                    )
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                result = resp.json().get("data", {}).get("result", [])
                volumes[name] = int(float(result[0]["value"][1])) if result else 0
    except Exception as e:
        logger.warning(
            "Failed to query Loki log volumes via %s, skipping: %s",
            grafana_ui_url,
            e,
            extra={"component": "ops"},
        )
        return None, None
    return volumes, None


def _foreign_native_service_issues(identity: InstallIdentity) -> list[str]:
    """Report api/web services registered by an install this machine did not record.

    The detection half of #3861. The owner's Mac carried four concurrent
    install identities -- two keg pairs and a LaunchAgent set -- two of them
    with `keep_alive` services bound to the same ports, and no command in the
    product could say so: `ops status` reported the *stable* names it
    expected, `doctor` reported a mode. Comparing what the managers actually
    have registered against the identity the marker records is what makes
    that state nameable.

    A machine whose marker predates identities has nothing to compare, so it
    is reported as that rather than as a clean bill of health.
    """
    registered = _discover_native_services()
    if not identity.known:
        if registered:
            names = ", ".join(sorted(f"{name} ({manager})" for manager, name in registered))
            return [
                "No install identity is recorded for the native api/web services, so nyxGPT "
                f"cannot say which build these registered services belong to: {names}. Re-run "
                "`nyxgpt up` (add --dev from a checkout) to reconcile them and record one."
            ]
        return []
    foreign = sorted(
        f"{name} ({manager})"
        for manager, name in registered
        if name not in set(identity.service_names)
    )
    if not foreign:
        return []
    return [
        "Native api/web services are registered that the recorded install does not own: "
        f"{', '.join(foreign)}. The recorded install is {identity.detail()}. Two installs "
        "registered on the same ports keep restarting into each other (#3853); re-run "
        "`nyxgpt up` (add --dev from a checkout) to retire the ones that are not this install's."
    ]


def _terraform_install_mode_issues() -> list[str]:
    """Print the Terraform deployment's install mode and return its issues (#3835).

    Separate from the native install mode `doctor` reports just above the
    call: it is a different deployment, installed independently, and
    frequently in the other mode -- stating one for the other is exactly the
    defect this marker was added to fix. Says nothing at all on a machine
    that has never deployed Terraform.

    A deployment that is running with no marker is reported as *unrecorded*
    rather than defaulted to artifact, which for Terraform states the reverse
    of the truth (every pre-#3835 deployment was built from a working tree).
    That is printed, not raised as an issue: an unrecorded build is an
    unknown, not a fault -- the stack is serving, and failing `doctor` (and
    with it `ops verify`) on every machine that deployed before the marker
    existed would report a healthy stack as broken.

    A marker with *nothing* deployed is reported as a record of the last
    Terraform install rather than described in the present tense (#3989):
    the marker alone is enough to reach this function, so a torn-down or
    failed install would otherwise have its recorded mode read as a
    description of containers that are not there.

    The one issue raised here is the Terraform twin of the native dev-mode
    check: a running dev-mode deployment whose checkout is gone is running
    images nothing can rebuild.
    """
    deployed = any(_container_deployed(state) for state in terraform_stack_state().values())
    if not (deployed or install_mode_file(SUBSTRATE_TERRAFORM).exists()):
        return []
    state = read_install_mode(substrate=SUBSTRATE_TERRAFORM)
    print(f"Install mode (terraform): {state.label(deployed=deployed)}")
    if not deployed:
        # Reached whenever a marker exists and nothing is up -- a torn-down
        # or failed Terraform install. Printed, never raised: a record of a
        # past install is not a fault. Said out loud because the line above
        # otherwise reads as a description of a running stack (#3989).
        print(
            "  No Terraform deployment on this machine -- that is a record of the "
            "last Terraform install, not a statement about whatever is serving now."
        )
        return []
    if not state.recorded:
        print(
            "  (nothing recorded what these containers were built from -- redeploy with "
            "`nyxgpt up --terraform`, or `--dev` for a working-tree build, to "
            "record it)"
        )
        return []
    if not state.is_dev:
        return []
    checkout = Path(state.checkout) if state.checkout else None
    if checkout is not None and checkout.is_dir():
        return []
    return [
        "Dev-mode Terraform deployment recorded, but its checkout is missing "
        f"({state.checkout or 'no path recorded'}) -- the running api/web images were "
        "built from a tree that is no longer there and cannot be rebuilt. Re-run "
        "`nyxgpt up --terraform --dev` from a checkout, or without --dev to "
        "deploy the published images."
    ]


def doctor(_args) -> int:
    """CLI entrypoint for `nyxgpt ops doctor`.

    Checks for common misconfigurations: missing ~/.nyxGPT/config.ini,
    non-executable helper scripts, missing native-service-manager/docker/
    node/npm tools on PATH (brew on macOS, systemctl on Linux),
    missing/incomplete web dependencies (node_modules, undici),
    (when log aggregation is enabled and native logs exist) whether
    promtail is actually wired to see native-mode host logs, (when tracing
    is enabled) whether the configured OTLP endpoint actually has something
    listening on it, (when monitoring is enabled and Prometheus is up)
    whether Prometheus's `nyxgpt-api` scrape target is actually up -- a
    failed scrape leaves every Grafana dashboard empty while the stack
    still looks healthy (#3721), (once the shared Ollama store has been configured)
    whether native Ollama's OLLAMA_MODELS env has drifted from it (#3431),
    and whether `~/.nyxGPT/secrets` is writable / holds the GlitchTip
    Grafana token when the observability stack is up (#3432), whether a
    configured error-tracking DSN's public key still matches a live
    GlitchTip project key -- a stale/re-minted key silently drops every
    event with no other visible symptom (#3565) -- and whether the
    installed environment actually has every dependency declared in
    `pyproject.toml` -- catches a venv that wasn't refreshed after a `git
    pull` added or bumped one (#3487).
    Also prints a per-logger recent log volume
    (last 24h, via Loki) when log aggregation
    and the monitoring stack are both up, so idle curated components aren't
    mistaken for a broken pipeline -- and, if that check's Grafana
    service-account token is missing or rejected, reports it as an issue
    rather than silently omitting the log volume line (#3438). Prints each
    issue found.

    Returns 0 if no issues were found, else 2.
    """
    logger.info("ops: doctor starting", extra={"component": "ops", "action": "doctor"})
    issues: list[str] = []

    # Stated before the checks below so a dev-mode diagnosis is never read as
    # a verdict on the artifact path (#3789), and so a dev install whose
    # checkout has been moved/deleted -- which leaves api/web running code
    # nothing can rebuild -- is reported rather than silently tolerated.
    #
    # Named as the *native* api/web's mode, with the Kubernetes and Terraform
    # deployments' own modes reported beside it when there are any (#3834,
    # #3835): they are separate installs and one line cannot speak for all of
    # them.
    install_mode = read_install_mode()
    print(f"Install mode (native api/web): {install_mode.label()}")
    k8s_install_mode = read_install_mode(substrate=SUBSTRATE_KUBERNETES)
    # Whether the checks below should be asking the cluster rather than this
    # host (#3987). Gated on the marker so a machine that has never deployed
    # to Kubernetes pays nothing for the question: `ops doctor` runs on every
    # native install, and a `kubectl get pods` against a configured-but-absent
    # cluster is a probe with a timeout attached. A marker left behind by a
    # torn-down deployment just yields no Pods, and the native reading stands.
    k8s = (
        _k8s_deployment_probe()
        if k8s_install_mode.recorded
        else K8sDeploymentProbe([], "no Kubernetes install recorded on this machine")
    )
    k8s_deployed = k8s.deployed
    if k8s_install_mode.recorded:
        print(f"Install mode (kubernetes): {k8s_install_mode.label()}")
        # Said out loud, next to the mode, so the checks below are read
        # against the right machine (#3987) -- doctor otherwise names a
        # Kubernetes install mode and then reports exclusively on this host.
        print(f"Kubernetes deployment: {k8s.summary}")
        print(
            "  Model readiness below is reported against the cluster, not this host."
            if k8s_deployed
            else "  The checks below report on this host."
        )
        if k8s_install_mode.is_dev:
            k8s_checkout = Path(k8s_install_mode.checkout) if k8s_install_mode.checkout else None
            if k8s_checkout is None or not k8s_checkout.is_dir():
                issues.append(
                    "Dev-mode Kubernetes deployment recorded, but its checkout is missing "
                    f"({k8s_install_mode.checkout or 'no path recorded'}) -- the images in "
                    "the cluster were built from a tree that is no longer there and cannot "
                    "be rebuilt. Re-run `nyxgpt ops install --kubernetes` (add "
                    "--dev from a checkout to stay on the working tree)."
                )
        issues.extend(_k8s_access_bridge_issues())
    if install_mode.is_dev:
        checkout = Path(install_mode.checkout) if install_mode.checkout else None
        if checkout is None or not checkout.is_dir():
            issues.append(
                "Dev-mode install recorded, but its checkout is missing "
                f"({install_mode.checkout or 'no path recorded'}) -- the api/web services "
                "point at a tree that is no longer there. Re-run `nyxgpt up --dev` from a "
                "checkout, or `nyxgpt up` to return to the artifact path."
            )
        elif not (checkout / "web" / "node_modules").is_dir():
            issues.append(
                f"Dev-mode web service has no dependencies installed ({checkout}/web/"
                "node_modules is missing) -- the Next dev server cannot start "
                "(run: nyxgpt up --dev)"
            )

    issues += _foreign_native_service_issues(install_mode.identity)
    issues += _terraform_install_mode_issues()

    cfg = Path.home() / ".nyxGPT" / "config.ini"
    if not cfg.exists():
        issues.append(f"Missing config {cfg}")

    volume_info: str | None = None
    cfg_parser: ConfigParser | None = None
    if cfg.exists():
        try:
            parsed = ConfigParser()
            parsed.read(cfg)
            cfg_parser = parsed
        except configparser.Error as e:
            # A doctor that only logs "Failed to parse <path>" and moves on is
            # not actionable: this is the single fault that takes the whole API
            # down (every request loads config.ini), and the user needs the
            # line to fix it (#3944). Report it as an issue, with the line.
            #
            # This is the one caller that opts into quoting the file's own text
            # (`include_line_text`), and it is the reason the default is off:
            # doctor is a local command, run by the owner of the file, printing
            # to their terminal. The API's rendering of the same fault is
            # redacted because it is reachable pre-auth, and it points here.
            issues.append(describe_config_parse_error(cfg, e, include_line_text=True))
            cfg_parser = None
        except Exception as e:
            logger.warning(
                "Failed to read %s, skipping config-dependent doctor checks: %s",
                cfg,
                e,
                extra={"component": "ops"},
            )
            cfg_parser = None
    if (
        cfg_parser is not None
        and get_log_aggregation_enabled(cfg_parser)
        and _compose_stack_snapshot().get("promtail") == "running"
    ):
        monitoring_config = get_monitoring_config(cfg_parser)
        volumes, loki_issue = _loki_recent_volume_by_logger(monitoring_config["grafana_ui_url"])
        if volumes is not None:
            volume_info = ", ".join(f"{k}={v}" for k, v in volumes.items())
        if loki_issue is not None:
            issues.append(loki_issue)

    for name in (
        "run-web.sh",
        "follow-cassandra-logs.sh",
        "follow-ollama-logs.sh",
        "set-ollama-models-env.sh",
    ):
        p = Path.home() / ".nyxGPT" / "scripts" / name
        if p.exists() and not os.access(p, os.X_OK):
            issues.append(f"Script not executable {p}")

    if _is_linux():
        required_native_tools: tuple[str, ...] = ("systemctl", "docker")
    elif _is_macos():
        required_native_tools = ("brew", "docker")
    else:
        required_native_tools = ("docker",)
    for tool in required_native_tools:
        if _which(tool) is None:
            issues.append(f"Missing tool in PATH: {tool}")

    if _which("docker") is not None:
        # Two different findings, kept apart (#4022). "Missing" is a claim
        # about the container; a denied Docker socket is a claim about this
        # session, and telling the operator to re-run `ops install` because a
        # read was refused sends them to fix a machine that is fine.
        cassandra_probe = _docker_container_probe(CASSANDRA_CONTAINER_NAME)
        if not cassandra_probe.known:
            user = getpass.getuser()
            issues.append(
                f"Cannot read Docker container state from this session ({cassandra_probe.reason}) "
                f"-- container status is unknown, not absent (run: sudo usermod -aG docker "
                f"{user}, then sudo loginctl terminate-user {user})"
            )
        elif cassandra_probe.state == "absent":
            issues.append(
                f"Missing local Cassandra container: {CASSANDRA_CONTAINER_NAME} "
                "(run: nyxgpt ops install)"
            )

    if _which("docker") is not None:
        restarting = sorted(
            service for service, state in _compose_stack_snapshot().items() if state == "restarting"
        )
        if restarting:
            issues.append(
                "Compose service(s) stuck in a restart/crash loop: "
                f"{', '.join(restarting)} (run: nyxgpt ops logs <service> to see why it's "
                "failing to start)"
            )

    web_dir = REPO_ROOT / "web"
    if web_dir.exists():

        def _can_resolve(pkg: str) -> bool:
            try:
                cp = subprocess.run(
                    ["node", "-p", f"require.resolve('{pkg}')"],
                    cwd=str(web_dir),
                    text=True,
                    capture_output=True,
                )
                return cp.returncode == 0
            except Exception as e:
                logger.warning(
                    "Failed to resolve node package %r, assuming missing: %s",
                    pkg,
                    e,
                    extra={"component": "ops"},
                )
                return False

        if _which("node") is None:
            issues.append("Missing tool in PATH: node")
        if _which("npm") is None:
            issues.append("Missing tool in PATH: npm")
        if not (web_dir / "node_modules").exists():
            issues.append(f"Missing web deps: {web_dir / 'node_modules'} (run: nyxgpt ops install)")
        elif not _can_resolve("undici"):
            issues.append("Missing web dependency: undici (run: nyxgpt ops install)")

    log_issue = _log_aggregation_wiring_issue()
    if log_issue:
        issues.append(log_issue)

    tracing_issue = _tracing_wiring_issue()
    if tracing_issue:
        issues.append(tracing_issue)

    tracing_packages_issue = _tracing_packages_doctor_issue()
    if tracing_packages_issue:
        issues.append(tracing_packages_issue)

    scrape_issue = _prometheus_api_scrape_issue()
    if scrape_issue:
        issues.append(scrape_issue)

    # The mirror image of the check above: not "the scrape is broken" but "the
    # scrape only works because the bind was widened by hand" (#3509/#3721).
    insecure_bind_issue = _insecure_api_bind_issue()
    if insecure_bind_issue:
        issues.append(insecure_bind_issue)

    if _is_macos():
        # Linux has no equivalent drift to check: nyxgpt-ollama.service's
        # Environment=OLLAMA_MODELS is part of the unit file itself (applied
        # fresh on every start), unlike launchd's per-session `launchctl
        # setenv` -- see _install_ollama_env_agent.
        ollama_env_issue = _ollama_env_drift_issue()
        if ollama_env_issue:
            issues.append(ollama_env_issue)

    linux_ollama_conflict_issue = _linux_ollama_port_conflict_issue()
    if linux_ollama_conflict_issue:
        issues.append(linux_ollama_conflict_issue)

    missing_models_issue = _missing_required_models_issue(kubernetes=k8s_deployed)
    if missing_models_issue:
        issues.append(missing_models_issue)

    docker_access_issue = _docker_access_doctor_issue()
    if docker_access_issue:
        issues.append(docker_access_issue)

    issues += _observability_volume_doctor_issues()
    issues += _glitchtip_secrets_doctor_issues()
    error_tracking_drift_issue = _error_tracking_dsn_drift_issue()
    if error_tracking_drift_issue:
        issues.append(error_tracking_drift_issue)
    issues += _stale_venv_doctor_issues()

    if (
        TERRAFORM_DIR.joinpath("terraform.tfstate").exists()
        and _terraform_state_has_resources()
        and all(state == "absent" for state in terraform_stack_state().values())
    ):
        issues.append(
            "Terraform state exists but no nyxgpt-tf-* containers are running "
            "(run: nyxgpt ops install --terraform, or nyxgpt ops down --terraform "
            "to clean up the stale state)"
        )

    try:
        dual_stack_conflicts = detect_deployment_mode().terraform_conflicts
    except Exception as e:  # never let dual-stack detection block the rest of doctor
        logger.warning(
            "ops: dual-stack detection failed, skipping: %s: %s",
            type(e).__name__,
            e,
            extra={"component": "ops", "action": "doctor"},
        )
        dual_stack_conflicts = []
    if dual_stack_conflicts:
        issues.append(
            ", ".join(sorted(dual_stack_conflicts))
            + " reported running under BOTH native/Compose and Terraform at once -- an "
            "incomplete mode switch left two core stacks up (run: nyxgpt ops down "
            "--terraform, or nyxgpt ops down, to drop the mode you don't want)"
        )

    if issues:
        print("nyxGPT ops doctor: FAIL")
        for i in issues:
            print(f"- {i}")
        if volume_info is not None:
            print(f"Log volume (last 24h) by logger: {volume_info}")
        logger.warning(
            "ops: doctor found %d issue(s): %s",
            len(issues),
            "; ".join(issues),
            extra={"component": "ops", "action": "doctor", "ok": False, "issues": issues},
        )
        return 2

    print("nyxGPT ops doctor: OK")
    if volume_info is not None:
        print(f"Log volume (last 24h) by logger: {volume_info}")
    logger.info(
        "ops: doctor found no issues",
        extra={"component": "ops", "action": "doctor", "ok": True, "issues": []},
    )
    return 0


def _clear_intentional_stops(components: list[str]) -> list[OpsResult]:
    """Clear the intentional-stop marker for `components` (they're being brought back up).

    Called from `install`/`restart` for whatever they bring up -- doing so is
    itself the "this is desired again" signal, so self-heal resumes guarding
    the component against future crashes (see self_heal.py's intentional-stop
    registry and its module docstring, #3406). A component never marked
    stopped is a no-op, so this is safe to call unconditionally.
    """
    for component in components:
        self_heal.clear_intentionally_stopped(component)
    return [OpsResult(True, f"Cleared intentional-stop marker(s): {', '.join(components)}")]


# --- Restart public API ---


def _compose_conflict_result(component: str, compose: dict[str, str]) -> OpsResult | None:
    """Return a refusal OpsResult if a Compose deployment of `component` is live."""
    if compose.get(component) != "running":
        return None
    port = COMPOSE_COMPONENT_PORTS.get(component)
    port_note = f" on port {port}" if port else ""
    return OpsResult(
        False,
        f"Refusing to restart local {component}: a Docker Compose deployment of "
        f"{component} is already running{port_note}",
        "Both deployments would try to bind the same port. Stop the Compose deployment "
        "of this component (or manage it there) before restarting the local one.",
    )


def _restart_component(component: str, compose: dict[str, str]) -> list[OpsResult]:
    """Restart a native (Homebrew/systemd) component, refusing first if Compose already runs it.

    Shared by `restart()`'s api/web/ollama steps (#3558) -- factored out of
    the old inline per-component `if` blocks so each component becomes a
    single named step for the live-progress step runner. Restarts via
    `_restart_native_service`, which dispatches to the OS-appropriate
    mechanism (brew on macOS, systemd on Linux).
    """
    conflict = _compose_conflict_result(component, compose)
    if conflict:
        return [conflict]
    self_heal.clear_intentionally_stopped(component)
    return _restart_native_service(component)


def _restart_cassandra_component(compose: dict[str, str]) -> list[OpsResult]:
    """Restart the Cassandra Docker container, refusing first if Compose already runs it.

    Cassandra restarts via `_restart_docker_container` rather than
    `_restart_brew_service` (it has no Homebrew service), so it needs its own
    step function alongside `_restart_component` (#3558).
    """
    conflict = _compose_conflict_result("cassandra", compose)
    if conflict:
        return [conflict]
    self_heal.clear_intentionally_stopped("cassandra")
    return _restart_docker_container("nyxgpt-cassandra")


def restart(args) -> int:
    """Restart operational components.

    target: all|api|web|ollama|cassandra|cassandra-logs|ollama-logs|observability

    Before touching a native component, checks whether a Docker Compose deployment
    of that same component is already live and, if so, refuses rather than starting
    a second process/container that would collide on the same port.

    Unlike `stop`/`down` (where `observability` is opt-in via its own target and
    excluded from `all`), `restart all` also restarts every currently running
    observability Compose service (monitoring/logging/tracing/errors profiles) --
    so a single wrapped command can bounce the whole local stack, core services
    plus dashboards, after a config change. Observability services that aren't
    enabled/running are skipped cleanly rather than started.
    """
    target = getattr(args, "target", "all") or "all"
    logger.info(
        "ops: restart starting (target=%s)",
        target,
        extra={"component": "ops", "action": "restart", "target": target},
    )

    compose = _compose_stack_snapshot()

    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = []
    if target in ("all", "api"):
        steps.append(("restart api", lambda: _restart_component("api", compose)))
    if target in ("all", "web"):
        steps.append(("restart web", lambda: _restart_component("web", compose)))
    if target in ("all", "ollama"):
        steps.append(("restart ollama", lambda: _restart_component("ollama", compose)))
    if target in ("all", "cassandra"):
        steps.append(("restart cassandra", lambda: _restart_cassandra_component(compose)))
    for follower in NATIVE_LOG_FOLLOWERS:
        if target in ("all", follower):
            steps.append(
                (
                    f"restart {follower} agent",
                    partial(_restart_native_log_follower, follower),
                )
            )
    if target in ("all", "observability"):
        steps.append(("restart observability stack", _restart_observability_stack))

    quiet = bool(getattr(args, "quiet", False))
    results, slow_steps = _run_steps("restart", steps, quiet=quiet)
    ok = all(r.ok for r in results)
    if not quiet:
        _print_slow_steps_summary(slow_steps)
    logger.info(
        "ops: restart %s (target=%s, %d/%d ok)",
        "succeeded" if ok else "failed",
        target,
        sum(1 for r in results if r.ok),
        len(results),
        extra={"component": "ops", "action": "restart", "target": target, "ok": ok},
    )
    result, message = _ops_action_outcome(results)
    _record_ops_action("restart", target, result, message)

    # A component that actually restarted has picked up whatever config.ini
    # says now, so its pending-restart notice is answered and must stop being
    # shown (#3806). Doing it here rather than only in the API's restart
    # endpoint is what makes `nyxgpt ops restart web` and the dashboard button
    # equivalent: either one clears the flag the other raised.
    if ok:
        cleared = ("api", "web", "ollama", "cassandra") if target == "all" else (target,)
        for component in cleared:
            restart_state.clear_pending(component)

    return 0 if ok else 2


# --- Stop/down helpers ---


def _force_deregister_brew_service(name: str) -> list[OpsResult]:
    """Do by hand the two things a successful `brew services stop` does.

    Unload the job from the GUI domain and delete the LaunchAgent plist brew
    copied into ~/Library/LaunchAgents, in that order -- booting a job out
    without removing its plist reinstates it at the next login
    (`_remove_launchagents` records the same ordering rule).

    Only ever called after a stop that did not de-register (see
    `_stop_brew_service`), and only on macOS: brew services on Linux drive
    systemd units, where `launchctl` and ~/Library/LaunchAgents do not exist.
    """
    if not _is_macos():
        return []
    _, plist = _brew_service_registration(name)
    if plist is None:
        # brew named no file (it reports none for an unregistered service),
        # so fall back to the label brew has used for its service plists.
        plist = _launchagents_dir() / f"homebrew.mxcl.{name}.plist"
    results = _stop_launchagent(plist.stem)
    if plist.exists():
        try:
            plist.unlink()
            results.append(OpsResult(True, f"Removed brew service plist: {name}", str(plist)))
        except OSError as e:
            results.append(
                OpsResult(
                    False,
                    f"Failed to remove brew service plist: {name}",
                    f"{plist}: {type(e).__name__}: {e}",
                )
            )
    return results


def _stop_brew_service(name: str) -> list[OpsResult]:
    """Stop Homebrew service `name` and verify it is really de-registered.

    Not `brew services stop` alone, and not its exit code. What the exit code
    reports is that brew ran, not that launchd forgot the service: brew exits
    0 for a service that is registered but not currently running -- the
    `error` state a crash-looping keg sits in, which is exactly the state the
    owner's Mac was in (#3853) -- and the exit code says nothing either way
    about whether the LaunchAgent plist survived. Trusting it is what made
    #3861's reconcile print `Stopped brew service: nyxgpt-api` on a machine
    the step then read back as still registered (macos-brew-smoke run
    32222041921, the evidence job for this very fix).

    So the stop is *checked* -- against launchd, per
    `_brew_service_is_registered` -- and escalated to
    `_force_deregister_brew_service` when it did not take, and a service still
    registered after that is reported as a failure rather than as a stop.

    On every run this project has measured since the observation layer was
    fixed, plain `brew services stop` **has** de-registered the `error`-state
    service and the escalation has not fired (runs 32229751239 and
    32233162053, four stops each across both reconcile directions and the
    teardown). The escalation is
    therefore a guard against a state that has not been observed here, not a
    path known to be needed; the *check* is what is load-bearing, because
    everything downstream -- the identity reconcile, `ops stop`, teardown --
    acts on the claim that nothing will start this service again, and that
    claim has to be verified or say it is not.
    """
    if _which("brew") is None:
        return [OpsResult(False, f"brew not found; cannot stop {name}")]
    try:
        cp = _run(["brew", "services", "stop", _brew_formula_spec(name)], check=False)
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to stop brew service: {name}",
                f"{type(e).__name__}: {e}",
            )
        ]

    results: list[OpsResult] = []
    if cp.returncode != 0:
        results.append(
            OpsResult(False, f"Failed to stop brew service: {name}", _output_excerpt(cp).strip())
        )
    if not _brew_service_is_registered(name):
        if cp.returncode == 0:
            results.append(OpsResult(True, f"Stopped brew service: {name}"))
        return results

    results.extend(_force_deregister_brew_service(name))
    if _brew_service_is_registered(name):
        results.append(
            OpsResult(
                False,
                f"Brew service is still registered after stopping it: {name}",
                "`brew services stop` exited without de-registering it, and unloading it by "
                "hand did not either -- launchd will start it again at the next login. "
                f"Check `brew services list` and ~/Library/LaunchAgents for {name}.",
            )
        )
    else:
        results.append(
            OpsResult(
                True,
                f"Stopped and de-registered brew service: {name}",
                "`brew services stop` left it registered (the state a crashed service sits "
                "in); unloaded the job and removed its LaunchAgent plist.",
            )
        )
    return results


def _stop_docker_container(name: str) -> list[OpsResult]:
    """Stop Docker container `name` via `docker stop` (container is preserved, not removed)."""
    if _which("docker") is None:
        return [OpsResult(False, f"docker not found; cannot stop {name}")]
    try:
        cp = _run(["docker", "stop", name], check=False)
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to stop docker container: {name}",
                f"{type(e).__name__}: {e}",
            )
        ]
    if cp.returncode == 0:
        return [OpsResult(True, f"Stopped docker container: {name}")]
    details = _output_excerpt(cp)
    return [OpsResult(False, f"Failed to stop docker container: {name}", details.strip())]


def _stop_launchagent(label: str) -> list[OpsResult]:
    """Unload the LaunchAgent `label` via `launchctl bootout` in the current GUI domain.

    Unlike `_restart_launchagent`'s kickstart, `bootout` actually unloads the
    agent so it stops for good instead of being immediately relaunched by
    launchd. A "not loaded" failure (already stopped) is reported as success.
    """
    domain = f"gui/{os.getuid()}/{label}"
    try:
        cp = _run(["launchctl", "bootout", domain], check=False, expected=True)
        details = _output_excerpt(cp)
        if cp.returncode == 0:
            return [OpsResult(True, f"Stopped LaunchAgent: {label}")]
        if "no such process" in details.lower() or "could not find" in details.lower():
            return [OpsResult(True, f"LaunchAgent already stopped: {label}")]
        return [OpsResult(False, f"Failed to stop LaunchAgent: {label}", details.strip())]
    except Exception as e:
        return [
            OpsResult(
                False,
                f"Failed to stop LaunchAgent: {label}",
                f"{type(e).__name__}: {e}",
            )
        ]


def _compose_stop_service(service: str) -> list[OpsResult]:
    """Stop (but don't remove) a single running Compose service: `docker compose stop`."""
    if _which("docker") is None:
        return [OpsResult(False, f"docker not found; cannot stop compose service: {service}")]
    cp = _run(
        ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "stop", service], check=False
    )
    if cp.returncode == 0:
        return [OpsResult(True, f"Stopped Compose service: {service}")]
    details = _output_excerpt(cp)
    return [OpsResult(False, f"Failed to stop Compose service: {service}", details.strip())]


def _resolve_observability_services() -> tuple[list[str] | None, OpsResult | None]:
    """Resolve the Compose service names for the observability profiles.

    Mirrors `_start_observability_stack`'s own resolution (profiles minus
    `CORE_APP_SERVICES`) so `stop`/`down` agree with `install`/`observability`
    on what "observability" means. Returns `(services, None)` on success or
    `(None, failure_result)` if the services list couldn't be resolved.
    """
    base_cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE)]
    for profile in OBSERVABILITY_PROFILES:
        base_cmd += ["--profile", profile]
    cp = _run(base_cmd + ["config", "--services"], check=False)
    if cp.returncode != 0:
        return None, OpsResult(
            False,
            "Failed to resolve observability services",
            _output_excerpt(cp),
        )
    services = sorted({s.strip() for s in cp.stdout.splitlines() if s.strip()} - CORE_APP_SERVICES)
    return services, None


def _resolve_app_services() -> tuple[list[str] | None, OpsResult | None]:
    """Resolve the Compose service names for the core app tier (`CORE_APP_SERVICES`).

    Returns `(services, None)` on success or `(None, failure_result)` if the
    services list couldn't be resolved.
    """
    cp = _run(
        ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "config", "--services"],
        check=False,
    )
    if cp.returncode != 0:
        return None, OpsResult(
            False,
            "Failed to resolve app services",
            _output_excerpt(cp),
        )
    services = sorted({s.strip() for s in cp.stdout.splitlines() if s.strip()} & CORE_APP_SERVICES)
    return services, None


def _stop_observability_stack() -> list[OpsResult]:
    """Stop (but don't remove) every running observability Compose service."""
    if not _compose_available():
        return [OpsResult(True, "Skipped observability stack (Docker not found)")]

    services, err = _resolve_observability_services()
    if err is not None:
        return [err]
    if not services:
        return [OpsResult(True, "No observability services resolved to stop")]

    cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "stop"] + services
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return [
            OpsResult(
                False,
                "Failed to stop observability stack",
                _output_excerpt(cp),
            )
        ]
    return [OpsResult(True, "Observability stack stopped (containers and data preserved)")]


def _restart_observability_stack() -> list[OpsResult]:
    """Restart every currently running observability Compose service.

    Resolves the monitoring/logging/tracing/errors profile services --
    the same set `_start_observability_stack`/`_stop_observability_stack` use --
    and restarts only the ones actually running. A profile service that isn't
    enabled/up is reported as skipped rather than started, since `restart`
    should bounce what's live, not change which services are enabled (that's
    `nyxgpt ops observability`'s job).
    """
    if not _compose_available():
        return [OpsResult(True, "Skipped observability stack (Docker not found)")]

    services, err = _resolve_observability_services()
    if err is not None:
        return [err]
    if not services:
        return [OpsResult(True, "No observability services resolved to restart")]

    running = _compose_stack_snapshot()
    to_restart = [s for s in services if running.get(s) == "running"]
    skipped = [s for s in services if s not in to_restart]

    results: list[OpsResult] = [
        OpsResult(True, f"Observability service not running (skipped): {s}") for s in skipped
    ]

    if not to_restart:
        results.append(OpsResult(True, "No running observability services to restart"))
        return results

    cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "restart"] + to_restart
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        results.append(
            OpsResult(
                False,
                "Failed to restart observability stack",
                _output_excerpt(cp),
            )
        )
        return results

    results.append(OpsResult(True, f"Restarted observability services: {', '.join(to_restart)}"))
    return results


# Compose service name -> volume_dir() component(s) it bind-mounts (see
# VOLUME_DIR_NAMES). `_compose_down` uses this to actually delete data when
# `--volumes` is passed: `docker compose down --volumes` only removes
# Docker-managed named volumes, and since #3346 these are plain host bind
# mounts, so that flag alone is a silent no-op against the data it used to
# delete. glitchtip/glitchtip-worker share one uploads directory.
SERVICE_VOLUME_DIRS: dict[str, list[str]] = {
    "ollama": ["ollama"],
    "cassandra": ["cassandra"],
    "api": ["nyxgpt-data"],
    "prometheus": ["prometheus"],
    "grafana": ["grafana"],
    "loki": ["loki"],
    "glitchtip-postgres": ["glitchtip-postgres"],
    "glitchtip": ["glitchtip-uploads"],
    "glitchtip-worker": ["glitchtip-uploads"],
}


def _remove_volume_dirs(services: list[str]) -> list[OpsResult]:
    """Delete the host bind-mount directories owned by `services` (see `SERVICE_VOLUME_DIRS`).

    Called only when `docker compose down --volumes` is requested -- that
    flag no longer touches bind-mounted data itself (see `_compose_down`), so
    this is what actually makes `--volumes` destructive again.
    """
    dirs = sorted({d for s in services for d in SERVICE_VOLUME_DIRS.get(s, [])})
    if not dirs:
        return []
    results: list[OpsResult] = []
    for name in dirs:
        path = volume_dir(name)
        try:
            shutil.rmtree(path)
            results.append(OpsResult(True, f"Removed data directory: {path}"))
        except Exception as e:
            results.append(
                OpsResult(
                    False, f"Failed to remove data directory: {path}", f"{type(e).__name__}: {e}"
                )
            )
    return results


def _compose_down(services: list[str], *, volumes: bool) -> list[OpsResult]:
    """Tear down the given Compose `services` via `docker compose down`.

    Removes containers/networks for exactly the listed services; `volumes`
    additionally deletes their ~/.nyxGPT/volumes/ bind-mount directories
    (destructive -- data loss; see `_remove_volume_dirs`).
    """
    if not _compose_available():
        return [OpsResult(True, "Skipped Compose teardown (Docker not found)")]
    if not services:
        return [OpsResult(True, "No Compose services to tear down for this scope")]

    cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "down"] + services
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return [
            OpsResult(
                False,
                "Failed to tear down Compose services",
                _output_excerpt(cp),
            )
        ]
    suffix = " and their data directories" if volumes else " (data directories preserved)"
    results = [OpsResult(True, f"Removed Compose containers{suffix}: {', '.join(services)}")]
    if volumes:
        results += _remove_volume_dirs(services)
    return results


def _stop_dual_mode(
    component: str,
    native_stop: Callable[[], list[OpsResult]],
    mode: DeploymentMode,
    tf_or_k8s: Container[str],
) -> list[OpsResult]:
    """Stop `component` in whichever mode(s) it's actually running -- native, Compose, or both.

    Factored out of `stop()`'s old inline per-component closure (#3558) so
    each component becomes a single named step for the live-progress step
    runner. Marks the component intentionally stopped first (unless it's
    Terraform/Kubernetes-managed, which this call never touches) so self-heal
    doesn't immediately restart what this stops.
    """
    if component in tf_or_k8s:
        return [
            OpsResult(
                True,
                f"{component}: running under Terraform/Kubernetes, not native/Compose -- "
                "left alone (self-heal still guards it; use --terraform/--kubernetes to "
                "tear it down)",
            )
        ]
    results: list[OpsResult] = []
    try:
        self_heal.mark_intentionally_stopped(component)
    except Exception as e:  # never let self-heal bookkeeping block a stop
        results.append(
            OpsResult(
                True,
                f"Could not mark {component} as intentionally stopped "
                f"({type(e).__name__}: {e})",
            )
        )
    native_running = mode.native.get(component) in ("started", "running")
    compose_running = mode.compose.get(component) == "running"
    if not native_running and not compose_running:
        results.append(OpsResult(True, f"{component}: already stopped"))
        return results
    if native_running and compose_running:
        results.append(
            OpsResult(
                True,
                f"{component}: running in BOTH native and Compose (mixed mode) -- stopping both",
            )
        )
    if native_running:
        results.extend(native_stop())
    if compose_running:
        results.extend(_compose_stop_service(component))
    return results


def stop(args) -> int:
    """Stop operational components.

    target: all|api|web|ollama|cassandra|cassandra-logs|ollama-logs|observability

    For components that can run either natively or under Docker Compose
    (api/web/ollama/cassandra), detects which mode is actually running and
    stops the right one -- if both are live (mixed mode), stops both and
    reports it clearly. Does not delete data volumes or remove containers
    (Compose services are stopped, not brought down).

    The log followers (`NATIVE_LOG_FOLLOWERS`) are native-only and are all
    included in `all`; `ollama-logs` is selectable in its own right, so
    stopping the ollama watcher no longer means reaching for `launchctl
    bootout` (#4033).

    `observability` has no native equivalent, so -- like `restart` -- it is
    not included in `all`; select it explicitly to stop the Grafana/Loki/
    Jaeger/GlitchTip Compose profiles.
    """
    target = getattr(args, "target", "all") or "all"
    logger.info(
        "ops: stop starting (target=%s)",
        target,
        extra={"component": "ops", "action": "stop", "target": target},
    )

    mode = detect_deployment_mode()
    tf_or_k8s = _terraform_or_kubernetes_managed_components()

    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = []
    if target in ("all", "api"):
        steps.append(
            (
                "stop api",
                lambda: _stop_dual_mode(
                    "api", lambda: _stop_native_service("api"), mode, tf_or_k8s
                ),
            )
        )
    if target in ("all", "web"):
        steps.append(
            (
                "stop web",
                lambda: _stop_dual_mode(
                    "web", lambda: _stop_native_service("web"), mode, tf_or_k8s
                ),
            )
        )
    if target in ("all", "ollama"):
        steps.append(
            (
                "stop ollama",
                lambda: _stop_dual_mode(
                    "ollama", lambda: _stop_native_service("ollama"), mode, tf_or_k8s
                ),
            )
        )
    if target in ("all", "cassandra"):
        steps.append(
            (
                "stop cassandra",
                lambda: _stop_dual_mode(
                    "cassandra",
                    lambda: _stop_docker_container("nyxgpt-cassandra"),
                    mode,
                    tf_or_k8s,
                ),
            )
        )
    for follower in NATIVE_LOG_FOLLOWERS:
        if target in ("all", follower):
            steps.append(
                (
                    f"stop {follower} agent",
                    partial(_stop_native_log_follower, follower),
                )
            )
    if target == "observability":
        # Not part of "all" -- like `restart`, "all" only covers the core
        # api/web/ollama/cassandra components plus the log followers;
        # observability has no native equivalent and is opt-in via its own
        # target/command.
        steps.append(("stop observability stack", _stop_observability_stack))

    quiet = bool(getattr(args, "quiet", False))
    results, slow_steps = _run_steps("stop", steps, quiet=quiet)
    ok = all(r.ok for r in results)
    if not quiet:
        _print_slow_steps_summary(slow_steps)
    logger.info(
        "ops: stop %s (target=%s, %d/%d ok)",
        "succeeded" if ok else "failed",
        target,
        sum(1 for r in results if r.ok),
        len(results),
        extra={"component": "ops", "action": "stop", "target": target, "ok": ok},
    )
    result, message = _ops_action_outcome(results)
    _record_ops_action("stop", target, result, message)

    return 0 if ok else 2


def _down_mark_intentional_stops() -> list[OpsResult]:
    """Mark api/web/ollama/cassandra intentionally stopped before `down` tears them down.

    Extracted from `down()`'s inline prelude (#3558) into its own named step
    for the live-progress step runner -- see `down()`'s own docstring/comment
    for why this has to happen before any component is actually stopped.
    """
    tf_or_k8s = _terraform_or_kubernetes_managed_components()
    results: list[OpsResult] = []
    try:
        marked = [
            component
            for component in ("api", "web", "ollama", "cassandra")
            if component not in tf_or_k8s
        ]
        for component in marked:
            self_heal.mark_intentionally_stopped(component)
        if marked:
            results.append(
                OpsResult(
                    True,
                    f"Marked {', '.join(marked)} as intentionally stopped "
                    "(self-heal will leave them alone until brought back up)",
                )
            )
        skipped = [c for c in ("api", "web", "ollama", "cassandra") if c in tf_or_k8s]
        if skipped:
            results.append(
                OpsResult(
                    True,
                    f"Left {', '.join(skipped)} alone -- running under Terraform/"
                    "Kubernetes, not native/Compose (self-heal still guards them; "
                    "use --terraform/--kubernetes to tear those down)",
                )
            )
    except Exception as e:  # never let self-heal bookkeeping block a teardown
        results.append(
            OpsResult(
                True,
                f"Could not mark components as intentionally stopped ({type(e).__name__}: {e})",
            )
        )
    return results


def _down_compose_teardown(scope: str, volumes: bool) -> list[OpsResult]:
    """Tear down the Compose app/observability tiers for `down`'s `scope`.

    Extracted from `down()`'s inline Compose block (#3558) into its own
    named step for the live-progress step runner. Runs for every scope
    (including `--app-only`/`--observability-only`) since it's the single
    place that resolves and tears down whichever Compose services the scope
    selects.
    """
    if not _compose_available():
        return [OpsResult(True, "Skipped Compose teardown (Docker not found)")]

    compose_services: list[str] = []
    results: list[OpsResult] = []
    if scope in ("all", "app"):
        app_services, err = _resolve_app_services()
        if err is not None:
            results.append(err)
        elif app_services:
            compose_services += app_services

    if scope in ("all", "observability"):
        obs_services, err = _resolve_observability_services()
        if err is not None:
            results.append(err)
        elif obs_services:
            compose_services += obs_services

    if compose_services:
        results.extend(_compose_down(compose_services, volumes=volumes))
    else:
        results.append(OpsResult(True, "No Compose services running for this scope"))
    return results


def down(args) -> int:
    """Tear down the local stack: native services plus the Compose app/observability tiers.

    Native services (api/web/ollama/cassandra plus every log follower in
    `NATIVE_LOG_FOLLOWERS`) are stopped;
    Compose containers for the selected scope are removed via `docker
    compose down` (networks too, volumes preserved unless `--volumes` is
    also given). `--app-only`/`--observability-only` scope the teardown to
    one tier so, e.g., a stale Compose app tier can be dropped while
    observability (or vice versa) stays up.

    `--terraform`/`--kubernetes` tear down those deployments instead
    (`terraform destroy` / removing the `nyxgpt` namespace's resources) --
    mutually exclusive with the native/Compose scope flags above.
    """
    if getattr(args, "terraform", False) and getattr(args, "kubernetes", False):
        print("ERROR: --terraform and --kubernetes are mutually exclusive", file=sys.stderr)
        return 2
    if getattr(args, "terraform", False):
        return _down_terraform(args)
    if getattr(args, "kubernetes", False):
        return _down_kubernetes(args)

    app_only = bool(getattr(args, "app_only", False))
    observability_only = bool(getattr(args, "observability_only", False))
    volumes = bool(getattr(args, "volumes", False))
    yes_really = bool(getattr(args, "yes_really", False))

    if volumes and not yes_really:
        print(
            "ERROR: refusing to remove volumes without --yes-really "
            "(this deletes Cassandra/Postgres/Grafana data)",
            file=sys.stderr,
        )
        return 2

    scope = "observability" if observability_only else ("app" if app_only else "all")
    logger.info(
        "ops: down starting (scope=%s, volumes=%s)",
        scope,
        volumes,
        extra={"component": "ops", "action": "down", "scope": scope, "volumes": volumes},
    )

    # Mark api/web/ollama/cassandra as intentionally stopped BEFORE stopping
    # anything. Otherwise the next heal pass sees them as unhealthy and
    # restarts them (`brew services restart` / `docker restart`), fighting
    # the teardown -- the ports get re-occupied within seconds, which then
    # blocks `nyxgpt ops install --terraform --local` with a spurious port
    # collision. This is per-component (self_heal.py's intentional-stop
    # registry, #3406), not a global watchdog disable: an armed watchdog
    # keeps healing genuine crashes of every other component the whole
    # time. `nyxgpt ops install`/`restart`/`up` of a component clears its
    # marker automatically.
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = []
    if scope in ("all", "app"):
        steps.append(("mark intentionally stopped", _down_mark_intentional_stops))
        steps.append(("stop api", lambda: _stop_native_service("api")))
        steps.append(("stop web", lambda: _stop_native_service("web")))
        steps.append(("stop ollama", lambda: _stop_native_service("ollama")))
        steps.append(
            ("stop cassandra container", lambda: _stop_docker_container("nyxgpt-cassandra"))
        )
        for follower in NATIVE_LOG_FOLLOWERS:
            steps.append(
                (
                    f"stop {follower} agent",
                    partial(_stop_native_log_follower, follower),
                )
            )
    steps.append(("compose teardown", lambda: _down_compose_teardown(scope, volumes)))

    quiet = bool(getattr(args, "quiet", False))
    results, slow_steps = _run_steps("down", steps, quiet=quiet)
    ok = all(r.ok for r in results)
    if not quiet:
        _print_slow_steps_summary(slow_steps)
    logger.info(
        "ops: down %s (scope=%s, volumes=%s, %d/%d ok)",
        "succeeded" if ok else "failed",
        scope,
        volumes,
        sum(1 for r in results if r.ok),
        len(results),
        extra={"component": "ops", "action": "down", "scope": scope, "ok": ok},
    )
    result, message = _ops_action_outcome(results)
    _record_ops_action("down", scope, result, message)

    return 0 if ok else 2


# --- Uninstall teardown (#3859) ---
#
# `nyxgpt ops down` stops what is *running*; it deliberately leaves the
# machine installed, so every agent and service is registered to come back at
# the next login. That is right for "stop the stack" and wrong for "I am
# removing nyxGPT", and the gap is not cosmetic: `brew uninstall` deletes a
# keg's files without stopping its service first, and a running process
# survives deletion of its executable. The owner's macOS teardown left
# `nyxgpt-api`/`nyxgpt-web` serving on :8000/:3000 out of kegs whose 6,117 and
# 48,824 files had just been removed -- and, the tap being gone, brew could no
# longer resolve the formula names to stop them.
#
# Three populations, only one of which Homebrew has ever known about:
#
#   brew services   `homebrew.mxcl.nyxgpt-api@X.Y.Zrc` and its web twin.
#                   Stopped through `brew services stop` where brew can still
#                   resolve the formula, and through launchd directly where it
#                   cannot -- which is the state an operator reaches by
#                   untapping first, and the state that had no recovery path.
#   nyxGPT agents   `com.nyxgpt.*` -- the dev-mode api/web pair plus the
#                   unconditionally-installed log/env agents. `nyxgpt ops
#                   install` wrote these itself, so no `brew uninstall` could
#                   ever have removed them.
#   containers      `nyxgpt-cassandra` plus the observability Compose tier,
#                   torn down by the same steps `down` uses.
#
# Removal, not just unloading, for everything with a plist or unit file on
# disk: launchd and systemd --user both reinstate a registered job at the next
# login.

_BREW_SERVICE_LABEL_PREFIX = "homebrew.mxcl."


def _loaded_launchd_labels(prefix: str) -> list[str]:
    """Every currently-loaded launchd label starting with `prefix`.

    The plural of `_launchd_agent_loaded`, and same contract: any failure to
    read launchd is "nothing known to be loaded" rather than an exception,
    because both callers (the uninstall teardown and the install-time orphan
    report) must still do their real work on a machine whose launchctl cannot
    be queried.
    """
    if _which("launchctl") is None:
        return []
    try:
        cp = _run(["launchctl", "list"], check=False, expected=True)
    except Exception as e:
        logger.warning(
            "Could not query launchctl list for %s*: %s", prefix, e, extra={"component": "ops"}
        )
        return []
    if cp.returncode != 0:
        return []
    labels = []
    for line in (cp.stdout or "").splitlines():
        parts = line.split()
        if parts and parts[-1].startswith(prefix):
            labels.append(parts[-1])
    return labels


def _brew_service_launchd_labels() -> list[str]:
    """Every `homebrew.mxcl.nyxgpt*` launchd label this machine still carries.

    The union of what is on disk and what is loaded, because either outlives
    the other: `brew services stop` removes the plist while an already-booted
    job keeps running under a label with no file behind it, and a plist that
    was never bootstrapped is loaded by nothing until the next login. Matched
    by prefix rather than against a known formula list on purpose -- the
    candidate channel's services are named after their formula
    (`nyxgpt-api@3.0.0rc`), and a release line this build has never heard of
    is exactly the leftover a teardown is for.
    """
    labels: set[str] = set()
    try:
        for plist in _launchagents_dir().glob(f"{_BREW_SERVICE_LABEL_PREFIX}nyxgpt*.plist"):
            labels.add(plist.name[: -len(".plist")])
    except OSError as e:
        logger.warning("Could not list %s: %s", _launchagents_dir(), e, extra={"component": "ops"})
    labels.update(_loaded_launchd_labels(f"{_BREW_SERVICE_LABEL_PREFIX}nyxgpt"))
    return sorted(labels)


def _remove_brew_service_launchd_jobs() -> list[OpsResult]:
    """Stop and deregister every Homebrew-managed nyxgpt service (macOS).

    `brew services stop` first, so Homebrew's own state stays consistent when
    it can still resolve the formula -- then `launchctl bootout` and the plist
    unlink regardless, which is the half that works after `brew untap` has
    made the formula unresolvable. Failing to stop through brew is tolerated
    and unreported: an orphaned job is the normal case here, not an error.
    """
    labels = _brew_service_launchd_labels()
    if not labels:
        return [OpsResult(True, "No Homebrew-managed nyxgpt services left registered with launchd")]

    brew = _which("brew")
    la_dir = _launchagents_dir()
    results: list[OpsResult] = []
    for label in labels:
        formula = label[len(_BREW_SERVICE_LABEL_PREFIX) :]
        if brew is not None:
            cp = _run(
                ["brew", "services", "stop", _brew_formula_spec(formula)],
                check=False,
                expected=True,
            )
            if cp.returncode == 0:
                results.append(OpsResult(True, f"Stopped brew service: {formula}"))
        results.extend(_stop_launchagent(label))
        plist = la_dir / f"{label}.plist"
        if plist.exists():
            try:
                plist.unlink()
                results.append(
                    OpsResult(True, f"Removed brew service LaunchAgent for {formula}", str(plist))
                )
            except OSError as e:
                results.append(
                    OpsResult(
                        False,
                        f"Failed to remove brew service LaunchAgent for {formula}",
                        f"{type(e).__name__}: {e}",
                    )
                )
    return results


def _remove_native_systemd_units() -> list[OpsResult]:
    """Stop, disable and delete every nyxgpt systemd --user unit (Linux).

    The Linux twin of the two macOS removal paths: `disable --now` both stops
    the unit and drops the login-time symlink, and the unit file is then
    deleted so a `daemon-reload` leaves nothing to re-enable. Unit names come
    from the same maps the installers write from, plus whatever
    `nyxgpt-*.service` files are actually in `~/.config/systemd/user` -- so a
    unit installed by an older nyxGPT that this build no longer knows about
    is still removed rather than left running.
    """
    unit_dir = _systemd_user_dir()
    units = set(NATIVE_SYSTEMD_SERVICES.values()) | set(SUPPORT_SYSTEMD_UNITS.values())
    try:
        units.update(path.name[: -len(".service")] for path in unit_dir.glob("nyxgpt-*.service"))
    except OSError as e:
        logger.warning("Could not list %s: %s", unit_dir, e, extra={"component": "ops"})

    systemctl = _which("systemctl")
    results: list[OpsResult] = []
    for unit in sorted(units):
        path = unit_dir / f"{unit}.service"
        if systemctl is not None:
            _run(
                ["systemctl", "--user", "disable", "--now", f"{unit}.service"],
                check=False,
                expected=True,
            )
        if not path.exists():
            continue
        try:
            path.unlink()
            results.append(OpsResult(True, f"Removed systemd unit: {unit}", str(path)))
        except OSError as e:
            results.append(
                OpsResult(
                    False, f"Failed to remove systemd unit: {unit}", f"{type(e).__name__}: {e}"
                )
            )
    if systemctl is not None:
        _run(["systemctl", "--user", "daemon-reload"], check=False, expected=True)
    if not results:
        results.append(OpsResult(True, "No nyxgpt systemd --user units left installed"))
    return results


def _brew_formula_installed(name: str) -> bool:
    """True if Homebrew reports formula `name` as installed on this machine."""
    if _which("brew") is None:
        return False
    try:
        return (
            _run(
                ["brew", "list", "--formula", _brew_formula_spec(name)],
                check=False,
                expected=True,
            ).returncode
            == 0
        )
    except Exception as e:
        logger.warning("Could not ask brew about %s: %s", name, e, extra={"component": "ops"})
        return False


def _uninstall_stop_native_service(component: str) -> list[OpsResult]:
    """Stop `component` for the teardown, skipping one that was never installed.

    `down` reports a stop it could not perform, and should: the operator asked
    to stop a running stack, so `brew services stop ollama` failing is news.
    Uninstall is the opposite -- it runs against machines where whole
    populations are already gone (a keg removed by hand, ollama never
    installed, the candidate channel's service registered under a formula name
    this map does not carry), and "it is not there" is the desired end state,
    not a reason to exit 2. What is genuinely still registered is found and
    removed by `_uninstall_native_service_managers` regardless of this step.
    """
    if _is_macos() and _dev_launchd_label(component) is None:
        # Resolved rather than indexed, so the candidate channel's keg is
        # recognized as installed instead of being skipped as "no
        # nyxgpt-api keg" and left registered by the teardown (#3853).
        name = _resolved_brew_service(component)
        if not _brew_formula_installed(name):
            return [OpsResult(True, f"Skipped stopping {component}: no {name} keg installed")]
    if _is_linux():
        unit = NATIVE_SYSTEMD_SERVICES[component]
        if not (_systemd_user_dir() / f"{unit}.service").exists():
            return [OpsResult(True, f"Skipped stopping {component}: no {unit}.service installed")]
    return _stop_native_service(component)


def _uninstall_stop_container(name: str) -> list[OpsResult]:
    """Stop container `name` for the teardown, skipping one that is not there.

    Same rule as `_uninstall_stop_native_service`: a machine with no Docker,
    or with the container already removed, is in the state this command is
    trying to reach.
    """
    if _which("docker") is None:
        return [OpsResult(True, f"Skipped stopping {name}: Docker not found")]
    try:
        cp = _run(["docker", "ps", "-aq", "--filter", f"name=^{name}$"], check=False, expected=True)
    except Exception as e:
        logger.warning("Could not list containers for %s: %s", name, e, extra={"component": "ops"})
        return [OpsResult(True, f"Skipped stopping {name}: could not query Docker")]
    if cp.returncode != 0 or not (cp.stdout or "").strip():
        return [OpsResult(True, f"Skipped stopping {name}: no such container")]
    return _stop_docker_container(name)


def _uninstall_compose_teardown(volumes: bool) -> list[OpsResult]:
    """Tear the Compose tiers down, skipping a machine that has no Compose file.

    `_down_compose_teardown` resolves service names by asking `docker compose
    -f <file> config --services`, which fails outright when the ops-managed
    docker-compose.yml is not there. That is an uninstall's *end* state, and on
    a machine that only ever ran the native path it is the starting one -- so
    reporting it as two failed checks would exit 2 on exactly the machines this
    command has just finished cleaning.
    """
    compose_file = Path(self_heal.COMPOSE_FILE)
    if not compose_file.exists():
        return [OpsResult(True, f"Skipped Compose teardown: no {compose_file}")]
    return _down_compose_teardown("all", volumes)


def _uninstall_native_service_managers() -> list[OpsResult]:
    """Deregister nyxGPT from this OS's service manager, whichever it is."""
    if _is_macos():
        results = _remove_dev_launchagents()
        results.extend(_remove_support_launchagents())
        results.extend(_remove_brew_service_launchd_jobs())
        return results
    if _is_linux():
        return _remove_native_systemd_units()
    return _unsupported_os_result("native service teardown")


def _uninstall_clear_install_mode() -> list[OpsResult]:
    """Drop the native install-mode marker: the deployment it described is gone.

    Same reason `ops down --kubernetes` clears its own marker -- a marker left
    behind is a record of a deployment that no longer exists, and `ops
    status`/`restart` read it to decide which service manager to drive.
    """
    marker = clear_install_mode()
    return [OpsResult(True, "Cleared the native install-mode marker", str(marker))]


def _uninstall_next_steps() -> str:
    """The artifact-removal command to run *after* the teardown, per platform.

    Named rather than performed: removing the artifact is the package
    manager's job, and nyxGPT deliberately does not uninstall kegs out from
    under Homebrew's own bookkeeping. What this teardown guarantees is that
    the command below is now safe to run -- nothing is left holding a port or
    registered to come back.
    """
    if _is_macos():
        return (
            "Next: remove the installed artifacts with Homebrew, e.g.\n"
            "  brew uninstall $(brew list --formula | grep '^nyxgpt')\n"
            "  brew untap <your-tap>\n"
            "Your data and configuration are untouched -- ~/.nyxGPT (config.ini, "
            "volumes, logs) is preserved. Delete it by hand if you want it gone."
        )
    return (
        "Next: remove the installed artifacts, e.g. `pip uninstall nyxgpt` or by deleting\n"
        "  ~/.nyxGPT/opt/nyxgpt-api and ~/.nyxGPT/opt/nyxgpt-web\n"
        "Your data and configuration are untouched -- ~/.nyxGPT (config.ini, "
        "volumes, logs) is preserved. Delete it by hand if you want it gone."
    )


def uninstall(args) -> int:
    """CLI entrypoint for `nyxgpt ops uninstall` -- the wrapped teardown (#3859).

    Stops and *deregisters* everything nyxGPT installed on this machine: the
    Homebrew-managed api/web services (macOS) or systemd --user units (Linux),
    the `com.nyxgpt.*` LaunchAgents nyxGPT installed itself, the Cassandra
    container and the Compose observability tier. Run it before `brew
    uninstall` -- Homebrew has no uninstall hook, so nothing else can.

    Volumes are preserved unless `--volumes --yes-really` is given, and
    `~/.nyxGPT` is never touched: uninstalling the software is not deleting
    the operator's data.

    Idempotent, and deliberately so -- the states it runs against are
    routinely partial (the tap already gone, half the services already
    stopped, a plist with no keg behind it). Returns 0 if every step
    succeeded, else 2.
    """
    volumes = bool(getattr(args, "volumes", False))
    if volumes and not bool(getattr(args, "yes_really", False)):
        print(
            "ERROR: refusing to remove volumes without --yes-really "
            "(this deletes Cassandra/Postgres/Grafana data)",
            file=sys.stderr,
        )
        return 2

    logger.info(
        "ops: uninstall starting (volumes=%s)",
        volumes,
        extra={"component": "ops", "action": "uninstall", "volumes": volumes},
    )

    # Same ordering rule as `down`: mark the components intentionally stopped
    # before stopping any of them, or the next self-heal pass restarts what
    # this command just tore down and re-occupies the ports within seconds.
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        ("mark intentionally stopped", _down_mark_intentional_stops),
        ("stop api", lambda: _uninstall_stop_native_service("api")),
        ("stop web", lambda: _uninstall_stop_native_service("web")),
        ("stop ollama", lambda: _uninstall_stop_native_service("ollama")),
        ("stop cassandra container", lambda: _uninstall_stop_container("nyxgpt-cassandra")),
        # After the stops, because deleting a plist out from under a loaded
        # job leaves the job loaded with nothing to report it.
        ("deregister native services", _uninstall_native_service_managers),
        ("compose teardown", lambda: _uninstall_compose_teardown(volumes)),
        ("clear install mode", _uninstall_clear_install_mode),
    ]

    quiet = bool(getattr(args, "quiet", False))
    results, slow_steps = _run_steps("uninstall", steps, quiet=quiet)
    ok = all(r.ok for r in results)
    if not quiet:
        _print_slow_steps_summary(slow_steps)
        print("\n" + _uninstall_next_steps())
    logger.info(
        "ops: uninstall %s (%d/%d ok)",
        "succeeded" if ok else "failed",
        sum(1 for r in results if r.ok),
        len(results),
        extra={"component": "ops", "action": "uninstall", "ok": ok},
    )
    result, message = _ops_action_outcome(results)
    _record_ops_action("uninstall", "all", result, message)

    return 0 if ok else 2


def _report_orphaned_launchd_jobs() -> list[OpsResult]:
    """Report launchd jobs left over from a previous install (macOS, install-time).

    Homebrew has no uninstall hook, so an operator who removes a keg without
    running `nyxgpt ops uninstall` first leaves its service loaded and its
    plist in place; the next install then races that orphan for :8000/:3000
    and launchd keeps restarting it (`KeepAlive`), which is the
    `[Errno 48] address already in use` crash loop from the install side
    (#3853). This is the *report* half only: it never stops or removes
    anything, because at install time a loaded nyxgpt job is usually the
    operator's own running stack, which the install is about to restart
    normally. It names `nyxgpt ops uninstall` as the fix for the case where
    it is not.

    Always succeeds -- a diagnostic that fails an install is a diagnostic
    that gets skipped.
    """
    if not _is_macos():
        return []
    loaded = _loaded_launchd_labels(f"{_BREW_SERVICE_LABEL_PREFIX}nyxgpt")
    loaded += [
        label
        for label in sorted(set(DEV_LAUNCHD_LABELS.values()) | set(SUPPORT_LAUNCHD_LABELS.values()))
        if _launchd_agent_loaded(label)
    ]
    orphans = [label for label in loaded if _launchd_job_is_orphaned(label)]
    if not orphans:
        return []
    return [
        OpsResult(
            True,
            f"{len(orphans)} launchd job(s) from a previous install are loaded but "
            "point at files that no longer exist",
            "\n".join(orphans)
            + "\nThey survive `brew uninstall` (Homebrew has no uninstall hook) and will "
            "fight this install for ports 8000/3000.\nRun `nyxgpt ops uninstall` to clear "
            "them, then install again.",
        )
    ]


def _launchd_job_is_orphaned(label: str) -> bool:
    """True if `label`'s plist names a file that is no longer on disk.

    "Orphaned" is specifically "registered against a deleted install", not
    "not running": a loaded job whose files still exist is a live service, and
    reporting that as leftover would flag every healthy machine.

    Every absolute path in the invocation is checked, not just `argv[0]`. A
    Homebrew service plist runs `["/bin/bash", "<keg>/bin/nyxgpt-api"]`, so
    `argv[0]` is `/bin/bash` and survives any uninstall -- the keg wrapper
    behind it is the file that vanishes, and it is the whole signal. Parsed
    with plistlib rather than `launchctl print` so the answer does not depend
    on that command's output format; an unreadable plist, or one naming no
    absolute path at all, is not orphaned, because this feeds a warning and
    guessing loudly is worse than saying nothing.
    """
    plist = _launchagents_dir() / f"{label}.plist"
    try:
        with plist.open("rb") as handle:
            parsed = plistlib.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    invocation = [parsed.get("Program")]
    argv = parsed.get("ProgramArguments")
    if isinstance(argv, list):
        invocation.extend(argv)
    paths = [entry for entry in invocation if isinstance(entry, str) and entry.startswith("/")]
    return any(not Path(entry).exists() for entry in paths)


def logs(args) -> int:
    """Print recent logs for a single component, in whichever mode it's actually running.

    Wraps `docker compose logs`/`docker logs`/`kubectl logs`/the component's
    own log files so operators never need to run a raw `docker`/`docker
    compose`/`kubectl` command (or hunt for a native log path) themselves --
    e.g. to read the `errors` profile's GlitchTip container output for the
    first-account registration confirmation link its console email backend
    prints there, or the native API's own log file. See
    `self_heal.component_logs` for the per-mode dispatch (#3442).
    """
    service = args.service
    tail = getattr(args, "tail", None)
    if tail is None:
        tail = 200

    logger.info(
        "ops: logs requested for %s (tail=%d)",
        service,
        tail,
        extra={"component": "ops", "action": "logs", "service": service, "tail": tail},
    )

    result = self_heal.component_logs(service, tail=tail)
    if not result.ok:
        print(f"[FAIL] {result.message}")
        if result.details:
            print(f"  {result.details}")
        logger.warning(
            "ops: logs failed for %s: %s",
            service,
            result.message,
            extra={
                "component": "ops",
                "action": "logs",
                "service": service,
                "ok": False,
                "result_message": result.message,
                "details": result.details,
            },
        )
        return 2

    print(f"--- {result.message} ---")
    print(result.details or "(no output)")
    # Log the outcome, not the log body itself -- the tailed output can be
    # large and would otherwise duplicate the target service's own logs.
    logger.info(
        "ops: logs %s",
        result.message,
        extra={"component": "ops", "action": "logs", "service": service, "ok": True},
    )
    return 0


# --- Observability stack (Grafana/Prometheus/Loki/Jaeger/GlitchTip) ---

# Docker Compose profiles that make up the SRE observability suite. These
# tools have no native/Homebrew path (see docker/config.docker.ini) -- they
# only ever run as Compose services, regardless of whether the core app
# (api/web/cassandra/ollama) is deployed native or Compose.
OBSERVABILITY_PROFILES: list[str] = ["monitoring", "logging", "tracing", "errors"]

# Core app services in docker-compose.yml that have no `profiles:` key, which
# makes Compose treat them as "default" services started on *every* `up`
# regardless of which `--profile` flags are passed -- `--profile` only adds
# services, it never filters the always-on ones. `_start_observability_stack`
# must explicitly exclude these from the service list it starts, or `nyxgpt
# ops install` would silently bring up a full Dockerized copy of the app
# (building api/web images) alongside a native install.
CORE_APP_SERVICES: frozenset[str] = frozenset({"nyxgpt", "ollama", "cassandra", "api", "web"})

# config.ini sections flipped to `enabled = true` once their matching
# Compose profile is confirmed up, so the SRE/admin dashboard (and, for
# tracing, the API's own span-export init) reflect that the stack is
# actually live instead of still showing "opt-in, not running". Deliberately
# excludes `error_tracking`: GlitchTip is only reachable once its container
# passes its health check (well after `up -d` returns), so it's flipped on
# by `_provision_glitchtip` instead, once a DSN actually exists to report to.
OBSERVABILITY_ENABLE_SECTIONS: list[str] = ["monitoring", "log_aggregation", "tracing"]


def _compose_available() -> bool:
    """Return True if both `docker` and `docker compose` are usable on this host."""
    if _which("docker") is None:
        return False
    cp = _run(["docker", "compose", "version"], check=False, expected=True)
    return cp.returncode == 0


def _enable_observability_config(cfg_path: Path | None = None) -> None:
    """Flip `enabled = true` for `OBSERVABILITY_ENABLE_SECTIONS` in config.ini.

    Only writes the file if a change is actually needed, so re-running
    `nyxgpt ops install`/`nyxgpt ops observability` after the first time is a
    no-op here. Silently does nothing if config.ini doesn't exist yet (run
    `nyxgpt wizard` first) -- this is a follow-on convenience, not something
    that should fail the observability step itself.
    """
    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return

    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(cfg_path)

    changed = False
    for section in OBSERVABILITY_ENABLE_SECTIONS:
        if not parser.has_section(section):
            parser.add_section(section)
        if parser.get(section, "enabled", fallback="false").strip().lower() != "true":
            parser.set(section, "enabled", "true")
            changed = True

    if not changed:
        return

    with cfg_path.open("w", encoding="utf-8") as f:
        parser.write(f)
    os.chmod(cfg_path, 0o600)


def _grafana_datasources_dir() -> Path:
    """`~/.nyxGPT/docker/grafana/provisioning/datasources/` (synced from the
    packaged `nyxgpt.resources` tree by `_sync_packaged_resources`) -- one
    YAML file per datasource group (GlitchTip lives in its own file,
    `glitchtip.yml`, separate from `datasource.yml`'s Prometheus/Loki/Jaeger
    -- #3432 -- so a `$__file{}` interpolation failure in one file can't take
    the others down; see `docker/grafana/provisioning/datasources/glitchtip.yml`)."""
    return OPS_DOCKER_DIR / "grafana" / "provisioning" / "datasources"


def _grafana_provisioning_fingerprint() -> str:
    """sha256 over everything that only takes effect when the `grafana`
    container is recreated: every datasource provisioning file (new/changed
    datasources) and the Compose file itself (image tag, `GF_INSTALL_PLUGINS`,
    any other env). `docker compose up -d` alone does NOT pick up an env or
    image change on an already-running container -- it needs
    `--force-recreate`. This fingerprint lets `_start_observability_stack`
    detect that drift deterministically (#3424) instead of leaving dashboards
    silently pointed at a stale container missing a plugin or datasource.
    """
    digest = hashlib.sha256()
    datasources_dir = _grafana_datasources_dir()
    paths = sorted(datasources_dir.glob("*.yml")) if datasources_dir.exists() else []
    for path in (*paths, self_heal.COMPOSE_FILE):
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _grafana_provisioning_fingerprint_marker() -> Path:
    """Host-side marker recording the provisioning fingerprint last applied.

    Stored outside Grafana's own bind-mounted data dir (like
    `_migration_marker_path`) so it survives independently of the
    container/volume lifecycle and reflects what THIS tool last reconciled,
    not what the container happens to contain.
    """
    state_dir = Path.home() / ".nyxGPT" / ".migration-state"
    _ensure_dir(state_dir)
    return state_dir / "grafana-provisioning.fingerprint"


def _grafana_provisioning_drifted() -> bool:
    """True if the datasource provisioning file or Compose grafana config
    changed since the last time this tool recreated the container for it."""
    marker = _grafana_provisioning_fingerprint_marker()
    if not marker.exists():
        return True
    try:
        return marker.read_text().strip() != _grafana_provisioning_fingerprint()
    except OSError:
        return True


def _record_grafana_provisioning_fingerprint() -> None:
    """Persist the current provisioning fingerprint as "already applied"."""
    _grafana_provisioning_fingerprint_marker().write_text(_grafana_provisioning_fingerprint())


def _grafana_provisioned_datasource_uids() -> list[str]:
    """Datasource `uid`s declared across every file in
    `docker/grafana/provisioning/datasources/`.

    Deliberately regex-scraped rather than parsed with PyYAML: PyYAML isn't a
    runtime dependency of this package (only a test-suite one), and these
    files are repo-controlled config, not user input, so a targeted regex
    over their known `uid: <value>` shape is sufficient.
    """
    datasources_dir = _grafana_datasources_dir()
    if not datasources_dir.exists():
        return []
    uids: list[str] = []
    for path in sorted(datasources_dir.glob("*.yml")):
        uids += re.findall(r"^\s*uid:\s*(\S+)\s*$", path.read_text(), re.MULTILINE)
    return uids


def _grafana_admin_password_path() -> Path:
    """Where the ops-managed Grafana admin password is stored.

    Thin alias for `config.grafana_admin_password_path` -- kept as a
    module-level name here so existing call sites/tests in this module don't
    need to change, but the actual resolution logic lives in `config.py` so
    `health.py` can share it (#3466) instead of drifting apart (#3458).
    """
    return grafana_admin_password_path()


def _grafana_admin_password(cfg: ConfigParser) -> str:
    """Resolve the Grafana admin password `nyxgpt ops` should reconcile the
    container to.

    Thin alias for `config.resolve_grafana_admin_password` -- see that
    function's docstring for the resolution order.
    """
    return resolve_grafana_admin_password(cfg)


def _grafana_admin_client(grafana_ui_url: str, grafana_admin_password: str) -> httpx.Client:
    """Single source of truth for how ops's install-time checks authenticate
    against Grafana's API -- one place to change the URL/credential wiring
    instead of three separate `auth=("admin", ...)` call sites (#3458)."""
    return httpx.Client(
        base_url=grafana_ui_url,
        auth=("admin", grafana_admin_password),
        timeout=5.0,
    )


@contextlib.contextmanager
def _quiet_httpx_retries() -> Iterator[None]:
    """Suppress httpx's per-request INFO log line for the duration of a
    bounded retry loop against Grafana.

    Each attempt otherwise logs an `HTTP Request: GET ... "200 OK"` line at
    INFO -- pure noise for a handful of retries polled every couple seconds,
    the same "expected outcome shouldn't spam the console" philosophy #3436
    applied to subprocess probe failures.
    """
    httpx_logger = logging.getLogger("httpx")
    previous_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        httpx_logger.setLevel(previous_level)


def _grafana_admin_auth_status(grafana_ui_url: str, grafana_admin_password: str) -> str:
    """Probe `grafana_admin_password` against the running Grafana instance.

    Returns one of `"ok"` (200), `"unauthorized"` (401 -- Grafana answered,
    the credential is genuinely wrong), or `"unreachable"` (connection
    error, or any other status) -- e.g. Grafana still starting, or stuck in
    a crash loop. `_reconcile_grafana_admin_credential` (#3538) uses this
    distinction to give a failure message that points at the right fix: a
    rejected credential and an unreachable/crash-looping Grafana need
    opposite operator responses.
    """
    try:
        with _grafana_admin_client(grafana_ui_url, grafana_admin_password) as client:
            status_code = client.get("/api/org").status_code
    except httpx.HTTPError:
        return "unreachable"
    if status_code == 200:
        return "ok"
    if status_code == 401:
        return "unauthorized"
    return "unreachable"


def _grafana_admin_authenticates(grafana_ui_url: str, grafana_admin_password: str) -> bool:
    """Whether `grafana_admin_password` currently authenticates as `admin`
    against the running Grafana instance."""
    return _grafana_admin_auth_status(grafana_ui_url, grafana_admin_password) == "ok"


def _reset_grafana_admin_password(password: str) -> OpsResult:
    """Force Grafana's actual admin password to `password` via `grafana cli
    admin reset-admin-password` run inside the container.

    Unlike `GF_SECURITY_ADMIN_PASSWORD`, this takes effect regardless of
    whether the volume is fresh (first boot) or long-lived (env var is only
    applied at first boot -- irrelevant to an existing volume, which is
    exactly the deployment shape that reproduced #3458's 401s).

    `password` is piped in over stdin rather than appended to `cmd` as a
    positional argv value: `_redact_cmd`'s masking only recognizes secrets
    that follow a secret-named flag (`--api-key VALUE`) or an inline
    `--flag=value`, so a bare positional value like this one would reach
    `_run`'s non-zero-exit logging -- and `ps`/shell history -- in clear
    text (#3644, CodeQL py/clear-text-logging-sensitive-data #105/#106).
    """
    if not _compose_available():
        return OpsResult(
            False,
            "Cannot reset Grafana admin password",
            "Docker Compose not found -- install Docker to manage the observability stack.",
        )
    cmd = [
        "docker",
        "compose",
        "-f",
        str(self_heal.COMPOSE_FILE),
        "exec",
        "-T",
        "grafana",
        "sh",
        "-c",
        'grafana cli admin reset-admin-password "$(cat)"',
    ]
    cp = _run(cmd, check=False, input=password)
    if cp.returncode != 0:
        return OpsResult(
            False,
            "Failed to reset Grafana admin password",
            _output_excerpt(cp),
        )
    return OpsResult(True, "Reset Grafana admin password to the ops-managed credential")


def _reconcile_grafana_admin_credential(
    grafana_ui_url: str,
    cfg: ConfigParser,
    *,
    attempts: int = 3,
    delay_s: float = 2.0,
) -> tuple[str | None, OpsResult]:
    """Make the Grafana admin credential deterministic before anything else
    authenticates with it (#3458).

    Never authenticates with an empty password: `_grafana_admin_password`
    always returns a real value. Tries that value against the running
    instance first (retrying briefly for Grafana's own startup window); if
    it doesn't work -- unset config default drifted from an existing
    volume's real password, a hand-edited `.env`, whatever -- resets the
    container's password to it via `grafana cli admin reset-admin-password`
    and re-verifies. Returns `(password, result)`: `password` is `None` and
    `result.ok` is `False` when reconciliation could not be completed, so
    callers can skip credential-dependent follow-on checks and surface this
    ONE actionable failure instead of each of them separately 401ing.

    The final failure message distinguishes a genuinely rejected credential
    (Grafana answered 401 -- the stored password is wrong) from Grafana
    being unreachable or crash-looping the whole time (#3538: these need
    opposite operator responses, and were previously reported with the same
    "still doesn't authenticate" message regardless of which one happened).
    """
    password = _grafana_admin_password(cfg)
    last_status = "unreachable"

    with _quiet_httpx_retries():
        for attempt in range(attempts):
            last_status = _grafana_admin_auth_status(grafana_ui_url, password)
            if last_status == "ok":
                return password, OpsResult(True, "Grafana admin credential already reconciled")
            if attempt < attempts - 1:
                time.sleep(delay_s)

        reset_result = _reset_grafana_admin_password(password)
        if not reset_result.ok:
            return None, OpsResult(
                False,
                "Could not reconcile Grafana admin credential",
                f"{reset_result.message}"
                + (f": {reset_result.details}" if reset_result.details else "")
                + " -- check `nyxgpt ops logs grafana` and confirm the grafana container "
                "is running.",
            )

        for attempt in range(attempts):
            last_status = _grafana_admin_auth_status(grafana_ui_url, password)
            if last_status == "ok":
                return password, OpsResult(
                    True,
                    f"{reset_result.message} (stored at {_grafana_admin_password_path()})",
                )
            if attempt < attempts - 1:
                time.sleep(delay_s)

    if last_status == "unreachable":
        return None, OpsResult(
            False,
            "Grafana is unreachable after reset",
            "Reset via `grafana cli admin reset-admin-password` succeeded but Grafana never "
            "answered GET /api/org afterward -- this looks like Grafana crash-looping or still "
            "starting, not a rejected credential. Check `nyxgpt ops status` (a compose service "
            "stuck `restarting` is the tell) and `nyxgpt ops logs grafana` for the boot error.",
        )
    return None, OpsResult(
        False,
        "Grafana admin credential still doesn't authenticate after reset",
        f"Reset via `grafana cli admin reset-admin-password` succeeded but GET /api/org "
        f"still rejects the password stored at {_grafana_admin_password_path()} -- check "
        "`nyxgpt ops logs grafana` for a startup error.",
    )


def _verify_grafana_datasources_resolve(
    grafana_ui_url: str,
    grafana_admin_password: str,
    *,
    attempts: int = 5,
    delay_s: float = 2.0,
) -> OpsResult:
    """Confirm every datasource declared in provisioning actually resolves in
    the running Grafana instance -- i.e. `GET /api/datasources` lists it.

    This directly targets the #3424 failure mode: a provisioning/plugin/env
    change that silently fails to apply (stale container, broken plugin
    download, a provisioning YAML error for one entry) leaves a declared
    datasource unreachable, and every panel that queries it shows "Datasource
    was not found" instead of a clear ops-level error. Retries briefly since
    Grafana may still be inside its startup/provisioning window right after
    `up -d`.
    """
    expected = set(_grafana_provisioned_datasource_uids())
    if not expected:
        return OpsResult(True, "No datasources declared in provisioning -- nothing to verify")

    last_error: Exception | None = None
    with _quiet_httpx_retries():
        for attempt in range(attempts):
            try:
                with _grafana_admin_client(grafana_ui_url, grafana_admin_password) as client:
                    resp = client.get("/api/datasources")
                    if resp.status_code >= 400:
                        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                    actual = {ds.get("uid") for ds in resp.json()}
                missing = expected - actual
                if not missing:
                    return OpsResult(
                        True,
                        f"Verified {len(expected)} provisioned Grafana datasource(s) resolve",
                    )
                if attempt < attempts - 1:
                    time.sleep(delay_s)
                    continue
                return OpsResult(
                    False,
                    f"Grafana datasource(s) failed to provision: {', '.join(sorted(missing))}",
                    "Declared in docker/grafana/provisioning/datasources/*.yml but not "
                    "returned by GET /api/datasources -- check `nyxgpt ops logs grafana` for a "
                    "provisioning error (bad plugin id, YAML syntax, unreachable datasource "
                    "type) near 'inserting datasource from configuration'.",
                )
            except Exception as e:
                last_error = e
                if attempt < attempts - 1:
                    time.sleep(delay_s)
                    continue
    return OpsResult(
        False,
        "Could not reach Grafana to verify provisioned datasources",
        str(last_error) if last_error else "",
    )


def _grafana_expected_plugin_ids() -> list[str]:
    """Plugin ids declared in `docker-compose.yml`'s `GF_INSTALL_PLUGINS`.

    Regex-scraped for the same reason as `_grafana_provisioned_datasource_uids`
    -- no PyYAML runtime dependency, and this is repo-controlled config.
    """
    if not self_heal.COMPOSE_FILE.exists():
        return []
    match = re.search(r'GF_INSTALL_PLUGINS:\s*"([^"]*)"', self_heal.COMPOSE_FILE.read_text())
    if not match:
        return []
    return [p.strip() for p in match.group(1).split(",") if p.strip()]


def _verify_grafana_plugins_installed(
    grafana_ui_url: str,
    grafana_admin_password: str,
    *,
    attempts: int = 5,
    delay_s: float = 2.0,
) -> OpsResult:
    """Confirm every plugin listed in `GF_INSTALL_PLUGINS` is actually
    installed, rather than assuming a successful env-var-driven download
    (#3424's "plugin installation is confirmed... rather than assumed from
    GF_INSTALL_PLUGINS" AC). `GF_INSTALL_PLUGINS` downloads each plugin from
    the network at container startup and fails silently (a logged error, not
    a crash) if that download fails -- e.g. no network access, registry
    outage, or a renamed/typo'd plugin id. On any such failure the plugin is
    never registered, so `GET /api/plugins/<id>/settings` 404s -- that's the
    signal used for every plugin type.

    `enabled` in that response is only meaningful for `type: "app"` plugins,
    which have an explicit on/off toggle; datasource/panel/renderer plugins
    have no such toggle and always report `enabled: false` even once fully
    installed and loaded (#3560 -- `yesoreyeram-infinity-datasource` false-
    failed here despite its provisioned datasource resolving fine in the very
    next check). So `enabled` is only enforced for app plugins; for every
    other type, the 200 itself is proof Grafana registered the plugin.
    """
    expected = _grafana_expected_plugin_ids()
    if not expected:
        return OpsResult(True, "No plugins declared in GF_INSTALL_PLUGINS -- nothing to verify")

    last_error: Exception | None = None
    with _quiet_httpx_retries():
        for attempt in range(attempts):
            try:
                missing: list[str] = []
                missing_details: list[str] = []
                with _grafana_admin_client(grafana_ui_url, grafana_admin_password) as client:
                    for plugin_id in expected:
                        resp = client.get(f"/api/plugins/{plugin_id}/settings")
                        if resp.status_code != 200:
                            missing.append(plugin_id)
                            missing_details.append(
                                f"{plugin_id}: HTTP {resp.status_code}: {resp.text[:500]}"
                            )
                            continue
                        settings = resp.json()
                        if settings.get("type") == "app" and not settings.get("enabled"):
                            missing.append(plugin_id)
                            missing_details.append(f"{plugin_id}: not enabled: {resp.text[:500]}")
                if not missing:
                    return OpsResult(
                        True, f"Verified {len(expected)} GF_INSTALL_PLUGINS plugin(s) installed"
                    )
                if attempt < attempts - 1:
                    time.sleep(delay_s)
                    continue
                return OpsResult(
                    False,
                    f"Grafana plugin(s) failed to install: {', '.join(missing)}",
                    "Listed in docker-compose.yml's GF_INSTALL_PLUGINS but not registered (or, "
                    "for app-type plugins, not enabled) per GET /api/plugins/<id>/settings -- "
                    "check `nyxgpt ops logs grafana` for a plugin download error (no network "
                    "access, registry outage, renamed/typo'd plugin id).\n"
                    + "\n".join(missing_details),
                )
            except Exception as e:
                last_error = e
                if attempt < attempts - 1:
                    time.sleep(delay_s)
                    continue
    return OpsResult(
        False,
        "Could not reach Grafana to verify installed plugins",
        str(last_error) if last_error else "",
    )


# Grafana service account minted for `nyxgpt ops doctor`'s Loki log-volume
# check -- see `_grafana_doctor_token_path` and `_provision_grafana_doctor_token`.
GRAFANA_DOCTOR_SA_NAME = "nyxgpt-ops-doctor"
GRAFANA_DOCTOR_TOKEN_NAME = "nyxgpt-ops-doctor"


def _provision_grafana_doctor_token(grafana_ui_url: str, grafana_admin_password: str) -> OpsResult:
    """Mint a Viewer-scoped Grafana service-account token for doctor's Loki
    check, so it authenticates without depending on the shared admin
    password at query time (#3438).

    Grafana only returns a service-account token's secret at creation time
    (like a GitHub PAT) -- it can't be re-fetched later even to confirm it
    still works -- so this only mints a fresh one when
    `_grafana_doctor_token_path()` doesn't already hold one. A token that's
    since been revoked/gone stale is instead caught (and reported as an
    actionable `doctor` finding, not a silent skip) by
    `_loki_recent_volume_by_logger` at query time.
    """
    token_path = _grafana_doctor_token_path()
    if token_path.exists() and token_path.read_text().strip():
        return OpsResult(True, f"{token_path} already holds a Grafana service-account token")

    try:
        with _grafana_admin_client(grafana_ui_url, grafana_admin_password) as client:
            sa_id: Any = None
            resp = client.get(
                "/api/serviceaccounts/search", params={"query": GRAFANA_DOCTOR_SA_NAME}
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            for sa in resp.json().get("serviceAccounts", []):
                if isinstance(sa, dict) and sa.get("name") == GRAFANA_DOCTOR_SA_NAME:
                    sa_id = sa.get("id")
                    break
            if sa_id is None:
                resp = client.post(
                    "/api/serviceaccounts",
                    json={"name": GRAFANA_DOCTOR_SA_NAME, "role": "Viewer"},
                )
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                sa_id = resp.json().get("id")

            resp = client.post(
                f"/api/serviceaccounts/{sa_id}/tokens",
                json={"name": GRAFANA_DOCTOR_TOKEN_NAME},
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            token: Any = resp.json().get("key")
    except Exception as e:
        return OpsResult(
            False,
            "Failed to provision Grafana service-account token for doctor's Loki check",
            f"{type(e).__name__}: {e}",
        )

    if not token:
        return OpsResult(
            False,
            "Grafana service-account token response missing a key",
            "Check `nyxgpt ops logs grafana` for a /api/serviceaccounts error.",
        )

    try:
        token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        token_path.write_text(str(token))
        token_path.chmod(0o600)
    except OSError as e:
        return OpsResult(
            False, f"Failed to write Grafana service-account token to {token_path}", str(e)
        )

    return OpsResult(True, f"Provisioned Grafana service-account token for doctor at {token_path}")


def _recreate_grafana_if_provisioning_drifted() -> OpsResult | None:
    """Force-recreate just the `grafana` Compose container when provisioning
    or env has drifted since this tool last reconciled it.

    `docker compose up -d` is a no-op for a container whose image/env didn't
    change from Compose's point of view even when the datasource YAML it
    mounts read-only did -- and env vars like `GF_INSTALL_PLUGINS` only take
    effect at container start. Recreating (not merely restarting) guarantees
    both are picked up on the next boot. Returns None if there's nothing to
    do (not drifted, Docker unavailable, or grafana isn't running yet -- the
    normal `up -d` right after this call handles first-boot).
    """
    if not _compose_available():
        return None
    if not _grafana_provisioning_drifted():
        return None
    if _compose_stack_snapshot().get("grafana") != "running":
        # Nothing running yet to be stale -- the normal `up -d` below will
        # create it fresh with current provisioning/env already.
        return None

    cmd = [
        "docker",
        "compose",
        "-f",
        str(self_heal.COMPOSE_FILE),
        "--profile",
        "monitoring",
        "up",
        "-d",
        "--force-recreate",
        "grafana",
    ]
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return OpsResult(
            False,
            "Failed to recreate Grafana for provisioning/env drift",
            _output_excerpt(cp),
        )
    return OpsResult(
        True,
        "Recreated Grafana (provisioning/env drift detected) so plugin installs and "
        "datasource changes actually take effect",
    )


# --- Linux bridge->loopback relay for the native API (#3721) ---

# Compose profile the `host-api-relay` service joins when it's needed, and the
# inert profile name that keeps it out of every `--profile` selection when it
# isn't. `monitoring` rather than a profile of its own, so the relay starts,
# stops, restarts, and comes down in lockstep with prometheus -- it exists
# purely to make prometheus's scrape of the native API reachable.
HOST_RELAY_ENABLED_PROFILE = "monitoring"
HOST_RELAY_DISABLED_PROFILE = "disabled"

# Compose `.env` variables `_sync_host_relay_env` owns end-to-end. Like
# COMPOSE_ENV_SECRET_MAP's secrets these are derived, never hand-edited: every
# observability bring-up recomputes them from the OS, the docker bridge, and
# config.ini, so a machine that changes (docker reinstalled on a different
# subnet, `[api] host` widened by hand) reconciles on the next run.
HOST_RELAY_ENV_PROFILE_VAR = "NYXGPT_HOST_RELAY_PROFILE"
HOST_RELAY_ENV_GATEWAY_VAR = "NYXGPT_HOST_GATEWAY_IP"

# `[api] host` values that leave the native API reachable only from the host
# itself -- exactly the case the relay exists to bridge. Anything else already
# listens on an address containers can reach (and, per app.py's P6-1 gate,
# already required auth to start), so the relay would be redundant *and* would
# fail to bind its own listener against the API's wildcard socket.
LOOPBACK_API_HOSTS: frozenset[str] = frozenset({"", "127.0.0.1", "localhost", "::1"})


def _docker_bridge_gateway_ip() -> str | None:
    """Return the IPv4 gateway of docker's default `bridge` network.

    This is the address `extra_hosts: host.docker.internal:host-gateway`
    resolves to inside a container on a plain Linux engine (docker defaults
    `--host-gateway-ip` to it), so it is the address the relay must listen on
    for `host.docker.internal:8000` to reach the native API.

    Returns None when docker isn't usable, the network has no IPv4 gateway, or
    the output can't be parsed -- callers treat that as "leave the relay off"
    rather than guessing at 172.17.0.1.
    """
    cp = _run(
        [
            "docker",
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{range .IPAM.Config}}{{.Gateway}} {{end}}",
        ],
        check=False,
        expected=True,
    )
    if cp.returncode != 0:
        return None
    for token in (cp.stdout or "").split():
        try:
            addr = ipaddress.ip_address(token.strip())
        except ValueError:
            continue
        if addr.version == 4:
            return str(addr)
    return None


def _native_api_host(cfg_path: Path) -> str:
    """Read `[api] host` from config.ini without importing the full config stack.

    Falls back to the same `127.0.0.1` default the native wrapper scripts use
    (`_NATIVE_API_WRAPPER_TEMPLATE`) when the file is missing or unparseable,
    which is also the value that makes the relay necessary.
    """
    parser = ConfigParser()
    try:
        parser.read(cfg_path, encoding="utf-8")
    except Exception as e:
        logger.warning(
            "Failed to parse %s while resolving the native API bind host; "
            "assuming the 127.0.0.1 default: %s",
            cfg_path,
            e,
            extra={"component": "ops"},
        )
        return "127.0.0.1"
    return parser.get("api", "host", fallback="127.0.0.1").strip()


def _host_relay_decision(cfg_path: Path) -> tuple[bool, str, str]:
    """Decide whether the `host-api-relay` Compose service should run here.

    Returns `(enabled, gateway_ip, reason)`. `gateway_ip` is always a usable
    literal (the loopback placeholder when disabled) so the generated `.env`
    never leaves Compose interpolating an empty `bind=` argument.
    """
    if not _is_linux():
        # Docker Desktop proxies host.docker.internal to the host's loopback
        # itself, so prometheus already reaches a native API on 127.0.0.1.
        return False, "127.0.0.1", f"not needed on {platform.system()}"

    api_host = _native_api_host(cfg_path)
    if api_host.lower() not in LOOPBACK_API_HOSTS:
        return (
            False,
            "127.0.0.1",
            f"[api] host = {api_host} already listens beyond loopback",
        )

    gateway = _docker_bridge_gateway_ip()
    if gateway is None:
        return False, "127.0.0.1", "could not resolve the docker bridge gateway address"

    return True, gateway, f"relaying {gateway} -> 127.0.0.1 for container scrapes"


def _apply_env_updates(lines: list[str], updates: dict[str, str]) -> list[str]:
    """Rewrite `VAR=...` lines in a `.env` file body, appending any that are absent.

    Mirrors `sync_env_from_config`'s line-based rewrite so comments, ordering,
    and every variable this function doesn't own survive verbatim.
    """
    patched = list(lines)
    for var_name, value in updates.items():
        new_line = f"{var_name}={value}"
        for i, line in enumerate(patched):
            if line.startswith(f"{var_name}="):
                patched[i] = new_line
                break
        else:
            patched.append(new_line)
    return patched


def _sync_host_relay_env(cfg_path: Path | None = None, env_path: Path | None = None) -> OpsResult:
    """Write the `host-api-relay` toggle + bind address into Compose's `.env`.

    On a plain Linux docker engine a container has no route to the host's
    127.0.0.1, so prometheus cannot scrape a natively-installed `nyxgpt-api`
    and every Grafana panel stays empty (#3721). Widening `[api] host` to
    `0.0.0.0` fixes the scrape but publishes the API on every interface and
    trips app.py's P6-1 bind-security gate (forcing auth on with it), which is
    exactly the "no 0.0.0.0 ingress" posture P6-4 is meant to hold. Instead,
    enable the `host-api-relay` Compose service: it shares the host's network
    namespace, listens only on the docker bridge gateway, and forwards to the
    host's own loopback.

    Reconciles in both directions -- a host that no longer needs the relay
    (moved to macOS's Docker Desktop, `[api] host` widened by hand, docker
    removed) gets the profile written back to its inert `disabled` value, so
    the next `up` drops the container instead of leaving it bound.

    Never fails the caller: a `.env` that can't be written means Compose falls
    back to the compose-file defaults (relay off), which is the pre-#3721
    behaviour, not a broken stack.
    """
    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    # Anchored to the compose file every `docker compose -f ...` in this module
    # passes, not to NYXGPT_HOME directly: Compose resolves `.env` relative to
    # the compose file's own directory, and `self_heal.COMPOSE_FILE` is the one
    # place that path can be overridden (NYXGPT_COMPOSE_FILE, used from inside
    # the api container). Normally the two are the same `~/.nyxGPT`.
    env_path = env_path or (self_heal.COMPOSE_FILE.parent / ".env")
    example_path = env_path.parent / ".env.example"

    enabled, gateway, reason = _host_relay_decision(cfg_path)
    profile = HOST_RELAY_ENABLED_PROFILE if enabled else HOST_RELAY_DISABLED_PROFILE

    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    elif example_path.exists():
        lines = example_path.read_text(encoding="utf-8").splitlines()
    else:
        # `_sync_packaged_resources` hasn't run yet (or this is a bare test
        # home). Compose has no `.env` to read either, so its own
        # `disabled`/loopback defaults already describe the desired state.
        return OpsResult(
            True,
            "Skipped host API relay (no Compose .env yet)",
            f"Neither {env_path} nor {example_path} exists -- run `nyxgpt ops install`.",
        )

    updates = {
        HOST_RELAY_ENV_PROFILE_VAR: profile,
        HOST_RELAY_ENV_GATEWAY_VAR: gateway,
    }
    patched = _apply_env_updates(lines, updates)
    if patched != lines or not env_path.exists():
        try:
            _ensure_dir(env_path.parent)
            env_path.write_text("\n".join(patched) + "\n", encoding="utf-8")
            os.chmod(env_path, 0o600)
        except OSError as e:
            return OpsResult(
                True,
                "Could not update the host API relay settings in .env",
                f"{env_path}: {e}. Prometheus may not be able to scrape a native "
                "API on Linux until this is writable.",
            )

    if enabled:
        return OpsResult(
            True,
            f"Host API relay enabled ({reason})",
            "Prometheus scrapes the native API through the docker bridge gateway; "
            "`[api] host` stays loopback-only, so the API is not exposed to the LAN.",
        )
    # A relay disabled *because the bind was already widened* is not a clean
    # bill of health -- it is the pre-#3721 workaround still in force, and
    # every reconcile silently re-affirms it (#3509). Say so here rather than
    # letting "disabled (already listens beyond loopback)" read as approval;
    # `nyxgpt ops doctor` reports the same thing via _insecure_api_bind_issue.
    if _is_linux() and _native_api_host(cfg_path).lower() not in LOOPBACK_API_HOSTS:
        return OpsResult(True, f"Host API relay disabled ({reason})", HOST_RELAY_REVERT_REMEDIATION)
    return OpsResult(True, f"Host API relay disabled ({reason})")


# How long `_start_observability_stack` waits for the containers it just
# started to settle, and how often it looks. `docker compose up -d` exits 0
# once the containers are *created*, which is a claim about Docker, not about
# the software inside them: the Grafana crash loop in #3993 was created
# successfully and then died on its own provisioning directory, over and over,
# while the step printed "Observability stack up". Twenty seconds is bounded
# (an install step may not hang) and is comfortably longer than Docker's
# initial restart backoff, so a container that cannot survive its own boot is
# observed `restarting` well inside it.
OBSERVABILITY_SETTLE_TIMEOUT_SECONDS = 20.0
OBSERVABILITY_SETTLE_POLL_SECONDS = 2.0

# A verdict must hold twice before the step calls the stack settled. One
# reading taken immediately after `up -d` sees "running" for any container that
# has not crashed *yet* -- which is every crash loop, for its first second.
OBSERVABILITY_SETTLE_CONFIRMATIONS = 2

# Number of log lines pulled from a container that failed to settle, of which
# only the last non-empty one is quoted into the step message.
_SETTLE_LOG_TAIL = 50

# Compose states that mean a container this step just started is not staying
# up: `restarting` is the crash loop itself, `exited`/`dead` are the container
# already gone. (`_parse_compose_ps` has already dropped the one-shot services
# that exited 0 -- a completed migration is not a crashed container.)
_CRASHED_CONTAINER_STATES = frozenset({"restarting", "exited", "dead", "removing"})

# The three answers the settle check can give. They are deliberately three and
# not two: "stayed up", "could not be determined from here" and "definitely
# failed" call for different operator responses, and collapsing the middle one
# into either of the outer two is the defect class #3812 and #3993 were both
# filed against.
SETTLE_STATE_SETTLED = "settled"
SETTLE_STATE_UNDETERMINED = "undetermined"
SETTLE_STATE_CRASHED = "crashed"


@dataclass(frozen=True)
class _SettleVerdict:
    """What the post-`up -d` stability check established, and why.

    `state` is one of `SETTLE_STATE_SETTLED` / `SETTLE_STATE_UNDETERMINED` /
    `SETTLE_STATE_CRASHED`. `detail` is the operator-facing reason (the
    container's own last log line for a crash, the probe's own failure reason
    for an undetermined verdict) and `services` names the containers the
    verdict is about.
    """

    state: str
    detail: str = ""
    services: tuple[str, ...] = ()


def _observability_failure_reason(service: str) -> str:
    """The last line `service` printed before it stopped staying up.

    Goes through `self_heal.component_logs` -- the same wrapped path `nyxgpt
    ops logs <service>` uses -- rather than shelling out to `docker logs`
    here, so there is one implementation of "read a component's output" and it
    keeps working in the non-Compose modes. The point of quoting it is #3993's
    core complaint: the real cause ("Grafana provisioning error: ...") was one
    line away from the operator and took three SSH sessions to find, because
    the step reported success and the *next* step reported a credential
    problem it did not have.

    Never raises and never returns an empty string: a log read that fails says
    so, because "couldn't read the logs" is still better evidence than silence.
    """
    try:
        result = self_heal.component_logs(service, tail=_SETTLE_LOG_TAIL)
    except Exception as e:  # pragma: no cover - defensive; component_logs is total
        return f"(could not read {service} logs: {type(e).__name__}: {e})"
    if not result.ok:
        return f"(could not read {service} logs: {result.message})"
    lines = [line.strip() for line in (result.details or "").splitlines() if line.strip()]
    if not lines:
        return "(no log output)"
    tail = lines[-1]
    return tail[:197] + "..." if len(tail) > 200 else tail


def _observability_settle_verdict(services: list[str]) -> _SettleVerdict:
    """Watch the just-started `services` for a bounded window and rule on them.

    `docker compose up -d` exiting 0 means the containers were created. It says
    nothing about whether they are still there a second later, which is why the
    observability step used to report "Observability stack up" over a Grafana in
    a permanent crash loop, and why the *next* step's "Could not reconcile
    Grafana admin credential" became the only visible symptom of a completely
    different fault (#3993).

    Answers with one of three verdicts, never collapsing them:

    - **crashed** -- a service is `restarting`/`exited`/`dead`. Definite: the
      probe ran and reported a container that is not staying up. Carries that
      container's own last log line.
    - **settled** -- every service the probe reported for this stack was
      `running` on `OBSERVABILITY_SETTLE_CONFIRMATIONS` consecutive readings.
      Two readings, not one: a container that dies a second after creation
      reads `running` on the first look.
    - **undetermined** -- the probe could not run (no Docker access from here:
      `compose_probe().available` is False, see #3812), reported none of the
      started services, or the window closed with services neither running nor
      crashed (e.g. still `created`). Nothing is claimed either way; the caller
      must not turn this into a success *or* a failure.

    Reuses `self_heal.compose_probe()` rather than adding a second Docker hop:
    that function is already the project's "ask Docker, and say so if you
    couldn't" primitive, and duplicating it would mean two answers to one
    question.
    """
    wanted = set(services)
    deadline = time.monotonic() + OBSERVABILITY_SETTLE_TIMEOUT_SECONDS
    confirmations = 0
    last_detail = ""
    while True:
        probe = self_heal.compose_probe()
        if not probe.available:
            return _SettleVerdict(
                SETTLE_STATE_UNDETERMINED,
                probe.reason or "the Compose probe could not run from here",
                tuple(sorted(wanted)),
            )
        observed = {s.service: s for s in probe.statuses if s.service in wanted}
        crashed = sorted(
            name for name, s in observed.items() if s.state in _CRASHED_CONTAINER_STATES
        )
        if crashed:
            reasons = "; ".join(
                f"{name} ({observed[name].state}): {_observability_failure_reason(name)}"
                for name in crashed
            )
            return _SettleVerdict(SETTLE_STATE_CRASHED, reasons, tuple(crashed))
        if not observed:
            return _SettleVerdict(
                SETTLE_STATE_UNDETERMINED,
                "`docker compose ps` ran but reported none of the services this step "
                "started, so nothing here establishes whether they are up",
                tuple(sorted(wanted)),
            )
        unsettled = sorted(name for name, s in observed.items() if s.state != "running")
        if unsettled:
            confirmations = 0
            last_detail = "still not running after the settle window: " + ", ".join(
                f"{name} ({observed[name].state})" for name in unsettled
            )
        else:
            confirmations += 1
            if confirmations >= OBSERVABILITY_SETTLE_CONFIRMATIONS:
                return _SettleVerdict(SETTLE_STATE_SETTLED, "", tuple(sorted(observed)))
        if time.monotonic() >= deadline:
            return _SettleVerdict(
                SETTLE_STATE_UNDETERMINED,
                last_detail
                or (
                    "the settle window closed before the containers could be confirmed "
                    "running twice"
                ),
                tuple(sorted(observed)),
            )
        time.sleep(OBSERVABILITY_SETTLE_POLL_SECONDS)


def _start_observability_stack(
    extra_compose_files: list[Path] | None = None, force_recreate: bool = False
) -> list[OpsResult]:
    """Start the Grafana/Loki/Jaeger/GlitchTip Compose profiles (idempotent).

    Wraps `docker compose --profile monitoring --profile logging --profile
    tracing --profile errors up -d <services>` so operators never type that
    raw command themselves (the ops-wrapper principle) -- dashboards are
    already pre-provisioned as code via docker/grafana/provisioning, so
    bringing the stack up is the only step needed to get a populated SRE
    view. Re-running is safe: `docker compose up -d` only (re)creates what's
    missing/changed.

    Reports the stack up only once the containers it started are verified to
    have *stayed* up (`_observability_settle_verdict`, #3993). `up -d` exiting
    0 means "created", not "running a second later", and a container that
    crash-loops on its own config was previously reported as a successful
    step. A crash loop now fails the step with that container's own last log
    line; a stack whose state could not be read from here is reported as
    exactly that (`status="UNKNOWN"`), never as either outcome.

    The service list passed to `up -d` is resolved dynamically via `docker
    compose config --services` and explicitly excludes `CORE_APP_SERVICES`.
    This matters because `ollama`/`cassandra`/`api`/`web` have no `profiles:`
    key in docker-compose.yml, so Compose treats them as always-on default
    services -- `--profile` flags alone would NOT stop `up -d` from also
    building and starting the entire core app stack, which would collide
    with a native install's own processes on the same ports.

    Skips (without failing `ops install`) on hosts without Docker: these
    tools have no native/Homebrew path, so a native-only host simply won't
    have dashboards until Docker is installed.
    """
    if not _compose_available():
        return [
            OpsResult(
                True,
                "Skipped observability stack (Docker not found)",
                "Grafana/Loki/Jaeger/GlitchTip only ship as Docker Compose profiles -- "
                "install Docker, then re-run `nyxgpt ops install` (or `nyxgpt ops "
                "observability`) to get dashboards. See docs/docker-compose.md.",
            )
        ]

    base_cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE)]
    for extra in extra_compose_files or []:
        base_cmd += ["-f", str(extra)]
    for profile in OBSERVABILITY_PROFILES:
        base_cmd += ["--profile", profile]

    services_cp = _run(base_cmd + ["config", "--services"], check=False)
    if services_cp.returncode != 0:
        return [
            OpsResult(
                False,
                "Failed to resolve observability services",
                (services_cp.stderr or services_cp.stdout or "").strip(),
            )
        ]

    observability_services = sorted(
        {s.strip() for s in services_cp.stdout.splitlines() if s.strip()} - CORE_APP_SERVICES
    )
    if not observability_services:
        return [
            OpsResult(
                False,
                "No observability services resolved",
                "`docker compose config --services` returned no services outside the "
                "core app stack for the monitoring/logging/tracing/errors profiles -- "
                "check docker-compose.yml.",
            )
        ]

    cmd = base_cmd + ["up", "-d"]
    if force_recreate:
        # Ensure containers attach to the current network even if ones from an
        # earlier bring-up linger on a different network (e.g. switching to the
        # terraform-net override) -- otherwise glitchtip-migrate et al. can't
        # resolve their peers and the stack silently half-starts.
        cmd += ["--force-recreate"]
    cmd += observability_services

    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return [
            OpsResult(
                False,
                "Failed to start observability stack",
                _output_excerpt(cp),
            )
        ]

    # `up -d` exiting 0 is not the end of the question (#3993) -- see
    # `_observability_settle_verdict`.
    verdict = _observability_settle_verdict(observability_services)

    if verdict.state == SETTLE_STATE_CRASHED:
        # Definitely failed, and the config flags stay off: the same rule the
        # `up -d` failure above follows, for the same reason -- a stack that is
        # not running is not a stack to advertise as enabled.
        return [
            OpsResult(
                False,
                "Observability stack did not stay up: "
                + ", ".join(verdict.services)
                + " crash-looping or exited",
                f"{verdict.detail} -- read the full output with `nyxgpt ops logs "
                f"{verdict.services[0]}`, fix the cause, then re-run `nyxgpt ops "
                "observability`.",
            )
        ]

    _enable_observability_config()

    if verdict.state == SETTLE_STATE_UNDETERMINED:
        # Not a success and not a failure: the containers were created (Docker
        # said so) but nothing here could confirm they stayed up. Saying "up"
        # would be the #3993 lie with a different cause, and saying "failed"
        # would be #3812's -- reporting an unqueryable probe as an outage.
        return [
            OpsResult(
                True,
                "Observability stack started; could not verify the containers stayed up",
                f"{verdict.detail} -- check `nyxgpt ops status` and `nyxgpt ops doctor` "
                "from a session that can reach the Docker daemon before relying on "
                "Grafana http://localhost:3001 / Jaeger http://localhost:16686 / "
                "GlitchTip http://localhost:8080.",
                status="UNKNOWN",
            )
        ]

    return [
        OpsResult(
            True,
            "Observability stack up: Grafana http://localhost:3001, "
            "Jaeger http://localhost:16686, Loki via Grafana Explore, "
            "GlitchTip http://localhost:8080",
            "Containers verified running (not restarting) after start. "
            "Dashboards, tracing, and log search are live with no further steps. "
            "GlitchTip's admin user/org/project/DSN are auto-provisioned next by "
            "`nyxgpt ops glitchtip-init` (run automatically as part of `nyxgpt ops "
            "install`) once its container passes its health check.",
        )
    ]


def _reconcile_grafana_provisioning() -> list[OpsResult]:
    """Bring the observability stack up AND make Grafana's provisioning
    state deterministic: recreate on drift before starting, reconcile the
    admin credential (#3458), then verify the declared datasources and
    GF_INSTALL_PLUGINS plugins actually resolve afterward (#3424), and mint
    doctor's Loki service-account token if it doesn't already have one
    (#3438).

    This is the entrypoint `nyxgpt ops install` / `nyxgpt ops observability`
    / wizard toggles use instead of calling `_start_observability_stack`
    directly, so the extra drift/verification steps apply everywhere the
    stack gets (re)started but stay out of `_start_observability_stack`
    itself (which several other call sites -- including the terraform-network
    variant and tests -- use as a narrower "just run compose up" primitive).

    Assumes the packaged ops resources (`self_heal.COMPOSE_FILE`, the
    Grafana provisioning directory) are already synced to `NYXGPT_HOME` --
    `install()` sequences its own `_sync_packaged_resources` step before this
    one; standalone callers (`observability()`) sync first themselves.
    """
    drift_result = _recreate_grafana_if_provisioning_drifted()
    results = [drift_result] if drift_result is not None else []
    if drift_result is not None and not drift_result.ok:
        return results

    # Must run before the stack starts: on Linux dockerd creates a missing
    # bind-mount source as root:root, and Prometheus (65534) / Grafana (472) /
    # Loki (10001) then crash-loop unable to write their own data dirs (#3632).
    # This lives here rather than in `install()`'s step list because `install`
    # is not the only way the stack starts: `nyxgpt ops observability` is
    # documented as runnable without an install having gone first, and the SRE
    # dashboard's observability toggle calls `reconcile_observability` -- both
    # reach the stack only through this function, so both brought it up on
    # root-owned volumes (#3721). One entrypoint, one ownership guarantee.
    results += _ensure_observability_volume_dirs()

    # Also before the stack starts: this decides whether Compose resolves the
    # `host-api-relay` service into the monitoring profile at all, which is what
    # gives prometheus a route to a natively-installed API on Linux (#3721).
    # `_start_observability_stack` reads `.env` when it enumerates services, so
    # the write has to land first.
    results.append(_sync_host_relay_env())

    start_results = _start_observability_stack()
    results += start_results
    # A stack that did not settle is not a stack to reconcile against (#3993):
    # authenticating against a restarting Grafana turns a crash loop into
    # "Could not reconcile Grafana admin credential", which is the wrong fault
    # reported to the operator. `ok=False` here now includes the settle
    # check's *definite* crash verdict, so the reconcile below never runs
    # against a container that is not staying up.
    #
    # The settle check's undetermined verdict is deliberately NOT a stop: it is
    # `ok=True` with `status="UNKNOWN"`, because a probe that could not run is
    # not evidence of a broken stack (#3812). That case falls through to the
    # `grafana_running` gate below, which asks the same probe and simply
    # declines to wait on a Grafana it cannot see running.
    if not all(r.ok for r in start_results):
        return results

    _record_grafana_provisioning_fingerprint()

    cfg_path = Path.home() / ".nyxGPT" / "config.ini"
    if cfg_path.exists():
        try:
            cfg_parser = ConfigParser()
            cfg_parser.read(cfg_path)
            monitoring_config = get_monitoring_config(cfg_parser)
            grafana_ui_url = monitoring_config["grafana_ui_url"]

            # Bounded wait-for-healthy (#3538) before anything authenticates
            # against Grafana: `_start_observability_stack`/the drift
            # recreate above may have just (re)started the container, and a
            # container stuck crash-looping (e.g. a broken alerting-
            # provisioning file) should surface here as one clear failure
            # rather than as a misleading "credential doesn't authenticate".
            # Skipped as a no-op when Grafana isn't part of the running
            # Compose stack at all (e.g. Docker not found) -- the
            # credential-reconcile call below already handles that host
            # shape on its own terms.
            grafana_running = _compose_stack_snapshot().get("grafana") == "running"
            if grafana_running and not _wait_for_grafana_healthy():
                results.append(
                    OpsResult(
                        False,
                        "Grafana never became healthy",
                        "Check `nyxgpt ops status` (a compose service stuck `restarting` is "
                        "the tell) and `nyxgpt ops logs grafana` for the boot error.",
                    )
                )
                return results

            grafana_admin_password, credential_result = _reconcile_grafana_admin_credential(
                grafana_ui_url, cfg_parser
            )
            results.append(credential_result)
            # Only attempt the credential-dependent checks once the admin
            # password is known-good -- otherwise each would independently
            # 401 and turn one auth problem into three separate FAILs.
            if grafana_admin_password is not None:
                results.append(
                    _verify_grafana_plugins_installed(grafana_ui_url, grafana_admin_password)
                )
                results.append(
                    _verify_grafana_datasources_resolve(grafana_ui_url, grafana_admin_password)
                )
                results.append(
                    _provision_grafana_doctor_token(grafana_ui_url, grafana_admin_password)
                )
        except Exception as e:
            logger.warning(
                "Failed to verify Grafana plugins/datasources, skipping: %s",
                e,
                extra={"component": "ops"},
            )

    return results


def _start_observability_stack_terraform() -> list[OpsResult]:
    """Start the observability Compose profiles on the terraform network.

    A step in `nyxgpt ops install --terraform --local`, run after `terraform
    apply` has created the `nyxgpt-terraform` network and the core containers.
    Reuses `_start_observability_stack` with the terraform-net override so the
    observability containers join that network and interoperate with the
    terraform-managed core (Prometheus scrapes the tf-api, the api reaches
    otel-collector/glitchtip, etc.).
    """
    if not TERRAFORM_NET_OVERRIDE.exists():
        return [
            OpsResult(
                False,
                f"Missing {TERRAFORM_NET_OVERRIDE}",
                "Cannot attach observability to the terraform network without the override.",
            )
        ]
    results = _start_observability_stack(
        extra_compose_files=[TERRAFORM_NET_OVERRIDE], force_recreate=True
    )
    # Bounded wait-for-healthy (#3538), mirroring the native
    # `_reconcile_grafana_provisioning` path: `docker compose up -d` returns as
    # soon as the containers are created, but Grafana (57 plugins + provisioning)
    # takes ~30s to actually serve, and `_terraform_stack_health` only gates on
    # the core containers. Without this wait `ops install --terraform` returns
    # before Grafana is reachable and callers (the smoke gate, a user opening the
    # dashboard) race its startup. A container stuck crash-looping never reports
    # healthy, so this surfaces as one clear failure instead of a silent race.
    if _compose_stack_snapshot().get("grafana") == "running" and not _wait_for_grafana_healthy():
        results.append(
            OpsResult(
                False,
                "Grafana never became healthy",
                "Check `nyxgpt ops status` (a compose service stuck `restarting` is the tell) "
                "and `nyxgpt ops logs grafana` for the boot error.",
            )
        )
    return results


def _stop_observability_stack_terraform() -> list[OpsResult]:
    """Tear down the observability Compose stack attached to the terraform network.

    The mirror of `_start_observability_stack_terraform`, run BEFORE `terraform
    destroy`. `install --terraform --local` brings observability up on the
    `nyxgpt-terraform` network; `terraform destroy` then can't remove that
    network while those containers are still attached (it times out on the
    network delete). This `docker compose down` (with the terraform-net
    override) removes the observability containers and detaches them from the
    network -- the external network itself is left for terraform to destroy.
    Best-effort: a Docker-less host or a missing override just skips it.
    """
    if not _compose_available():
        return [OpsResult(True, "Skipped observability teardown (Docker not found)")]
    if not TERRAFORM_NET_OVERRIDE.exists():
        return [
            OpsResult(True, f"Skipped observability teardown (no {TERRAFORM_NET_OVERRIDE.name})")
        ]
    cmd = [
        "docker",
        "compose",
        "-f",
        str(self_heal.COMPOSE_FILE),
        "-f",
        str(TERRAFORM_NET_OVERRIDE),
    ]
    for profile in OBSERVABILITY_PROFILES:
        cmd += ["--profile", profile]
    cmd += ["down"]
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return [OpsResult(False, "Failed to tear down observability stack", _cp_details(cp))]
    return [OpsResult(True, "Observability stack torn down (detached from terraform network)")]


def observability(args) -> int:
    """CLI entrypoint for `nyxgpt ops observability`.

    Starts the monitoring/logging/tracing/errors Compose profiles (Grafana,
    Prometheus, Loki, promtail, the OTel collector, Jaeger, GlitchTip) so
    operators never need to run a raw `docker compose --profile X up`
    themselves. Idempotent: re-running just confirms everything is already up.

    Documented as runnable without `nyxgpt ops install` having gone first, so
    syncs the packaged ops resources itself (#3621): `_reconcile_grafana_provisioning`
    reads `self_heal.COMPOSE_FILE` and the Grafana provisioning directory,
    both of which only exist under `NYXGPT_HOME` once
    `_sync_packaged_resources` has copied them there.

    Returns 0 on success, else 2.
    """
    logger.info(
        "ops: observability starting", extra={"component": "ops", "action": "observability"}
    )

    # `--kubernetes --local`: the same command, applied to a cluster instead
    # of Compose (#3787). Kubernetes mode cannot use the Compose profiles at
    # all -- they scrape `host.docker.internal` and resolve Compose service
    # names -- so this branches to the in-cluster overlay rather than trying
    # to reconcile both.
    if getattr(args, "kubernetes", False):
        if _resolve_locality(args) is None:
            return 2
        results = observability_kubernetes()
        ok = _emit_results("observability --kubernetes", results)
        result, message = _ops_action_outcome(results)
        _record_ops_action("observability", "kubernetes", result, message)
        logger.info(
            "ops: observability %s",
            "succeeded" if ok else "failed",
            extra={"component": "ops", "action": "observability", "mode": "kubernetes", "ok": ok},
        )
        return 0 if ok else 2

    sync_results: list[OpsResult] = []

    def _observability_sync_step() -> list[OpsResult]:
        nonlocal sync_results
        sync_results = _sync_packaged_resources()
        return sync_results

    def _observability_reconcile_step() -> list[OpsResult]:
        if not all(r.ok for r in sync_results):
            return [
                OpsResult(
                    True,
                    "Skipped Grafana provisioning reconcile (packaged resource sync failed)",
                )
            ]
        return _reconcile_grafana_provisioning()

    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        ("sync packaged resources", _observability_sync_step),
        ("reconcile observability stack", _observability_reconcile_step),
    ]
    quiet = bool(getattr(args, "quiet", False))
    results, slow_steps = _run_steps("observability", steps, quiet=quiet)
    ok = all(r.ok for r in results)
    if not quiet:
        _print_slow_steps_summary(slow_steps)
    logger.info(
        "ops: observability %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "observability", "ok": ok},
    )
    result, message = _ops_action_outcome(results)
    _record_ops_action("observability", "observability", result, message)
    return 0 if ok else 2


def reconcile_observability(enable: bool) -> list[OpsResult]:
    """Bring the observability Compose stack in line with a desired enabled state.

    Used by the web config wizard (#3354) after config.ini's
    monitoring/tracing/error_tracking/log_aggregation `enabled` flags change
    via the API, so enabling one of those sections in the wizard results in
    the Compose profile actually starting -- not just a flag flip -- and
    disabling all of them tears the stack back down. Mirrors `nyxgpt ops
    observability` / `nyxgpt ops stop --target observability` exactly, so
    callers (the config API) never need to shell out to `docker compose`
    themselves. Records its own ops lifecycle action/event (#3390) since,
    unlike `restart()`/`stop()`, this is the entrypoint the dashboard calls
    directly rather than going through the CLI-facing `observability()`.
    """
    results = _reconcile_grafana_provisioning() if enable else _stop_observability_stack()
    result, message = _ops_action_outcome(results)
    _record_ops_action("observability" if enable else "stop", "observability", result, message)
    return results


# --- Env sync public API ---

# Maps a Docker Compose `.env` variable to the config.ini `[section] key` it's
# derived from. config.ini (generated by `nyxgpt wizard`) is the single
# source of truth for these secrets -- Compose's `.env` is a generated
# artifact, not something the user hand-edits.
COMPOSE_ENV_SECRET_MAP: dict[str, tuple[str, str]] = {
    "NYXGPT_AUTH_API_KEY": ("auth", "api_key"),
    "GRAFANA_ADMIN_PASSWORD": ("monitoring", "grafana_admin_password"),
}

# Non-secret `.env` variables derived from config.ini the same way, kept in
# their own map so the "no secrets to sync" diagnostics above stay about
# secrets (#3824). The Compose `ollama` service pre-pulls these two models and
# gates its healthcheck on them, so they must follow config.ini rather than
# being hand-edited into `.env` -- that is what makes the Compose run mode's
# pull config-driven instead of a literal in docker-compose.yml.
COMPOSE_ENV_MODEL_MAP: dict[str, tuple[str, str]] = {
    "NYXGPT_DEFAULT_MODEL": ("nyxgpt", "default_model"),
    "NYXGPT_EMBEDDING_MODEL": ("rag", "embedding_model"),
}


def sync_env_from_config(
    cfg_path: Path | None = None, env_path: Path | None = None
) -> list[OpsResult]:
    """Derive Docker Compose's `.env` secrets from `~/.nyxGPT/config.ini`.

    Overwrites (or appends) only the secret lines listed in
    `COMPOSE_ENV_SECRET_MAP`; every other line in `.env` (ports, image tags,
    etc.) is left untouched. If `.env` doesn't exist yet, it's seeded from
    `.env.example`.
    """
    from nyxgpt.config import load_config

    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return [
            OpsResult(
                False,
                f"Missing config {cfg_path}",
                "Run `nyxgpt wizard` first to generate config.ini and its secrets.",
            )
        ]

    cfg = load_config(cfg_path)

    # `.env` lives alongside OPS_COMPOSE_FILE (NYXGPT_HOME by default) since
    # Docker Compose resolves it relative to the compose file's own
    # directory. `.env.example` -- the packaged template
    # `_sync_packaged_resources` copies there (#3621) -- always sits next to
    # whichever `.env` this call targets, so an explicit `env_path` override
    # (as `env_sync`'s `--env-file` and this function's own tests use) still
    # finds the matching example alongside it rather than NYXGPT_HOME's.
    env_path = env_path or (NYXGPT_HOME / ".env")
    example_path = env_path.parent / ".env.example"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    elif example_path.exists():
        lines = example_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    def _set(var_name: str, value: str) -> None:
        new_line = f"{var_name}={value}"
        for i, line in enumerate(lines):
            if line.startswith(f"{var_name}="):
                lines[i] = new_line
                return
        lines.append(new_line)

    synced: list[str] = []
    for var_name, (section, key) in COMPOSE_ENV_SECRET_MAP.items():
        value = cfg.get(section, key, fallback="")
        if not value:
            continue
        _set(var_name, value)
        synced.append(var_name)

    # Derived, not secret: the Compose `ollama` service reads these to know
    # which models to pre-pull and gate its healthcheck on (#3824). Written
    # even when no secret was found -- the early returns below are about
    # secrets -- and the resolved *chat* model is the fallback for an empty
    # `[rag] embedding_model`, matching how RAG itself resolves it.
    from nyxgpt.config import get_default_model

    model_values = {
        "NYXGPT_DEFAULT_MODEL": get_default_model(cfg),
        "NYXGPT_EMBEDDING_MODEL": (
            cfg.get("rag", "embedding_model", fallback="").strip() or get_default_model(cfg)
        ),
    }
    models_synced = [var for var, value in model_values.items() if value]
    for var_name, value in model_values.items():
        if value:
            _set(var_name, value)
    if models_synced and not synced:
        _ensure_dir(env_path.parent)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(env_path, 0o600)

    if not synced:
        if not cfg.getboolean("auth", "enabled", fallback=False):
            return [
                OpsResult(
                    True,
                    "No secrets to sync (auth disabled)",
                    "[auth] enabled = false with no api_key set is a valid "
                    "localhost-only configuration -- no secret line written "
                    f"({', '.join(models_synced)} still synced). Run "
                    "`nyxgpt wizard` to generate secrets before any networked "
                    "deploy, then re-run `nyxgpt ops env-sync`.",
                )
            ]
        return [
            OpsResult(
                False,
                "No secrets found in config.ini to sync",
                f"Set [auth] api_key and/or [monitoring] grafana_admin_password in "
                f"{cfg_path} (re-run `nyxgpt wizard` to generate them), then retry"
                + (
                    f" -- {', '.join(models_synced)} were still written to {env_path}."
                    if models_synced
                    else "."
                ),
            )
        ]

    _ensure_dir(env_path.parent)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)

    return [
        OpsResult(
            True,
            f"Synced {', '.join(synced + models_synced)} into {env_path} from {cfg_path}",
        )
    ]


def env_sync(args) -> int:
    """CLI entrypoint for `nyxgpt ops env-sync`.

    Reads `--config`/`--env-file` overrides (if given) from `args`, then
    seeds the live `docker/config.docker.ini` from its template (if missing)
    and derives Docker Compose's `.env` secrets from config.ini via
    `sync_env_from_config`, printing an OK/FAIL line per result.

    Seeding the Compose config here (not just in `nyxgpt ops install`) covers
    the Compose-only Quickstart, which runs `nyxgpt ops env-sync` before
    `docker compose up` without going through the native install flow -- so a
    fresh checkout gets the bind-mounted file created before Compose needs it.
    For that same install()-less Quickstart flow to seed `.env` from
    `.env.example` (see `sync_env_from_config`), the packaged ops resources
    have to be synced to `NYXGPT_HOME` first too (#3621) -- otherwise
    `.env.example` isn't there yet and `.env` silently ends up with only the
    secret lines.

    Returns 0 on success, else 2.
    """
    cfg_path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    env_path = Path(args.env_file).expanduser() if getattr(args, "env_file", None) else None

    logger.info(
        "ops: env-sync starting (config=%s, env_file=%s)",
        cfg_path,
        env_path,
        extra={"component": "ops", "action": "env-sync"},
    )

    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        ("sync packaged resources", _sync_packaged_resources),
        ("compose config (derive from native)", _generate_compose_config),
        (
            "sync secrets from config.ini",
            lambda: sync_env_from_config(cfg_path=cfg_path, env_path=env_path),
        ),
        (
            "sync grafana slack webhook secret",
            lambda: _sync_grafana_slack_webhook_secret(cfg_path=cfg_path),
        ),
    ]
    quiet = bool(getattr(args, "quiet", False))
    results, slow_steps = _run_steps("env-sync", steps, quiet=quiet)
    ok = all(r.ok for r in results)
    if not quiet:
        _print_slow_steps_summary(slow_steps)
    logger.info(
        "ops: env-sync %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "env-sync", "ok": ok},
    )

    return 0 if ok else 2


# --- Session backend selection (#3865) ---------------------------------
#
# `[nyxgpt] session_backend` decides whether chat sessions live as JSON files
# on one machine's disk or as rows in the stack's Cassandra -- and therefore
# whether every deployment mode pointed at the same Cassandra sees one
# session list (docs/session-storage.md, #3590). Kubernetes sets it
# declaratively in k8s/configmap.yaml; Compose and Terraform-local inherit it
# because `_generate_compose_config` copies the native config verbatim. The
# provisioning paths (`nyxgpt cloud deploy`, `nyxgpt cloud user-data`) had no
# way to set it at all, so a cloud instance silently ran the back-compat
# `file` default and the only fix was to SSH in and hand-edit config.ini --
# a raw-operations flow the operational command wrapping requirement
# (CLAUDE.md, 2026-07-15) forbids as the user-facing path. This is the
# wrapped setter those paths call, and that an operator can call directly.


def set_session_backend(backend: str, cfg_path: Path | None = None) -> list[OpsResult]:
    """Set `[nyxgpt] session_backend` in config.ini to `backend`, idempotently.

    Line-based via `_patch_ini_value` rather than a `ConfigParser` round-trip:
    the file this rewrites is normally the one just seeded from
    `example.config.ini`, whose comments document every other key, and a
    round-trip would drop all of them.

    Re-running with the value already set writes nothing, so a re-deploy (the
    provisioning scripts are reconciles, not first-run bootstraps) is a no-op
    here. Refuses an unknown backend rather than writing a value
    `get_session_backend` would later reject and silently downgrade to
    `file` -- the failure this whole function exists to stop being silent.
    """
    normalized = backend.strip().lower()
    if normalized not in VALID_SESSION_BACKENDS:
        return [
            OpsResult(
                False,
                f"Unknown session backend {backend!r}",
                f"Choose one of: {', '.join(VALID_SESSION_BACKENDS)}",
            )
        ]

    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return [
            OpsResult(
                False,
                f"No config.ini at {cfg_path}",
                "Run `nyxgpt wizard` (or `nyxgpt ops install`) to create it, then retry.",
            )
        ]

    try:
        text = cfg_path.read_text(encoding="utf-8")
        patched = _patch_ini_value(text, "nyxgpt", "session_backend", normalized)
        if patched != text:
            cfg_path.write_text(patched, encoding="utf-8")
            # config.ini carries [auth] api_key and other secrets; keep the
            # 0600 the seeding step gave it even if the umask would not.
            os.chmod(cfg_path, 0o600)
    except OSError as e:
        return [
            OpsResult(
                False,
                f"Failed to set the session backend in {cfg_path}",
                f"{type(e).__name__}: {e}",
            )
        ]

    if patched == text:
        return [OpsResult(True, f"Session backend already `{normalized}` in {cfg_path}")]
    detail = (
        "Sessions are stored in the stack's Cassandra -- every deployment mode pointed at "
        "the same Cassandra shares one session list. Restart the API to pick this up "
        "(`nyxgpt ops restart api`)."
        if normalized == "cassandra"
        else (
            "Sessions are stored as JSON files under `[nyxgpt] sessions_dir` on this host "
            "only. Restart the API to pick this up (`nyxgpt ops restart api`)."
        )
    )
    return [OpsResult(True, f"Set session backend to `{normalized}` in {cfg_path}", detail)]


def session_backend(args: Any) -> int:
    """CLI entrypoint for `nyxgpt ops session-backend [file|cassandra]`.

    With no backend argument this reports the effective backend rather than
    changing anything, which is deliberately not the same as "what config.ini
    says": `NYXGPT_SESSION_BACKEND` overrides the file (config.get_session_backend),
    and an operator debugging a container that disagrees with its config needs
    to see the value actually in force.

    Returns 0 on success, else 2.
    """
    from nyxgpt.config import load_config

    cfg_path = (
        Path(args.config).expanduser()
        if getattr(args, "config", None)
        else (Path.home() / ".nyxGPT" / "config.ini")
    )
    requested = str(getattr(args, "backend", None) or "").strip()

    if not requested:
        exists = cfg_path.exists()
        cfg = load_config(cfg_path) if exists else ConfigParser()
        effective = get_session_backend(cfg)
        print(f"session_backend = {effective}")
        override = os.environ.get("NYXGPT_SESSION_BACKEND", "").strip()
        if override:
            print(f"  (forced by NYXGPT_SESSION_BACKEND={override}, overriding {cfg_path})")
        elif exists:
            print(f"  (from {cfg_path})")
        else:
            # Attributing the answer to a file that is not there reads as "your
            # config says file" when what happened is that nothing said
            # anything and the back-compat default answered.
            print(f"  (built-in default; no config.ini at {cfg_path})")
        return 0

    logger.info(
        "ops: session-backend setting %s in %s",
        requested,
        cfg_path,
        extra={"component": "ops", "action": "session-backend"},
    )
    results = set_session_backend(requested, cfg_path=cfg_path)
    return 0 if _emit_results("session-backend", results) else 2


# --- Secrets sync: config.ini -> GitHub Actions secrets (#3505) ---
#
# The canonical-store pattern this codifies: several external tokens
# (Slack bot tokens, agent PATs) are write-once -- the issuing service shows
# them only at creation, so hand-editing a second copy into GitHub's
# Settings -> Secrets UI lets the two silently drift. config.ini is the one
# place a human ever pastes these in (via `nyxgpt secrets setup`); this
# pushes them outward, one direction only, to the matching Actions secret.
# `config.SECRETS_SYNC_MANIFEST` is the sole source of truth for *which*
# config.ini keys are in scope -- anything not listed there is never pushed.

GITHUB_API_BASE_URL = "https://api.github.com"


def _github_actions_client(pat: str) -> httpx.Client:
    """Construct an `httpx.Client` authenticated against the GitHub REST API."""
    return httpx.Client(
        base_url=GITHUB_API_BASE_URL,
        timeout=15.0,
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _manifest_targets(cfg: ConfigParser, manifest: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return `(full_key, value, actions_name)` for every manifest entry with a value.

    A manifest key absent or blank in config.ini is silently skipped (nothing
    to sync yet, not an error) -- `nyxgpt secrets setup`/manual entry decides
    when a given secret exists, and a blank *variable* is how "unset" is
    spelled for the optional ones (an empty `SLACK_HUDDLE_CHANNEL` means the
    huddle degrades to transcript-only, which is not the same as pushing "").
    """
    targets = []
    for full_key, actions_name in manifest.items():
        section, key = full_key.split(".", 1)
        value = cfg.get(section, key, fallback="").strip()
        if value:
            targets.append((full_key, value, actions_name))
    return targets


def _secrets_sync_targets(cfg: ConfigParser) -> list[tuple[str, str, str]]:
    """Return `(full_key, value, actions_secret_name)` for `SECRETS_SYNC_MANIFEST`."""
    from nyxgpt.config import SECRETS_SYNC_MANIFEST

    return _manifest_targets(cfg, SECRETS_SYNC_MANIFEST)


def _variables_sync_targets(cfg: ConfigParser) -> list[tuple[str, str, str]]:
    """Return `(full_key, value, actions_variable_name)` for `VARIABLES_SYNC_MANIFEST`.

    Re-checks the secret/variable split at push time as well as at import
    (`config._assert_manifests_are_disjoint`). Belt and braces on purpose: this
    is the last point before a value is handed to the world-readable variables
    API, and the cost of the check is a set intersection.
    """
    from nyxgpt.config import SECRETS_SYNC_MANIFEST, VARIABLES_SYNC_MANIFEST

    leaking = sorted(set(VARIABLES_SYNC_MANIFEST) & set(SECRETS_SYNC_MANIFEST))
    if leaking:
        raise RuntimeError(
            f"refusing to push secrets to the GitHub Actions variables API: {leaking}"
        )
    return _manifest_targets(cfg, VARIABLES_SYNC_MANIFEST)


def _encrypt_for_actions_secret(public_key_b64: str, value: str) -> str:
    """Encrypt `value` for the GitHub Actions secrets API's libsodium sealed-box scheme.

    GitHub requires each secret value sealed with the repo's public key
    (base64-encoded in its API response) before it's PUT to the API -- see
    https://docs.github.com/en/rest/actions/secrets. Returns the
    base64-encoded ciphertext the API expects as `encrypted_value`.
    """
    public_key = nacl_public.PublicKey(base64.b64decode(public_key_b64))
    sealed_box = nacl_public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def sync_secrets_to_github_actions(
    cfg_path: Path | None = None, dry_run: bool = False
) -> list[OpsResult]:
    """Push `config.SECRETS_SYNC_MANIFEST`'s config.ini values to GitHub Actions secrets.

    One direction only: config.ini -> Actions. Never reads a secret back from
    GitHub (the API can't return one anyway -- write-once). `dry_run=True`
    reports which secrets *would* be pushed (names only, no network call, no
    values) without touching the GitHub API or requiring a valid PAT.
    Returns one `OpsResult` per synced secret (name only in the message --
    never the value) plus, on failure, enough detail to fix the problem
    (missing config field, HTTP status, etc.).
    """
    from nyxgpt.config import load_config

    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return [
            OpsResult(
                False,
                f"Missing config {cfg_path}",
                "Run `nyxgpt wizard` first to generate config.ini.",
            )
        ]

    cfg = load_config(cfg_path)
    targets = _secrets_sync_targets(cfg)
    if not targets:
        return [
            OpsResult(
                True,
                "No mapped secrets have a value set in config.ini -- nothing to sync",
                "Run `nyxgpt secrets setup` (or set a mapped [monitoring]/[github] key) "
                "first, then retry.",
            )
        ]

    if dry_run:
        return [
            OpsResult(True, f"[dry-run] would sync {full_key} -> Actions secret {actions_secret}")
            for full_key, _value, actions_secret in targets
        ]

    credentials = _github_repo_credentials(cfg)
    if isinstance(credentials, OpsResult):
        return [credentials]
    pat, repo_owner, repo_name = credentials

    results: list[OpsResult] = []
    with _github_actions_client(pat) as client:
        try:
            response = client.get(f"/repos/{repo_owner}/{repo_name}/actions/secrets/public-key")
            response.raise_for_status()
        except httpx.HTTPError as e:
            return [
                OpsResult(
                    False,
                    "Failed to fetch the repo's Actions secrets public key",
                    f"{e} -- check [github] pat has 'repo' (or 'actions:write') scope and "
                    f"[github] repo_owner/repo_name are correct.",
                )
            ]
        public_key_data = response.json()
        key_id = public_key_data["key_id"]
        public_key_b64 = public_key_data["key"]

        for full_key, value, actions_secret in targets:
            try:
                encrypted_value = _encrypt_for_actions_secret(public_key_b64, value)
                put_response = client.put(
                    f"/repos/{repo_owner}/{repo_name}/actions/secrets/{actions_secret}",
                    json={"encrypted_value": encrypted_value, "key_id": key_id},
                )
                put_response.raise_for_status()
            except httpx.HTTPError as e:
                results.append(
                    OpsResult(
                        False,
                        f"Failed to sync {full_key} -> Actions secret {actions_secret}",
                        f"{e} -- verify [github] pat has permission to manage Actions secrets "
                        f"on {repo_owner}/{repo_name}.",
                    )
                )
                continue
            results.append(OpsResult(True, f"Synced {full_key} -> Actions secret {actions_secret}"))

    return results


def _github_repo_credentials(cfg: ConfigParser) -> tuple[str, str, str] | OpsResult:
    """Return `(pat, repo_owner, repo_name)` from config.ini, or the `OpsResult` explaining why not."""
    from nyxgpt.config import get_github_pat, get_github_repo_name, get_github_repo_owner

    pat = get_github_pat(cfg)
    if not pat:
        return OpsResult(
            False,
            "Cannot sync: [github] pat is not set",
            "Run `nyxgpt secrets setup` to configure a GitHub PAT with repo scope.",
        )
    repo_owner = get_github_repo_owner(cfg)
    repo_name = get_github_repo_name(cfg)
    if not repo_owner or not repo_name:
        return OpsResult(
            False,
            "Cannot sync: [github] repo_owner/repo_name is not set in config.ini",
            "Set both under [github] in config.ini, then retry.",
        )
    return pat, repo_owner, repo_name


def sync_variables_to_github_actions(
    cfg_path: Path | None = None, dry_run: bool = False
) -> list[OpsResult]:
    """Push `config.VARIABLES_SYNC_MANIFEST`'s config.ini values to GitHub Actions variables (#3976).

    The variables half of the same canonical-store rule `secrets-sync`
    implements for secrets, and it exists because the repo had no variables
    push at all: the 2026-02 shell script that did this was deleted and never
    replaced, so every variable added since -- and every one whose value
    changed -- was typed into GitHub's settings UI by hand, which is not a
    thing a clean machine can reproduce from the repository.

    One direction only (config.ini -> Actions), and unlike a secret a variable
    *can* be read back, so create-then-update is done against the API rather
    than guessed: POST creates, and a repeat POST 409s, which is when the
    PATCH runs. `dry_run=True` reports names and destinations without a
    network call.

    Values are not printed. A variable is world-readable at GitHub, so this is
    not a confidentiality claim -- it keeps the transcript of an ops run from
    becoming a second uncontrolled copy of configuration, the same reason
    `secrets-sync` reports names only.
    """
    from nyxgpt.config import load_config

    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return [
            OpsResult(
                False,
                f"Missing config {cfg_path}",
                "Run `nyxgpt wizard` first to generate config.ini.",
            )
        ]

    cfg = load_config(cfg_path)
    targets = _variables_sync_targets(cfg)
    if not targets:
        return [
            OpsResult(
                True,
                "No mapped variables have a value set in config.ini -- nothing to sync",
                "Set the [github]/[homebrew]/[monitoring] keys listed in "
                "docs/github-tokens.md, then retry.",
            )
        ]

    if dry_run:
        return [
            OpsResult(True, f"[dry-run] would sync {full_key} -> Actions variable {name}")
            for full_key, _value, name in targets
        ]

    credentials = _github_repo_credentials(cfg)
    if isinstance(credentials, OpsResult):
        return [credentials]
    pat, repo_owner, repo_name = credentials

    results: list[OpsResult] = []
    with _github_actions_client(pat) as client:
        for full_key, value, name in targets:
            try:
                response = client.post(
                    f"/repos/{repo_owner}/{repo_name}/actions/variables",
                    json={"name": name, "value": value},
                )
                if response.status_code == 409:
                    # Already exists -- the only supported update path.
                    response = client.patch(
                        f"/repos/{repo_owner}/{repo_name}/actions/variables/{name}",
                        json={"name": name, "value": value},
                    )
                response.raise_for_status()
            except httpx.HTTPError as e:
                results.append(
                    OpsResult(
                        False,
                        f"Failed to sync {full_key} -> Actions variable {name}",
                        f"{e} -- verify [github] pat has permission to manage Actions "
                        f"variables on {repo_owner}/{repo_name}.",
                    )
                )
                continue
            results.append(OpsResult(True, f"Synced {full_key} -> Actions variable {name}"))

    return results


def secrets_sync(args) -> int:
    """CLI entrypoint for `nyxgpt ops secrets-sync`.

    Returns 0 if every mapped secret with a value synced successfully
    (or there was nothing to sync), else 2.
    """
    cfg_path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    dry_run = bool(getattr(args, "dry_run", False))

    logger.info(
        "ops: secrets-sync starting (config=%s, dry_run=%s)",
        cfg_path,
        dry_run,
        extra={"component": "ops", "action": "secrets-sync"},
    )

    results = sync_secrets_to_github_actions(cfg_path=cfg_path, dry_run=dry_run)
    ok = _emit_results("secrets-sync", results)

    result, message = _ops_action_outcome(results)
    _record_ops_action("secrets-sync", "github-actions", result, message)

    logger.info(
        "ops: secrets-sync %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "secrets-sync", "ok": ok},
    )

    return 0 if ok else 2


def config_sync(args) -> int:
    """CLI entrypoint for `nyxgpt ops config-sync`: push secrets *and* variables (#3976).

    The one wrapped command the canonical-store rule needs. `secrets-sync`
    only ever covered half of what config.ini carries, and the missing half
    had no command at all -- so "config.ini is the canonical store" was true
    of secrets and false of variables, and nothing failed when it fell behind.

    Returns 0 only if both halves succeeded. The variables push runs even when
    the secrets push failed: they are independent destinations, and stopping
    at the first failure would leave the operator guessing which half landed.
    """
    cfg_path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    dry_run = bool(getattr(args, "dry_run", False))

    logger.info(
        "ops: config-sync starting (config=%s, dry_run=%s)",
        cfg_path,
        dry_run,
        extra={"component": "ops", "action": "config-sync"},
    )

    results = sync_secrets_to_github_actions(cfg_path=cfg_path, dry_run=dry_run)
    results += sync_variables_to_github_actions(cfg_path=cfg_path, dry_run=dry_run)
    ok = _emit_results("config-sync", results)

    result, message = _ops_action_outcome(results)
    _record_ops_action("config-sync", "github-actions", result, message)

    logger.info(
        "ops: config-sync %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "config-sync", "ok": ok},
    )

    return 0 if ok else 2


def config_drift(args) -> int:
    """CLI entrypoint for `nyxgpt ops config-drift` (#3976).

    Reconciles config.ini against `example.config.ini` in both directions,
    across *every* section -- including the ones the wizard excludes, which is
    where the credentials are and where the admin dashboard's stale-key banner
    is structurally blind.

    Reports names only, never values, so the output is safe to paste into an
    issue. Exit 0 when the two agree, 2 when they do not: this is a check, and
    a check that always exits 0 cannot be wired into anything.
    """
    from nyxgpt.config import load_config
    from nyxgpt.config_wizard import find_config_drift

    cfg_path = (
        Path(args.config).expanduser()
        if getattr(args, "config", None)
        else (Path.home() / ".nyxGPT" / "config.ini")
    )
    if not cfg_path.exists():
        _emit_results(
            "config-drift",
            [
                OpsResult(
                    False,
                    f"Missing config {cfg_path}",
                    "Run `nyxgpt wizard` first to generate config.ini.",
                )
            ],
        )
        return 2

    drift = find_config_drift(load_config(cfg_path))
    undeclared, missing = drift["undeclared"], drift["missing"]

    results = [
        OpsResult(
            False,
            f"In config.ini but not declared in example.config.ini: {key}",
            "Declare it in example.config.ini if it is live, or remove it from "
            "config.ini if it is retired -- `nyxgpt ops config-drift` never "
            "decides that for you.",
        )
        for key in undeclared
    ] + [
        OpsResult(
            False,
            f"Declared in example.config.ini but absent from config.ini: {key}",
            "Running on its fallback default. Add it to config.ini to pin the value.",
        )
        for key in missing
    ]
    if not results:
        results = [OpsResult(True, f"{cfg_path} and example.config.ini agree on every key")]

    ok = _emit_results("config-drift", results)
    logger.info(
        "ops: config-drift found %d undeclared / %d missing",
        len(undeclared),
        len(missing),
        extra={"component": "ops", "action": "config-drift", "ok": ok},
    )
    return 0 if ok else 2


# --- GlitchTip auto-provisioning (`nyxgpt ops glitchtip-init`) ---

# Fixed org/project names -- reused (not recreated) on every re-run, which is
# what makes this idempotent.
GLITCHTIP_ORG_SLUG = "nyxgpt"
GLITCHTIP_ORG_NAME = "nyxgpt"
GLITCHTIP_TEAM_SLUG = "nyxgpt"
GLITCHTIP_PROJECT_SLUG = "nyxgpt-backend"
GLITCHTIP_PROJECT_NAME = "nyxgpt-backend"
GLITCHTIP_TOKEN_NAME = "nyxgpt-ops-glitchtip-init"
# event:read lets Grafana's Infinity datasource (#3411) query issues/events
# via GlitchTip's Sentry-compatible REST API; team:read/team:write let
# `_glitchtip_ensure_team_membership` (#3565 round 6) confirm/add the
# provisioning admin to the canonical team -- without it, GlitchTip's
# `/organizations/{org}/members/me/teams/{team}/` join endpoint 403s.
GLITCHTIP_TOKEN_SCOPES = [
    "org:read",
    "org:write",
    "project:read",
    "project:write",
    "event:read",
    "team:read",
    "team:write",
]
GLITCHTIP_DEFAULT_ADMIN_EMAIL = "admin@nyxgpt.local"

# The `glitchtip` Compose service's network alias and container-internal port
# (docker-compose.yml: `PORT: 8080`, exposed as `8080:8080`) -- always 8080
# regardless of `GLITCHTIP_UI_PORT`, which only remaps the *host*-side port.
# Used to rewrite a DSN for containerized-api consumption; see
# `_containerized_error_tracking_dsn`.
GLITCHTIP_CONTAINER_HOST = "glitchtip"
GLITCHTIP_CONTAINER_PORT = 8080


def _glitchtip_grafana_token_path() -> Path:
    """Where the GlitchTip API token is written for Grafana's Infinity
    datasource to read (#3411) -- see
    docker/grafana/provisioning/datasources/glitchtip.yml's `$__file{}`
    reference and docs/docker-compose.md#grafana-single-pane-of-glass. Lives
    outside `~/.nyxGPT/volumes/grafana` (Grafana's own data dir) so it isn't
    swept by `nyxgpt ops down --volumes`, which deletes volume_dir()
    contents -- this is a credential, not app data.

    Computed fresh on every call (like `_provision_glitchtip`'s
    `native_cfg_path`), not a module-level constant, so tests can
    monkeypatch `Path.home`.
    """
    return Path.home() / ".nyxGPT" / "secrets" / "glitchtip-grafana-token"


def _glitchtip_secrets_dir_unwritable_result(path: Path) -> OpsResult:
    """Actionable failure for a `~/.nyxGPT/secrets` that exists but this
    process can't write to -- shared by the install preflight and the token
    writer itself so both report the same guidance (#3432)."""
    return OpsResult(
        False,
        f"{path} exists but is not writable by {getpass.getuser()!r}",
        f"On Linux, dockerd runs as root and auto-creates a missing Docker bind-mount "
        f"source directory as root:root the first time a container starts -- if "
        f"Grafana started before this directory existed, that's almost certainly what "
        f"happened here. Fix with: sudo chown -R $(whoami) {path} && chmod 755 {path}, "
        "then re-run `nyxgpt ops install` (or `nyxgpt ops glitchtip-init`).",
    )


# A non-empty, syntactically-harmless stand-in for the GlitchTip Infinity
# datasource bearer token, mirroring GRAFANA_SLACK_WEBHOOK_PLACEHOLDER_URL. Its
# only job is to make the datasource's `$__file{}` reference resolve to a
# non-empty value so Grafana boots; `ops glitchtip-init` overwrites it with the
# real token when GlitchTip is provisioned.
GRAFANA_GLITCHTIP_TOKEN_PLACEHOLDER = "UNCONFIGURED-glitchtip-token"


def _ensure_grafana_secret_placeholders() -> None:
    """Seed crash-safe placeholders for the secrets Grafana reads via `$__file{}`.

    Grafana 13.x hard-fails its *entire* startup (crash-loops, never serves) when
    a `$__file{}` datasource/contact-point secret references a file that is
    missing -- or, per #3538, present but empty. On a fresh `ops install` neither
    value exists yet: the GlitchTip Grafana token is minted later by
    `ops glitchtip-init`/`_provision_glitchtip` (which runs *after* the
    observability stack starts), and the Slack webhook file is only written when
    one is configured. So provisioning would reference two unresolved files and
    take Grafana down with it (observed on both the native and terraform smoke
    gates). Seed valid, non-empty placeholders here -- ahead of the observability
    bring-up, from `_ensure_glitchtip_secrets_dir` which both install paths run
    first -- so `$__file{}` always resolves: a placeholder GlitchTip token is an
    Infinity datasource that returns 401 until configured, and the placeholder
    Slack URL is a contact point that won't deliver -- both degrade gracefully
    instead of crashing Grafana. Only seeds when the file is absent, so a real
    token/URL is never clobbered; the real writers overwrite the placeholder when
    a value exists. Best-effort -- exceptions are swallowed so this never breaks
    the preflight it runs inside.
    """
    if not _slack_webhook_secret_path().exists():
        with contextlib.suppress(Exception):
            # Passing "" makes the writer store GRAFANA_SLACK_WEBHOOK_PLACEHOLDER_URL.
            _write_grafana_slack_webhook_secret("")
    if not _glitchtip_grafana_token_path().exists():
        with contextlib.suppress(Exception):
            _write_grafana_glitchtip_token(GRAFANA_GLITCHTIP_TOKEN_PLACEHOLDER)


def _ensure_glitchtip_secrets_dir() -> list[OpsResult]:
    """Ensure `~/.nyxGPT/secrets` exists and is writable by the invoking user
    *before* anything bind-mounts it (#3432).

    On Linux, dockerd runs as root and auto-creates a missing bind-mount
    source directory as root:root the first time a container starts. Since
    `docker-compose.yml` mounts this directory read-only into Grafana
    (`${HOME}/.nyxGPT/secrets:/etc/nyxgpt-secrets:ro`), if Grafana starts
    (via `_reconcile_grafana_provisioning`/`_start_observability_stack_terraform`)
    before this directory exists, Docker creates it root-owned and every
    later attempt by this (non-root) process to write the GlitchTip token
    into it -- `_write_grafana_glitchtip_token` -- fails with a raw
    `PermissionError`. Running this as an early install step, ahead of
    those, means Docker always finds the directory already present and
    never touches its ownership. macOS (Docker Desktop) doesn't hit this:
    its VM handles bind-mount ownership differently.

    Mode is `0o755`, not `0o700` (#3588): the official Grafana image runs
    as a fixed non-root uid (472) inside the container, and a native Linux
    bind mount exposes host files under their literal host uid/gid -- no
    user-namespace remapping -- so a `0o700` dir owned by the host user
    blocks Grafana's uid from even traversing into it to stat the token
    file it needs to read. `0o755` lets any uid traverse and read; only the
    owning host user can write, which is what actually needs protecting
    here (these are locally-scoped tokens for this machine's own GlitchTip/
    Grafana instances, not high-value secrets).

    Run as a best-effort preflight step (`install()` / `_install_terraform_steps`
    catch and log any exception a step raises), so it never needs to raise
    itself -- it just reports whether the directory is now usable.
    """
    path = _glitchtip_grafana_token_path().parent
    if not path.exists():
        try:
            path.mkdir(mode=0o755, parents=True, exist_ok=True)
        except OSError as e:
            return [OpsResult(False, f"Failed to create {path}", f"{type(e).__name__}: {e}")]
        _ensure_grafana_secret_placeholders()
        return [OpsResult(True, f"Created {path}")]

    if not os.access(path, os.W_OK | os.X_OK):
        return [_glitchtip_secrets_dir_unwritable_result(path)]

    # Owned-and-writable is what matters; a chmod that fails here is harmless.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o755)
    _ensure_grafana_secret_placeholders()
    return [OpsResult(True, f"{path} exists and is writable")]


# The uid:gid each observability image runs its main process as, for the host
# directories docker-compose.yml bind-mounts into them. These are fixed in the
# upstream images (they are not configurable per-deployment): Prometheus drops
# to `nobody`, Grafana to its own `grafana` user in the root group, Loki to
# `loki`. Services whose entrypoint still starts as root and fixes up its own
# data directory (postgres/glitchtip) are deliberately absent -- they need the
# directory to *exist* (so dockerd doesn't create it) but not to be chowned.
OBSERVABILITY_VOLUME_OWNERS: dict[str, tuple[int, int]] = {
    "prometheus": (65534, 65534),
    "grafana": (472, 0),
    "loki": (10001, 10001),
}

# Every ~/.nyxGPT/volumes/* directory an observability container bind-mounts,
# whether or not it needs a chown -- all of them must exist before the stack
# starts so dockerd never auto-creates one as root:root.
OBSERVABILITY_VOLUME_DIRS: tuple[str, ...] = (
    "prometheus",
    "grafana",
    "loki",
    "glitchtip-postgres",
    "glitchtip-uploads",
)


def _dir_acl_grants_uid(path: Path, uid: int) -> bool:
    """True if `path` already carries an effective POSIX ACL granting `uid` rwx.

    Used both to keep `_ensure_observability_volume_dir` idempotent (don't
    re-run the grant on every `ops install`) and to keep `ops doctor` quiet
    about a directory that is reachable by its container through an ACL
    rather than through ownership. `getfacl -cn` prints numeric entries with
    no header; an entry the ACL mask has stripped down is annotated
    `#effective:`, so a line carrying that marker is *not* a grant.
    """
    getfacl = _which("getfacl")
    if getfacl is None:
        return False
    cp = _run([getfacl, "-cn", str(path)], check=False, expected=True)
    if cp.returncode != 0:
        return False
    return any(
        line.strip().startswith(f"user:{uid}:rwx") and "#effective" not in line
        for line in (cp.stdout or "").splitlines()
    )


def _grant_dir_acl_to_uid(path: Path, uid: int) -> bool:
    """Grant `uid` rwx on `path` (recursively) via a POSIX ACL, without root.

    `setfacl` only requires being the file's owner, so this is the one lever a
    non-root host user has to make a bind-mount source writable by a
    container's uid -- bind mounts expose host uids verbatim (no user
    namespace remapping), and the container's uid is neither ours nor in our
    group. Unlike a world-writable chmod it grants exactly one uid and leaves
    every other local user out, which matters because these directories hold
    real state (`grafana.db` carries sessions and hashed credentials).

    Recursive on purpose: a directory whose top level we own may still contain
    root-owned files from an earlier broken run. `setfacl -R` fails on those,
    which correctly drops the caller through to the actionable `sudo chown -R`
    message instead of reporting success on a container that would still
    crash-loop. Returns False when the acl(5) tools are absent or the
    filesystem was not mounted with ACL support.
    """
    setfacl = _which("setfacl")
    if setfacl is None:
        return False
    cp = _run([setfacl, "-R", "-m", f"u:{uid}:rwx", str(path)], check=False, expected=True)
    return cp.returncode == 0


def _ensure_observability_volume_dir(component: str) -> OpsResult:
    """Make one `~/.nyxGPT/volumes/<component>` directory usable by its container's uid."""
    path = volume_dir(component)  # creates it, owned by *this* user, if missing
    want = OBSERVABILITY_VOLUME_OWNERS.get(component)
    if want is None:
        return OpsResult(True, f"{path} exists")

    uid, gid = want
    try:
        st = path.stat()
    except OSError as e:
        return OpsResult(False, f"Failed to stat {path}", f"{type(e).__name__}: {e}")
    if st.st_uid == uid:
        return OpsResult(True, f"{path} is owned by the container's uid ({uid})")
    if _dir_acl_grants_uid(path, uid):
        # Already reconciled by this function's no-root fallback below; don't
        # re-run a sudo chown on every `ops install`.
        return OpsResult(True, f"{path} grants write access to the container's uid ({uid})")

    cp = _privileged_run(["chown", "-R", f"{uid}:{gid}", str(path)], expected=True)
    if cp is not None and cp.returncode == 0:
        return OpsResult(True, f"Set owner of {path} to {uid}:{gid} for its container")

    # No passwordless root. If we own the directory we can still hand exactly
    # this one uid write access with a POSIX ACL, which needs no root at all.
    if st.st_uid == os.getuid() and _grant_dir_acl_to_uid(path, uid):
        return OpsResult(True, f"Granted the container's uid ({uid}) write access to {path}")

    return OpsResult(
        False,
        f"{path} is owned by uid {st.st_uid} and cannot be made writable by its container",
        "dockerd runs as root and creates a missing bind-mount source directory as "
        "root:root, which then leaves the container's non-root uid unable to write "
        "(the container crash-loops as unhealthy). Neither passwordless sudo nor a "
        "POSIX ACL was available to reconcile it. Fix with:\n"
        f"  sudo chown -R {uid}:{gid} {path}\n"
        "then re-run `nyxgpt ops install`.",
    )


def _ensure_observability_volume_dirs() -> list[OpsResult]:
    """Pre-create every observability bind-mount directory with an ownership its
    container can actually write to (#3632).

    On Linux, dockerd (running as root) auto-creates a missing bind-mount
    source directory as `root:root` the first time the container starts.
    Prometheus then runs as uid 65534 inside the container, cannot write to
    `/prometheus`, panics, and crash-loops -- which is what surfaced as
    "dependency failed to start: container nyxgpt-prometheus-1 is unhealthy"
    and took the whole observability bring-up (and GlitchTip provisioning
    behind it) down with it. Grafana (uid 472) and Loki (uid 10001) sit on
    the same landmine.

    macOS never hits this -- Docker Desktop's VirtioFS/gRPC-FUSE sharing
    presents bind mounts as owned by whatever uid the container asks for --
    which is exactly why it went unnoticed until a plain Linux engine was
    exercised. Best-effort (same shape as `_ensure_glitchtip_secrets_dir`,
    which fixed the sibling #3432 case for `~/.nyxGPT/secrets`), so it never
    raises: a directory that cannot be reconciled comes back as a failed
    OpsResult carrying the `sudo chown` to run.

    Called from `_reconcile_grafana_provisioning`, not from `install()`'s step
    list, so it covers every path that starts the stack -- `nyxgpt ops
    install`, the standalone `nyxgpt ops observability`, and the SRE
    dashboard's observability toggle (`reconcile_observability`). Wiring it to
    `install` alone left the other two starting the stack on root-owned
    volumes, which is the Linux half of #3721.
    """
    if not _is_linux():
        return [OpsResult(True, "Not Linux; Docker Desktop handles bind-mount ownership")]
    return [_ensure_observability_volume_dir(c) for c in OBSERVABILITY_VOLUME_DIRS]


def _observability_volume_doctor_issues() -> list[str]:
    """`nyxgpt ops doctor` checks for the #3632 root-owned bind-mount directories."""
    if not _is_linux():
        return []
    issues: list[str] = []
    for component, (uid, gid) in OBSERVABILITY_VOLUME_OWNERS.items():
        path = Path.home() / ".nyxGPT" / "volumes" / VOLUME_DIR_NAMES[component]
        if not path.exists():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        # World-writable is not something install() produces any more, but a
        # machine reconciled by the first cut of this fix still carries it and
        # its container is genuinely fine -- don't nag about it.
        if st.st_uid == uid or bool(st.st_mode & 0o002) or _dir_acl_grants_uid(path, uid):
            continue
        issues.append(
            f"{path} is owned by uid {st.st_uid}, so the {component} container "
            f"(uid {uid}) cannot write to it and will crash-loop as unhealthy "
            f"(run: sudo chown -R {uid}:{gid} {path} && nyxgpt ops install)"
        )
    return issues


def _glitchtip_secrets_doctor_issues() -> list[str]:
    """`nyxgpt ops doctor` checks for the #3432 Linux bind-mount ownership
    failure mode: a `~/.nyxGPT/secrets` that exists but isn't writable by
    the current user (see `_ensure_glitchtip_secrets_dir`), and -- once the
    observability stack is actually up -- a missing GlitchTip token file,
    which leaves Grafana's GlitchTip datasource (glitchtip.yml) unable to
    authenticate even though Prometheus/Loki/Jaeger are fine.

    The second check only queries `_compose_stack_snapshot()` when Docker is
    on PATH, same guard `doctor()` already uses for the Cassandra container
    check -- avoids an unnecessary `docker compose ps` on a host without
    Docker at all.
    """
    issues: list[str] = []

    secrets_dir = _glitchtip_grafana_token_path().parent
    if secrets_dir.exists() and not os.access(secrets_dir, os.W_OK | os.X_OK):
        issues.append(
            f"{secrets_dir} exists but is not writable by {getpass.getuser()!r} "
            f"(run: sudo chown -R $(whoami) {secrets_dir} && nyxgpt ops install)"
        )

    if _which("docker") is not None and _compose_stack_snapshot().get("grafana") == "running":
        token_path = _glitchtip_grafana_token_path()
        if not token_path.exists():
            issues.append(
                f"Observability stack is up but {token_path} is missing -- Grafana's "
                "GlitchTip datasource can't authenticate (run: nyxgpt ops glitchtip-init)"
            )

    return issues


def _error_tracking_dsn_drift_issue(cfg_path: Path | None = None) -> str | None:
    """Detect the #3565 acceptance-failure shape: a configured error-tracking
    DSN whose public key no longer matches any live GlitchTip project key.

    sentry_sdk's HTTP transport is fire-and-forget, exactly like the OTLP
    span exporter `_tracing_wiring_issue` already guards against: when
    GlitchTip rejects an event (401, unrecognized key) the SDK just drops it
    -- nothing raises, nothing logs anywhere an operator would see, and
    `/api/error-tracking` keeps reporting `active: true` because that only
    reflects whether `sentry_sdk.init()` ran, not whether GlitchTip still
    accepts the key it was given. This happens whenever GlitchTip's
    org/project/key gets re-minted independently of `config.ini` (e.g. its
    Postgres data was reset out-of-band) -- `_provision_glitchtip` only
    restarts a *native* `nyxgpt-api` when it personally changes the DSN, so
    a drift introduced any other way is never detected until this check.

    Authenticates with the same ops-provisioned API token already written
    for Grafana's Infinity datasource (`_glitchtip_grafana_token_path`) --
    no extra credential to manage, and no check at all if that token, a
    configured DSN, or a reachable GlitchTip aren't all present, so this
    never blocks `doctor` on a host where error tracking isn't in play.
    """
    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return None

    parser = ConfigParser()
    try:
        parser.read(cfg_path)
    except Exception as e:
        logger.warning(
            "Failed to parse %s, skipping error tracking DSN drift check: %s",
            cfg_path,
            e,
            extra={"component": "ops"},
        )
        return None

    if not get_error_tracking_enabled(parser):
        return None

    error_tracking_config = get_error_tracking_config(parser)
    dsn = (error_tracking_config["dsn"] or "").strip()
    if not dsn:
        return None

    try:
        configured_key = httpx.URL(dsn).username
    except Exception:
        return None
    if not configured_key:
        return None

    token_path = _glitchtip_grafana_token_path()
    if not token_path.exists():
        return None
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not token:
        return None

    base_url = error_tracking_config["glitchtip_ui_url"]
    try:
        with _glitchtip_http_client(
            base_url, headers={"Authorization": f"Bearer {token}"}
        ) as client:
            resp = client.get(
                f"/api/0/projects/{GLITCHTIP_ORG_SLUG}/{GLITCHTIP_PROJECT_SLUG}/keys/"
            )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None

    keys: Any = resp.json()
    if not isinstance(keys, list):
        return None
    live_public_keys = set()
    for key in keys:
        key_dsn = _extract_dsn(key)
        if not key_dsn:
            continue
        try:
            live_public_keys.add(httpx.URL(key_dsn).username)
        except Exception:
            continue

    if not live_public_keys or configured_key in live_public_keys:
        return None

    return (
        f"error_tracking DSN in {cfg_path} doesn't match any current GlitchTip key for "
        f"{GLITCHTIP_ORG_SLUG}/{GLITCHTIP_PROJECT_SLUG} -- every event is being rejected "
        "(401) and silently dropped, the same way an unreachable OTLP collector silently "
        "drops spans. The project's key was likely re-minted since this DSN was written. "
        "Fix: nyxgpt ops glitchtip-init && nyxgpt ops restart api."
    )


def _requirement_distribution_name(requirement: str) -> str:
    """Extract the distribution name from a PEP 508 requirement string.

    e.g. "opentelemetry-instrumentation-urllib>=0.45b0" ->
    "opentelemetry-instrumentation-urllib". Strips any environment marker
    (`; python_version ...`) and version specifier/extras.
    """
    without_marker = requirement.split(";", 1)[0].strip()
    match = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", without_marker)
    return match.group(0) if match else without_marker


def _stale_venv_doctor_issues() -> list[str]:
    """`nyxgpt ops doctor` check for a Python venv that wasn't refreshed
    after a `git pull` added or bumped a declared dependency (#3487) -- the
    root cause behind a bare top-level import of a newly-added package (e.g.
    an OTel instrumentation) crashing every `nyxgpt` command with a raw
    `ModuleNotFoundError` instead of a targeted, actionable finding here.
    """
    pyproject_path = REPO_ROOT / "pyproject.toml"
    if not pyproject_path.exists():
        return []

    try:
        declared = tomllib.loads(pyproject_path.read_text())["project"]["dependencies"]
    except Exception as e:
        logger.warning(
            "Failed to parse %s for the stale-venv doctor check: %s",
            pyproject_path,
            e,
            extra={"component": "ops"},
        )
        return []

    missing = []
    for requirement in declared:
        name = _requirement_distribution_name(requirement)
        try:
            importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)

    if not missing:
        return []

    return [
        "Installed environment is missing declared "
        f"dependenc{'y' if len(missing) == 1 else 'ies'}: {', '.join(sorted(missing))} "
        "-- your venv is likely stale after a pull (run: pip install -e .)"
    ]


def _glitchtip_container_healthy() -> bool:
    """Whether the `glitchtip` Compose container has actually passed its health check.

    Checks `health` directly rather than reusing `ComponentStatus.healthy`,
    which deliberately treats a "starting" container as healthy (see
    self_heal.py's start_period-grace comment) so the self-heal watchdog
    doesn't restart something mid-boot -- the right call there, but the wrong
    one here: a container freshly (re)started reports `state=running,
    health=starting` for its whole healthcheck `start_period`, so a caller
    waiting to know the container is *actually* ready (not just "not yet
    proven broken") needs `health == "healthy"`, or this returns as soon as
    the container exists (#3588's grafana mirror of this same bug).
    """
    for status in self_heal.list_component_status():
        if status.service == "glitchtip":
            return status.state == "running" and status.health in ("", "healthy")
    return False


def _wait_for_glitchtip_healthy(timeout: float = 120.0, poll_interval: float = 3.0) -> bool:
    """Poll until the `glitchtip` container reports healthy, or `timeout` elapses.

    Returns False immediately (no polling) if the container isn't part of the
    currently running Compose stack at all -- e.g. `--skip-observability` was
    used, Docker isn't installed, or `error_tracking` is enabled in config but
    the container was torn down (`state == "absent"`, reported by self_heal's
    desired-state reconciliation) -- so a host with no GlitchTip never stalls
    `nyxgpt ops install`, and a standalone `nyxgpt ops glitchtip-init` run
    against a torn-down stack fails fast instead of polling out the full
    `timeout` for a container nothing in this call path starts. Otherwise
    waits out its health-check `start_period` (see docker-compose.yml), since
    a container freshly started by `_start_observability_stack` is not
    immediately reachable.
    """
    statuses = [s for s in self_heal.list_component_status() if s.service == "glitchtip"]
    if not statuses or statuses[0].state == "absent":
        return False
    if statuses[0].state == "running" and statuses[0].health in ("", "healthy"):
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        if _glitchtip_container_healthy():
            return True
    return False


def _resolve_admin_credentials(cfg_path: Path) -> tuple[str, str, bool]:
    """Return `(email, password, generated)` for the GlitchTip admin user.

    Reads `[error_tracking] admin_email`/`admin_password` from `cfg_path` if
    already set; generates a strong password when none is configured. Does
    not write anything -- see `_persist_admin_credentials`.
    """
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(cfg_path)

    email = parser.get("error_tracking", "admin_email", fallback="").strip()
    email = email or GLITCHTIP_DEFAULT_ADMIN_EMAIL
    password = parser.get("error_tracking", "admin_password", fallback="").strip()
    generated = not password
    if generated:
        password = secrets.token_urlsafe(24)
    return email, password, generated


def _persist_admin_credentials(cfg_path: Path, email: str, password: str) -> None:
    """Write the (possibly generated) admin email/password back to `cfg_path`.

    Same trust model as `[auth] api_key`: GlitchTip is loopback-only, so this
    is acceptable to store in config.ini chmod 600 -- see docs/self-healing.md.
    Only ever called with the native `~/.nyxGPT/config.ini` path, never the
    derived `docker/config.docker.ini`.
    """
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(cfg_path)
    if not parser.has_section("error_tracking"):
        parser.add_section("error_tracking")
    parser.set("error_tracking", "admin_email", email)
    parser.set("error_tracking", "admin_password", password)

    with cfg_path.open("w", encoding="utf-8") as f:
        parser.write(f)
    os.chmod(cfg_path, 0o600)


def _glitchtip_ensure_superuser(email: str, password: str) -> OpsResult:
    """Idempotently ensure a GlitchTip superuser exists via Django's `createsuperuser --noinput`.

    Non-interactive: credentials are passed as the `DJANGO_SUPERUSER_*`
    environment variables Django's own `createsuperuser` management command
    reads directly, so this never blocks on a TTY prompt. `--noinput` exits
    rc=1 by design if the account already exists -- declared to `_run` as an
    expected returncode so a healthy re-run of `glitchtip-init` logs at INFO
    instead of a scary WARNING (#3574), while still treating it as success
    so re-running after the first successful run is a no-op.

    The password never touches argv (CodeQL #105/#106,
    py/clear-text-logging-sensitive-data): bare `-e VAR` flags make
    `docker compose exec` forward each variable's value from this process's
    environment (passed via `_run(env=...)`) into the container, so
    `_run`'s non-zero-exit logging -- which fires at INFO on every
    idempotent rc=1 re-run -- only ever sees the variable NAMES. The old
    `-e NAME=value` argv form slipped past `_redact_cmd`'s dash-prefixed
    flag masking and put the real password in the log `extra` on every
    re-run.
    """
    cmd = [
        "docker",
        "compose",
        "-f",
        str(self_heal.COMPOSE_FILE),
        "exec",
        "-T",
        "-e",
        "DJANGO_SUPERUSER_EMAIL",
        "-e",
        "DJANGO_SUPERUSER_PASSWORD",
        "-e",
        "DJANGO_SUPERUSER_USERNAME",
        "glitchtip",
        "./manage.py",
        "createsuperuser",
        "--noinput",
    ]
    try:
        cp = _run(
            cmd,
            check=False,
            expected_returncodes={1},
            expected_message=(
                f"GlitchTip superuser {email} already exists -- expected rc=1, "
                "treated as success"
            ),
            env={
                **os.environ,
                "DJANGO_SUPERUSER_EMAIL": email,
                "DJANGO_SUPERUSER_PASSWORD": password,
                "DJANGO_SUPERUSER_USERNAME": email,
            },
        )
    except Exception as e:
        return OpsResult(
            False, "Failed to run GlitchTip createsuperuser", f"{type(e).__name__}: {e}"
        )

    if cp.returncode == 0:
        return OpsResult(True, f"Created GlitchTip admin user {email}")

    combined = ((cp.stdout or "") + (cp.stderr or "")).lower()
    if "already" in combined or "unique" in combined:
        return OpsResult(True, f"GlitchTip admin user {email} already exists")

    details = _output_excerpt(cp)
    return OpsResult(False, "Failed to ensure GlitchTip admin user", details.strip())


def _glitchtip_http_client(base_url: str, **kwargs: Any) -> httpx.Client:
    """Construct an `httpx.Client` against GlitchTip's base URL.

    A thin wrapper purely so tests can monkeypatch it to inject an
    `httpx.MockTransport` instead of hitting a real network socket.
    """
    return httpx.Client(base_url=base_url, timeout=10.0, **kwargs)


def _glitchtip_login(
    base_url: str, email: str, password: str
) -> tuple[httpx.Client | None, OpsResult]:
    """Log into GlitchTip as `email`/`password`, returning an authenticated client.

    Modern GlitchTip (>= 5.x, verified on 6.2.0) serves django-allauth's
    headless browser API: `GET /_allauth/browser/v1/config` primes the
    `csrftoken` cookie, which is echoed back as `X-CSRFToken` on the login
    POST; the resulting session cookie carries auth forward for
    `_glitchtip_ensure_api_token` to mint a bearer token from. Older
    releases used a plain Django/DRF session view at `/api/auth/login/` --
    if the headless route 404s, this falls back to that legacy flow so the
    provisioning works across image pins.
    """

    def _csrf_headers(client: httpx.Client) -> dict[str, str]:
        headers = {"Referer": base_url}
        csrf_token = client.cookies.get("csrftoken", "")
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
        return headers

    client = _glitchtip_http_client(base_url, follow_redirects=True)
    try:
        client.get("/_allauth/browser/v1/config")
        resp = client.post(
            "/_allauth/browser/v1/auth/login",
            json={"email": email, "password": password},
            headers=_csrf_headers(client),
        )
        if resp.status_code == 404:
            client.get("/api/auth/login/")
            resp = client.post(
                "/api/auth/login/",
                json={"email": email, "password": password},
                headers=_csrf_headers(client),
            )
        if resp.status_code >= 400:
            client.close()
            return None, OpsResult(
                False,
                "Failed to authenticate to GlitchTip",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        return client, OpsResult(True, "Authenticated to GlitchTip")
    except httpx.HTTPError as e:
        client.close()
        return None, OpsResult(False, "Failed to reach GlitchTip API", f"{type(e).__name__}: {e}")


def _glitchtip_ensure_api_token(
    client: httpx.Client, base_url: str
) -> tuple[str | None, OpsResult]:
    """Mint (or reuse) a scoped GlitchTip API token for `nyxgpt ops` automation.

    Uses the authenticated session from `_glitchtip_login` for this one call
    (token creation is CSRF-guarded like any other POST); every call after
    this switches to `Authorization: Bearer <token>`.

    GlitchTip's `APIToken` model field (and the `api-tokens/` response) is
    `label`, not `name` -- posting `name` (as this function did through
    #3565 round 5) silently drops on the floor (`APITokenIn`'s schema only
    accepts `label`/`scopes`), so every token is created with `label: ""`.
    The very next run's reuse lookup then matches on `tok.get("name")`,
    which is *never* present in the response, so it can never find a match
    either -- every `glitchtip-init`/`install`/`env-sync` run minted a brand
    new orphaned token, silently defeating the "idempotent" contract
    (verified live: booted a real GlitchTip 6.2.0 instance, ran the
    pre-fix code twice, got two distinct tokens both labeled ""). Posting
    `label` and reusing by `label` fixes both sides of that bug.

    A reused token whose stored `scopes` don't cover every scope in
    `GLITCHTIP_TOKEN_SCOPES` (e.g. an older token from before a scope was
    added here) is deleted and re-minted -- GlitchTip's API has no token
    PUT/PATCH, only create/delete, so upgrading scopes means replacing it.
    """
    try:
        csrf_token = client.cookies.get("csrftoken", "")
        headers = {"Referer": base_url}
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token

        listing = client.get("/api/0/api-tokens/")
        if listing.status_code == 200:
            existing: Any = listing.json()
            if isinstance(existing, list):
                for tok in existing:
                    if not isinstance(tok, dict):
                        continue
                    if tok.get("label") != GLITCHTIP_TOKEN_NAME or not tok.get("token"):
                        continue
                    if set(GLITCHTIP_TOKEN_SCOPES) <= set(tok.get("scopes") or []):
                        return str(tok["token"]), OpsResult(
                            True, "Reusing existing GlitchTip API token"
                        )
                    token_id = tok.get("id")
                    if token_id is not None:
                        client.delete(f"/api/0/api-tokens/{token_id}/", headers=headers)
                    break

        resp = client.post(
            "/api/0/api-tokens/",
            json={"label": GLITCHTIP_TOKEN_NAME, "scopes": GLITCHTIP_TOKEN_SCOPES},
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            return None, OpsResult(
                False,
                "Failed to create GlitchTip API token",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        data: Any = resp.json()
        token = None
        if isinstance(data, dict):
            token = data.get("token") or data.get("key")
        if not token:
            return None, OpsResult(
                False, "GlitchTip API token response missing a token", str(data)[:500]
            )
        return str(token), OpsResult(True, "Created GlitchTip API token")
    except httpx.HTTPError as e:
        return None, OpsResult(
            False, "Failed to create GlitchTip API token", f"{type(e).__name__}: {e}"
        )


def _glitchtip_ensure_organization(client: httpx.Client) -> tuple[str | None, OpsResult]:
    """Ensure the `nyxgpt` GlitchTip organization exists, returning its slug."""
    try:
        listing = client.get("/api/0/organizations/")
        if listing.status_code == 200:
            existing: Any = listing.json()
            if isinstance(existing, list):
                for org in existing:
                    if isinstance(org, dict) and org.get("slug") == GLITCHTIP_ORG_SLUG:
                        return GLITCHTIP_ORG_SLUG, OpsResult(
                            True, f"Using existing GlitchTip organization {GLITCHTIP_ORG_SLUG}"
                        )

        resp = client.post("/api/0/organizations/", json={"name": GLITCHTIP_ORG_NAME})
        if resp.status_code not in (200, 201):
            return None, OpsResult(
                False,
                "Failed to create GlitchTip organization",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        data: Any = resp.json()
        slug = (
            str(data.get("slug", GLITCHTIP_ORG_SLUG))
            if isinstance(data, dict)
            else GLITCHTIP_ORG_SLUG
        )
        return slug, OpsResult(True, f"Created GlitchTip organization {slug}")
    except httpx.HTTPError as e:
        return None, OpsResult(
            False, "Failed to ensure GlitchTip organization", f"{type(e).__name__}: {e}"
        )


def _glitchtip_ensure_team(client: httpx.Client, org_slug: str) -> tuple[str | None, OpsResult]:
    """Ensure the `nyxgpt` GlitchTip team exists under `org_slug`, returning its slug.

    Modern GlitchTip (verified on 6.2.0) only creates projects under a team
    (the org-level projects route is list-only), so provisioning needs a team
    before `_glitchtip_ensure_project` can create anything.
    """
    try:
        listing = client.get(f"/api/0/organizations/{org_slug}/teams/")
        if listing.status_code == 200:
            existing: Any = listing.json()
            if isinstance(existing, list):
                for team in existing:
                    if isinstance(team, dict) and team.get("slug") == GLITCHTIP_TEAM_SLUG:
                        return GLITCHTIP_TEAM_SLUG, OpsResult(
                            True, f"Using existing GlitchTip team {GLITCHTIP_TEAM_SLUG}"
                        )

        resp = client.post(
            f"/api/0/organizations/{org_slug}/teams/",
            json={"slug": GLITCHTIP_TEAM_SLUG},
        )
        if resp.status_code not in (200, 201):
            return None, OpsResult(
                False,
                "Failed to create GlitchTip team",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        data: Any = resp.json()
        slug = (
            str(data.get("slug", GLITCHTIP_TEAM_SLUG))
            if isinstance(data, dict)
            else GLITCHTIP_TEAM_SLUG
        )
        return slug, OpsResult(True, f"Created GlitchTip team {slug}")
    except httpx.HTTPError as e:
        return None, OpsResult(False, "Failed to ensure GlitchTip team", f"{type(e).__name__}: {e}")


def _glitchtip_ensure_team_membership(
    client: httpx.Client, org_slug: str, team_slug: str
) -> OpsResult:
    """Ensure the provisioning admin is a member of `team_slug`, idempotently.

    GlitchTip's UI only lists projects on teams the logged-in user belongs
    to -- a superuser who is an org member but not on any team still sees
    "This organization has no projects" (#3565 round 5 acceptance failure,
    live-diagnosed by the owner via Django admin). `_glitchtip_ensure_team`
    creating a brand-new team already adds the requesting user (verified by
    reading GlitchTip's `create_team` view: it `team.members.aadd()`s the
    creator's `OrganizationUser`), so this only actually does work the first
    time a *pre-existing* team is reused by a different/new admin. It's
    cheap and idempotent (`team.members.aadd()` is a no-op if already a
    member) so it's called unconditionally rather than trying to detect
    which case applies.

    Uses the join endpoint's `me` alias
    (`POST /organizations/{org}/members/me/teams/{team}/`) rather than
    looking up a member id, and requires the `team:write` scope on the
    client's token (see `GLITCHTIP_TOKEN_SCOPES`) -- without it this 403s.
    """
    try:
        resp = client.post(f"/api/0/organizations/{org_slug}/members/me/teams/{team_slug}/")
        if resp.status_code in (200, 201):
            return OpsResult(True, f"Confirmed GlitchTip team membership on {team_slug}")
        return OpsResult(
            False,
            "Failed to confirm GlitchTip team membership",
            f"HTTP {resp.status_code}: {resp.text[:500]}",
        )
    except httpx.HTTPError as e:
        return OpsResult(
            False, "Failed to confirm GlitchTip team membership", f"{type(e).__name__}: {e}"
        )


def _glitchtip_ensure_project(
    client: httpx.Client, org_slug: str, team_slug: str
) -> tuple[str | None, OpsResult]:
    """Ensure the `nyxgpt-backend` GlitchTip project exists under `org_slug`, returning its slug.

    Creates via the team-scoped route (`/api/0/teams/{org}/{team}/projects/`),
    the only creation path modern GlitchTip serves; falls back to the legacy
    org-level POST if the team route is absent on an older image.
    """
    try:
        listing = client.get(f"/api/0/organizations/{org_slug}/projects/")
        if listing.status_code == 200:
            existing: Any = listing.json()
            if isinstance(existing, list):
                for proj in existing:
                    if isinstance(proj, dict) and proj.get("slug") == GLITCHTIP_PROJECT_SLUG:
                        return GLITCHTIP_PROJECT_SLUG, OpsResult(
                            True, f"Using existing GlitchTip project {GLITCHTIP_PROJECT_SLUG}"
                        )

        resp = client.post(
            f"/api/0/teams/{org_slug}/{team_slug}/projects/",
            json={"name": GLITCHTIP_PROJECT_NAME, "platform": "python"},
        )
        if resp.status_code in (404, 405):
            resp = client.post(
                f"/api/0/organizations/{org_slug}/projects/",
                json={"name": GLITCHTIP_PROJECT_NAME, "platform": "python"},
            )
        if resp.status_code not in (200, 201):
            return None, OpsResult(
                False,
                "Failed to create GlitchTip project",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        data: Any = resp.json()
        slug = (
            str(data.get("slug", GLITCHTIP_PROJECT_SLUG))
            if isinstance(data, dict)
            else GLITCHTIP_PROJECT_SLUG
        )
        return slug, OpsResult(True, f"Created GlitchTip project {slug}")
    except httpx.HTTPError as e:
        return None, OpsResult(
            False, "Failed to ensure GlitchTip project", f"{type(e).__name__}: {e}"
        )


def _extract_dsn(key: Any) -> str:
    """Pull the public DSN out of a GlitchTip ProjectKey response, tolerating shape variants."""
    if not isinstance(key, dict):
        return ""
    dsn_field = key.get("dsn")
    if isinstance(dsn_field, dict):
        return str(dsn_field.get("public") or "")
    if isinstance(dsn_field, str):
        return dsn_field
    return ""


def _glitchtip_ensure_project_key(
    client: httpx.Client, org_slug: str, project_slug: str
) -> tuple[str | None, OpsResult]:
    """Ensure a GlitchTip project key exists for `org_slug`/`project_slug`, returning its DSN."""
    try:
        listing = client.get(f"/api/0/projects/{org_slug}/{project_slug}/keys/")
        if listing.status_code == 200:
            existing: Any = listing.json()
            if isinstance(existing, list):
                for key in existing:
                    dsn = _extract_dsn(key)
                    if dsn:
                        return dsn, OpsResult(True, "Using existing GlitchTip project key")

        resp = client.post(
            f"/api/0/projects/{org_slug}/{project_slug}/keys/", json={"name": "nyxgpt"}
        )
        if resp.status_code not in (200, 201):
            return None, OpsResult(
                False,
                "Failed to create GlitchTip project key",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
        data: Any = resp.json()
        dsn = _extract_dsn(data)
        if not dsn:
            return None, OpsResult(
                False, "GlitchTip project key response missing a DSN", str(data)[:500]
            )
        return dsn, OpsResult(True, "Created GlitchTip project key")
    except httpx.HTTPError as e:
        return None, OpsResult(
            False, "Failed to ensure GlitchTip project key", f"{type(e).__name__}: {e}"
        )


def _patch_ini_value(text: str, section: str, key: str, value: str) -> str:
    """Return `text` with `key = value` set inside `[section]`, preserving
    every other line -- including comments -- verbatim.

    Unlike a `ConfigParser` read/write round-trip (which drops comments),
    this only ever rewrites the single matching `key = ...` line, or
    appends one if the key/section isn't present yet. Used for
    `docker/config.docker.ini`, whose `[error_tracking]` section carries
    hand-written documentation comments (seeded from its tracked `.example`
    template) that must survive `nyxgpt ops glitchtip-init` re-runs.
    """
    lines = text.splitlines(keepends=True)
    section_header_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    key_re = re.compile(rf"^(\s*){re.escape(key)}(\s*=\s*).*$")

    section_start: int | None = None
    section_end = len(lines)
    for i, line in enumerate(lines):
        m = section_header_re.match(line)
        if m:
            if section_start is not None:
                section_end = i
                break
            if m.group(1) == section:
                section_start = i

    if section_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(f"[{section}]\n")
        lines.append(f"{key} = {value}\n")
        return "".join(lines)

    for i in range(section_start + 1, section_end):
        if key_re.match(lines[i]):
            lines[i] = f"{key} = {value}\n"
            return "".join(lines)

    lines.insert(section_end, f"{key} = {value}\n")
    return "".join(lines)


def _containerized_error_tracking_dsn(dsn: str) -> str:
    """Rewrite a GlitchTip DSN's host:port for a containerized api to use.

    GlitchTip mints DSNs from its own `GLITCHTIP_DOMAIN` env var
    (`http://localhost:${GLITCHTIP_UI_PORT}` -- see docker-compose.yml's
    `glitchtip` service), because that's what a browser opening an issue link
    needs. But a *containerized* api (Compose `--profile errors` or
    `terraform ... --local`, both consuming `docker/config.docker.ini`) reads
    that same DSN to send events server-side -- inside the container network,
    `localhost` resolves to the api container itself, not the docker host.
    `sentry_sdk`'s HTTP transport is fire-and-forget, so the connection
    failure is silently swallowed: `capture_exception` returns normally and
    nothing ever reaches GlitchTip (#3565 round 5 -- confirmed live: the
    error-tracking endpoint reported 202 while GlitchTip received nothing).

    Points the DSN at the `glitchtip` service's network alias and
    container-internal port instead, which every containerized deploy mode
    shares a docker network with (`_COMPOSE_CONFIG_OVERRIDES` makes the same
    assumption for `ollama`/`cassandra`). Falls back to returning `dsn`
    unchanged if it doesn't parse as a URL with a host.
    """
    try:
        url = httpx.URL(dsn)
    except Exception:
        return dsn
    if not url.host:
        return dsn
    return str(url.copy_with(host=GLITCHTIP_CONTAINER_HOST, port=GLITCHTIP_CONTAINER_PORT))


def _current_error_tracking_dsn(cfg_path: Path) -> str:
    """Return the `[error_tracking] dsn` currently in `cfg_path`, or `""` if
    the file/section/key is absent -- used by `_provision_glitchtip` to
    detect a DSN change (re-minted project/key) before `nyxgpt-api` has
    reloaded it, see `_restart_native_api_if_running`."""
    if not cfg_path.exists():
        return ""
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(cfg_path)
    return parser.get("error_tracking", "dsn", fallback="").strip()


def _write_error_tracking_dsn(cfg_path: Path, dsn: str, *, chmod_600: bool) -> OpsResult:
    """Write the provisioned DSN and `enabled = true` into `[error_tracking]` in `cfg_path`.

    No-ops (reports ok) if `cfg_path` doesn't exist -- not every host has
    both `~/.nyxGPT/config.ini` (native) and `docker/config.docker.ini`
    (Compose) in play. `chmod_600` should only be set for the native path:
    the live `docker/config.docker.ini` is a derived, git-ignored artifact
    seeded from a tracked template, not a secrets file -- the DSN is a public
    key, safe to store there (see docs/self-healing.md) -- so its permissions
    are left untouched.

    Patches only the `dsn`/`enabled` lines in place (via `_patch_ini_value`)
    rather than round-tripping through `ConfigParser`, so any comments in
    `cfg_path` -- notably the documentation comments in
    `docker/config.docker.ini` -- survive untouched.
    """
    if not cfg_path.exists():
        return OpsResult(True, f"Skipped {cfg_path} (not present)")

    text = cfg_path.read_text(encoding="utf-8")
    text = _patch_ini_value(text, "error_tracking", "dsn", dsn)
    text = _patch_ini_value(text, "error_tracking", "enabled", "true")
    cfg_path.write_text(text, encoding="utf-8")

    if chmod_600:
        os.chmod(cfg_path, 0o600)

    return OpsResult(True, f"Wrote GlitchTip DSN into {cfg_path}")


def _write_grafana_glitchtip_token(token: str) -> tuple[bool, OpsResult]:
    """Write `token` to `_glitchtip_grafana_token_path()` for Grafana's Infinity datasource.

    Returns `(changed, result)` -- `changed` is False when the file already
    holds this exact token, which callers use to skip an unnecessary Grafana
    restart (Grafana only re-reads provisioning files, including `$__file{}`
    targets, at startup).

    `_ensure_glitchtip_secrets_dir` runs earlier in `install()`/
    `_install_terraform_steps` specifically so this doesn't hit a
    root-owned bind-mount directory (#3432), but this still guards the
    write with its own try/except -- e.g. a standalone `nyxgpt ops
    glitchtip-init` run skips that preflight -- so a permission problem
    surfaces as this actionable `OpsResult` instead of an uncaught
    `PermissionError` traceback bubbling up through `_provision_glitchtip`.

    File mode is `0o644`, not `0o600` (#3588): Grafana's official image
    runs as non-root uid 472, and a native Linux bind mount preserves host
    uid/gid checks (no user-namespace remap), so a `0o600` file owned by
    the host user is unreadable by Grafana's container process -- Grafana
    fails to boot with a `stat ...: permission denied` on its GlitchTip
    datasource provisioning. `0o644` keeps write access restricted to the
    owning host user while letting any uid read it, matching the parent
    directory's `0o755` in `_ensure_glitchtip_secrets_dir`.
    """
    path = _glitchtip_grafana_token_path()
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == token:
            return False, OpsResult(True, f"{path} already holds the current GlitchTip token")

        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        os.chmod(path, 0o644)
    except OSError as e:
        result = _glitchtip_secrets_dir_unwritable_result(path.parent)
        return False, OpsResult(
            False,
            f"Cannot write GlitchTip token to {path}",
            f"{result.details}\n{type(e).__name__}: {e}",
        )
    return True, OpsResult(True, f"Wrote GlitchTip API token for Grafana to {path}")


def _grafana_container_healthy() -> bool:
    """Whether the `grafana` Compose container has actually passed its health check.

    Checks `health` directly rather than reusing `ComponentStatus.healthy`
    -- see `_glitchtip_container_healthy`'s docstring for why that flag's
    "starting counts as healthy" semantics (correct for the self-heal
    watchdog) are wrong for this caller. This was the actual cause of the
    `terraform-local-smoke` CI race (#3588 review round 2): right after
    `docker compose restart grafana`, `_wait_for_grafana_healthy` polled
    `ComponentStatus.healthy`, which was already `True` while the container
    was still in its healthcheck `start_period` (`state=running,
    health=starting`) -- so the restart was reported done and the install
    command returned before Grafana was actually reachable, and the smoke
    test's immediate curl hit connection-refused.
    """
    for status in self_heal.list_component_status():
        if status.service == "grafana":
            return status.state == "running" and status.health in ("", "healthy")
    return False


def _wait_for_grafana_healthy(timeout: float = 120.0, poll_interval: float = 3.0) -> bool:
    """Poll until the `grafana` container reports healthy, or `timeout` elapses.

    Mirrors `_wait_for_glitchtip_healthy` (#3432): returns False immediately
    (no polling) if `grafana` isn't part of the currently running Compose
    stack at all, so a host without the observability stack never stalls.
    Otherwise waits out Grafana's healthcheck `start_period` (see
    docker-compose.yml's `grafana` service) since a container just
    (re)started is not immediately reachable -- and, critically, a container
    stuck crash-looping (#3538, e.g. an alerting-provisioning file Grafana
    can't validate) never reports healthy, so this returns False instead of
    hanging forever, letting callers surface that as an actionable failure
    instead of reporting a restart as OK when Grafana never actually comes
    back up.
    """
    statuses = [s for s in self_heal.list_component_status() if s.service == "grafana"]
    if not statuses or statuses[0].state == "absent":
        return False
    if statuses[0].state == "running" and statuses[0].health in ("", "healthy"):
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        if _grafana_container_healthy():
            return True
    return False


def _restart_grafana_if_running(reason: str = "the new GlitchTip token") -> OpsResult:
    """Restart the `grafana` Compose container if its container exists.

    Grafana only reads `$__file{}` provisioning targets (the GlitchTip
    Infinity token, see `_write_grafana_glitchtip_token`, and the Slack
    alerting webhook, see `_write_grafana_slack_webhook_secret`) at startup,
    so a freshly (re)written secret needs this to actually take effect.

    Only skips when the container is entirely `"absent"` (no container to
    restart at all, e.g. the observability profile was never started) --
    NOT merely non-`"running"`. On a from-scratch install, Grafana's
    GlitchTip-Infinity datasource provisioning fails and crash-loops
    (`state="exited"`) before `_provision_glitchtip` has written the token
    this restart exists to deliver; skipping on anything but "running" left
    it dead for good instead of bringing it back (#3588's `terraform-local-
    smoke` regression). `docker compose restart` covers both a running and
    an already-stopped container -- there's no need to branch to `up -d`.

    Waits for Grafana to report healthy again before returning (#3538) --
    without this, a restart that leaves Grafana crash-looping (e.g. a broken
    alerting-provisioning file) was previously reported as a plain "OK,
    restarted", with the crash loop only surfacing later, misleadingly, as
    an unrelated-looking credential-verify failure.
    """
    if not _compose_available():
        return OpsResult(True, "Skipped Grafana restart (Docker not found)")

    running = _compose_stack_snapshot()
    if running.get("grafana", "absent") == "absent":
        return OpsResult(True, "Skipped Grafana restart (not running)")

    cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE), "restart", "grafana"]
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return OpsResult(False, "Failed to restart Grafana", _output_excerpt(cp))

    if not _wait_for_grafana_healthy():
        return OpsResult(
            False,
            f"Restarted Grafana to pick up {reason}, but it never became healthy again",
            "Check `nyxgpt ops status` (a compose service stuck `restarting` is the tell) "
            "and `nyxgpt ops logs grafana` for the boot error.",
        )
    return OpsResult(True, f"Restarted Grafana to pick up {reason}")


def _restart_native_api_if_running(reason: str = "the new GlitchTip DSN") -> OpsResult:
    """Restart the native `nyxgpt-api` Homebrew service if it's currently running.

    The API's error-tracking SDK reads `[error_tracking] dsn` from
    `~/.nyxGPT/config.ini` once, at process startup -- it does not hot-reload
    config.ini. If `_provision_glitchtip` re-mints the GlitchTip project/key
    (e.g. after a `down`+`install` cycle against a fresh GlitchTip database)
    the DSN written to config.ini changes, but a running `nyxgpt-api` keeps
    reporting to the *old* DSN until restarted -- silently dropping every
    event from then on. Mirrors `_restart_grafana_if_running` for the
    Grafana Infinity token. No-ops (not a failure) when `nyxgpt-api` isn't
    running as a native brew service -- a Compose-deployed `nyxgpt-api`
    reads its DSN from `docker/config.docker.ini` fresh on each container
    start and restarts through `nyxgpt ops restart api` like any other
    Compose service.
    """
    snapshot = _brew_services_snapshot()
    # Resolved: on a candidate install the service holding the DSN-reading
    # process is `nyxgpt-api@<line>rc`, so the stable-name lookup answered
    # "not running natively" and the API kept reporting to the dead DSN
    # forever (#3853).
    service = _resolved_brew_service("api", snapshot)
    if snapshot.get(service) not in brew_services.LIVE_STATES:
        return OpsResult(True, "Skipped nyxgpt-api restart (not running natively)")

    result = _restart_brew_service(service)[0]
    if not result.ok:
        return result
    return OpsResult(True, f"Restarted {service} to pick up {reason}")


def _slack_webhook_secret_path() -> Path:
    """Where the Slack incoming-webhook URL is written for Grafana's alerting
    contact point to read (#3466) -- see
    docker/grafana/provisioning/alerting/contact-points.yml's `$__file{}`
    reference. Lives in the same `~/.nyxGPT/secrets` directory as the
    GlitchTip Grafana token (`_glitchtip_grafana_token_path`), so the
    existing `_ensure_glitchtip_secrets_dir` preflight (#3432) already
    guards this write against the root-owned bind-mount directory failure
    mode too.

    Computed fresh on every call, not a module-level constant, so tests can
    monkeypatch `Path.home`.
    """
    return Path.home() / ".nyxGPT" / "secrets" / "slack-webhook-url"


# Written to the secret file in place of an empty string when [monitoring]
# slack_webhook_url is unset (#3538). Grafana's alerting-provisioning
# validator requires a Slack integration to have a non-empty url/token/
# recipient -- an empty url doesn't mean "configured but broken", it fails
# validation identically to a missing one and crash-loops the whole Grafana
# container. This placeholder is syntactically a well-formed Slack webhook
# URL (satisfies validation, so Grafana boots) but not a real one -- delivery
# to it fails at send time, visible under Alerting -> Contact points -> Test,
# which is the "alerts still fire, Slack delivery silently fails" behavior
# #3466 actually intended.
GRAFANA_SLACK_WEBHOOK_PLACEHOLDER_URL = (
    "https://hooks.slack.com/services/UNCONFIGURED/UNCONFIGURED/UNCONFIGURED"
)


def _write_grafana_slack_webhook_secret(url: str) -> tuple[bool, OpsResult]:
    """Write `url` to `_slack_webhook_secret_path()` for Grafana's
    `nyxgpt-slack` contact point -- `GRAFANA_SLACK_WEBHOOK_PLACEHOLDER_URL`
    when `url` is empty/unset.

    Never writes an empty string, even though `url` may be `""` -- the
    contact point's `$__file{}` reference must resolve to a non-empty,
    syntactically-valid webhook URL at Grafana startup, or Grafana's
    alerting-provisioning validator refuses to boot the container entirely
    (#3538: confirmed by booting grafana/grafana:13.1.1 against this exact
    provisioning with an empty secret file -- it crash-loops, it does not
    degrade to "Slack delivery fails silently" as originally intended by
    #3466). The placeholder preserves that original intent instead: Grafana
    boots, and only the (already-broken, unconfigured) Slack delivery fails,
    visibly, under Grafana's Alerting -> Contact points -> Test.

    Returns `(changed, result)` -- `changed` is False when the file already
    holds this exact value, which callers use to skip an unnecessary
    Grafana restart (mirrors `_write_grafana_glitchtip_token`).

    File/dir modes are `0o644`/`0o755`, not `0o600`/`0o700`, for the same
    reason as `_write_grafana_glitchtip_token` (#3588): Grafana's
    non-root container uid needs to read this file across a native Linux
    bind mount.
    """
    path = _slack_webhook_secret_path()
    resolved = url.strip() or GRAFANA_SLACK_WEBHOOK_PLACEHOLDER_URL
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == resolved:
            return False, OpsResult(True, f"{path} already holds the current Slack webhook URL")

        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_text(resolved, encoding="utf-8")
        os.chmod(path, 0o644)
    except OSError as e:
        result = _glitchtip_secrets_dir_unwritable_result(path.parent)
        return False, OpsResult(
            False,
            f"Cannot write Slack webhook URL to {path}",
            f"{result.details}\n{type(e).__name__}: {e}",
        )
    if url.strip():
        return True, OpsResult(True, f"Wrote Slack webhook URL for Grafana at {path}")
    return True, OpsResult(
        True,
        f"Wrote placeholder Slack webhook URL for Grafana at {path} "
        "(no [monitoring] slack_webhook_url configured)",
    )


def _sync_grafana_slack_webhook_secret(cfg_path: Path | None = None) -> list[OpsResult]:
    """Provision Grafana's Slack contact point secret from config.ini's
    `[monitoring] slack_webhook_url` (#3466).

    Run from both `install()` and `env_sync()` so the webhook is picked up
    whether the user runs the full native install or just the Compose-only
    Quickstart's `nyxgpt ops env-sync` before `docker compose up`.
    """
    from nyxgpt.config import load_config

    cfg_path = cfg_path or (Path.home() / ".nyxGPT" / "config.ini")
    if not cfg_path.exists():
        return [OpsResult(True, "Skipped Slack webhook sync (no config.ini yet)")]
    cfg = load_config(cfg_path)

    url = get_monitoring_slack_webhook_url(cfg)
    changed, write_result = _write_grafana_slack_webhook_secret(url)
    results = [write_result]
    if changed:
        results.append(_restart_grafana_if_running("the new Slack webhook URL"))
    return results


# The `nyxgpt-slack` contact point / integration provisioned by
# docker/grafana/provisioning/alerting/contact-points.yml -- `alert_test`
# needs both to address Grafana's receiver-test API directly (#3545).
GRAFANA_SLACK_CONTACT_POINT_NAME = "nyxgpt-slack"
GRAFANA_SLACK_INTEGRATION_UID = "nyxgpt-slack-receiver"


def _grafana_receiver_k8s_name(contact_point_name: str) -> str:
    """Grafana's alerting-notifications app addresses a legacy-provisioned
    contact point by `base64.RawURLEncoding(name)` -- see `NameToUid` /
    `convertToK8sResource` in grafana/grafana's
    pkg/services/ngalert/models/receivers.go and
    pkg/registry/apps/alerting/notifications/receiver/conversions.go.
    Confirmed live by booting grafana/grafana:13.1.1 (the pinned image)
    against this repo's exact provisioning and listing
    `/apis/notifications.alerting.grafana.app/v0alpha1/namespaces/default/receivers`
    (#3545): `nyxgpt-slack` -> `bnl4Z3B0LXNsYWNr`.
    """
    return base64.urlsafe_b64encode(contact_point_name.encode()).rstrip(b"=").decode()


def _send_grafana_test_alert(
    grafana_ui_url: str, grafana_admin_password: str, webhook_configured: bool
) -> OpsResult:
    """POST a test notification through the `nyxgpt-slack` receiver via
    Grafana's receiver-test API -- the same
    `/apis/notifications.alerting.grafana.app/.../receivers/{name}/test`
    endpoint Grafana's own Alerting -> Contact points -> Test button calls
    (#3545).

    #3466 originally posted straight into Grafana's embedded Alertmanager
    ingestion API (`/api/alertmanager/grafana/api/v2/alerts`), which only
    accepts alerts from Grafana's own rule engine and 400s on anything
    posted externally. The legacy contact-point test route that would have
    worked instead (`/api/alertmanager/grafana/config/api/v1/receivers/test`)
    also doesn't help -- it returns 410 Gone on the pinned grafana/grafana
    image, having been replaced by this one. This proves delivery through
    the contact point, not rule evaluation or notification-policy routing --
    see docs/alerting.md#testing-the-pipeline for the full-path (deliberate
    threshold breach) alternative that covers those.

    `webhook_configured` is the caller's own read of config.ini's
    `[monitoring] slack_webhook_url`. Grafana redacts/encrypts the `url`
    setting in every API response (it's schema-flagged secure regardless of
    which YAML section provisions it -- see contact-points.yml), so there is
    no way to distinguish "no webhook configured" from "webhook configured
    but wrong" by inspecting the test response alone; the caller's own
    config read is the only source of truth for that distinction.
    """
    receiver_name = _grafana_receiver_k8s_name(GRAFANA_SLACK_CONTACT_POINT_NAME)
    payload = {
        "integration": {
            "uid": GRAFANA_SLACK_INTEGRATION_UID,
            "type": "slack",
            "settings": {},
            # Reuse the currently-provisioned (possibly placeholder) `url`
            # secret rather than resupplying it -- this CLI has no access to
            # the secret file's contents, nor should it need any.
            "secureFields": {"url": True},
        },
        "alert": {
            "labels": {"alertname": "NyxGPTAlertTest", "severity": "warning"},
            "annotations": {"summary": "Test alert triggered by `nyxgpt ops alert-test`"},
        },
    }
    try:
        with _grafana_admin_client(grafana_ui_url, grafana_admin_password) as client:
            resp = client.post(
                "/apis/notifications.alerting.grafana.app/v0alpha1/namespaces/default/"
                f"receivers/{receiver_name}/test",
                json=payload,
            )
    except httpx.HTTPError as e:
        return OpsResult(
            False,
            "Failed to reach Grafana's receiver-test API",
            f"{type(e).__name__}: {e}",
        )
    if resp.status_code != 200:
        return OpsResult(
            False,
            "Grafana rejected the nyxgpt-slack contact-point test request",
            f"HTTP {resp.status_code}: {resp.text[:500]}",
        )
    data = resp.json()
    if data.get("status") == "success":
        return OpsResult(
            True,
            "Sent a test notification through the nyxgpt-slack contact point -- "
            "check Slack for delivery.",
        )
    error = str(data.get("error") or "Grafana reported failure with no error message")
    if not webhook_configured:
        return OpsResult(
            True,
            "Alerting pipeline is intact -- Grafana reached the nyxgpt-slack contact "
            "point and attempted delivery, but no [monitoring] slack_webhook_url is "
            "configured in config.ini, so delivery to the placeholder webhook failed "
            "as expected.",
            error[:500],
        )
    return OpsResult(False, "Slack delivery test failed", error[:500])


def alert_test(_args: Any) -> int:
    """CLI entrypoint for `nyxgpt ops alert-test`.

    Pushes a test notification through the `nyxgpt-slack` contact point via
    Grafana's receiver-test API -- the acceptance test for Slack delivery:
    confirms the receiver and webhook are wired correctly without waiting
    for a real CPU/memory/disk/self-heal/canary threshold breach. This
    covers contact-point delivery only, not rule evaluation or
    notification-policy routing -- see docs/alerting.md#testing-the-pipeline
    for the full-path alternative. No-ops with a clear, actionable message
    if monitoring is disabled, unset up, or Grafana isn't reachable.

    Returns 0 on success, else 2.
    """
    from nyxgpt.config import load_config

    logger.info("ops: alert-test starting", extra={"component": "ops", "action": "alert-test"})

    cfg_path = Path.home() / ".nyxGPT" / "config.ini"
    if not cfg_path.exists():
        results = [
            OpsResult(
                False,
                f"Missing config {cfg_path}",
                "Run `nyxgpt wizard` first to generate config.ini.",
            )
        ]
    else:
        cfg = load_config(cfg_path)
        monitoring = get_monitoring_config(cfg)
        if not monitoring["enabled"]:
            results = [
                OpsResult(
                    False,
                    "Monitoring is disabled",
                    "Set [monitoring] enabled = true in config.ini, then run `nyxgpt ops "
                    "install` (or start the `monitoring` Compose profile) before testing "
                    "alerts.",
                )
            ]
        else:
            grafana_admin_password = _grafana_admin_password(cfg)
            webhook_configured = bool(get_monitoring_slack_webhook_url(cfg).strip())
            results = [
                _send_grafana_test_alert(
                    monitoring["grafana_ui_url"], grafana_admin_password, webhook_configured
                )
            ]

    ok = _emit_results("alert-test", results)
    logger.info(
        "ops: alert-test %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "alert-test", "ok": ok},
    )
    return 0 if ok else 2


def _provision_glitchtip() -> list[OpsResult]:
    """Auto-provision a GlitchTip admin user, org, project, and DSN -- idempotent, zero-touch.

    Only runs when the `glitchtip` Compose container is up and passes its
    health check; no-ops with a clear message otherwise (e.g.
    `--skip-observability`, no Docker, or a freshly started container still
    inside its health-check `start_period`). Safe to call repeatedly: every
    step first checks for an existing admin user / org / project / key
    before creating one, so re-running never duplicates anything.
    """
    if not _compose_available():
        return [OpsResult(True, "Skipped GlitchTip auto-provisioning (Docker not found)")]

    if not _wait_for_glitchtip_healthy():
        return [
            OpsResult(
                True,
                "Skipped GlitchTip auto-provisioning (glitchtip container not up/healthy)",
                "Run `nyxgpt ops observability` (or `nyxgpt ops install`) to start it, then "
                "retry `nyxgpt ops glitchtip-init`.",
            )
        ]

    native_cfg_path = Path.home() / ".nyxGPT" / "config.ini"
    if not native_cfg_path.exists():
        return [
            OpsResult(
                False,
                f"Missing config {native_cfg_path}",
                "Run `nyxgpt wizard` first to generate config.ini.",
            )
        ]

    results: list[OpsResult] = []

    email, password, generated = _resolve_admin_credentials(native_cfg_path)
    if generated:
        _persist_admin_credentials(native_cfg_path, email, password)
        results.append(
            OpsResult(True, f"Generated and saved a GlitchTip admin password to {native_cfg_path}")
        )

    su_result = _glitchtip_ensure_superuser(email, password)
    results.append(su_result)
    if not su_result.ok:
        return results

    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(native_cfg_path)
    base_url = parser.get("error_tracking", "glitchtip_ui_url", fallback="http://localhost:8080")

    login_client, login_result = _glitchtip_login(base_url, email, password)
    results.append(login_result)
    if login_client is None:
        return results

    token: str | None = None
    try:
        token, token_result = _glitchtip_ensure_api_token(login_client, base_url)
        results.append(token_result)
    finally:
        login_client.close()
    if token is None:
        return results

    api_client = _glitchtip_http_client(base_url, headers={"Authorization": f"Bearer {token}"})
    dsn: str | None = None
    try:
        org_slug, org_result = _glitchtip_ensure_organization(api_client)
        results.append(org_result)
        if org_slug is None:
            return results

        team_slug, team_result = _glitchtip_ensure_team(api_client, org_slug)
        results.append(team_result)
        if team_slug is None:
            return results

        results.append(_glitchtip_ensure_team_membership(api_client, org_slug, team_slug))

        project_slug, project_result = _glitchtip_ensure_project(api_client, org_slug, team_slug)
        results.append(project_result)
        if project_slug is None:
            return results

        dsn, key_result = _glitchtip_ensure_project_key(api_client, org_slug, project_slug)
        results.append(key_result)
        if dsn is None:
            return results
    finally:
        api_client.close()

    previous_native_dsn = _current_error_tracking_dsn(native_cfg_path)
    results.append(_write_error_tracking_dsn(native_cfg_path, dsn, chmod_600=True))
    results.append(
        _write_error_tracking_dsn(
            COMPOSE_CONFIG_FILE, _containerized_error_tracking_dsn(dsn), chmod_600=False
        )
    )
    if dsn != previous_native_dsn:
        results.append(_restart_native_api_if_running())

    token_changed, token_write_result = _write_grafana_glitchtip_token(token)
    results.append(token_write_result)
    if token_changed:
        results.append(_restart_grafana_if_running())

    return results


# --- The Kubernetes half of GlitchTip provisioning (#3990) ---
#
# `nyxgpt ops glitchtip-init` provisions GlitchTip for the Compose/native
# deploys and had no Kubernetes equivalent, so every `--kubernetes` install
# ran without the two values that provisioning produces:
#
#   * Grafana's Infinity datasource authenticated with the PLACEHOLDER token
#     the manifest ships (deliberately non-empty -- an empty `$__file{}`
#     target crash-loops Grafana's alerting validator, #3538), so every SRE
#     Home GlitchTip panel answered `401 Unauthorized`;
#   * the api had no DSN at all, so nothing was ever reported to the
#     in-cluster GlitchTip -- those panels would have been empty even with a
#     valid token.
#
# The provisioning ITSELF is shared with the Compose path, deliberately:
# everything from `_glitchtip_login` down is plain HTTP against GlitchTip's
# API and knows nothing about how the process was reached. Only the two ends
# differ, and they are what this section supplies -- how to run
# `createsuperuser` in a Pod instead of a container, how to reach the API on
# a ClusterIP-only Service, and where the provisioned values have to land
# (Kubernetes Secrets, not files on the host).

K8S_GLITCHTIP_DEPLOYMENT = "glitchtip"

# The Secret keys the two provisioned values are stored under, and the
# manifests that consume them: k8s/secret.example.yaml's `error-tracking-dsn`
# (read as NYXGPT_ERROR_TRACKING_DSN by the api and web Deployments) and
# k8s/observability/secret.example.yaml's `glitchtip-grafana-token` (mounted
# into Grafana at `K8S_GRAFANA_GLITCHTIP_TOKEN_MOUNT`).
#
# Both are key NAMES, not values -- the values live only in the cluster and in
# the gitignored secret.yaml files (the allowlist pragmas below say so to
# detect-secrets, which reads any `*_TOKEN_* = "..."` assignment as a leak).
K8S_ERROR_TRACKING_DSN_SECRET_KEY = "error-tracking-dsn"  # pragma: allowlist secret
K8S_GRAFANA_GLITCHTIP_TOKEN_SECRET_KEY = "glitchtip-grafana-token"  # pragma: allowlist secret

# What has to be rolled when the DSN changes. An environment is fixed at
# process start, so a Pod that started before provisioning keeps the empty
# DSN it booted with -- which is exactly the "installed, healthy, reporting
# nothing" state this issue is about. The canary tracks are deliberately not
# listed: they rest at zero replicas (#3833), and `nyxgpt canary start` mints
# their Pods later from the Secret as it stands then.
K8S_DSN_CONSUMER_DEPLOYMENTS = ("nyxgpt-api-stable", "nyxgpt-web-stable")

# How long to wait for a `kubectl port-forward` to start carrying traffic.
K8S_PORT_FORWARD_READY_TIMEOUT_S = 30


def _k8s_glitchtip_ensure_superuser(email: str, password: str) -> OpsResult:
    """`createsuperuser --noinput` in the glitchtip Pod -- the Kubernetes twin
    of `_glitchtip_ensure_superuser`.

    Same idempotency contract (rc=1 means "already exists", declared to
    `_run` as expected so a healthy re-run logs at INFO rather than WARNING)
    and the same rule about the password: it NEVER touches argv. `kubectl
    exec` has no `-e VAR` forwarding the way `docker compose exec` does, so
    the password is piped on stdin and read inside the container instead --
    `_run` logs the command it ran, and an argv-borne password would land in
    that log on every idempotent re-run (CodeQL #105/#106). The email is an
    argument because it is not a secret.
    """
    script = (
        'DJANGO_SUPERUSER_EMAIL="$1"; DJANGO_SUPERUSER_USERNAME="$1"; '
        "read -r DJANGO_SUPERUSER_PASSWORD; "
        "export DJANGO_SUPERUSER_EMAIL DJANGO_SUPERUSER_USERNAME DJANGO_SUPERUSER_PASSWORD; "
        "./manage.py createsuperuser --noinput"
    )
    try:
        cp = _run(
            [
                "kubectl",
                "-n",
                K8S_NAMESPACE,
                "exec",
                "-i",
                f"deploy/{K8S_GLITCHTIP_DEPLOYMENT}",
                "--",
                "sh",
                "-c",
                script,
                "sh",
                email,
            ],
            check=False,
            input=f"{password}\n",
            expected_returncodes={1},
            expected_message=(
                f"GlitchTip superuser {email} already exists -- expected rc=1, "
                "treated as success"
            ),
        )
    except Exception as e:
        return OpsResult(
            False,
            "Failed to run GlitchTip createsuperuser in the cluster",
            f"{type(e).__name__}: {e}",
        )

    if cp.returncode == 0:
        return OpsResult(True, f"Created GlitchTip admin user {email} in the cluster")
    combined = ((cp.stdout or "") + (cp.stderr or "")).lower()
    if "already" in combined or "unique" in combined:
        return OpsResult(True, f"GlitchTip admin user {email} already exists in the cluster")
    return OpsResult(
        False, "Failed to ensure the in-cluster GlitchTip admin user", _output_excerpt(cp).strip()
    )


@contextlib.contextmanager
def _k8s_port_forward(service: str, remote_port: int) -> Iterator[str | None]:
    """Forward `service` to an ephemeral local port for the duration of the block.

    Yields the base URL, or None when the tunnel never carried traffic (the
    caller reports that; a context manager cannot).

    GlitchTip's Service is ClusterIP-only, like every Service in `k8s/`, so
    there is no way to speak to its REST API from this process without one --
    and speaking to it from here is what lets the whole provisioning sequence
    be SHARED with the Compose path instead of reimplemented against `kubectl
    exec`. The local port is ephemeral rather than GlitchTip's usual 8080 so
    this never collides with an operator's own `nyxgpt ops port-forward
    --target glitchtip`, or with a native GlitchTip on the same workstation.

    Readiness is decided by a real HTTP request, not by the socket accepting:
    `kubectl port-forward` binds its listener immediately and only then dials
    the Pod, so a bare TCP connect proves nothing about the far end.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        local_port = int(probe.getsockname()[1])

    base_url = f"http://127.0.0.1:{local_port}"
    proc = subprocess.Popen(
        [
            "kubectl",
            "-n",
            K8S_NAMESPACE,
            "port-forward",
            f"svc/{service}",
            f"{local_port}:{remote_port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready = False
        deadline = time.monotonic() + K8S_PORT_FORWARD_READY_TIMEOUT_S
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                httpx.get(f"{base_url}/", timeout=5.0)
            except httpx.HTTPError:
                time.sleep(1.0)
                continue
            ready = True
            break
        yield base_url if ready else None
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


def _write_k8s_secret_value(secret_path: Path, key: str, value: str) -> tuple[bool, OpsResult]:
    """Set one `stringData` key in a bootstrapped Secret manifest.

    Returns `(changed, result)` -- `changed` is what drives the rollout
    restarts, the same way `_write_grafana_glitchtip_token`'s `changed`
    drives the Compose Grafana restart: a re-run that produced the identical
    value must not bounce a Pod.

    The FILE is written, not just the live Secret, because the file is what
    `kubectl apply -k` re-applies: patching only the cluster would leave the
    next install silently reverting the value to the placeholder. The value
    is line-rewritten in place (the treatment `_ensure_k8s_secret` gives the
    API key) so every explanatory comment in the manifest survives.
    """
    if not secret_path.exists():
        return False, OpsResult(
            False,
            f"Missing {secret_path} to write {key} into",
            "Re-run `nyxgpt ops install --kubernetes`, which bootstraps it from "
            "secret.example.yaml.",
        )
    if '"' in value or "\n" in value:
        # Nothing GlitchTip mints looks like this; refuse rather than write a
        # manifest that would parse as something else.
        return False, OpsResult(False, f"Refusing to write an unquotable value into {key}")

    text = secret_path.read_text(encoding="utf-8")
    patched, count = re.subn(
        rf'^(\s*{re.escape(key)}:\s*)".*"$',
        lambda m: f'{m.group(1)}"{value}"',
        text,
        flags=re.MULTILINE,
    )
    if count == 0:
        return False, OpsResult(
            False,
            f"{secret_path} has no {key} entry to write into",
            f"Delete {secret_path} and re-run `nyxgpt ops install --kubernetes` to "
            "re-bootstrap it from the current template.",
        )
    if patched == text:
        return False, OpsResult(True, f"{secret_path} already holds the current {key}")

    secret_path.write_text(patched, encoding="utf-8")
    os.chmod(secret_path, 0o600)
    return True, OpsResult(True, f"Wrote {key} into {secret_path}")


def _apply_k8s_secret_file(secret_path: Path) -> OpsResult:
    """`kubectl apply -f` one Secret manifest (never its value on the command line)."""
    cp = _run(["kubectl", "apply", "-f", str(secret_path)], check=False)
    if cp.returncode != 0:
        return OpsResult(False, f"kubectl apply {secret_path.name} failed", _cp_details(cp))
    return OpsResult(True, f"Applied {secret_path.name} to the cluster", _cp_details(cp))


def _restart_k8s_dsn_consumers() -> list[OpsResult]:
    """Roll the api and web Deployments so they read the newly written DSN.

    Waited on, unlike the Compose path's fire-and-forget restart: this runs
    at the very end of `install --kubernetes`, and everything that reads the
    cluster afterwards -- the install's own health snapshot, `nyxgpt ops
    status`, an operator opening the dashboard -- would otherwise be looking
    at a stack mid-rollout and reporting a half-replaced Pod set as the
    install's verdict (the #3827 lesson, applied to the restart this fix
    introduces rather than to the ones it inherited).
    """
    results: list[OpsResult] = []
    for deployment in K8S_DSN_CONSUMER_DEPLOYMENTS:
        cp = _run(
            ["kubectl", "-n", K8S_NAMESPACE, "rollout", "restart", f"deploy/{deployment}"],
            check=False,
        )
        ok = cp.returncode == 0
        results.append(
            OpsResult(
                ok,
                (
                    f"Restarted {deployment} to pick up the GlitchTip DSN"
                    if ok
                    else f"Could not restart {deployment} for the new GlitchTip DSN"
                ),
                _cp_details(cp),
            )
        )
    if all(r.ok for r in results):
        results += _wait_for_k8s_rollouts(
            [
                (f"deploy/{d}", f"{d} (new GlitchTip DSN)", K8S_APP_TIER_ROLLOUT_TIMEOUT_S)
                for d in K8S_DSN_CONSUMER_DEPLOYMENTS
            ],
            remedy=(
                "The replacement Pods carry the provisioned error-tracking DSN. Check "
                "`nyxgpt ops status`; the previous Pods keep serving until they roll."
            ),
        )
    return results


def _k8s_provision_glitchtip() -> list[OpsResult]:
    """Provision the in-cluster GlitchTip and wire both halves of it up (#3990).

    The Kubernetes equivalent of `_provision_glitchtip`, and idempotent in
    the same way: every step reuses what is already there, so re-running it
    (`nyxgpt ops glitchtip-init --kubernetes`, or any re-install) mints
    nothing twice and restarts nothing that did not change.

    Produces the two values a Kubernetes deployment was missing entirely: the
    api/web DSN (into the `nyxgpt-secrets` Secret, rewritten to the
    in-cluster `glitchtip:8080` because a Pod using GlitchTip's own
    browser-facing localhost DSN drops every event silently, #3565) and
    Grafana's bearer token (into `nyxgpt-observability-secrets`, replacing
    the placeholder that made the SRE Home panels 401).

    Skips -- successfully, with the remedy named -- when there is nothing to
    provision against: no kubectl, a GlitchTip that is not ready yet, or no
    native config.ini to persist the admin credentials in. A skip must not
    fail an install: the app tier works without error tracking, and the
    operator can run the command again once the missing piece is there.
    """
    if _which("kubectl") is None:
        return [OpsResult(True, "Skipped GlitchTip provisioning (kubectl not found)")]

    state = _k8s_observability_workload_state().get(K8S_GLITCHTIP_DEPLOYMENT, "absent")
    if (
        _classify_k8s_observability_workload(K8S_GLITCHTIP_DEPLOYMENT, state).state
        != K8S_STATE_READY
    ):
        return [
            OpsResult(
                True,
                f"Skipped GlitchTip provisioning (in-cluster glitchtip: {state})",
                "Deploy the observability layer with `nyxgpt ops observability --kubernetes`, "
                "then re-run `nyxgpt ops glitchtip-init --kubernetes`.",
            )
        ]

    # The admin credentials have to SURVIVE this run: `createsuperuser` is a
    # no-op the second time, so a freshly generated password would simply fail
    # to log in on the next re-install. The native config.ini is where the
    # Compose path already keeps them, and reusing it means one machine has
    # one GlitchTip admin login however its stack is deployed.
    native_cfg_path = Path.home() / ".nyxGPT" / "config.ini"
    if not native_cfg_path.exists():
        return [
            OpsResult(
                True,
                f"Skipped GlitchTip provisioning (no {native_cfg_path} to store the admin "
                "credentials in)",
                "Run `nyxgpt wizard`, then `nyxgpt ops glitchtip-init --kubernetes`.",
            )
        ]

    results: list[OpsResult] = []
    email, password, generated = _resolve_admin_credentials(native_cfg_path)
    if generated:
        _persist_admin_credentials(native_cfg_path, email, password)
        results.append(
            OpsResult(True, f"Generated and saved a GlitchTip admin password to {native_cfg_path}")
        )

    su_result = _k8s_glitchtip_ensure_superuser(email, password)
    results.append(su_result)
    if not su_result.ok:
        return results

    token: str | None = None
    dsn: str | None = None
    with _k8s_port_forward(K8S_GLITCHTIP_DEPLOYMENT, GLITCHTIP_CONTAINER_PORT) as base_url:
        if base_url is None:
            results.append(
                OpsResult(
                    False,
                    "Could not reach the in-cluster GlitchTip API",
                    "The port-forward to svc/glitchtip never carried traffic. Check "
                    "`nyxgpt ops status` and retry `nyxgpt ops glitchtip-init --kubernetes`.",
                )
            )
            return results

        login_client, login_result = _glitchtip_login(base_url, email, password)
        results.append(login_result)
        if login_client is None:
            return results
        try:
            token, token_result = _glitchtip_ensure_api_token(login_client, base_url)
            results.append(token_result)
        finally:
            login_client.close()
        if token is None:
            return results

        api_client = _glitchtip_http_client(base_url, headers={"Authorization": f"Bearer {token}"})
        try:
            org_slug, org_result = _glitchtip_ensure_organization(api_client)
            results.append(org_result)
            if org_slug is None:
                return results

            team_slug, team_result = _glitchtip_ensure_team(api_client, org_slug)
            results.append(team_result)
            if team_slug is None:
                return results

            results.append(_glitchtip_ensure_team_membership(api_client, org_slug, team_slug))

            project_slug, project_result = _glitchtip_ensure_project(
                api_client, org_slug, team_slug
            )
            results.append(project_result)
            if project_slug is None:
                return results

            dsn, key_result = _glitchtip_ensure_project_key(api_client, org_slug, project_slug)
            results.append(key_result)
        finally:
            api_client.close()

    if dsn is None:
        return results

    # GlitchTip mints the DSN from its own GLITCHTIP_DOMAIN (a browser-facing
    # localhost URL). Inside a Pod that resolves to the Pod itself, so it is
    # rewritten to the in-cluster Service -- the same host and port the
    # Compose path rewrites it to, since the Service and the Compose alias
    # are deliberately both named `glitchtip`.
    app_secret = K8S_DIR / "secret.yaml"
    dsn_changed, dsn_result = _write_k8s_secret_value(
        app_secret, K8S_ERROR_TRACKING_DSN_SECRET_KEY, _containerized_error_tracking_dsn(dsn)
    )
    results.append(dsn_result)
    if dsn_result.ok:
        applied = _apply_k8s_secret_file(app_secret)
        results.append(applied)
        if applied.ok and dsn_changed:
            results += _restart_k8s_dsn_consumers()

    observability_secret = K8S_OBSERVABILITY_DIR / "secret.yaml"
    token_changed, token_write_result = _write_k8s_secret_value(
        observability_secret, K8S_GRAFANA_GLITCHTIP_TOKEN_SECRET_KEY, token
    )
    results.append(token_write_result)
    if token_write_result.ok:
        applied = _apply_k8s_secret_file(observability_secret)
        results.append(applied)
        # Grafana reads `$__file{}` provisioning targets at startup only, so a
        # rewritten token is invisible until the Pod restarts -- the same
        # reason `_provision_glitchtip` restarts the Compose container.
        if applied.ok and token_changed:
            results.append(_restart_k8s_grafana())

    return results


def glitchtip_init(args: Any) -> int:
    """CLI entrypoint for `nyxgpt ops glitchtip-init`.

    Auto-provisions a GlitchTip admin user, organization, project, and DSN
    with no manual sign-in step, writing the DSN into config.ini. Idempotent
    -- safe to re-run any time. No-ops with a clear message if the
    `glitchtip` Compose container isn't up/healthy.

    `--kubernetes` provisions the IN-CLUSTER GlitchTip instead (#3990): same
    sequence, but the DSN and Grafana token land in the deployment's Secrets
    rather than in config.ini and `~/.nyxGPT/secrets`, since a Pod reads
    neither. The install runs it automatically; this is the way to re-run it
    on its own, which is what an operator needs after a GlitchTip whose
    Postgres was reset out from under the recorded DSN.

    Returns 0 on success (including a clean no-op), else 2.
    """
    kubernetes = bool(getattr(args, "kubernetes", False))
    logger.info(
        "ops: glitchtip-init starting",
        extra={
            "component": "ops",
            "action": "glitchtip-init",
            "substrate": SUBSTRATE_KUBERNETES if kubernetes else "compose",
        },
    )
    steps: list[tuple[str, Callable[[], list[OpsResult]]]] = [
        (
            "provision glitchtip",
            _k8s_provision_glitchtip if kubernetes else _provision_glitchtip,
        ),
    ]
    quiet = bool(getattr(args, "quiet", False))
    results, slow_steps = _run_steps("glitchtip-init", steps, quiet=quiet)
    ok = all(r.ok for r in results)
    if not quiet:
        _print_slow_steps_summary(slow_steps)
    logger.info(
        "ops: glitchtip-init %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "glitchtip-init", "ok": ok},
    )
    return 0 if ok else 2


# --- Wrapped credential retrieval (`nyxgpt ops credentials`, #3718) ---
#
# Both observability UIs behind the SRE dashboard have admin credentials
# that are deliberately never exposed over the HTTP API (#3458/#3466), and
# until now had no wrapped way to read them either -- so logging into
# Grafana or GlitchTip meant `ssh` + `cat` on the box, the rawest possible
# violation of the Operational Command Wrapping requirement. These commands
# close that gap CLI-side only: `nyxgpt ops credentials` on the machine
# running the stack, `nyxgpt cloud credentials` (see cloud_deploy.py) for a
# deployment, over the same wrapped SSH access path everything else uses.

GRAFANA_ADMIN_USERNAME = "admin"

CREDENTIAL_SERVICES = ("grafana", "glitchtip")


@dataclass(frozen=True)
class ServiceCredential:
    """One service's admin login, plus where the password actually came from.

    `password` is empty when nothing is resolvable yet; `remediation` then
    says which wrapped command provisions it. `source` exists because the
    same service can be credentialed three different ways (config.ini
    override, ops-managed secret file, `glitchtip-init` provisioning) and an
    operator debugging a failed login needs to know which one answered.
    """

    service: str
    url: str
    username: str
    password: str
    source: str
    remediation: str = ""

    @property
    def available(self) -> bool:
        """Whether a password could be resolved at all."""
        return bool(self.password)

    def as_dict(self) -> dict[str, Any]:
        """JSON-serializable form, for `--json` and the cloud path's transport."""
        return {
            "service": self.service,
            "url": self.url,
            "username": self.username,
            "password": self.password,
            "source": self.source,
            "remediation": self.remediation,
            "available": self.available,
        }


def _grafana_credential(cfg: ConfigParser) -> ServiceCredential:
    """Resolve Grafana's admin login the way `nyxgpt ops install` reconciles it.

    Read-only: uses `read_grafana_admin_password` rather than
    `resolve_grafana_admin_password`, so merely *asking* for the password on
    a host that was never installed doesn't mint one -- see that function's
    docstring.
    """
    password, source = read_grafana_admin_password(cfg)
    return ServiceCredential(
        service="grafana",
        url=str(get_monitoring_config(cfg).get("grafana_ui_url", "")),
        username=GRAFANA_ADMIN_USERNAME,
        password=password,
        source=source,
        remediation=(
            ""
            if password
            else (
                "No Grafana admin password has been provisioned yet -- run "
                "`nyxgpt ops install` (it generates and reconciles one), or set "
                "[monitoring] grafana_admin_password in config.ini."
            )
        ),
    )


def _glitchtip_credential(cfg: ConfigParser, cfg_path: Path) -> ServiceCredential:
    """Resolve GlitchTip's admin login from the config.ini `glitchtip-init` wrote.

    Reads `cfg_path` directly rather than the cached `cfg`: `nyxgpt ops
    glitchtip-init` persists the generated credentials by rewriting
    config.ini (`_persist_admin_credentials`), and a long-lived process's
    cached parser can be a provisioning run behind what is on disk.
    """
    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(cfg_path)

    password = parser.get("error_tracking", "admin_password", fallback="").strip()
    email = parser.get("error_tracking", "admin_email", fallback="").strip()
    return ServiceCredential(
        service="glitchtip",
        url=str(get_error_tracking_config(cfg).get("glitchtip_ui_url", "")),
        username=email or GLITCHTIP_DEFAULT_ADMIN_EMAIL,
        password=password,
        source=f"{cfg_path} [error_tracking] admin_password" if password else "",
        remediation=(
            ""
            if password
            else (
                "No GlitchTip admin password has been provisioned yet -- run "
                "`nyxgpt ops glitchtip-init` (it provisions the admin user and "
                "saves the credentials)."
            )
        ),
    )


def resolve_service_credentials(
    cfg_path: Path | None = None, services: Container[str] | None = None
) -> list[ServiceCredential]:
    """Resolve the admin logins for the observability UIs on this machine.

    Each credential comes from its real source -- a config.ini override, the
    ops-managed secret file, or the values `glitchtip-init` provisioned --
    so what this prints is what the running service actually accepts.
    `services` filters to a subset (default: all of `CREDENTIAL_SERVICES`).
    """
    from nyxgpt.config import load_config

    path = cfg_path or (NYXGPT_HOME / "config.ini")
    cfg = load_config(path)
    wanted = CREDENTIAL_SERVICES if services is None else services

    resolvers: dict[str, Callable[[], ServiceCredential]] = {
        "grafana": lambda: _grafana_credential(cfg),
        "glitchtip": lambda: _glitchtip_credential(cfg, path),
    }
    return [resolvers[name]() for name in CREDENTIAL_SERVICES if name in wanted]


def format_credentials(creds: list[ServiceCredential]) -> str:
    """Render `creds` for a terminal, one labeled block per service."""
    blocks: list[str] = []
    for cred in creds:
        lines = [cred.service]
        if cred.url:
            lines.append(f"  URL:      {cred.url}")
        if cred.available:
            lines.append(f"  Username: {cred.username}")
            lines.append(f"  Password: {cred.password}")
            lines.append(f"  Source:   {cred.source}")
        else:
            lines.append(f"  Username: {cred.username}")
            lines.append("  Password: (not provisioned)")
            lines.append(f"  Fix:      {cred.remediation}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def credentials(args: Any) -> int:
    """CLI entrypoint for `nyxgpt ops credentials`.

    Prints the Grafana and GlitchTip admin logins for the stack on this
    machine, replacing the `ssh` + `cat ~/.nyxGPT/secrets/...` an operator
    otherwise needs to sign in (#3718). Output goes to stdout only: the
    passwords are never logged (the log line below records service names and
    whether each resolved, never a value) and remain absent from every HTTP
    API response, preserving #3458/#3466.

    Returns 0 when every requested service resolved, else 2 -- so a missing
    credential is a scriptable failure rather than a silently empty field.
    """
    cfg_path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    service = getattr(args, "service", "all") or "all"
    wanted = CREDENTIAL_SERVICES if service == "all" else (service,)

    creds = resolve_service_credentials(cfg_path=cfg_path, services=wanted)

    if getattr(args, "json", False):
        print(json.dumps([c.as_dict() for c in creds], indent=2))
    else:
        print(format_credentials(creds))

    ok = all(c.available for c in creds)
    logger.info(
        "ops: credentials %s",
        "resolved" if ok else "incomplete",
        extra={
            "component": "ops",
            "action": "credentials",
            "services": [c.service for c in creds],
            "resolved": [c.service for c in creds if c.available],
            "ok": ok,
        },
    )
    return 0 if ok else 2


# --- Live verification harness (`nyxgpt ops verify`, #3555 / P6-18) ---


def _verify_repo_index_path(mode: DeploymentMode) -> str:
    """Resolve the repo-ingest fixture's path as the running `api` process will see it.

    Writes a tiny fixture repo under the shared `nyxgpt-data` volume
    (`volume_dir("nyxgpt-data")`) and translates it to the API process's own
    view of that path: the containerized `api` service bind-mounts that host
    directory at `/root/.nyxGPT` (see docker-compose.yml), while a native
    `api` process sees the same host path directly (both pass
    `ingest_repository`'s "must be under Path.home()" check either way).
    """
    host_dir = volume_dir("nyxgpt-data") / "rag-verify-repo"
    verify_mod.write_verify_repo_fixture(host_dir)
    if mode.compose.get("api", "absent") not in ("absent", "none", ""):
        rel = host_dir.relative_to(Path.home())
        return f"/root/{rel.as_posix()}"
    return str(host_dir)


def _boot_verify_stack() -> list[OpsResult]:
    """Bring up the full Compose stack (core app + observability profiles) for `nyxgpt ops verify`.

    Unlike `_start_observability_stack` (which deliberately excludes
    `CORE_APP_SERVICES` so a native-first install's own processes aren't
    duplicated in Compose), `verify`'s target environment -- CI, or an
    ephemeral local run -- has no native brew/launchd path at all, so this
    issues a plain `docker compose --profile X... up -d` with no
    service-list filtering: Compose brings up every default (core app)
    service plus the requested observability profiles together, the same
    shape `scripts/smoke-test.sh` deploys for its own live smoke test.
    """
    if not _compose_available():
        return [
            OpsResult(
                False,
                "docker not found -- `nyxgpt ops verify` needs the Compose stack",
                "Install Docker, or pass --skip-boot if a stack (native or Compose) is "
                "already up.",
            )
        ]
    cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE)]
    for profile in OBSERVABILITY_PROFILES:
        cmd += ["--profile", profile]
    cmd += ["up", "-d"]
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return [OpsResult(False, "Failed to boot the Compose stack for verify", _cp_details(cp))]
    return [OpsResult(True, "Compose stack up (core app + observability profiles)")]


def _wait_for_verify_stack_healthy(
    services: tuple[str, ...] = ("api", "web", "ollama", "cassandra"),
    timeout: float = 300.0,
    poll_interval: float = 5.0,
) -> list[OpsResult]:
    """Poll `self_heal.list_component_status()` until every service in `services` is healthy."""
    deadline = time.monotonic() + timeout
    pending = set(services)
    while pending and time.monotonic() < deadline:
        statuses = {s.service: s for s in self_heal.list_component_status()}
        for service in list(pending):
            status = statuses.get(service)
            if status is not None and status.healthy:
                pending.discard(service)
        if pending:
            time.sleep(poll_interval)
    if pending:
        return [
            OpsResult(
                False,
                f"Timed out waiting for {', '.join(sorted(pending))} to become healthy",
                f"Waited {timeout:g}s -- check `nyxgpt ops status` / `docker compose logs`.",
            )
        ]
    return [OpsResult(True, f"{', '.join(services)} all healthy")]


def _teardown_verify_stack() -> list[OpsResult]:
    """Tear down the Compose stack `_boot_verify_stack` brought up (app + observability)."""
    if not _compose_available():
        return [OpsResult(True, "Skipped verify stack teardown (Docker not found)")]
    cmd = ["docker", "compose", "-f", str(self_heal.COMPOSE_FILE)]
    for profile in OBSERVABILITY_PROFILES:
        cmd += ["--profile", profile]
    cmd += ["down"]
    cp = _run(cmd, check=False)
    if cp.returncode != 0:
        return [OpsResult(False, "Failed to tear down verify stack", _cp_details(cp))]
    return [OpsResult(True, "Verify stack torn down")]


def verify(args: Any) -> int:
    """CLI entrypoint for `nyxgpt ops verify`.

    The live smoke harness behind #3555/P6-18: boots the Compose stack
    (core app + observability profiles) in an ephemeral environment unless
    `--skip-boot` is passed, generates one known unit of traffic per
    acceptance-criteria path (a chat round-trip, one RAG ingest per source
    -- document/upload/repo -- and a RAG query -- see `nyxgpt.verify`),
    then asserts it landed via two independent live checks: Prometheus
    instant-query counter deltas, and Grafana's HTTP API re-executing each
    touched dashboard panel's own query. Captures Playwright screenshots of
    the touched dashboards as visual evidence for the review agent
    (multimodal) to inspect. Tears the stack back down afterward unless
    `--keep-up` is passed (or `--skip-boot` was used, in which case nothing
    was booted to tear down).

    This is what the review agent runs in CI on every PR touching
    observability, metrics, or UI surfaces (see
    agents/runbooks/review-runbook.md), and what the owner can run locally
    as a one-command pre-check before acceptance testing.

    Returns 0 if every check passed, else 2.
    """
    from nyxgpt.config import get_api_port, get_auth_api_key, load_config

    logger.info("ops: verify starting", extra={"component": "ops", "action": "verify"})

    cfg_path = Path.home() / ".nyxGPT" / "config.ini"
    if not cfg_path.exists():
        precondition_results = [
            OpsResult(
                False, f"Missing config {cfg_path}", "Run `nyxgpt wizard` first to generate it."
            )
        ]
        ok = _emit_results("verify", precondition_results)
        return 0 if ok else 2

    cfg = load_config(cfg_path)
    monitoring = get_monitoring_config(cfg)
    if not monitoring["enabled"]:
        precondition_results = [
            OpsResult(
                False,
                "Monitoring is disabled",
                "Set [monitoring] enabled = true in config.ini, then run `nyxgpt ops install` "
                "(or start the `monitoring` Compose profile) before running `nyxgpt ops "
                "verify` -- it asserts against Prometheus/Grafana, which need to be up.",
            )
        ]
        ok = _emit_results("verify", precondition_results)
        return 0 if ok else 2

    results: list[OpsResult] = []
    booted = False
    try:
        if not getattr(args, "skip_boot", False):
            boot_results = _boot_verify_stack()
            results.extend(boot_results)
            if not all(r.ok for r in boot_results):
                ok = _emit_results("verify", results)
                return 0 if ok else 2
            booted = True

            health_results = _wait_for_verify_stack_healthy(timeout=getattr(args, "timeout", 300.0))
            results.extend(health_results)
            results.extend(_reconcile_grafana_provisioning())
            if not all(r.ok for r in health_results):
                ok = _emit_results("verify", results)
                return 0 if ok else 2

        api_url = getattr(args, "api_url", None) or f"http://127.0.0.1:{get_api_port(cfg)}"
        api_key = get_auth_api_key(cfg) or None
        grafana_admin_password = _grafana_admin_password(cfg)
        prometheus_url = monitoring["prometheus_ui_url"]
        grafana_url = monitoring["grafana_ui_url"]

        repo_index_path = _verify_repo_index_path(detect_deployment_mode())

        before = verify_mod.snapshot_counters(prometheus_url, verify_mod.EXPECTED_COUNTER_QUERIES)

        _markers, traffic_checks = verify_mod.generate_traffic(
            api_url, api_key, repo_index_path=repo_index_path
        )
        results.extend(OpsResult(c.ok, c.message, c.details) for c in traffic_checks)

        prometheus_checks = verify_mod.assert_counter_deltas(
            prometheus_url, before, verify_mod.EXPECTED_COUNTER_QUERIES
        )
        results.extend(OpsResult(c.ok, c.message, c.details) for c in prometheus_checks)

        try:
            dashboard_paths = verify_mod.resolve_touched_dashboards(
                getattr(args, "dashboards", None)
            )
        except ValueError as e:
            results.append(OpsResult(False, "Could not resolve touched dashboards", str(e)))
            dashboard_paths = []

        if dashboard_paths:
            with _grafana_admin_client(grafana_url, grafana_admin_password) as grafana_client:
                panel_checks = verify_mod.assert_panel_queries(grafana_client, dashboard_paths)
            results.extend(OpsResult(c.ok, c.message, c.details) for c in panel_checks)

            if not getattr(args, "skip_screenshots", False):
                out_dir = Path(
                    getattr(args, "screenshot_dir", None)
                    or (Path.home() / ".nyxGPT" / "verify-artifacts")
                )
                screenshot_checks = verify_mod.capture_dashboard_screenshots(
                    grafana_url,
                    grafana_admin_password,
                    [verify_mod.dashboard_uid(p) for p in dashboard_paths],
                    out_dir,
                )
                results.extend(OpsResult(c.ok, c.message, c.details) for c in screenshot_checks)
    finally:
        if booted and not getattr(args, "keep_up", False):
            results.extend(_teardown_verify_stack())

    ok = _emit_results("verify", results)
    logger.info(
        "ops: verify %s",
        "succeeded" if ok else "failed",
        extra={"component": "ops", "action": "verify", "ok": ok},
    )
    result, message = _ops_action_outcome(results)
    _record_ops_action("verify", "verify", result, message)
    return 0 if ok else 2
