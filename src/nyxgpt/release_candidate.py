"""The single PyPI publish core: dev, rc and stable channels (#3727).

Acceptance testing of the repo-less paths installs nyxGPT **from PyPI**
(`pip install nyxgpt`, the EC2 user-data bootstrap, `nyxgpt cloud deploy`),
so without a published artifact it can only ever exercise the *last stable
release* -- never the release-branch tip carrying the acceptance-failure
fixes merged during testing. This module is the version arithmetic and the
guardrails behind the fix, and it is the *only* one: per the owner decision
of 2026-08-11 there is one channel-parameterized build/publish pipeline
(`.github/workflows/release-publish-pypi.yml`) with three entry points --

* **dev** -- a nightly schedule publishes `3.0.0.devN` from the
  release-branch tip, skipping the run when the tip has not moved since the
  last successful nightly (`select_last_scheduled_sha`);
* **rc** -- a manual dispatch cuts a deliberate `3.0.0rcN`;
* **stable** -- `scripts/release_ceremony.sh` Phase 2 delegates to the same
  workflow. The ceremony keeps only its ceremony-exclusive steps (master
  merge, tag, Homebrew tap, GitHub Release, sign-off); a dev or rc build
  never runs any of them.

PEP 440 orders these `3.0.0.devN < 3.0.0aN < 3.0.0bN < 3.0.0rcN < 3.0.0`, so
a nightly can never shadow an RC and neither can shadow the release.

Four properties matter, and each is a function here rather than a line of
YAML, so they are testable and cannot drift between the workflow, the CLI,
the ceremony and the dashboard:

* **Pre-release unless the channel is stable.** What dev and rc publish is
  `3.0.0.devN` / `3.0.0rcN`, which pip's resolver excludes from
  `pip install nyxgpt` unless the user asks for it (`--pre`) or pins it
  exactly. Cutting either therefore cannot change what a normal install
  resolves to -- see `is_prerelease`.
* **Release branch only.** A build is the *release line's* tip, so
  publishing is refused from anywhere but a `v<X.Y.Z>` branch whose number
  matches the version declared in `pyproject.toml`
  (`release_branch_version`, `plan`). A `3.0.0rc4` cut from a feature branch
  would claim to be the v3.0.0 line and would not be.
* **Never reuses a version.** `next_rc_number` and `resolve_dev_number` read
  what PyPI already serves; numbers only ever go up, and PyPI itself refuses
  a re-upload of a version it already has (versions are immutable).
* **Stable is ceremony-only.** The stable channel additionally requires the
  release tag to be at the ref's tip (the ceremony creates it in Phase 1,
  before it delegates the publish) plus an explicit confirmation token, so a
  stable version cannot be published by dispatching the workflow by hand --
  see `STABLE_CONFIRMATION` and `main`.

Credentials: the workflow publishes with PyPI Trusted Publishing (OIDC,
`id-token: write`). There is no stored PyPI token anywhere in this path --
the owner retired it with the same 2026-08-11 decision.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

#: The PyPI project acceptance installs pull from.
PYPI_PROJECT = "nyxgpt"

PYPI_JSON_URL = "https://pypi.org/pypi/{project}/json"

#: The one publish workflow. Named here so the CLI, the ceremony, the docs
#: surface and the workflow itself agree on one filename -- it is also the
#: name PyPI's Trusted Publisher configuration has to carry.
PUBLISH_WORKFLOW_FILE = "release-publish-pypi.yml"

#: The channels the pipeline understands, weakest version first.
CHANNELS = ("dev", "rc", "stable")

#: Printed by `main` instead of a version when a nightly has nothing to do
#: (the release-branch tip has not moved since the last successful nightly).
SKIP_SENTINEL = "SKIP"

#: The stable channel refuses to resolve a version without this token, which
#: only `scripts/release_ceremony.sh` passes.
STABLE_CONFIRMATION = "ceremony"

DOCS_ANCHOR = "docs/cloud.md#pypi-publishing-dev-rc-and-stable"

# `v3.0.0` -- the release-branch naming this repo uses (CLAUDE.md: master is
# releases only; work merges to the active release branch).
_RELEASE_BRANCH_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")

_RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# PEP 440's normalized pre-release spellings, which are what `python -m build`
# emits and what `pip install nyxgpt==3.0.0rc1` resolves.
_RC_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+)rc(\d+)$")

_DEV_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+)\.dev(\d+)$")

# Any PEP 440 pre-/dev-release suffix: alpha, beta, release candidate, dev.
_PRERELEASE_RE = re.compile(r"(?:a|b|rc)\d+$|\.dev\d+$")

# `version = "3.0.0"` inside pyproject.toml's `[project]` table.
_VERSION_ASSIGNMENT_RE = re.compile(r'^version\s*=\s*".*"\s*$')


class ReleaseCandidateError(RuntimeError):
    """A build cannot be planned or published as asked."""


def _check_channel(channel: str) -> str:
    """Normalize and validate a channel name."""
    normalized = channel.strip().lower()
    if normalized not in CHANNELS:
        raise ReleaseCandidateError(
            f"Unknown channel {channel!r} -- expected one of {', '.join(CHANNELS)}"
        )
    return normalized


# --- Version arithmetic -------------------------------------------------


def release_branch_version(branch: str) -> str | None:
    """Return the release version a `v<X.Y.Z>` branch names, or None.

    None means "not a release branch", which is the guardrail every entry
    point refuses on.
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


def parse_dev_version(version: str) -> tuple[str, int] | None:
    """Split `3.0.0.dev7` into `("3.0.0", 7)`; None if it isn't a dev version."""
    match = _DEV_VERSION_RE.match(version.strip())
    return (match.group(1), int(match.group(2))) if match else None


def is_prerelease(version: str) -> bool:
    """True when pip's resolver treats `version` as a pre-release.

    The load-bearing property of the dev and rc channels: while this is true
    of everything they publish, `pip install nyxgpt` cannot resolve to one
    (PEP 440 -- pre-releases are excluded unless requested).
    """
    return bool(_PRERELEASE_RE.search(version.strip()))


def release_line(version: str) -> str:
    """Return the release a version belongs to: `3.0.0rc2`, `3.0.0.dev4` -> `3.0.0`."""
    parsed = parse_rc_version(version) or parse_dev_version(version)
    return parsed[0] if parsed else version.strip()


def rc_version(release: str, number: int) -> str:
    """Compose the PEP 440 pre-release version for `release` + RC `number`."""
    _require_release_version(release)
    if number < 1:
        raise ReleaseCandidateError(f"RC number must be 1 or greater, got {number}")
    return f"{release.strip()}rc{number}"


def dev_version(release: str, number: int) -> str:
    """Compose the PEP 440 dev version for `release` + build `number`.

    `3.0.0.dev41` sorts below every RC of the same line, so a nightly can
    never shadow a candidate or the release itself.
    """
    _require_release_version(release)
    if number < 1:
        raise ReleaseCandidateError(f"Dev build number must be 1 or greater, got {number}")
    return f"{release.strip()}.dev{number}"


def _require_release_version(release: str) -> None:
    """Raise unless `release` is a bare `X.Y.Z` release version."""
    if not _RELEASE_VERSION_RE.match(release.strip()):
        raise ReleaseCandidateError(
            f"{release!r} is not a release version -- expected X.Y.Z, e.g. 3.0.0"
        )


def channel_version(channel: str, release: str, number: int | None = None) -> str:
    """The version `channel` publishes for `release`.

    `stable` is the bare release version and takes no number; `dev` and `rc`
    require one (the callers below resolve it from what PyPI serves).
    """
    resolved = _check_channel(channel)
    if resolved == "stable":
        _require_release_version(release)
        return release.strip()
    if number is None:
        raise ReleaseCandidateError(f"The {resolved} channel needs a build number")
    return rc_version(release, number) if resolved == "rc" else dev_version(release, number)


def published_rc_numbers(release: str, published: list[str] | tuple[str, ...]) -> list[int]:
    """RC numbers already published for `release`, ascending."""
    return _published_numbers(parse_rc_version, release, published)


def published_dev_numbers(release: str, published: list[str] | tuple[str, ...]) -> list[int]:
    """Dev build numbers already published for `release`, ascending."""
    return _published_numbers(parse_dev_version, release, published)


def _published_numbers(
    parser: Any, release: str, published: list[str] | tuple[str, ...]
) -> list[int]:
    """The numbers `parser` recognizes for `release` in `published`, ascending."""
    numbers = []
    for version in published:
        parsed = parser(version)
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


def next_dev_number(release: str, published: list[str] | tuple[str, ...]) -> int:
    """The next unused dev build number for `release` (1 when none exist yet)."""
    numbers = published_dev_numbers(release, published)
    return numbers[-1] + 1 if numbers else 1


def next_rc_version(release: str, published: list[str] | tuple[str, ...]) -> str:
    """The next unused RC version for `release`, e.g. `3.0.0rc3`."""
    return rc_version(release, next_rc_number(release, published))


def next_dev_version(release: str, published: list[str] | tuple[str, ...]) -> str:
    """The next unused dev version for `release`, e.g. `3.0.0.dev12`."""
    return dev_version(release, next_dev_number(release, published))


def resolve_dev_number(
    release: str, published: list[str] | tuple[str, ...], run_number: int | None = None
) -> int:
    """The dev build number a nightly should publish.

    The workflow passes its `GITHUB_RUN_NUMBER` so every run gets a distinct
    version (PyPI versions are immutable -- a repeat upload is rejected), but
    a run counter restarts at 1 for a newly added workflow file, which could
    collide with builds already on PyPI. Taking the larger of the two keeps
    the version both unique and monotonic.
    """
    lowest_free = next_dev_number(release, published)
    if run_number is None:
        return lowest_free
    if run_number < 1:
        raise ReleaseCandidateError(f"Run number must be 1 or greater, got {run_number}")
    return max(run_number, lowest_free)


def next_channel_version(
    channel: str,
    release: str,
    published: list[str] | tuple[str, ...],
    run_number: int | None = None,
) -> str:
    """The version `channel` would publish next for `release`."""
    resolved = _check_channel(channel)
    if resolved == "stable":
        return channel_version("stable", release)
    if resolved == "rc":
        return next_rc_version(release, published)
    return dev_version(release, resolve_dev_number(release, published, run_number))


# --- pyproject.toml ------------------------------------------------------


def declared_version(pyproject_path: Path | None = None) -> str:
    """Return the release line this working tree (or installed package) is on.

    Prefers `pyproject.toml`'s `project.version`, which is what the release
    branch declares; falls back to the installed package metadata so the
    command still works repo-less (CLAUDE.md, 2026-08-01). Any pre-release
    suffix is stripped -- installing `3.0.0rc1` and asking for the next build
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

    The publish workflow builds a dev or rc artifact by rewriting this one
    line in the checkout it already has -- the pre-release version is never
    committed, so the release branch keeps declaring the stable version it is
    heading for. Only the `[project]` table's assignment is touched; a
    `version = "..."` under some other table (a tool config, say) is left
    alone.
    """
    recognized = (
        parse_rc_version(version)
        or parse_dev_version(version)
        or _RELEASE_VERSION_RE.match(version)
    )
    if not recognized:
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
    guessing the next number from an empty list after a failed lookup would
    re-propose a version that is already taken, and PyPI would reject the
    upload minutes into the run.
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


# --- Nightly change detection --------------------------------------------


def select_last_scheduled_sha(payload: dict[str, Any], exclude_run_id: str | int = "") -> str:
    """The commit the last successful *scheduled* run of the workflow built.

    The nightly's no-op condition (owner requirement: skip the publish when
    the release-branch tip is unchanged since the last published build).
    GitHub's own run history is the state store -- nothing has to be written
    back to the repo or to PyPI to remember what was last built.

    `exclude_run_id` drops the caller's own run, which is already listed as
    in-progress-then-success by the time a later run reads this.
    """
    runs = payload.get("workflow_runs") or []
    candidates = [
        run
        for run in runs
        if run.get("event") == "schedule"
        and run.get("conclusion") == "success"
        and str(run.get("id", "")) != str(exclude_run_id or "")
        and run.get("head_sha")
    ]
    if not candidates:
        return ""
    newest = max(candidates, key=lambda run: str(run.get("created_at") or ""))
    return str(newest["head_sha"])


def fetch_last_scheduled_sha(
    repo: str,
    workflow_file: str = PUBLISH_WORKFLOW_FILE,
    token: str = "",
    exclude_run_id: str | int = "",
    timeout: float = 30.0,
) -> str:
    """Ask GitHub for the commit the last successful nightly published.

    Returns "" when there is no such run (the first nightly always
    publishes). Raises on a transport or protocol failure: silently treating
    an API outage as "nothing published yet" would upload a redundant build
    every night the API is flaky.
    """
    import httpx

    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}"
        "/runs?event=schedule&status=success&per_page=30"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ReleaseCandidateError(f"Could not reach the GitHub API: {exc}") from exc
    if response.status_code == 404:
        # The workflow has no run history yet (first nightly on a new file).
        return ""
    if response.status_code != 200:
        raise ReleaseCandidateError(
            f"GitHub returned HTTP {response.status_code} listing runs of {workflow_file}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ReleaseCandidateError(f"GitHub returned invalid JSON for {url}: {exc}") from exc
    return select_last_scheduled_sha(payload, exclude_run_id)


# --- The plan the CLI, the API, the ceremony and the dashboard all render --


def plan(
    branch: str,
    channel: str = "rc",
    published: list[str] | tuple[str, ...] | None = None,
    pyproject_path: Path | None = None,
    run_number: int | None = None,
) -> dict[str, Any]:
    """Describe the build that *would* be published from `branch`, and whether it may be.

    Pure apart from the PyPI lookup (skipped when `published` is supplied).
    Never publishes anything: `publishable` is the machine-readable form of
    "the guardrails allow this", and the workflow re-checks them itself on
    the runner rather than trusting a caller's word for it.
    """
    resolved_channel = _check_channel(channel)
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
    dev_numbers = published_dev_numbers(release, published)
    candidate = next_channel_version(resolved_channel, release, published, run_number)
    version_matches_branch = bool(branch_version) and branch_version == declared

    blockers: list[str] = []
    if not branch_version:
        blockers.append(
            f"{branch or '(no branch)'} is not a release branch -- a build is only ever cut "
            "from the release line's tip (v3.0.0)"
        )
    elif not version_matches_branch:
        blockers.append(
            f"branch {branch} names release {branch_version}, but pyproject.toml declares "
            f"{declared} -- the build would misreport which line it came from"
        )
    if lookup_error:
        blockers.append(
            f"PyPI lookup failed, so the next version for the {resolved_channel} channel is "
            f"unknown: {lookup_error}"
        )
    elif candidate in published:
        blockers.append(
            f"PyPI already serves {candidate} and versions are immutable -- it would reject "
            "the upload"
        )

    return {
        "branch": branch,
        "channel": resolved_channel,
        "channels": list(CHANNELS),
        "is_release_branch": bool(branch_version),
        "branch_version": branch_version or "",
        "declared_version": declared,
        "version_matches_branch": version_matches_branch,
        "release": release,
        "published_releases": [v for v in published if not is_prerelease(v)],
        "published_rcs": [rc_version(release, n) for n in rc_numbers],
        "published_dev_builds": [dev_version(release, n) for n in dev_numbers],
        "next_rc_number": next_rc_number(release, published),
        "next_rc_version": next_rc_version(release, published),
        "next_dev_version": next_dev_version(release, published),
        # What *this* channel would publish -- the same field whichever
        # entry point asked, so the dashboard and the workflow agree.
        "version": candidate,
        # True for dev and rc by construction, false for stable; surfaced so
        # the dashboard can state the guarantee rather than assert it in
        # hand-written prose.
        "is_prerelease": is_prerelease(candidate),
        "workflow": PUBLISH_WORKFLOW_FILE,
        "pypi_lookup_error": lookup_error,
        "publishable": not blockers,
        "blockers": blockers,
        "commands": {
            "plan": f"nyxgpt release publish --channel {resolved_channel}",
            "publish": f"nyxgpt release publish --channel {resolved_channel} --publish",
            "install": f"pip install nyxgpt=={candidate}",
            "user_data": f"nyxgpt cloud user-data --os linux --version {candidate}",
            "deploy": f"nyxgpt cloud deploy --version {candidate}",
        },
        "guardrails": _guardrails(resolved_channel, candidate),
        "docs": DOCS_ANCHOR,
    }


def _guardrails(channel: str, candidate: str) -> list[str]:
    """The guardrails in force for `channel`, in the words the surfaces render."""
    shared = [
        "Scheduled and dispatch triggers only: the workflow has no push, tag or release trigger.",
        "Release branches only: it refuses any ref that is not v<X.Y.Z> matching pyproject.toml.",
        "Trusted Publishing (OIDC) only: no PyPI token is stored in the repo or in Actions.",
    ]
    if channel == "stable":
        return shared + [
            "Ceremony only: the stable channel additionally requires the release tag at the "
            f"ref's tip and the '{STABLE_CONFIRMATION}' confirmation, which only "
            "scripts/release_ceremony.sh passes.",
            "PyPI versions are immutable: a release that is already published is refused.",
        ]
    return shared + [
        f"Pre-release version ({candidate}): `pip install nyxgpt` never resolves to it.",
        "Acceptance only: a dev or rc build is never announced, and never runs a ceremony "
        "step (master merge, tag, Homebrew tap, GitHub Release).",
    ]


# --- Dispatching the publish workflow ------------------------------------


def dispatch(branch: str, channel: str = "rc", number: int | None = None) -> dict[str, Any]:
    """Ask GitHub to run the publish workflow on `branch` for `channel`.

    The owner-side wrapper for the workflow (CLAUDE.md's Operational Command
    Wrapping requirement): the operator runs `nyxgpt release publish
    --publish`, not a raw API call. Credentials are the ones config.ini
    already holds for `nyxgpt ops secrets-sync` (`[github] pat`,
    `repo_owner`, `repo_name`).

    The stable channel is not dispatchable here on purpose -- a release is
    published by `scripts/release_ceremony.sh`, which delegates to this same
    workflow with the tag and the confirmation only it can supply.
    """
    import httpx

    from nyxgpt.config import (
        get_github_pat,
        get_github_repo_name,
        get_github_repo_owner,
        load_config,
    )

    resolved_channel = _check_channel(channel)
    if resolved_channel == "stable":
        raise ReleaseCandidateError(
            "The stable channel is published by scripts/release_ceremony.sh (which delegates "
            f"to {PUBLISH_WORKFLOW_FILE}), not by this command."
        )
    if not is_release_branch(branch):
        raise ReleaseCandidateError(
            f"Refusing to publish from {branch!r} -- release branches (v3.0.0) only."
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

    url = (
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/"
        f"{PUBLISH_WORKFLOW_FILE}/dispatches"
    )
    inputs: dict[str, str] = {"channel": resolved_channel}
    if number is not None:
        inputs["number"] = str(number)
    try:
        response = httpx.post(
            url,
            json={"ref": branch, "inputs": inputs},
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
        "workflow": PUBLISH_WORKFLOW_FILE,
        "channel": resolved_channel,
        "ref": branch,
        "runs_url": (
            f"https://github.com/{owner}/{repo}/actions/workflows/{PUBLISH_WORKFLOW_FILE}"
        ),
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
    print(
        f"PyPI publish plan for {report['branch']} "
        f"(line {report['release']}, {report['channel']} channel)\n"
    )
    print(f"  declared version   {report['declared_version']}")
    print(f"  published RCs      {', '.join(report['published_rcs']) or 'none'}")
    print(f"  published dev      {', '.join(report['published_dev_builds']) or 'none'}")
    print(f"  next version       {report['version']}")
    print(f"  workflow           .github/workflows/{report['workflow']}")

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


def release_publish(args: argparse.Namespace) -> int:
    """`nyxgpt release publish`: plan a build, or dispatch the workflow with `--publish`.

    `nyxgpt release rc` is the same command with `--channel rc`.
    """
    branch = (getattr(args, "branch", None) or default_branch()).strip()
    channel = (getattr(args, "channel", None) or "rc").strip().lower()
    number = getattr(args, "number", None)
    if number is None:
        # `nyxgpt release rc --rc-number N` is the channel-specific spelling.
        number = getattr(args, "rc_number", None)

    try:
        channel = _check_channel(channel)
        report = plan(branch, channel)
        if number is not None:
            # Fail here rather than dispatching a run that the workflow's own
            # guardrail will reject minutes later.
            requested = channel_version(channel, report["release"], number)
            if requested in report["published_rcs"] or requested in report["published_dev_builds"]:
                raise ReleaseCandidateError(
                    f"{requested} is already on PyPI -- versions are immutable. "
                    f"Next unused is {report['version']}."
                )
            report["version"] = requested
            report["commands"] = _pinned_commands(report["commands"], requested)
    except ReleaseCandidateError as exc:
        print(f"nyxgpt release {channel or 'publish'}: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "publish", False):
        if not report["publishable"]:
            print("nyxgpt release publish: refusing to publish:", file=sys.stderr)
            for blocker in report["blockers"]:
                print(f"  - {blocker}", file=sys.stderr)
            return 1
        try:
            dispatched = dispatch(branch, channel, number)
        except ReleaseCandidateError as exc:
            print(f"nyxgpt release publish: {exc}", file=sys.stderr)
            return 1
        report["dispatched"] = dispatched
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2))
        else:
            print(
                f"Dispatched {report['workflow']} on {branch} to publish "
                f"{report['version']} ({channel} channel)."
            )
            print(f"Watch it: {dispatched['runs_url']}")
        return 0

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
    else:
        _print_plan(report)
    return 0 if report["publishable"] else 1


def _pinned_commands(commands: dict[str, str], version: str) -> dict[str, str]:
    """The plan's commands re-pinned to an explicitly requested version."""
    return {
        **commands,
        "install": f"pip install nyxgpt=={version}",
        "user_data": f"nyxgpt cloud user-data --os linux --version {version}",
        "deploy": f"nyxgpt cloud deploy --version {version}",
    }


def release_rc(args: argparse.Namespace) -> int:
    """`nyxgpt release rc`: the rc channel of `nyxgpt release publish`."""
    if not getattr(args, "channel", None):
        args.channel = "rc"
    return release_publish(args)


# --- Module entry point used by the publish workflow ---------------------


def main(argv: list[str] | None = None) -> int:
    """`python -m nyxgpt.release_candidate`: resolve (and optionally pin) the version.

    The publish workflow runs exactly this instead of re-implementing the
    guardrails and the version arithmetic in shell, so CI, the ceremony and
    the CLI can never disagree about what a channel publishes. Prints the
    resolved version to stdout -- or `SKIP` when a nightly has nothing to do;
    everything else goes to stderr so the caller can use `$(...)` directly.
    """
    parser = argparse.ArgumentParser(
        prog="python -m nyxgpt.release_candidate",
        description="Resolve the next PyPI version for a release branch and channel.",
    )
    parser.add_argument(
        "--branch", required=True, help="Git ref the build is cut from, e.g. v3.0.0"
    )
    parser.add_argument(
        "--channel",
        default="rc",
        help=f"Publish channel: {', '.join(CHANNELS)} (default: rc)",
    )
    parser.add_argument(
        "--number",
        default="",
        help="Explicit rc/dev number (blank = next unused, resolved from PyPI)",
    )
    parser.add_argument(
        "--run-number",
        default="",
        help="GITHUB_RUN_NUMBER, used to keep each nightly dev version unique",
    )
    parser.add_argument(
        "--pin",
        default="",
        help="pyproject.toml to rewrite in place with the resolved version",
    )
    parser.add_argument(
        "--head-sha",
        default="",
        help="Commit being built; with --skip-if-unchanged, compared against the last nightly",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="owner/name used to look up the last successful nightly (GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--exclude-run-id", default="", help="Run id to ignore in that lookup (GITHUB_RUN_ID)"
    )
    parser.add_argument(
        "--skip-if-unchanged",
        action="store_true",
        help="dev channel: print SKIP when the tip is unchanged since the last nightly",
    )
    parser.add_argument(
        "--tags-at-head",
        default="",
        help="stable channel: tags pointing at the built commit (the release tag must be one)",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"stable channel: must be '{STABLE_CONFIRMATION}' (only the ceremony passes it)",
    )
    parser.add_argument("--json", action="store_true", help="Print the full plan as JSON to stderr")
    args = parser.parse_args(argv)

    try:
        channel = _check_channel(args.channel)
        run_number = _optional_int(args.run_number, "--run-number")
        requested = _optional_int(args.number, "--number")
        report = plan(args.branch, channel, run_number=run_number)
    except ReleaseCandidateError as exc:
        return _fail(str(exc))

    if args.json:
        print(json.dumps(report, indent=2), file=sys.stderr)

    if not report["publishable"]:
        print("release-publish: refusing to publish:", file=sys.stderr)
        for blocker in report["blockers"]:
            print(f"  - {blocker}", file=sys.stderr)
        return 1

    if channel == "stable":
        # Ceremony-exclusive guardrails: the tag the ceremony creates in
        # Phase 1 must be at the built commit, and only the ceremony knows
        # the confirmation token. Together they mean a stable version cannot
        # be published by dispatching this workflow by hand.
        if args.confirm.strip() != STABLE_CONFIRMATION:
            return _fail(
                "the stable channel is ceremony-only -- run scripts/release_ceremony.sh, "
                f"which passes the '{STABLE_CONFIRMATION}' confirmation"
            )
        tags = [tag for tag in re.split(r"[\s,]+", args.tags_at_head) if tag]
        if report["release"] not in tags:
            return _fail(
                f"tag {report['release']} is not at the built commit (tags: "
                f"{', '.join(tags) or 'none'}) -- the ceremony tags the release before it "
                "delegates the publish"
            )

    if channel == "dev" and args.skip_if_unchanged:
        if not args.head_sha.strip():
            return _fail("--skip-if-unchanged needs --head-sha")
        try:
            last = fetch_last_scheduled_sha(
                args.repo,
                token=os.environ.get("GITHUB_TOKEN", ""),
                exclude_run_id=args.exclude_run_id,
            )
        except ReleaseCandidateError as exc:
            return _fail(str(exc))
        if last and last == args.head_sha.strip():
            print(
                f"release-publish: {args.branch} is still at {last[:12]}, unchanged since the "
                "last successful nightly -- nothing to publish.",
                file=sys.stderr,
            )
            print(SKIP_SENTINEL)
            return 0

    if requested is not None:
        try:
            version = channel_version(channel, report["release"], requested)
        except ReleaseCandidateError as exc:
            return _fail(str(exc))
        if version in report["published_rcs"] or version in report["published_dev_builds"]:
            return _fail(
                f"{version} is already on PyPI -- PyPI never accepts a re-upload. "
                f"Next unused is {report['version']}."
            )
    else:
        version = report["version"]

    if args.pin:
        path = Path(args.pin)
        try:
            pinned = pin_version(path.read_text(encoding="utf-8"), version)
        except (OSError, ReleaseCandidateError) as exc:
            return _fail(str(exc))
        path.write_text(pinned, encoding="utf-8")
        print(f"release-publish: pinned {path} to {version}", file=sys.stderr)

    print(version)
    return 0


def _optional_int(raw: str | int | None, flag: str) -> int | None:
    """Parse a CLI number that may legitimately be blank."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if not text.isdigit():
        raise ReleaseCandidateError(f"{flag} must be a number, got {text!r}")
    return int(text)


def _fail(message: str) -> int:
    """Report a refusal on stderr and return the exit code `main` should use."""
    print(f"release-publish: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised by the publish workflow
    raise SystemExit(main())
