import json
import logging
from unittest.mock import MagicMock

import pytest

from nyxgpt import canary
from nyxgpt import metrics as prom_metrics

# Captured before the autouse fixture stubs the module attribute, so the
# attribution tests below can drive the real implementation against a faked
# `_run` while every other test is protected from shelling out to a cluster.
REAL_TRACK_METRICS = canary.track_metrics

# The default histogram bucket bounds prometheus_client uses for
# `nyxgpt_http_request_duration_seconds` (see metrics.py).
HISTOGRAM_BUCKETS = [
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
]


class CP:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _pod_list(*names, ready=True, phase="Running"):
    """Render a `kubectl get pods -o json` body for the given Pod names."""
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": name},
                    "status": {
                        "phase": phase,
                        "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                    },
                }
                for name in names
            ]
        }
    )


def _exposition(*, ok=0, errors=0, probes=0, latency_s=0.05, path="/api/v1/chat"):
    """Render a Pod's Prometheus exposition for the HTTP families canary.py reads.

    `probes` adds `/health` and `/metrics` samples with an absurd latency, so
    a test can prove they are excluded from both the request count and p95
    rather than merely not dominating them.
    """
    lines = [
        "# HELP nyxgpt_http_requests_total Total HTTP requests handled by the API",
        "# TYPE nyxgpt_http_requests_total counter",
    ]
    if ok:
        lines.append(
            f'nyxgpt_http_requests_total{{method="GET",path="{path}",status="200"}} {float(ok)}'
        )
    if errors:
        lines.append(
            f'nyxgpt_http_requests_total{{method="GET",path="{path}",status="500"}} {float(errors)}'
        )
    total = float(ok + errors)
    lines.append("# TYPE nyxgpt_http_request_duration_seconds histogram")
    for bound in HISTOGRAM_BUCKETS:
        cumulative = total if bound >= latency_s else 0.0
        lines.append(
            f"nyxgpt_http_request_duration_seconds_bucket"
            f'{{method="GET",path="{path}",le="{bound}"}} {cumulative}'
        )
    lines.append(
        f"nyxgpt_http_request_duration_seconds_bucket"
        f'{{method="GET",path="{path}",le="+Inf"}} {total}'
    )
    lines.append(
        f'nyxgpt_http_request_duration_seconds_count{{method="GET",path="{path}"}} {total}'
    )
    lines.append(
        f'nyxgpt_http_request_duration_seconds_sum{{method="GET",path="{path}"}} '
        f"{total * latency_s}"
    )

    for probe_path in ("/health", "/metrics"):
        if not probes:
            continue
        lines.append(
            f'nyxgpt_http_requests_total{{method="GET",path="{probe_path}",status="200"}} '
            f"{float(probes)}"
        )
        for bound in HISTOGRAM_BUCKETS:
            lines.append(
                f"nyxgpt_http_request_duration_seconds_bucket"
                f'{{method="GET",path="{probe_path}",le="{bound}"}} 0.0'
            )
        lines.append(
            f"nyxgpt_http_request_duration_seconds_bucket"
            f'{{method="GET",path="{probe_path}",le="+Inf"}} {float(probes)}'
        )
        lines.append(
            f'nyxgpt_http_request_duration_seconds_count{{method="GET",path="{probe_path}"}} '
            f"{float(probes)}"
        )
    return "\n".join(lines) + "\n"


def _fake_cluster(*, canary_pods=(), stable_pods=(), bodies=None):
    """A `_run` stub answering the Pod list and Pod-proxy scrape canary.py makes.

    `bodies` maps a Pod name to its exposition; a Pod missing from it answers
    the scrape with a non-zero exit, i.e. an unreadable /metrics.
    """
    bodies = bodies or {}

    def _fn(cmd, **_kwargs):
        if cmd[:3] == ["kubectl", "get", "pods"]:
            selector = cmd[cmd.index("-l") + 1]
            names = canary_pods if selector.endswith("track=canary") else stable_pods
            return CP(stdout=_pod_list(*names))
        if cmd[:3] == ["kubectl", "get", "--raw"]:
            pod = cmd[3].split("/pods/")[1].split(":")[0]
            if pod not in bodies:
                return CP(returncode=1, stderr="pods proxy unreachable")
            return CP(stdout=bodies[pod])
        return CP(returncode=0)

    return _fn


def _measured(requests=50, *, error_rate=0.0, p95_ms=100.0, pods=1):
    """A `track_metrics` stub reporting attributable vitals for whichever track is asked."""

    def _fn(track="canary", namespace=None, *, component="api"):
        return canary.TrackMetrics(
            track=track,
            attributable=True,
            source="pods",
            pods_ready=pods,
            pods_scraped=pods,
            total_requests=requests,
            error_rate_percent=error_rate,
            p95_latency_ms=p95_ms,
        )

    return _fn


def _deployment_json(*, replicas=1, ready=1, image="nyxgpt-api:local"):
    return json.dumps(
        {
            "spec": {
                "replicas": replicas,
                "template": {"spec": {"containers": [{"image": image}]}},
            },
            "status": {"readyReplicas": ready},
        }
    )


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "_state_path", lambda: tmp_path / "canary_state.json")
    monkeypatch.setattr(canary, "_which", lambda _: "/usr/local/bin/kubectl")
    monkeypatch.setattr(canary.time, "time", lambda: 1234.0)
    # Track-scoped metrics reach a real cluster through kubectl (#3829), so
    # unit tests get an "unattributable, no cluster" default: a test that
    # cares about attribution patches `_run` and calls REAL_TRACK_METRICS,
    # and no test can silently shell out to whatever cluster the runner has.
    monkeypatch.setattr(
        canary,
        "track_metrics",
        lambda track="canary", namespace=None, *, component="api": canary.TrackMetrics(
            track=track, attributable=False, reason="no cluster in this test"
        ),
    )
    monkeypatch.setattr(canary.ops_module, "terraform_stack_state", lambda: {})
    monkeypatch.delenv("NYXGPT_COMPOSE_FILE", raising=False)


def _healthy(namespace_unused=None):
    """A deployment_health stub returning "healthy" with a version, for promote()'s gate."""

    def _fn(name, ns):
        return canary.TrackHealth("healthy", f"{name} healthy", "1.2.3-abcd123")

    return _fn


def _kubernetes_mode(monkeypatch):
    """Put `status()` in the one mode where it reads per-track health at all (#3858).

    Outside Kubernetes mode `status()` skips `deployment_health` entirely, so a
    test about what the tracks report has to say which mode it is testing.
    """
    monkeypatch.setattr(canary, "current_mode", lambda: "kubernetes")


@pytest.mark.unit
def test_run_logs_cmd_rc_stderr_tail_on_nonzero_exit(caplog):
    # #3415 gap 5: subprocess evidence must reach Loki even though canary's
    # `_run` never raises (always check=False).
    with caplog.at_level("DEBUG", logger="nyxgpt.canary"):
        cp = canary._run(["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])

    assert cp.returncode == 3
    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected _run to log the non-zero exit"
    assert records[0].levelno == logging.WARNING
    assert "rc=3" in records[0].getMessage()
    assert records[0].returncode == 3
    assert "boom" in records[0].stderr_tail


@pytest.mark.unit
def test_run_expected_true_logs_debug_not_warning_on_nonzero_exit(caplog):
    with caplog.at_level("DEBUG", logger="nyxgpt.canary"):
        cp = canary._run(
            ["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
            expected=True,
        )

    assert cp.returncode == 3
    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected _run to log the non-zero exit at DEBUG"
    assert records[0].levelno == logging.DEBUG
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.unit
def test_load_state_logs_and_falls_back_on_corrupt_state_file(tmp_path, monkeypatch, caplog):
    state_path = tmp_path / "canary_state.json"
    state_path.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(canary, "_state_path", lambda: state_path)

    with caplog.at_level("WARNING", logger="nyxgpt.canary"):
        state = canary._load_state()

    assert state == {"active": False, "weight_percent": 0, "history": []}
    assert any("Failed to load canary state" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_deployment_health_healthy(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(stdout=_deployment_json()))
    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")
    assert result.state == "healthy"
    assert "healthy" in result.message
    assert result.version == "local"


@pytest.mark.unit
def test_deployment_health_not_ready(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(stdout=_deployment_json(ready=0)))
    result = canary.deployment_health("nyxgpt-api-canary", "nyxgpt")
    assert result.state == "unhealthy"
    assert "not healthy" in result.message


@pytest.mark.unit
def test_deployment_health_zero_replicas_is_not_deployed_not_an_alarm(monkeypatch):
    monkeypatch.setattr(
        canary, "_run", lambda cmd, **_k: CP(stdout=_deployment_json(replicas=0, ready=0))
    )
    result = canary.deployment_health("nyxgpt-api-canary", "nyxgpt")
    assert result.state == "not_deployed"
    assert "0 desired replicas" in result.message


@pytest.mark.unit
def test_deployment_health_unparseable_status(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(stdout="not json"))
    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")
    assert result.state == "error"
    assert "Could not parse status" in result.message


@pytest.mark.unit
def test_deployment_health_not_found_is_not_deployed_with_install_pointer(monkeypatch):
    """A missing Deployment (e.g. terraform/native mode) must render as not_deployed, never
    Unhealthy -- this is the #3409 bug: "Could not read deployment" was falsely an alarm."""
    monkeypatch.setattr(
        canary,
        "_run",
        lambda cmd, **_k: CP(
            returncode=1, stderr='Error from server (NotFound): deployments.apps "x" not found'
        ),
    )
    result = canary.deployment_health("nyxgpt-api-canary", "nyxgpt")
    assert result.state == "not_deployed"
    assert "nyxgpt ops install --kubernetes" in result.message


@pytest.mark.unit
def test_deployment_health_cluster_unreachable_is_not_deployed(monkeypatch):
    monkeypatch.setattr(
        canary,
        "_run",
        lambda cmd, **_k: CP(
            returncode=1, stderr="Unable to connect to the server: dial tcp: no such host"
        ),
    )
    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")
    assert result.state == "not_deployed"
    assert "No reachable Kubernetes cluster" in result.message


@pytest.mark.unit
def test_deployment_health_genuine_kubectl_error_is_distinguishable(monkeypatch):
    """A real kubectl failure against a reachable cluster (e.g. RBAC denial) must not be
    silently folded into "not_deployed" -- it needs its own honest "error" state."""
    monkeypatch.setattr(
        canary,
        "_run",
        lambda cmd, **_k: CP(returncode=1, stderr="Error from server (Forbidden): access denied"),
    )
    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")
    assert result.state == "error"
    assert "Could not read deployment" in result.message


@pytest.mark.unit
def test_load_state_handles_corrupted_json(tmp_path):
    """A corrupted state file must not raise; _load_state() should fall back
    to the default state instead of propagating the JSON parse error."""
    (tmp_path / "canary_state.json").write_text("not valid json{", encoding="utf-8")

    state = canary._load_state()

    assert state == {"active": False, "weight_percent": 0, "history": []}


@pytest.mark.unit
def test_scale_kubectl_missing(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    result = canary._scale("nyxgpt-api-canary", 2, "nyxgpt")
    assert not result.ok
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_set_image_kubectl_missing(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    result = canary._set_image("nyxgpt-api-canary", "nyxgpt-api:1.2.3-abcd", "nyxgpt")
    assert not result.ok
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_set_image_success(monkeypatch):
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", run_mock)
    result = canary._set_image("nyxgpt-api-canary", "nyxgpt-api:1.2.3-abcd", "nyxgpt")
    assert result.ok
    cmd = run_mock.call_args.args[0]
    assert cmd == [
        "kubectl",
        "set",
        "image",
        "deployment/nyxgpt-api-canary",
        "nyxgpt-api=nyxgpt-api:1.2.3-abcd",
        "-n",
        "nyxgpt",
    ]


@pytest.mark.unit
def test_wait_rollout_success_and_failure(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=0))
    assert canary._wait_rollout("nyxgpt-api-canary", "nyxgpt").ok

    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=1, stderr="timed out"))
    result = canary._wait_rollout("nyxgpt-api-canary", "nyxgpt", timeout_seconds=5)
    assert not result.ok
    assert "did not become healthy within 5s" in result.message


@pytest.mark.unit
def test_deployment_health_kubectl_missing(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")
    assert result.state == "not_deployed"
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_deployment_health_kubectl_missing_under_compose(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")
    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")
    assert result.state == "not_deployed"
    assert "kubectl not found" not in result.message
    assert "Kubernetes deployment mode" in result.message


@pytest.mark.unit
@pytest.mark.parametrize(
    "total,weight,expected",
    [
        (4, 0, (0, 4)),
        (4, 10, (1, 3)),
        (4, 50, (2, 2)),
        (4, 100, (4, 0)),
        # A one-replica pool cannot express a partial split, so the split
        # widens to 2 rather than returning stable=0 (#3833) -- stable=0
        # below 100% is a full cutover, not a canary.
        (1, 50, (1, 1)),
        (1, 10, (1, 1)),
        (1, 99, (1, 1)),
    ],
)
def test_split_replicas(total, weight, expected):
    assert canary._split_replicas(total, weight) == expected


@pytest.mark.unit
@pytest.mark.parametrize("total", [1, 2, 3, 4, 7])
@pytest.mark.parametrize("weight", [1, 10, 25, 33, 50, 66, 75, 90, 99])
def test_split_replicas_never_starves_stable_below_100_percent(total, weight):
    """AC #3833: no (pool, weight<100) pair may leave the stable track at 0 replicas."""
    canary_replicas, stable_replicas = canary._split_replicas(total, weight)
    assert stable_replicas >= 1
    assert canary_replicas >= 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "resting,weight,max_pool,expected",
    [
        # The pool grows only as far as the requested weight needs it to.
        (1, 50, 4, (1, 1, 50)),
        (1, 33, 4, (1, 2, 33)),
        (1, 25, 4, (1, 3, 25)),
        (1, 75, 4, (3, 1, 75)),
        # Not expressible within the ceiling -- closest split, reported honestly.
        (1, 10, 4, (1, 3, 25)),
        (1, 99, 4, (3, 1, 75)),
        # A ceiling of 2 is the cheapest possible rollout: one extra Pod.
        (1, 10, 2, (1, 1, 50)),
        # An operator-scaled stable is never shrunk below its resting count...
        (3, 25, 4, (1, 3, 25)),
        # ...even when that forces the pool past the configured ceiling.
        (6, 25, 4, (2, 4, 33)),
    ],
)
def test_plan_rollout(resting, weight, max_pool, expected):
    plan = canary._plan_rollout(resting, weight, max_pool)
    assert (plan.canary_replicas, plan.stable_replicas, plan.achieved_percent) == expected
    assert plan.pool == plan.canary_replicas + plan.stable_replicas
    assert plan.pool >= max(2, resting)


@pytest.mark.unit
def test_plan_rollout_describes_the_rounding_it_had_to_do():
    exact = canary._plan_rollout(1, 50, 4)
    assert exact.rounded is False
    assert exact.describe() == "50% (1/2 replicas)"

    rounded = canary._plan_rollout(1, 10, 4)
    assert rounded.rounded is True
    assert rounded.describe().startswith("25% (1/4 replicas); 10% is not expressible")
    assert "raise `[canary] total_replicas`" in rounded.describe()


@pytest.mark.unit
def test_current_mode_compose(monkeypatch):
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")
    assert canary.current_mode() == "compose"


@pytest.mark.unit
def test_current_mode_terraform(monkeypatch):
    monkeypatch.setattr(canary.ops_module, "terraform_stack_state", lambda: {"api": "running"})
    assert canary.current_mode() == "terraform"


@pytest.mark.unit
def test_current_mode_does_not_claim_terraform_from_reads_that_never_happened(monkeypatch):
    """#4022 review: a denied Docker session reads `unknown`, not `absent`.

    The old predicate was `state != "absent"`, so every unknown promoted a
    *native* install into "terraform" -- reached from the API process through
    `status()`, telling the operator canary "doesn't apply in terraform mode".
    D-027's rule for this function is that it answers "unknown" rather than
    asserting a substrate nothing could see.
    """
    monkeypatch.setattr(
        canary.ops_module,
        "terraform_stack_state",
        lambda: {"api": "unknown", "web": "unknown", "cassandra": "unknown"},
    )
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(stdout=""))

    mode = canary.current_mode()

    assert mode != "terraform", "asserted a substrate from reads that never happened"
    assert mode == "unknown", "an all-unknown read is not evidence of a native install either"


@pytest.mark.unit
def test_current_mode_still_sees_terraform_when_one_container_really_runs(monkeypatch):
    """The guard must not invert the true positive: a real container still counts."""
    monkeypatch.setattr(
        canary.ops_module,
        "terraform_stack_state",
        lambda: {"api": "running", "web": "unknown"},
    )
    assert canary.current_mode() == "terraform"


@pytest.mark.unit
def test_current_mode_kubernetes(monkeypatch):
    monkeypatch.setattr(
        canary, "_run", lambda cmd, **_k: CP(stdout="nyxgpt-api-stable-abc 1/1 Running")
    )
    assert canary.current_mode() == "kubernetes"


@pytest.mark.unit
def test_current_mode_falls_back_to_native(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(stdout=""))
    assert canary.current_mode() == "native"


@pytest.mark.unit
def test_status_mode_message_when_not_kubernetes(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(stdout=""))
    data = canary.status("nyxgpt")
    assert data["mode"] == "native"
    assert data["mode_supported"] is False
    assert "nyxgpt ops install --kubernetes" in data["mode_message"]


@pytest.mark.unit
def test_start_scales_canary_and_stable(monkeypatch):
    # The stable Deployment rests at 1 replica (#3833), so a 25% step has to
    # GROW the pool to 4 for the duration of the rollout: stable first (never
    # route a bigger share to the canary than asked), then canary.
    monkeypatch.setattr(canary, "_desired_replicas", lambda name, ns=None: 1)
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.start(namespace="nyxgpt", weight_percent=25, total_replicas=4)

    assert result.ok
    assert "25%" in result.message
    assert "returns to 1 replica" in result.message
    calls = [c.args[0] for c in scale_mock.call_args_list]
    assert calls[0][:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-stable"]
    assert "--replicas=3" in calls[0]
    assert calls[1][:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-canary"]
    assert "--replicas=1" in calls[1]

    state = canary._load_state()
    assert state["active"] is True
    assert state["weight_percent"] == 25
    assert state["total_replicas"] == 4
    assert state["resting_replicas"] == 1


@pytest.mark.unit
def test_start_reads_the_live_stable_replica_count(monkeypatch):
    """The resting count comes from the cluster, never from a constant (#3833).

    An operator who scaled stable to 3 must get 3 back when the rollout ends,
    and must not have the pool silently re-inflated to some hardcoded width.
    """
    monkeypatch.setattr(canary, "_desired_replicas", lambda name, ns=None: 3)
    monkeypatch.setattr(canary, "_run", MagicMock(return_value=CP(returncode=0)))

    result = canary.start(namespace="nyxgpt", weight_percent=25, total_replicas=4)

    assert result.ok
    assert "returns to 3 replicas" in result.message
    assert canary._load_state()["resting_replicas"] == 3


@pytest.mark.unit
def test_start_defaults_resting_to_the_manifest_count_when_unreadable(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=1, stderr="NotFound"))
    assert (
        canary._resting_replicas("nyxgpt-api-stable", "nyxgpt") == canary.DEFAULT_RESTING_REPLICAS
    )
    assert canary.DEFAULT_RESTING_REPLICAS == 1


@pytest.mark.unit
def test_start_reports_the_weight_it_actually_rounded_to(monkeypatch):
    """A 10% ask is not expressible in a 4-replica pool -- say so (#3833)."""
    monkeypatch.setattr(canary, "_desired_replicas", lambda name, ns=None: 1)
    monkeypatch.setattr(canary, "_run", MagicMock(return_value=CP(returncode=0)))

    result = canary.start(namespace="nyxgpt", weight_percent=10, total_replicas=4)

    assert result.ok
    assert "Started canary rollout at 25% (1/4 replicas)" in result.message
    assert "10% is not expressible in a pool of at most 4 replicas" in result.message
    assert canary._load_state()["weight_percent"] == 25


@pytest.mark.unit
def test_start_grows_the_pool_only_as_far_as_the_weight_needs(monkeypatch):
    """50% is exactly expressible at 2 replicas, so the ceiling of 4 is not spent."""
    monkeypatch.setattr(canary, "_desired_replicas", lambda name, ns=None: 1)
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.start(namespace="nyxgpt", weight_percent=50, total_replicas=4)

    assert result.ok
    assert "50% (1/2 replicas)" in result.message
    assert "not expressible" not in result.message
    replicas = [
        arg for call in scale_mock.call_args_list for arg in call.args[0] if "--replicas=" in arg
    ]
    assert replicas == ["--replicas=1", "--replicas=1"]


@pytest.mark.unit
def test_start_puts_stable_back_when_the_canary_scale_fails(monkeypatch):
    """A failed start must not leave an inflated pool standing (#3833)."""
    monkeypatch.setattr(canary, "_desired_replicas", lambda name, ns=None: 1)

    def fake_run(cmd, **_k):
        if "nyxgpt-api-canary" in cmd:
            return CP(returncode=1, stderr="boom")
        return CP(returncode=0)

    scale_mock = MagicMock(side_effect=fake_run)
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.start(namespace="nyxgpt", weight_percent=25, total_replicas=4)

    assert not result.ok
    scaled = [
        (call.args[0][3], [a for a in call.args[0] if a.startswith("--replicas=")])
        for call in scale_mock.call_args_list
        if call.args[0][1] == "scale"
    ]
    assert scaled == [
        ("nyxgpt-api-stable", ["--replicas=3"]),
        ("nyxgpt-api-canary", ["--replicas=1"]),
        ("nyxgpt-api-stable", ["--replicas=1"]),
    ]
    assert canary._load_state()["active"] is False


@pytest.mark.unit
def test_start_refuses_when_already_active(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 25, "history": []})
    run_mock = MagicMock()
    monkeypatch.setattr(canary, "_run", run_mock)

    result = canary.start(namespace="nyxgpt")

    assert not result.ok
    assert "already in progress" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
def test_start_kubectl_missing(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    result = canary.start(namespace="nyxgpt")
    assert not result.ok
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_start_kubectl_missing_under_compose(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")
    result = canary.start(namespace="nyxgpt")
    assert not result.ok
    assert "Kubernetes deployment mode" in result.message


@pytest.mark.unit
def test_start_returns_error_when_canary_scale_fails(monkeypatch):
    def fake_run(cmd, **_k):
        if "nyxgpt-api-canary" in cmd:
            return CP(returncode=1, stderr="canary scale boom")
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", fake_run)

    result = canary.start(namespace="nyxgpt", weight_percent=10, total_replicas=4)

    assert not result.ok
    assert "kubectl scale failed for nyxgpt-api-canary" in result.message
    # Rollout must not be recorded as started since the scale failed.
    state = canary._load_state()
    assert state.get("active") is not True


@pytest.mark.unit
def test_start_returns_error_when_stable_scale_fails(monkeypatch):
    def fake_run(cmd, **_k):
        if "nyxgpt-api-stable" in cmd:
            return CP(returncode=1, stderr="stable scale boom")
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", fake_run)

    result = canary.start(namespace="nyxgpt", weight_percent=10, total_replicas=4)

    assert not result.ok
    assert "kubectl scale failed for nyxgpt-api-stable" in result.message
    state = canary._load_state()
    assert state.get("active") is not True


@pytest.mark.unit
def test_status_reports_active_state_and_health(monkeypatch):
    _kubernetes_mode(monkeypatch)
    canary._save_state({"active": True, "weight_percent": 25, "history": []})
    monkeypatch.setattr(
        canary,
        "deployment_health",
        lambda name, ns: canary.TrackHealth("healthy", f"{name} healthy", "1.0.0-abcd"),
    )

    data = canary.status("nyxgpt")

    assert data["namespace"] == "nyxgpt"
    assert data["active"] is True
    assert data["weight_percent"] == 25
    assert data["stable"]["state"] == "healthy"
    assert data["stable"]["version"] == "1.0.0-abcd"
    assert data["canary"]["state"] == "healthy"
    assert data["metrics"]["track"] == "canary"
    assert data["metrics"]["attributable"] is False
    assert data["stable_metrics"]["track"] == "stable"
    assert data["available"] is True
    assert data["unavailable_reason"] is None


@pytest.mark.unit
def test_status_reports_the_elastic_pool(monkeypatch):
    """The dashboard has to be able to see what the rollout inflated, and to what (#3833)."""
    _kubernetes_mode(monkeypatch)
    monkeypatch.setattr(
        canary,
        "deployment_health",
        lambda name, ns: canary.TrackHealth("healthy", f"{name} healthy", "1.0.0-abcd"),
    )

    idle = canary.status("nyxgpt")
    assert idle["pool_replicas"] == 0
    assert idle["resting_replicas"] == 0

    canary._save_state(
        {
            "active": True,
            "weight_percent": 25,
            "total_replicas": 4,
            "resting_replicas": 1,
            "history": [],
        }
    )
    active = canary.status("nyxgpt")
    assert active["pool_replicas"] == 4
    assert active["resting_replicas"] == 1


@pytest.mark.unit
def test_status_reports_unavailable_when_kubectl_missing_under_compose(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")

    data = canary.status("nyxgpt")

    assert data["available"] is False
    assert "Kubernetes deployment mode" in data["unavailable_reason"]


@pytest.mark.unit
def test_evaluate_no_active_rollout():
    result = canary.evaluate("nyxgpt")
    assert not result.ok
    assert "No canary rollout" in result.message


@pytest.mark.unit
def test_evaluate_insufficient_data(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "history": []})
    monkeypatch.setattr(canary, "track_metrics", _measured(5, p95_ms=100.0))

    result = canary.evaluate("nyxgpt", min_requests=20)

    assert result.ok
    assert "Insufficient data" in result.message


@pytest.mark.unit
def test_evaluate_passes_within_thresholds(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "history": []})
    monkeypatch.setattr(canary, "track_metrics", _measured(50, error_rate=1.0, p95_ms=500.0))

    result = canary.evaluate(
        "nyxgpt", error_rate_threshold_percent=5.0, latency_p95_threshold_ms=2000.0, min_requests=20
    )

    assert result.ok
    assert "safe to promote" in result.message


@pytest.mark.unit
def test_evaluate_triggers_automatic_rollback_on_error_rate_breach(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 25, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "track_metrics", _measured(50, error_rate=12.0, p95_ms=200.0))
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=0))

    result = canary.evaluate(
        "nyxgpt", error_rate_threshold_percent=5.0, latency_p95_threshold_ms=2000.0, min_requests=20
    )

    assert not result.ok
    assert "automatically rolled back" in result.message
    state = canary._load_state()
    assert state["active"] is False
    assert state["weight_percent"] == 0


@pytest.mark.unit
def test_evaluate_triggers_automatic_rollback_on_latency_breach(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 25, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "track_metrics", _measured(50, p95_ms=5000.0))
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=0))

    result = canary.evaluate(
        "nyxgpt", error_rate_threshold_percent=5.0, latency_p95_threshold_ms=2000.0, min_requests=20
    )

    assert not result.ok
    assert "p95 latency" in result.message


@pytest.mark.unit
def test_promote_increases_weight(monkeypatch):
    canary._save_state(
        {
            "active": True,
            "weight_percent": 25,
            "total_replicas": 4,
            "resting_replicas": 1,
            "stable_replicas": 3,
            "history": [],
        }
    )
    monkeypatch.setattr(canary, "deployment_health", _healthy())
    monkeypatch.setattr(canary, "_desired_replicas", lambda name, ns=None: 3)
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.promote(namespace="nyxgpt", step_percent=25, total_replicas=4)

    assert result.ok
    # 50% is exactly expressible at 2 replicas, so the step re-plans the pool
    # DOWN from 4 rather than re-slicing a fixed one (#3833).
    assert "Promoted canary to 50% (1/2 replicas)" in result.message
    state = canary._load_state()
    assert state["weight_percent"] == 50
    assert state["total_replicas"] == 2
    assert state["resting_replicas"] == 1
    assert state["active"] is True
    scaled = [
        (call.args[0][3], [a for a in call.args[0] if a.startswith("--replicas=")])
        for call in scale_mock.call_args_list
        if call.args[0][1] == "scale"
    ]
    # Canary grows before stable shrinks, so the pool never dips below the
    # capacity it already had.
    assert scaled == [
        ("nyxgpt-api-canary", ["--replicas=1"]),
        ("nyxgpt-api-stable", ["--replicas=1"]),
    ]


@pytest.mark.unit
def test_promote_restores_the_resting_count_recorded_at_start(monkeypatch):
    """The final promote deflates the pool to what stable rested at, not to the ceiling (#3833)."""
    canary._save_state(
        {
            "active": True,
            "weight_percent": 75,
            "total_replicas": 4,
            "resting_replicas": 2,
            "stable_replicas": 1,
            "history": [],
        }
    )
    monkeypatch.setattr(canary, "deployment_health", _healthy())
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.promote(namespace="nyxgpt", step_percent=25, total_replicas=4)

    assert result.ok
    assert "stable back to its resting 2 replicas" in result.message
    scaled = [
        (call.args[0][3], [a for a in call.args[0] if a.startswith("--replicas=")])
        for call in scale_mock.call_args_list
        if call.args[0][1] == "scale"
    ]
    assert scaled == [
        ("nyxgpt-api-canary", ["--replicas=0"]),
        ("nyxgpt-api-stable", ["--replicas=2"]),
    ]


@pytest.mark.unit
def test_promote_restores_a_pre_3833_state_files_fixed_pool(monkeypatch):
    """A rollout started before #3833 recorded only `total_replicas` -- that WAS its resting count."""
    canary._save_state({"active": True, "weight_percent": 90, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "deployment_health", _healthy())
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.promote(namespace="nyxgpt", step_percent=25, total_replicas=4)

    assert result.ok
    assert "stable back to its resting 4 replicas" in result.message


@pytest.mark.unit
def test_promote_refuses_when_canary_unhealthy(monkeypatch):
    """Weight shifts must refuse to send more traffic to an unhealthy canary (#3409)."""
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})
    monkeypatch.setattr(
        canary, "deployment_health", lambda name, ns: canary.TrackHealth("unhealthy", "not ready")
    )
    run_mock = MagicMock()
    monkeypatch.setattr(canary, "_run", run_mock)

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert not result.ok
    assert "Refusing to shift more traffic" in result.message
    run_mock.assert_not_called()
    state = canary._load_state()
    assert state["weight_percent"] == 10


@pytest.mark.unit
def test_promote_finalizes_by_copying_canary_version_to_stable(monkeypatch):
    """At 100%, promotion must copy canary's image to stable, wait for its rollout, then
    scale canary back to 0 and stable to total -- "returns weight to 100% stable"."""
    canary._save_state({"active": True, "weight_percent": 90, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "deployment_health", _healthy())
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", run_mock)

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert result.ok
    assert "Promoted 1.2.3-abcd123" in result.message
    assert "nyxgpt-api-stable" in result.message

    calls = [c.args[0] for c in run_mock.call_args_list]
    assert calls[0] == [
        "kubectl",
        "set",
        "image",
        "deployment/nyxgpt-api-stable",
        "nyxgpt-api=nyxgpt-api:1.2.3-abcd123",
        "-n",
        "nyxgpt",
    ]
    assert calls[1][:4] == ["kubectl", "rollout", "status", "deployment/nyxgpt-api-stable"]
    assert calls[2][:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-canary"]
    assert "--replicas=0" in calls[2]
    assert calls[3][:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-stable"]
    assert "--replicas=4" in calls[3]

    state = canary._load_state()
    assert state["weight_percent"] == 0
    assert state["active"] is False


@pytest.mark.unit
def test_promote_finalize_stops_if_stable_rollout_fails(monkeypatch):
    """A failed stable rollout during promotion must not touch canary's replica count."""
    canary._save_state({"active": True, "weight_percent": 90, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "deployment_health", _healthy())

    def fake_run(cmd, **_k):
        if cmd[:3] == ["kubectl", "rollout", "status"]:
            return CP(returncode=1, stderr="timed out")
        return CP(returncode=0)

    scale_mock = MagicMock(side_effect=fake_run)
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert not result.ok
    assert "did not become healthy" in result.message
    calls = [c.args[0] for c in scale_mock.call_args_list]
    assert not any(c[:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-canary"] for c in calls)
    state = canary._load_state()
    assert state["active"] is True
    assert state["weight_percent"] == 90


@pytest.mark.unit
def test_promote_no_active_rollout():
    result = canary.promote(namespace="nyxgpt")
    assert not result.ok
    assert "No canary rollout" in result.message


@pytest.mark.unit
def test_promote_kubectl_missing(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_which", lambda _: None)

    result = canary.promote(namespace="nyxgpt")

    assert not result.ok
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_promote_returns_error_when_canary_scale_fails(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "deployment_health", _healthy())

    def fake_run(cmd, **_k):
        if "nyxgpt-api-canary" in cmd:
            return CP(returncode=1, stderr="boom")
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", fake_run)

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert not result.ok
    assert "kubectl scale failed for nyxgpt-api-canary" in result.message
    # Weight must be unchanged since the scale failed before state was saved.
    state = canary._load_state()
    assert state["weight_percent"] == 10


@pytest.mark.unit
def test_promote_returns_error_when_stable_scale_fails(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "deployment_health", _healthy())

    def fake_run(cmd, **_k):
        if "nyxgpt-api-stable" in cmd:
            return CP(returncode=1, stderr="boom")
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", fake_run)

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert not result.ok
    assert "kubectl scale failed for nyxgpt-api-stable" in result.message
    state = canary._load_state()
    assert state["weight_percent"] == 10


@pytest.mark.unit
def test_rollback_scales_canary_to_zero_and_restores_stable(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.rollback(namespace="nyxgpt")

    assert result.ok
    assert "Rolled back canary rollout from 50% to 0%" in result.message
    calls = [c.args[0] for c in scale_mock.call_args_list]
    assert calls[0][:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-canary"]
    assert "--replicas=0" in calls[0]
    assert calls[1][:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-stable"]
    assert "--replicas=4" in calls[1]

    state = canary._load_state()
    assert state["active"] is False
    assert state["weight_percent"] == 0


@pytest.mark.unit
def test_rollback_deflates_the_pool_to_the_resting_count(monkeypatch):
    """Ending a rollout gives back the capacity the rollout borrowed (#3833)."""
    canary._save_state(
        {
            "active": True,
            "weight_percent": 25,
            "total_replicas": 4,
            "resting_replicas": 1,
            "stable_replicas": 3,
            "history": [],
        }
    )
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.rollback(namespace="nyxgpt", total_replicas=4)

    assert result.ok
    assert "restored to its resting 1 replica" in result.message
    calls = [c.args[0] for c in scale_mock.call_args_list]
    assert "--replicas=0" in calls[0]
    assert calls[1][:4] == ["kubectl", "scale", "deployment", "nyxgpt-api-stable"]
    assert "--replicas=1" in calls[1]
    assert canary._load_state()["total_replicas"] == 1


@pytest.mark.unit
def test_rollback_no_active_rollout():
    result = canary.rollback(namespace="nyxgpt")
    assert not result.ok
    assert "No canary rollout" in result.message


@pytest.mark.unit
def test_rollback_kubectl_missing(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_which", lambda _: None)

    result = canary.rollback(namespace="nyxgpt")

    assert not result.ok
    assert "kubectl not found" in result.message
    # Must not have been marked as rolled back since we bailed before scaling.
    state = canary._load_state()
    assert state["active"] is True


@pytest.mark.unit
def test_rollback_returns_error_when_canary_scale_fails(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=1, stderr="boom"))

    result = canary.rollback(namespace="nyxgpt")

    assert not result.ok
    assert "kubectl scale failed for nyxgpt-api-canary" in result.message
    # State must be unchanged since we returned before saving.
    state = canary._load_state()
    assert state["active"] is True
    assert state["weight_percent"] == 50


@pytest.mark.unit
def test_rollback_reports_partial_failure_but_still_cuts_canary_traffic(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})

    def fake_run(cmd, **_k):
        if "nyxgpt-api-canary" in cmd:
            return CP(returncode=0)
        return CP(returncode=1, stderr="boom")

    monkeypatch.setattr(canary, "_run", fake_run)

    result = canary.rollback(namespace="nyxgpt")

    assert result.ok
    assert "Canary traffic stopped" in result.message
    state = canary._load_state()
    assert state["active"] is False


@pytest.mark.unit
def test_track_metrics_unattributable_when_kubectl_missing(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)

    metrics = REAL_TRACK_METRICS("canary", "nyxgpt")

    assert metrics.attributable is False
    assert "kubectl not found" in metrics.reason
    assert metrics.total_requests == 0


def _metric_value(name, **labels):
    return prom_metrics.REGISTRY.get_sample_value(name, labels or None)


@pytest.mark.unit
def test_deploy_builds_sets_image_and_waits_for_rollout(monkeypatch):
    monkeypatch.setattr(canary.ops_module, "project_version", lambda: "1.2.3")
    monkeypatch.setattr(canary, "_git_short_sha", lambda: "abcd123")
    build_mock = MagicMock(return_value=[canary.CanaryResult(True, "built")])
    monkeypatch.setattr(canary.ops_module, "build_and_load_k8s_image", build_mock)
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", run_mock)

    result = canary.deploy(namespace="nyxgpt")

    assert result.ok
    assert "nyxgpt-api:1.2.3-abcd123" in result.message
    build_mock.assert_called_once_with("nyxgpt-api:1.2.3-abcd123")
    calls = [c.args[0] for c in run_mock.call_args_list]
    assert calls[0] == [
        "kubectl",
        "set",
        "image",
        "deployment/nyxgpt-api-canary",
        "nyxgpt-api=nyxgpt-api:1.2.3-abcd123",
        "-n",
        "nyxgpt",
    ]
    assert calls[1][:4] == ["kubectl", "rollout", "status", "deployment/nyxgpt-api-canary"]
    state = canary._load_state()
    assert state["history"][-1]["action"] == "deploy"
    assert state["history"][-1]["version"] == "nyxgpt-api:1.2.3-abcd123"
    assert _metric_value("nyxgpt_canary_events_total", action="deploy", result="ok") >= 1


@pytest.mark.unit
def test_deploy_kubectl_missing(monkeypatch):
    monkeypatch.setattr(canary, "_which", lambda _: None)
    result = canary.deploy(namespace="nyxgpt")
    assert not result.ok
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_deploy_build_failure_never_touches_stable(monkeypatch):
    monkeypatch.setattr(
        canary.ops_module,
        "build_and_load_k8s_image",
        lambda image: [canary.CanaryResult(False, "docker build failed")],
    )
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", run_mock)

    result = canary.deploy(namespace="nyxgpt")

    assert not result.ok
    assert "Failed to build/load" in result.message
    # git rev-parse (to build the version tag) is fine; kubectl must never be called.
    kubectl_calls = [c for c in run_mock.call_args_list if c.args[0][0] == "kubectl"]
    assert kubectl_calls == []


@pytest.mark.unit
def test_deploy_rollout_failure_leaves_stable_untouched(monkeypatch):
    monkeypatch.setattr(
        canary.ops_module,
        "build_and_load_k8s_image",
        lambda image: [canary.CanaryResult(True, "built")],
    )

    def fake_run(cmd, **_k):
        if cmd[:3] == ["kubectl", "rollout", "status"]:
            return CP(returncode=1, stderr="timed out")
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", fake_run)

    result = canary.deploy(namespace="nyxgpt")

    assert not result.ok
    assert "stable was not touched" in result.message


@pytest.mark.unit
def test_start_logs_and_records_metrics(monkeypatch, caplog):
    monkeypatch.setattr(canary, "_desired_replicas", lambda name, ns=None: 1)
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=0))

    with caplog.at_level("INFO"):
        result = canary.start(namespace="nyxgpt", weight_percent=25, total_replicas=4)

    assert result.ok
    assert "canary: starting rollout at 25%" in caplog.text
    assert "canary: Started canary rollout at 25%" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="start", result="ok") >= 1
    assert _metric_value("nyxgpt_canary_rollout_active") == 1
    assert _metric_value("nyxgpt_canary_weight_percent") == 25


@pytest.mark.unit
def test_start_logs_and_records_metric_on_scale_failure(monkeypatch, caplog):
    def fake_run(cmd, **_k):
        if "nyxgpt-api-canary" in cmd:
            return CP(returncode=1, stderr="boom")
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", fake_run)

    with caplog.at_level("ERROR"):
        result = canary.start(namespace="nyxgpt", weight_percent=10, total_replicas=4)

    assert not result.ok
    assert "canary: start failed scaling canary" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="start", result="failed") >= 1


@pytest.mark.unit
def test_evaluate_pass_logs_and_records_metric(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 10, "history": []})
    monkeypatch.setattr(canary, "track_metrics", _measured(50, error_rate=1.0, p95_ms=500.0))

    with caplog.at_level("INFO"):
        result = canary.evaluate("nyxgpt", min_requests=20)

    assert result.ok
    assert "canary: evaluate passed" in caplog.text
    assert _metric_value("nyxgpt_canary_evaluations_total", result="pass") >= 1


@pytest.mark.unit
def test_evaluate_insufficient_data_logs_and_records_metric(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 10, "history": []})
    monkeypatch.setattr(canary, "track_metrics", _measured(5, p95_ms=100.0))

    with caplog.at_level("INFO"):
        result = canary.evaluate("nyxgpt", min_requests=20)

    assert result.ok
    assert "canary: evaluate holding, insufficient data" in caplog.text
    assert _metric_value("nyxgpt_canary_evaluations_total", result="insufficient_data") >= 1


@pytest.mark.unit
def test_evaluate_regression_logs_and_triggers_auto_rollback(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 25, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "track_metrics", _measured(50, error_rate=12.0, p95_ms=200.0))
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=0))

    with caplog.at_level("INFO"):
        result = canary.evaluate(
            "nyxgpt",
            error_rate_threshold_percent=5.0,
            latency_p95_threshold_ms=2000.0,
            min_requests=20,
        )

    assert not result.ok
    assert "canary: evaluate detected regression" in caplog.text
    assert "trigger=auto" in caplog.text
    assert _metric_value("nyxgpt_canary_evaluations_total", result="regression") >= 1
    assert _metric_value("nyxgpt_canary_events_total", action="rollback", result="ok") >= 1
    assert _metric_value("nyxgpt_canary_auto_rollback_total", component="api") >= 1


@pytest.mark.unit
def test_promote_logs_and_records_metrics(monkeypatch, caplog):
    canary._save_state(
        {
            "active": True,
            "weight_percent": 25,
            "total_replicas": 4,
            "resting_replicas": 1,
            "stable_replicas": 3,
            "history": [],
        }
    )
    monkeypatch.setattr(canary, "deployment_health", _healthy())
    monkeypatch.setattr(canary, "_desired_replicas", lambda name, ns=None: 3)
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=0))

    with caplog.at_level("INFO"):
        result = canary.promote(namespace="nyxgpt", step_percent=50, total_replicas=4)

    assert result.ok
    assert "canary: promoting rollout from 25% to 75%" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="promote", result="ok") >= 1
    assert _metric_value("nyxgpt_canary_weight_percent") == 75


@pytest.mark.unit
def test_promote_fully_promoted_logs_and_clears_active_gauge(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 90, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "deployment_health", _healthy())
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=0))

    with caplog.at_level("INFO"):
        result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert result.ok
    assert "canary: Promoted 1.2.3-abcd123 to nyxgpt-api-stable" in caplog.text
    assert _metric_value("nyxgpt_canary_rollout_active") == 0


@pytest.mark.unit
def test_rollback_logs_and_records_metric_manual_trigger(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=0))

    with caplog.at_level("INFO"):
        result = canary.rollback(namespace="nyxgpt")

    assert result.ok
    assert "canary: rolling back from 50% (trigger=manual)" in caplog.text
    assert "canary: rolled back from 50% to 0% (trigger=manual)" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="rollback", result="ok") >= 1
    assert _metric_value("nyxgpt_canary_rollout_active") == 0
    assert _metric_value("nyxgpt_canary_weight_percent") == 0


@pytest.mark.unit
def test_rollback_logs_and_records_metric_on_canary_scale_failure(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=1, stderr="boom"))

    with caplog.at_level("ERROR"):
        result = canary.rollback(namespace="nyxgpt")

    assert not result.ok
    assert "canary: rollback failed scaling canary to 0" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="rollback", result="failed") >= 1


@pytest.mark.unit
def test_rollback_logs_partial_failure_metric(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 50, "total_replicas": 4, "history": []})

    def fake_run(cmd, **_k):
        if "nyxgpt-api-canary" in cmd:
            return CP(returncode=0)
        return CP(returncode=1, stderr="boom")

    monkeypatch.setattr(canary, "_run", fake_run)

    with caplog.at_level("WARNING"):
        result = canary.rollback(namespace="nyxgpt")

    assert result.ok
    assert "canary: rollback partially failed" in caplog.text
    assert _metric_value("nyxgpt_canary_events_total", action="rollback", result="partial") >= 1


@pytest.mark.unit
def test_status_updates_rollout_gauges(monkeypatch):
    _kubernetes_mode(monkeypatch)
    canary._save_state({"active": True, "weight_percent": 42, "history": []})
    monkeypatch.setattr(
        canary,
        "deployment_health",
        lambda name, ns: canary.TrackHealth("healthy", f"{name} healthy", "1.0.0"),
    )

    canary.status("nyxgpt")

    assert _metric_value("nyxgpt_canary_rollout_active") == 1
    assert _metric_value("nyxgpt_canary_weight_percent") == 42
    assert _metric_value("nyxgpt_canary_track_version_info", track="stable", version="1.0.0") == 1
    assert _metric_value("nyxgpt_canary_track_version_info", track="canary", version="1.0.0") == 1


@pytest.mark.unit
def test_canary_lifecycle_actions_recorded_via_ops_module(monkeypatch):
    """Deploy/start/promote/rollback must funnel through ops._record_ops_action per #3390."""
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=0))

    canary.start(namespace="nyxgpt", weight_percent=10, total_replicas=4)

    assert (
        _metric_value(
            "nyxgpt_ops_actions_total", command="canary-start", service="api", result="success"
        )
        >= 1
    )


# --- Component parameter (#3419: web canary + documented ollama infeasibility) ---


@pytest.mark.unit
def test_component_spec_unknown_component_is_rejected():
    spec, err = canary._component_spec("cassandra")
    assert spec is None
    assert err is not None
    assert not err.ok
    assert "Unknown canary component" in err.message
    assert "'cassandra'" in err.message


@pytest.mark.unit
def test_component_spec_ollama_is_rejected_with_documented_reason():
    spec, err = canary._component_spec("ollama")
    assert spec is None
    assert err is not None
    assert not err.ok
    assert err.message == canary.OLLAMA_UNSUPPORTED_REASON
    assert "model store" in err.message


@pytest.mark.unit
def test_component_spec_api_and_web_are_supported():
    api_spec, api_err = canary._component_spec("api")
    web_spec, web_err = canary._component_spec("web")
    assert api_err is None and api_spec is not None
    assert web_err is None and web_spec is not None
    assert api_spec.stable_deployment == "nyxgpt-api-stable"
    assert web_spec.stable_deployment == "nyxgpt-web-stable"
    assert web_spec.canary_deployment == "nyxgpt-web-canary"
    assert web_spec.container_name == "nyxgpt-web"
    assert web_spec.image_repository == "nyxgpt-web"


@pytest.mark.unit
def test_status_unknown_component_reports_unavailable_with_reason():
    data = canary.status("nyxgpt", component="not-a-component")
    assert data["available"] is False
    assert data["mode_supported"] is False
    assert "Unknown canary component" in data["unavailable_reason"]
    assert "Unknown canary component" in data["mode_message"]


@pytest.mark.unit
def test_status_ollama_reports_unavailable_with_documented_reason():
    data = canary.status("nyxgpt", component="ollama")
    assert data["available"] is False
    assert data["unavailable_reason"] == canary.OLLAMA_UNSUPPORTED_REASON
    assert data["mode_message"] == canary.OLLAMA_UNSUPPORTED_REASON


@pytest.mark.unit
@pytest.mark.parametrize(
    "action_call",
    [
        lambda: canary.deploy(namespace="nyxgpt", component="ollama"),
        lambda: canary.start(namespace="nyxgpt", component="ollama"),
        lambda: canary.evaluate("nyxgpt", component="ollama"),
        lambda: canary.promote(namespace="nyxgpt", component="ollama"),
        lambda: canary.rollback(namespace="nyxgpt", component="ollama"),
    ],
)
def test_every_lifecycle_action_refuses_ollama(action_call):
    result = action_call()
    assert not result.ok
    assert result.message == canary.OLLAMA_UNSUPPORTED_REASON


@pytest.mark.unit
def test_deploy_web_component_builds_with_web_kwargs_and_sets_web_container(monkeypatch):
    monkeypatch.setattr(canary.ops_module, "project_version", lambda: "1.2.3")
    monkeypatch.setattr(canary, "_git_short_sha", lambda: "abcd123")
    build_mock = MagicMock(return_value=[canary.CanaryResult(True, "built")])
    monkeypatch.setattr(canary.ops_module, "build_and_load_k8s_image", build_mock)
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", run_mock)

    result = canary.deploy(namespace="nyxgpt", component="web")

    assert result.ok
    assert "nyxgpt-web:1.2.3-abcd123" in result.message
    build_mock.assert_called_once_with(
        "nyxgpt-web:1.2.3-abcd123",
        context=canary.COMPONENTS["web"].build_context,
        fingerprint_paths=canary.COMPONENTS["web"].build_fingerprint_paths,
        excludes=canary.COMPONENTS["web"].build_excludes,
        build_args=canary.COMPONENTS["web"].build_args,
    )
    calls = [c.args[0] for c in run_mock.call_args_list]
    assert calls[0] == [
        "kubectl",
        "set",
        "image",
        "deployment/nyxgpt-web-canary",
        "nyxgpt-web=nyxgpt-web:1.2.3-abcd123",
        "-n",
        "nyxgpt",
    ]
    assert calls[1][:4] == ["kubectl", "rollout", "status", "deployment/nyxgpt-web-canary"]


@pytest.mark.unit
def test_start_web_component_scales_web_deployments_and_uses_own_state(monkeypatch):
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    monkeypatch.setattr(canary, "_desired_replicas", lambda name, ns=None: 1)

    result = canary.start(namespace="nyxgpt", weight_percent=50, total_replicas=4, component="web")

    assert result.ok
    calls = [c.args[0] for c in scale_mock.call_args_list if c.args[0][1] == "scale"]
    assert calls[0][:4] == ["kubectl", "scale", "deployment", "nyxgpt-web-canary"]
    assert calls[1][:4] == ["kubectl", "scale", "deployment", "nyxgpt-web-stable"]

    web_state = canary._load_state("web")
    assert web_state["active"] is True
    assert web_state["weight_percent"] == 50

    # api's own state must be untouched by a web-only rollout (the "components"
    # sub-object storing web's state rides along in the same file, see _save_state).
    api_state = canary._load_state("api")
    assert api_state["active"] is False
    assert api_state["weight_percent"] == 0
    assert api_state["history"] == []


@pytest.mark.unit
def test_web_and_api_rollout_state_do_not_collide(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=0))

    canary.start(namespace="nyxgpt", weight_percent=25, total_replicas=4, component="api")
    canary.start(namespace="nyxgpt", weight_percent=50, total_replicas=4, component="web")

    api_state = canary._load_state("api")
    web_state = canary._load_state("web")
    assert api_state["weight_percent"] == 25
    assert web_state["weight_percent"] == 50

    # Both survive a rollback of just one component.
    canary.rollback(namespace="nyxgpt", component="web")
    assert canary._load_state("api")["weight_percent"] == 25
    assert canary._load_state("web")["active"] is False


@pytest.mark.unit
def test_promote_web_component_finalizes_with_web_container_and_repository(monkeypatch):
    canary._save_state(
        {"active": True, "weight_percent": 90, "total_replicas": 4, "history": []}, "web"
    )
    monkeypatch.setattr(
        canary,
        "deployment_health",
        lambda name, ns: canary.TrackHealth("healthy", f"{name} healthy", "1.2.3-abcd123"),
    )
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", run_mock)

    result = canary.promote(namespace="nyxgpt", step_percent=25, component="web")

    assert result.ok
    assert "nyxgpt-web-stable" in result.message
    calls = [c.args[0] for c in run_mock.call_args_list]
    assert calls[0] == [
        "kubectl",
        "set",
        "image",
        "deployment/nyxgpt-web-stable",
        "nyxgpt-web=nyxgpt-web:1.2.3-abcd123",
        "-n",
        "nyxgpt",
    ]
    assert canary._load_state("web")["active"] is False


@pytest.mark.unit
def test_rollback_web_component_scales_web_deployments(monkeypatch):
    canary._save_state(
        {"active": True, "weight_percent": 50, "total_replicas": 4, "history": []}, "web"
    )
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.rollback(namespace="nyxgpt", component="web")

    assert result.ok
    calls = [c.args[0] for c in scale_mock.call_args_list]
    assert calls[0][:4] == ["kubectl", "scale", "deployment", "nyxgpt-web-canary"]
    assert "--replicas=0" in calls[0]
    assert calls[1][:4] == ["kubectl", "scale", "deployment", "nyxgpt-web-stable"]


@pytest.mark.unit
def test_web_component_records_ops_action_with_web_service_label(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(returncode=0))

    canary.start(namespace="nyxgpt", weight_percent=10, total_replicas=4, component="web")

    assert (
        _metric_value(
            "nyxgpt_ops_actions_total", command="canary-start", service="web", result="success"
        )
        >= 1
    )


@pytest.mark.unit
def test_web_component_status_uses_component_labeled_metrics_only(monkeypatch):
    """The legacy api-only nyxgpt_canary_* metrics must not be touched by a web status call."""
    _kubernetes_mode(monkeypatch)
    canary._save_state({"active": True, "weight_percent": 42, "history": []}, "web")
    monkeypatch.setattr(
        canary,
        "deployment_health",
        lambda name, ns: canary.TrackHealth("healthy", f"{name} healthy", "2.0.0"),
    )

    canary.status("nyxgpt", component="web")

    assert _metric_value("nyxgpt_canary_component_rollout_active", component="web") == 1
    assert _metric_value("nyxgpt_canary_component_weight_percent", component="web") == 42
    assert (
        _metric_value(
            "nyxgpt_canary_component_track_version_info",
            component="web",
            track="stable",
            version="2.0.0",
        )
        == 1
    )


# --- Track-scoped metrics attribution (#3829) ---------------------------------
#
# The defect these cover: `evaluate()` read the in-process ResourceMonitor of
# whichever nyxgpt-api Pod served the request, so a canary with zero scheduled
# Pods was green-lit on a stable Pod's 459 requests and the gate could not
# fail. Every test below drives the REAL `track_metrics` against a faked
# cluster, since a stub of it would assert nothing about attribution.


@pytest.mark.unit
def test_evaluate_holds_when_canary_has_no_endpoints_and_stable_is_busy(monkeypatch):
    """The #3829 acceptance scenario: canary 0/1, stable healthy and serving hundreds.

    The old code compared the *serving* Pod's 459 requests against
    min_requests, found no breach, and answered "safe to promote" for a build
    that had never taken a request.
    """
    canary._save_state({"active": True, "weight_percent": 10, "history": []})
    monkeypatch.setattr(canary, "track_metrics", REAL_TRACK_METRICS)
    monkeypatch.setattr(
        canary,
        "_run",
        _fake_cluster(
            canary_pods=(),
            stable_pods=("nyxgpt-api-stable-aaa", "nyxgpt-api-stable-bbb"),
            bodies={
                "nyxgpt-api-stable-aaa": _exposition(ok=459, latency_s=0.005),
                "nyxgpt-api-stable-bbb": _exposition(ok=459, latency_s=0.005),
            },
        ),
    )

    result = canary.evaluate("nyxgpt", min_requests=20)

    assert "safe to promote" not in result.message
    assert "no ready Pods" in result.message
    assert result.ok  # a hold, not a regression: nothing to roll back


@pytest.mark.unit
def test_evaluate_reads_the_canary_track_not_the_stable_one(monkeypatch):
    """A clean canary must pass even while stable is melting down beside it."""
    canary._save_state({"active": True, "weight_percent": 10, "history": []})
    monkeypatch.setattr(canary, "track_metrics", REAL_TRACK_METRICS)
    monkeypatch.setattr(
        canary,
        "_run",
        _fake_cluster(
            canary_pods=("nyxgpt-api-canary-zzz",),
            stable_pods=("nyxgpt-api-stable-aaa",),
            bodies={
                "nyxgpt-api-canary-zzz": _exposition(ok=100, latency_s=0.05),
                # Stable: 100% 5xx and multi-second latency. Must not influence
                # the verdict in either direction.
                "nyxgpt-api-stable-aaa": _exposition(errors=500, latency_s=7.5),
            },
        ),
    )

    result = canary.evaluate(
        "nyxgpt", error_rate_threshold_percent=5.0, latency_p95_threshold_ms=2000.0, min_requests=20
    )

    assert result.ok
    assert "safe to promote" in result.message
    assert "100 requests" in result.message


@pytest.mark.unit
def test_evaluate_rolls_back_on_a_canary_track_regression(monkeypatch):
    """Auto-rollback fires on the canary's own error rate, not the fleet's."""
    canary._save_state({"active": True, "weight_percent": 25, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "track_metrics", REAL_TRACK_METRICS)
    monkeypatch.setattr(
        canary,
        "_run",
        _fake_cluster(
            canary_pods=("nyxgpt-api-canary-zzz",),
            stable_pods=("nyxgpt-api-stable-aaa",),
            bodies={
                "nyxgpt-api-canary-zzz": _exposition(ok=60, errors=40, latency_s=0.05),
                "nyxgpt-api-stable-aaa": _exposition(ok=5000, latency_s=0.005),
            },
        ),
    )

    result = canary.evaluate(
        "nyxgpt", error_rate_threshold_percent=5.0, latency_p95_threshold_ms=2000.0, min_requests=20
    )

    assert not result.ok
    assert "error rate 40.00%" in result.message
    assert "automatically rolled back" in result.message
    assert canary._load_state()["active"] is False


@pytest.mark.unit
def test_track_metrics_excludes_probe_and_scrape_traffic(monkeypatch):
    """Kubelet /health probes and Prometheus /metrics scrapes are not canary traffic.

    Counting them would let an idle canary cross min_requests within minutes
    of being scheduled -- the same "the gate cannot fail" defect in a subtler
    form.
    """
    monkeypatch.setattr(
        canary,
        "_run",
        _fake_cluster(
            canary_pods=("nyxgpt-api-canary-zzz",),
            bodies={"nyxgpt-api-canary-zzz": _exposition(ok=0, probes=250, latency_s=0.05)},
        ),
    )

    metrics = REAL_TRACK_METRICS("canary", "nyxgpt")

    assert metrics.attributable is True
    assert metrics.total_requests == 0
    assert metrics.p95_latency_ms == 0.0


@pytest.mark.unit
def test_track_metrics_sums_pods_and_estimates_p95(monkeypatch):
    monkeypatch.setattr(
        canary,
        "_run",
        _fake_cluster(
            canary_pods=("canary-a", "canary-b"),
            bodies={
                "canary-a": _exposition(ok=30, errors=10, latency_s=0.05),
                "canary-b": _exposition(ok=60, latency_s=0.05),
            },
        ),
    )

    metrics = REAL_TRACK_METRICS("canary", "nyxgpt")

    assert metrics.attributable is True
    assert metrics.source == "pods"
    assert metrics.pods_ready == 2
    assert metrics.pods_scraped == 2
    assert metrics.total_requests == 100
    assert metrics.error_rate_percent == 10.0
    # Every observation landed in the (0.025, 0.05] bucket, so the 95th
    # percentile interpolates to 0.025 + 0.025*0.95 = 48.75ms.
    assert round(metrics.p95_latency_ms, 2) == 48.75


@pytest.mark.unit
def test_track_metrics_reads_each_pod_through_the_pod_proxy(monkeypatch):
    calls = []

    def _record(cmd, **kwargs):
        calls.append(cmd)
        return _fake_cluster(
            canary_pods=("canary-a",),
            bodies={"canary-a": _exposition(ok=1, latency_s=0.05)},
        )(cmd, **kwargs)

    monkeypatch.setattr(canary, "_run", _record)

    REAL_TRACK_METRICS("canary", "nyxgpt")

    assert calls[0][:3] == ["kubectl", "get", "pods"]
    assert "app=nyxgpt-api-canary-pool,track=canary" in calls[0]
    assert calls[1] == [
        "kubectl",
        "get",
        "--raw",
        "/api/v1/namespaces/nyxgpt/pods/canary-a:8000/proxy/metrics",
    ]


@pytest.mark.unit
def test_track_metrics_unattributable_when_pods_are_not_ready(monkeypatch):
    """A scheduled-but-unready Pod serves nothing, so its counters are not evidence."""

    def _fn(cmd, **_kwargs):
        if cmd[:3] == ["kubectl", "get", "pods"]:
            return CP(stdout=_pod_list("canary-a", ready=False))
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", _fn)

    metrics = REAL_TRACK_METRICS("canary", "nyxgpt")

    assert metrics.attributable is False
    assert "no ready Pods" in metrics.reason


@pytest.mark.unit
def test_track_metrics_unattributable_when_no_pod_metrics_can_be_read(monkeypatch):
    monkeypatch.setattr(canary, "_run", _fake_cluster(canary_pods=("canary-a",), bodies={}))

    metrics = REAL_TRACK_METRICS("canary", "nyxgpt")

    assert metrics.attributable is False
    assert metrics.pods_ready == 1
    assert "could not read /metrics" in metrics.reason


@pytest.mark.unit
def test_track_metrics_caps_the_pods_it_scrapes(monkeypatch):
    pods = tuple(f"canary-{i}" for i in range(canary.MAX_SCRAPED_PODS + 3))
    monkeypatch.setattr(
        canary,
        "_run",
        _fake_cluster(
            canary_pods=pods,
            bodies={pod: _exposition(ok=1, latency_s=0.05) for pod in pods},
        ),
    )

    metrics = REAL_TRACK_METRICS("canary", "nyxgpt")

    assert metrics.pods_ready == len(pods)
    assert metrics.pods_scraped == canary.MAX_SCRAPED_PODS
    assert metrics.total_requests == canary.MAX_SCRAPED_PODS


@pytest.mark.unit
def test_track_metrics_says_web_traffic_is_not_measurable(monkeypatch):
    """Next.js Pods export no /metrics, so the honest answer is "unmeasurable", not a number."""
    run_mock = MagicMock()
    monkeypatch.setattr(canary, "_run", run_mock)

    metrics = REAL_TRACK_METRICS("canary", "nyxgpt", component="web")

    assert metrics.attributable is False
    assert "no /metrics endpoint" in metrics.reason
    run_mock.assert_not_called()


@pytest.mark.unit
def test_evaluate_holds_when_the_canary_track_is_unattributable(monkeypatch, caplog):
    canary._save_state({"active": True, "weight_percent": 10, "history": []}, "web")

    with caplog.at_level("WARNING"):
        result = canary.evaluate("nyxgpt", component="web")

    assert result.ok
    assert "safe to promote" not in result.message
    assert "Cannot evaluate the canary track" in result.message
    assert "unattributable" in caplog.text
    assert (
        _metric_value(
            "nyxgpt_canary_component_evaluations_total",
            component="web",
            result="unattributable",
        )
        >= 1
    )


@pytest.mark.unit
def test_status_reports_canary_track_metrics_and_skips_stable_when_idle(monkeypatch):
    canary._save_state({"active": False, "weight_percent": 0, "history": []})
    # The per-track reads are Kubernetes-mode behavior: outside it `status()`
    # makes no cluster call at all (#3858), so the mode has to be declared for
    # this test to be about metrics attribution rather than about that guard.
    monkeypatch.setattr(canary, "current_mode", lambda: "kubernetes")
    monkeypatch.setattr(canary, "track_metrics", REAL_TRACK_METRICS)
    monkeypatch.setattr(
        canary,
        "deployment_health",
        lambda name, ns: canary.TrackHealth("healthy", f"{name} healthy", "1.0.0"),
    )
    monkeypatch.setattr(
        canary,
        "_run",
        _fake_cluster(
            canary_pods=("canary-a",),
            stable_pods=("stable-a",),
            bodies={
                "canary-a": _exposition(ok=7, latency_s=0.05),
                "stable-a": _exposition(ok=999, latency_s=0.05),
            },
        ),
    )

    data = canary.status("nyxgpt")

    assert data["metrics"]["track"] == "canary"
    assert data["metrics"]["attributable"] is True
    assert data["metrics"]["total_requests"] == 7
    # Stable costs one Pod-proxy call per Pod on every poll; not worth it with
    # no canary to compare against.
    assert data["stable_metrics"]["attributable"] is False
    assert "no rollout is in progress" in data["stable_metrics"]["reason"]


@pytest.mark.unit
def test_status_measures_both_tracks_during_a_rollout(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 25, "history": []})
    monkeypatch.setattr(canary, "current_mode", lambda: "kubernetes")
    monkeypatch.setattr(canary, "track_metrics", REAL_TRACK_METRICS)
    monkeypatch.setattr(
        canary,
        "deployment_health",
        lambda name, ns: canary.TrackHealth("healthy", f"{name} healthy", "1.0.0"),
    )
    monkeypatch.setattr(
        canary,
        "_run",
        _fake_cluster(
            canary_pods=("canary-a",),
            stable_pods=("stable-a",),
            bodies={
                "canary-a": _exposition(ok=7, latency_s=0.05),
                "stable-a": _exposition(ok=999, latency_s=0.05),
            },
        ),
    )

    data = canary.status("nyxgpt")

    assert data["metrics"]["total_requests"] == 7
    assert data["stable_metrics"]["attributable"] is True
    assert data["stable_metrics"]["total_requests"] == 999


@pytest.mark.unit
def test_promote_refuses_a_canary_track_that_served_no_traffic(monkeypatch):
    """A build no request ever reached has not been canaried, however healthy its Pods look."""
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "deployment_health", _healthy())
    monkeypatch.setattr(canary, "track_metrics", _measured(0))
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert not result.ok
    assert "served no traffic" in result.message
    scale_mock.assert_not_called()
    assert canary._load_state()["weight_percent"] == 10


@pytest.mark.unit
def test_promote_refuses_the_final_step_too_when_no_traffic_was_served(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 90, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "deployment_health", _healthy())
    monkeypatch.setattr(canary, "track_metrics", _measured(0))
    scale_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(canary, "_run", scale_mock)

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert not result.ok
    assert "served no traffic" in result.message
    scale_mock.assert_not_called()
    assert canary._load_state()["active"] is True


@pytest.mark.unit
def test_promote_force_overrides_the_no_traffic_gate(monkeypatch):
    """An idle cluster is indistinguishable from an unreachable canary, so force stays available."""
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "deployment_health", _healthy())
    monkeypatch.setattr(canary, "track_metrics", _measured(0))
    monkeypatch.setattr(canary, "_run", MagicMock(return_value=CP(returncode=0)))

    result = canary.promote(namespace="nyxgpt", step_percent=25, force=True)

    assert result.ok
    assert "forced" in result.message
    # The step still lands: 35% is not expressible in this pool, so #3833's
    # planner rounds the achieved weight to 40% (2/5). What matters here is
    # that the traffic gate did not stop it.
    assert canary._load_state()["weight_percent"] == 40


@pytest.mark.unit
def test_promote_proceeds_when_the_canary_track_has_served_traffic(monkeypatch):
    canary._save_state({"active": True, "weight_percent": 10, "total_replicas": 4, "history": []})
    monkeypatch.setattr(canary, "deployment_health", _healthy())
    monkeypatch.setattr(canary, "track_metrics", _measured(120))
    monkeypatch.setattr(canary, "_run", MagicMock(return_value=CP(returncode=0)))

    result = canary.promote(namespace="nyxgpt", step_percent=25)

    assert result.ok
    assert "forced" not in result.message
    # 40%, not the requested 35%: #3833's planner reports the weight the pool
    # can actually express. The point of this test is that a canary with
    # measured traffic is promoted at all.
    assert canary._load_state()["weight_percent"] == 40


@pytest.mark.unit
def test_promote_proceeds_with_a_note_when_traffic_is_not_measurable(monkeypatch):
    """`web` has no /metrics to read, so the gate cannot fire -- and says so."""
    canary._save_state(
        {"active": True, "weight_percent": 10, "total_replicas": 4, "history": []}, "web"
    )
    monkeypatch.setattr(canary, "deployment_health", _healthy())
    monkeypatch.setattr(canary, "_run", MagicMock(return_value=CP(returncode=0)))

    result = canary.promote(namespace="nyxgpt", step_percent=25, component="web")

    assert result.ok
    assert "canary traffic not verified" in result.message


@pytest.mark.unit
def test_track_metrics_ignores_pods_that_are_terminating(monkeypatch):
    """After `canary deploy`, the draining Pods run the PREVIOUS image.

    Counting them would credit the old version's traffic to the version being
    judged -- and it is what made a one-replica stable track report three
    Pods' requests during the #3829 cluster smoke.
    """

    def _fn(cmd, **_kwargs):
        if cmd[:3] == ["kubectl", "get", "pods"]:
            items = json.loads(_pod_list("canary-new", "canary-old"))
            items["items"][1]["metadata"]["deletionTimestamp"] = "2026-08-18T14:00:00Z"
            return CP(stdout=json.dumps(items))
        if cmd[:3] == ["kubectl", "get", "--raw"]:
            pod = cmd[3].split("/pods/")[1].split(":")[0]
            return CP(stdout=_exposition(ok=5 if pod == "canary-new" else 500, latency_s=0.05))
        return CP(returncode=0)

    monkeypatch.setattr(canary, "_run", _fn)

    metrics = REAL_TRACK_METRICS("canary", "nyxgpt")

    assert metrics.pods_ready == 1
    assert metrics.total_requests == 5


# --- #3831: the Kubernetes reason must reach the operator, not "0/1 ready" ---


def _pending_pod_json(
    *,
    name="nyxgpt-api-canary-7f9c8b6d4-2xk9p",
    reason="Unschedulable",
    message="0/1 nodes are available: 1 Insufficient memory. preemption: 0/1 nodes are available.",
):
    """A real-shaped `kubectl get pods -o json` for a Pod the scheduler refused."""
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": name},
                    "status": {
                        "phase": "Pending",
                        "conditions": [
                            {
                                "type": "PodScheduled",
                                "status": "False",
                                "reason": reason,
                                "message": message,
                            }
                        ],
                    },
                }
            ]
        }
    )


def _route(deployment_json, pods_json):
    """Stub `_run` that answers `get deployment` and `get pods` differently."""

    def _fn(cmd, **_k):
        if "pods" in cmd:
            return CP(stdout=pods_json)
        return CP(stdout=deployment_json)

    return _fn


def _deployment_with_selector(**kwargs):
    data = json.loads(_deployment_json(**kwargs))
    data["spec"]["selector"] = {"matchLabels": {"app": "nyxgpt-api-canary-pool", "track": "canary"}}
    return json.dumps(data)


@pytest.mark.unit
def test_unhealthy_deployment_surfaces_the_scheduling_reason(monkeypatch):
    """#3831: "not healthy (0/1 ready)" alone made a full cluster look like a broken canary."""
    monkeypatch.setattr(
        canary, "_run", _route(_deployment_with_selector(ready=0), _pending_pod_json())
    )

    result = canary.deployment_health("nyxgpt-api-canary", "nyxgpt")

    assert result.state == "unhealthy"
    assert "not healthy (0/1 ready)" in result.message
    assert "Unschedulable" in result.message
    assert "Insufficient memory" in result.message
    assert "nyxgpt-api-canary-7f9c8b6d4-2xk9p" in result.message
    # The version field stays a version -- the reason belongs in the message.
    assert result.version == "local"


@pytest.mark.unit
def test_pod_selector_is_read_from_the_deployment_not_guessed(monkeypatch):
    seen: list[list[str]] = []

    def _fn(cmd, **_k):
        seen.append(cmd)
        if "pods" in cmd:
            return CP(stdout=_pending_pod_json())
        return CP(stdout=_deployment_with_selector(ready=0))

    monkeypatch.setattr(canary, "_run", _fn)
    canary.deployment_health("nyxgpt-api-canary", "nyxgpt")

    pod_cmd = next(cmd for cmd in seen if "pods" in cmd)
    assert pod_cmd == [
        "kubectl",
        "get",
        "pods",
        "-n",
        "nyxgpt",
        "-l",
        "app=nyxgpt-api-canary-pool,track=canary",
        "-o",
        "json",
    ]


@pytest.mark.unit
def test_healthy_deployment_does_not_query_pods(monkeypatch):
    """A healthy track costs no extra kubectl call (agentic first principle 1: cost)."""
    seen: list[list[str]] = []

    def _fn(cmd, **_k):
        seen.append(cmd)
        return CP(stdout=_deployment_with_selector(ready=1))

    monkeypatch.setattr(canary, "_run", _fn)
    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")

    assert result.state == "healthy"
    assert not any("pods" in cmd for cmd in seen)


@pytest.mark.unit
@pytest.mark.parametrize(
    "pod_status, expected",
    [
        (
            {
                "phase": "Pending",
                "containerStatuses": [
                    {
                        "ready": False,
                        "state": {
                            "waiting": {
                                "reason": "ImagePullBackOff",
                                "message": 'Back-off pulling image "nyxgpt-api:3.0.0-deadbee"',
                            }
                        },
                    }
                ],
            },
            "ImagePullBackOff: Back-off pulling image",
        ),
        (
            {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "ready": False,
                        "state": {
                            "terminated": {"reason": "Error", "message": "boom", "exitCode": 137}
                        },
                    }
                ],
            },
            "Error: boom (exit 137)",
        ),
        (
            {
                "phase": "Running",
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "False",
                        "reason": "ContainersNotReady",
                        "message": "containers with unready status: [nyxgpt-api]",
                    }
                ],
            },
            "ContainersNotReady: containers with unready status",
        ),
        ({"phase": "Pending"}, "Pending"),
        ({"phase": "Running"}, ""),
    ],
)
def test_pod_reason_reads_every_kubernetes_failure_shape(pod_status, expected):
    reason = canary._pod_reason({"metadata": {"name": "p"}, "status": pod_status})
    assert expected in reason
    if not expected:
        assert reason == ""


@pytest.mark.unit
def test_pod_reason_truncates_a_runaway_message():
    long_message = "x" * 900
    reason = canary._pod_reason(
        {
            "status": {
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": long_message,
                    }
                ]
            }
        }
    )
    assert len(reason) < canary.POD_REASON_MESSAGE_LIMIT + 40
    assert reason.endswith("…")


@pytest.mark.unit
def test_pod_failure_reasons_caps_the_list(monkeypatch):
    pods = {
        "items": [
            {
                "metadata": {"name": f"pod-{i}"},
                "status": {
                    "conditions": [
                        {
                            "type": "PodScheduled",
                            "status": "False",
                            "reason": "Unschedulable",
                            "message": "Insufficient memory",
                        }
                    ]
                },
            }
            for i in range(10)
        ]
    }
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(stdout=json.dumps(pods)))

    reasons = canary.pod_failure_reasons("app=x", "nyxgpt")

    assert len(reasons) == canary.POD_REASON_LIMIT


@pytest.mark.unit
@pytest.mark.parametrize(
    "stub",
    [
        lambda cmd, **_k: CP(returncode=1, stderr="Error from server (Forbidden)"),
        lambda cmd, **_k: CP(stdout="not json"),
        lambda cmd, **_k: CP(stdout=json.dumps({"items": [{"status": {"phase": "Running"}}]})),
    ],
)
def test_pod_failure_reasons_degrades_quietly(monkeypatch, stub):
    """An RBAC denial or junk response must never break the health read it enriches."""
    monkeypatch.setattr(canary, "_run", stub)
    assert canary.pod_failure_reasons("app=x", "nyxgpt") == []


@pytest.mark.unit
def test_pod_failure_reasons_without_selector_or_kubectl(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: pytest.fail("must not shell out"))
    assert canary.pod_failure_reasons("", "nyxgpt") == []
    monkeypatch.setattr(canary, "_which", lambda _: None)
    assert canary.pod_failure_reasons("app=x", "nyxgpt") == []


@pytest.mark.unit
def test_wait_rollout_failure_names_the_pod_reason(monkeypatch):
    """A rollout timeout used to say only "did not become healthy" (#3831)."""

    def _fn(cmd, **_k):
        if "rollout" in cmd:
            return CP(returncode=1, stderr="error: timed out waiting for the condition")
        if "pods" in cmd:
            return CP(stdout=_pending_pod_json())
        return CP(stdout=_deployment_with_selector(ready=0))

    monkeypatch.setattr(canary, "_run", _fn)
    result = canary._wait_rollout("nyxgpt-api-canary", "nyxgpt", timeout_seconds=5)

    assert not result.ok
    assert "did not become healthy within 5s" in result.message
    assert "Insufficient memory" in result.message
    assert "timed out waiting for the condition" in result.details


@pytest.mark.unit
def test_wait_rollout_failure_without_a_readable_selector(monkeypatch):
    """Unreadable Deployment -> no reason to add, and still an honest failure."""

    def _fn(cmd, **_k):
        if "rollout" in cmd:
            return CP(returncode=1, stderr="error: timed out")
        return CP(returncode=1, stderr="Error from server (Forbidden)")

    monkeypatch.setattr(canary, "_run", _fn)
    result = canary._wait_rollout("nyxgpt-api-canary", "nyxgpt", timeout_seconds=5)

    assert not result.ok
    assert result.message == "Rollout of nyxgpt-api-canary did not become healthy within 5s"


@pytest.mark.unit
def test_unreadable_deployment_puts_kubectl_stderr_in_the_message(monkeypatch):
    """The stderr used to be passed as the track's `version` and rendered as one."""
    monkeypatch.setattr(
        canary,
        "_run",
        lambda cmd, **_k: CP(returncode=1, stderr="Error from server (Forbidden): access denied"),
    )

    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")

    assert result.state == "error"
    assert "Could not read deployment" in result.message
    assert "Forbidden" in result.message
    assert result.version == ""


@pytest.mark.unit
def test_unparseable_deployment_puts_the_parse_error_in_the_message(monkeypatch):
    monkeypatch.setattr(canary, "_run", lambda cmd, **_k: CP(stdout="not json"))

    result = canary.deployment_health("nyxgpt-api-stable", "nyxgpt")

    assert result.state == "error"
    assert "Could not parse status" in result.message
    assert result.version == ""


@pytest.mark.unit
def test_selector_from_deployment_tolerates_a_missing_selector():
    assert canary._selector_from_deployment({"spec": {}}) == ""
    assert canary._selector_from_deployment({"spec": {"selector": {"matchLabels": "nope"}}}) == ""
