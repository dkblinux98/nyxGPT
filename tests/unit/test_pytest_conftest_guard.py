"""A workflow job that runs pytest must be able to collect what it runs (#4192).

The defect this file exists for was a *required check going red on a green
commit*, which is this issue's whole class arriving through the gate the
previous round added to enforce it.
`pipefail-pipeline-smoke.yml`'s `pipeline-readers-drain` installed
``pyyaml pytest`` and ran ``pytest tests/unit/test_sigpipe_pipeline_guard.py``.
Any target under ``tests/`` loads the *root* ``tests/conftest.py``, whose
import closure reaches ``tests/log_guard.py`` and therefore ``psutil``, so the
job died at collection::

    ImportError while loading conftest 'tests/conftest.py'
    E   ModuleNotFoundError: No module named 'psutil'
    ##[error]Process completed with exit code 4

pytest exit **4** is a usage error, not a test failure: the check was red while
every assertion it exists for was untested, and the head was fine. It reached a
hand-back (18:19Z) and then an owner escalation (18:28Z).

Four measurements, because each answers a different question:

1. **The closure, derived rather than restated** -- that
   `conftest_closure` really does reach ``psutil`` two files deep and really
   does notice the first-party import. This is the "one source" the rule
   rests on (CLAUDE.md, ledger D-066): 15 jobs make this decision, and none of
   them gets to carry its own copy of the dependency list.
2. **`pip install -e .` genuinely suffices** -- every third-party module in
   the closure is a declared runtime dependency of the package. If someone
   adds an import to `tests/conftest.py` that `pyproject.toml` does not
   declare, this fails and names it, instead of 15 jobs finding out one red
   check at a time.
3. **Both directions, against the planted fixtures** -- the defect's own shape
   must be rejected and *both* legitimate repairs must be accepted. A guard
   that flags ``--noconftest`` would be the vacuous-gate defect from the other
   side: it would have 14 false positives on this tree.
4. **The tree as it stands** -- no job violates the rule, and the guard is
   actually looking at the surface it claims (the real workflow directory,
   with the real job count).

Direction 3's *reject* half matters most. A guard asserted only against
compliant input passes on a tree where it has stopped parsing anything at all,
which is how the construct it polices reached the release branch
(#3753's lesson, ledger D-006).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "agents" / "lib" / "pytest_conftest_guard.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pytest_conftest_guard"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

sys.path.insert(0, str(GUARD.parent))

import pytest_conftest_guard as guard  # noqa: E402

# ---------------------------------------------------------------------------
# 1. The closure, derived from the conftest rather than restated here
# ---------------------------------------------------------------------------


def test_the_closure_reaches_psutil_two_files_deep() -> None:
    """`psutil` is the module the 15 workflow authors could not have guessed.

    Nothing in `tests/conftest.py` mentions it: it arrives through
    ``from log_guard import externally_held_log_files``, and `log_guard`
    imports it. A guard that only read the conftest's own import list would
    miss the one requirement that actually broke a check.
    """
    closure = guard.conftest_closure()
    assert "psutil" in closure.third_party
    assert Path(REPO_ROOT / "tests" / "log_guard.py") in closure.files


def test_the_closure_notices_the_first_party_import() -> None:
    """`needs_package` is why a bare module list can never satisfy the rule."""
    closure = guard.conftest_closure()
    assert closure.needs_package is True


def test_the_closure_drops_the_standard_library() -> None:
    """Otherwise the guard would demand `pip install pathlib` and be ignored."""
    closure = guard.conftest_closure()
    assert not {"logging", "os", "socket", "configparser", "pathlib"} & closure.third_party


def test_the_closure_is_computed_from_a_real_conftest(tmp_path: Path) -> None:
    """Fault injection on the derivation itself: a new import must show up.

    Without this, `conftest_closure` could be returning a hardcoded set and
    every assertion above would still pass.
    """
    (tmp_path / "helper.py").write_text("import tomli_w\n", encoding="utf-8")
    conftest = tmp_path / "conftest.py"
    conftest.write_text("import os\nfrom helper import x\nimport httpx\n", encoding="utf-8")

    closure = guard.conftest_closure(conftest)

    assert closure.third_party == {"httpx", "tomli_w"}
    assert closure.needs_package is False
    assert "os" not in closure.third_party


# ---------------------------------------------------------------------------
# 2. The link the rule rests on: `pip install -e .` really is enough
# ---------------------------------------------------------------------------

#: Import name -> distribution name, for the few that differ. Only the
#: closure's modules need an entry, and the test below fails loudly rather
#: than silently skipping anything missing from it.
_DIST_NAMES = {"yaml": "pyyaml"}


def test_the_editable_install_covers_the_closure() -> None:
    """Every third-party module the conftest needs is a declared runtime dep.

    This is the one assertion that makes "install the package" a *correct*
    repair rather than a lucky one. `psutil` is `pyproject.toml`'s line 24
    today; if the conftest grows an import that the package does not declare,
    `pip install -e .` would stop being sufficient and the six jobs relying
    on it would go red one at a time. This says so here instead.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        re.split(r"[<>=!~\[; ]", dep, maxsplit=1)[0].strip().lower().replace("_", "-")
        for dep in pyproject["project"]["dependencies"]
    }

    closure = guard.conftest_closure()
    # pytest itself is installed by every job explicitly; it is a dev tool,
    # not a runtime dependency, and demanding it in `[project.dependencies]`
    # would ship the test framework to users.
    needed = closure.third_party - {"pytest"}
    missing = {
        module
        for module in needed
        if _DIST_NAMES.get(module, module).lower().replace("_", "-") not in declared
    }

    assert not missing, (
        "tests/conftest.py's import closure needs "
        + ", ".join(sorted(missing))
        + ", which pyproject.toml does not declare as a runtime dependency -- so "
        "`pip install -e .` no longer makes the root conftest collectible and the "
        "jobs that rely on it will fail at collection with exit 4."
    )


# ---------------------------------------------------------------------------
# 3. Both directions, against the planted fixtures
# ---------------------------------------------------------------------------


def _run_guard(*args: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), *(str(a) for a in args)],
        capture_output=True,
        text=True,
    )


def test_the_guard_rejects_the_defects_own_shape() -> None:
    """The plant is the retired step verbatim; the guard must go red on it."""
    result = _run_guard(FIXTURES / "cannot-collect.yml")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot-collect.yml::cannot-collect" in result.stdout
    # The message has to name the fix, not just the sin.
    assert "--noconftest" in result.stdout
    assert "pip install -e ." in result.stdout
    # And the closure, so the reader learns what `psutil` has to do with it.
    assert "psutil" in result.stdout


@pytest.mark.parametrize(
    "fixture",
    ["skips-the-conftest.yml", "installs-the-package.yml"],
    ids=["noconftest", "editable-install"],
)
def test_the_guard_accepts_both_repairs(fixture: str) -> None:
    """Flagging either repair would mean 14 false positives on this tree."""
    result = _run_guard(FIXTURES / fixture)

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_continuation_line_flag_is_seen() -> None:
    """`--noconftest` wrapped onto line 2 is the shape every real job uses.

    A line-at-a-time scan reads the `pytest` line without its flags and
    reports all seven compliant smoke jobs as violations -- a guard that
    cries wolf gets switched off, which is where this issue started.
    """
    call = "python -m pytest -q --noconftest tests/unit/test_x.py"
    wrapped = "python -m pytest -q --noconftest \\\n  tests/unit/test_x.py"

    assert guard.loads_root_conftest(call) is False
    assert [guard.loads_root_conftest(c) for c in guard.pytest_calls(wrapped)] == [False]


@pytest.mark.parametrize(
    ("call", "loads"),
    [
        ("python -m pytest tests/unit/test_x.py -v", True),
        ("pytest tests/unit -q", True),
        # No target at all: `pyproject.toml` sets testpaths = ["tests"].
        ("python -m pytest -q", True),
        # A variable target is not guessed at.
        ("python -m pytest $PYTEST_TARGETS -v", True),
        # Flags that consume their argument must not be read as targets --
        # `-c /dev/null` and `--deselect tests/...` are both real forms here.
        ("python -m pytest --noconftest -c /dev/null tests/unit/test_x.py", False),
        (
            "python -m pytest --noconftest --rootdir . --deselect tests/unit/t.py::C tests/unit/t.py",
            False,
        ),
        # Not a test path, so the root conftest never loads.
        ("python -m pytest scripts/thing_test.py", False),
    ],
)
def test_loads_root_conftest_decides_correctly(call: str, loads: bool) -> None:
    assert guard.loads_root_conftest(call) is loads


@pytest.mark.parametrize(
    ("blob", "installs"),
    [
        ("pip install -e .", True),
        ('pip install -e ".[dev]"', True),
        ("python -m pip install --quiet -e .", True),
        ("python -m pip install --quiet pyyaml pytest", False),
        ("python -m pip install --quiet pytest pyyaml psutil", False),
    ],
)
def test_installs_package_recognises_the_real_spellings(blob: str, installs: bool) -> None:
    """All five spellings are lifted from jobs in this repository."""
    assert guard.installs_package(blob) is installs


def test_a_pip_install_of_pytest_is_not_a_pytest_call() -> None:
    """Otherwise every job's install step would be reported as a violation."""
    assert guard.pytest_calls("python -m pip install --quiet pytest pyyaml") == []


def test_a_commented_out_pytest_call_is_not_a_call() -> None:
    """These workflows carry long comment blocks that quote their own commands."""
    assert guard.pytest_calls("# python -m pytest tests/unit/test_x.py") == []


# ---------------------------------------------------------------------------
# 4. The tree as it stands, and that the guard is looking at it
# ---------------------------------------------------------------------------


def test_no_job_in_the_tree_violates_the_rule() -> None:
    result = _run_guard()

    assert result.returncode == 0, (
        "a workflow job runs pytest over the suite without installing the "
        "package, so it will die at collection with exit 4:\n" + result.stdout
    )


def test_the_guard_covers_the_surface_it_claims() -> None:
    """Vacuity check: the default scan must reach the real workflows.

    `test_no_job_in_the_tree_violates_the_rule` passes trivially if the glob
    resolves to nothing -- which is exactly what a refactor of `REPO_ROOT`
    would do, silently.
    """
    scanned = guard.workflow_paths([])

    assert len(scanned) > 50, f"only {len(scanned)} workflow(s) scanned"
    assert WORKFLOW_DIR / "pipefail-pipeline-smoke.yml" in scanned
    assert WORKFLOW_DIR / "ci-tests.yml" in scanned


def test_every_pytest_running_job_is_accounted_for() -> None:
    """The population the rule is about is real, and the guard parses it.

    If `pytest_calls` silently stopped matching, the clean run above would
    mean nothing. This counts the jobs the guard *finds* pytest in and pins
    that each resolves to one of the two legitimate answers.
    """
    found: dict[str, bool] = {}
    for path in guard.workflow_paths([]):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            continue
        for job_id, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            blob = "\n".join(
                str(step.get("run") or "")
                for step in (job.get("steps") or [])
                if isinstance(step, dict)
            )
            calls = guard.pytest_calls(blob)
            if not calls:
                continue
            found[f"{path.name}::{job_id}"] = guard.installs_package(blob) or all(
                not guard.loads_root_conftest(call) for call in calls
            )

    assert len(found) >= 14, f"the guard now finds pytest in only {len(found)} job(s): {found}"
    assert all(found.values()), {k: v for k, v in found.items() if not v}
