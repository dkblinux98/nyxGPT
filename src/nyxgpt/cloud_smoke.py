"""End-to-end cloud smoke test for `nyxgpt cloud smoke` (P6-17, #3515).

`scripts/smoke-test.sh` proves the *local* Compose deployment works: bring it
up, chat, ingest and query RAG, kill components, watch self-heal, tear down.
This module is that test's cloud counterpart, and it mirrors the same shape
against a live AWS deployment reached over the private access path:

1. **Deploy** -- `cloud_deploy.deploy`, which applies the substrate, installs
   a published nyxGPT release on the instance and opens the SSH tunnel.
2. **Access** -- confirm the tunnel is up (opening one if the run was pointed
   at an existing deployment with `--skip-deploy`). Every later step goes
   through `http://localhost:...`, because nothing on the instance listens on
   a non-loopback address
   (`product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md`) -- so verifying
   the app *is* verifying the access path.
3. **Model** -- make sure the instance's configured default model is pulled,
   the same check the local script does before chatting.
4. **Chat** -- a real round-trip through the API, asserting a non-empty reply.
5. **RAG** -- ingest a document containing a unique marker, then query for it
   and assert the marker comes back.
6. **Observability** -- every UI the deploy's enabled profiles forward
   (Grafana, Prometheus, Jaeger, GlitchTip) answers through the tunnel.
7. **Teardown, always** -- the destroy runs in the failure path as well as the
   success path, because the acceptance criterion is "leaves no billed
   resources behind on success *or* failure". A failing verification never
   skips it, an unexpected exception never skips it, and Ctrl-C never skips
   it; the only thing that does is an explicit `--keep`, which says so loudly.

Deliberately a wrapped CLI command rather than a script in this repository:
P6-16 accepts the cloud path from a clean machine with no checkout (CLAUDE.md,
repo-less portability), so the smoke test has to ship inside the installed
package like every other `nyxgpt` command. It is also the reason nothing here
shells out to `terraform`, `ssh` or `docker` on its own -- it drives
`nyxgpt.cloud_deploy`, exactly as an operator's terminal would.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from nyxgpt import cloud_deploy, cloud_infra
from nyxgpt.cloud import CloudCommandError

# Everything is reached through the tunnel `cloud_deploy` opens, on the same
# local ports it forwards (see `cloud_deploy.CORE_TUNNEL_PORTS`).
API_PORT = 8000
API_BASE = f"http://localhost:{API_PORT}/api/v1"

# The session and document this test creates on the instance. Fixed names, so
# a re-run reuses them instead of accumulating debris on a deployment kept
# with `--keep`.
SMOKE_SESSION = "cloud-smoke-test"
SMOKE_DOC_ID = "cloud-smoke-test-doc"

# The chat prompt is the local script's, verbatim: short, deterministic enough
# to be cheap, and only ever asserted to be non-empty.
CHAT_PROMPT = "Reply with exactly one word: OK"
RAG_QUESTION = "What is the secret smoke-test phrase?"

# Where each observability UI answers when it is up. Grafana and Prometheus
# have real readiness endpoints; Jaeger and GlitchTip are checked at their
# root, which is enough to prove the container is serving through the tunnel.
OBSERVABILITY_HEALTH_PATHS: dict[str, str] = {
    "grafana": "/api/health",
    "prometheus": "/-/ready",
    "jaeger": "/",
    "glitchtip": "/",
}

# Default per-phase budgets, in seconds. The model pull is the long pole on a
# first deploy (a multi-gigabyte download onto a fresh instance); GlitchTip is
# the slow starter among the observability containers.
DEFAULT_MODEL_TIMEOUT = 1800.0
DEFAULT_CHAT_TIMEOUT = 300.0
DEFAULT_RAG_TIMEOUT = 120.0
DEFAULT_OBSERVABILITY_TIMEOUT = 300.0

# A UI that answers with a redirect to a login page, or with 401/403, is
# *reachable* -- which is what this phase claims. Only a server error or no
# answer at all counts as a failure.
_REACHABLE_MAX_STATUS = 500

# Read from the instance rather than from this workstation's config: the API
# key and default model that matter are the ones the deployed stack is running
# with, and on a fresh deploy they were generated on the box. Executed by the
# instance's own venv interpreter, so it goes through the same config loader
# the API server uses (including cloud-sourced secrets).
_REMOTE_SETTINGS_PY = (
    "import json;from nyxgpt import config;cfg=config.load_config();"
    "print(json.dumps({'api_key':config.get_auth_api_key(cfg),"
    "'default_model':config.get_default_model(cfg)}))"
)


class SmokeFailure(CloudCommandError):
    """A verification phase failed against the live deployment."""


# --- HTTP through the access tunnel ------------------------------------


def _http(
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    api_key: str = "",
    timeout: float = 30.0,
    base: str = API_BASE,
) -> tuple[int, str]:
    """Call `base + path` through the tunnel, returning `(status, body)`.

    Never raises for a transport or HTTP error -- an unreachable service is a
    `(0, reason)` answer a phase can retry on, which is what polling a stack
    that is still starting needs.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, f"{exc.__class__.__name__}: {exc}"


def _json_body(body: str) -> dict[str, Any]:
    """Parse `body` as a JSON object, or return `{}` if it is not one."""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- Phases -------------------------------------------------------------


def remote_settings(target: cloud_deploy.DeployTarget) -> dict[str, str]:
    """Return the deployed stack's API key and default model, read over SSH.

    Best-effort: a box whose config cannot be read yields `{}` and the run
    continues without an auth header -- if auth is in fact enabled, the chat
    phase fails with the API's own 401, which is a clearer diagnostic than
    anything this function could invent.
    """
    command = f'"$HOME/.nyxGPT/venv/bin/python" -c {shlex.quote(_REMOTE_SETTINGS_PY)}'
    completed = cloud_deploy.run_remote(target, command, timeout=120)
    if completed.returncode != 0:
        return {}
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if not lines:
        return {}
    parsed = _json_body(lines[-1])
    return {
        "api_key": str(parsed.get("api_key") or ""),
        "default_model": str(parsed.get("default_model") or ""),
    }


def ensure_access(
    target: cloud_deploy.DeployTarget, profiles: list[str], health_timeout: float
) -> dict[str, Any]:
    """Make sure the access tunnel is open and the API answers through it.

    A deploy in this same run has already done both, so this is a no-op on the
    happy path; it earns its place on the `--skip-deploy` path, where the
    deployment exists but the tunnel from an earlier session may not.
    """
    status = cloud_deploy.tunnel_status()
    opened = False
    if not status["running"]:
        cloud_deploy.start_tunnel(target, profiles)
        opened = True
    health = cloud_deploy.wait_for_health(health_timeout, port=API_PORT)
    if not health["healthy"]:
        raise SmokeFailure(
            f"{health['url']} did not answer 200 within {health_timeout:.0f}s "
            f"(last status: {health['status'] or 'unreachable'}). The deployment is not "
            "reachable over the access tunnel, so nothing else can be verified."
        )
    return {
        "tunnel_opened": opened,
        "url": health["url"],
        "waited_seconds": round(health["waited"], 1),
    }


def ensure_model(api_key: str, model: str, timeout: float) -> dict[str, Any]:
    """Make sure `model` is pulled on the instance, pulling it if it is not.

    Mirrors the local script's pre-chat step: on a fresh instance the model is
    a multi-gigabyte download, and a chat that times out waiting for it looks
    like a broken deployment rather than a slow one.
    """
    if not model:
        return {"model": "", "skipped": True, "reason": "the instance reported no default model"}
    status, body = _http("/models", api_key=api_key, timeout=60.0)
    if status == 200 and model in (_json_body(body).get("models") or []):
        return {"model": model, "pulled": False, "already_present": True}
    status, body = _http("/models/pull", payload={"model": model}, api_key=api_key, timeout=timeout)
    if status != 200:
        raise SmokeFailure(
            f"could not pull the default model {model!r} on the instance "
            f"(HTTP {status or 'unreachable'}): {body[:400]}"
        )
    return {"model": model, "pulled": True, "already_present": False}


def verify_chat(api_key: str, timeout: float) -> dict[str, Any]:
    """Assert a chat round-trip returns a non-empty reply."""
    started = time.monotonic()
    status, body = _http(
        "/chat",
        payload={"prompt": CHAT_PROMPT, "session": SMOKE_SESSION, "new": True},
        api_key=api_key,
        timeout=timeout,
    )
    if status != 200:
        raise SmokeFailure(f"chat request failed (HTTP {status or 'unreachable'}): {body[:400]}")
    reply = str(_json_body(body).get("reply") or "").strip()
    if not reply:
        raise SmokeFailure(f"chat returned no reply: {body[:400]}")
    return {"reply": reply[:80], "waited_seconds": round(time.monotonic() - started, 1)}


def verify_rag(api_key: str, timeout: float) -> dict[str, Any]:
    """Ingest a document with a unique marker, then assert a query returns it."""
    marker = f"XYZZY-NYXGPT-CLOUD-{os.getpid()}"
    status, body = _http(
        "/rag/ingest",
        payload={
            "doc_id": SMOKE_DOC_ID,
            "text": f"The secret smoke-test phrase is {marker}.",
            "ensure_schema": True,
        },
        api_key=api_key,
        timeout=timeout,
    )
    if status != 200:
        raise SmokeFailure(f"RAG ingest failed (HTTP {status or 'unreachable'}): {body[:400]}")
    # The write is committed by the time ingest returns, but the vector store
    # is a separate service; a short settle keeps a fast query from racing it.
    time.sleep(2.0)
    status, body = _http(
        "/rag/query", payload={"query": RAG_QUESTION}, api_key=api_key, timeout=timeout
    )
    if status != 200:
        raise SmokeFailure(f"RAG query failed (HTTP {status or 'unreachable'}): {body[:400]}")
    if marker not in body:
        raise SmokeFailure(
            "RAG query did not surface the phrase that was just ingested "
            f"({marker}) -- retrieval is not working on this deployment: {body[:400]}"
        )
    return {"doc_id": SMOKE_DOC_ID, "marker": marker}


def verify_observability(profiles: list[str], timeout: float) -> dict[str, Any]:
    """Assert every observability UI the deploy forwards answers through the tunnel."""
    services = [
        (name, port)
        for profile in profiles
        for name, port in cloud_deploy.OBSERVABILITY_TUNNEL_PORTS.get(profile, ())
    ]
    if not services:
        return {
            "skipped": True,
            "reason": "the deployment has no observability profiles enabled",
            "services": {},
        }

    deadline = time.monotonic() + timeout
    reached: dict[str, int] = {}
    pending = list(services)
    while pending:
        still_pending: list[tuple[str, int]] = []
        for name, port in pending:
            path = OBSERVABILITY_HEALTH_PATHS.get(name, "/")
            status, _ = _http(path, timeout=15.0, base=f"http://localhost:{port}")
            if status and status < _REACHABLE_MAX_STATUS:
                reached[name] = status
            else:
                still_pending.append((name, port))
        pending = still_pending
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(5.0)

    if pending:
        unreachable = ", ".join(f"{name} (localhost:{port})" for name, port in pending)
        raise SmokeFailure(
            f"observability UIs not reachable through the tunnel within {timeout:.0f}s: "
            f"{unreachable}. `nyxgpt ops observability` on the instance starts them."
        )
    return {"skipped": False, "services": reached}


def teardown(args: argparse.Namespace, *, keep: bool) -> dict[str, Any]:
    """Destroy the deployment, whatever happened before it.

    Never raises: the caller has to be able to report a verification failure
    *and* whether the billed resources actually went away, and an exception
    here would hide one behind the other. A teardown error is returned as
    data and turns the whole run red -- leaked infrastructure is a failure of
    this test even if every verification passed.
    """
    if keep:
        return {
            "destroyed": False,
            "kept": True,
            "error": "",
            "reason": "--keep was passed, so the deployment (and its AWS charges) is still up",
        }
    if not cloud_infra.TFSTATE_FILE.exists():
        # Nothing was ever created (or it is already gone) -- a deploy that
        # failed before Terraform wrote state leaves nothing to bill for, so
        # this is a clean teardown rather than an error.
        return {"destroyed": False, "kept": False, "error": "", "reason": "nothing was provisioned"}
    try:
        cloud_deploy.destroy(args)
    # Ctrl-C during the teardown is caught alongside ordinary failures: an
    # interrupted destroy is the single most expensive thing that can happen
    # here, and the operator needs to be told about it rather than seeing a
    # traceback.
    except (Exception, KeyboardInterrupt) as exc:
        return {
            "destroyed": False,
            "kept": False,
            "error": f"{exc.__class__.__name__}: {exc}",
            "reason": (
                "the teardown failed -- AWS resources may still be running and billing. "
                "Run `nyxgpt cloud destroy --yes`, and check the console if it keeps failing."
            ),
        }
    return {"destroyed": True, "kept": False, "error": "", "reason": ""}


# --- The run ------------------------------------------------------------


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    """Deploy, verify chat/RAG/observability, and tear the deployment down.

    Returns the full record of the run (`passed`, the per-phase steps, and the
    teardown outcome) rather than raising, so the CLI and the deploy history
    both report the same thing -- including the case where verification failed
    but the teardown succeeded, which is a normal, clean outcome for a test
    whose job is to leave nothing behind.
    """
    keep = bool(getattr(args, "keep", False))
    skip_deploy = bool(getattr(args, "skip_deploy", False))
    model_timeout = float(getattr(args, "model_timeout", None) or DEFAULT_MODEL_TIMEOUT)
    chat_timeout = float(getattr(args, "chat_timeout", None) or DEFAULT_CHAT_TIMEOUT)
    observability_timeout = float(
        getattr(args, "observability_timeout", None) or DEFAULT_OBSERVABILITY_TIMEOUT
    )
    health_timeout = float(getattr(args, "health_timeout", None) or 900.0)

    started = time.monotonic()
    steps: list[dict[str, Any]] = []
    failure = ""
    host = ""
    version = ""
    instance_id = ""
    region = ""

    try:
        if skip_deploy:
            target = cloud_deploy.resolve_target(args)
            profiles = [str(p) for p in (cloud_deploy.load_deploy_state().get("profiles") or [])]
            version = str(cloud_deploy.load_deploy_state().get("version") or "")
            steps.append({"step": "deploy", "skipped": True, "host": target.host})
        else:
            result = cloud_deploy.deploy(args)
            target = cloud_deploy.DeployTarget(**result["target"])
            profiles = [str(p) for p in result["plan"]["profiles"]]
            version = str(result["plan"]["version"])
            steps.append(
                {
                    "step": "deploy",
                    "skipped": False,
                    "host": target.host,
                    "version": version,
                    "profiles": profiles,
                }
            )
        host, instance_id, region = target.host, target.instance_id, target.region

        steps.append({"step": "access", **ensure_access(target, profiles, health_timeout)})

        settings = remote_settings(target)
        api_key = str(
            getattr(args, "api_key", None)
            or os.environ.get("NYXGPT_AUTH_API_KEY", "")
            or settings.get("api_key", "")
        )
        steps.append(
            {
                "step": "model",
                **ensure_model(api_key, settings.get("default_model", ""), model_timeout),
            }
        )
        steps.append({"step": "chat", **verify_chat(api_key, chat_timeout)})
        steps.append(
            {
                "step": "rag",
                **verify_rag(
                    api_key, float(getattr(args, "rag_timeout", None) or DEFAULT_RAG_TIMEOUT)
                ),
            }
        )
        steps.append(
            {"step": "observability", **verify_observability(profiles, observability_timeout)}
        )
    except CloudCommandError as exc:
        failure = str(exc)
    except KeyboardInterrupt:
        failure = "interrupted (Ctrl-C) -- tearing the deployment down before exiting"
    # Broad on purpose: any escaping exception must still reach the teardown
    # below, or a bug in a verification phase becomes a bill.
    except Exception as exc:
        failure = f"unexpected error: {exc.__class__.__name__}: {exc}"

    teardown_result = teardown(args, keep=keep)
    steps.append({"step": "teardown", **teardown_result})

    passed = not failure and not teardown_result["error"]
    detail = failure or teardown_result["error"] or "chat, RAG and observability verified"
    if passed and teardown_result["kept"]:
        detail += " (deployment kept up by --keep)"
    cloud_deploy.record_history(
        "smoke",
        "succeeded" if passed else "failed",
        version=version,
        host=host,
        instance_id=instance_id,
        region=region,
        detail=detail,
    )
    return {
        "action": "smoke",
        "passed": passed,
        "failure": failure,
        "steps": steps,
        "teardown": teardown_result,
        "duration_seconds": round(time.monotonic() - started, 1),
    }


# --- CLI entry point ----------------------------------------------------


def _print_summary(result: dict[str, Any]) -> None:
    """Print the operator-facing result of a smoke run."""
    for step in result["steps"]:
        name = step["step"]
        if step.get("skipped"):
            print(f"[cloud-smoke] {name}: skipped ({step.get('reason', 'not applicable')})")
        elif name == "teardown":
            if step["error"]:
                print(f"[cloud-smoke] teardown: FAILED -- {step['error']}")
            elif step["kept"]:
                print(f"[cloud-smoke] teardown: skipped -- {step['reason']}")
            else:
                print("[cloud-smoke] teardown: deployment destroyed")
        else:
            print(f"[cloud-smoke] {name}: ok")

    if result["passed"]:
        print(f"\nCloud smoke test PASSED in {result['duration_seconds']:.0f}s.")
    else:
        print(
            f"\nCloud smoke test FAILED after {result['duration_seconds']:.0f}s.", file=sys.stderr
        )
        if result["failure"]:
            print(f"  {result['failure']}", file=sys.stderr)

    teardown_result = result["teardown"]
    if teardown_result["error"]:
        print(f"\nWARNING: {teardown_result['reason']}", file=sys.stderr)
    elif teardown_result["kept"]:
        print(
            "\nWARNING: the deployment is still running and still billing. "
            "Tear it down with `nyxgpt cloud destroy --yes`.",
            file=sys.stderr,
        )


def smoke_command(args: argparse.Namespace) -> int:
    """`nyxgpt cloud smoke` entry point."""
    # A deployment this run did not create is not this run's to delete without
    # being told so explicitly.
    if (
        getattr(args, "skip_deploy", False)
        and not getattr(args, "keep", False)
        and not getattr(args, "yes", False)
    ):
        print(
            "--skip-deploy targets the deployment that already exists, and this test "
            "destroys what it verifies -- that would delete a deployment this run did "
            "not create. Re-run with --yes to confirm, or with --keep to leave it up.",
            file=sys.stderr,
        )
        return 2
    result = run_smoke(args)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        _print_summary(result)
    return 0 if result["passed"] else 1
