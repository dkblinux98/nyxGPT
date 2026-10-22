"""`Ready` must mean "doing its job", not "the process is up" (#4208).

`glitchtip-worker` is one container holding two halves -- `runworker`'s
scheduler and its consumer -- and `manage.py runworker` awaits neither, so one
can die while the process stays up. In CI run 39679850907 the consumer half
had enqueued-and-never-processed 400 tasks while Kubernetes read `1/1 Running`
with 0 restarts, because the Deployment declared no probe at all; and the
Compose twin's heartbeat-file healthcheck was MEASURED green in a reproduction
of the same state, because `_heartbeat_loop` runs for as long as
`Worker.run()` has not returned, which in the wedge is forever.

Three things are therefore guarded here:

* the cause -- the worker does not start before its datastores will answer,
  which is the `depends_on` Compose states and this overlay did not;
* the detection -- both substrates ask the consumer's own counters, from one
  shared file, and neither is allowed back to the heartbeat;
* the class -- no workload in either substrate gets to report health without
  a probe, or with a waiver that has not been written down.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from nyxgpt import ops

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY_DIR = REPO_ROOT / "k8s" / "observability"
PROBE_PATH = OBSERVABILITY_DIR / "glitchtip-worker-ready.py"
# Where both substrates mount it. The Kubernetes volume and the Compose bind
# must agree with each other and with the probe commands, or the probe is a
# missing file and every container is unhealthy forever.
PROBE_MOUNT = "/etc/nyxgpt/probes/glitchtip-worker-ready.py"


def _compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


def _probe_module() -> ModuleType:
    """Import the probe despite a filename that is not a legal module name.

    The hyphens are deliberate: the file is reachable through
    `src/nyxgpt/resources/k8s`, and a legal module name there would be
    collected as `nyxgpt.resources.k8s.observability.<name>` by setuptools'
    namespace-package discovery.
    """
    spec = importlib.util.spec_from_file_location("glitchtip_worker_ready", PROBE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# The probe's own logic
# --------------------------------------------------------------------------

# Trimmed from the real exposition page of a healthy worker running
# glitchtip/glitchtip:6.2.0. The `_created` gauges beside each counter are
# kept because they are the trap: their values are unix timestamps, so a
# prefix match that swallowed them would read ~1.79e9 "tasks processed" and
# never report a stall.
HEALTHY_PAGE = """\
# HELP vtasks_tasks_processed_total Total number of tasks processed
# TYPE vtasks_tasks_processed_total counter
vtasks_tasks_processed_total{queue="default",status="success",task_name="a"} 44.0
vtasks_tasks_processed_total{queue="default",status="success",task_name="b"} 1.0
# HELP vtasks_tasks_processed_created Total number of tasks processed
# TYPE vtasks_tasks_processed_created gauge
vtasks_tasks_processed_created{queue="default",status="success",task_name="a"} 1.7926325e+09
# HELP vtasks_queue_depth Approximate number of tasks waiting in the queue
# TYPE vtasks_queue_depth gauge
vtasks_queue_depth{queue="default"} 0.0
vtasks_queue_depth{queue="ingest"} 0.0
"""

# And the wedged worker's, captured from the reproduction: the server is up
# and answering, and every vtasks metric family is declared with no samples
# at all, because neither half ever completed an iteration.
WEDGED_PAGE = """\
# HELP vtasks_tasks_processed_total Total number of tasks processed
# TYPE vtasks_tasks_processed_total counter
# HELP vtasks_queue_depth Approximate number of tasks waiting in the queue
# TYPE vtasks_queue_depth gauge
"""


def test_processed_total_ignores_the_created_gauge_beside_it() -> None:
    probe = _probe_module()
    total, samples = probe._sum_samples(HEALTHY_PAGE, "vtasks_tasks_processed_total")
    assert (total, samples) == (45.0, 2)


def test_queue_depth_samples_are_counted_separately_from_their_values() -> None:
    probe = _probe_module()
    # Both queues are EMPTY, so the values sum to zero -- the evidence is that
    # the samples exist at all. A check written against the value would read
    # a healthy idle worker as a dead one.
    total, samples = probe._sum_samples(HEALTHY_PAGE, "vtasks_queue_depth")
    assert total == 0.0
    assert samples == 2


def _run_probe(monkeypatch: pytest.MonkeyPatch, page: str, argv: list[str]) -> int:
    probe = _probe_module()

    class _Response:
        def read(self) -> bytes:
            return page.encode()

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(probe.urllib.request, "urlopen", lambda *a, **k: _Response())
    return probe.main(argv)


def test_wedged_worker_is_not_ready_on_the_first_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run-39679850907 verdict, and it needs no stall window to reach it.

    A missing `vtasks_queue_depth` sample means `consume_queue` never ran an
    iteration, which is decidable immediately -- so the probe does not have to
    wait out `--max-stall-seconds` to call a worker that never started.
    """
    code = _run_probe(
        monkeypatch,
        WEDGED_PAGE,
        ["--metrics-port", "9090", "--state-file", str(tmp_path / "s")],
    )
    assert code == 1
    assert "consumer loop has never run" in capsys.readouterr().err


def test_healthy_worker_is_ready_and_records_its_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "s"
    assert (
        _run_probe(
            monkeypatch, HEALTHY_PAGE, ["--metrics-port", "9090", "--state-file", str(state)]
        )
        == 0
    )
    recorded, _changed_at = state.read_text().split()
    assert float(recorded) == 45.0


def test_a_stalled_consumer_goes_red_once_the_window_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The later wedge: the loop ran, published its gauge, and then stopped.

    The gauge check above cannot see this one -- the sample is there and stays
    there, because a Prometheus gauge carries no staleness. Only the processed
    total not moving distinguishes it.
    """
    state = tmp_path / "s"
    state.write_text(f"45.0 {time.time() - 600}")
    args = ["--metrics-port", "9090", "--state-file", str(state)]

    assert _run_probe(monkeypatch, HEALTHY_PAGE, [*args, "--max-stall-seconds", "900"]) == 0
    assert _run_probe(monkeypatch, HEALTHY_PAGE, [*args, "--max-stall-seconds", "180"]) == 1
    assert "processed nothing for 600s" in capsys.readouterr().err


def test_progress_since_the_last_probe_clears_a_stale_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "s"
    state.write_text(f"44.0 {time.time() - 600}")
    assert (
        _run_probe(
            monkeypatch,
            HEALTHY_PAGE,
            ["--metrics-port", "9090", "--state-file", str(state), "--max-stall-seconds", "1"],
        )
        == 0
    )
    assert float(state.read_text().split()[0]) == 45.0


def test_an_unreadable_state_file_does_not_wedge_the_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Garbage in the state file must not make the worker permanently red."""
    state = tmp_path / "s"
    state.write_text("not-a-number")
    assert (
        _run_probe(
            monkeypatch, HEALTHY_PAGE, ["--metrics-port", "9090", "--state-file", str(state)]
        )
        == 0
    )


def test_no_metrics_port_fails_rather_than_passing_vacuously(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A probe with nothing to read is the blind probe this file replaces."""
    probe = _probe_module()
    assert probe.main(["--metrics-port", "0", "--state-file", str(tmp_path / "s")]) == 1
    assert "no way to ask the worker" in capsys.readouterr().err


def test_an_unreachable_metrics_server_is_not_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = _probe_module()

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("Connection refused")

    monkeypatch.setattr(probe.urllib.request, "urlopen", _boom)
    assert probe.main(["--metrics-port", "9090", "--state-file", str(tmp_path / "s")]) == 1
    assert "not serving metrics" in capsys.readouterr().err


def test_the_probe_runs_under_the_image_interpreter_without_dependencies() -> None:
    """It is invoked as bare `python` inside glitchtip/glitchtip:6.2.0.

    Nothing in that image is installable at probe time, so the script may use
    the standard library only -- a third-party import here would be an
    ImportError on every probe, i.e. a permanently unhealthy worker.
    """
    source = PROBE_PATH.read_text()
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    }
    assert imports <= {"__future__", "argparse", "os", "sys", "time", "urllib"}
    # And it is syntactically valid for that interpreter, checked by running
    # the real one rather than by reading it.
    compile(source, str(PROBE_PATH), "exec")


# --------------------------------------------------------------------------
# The cause: the start-up order Compose states and the overlay did not
# --------------------------------------------------------------------------


def _worker_deployment() -> dict:
    docs = yaml.safe_load_all((OBSERVABILITY_DIR / "glitchtip.yaml").read_text())
    for doc in docs:
        if doc and doc.get("metadata", {}).get("name") == "glitchtip-worker":
            return doc
    raise AssertionError("no glitchtip-worker Deployment in k8s/observability/glitchtip.yaml")


def test_the_worker_waits_for_its_broker_and_its_migrations() -> None:
    """The #4208 cause, stated the only way Kubernetes offers.

    Measured on glitchtip/glitchtip:6.2.0: a worker started while the broker
    was not yet accepting connections processed 0 tasks and never recovered --
    `runworker`'s first act is `Worker.run()` -> `backend._rescue_tasks()`,
    an unguarded Valkey call. Compose has always said
    `depends_on: glitchtip-redis: condition: service_healthy`; this overlay
    said nothing, so the Pod raced its own datastores.
    """
    spec = _worker_deployment()["spec"]["template"]["spec"]
    inits = {c["name"]: c for c in spec.get("initContainers", [])}
    assert "wait-for-redis" in inits, (
        "the worker Pod must not start its process before the broker will answer -- "
        "losing the first Valkey call wedges it permanently (#4208)"
    )
    assert "glitchtip-redis" in " ".join(inits["wait-for-redis"]["command"])
    assert "wait-for-migrations" in inits, (
        "Compose gates this container on glitchtip-migrate "
        "`service_completed_successfully`; the overlay needs the same gate"
    )
    migrate_cmd = " ".join(inits["wait-for-migrations"]["command"])
    assert "migrate --check" in migrate_cmd, (
        "`migrate --check` applies nothing, so it cannot race the `migrate` "
        "initContainer on the glitchtip Deployment that does"
    )


# --------------------------------------------------------------------------
# The detection, on both substrates, from one file
# --------------------------------------------------------------------------


def test_kubernetes_probes_ask_the_consumer_and_not_the_heartbeat() -> None:
    container = _worker_deployment()["spec"]["template"]["spec"]["containers"][0]

    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env.get("VTASKS_METRICS_PORT"), (
        "the probe reads the consumer's own counters, which runworker only "
        "serves when VTASKS_METRICS_PORT is set"
    )

    for kind in ("readinessProbe", "livenessProbe"):
        command = container[kind]["exec"]["command"]
        assert PROBE_MOUNT in command, f"{kind} must run the shared probe at {PROBE_MOUNT}"
        assert "VTASKS_HEALTH_CHECK_FILE" not in " ".join(command), (
            "the heartbeat file was MEASURED fresh in the wedged state -- "
            "`Worker.run()` had not returned, so `_heartbeat_loop` kept "
            "writing. It cannot be the probe again (#4208)"
        )

    # Liveness must be the slacker of the two: NotReady is a report, a restart
    # throws away in-flight work.
    def _stall(kind: str) -> float:
        command = container[kind]["exec"]["command"]
        if "--max-stall-seconds" not in command:
            return 180.0  # the probe's own default
        return float(command[command.index("--max-stall-seconds") + 1])

    assert _stall("livenessProbe") > _stall("readinessProbe")
    # ...and they must not share a state file, or each would keep resetting
    # the other's idea of when progress last happened.
    assert container["readinessProbe"]["exec"]["command"] != (
        container["livenessProbe"]["exec"]["command"]
    )


def test_the_probe_file_reaches_the_pod() -> None:
    """A ConfigMap volume and a mount path that actually line up.

    If they do not, the probe is a missing file -- which fails, which reports
    every worker unhealthy forever. `kubectl apply -k` must also be able to
    build it standalone, with no `nyxgpt ops` step: that is why the file lives
    inside the kustomize root.
    """
    kustomization = yaml.safe_load((OBSERVABILITY_DIR / "kustomization.yaml").read_text())
    generators = kustomization.get("configMapGenerator", [])
    generated = {g["name"]: g for g in generators}
    assert PROBE_PATH.name in generated["glitchtip-worker-probe"]["files"]

    spec = _worker_deployment()["spec"]["template"]["spec"]
    volume = next(v for v in spec["volumes"] if v.get("configMap"))
    assert volume["configMap"]["name"] == "glitchtip-worker-probe"
    mount = next(m for m in spec["containers"][0]["volumeMounts"] if m["name"] == volume["name"])
    assert f"{mount['mountPath'].rstrip('/')}/{PROBE_PATH.name}" == PROBE_MOUNT


def test_compose_runs_the_same_probe_from_the_same_file() -> None:
    worker = _compose()["services"]["glitchtip-worker"]

    test = worker["healthcheck"]["test"]
    assert PROBE_MOUNT in test, "the Compose healthcheck must run the shared probe"
    assert "VTASKS_HEALTH_CHECK_FILE" not in " ".join(test), (
        "this is the probe #4208 measured GREEN on the defect it existed to "
        "catch -- a fresh heartbeat with a consumer doing nothing at all"
    )
    assert worker["environment"]["VTASKS_METRICS_PORT"] == "9090"

    binds = [v for v in worker["volumes"] if PROBE_MOUNT in v]
    assert len(binds) == 1, f"glitchtip-worker does not mount {PROBE_MOUNT}"
    source = binds[0].split(":")[0]
    assert (REPO_ROOT / source).resolve() == PROBE_PATH.resolve(), (
        "one file answers 'is the worker consuming' for both substrates -- a "
        "second copy is a drift waiting to happen"
    )


def test_the_probe_ships_in_the_package() -> None:
    """Repo-less installs mount it through `nyxgpt.resources` (#3504).

    The Compose bind is relative to the compose file's own directory, so the
    packaged tree has to carry the probe next to the packaged
    docker-compose.yml or the mount resolves to nothing -- and Docker would
    then helpfully create a DIRECTORY at the source path, making `python
    <dir>` the healthcheck.
    """
    packaged = Path(ops.__file__).parent / "resources" / "k8s" / "observability" / PROBE_PATH.name
    assert packaged.is_file()
    assert packaged.read_text() == PROBE_PATH.read_text()


# --------------------------------------------------------------------------
# The class: nothing reports health without a probe or a written-down waiver
# --------------------------------------------------------------------------

# Verified by execution, not assumed -- `docker run --entrypoint sh <image>`
# was run against each of these while #4208 was being fixed.
COMPOSE_HEALTHCHECK_WAIVERS = {
    "glitchtip-migrate": "one-shot `manage.py migrate`; Compose waits on its EXIT CODE "
    "(`service_completed_successfully`), which is a stronger signal than any probe",
    "host-api-relay": "a socat/relay shim with no protocol of its own to answer",
    "loki": "grafana/loki:3.6.13 is distroless -- `docker run --entrypoint sh` fails with "
    '"sh: executable file not found", so neither CMD nor CMD-SHELL can run anything',
    "promtail": "grafana/promtail:3.6.11 has a shell but ships no wget and no curl, so its "
    "/ready endpoint on 9080 is unreachable from inside the container",
    "otel-collector": "otel/opentelemetry-collector-contrib:0.157.0 ships FROM scratch with no "
    "shell; self-heal falls back to Docker's container State for it",
}


def test_every_long_running_compose_service_can_be_asked_if_it_is_working() -> None:
    """Because "running" is what self-heal believes when nothing says otherwise.

    `self_heal._docker_compose_container_statuses` treats an empty `Health`
    field as healthy whenever the container is running, so a service with no
    healthcheck is a service that reports healthy while dead. That is the
    #4208 class on the Compose side, and the waiver list above is the
    difference between "we checked" and "we forgot".
    """
    services = _compose()["services"]
    unguarded = {
        name
        for name, service in services.items()
        if "healthcheck" not in service and name not in COMPOSE_HEALTHCHECK_WAIVERS
    }
    assert not unguarded, (
        f"{sorted(unguarded)} report healthy to self-heal whenever the container is "
        "running. Add a healthcheck, or a waiver in COMPOSE_HEALTHCHECK_WAIVERS saying "
        "which verified property of the image makes one impossible (#4208)"
    )
    stale = set(COMPOSE_HEALTHCHECK_WAIVERS) - set(services)
    assert not stale, f"waivers for services that no longer exist: {sorted(stale)}"


# Every one of these answers on a port, and its probe asks it on that port --
# which for a request-serving workload IS the job, unlike a worker whose job
# leaves no trace on any socket.
K8S_READINESS_WAIVERS = {
    "cassandra": "readiness is a cqlsh/tcp check on the CQL port it exists to serve",
    "ollama": "readiness is a tcp check on the inference port it exists to serve",
}


def test_every_k8s_workload_declares_a_readiness_probe() -> None:
    """The #4208 class guard, on the substrate the defect was found on.

    glitchtip-worker shipped with no probe at all, so no Kubernetes surface --
    `kubectl get pods`, the `nyxgpt ops status` workload lines, the SRE
    dashboard rows that read them, or scripts/k8s-local-smoke.sh's rollout
    wait -- could ever report it broken. Fixing the one instance leaves the
    next workload free to repeat it; this is what stops that.
    """
    missing: list[str] = []
    for manifest in sorted(REPO_ROOT.glob("k8s/**/*.yaml")):
        for doc in yaml.safe_load_all(manifest.read_text()):
            if not doc or doc.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet"):
                continue
            name = doc["metadata"]["name"]
            if name in K8S_READINESS_WAIVERS:
                continue
            for container in doc["spec"]["template"]["spec"]["containers"]:
                if "readinessProbe" not in container:
                    missing.append(f"{manifest.relative_to(REPO_ROOT)}:{name}/{container['name']}")
    assert not missing, (
        f"{missing} are Ready the moment their container starts, so every surface that "
        "reads readiness reports them working before they are (#4208)"
    )


# --------------------------------------------------------------------------
# What the OPERATOR sees: the dead-letter queue (#4208 AC3)
# --------------------------------------------------------------------------


def _llen_responses(*values: object) -> object:
    """Fake `kubectl exec ... redis-cli LLEN` results, in call order."""
    queue = list(values)

    def _run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        value = queue.pop(0)
        if value is None:
            return SimpleNamespace(returncode=1, stdout="", stderr="error")
        return SimpleNamespace(returncode=0, stdout=f"{value}\n", stderr="")

    return _run


def test_a_growing_dead_letter_queue_is_reported_to_the_operator() -> None:
    """#4208 AC3: visible on `ops status`, not only to someone with redis-cli.

    The reference cluster in the issue put 6 items in `{vt}:q:failed` within
    two minutes of start-up, and nothing on any surface said so -- the worker
    was up, consuming, and losing events, which is the one failure its
    readiness probe cannot express.
    """
    with (
        patch.object(ops, "_which", return_value="/usr/bin/kubectl"),
        patch.object(ops, "_run", _llen_responses(6, 12)),
    ):
        result = ops._k8s_error_ingestion_result()
    # `[ATTENTION]`, which in this codebase keeps `ok=True` on purpose: a
    # misconfiguration an operator must act on is not a reason to fail an
    # install that otherwise brought a working stack up (#3956). The LABEL is
    # the assertion, because that is what the operator reads.
    assert result.status == ops.ATTENTION_LABEL
    assert "6 ingestion task(s) in the worker's dead-letter queue" in result.message
    assert "12 event task(s) waiting" in result.details


def test_an_empty_dead_letter_queue_is_green_and_still_says_the_backlog() -> None:
    with (
        patch.object(ops, "_which", return_value="/usr/bin/kubectl"),
        patch.object(ops, "_run", _llen_responses(0, 3)),
    ):
        result = ops._k8s_error_ingestion_result()
    assert result.ok and not result.status
    assert "no ingestion task has been dead-lettered" in result.message
    assert "3 event task(s) waiting" in result.details


def test_an_unaskable_broker_is_no_data_rather_than_a_failure() -> None:
    """An un-asked question is not a failed one (#3956's distinction)."""
    with (
        patch.object(ops, "_which", return_value="/usr/bin/kubectl"),
        patch.object(ops, "_run", _llen_responses(None)),
    ):
        result = ops._k8s_error_ingestion_result()
    assert result.status == ops.K8S_NO_DATA_LABEL
    assert "could not ask" in result.message


def test_the_dlq_line_is_part_of_the_errors_data_flow_block() -> None:
    """Printed with its neighbours, not somewhere an operator has to go find.

    `ops status`'s three error questions are deliberately three lines -- #3990
    found that collapsing "can Grafana log in" into "are errors flowing" hid
    an api reporting nowhere behind a green tick. This is the fourth, and it
    belongs in the same block.
    """

    source = Path(ops.__file__).read_text()
    block = source.split("def _k8s_observability_data_flow")[1].split("\ndef ")[0]
    assert "_k8s_error_ingestion_result()" in block


def test_the_vtasks_key_names_match_the_backend() -> None:
    """`{vt}` is VTASKS_VALKEY_PREFIX's default, and conf.py appends the colon.

    `ValkeyTaskBackend._get_key` is `f"{self.prefix}{key}"` with `prefix`
    reading `settings.VTASKS_VALKEY_PREFIX`, which `VtasksSettings.__getattr__`
    forces a trailing `:` onto. So the DLQ is `{vt}:q:failed` -- confirmed
    against a running worker's keyspace.
    """
    assert ops.VTASKS_DLQ_KEY == "{vt}:q:failed"
    assert ops.VTASKS_INGEST_QUEUE_KEY == "{vt}:q:ingest"


def test_the_probe_is_a_real_file_and_not_a_broken_symlink() -> None:
    assert PROBE_PATH.is_file()
    assert sys.version_info >= (3, 10)  # the probe uses `X | None` annotations
