# The unit suite's expected floor

`pytest tests/unit` is expected to be **green**, and to produce the **same
result on every run of the same tree**. This page exists so that "the expected
floor" is a fact somebody wrote down rather than folklore passed between
sessions — before #4020 agents were told to expect "~2-3 failures" while
observing anywhere from 5 to 67.

Measured 2026-08-22 on `origin/v3.0.0` + the #4020 fix, macOS 15.6 / arm64 /
Python 3.11, five consecutive runs on a developer machine with a real
`~/.nyxGPT/config.ini` present and the stack installed: **1 failure, the same
one every time** (see below). CI, whose runners have neither, expects **0**.

## How to run it

```
PYTHONPATH=<worktree>/src python -m pytest tests/unit
```

You do **not** need to protect `~/.nyxGPT` from the suite, and you should not
try: since #4020 the suite gives itself a private `$HOME` in a temp directory
(`tests/home_sandbox.py`) before any `nyxgpt` module is imported, so the
operator's config, secrets, install-mode markers, Terraform state and logs are
out of reach rather than backed up and restored. A run that is killed halfway
leaves nothing of yours to repair.

## The floor, by name

Exactly one test is expected to fail on some developer machines and no CI
runner. If you see anything else, it is yours — or it is a regression.

### `tests/unit/test_resources_packaging.py::test_resources_resolve_from_installed_non_editable_wheel`

Fails on a checkout whose Terraform working directories contain local state.
`src/nyxgpt/resources/terraform` is a symlink to the repo's `terraform/` tree,
so a `terraform apply` run from the checkout leaves `terraform.tfvars` (and
friends) where the wheel build picks them up, and the test's own assertion
fires:

```
local Terraform working files leaked into the wheel:
  .../src/nyxgpt/resources/terraform/aws/mac/terraform.tfvars
  .../src/nyxgpt/resources/terraform/aws/mac-release/terraform.tfvars
```

That is the test working correctly — those files must never ship — but it is
reporting the state of *your machine*, not of the diff under test. It passes on
a clean checkout and in CI. Clean the untracked files under `terraform/aws/*/`
if you want a green local run.

## What is deliberately *not* on this list

**Order-dependent failures.** There are none, and there must not be: the suite
has no random ordering plugin, and after #4020 no shared mutable state outside
the process. Two runs of the same tree that disagree about which tests failed
are a defect in their own right, not an environment quirk — that is what the
`determinism` job in `.github/workflows/ci-tests.yml` fails on.

**Failures that "only happen when I run the suite twice at once."** Concurrent
pytest sessions used to fight over one `~/.nyxGPT/config.ini`; #4020 removed
the shared file. If concurrency ever appears to matter again, read
`tests/home_sandbox.py` first — the measurement that found it, and the reason
the fix is shaped the way it is, are recorded there.

## Keeping this page honest

If you fix one of these, delete its section in the same PR. If you find a new
environment-dependent failure, add it here **with the mechanism**, not just the
node id — a name with no cause is how a real regression gets waved through as
"one of the expected ones".
