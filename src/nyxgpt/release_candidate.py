"""Release-candidate publishing to PyPI (#3727).

Acceptance testing of the repo-less paths installs nyxGPT **from PyPI**
(`pip install nyxgpt`, the EC2 user-data bootstrap, `nyxgpt cloud deploy`),
so without a published artifact it can only ever exercise the *last stable
release* -- never the release-branch tip carrying the acceptance-failure
fixes merged during testing. This module is the version arithmetic and the
guardrails behind the fix: a dispatch-only workflow
(`.github/workflows/release-candidate-pypi.yml`) publishes the tip as a PEP
440 pre-release, `3.0.0rcN`, which a clean machine can install by exact pin
with no checkout.

Three properties matter, and each is a function here rather than a line of
YAML, so they are testable and cannot drift between the workflow, the CLI
and the dashboard:

* **Pre-release, always.** The published version is `<release>rc<N>`, which
  pip's resolver excludes from `pip install nyxgpt` unless the user asks for
  it (`--pre`) or pins it exactly. Cutting an RC therefore cannot change
  what a normal install resolves to -- see `is_prerelease`.
* **Release branch only.** An RC is the *release line's* tip, so publishing
  is refused from anywhere but a `v<X.Y.Z>` branch whose number matches the
  version declared in `pyproject.toml` (`release_branch_version`,
  `plan`). A `3.0.0rc4` cut from a feature branch would claim to be the
  v3.0.0 line and would not be.
* **Never reuses a number.** `next_rc_number` reads what PyPI already
  serves; RC numbers only ever go up, and PyPI itself refuses a re-upload
  of a version it already has.

The stable-release path is unchanged: `scripts/release_ceremony.sh` Phase 2
still publishes `3.0.0` from the owner's machine with the owner's token.
RCs are for acceptance only and are never announced as a release.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

#: The PyPI project acceptance installs pull from.
PYPI_PROJECT = "nyxgpt"

PYPI_JSON_URL = "https://pypi.org/pypi/{project}/json"

#: The dispatch-only workflow that actually publishes. Named here so the CLI,
#: the docs surface and the workflow itself agree on one filename.
RC_WORKFLOW_FILE = "release-candidate-pypi.yml"

DOCS_ANCHOR = "docs/cloud.md#release-candidates-acceptance-testing-unreleased-code"

# `v3.0.0` -- the release-branch naming this repo uses (CLAUDE.md: master is
# releases only; work merges to the active release branch).
_RELEASE_BRANCH_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")

_RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# PEP 440's normalized pre-release spelling, which is what `python -m build`
# emits and what `pip install nyxgpt==3.0.0rc1` resolves.
_RC_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+)rc(\d+)$")

# Any PEP 440 pre-/dev-release suffix: alpha, beta, release candidate, dev.
_PRERELEASE_RE = re.compile(r"(?:a|b|rc)\d+$|\.dev\d+$")

# `version = "3.0.0"` inside pyproject.toml's `[project]` table.
_VERSION_ASSIGNMENT_RE = re.compile(r'^version\s*=\s*".*"\s*$')


class ReleaseCandidateError(RuntimeError):
    """A release candidate cannot be planned or published as asked."""


# --- Version arithmetic -------------------------------------------------


def release_branch_version(branch: str) -> str | None:
    """Return the release version a `v<X.Y.Z>` branch names, or None.

    None means "not a release branch", which is the guardrail the workflow
    and `nyxgpt release rc --publish` both refuse on.
    """
    match = _RELEASE_BRANCH_RE.match(branch.strip())
    return match.group(1) if match else None


def is_release_branch(branch: str) -> bool:
    """True when `branch` is a release branch (`v3.0.0`), not a feature branch."""
    return release_branch_version(branch) is not None


def parse_rc_version(version: str) -> tuple[str, int] | None:
    """Split `3.0.0rc2` into `("3.0.0", 2)`; None if it isn't an RC version."""
    match = _RC_VERSION_RE.match(version.strip())
    return (match.group(1), int(match.group(2))) if match else None


def is_prerelease(version: str) -> bool:
    """True when pip's resolver treats `version` as a pre-release.

    The load-bearing property of the whole feature: while this is true of
    every version this module publishes, `pip install nyxgpt` cannot resolve
    to one (PEP 440 -- pre-releases are excluded unless requested).
    """
    return bool(_PRERELEASE_RE.search(version.strip()))


def release_line(version: str) -> str:
    """Return the release a version belongs to: `3.0.0rc2` and `3.0.0` -> `3.0.0`."""
    parsed = parse_rc_version(version)
    return parsed[0] if parsed else version.strip()


def rc_version(release: str, number: int) -> str:
    """Compose the PEP 440 pre-release version for `release` + `number`."""
    if not _RELEASE_VERSION_RE.match(release.strip()):
        raise ReleaseCandidateError(
            f"{release!r} is not a release version -- expected X.Y.Z, e.g. 3.0.0"
        )
    if number < 1:
        raise ReleaseCandidateError(f"RC number must be 1 or greater, got {number}")
    return f"{release.strip()}rc{number}"


def published_rc_numbers(release: str, published: list[str] | tuple[str, ...]) -> list[int]:
    """RC numbers already published for `release`, ascending."""
    numbers = []
    for version in published:
        parsed = parse_rc_version(version)
        if parsed and parsed[0] == release:
            numbers.append(parsed[1])
    return sorted(numbers)


def next_rc_number(release: str, published: list[str] | tuple[str, ...]) -> int:
    """The next unused RC number for `release` (1 when none exist yet).

    Derived from what PyPI already serves rather than from a counter in the
    repo: PyPI is the only place that knows, and it is also the thing that
    would reject a duplicate ten minutes into a publish run.
    """
    numbers = published_rc_numbers(release, published)
    return numbers[-1] + 1 if numbers else 1


def next_rc_version(release: str, published: list[str] | tuple[str, ...]) -> str:
    """The next unused RC version for `release`, e.g. `3.0.0rc3`."""
    return rc_version(release, next_rc_number(release, published))


# --- pyproject.toml ------------------------------------------------------


def declared_version(pyproject_path: Path | None = None) -> str:
    """Return the release line this working tree (or installed package) is on.

    Prefers `pyproject.toml`'s `project.version`, which is what the release
    branch declares; falls back to the installed package metadata so the
    command still works repo-less (CLAUDE.md, 2026-08-01). Any pre-release
    suffix is stripped -- installing `3.0.0rc1` and asking for the next RC
    must still plan against the `3.0.0` line.
    """
    path = pyproject_path or _checkout_pyproject()
    if path is not None and path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ReleaseCandidateError(f"Cannot read {path}: {exc}") from exc
        declared = str(data.get("project", {}).get("version", "")).strip()
        if declared:
            return release_line(declared)

    from nyxgpt.version import running_version

    return release_line(running_version())


def _checkout_pyproject() -> Path | None:
    """The checkout's pyproject.toml, or None when running from an installed wheel."""
    candidate = Path(__file__).resolve().parents[2] / "pyproject.toml"
    return candidate if candidate.is_file() else None


def pin_version(pyproject_text: str, version: str) -> str:
    """Return `pyproject_text` with `[project] version` set to `version`.

    The publish workflow builds an RC by rewriting this one line in the
    checkout it already has -- the RC version is never committed, so the
    release branch keeps declaring the stable version it is heading for.
    Only the `[project]` table's assignment is touched; a `version = "..."`
    under some other table (a tool config, say) is left alone.
    """
    if not parse_rc_version(version) and not _RELEASE_VERSION_RE.match(version):
        raise ReleaseCandidateError(f"Refusing to pin an unrecognized version: {version!r}")

    lines = pyproject_text.splitlines(keepends=True)
    in_project = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project and _VERSION_ASSIGNMENT_RE.match(stripped):
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f'version = "{version}"{newline}'
            return "".join(lines)

    raise ReleaseCandidateError(
        'pyproject.toml has no `version = "..."` assignment in its [project] table'
    )


# --- PyPI ----------------------------------------------------------------


def fetch_published_versions(project: str = PYPI_PROJECT, timeout: float = 10.0) -> list[str]:
    """Every version PyPI currently serves for `project`, including pre-releases.

    Raises `ReleaseCandidateError` on any transport or protocol failure --
    guessing the next RC number from an empty list after a failed lookup
    would re-propose a number that is already taken.
    """
    import httpx

    url = PYPI_JSON_URL.format(project=project)
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise ReleaseCandidateError(f"Could not reach PyPI at {url}: {exc}") from exc
    if response.status_code == 404:
        # A project that has never been published has no versions -- not an error.
        return []
    if response.status_code != 200:
        raise ReleaseCandidateError(f"PyPI returned HTTP {response.status_code} for {url}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ReleaseCandidateError(f"PyPI returned invalid JSON for {url}: {exc}") from exc
    releases = payload.get("releases") or {}
    return sorted(str(version) for version in releases)


# --- The plan the CLI, the API and the dashboard all render --------------


def plan(
    branch: str,
    published: list[str] | tuple[str, ...] | None = None,
    pyproject_path: Path | None = None,
) -> dict[str, Any]:
    """Describe the RC that *would* be cut from `branch`, and whether it may be.

    Pure apart from the PyPI lookup (skipped when `published` is supplied).
    Never publishes anything: `publishable` is the machine-readable form of
    "the guardrails allow this", and the workflow re-checks them itself on
    the runner rather than trusting a caller's word for it.
    """
    branch = branch.strip()
    branch_version = release_branch_version(branch)
    declared = declared_version(pyproject_path)

    lookup_error = ""
    if published is None:
        try:
            published = fetch_published_versions()
        except ReleaseCandidateError as exc:
            lookup_error = str(exc)
            published = []

    release = branch_version or declared
    rc_numbers = published_rc_numbers(release, published)
    candidate = next_rc_version(release, published)
    version_matches_branch = bool(branch_version) and branch_version == declared

    blockers: list[str] = []
    if not branch_version:
        blockers.append(
            f"{branch or '(no branch)'} is not a release branch -- an RC is only ever cut "
            "from the release line's tip (v3.0.0)"
        )
    elif not version_matches_branch:
        blockers.append(
            f"branch {branch} names release {branch_version}, but pyproject.toml declares "
            f"{declared} -- the RC would misreport which line it came from"
        )
    if lookup_error:
        blockers.append(f"PyPI lookup failed, so the next RC number is unknown: {lookup_error}")

    return {
        "branch": branch,
        "is_release_branch": bool(branch_version),
        "branch_version": branch_version or "",
        "declared_version": declared,
        "version_matches_branch": version_matches_branch,
        "release": release,
        "published_releases": [v for v in published if not is_prerelease(v)],
        "published_rcs": [rc_version(release, n) for n in rc_numbers],
        "next_rc_number": next_rc_number(release, published),
        "next_rc_version": candidate,
        # Always true by construction; surfaced so the dashboard can state
        # the guarantee rather than assert it in hand-written prose.
        "is_prerelease": is_prerelease(candidate),
        "workflow": RC_WORKFLOW_FILE,
        "pypi_lookup_error": lookup_error,
        "publishable": not blockers,
        "blockers": blockers,
        "commands": {
            "plan": "nyxgpt release rc",
            "publish": "nyxgpt release rc --publish",
            "install": f"pip install nyxgpt=={candidate}",
            "user_data": f"nyxgpt cloud user-data --os linux --version {candidate}",
            "deploy": f"nyxgpt cloud deploy --version {candidate}",
        },
        "guardrails": [
            "Dispatch-only: the workflow has no push, tag or release trigger.",
            "Release branches only: it refuses any ref that is not v<X.Y.Z>.",
            f"Pre-release version ({candidate}): `pip install nyxgpt` never resolves to it.",
            "Acceptance only: an RC is never announced, and never replaces the "
            "owner-run stable release (scripts/release_ceremony.sh Phase 2).",
        ],
        "docs": DOCS_ANCHOR,
    }


# --- Dispatching the publish workflow ------------------------------------


def dispatch(branch: str, rc_number: int | None = None) -> dict[str, Any]:
    """Ask GitHub to run the RC publish workflow on `branch`.

    The owner-side wrapper for the workflow (CLAUDE.md's Operational Command
    Wrapping requirement): the operator runs `nyxgpt release rc --publish`,
    not a raw API call. Credentials are the ones config.ini already holds for
    `nyxgpt ops secrets-sync` (`[github] pat`, `repo_owner`, `repo_name`).
    """
    import httpx

    from nyxgpt.config import (
        get_github_pat,
        get_github_repo_name,
        get_github_repo_owner,
        load_config,
    )

    if not is_release_branch(branch):
        raise ReleaseCandidateError(
            f"Refusing to publish an RC from {branch!r} -- release branches (v3.0.0) only."
        )

    cfg = load_config()
    pat = get_github_pat(cfg)
    owner = get_github_repo_owner(cfg)
    repo = get_github_repo_name(cfg)
    missing = [
        name
        for name, value in (
            ("[github] pat", pat),
            ("[github] repo_owner", owner),
            ("[github] repo_name", repo),
        )
        if not value
    ]
    if missing:
        raise ReleaseCandidateError(
            "Missing config.ini values needed to dispatch the workflow: "
            + ", ".join(missing)
            + " (run `nyxgpt secrets setup`)"
        )

    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{RC_WORKFLOW_FILE}/dispatches"
    body: dict[str, Any] = {"ref": branch, "inputs": {}}
    if rc_number is not None:
        body["inputs"]["rc_number"] = str(rc_number)
    try:
        response = httpx.post(
            url,
            json=body,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    except httpx.HTTPError as exc:
        raise ReleaseCandidateError(f"Could not reach the GitHub API: {exc}") from exc
    if response.status_code != 204:
        raise ReleaseCandidateError(
            f"GitHub refused the workflow dispatch (HTTP {response.status_code}): {response.text}"
        )
    return {
        "dispatched": True,
        "workflow": RC_WORKFLOW_FILE,
        "ref": branch,
        "runs_url": f"https://github.com/{owner}/{repo}/actions/workflows/{RC_WORKFLOW_FILE}",
    }


# --- CLI -----------------------------------------------------------------


def default_branch() -> str:
    """The release branch to plan against when `--branch` is not given.

    `[github] RELEASE_BRANCH` when the agent tooling's config is present,
    otherwise `v<declared version>` -- which is the right answer on a clean
    machine with only the installed package.
    """
    try:
        from nyxgpt.config import load_config

        configured = load_config().get("github", "RELEASE_BRANCH", fallback="").strip()
    except Exception:  # pragma: no cover - config is optional here
        configured = ""
    if configured:
        return configured
    return f"v{declared_version()}"


def _print_plan(report: dict[str, Any]) -> None:
    """Render the plan for an operator."""
    print(f"Release-candidate plan for {report['branch']} (line {report['release']})\n")
    print(f"  declared version   {report['declared_version']}")
    published = ", ".join(report["published_rcs"]) or "none"
    print(f"  published RCs      {published}")
    print(f"  next RC            {report['next_rc_version']}")
    print(f"  workflow           .github/workflows/{report['workflow']} (workflow_dispatch only)")

    print("\nGuardrails:")
    for guardrail in report["guardrails"]:
        print(f"  - {guardrail}")

    if report["publishable"]:
        print(f"\nReady to publish. Cut it with:\n  {report['commands']['publish']}")
    else:
        print("\nNOT publishable from here:")
        for blocker in report["blockers"]:
            print(f"  - {blocker}")

    print("\nOnce published, acceptance-test the tip with no checkout:")
    print(f"  {report['commands']['install']}")
    print(f"  {report['commands']['user_data']}")
    print(f"  {report['commands']['deploy']}")
    print(f"\nFull runbook: {report['docs']}")


def release_rc(args: argparse.Namespace) -> int:
    """`nyxgpt release rc`: plan an RC, or dispatch the publish workflow with `--publish`."""
    branch = (getattr(args, "branch", None) or default_branch()).strip()
    try:
        report = plan(branch)
    except ReleaseCandidateError as exc:
        print(f"nyxgpt release rc: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "publish", False):
        if not report["publishable"]:
            print("nyxgpt release rc: refusing to publish:", file=sys.stderr)
            for blocker in report["blockers"]:
                print(f"  - {blocker}", file=sys.stderr)
            return 1
        try:
            dispatched = dispatch(branch, getattr(args, "rc_number", None))
        except ReleaseCandidateError as exc:
            print(f"nyxgpt release rc: {exc}", file=sys.stderr)
            return 1
        report["dispatched"] = dispatched
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2))
        else:
            target = (
                rc_version(report["release"], args.rc_number)
                if getattr(args, "rc_number", None)
                else report["next_rc_version"]
            )
            print(f"Dispatched {report['workflow']} on {branch} to publish {target}.")
            print(f"Watch it: {dispatched['runs_url']}")
        return 0

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
    else:
        _print_plan(report)
    return 0 if report["publishable"] else 1


# --- Module entry point used by the publish workflow ---------------------


def main(argv: list[str] | None = None) -> int:
    """`python -m nyxgpt.release_candidate`: resolve (and optionally pin) the RC version.

    The publish workflow runs exactly this instead of re-implementing the
    guardrails and the version arithmetic in shell, so CI and the CLI can
    never disagree about what `3.0.0rcN` means. Prints the resolved version
    to stdout; everything else goes to stderr so the caller can use
    `$(...)` directly.
    """
    parser = argparse.ArgumentParser(
        prog="python -m nyxgpt.release_candidate",
        description="Resolve the next release-candidate version for a release branch.",
    )
    parser.add_argument("--branch", required=True, help="Git ref the RC is cut from, e.g. v3.0.0")
    parser.add_argument(
        "--rc-number",
        default="",
        help="Explicit RC number (blank = next unused, resolved from PyPI)",
    )
    parser.add_argument(
        "--pin",
        default="",
        help="pyproject.toml to rewrite in place with the resolved RC version",
    )
    parser.add_argument("--json", action="store_true", help="Print the full plan as JSON to stderr")
    args = parser.parse_args(argv)

    try:
        report = plan(args.branch)
    except ReleaseCandidateError as exc:
        print(f"release-candidate: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2), file=sys.stderr)

    if not report["publishable"]:
        print("release-candidate: refusing to publish:", file=sys.stderr)
        for blocker in report["blockers"]:
            print(f"  - {blocker}", file=sys.stderr)
        return 1

    requested = str(args.rc_number).strip()
    if requested:
        if not requested.isdigit():
            print(
                f"release-candidate: --rc-number must be a number, got {requested!r}",
                file=sys.stderr,
            )
            return 1
        try:
            version = rc_version(report["release"], int(requested))
        except ReleaseCandidateError as exc:
            print(f"release-candidate: {exc}", file=sys.stderr)
            return 1
        if version in report["published_rcs"]:
            print(
                f"release-candidate: {version} is already on PyPI -- PyPI never accepts a "
                f"re-upload. Next unused is {report['next_rc_version']}.",
                file=sys.stderr,
            )
            return 1
    else:
        version = report["next_rc_version"]

    if args.pin:
        path = Path(args.pin)
        try:
            pinned = pin_version(path.read_text(encoding="utf-8"), version)
        except (OSError, ReleaseCandidateError) as exc:
            print(f"release-candidate: {exc}", file=sys.stderr)
            return 1
        path.write_text(pinned, encoding="utf-8")
        print(f"release-candidate: pinned {path} to {version}", file=sys.stderr)

    print(version)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the publish workflow
    raise SystemExit(main())
