#!/usr/bin/env python3
"""Executed evidence for #3806: rotate the auth key on a running stack (D-006).

Unit tests can assert that `[auth] api_key` is *classified* restart-required
for `web`. They structurally cannot see the thing the owner actually hit: a
real Next.js process, started by the real service wrapper, holding a real
stale key in its environment and turning every proxied call into a blank 401.
That is a property of what the wrapper does at process start on a real
machine, so it has to be run.

This script runs it. Two real processes -- the FastAPI backend (uvicorn) and
the web tier launched through the wrapper `ops.py` actually generates -- with
a real `config.ini` between them, and drives the whole acceptance scenario:

  1. Baseline          proxied call through web -> 200 (auth on, keys agree).
  2. Rotate            through the real POST /api/v1/config/sections.
  3. THE DEFECT        the api tier accepts the new key immediately, the web
                       tier keeps sending the old one -> proxied call 401s.
                       Reproduced, not asserted.
  4. The notice        GET /infra/restart-status names `web`, the pending key,
                       the wrapped command, and the session-drop warning.
  5. Defer             restart the API process; the notice is still there.
                       (The #3407 in-memory version lost it here.)
  6. CLI parity        a separate process writing the key via
                       `secrets_setup.write_secret` shows up in the running
                       API's notice. One behavior, two surfaces.
  7. Dashboard parity  the Admin Dashboard's Access panel
                       (`POST /admin/access`) is the third writer of the key
                       and raises the same notice -- on the very page that
                       hosts it.
  8. Failed restart    `nyxgpt ops restart web` against a stack it cannot
                       manage must NOT clear the flag -- a restart that did
                       not happen must never read as "all good".
  9. Revert            putting the value back to what web is still running
                       retires the notice AND removes the 401 wall, with no
                       restart at all.
 10. Restart           rotate again, then re-exec the wrapper (what a web
                       restart does) -> proxied calls are 200 on the new key.
                       Healthy stack, not a blank 401.
 11. Fault injection   redo step 3 with the classification stripped, and
                       assert this script would have caught the pre-fix
                       product: the 401 wall still happens and NO notice
                       appears. Without this, steps 3-4 would pass on any
                       build, including the broken one (#3753's rule).
 12. Self-restart      the api's OWN restart clears the api's own notice, and
                       leaves `web`'s standing. This is #3806 round two: the
                       flag was cleared at the end of a callback running
                       *inside* the api process, so restarting `api` killed
                       the thread before it ran, and a restart that worked
                       reported "Saved -- but not yet in effect" forever.
 13. Fault injection   redo step 12 against an api started with the startup
                       reconciliation neutered -- the pre-fix product -- and
                       assert the flag survives the restart. Without this,
                       step 12 would pass on the broken build too.

Exit code 0 on success; non-zero with a printed reason otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

API_PORT = 8123
WEB_PORT = 3123
API_URL = f"http://127.0.0.1:{API_PORT}"
WEB_URL = f"http://127.0.0.1:{WEB_PORT}"

# The three distinct values this scenario moves `[auth] api_key` between:
# what the tiers start on, what the dashboard rotation writes, and what the
# CLI writer writes. Nothing about the scenario depends on their content --
# only on their being different from each other -- so they are generated per
# run into a throwaway config under a temp directory that `cleanup()` removes,
# rather than committed as literals.
ORIGINAL_VALUE = f"smoke-original-{uuid.uuid4().hex}"
ROTATED_VALUE = f"smoke-rotated-{uuid.uuid4().hex}"
CLI_WRITTEN_VALUE = f"smoke-cli-written-{uuid.uuid4().hex}"
FINAL_VALUE = f"smoke-final-{uuid.uuid4().hex}"

failures: list[str] = []
_procs: list[subprocess.Popen] = []


def log(message: str) -> None:
    print(f"[restart-activation-smoke] {message}", flush=True)


def check(ok: bool, description: str) -> bool:
    if ok:
        log(f"  PASS  {description}")
    else:
        log(f"  FAIL  {description}")
        print(f"::error::{description}", flush=True)
        failures.append(description)
    return ok


def http(url: str, *, headers: dict[str, str] | None = None, timeout: float = 10.0) -> int:
    """GET `url`, returning the HTTP status (including error statuses)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except Exception:
        return 0


def restart_status(value: str) -> dict:
    """Read the pending-restart notice from the API, authenticating with `value`.

    Auth is deliberately on for this whole run, so every direct call needs the
    header -- and after a rotation that means the NEW key, since the api tier
    is the one that applies it immediately.
    """
    return get_json(f"{API_URL}/api/v1/infra/restart-status", headers={"X-API-Key": value})


def get_json(url: str, *, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict, *, headers: dict[str, str] | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_for(url: str, *, expect: tuple[int, ...], timeout: float = 180.0) -> bool:
    """Poll `url` until it answers with one of `expect`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if http(url) in expect:
            return True
        time.sleep(1)
    return False


def write_config(cfg_path: Path, value: str) -> None:
    """Write the minimal config both tiers read. Auth is ON -- that's the point.

    `cfg_path` is a throwaway file under the smoke's own temp directory and
    `value` is a per-run generated string; a plain-text `config.ini` is also
    exactly what the product stores here by design (0600, `~/.nyxGPT`), so the
    scenario cannot be reproduced through any other store.
    """
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        "[ollama]\n"
        "base_url = http://127.0.0.1:11434\n"
        "\n"
        "[api]\n"
        "host = 127.0.0.1\n"
        f"port = {API_PORT}\n"
        "\n"
        "[web]\n"
        "host = 127.0.0.1\n"
        f"port = {WEB_PORT}\n"
        f"api_base_url = {API_URL}\n"
        "\n"
        "[auth]\n"
        "enabled = true\n"
        f"api_key = {value}\n"
        "header = X-API-Key\n",
        encoding="utf-8",
    )


def start_api(env: dict[str, str]) -> subprocess.Popen:
    """Start the real FastAPI app with uvicorn, exactly as the api wrapper does."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "nyxgpt.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        # Own session, so `stop()`'s killpg targets this server and not the
        # smoke process's own group.
        preexec_fn=os.setsid,
    )
    _procs.append(proc)
    return proc


# The pre-#3806-round-two api: identical in every way except that the startup
# reconciliation is neutered before the app is imported. `app.py` calls
# `restart_state_module.clear_started(...)` through the module object, so
# replacing the attribute reproduces exactly the product that shipped -- one
# where nothing retired a pending flag when the api came back up.
_PRE_FIX_API_LAUNCHER = """
import uvicorn
from nyxgpt import restart_state

restart_state.clear_started = lambda component, keys: []

uvicorn.run("nyxgpt.app:app", host="127.0.0.1", port={port})
"""


def start_api_without_startup_reconciliation(env: dict[str, str]) -> subprocess.Popen:
    """Start the api as it behaved BEFORE the fix, for the step 13 fault injection."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _PRE_FIX_API_LAUNCHER.format(port=API_PORT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    _procs.append(proc)
    return proc


def start_web(wrapper: Path, env: dict[str, str]) -> subprocess.Popen:
    """Start the web tier THROUGH THE REAL WRAPPER ops.py generates.

    This is the whole point of the smoke: the wrapper is where `[auth]
    api_key` is read once and exported as NYXGPT_AUTH_API_KEY. Starting Next
    directly with a hand-set env var would prove nothing about the product.
    """
    proc = subprocess.Popen(
        ["bash", str(wrapper)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    _procs.append(proc)
    return proc


def stop(proc: subprocess.Popen | None) -> None:
    """Stop a process (and its group, for the wrapper's `exec`ed child)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()


def cleanup() -> None:
    for proc in reversed(_procs):
        stop(proc)


def build_web_wrapper(home: Path) -> Path:
    """Generate the production web wrapper via ops.py itself, then point it at web/.

    Uses `_NATIVE_WEB_WRAPPER_TEMPLATE` -- the same string the installer
    writes -- so the key-reading logic under test is the shipped one. Only the
    substitution placeholders are filled here.
    """
    from nyxgpt import ops

    wrapper = home / "nyxgpt-web-wrapper.sh"
    body = (
        ops._NATIVE_WEB_WRAPPER_TEMPLATE.replace("__NYXGPT_WEB_ROOT__", str(REPO_ROOT / "web"))
        .replace("__NYXGPT_WEB_MODE__", "smoke")
        .replace("__NYXGPT_WEB_START_CMD__", 'npm run start -- --hostname "$HOST" --port "$PORT"')
    )
    # The template hard-codes ~/.nyxGPT/config.ini; $HOME is redirected for
    # this whole run, so that resolves to the smoke's own config.
    wrapper.write_text(body, encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def proxied_status() -> int:
    """Status of a call that goes browser -> web proxy -> api.

    `/api/info` is a Next route that reaches the backend through
    `apiProxy.ts`, which is what attaches the frozen NYXGPT_AUTH_API_KEY.
    """
    return http(f"{WEB_URL}/api/info")


def main() -> int:  # noqa: C901 -- a linear scenario reads better in one place
    if shutil.which("npm") is None:
        print("::error::npm is required to run the web tier for this smoke", flush=True)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="restart-activation-smoke-"))
    home = tmp / "home"
    home.mkdir(parents=True)
    cfg_path = home / ".nyxGPT" / "config.ini"
    pending_path = home / ".nyxGPT" / "pending-restart.json"

    # $HOME is redirected so both tiers read *this* config.ini -- the wrapper
    # hard-codes ~/.nyxGPT/config.ini, and redirecting HOME is the only way to
    # exercise it as written rather than a rewritten copy. Pin PYTHONUSERBASE
    # to the real one first: Python derives the user site-packages directory
    # from $HOME, so without this the redirected children lose every
    # pip --user package (uvicorn, nyxgpt itself) and simply fail to start.
    env = {
        **os.environ,
        "HOME": str(home),
        "PYTHONUSERBASE": os.environ.get("PYTHONUSERBASE") or site.getuserbase(),
        "NYXGPT_PENDING_RESTART_PATH": str(pending_path),
        "NYXGPT_API_BASE_URL": API_URL,
        "PYTHONUNBUFFERED": "1",
    }
    os.environ["NYXGPT_PENDING_RESTART_PATH"] = str(pending_path)
    os.environ["HOME"] = str(home)

    write_config(cfg_path, ORIGINAL_VALUE)

    api_proc: subprocess.Popen | None = None
    web_proc: subprocess.Popen | None = None
    try:
        log("Building the web tier (npm ci && npm run build)")
        subprocess.run(["npm", "ci"], cwd=REPO_ROOT / "web", check=True)
        subprocess.run(["npm", "run", "build"], cwd=REPO_ROOT / "web", check=True)

        log("Step 1: start both tiers, keys in agreement")
        api_proc = start_api(env)
        if not wait_for(f"{API_URL}/health", expect=(200,)):
            print("::error::api did not become healthy", flush=True)
            return 2

        wrapper = build_web_wrapper(home)
        web_proc = start_web(wrapper, env)
        if not wait_for(f"{WEB_URL}/api/info", expect=(200, 401, 502)):
            print("::error::web did not start", flush=True)
            return 2

        check(proxied_status() == 200, "baseline: a proxied call through web succeeds (200)")

        log("Step 2-3: rotate the key and reproduce the 401 wall")
        post_json(
            f"{API_URL}/api/v1/config/sections",
            {"auth": {"api_key": ROTATED_VALUE}},
            headers={"X-API-Key": ORIGINAL_VALUE},
        )

        check(
            http(f"{API_URL}/api/v1/info", headers={"X-API-Key": ROTATED_VALUE}) == 200,
            "the api tier honours the rotated key immediately (hot)",
        )
        check(
            http(f"{API_URL}/api/v1/info", headers={"X-API-Key": ORIGINAL_VALUE}) == 401,
            "the api tier rejects the old key immediately (hot)",
        )
        wall = proxied_status()
        check(
            wall == 401,
            f"THE DEFECT, reproduced: the still-running web tier 401s on every proxied call "
            f"(got {wall}) -- its NYXGPT_AUTH_API_KEY is frozen at the old value",
        )

        log("Step 4: the notice explains it instead of leaving a blank wall")
        status = restart_status(ROTATED_VALUE)
        check("web" in status["pending"], "restart-status flags `web` as pending")
        check(
            status["pending"].get("web", {}).get("keys") == ["auth.api_key"],
            "restart-status names the key that caused it (auth.api_key)",
        )
        check(
            status.get("restart_command") == "nyxgpt ops restart web",
            "restart-status offers the wrapped command, not a raw docker/brew one",
        )
        check(
            status.get("session_disrupting") == ["web"],
            "restart-status warns that restarting web drops the caller's session",
        )

        log("Step 5: defer -- restart the API and confirm the notice survives")
        stop(api_proc)
        api_proc = start_api(env)
        if not wait_for(f"{API_URL}/health", expect=(200,)):
            print("::error::api did not come back after restart", flush=True)
            return 2
        status = restart_status(ROTATED_VALUE)
        check(
            "web" in status["pending"],
            "the notice persists across an api restart (on-disk state, not in-memory)",
        )
        check(proxied_status() == 401, "deferring changes nothing about the running web tier")

        log("Step 6: a key written by the CLI shows up in the running API's notice")
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path\n"
                "from nyxgpt import secrets_setup\n"
                "spec = secrets_setup.find_guided_secret('auth', 'api_key')\n"
                f"secrets_setup.write_secret(Path({str(cfg_path)!r}), spec, {CLI_WRITTEN_VALUE!r})\n",
            ],
            env=env,
            check=True,
        )
        status = restart_status(CLI_WRITTEN_VALUE)
        check(
            "web" in status["pending"] and "auth.api_key" in status["pending"]["web"]["keys"],
            "a separate CLI process's write is visible to the running API (one behavior, two surfaces)",
        )

        log("Step 7: the Admin Dashboard's Access panel is the third writer")
        # `POST /admin/access` rotates `[auth] api_key` too, from the very page
        # that hosts the notice. A writer that stayed silent here would rebuild
        # the 401 wall on the one surface guaranteed to be looking at it.
        dashboard_value = post_json(
            f"{API_URL}/api/v1/admin/access",
            {"rotate": True},
            headers={"X-API-Key": CLI_WRITTEN_VALUE},
        )["api_key"]
        status = restart_status(dashboard_value)
        check(
            "web" in status["pending"] and "auth.api_key" in status["pending"]["web"]["keys"],
            "a dashboard rotation raises the same notice as the wizard and the CLI",
        )
        check(
            proxied_status() == 401,
            "and the web tier is demonstrably still frozen while it stands",
        )

        log("Step 8: a restart that did NOT happen must not clear the flag")
        subprocess.run(
            ["nyxgpt", "ops", "restart", "web"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        status = restart_status(dashboard_value)
        check(
            "web" in status["pending"],
            "a failed `nyxgpt ops restart web` leaves the notice standing (no false all-clear)",
        )

        log("Step 9: reverting retires the notice with no restart at all")
        # The web tier is still running ORIGINAL_VALUE. Writing that exact value
        # back means saved and running agree again -- which is the other way
        # #3806 says the notice retires ("...until the restart happens or the
        # value is reverted"). Nothing is restarted here, and both the notice
        # AND the 401 wall must go away.
        post_json(
            f"{API_URL}/api/v1/config/sections",
            {"auth": {"api_key": ORIGINAL_VALUE}},
            headers={"X-API-Key": dashboard_value},
        )
        check(
            restart_status(ORIGINAL_VALUE)["pending"] == {},
            "reverting to the value web is still running retires the notice, with no restart",
        )
        check(
            proxied_status() == 200,
            "and the 401 wall is gone -- saved and running agree again",
        )

        log("Step 10: rotate again, restart web, and show a healthy stack")
        post_json(
            f"{API_URL}/api/v1/config/sections",
            {"auth": {"api_key": ROTATED_VALUE}},
            headers={"X-API-Key": ORIGINAL_VALUE},
        )
        check(proxied_status() == 401, "rotating again re-raises the 401 wall")
        check("web" in restart_status(ROTATED_VALUE)["pending"], "and re-raises the notice")

        # Restarting the web tier IS re-execing this wrapper -- that is what
        # `nyxgpt ops restart web` ultimately does through systemd/launchd.
        stop(web_proc)
        web_proc = start_web(wrapper, env)
        if not wait_for(f"{WEB_URL}/api/info", expect=(200, 401)):
            print("::error::web did not come back after restart", flush=True)
            return 2
        restored = proxied_status()
        check(
            restored == 200,
            f"after the web restart the stack is healthy: proxied calls succeed on the new "
            f"key (got {restored}), not a blank 401",
        )

        # The product clears the flag from `ops.restart()` on a successful
        # restart. This runner has no service manager for `web` (step 7 proved
        # the failure path), so the clearing contract is exercised the way it
        # actually crosses processes: another process clears, the running API
        # stops reporting it.
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from nyxgpt import restart_state; restart_state.clear_pending('web')",
            ],
            env=env,
            check=True,
        )
        check(
            restart_status(ROTATED_VALUE)["pending"] == {},
            "clearing after a restart is visible to the already-running API process",
        )

        log("Step 11: fault injection -- prove steps 3-4 are not vacuous")
        # Rebuild the classification WITHOUT auth.api_key -- the pre-#3806
        # product -- in a subprocess, and confirm the rotation then produces
        # the 401 wall with NO notice at all. If this passes, steps 3-4 above
        # were testing something real.
        injected = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json\n"
                "from configparser import ConfigParser\n"
                "from nyxgpt import config_wizard, restart_state\n"
                "restart_state.reset()\n"
                "# Strip the classification, as it was before #3806.\n"
                "spec = config_wizard._SCHEMA_BY_SECTION['auth']\n"
                "for f in spec.fields:\n"
                "    object.__setattr__(f, 'restart_components', ())\n"
                "cfg = ConfigParser()\n"
                "cfg.read_dict({'auth': {'api_key': 'old'}})\n"
                "detail = config_wizard.restart_required_detail("
                "{'auth': {'api_key': 'new'}}, cfg)\n"
                "for component, changes in detail.items():\n"
                "    restart_state.mark_pending(component, changes)\n"
                "print(json.dumps(restart_state.snapshot()))\n",
            ],
            env={**env, "NYXGPT_PENDING_RESTART_PATH": str(tmp / "injected.json")},
            capture_output=True,
            text=True,
            check=True,
        )
        check(
            json.loads(injected.stdout.strip()) == {},
            "with the classification stripped (pre-#3806), the rotation raises NO notice "
            "-- so the notice seen in step 4 is produced by this change, not by luck",
        )

        log("Step 12: the api's own restart clears the api's own notice")
        # #3806 round two, the owner's re-test. The notice named `api --
        # cache.embedding_cache_enabled`, the api restarted fine, and the
        # notice never cleared: `clear_pending` ran at the end of a callback
        # living in the process the restart killed. A `web` entry is raised
        # alongside it deliberately -- clearing on startup must retire what
        # THIS process's restart settled and nothing else.
        post_json(
            f"{API_URL}/api/v1/config/sections",
            {"auth": {"api_key": FINAL_VALUE}, "cache": {"embedding_cache_enabled": True}},
            headers={"X-API-Key": ROTATED_VALUE},
        )
        pending = restart_status(FINAL_VALUE)["pending"]
        check(
            pending.get("api", {}).get("keys") == ["cache.embedding_cache_enabled"],
            "an api-classified save raises an `api` pending entry",
        )
        check("web" in pending, "and the `web` rotation alongside it raises a `web` entry")

        # What `nyxgpt ops restart api` ultimately does through
        # launchd/systemd: this process goes away, a new one takes its place.
        stop(api_proc)
        api_proc = start_api(env)
        if not wait_for(f"{API_URL}/health", expect=(200,)):
            print("::error::api did not come back after its own restart", flush=True)
            return 2

        pending = restart_status(FINAL_VALUE)["pending"]
        check(
            "api" not in pending,
            "THE DEFECT, fixed: after the api restarts itself the `api` notice is gone "
            "-- the process that came back retires the flag the dead one could not",
        )
        check(
            pending.get("web", {}).get("keys") == ["auth.api_key"],
            "and `web` is untouched: an api restart says nothing about the web tier",
        )

        log("Step 13: fault injection -- prove step 12 is not vacuous")
        # Raise a fresh api entry, then bring the api back as the PRE-FIX
        # product. The flag must survive -- which is the owner's bug, and the
        # proof that step 12's clearing is produced by this change.
        post_json(
            f"{API_URL}/api/v1/config/sections",
            {"cache": {"response_cache_enabled": True}},
            headers={"X-API-Key": FINAL_VALUE},
        )
        check(
            "api" in restart_status(FINAL_VALUE)["pending"],
            "a second api-classified save raises the notice again",
        )
        stop(api_proc)
        api_proc = start_api_without_startup_reconciliation(env)
        if not wait_for(f"{API_URL}/health", expect=(200,)):
            print("::error::pre-fix api did not start", flush=True)
            return 2
        check(
            "api" in restart_status(FINAL_VALUE)["pending"],
            "with the startup reconciliation neutered (pre-fix), a successful api restart "
            "leaves the notice standing forever -- so step 12 is testing this change",
        )

        # And the shipped api clears it on the next start: the state is not
        # wedged by having been through the broken build.
        stop(api_proc)
        api_proc = start_api(env)
        if not wait_for(f"{API_URL}/health", expect=(200,)):
            print("::error::api did not come back after the fault injection", flush=True)
            return 2
        check(
            "api" not in restart_status(FINAL_VALUE)["pending"],
            "and the shipped api clears that same standing flag on its next start",
        )

    finally:
        cleanup()
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        log(f"FAILED: {len(failures)} check(s) failed")
        for f in failures:
            log(f"  - {f}")
        return 1
    log("All checks passed: the rotation is announced, deferrable, and survivable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
