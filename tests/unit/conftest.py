"""Unit-test configuration and fixtures."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# The real home of the machine running the suite, captured at import -- before
# any test's `monkeypatch.setattr(ops.Path, "home", ...)` can move it.
# `_isolate_grafana_secret_files` compares against this to tell "this test
# isolated its own home" from "this write is about to land in the developer's
# real ~/.nyxGPT".
_REAL_HOME = Path.home()


@pytest.fixture(autouse=True)
def _reset_cassandra_pool():
    """Reset the module-level Cassandra connection pool before and after each test.

    Without this, a pool created in one test would be reused in the next test,
    causing stale mocks and unexpected connection attempts.
    """
    import contextlib

    from nyxgpt.rag.vectorstore_cassandra import reset_connection_pool

    with contextlib.suppress(Exception):
        reset_connection_pool()

    yield

    with contextlib.suppress(Exception):
        reset_connection_pool()


@pytest.fixture(autouse=True)
def _reset_query_result_cache():
    """Reset the module-level RAG query result cache before and after each test.

    Without this, a cache backend (or disabled NoOpCache) created in one test
    would be reused in later tests, causing stale hits/misses depending on
    test execution order.
    """
    import nyxgpt.rag.rag as rag_module

    rag_module._query_result_cache = None
    yield
    rag_module._query_result_cache = None


@pytest.fixture(autouse=True)
def _reset_config_fallback_warnings():
    """Reset the config module's once-per-key fallback warning dedup set.

    `config._log_fallback_once` only logs a given key's fallback once per
    process, so without this reset a test asserting a fallback WARNING would
    pass or fail depending on whether an earlier test already triggered the
    same key's fallback.
    """
    from nyxgpt.config import reset_fallback_warnings

    reset_fallback_warnings()
    yield
    reset_fallback_warnings()


@pytest.fixture(autouse=True)
def _isolate_install_mode_marker(monkeypatch, tmp_path):
    """Redirect the install-mode markers into `tmp_path` for every unit test (#3789).

    `install_mode.INSTALL_MODE_FILE` defaults to the developer's real
    `~/.nyxGPT/install-mode.json`, and it is not an inert file: it is what
    `ops._reconcile_install_mode()` compares the requested mode against, so a
    test that reaches that function on a machine recording `dev` (exactly the
    machine `nyxgpt up --dev` produces) makes it decide the mode is *changing*
    -- which deletes the real `~/.nyxGPT/opt/nyxgpt-api/venv`, boots out the
    live dev LaunchAgents on macOS, and rewrites the real marker to artifact.
    The suite would then be destroying the state of the very machine it runs
    on, and passing or failing according to what that machine happens to have
    installed.

    Isolating the marker here, once, closes that for every present and future
    test rather than per call site. Tests that need to exercise the marker
    itself still write to it -- they just write into `tmp_path`.

    `NYXGPT_HOME` is redirected alongside it because the per-substrate markers
    (#3834 -- `install-mode-kubernetes.json`; #3835 --
    `install-mode-terraform.json`) are resolved from it rather than from a
    module-level constant of their own.
    """
    from nyxgpt import install_mode

    home = tmp_path / "install-mode-home"
    monkeypatch.setattr(install_mode, "NYXGPT_HOME", home)
    monkeypatch.setattr(install_mode, "INSTALL_MODE_FILE", home / "install-mode.json")
    yield


@pytest.fixture(autouse=True)
def _isolate_ops_terraform_dir(monkeypatch, tmp_path):
    """Redirect the ops-managed Terraform working directory into `tmp_path` (#3835).

    `ops.TERRAFORM_DIR` defaults to the developer's real
    `~/.nyxGPT/terraform`, which `_sync_local_terraform_config` writes into
    (and `terraform destroy` runs against). A unit test reaching either must
    not touch the state of a real deployment on the machine running the
    suite -- the same reasoning as the install-mode marker above.
    """
    from nyxgpt import ops

    monkeypatch.setattr(ops, "TERRAFORM_DIR", tmp_path / "ops-terraform")
    yield


@pytest.fixture(autouse=True)
def _isolate_image_build_state(monkeypatch, tmp_path):
    """Redirect the Docker build staging and image markers into `tmp_path` (#3834).

    Same hazard as the install-mode marker above, one directory over.
    `ops.K8S_BUILD_DIR` is where the Kubernetes artifact path unpacks the
    published `nyxgpt-api`/`nyxgpt-web` tarballs to build from, and
    `DOCKER_IMAGE_MARKER_DIR` records which source each `:local` image was
    last built from. A test that reaches either -- by stubbing one layer of
    the build and not the one below it, which is exactly how this was found
    -- otherwise copies the whole working tree into the developer's real
    `~/.nyxGPT`, and can make their next real install skip a rebuild it
    needed (or run one it didn't).
    """
    from nyxgpt import ops

    monkeypatch.setattr(ops, "K8S_BUILD_DIR", tmp_path / "k8s-build-home")
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path / "docker-image-markers")
    yield


@pytest.fixture(autouse=True)
def _isolate_grafana_secret_files(monkeypatch, tmp_path):
    """Keep every unit test's Grafana secret writes out of the real `~/.nyxGPT` (#3947).

    `_slack_webhook_secret_path()` and `_glitchtip_grafana_token_path()`
    resolve `Path.home()/".nyxGPT"/"secrets"/...` fresh on every call, and the
    writers behind them are reached from ordinary-looking tests: anything that
    calls `ops.env_sync(args)` runs its "sync grafana slack webhook secret"
    step, and that step writes the *invoking machine's* secret file even when
    every path the test passes in is under `tmp_path` -- `env_sync` forwards
    only `--config`, so nothing about the secret path is derived from it. Four
    `env_sync` tests were doing exactly that (verified by running one:
    `test_env_sync_logs_summary` alone created the real
    `~/.nyxGPT/secrets/slack-webhook-url`), so on a machine with a webhook
    actually configured, running the unit suite overwrote the real secret with
    the unconfigured placeholder -- and then restarted the real Grafana to
    "pick up" it (see `real_restart_grafana_if_running` below for that half).

    Redirect rather than refuse, and only when the write would land in the real
    home: a test that isolates `Path.home()` itself (the
    `_ensure_glitchtip_secrets_dir` tests do, and assert on the files they get)
    keeps its own path untouched, while a test that isolates nothing gets
    `tmp_path` instead of the developer's machine. Closed once, here, rather
    than at each present and future call site -- the same reasoning as
    `_isolate_install_mode_marker` above.
    """
    from nyxgpt import ops

    sandbox = tmp_path / "ops-secrets-home"

    def redirected(real):
        def sandboxed() -> Path:
            path = real()
            try:
                relative = path.relative_to(_REAL_HOME)
            except ValueError:
                return path  # this test already redirected `Path.home()` itself
            return sandbox / relative

        return sandboxed

    for name in ("_slack_webhook_secret_path", "_glitchtip_grafana_token_path"):
        monkeypatch.setattr(ops, name, redirected(getattr(ops, name)))
    yield


@pytest.fixture(autouse=True)
def real_restart_grafana_if_running(monkeypatch):
    """Stub `ops._restart_grafana_if_running` for every unit test -- and hand the
    real one to the tests that are about it (#3947).

    This is the live-Docker half of the hazard `_isolate_grafana_secret_files`
    covers: `_sync_grafana_slack_webhook_secret` calls it whenever the secret
    file's contents changed, and it runs `docker compose restart grafana`
    against the machine's own daemon, then polls Grafana's health for up to two
    minutes. On a clean runner no `grafana` container exists and it skips --
    which is the only reason the tests reaching it were ever green. On a runner
    where one does exist (a leftover observability stack; a crash-looping one
    especially) those tests fail `assert rc == 0` two minutes later, as
    transient-looking red with nothing to do with their subject. That is what
    cost #3947 three verification runs, under two different symptoms.

    Requesting this fixture by name yields the real function, so
    `test_restart_grafana_if_running_*` still exercises it with their own stubs
    of `_compose_available`/`_compose_stack_snapshot`/`_run` in place. Tests
    that assert a restart *was* requested keep patching the name themselves;
    their patch is applied after this one and wins.
    """
    from nyxgpt import ops

    real = ops._restart_grafana_if_running
    monkeypatch.setattr(
        ops,
        "_restart_grafana_if_running",
        lambda reason="": ops.OpsResult(True, "Skipped Grafana restart (stubbed for unit tests)"),
    )
    return real


@pytest.fixture(autouse=True)
def _refuse_real_docker_builds(monkeypatch):
    """Fail any unit test that reaches a real `docker build` (#3834).

    A unit test that shells out to the machine's Docker daemon is not a unit
    test: it takes minutes on a cold cache, it depends on a daemon being
    there at all, and it leaves `nyxgpt-api:local` / `nyxgpt-web:local`
    behind on the developer's machine -- where the next real `nyxgpt ops
    install` reads them, and skips or forces a rebuild accordingly.

    It is easy to do by accident, because the install paths build images
    through several layers: stubbing one and not the one below it is enough.
    Two suites were doing it when this guard was added (the Terraform
    lifecycle-ledger test, and the Kubernetes install-step tests that stubbed
    the low-level builder while the step called the new per-component
    wrapper). The guard names the command, so the fix is obvious: stub the
    build step this test isn't about.
    """
    from nyxgpt import ops

    real_run = ops._run

    def guarded(cmd, *args, **kwargs):
        if list(cmd)[:2] == ["docker", "build"]:
            raise AssertionError(
                "a unit test reached a real `docker build` -- stub the build step it is "
                f"not testing (command: {list(cmd)})"
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ops, "_run", guarded)
    yield


# Programs that speak to the machine's own stack. Everything else a unit test
# executes (git, bash, python, tar, ...) is left alone -- several suites
# legitimately drive real `git` repos and shell scripts in tmp dirs.
#
# Per program: the subcommands that only *read* state, and the ones that
# *change* it. Reads are answered with a deterministic "nothing here" (see
# `_no_real_host_stack_commands`); changes are refused; anything unlisted is
# treated as a change, so a verb nobody thought about fails loudly rather than
# running against the developer's machine.
_HOST_STACK_READS: dict[str, frozenset[str]] = {
    "docker": frozenset(
        {
            "ps",
            "ls",
            "info",
            "inspect",
            "images",
            "version",
            "logs",
            "port",
            "top",
            "stats",
            "events",
            "df",
        }
    ),
    "docker compose": frozenset({"ps", "config", "version", "images", "logs", "top", "port"}),
    "brew": frozenset({"list", "info", "--prefix", "--version", "outdated", "config", "deps"}),
    "brew services": frozenset({"list", "info"}),
    "launchctl": frozenset({"list", "print", "print-disabled", "dumpstate", "version"}),
    "systemctl": frozenset(
        {"is-active", "is-enabled", "is-failed", "status", "show", "list-units", "cat"}
    ),
    "kubectl": frozenset(
        {
            "get",
            "describe",
            "config",
            "version",
            "cluster-info",
            "top",
            "logs",
            "explain",
            "api-resources",
        }
    ),
    "terraform": frozenset({"output", "show", "version", "validate", "providers"}),
}

# The verbs that unambiguously change machine state, checked before the read
# table above (see `_host_stack_verdict`). Not exhaustive and does not need to
# be: an unlisted verb with no read verb in the argv is treated as a change
# anyway. This list exists for the argv that carries both.
_HOST_STACK_CHANGES: dict[str, frozenset[str]] = {
    "docker": frozenset(
        {
            "build",
            "pull",
            "push",
            "run",
            "rm",
            "rmi",
            "create",
            "start",
            "stop",
            "restart",
            "kill",
            "login",
            "logout",
            "tag",
            "load",
            "exec",
            "cp",
            "prune",
            "commit",
        }
    ),
    "docker compose": frozenset(
        {"up", "down", "restart", "start", "stop", "rm", "pull", "build", "exec", "cp", "kill"}
    ),
    "brew": frozenset({"install", "uninstall", "reinstall", "upgrade", "tap", "untap", "link"}),
    "brew services": frozenset({"start", "stop", "restart", "kill", "cleanup"}),
    "launchctl": frozenset(
        {"bootstrap", "bootout", "kickstart", "setenv", "unsetenv", "load", "unload", "remove"}
    ),
    "systemctl": frozenset(
        {"start", "stop", "restart", "reload", "enable", "disable", "daemon-reload", "mask"}
    ),
    "kubectl": frozenset(
        {"apply", "delete", "create", "patch", "replace", "scale", "rollout", "exec", "drain"}
    ),
    "terraform": frozenset({"apply", "destroy", "init", "import", "taint", "state"}),
}

# Programs where *every* invocation is a change to the machine (or an escalation
# to one), so there is no read subcommand to allow.
_HOST_STACK_ALWAYS_REFUSED = frozenset({"sudo", "sg", "doas"})


def _host_stack_verdict(cmd) -> tuple[str, str] | None:
    """Classify one argv as a host-stack read, a host-stack change, or neither.

    Returns `(kind, program)` where kind is "read" or "change", or None for a
    command this guard has no opinion about.
    """
    try:
        argv = [str(part) for part in cmd]
    except TypeError:  # a shell string -- not something this guard parses
        return None
    if not argv:
        return None

    # A binary a test built for itself (`tmp_path/"terraform"` -- the
    # `cloud_infra._run_terraform` tests write a shell script there and run it)
    # is not the machine's tooling however it is named.
    if os.path.dirname(argv[0]) and os.path.realpath(argv[0]).startswith(
        os.path.realpath(tempfile.gettempdir())
    ):
        return None

    program = os.path.basename(argv[0])
    if program in _HOST_STACK_ALWAYS_REFUSED:
        return "change", program

    if program not in _HOST_STACK_READS:
        return None

    # `docker compose ...` and `brew services ...` have their own verb tables:
    # `docker compose ps` reads, `docker compose up` does not.
    rest = argv[1:]
    if program in ("docker", "brew") and rest and rest[0] in ("compose", "services"):
        program = f"{program} {rest[0]}"
        rest = rest[1:]

    # Match the verb anywhere in the remaining argv rather than positionally:
    # the verb's position varies with each tool's global flags (`kubectl
    # --request-timeout=5s -n nyxgpt get pods`, `docker compose -f FILE ps`,
    # `systemctl --user is-active docker`), and a rule that tries to tell a
    # flag's value from the verb gets one of those three wrong. A read verb is
    # a distinctive token, so its presence is the signal; an argv with none is
    # a change.
    #
    # Known change verbs are checked *first*, because a change command can
    # carry a read verb as an argument -- `docker run --rm -v vol:/legacy
    # alpine ls -la /legacy` is the real one from `migrate_legacy_volumes`, and
    # reading it as `ls` would answer it silently instead of naming it.
    if any(part in _HOST_STACK_CHANGES.get(program, frozenset()) for part in rest):
        return "change", program
    kind = "read" if any(part in _HOST_STACK_READS[program] for part in rest) else "change"
    return kind, program


@pytest.fixture(autouse=True)
def _no_real_host_stack_commands(monkeypatch):
    """No unit test reads -- or changes -- the machine's own stack (#3983).

    `_refuse_real_docker_builds` above closes one command at the `ops._run`
    layer. This closes the class at the layer where a process actually starts,
    which is the only place that can tell "this test stubbed `subprocess.run`
    and merely *described* a command" from "this test is running it". Measured
    on the v3.0.0 head, one `pytest tests/unit/` really executed 100 `docker
    ps`, 99 `brew services list`, 81 `launchctl list`, 38 `docker compose`, 16
    `docker pull`, 7 `docker volume rm`, 7 `docker run`, 6 `sudo` and 4
    `systemctl --user` calls against the developer's own machine.

    Both halves of that are defects:

    - **Reads make the suite's result depend on the machine.** Three
      `ops doctor` tests stub `_docker_container_probe` and
      `_compose_stack_snapshot` but not `_brew_services_snapshot`, so on a host
      where `ollama` is a running brew service -- i.e. one actually running
      nyxGPT -- `detect_deployment_mode()` reports a native/Terraform conflict
      and `doctor` returns 2. Green in CI, red on the machine that runs the
      stack. Reads are therefore answered with a deterministic "nothing here",
      which is what a clean CI runner answers anyway, so the suite gets the
      same result everywhere.
    - **Changes act on the machine.** `docker volume rm nyxgpt_grafana_data`,
      `launchctl setenv OLLAMA_MODELS`, `sudo -n chown` and `docker pull
      ghcr.io/...` were all executing for real from unit tests. The pulls are
      also why `test_install_terraform_*` failed on an arm64 machine: the step
      is not in the tests' patch list, so it ran, and 3.0.0 has no published
      arm64 manifest. Changes are refused by name, so the fix is the same one
      the docker-build guard asks for: stub the step this test isn't about.

    A test that legitimately drives one of these commands stubs
    `subprocess.run` itself; that patch lands after this fixture and wins.
    """
    import subprocess

    real_run = subprocess.run
    real_popen = subprocess.Popen

    def _refuse(kind_program, cmd, action):
        _kind, program = kind_program
        raise AssertionError(
            f"a unit test {action} a real `{program}` command that changes machine state -- "
            f"stub the step it is not testing (command: {[str(c) for c in cmd]})"
        )

    def guarded_run(cmd, *args, **kwargs):
        verdict = _host_stack_verdict(cmd)
        if verdict is None:
            return real_run(cmd, *args, **kwargs)
        if verdict[0] == "change":
            _refuse(verdict, cmd, "ran")
        # A read: answer as a machine with none of this running would.
        text = kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")
        empty = "" if text else b""
        return subprocess.CompletedProcess([str(c) for c in cmd], 0, empty, empty)

    def guarded_popen(cmd, *args, **kwargs):
        verdict = _host_stack_verdict(cmd)
        if verdict is not None and verdict[0] == "change":
            _refuse(verdict, cmd, "spawned")
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    yield
