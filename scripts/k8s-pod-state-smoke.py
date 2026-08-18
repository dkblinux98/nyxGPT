#!/usr/bin/env python3
"""Executed proof that a Pending Pod is reported as pending, not as a failure (#3827).

Owner acceptance of `nyxgpt ops install --kubernetes --local` exited failure
with ten `[FAIL] pod <name>: Pending` lines. Every one of those Pods was
Running three minutes later -- they were the #3787 observability Pods, still
pulling images while `_k8s_stack_health` read their phase. The same run also
printed `[OK] observability grafana: 0/1 ready`, so one command gave two
contradictory verdicts on the same condition. And it mattered: the wall of
nine false failures buried the one Pod that was genuinely broken (prometheus,
`Insufficient memory`).

Unit tests can only assert the classifier against JSON a test author wrote.
The claim here is about what a real kubelet and a real scheduler put in
`.status`, so this script drives a real cluster and classifies what comes
back. Three Pods, each a condition the fix has to tell apart, all
deterministic (no image-pull races):

* **transient pending** -- a Pod whose volume references a ConfigMap that does
  not exist yet. It is scheduled and stays `Pending`/`ContainerCreating` while
  kubelet retries the mount. It must classify PENDING, and the script then
  CREATES the ConfigMap and asserts the very same Pod reaches READY -- which
  is what proves the state really was transient and `[FAIL]` really was a lie.
* **unschedulable** -- a Pod requesting more CPU than the node has. The
  scheduler leaves `PodScheduled=False/Unschedulable`; no amount of waiting
  fixes it. It must classify FAILED, with the scheduler's own reason.
* **bad image** -- an image that does not exist. kubelet retries forever in
  `ImagePullBackOff`. Must classify FAILED.

Both halves, per the fault-injection rule (CLAUDE.md, #3753): against the
SAME real Pod JSON the script also runs the pre-fix rule (`ok = phase ==
"Running"`) and asserts it calls the transient Pod a failure and the
unschedulable Pod exactly the same thing -- i.e. that this cluster really does
reproduce the reported defect, and that the check would fail on a build
without the fix rather than passing vacuously.

Finally it exercises the wait itself: `_wait_for_k8s_rollouts` against an
unschedulable Deployment must give up in a couple of poll slices naming that
Pod, not burn its whole budget and then blame the workload it happened to be
waiting on.

Prerequisites: Docker, and a `nyxgpt` importable (`pip install -e .`).
kubectl and kind are installed by ops itself when missing (#3724). The
cluster is created only if no context is already reachable; a `nyxgpt-local`
cluster this script created is left in place for the caller to reuse (the
workflow's cleanup step tears it down).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

from nyxgpt import ops

NAMESPACE = "nyxgpt-pod-state-smoke"
CONFIGMAP = "late-configmap"

# A Pod that is Pending because kubelet cannot mount a ConfigMap that does not
# exist yet: scheduled (so it is not the unschedulable case), stuck in
# ContainerCreating, and curable by creating the ConfigMap. That curability is
# the whole point -- it is the "still pulling images" case, reproduced without
# depending on how fast a registry answers.
TRANSIENT_POD = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": "transient-pending", "namespace": NAMESPACE},
    "spec": {
        "containers": [
            {
                "name": "app",
                "image": "busybox:1.36",
                "command": ["sh", "-c", "sleep 3600"],
                "volumeMounts": [{"name": "late", "mountPath": "/late"}],
            }
        ],
        "volumes": [{"name": "late", "configMap": {"name": CONFIGMAP}}],
    },
}

# More CPU than any runner node has, so the scheduler can never place it.
UNSCHEDULABLE_POD = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": "unschedulable", "namespace": NAMESPACE},
    "spec": {
        "containers": [
            {
                "name": "app",
                "image": "busybox:1.36",
                "command": ["sh", "-c", "sleep 3600"],
                "resources": {"requests": {"cpu": "1000"}},
            }
        ]
    },
}

BAD_IMAGE_POD = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": "bad-image", "namespace": NAMESPACE},
    "spec": {
        "containers": [{"name": "app", "image": "nyxgpt.invalid/nope:does-not-exist"}],
        # Fail fast rather than spending the smoke's time on kubelet backoff.
        "restartPolicy": "Never",
    },
}

# An unschedulable Deployment, for the wait half.
UNSCHEDULABLE_DEPLOYMENT = {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {"name": "unschedulable-deploy", "namespace": NAMESPACE},
    "spec": {
        "replicas": 1,
        "selector": {"matchLabels": {"app": "unschedulable-deploy"}},
        "template": {
            "metadata": {"labels": {"app": "unschedulable-deploy"}},
            "spec": {
                "containers": [
                    {
                        "name": "app",
                        "image": "busybox:1.36",
                        "command": ["sh", "-c", "sleep 3600"],
                        "resources": {"requests": {"cpu": "1000"}},
                    }
                ]
            },
        },
    },
}


def log(msg: str) -> None:
    print(f"[k8s-pod-state-smoke] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[k8s-pod-state-smoke] FAIL: {msg}", file=sys.stderr, flush=True)
    diagnostics()
    sys.exit(1)


def kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    cp = subprocess.run(
        ["kubectl", *args], input=stdin, capture_output=True, text=True, check=False
    )
    if check and cp.returncode != 0:
        die(f"kubectl {' '.join(args)} exited {cp.returncode}: {cp.stderr.strip()}")
    return cp.stdout


def diagnostics() -> None:
    print("--- diagnostics ---", file=sys.stderr, flush=True)
    for args in (
        ("-n", NAMESPACE, "get", "pods", "-o", "wide"),
        ("-n", NAMESPACE, "describe", "pods"),
    ):
        cp = subprocess.run(["kubectl", *args], capture_output=True, text=True, check=False)
        print(cp.stdout[-4000:], file=sys.stderr, flush=True)


def wait_for(what: str, predicate, timeout: float = 180.0) -> None:
    """Poll `predicate` until true, or die naming `what`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(3)
    die(f"timed out after {timeout:.0f}s waiting for {what}")


def states() -> dict[str, ops.K8sWorkloadState]:
    """The shipped classification of every Pod in the smoke namespace."""
    classified, read_failure = ops._k8s_pod_states(NAMESPACE)
    if read_failure is not None:
        die(f"could not read Pod states: {read_failure.message}: {read_failure.details}")
    return {s.name: s for s in classified}


def pod_json(name: str) -> dict:
    return json.loads(kubectl("-n", NAMESPACE, "get", "pod", name, "-o", "json"))


def pre_fix_verdict(pod: dict) -> bool:
    """The pre-#3827 verdict, verbatim: `OpsResult(phase == "Running", ...)`."""
    return str((pod.get("status") or {}).get("phase")) == "Running"


def setup_cluster() -> None:
    results = ops._ensure_kubectl_and_cluster()
    for r in results:
        log(f"  cluster: [{'OK' if r.ok else 'FAIL'}] {r.message}")
    if not all(r.ok for r in results):
        die("no Kubernetes cluster available to run this smoke against")
    kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true")
    kubectl("create", "namespace", NAMESPACE)
    for manifest in (TRANSIENT_POD, UNSCHEDULABLE_POD, BAD_IMAGE_POD, UNSCHEDULABLE_DEPLOYMENT):
        kubectl("apply", "-f", "-", stdin=json.dumps(manifest))


def check_pre_fix_rule_reproduces_the_defect() -> None:
    """Fault injection: without the fix, this cluster really does report FAIL."""
    transient = pod_json("transient-pending")
    unschedulable = pod_json("unschedulable")
    if pre_fix_verdict(transient):
        die(
            "the transient Pod is already Running -- this run cannot reproduce the "
            "defect and would pass vacuously"
        )
    if pre_fix_verdict(transient) != pre_fix_verdict(unschedulable):
        die("the pre-fix rule already distinguished these two Pods; injection is wrong")
    log(
        'pre-fix rule (`ok = phase == "Running"`) calls BOTH the transient and the '
        "unschedulable Pod a failure -- the defect reproduces here"
    )


def check_classification() -> None:
    transient = states()["transient-pending"]
    if transient.state != ops.K8S_STATE_PENDING:
        die(f"a Pod waiting on a mount classified {transient.state}: {transient.summary}")
    if not transient.ok:
        die("a merely-Pending Pod must not count against the install's exit status")
    log(f"transient  -> [{transient.label}] {transient.summary}")

    unschedulable = states()["unschedulable"]
    if unschedulable.state != ops.K8S_STATE_FAILED:
        die(f"an unschedulable Pod classified {unschedulable.state}: {unschedulable.summary}")
    if "unschedulable" not in unschedulable.summary:
        die(f"an unschedulable Pod must say so, got {unschedulable.summary!r}")
    if not unschedulable.details:
        die("an unschedulable Pod must carry the scheduler's reason (e.g. Insufficient cpu)")
    log(
        f"unschedulable -> [{unschedulable.label}] {unschedulable.summary}: {unschedulable.details}"
    )

    wait_for(
        "the bad-image Pod to reach ImagePullBackOff",
        lambda: states()["bad-image"].state == ops.K8S_STATE_FAILED,
    )
    bad = states()["bad-image"]
    log(f"bad image  -> [{bad.label}] {bad.summary}")

    # The distinction the issue asks for, stated as one assertion: two Pods,
    # both `Pending`, classified differently and for a named reason.
    if transient.summary == unschedulable.summary:
        die("the two Pending Pods are reported identically -- the distinction is not there")


def check_transient_pod_really_was_transient() -> None:
    """The pending verdict has to be TRUE, not merely kinder than the old one."""
    kubectl("-n", NAMESPACE, "create", "configmap", CONFIGMAP, "--from-literal=now=exists")
    wait_for(
        "the previously-Pending Pod to become Ready once its ConfigMap exists",
        lambda: states()["transient-pending"].state == ops.K8S_STATE_READY,
    )
    log("transient  -> [OK] Running, after the ConfigMap it was waiting for appeared")


def check_the_wait_fails_fast_on_a_blocked_pod() -> None:
    """A Pod that will never start must end the wait in slices, not budgets."""
    started = time.monotonic()
    results = ops._wait_for_k8s_rollouts(
        [("deploy/unschedulable-deploy", "unschedulable workload", time.monotonic() + 900)],
        remedy="(smoke)",
    )
    elapsed = time.monotonic() - started
    if not results or results[-1].ok:
        die("the wait reported success for a workload whose Pod can never be scheduled")
    if "cannot start" not in results[-1].message:
        die(f"the wait must name the blocking Pod, got {results[-1].message!r}")
    budget_fraction = elapsed / 900
    if budget_fraction > 0.25:
        die(f"the wait took {elapsed:.0f}s of a 900s budget to notice a permanent failure")
    log(f"wait       -> [FAIL] {results[-1].message} (after {elapsed:.0f}s, not 900s)")


def main() -> int:
    if ops._which("docker") is None:
        log("SKIP: no docker on this host, so there is no cluster to classify against")
        return 0
    # Point the shipped code paths at this smoke's OWN namespace, process-local
    # and for this process only: the waits and the blocked-Pod scan read
    # `ops.K8S_NAMESPACE`, and nothing here may act on a real deployment that
    # happens to share the cluster.
    log(f"namespace: {NAMESPACE} (the real {ops.K8S_NAMESPACE} one is never touched)")
    ops.K8S_NAMESPACE = NAMESPACE
    setup_cluster()
    wait_for(
        "the three probe Pods to appear",
        lambda: {"transient-pending", "unschedulable", "bad-image"} <= set(states()),
    )
    wait_for(
        "the scheduler to rule on the unschedulable Pod",
        lambda: states()["unschedulable"].state == ops.K8S_STATE_FAILED,
    )
    check_pre_fix_rule_reproduces_the_defect()
    check_classification()
    check_transient_pod_really_was_transient()
    check_the_wait_fails_fast_on_a_blocked_pod()
    kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=false", check=False)
    log("PASS: Pending is pending, unschedulable is a named failure, and the wait fails fast")
    return 0


if __name__ == "__main__":
    sys.exit(main())
