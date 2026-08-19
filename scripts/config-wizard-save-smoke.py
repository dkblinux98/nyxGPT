#!/usr/bin/env python3
"""Executed verification for #3944: a wizard save must not brick the running API.

The question this answers, which no unit test can: **after a real
`POST /api/v1/config/sections` against a config.ini spelled the way the
owner's is, does the API still serve requests -- and can it still boot?**

The defect was not "the save failed". `config_wizard.apply_updates` wrote
config.ini before anything checked the result parsed; a key spelled
`SLACK_BOT_TOKEN` on disk was invisible to the case-sensitive matcher, so the
save *inserted a second* `slack_bot_token` line. `configparser` rejects that
file, `load_config` did not catch it, and every subsequent request 500'd while
a restarted API could not boot at all (502 through the web proxy). One UI
click, no in-product way back, damage on disk that no restart clears.

So this drives the owner's exact sequence against a real uvicorn process:

  1. Seed an isolated `$HOME/.nyxGPT/config.ini` from `example.config.ini`,
     with `[monitoring] SLACK_BOT_TOKEN` uppercase (the owner's convention --
     these keys mirror GitHub secret names) plus a stale key to Remove.
  2. Start the API, click Remove, then Save.
  3. Assert the save succeeded, the stale key really went, the file still
     parses, the option was rewritten *in place with its casing intact*, and
     the API still answers.
  4. Kill the API and start it again from that same config.ini -- the 502
     half of the failure, which only a fresh boot can see.

Then, per the #3753 fault-injection rule, it does the whole thing again with
the pre-fix behaviour monkeypatched back in (case-sensitive matcher, unchecked
`write_text`) and asserts the API *does* break. Without that half this job
would pass on any build, including one that never fixed anything.

A second pair of scenarios covers what the stated cause is allowed to *say*
(review finding on PR #3960). The guard that reports an unreadable config.ini
has to be the outermost middleware, because `api_key_auth` calls `load_config`
itself before it can read `[auth] enabled` or compare a key -- so while the
file is unparseable there is no request on which auth is enforceable, and that
diagnosis reaches anyone who can reach the port. With auth switched on and a
credential in the file, the API is booted on a good config, the file is then
hand-damaged so a secret-bearing line sits above the first section header, and
an *anonymous* request must come back naming the error class and the line
number and quoting none of the file. The leaking rendering is injected first
and required to leak, for the same reason as above.

Run: `python3 scripts/config-wizard-save-smoke.py` (no arguments; needs the
package importable, e.g. `pip install -e .`).
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "example.config.ini"

#: The section and key that fired on the owner's machine. `slack_bot_token` is
#: non-secret in `WIZARD_SCHEMA`, so the wizard echoes it to the form and posts
#: it back on *every* save -- which is why an ordinary save was enough.
SECTION = "monitoring"
KEY = "slack_bot_token"
DISK_SPELLING = "SLACK_BOT_TOKEN"
STALE_KEY = "an_option_no_longer_in_the_example_config"

# Restores the exact pre-fix behaviour of both halves of the defect: a
# case-sensitive matcher (which duplicates instead of updating) and a write
# that never checks the merged text parses. Executed inside the API process,
# before uvicorn imports the app.
INJECT_PREFIX_BEHAVIOUR = """
import nyxgpt.config_wizard as w


def _prefix_find_key_line(lines, start, end, key):
    found = None
    for i in range(start, end):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        if "=" not in lines[i]:
            continue
        if lines[i].split("=", 1)[0].strip() == key:
            found = i
    return found


def _prefix_write(cfg_path, new_text, _original_text):
    cfg_path.write_text(new_text, encoding="utf-8")
    cfg_path.chmod(0o600)


w._find_key_line = _prefix_find_key_line
w._write_ini_checked = _prefix_write
"""

#: A credential seeded into config.ini, used by the disclosure half below.
SECRET_ON_DISK = "sk-live-SMOKE-SECRET-VALUE"

# Restores the pre-review rendering of a parse error: one that quotes the raw
# offending line. `MissingSectionHeaderError.line` is the setting that appears
# before any header, so on a hand-edit slip that line is a credential -- and
# this diagnosis is what the API returns to an *unauthenticated* caller,
# because `api_key_auth` cannot check a key it cannot load a config to find.
INJECT_LEAKY_DIAGNOSIS = """
import configparser
import nyxgpt.config as c

_original_describe = c.describe_config_parse_error


def _leaky_describe(config_path, exc, **_kwargs):
    if isinstance(exc, configparser.MissingSectionHeaderError):
        return (
            f"Cannot parse {config_path}: {type(exc).__name__} at line {exc.lineno}: "
            f"{exc.line.strip()!r} appears before any [section] header."
        )
    return _original_describe(config_path, exc)


c.describe_config_parse_error = _leaky_describe
"""


def log(message: str) -> None:
    print(f"[config-wizard-save-smoke] {message}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def seed_config(home: Path) -> Path:
    """Write an isolated config.ini in the shape that reproduces #3944."""
    cfg_dir = home / ".nyxGPT"
    cfg_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.ini"
    shutil.copyfile(EXAMPLE_CONFIG, cfg_path)

    text = cfg_path.read_text(encoding="utf-8")
    # Uppercase on disk, and a stale key for the Remove click. Appended as a
    # fresh section body so this does not depend on how example.config.ini
    # currently spells or orders [monitoring].
    if not text.endswith("\n"):
        text += "\n"
    text += (
        f"\n[{SECTION}]\n"
        "enabled = false\n"
        f"{DISK_SPELLING} = xoxb-seeded-by-the-smoke-test\n"
        f"{STALE_KEY} = leftover\n"
    )
    # `[monitoring]` may already exist in example.config.ini; two headers of
    # the same name is itself a DuplicateSectionError, so drop the first one's
    # body into ours instead of appending a second header.
    cfg_path.write_text(_dedupe_section(text, SECTION), encoding="utf-8")
    cfg_path.chmod(0o600)

    # The seed itself must be valid -- otherwise every assertion below is
    # measuring the fixture, not the product.
    configparser.ConfigParser().read(cfg_path, encoding="utf-8")
    return cfg_path


def _dedupe_section(text: str, section: str) -> str:
    """Keep only the LAST `[section]` block, merging nothing (fixture helper)."""
    lines = text.splitlines()
    header = f"[{section}]"
    starts = [i for i, line in enumerate(lines) if line.strip() == header]
    if len(starts) < 2:
        return "\n".join(lines) + "\n"
    keep = starts[-1]
    out: list[str] = []
    skipping = False
    for i, line in enumerate(lines):
        if line.strip().startswith("[") and line.strip().endswith("]"):
            skipping = line.strip() == header and i != keep
        if not skipping:
            out.append(line)
    return "\n".join(out) + "\n"


def start_api(home: Path, port: int, *, bootstrap: str = "") -> subprocess.Popen:
    """Launch a real uvicorn serving `nyxgpt.app:app` against `home`'s config.ini.

    `bootstrap` is executed in the API process *before* uvicorn imports the
    app, which is how the pre-fix behaviours are injected (#3753's rule: a
    green run must be unable to be green by luck).
    """
    code = (
        bootstrap
        + "\nimport uvicorn\n"
        + f"uvicorn.run('nyxgpt.app:app', host='127.0.0.1', port={port}, log_level='warning')\n"
    )
    # `$HOME` is redirected so the API reads the seeded config.ini rather than
    # the developer's real one -- which also moves the *user* site-packages
    # dir out from under an editable/user install, so carry this interpreter's
    # own import path across explicitly.
    env = dict(
        os.environ,
        HOME=str(home),
        PYTHONPATH=os.pathsep.join(p for p in sys.path if p),
    )
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def wait_for_health(proc: subprocess.Popen, port: int, timeout: float = 60.0) -> bool:
    """Return True once `/health` answers; False if it never does in `timeout`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed loopback URL
                f"http://127.0.0.1:{port}/health", timeout=2
            ) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def http_json(port: int, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """POST (or GET) `path` and return `(status, body)`, treating 4xx/5xx as data."""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(  # noqa: S310 - fixed loopback URL
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read() or b"{}"
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body.decode(errors="replace")}


def stop(proc: subprocess.Popen) -> str:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ""


def drive_save(home: Path, port: int) -> dict:
    """Run the owner's two clicks -- Remove, then Save -- and report what happened."""
    remove_status, _ = http_json(
        port,
        "/api/v1/config/sections/stale-keys/remove",
        {"remove": {SECTION: [STALE_KEY]}},
    )
    save_status, save_body = http_json(
        port,
        "/api/v1/config/sections",
        {SECTION: {KEY: "xoxb-saved-by-the-smoke-test"}},
    )
    read_back_status, _ = http_json(port, "/api/v1/config/sections")
    cfg_text = (home / ".nyxGPT" / "config.ini").read_text(encoding="utf-8")
    parses = True
    try:
        configparser.ConfigParser().read_string(cfg_text)
    except configparser.Error:
        parses = False
    return {
        "remove_status": remove_status,
        "save_status": save_status,
        "save_body": save_body,
        "read_back_status": read_back_status,
        "config_text": cfg_text,
        "config_parses": parses,
    }


def scenario(*, inject_prefix: bool) -> dict:
    """Seed, start, Remove+Save, then restart from the resulting config.ini."""
    with tempfile.TemporaryDirectory(prefix="nyxgpt-3944-") as tmp:
        home = Path(tmp)
        seed_config(home)
        port = free_port()

        bootstrap = INJECT_PREFIX_BEHAVIOUR if inject_prefix else ""
        proc = start_api(home, port, bootstrap=bootstrap)
        if not wait_for_health(proc, port):
            raise AssertionError(
                "the API never came up on the SEEDED config.ini, so this run proves "
                f"nothing about the save:\n{stop(proc)}"
            )
        try:
            result = drive_save(home, port)
        finally:
            result_log = stop(proc)

        # The 502 half: can a *fresh* process still boot from this file?
        reboot = start_api(home, port, bootstrap=bootstrap)
        result["reboots"] = wait_for_health(reboot, port, timeout=45.0)
        reboot_log = stop(reboot)

        result["log"] = result_log + reboot_log
        return result


def check_fixed() -> None:
    log("shipped behaviour: Remove, then Save, against an uppercase key on disk")
    r = scenario(inject_prefix=False)

    failures: list[str] = []
    if r["save_status"] != 200:
        failures.append(f"save returned {r['save_status']}: {r['save_body']}")
    if r["remove_status"] != 200:
        failures.append(f"stale-key removal returned {r['remove_status']}")
    if not r["config_parses"]:
        failures.append("config.ini no longer parses after the save -- this is the brick")
    if r["read_back_status"] != 200:
        failures.append(f"the API stopped answering after the save ({r['read_back_status']})")
    if not r["reboots"]:
        failures.append("a fresh API process could not boot from the saved config.ini")

    text = r["config_text"]
    occurrences = sum(1 for line in text.splitlines() if line.strip().lower().startswith(KEY))
    if occurrences != 1:
        failures.append(f"expected exactly one {KEY} line, found {occurrences}")
    if f"{DISK_SPELLING} = xoxb-saved-by-the-smoke-test" not in text:
        failures.append(
            f"the value was not written to the existing {DISK_SPELLING} line with its "
            "casing preserved"
        )
    if STALE_KEY in text:
        failures.append("Remove reported success but the stale key is still on disk")

    if failures:
        raise AssertionError(
            "the shipped code still bricks the API:\n  - "
            + "\n  - ".join(failures)
            + f"\n--- config.ini ---\n{text}\n--- api log ---\n{r['log'][-4000:]}"
        )
    log("PASS: save applied in place, file parses, API answers, fresh process boots")


def check_injection_reproduces() -> None:
    """Without the fix, the same clicks must break the API (#3753's inverse proof)."""
    log("injected pre-fix behaviour: the same two clicks must brick the API")
    r = scenario(inject_prefix=True)

    broke = (not r["config_parses"]) or r["read_back_status"] != 200 or not r["reboots"]
    if not broke:
        raise AssertionError(
            "injecting the pre-fix matcher and unchecked write did NOT break anything, so "
            "this job cannot tell a fixed build from a broken one. Either the injection no "
            "longer matches the pre-fix code or the fixture stopped reproducing the "
            f"defect.\n--- config.ini ---\n{r['config_text']}"
        )
    log(
        "PASS: pre-fix behaviour reproduces the defect "
        f"(parses={r['config_parses']}, read_back={r['read_back_status']}, "
        f"reboots={r['reboots']})"
    )


def seed_config_with_auth_enabled(home: Path) -> Path:
    """Seed the same config.ini, with API-key auth on and a real secret in it."""
    cfg_path = seed_config(home)
    text = cfg_path.read_text(encoding="utf-8")
    text += f"\n[auth]\nenabled = true\napi_key = {SECRET_ON_DISK}\nheader = X-API-Key\n"
    cfg_path.write_text(_dedupe_section(text, "auth"), encoding="utf-8")
    cfg_path.chmod(0o600)
    configparser.ConfigParser().read(cfg_path, encoding="utf-8")
    return cfg_path


def http_status_and_text(port: int, path: str) -> tuple[int, str]:
    """GET `path` with **no** credentials and return `(status, raw body text)`."""
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url)  # noqa: S310 - fixed loopback URL
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, (resp.read() or b"").decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"").decode(errors="replace")


def disclosure_scenario(*, inject_leak: bool) -> dict:
    """Boot on a good config, then hand-damage it, and read the anonymous 500 body.

    This is the state the review found: config.ini becomes unparseable while
    the API is up (a hand-edit, or any other writer), and from that moment
    `api_key_auth` cannot enforce anything -- it calls `load_config` before it
    can read `[auth] enabled` or compare a key. So whatever the parse
    diagnosis says travels to any caller who can reach the port, with auth
    configured and switched on. Only a real ASGI stack shows this: it depends
    on the middleware registration order and on the guard being outermost.
    """
    with tempfile.TemporaryDirectory(prefix="nyxgpt-3944-disclosure-") as tmp:
        home = Path(tmp)
        cfg_path = seed_config_with_auth_enabled(home)
        port = free_port()
        bootstrap = INJECT_LEAKY_DIAGNOSIS if inject_leak else ""

        proc = start_api(home, port, bootstrap=bootstrap)
        if not wait_for_health(proc, port):
            raise AssertionError(f"the API never came up on the seeded config.ini:\n{stop(proc)}")
        try:
            # Auth really is on: an anonymous request is refused while the
            # file is still readable. Without this the run below would prove
            # nothing about *pre-auth* disclosure.
            authed_status, _ = http_status_and_text(port, "/api/v1/config/sections")

            # The hand-edit slip the recovery docs walk a user through: a
            # setting ends up above its section header. `configparser` reports
            # line 1 -- and line 1 is a credential.
            cfg_path.write_text(
                f"api_key = {SECRET_ON_DISK}\n" + cfg_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            status, body = http_status_and_text(port, "/api/v1/config/sections")
        finally:
            api_log = stop(proc)

        return {
            "status_before_damage": authed_status,
            "status": status,
            "body": body,
            "log": api_log,
        }


def check_no_pre_auth_disclosure() -> None:
    """The stated cause must not carry the file's own bytes to an anonymous caller."""
    log("shipped behaviour: an anonymous caller hitting a hand-damaged config.ini")
    r = disclosure_scenario(inject_leak=False)

    failures: list[str] = []
    if r["status_before_damage"] != 401:
        failures.append(
            "auth was not being enforced before the damage "
            f"(got {r['status_before_damage']}, expected 401), so this run cannot "
            "show anything about pre-auth disclosure"
        )
    if r["status"] != 500:
        failures.append(f"expected 500 while config.ini is unparseable, got {r['status']}")
    if "config_unreadable" not in r["body"]:
        failures.append("the response no longer names its cause -- redaction cost the diagnosis")
    if "MissingSectionHeaderError" not in r["body"] or "line 1" not in r["body"]:
        failures.append("the error class and line number must survive redaction")
    if SECRET_ON_DISK in r["body"]:
        failures.append(
            "the response body contains a credential read out of config.ini: " f"{r['body'][:400]}"
        )
    if failures:
        raise AssertionError(
            "the anonymous config_unreadable response is wrong:\n  - " + "\n  - ".join(failures)
        )
    log("PASS: anonymous 500 names the class and line, and carries no line content")


def check_leak_injection_reproduces() -> None:
    """Without the redaction, that same response must leak (#3753's inverse proof)."""
    log("injected pre-review diagnosis: the same anonymous request must leak the line")
    r = disclosure_scenario(inject_leak=True)

    if SECRET_ON_DISK not in r["body"]:
        raise AssertionError(
            "injecting the line-quoting diagnosis did NOT leak the credential, so this "
            "job cannot tell a redacted build from a leaking one. Either the injection no "
            "longer matches the pre-review rendering or the fixture stopped reaching it "
            f"(status={r['status']}, body={r['body'][:400]})"
        )
    log(f"PASS: pre-review rendering discloses the line (status={r['status']})")


def main() -> int:
    if not EXAMPLE_CONFIG.is_file():
        log(f"FAIL: {EXAMPLE_CONFIG} not found")
        return 1
    try:
        check_injection_reproduces()
        check_fixed()
        check_leak_injection_reproduces()
        check_no_pre_auth_disclosure()
    except AssertionError as e:
        log(f"FAIL: {e}")
        return 1
    log("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
