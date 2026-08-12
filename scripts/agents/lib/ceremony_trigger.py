#!/usr/bin/env python3
"""Release-ceremony trigger decision (#3730).

Owner decision 2026-08-12: the owner moving the RELEASE TRACKING ISSUE to
`For Release` is the human sign-off for the release. From that signal the
ceremony runs end-to-end unattended -- master merge, tag, GitHub Release,
`stable` publish (#3727 pipeline), stable tap stamp and retirement of that
line's rc formulas. This supersedes the old "master merges are
human-controlled" rule: the move IS the human control point.

Because the ceremony is irreversible (it fast-forwards master, cuts a tag
and publishes to PyPI), the trigger is deliberately narrow. This module
holds that decision on its own so the guardrails can be tested without
touching GitHub:

  * it fires for the RELEASE ISSUE only -- any other issue reaching
    `For Release` is ordinary accepted work;
  * it fires on the TRANSITION only -- a ceremony marker already on the
    issue means this ceremony has run (or is running), and every later
    poll is a no-op. The watcher polls, so without this a completed
    release would re-run the ceremony every 15 minutes;
  * it needs a parseable vX.Y.Z version in the issue title, because the
    ceremony is version-driven. No version -> conservative stop.
"""

from __future__ import annotations

import json
import re
import sys

VERSION_RE = re.compile(r"v(\d+\.\d+\.\d+)")

# Stamped on the release issue when a ceremony starts. Its presence is what
# makes the trigger edge-triggered rather than level-triggered.
CEREMONY_MARKER_PREFIX = "<!-- nyxgpt-release-ceremony:"


def marker_for(version: str) -> str:
    return f"{CEREMONY_MARKER_PREFIX}{version} -->"


def decide(state: dict) -> dict:
    """state keys:
    issue                the issue that changed (int)
    release_issue        RELEASE_ISSUE_NUMBER (int or None)
    status               that issue's current Status field value
    for_release_status   the "For Release" option name
    title                the issue title (carries the version)
    already_fired        a ceremony marker for this version exists
    """
    issue = state.get("issue")
    release_issue = state.get("release_issue")
    status = (state.get("status") or "").strip()
    for_release = (state.get("for_release_status") or "For Release").strip()

    if release_issue in (None, "", 0):
        return {"fire": False, "reason": "no release issue configured", "version": None}
    if str(issue) != str(release_issue):
        return {
            "fire": False,
            "reason": f"#{issue} is not the release tracking issue (#{release_issue})",
            "version": None,
        }
    if status != for_release:
        return {
            "fire": False,
            "reason": f"release issue status is '{status or '(none)'}', not '{for_release}'",
            "version": None,
        }

    match = VERSION_RE.search(state.get("title") or "")
    if not match:
        return {
            "fire": False,
            "reason": "release issue title carries no vX.Y.Z version -- conservative stop",
            "version": None,
        }
    version = match.group(1)

    if state.get("already_fired"):
        return {
            "fire": False,
            "reason": f"ceremony for {version} already started (marker present)",
            "version": version,
        }

    return {"fire": True, "reason": f"release issue moved to '{for_release}'", "version": version}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: ceremony_trigger.py {decide|marker <version>}", file=sys.stderr)
        return 2
    if argv[0] == "decide":
        print(json.dumps(decide(json.load(sys.stdin))))
        return 0
    if argv[0] == "marker":
        if len(argv) < 2:
            print("usage: ceremony_trigger.py marker <version>", file=sys.stderr)
            return 2
        print(marker_for(argv[1]))
        return 0
    print(f"unknown subcommand: {argv[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
