"""Pin the release line the publish-pipeline unit tests plan against (#4167).

`release_candidate.plan()` compares the branch it is given against whatever
`pyproject.toml` declares, resolved through `declared_version()` ->
`_checkout_pyproject()` -- the *live* checkout. The publish-pipeline tests are
written against one line: `PUBLISHED` encodes that line's PyPI history, and
the expected next version (`3.0.0rc3`, `3.0.0rc2`) is arithmetic on it. Left
to read the checkout, all of that holds only while the repo happens to sit on
the same line the fixtures name.

It does not. The v3.0.1 release ceremony (`ae64a2cd`) bumped `project.version`
to `3.0.1`, and 28 tests across `test_release_candidate.py` and
`test_release_candidate_endpoint.py` went red on the release branch itself,
every one of them on the same blocker:

    branch v3.0.0 names release 3.0.0, but pyproject.toml declares 3.0.1
      -- the build would misreport which line it came from

Rewriting the literals to `3.0.1` would clear it and re-arm it for v3.0.2.
The declared version is a fixture like `PUBLISHED` is, so these tests supply
it instead of inheriting it, and a ceremony bump stops being able to break
them. The production guardrail is untouched: `declared_version(path)` still
honours an explicit path, so the `--pin` tests still prove `main()` rewrites
the pyproject it is handed.
"""

from __future__ import annotations

import pytest

from nyxgpt import release_candidate as rc

#: The release line the publish-pipeline fixtures are written against. It is
#: deliberately NOT the repo's current version -- see the module docstring.
TEST_RELEASE_LINE = "3.0.0"

_PINNED_PYPROJECT = f'[project]\nname = "nyxGPT"\nversion = "{TEST_RELEASE_LINE}"\n'


def pin_declared_release_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> str:
    """Make `declared_version()` report `TEST_RELEASE_LINE`, not the checkout's.

    Patches `_checkout_pyproject` -- the one place `declared_version()` goes
    looking for the working tree -- at a path of its own, so a test that
    writes its own `pyproject.toml` into `tmp_path` cannot collide with it.
    """
    pinned = tmp_path_factory.mktemp("declared_release_line") / "pyproject.toml"
    pinned.write_text(_PINNED_PYPROJECT, encoding="utf-8")
    monkeypatch.setattr(rc, "_checkout_pyproject", lambda: pinned)
    return TEST_RELEASE_LINE
