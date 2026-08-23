"""One implementation of "can this process reach the Docker socket, and how" (#4022).

**The fault this exists to stop.** Group membership is stamped into a
session's credentials when the session is created, so `usermod -aG docker
<user>` never reaches an already-running `systemd --user` manager -- nor any
unit that manager forked, `nyxgpt-api` included. Restarting the *unit* cannot
fix it (an unprivileged manager cannot grant itself a group); only recreating
the login session does. On the owner's EC2 instance that made every Docker
call from the API process fail with `permission denied while trying to connect
to the Docker daemon socket`, while the very same call from a fresh SSH shell
succeeded. `sg docker` closes the gap without a restart: it reads `/etc/group`
live, so it applies the membership the user genuinely already holds.

**Why one module and not a helper in each caller.** `ops.py` imports
`self_heal.py` (`ops.py`'s `_compose_stack_snapshot`, and the constraint is
documented at `ops._observability_container_names`), so self-heal cannot
import ops, and the hop was written twice: `ops._enable_docker_socket_hop`
for the install path and `self_heal._docker_run` for the API path (#3812).
Two copies of a probe is two chances to diverge on the question they exist to
answer identically -- and they *had* diverged, which is #4022: self-heal's
copy retried a denied read through `sg docker` and reported `unknown` when it
could not, while `ops._docker_container_state` did neither and rendered every
denied read as the definite negative `absent`. The dashboard said Cassandra
was absent while it was running. So the mechanism lives here, once, below
both -- the same reason `k8s_pod_state.py`, `brew_services.py` and
`install_mode.py` sit where they do (D-022). This module imports nothing from
`nyxgpt`.

**Policy is the caller's, mechanism is this module's.** The two callers
genuinely differ on which hops they may use, and that difference is load
bearing:

* `self_heal.py` and every ops read reachable from a *polled endpoint* run
  inside the public-facing API process. They pass `("sg",)` and nothing else.
  `sg` only ever claims a group the invoking user is already a member of, so
  it grants no authority the operator did not already give this account.
* `ops._enable_docker_socket_hop`, reached only from an interactive
  `nyxgpt ops install` -- already a privileged operation -- may escalate to
  `("sg", "sudo")`. That choice is spelled at the call site, not defaulted
  here, so no future caller inherits `sudo` by accident.

Both are held to the same bar before adoption: the hop must reach the daemon
*and* hand this process's environment through unchanged (`hop_preserves_env`).
An env-resetting hop is worse than no hop -- see that function.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from typing import Protocol

__all__ = [
    "ACCESS_FAILURE_MARKERS",
    "DEFAULT_RECHECK_SECONDS",
    "ENV_PROBE_VAR",
    "HOP_LABELS",
    "DockerSocketHop",
    "hop_argv",
    "hop_preserves_env",
    "hop_reaches_daemon",
    "looks_like_access_failure",
]


class Runner(Protocol):
    """How this module asks its caller to run a subprocess.

    Deliberately not `subprocess.run`: each caller's own runner already
    carries that module's logging, redaction, `bounded_argv` guard and
    timeout-to-returncode translation, and a probe that bypassed those would
    be the one Docker call in the process that could raise `TimeoutExpired`
    past a status endpoint (#3858). `expected=True` marks a read-only probe
    whose non-zero exit is a normal outcome, so it logs at DEBUG.
    """

    def __call__(
        self,
        cmd: list[str],
        *,
        timeout: float,
        expected: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run `cmd`, returning its result rather than raising on a non-zero exit."""
        ...


# Substrings that identify "this process may not talk to the Docker socket",
# as opposed to "Docker answered and said no". Matched case-insensitively
# against stderr. Docker's CLI and the Compose plugin word it differently
# ("Docker daemon socket" vs "docker API at unix://"), and the owner's rc12
# and rc13 instances produced one each.
ACCESS_FAILURE_MARKERS = (
    "permission denied",
    "cannot connect to the docker daemon",
    "connect to the docker api",
    "is the docker daemon running",
)

# How each hop is spelled in operator-facing text.
HOP_LABELS = {"sg": "`sg docker`", "sudo": "`sudo -n docker`"}

# Name of the throwaway variable `hop_preserves_env` passes through a
# candidate hop. It stands in for the real `env=`-forwarded secrets
# (`DJANGO_SUPERUSER_PASSWORD` and friends, see `ops._glitchtip_ensure_superuser`)
# without putting a real one on a probe's argv.
ENV_PROBE_VAR = "NYXGPT_DOCKER_HOP_PROBE"

# How long a "no usable hop here" answer is trusted before it is asked again.
# The answer can change under a running API process -- an operator who runs
# `sudo usermod -aG docker <user>` while the service is up makes `sg docker`
# start working from that moment -- so a permanent negative would make the
# very repair the dashboard points the operator at require a restart to take
# effect. Re-asking costs two short subprocesses at most once per interval.
DEFAULT_RECHECK_SECONDS = 300.0

# Bounds for the probes themselves. Short by design: these run only after a
# real Docker call has already failed, on a path a dashboard may be polling.
_REACH_PROBE_TIMEOUT = 20.0
_ENV_PROBE_TIMEOUT = 10.0


def looks_like_access_failure(cp: subprocess.CompletedProcess[str]) -> bool:
    """True if `cp` failed because Docker was unreachable, not because Docker said no.

    Only this class of failure is worth retrying through a group hop. A
    `docker compose up -d` that failed because an image pull 404'd must not
    drag a hop probe along behind it on every watchdog pass.
    """
    stderr = (cp.stderr or "").lower()
    return any(marker in stderr for marker in ACCESS_FAILURE_MARKERS)


def hop_argv(cmd: Sequence[str], hop: str | None) -> list[str]:
    """Rewrite `cmd` to run through `hop`, preserving the environment it sees.

    Both forms are environment-preserving *by construction*, which is the
    whole point (#3760 review): `docker-compose.yml` interpolates `${HOME}`
    for every bind mount, and `docker compose exec -e VAR` (bare, no `=value`)
    forwards secrets out of this process's environment without ever putting
    them on argv (CodeQL #105/#106). A hop that resets the environment would
    silently relocate every volume under `/root` and drop those secrets.

    - `sg docker -c ...` re-runs the command with the `docker` group added to
      the credentials, leaving the environment entirely alone. It takes a
      command *string*, so argv is shell-quoted with `shlex.join` -- the
      sanitizer barrier for this call (CodeQL #4, py/command-line-injection),
      on top of the `re.fullmatch` barriers each caller already applies to
      service/container names before they reach an argv at all. It also keeps
      a compose file path containing a space from silently becoming two
      arguments.
    - `sudo -n --preserve-env ...` opts out of sudo's default `env_reset`
      (and, on Amazon Linux/RHEL, its `always_set_home`). `--preserve-env`
      needs the sudoers SETENV tag, which `NOPASSWD: ALL` implies -- and
      `hop_preserves_env` verifies it rather than assuming it.

    An unknown or absent `hop` returns the argv unchanged, so a caller may
    pass its current hop without first testing whether it has one.
    """
    argv = list(cmd)
    if hop == "sg":
        return ["sg", "docker", "-c", shlex.join(argv)]
    if hop == "sudo":
        return ["sudo", "-n", "--preserve-env", *argv]
    return argv


def hop_preserves_env(hop: str, runner: Runner) -> bool:
    """True if `hop` hands this process's environment through unchanged.

    Two variables are checked because two different mechanisms depend on
    them, and both fail *silently* rather than loudly if the hop resets the
    environment (#3760 review):

    - `HOME`, which `docker-compose.yml` interpolates into every bind-mount
      source. Under sudo's `env_reset` + `always_set_home` (the Amazon
      Linux/RHEL default, i.e. exactly the distro this targets) it becomes
      `/root`, so the observability stack comes up bound to
      `/root/.nyxGPT/volumes/...` while the ownership fixes chown the user's
      home -- Grafana/Prometheus/Loki cannot write their data dirs, and the
      next non-hop run recreates the containers against the user-home mounts,
      abandoning whatever was written under `/root`. `sg` execs the user's
      login shell, so a shell that resets the environment would do the same.
    - an arbitrary caller-supplied variable, standing in for the
      `run(env=...)` secrets `docker compose exec -e VAR` forwards into a
      container without ever putting the value on argv.

    A hop that fails this check is not used at all: a loud "cannot reach the
    daemon, reconnect" is a better outcome than containers quietly built
    against the wrong paths.

    Only the *last two* lines of output are compared. `sg` runs the user's
    login shell, whose rc files may print a banner before the probe's own
    output; a login banner is not an environment reset, and failing the hop
    over one would deny the repair to exactly the hosts that need it.
    """
    home = os.environ.get("HOME")
    if not home:  # pragma: no cover - defensive; POSIX logins always set HOME
        return False
    sentinel = "nyxgpt-hop-probe"
    script = f'printf "%s\\n%s\\n" "$HOME" "${ENV_PROBE_VAR}"'
    cp = runner(
        hop_argv(["sh", "-c", script], hop),
        timeout=_ENV_PROBE_TIMEOUT,
        expected=True,
        env={**os.environ, ENV_PROBE_VAR: sentinel},
    )
    if cp.returncode != 0:
        return False
    return (cp.stdout or "").strip().splitlines()[-2:] == [home, sentinel]


def hop_reaches_daemon(hop: str, runner: Runner) -> bool:
    """True if a `docker` call made through `hop` can talk to the daemon."""
    cp = runner(
        hop_argv(["docker", "info", "--format", "{{.ServerVersion}}"], hop),
        timeout=_REACH_PROBE_TIMEOUT,
        expected=True,
    )
    return cp.returncode == 0


class DockerSocketHop:
    """The hop this process routes its Docker calls through, if it needs one.

    Holds the probe result for the life of the process (see
    `DEFAULT_RECHECK_SECONDS` for why a *negative* answer expires but a
    positive one does not: an established hop cannot stop working, while a
    missing group membership can start existing).

    `which` is the caller's own PATH lookup, so a test that stubs the
    module's `_which` stubs this too, and `on_adopt`/`on_probe_error` are the
    caller's logging -- this module has no logger of its own, by the same
    import-free rule that put it here.
    """

    def __init__(
        self,
        *,
        runner: Runner,
        which: Callable[[str], str | None],
        candidates: Sequence[str] = ("sg",),
        recheck_seconds: float = DEFAULT_RECHECK_SECONDS,
        on_adopt: Callable[[str], None] | None = None,
        on_probe_error: Callable[[str, BaseException], None] | None = None,
    ) -> None:
        """Build a hop holder. `candidates` is the default policy for `resolve`."""
        self._runner = runner
        self._which = which
        self._candidates = tuple(candidates)
        self._recheck_seconds = recheck_seconds
        self._on_adopt = on_adopt
        self._on_probe_error = on_probe_error
        self._lock = threading.RLock()
        self._hop: str | None = None
        self._checked_at = 0.0

    @property
    def active(self) -> str | None:
        """The hop currently in force, or None while Docker is reached directly."""
        return self._hop

    @property
    def label(self) -> str:
        """How the active hop reads in operator-facing text (empty if there is none)."""
        return HOP_LABELS.get(self._hop or "", "")

    def wrap(self, cmd: Sequence[str]) -> list[str]:
        """`cmd` routed through the active hop (unchanged when there is none)."""
        return hop_argv(cmd, self._hop)

    def apply(self, cmd: Sequence[str]) -> list[str]:
        """Route a *bare* `docker ...` argv through the active hop.

        Only rewrites argv whose first element is `docker`, so an argv that is
        already privileged (`ops._privileged_run` puts `sudo` in front) is
        never double-wrapped, and no non-Docker command is touched.
        """
        argv = list(cmd)
        if self._hop is not None and argv[:1] == ["docker"]:
            return hop_argv(argv, self._hop)
        return argv

    def adopt(self, candidates: Sequence[str] | None = None) -> str | None:
        """Probe `candidates` in order and adopt the first usable one. Not cached.

        For the *eager* caller (`ops._enable_docker_socket_hop`), which asks
        exactly once at a known point in an install and must not be silenced
        by a negative answer some earlier probe cached.
        """
        with self._lock:
            if self._hop is not None:
                return self._hop
            for hop in candidates if candidates is not None else self._candidates:
                if self._which(hop) is None or self._which("docker") is None:
                    continue
                try:
                    usable = hop_reaches_daemon(hop, self._runner) and hop_preserves_env(
                        hop, self._runner
                    )
                except Exception as e:  # pragma: no cover - defensive; a probe must never raise
                    if self._on_probe_error is not None:
                        self._on_probe_error(hop, e)
                    continue
                if usable:
                    self._hop = hop
                    self._checked_at = time.monotonic()
                    if self._on_adopt is not None:
                        self._on_adopt(hop)
                    return hop
            self._checked_at = time.monotonic()
            return None

    def resolve(self) -> str | None:
        """The hop for this process, probing at most once per recheck interval.

        For the *lazy* callers -- anything that discovers it needs a hop only
        because a Docker call just failed. Called only after such a failure
        (see `run`), so a host where Docker works normally never pays for
        these probes at all.
        """
        with self._lock:
            if self._hop is not None:
                return self._hop
            now = time.monotonic()
            if self._checked_at and (now - self._checked_at) < self._recheck_seconds:
                return None
            return self.adopt()

    def run(
        self, cmd: list[str], *, timeout: float, expected: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Run a `docker`/`docker compose` command, retrying through the hop if denied.

        Every Docker call a caller routes through here degrades identically:
        the survey, the container/health probes, the restarts and the log
        reads. Fixing only the survey would have produced a panel that could
        see eleven components and heal none of them (#3812), and fixing only
        self-heal produced a dashboard that healed Compose correctly while
        still calling a running Cassandra `absent` (#4022).

        The bare command is tried first and its result returned untouched when
        it succeeds, so a healthy host pays nothing. A hop is looked for only
        when the failure is a socket-access failure
        (`looks_like_access_failure`); once one is established it is used up
        front. When no hop is available the original failure is returned
        unchanged -- the caller still reports "cannot determine" with Docker's
        own words, which is the honest answer on a host where the group change
        itself was never made.
        """
        if self._hop is not None:
            return self._runner(self.wrap(cmd), timeout=timeout, expected=expected)
        cp = self._runner(cmd, timeout=timeout, expected=expected)
        if cp.returncode == 0 or not looks_like_access_failure(cp):
            return cp
        if self.resolve() is None:
            return cp
        return self._runner(self.wrap(cmd), timeout=timeout, expected=expected)

    def reset(self) -> None:
        """Forget any established/attempted hop. For tests -- nothing in the app calls it."""
        with self._lock:
            self._hop = None
            self._checked_at = 0.0

    def force(self, hop: str | None) -> None:
        """Adopt `hop` without probing. For tests -- nothing in the app calls it.

        The application never sets a hop it has not proved (`adopt`/`resolve`
        are the only paths in), so this exists purely to put a test into the
        hopped state without stubbing four subprocesses to get there.
        """
        with self._lock:
            self._hop = hop
            self._checked_at = time.monotonic() if hop is not None else 0.0
