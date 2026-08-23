"""Cross-process tracking of pending, restart-required config changes (#3407, #3806).

A config key whose value is read once at process start cannot be hot-applied:
the saved value and the *running* value diverge the moment it is written, and
stay diverged until that process restarts. `config_wizard`'s activation
classification (`FieldSpec.restart_components`) says which keys those are and
which services they belong to; this module records the resulting divergence so
the product can *tell the user about it* rather than letting the two values
drift apart silently (the `[auth] api_key` 401 wall, #3806).

Two properties the #3407 in-memory version did not have, both required by
#3806:

* **On disk, not in memory.** The writer and the reader are frequently
  different processes -- `nyxgpt secrets setup` writes `[auth] api_key` from a
  short-lived CLI process while the API serves the Admin Dashboard that has to
  show the notice. An in-memory flag in the API process is invisible to the
  CLI and vice versa. The state lives at `~/.nyxGPT/pending-restart.json`
  (override with `$NYXGPT_PENDING_RESTART_PATH`), so *one behavior, two
  surfaces* is a shared file rather than two implementations. It also means
  the notice survives an API restart, which is what "the user can leave and
  come back and still see it" requires when the pending component is `web`.

* **Reverts resolve themselves.** Each pending key records the value that was
  on disk *before* the change -- which is the value the running process
  actually loaded. If a later save puts that exact value back, saved and
  running agree again and the key is dropped from the pending set without a
  restart (`reconcile_saved`). Without this, reverting a mistake would leave a
  permanent, un-clearable "restart required" banner.

Pending entries are cleared when the restart actually happens, and *which
process* does the clearing depends on whether the restart is survivable by the
actor that asked for it:

* **A component that is not this process** (`web`, `ollama`, `cassandra`) is
  cleared by whoever drove the restart and lived through it: `app.py`'s
  `_do_restart_required` (the dashboard/wizard button) and `ops.restart()`
  (the `nyxgpt ops restart <target>` CLI) both call `clear_pending` once the
  restart reports success.

* **A component clearing its own flag cannot work** (#3806, second round).
  Restarting `api` from inside the API process kills the thread that was going
  to run `clear_pending`, so the flag survived a *successful* restart and the
  dashboard sat on "Saved -- but not yet in effect" forever. The completion
  signal for a self-restart therefore comes from the only actor that can
  always observe that the restart really happened: **the process that came
  back**. `app.py`'s lifespan startup calls `clear_started("api", ...)` --
  see that function for why a newly started process is entitled to retire its
  own pending keys, and for what it deliberately does not retire.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Guards this process's own read-modify-write cycles. Cross-process safety
# rests on the atomic `os.replace` in `_write`: the writers here are a human
# saving a wizard page and a human running a CLI setup command, so a lost
# update needs two humans writing within milliseconds of each other, and the
# consequence would be a missing line in an advisory notice. A lock file
# would buy nothing for that risk and would add a stale-lock failure mode to
# a path that must never block a config save.
_lock = threading.Lock()

_DEFAULT_STATE_PATH = Path.home() / ".nyxGPT" / "pending-restart.json"


def state_path() -> Path:
    """Return the pending-restart state file's path.

    `$NYXGPT_PENDING_RESTART_PATH` overrides the default so tests (and a
    non-default `--config` layout) can redirect it. Read per call rather than
    cached at import time: `app.py` and the CLI import this module at very
    different points in a process's life.
    """
    override = os.environ.get("NYXGPT_PENDING_RESTART_PATH")
    return Path(override) if override else _DEFAULT_STATE_PATH


def _read() -> dict[str, dict[str, Any]]:
    """Load the state file, returning `{}` for a missing or unreadable one.

    A corrupt or unreadable state file must never break a config save or an
    API response -- this is advisory UI state, so it degrades to "nothing
    pending" rather than raising.
    """
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("Ignoring unreadable pending-restart state at %s", path, exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for component, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        keys = entry.get("keys")
        if not isinstance(keys, dict):
            continue
        out[str(component)] = {
            "keys": {str(k): str(v) for k, v in keys.items()},
            "since": float(entry.get("since") or 0.0),
        }
    return out


def _write(state: dict[str, dict[str, Any]]) -> None:
    """Persist `state`, atomically, creating the parent directory if needed.

    Written via a temp file + `os.replace` so a reader never sees a half-written
    file. Failure is logged and swallowed for the same reason `_read` is
    forgiving: a config save must not fail because an advisory notice could not
    be recorded.
    """
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".pending-restart-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError:
        log.warning("Could not persist pending-restart state to %s", path, exc_info=True)


def mark_pending(component: str, changes: dict[str, str]) -> None:
    """Record that `component` needs a restart for `changes` to take effect.

    Args:
        component: A `nyxgpt ops restart` target (`api`, `web`, ...).
        changes: Maps `section.key` to the value that was on disk *before*
            this save -- i.e. the value the running process loaded and is
            still using. `reconcile_saved` compares against it to detect a
            revert.

    Merges with anything already pending for `component`, and deliberately
    keeps the *first* recorded previous value for a key: the running value
    does not change until the restart happens, so a second edit before the
    restart must not overwrite it with the intermediate saved value.
    """
    if not changes:
        return
    with _lock:
        state = _read()
        entry = state.setdefault(component, {"keys": {}, "since": time.time()})
        for full_key, previous in changes.items():
            entry["keys"].setdefault(full_key, previous)
        _write(state)


def reconcile_saved(component: str, saved: dict[str, str]) -> None:
    """Drop pending keys in `component` that `saved` has restored to the running value.

    `saved` maps `section.key` to the value just written. A key whose new
    value equals the previous value recorded by `mark_pending` is back in
    agreement with the running process, so no restart is owed for it any
    more. A component left with no pending keys is removed entirely.
    """
    if not saved:
        return
    with _lock:
        state = _read()
        entry = state.get(component)
        if not entry:
            return
        for full_key, value in saved.items():
            if entry["keys"].get(full_key) == value:
                entry["keys"].pop(full_key, None)
        if not entry["keys"]:
            state.pop(component, None)
        _write(state)


def clear_pending(component: str) -> None:
    """Clear `component`'s pending-restart flag, e.g. after it's been restarted."""
    with _lock:
        state = _read()
        if state.pop(component, None) is not None:
            _write(state)


def clear_started(component: str, keys: Iterable[str]) -> list[str]:
    """Retire `keys` for `component` because a fresh `component` process has just started.

    This is the completion signal for a restart the restarting process cannot
    report on. `clear_pending` is called by whoever *drove* a restart, which
    works only while that actor outlives it -- and it does not for `api`,
    where the restart kills the API process holding the callback (#3806: the
    flag survived a successful restart and the notice never cleared).

    A process that has just started has, by definition, read the
    configuration currently on disk: for every key it consumes at startup the
    saved value and the running value are the same value, so nothing is owed.
    That is the same reasoning `ops.restart()` uses when it clears after a
    successful restart -- but asserted by the one actor that cannot be wrong
    about whether the restart happened, because it *is* the restart having
    happened. A restart that failed produces no new process, so nothing
    clears and the notice stands (which is the property #3806 needs kept: the
    banner must go away on success, not always).

    Args:
        component: The `nyxgpt ops restart` target that just started.
        keys: The `section.key` entries this process is entitled to retire --
            the caller's snapshot of what was pending *before* it read its
            configuration. Anything marked after that snapshot is left
            standing: the caller may have loaded the file before that write
            landed, and a notice that stays up one restart too long is honest
            where one that clears too early is not.

    Returns:
        The keys actually retired, for the caller to log.
    """
    wanted = set(keys)
    if not wanted:
        return []
    with _lock:
        state = _read()
        entry = state.get(component)
        if not entry:
            return []
        cleared = sorted(wanted & set(entry["keys"]))
        if not cleared:
            return []
        for full_key in cleared:
            entry["keys"].pop(full_key, None)
        if not entry["keys"]:
            state.pop(component, None)
        _write(state)
    return cleared


def snapshot() -> dict[str, dict[str, Any]]:
    """Return the current pending-restart state, JSON-serializable.

    Shape: `{component: {"keys": [...sorted section.key...], "since": epoch}}`.
    The recorded previous values are *not* exposed -- several classified keys
    are secrets (`[auth] api_key`), and the UI only needs to know which
    settings are waiting, never what they used to be.
    """
    with _lock:
        state = _read()
    return {
        component: {"keys": sorted(entry["keys"]), "since": entry["since"]}
        for component, entry in state.items()
        if entry["keys"]
    }


def pending_components() -> list[str]:
    """Return the sorted list of components currently awaiting a restart."""
    return sorted(snapshot())


def restart_command(components: list[str]) -> str:
    """Return the wrapped command that clears `components`' pending restarts.

    Always a `nyxgpt ops restart` invocation -- never a raw `brew services` /
    `docker` / `kubectl` command (the operational-wrapping rule). Shared by
    the CLI notice and the docs so the two cannot drift.
    """
    targets = sorted(set(components))
    if len(targets) == 1:
        return f"nyxgpt ops restart {targets[0]}"
    return " && ".join(f"nyxgpt ops restart {t}" for t in targets)


def reset() -> None:
    """Delete all pending-restart state. Test-only."""
    with _lock:
        state_path().unlink(missing_ok=True)
