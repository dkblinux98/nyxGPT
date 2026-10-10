"""The one place nyxGPT says which Node.js it runs (#4194).

Before this module there was no declaration at all, so every surface picked
its own: seven `setup-node` steps and `web/Dockerfile` said ``20``, the
retro-dashboard smoke said ``22``, the Linux/EC2 bootstraps pulled NodeSource
``setup_20.x``, and macOS got whatever Homebrew's unversioned ``node`` happened
to be that week (26.11 on the owner's machine) because the keg depended on
``node`` and the web service just took the first ``node`` on ``PATH``. Two
consequences, both of which had already happened: CI could not break on the
Node macOS users actually run, and the Node CI *did* test went end-of-life in
April 2026 with nothing to notice.

**The rule this module exists to hold:** a runtime version the product depends
on is declared once, and every place that installs, tests or runs that runtime
takes it from the declaration. No surface picks its own, and no surface leaves
it to whatever is on ``PATH``.

`NODE_MAJOR` is that declaration. It is a Python constant rather than a read of
`.nvmrc` because this module has to answer on an **artifact** install, where
there is no checkout to read -- the repo-less portability requirement. The
checked-in copies that non-Python tooling needs (`.nvmrc`, both `package.json`
``engines``, `web/Dockerfile`, the NodeSource bootstraps, the Homebrew
formulas, every workflow's `node-version-file`) are held equal to it by
`tests/unit/test_node_version_declaration.py`, which fails the build on any
divergence. That guard is the mechanism: copies that cannot drift are one
declaration with several spellings, and a copy with no guard is how this
defect happened in the first place.

**Why 24.** Node 24 is the Active LTS line (24.x entered LTS in October 2025;
security support runs to April 2028). 20 is end-of-life, and 22 is in
maintenance. The declaration is a *major*, written as the range ``24.x`` --
``>=24 <25`` -- on purpose: ``>=24`` would let a ``brew upgrade node`` walk the
install onto Node 26 silently, which is the exact failure mode this module
removes. Supported lines move; when this one does, change `NODE_MAJOR` and the
guard test will name every file that has to move with it.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass

# --- The declaration ----------------------------------------------------

#: The Node.js major nyxGPT installs, tests and runs. The one source.
NODE_MAJOR = 24

#: The version file the Node tooling reads (`nvm use`, `actions/setup-node`'s
#: `node-version-file`). Repo-root-relative; holds `NODE_MAJOR` and nothing else.
NODE_VERSION_FILE = ".nvmrc"

#: `engines.node` in both `package.json` files. The range is a single major by
#: design -- see the module docstring on why not `>=`.
NODE_ENGINES_RANGE = f"{NODE_MAJOR}.x"

#: `FROM node:<tag>` for every stage of `web/Dockerfile`.
NODE_DOCKER_IMAGE_TAG = f"{NODE_MAJOR}-alpine"

#: The Homebrew formula the macOS kegs depend on. Versioned, not the
#: unversioned `node`: an unversioned dependency is satisfied by whatever major
#: homebrew-core has moved to, which is how macOS ended up on 26 while CI was
#: on 20.
HOMEBREW_NODE_FORMULA = f"node@{NODE_MAJOR}"

#: The NodeSource setup script path the Linux/EC2 bootstraps curl.
NODESOURCE_SETUP_SCRIPT = f"setup_{NODE_MAJOR}.x"

#: Set to an absolute `node` path to override resolution. Exists for the
#: wrong-major fault injection in the smoke jobs (and for an operator with a
#: Node the normal search cannot find); it is read first and trusted as given.
NODE_BIN_ENV_VAR = "NYXGPT_NODE_BIN"

#: The wrapped command that reconciles this host's Node toolchain. Named by
#: `ops doctor`/`ops status` and by the Infrastructure page beside any
#: mismatch, so neither surface reports a problem with no remedy.
NODE_REMEDIATION_COMMAND = "nyxgpt ops node"


# --- Resolving the declared Node on this host ---------------------------

_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def _homebrew_prefixes() -> list[str]:
    """Homebrew prefixes to look for the versioned keg under, most likely first.

    `HOMEBREW_PREFIX` when the caller's environment carries it (a `brew`
    session, or an unusual install root), then the two standard ones: Apple
    Silicon's `/opt/homebrew` and Intel's `/usr/local`.
    """
    prefixes = []
    env_prefix = os.environ.get("HOMEBREW_PREFIX", "").strip()
    if env_prefix:
        prefixes.append(env_prefix)
    for standard in ("/opt/homebrew", "/usr/local"):
        if standard not in prefixes:
            prefixes.append(standard)
    return prefixes


def homebrew_node_bin_dirs() -> list[str]:
    """`bin` directories of the versioned Homebrew keg, in search order.

    `opt/node@NN/bin` rather than `bin`: `node@NN` is keg-only, so Homebrew
    deliberately does NOT link it into `$(brew --prefix)/bin`. That is the
    property being relied on -- the declared Node is reachable by name here and
    nowhere else, so `brew upgrade node` moving the unversioned formula to a
    new major cannot change what nyxGPT runs.
    """
    return [f"{prefix}/opt/{HOMEBREW_NODE_FORMULA}/bin" for prefix in _homebrew_prefixes()]


def resolve_node_bin() -> str:
    """Absolute path to the `node` nyxGPT should run, or "" if none was found.

    Order, and the reason for it:

    1. `NYXGPT_NODE_BIN` -- an explicit operator/test override.
    2. The versioned Homebrew keg (macOS). First, not last: `PATH` on a Mac
       almost always leads with `/opt/homebrew/bin`, where the *unversioned*
       `node` lives, so searching `PATH` first would re-introduce the defect on
       the one platform that had it.
    3. `PATH`. The Linux/EC2 answer, where NodeSource installs the declared
       major as the system `node`, and the dev-checkout answer when `nvm` has
       activated `.nvmrc`.

    Returns a path, not a version verdict: whether what was found IS the
    declared major is `node_version_report`'s job, and the two are kept apart
    so a wrong-major Node is reported as wrong rather than as missing.
    """
    override = os.environ.get(NODE_BIN_ENV_VAR, "").strip()
    if override:
        return override
    if platform.system() == "Darwin":
        for bin_dir in homebrew_node_bin_dirs():
            candidate = os.path.join(bin_dir, "node")
            if os.access(candidate, os.X_OK):
                return candidate
    return shutil.which("node") or ""


def resolve_npm_bin() -> str:
    """Absolute path to the `npm` that belongs to the declared Node, or "".

    Same keg, deliberately: an `npm` from one major driving a `node` from
    another is the mismatch this module removes, wearing a different name.
    """
    override = os.environ.get(NODE_BIN_ENV_VAR, "").strip()
    if override:
        sibling = os.path.join(os.path.dirname(override), "npm")
        if os.access(sibling, os.X_OK):
            return sibling
    if platform.system() == "Darwin":
        for bin_dir in homebrew_node_bin_dirs():
            candidate = os.path.join(bin_dir, "npm")
            if os.access(candidate, os.X_OK):
                return candidate
    return shutil.which("npm") or ""


def node_path_prepend() -> str:
    """`PATH` prefix that puts the declared Node ahead of anything else, or "".

    For the subprocesses that have to run `npm`/`npx` rather than `node`
    directly (`npm ci`, `npm run build`): those re-exec `node` from their own
    `PATH`, so passing an absolute `npm` is not enough on its own.
    """
    node_bin = resolve_node_bin()
    return os.path.dirname(node_bin) if node_bin else ""


def node_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """`base` (default `os.environ`) with the declared Node first on `PATH`."""
    env = dict(os.environ if base is None else base)
    prefix = node_path_prepend()
    if prefix:
        env["PATH"] = prefix + os.pathsep + env.get("PATH", "")
    return env


def parse_node_major(version_text: str) -> int | None:
    """The major in `node -v`-style output (`v24.21.0`), or None if unreadable."""
    match = _VERSION_RE.search(version_text or "")
    return int(match.group(1)) if match else None


# --- Reporting it --------------------------------------------------------

#: `NodeRuntimeReport.state` values.
NODE_OK = "ok"
NODE_MISSING = "missing"
NODE_MISMATCH = "mismatch"
NODE_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class NodeRuntimeReport:
    """What Node this host actually runs, against what nyxGPT declares.

    One report, consumed by `ops install`, `ops doctor`, `ops status`, `ops
    node` and the Infrastructure page's install card, so no two of them can
    disagree about one machine. The four states are kept distinct on purpose:
    "no `node` at all", "a `node` whose version could not be read" and "a
    `node` of the wrong major" have different causes and different remedies,
    and the checks this replaces collapsed all three into `node is None` --
    which is why a Node 26 install reported "Node OK".
    """

    state: str
    path: str
    version: str
    major: int | None
    declared_major: int
    detail: str
    remediation: str

    @property
    def ok(self) -> bool:
        return self.state == NODE_OK

    def to_dict(self) -> dict[str, object]:
        """JSON for `infra_status` -> the Infrastructure page."""
        return {
            "state": self.state,
            "ok": self.ok,
            "path": self.path,
            "version": self.version,
            "major": self.major,
            "declared_major": self.declared_major,
            "detail": self.detail,
            "remediation": self.remediation,
        }

    def summary(self) -> str:
        """One line for `ops status` / `ops doctor`."""
        return self.detail


def node_version_report(*, timeout: float = 15.0) -> NodeRuntimeReport:
    """Run the resolved `node -v` and classify it against `NODE_MAJOR`.

    A *run*, not an existence check: `shutil.which("node") is not None` is what
    every surface used to ask, and it is true of every wrong major as well as
    every right one. The version has to come out of the binary that would
    actually serve the web tier.
    """
    path = resolve_node_bin()
    if not path:
        return NodeRuntimeReport(
            state=NODE_MISSING,
            path="",
            version="",
            major=None,
            declared_major=NODE_MAJOR,
            detail=f"Node.js not found; nyxGPT requires Node {NODE_MAJOR}",
            remediation=NODE_REMEDIATION_COMMAND,
        )
    try:
        cp = subprocess.run(  # noqa: S603 - fixed argv, resolved path
            [path, "-v"], text=True, capture_output=True, timeout=timeout
        )
    except Exception as exc:  # pragma: no cover - defensive
        return NodeRuntimeReport(
            state=NODE_UNREADABLE,
            path=path,
            version="",
            major=None,
            declared_major=NODE_MAJOR,
            detail=f"Could not run {path} -v ({type(exc).__name__}: {exc})",
            remediation=NODE_REMEDIATION_COMMAND,
        )
    version_text = (cp.stdout or cp.stderr or "").strip()
    major = parse_node_major(version_text) if cp.returncode == 0 else None
    if major is None:
        return NodeRuntimeReport(
            state=NODE_UNREADABLE,
            path=path,
            version=version_text,
            major=None,
            declared_major=NODE_MAJOR,
            detail=(
                f"{path} -v did not report a readable version "
                f"(exit {cp.returncode}: {version_text[:120] or 'no output'})"
            ),
            remediation=NODE_REMEDIATION_COMMAND,
        )
    if major != NODE_MAJOR:
        return NodeRuntimeReport(
            state=NODE_MISMATCH,
            path=path,
            version=version_text,
            major=major,
            declared_major=NODE_MAJOR,
            detail=(
                f"Node {version_text} at {path} is not the Node {NODE_MAJOR} nyxGPT "
                f"declares (.nvmrc / package.json engines)"
            ),
            remediation=NODE_REMEDIATION_COMMAND,
        )
    return NodeRuntimeReport(
        state=NODE_OK,
        path=path,
        version=version_text,
        major=major,
        declared_major=NODE_MAJOR,
        detail=f"Node {version_text} at {path} (declared major {NODE_MAJOR})",
        remediation="",
    )


# --- Provisioning it -----------------------------------------------------


def nodesource_setup_url(package_manager: str) -> str:
    """The NodeSource setup URL for `dnf` (rpm) or `apt` (deb) hosts.

    Shared by `cloud_deploy`'s bootstrap renderer and the EC2 user-data
    template's placeholder so the two cannot name different majors -- they did,
    independently, for as long as both had the literal `setup_20.x` in them.
    """
    host = "rpm" if package_manager == "dnf" else "deb"
    return f"https://{host}.nodesource.com/{NODESOURCE_SETUP_SCRIPT}"
