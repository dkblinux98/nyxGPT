"""Is the process that is *serving* running the build that is *installed*?

Every answer `nyxgpt ops` gave about a native api before #4133 was derived
from what is on disk: `_installed_keg_version` reads the Cellar,
`_native_service_version` reads package metadata, `brew services list`
reports what launchd has registered. None of them asks the process that is
actually answering on :8000 what it is executing, so the two can disagree
indefinitely and nothing says so.

They did, on the owner's Mac during the 3.0.0 acceptance round (#4133). A `brew
upgrade` took `nyxgpt-api@3.0.0rc` from rc14 to rc17; `nyxgpt ops install`
reported 56/56 steps `[OK]` including a service restart, and `nyxgpt ops
status` reported the keg's version -- while the process serving requests was
still a **python3.11** venv under `~/.nyxGPT/opt/nyxgpt-api/venv`, a path the
upgrade had already emptied. It survived only while it held the deleted files
open, and the next restart re-exec'd into a path that no longer existed:

    ModuleNotFoundError: No module named 'anyio._backends'

Why no reconcile step caught it: `ops._retire_previous_identity` finds
competing api services by asking the *service managers* -- `brew services
list` rows, and `DEV_LAUNCHD_LABELS` plists on disk. A process whose plist an
earlier install already removed while the process itself survived is in
neither population, so it is invisible to the retire sweep, keeps :8000, and
every disk-derived report describes the keg beside it instead.

The rule this module encodes, and the reason it is a module rather than a
branch in `ops.py`: **what a process is running is read from that process,
never asserted from what is installed next to it.** Two callers need it and
must not be able to disagree --

* the **api itself**, which answers `GET /api/v1/info` and the Infrastructure
  page's native card. Served from the api process, `local_runtime_build()` is
  self-description: the process that may be wrong is the one reporting, which
  is the most direct evidence there is.
* **`ops`**, in another process entirely (`nyxgpt ops install`, `status`,
  `doctor`), which reads that report over HTTP and compares it against the
  venv the *installed* service would exec.

It imports only `nyxgpt.version`, so `app.py` and `ops.py` can both have it
(the reason `brew_services.py` sits below both for the service-name question
-- D-022).

A note on what "mismatch" is allowed to mean here. `classify()` is given the
expected prefix by its caller and never derives one, because only the caller
knows whether the question applies at all: an api in a Compose container or a
Kubernetes Pod has a `sys.prefix` inside that image, and comparing it to a
host keg path would report drift on a correctly deployed stack. Callers that
cannot establish a native expectation pass `None` and get
`BUILD_UNDETERMINED`, which is reported as not-known rather than as either
answer -- the same distinction `infra_status`'s `probe_available` flags draw
(#3812, #4022).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nyxgpt.version import UNKNOWN_VERSION, running_version

__all__ = [
    "BUILD_MATCH",
    "BUILD_MISMATCH",
    "BUILD_NOT_APPLICABLE",
    "BUILD_UNDETERMINED",
    "IMAGE_SUBSTRATE_SUBJECTS",
    "BuildDrift",
    "BuildScope",
    "RuntimeBuild",
    "classify",
    "local_runtime_build",
    "native_build_scope",
    "same_tree",
]

#: The live process is executing the build that is installed.
BUILD_MATCH = "match"
#: The live process is executing some *other* build than the installed one --
#: #4133's state. Always actionable, and never reported as a version.
BUILD_MISMATCH = "mismatch"
#: The comparison could not be made (the api did not answer, or the installed
#: service's venv could not be located). Not an answer in either direction.
BUILD_UNDETERMINED = "undetermined"
#: There is no native api on this vantage point to compare -- a Compose
#: container, a Kubernetes Pod, or a host with no native install.
BUILD_NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class RuntimeBuild:
    """What one live nyxGPT process is executing, as that process reports it.

    `prefix` is the load-bearing field: `sys.prefix` is the venv root the
    interpreter was started from, which is the "process's own interpreter
    path" #4133's first acceptance criterion asks to be verified by. `version`
    is kept beside it deliberately -- a stale process reports a *plausible*
    version (it imports the metadata its own venv carries, or whatever is left
    of it), so the version is evidence about the process, never the test.

    `prefix_exists` is recorded at report time, by the process itself: a
    `False` here is the acute form of the defect -- the running interpreter's
    own venv has been deleted out from under it, so the process is alive only
    until something restarts it.
    """

    executable: str
    prefix: str
    python: str
    pid: int
    version: str
    prefix_exists: bool

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form, as sent on `/api/v1/info`."""
        return {
            "executable": self.executable,
            "prefix": self.prefix,
            "python": self.python,
            "pid": self.pid,
            "version": self.version,
            "prefix_exists": self.prefix_exists,
        }

    @classmethod
    def from_dict(cls, data: Any) -> RuntimeBuild | None:
        """Parse a `/api/v1/info` `runtime` block, or None if it carries no prefix.

        Tolerant on purpose: `ops` from one release reads this off an api
        process from another (that is the whole scenario -- a stale process
        from a *previous* build is what is being diagnosed), and a candidate
        predating this field answers with no `runtime` block at all. Missing
        or malformed reads as "cannot determine", which `classify()` renders
        as `BUILD_UNDETERMINED`; it must never read as a match.
        """
        if not isinstance(data, dict):
            return None
        prefix = str(data.get("prefix") or "").strip()
        if not prefix:
            return None
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        return cls(
            executable=str(data.get("executable") or ""),
            prefix=prefix,
            python=str(data.get("python") or ""),
            pid=pid,
            version=str(data.get("version") or UNKNOWN_VERSION),
            prefix_exists=bool(data.get("prefix_exists", True)),
        )


def local_runtime_build() -> RuntimeBuild:
    """This process's own interpreter facts -- self-description, not a probe.

    `sys.prefix` rather than `sys.executable`'s parent: a venv's
    `bin/python3` is frequently a symlink to the base interpreter, so the
    executable path alone can name an interpreter outside the venv that is
    actually in use. `sys.prefix` is the venv root in every case
    (`venv`/`virtualenv` both set it), which is what the installed service's
    venv has to be compared against.
    """
    return RuntimeBuild(
        executable=sys.executable or "",
        prefix=sys.prefix,
        python=".".join(str(n) for n in sys.version_info[:3]),
        pid=os.getpid(),
        version=running_version(),
        prefix_exists=Path(sys.prefix).is_dir(),
    )


#: Every substrate whose api interpreter lives in a deployed image rather
#: than in a venv on this host, mapped to the sentence naming what reports on
#: it instead. A native keg/venv comparison has no subject on any of them, so
#: the question is `not_applicable` there however the probe went.
IMAGE_SUBSTRATE_SUBJECTS: dict[str, str] = {
    "compose": (
        "the api port on this host is held by a `compose` container/cluster deployment, "
        "whose interpreter lives in its image -- a native keg/venv comparison does not apply"
    ),
    "terraform": (
        "the api port on this host is held by a `terraform` container/cluster deployment, "
        "whose interpreter lives in its image -- a native keg/venv comparison does not apply"
    ),
    "kubernetes": (
        "the api port on this host is held by a `kubernetes` container/cluster deployment, "
        "whose interpreter lives in the deployed image -- a native keg/venv comparison does "
        "not apply. The Kubernetes section reports that deployment's build"
    ),
}


@dataclass(frozen=True)
class BuildScope:
    """Whether the native running-build question has a subject here, and why not.

    One type, one decision, because the two surfaces that ask it had drifted
    into opposite orderings and #4182 is the bill for that. The Infrastructure
    page settled applicability *first* (`_infra_running_build`) and so stayed
    quiet on a cluster-served host; `ops status` probed first and reported the
    probe's failure, so the one command an operator runs printed
    `CANNOT DETERMINE -- http://127.0.0.1:8000/api/v1/info answered HTTP 401`
    about a question that had no subject on that machine at all.

    `detail` is only meaningful when `applicable` is False: it is the reason
    the question does not apply, phrased for an operator.
    """

    applicable: bool
    detail: str


def native_build_scope(
    *,
    in_cluster: bool,
    image_substrate: str = "",
    native_venv: str = "",
    native_venv_reason: str = "",
    missing_venv_is_out_of_scope: bool = True,
) -> BuildScope:
    """Does "is the serving api the installed keg's venv?" have a subject here?

    The single source for that decision (#4182). Three ways the answer is no,
    and none of them depends on whether the api answered a probe -- which is
    the whole point: "I could not reach it" is not a fact about scope, and
    reporting it as one is how a Kubernetes host was told its native build
    could not be determined.

    * **`in_cluster`** -- this process is itself a Pod. Its `sys.prefix` comes
      from the deployed image.
    * **`image_substrate`** -- something in `IMAGE_SUBSTRATE_SUBJECTS` holds
      the api on this host. Same reason, from the outside.
    * **no native venv** -- nothing a native install ever created is on disk,
      so whatever answers :8000 is not one and there is nothing to compare it
      against. `native_venv_reason` carries the caller's own words for that
      (`_expected_native_api_venv` returns them), because "no keg carrying a
      libexec/venv was found" and "Homebrew not found" send an operator to
      different places.

    `missing_venv_is_out_of_scope=False` is for a caller that already knows a
    native service IS registered and started: there, a venv that cannot be
    located is an anomaly worth reporting as "cannot determine" rather than
    as silence, and `classify()` renders it that way from an empty
    expectation.
    """
    if in_cluster:
        return BuildScope(
            False,
            "this api runs inside a Kubernetes Pod, whose interpreter lives in the deployed "
            "image. The Kubernetes card reports that deployment's build.",
        )
    if image_substrate in IMAGE_SUBSTRATE_SUBJECTS:
        return BuildScope(False, IMAGE_SUBSTRATE_SUBJECTS[image_substrate])
    if not native_venv and missing_venv_is_out_of_scope:
        return BuildScope(
            False, native_venv_reason or "there is no native api service on this machine"
        )
    return BuildScope(True, "")


def same_tree(running: str, expected: str) -> bool:
    """Whether `running` is `expected` or sits inside it, symlinks resolved.

    Both sides are `realpath`-ed because the Homebrew side is a symlink by
    construction: a keg's service execs `<prefix>/opt/<formula>/libexec/venv`,
    which resolves into `<prefix>/Cellar/<formula>/<version>/libexec/venv`,
    and the running process reports the resolved form. Comparing the literals
    would report drift on every correct brew install -- the inverse of #4133
    and just as useless.

    `realpath` is used rather than `Path.resolve(strict=True)` precisely
    because the running side may no longer exist: a deleted venv is the state
    worth reporting, so a comparison that raises on it is the wrong tool.
    Containment (rather than equality alone) allows a caller to pass a keg
    root and still match the venv inside it.
    """
    if not running or not expected:
        return False
    run_real = Path(os.path.realpath(running))
    want_real = Path(os.path.realpath(expected))
    if run_real == want_real:
        return True
    return want_real in run_real.parents


@dataclass(frozen=True)
class BuildDrift:
    """The comparison between a live process and the build installed beside it."""

    state: str
    running: RuntimeBuild | None
    expected_prefix: str
    expected_source: str
    detail: str
    remediation: str

    @property
    def mismatched(self) -> bool:
        """True only for the actionable state -- never for "could not tell"."""
        return self.state == BUILD_MISMATCH

    @property
    def running_prefix(self) -> str:
        """The live process's venv root, or "" when nothing answered."""
        return self.running.prefix if self.running is not None else ""

    def summary(self) -> str:
        """One line fit for `ops status`, `doctor` and an `OpsResult` message.

        A mismatch is phrased as a mismatch and carries both paths, which is
        #4133's third acceptance criterion: the failure mode being fixed is a
        surface that printed `version 3.0.0rc17` for a process running some
        other version's venv, so a version string alone is not an acceptable
        rendering of this state.
        """
        if self.state == BUILD_MATCH:
            return f"the running api is executing the installed build ({self.expected_prefix})"
        if self.state == BUILD_MISMATCH:
            return (
                f"MISMATCH: the running api is executing {self.running_prefix}, "
                f"but the installed service execs {self.expected_prefix}"
            )
        if self.state == BUILD_NOT_APPLICABLE:
            return f"not applicable here: {self.detail}"
        return f"could not determine which build the running api is executing: {self.detail}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form, as sent to the Infrastructure page."""
        return {
            "state": self.state,
            "running": self.running.to_dict() if self.running is not None else None,
            "expected_prefix": self.expected_prefix,
            "expected_source": self.expected_source,
            "detail": self.detail,
            "remediation": self.remediation,
            "summary": self.summary(),
        }


def classify(
    running: RuntimeBuild | None,
    expected_prefix: str | None,
    *,
    expected_source: str = "",
    remediation: str = "",
    undetermined_detail: str = "",
) -> BuildDrift:
    """Compare a live process's report against the installed service's venv.

    Ordering is load-bearing, and it is "cannot tell" first in both
    directions. A missing report and a missing expectation each produce
    `BUILD_UNDETERMINED`, because the only wrong answer available here is a
    confident one: reporting `BUILD_MATCH` over an api that did not answer is
    exactly the `[OK]`-over-a-mismatch this issue was filed about, and
    reporting `BUILD_MISMATCH` because the expectation could not be located
    would send an operator to restart a service that is fine.
    """
    if running is None:
        return BuildDrift(
            state=BUILD_UNDETERMINED,
            running=None,
            expected_prefix=expected_prefix or "",
            expected_source=expected_source,
            detail=undetermined_detail
            or "the api did not report its runtime (not running, or an older build)",
            remediation=remediation,
        )
    if not expected_prefix:
        return BuildDrift(
            state=BUILD_UNDETERMINED,
            running=running,
            expected_prefix="",
            expected_source=expected_source,
            detail=undetermined_detail
            or "the venv the installed api service execs could not be located",
            remediation=remediation,
        )
    if same_tree(running.prefix, expected_prefix):
        return BuildDrift(
            state=BUILD_MATCH,
            running=running,
            expected_prefix=expected_prefix,
            expected_source=expected_source,
            detail="",
            remediation="",
        )
    detail = (
        f"pid {running.pid} is running python {running.python} from {running.prefix} "
        f"and reports version {running.version}; the installed service execs "
        f"{expected_prefix}"
    )
    if not running.prefix_exists:
        # The acute form, and the one that makes this urgent rather than
        # merely untidy: the interpreter's own venv is gone, so the process
        # is alive only until something restarts it, and the next restart --
        # a reboot, self-heal, the admin Restart control -- leaves the api
        # down with a ModuleNotFoundError (#4133).
        detail += (
            ". That path no longer exists: the process is holding deleted files open and "
            "the next restart by ANY path will fail to start it"
        )
    return BuildDrift(
        state=BUILD_MISMATCH,
        running=running,
        expected_prefix=expected_prefix,
        expected_source=expected_source,
        detail=detail,
        remediation=remediation,
    )
