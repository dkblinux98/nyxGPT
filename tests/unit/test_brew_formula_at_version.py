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


@pytest.fixture
def macos(monkeypatch):
    """Pin the platform, because the restart command differs by it (#4043).

    Without this the heal tests below assert whatever the *runner* is, which
    is green on Linux CI and red on the macOS laptop the behaviour is about.
    """
    monkeypatch.setattr(self_heal, "_is_macos", lambda: True)
    monkeypatch.setattr(self_heal, "_is_linux", lambda: False)


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


@pytest.mark.parametrize(
    "guard",
    [self_heal._restart_brew_service, self_heal.kickstart_brew_service],
)
def test_the_inline_barrier_matches_the_shared_pattern(guard):
    """Every inline literal for a Homebrew name must stay identical to the authority.

    The guard cannot *call* `brew_services.SEGMENT_PATTERN`: CodeQL's
    barrier-guard analysis recognizes the `re.fullmatch(r"...", x)` call form
    and not a module constant or a helper, which is why the literal is
    duplicated at all. A duplicate with no test is how one of the two gets
    widened and the other does not -- the exact failure mode #3861's first
    fix had, where ops' sites were repaired and self_heal's was not.

    Parametrized rather than asserted on one function because #4043's second
    round added a *second* Homebrew sink (the launchd label built from the
    formula name), and a test that pins only the first would have let the new
    copy drift from day one.
    """
    source = inspect.getsource(guard)
    assert f'r"{brew_services.SEGMENT_PATTERN}"' in source, (
        f"the inline barrier in self_heal.{guard.__name__} has drifted from "
        f"brew_services.SEGMENT_PATTERN ({brew_services.SEGMENT_PATTERN!r})"
    )


def test_the_launchd_label_of_a_candidate_service_passes_the_barrier():
    """`sh.brew.nyxgpt-api@3.0.0rc` is the real label, and it has to be admitted.

    The narrow class would refuse it for the same reason it refused the
    formula name itself, which would have reintroduced #4043's defect one
    layer further in: a refusal instead of a restart, on the one channel
    acceptance testing uses.
    """
    segment = re.compile(brew_services.SEGMENT_PATTERN)
    labels = brew_services.launchd_labels("nyxgpt-api@3.0.0rc")
    assert labels == ["homebrew.mxcl.nyxgpt-api@3.0.0rc", "sh.brew.nyxgpt-api@3.0.0rc"]
    for label in labels:
        assert segment.fullmatch(label), f"a real Homebrew label is refused: {label!r}"


def test_ops_reads_the_label_prefixes_from_the_shared_module():
    """One definition of Homebrew's label schemes, not two (D-022).

    `ops.py` guessed these labels first and `self_heal.py` now needs the same
    guess; a second copy is how #3861 repaired the manual path and left the
    automated one broken.
    """
    from nyxgpt import ops

    assert ops._BREW_SERVICE_LABEL_PREFIXES is brew_services.LAUNCHD_LABEL_PREFIXES


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


def test_heal_of_a_candidate_api_reaches_the_real_service_name(monkeypatch, macos):
    """The acceptance-failure repro: heal `api` on an rc install.

    Before the first fix this returned `ok=False` with "Refused to act on
    invalid service name: 'nyxgpt-api@3.0.0rc'" and issued no command at all
    -- the silent no-op the Restart button reported as
    `{"status": "running"}`. What matters here is the *name*: the command the
    resolved name reaches is pinned by `test_api_self_restart.py`.
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
    assert seen, "the heal issued no command at all"
    assert any(
        "nyxgpt-api@3.0.0rc" in arg for arg in seen[0]
    ), f"the heal did not act on the candidate's own service name: {seen[0]}"


@pytest.mark.parametrize("on_macos", [True, False])
def test_a_crafted_versioned_name_still_never_reaches_a_subprocess(monkeypatch, on_macos):
    """Checked on both platforms, since each takes a different command path."""
    monkeypatch.setattr(self_heal, "_is_macos", lambda: on_macos)
    monkeypatch.setattr(self_heal, "_which", lambda tool: f"/opt/homebrew/bin/{tool}")
    seen: list[list[str]] = []
    monkeypatch.setattr(self_heal, "_run", lambda cmd, **_k: seen.append(cmd) or _cp())

    result = self_heal._restart_brew_service("nyxgpt-api@3.0.0rc; rm -rf /")

    assert result.ok is False
    assert "invalid service name" in result.message
    assert seen == [], "an unsafe name reached a subprocess"
