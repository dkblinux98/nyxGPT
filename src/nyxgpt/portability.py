"""The repo-less portability matrix and the capstone acceptance sequence (P6-16, #3516).

CLAUDE.md's Repo-less Portability requirement (2026-08-01) names five
deployment targets that must each be installable and operable **without a
repo checkout** -- macOS native, Linux native (systemd), Docker/Compose,
Kubernetes, AWS EC2 (Windows explicitly out of scope) -- and the Phase 6
capstone accepts the whole phase by demonstrating that matrix.

The matrix lives here, as data, rather than only in prose, because a
hand-written table is exactly the artifact that goes quietly stale: it
claims a target installs from published artifacts long after someone
reintroduced a `git clone`, and nothing fails. So each row carries the
commands it claims, the evidence backing the claim, and the gaps that are
still open, and `check_matrix` asserts the invariants that can be checked
mechanically:

* **repo-less** -- no install/operate/teardown command in a row fetches
  source (`git clone`, a `git@` remote, a source-archive download). What a
  row installs is a published artifact: PyPI, the remote Homebrew tap, or a
  container image.
* **wrapped** -- no command is a raw `docker`/`docker compose`/`kubectl`/
  `terraform` invocation, per CLAUDE.md's Operational Command Wrapping
  requirement. Package managers (`pip`, `brew`) are how an artifact is
  installed in the first place and are not orchestrators, so they are
  allowed for the install step only.
* **evidenced** -- every path a row cites as evidence exists. This one is a
  dev-checkout-only check: an installed wheel has no `.github/` beside it,
  so `check_matrix` reports the evidence check as skipped rather than failed
  when there is no checkout (see `checkout_root`).

`gaps` is the deliberately honest part. A target that cannot be installed
without a checkout says so mechanically, rather than letting a matrix imply
five green rows -- `acceptance_ready` is false while any such gap is open,
which is the machine-readable form of "the portability AC is not met yet",
and `nyxgpt ops portability --strict` exits non-zero on it so CI can hold the
line once the gaps close. Compose is the remaining one: its `api`/`web`
services still come up from images built out of a checkout, even though those
images are published to ghcr.io on every release. Kubernetes closed its gap in
#3834 -- the manifests became package data and the images are built from the
published `nyxgpt-api`/`nyxgpt-web` tarballs.

The other half of the capstone is `ACCEPTANCE_SEQUENCE`: the exact command
sequence an owner runs on a clean machine to accept Phase 6, from
`pip install nyxgpt` through a live AWS deploy, the smoke test, and
teardown. It is data for the same reason the matrix is -- `docs/portability-
matrix.md` renders it from here, so the runbook cannot drift from what the
CLI actually accepts.

There is deliberately no dashboard surface for any of this (#3803). #3516
added one; the owner removed it, because a matrix describing the *product*
tells you nothing about the machine whose dashboard you are looking at.
`nyxgpt ops portability` is how the matrix is read.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Fetching source rather than installing a published artifact. Matched
# case-insensitively against every command in a row: the whole point of the
# repo-less requirement is that none of these can appear in a user-facing
# install or operate path.
_SOURCE_FETCH_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bgit\s+clone\b", "clones the repository"),
    (r"git@[\w.-]+:", "fetches over an SSH git remote"),
    (r"https?://\S+\.git\b", "fetches a git URL"),
    (r"/archive/refs/\S+\.(?:tar\.gz|zip)", "downloads a source archive"),
    (r"\bgit\s+(?:pull|fetch|checkout)\b", "operates on a working checkout"),
)

# Raw orchestrator invocations. CLAUDE.md: "No nyxGPT operation may require
# the user to run a raw docker, docker compose, docker-compose, kubectl, or
# terraform command directly." Internals may shell out to them; a matrix row
# is a user instruction, so it may not.
_RAW_ORCHESTRATORS: tuple[str, ...] = (
    "docker",
    "docker-compose",
    "kubectl",
    "terraform",
    "helm",
)

# Package managers a repo-less install legitimately starts from -- they are
# how a published artifact gets onto a clean machine, not orchestrators of a
# deployment. Anything else in an install command must be `nyxgpt`.
_INSTALL_TOOLS: tuple[str, ...] = ("pip", "pip3", "pipx", "python", "python3", "brew")


@dataclass(frozen=True)
class Target:
    """One row of the portability matrix.

    `status` is the row's verification level, and is deliberately more
    granular than pass/fail because the five targets are not verifiable the
    same way -- one runs end to end on a GitHub Actions runner, one needs a
    billed AWS account, one is only half coverable by a hosted runner:

    * ``ci-verified`` -- a workflow in this repo installs and operates the
      target from published artifacts on every release, with no checkout.
    * ``acceptance`` -- the path is implemented and CI covers what it can,
      but the final demonstration needs an account or a persistent machine
      no runner is (a real AWS account; a macOS workstation whose brew
      services and launchd agents survive the job). Owner acceptance closes
      it. Note this is *not* "CI has no Apple Silicon": hosted `macos-15`
      runners are Apple Silicon and `macos-brew-smoke.yml` uses them to
      verify the macOS *install* half (#3753).
    * ``gap`` -- the target cannot be installed without a checkout today.
      `gaps` says exactly what is missing.
    """

    key: str
    name: str
    artifact: str
    install: tuple[str, ...]
    operate: tuple[str, ...]
    teardown: str
    status: str
    evidence: tuple[str, ...]
    notes: str
    gaps: tuple[str, ...] = field(default=())

    @property
    def commands(self) -> tuple[str, ...]:
        """Every command this row claims, install through teardown."""
        return (*self.install, *self.operate, self.teardown)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form, as returned by the API and `--json`."""
        return {
            "key": self.key,
            "name": self.name,
            "artifact": self.artifact,
            "install": list(self.install),
            "operate": list(self.operate),
            "teardown": self.teardown,
            "status": self.status,
            "evidence": list(self.evidence),
            "notes": self.notes,
            "gaps": list(self.gaps),
        }


# The matrix itself. Ordered the way CLAUDE.md's requirement lists the
# targets, so the rendered table reads as the requirement does.
TARGETS: tuple[Target, ...] = (
    Target(
        key="macos-native",
        name="macOS native (Homebrew + launchd)",
        artifact="Remote Homebrew tap (dkblinux98/nyxgpt) + PyPI wheel",
        install=(
            "brew tap dkblinux98/nyxgpt",
            # Homebrew gates formulas from third-party taps: without this,
            # `brew install` stops instead of installing (#3752). One-time
            # per tap, per machine -- docs/homebrew.md#trusting-the-tap.
            "brew tap-trust dkblinux98/nyxgpt",
            "brew install nyxgpt-api nyxgpt-web",
        ),
        operate=("nyxgpt up", "nyxgpt ops status", "nyxgpt ops doctor"),
        teardown="nyxgpt down",
        status="acceptance",
        evidence=(
            ".github/workflows/release-artifacts.yml",
            ".github/workflows/macos-brew-smoke.yml",
            "docs/homebrew.md",
            "docs/ops.md",
        ),
        notes=(
            "Split coverage. The *install* half is CI-verified on a real macOS "
            "Homebrew: macos-brew-smoke.yml installs the formulas on a macos-15 "
            "runner -- the working tree's own recipe on every formula change, and "
            "the published candidate from the tap after every rc cut (#3753). The "
            "*operate* half (brew services / launchd reconciliation, nyxgpt up) "
            "stays owner-verified on the owner's workstation. Publishing is "
            "automated -- release-artifacts.yml stamps and pushes both formulas to "
            "the remote tap when HOMEBREW_TAP_REPO is configured, and uploads them "
            "as a workflow artifact otherwise. The tarballs they install from are "
            "published on a companion <version>-homebrew release (a published "
            "release is immutable and can never gain an asset), or served from "
            "the version's own release when it already carries them -- "
            "docs/homebrew.md#where-the-tarballs-are-published."
        ),
    ),
    Target(
        key="linux-native",
        name="Linux native (systemd --user)",
        artifact="PyPI wheel (nyxgpt)",
        install=("pip install nyxgpt",),
        operate=("nyxgpt up", "nyxgpt ops status", "nyxgpt ops doctor"),
        teardown="nyxgpt down",
        status="ci-verified",
        evidence=(
            ".github/workflows/release-artifacts.yml",
            ".github/workflows/linux-native-smoke.yml",
            "docs/systemd.md",
        ),
        notes=(
            "release-artifacts.yml's artifact-install-smoke job installs from the "
            "just-published PyPI wheel with no repo checkout anywhere in the job, "
            "brings the stack up, verifies the systemd --user units and endpoints, "
            "and tears it down again."
        ),
    ),
    Target(
        key="docker-compose",
        name="Docker / Compose",
        artifact="ghcr.io/dkblinux98/nyxgpt-api, ghcr.io/dkblinux98/nyxgpt-web",
        install=("pip install nyxgpt",),
        operate=("nyxgpt up", "nyxgpt ops observability", "nyxgpt ops status"),
        teardown="nyxgpt down",
        status="gap",
        evidence=(
            ".github/workflows/release-artifacts.yml",
            "docs/docker-compose.md",
            "src/nyxgpt/resources/docker-compose.yml",
        ),
        notes=(
            "Half repo-less already: the Compose file and its templates ship as "
            "package data (nyxgpt.resources, #3621) and `nyxgpt ops observability` "
            "brings the monitoring/logging/tracing/errors profiles up from public "
            "images without a checkout. The core services are the gap."
        ),
        gaps=(
            "docker-compose.yml's api/web services carry a `build:` context (`.` and "
            "`./web`), so Compose builds them from a checkout instead of pulling the "
            "published ghcr.io images -- see docs/docker-compose.md's container "
            "images section. The Compose path is the one that still needs a "
            "CHECKOUT to build: `nyxgpt ops install --terraform` builds the same "
            "two images with no checkout at all, from the published nyxgpt-api/"
            "nyxgpt-web source tarballs (#3835, #3985).",
        ),
    ),
    Target(
        key="kubernetes",
        name="Kubernetes",
        artifact="PyPI wheel (nyxgpt); nyxgpt-api / nyxgpt-web release tarballs",
        install=("pip install nyxgpt",),
        operate=(
            "nyxgpt ops install --kubernetes",
            "nyxgpt ops status",
            "nyxgpt ops port-forward",
            "nyxgpt ops port-forward --target observability",
        ),
        teardown="nyxgpt ops down --kubernetes",
        status="ci-verified",
        evidence=(
            "docs/kubernetes.md",
            # The checkout-free install itself, executed: a wheel install in a
            # directory with no repository anywhere, then the real bring-up
            # (#3834).
            ".github/workflows/k8s-artifact-smoke.yml",
            "scripts/k8s-artifact-smoke.sh",
            # The observability tier this mode deploys, executed on a real
            # kind cluster rather than inspected (#3787).
            ".github/workflows/k8s-observability-smoke.yml",
        ),
        notes=(
            "Fully wrapped (no raw kubectl in any user instruction, and the command "
            "provisions a kind cluster itself) and checkout-free since #3834: the "
            "manifests ship as package data (nyxgpt.resources.k8s, synced to "
            "~/.nyxGPT/k8s) and both images are built from the published "
            "nyxgpt-api/nyxgpt-web tarballs -- the same artifacts the Homebrew "
            "formulas install, which a release candidate publishes and a container "
            "image is not. `--dev` builds the working tree instead, records that it "
            "did, and is refused where there is no checkout."
        ),
    ),
    Target(
        key="aws-ec2",
        name="AWS EC2 (private access path)",
        artifact="PyPI wheel on the workstation; PyPI wheel or remote tap on the instance",
        install=("pip install nyxgpt", "nyxgpt cloud credentials-setup"),
        operate=("nyxgpt cloud deploy", "nyxgpt cloud tunnel", "nyxgpt cloud smoke"),
        teardown="nyxgpt cloud destroy --yes",
        status="acceptance",
        evidence=(
            ".github/workflows/release-artifacts.yml",
            ".github/workflows/terraform-aws-validate.yml",
            "src/nyxgpt/cloud_deploy.py",
            "src/nyxgpt/cloud_provision.py",
            "docs/cloud.md",
        ),
        notes=(
            "The instance installs a published release and never clones "
            "(cloud_deploy.render_provision_script / cloud_provision.render_user_data, "
            "both asserted clone-free by unit tests). release-artifacts.yml's "
            "ec2-linux-user-data-smoke job executes the rendered Linux bootstrap "
            "script itself on a fresh, never-logged-in account. What CI cannot do is "
            "spend money: terraform-aws-validate.yml runs with dummy credentials, so "
            "the live deploy/smoke/teardown against a real account is the owner "
            "acceptance run in ACCEPTANCE_SEQUENCE. EC2 Mac targets are "
            "documentation-verified only: GitHub Actions' macOS runners are hosted "
            "(fine for brew installs -- see macos-native -- but not an EC2 "
            "instance). `nyxgpt cloud deploy --os macos` allocates the Dedicated "
            "Host itself after a priced confirmation (#3995) and `cloud destroy` "
            "schedules its deferred release, but neither can be executed in CI: a "
            "host bills a 24h minimum and no job can make that clock pass."
        ),
    ),
)


# The clean-machine acceptance run: what an owner types, in order, on a
# machine that has never seen this repository. Rendered by the CLI and by
# docs/portability-matrix.md from this one definition.
#
# `cloud smoke --skip-deploy --keep` rather than a bare `cloud smoke`: the
# deploy that step verifies is the one the previous step just made, which is
# also the thing being accepted. A bare `smoke` would deploy a second,
# throwaway stack and prove nothing about the first.
ACCEPTANCE_SEQUENCE: tuple[dict[str, str], ...] = (
    {
        "step": "install",
        "command": "pip install nyxgpt",
        "expect": "`nyxgpt --version` prints the released version; no checkout exists",
    },
    {
        "step": "credentials",
        "command": "nyxgpt cloud credentials-setup",
        "expect": "AWS credentials collected and validated (P6-13, #3512)",
    },
    {
        "step": "deploy",
        "command": "nyxgpt cloud deploy",
        "expect": (
            "substrate applied, instance provisioned from published artifacts, "
            "observability profiles up, self-heal enabled, tunnel open, health 200, "
            "localhost URLs printed"
        ),
    },
    {
        "step": "reachability",
        "command": "nyxgpt cloud status",
        "expect": (
            "the SSH target, the public IP and SSH-only ingress from the owner CIDR are "
            "printed -- every app and observability URL is a localhost one through the tunnel"
        ),
    },
    {
        "step": "containers",
        "command": "nyxgpt cloud ops status",
        "expect": (
            "the instance's own `nyxgpt ops status` answers over the wrapped SSH path -- "
            "container state without a hand-rolled ssh or a raw docker compose"
        ),
    },
    {
        "step": "verify",
        "command": "nyxgpt cloud smoke --skip-deploy --keep",
        "expect": "chat round-trip, RAG ingest+query, and every observability UI green",
    },
    {
        "step": "self-heal",
        "command": "nyxgpt self-heal status",
        "expect": "enabled=true, every component healthy",
    },
    {
        "step": "teardown",
        "command": "nyxgpt cloud destroy --yes",
        "expect": "tunnel closed, substrate destroyed, no billed resources left",
    },
)

# The wrapped commands this surface points at, returned with every payload so
# any consumer names what the CLI documents rather than keeping its own copy
# (the same pattern as cloud_deploy.LIFECYCLE_COMMANDS).
PORTABILITY_COMMANDS: dict[str, str] = {
    "report": "nyxgpt ops portability",
    "strict": "nyxgpt ops portability --strict",
    "json": "nyxgpt ops portability --json",
}


def checkout_root() -> Path | None:
    """Return the repo checkout this module lives in, or None if there isn't one.

    Evidence paths are repo files, so they can only be checked from a
    checkout. This is the dev-checkout-only diagnostic case #3621 explicitly
    allows: it must no-op cleanly on an installed wheel rather than fail,
    because "no checkout here" is the normal, *desired* state for every
    target in this matrix.
    """
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() and (root / "docs").is_dir() else None


# Tokens that stand in front of the executable without being it. A raw
# orchestrator call is still a raw orchestrator call when it hides behind
# `sudo` or an inline environment assignment, and those are exactly the forms
# someone reaches for when a wrapped command "doesn't work" -- so the checker
# has to see through them rather than read `sudo` as the executable.
_COMMAND_PREFIXES = frozenset({"sudo", "env", "command", "exec"})
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _first_word(command: str) -> str:
    """The executable a command line invokes (`docker compose up` -> `docker`).

    Prefixes are skipped, so `sudo docker compose up` and
    `DOCKER_HOST=... docker compose up` both resolve to `docker`. Flags are
    only skipped after a prefix (`sudo -E docker` -> `docker`), because a
    leading flag anywhere else is not a command at all.
    """
    seen_prefix = False
    for part in command.split():
        if part in _COMMAND_PREFIXES or _ENV_ASSIGNMENT.match(part):
            seen_prefix = True
            continue
        if seen_prefix and part.startswith("-"):
            continue
        return part
    return ""


def _source_fetch_findings(commands: tuple[str, ...]) -> list[str]:
    """Commands in `commands` that fetch source instead of a published artifact."""
    findings: list[str] = []
    for command in commands:
        for pattern, why in _SOURCE_FETCH_PATTERNS:
            if re.search(pattern, command, flags=re.IGNORECASE):
                findings.append(f"{command!r} {why}")
                break
    return findings


def _raw_orchestrator_findings(target: Target) -> list[str]:
    """Commands in `target` that invoke an orchestrator the operator shouldn't type."""
    findings: list[str] = []
    for command in target.commands:
        tool = _first_word(command)
        if tool in _RAW_ORCHESTRATORS:
            findings.append(f"{command!r} invokes {tool} directly instead of a `nyxgpt` wrapper")
    return findings


def _unwrapped_operation_findings(target: Target) -> list[str]:
    """Operate/teardown commands that aren't `nyxgpt` commands.

    Install may be a package manager (that is how the artifact arrives);
    everything after it has to go through the wrapper, which is the whole
    substance of CLAUDE.md's Operational Command Wrapping requirement.
    """
    findings = [
        f"{command!r} is not a `nyxgpt` command"
        for command in (*target.operate, target.teardown)
        if _first_word(command) != "nyxgpt"
    ]
    findings += [
        f"{command!r} is neither `nyxgpt` nor a package manager " f"({', '.join(_INSTALL_TOOLS)})"
        for command in target.install
        if _first_word(command) not in ("nyxgpt", *_INSTALL_TOOLS)
    ]
    return findings


def _missing_evidence(target: Target, root: Path) -> list[str]:
    """Evidence paths `target` cites that don't exist in the checkout at `root`."""
    return [path for path in target.evidence if not (root / path).exists()]


def check_target(target: Target, root: Path | None = None) -> dict[str, Any]:
    """Check one row's invariants and return its report.

    `root` is the checkout to resolve evidence against; pass None (the
    default resolution) to let `checkout_root` decide, which yields a skipped
    evidence check on an installed package.
    """
    root = checkout_root() if root is None else root
    checks: list[dict[str, Any]] = []

    source_findings = _source_fetch_findings(target.commands)
    checks.append(
        {
            "check": "repo_less",
            "passed": not source_findings,
            "skipped": False,
            "detail": (
                "; ".join(source_findings)
                if source_findings
                else f"all {len(target.commands)} commands install/operate from published artifacts"
            ),
        }
    )

    wrapper_findings = _raw_orchestrator_findings(target) + _unwrapped_operation_findings(target)
    checks.append(
        {
            "check": "wrapped",
            "passed": not wrapper_findings,
            "skipped": False,
            "detail": (
                "; ".join(wrapper_findings)
                if wrapper_findings
                else "no raw docker/kubectl/terraform in any documented command"
            ),
        }
    )

    if root is None:
        checks.append(
            {
                "check": "evidence",
                "passed": True,
                "skipped": True,
                "detail": "no repo checkout here, so evidence paths cannot be resolved (expected)",
            }
        )
    else:
        missing = _missing_evidence(target, root)
        checks.append(
            {
                "check": "evidence",
                "passed": not missing,
                "skipped": False,
                "detail": (
                    "missing: " + ", ".join(missing)
                    if missing
                    else f"all {len(target.evidence)} cited paths exist"
                ),
            }
        )

    return {
        **target.to_dict(),
        "checks": checks,
        "invariants_passed": all(c["passed"] for c in checks),
        # A row is acceptance-ready when its invariants hold *and* nothing is
        # still missing for a checkout-free install. `status == "acceptance"`
        # is ready in this sense: the code path is complete, and what remains
        # is a demonstration on hardware or an account CI does not have.
        "acceptance_ready": all(c["passed"] for c in checks) and not target.gaps,
    }


def check_matrix(root: Path | None = None) -> dict[str, Any]:
    """Check every row and summarize -- the payload behind the CLI and the API."""
    resolved_root = checkout_root() if root is None else root
    targets = [check_target(target, resolved_root) for target in TARGETS]
    ready = [t for t in targets if t["acceptance_ready"]]
    return {
        "targets": targets,
        "acceptance_sequence": [dict(step) for step in ACCEPTANCE_SEQUENCE],
        "commands": dict(PORTABILITY_COMMANDS),
        "checkout": str(resolved_root) if resolved_root else "",
        "summary": {
            "total": len(targets),
            "acceptance_ready": len(ready),
            "invariants_failed": sum(1 for t in targets if not t["invariants_passed"]),
            "open_gaps": sum(len(t["gaps"]) for t in targets),
            "windows_in_scope": False,
        },
        # The capstone's own AC in one boolean: every in-scope target
        # installable and operable without a checkout.
        "acceptance_ready": len(ready) == len(targets),
    }


# --- CLI entry point ---------------------------------------------------

_STATUS_LABELS: dict[str, str] = {
    "ci-verified": "verified in CI",
    "acceptance": "owner acceptance",
    "gap": "GAP",
}


def _print_report(report: dict[str, Any]) -> None:
    """Print the matrix, its gaps, and the acceptance sequence for an operator."""
    print("Repo-less portability matrix (CLAUDE.md 2026-08-01; Windows out of scope)\n")
    for target in report["targets"]:
        mark = "OK  " if target["acceptance_ready"] else "GAP "
        label = _STATUS_LABELS.get(target["status"], target["status"])
        print(f"{mark}{target['name']}  [{label}]")
        print(f"      artifact  {target['artifact']}")
        for command in target["install"]:
            print(f"      install   {command}")
        for command in target["operate"]:
            print(f"      operate   {command}")
        print(f"      teardown  {target['teardown']}")
        for check in target["checks"]:
            state = "skip" if check["skipped"] else ("pass" if check["passed"] else "FAIL")
            print(f"      {check['check']:<9} {state}: {check['detail']}")
        for gap in target["gaps"]:
            print(f"      gap       {gap}")
        print()

    summary = report["summary"]
    print(
        f"{summary['acceptance_ready']}/{summary['total']} targets installable and operable "
        f"without a repo checkout; {summary['open_gaps']} open gap(s), "
        f"{summary['invariants_failed']} invariant failure(s)."
    )
    if not report["acceptance_ready"]:
        print(
            "\nThe Phase 6 capstone portability criterion is NOT met while any gap is open.\n"
            "Every gap above is a product gap, not a documentation one."
        )

    print("\nClean-machine acceptance sequence (run on a machine with no checkout):")
    for index, step in enumerate(report["acceptance_sequence"], start=1):
        print(f"  {index}. {step['command']}")
        print(f"     -> {step['expect']}")
    print("\nFull runbook: docs/portability-matrix.md")


def portability(args: argparse.Namespace) -> int:
    """`nyxgpt ops portability`: report the matrix, or gate on it with --strict.

    Exit code is 0 whenever the mechanical invariants hold, so the default
    invocation is a report an operator can run anywhere. `--strict` makes it
    a gate that also requires every row to be checkout-free -- which is what
    a CI job should assert once the remaining Compose gap closes, so no
    closed gap can silently reopen.
    """
    report = check_matrix()
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)

    if report["summary"]["invariants_failed"]:
        return 1
    if getattr(args, "strict", False) and not report["acceptance_ready"]:
        return 1
    return 0
