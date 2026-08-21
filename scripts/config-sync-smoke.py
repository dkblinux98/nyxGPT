#!/usr/bin/env python3
"""Executed evidence for `nyxgpt ops config-sync` and `config-drift` (#3976, #3775).

Answers the three questions inspection cannot:

1. **Does a repository variable actually reach GitHub from config.ini?**
   A variable is created with `POST /repos/{o}/{r}/actions/variables` and
   updated with `PATCH .../variables/{name}` -- a different shape from the
   secrets API's name-addressed `PUT`, and a shape no unit test can confirm
   because the transport is mocked. It is also the shape the second run of the
   command depends on: a repeat POST 409s, and treating that as an error would
   make the push work exactly once per variable. So this pushes the canary
   twice and reads the value back.

2. **Does a secret actually reach GitHub from config.ini?** Same question for
   the other manifest. A secret cannot be read back (that is the point of
   one), so the assertion is that its *name* appears in the repo's secret list
   after the push and did not before.

3. **Does `nyxgpt ops config-drift` report a seeded mismatch?** Run as the
   real CLI, in both directions: exit 0 on a config.ini that matches
   `example.config.ini`, exit 2 naming the key on one that does not. A check
   that cannot fail is not a check -- the #3753 fault-injection rule.

Nothing here touches a real secret or variable. The canary names are
`NYXGPT_CONFIG_SYNC_CANARY_*` and both are deleted in a `finally`, whatever
happened. The manifests are swapped for canary-only ones so the real values in
GitHub are never written; everything below that swap -- the client, the
sealed-box encryption, the request shapes, the error handling -- is the
shipped code path that `nyxgpt ops config-sync` calls.

Requires: `pip install -e .`, and `GITHUB_TOKEN_CANDIDATES` naming one or more
environment variables that may hold a token able to administer this repo's
Actions settings. The first that works is used and named in the output, which
is itself worth knowing: managing Actions secrets/variables needs admin on the
repo, not write.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from nyxgpt import config, ops  # noqa: E402

CANARY_VARIABLE = "NYXGPT_CONFIG_SYNC_CANARY_VARIABLE"
CANARY_SECRET = "NYXGPT_CONFIG_SYNC_CANARY_SECRET"

#: The canary rides on two keys that already exist in every config.ini, so the
#: file this smoke writes is an ordinary one rather than a special shape.
CANARY_VARIABLE_KEY = "github.repo_owner"
CANARY_SECRET_KEY = "monitoring.slack_bot_token"


def fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def announce(message: str) -> None:
    print(f"\n=== {message} ===", flush=True)


#: Probe name, distinct from the canary the assertions below use so a failed
#: probe can never be mistaken for a failed push.
PROBE_VARIABLE = "NYXGPT_CONFIG_SYNC_PROBE"


def pick_token(owner: str, name: str) -> tuple[str, str]:
    """Return `(env_var_name, token)` for the first candidate that can administer the repo.

    Managing Actions secrets and variables requires **admin** on the
    repository, which is a stronger permission than the write access the agent
    tokens need for everything else -- and GitHub's read and write bars for
    variables are different, so a token that can *list* them may still 403 on
    the create. The probe is therefore a real create-and-delete, not a read:
    the permission under test is the one that gets exercised.
    """
    candidates = [c.strip() for c in os.environ.get("GITHUB_TOKEN_CANDIDATES", "").split(",")]
    candidates = [c for c in candidates if c and os.environ.get(c)]
    if not candidates:
        fail("no candidate tokens are set -- GITHUB_TOKEN_CANDIDATES named none with a value")

    for env_name in candidates:
        token = os.environ[env_name]
        with ops._github_actions_client(token) as client:
            created = client.post(
                f"/repos/{owner}/{name}/actions/variables",
                json={"name": PROBE_VARIABLE, "value": "probe"},
            )
            if created.status_code in (201, 409):
                client.delete(f"/repos/{owner}/{name}/actions/variables/{PROBE_VARIABLE}")
                print(f"using {env_name}: it can write {owner}/{name}'s Actions variables")
                return env_name, token
        print(f"{env_name}: HTTP {created.status_code} creating a probe variable -- trying next")

    fail(
        "none of the candidate tokens can administer this repository's Actions "
        "settings. `nyxgpt ops config-sync` needs a PAT with admin on the repo; "
        "see docs/github-tokens.md."
    )
    raise AssertionError("unreachable")


def write_config(path: Path, owner: str, name: str, token: str, secret_value: str) -> None:
    path.write_text(
        "[github]\n"
        f"pat = {token}\n"
        f"repo_owner = {owner}\n"
        f"repo_name = {name}\n"
        "\n"
        "[monitoring]\n"
        f"slack_bot_token = {secret_value}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def delete_canaries(owner: str, name: str, token: str) -> None:
    with ops._github_actions_client(token) as client:
        for path in (
            f"/repos/{owner}/{name}/actions/variables/{CANARY_VARIABLE}",
            f"/repos/{owner}/{name}/actions/secrets/{CANARY_SECRET}",
        ):
            response = client.delete(path)
            print(f"cleanup: DELETE {path} -> HTTP {response.status_code}")


def main() -> int:
    owner = os.environ.get("REPO_OWNER", "").strip()
    name = os.environ.get("REPO_NAME", "").strip()
    if not owner or not name:
        fail("REPO_OWNER/REPO_NAME are not set")

    _env_name, token = pick_token(owner, name)

    # Swap both manifests for canary-only ones. `ops` imports them from
    # `nyxgpt.config` inside each function, so this is the same lookup the
    # shipped code makes -- everything below it is untouched.
    config.SECRETS_SYNC_MANIFEST = {CANARY_SECRET_KEY: CANARY_SECRET}
    config.VARIABLES_SYNC_MANIFEST = {CANARY_VARIABLE_KEY: CANARY_VARIABLE}

    tmpdir = Path(tempfile.mkdtemp(prefix="nyxgpt-config-sync-smoke-"))
    cfg_path = tmpdir / "config.ini"
    secret_value = f"xoxb-canary-{os.environ.get('GITHUB_RUN_ID', 'local')}"

    try:
        announce("A secret that is not there yet")
        with ops._github_actions_client(token) as client:
            # per_page=100: the default page is 30, and "the canary is in the
            # list" would start failing spuriously the day the repo crosses
            # that many secrets.
            before = client.get(f"/repos/{owner}/{name}/actions/secrets?per_page=100")
            before.raise_for_status()
            existing = {s["name"] for s in before.json()["secrets"]}
        if CANARY_SECRET in existing:
            print(f"{CANARY_SECRET} left over from an earlier run -- deleting before the test")
            delete_canaries(owner, name, token)

        announce("Push a variable and a secret from config.ini, the way the CLI does")
        write_config(cfg_path, owner, name, token, secret_value)

        results = ops.sync_secrets_to_github_actions(cfg_path=cfg_path)
        results += ops.sync_variables_to_github_actions(cfg_path=cfg_path)
        for r in results:
            print(
                f"  [{'ok' if r.ok else 'FAIL'}] {r.message}"
                + (f" -- {r.details}" if r.details else "")
            )
        if not all(r.ok for r in results):
            fail("config-sync reported a failure pushing the canaries")
        if secret_value in " ".join(r.message for r in results):
            fail("a secret value appeared in the ops output")

        announce("Read the variable back: it must be there, with the value from config.ini")
        with ops._github_actions_client(token) as client:
            got = client.get(f"/repos/{owner}/{name}/actions/variables/{CANARY_VARIABLE}")
            got.raise_for_status()
            value = got.json()["value"]
        print(f"  {CANARY_VARIABLE} = {value!r}")
        if value != owner:
            fail(f"variable reached GitHub with the wrong value: {value!r} != {owner!r}")

        announce("Confirm the secret landed: its name must now be listed")
        with ops._github_actions_client(token) as client:
            after = client.get(f"/repos/{owner}/{name}/actions/secrets?per_page=100")
            after.raise_for_status()
            names = {s["name"] for s in after.json()["secrets"]}
        if CANARY_SECRET not in names:
            fail(f"{CANARY_SECRET} is not in the repository's secret list after the push")
        print(f"  {CANARY_SECRET} present (value not readable, by design)")

        announce("Push again: the second run is the 409 -> PATCH path, not an error")
        # A different value proves the PATCH updated rather than silently no-opping.
        write_config(cfg_path, f"{owner}", name, token, secret_value)
        config.VARIABLES_SYNC_MANIFEST = {"github.repo_name": CANARY_VARIABLE}
        second = ops.sync_variables_to_github_actions(cfg_path=cfg_path)
        for r in second:
            print(
                f"  [{'ok' if r.ok else 'FAIL'}] {r.message}"
                + (f" -- {r.details}" if r.details else "")
            )
        if not all(r.ok for r in second):
            fail("the second push failed -- the 409 -> PATCH update path is broken")
        with ops._github_actions_client(token) as client:
            got = client.get(f"/repos/{owner}/{name}/actions/variables/{CANARY_VARIABLE}")
            got.raise_for_status()
            value = got.json()["value"]
        print(f"  {CANARY_VARIABLE} = {value!r} after the update")
        if value != name:
            fail(f"the update did not take: {value!r} != {name!r}")

        announce("The drift check must exit 0 on a config.ini that matches the example")
        clean = tmpdir / "clean.ini"
        clean.write_text((REPO_ROOT / "example.config.ini").read_text("utf-8"), "utf-8")
        rc = run_cli(["ops", "config-drift", "--config", str(clean)])
        if rc != 0:
            fail(f"config-drift exited {rc} on a config.ini that matches example.config.ini")

        announce("...and report a seeded mismatch, naming the key and never the value")
        seeded = tmpdir / "seeded.ini"
        seeded.write_text(
            clean.read_text("utf-8").replace(
                "[homebrew]", "[homebrew]\nseeded_undeclared_key = seeded-secret-value\n"
            ),
            "utf-8",
        )
        rc, out = run_cli_capture(["ops", "config-drift", "--config", str(seeded)])
        print(out)
        if rc == 0:
            fail("config-drift exited 0 on a seeded mismatch -- the check cannot fail")
        if "seeded_undeclared_key" not in out:
            fail("config-drift did not name the seeded key")
        if "seeded-secret-value" in out:
            fail("config-drift printed a value")

        announce("All three questions answered by running them")
        return 0
    finally:
        delete_canaries(owner, name, token)


#: Drive the installed console script, not `python -m nyxgpt.cli` -- the
#: module has no `__main__` guard, so `-m` exits 0 having done nothing, which
#: would make every CLI assertion below pass vacuously.
_NYXGPT_CLI = shutil.which("nyxgpt") or str(Path(sys.executable).parent / "nyxgpt")


def _cli_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def run_cli(args: list[str]) -> int:
    """Run the real `nyxgpt` CLI, streaming its output."""
    return subprocess.run([_NYXGPT_CLI, *args], env=_cli_env(), check=False).returncode


def run_cli_capture(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [_NYXGPT_CLI, *args],
        env=_cli_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


if __name__ == "__main__":
    sys.exit(main())
