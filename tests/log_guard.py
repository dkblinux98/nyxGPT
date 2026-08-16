"""Attribution helper for the production-log-dir guard in `tests/conftest.py`.

Kept out of `conftest.py` itself so it can be imported and unit-tested
directly (`tests/unit/test_conftest_log_guard.py`); a bare `import conftest`
from a test module resolves to the nearest conftest, not the root one.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import psutil


def externally_held_log_files(log_dir: Path) -> set[str]:
    """Real paths under `log_dir` held open by processes outside this pytest run.

    The #3443 guard compares the production log dir before and after the
    session and fails if anything changed. That is exactly right when pytest is
    the only writer -- but on a machine where the stack is actually installed
    and running natively (launchd on macOS, systemd on Linux since #3508), the
    service supervisors hold `nyxgpt-api.err.log`, `nyxgpt-web.log`,
    `cassandra.log` and `ollama-native.log` open and append to them for the
    whole session. Those writes are not pytest's doing, and failing the suite
    for them would mean the suite cannot pass on any machine that is running
    the product it tests.

    Ownership is the discriminator: a file an *external* process holds open was
    written by that process, not by the code under test. The current process
    and its children are deliberately not excluded -- a test-spawned subprocess
    writing to the real log dir is precisely the regression #3443 is about.

    Processes belonging to other users raise AccessDenied and are skipped; the
    effect is that such a file stays in the guard's scope (fail closed).
    """
    ours = {os.getpid()}
    with contextlib.suppress(psutil.Error):  # pragma: no cover - our own process vanished
        ours |= {child.pid for child in psutil.Process().children(recursive=True)}

    root = os.path.realpath(log_dir)
    held: set[str] = set()
    for proc in psutil.process_iter():
        if proc.pid in ours:
            continue
        try:
            open_paths = [f.path for f in proc.open_files()]
        except (psutil.Error, OSError):
            # AccessDenied / NoSuchProcess / platform quirk -- treat as "not
            # externally held" so the guard keeps its teeth.
            continue
        for path in open_paths:
            real = os.path.realpath(path)
            if real != root and os.path.commonpath([root, real]) == root:
                held.add(real)
    return held
