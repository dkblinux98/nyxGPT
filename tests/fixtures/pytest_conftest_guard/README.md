# `pytest_conftest_guard` fault-injection fixtures (#4192)

Three one-job workflows: the defect, and the two legitimate repairs. They are
real files rather than heredocs inside a `run:` block for one reason — the
guard reads `run:` bodies **textually**, heredoc content included, so a plant
written inline would be scanned as part of the job that plants it. The
`pip install -e .` in `installs-the-package.yml` would then have exempted the
whole planting job and hidden any real violation in it; the
`sigpipe_pipeline_guard` plants dodge the same trap by assembling `-q` from a
shell variable.

The guard's default scan covers `.github/workflows/*.yml` only, so nothing
here is ever picked up by an ordinary run — these are reached only when passed
as explicit arguments.

Both halves of the executed evidence point at these same files
(`pipefail-pipeline-smoke.yml`'s `pytest-jobs-can-collect` job and
`tests/unit/test_pytest_conftest_guard.py`), so the plant cannot drift apart
from the assertion about it.

| file | shape | guard must |
|---|---|---|
| `cannot-collect.yml` | minimal install, target under `tests/`, no `--noconftest` | **reject** |
| `skips-the-conftest.yml` | minimal install, `--noconftest` on a continuation line | accept |
| `installs-the-package.yml` | `pip install -e .`, conftest loaded deliberately | accept |
