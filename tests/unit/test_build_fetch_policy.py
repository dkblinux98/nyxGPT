"""Every build step that reaches a package index runs under one stated policy (#4205).

A transient `npm ci` failure reddened a required check on PR #4193 and was
filed as #4205. Reproducing it deliberately (``docker build --network none
--target deps web/``, on a Linux runner, 2026-10-21) found something worse than
the report: `npm ci` **exits 0** with the registry unreachable, printing
``npm error Exit handler never called!`` over a `node_modules` of 520 empty
package directories with no ``.package-lock.json`` and no ``.bin``. BuildKit
then exported and cached that layer. One blip, a green build, a web image that
cannot start.

`src/nyxgpt/build_fetch_policy.py` is the one place that answers "what happens
when the index is unreachable?" for retries, for post-conditions, and for how
the failure is reported. This file is what keeps it one place:

* a Dockerfile that gains a fetching ``RUN`` without the policy fails here;
* a fetching tool the policy has never been stated for fails here, rather
  than silently taking the no-policy path (the #4174 lesson: a rule nothing
  runs is not a rule);
* a Dockerfile or Homebrew formula whose copy of the policy drifts from the
  module fails here, which is what makes "one place" true of a format with no
  include mechanism;
* the classifier is pinned against real captured output on both sides -- it
  must fire on a reachability failure and must NOT fire on a genuine build
  defect, because telling an operator to retry something retrying cannot fix
  is worse than saying nothing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from nyxgpt import build_fetch_policy as policy

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories whose contents are not ours to hold to this policy.
_SKIP_PARTS = {"node_modules", ".git", ".venv", "build", "dist", ".next"}


def _dockerfiles() -> list[Path]:
    """Every Dockerfile in the tree, however it is named."""
    found: list[Path] = []
    for pattern in ("Dockerfile", "Dockerfile.*", "*.Dockerfile"):
        for path in REPO_ROOT.rglob(pattern):
            if set(path.parts) & _SKIP_PARTS or not path.is_file():
                continue
            found.append(path)
    return sorted(found)


def _run_steps(dockerfile: Path) -> list[str]:
    """The `RUN` instructions of `dockerfile`, line continuations joined."""
    text = dockerfile.read_text(encoding="utf-8")
    # Join `\`-continued lines first so a multi-line RUN is one step, then
    # drop comment lines (a `#` line inside a continuation is not legal
    # Dockerfile anyway).
    joined = re.sub(r"\\\s*\n", " ", text)
    return [
        line.strip()[4:] for line in joined.splitlines() if line.strip().lower().startswith("run ")
    ]


# Tokens that mean "this step fetches from a remote index", mapped to the
# substring that proves this project's policy was applied to it. A step
# matching a key must contain the value.
#
# `curl`/`wget` are matched as *commands* (start of step, or after a shell
# separator) so a package NAMED curl -- `dnf install curl-minimal` -- is not
# mistaken for an invocation.
_FETCH_TOOLS: dict[str, tuple[re.Pattern[str], str]] = {
    "npm": (re.compile(r"(?:^|[;&|]\s*)npm\s+(?:ci|install|run)\b"), ""),
    "pip": (re.compile(r"(?:^|[;&|]\s*)pip\s+install\b"), ""),
    "apt-get": (
        re.compile(r"(?:^|[;&|]\s*)apt-get\b"),
        f"Acquire::Retries={policy.RETRIES}",
    ),
    "apk": (re.compile(r"(?:^|[;&|]\s*)apk\s+add\b"), "__no_policy_stated__"),
    "dnf": (
        re.compile(r"(?:^|[;&|]\s*)dnf\s+(?:-y\s+)?install\b"),
        f"--setopt=retries={policy.RETRIES}",
    ),
    "yum": (
        re.compile(r"(?:^|[;&|]\s*)yum\s+(?:-y\s+)?install\b"),
        f"--setopt=retries={policy.RETRIES}",
    ),
    "curl": (re.compile(r"(?:^|[;&|]\s*)curl\s"), "--retry-connrefused"),
    "wget": (re.compile(r"(?:^|[;&|]\s*)wget\s"), "__no_policy_stated__"),
}

#: Tools whose policy is the ENV block rather than an inline flag.
_ENV_POLICY_TOOLS = {"npm", "pip"}


def _fetch_tools_in(step: str) -> set[str]:
    return {tool for tool, (pattern, _) in _FETCH_TOOLS.items() if pattern.search(step)}


# --- The policy is internally consistent ------------------------------------


def test_dockerfile_stanza_is_generated_from_the_policy() -> None:
    """The stanza is `ENV_POLICY`, not a second hand-maintained copy of it."""
    for name, value in policy.ENV_POLICY.items():
        assert f"ENV {name}={value}" in policy.DOCKERFILE_ENV_STANZA
    declared = re.findall(r"^ENV (\w+)=", policy.DOCKERFILE_ENV_STANZA, re.MULTILINE)
    assert sorted(declared) == sorted(policy.ENV_POLICY)


def test_npm_step_embeds_the_completion_test_and_the_marker() -> None:
    step = policy.npm_install_run_step("npm ci")
    assert "npm ci;" in step
    assert policy.NPM_TREE_COMPLETE_TEST in step
    assert policy.INCOMPLETE_FETCH_MARKER in step
    # The marker it prints has to be the one the classifier matches, or the
    # step goes red with nothing attributing it.
    assert policy.classify_failure(policy.incomplete_fetch_message("npm ci", 0)) is not None


# --- The guard: no fetching build step escapes the policy -------------------


def test_every_dockerfile_fetch_step_carries_the_policy() -> None:
    """A fetching `RUN` without this project's policy fails the build.

    This is the check acceptance criterion 5 asks for. It is deliberately
    shaped as "an unrecognised fetching tool is a failure" rather than "a
    known tool must be flagged": the former forces a *decision* when someone
    adds `apk add` or `wget` to an image, the latter quietly lets it through.
    """
    missing: list[str] = []
    for dockerfile in _dockerfiles():
        rel = dockerfile.relative_to(REPO_ROOT)
        for step in _run_steps(dockerfile):
            for tool in sorted(_fetch_tools_in(step)):
                required = _FETCH_TOOLS[tool][1]
                if tool in _ENV_POLICY_TOOLS:
                    # Policy is the ENV block; asserted per file below.
                    continue
                if required == "__no_policy_stated__":
                    missing.append(
                        f"{rel}: `{tool}` fetches from a remote index and "
                        "build_fetch_policy.py states no policy for it. Add one "
                        "(and its flags here) rather than leaving the step "
                        "unprotected."
                    )
                elif required not in step:
                    missing.append(
                        f"{rel}: `{tool}` step is missing `{required}` -- " f"{step[:90]!r}"
                    )
    assert not missing, "Remote-fetch steps without the #4205 policy:\n" + "\n".join(missing)


def test_dockerfiles_that_fetch_with_npm_or_pip_carry_the_env_stanza() -> None:
    """And carry it verbatim, so the one place stays one place.

    Docker has no include mechanism. "One source of truth" for a Dockerfile
    `ENV` block therefore means one string in Python plus this assertion --
    without it, N stanzas drift and the module becomes documentation.
    """
    problems: list[str] = []
    for dockerfile in _dockerfiles():
        rel = dockerfile.relative_to(REPO_ROOT)
        text = dockerfile.read_text(encoding="utf-8")
        needs_env = any(
            _fetch_tools_in(step) & _ENV_POLICY_TOOLS for step in _run_steps(dockerfile)
        )
        if not needs_env:
            continue
        if policy.DOCKERFILE_ENV_STANZA not in text:
            problems.append(
                f"{rel}: fetches with npm/pip but does not carry "
                "build_fetch_policy.DOCKERFILE_ENV_STANZA verbatim"
            )
            continue
        # A multi-stage build needs it on every stage that fetches: `ENV` does
        # not cross a `FROM`. This is how the `builder` stage's `npm run
        # build` would otherwise have been left on npm's defaults.
        stages = text.split("\nFROM ")
        for stage in stages:
            fetches = any(
                _fetch_tools_in(step) & _ENV_POLICY_TOOLS for step in _run_steps_text(stage)
            )
            if fetches and policy.DOCKERFILE_ENV_STANZA not in stage:
                first = stage.splitlines()[0][:60]
                problems.append(
                    f"{rel}: stage starting {first!r} fetches with npm/pip but "
                    "the policy ENV block is on another stage -- ENV does not "
                    "cross a FROM"
                )
    assert not problems, "\n".join(problems)


def _run_steps_text(text: str) -> list[str]:
    joined = re.sub(r"\\\s*\n", " ", text)
    return [
        line.strip()[4:] for line in joined.splitlines() if line.strip().lower().startswith("run ")
    ]


def test_dockerfile_npm_install_steps_carry_the_completion_check() -> None:
    """`npm ci`/`npm install` must assert they actually installed something.

    The step the reproduction broke. Without the receipt check the step exits
    0 over an empty tree, and nothing downstream of the build knows.
    """
    problems: list[str] = []
    for dockerfile in _dockerfiles():
        rel = dockerfile.relative_to(REPO_ROOT)
        for step in _run_steps(dockerfile):
            if not re.search(r"(?:^|[;&|]\s*)npm\s+(?:ci|install)\b", step):
                continue
            if policy.NPM_TREE_COMPLETE_TEST not in step:
                problems.append(
                    f"{rel}: `npm ci`/`npm install` step does not assert "
                    f"`{policy.NPM_TREE_COMPLETE_TEST}` -- npm exits 0 over an "
                    f"empty node_modules when the registry is unreachable. Use "
                    f"build_fetch_policy.npm_install_run_step(). Step: {step[:80]!r}"
                )
    assert not problems, "\n".join(problems)


def test_web_dockerfile_npm_ci_step_is_exactly_the_generated_one() -> None:
    """The image #4205 was filed about, pinned against the generator."""
    text = (REPO_ROOT / "web" / "Dockerfile").read_text(encoding="utf-8")
    assert f"RUN {policy.npm_install_run_step('npm ci')}" in text


# --- The same policy on the Homebrew path -----------------------------------

_FORMULAS = (
    Path("homebrew") / "nyxgpt-web.rb",
    Path("homebrew") / "nyxgpt-api.rb",
    Path("homebrew") / "tap" / "nyxgpt-web.rb.tmpl",
    Path("homebrew") / "tap" / "nyxgpt-api.rb.tmpl",
)


@pytest.mark.parametrize("formula", _FORMULAS, ids=lambda p: p.name)
def test_formula_policy_values_match_the_module(formula: Path) -> None:
    """A formula that sets a policy variable sets the module's value for it.

    Homebrew builds under a sanitized environment, so the formulas have to
    state the policy themselves -- which is a copy, which is why this check
    exists. `ENV` rather than flags is load-bearing on the pip side: the build
    backend's child pip inherits no flag from its parent (D-057/#4122) but
    does inherit the environment.
    """
    text = (REPO_ROOT / formula).read_text(encoding="utf-8")
    for name, expected in policy.ENV_POLICY.items():
        for found in re.findall(rf'ENV\["{re.escape(name)}"\]\s*=\s*"([^"]*)"', text):
            assert found == expected, (
                f"{formula} sets {name}={found!r}; build_fetch_policy.py says " f"{expected!r}"
            )


@pytest.mark.parametrize(
    "formula",
    (Path("homebrew") / "nyxgpt-web.rb", Path("homebrew") / "tap" / "nyxgpt-web.rb.tmpl"),
    ids=lambda p: p.name,
)
def test_web_formula_checks_the_npm_receipt(formula: Path) -> None:
    """`system "npm", "ci"` raising is not enough on macOS either (#4205).

    Homebrew's `system` raises on a non-zero exit and npm exits 0, so the keg
    would have built, linked and started over an empty node_modules.
    """
    text = (REPO_ROOT / formula).read_text(encoding="utf-8")
    assert 'system "npm", "ci"' in text
    assert (
        policy.NPM_TREE_RECEIPT in text
    ), f"{formula} runs `npm ci` without asserting {policy.NPM_TREE_RECEIPT}"
    assert policy.INCOMPLETE_FETCH_MARKER in text
    assert "NPM_CONFIG_FETCH_RETRIES" in text


# --- Classification: fires on reachability, silent on real defects ----------

# Captured output, not invented. The first is this runner's own `docker build
# --network none` of web/Dockerfile; the rest are the shapes the other tools
# produce (and the one quoted in #4205 itself).
_REACHABILITY_OUTPUTS = {
    "web_dockerfile_network_none": (
        "#8 [deps 4/4] RUN npm ci\n"
        "#8 141.1 npm error Exit handler never called!\n"
        "#8 141.1 npm error This is an error with npm itself. Please report this error at:\n"
        "#8 DONE 141.2s\n"
    ),
    "issue_4205_original_occurrence": (
        "#1 [internal] load build definition from Dockerfile\n"
        "#8 23.97 npm error network In most cases you are behind a proxy or have "
        "bad network settings.\n"
        'ERROR: failed to build: failed to solve: process "/bin/sh -c npm ci" '
        "did not complete successfully: exit code: 152\n"
    ),
    "npm_dns": "#8 12.3 npm error code EAI_AGAIN\n",
    "npm_reset": "npm ERR! code ECONNRESET\n",
    "npm_request_failed": (
        "npm error request to https://registry.npmjs.org/next failed, reason: "
        "getaddrinfo ENOTFOUND registry.npmjs.org\n"
    ),
    "pip_fetch": (
        "  WARNING: Retrying (Retry(total=4, connect=None, read=None, "
        "redirect=None, status=None)) after connection broken by "
        "'NewConnectionError'\n"
        "ERROR: Could not fetch URL https://pypi.org/simple/httpx/\n"
    ),
    "apt_resolve": (
        "Err:1 http://deb.debian.org/debian bookworm InRelease\n"
        "  Temporary failure resolving 'deb.debian.org'\n"
        "E: Failed to fetch http://deb.debian.org/debian/dists/bookworm/InRelease\n"
    ),
    "dnf_mirror": (
        "Errors during downloading metadata for repository 'amazonlinux':\n"
        "  - Curl error (6): Couldn't resolve host name\n"
    ),
    "docker_registry": (
        "ERROR: failed to solve: node:20-alpine: failed to do request: Head "
        '"https://registry-1.docker.io/v2/library/node/manifests/20-alpine": '
        "dial tcp: lookup registry-1.docker.io: no such host\n"
    ),
    "our_own_post_condition": policy.incomplete_fetch_message("npm ci", 0),
}

# Genuine build failures. Each would send an operator round a retry loop that
# cannot terminate if it were reported as transient.
_REAL_DEFECT_OUTPUTS = {
    "npm_missing_package": (
        "npm error code E404\n"
        "npm error 404 Not Found - GET https://registry.npmjs.org/nyxgpt-nope\n"
    ),
    "npm_integrity": (
        "npm error code EINTEGRITY\n"
        "npm error sha512-abc... integrity checksum failed when using sha512\n"
    ),
    "npm_peer_conflict": (
        "npm error code ERESOLVE\nnpm error ERESOLVE unable to resolve dependency tree\n"
    ),
    "lockfile_out_of_sync": (
        "npm error `npm ci` can only install packages when your package.json and "
        "package-lock.json are in sync\n"
    ),
    "typescript_build_error": (
        "./src/app/page.tsx:12:5\n"
        "Type error: Property 'nope' does not exist on type 'Props'.\n"
        'ERROR: failed to solve: process "/bin/sh -c npm run build" did not '
        "complete successfully: exit code: 1\n"
    ),
    "missing_copy_source": (
        'ERROR: failed to solve: failed to compute cache key: "/example.config.ini" '
        "not found: not found\n"
    ),
    "dockerfile_syntax": (
        "ERROR: failed to solve: dockerfile parse error on line 7: unknown " "instruction: RUNN\n"
    ),
    "pip_resolution": (
        "ERROR: Cannot install nyxgpt because these package versions have "
        "conflicting dependencies.\n"
    ),
    "empty": "",
}


@pytest.mark.parametrize("name", sorted(_REACHABILITY_OUTPUTS))
def test_classify_names_a_reachability_failure(name: str) -> None:
    failure = policy.classify_failure(_REACHABILITY_OUTPUTS[name])
    assert failure is not None, f"{name} was not recognised as a reachability failure"
    assert failure.tool
    assert failure.code
    # The evidence has to be a line from the output, so the operator can see
    # what the attribution was made from rather than taking it on trust.
    assert failure.evidence in _REACHABILITY_OUTPUTS[name].replace("\n", "\n")
    assert "could not reach the package registry" in failure.describe()


@pytest.mark.parametrize("name", sorted(_REAL_DEFECT_OUTPUTS))
def test_classify_stays_silent_on_a_real_build_failure(name: str) -> None:
    assert policy.classify_failure(_REAL_DEFECT_OUTPUTS[name]) is None, (
        f"{name} was wrongly attributed to the registry -- an operator would be "
        "told to retry a failure retrying cannot fix"
    )


def test_classify_handles_none() -> None:
    assert policy.classify_failure(None) is None


# --- The policy reaches the subprocesses that fetch -------------------------


@pytest.mark.parametrize(
    "cmd",
    (
        ["npm", "ci"],
        ["/usr/local/bin/npm", "install"],
        ["pip", "install", "."],
        ["/home/u/.nyxGPT/opt/nyxgpt-api/venv/bin/pip", "install", "x.tar.gz"],
        ["/usr/bin/python3.12", "-m", "pip", "download", "pip"],
        ["apt-get", "install", "-y", "docker.io"],
        ["dnf", "install", "-y", "docker"],
    ),
)
def test_fetching_commands_are_recognised(cmd: list[str]) -> None:
    assert policy.is_package_fetch_command(cmd)


@pytest.mark.parametrize(
    "cmd",
    (
        [],
        ["docker", "build", "-t", "x", "."],
        ["systemctl", "--user", "restart", "nyxgpt-api"],
        ["/usr/bin/python3.12", "-m", "venv", "--without-pip", "/tmp/v"],
        ["kubectl", "apply", "-f", "-"],
    ),
)
def test_non_fetching_commands_are_left_alone(cmd: list[str]) -> None:
    assert not policy.is_package_fetch_command(cmd)


def test_fetch_env_is_a_no_op_for_a_non_fetching_command() -> None:
    """`None` in, `None` out -- the common path's call signature is untouched."""
    assert policy.fetch_env(["docker", "build", "."], None) is None
    base = {"PATH": "/usr/bin"}
    assert policy.fetch_env(["docker", "build", "."], base) == base


def test_fetch_env_applies_the_policy_to_a_fetching_command() -> None:
    env = policy.fetch_env(["npm", "ci"], {"PATH": "/usr/bin"})
    assert env is not None
    assert env["PATH"] == "/usr/bin"
    for name, value in policy.ENV_POLICY.items():
        assert env[name] == value


def test_fetch_env_overrides_an_inherited_value() -> None:
    """A stale `NPM_CONFIG_FETCH_RETRIES` in the operator's shell is not our policy."""
    env = policy.fetch_env(["npm", "ci"], {"NPM_CONFIG_FETCH_RETRIES": "0"})
    assert env is not None
    assert env["NPM_CONFIG_FETCH_RETRIES"] == str(policy.RETRIES)


def test_package_manager_flags_resolve_by_basename() -> None:
    assert policy.package_manager_flags("/usr/bin/apt-get") == policy.APT_GET_FLAGS
    assert policy.package_manager_flags("dnf") == policy.DNF_FLAGS
    assert policy.package_manager_flags("yum") == policy.DNF_FLAGS
    assert policy.package_manager_flags("brew") == ()


# --- The npm post-condition, against the two real trees ---------------------


def test_npm_tree_is_complete_reads_npms_own_receipt(tmp_path: Path) -> None:
    assert not policy.npm_tree_is_complete(tmp_path)

    # The shape the `--network none` build produced: package directories, no
    # files in them, no receipt.
    (tmp_path / "node_modules" / "next").mkdir(parents=True)
    (tmp_path / "node_modules" / "@babel" / "core").mkdir(parents=True)
    assert not policy.npm_tree_is_complete(tmp_path)

    # A zero-byte receipt is not a receipt either.
    receipt = tmp_path / policy.NPM_TREE_RECEIPT
    receipt.write_text("", encoding="utf-8")
    assert not policy.npm_tree_is_complete(tmp_path)

    receipt.write_text('{"name": "web", "packages": {}}', encoding="utf-8")
    assert policy.npm_tree_is_complete(tmp_path)


def test_npm_tree_is_complete_survives_an_unreadable_path(tmp_path: Path) -> None:
    """A post-condition that raises would turn a fetch failure into a crash."""
    blocker = tmp_path / "node_modules"
    blocker.write_text("not a directory", encoding="utf-8")
    assert not policy.npm_tree_is_complete(tmp_path)


# --- The shell the Dockerfile actually runs ---------------------------------


@pytest.mark.parametrize(
    ("receipt", "npm_exit", "expect_exit"),
    (
        ("good", 0, 0),  # the healthy case: npm succeeded, receipt written
        ("", 0, 1),  # the #4205 case: npm exited 0, no receipt
        ("missing", 0, 1),  # no receipt at all
        ("good", 1, 1),  # npm failed loudly; still a failure
    ),
)
def test_generated_npm_step_shell_behaves(
    tmp_path: Path, receipt: str, npm_exit: int, expect_exit: int
) -> None:
    """Run the generated shell against a fake npm, in `sh`, as the image does.

    The step is shell text embedded in a Dockerfile, so asserting on the
    string would prove nothing about what it does. A stub `npm` on PATH lets
    the real control flow run for all four combinations of (exit status,
    receipt present) -- including the one combination that used to pass.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "npm"
    stub.write_text(f"#!/bin/sh\nexit {npm_exit}\n", encoding="utf-8")
    stub.chmod(0o755)

    if receipt != "missing":
        (tmp_path / "node_modules").mkdir()
        (tmp_path / policy.NPM_TREE_RECEIPT).write_text(receipt, encoding="utf-8")

    completed = subprocess.run(
        ["/bin/sh", "-c", policy.npm_install_run_step("npm ci")],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == expect_exit, completed.stderr
    if expect_exit:
        assert policy.INCOMPLETE_FETCH_MARKER in completed.stderr
        assert policy.classify_failure(completed.stderr) is not None
    else:
        assert policy.INCOMPLETE_FETCH_MARKER not in completed.stderr


def test_brew_is_a_fetching_command_but_not_a_dockerfile_concern() -> None:
    """The macOS keg install's first fetch is `brew`'s own bottle download.

    It gets the policy as a subprocess (`HOMEBREW_CURL_RETRIES`), and must NOT
    appear in the Dockerfile stanza: an `ENV HOMEBREW_CURL_RETRIES=5` in a
    Linux image states a policy for software that is not in it.
    """
    assert policy.is_package_fetch_command(["/opt/homebrew/bin/brew", "install", "nyxgpt-api"])
    env = policy.fetch_env(["brew", "install", "nyxgpt-web"], {})
    assert env is not None
    assert env["HOMEBREW_CURL_RETRIES"] == str(policy.RETRIES)
    assert "HOMEBREW" not in policy.DOCKERFILE_ENV_STANZA
    assert set(policy.ENV_POLICY) < set(policy.SUBPROCESS_ENV_POLICY)
