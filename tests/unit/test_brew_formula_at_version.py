"""Self-heal can act on a versioned Homebrew formula (#4043).

The barrier that keeps a crafted service name out of a `brew services`
argv -- `[A-Za-z0-9][A-Za-z0-9._-]*`, inlined at each sink since CodeQL #4 --
had no `@`. Homebrew's versioned-formula syntax does, and the candidate
channel is built on it: an rc install registers `nyxgpt-api@3.0.0rc`, #3853
made the probe resolve to that real name, and #3861 made it qualify the name
with its owning tap. Every one of those is correct; the validator they then
handed the name to was not, so it refused:

    'nyxgpt-api'                            is_safe=True
    'nyxgpt-api@3.0.0rc'                    is_safe=False
    'dkblinux98/nyxgpt/nyxgpt-api@3.0.0rc'  is_safe=False

The consequence found in acceptance testing was the pending-restart notice's
Restart button doing nothing on an rc install -- but the refusal is on the
shared heal path, so the *watchdog* could not heal `api` or `web` on any rc
install either, which is the channel release candidates are accepted on (the
D-030(b) shape: the one flow used to accept a release is the one the
machinery structurally cannot run in). `nyxgpt ops restart api` worked on the
same machine, because `ops` does not route through this check.

What these tests pin, in order: the real names are admitted; the wider class
still refuses everything the narrower one did; the inline copy of the pattern
has not drifted from the shared one; and the restart actually reaches `brew`.
"""

from __future__ import annotations

import inspect
import re
import subprocess

import pytest

from nyxgpt import brew_services, self_heal

pytestmark = pytest.mark.unit


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


# --- The names the candidate channel actually produces --------------------


@pytest.mark.parametrize(
    "spec",
    [
        "nyxgpt-api",
        "nyxgpt-api@3.0.0rc",
        "nyxgpt-web@3.0.0rc",
        "python@3.12",
        "dkblinux98/nyxgpt/nyxgpt-api@3.0.0rc",
        "dkblinux98/nyxgpt-local/nyxgpt-web@3.0.0rc",
    ],
)
def test_versioned_formula_names_are_accepted(spec):
    """The three forms from the issue, plus the `python@3.12` precedent."""
    assert brew_services.is_safe_formula_spec(spec), f"legitimate formula refused: {spec!r}"


@pytest.mark.parametrize(
    "bad",
    [
        # Everything CodeQL #4 closed, which admitting `@` must not reopen.
        "--privileged",
        "-rf",
        "a b",
        "x;rm -rf /",
        "../etc",
        "$(id)",
        "`id`",
        "a|b",
        "a&b",
        "a>b",
        "nyxgpt-api@3.0.0rc; rm -rf /",
        "nyxgpt-api@$(id)",
        "",
        # Shapes the `@` group deliberately does not admit.
        "@3.0.0rc",
        "nyxgpt-api@",
        "nyxgpt-api@3.0@0rc",
        "@",
        # Still exactly one or three slash-separated segments, never two/four.
        "nyxgpt/nyxgpt-api@3.0.0rc",
        "a/b/c/d",
        "dkblinux98/nyxgpt/../../etc/passwd",
    ],
)
def test_unsafe_specs_are_still_refused(bad):
    assert not brew_services.is_safe_formula_spec(bad), f"unsafe spec admitted: {bad!r}"


def test_the_inline_barrier_matches_the_shared_pattern():
    """`_restart_brew_service`'s inline literal must stay identical to the authority.

    The guard cannot *call* `brew_services.SEGMENT_PATTERN`: CodeQL's
    barrier-guard analysis recognizes the `re.fullmatch(r"...", x)` call form
    and not a module constant or a helper, which is why the literal is
    duplicated at all. A duplicate with no test is how one of the two gets
    widened and the other does not -- the exact failure mode #3861's first
    fix had, where ops' sites were repaired and self_heal's was not.
    """
    source = inspect.getsource(self_heal._restart_brew_service)
    assert f'r"{brew_services.SEGMENT_PATTERN}"' in source, (
        "the inline barrier in self_heal._restart_brew_service has drifted from "
        f"brew_services.SEGMENT_PATTERN ({brew_services.SEGMENT_PATTERN!r})"
    )


def test_the_segment_pattern_anchors_and_excludes_metacharacters():
    """A direct read of the pattern, independent of either caller.

    `fullmatch` plus a leading-alphanumeric requirement is what makes the
    value unusable as a CLI flag; this asserts the class itself rather than
    trusting the two functions that use it.
    """
    segment = re.compile(brew_services.SEGMENT_PATTERN)
    assert segment.fullmatch("nyxgpt-api@3.0.0rc")
    for char in " \t\n;|&$`<>()'\"\\*?[]{}!#~^%,:=+/":
        assert not segment.fullmatch(f"nyxgpt-api{char}3"), f"metacharacter admitted: {char!r}"
        assert not segment.fullmatch(f"{char}nyxgpt-api"), f"leading {char!r} admitted"


# --- End to end: the heal reaches brew with the versioned name ------------


def test_heal_of_a_candidate_api_reaches_brew_services_restart(monkeypatch):
    """The acceptance-failure repro: heal `api` on an rc install.

    Before the fix this returned `ok=False` with "Refused to act on invalid
    service name: 'nyxgpt-api@3.0.0rc'" and issued no command at all -- the
    silent no-op the Restart button reported as `{"status": "running"}`.
    """
    monkeypatch.setattr(self_heal, "_which", lambda tool: f"/opt/homebrew/bin/{tool}")
    monkeypatch.setattr(self_heal, "_is_linux", lambda: False)
    monkeypatch.setattr(self_heal, "_dev_mode_active", lambda: False)
    monkeypatch.setattr(
        self_heal,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api@3.0.0rc": "started", "nyxgpt-web@3.0.0rc": "started"},
    )
    # No Cellar in this test env, so `formula_spec` leaves the name bare --
    # the dual-tap qualification of the same name is covered by
    # test_brew_dual_tap_qualification.py.
    seen: list[list[str]] = []
    monkeypatch.setattr(self_heal, "_run", lambda cmd, **_k: seen.append(cmd) or _cp())

    result = self_heal.restart_native_component("api")

    assert result.ok is True, result.message
    assert seen == [["brew", "services", "restart", "nyxgpt-api@3.0.0rc"]]


def test_a_crafted_versioned_name_still_never_reaches_a_subprocess(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda tool: "/opt/homebrew/bin/brew")
    seen: list[list[str]] = []
    monkeypatch.setattr(self_heal, "_run", lambda cmd, **_k: seen.append(cmd) or _cp())

    result = self_heal._restart_brew_service("nyxgpt-api@3.0.0rc; rm -rf /")

    assert result.ok is False
    assert "invalid service name" in result.message
    assert seen == [], "an unsafe name reached a subprocess"
