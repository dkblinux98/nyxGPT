"""The single PyPI publish core: the rc and stable channels (#3727).

Acceptance testing of the repo-less paths installs nyxGPT **from PyPI**
(`pip install nyxgpt`, the EC2 user-data bootstrap, `nyxgpt cloud deploy`),
so without a published artifact it can only ever exercise the *last stable
release* -- never the release-branch tip carrying the acceptance-failure
fixes merged during testing. This module is the version arithmetic and the
guardrails behind the fix, and it is the *only* one: per the owner decision
of 2026-08-11, as revised on 2026-08-12 (#3735), there is one
channel-parameterized build/publish pipeline
(`.github/workflows/release-publish-pypi.yml`) with exactly two channels --
`rc` and `stable`, one per acceptance candidate and one per release. There
is no scheduled publish of any kind: what gets published is always a
deliberate act.

* **rc** -- a dispatch cuts a deliberate `3.0.0rcN`, by hand or from the
  sprint autopilot when the sprint reaches agentic-work-complete (#3729,
  `autopilot_rc_preflight`). This is the channel with a step past PyPI:
  it also cuts a GitHub *prerelease* carrying the service tarballs and stamps
  `nyxgpt-api@3.0.0rc` / `nyxgpt-web@3.0.0rc` into the Homebrew tap, so a
  candidate is installable on macOS the same repo-less way a release is --
  without ever writing the stable formulas (`rc_formulas`). It no-ops on an
  unchanged tip, which is what lets the autopilot fire at every park without
  cutting duplicate candidates (`select_last_published_sha`);
* **stable** -- `scripts/release_ceremony.sh` Phase 2 delegates to the same
  workflow. The ceremony keeps only its ceremony-exclusive steps (master
  merge, tag, Homebrew tap, GitHub Release, sign-off); an rc build never
  runs any of them.

PEP 440 orders these `3.0.0aN < 3.0.0bN < 3.0.0rcN < 3.0.0`, so a candidate
can never shadow the release.

Four properties matter, and each is a function here rather than a line of
YAML, so they are testable and cannot drift between the workflow, the CLI,
the ceremony and the dashboard:

* **Pre-release unless the channel is stable.** What rc publishes is
  `3.0.0rcN`, which pip's resolver excludes from `pip install nyxgpt` unless
  the user asks for it (`--pre`) or pins it exactly. Cutting a candidate
  therefore cannot change what a normal install resolves to -- see
  `is_prerelease`.
* **Release branch only.** A build is the *release line's* tip, so
  publishing is refused from anywhere but a `v<X.Y.Z>` branch whose number
  matches the version declared in `pyproject.toml`
  (`release_branch_version`, `plan`). A `3.0.0rc4` cut from a feature branch
  would claim to be the v3.0.0 line and would not be.
* **Never reuses a version.** `next_rc_number` reads what PyPI already
  serves; numbers only ever go up, and PyPI itself refuses a re-upload of a
  version it already has (versions are immutable).
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

#: The channels the pipeline understands, weakest version first. Two only
#: (owner decision 2026-08-12, #3735): a candidate per acceptance round and
#: the release itself. The nightly `dev` channel is retired -- nothing is
#: published on a schedule.
CHANNELS = ("rc", "stable")

#: How each channel reaches the pipeline: every publish is a deliberate
#: `workflow_dispatch` (the sprint autopilot's rc, #3729, and the ceremony's
#: stable). There is no `schedule` trigger. Used to find the last successful
#: build of a channel when deciding whether the tip has moved.
CHANNEL_TRIGGER_EVENT = {
    "rc": "workflow_dispatch",
    "stable": "workflow_dispatch",
}

#: The remote Homebrew tap an rc publish stamps. `brew tap dkblinux98/nyxgpt`
#: resolves to the `dkblinux98/homebrew-nyxgpt` repository -- the `homebrew-`
#: prefix is Homebrew's own tap-naming convention.
HOMEBREW_TAP = "dkblinux98/nyxgpt"

#: The services the tap carries a formula for, stable and candidate alike.
TAP_SERVICES = ("nyxgpt-api", "nyxgpt-web")

#: Printed by `main` instead of a version when a publish has nothing to do
#: (the release-branch tip has not moved since the last published candidate).
SKIP_SENTINEL = "SKIP"

#: The stable channel refuses to resolve a version without this token, which
#: only `scripts/release_ceremony.sh` passes.
STABLE_CONFIRMATION = "ceremony"

DOCS_ANCHOR = "docs/cloud.md#pypi-publishing-rc-and-stable"

# `v3.0.0` -- the release-branch naming this repo uses (CLAUDE.md: master is
# releases only; work merges to the active release branch).
_RELEASE_BRANCH_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")

_RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# PEP 440's normalized pre-release spellings, which are what `python -m build`
# emits and what `pip install nyxgpt==3.0.0rc1` resolves.
_RC_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+)rc(\d+)$")

# Any PEP 440 pre-/dev-release suffix: alpha, beta, release candidate, dev.
# `.devN` is still recognized although nothing publishes one any more (#3735
# retired the nightly): PyPI keeps every version it was ever given, so a
# leftover dev build must still read as a pre-release rather than a release.
_PRERELEASE_RE = re.compile(r"(?:a|b|rc)\d+$|\.dev\d+$")

# `version = "3.0.0"` inside pyproject.toml's `[project]` table.
_VERSION_ASSIGNMENT_RE = re.compile(r'^version\s*=\s*".*"\s*$')

# The title a *publishing* run carries, as the publish workflow's
# `run-name:` renders it ("publish rc from v3.0.0"). Every publish is a
# `workflow_dispatch`, so this title is the only record of which channel a
# run built. A dry run renders "publish rc [dry run] from ..." and uploads
# nothing, so it deliberately fails this match -- counting it as a published
# candidate would suppress the next real one.
_RUN_TITLE_TEMPLATE = r"^publish\s+{channel}\s+from\s+\S+"

_RC_RUN_TITLE_RE = re.compile(_RUN_TITLE_TEMPLATE.format(channel="rc"), re.IGNORECASE)


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


def is_prerelease(version: str) -> bool:
    """True when pip's resolver treats `version` as a pre-release.

    The load-bearing property of the rc channel: while this is true of
    everything it publishes, `pip install nyxgpt` cannot resolve to one
    (PEP 440 -- pre-releases are excluded unless requested).
    """
    return bool(_PRERELEASE_RE.search(version.strip()))


def release_line(version: str) -> str:
    """Return the release a version belongs to: `3.0.0rc2` -> `3.0.0`."""
    parsed = parse_rc_version(version)
    return parsed[0] if parsed else version.strip()


def rc_formula_name(service: str, release: str) -> str:
    """The Homebrew formula the rc channel stamps for `service` on `release`.

    `("nyxgpt-api", "3.0.0")` -> `nyxgpt-api@3.0.0rc` (owner decision
    2026-08-12, #3735). Deliberately NOT `nyxgpt-api`: Homebrew has no
    pre-release semantics, so the only way to keep `brew install nyxgpt-api`
    resolving to the latest *stable* release is for the candidate to be a
    separate formula. The name carries the release line, so a machine on
    `nyxgpt-api@3.0.0rc` can never silently cross to the next line's
    candidates -- those are a differently named formula -- and retiring a
    shipped line's candidates is a matter of deleting formulas by name. It
    loads because a digit follows the `@`: brew's `Formulary.class_s` turns
    `nyxgpt-api@3.0.0rc` into `NyxgptApiAT300rc`.
    """
    _require_release_version(release)
    return f"{service}@{release_line(release.strip())}rc"


def rc_formulas(release: str) -> tuple[str, ...]:
    """Both formulas an rc publish of `release` stamps into the tap.

    They declare `conflicts_with` their stable counterparts, so a machine can
    only ever be on one channel at a time.
    """
    return tuple(rc_formula_name(service, release) for service in TAP_SERVICES)


def rc_version(release: str, number: int) -> str:
    """Compose the PEP 440 pre-release version for `release` + RC `number`."""
    _require_release_version(release)
    if number < 1:
        raise ReleaseCandidateError(f"RC number must be 1 or greater, got {number}")
    return f"{release.strip()}rc{number}"


def _require_release_version(release: str) -> None:
    """Raise unless `release` is a bare `X.Y.Z` release version."""
    if not _RELEASE_VERSION_RE.match(release.strip()):
        raise ReleaseCandidateError(
            f"{release!r} is not a release version -- expected X.Y.Z, e.g. 3.0.0"
        )


def channel_version(channel: str, release: str, number: int | None = None) -> str:
    """The version `channel` publishes for `release`.

    `stable` is the bare release version and refuses a number -- there is no
    such thing as "release 3.0.0, build 4", so silently dropping one would
    let `--number 4` look honoured while publishing something else. `rc`
    requires one (the callers below resolve it from what PyPI serves).
    """
    resolved = _check_channel(channel)
    if resolved == "stable":
        _require_release_version(release)
        if number is not None:
            raise ReleaseCandidateError(
                f"The stable channel publishes {release.strip()} itself and takes no build "
                f"number (got {number})"
            )
        return release.strip()
    if number is None:
        raise ReleaseCandidateError(f"The {resolved} channel needs a build number")
    return rc_version(release, number)


def published_rc_numbers(release: str, published: list[str] | tuple[str, ...]) -> list[int]:
    """RC numbers already published for `release`, ascending."""
    return _published_numbers(parse_rc_version, release, published)


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


def next_rc_version(release: str, published: list[str] | tuple[str, ...]) -> str:
    """The next unused RC version for `release`, e.g. `3.0.0rc3`."""
    return rc_version(release, next_rc_number(release, published))


def next_channel_version(
    channel: str,
    release: str,
    published: list[str] | tuple[str, ...],
) -> str:
    """The version `channel` would publish next for `release`."""
    resolved = _check_channel(channel)
    if resolved == "stable":
        return channel_version("stable", release)
    return next_rc_version(release, published)


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

    The publish workflow builds an rc artifact by rewriting this one line in
    the checkout it already has -- the pre-release version is never
    committed, so the release branch keeps declaring the stable version it is
    heading for. Only the `[project]` table's assignment is touched; a
    `version = "..."` under some other table (a tool config, say) is left
    alone.
    """
    recognized = parse_rc_version(version) or _RELEASE_VERSION_RE.match(version)
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


# --- Tip-unchanged detection (the rc autopilot) --------------------------


def select_last_published_sha(
    payload: dict[str, Any],
    channel: str = "rc",
    exclude_run_id: str | int = "",
) -> str:
    """The commit the last successful publish of `channel` built.

    The tip-unchanged condition behind the rc no-op, an owner requirement:
    the sprint autopilot dispatches a candidate every time the sprint parks
    at agentic-work-complete (#3729), so repeated observations of the same
    parked state must not cut duplicate candidates. Only a tip that moved
    since the last published rc earns a new rcN.

    GitHub's own run history is the state store -- nothing has to be written
    back to the repo or to PyPI to remember what was last built. Both
    channels arrive as a `workflow_dispatch`, so the run's title is what
    records which one it was (see `_RC_RUN_TITLE_RE` -- the publish
    workflow's `run-name:` is load-bearing for this and is shape-tested).

    `exclude_run_id` drops the caller's own run, which is already listed as
    in-progress-then-success by the time a later run reads this.
    """
    resolved = _check_channel(channel)
    runs = payload.get("workflow_runs") or []
    candidates = [
        run
        for run in runs
        if run_published_channel(run, resolved)
        and run.get("conclusion") == "success"
        and str(run.get("id", "")) != str(exclude_run_id or "")
        and run.get("head_sha")
    ]
    if not candidates:
        return ""
    newest = max(candidates, key=lambda run: str(run.get("created_at") or ""))
    return str(newest["head_sha"])


def run_published_channel(run: dict[str, Any], channel: str) -> bool:
    """Whether a workflow run of the publish pipeline is a `channel` build.

    Both channels arrive as a `workflow_dispatch`, so a run is identified by
    the title the workflow's `run-name:` renders ("publish rc from v3.0.0").
    A dry run titles itself "publish rc [dry run] from ..." and uploads
    nothing, so it deliberately does NOT match -- treating it as a published
    candidate would suppress the next real one.
    """
    resolved = _check_channel(channel)
    trigger = CHANNEL_TRIGGER_EVENT.get(resolved, "")
    if not trigger or run.get("event") != trigger:
        return False
    title = str(run.get("display_title") or run.get("name") or "").strip()
    title_re = re.compile(_RUN_TITLE_TEMPLATE.format(channel=re.escape(resolved)), re.IGNORECASE)
    return bool(title_re.match(title))


def fetch_last_published_sha(
    repo: str,
    channel: str = "rc",
    workflow_file: str = PUBLISH_WORKFLOW_FILE,
    token: str = "",
    exclude_run_id: str | int = "",
    timeout: float = 30.0,
) -> str:
    """Ask GitHub for the commit the last successful `channel` build published.

    Returns "" when there is no such run (the first candidate of a line
    always publishes). Raises on a transport or protocol failure: silently
    treating an API outage as "nothing published yet" would upload a
    redundant build every time the API is flaky.
    """
    import httpx

    event = CHANNEL_TRIGGER_EVENT[_check_channel(channel)]
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}"
        f"/runs?event={event}&status=success&per_page=30"
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
        # The workflow has no run history yet (first publish on a new file).
        return ""
    if response.status_code != 200:
        raise ReleaseCandidateError(
            f"GitHub returned HTTP {response.status_code} listing runs of {workflow_file}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ReleaseCandidateError(f"GitHub returned invalid JSON for {url}: {exc}") from exc
    return select_last_published_sha(payload, channel, exclude_run_id)


# --- The sprint autopilot's rc preflight (#3729) --------------------------


def autopilot_rc_preflight(
    branch: str,
    published: list[str] | tuple[str, ...],
    runs: dict[str, Any],
    head_sha: str = "",
) -> dict[str, Any]:
    """What dispatching an rc for `head_sha` would publish -- decided without
    dispatching anything.

    The sprint autopilot parks when the sprint reaches agentic-work-complete
    and dispatches a candidate for that acceptance round (#3729). It calls
    this first so the park note can name what the owner should install,
    instead of pointing at a run whose version nobody knows yet. The
    decision is the same tip comparison the pipeline itself enforces
    (`select_last_published_sha`), so a preflight that says "dispatch" and a
    pipeline that then SKIPs cannot disagree about the same tip.

    Inputs are handed in rather than fetched: the caller (the agent shell
    library) already has `gh`/`curl` authenticated, and this keeps the
    decision a pure, testable function with no network of its own.

    Returns `dispatch` (should the autopilot fire?), `version` (the rcN it
    would cut, or the already-published rcN when it would not), `last_sha`
    and a human `reason` for the park note.
    """
    release = release_branch_version(branch)
    if release is None:
        raise ReleaseCandidateError(
            f"Refusing to plan a candidate from {branch!r} -- release branches (v3.0.0) only."
        )
    versions = [str(v) for v in published]
    last_sha = select_last_published_sha(runs or {}, "rc")
    head = (head_sha or "").strip()
    numbers = published_rc_numbers(release, versions)
    current = rc_version(release, numbers[-1]) if numbers else ""

    if last_sha and head and last_sha == head:
        return {
            "dispatch": False,
            "version": current,
            "last_sha": last_sha,
            "release": release,
            "reason": (
                f"{branch} is unchanged since the last published candidate "
                f"({last_sha[:12]}) -- no new candidate is needed."
            ),
        }
    return {
        "dispatch": True,
        "version": next_rc_version(release, versions),
        "last_sha": last_sha,
        "release": release,
        "reason": (
            f"{branch} has moved since the last published candidate"
            if last_sha
            else f"no candidate has been published from {branch} yet"
        ),
    }


# --- The plan the CLI, the API, the ceremony and the dashboard all render --


def plan(
    branch: str,
    channel: str = "rc",
    published: list[str] | tuple[str, ...] | None = None,
    pyproject_path: Path | None = None,
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
    candidate = next_channel_version(resolved_channel, release, published)
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
        "next_rc_number": next_rc_number(release, published),
        "next_rc_version": next_rc_version(release, published),
        # The tap formulas this line's candidates are installed as -- named
        # for the release (#3735), so every surface can render the exact
        # `brew install` line instead of hard-coding one that goes stale.
        "rc_formulas": list(rc_formulas(release)),
        # What *this* channel would publish -- the same field whichever
        # entry point asked, so the dashboard and the workflow agree.
        "version": candidate,
        # True for rc by construction, false for stable; surfaced so the
        # dashboard can state the guarantee rather than assert it in
        # hand-written prose.
        "is_prerelease": is_prerelease(candidate),
        "workflow": PUBLISH_WORKFLOW_FILE,
        "pypi_lookup_error": lookup_error,
        "publishable": not blockers,
        "blockers": blockers,
        "commands": {
            **_channel_commands(resolved_channel, release),
            "install": f"pip install nyxgpt=={candidate}",
            "user_data": f"nyxgpt cloud user-data --os linux --version {candidate}",
            "deploy": f"nyxgpt cloud deploy --version {candidate}",
        },
        "guardrails": _guardrails(resolved_channel, candidate),
        "docs": DOCS_ANCHOR,
    }


def _channel_commands(channel: str, release: str) -> dict[str, str]:
    """How an operator plans and cuts a build on `channel`.

    Every surface renders these verbatim, so they have to be commands that
    actually run. `nyxgpt release publish --channel stable` is not one: the
    CLI's `--channel` offers `rc` only, because a release is cut by the
    ceremony (which is what supplies the tag and the confirmation the stable
    channel demands). Rendering it anyway would hand the operator a line that
    fails with an argparse error.
    """
    if channel == "stable":
        return {
            "plan": "scripts/release_ceremony.sh --dry-run",
            "publish": "scripts/release_ceremony.sh",
        }
    return {
        "plan": f"nyxgpt release publish --channel {channel}",
        "publish": f"nyxgpt release publish --channel {channel} --publish",
        # An rc is a pre-release that is also installable with brew: its
        # publish stamps this line's `@<release>rc` formulas into the tap.
        "brew": f"brew tap {HOMEBREW_TAP} && brew install {' '.join(rc_formulas(release))}",
    }


def _guardrails(channel: str, candidate: str) -> list[str]:
    """The guardrails in force for `channel`, in the words the surfaces render."""
    shared = [
        "Dispatch trigger only: the workflow has no schedule, push, tag or release trigger, "
        "so nothing is ever published without someone (or the autopilot) asking for it.",
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
    return (
        shared
        + [
            f"Pre-release version ({candidate}): `pip install nyxgpt` never resolves to it.",
            f"Separate Homebrew formulas ({', '.join(rc_formulas(release_line(candidate)))}): "
            "the stable nyxgpt-api/nyxgpt-web formulas are never written by an rc publish, so "
            "`brew install nyxgpt-api` still resolves to the latest stable release. The name "
            "carries the release line, so a candidate never crosses to the next one.",
            "GitHub prerelease, never 'latest': the rc's release is marked as a prerelease, "
            "so it does not become the repository's latest release and does not trigger the "
            "stable artifact workflow (which fires on `released`, not `prereleased`).",
            "Cut automatically when the sprint reaches agentic-work-complete: the sprint "
            "autopilot dispatches this channel (and only this channel) at that park, and "
            "names the candidate on the release tracking issue.",
            "No duplicate candidates: an rc whose release-branch tip has not moved since the "
            "last published candidate publishes nothing, so re-observing the same parked "
            "state cuts no second rcN.",
            "Acceptance only: an rc build is never announced, and never runs a ceremony "
            "step (master merge, release tag, stable Homebrew formulas, GitHub Release).",
        ]
    )


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
    print(f"  next version       {report['version']}")
    print(f"  tap formulas       {', '.join(report['rc_formulas'])}")
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
    if report["commands"].get("brew"):
        print(f"  {report['commands']['brew']}")
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
            if requested in report["published_rcs"]:
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
            if channel == "rc" and number is None:
                # Same guard, same reason to say it out loud (#3729): the rc
                # channel no-ops on an unchanged tip so the autopilot can
                # dispatch a candidate at every park without cutting
                # duplicates. An explicit --number overrides it.
                print(
                    "Note: an rc run publishes nothing when the tip has not moved since the "
                    "last published candidate -- that commit already has one. Pass a number "
                    "to override."
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
    resolved version to stdout -- or `SKIP` when an rc has nothing to do (the
    tip has not moved since the last published candidate); everything else
    goes to stderr so the caller can use `$(...)` directly.
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
        help="Explicit rc number (blank = next unused, resolved from PyPI)",
    )
    parser.add_argument(
        "--pin",
        default="",
        help="pyproject.toml to rewrite in place with the resolved version",
    )
    parser.add_argument(
        "--head-sha",
        default="",
        help=(
            "Commit being built; with --skip-if-unchanged, compared against the last "
            "published candidate"
        ),
    )
    parser.add_argument(
        "--repo",
        default="",
        help="owner/name used to look up the last published candidate (GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--exclude-run-id", default="", help="Run id to ignore in that lookup (GITHUB_RUN_ID)"
    )
    parser.add_argument(
        "--skip-if-unchanged",
        action="store_true",
        help="rc channel: print SKIP when the tip is unchanged since the last published candidate",
    )
    parser.add_argument(
        "--autopilot-preflight",
        action="store_true",
        help=(
            "Print, as JSON, whether the sprint autopilot should dispatch an rc for "
            "--head-sha and which version that would be. Reads "
            '{"published": [...], "runs": {...}} on stdin; makes no network calls.'
        ),
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

    if args.autopilot_preflight:
        # The autopilot's naming question, not a build: it needs the answer
        # *before* dispatching, and it runs in the agent workflows where the
        # package's HTTP dependency may not be installed -- so the caller
        # hands the two API reads in on stdin and this stays pure (#3729).
        return _autopilot_preflight_cli(args)

    try:
        channel = _check_channel(args.channel)
        requested = _optional_int(args.number, "--number")
        report = plan(args.branch, channel)
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

    if args.skip_if_unchanged:
        # The tip guard, for the one channel that can fire repeatedly on its
        # own: the sprint autopilot's rc dispatch at agentic-work-complete
        # (#3729). Stable is ceremony-only and cannot repeat, so it has no
        # such guard.
        if channel == "stable":
            return _fail(
                "--skip-if-unchanged is the rc tip guard -- the stable channel is "
                "ceremony-only and publishes exactly what the ceremony tagged"
            )
        if requested is not None:
            # An explicitly numbered build is deliberate intent (the operator
            # named the version), so it overrides the guard here exactly as
            # the workflow drops the flag for it -- the two must agree, or
            # `--number` would look honoured and publish nothing.
            print(
                f"release-publish: --number {requested} was given explicitly -- "
                "ignoring the unchanged-tip guard.",
                file=sys.stderr,
            )
        elif not args.head_sha.strip():
            return _fail("--skip-if-unchanged needs --head-sha")
        else:
            try:
                last = fetch_last_published_sha(
                    args.repo,
                    channel,
                    token=os.environ.get("GITHUB_TOKEN", ""),
                    exclude_run_id=args.exclude_run_id,
                )
            except ReleaseCandidateError as exc:
                return _fail(str(exc))
            if last and last == args.head_sha.strip():
                print(
                    f"release-publish: {args.branch} is still at {last[:12]}, unchanged since the "
                    "last published candidate -- nothing to publish.",
                    file=sys.stderr,
                )
                print(SKIP_SENTINEL)
                return 0

    if requested is not None:
        try:
            version = channel_version(channel, report["release"], requested)
        except ReleaseCandidateError as exc:
            return _fail(str(exc))
        if version in report["published_rcs"]:
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


def _autopilot_preflight_cli(args: argparse.Namespace) -> int:
    """`--autopilot-preflight`: answer the sprint autopilot's rc question.

    Stdin carries what the caller already fetched -- `published` (PyPI's
    version list) and `runs` (this workflow's successful dispatch history) --
    and stdout carries the JSON decision the park note renders. Everything
    the answer depends on is an input, so the agent shell library needs no
    Python HTTP stack and this stays a unit-testable function.
    """
    try:
        raw = sys.stdin.read()
    except OSError as exc:  # pragma: no cover - stdin is always readable in practice
        return _fail(f"could not read the preflight payload on stdin: {exc}")
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError as exc:
        return _fail(f"the preflight payload on stdin is not JSON: {exc}")
    if not isinstance(payload, dict):
        return _fail("the preflight payload on stdin must be a JSON object")

    published = payload.get("published") or []
    runs = payload.get("runs") or {}
    if not isinstance(published, list) or not isinstance(runs, dict):
        return _fail('the preflight payload needs "published" (list) and "runs" (object)')

    try:
        decision = autopilot_rc_preflight(args.branch, published, runs, args.head_sha)
    except ReleaseCandidateError as exc:
        return _fail(str(exc))
    print(json.dumps(decision))
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
