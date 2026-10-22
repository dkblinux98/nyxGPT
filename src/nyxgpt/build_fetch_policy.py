"""The one fetch policy every nyxGPT build step that reaches a package index runs under.

#4205. Before this module there was no policy at all: `web/Dockerfile`'s step
was a bare ``RUN npm ci``, the api image ran a bare ``apt-get install`` and
``pip install``, and the native/Homebrew installs shelled out to ``npm`` with
nothing said about what a registry blip should do. Each build step therefore
answered "what happens when the registry is unreachable?" by itself, which
means nothing answered it.

What the reproduction actually showed
-------------------------------------
Captured deliberately on a Linux runner, 2026-10-21 -- ``docker build
--network none --target deps web/``, DNS confirmed dead (a ``dns.lookup`` in
the same sandbox answers ``EAI_AGAIN``). The capture is now a job rather than
a recipe: ``scripts/image-build-fetch-policy-smoke.sh``, run by
``.github/workflows/image-build-fetch-policy-smoke.yml``, injects the same
condition on every change to this module or the Dockerfiles it governs::

    #8 [deps 4/4] RUN npm ci
    #8 141.1 npm error Exit handler never called!
    #8 141.1 npm error This is an error with npm itself. Please report ...
    #8 DONE 141.2s
    #9 exporting to image ... DONE

``npm ci`` **exited 0** with the registry unreachable, so the step was
reported as DONE and the layer was exported. Inspecting it: 520 top-level
entries under ``node_modules``, ``find node_modules -type f`` counts **zero
files** -- every package directory is empty -- ``node_modules/.bin`` and
``node_modules/.package-lock.json`` are both absent, and ``npm ls --depth=0``
calls every dependency extraneous. The same build with the network up writes
a 419 KB ``node_modules/.package-lock.json`` and ``npm ls --depth=0`` exits 0.

So the defect class is wider than "there is no retry". The step does not check
its own result, and npm's exit status is not evidence that the fetch happened:
a blip yields a *green* build over an empty dependency tree, and BuildKit
caches that layer for every later build on the machine. A loud transient
failure costs a CI run; a silent one ships an image whose web service cannot
start, and says nothing.

The policy, in three parts
--------------------------
1. **Retry in the tool, not around it.** The fetching tool knows which errors
   are retryable; a wrapper that re-runs the whole build does not. Expressed
   as `ENV_POLICY` (env vars npm and pip read on their own) plus the inline
   flags for the tools that have no env knob (`APT_GET_FLAGS`, `DNF_FLAGS`,
   `CURL_FLAGS`). Declared as Dockerfile ``ENV`` rather than ``--build-arg``
   so *every* builder inherits it -- ops, `docker compose build`, the
   Terraform and Kubernetes paths, and a CI job running `docker build` by
   hand -- instead of only the callers that remember to pass it.
2. **Never trust exit 0 from a fetch that can lie.** A step whose tool is
   known to exit 0 on an incomplete fetch asserts its own post-condition and
   prints `INCOMPLETE_FETCH_MARKER` when it is not met. `npm ci`/`npm install`
   are the known case, proven above; `NPM_TREE_COMPLETE_TEST` is the check,
   and it is ``test -s node_modules/.package-lock.json`` because that file is
   npm's own receipt for a completed reify and is exactly what the broken
   tree lacked.
3. **Say which failure it was.** `classify_failure` is the single place that
   decides "could the build reach the registry?", so the operator is told the
   registry was unreachable rather than that nyxGPT is broken, and so no two
   surfaces can disagree about one failure.

Deliberately conservative: a failure this module cannot positively attribute
to reachability is reported as an ordinary build failure. Claiming "transient,
just retry" about a genuine defect sends the operator round a loop that cannot
terminate, which is worse than the unhelpful-but-honest message they get now.
``E404`` (no such package), ``EINTEGRITY`` (the registry served content the
lockfile does not describe) and ``ERESOLVE`` (dependency conflict) are
therefore *not* reachability failures and are not matched here.

`tests/unit/test_build_fetch_policy.py` is the guard: it fails the build when
a Dockerfile gains a remote-fetch step that does not carry this policy, when a
Dockerfile's policy stanza drifts from the text below, or when an `ops` fetch
site is added without it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# --- The numbers -------------------------------------------------------------
#
# npm's own defaults are `fetch-retries=2`, `fetch-retry-mintimeout=10000`,
# `fetch-retry-maxtimeout=60000`, `fetch-timeout=300000`; pip's are
# `retries=5`, `timeout=15`; Debian's `Acquire::Retries` defaults to 0, so
# apt does not retry at all. Five attempts with a 2s..60s exponential backoff
# is ~62s of waiting in the worst case -- long enough to outlast the kind of
# blip that fails one request and short enough that a genuinely unreachable
# registry still reports inside the existing job timeouts (the no-network
# reproduction above took 141s with npm's *default* two retries).
RETRIES = 5
RETRY_MIN_TIMEOUT_MS = 2_000
RETRY_MAX_TIMEOUT_MS = 60_000
# Per-request ceiling. npm's 300000 default is already generous; kept rather
# than raised, because a request that has made no progress for five minutes is
# not a blip and more waiting does not fix it.
FETCH_TIMEOUT_MS = 300_000
# Connect/read timeout for the tools that measure it in seconds (pip, curl,
# dnf). Above the few seconds a healthy index takes, below the point where a
# dead host wedges a job.
TIMEOUT_SECONDS = 30


# --- How each tool expresses the policy --------------------------------------

#: Env vars npm reads as config (`NPM_CONFIG_<key>`), so a plain `npm ci`
#: inherits the policy with no flags at the call site.
NPM_ENV = {
    "NPM_CONFIG_FETCH_RETRIES": str(RETRIES),
    "NPM_CONFIG_FETCH_RETRY_MINTIMEOUT": str(RETRY_MIN_TIMEOUT_MS),
    "NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT": str(RETRY_MAX_TIMEOUT_MS),
    "NPM_CONFIG_FETCH_TIMEOUT": str(FETCH_TIMEOUT_MS),
}

#: pip reads every option from `PIP_<OPTION>` too.
PIP_ENV = {
    "PIP_RETRIES": str(RETRIES),
    "PIP_TIMEOUT": str(TIMEOUT_SECONDS),
}

#: The whole policy as environment, for a build step or a subprocess.
ENV_POLICY: dict[str, str] = {**NPM_ENV, **PIP_ENV}

#: apt has no env knob for retries -- `APT_CONFIG` names a config *file*, not
#: a value -- so the policy is an inline `-o` on each apt-get invocation.
APT_GET_FLAGS = ("-o", f"Acquire::Retries={RETRIES}")

#: dnf likewise: `--setopt` on each invocation.
DNF_FLAGS = (
    f"--setopt=retries={RETRIES}",
    f"--setopt=timeout={TIMEOUT_SECONDS}",
)

#: The policy as flags, keyed by the package manager that needs them inline.
#: `yum` on every distro nyxGPT targets is dnf (AL2023 symlinks it) and yum-3
#: accepts `--setopt` too, so one entry covers both.
PACKAGE_MANAGER_FLAGS: dict[str, tuple[str, ...]] = {
    "apt": APT_GET_FLAGS,
    "apt-get": APT_GET_FLAGS,
    "dnf": DNF_FLAGS,
    "yum": DNF_FLAGS,
}


def package_manager_flags(tool: str) -> tuple[str, ...]:
    """The policy flags for `tool`, or `()` if it needs none / has none.

    `tool` may be a bare name or an absolute path -- callers resolve package
    managers through `shutil.which`, and the policy is a property of the tool,
    not of where it lives.
    """
    return PACKAGE_MANAGER_FLAGS.get(Path(tool).name, ())


#: curl retries only when asked, and by default treats a refused connection as
#: final -- `--retry-connrefused` is what makes "the registry is restarting"
#: retryable rather than fatal.
CURL_FLAGS = (
    "--retry",
    str(RETRIES),
    "--retry-connrefused",
    "--retry-delay",
    "2",
    "--connect-timeout",
    str(TIMEOUT_SECONDS),
)


# --- Part 2: the post-condition for a fetch that can exit 0 while failing ----

#: Emitted by a fetch step whose post-condition failed. `classify_failure`
#: treats it as definitive: nothing else prints it, and the step that does
#: has already established that its fetch did not complete.
INCOMPLETE_FETCH_MARKER = "nyxgpt-build: remote fetch did not complete"

#: npm writes `node_modules/.package-lock.json` only once a reify completes.
#: The no-network reproduction has 520 empty package directories and no such
#: file; a healthy `npm ci` of the same tree writes 419 KB of it.
NPM_TREE_RECEIPT = "node_modules/.package-lock.json"

#: The same check as shell, for a Dockerfile `RUN`. `-s` (exists and
#: non-empty) rather than `-f` so a zero-byte stub cannot pass either.
NPM_TREE_COMPLETE_TEST = f"test -s {NPM_TREE_RECEIPT}"


def npm_tree_is_complete(project_dir: Path) -> bool:
    """Did an `npm ci`/`npm install` in `project_dir` actually install the tree?

    The Python half of `NPM_TREE_COMPLETE_TEST`, from the same constant, for
    the native and Homebrew install paths -- they run the same `npm ci` as the
    image build and inherit the same lie about its exit status (#4205).
    """
    receipt = project_dir / NPM_TREE_RECEIPT
    try:
        return receipt.is_file() and receipt.stat().st_size > 0
    except OSError:
        return False


def incomplete_fetch_message(step: str, returncode: int) -> str:
    """The message a failed post-condition reports, from the one marker.

    Matches what `npm_install_run_step`'s shell prints, so a native failure
    and an in-image failure are classified identically by `classify_failure`
    and read identically to the operator.
    """
    return (
        f"{INCOMPLETE_FETCH_MARKER}: '{step}' left an incomplete node_modules "
        f"(exit {returncode}) -- the package registry was most likely unreachable"
    )


def npm_install_run_step(npm_command: str) -> str:
    """The shell for an `npm ci`/`npm install` build step, policy included.

    `npm_command` is the npm invocation as it would be written on its own
    (e.g. ``npm ci``). The returned shell runs it, then refuses to let exit 0
    stand on its own: an incomplete tree fails the step and prints
    `INCOMPLETE_FETCH_MARKER`, so the build goes red where it used to go green
    and `classify_failure` can name the cause.

    One function rather than hand-written shell per Dockerfile: the text is
    identical everywhere and
    `tests/unit/test_build_fetch_policy.py::test_dockerfile_npm_steps_carry_the_completion_check`
    fails the build when a Dockerfile's copy drifts from it.
    """
    return (
        f"{npm_command}; status=$?; "
        f"if [ $status -ne 0 ] || ! {NPM_TREE_COMPLETE_TEST}; then "
        f"echo \"{INCOMPLETE_FETCH_MARKER}: '{npm_command}' left an incomplete "
        "node_modules (exit $status) -- the package registry was most likely "
        'unreachable" >&2; exit 1; fi'
    )


#: The `ENV` block a Dockerfile with a remote-fetch step must carry, verbatim.
#: Docker has no include mechanism, so "one place" is this string plus a guard
#: that fails the build when a copy of it drifts -- not N independently
#: maintained stanzas.
DOCKERFILE_ENV_STANZA = "\n".join(
    [
        "# --- nyxGPT remote-fetch policy (#4205). Generated from",
        "# src/nyxgpt/build_fetch_policy.py, which states the reasoning;",
        "# tests/unit/test_build_fetch_policy.py fails the build if these drift or",
        "# if a new fetching step appears without them. Do not edit by hand.",
        *(f"ENV {name}={value}" for name, value in ENV_POLICY.items()),
        "# --- end nyxGPT remote-fetch policy",
    ]
)


# --- Part 3: the one place that decides "was this a reachability failure?" ---


@dataclass(frozen=True)
class FetchFailure:
    """A build/install failure attributed to an unreachable package index."""

    tool: str
    """Which fetcher reported it ("npm", "pip", "apt", "dnf", "docker", or
    "nyxgpt" for our own post-condition marker)."""

    code: str
    """The named error code where the tool printed one (``EAI_AGAIN``,
    ``ECONNRESET``, ...), or a short label where it did not."""

    evidence: str
    """The matched output line, so the operator sees what the attribution was
    made from rather than having to take it on trust."""

    def describe(self) -> str:
        """One line naming the failure, for an `OpsResult`'s detail."""
        return (
            f"{self.tool} could not reach the package registry " f"({self.code}): {self.evidence}"
        )


#: What a reachability failure looks like, per tool. Ordered: our own marker
#: first because it is definitive, then the tools' own named codes, then their
#: prose. Each pattern's group 1, when present, is the error code.
_REACHABILITY_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "nyxgpt",
        "incomplete-fetch",
        re.compile(re.escape(INCOMPLETE_FETCH_MARKER), re.IGNORECASE),
    ),
    (
        "npm",
        "",
        re.compile(
            r"npm (?:ERR!|error)\s+(?:code\s+)?"
            r"(ENOTFOUND|EAI_AGAIN|ECONNRESET|ECONNREFUSED|ECONNABORTED"
            r"|ETIMEDOUT|ENETUNREACH|EHOSTUNREACH|EPROTO|ERR_SOCKET_TIMEOUT"
            r"|ERR_SSL_WRONG_VERSION_NUMBER)\b"
        ),
    ),
    ("npm", "network", re.compile(r"npm (?:ERR!|error)\s+network\b")),
    (
        "npm",
        "registry-request-failed",
        re.compile(r"request to https?://\S+ failed, reason:"),
    ),
    (
        "npm",
        "exit-handler-never-called",
        # npm's own internal give-up after a failed fetch -- the line the
        # no-network reproduction printed, and the one that accompanies exit 0
        # over an empty tree. Matched so a build that *is* red for this reason
        # (the builder stage's `npm run build`, say) is still attributed.
        re.compile(r"npm (?:ERR!|error)\s+Exit handler never called!"),
    ),
    (
        "pip",
        "",
        re.compile(
            r"(?:Could not fetch URL|Temporary failure in name resolution"
            r"|(ReadTimeoutError|NewConnectionError|ConnectTimeoutError"
            r"|ProxyError|SSLError|MaxRetryError)"
            r"|Connection broken|Retrying \(Retry\(total=)"
        ),
    ),
    (
        "apt",
        "",
        re.compile(
            r"(?:Temporary failure resolving|Could not resolve ['\"]"
            r"|Failed to fetch\b|Could not connect to\b"
            r"|Unable to connect to\b|Connection failed\b)"
        ),
    ),
    (
        "dnf",
        "",
        re.compile(
            r"(?:Curl error|Could not resolve host|Cannot download"
            r"|Failed to download metadata|Errors during downloading metadata)"
        ),
    ),
    (
        "docker",
        "",
        re.compile(
            r"(?:failed to do request|TLS handshake timeout"
            r"|dial tcp\b.*(?:i/o timeout|connection refused|no such host)"
            r"|net/http: request canceled while waiting for connection)",
            re.IGNORECASE,
        ),
    ),
)


def classify_failure(output: str | None) -> FetchFailure | None:
    """Was this failure the package index being unreachable? The one answer.

    `output` is the **full** combined stdout+stderr of the failed step, not a
    bounded excerpt: `ops._bounded_output`'s salient-line rescue anchors on
    lines that *start* with ``error``/``failed``, and BuildKit prefixes every
    line of a `docker build` with ``#8 23.97 ``, so classifying an excerpt
    would miss exactly the lines that name the cause (#4192's reporting fix
    carries them out of the elision for native tools; it cannot for a build).

    Returns the attribution, or None when the output gives no positive
    evidence of a reachability failure -- in which case the caller reports an
    ordinary build failure. See the module docstring for why that default is
    the safe one.
    """
    if not output:
        return None
    for line in output.splitlines():
        if _is_echoed_command(line):
            continue
        for tool, label, pattern in _REACHABILITY_PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            code = label
            groups = [g for g in match.groups() if g]
            if groups:
                code = groups[0]
            return FetchFailure(tool=tool, code=code or "unnamed", evidence=line.strip())
    return None


def _is_echoed_command(line: str) -> bool:
    """Is this line a build step's *command*, rather than its output?

    BuildKit prints each `RUN` instruction before running it, and then again
    in the failure summary -- so a step whose shell contains
    `echo "<marker>"` has the marker on three lines, only one of which is the
    message actually printed. Matching the instruction echo attributes the
    failure correctly but quotes the whole Dockerfile line as its "evidence",
    which is noise where the point of carrying evidence is that an operator
    can read it. Skip the echoes and keep the output.

    Narrow on purpose: `echo "` immediately around a marker is a command, and
    nothing else here is guessed at.
    """
    position = line.find(INCOMPLETE_FETCH_MARKER)
    return position > 0 and 'echo "' in line[:position]


#: What to tell the operator once a failure is attributed to reachability.
#: One string so the CLI, the dashboard's status feed and the logs cannot
#: offer three different remedies for one cause.
REMEDIATION = (
    "This is a package-registry reachability failure, not a nyxGPT defect: the "
    f"build retried {RETRIES} times with backoff (2s-60s) and still could not "
    "fetch. Check network/proxy/VPN reachability and the index's own status, "
    "then retry with `nyxgpt ops install`. `nyxgpt ops doctor` reports the "
    "local prerequisites; nothing needs to be cleaned up first -- a failed "
    "fetch records no fingerprint, so the next install rebuilds from scratch."
)


def subprocess_env(base: dict[str, str]) -> dict[str, str]:
    """`base` with the policy applied, for a subprocess that fetches.

    Takes the base environment explicitly rather than reading `os.environ`
    here, so a caller can see what it is handing on and a test can build one.
    The policy wins over an inherited value: a stale `NPM_CONFIG_FETCH_RETRIES`
    in the operator's shell is not a decision this project made.
    """
    merged = dict(base)
    merged.update(ENV_POLICY)
    return merged


#: Programs that fetch from a remote package index when run. Matched on the
#: argv[0] *basename*, because callers resolve these through `shutil.which`
#: and a venv's `pip` is `<venv>/bin/pip`.
FETCH_PROGRAMS = frozenset({"apt", "apt-get", "dnf", "npm", "npx", "pip", "pip3", "uv", "yum"})


def is_package_fetch_command(cmd: Sequence[str]) -> bool:
    """Does running `cmd` fetch from a remote package index?

    Covers the `python -m pip ...` form too: `ops` installs into service venvs
    through both the venv's `pip` script and `sys.executable -m pip`, and a
    policy that only recognised one of them would be a policy with a hole in
    it.
    """
    if not cmd:
        return False
    if Path(cmd[0]).name in FETCH_PROGRAMS:
        return True
    return len(cmd) >= 3 and cmd[1] == "-m" and cmd[2] in {"pip", "uv"}


def fetch_env(cmd: Sequence[str], env: dict[str, str] | None) -> dict[str, str] | None:
    """`env` with the policy applied when `cmd` fetches, unchanged when it does not.

    Shaped like `subprocess_bounds.kubectl_env` and used the same way -- from
    inside `ops._run`, so every fetching subprocess in `ops` runs under the
    policy whether or not its call site remembered to ask. A call-site opt-in
    is a policy each new site can forget; this one it cannot (#4205).

    `None` in, `None` out for a non-fetching command, so the common path's
    call signature is untouched and the subprocess still inherits this
    process's environment.
    """
    if not is_package_fetch_command(cmd):
        return env
    return subprocess_env(dict(os.environ if env is None else env))
