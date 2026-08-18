#!/usr/bin/env python3
"""Executed proof that self-heal watches the WHOLE Kubernetes deployment (#3828, #3775).

Run against a live cluster that already has ``nyxgpt ops install --kubernetes
--local`` applied -- ``scripts/k8s-local-smoke.sh`` calls it as one of its
steps, which is where the cluster comes from.

The defect it gates, as owner acceptance saw it on the Self-Heal page:

* **"Detected mode: Nothing detected running"** printed directly above a list
  of four running ``kubernetes`` components. Every Kubernetes row is named
  after a Pod, and no Pod name is one of ``api``/``web``/``ollama``/
  ``cassandra``, so the mode detector matched nothing.
* **api tier only.** The Pod survey selected
  ``app=nyxgpt-api-canary-pool``, so web, Cassandra, Ollama and the entire
  in-cluster observability tier (#3787) were watched, healed and reported by
  nothing at all.
* **A Compose "cannot determine from here" banner** over an observability
  tier that runs in-cluster and answers ``kubectl`` perfectly well.

None of that is reachable by unit test: its substance is what a real
``kubectl get pods`` returns from a real cluster, and what deleting a real Pod
does. So this script asserts, by running it:

1. ``self_heal.status()`` names the deployment ``kubernetes`` and reports the
   observability tier as read in-cluster, with no undetermined rows.
2. Every core tier the install deploys -- api, web, cassandra, ollama -- has at
   least one watched Pod, and the observability workloads are watched too.
3. **Fault injection** (the rule from ``macos-brew-smoke.yml`` / #3753): the
   pre-fix survey is reconstructed from the same live cluster -- the api-only
   label selector, and rows without the tier that names them -- and the checks
   in 1 and 2 are asserted to FAIL against it. Without this half the job would
   pass on a build that regressed to watching the api pool alone.
4. A real heal of a **non-api** Pod: ``heal_now`` on a web Pod deletes it and
   its controller replaces it. The api pool was the only tier that could
   demonstrate this before.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

NAMESPACE = "nyxgpt"
# The label selector self-heal used before #3828 -- the pre-fix survey, kept
# here (not imported) precisely because the fix deleted it from the module.
PRE_FIX_SELECTOR_APP = "nyxgpt-api-canary-pool"
# The observability overlay ships ten workloads (k8s/observability/); a single
# Pod each. Asserted as a floor, not an equality, so an added workload does not
# fail this check.
MIN_OBSERVABILITY_PODS = 10
POD_REPLACEMENT_TIMEOUT_S = 180.0


def log(msg: str) -> None:
    print(f"[k8s-self-heal-coverage-smoke] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[k8s-self-heal-coverage-smoke] FAIL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def kubectl(*args: str) -> str:
    cp = subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args], check=False, text=True, capture_output=True
    )
    if cp.returncode != 0:
        die(f"kubectl {' '.join(args)} exited {cp.returncode}: {cp.stderr.strip()}")
    return cp.stdout


def pod_app_labels() -> dict[str, str]:
    """{pod name: its `app` label} straight from the cluster."""
    data = json.loads(kubectl("get", "pods", "-o", "json"))
    return {
        pod["metadata"]["name"]: (pod["metadata"].get("labels") or {}).get("app", "")
        for pod in data.get("items", [])
    }


def main() -> None:
    from nyxgpt import self_heal

    log("querying the live cluster through self_heal.status()")
    payload = self_heal.status()
    components = payload["components"]
    kubernetes_rows = [c for c in components if c["source"] == "kubernetes"]
    core_rows = [c for c in kubernetes_rows if c["tier"] == "core"]
    observability_rows = [c for c in kubernetes_rows if c["tier"] == "observability"]
    log(
        f"mode={payload['mode']} observability_source={payload['observability_source']} "
        f"core_pods={len(core_rows)} observability_pods={len(observability_rows)}"
    )
    for row in sorted(kubernetes_rows, key=lambda c: (c["tier"], c["service"])):
        log(f"  {row['tier']:14s} {row['service']:40s} {row['state']}/{row['health']}")

    # --- 1. the page's own two sentences ------------------------------------
    if payload["mode"] != "kubernetes":
        die(
            f"detected mode is {payload['mode']!r} on a cluster running "
            f"{len(core_rows)} core Pod(s) -- this is the '#3828 Nothing detected running' bug"
        )
    if payload["observability_source"] != "kubernetes":
        die(
            "observability_source is "
            f"{payload['observability_source']!r}: the page would show the Compose "
            "'cannot determine from here' banner over an in-cluster tier"
        )
    undetermined = [c["service"] for c in components if not c["known"]]
    if undetermined:
        die(f"rows reported undeterminable in Kubernetes mode: {undetermined}")

    # --- 2. every tier the install deploys is watched ------------------------
    labels = pod_app_labels()
    watched_apps = {labels.get(c["service"], "") for c in core_rows}
    expected = set(self_heal.K8S_CORE_POD_APPS)
    missing = expected - watched_apps
    if missing:
        die(f"core tiers watched by nothing: {sorted(missing)} (watched: {sorted(watched_apps)})")
    if len(observability_rows) < MIN_OBSERVABILITY_PODS:
        die(
            f"only {len(observability_rows)} observability Pod(s) watched, expected at least "
            f"{MIN_OBSERVABILITY_PODS} -- the in-cluster tier (#3787) is not covered"
        )
    log(f"all four core tiers watched: {sorted(watched_apps)}")

    # --- 3. fault injection: the pre-fix survey must fail those checks -------
    # Same live cluster, reconstructed as self-heal saw it before #3828: only
    # Pods carrying the api pool's label, and no tier on the rows.
    pre_fix_rows = [
        dict(c, tier="")
        for c in kubernetes_rows
        if labels.get(c["service"]) == PRE_FIX_SELECTOR_APP
    ]
    pre_fix_statuses = [
        self_heal.ComponentStatus(
            service=c["service"],
            container=c["container"],
            state=c["state"],
            health=c["health"],
            healthy=c["healthy"],
            source="kubernetes",
            tier="",
        )
        for c in pre_fix_rows
    ]
    if not pre_fix_statuses:
        die("no api-pool Pods on this cluster -- the pre-fix survey cannot be reconstructed")
    if self_heal.detected_mode(pre_fix_statuses) != "none":
        die(
            "the pre-fix survey no longer reports 'none' -- check 1 above would pass on a "
            "build that regressed, and is worthless as a gate"
        )
    pre_fix_apps = {labels.get(s.service, "") for s in pre_fix_statuses}
    if pre_fix_apps != {PRE_FIX_SELECTOR_APP}:
        die(f"the pre-fix survey covered more than the api pool: {sorted(pre_fix_apps)}")
    if self_heal.kubernetes_mode_active(pre_fix_statuses):
        die("the pre-fix survey would not have suppressed the Compose observability banner")
    log(
        f"fault injection: the pre-fix (api-only) survey covers {len(pre_fix_statuses)} Pod(s), "
        "reports mode 'none', and fails every check above -- as it must"
    )

    # --- 4. a real heal of a non-api Pod ------------------------------------
    web_pods = [
        c
        for c in core_rows
        if labels.get(c["service"]) == "nyxgpt-web-canary-pool" and c["healthy"]
    ]
    if not web_pods:
        die("no healthy web Pod to heal -- the web tier is not in the survey")
    target = web_pods[0]["service"]
    log(f"healing a web Pod for real: heal_now(service={target})")
    result = self_heal.heal_now(service=target)
    healed = result.get("healed", [])
    if not healed or not healed[0]["ok"]:
        die(f"heal_now on the web Pod did not succeed: {result}")

    deadline = time.time() + POD_REPLACEMENT_TIMEOUT_S
    while time.time() < deadline:
        current = pod_app_labels()
        replacements = [
            name
            for name, app in current.items()
            if app == "nyxgpt-web-canary-pool" and name != target
        ]
        if target not in current and replacements:
            log(f"{target} was deleted and the web Deployment replaced it ({len(replacements)} up)")
            break
        time.sleep(3)
    else:
        die(f"{target} was never replaced within {POD_REPLACEMENT_TIMEOUT_S:.0f}s of the heal")

    log("PASS: self-heal watches, names and heals every tier of the Kubernetes deployment")


if __name__ == "__main__":
    main()
