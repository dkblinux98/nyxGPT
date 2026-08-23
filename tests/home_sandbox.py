"""The suite runs in a private HOME of its own, never the operator's (#4020).

Importing this module moves `$HOME` to a fresh per-process temp directory,
seeded with the suite's own `.nyxGPT/config.ini` (`TEST_CONFIG_TEXT`). It must
be imported before anything imports `nyxgpt`, because `config.DEFAULT_CONFIG_PATH`,
`ops.NYXGPT_HOME`, `cloud.NYXGPT_HOME`, `cloud_infra.NYXGPT_HOME`,
`install_mode.NYXGPT_HOME`, `restart_state._DEFAULT_STATE_PATH` and
`workflow_log_store.DEFAULT_DB_PATH` all resolve `Path.home()` at *module* level
-- an import-time capture no later swap can reach. `tests/conftest.py` imports
this module first for that reason, and `_assert_not_too_late` below fails loudly
rather than silently leaving the operator's home in play if that order is ever
broken.

Why a whole HOME rather than a config-file swap
-----------------------------------------------
The previous design (#3443, then #3983/#4006) wrote `TEST_CONFIG_TEXT` into the
operator's real `~/.nyxGPT/config.ini` for the session, kept the original in a
sibling `config.ini.pytest-bak`, and restored it at teardown. That is safe for
exactly one pytest process at a time, and this repository is worked by many
concurrent agent sessions. Measured 2026-08-22 on the v3.0.0 head, with a
second session started 45 seconds into the first:

  - the first session's `config.ini` flipped from the suite's config to the
    operator's 13 seconds after the second session started -- the second
    session's fixture found the first's `config.ini.pytest-bak`, took it for a
    crashed run's leftovers, and "recovered" it;
  - of the first session's 6959 tests, the 2181 that ran before that moment
    failed 0, and the 4789 that ran after it failed 65.

That is the whole of the 5/28/53/67-failure spread reported in #4020: the
failure count is just a measure of how early some other session happened to
start. An audit hook over `open`/`rename`/`unlink` of the config path proved
the writer was never in-process -- the only in-process writes all session were
the fixture's own two.

The same fixed-path backup destroyed the operator's config twice on 2026-08-22
(`~/.nyxGPT/config.ini.clobbered-2`, 711 bytes, is `TEST_CONFIG_TEXT` plus a
`[logging] dir` pointing into a `pytest-of-darlabaker` tmp dir). The interleave:
session A's teardown unlinks the shared backup path while session B is still
running, B is then killed before its own teardown, and the next session
snapshots B's leftover *test* config as "the original" and restores that
forever -- file and backup gone together.

Making the backup cleverer (a unique per-run path, an atomic claim) would fix
the destruction but not the flakes: two sessions still fight over one
`config.ini`. A private HOME per process removes the shared resource instead of
arbitrating it, so both go away at once -- and it generalises, because
`~/.nyxGPT` holds far more operator state than the config file (secrets/,
install-mode markers, terraform/, docker-images/, logs/), each of which has
needed its own isolation fixture as some test reached it (#3789, #3834, #3835,
#3947). Nothing in here needs to know which ones.

The sandbox is deliberately *not* removed on a hard kill: it is a temp
directory holding only synthetic state, so leaking one costs nothing, whereas
any cleanup path that runs against the real home is the defect this replaces.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

from session_config import TEST_CONFIG_TEXT

# The machine's real home, captured before the swap. Tests that need to talk
# about the operator's actual machine (the production-log-dir guard in
# `tests/conftest.py`) read it from here; nothing writes through it.
REAL_HOME = Path(os.path.expanduser("~")).resolve()

# Set in the environment as well as exported here, so a subprocess a test spawns
# (and any second import of this module) can tell an installed sandbox from a
# missing one.
_ENV_MARKER = "NYXGPT_TEST_HOME"

# Modules whose `Path.home()` capture happens at import time. Listing them by
# name makes the failure message below actionable instead of abstract.
_HOME_CAPTURING_MODULES = (
    "nyxgpt.config",
    "nyxgpt.ops",
    "nyxgpt.cloud",
    "nyxgpt.cloud_infra",
    "nyxgpt.install_mode",
    "nyxgpt.restart_state",
    "nyxgpt.workflow_log_store",
)


def _assert_not_too_late() -> None:
    """Refuse to pretend the sandbox works when it cannot.

    If any home-capturing module is already imported, its module-level constant
    still points into the operator's real `~/.nyxGPT` and no `$HOME` swap can
    move it. Installing the sandbox anyway would produce a suite that *looks*
    isolated while writing to the developer's machine -- strictly worse than
    the state this replaces, because the guard tests would pass.
    """
    already = [name for name in _HOME_CAPTURING_MODULES if name in sys.modules]
    if already:
        raise RuntimeError(
            "tests/home_sandbox.py was imported after "
            f"{already} -- those modules resolved `Path.home()` at import time, "
            "so the suite would run against the operator's real ~/.nyxGPT. "
            "Import this module (or `tests/conftest.py`, which does) before any "
            "`nyxgpt` import."
        )


def _seed(home: Path) -> None:
    """Write the suite's own `~/.nyxGPT/config.ini` into the sandbox.

    Seeded here rather than in a fixture so that the very first `nyxgpt` import
    -- which happens while `tests/conftest.py` is still executing its own
    imports, before any fixture runs -- already sees the suite's config.
    """
    nyxgpt_dir = home / ".nyxGPT"
    nyxgpt_dir.mkdir(parents=True, exist_ok=True)
    (nyxgpt_dir / "config.ini").write_text(TEST_CONFIG_TEXT, encoding="utf-8")


def _install() -> Path:
    existing = os.environ.get(_ENV_MARKER)
    if existing:
        # A pytest session a test spawned as a subprocess: it inherited both
        # `$HOME` and this marker, so it is already inside the parent's
        # sandbox. Adopt it rather than making a second one -- and in
        # particular do not register an `atexit` removal, because the
        # directory belongs to the parent, which is still using it.
        os.environ["HOME"] = existing
        return Path(existing)

    _assert_not_too_late()

    # `.resolve()` matters: on macOS `mkdtemp` hands back `/tmp/...` while
    # `/tmp` is a symlink to `/private/tmp`, and product code that resolves its
    # paths would then disagree with a test comparing against `Path.home()`
    # -- two tests (`test_get_vectorstore_dir_default`,
    # `test_config_get_sessions_dir_accepts_home_relative_path`) fail on
    # exactly that mismatch and on nothing else.
    home = Path(tempfile.mkdtemp(prefix="nyxgpt-test-home-")).resolve()
    _seed(home)

    os.environ["HOME"] = str(home)
    os.environ[_ENV_MARKER] = str(home)
    # `Path.home()` consults `$HOME` on every call, but `os.path.expanduser`
    # caches nothing while `pwd` would answer the real home -- assert rather
    # than assume, since everything below depends on it.
    assert Path.home() == home, f"HOME swap did not take: Path.home() is {Path.home()}"

    atexit.register(shutil.rmtree, str(home), True)
    return home


SANDBOX_HOME = _install()
