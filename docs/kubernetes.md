# Kubernetes Deployment

nyxGPT can be deployed to a Kubernetes cluster as an alternative to the
Homebrew / `nyxgpt ops` workflow described in [ops.md](ops.md). There are two
targets, and they run the **same `k8s/*.yaml` manifests** on the same
single-node shape:

| Target | Command | Cluster |
|---|---|---|
| Your own workstation | `nyxgpt ops install --kubernetes --local` | kind, minikube, k3s, Docker Desktop's built-in cluster, or any reachable context |
| An AWS EC2 instance | `nyxgpt cloud deploy --kubernetes` | a single-node **k3s** cluster the deploy installs on the instance |

Most of this document is about the workstation target, in line with the
project's local-first [VISION.md](../product_management/VISION.md). The cloud
target is [Kubernetes on the cloud target](#kubernetes-on-the-cloud-target)
below and in [cloud.md](cloud.md); it is deliberately *not* a separate
deployment path — it puts a cluster on the instance and then runs the
workstation command there.

Scope: this deploys a **self-contained, chattable stack** — the FastAPI
backend (`nyxgpt-api`), the web UI (`nyxgpt-web`), and the data/LLM tier
they need: an in-cluster **Cassandra** holding the chat sessions every api
replica shares, and an in-cluster **Ollama** that answers them, pre-loaded
with the configured chat model and the configured embedding model (#3786,
#3824). Nothing on the host is required
once the cluster is up, and nothing has to be pointed at your own database
by hand. See [Data and LLM tier](#data-and-llm-tier) below. The
observability tier — Prometheus, Grafana, Loki + promtail, the OTel
collector, Jaeger and GlitchTip — is deployed as in-cluster workloads too
(#3787), so the SRE surface works in this mode as well; see [Observability
in the cluster](#observability-in-the-cluster).

Both `nyxgpt-api` and `nyxgpt-web` are deployed as **stable/canary pairs**
(`nyxgpt-api-stable`/`nyxgpt-api-canary`, `nyxgpt-web-stable`/
`nyxgpt-web-canary`), each fronted by a single Service, supporting a deploy
-> gate -> promote (or rollback) release cycle with metrics-gated gradual
traffic shift; see [Canary Deployment](#canary-deployment). (Blue/green --
a separate two-color pair with instant all-or-nothing cutover -- was retired
in favor of canary: 0%/100% traffic weight reproduces the same cutover, plus
canary adds the gradual shift and auto-rollback blue/green never had.)

## One-command bring-up (`nyxgpt ops`)

```bash
nyxgpt ops install --kubernetes
```

Per the project's [Operational Command Wrapping](../CLAUDE.md) rule, this is
the supported way to bring this deployment up — no raw `docker build`/
`kubectl`/`kind` commands required, and **no pre-existing cluster is
required either** (#3596), and **no pre-installed `kubectl`/`kind` either**
(#3724). It wraps the whole documented flow below into one step: makes sure
`kubectl` is available (installing it if it isn't — see [CLI tools nyxgpt
installs for you](#cli-tools-nyxgpt-installs-for-you)), then checks whether
kubectl's current context already reaches a cluster --

- **A cluster is already reachable** (minikube, Docker Desktop, an existing
  kind cluster, a remote context, ...): that cluster is used as-is, exactly
  like before -- bring-your-own remains fully supported.
- **No cluster is reachable**: installs [kind](https://kind.sigs.k8s.io/) if
  it's missing, then provisions a local kind cluster named `nyxgpt-local`
  (owner decision 2026-08-03) -- reusing it on later runs instead of
  recreating it. Docker is the one real prerequisite this can't install for
  you (it needs a privileged system install / Docker Desktop); if it's
  missing, the command fails with an actionable error pointing at where to
  install it, rather than a raw command to run yourself.

### CLI tools nyxgpt installs for you

`kubectl` and `kind` are provisioned automatically when they're missing
(#3724) — you never have to install them yourself first:

1. Via **Homebrew**, when `brew` is available, so the tool stays upgradable
   through your own package manager.
2. Otherwise (or if the formula fails) by downloading the **official release
   binary** — kind's latest GitHub release asset, kubectl's current stable
   build — into `~/.nyxGPT/bin`, which nyxgpt puts on `PATH` for the run that
   installs it and for every later `nyxgpt ops` invocation. No `sudo` is
   needed at any point.

Add `~/.nyxGPT/bin` to your own shell `PATH` if you want to run `kubectl`
directly outside of `nyxgpt ops`. Only when neither path can supply the tool
(an unsupported platform, or no network) does the command fail, and then with
a link to the installer.

It then builds `nyxgpt-api:local` and `nyxgpt-web:local` **from the published
artifacts** (see [Install modes](#install-modes-artifact-and---dev)) and loads
each into the cluster's image cache (kind/minikube get an explicit load step;
Docker Desktop's built-in cluster shares the host cache already), bootstraps
`~/.nyxGPT/k8s/secret.yaml` from the example (prompting for the API key
interactively, or pass `--api-key` — the value is never committed), applies the
kustomization (which includes both the api and web stable/canary pairs, see
[Canary Deployment](#canary-deployment), plus the [data and LLM
tier](#data-and-llm-tier)), waits for Cassandra and Ollama to report Ready --
which for Ollama includes the first pull of both the chat and the embedding
model, so the command returns only when a chat can actually be answered, with
RAG on or off -- waits for the api and web Deployments, brings up the
[observability tier](#observability-in-the-cluster) and waits for its ten
workloads to roll out too, and only then snapshots Pod/Service health for all
of them.

All three waits exist for the same reason: `kubectl apply` returns when the
objects are accepted, not when they work, so without them the command reports
on Pods that are still pulling images and its exit status describes a
mid-rollout snapshot rather than the stack the operator is handed (#3826,
#3827).

### Ready, pending, failed

Every Kubernetes readout `nyxgpt ops` prints — the install's health snapshot,
the observability workload list, `nyxgpt ops status` — classifies a workload
into one of three states, and the same way in each (#3827):

| Label | Meaning | Counts as a failure? |
| --- | --- | --- |
| `[OK]` | Running and passing its readiness probe (or `Succeeded`) | no |
| `[PENDING]` | Still starting: being scheduled, pulling images, creating containers, or ready on some replicas but not all | **no** |
| `[FAIL]` | Will not start without intervention: the scheduler has not placed it (`Unschedulable` — the node cannot fit it — or `SchedulingGated`), `ImagePullBackOff`, `CrashLoopBackOff`, a container config error, or a `Failed` Pod | yes |

Since #3832 the *reading* behind this table — phase, readiness, whether the
scheduler placed the Pod, and the cluster's own words for why it did not —
is `src/nyxgpt/k8s_pod_state.py`, shared with the
[self-heal watchdog](self-healing.md#pending-pods-are-reported-not-deleted)
so the install report and the watchdog cannot disagree about the same Pod.
The table above is `nyxgpt ops`' policy *on* that reading, and stays its own:
a `CrashLoopBackOff` Pod fails an install while the watchdog restarts it.

A Pod pulling a multi-hundred-megabyte image is doing what it is supposed to,
so `Pending` is reported as pending and never fails the command; what decides
whether the stack settled is the wait, which fails when its budget runs out.
The distinction is load-bearing rather than cosmetic: the acceptance run that
produced #3827 printed ten `[FAIL] pod …: Pending` lines for Pods that were
all Running three minutes later, and the one Pod that genuinely could not
start (`Insufficient memory`) was indistinguishable among them.

The waits use the same vocabulary, so a Pod in a state waiting cannot fix ends
the wait as soon as it is confirmed — naming that Pod and the scheduler's or
kubelet's own reason — instead of consuming the whole rollout budget first.
`CrashLoopBackOff` is the exception that gets a longer confirmation: it is the
one blocked reason a healthy bring-up passes through, because kubelet
escalates the restart delay up to five minutes and leaves the reason visible
long after the attempt that will succeed has been scheduled.

**Each workload's rollout budget is its own**, stamped when that workload's
wait begins. The waits run one after another, so a single deadline computed
up front spends the slow workloads' budgets on the ones before them — Ollama's
larger allowance exists precisely because a cold default-model pull is the
slowest thing the install does, and charging it for Cassandra's bootstrap
reinstated this issue's own false failure. The one deliberate exception is the
observability layer, whose dozen small workloads share a single pooled budget
and are passed one explicitly.

Both readouts the operator actually looks at carry these labels, not raw
`kubectl` output: `nyxgpt ops status` prints `[OK]`/`[PENDING]`/`[FAIL]` per
Pod (with the reason for a failed one) and per observability workload, and the
Infrastructure page in the admin dashboard badges both lists READY / PENDING
(amber) / FAILED from the same classification.

Each image build mirrors the Homebrew reinstall-if-needed behavior (see
[ops.md](ops.md)): it fingerprints the app source that image is built from
(`src/nyxgpt/` + `pyproject.toml` for `nyxgpt-api`; the web tree for
`nyxgpt-web`) and only re-runs `docker build` when that source changed since
the image was last built, reporting `<image>: built` / `rebuilt (source changed since last
build)` / `already up to date (skipped rebuild)` instead of always
rebuilding. `nyxgpt-web:local`'s build bakes `NEXT_PUBLIC_API_BASE_URL` into
the browser bundle at build time (see [web/Dockerfile](../web/Dockerfile)); it
defaults to the same host-local address the [Verify](#4-verify) section below
publishes. Nothing under `web/src` actually reads it today — every browser call
is a relative `/api/...` served by a Next.js route handler — so it is passed
for parity with the Terraform build rather than because the browser needs it
(#3986).

## Install modes: artifact (default) and `--dev`

`nyxgpt ops install --kubernetes` has the same two install modes the
native install has (#3789, #3834), and records which one this deployment is
running so nothing has to guess:

| | Where the two images come from | Needs a checkout? |
|---|---|---|
| **artifact** (default) | the published `nyxgpt-api-<version>.tar.gz` / `nyxgpt-web-<version>.tar.gz` release artifacts — the same ones the Homebrew formulas install — staged into their own build context under `~/.nyxGPT/build/kubernetes` | no |
| **dev** (`--dev`) | the working tree of the checkout you run the command in | yes — refused without one |

The artifact path is what makes this mode satisfy the project's [Repo-less
Portability](../CLAUDE.md) requirement: the manifests ship inside the package
(`nyxgpt.resources.k8s`, synced to `~/.nyxGPT/k8s` — which is why the
kustomization and `secret.yaml` live there and not in a repository), and the
images are built from published artifacts, so `pip install nyxgpt` followed by
this one command is a working deployment on a machine that has never seen this
repository. `.github/workflows/k8s-artifact-smoke.yml` runs exactly that, with
no checkout in reach, and requires a real chat to answer.

The artifacts are the *source* tarballs rather than the `ghcr.io` images on
purpose: a release publishes images, but a **release candidate** publishes only
the tarballs — and a candidate is what acceptance testing installs. One
artifact channel serves every local install mode, so the command behaves the
same on both.

`--dev` builds the working tree instead. The Pods then run images built from
that tree **as it was at install time** — re-run the command to pick new code
up; there is no live reload, unlike the native dev mode's Next dev server. It
is refused up front (exit 2) when there is no checkout to build, rather than
half-installing.

**Where dev mode is available.** `--dev` applies to the machine you run
`nyxgpt ops install`/`nyxgpt up` on — natively, under `--terraform`, and under
`--kubernetes` as described here. On a cloud target,
`nyxgpt cloud deploy --dev` ships your checkout to the EC2 instance and
installs it there ([cloud.md](cloud.md#dev-mode-on-a-cloud-target)) — on the
**native** stack by default, and on the **cluster** when combined with
`nyxgpt cloud deploy --kubernetes` (#3956), which passes `--dev` straight
through to the `nyxgpt ops install --kubernetes` it runs on the instance. So
the two flags compose there exactly as they do here: `--kubernetes` chooses the
substrate, `--dev` chooses where the images come from. That is
`product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md` (#3506) implemented —
it rejects a *managed EKS control plane* and calls for the existing
`k8s/*.yaml` layered on a single-node k3s cluster on that instance, which is
what [Kubernetes on the cloud target](#kubernetes-on-the-cloud-target) below
does. `--dev` on an EC2 **Mac** target (`--os macos`) is refused rather than
ignored, as is `--os macos --kubernetes`.

Switching between the modes re-rolls the app tier: both modes produce the same
`:local` tags, so the Deployment specs are identical across a switch and
`kubectl apply` alone would leave the Pods on the previous mode's image while
every report claimed the new one.

`nyxgpt ops status` prints the mode under the Kubernetes section (and
`nyxgpt ops doctor` as `Install mode (kubernetes): …`), separately from the
native api/web install mode — one machine can run a native dev install and a
Kubernetes artifact deployment at the same time. The Infrastructure page in
the admin dashboard shows the same thing. `nyxgpt ops down --kubernetes`
clears the record along with the deployment.

Locality: this flag deploys to a cluster on the local machine, and that is the
default — `nyxgpt ops install --kubernetes` needs no locality flag (#3948).
`--local` is still accepted as an explicit no-op, so the commands on this page
and in your scripts keep working. `--cloud` is accepted by the CLI surface but
rejected, and that is a limit of *this flag*, not of the product: the cloud
target is a different command, and it is deployed with
[`nyxgpt cloud deploy --kubernetes`](#kubernetes-on-the-cloud-target), which
provisions the substrate, installs a single-node k3s cluster on the instance,
and then runs `nyxgpt ops install --kubernetes` **there** (#3956).

The command refuses to start if the native/Compose stack already owns the
`api` port — run `nyxgpt ops down` (or stop the conflicting components)
first. `nyxgpt ops status`/`doctor` show this namespace's Pod states
alongside native/Compose, plus a per-component (`api`, `web`) canary rollout
line (stable/canary state and version) once pods are present -- see the
Canary Operations page (`/admin/canary`) for the equivalent web view and
traffic control.

Tear down (removes the `nyxgpt` namespace and everything in it, and --
only when it's the `nyxgpt-local` kind cluster nyxgpt provisioned above,
never a bring-your-own cluster -- the cluster itself too) with:

```bash
nyxgpt ops down --kubernetes
```

The rest of this document walks through what those two commands do, plus
the canary rollout tooling that operates on top of this deployment once
it's up — useful if you want to run the bring-up steps individually or
troubleshoot a failure. It is reference material, not something you're
expected to type by hand.

## Kubernetes on the cloud target

```bash
nyxgpt cloud deploy --kubernetes
```

Deploys the same stack onto an AWS EC2 instance, running on a **single-node
k3s cluster** installed on that instance. This is what makes
[canary rollout](#canary-deployment) available on the cloud target: the
stable/canary manifests need a cluster to weight traffic in, and without one
there is nothing for `nyxgpt canary` to operate on.

The substrate choice is the owner-approved decision in
[`DECISION_AWS_COMPUTE_SUBSTRATE.md`](../product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md):
EC2 single-box with these manifests layered on k3s, rather than a managed EKS
control plane. What is *not* used, and why:

- **No EKS.** A managed control plane is a standing monthly charge and a
  second cluster-lifecycle subsystem, and its natural ingress story (an ALB)
  contradicts the SSH-tunnel-only access model below.
- **No ingress controller and no `Service: LoadBalancer`.** k3s ships Traefik
  and `servicelb` on by default; the deploy disables both. Nothing in
  `k8s/*.yaml` asks for either, which is exactly the property that lets the
  manifests run on any cluster flavour unchanged.
- **The API server binds the instance's private address**, not `0.0.0.0`.
  The security group still allows only TCP 22, so this is defence in depth
  rather than the only barrier.
- **`local-path` stays enabled.** The Cassandra and Ollama StatefulSets bind
  through whatever the cluster's default StorageClass is, and on k3s that is
  `local-path`.
- **The pod and Service networks are pinned to `100.96.0.0/16` and
  `100.97.0.0/16`**, off k3s's own defaults, and the deploy **refuses** to
  install a cluster whose networks overlap the VPC's. See
  [Why the cluster networks are pinned](#why-the-cluster-networks-are-pinned)
  below.
- **CoreDNS forwards to `169.254.169.253`** on an AWS target — the VPC
  resolver's link-local alias — rather than to whatever the node's
  `/etc/resolv.conf` holds.

### Why the cluster networks are pinned

k3s defaults to `--cluster-cidr=10.42.0.0/16` for pods and
`--service-cidr=10.43.0.0/16` for Services. The substrate's VPC defaults to
`10.42.0.0/16`. Those two were byte-identical, and the result was a cluster
that could not resolve a name from the moment it started (#3956, found in
owner acceptance testing 2026-08-22):

1. AWS puts the VPC's DNS resolver at VPC-base + 2 — `10.42.0.2` — and hands
   it to the instance over DHCP.
2. k3s starts, and its CNI claims the on-node route for `10.42.0.0/16`,
   shadowing that resolver. Queries to `10.42.0.2` now land on whatever Pod
   holds that address.
3. That Pod is CoreDNS, whose own upstream is the node's resolver — itself.
   Its loop guard fires (`[FATAL] plugin/loop: Loop … detected for zone "."`)
   and it CrashLoopBackOffs.
4. Nothing in the cluster can resolve anything. The visible symptom is three
   layers away: the Ollama Pod cannot reach the model registry, never becomes
   Ready, and the deploy reports **"Ollama did not become ready in time"**.

Two things prevent it now, and a third stops it being silent:

- **The pin.** `100.96.0.0/16` and `100.97.0.0/16` are RFC 6598 (carrier-grade
  NAT) space — outside every RFC 1918 range a VPC is normally cut from. The
  VPC default is deliberately left alone: changing `vpc_cidr` makes Terraform
  *replace* the VPC, and with it the subnet, the instance and its root volume.
- **The refusal.** An operator-chosen `vpc_cidr` can still overlap, so the
  deploy reads the VPC's own network from instance metadata and refuses to
  install a cluster on top of an overlap, naming both networks. A cluster
  already on this instance whose networks overlap is replaced rather than
  reused — otherwise the "k3s is already here" fast path would leave a
  permanently DNS-dead cluster in place across every re-deploy.
- **CoreDNS's upstream.** On AWS it is set to `169.254.169.253`, the same
  resolver on its link-local alias, which no pod network can shadow. This is
  a k3s server flag rather than an edit to the CoreDNS ConfigMap on purpose:
  k3s re-applies its bundled CoreDNS manifest whenever the service restarts,
  so a patched ConfigMap silently reverts.

The Ollama Pod's model pull is no longer allowed to hide a failure like this
either: it retries with backoff, and a pull that ultimately fails exits
non-zero so kubelet records the registry's own error in a
`FailedPostStartHook` event, instead of the Pod sitting `0/1` forever with the
reason recorded nowhere.

**How it reuses the workstation path.** The deploy installs k3s, writes a
kubeconfig the login user owns, and then runs
`nyxgpt ops install --kubernetes --local` on the instance — the same command
documented above, taking the same bring-your-own-cluster branch it takes when
you already have a cluster running. There is no cloud-specific deployment code
path and the manifests are applied unchanged. The images are built on the
instance from the **published** `nyxgpt-api`/`nyxgpt-web` artifacts and
imported into k3s's containerd; no repository is ever checked out on the
instance.

**How you reach it.** Exactly as for a native cloud deployment: `nyxgpt cloud
tunnel` forwards `127.0.0.1:8000`/`127.0.0.1:3000` on your workstation over
SSH. The tunnel forwards to the *instance's* loopback, and the k3s cluster does
not bind it, so the deploy installs a small **access bridge** on the instance —
systemd `--user` services running `nyxgpt ops port-forward`, which is what
holds those loopback ports for the tunnel to forward to. They restart
automatically, which matters during a canary rollout: replacing a Pod ends a
port-forward. (The host publishing the local `kind` cluster gets since #3986
does not apply here: the base Services stay `ClusterIP` on this substrate, so
nothing but port 22 exists on the instance, and `nyxgpt ops install
--kubernetes` detects k3s and leaves those two loopback ports to the bridge
rather than starting a forward of its own.)

**Canary rollout against the cloud deployment:**

```bash
nyxgpt cloud canary status
nyxgpt cloud canary start --weight 10
nyxgpt cloud canary evaluate
nyxgpt cloud canary promote --step 25
nyxgpt cloud canary rollback
```

These run the instance's own `nyxgpt canary` over the same wrapped SSH path
`nyxgpt cloud ops` uses — the cluster's API server is reachable from the
instance and from nowhere else. `--component api|web` selects the pair, as it
does locally. `canary deploy` has no cloud form on purpose: it builds an image
from a source checkout, and the instance has none. Roll a new release out with
`nyxgpt cloud deploy --version <release>`, which is idempotent.

**Which substrate is running** is reported by `nyxgpt cloud status` and by the
admin dashboard's Infrastructure page. The choice is recorded, so a later bare
`nyxgpt cloud deploy` reconciles the same Kubernetes deployment rather than
installing a native stack beside it; `--no-kubernetes` moves a deployment back
to the native substrate. Both directions are a real transition, not a second
install: the deploy retires the substrate it replaces first (`nyxgpt ops down`
on the instance going in, the access bridge plus k3s's own uninstaller coming
out). Sessions do not follow the move — each substrate has its own Cassandra;
see [cloud.md](cloud.md#kubernetes-on-the-instance-3956).

**Teardown** is `nyxgpt cloud destroy --yes`. The cluster is entirely on the
instance — control plane, image store, `local-path` volumes — so terminating
the instance removes it; there is no separate cluster to tear down first.

See [cloud.md](cloud.md) for the substrate, access model and cost.

## Prerequisites

- Docker (to build the image, and to run `kind`'s cluster nodes as
  containers) -- the one prerequisite you install yourself
- A cluster VM with **8GiB of memory and 4 CPUs** -- the default Docker
  Desktop allocation. See [Node capacity: what the stack reserves](#node-capacity-what-the-stack-reserves)
  for what the deployment asks for and what the install does when it doesn't
  fit
- `kubectl` (with `kustomize` support, built in since 1.14) -- installed for
  you by `nyxgpt ops install --kubernetes` if it's missing (#3724)
- [kind](https://kind.sigs.k8s.io/#installation) -- also installed for you,
  and only needed if you want `nyxgpt ops install --kubernetes` to
  provision a cluster. If you already have a reachable cluster (minikube,
  Docker Desktop's built-in cluster, an existing kind cluster, or anything
  else `kubectl`'s current context points at), that's used as-is and `kind`
  is never installed or invoked.
- The [metrics-server](https://github.com/kubernetes-sigs/metrics-server) addon, required for the HorizontalPodAutoscaler to read CPU usage
  - minikube: `minikube addons enable metrics-server`
  - kind: `kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml` (add `--kubelet-insecure-tls` to the container args for local clusters without valid kubelet certs)

## Node capacity: what the stack reserves

The default deployment — app tier, data/LLM tier and the
[in-cluster observability layer](#observability-in-the-cluster) — reserves
about **5.5GiB of memory and 1.6 CPUs** in requests on a single-node cluster,
and a canary rollout **borrows** a further ~1344Mi and ~450m for as long as it
runs. That borrowing is the whole of [the elastic
pool](#the-replica-pool-is-borrowed-not-standing-3833): the stable
Deployments rest at 1 replica and `nyxgpt canary start` grows each track to at
most `[canary] total_replicas` (4 by default) Pods, so the peak is the same
figure the pool used to reserve standing — it is just no longer held by an
install nobody is rolling out.
A stock Docker Desktop VM (8GiB, 4 CPUs) offers 7936Mi of *allocatable*
memory and 4 CPUs — the kubelet's reserved slice is already out of those
numbers — of which kube-system holds a few hundred MiB and ~950m. So the
default stack fits both, with room for the rollout.

Two things are worth knowing about those figures:

- A Pod's **request** reserves capacity when the scheduler places it; its
  **limit** caps what it may then use. The api requests 256Mi and is capped
  at 1Gi, so a RAG or concurrent-chat burst has headroom without four
  replicas reserving a quarter of the node between them. Sizing the request
  like a limit is what left prometheus unschedulable in #3825.
- Requests are compared against **allocatable**, not against free memory. A
  node with plenty of RAM idle will still refuse a Pod whose request does
  not fit the unreserved remainder.
- **CPU is checked the same way, and it is the wall right behind memory.**
  With the memory right-sized, four api replicas reserving 250m each still
  stranded the canary Pod on a 4-CPU VM (`Insufficient cpu`) — the same
  failure with a different word in it. The api requests 100m and is capped
  at a full core; the web tier requests 50m and is capped at 500m.

`nyxgpt ops install --kubernetes` measures this **before it builds
the api and web images**, which is the expensive half of that command and the
first thing it would otherwise spend twenty minutes on: it totals what the
manifests will reserve, memory and CPU alike, compares each against the node's
allocatable capacity minus what other namespaces already hold, and — per
resource —

- **refuses**, naming the shortfall and the resource, if the stack cannot
  fit — rather than applying it and leaving a Pod `Pending /
  FailedScheduling: Insufficient memory` for you to find. Nothing is built,
  loaded or applied, so the refusal costs seconds and leaves the machine as it
  found it. Give the cluster VM more of whichever it named (Docker Desktop:
  Settings → Resources), or install without the observability layer:

  ```bash
  nyxgpt ops install --kubernetes --skip-observability
  ```

  The refusal names that second option only when dropping the layer would
  actually close the shortfall — if the app tier alone is still too big for
  the node, more memory is the only thing that helps and it says so.

- **warns** if it fits but a canary rollout would not, so
  `nyxgpt canary start` failing later is a known constraint rather than a
  surprise;
- **skips** itself, never blocking, if it cannot read the node — and on a
  multi-node cluster reports rather than refuses, since summed allocatable
  capacity can disprove a placement but never prove one.

After the fact, the [Infrastructure page](#infrastructure-status-card-3468)
names any Pod no node would take, separately from the Pod list: an
unschedulable Pod reads as `Pending` there, which is also what a Pod that is
placed and pulling its image reads as — so the stranded prometheus of #3825
looked, on that page, exactly like a stack still starting up.

## 0. Create a cluster (if you don't have one)

`nyxgpt ops install --kubernetes` does this step for you automatically
(see [One-command bring-up](#one-command-bring-up-nyxgpt-ops) above) --
provisioning a `kind` cluster named `nyxgpt-local` when kubectl's current
context has no reachable cluster. This section documents what that step does
manually, for reference:

```bash
# The config is what publishes the app tier on the host (#3986) -- a bare
# `kind create cluster` publishes nothing, which is why the web UI used to be
# unreachable after a successful install. `nyxgpt ops install --kubernetes`
# writes this file to ~/.nyxGPT/k8s/kind-cluster.yaml and passes it.
cat > kind-cluster.yaml <<'YAML'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30300     # the node port nyxgpt-web is published on
        hostPort: 3000
        listenAddress: "127.0.0.1"
        protocol: TCP
      - containerPort: 30800     # ...and nyxgpt-api
        hostPort: 8000
        listenAddress: "127.0.0.1"
        protocol: TCP
YAML
kind create cluster --name nyxgpt-local --config kind-cluster.yaml
```

If you'd rather bring your own cluster (minikube, Docker Desktop, a remote
context, ...), create/select it yourself and the wrapper will use it as-is.

> The steps below are the **manual** equivalent of the wrapped command, run
> from a source checkout. They are reference material for troubleshooting; the
> wrapped command reads its manifests from the synced copy under
> `~/.nyxGPT/k8s` instead of from a checkout.

## 1. Build the image

```bash
docker build -t nyxgpt-api:local .
```

Load it into your cluster's local image cache (skip this for Docker Desktop's
built-in cluster, which shares the host's image cache):

```bash
# kind
kind load docker-image nyxgpt-api:local

# minikube
minikube image load nyxgpt-api:local
```

## 2. Configure the secret

The API key used for `[auth]` (see `k8s/configmap.yaml`) is supplied via a
Secret rather than committed to the repo:

```bash
cp k8s/secret.example.yaml k8s/secret.yaml
# edit k8s/secret.yaml and set a real api-key value
```

`k8s/secret.yaml` is gitignored — never commit real credentials.

## 3. Apply the manifests

```bash
kubectl apply -k k8s/
```

This creates the `nyxgpt` namespace, the ConfigMap, Secret, RBAC
(`k8s/rbac.yaml` — a `nyxgpt-api` ServiceAccount and a Role/RoleBinding
scoped to just the Deployment/Service operations below), the `cassandra` and
`ollama` StatefulSets and Services that make up the [data and LLM
tier](#data-and-llm-tier), the
`nyxgpt-api-stable` Deployment (1 replica by default) and
`nyxgpt-api-canary` Deployment (0 replicas — idle until a rollout starts),
the same stable/canary pair for `nyxgpt-web` (`k8s/deployment-web-stable.yaml`
/ `k8s/deployment-web-canary.yaml`, 1/0 replicas by default), and the
`nyxgpt-api`/`nyxgpt-api-canary`/`nyxgpt-web`/`nyxgpt-web-canary` Services
(each pair selects every Pod from either Deployment in that component;
traffic split is by replica count, not Service selector). Every Service here
is `ClusterIP`; on a cluster nyxGPT provisioned, the wrapped install publishes
`nyxgpt-web`/`nyxgpt-api` on node ports `30300`/`30800` afterwards (#3986) —
see [4. Verify](#4-verify). Doing that by hand is
`kubectl -n nyxgpt patch svc nyxgpt-web -p '{"spec":{"type":"NodePort",...}}'`,
which is exactly why it is wrapped. `nyxgpt-web`'s
Pods get `NYXGPT_API_BASE_URL=http://nyxgpt-api:8000` (the api Service's
in-cluster DNS name) so its server-side proxy routes reach the api without
needing the api exposed outside the cluster, and the same `NYXGPT_AUTH_API_KEY`
secret the api Deployments use.

Every `nyxgpt-api` Pod (stable/canary) runs as the `nyxgpt-api`
ServiceAccount and ships its own `kubectl`, so `/admin/canary` (and the API
endpoints behind it) works when hit through a Pod running in this cluster —
it calls `kubectl` in-cluster, authenticated via the mounted ServiceAccount
token, scoped by `k8s/rbac.yaml`'s Role (which is namespace-scoped rather
than restricted to specific Deployment names, so it already covers the
`nyxgpt-web-stable`/`-canary` Deployments too — no RBAC changes were needed
to add web canary). `nyxgpt-web` Pods don't run `kubectl` themselves; the
web UI's Canary Operations page just calls the api's endpoints. This is what
makes the Kubernetes deployment mode, not docker-compose, the one where that
dashboard is operable (see
[docker-compose.md](docker-compose.md#canary-deployment)).

## 4. Verify

```bash
nyxgpt ops status
curl -H "X-API-Key: <your api-key>" http://127.0.0.1:8000/health
```

Then open `http://127.0.0.1:3000` — **no port-forward required** (#3986). The
`kind` cluster `nyxgpt ops install --kubernetes` provisions is created with
`extraPortMappings` publishing node ports `30300`/`30800` on the host's
`3000`/`8000` (loopback only), and the install then publishes `nyxgpt-web` and
`nyxgpt-api` on those node ports. Because both halves are properties of the
*cluster* rather than of a running process, the URL keeps working across a
canary rollout, a self-heal Pod restart and an image change — which a `kubectl
port-forward` does not: it attaches to one Pod and exits when that Pod is
replaced.

The NodePort is **applied by the install, not declared in `k8s/`**, and that is
deliberate. The same manifests are applied by the AWS k3s deployment, whose
invariant is that nothing but port 22 exists on the instance (#3503,
[security.md](security.md)); a NodePort in the base manifest would bind on that
node's interfaces too. So the base Services stay `ClusterIP` everywhere, and
the patch is applied only where nyxGPT created the cluster *and* mapped its
ports to loopback. A bring-your-own local cluster gets the same treatment for
the same reason — opening node ports on a cluster nyxGPT did not create is not
its call — and is reached through the managed background forward below.

`nyxgpt up --kubernetes` prints the URL once the stack reports healthy, and the
install verifies it before it returns.

### Reaching a bring-your-own cluster

On a cluster nyxGPT did not create, mapping a host port is not nyxGPT's to
arrange — the NodePorts exist, but whether they reach your workstation depends
on where the nodes are. The install therefore establishes a **managed
background forward** instead, in the same shape as `nyxgpt cloud tunnel
--background`: detached, its pid recorded, and *supervised*, so it is restarted
when a Pod is replaced.

```bash
# Started automatically by the install; these inspect and end it
nyxgpt ops port-forward --status
nyxgpt ops port-forward --stop

# Or establish it yourself (web + api together)
nyxgpt ops port-forward --target app --background
```

`nyxgpt ops down --kubernetes` releases it along with the deployment. A
foreground `nyxgpt ops port-forward` still works and is unchanged, for a
one-off look at a single Service.

**Not on the AWS k3s deployment**, which already has an owner for those two
loopback ports: the systemd `--user` [access
bridge](#kubernetes-on-the-cloud-target) the deploy installs. The install
detects that substrate and reports the arrangement instead of starting a
second forward that would win the bind race and leave every bridge unit
restarting forever.

The web UI does **not** need the api forwarded to reach it: nothing under
`web/src` reads `NEXT_PUBLIC_API_BASE_URL`, and every browser call is a
relative `/api/...` served by a Next.js route handler that reaches the api
in-cluster. The api forward is there for `curl`, the CLI and
[api.md](api.md)'s examples.

The observability UIs are reached the same way, all four at once:

```bash
nyxgpt ops port-forward --target observability
```

See [Observability in the cluster](#observability-in-the-cluster) below.

Every Pod deployed here -- api, web, Cassandra, Ollama and the observability
overlay -- is also watched by the same
[self-heal watchdog](self-healing.md) as every other deployment path -- see
[self-healing.md#kubernetes-mode](self-healing.md#kubernetes-mode) for how it
checks Pod readiness via `kubectl get pods` and heals via `kubectl delete
pod`, on top of (not instead of) the liveness probes and the canary
mechanism described below. That remedy is restricted to a Pod that is
Running but not Ready -- a `Pending` Pod is reported with the cluster's own
reason and never deleted (#3832; see [Pending Pods are reported, not
deleted](self-healing.md#pending-pods-are-reported-not-deleted)). `k8s/rbac.yaml`'s `nyxgpt-api` Role grants the
`get`/`list`/`delete` on `pods` this needs, alongside what canary already
used.

## Observability in the cluster

`nyxgpt ops install --kubernetes` deploys the observability tier
into the cluster alongside the app tier (#3787). The Compose observability
profiles cannot serve this mode -- they scrape `host.docker.internal` and
resolve Compose service names on a Docker network, neither of which exists
from inside a cluster -- so `k8s/observability/` ships the same components as
in-cluster workloads:

| Workload | Role | Service |
| --- | --- | --- |
| `prometheus` | scrapes `svc/nyxgpt-api:8000/metrics` | `prometheus:9090` |
| `grafana` | single pane of glass (dashboards, alerting, Explore) | `grafana:3000` |
| `loki` | log store | `loki:3100` |
| `promtail` (DaemonSet) | ships every `nyxgpt` namespace Pod's logs into Loki | — |
| `otel-collector` | OTLP endpoint for the api's spans | `otel-collector:4317/4318` |
| `jaeger` | trace storage + query API | `jaeger:16686` |
| `glitchtip`, `glitchtip-worker`, `glitchtip-postgres`, `glitchtip-redis` | self-hosted error tracking | `glitchtip:8080` |

Grafana's datasources, dashboards and alerting are **not** a second copy:
`nyxgpt ops` generates ConfigMaps straight from `docker/grafana/` (synced
under `~/.nyxGPT/docker`) at apply time, and the Services above deliberately
carry the same names as their Compose counterparts, so provisioning like
`url: http://prometheus:9090` resolves unchanged. One set of dashboards
serves every deployment mode.

Commands (all wrapped -- no raw `kubectl`):

```bash
# Deploy or re-apply the layer on its own, without touching the app tier
nyxgpt ops observability --kubernetes

# Publish Grafana (3001), Prometheus (9090), Jaeger (16686) and GlitchTip
# (8080) on localhost -- the same ports the admin dashboard links to
nyxgpt ops port-forward --target observability

# Per-workload readiness, alongside the app tier's Pods
nyxgpt ops status
```

`nyxgpt ops install --kubernetes --skip-observability` opts out (the
app tier only), and `nyxgpt ops down --kubernetes` removes the layer with
everything else in the namespace.

Notes:

- **Storage is ephemeral.** Prometheus, Loki, Grafana and GlitchTip's
  Postgres use `emptyDir`, not PersistentVolumeClaims: `nyxgpt ops down
  --kubernetes` deletes the local cluster nyxgpt provisioned, so there is
  nothing for that data to outlive. The same holds on the
  [cloud target](#kubernetes-on-the-cloud-target), where `nyxgpt cloud
  destroy` deletes the instance the cluster lives on. Long-lived retention is
  the native / Compose path's job (see [ops.md](ops.md)).
- **Secrets.** `k8s/observability/secret.yaml` is bootstrapped from
  `secret.example.yaml` on first apply and never committed. It carries
  Grafana's admin password (from `[monitoring] grafana_admin_password` when
  set), the Slack webhook Grafana's alerting contact point reads via
  `$__file{}` (from `[monitoring] slack_webhook_url`), the GlitchTip token
  placeholder, and GlitchTip's own Django/Postgres credentials. Every value
  is non-empty even when unconfigured -- Grafana's alerting validator
  refuses to boot on an empty contact-point URL (#3538). Delete the file to
  re-bootstrap it from current config.
- **Log labels.** promtail discovers Pods through the Kubernetes API instead
  of tailing files, but keeps the label contract the dashboards query on:
  `job="nyxgpt"` plus a per-component `service_name` (`api`, `web`,
  `grafana`, ...), with the same level/logger extraction as
  `docker/promtail-config.yml`.
- **Restarts are Kubernetes' own, with the watchdog on top.** Each of these
  workloads is a Deployment (or DaemonSet), so the cluster's own controllers
  restart a failed Pod. The [self-heal watchdog](self-healing.md) watches
  these Pods too (#3828 -- it reports them under `tier: observability` and
  heals a stuck one by deleting it, the same action it takes on an app-tier
  Pod, and under the same restriction: Running-but-not-Ready only, never a
  `Pending` Pod -- see [Pending Pods are reported, not
  deleted](self-healing.md#pending-pods-are-reported-not-deleted)), which is
  what makes the tier visible on the Self-Heal page in this
  mode rather than surveyed through a Compose stack the deployment does not
  have.
- **Evidence.** `.github/workflows/k8s-observability-smoke.yml` runs the
  whole thing on a real kind cluster: it first proves the pre-#3787 app-tier
  apply leaves zero observability workloads, then asserts all ten roll out,
  every UI answers, Grafana has its four provisioned datasources and the SRE
  Home dashboard, and promtail's logs actually reach Loki.
  `.github/workflows/k8s-local-smoke.yml` covers the other half -- that the
  layer comes up *with* the app tier in the **default** install, on one node
  the size of a stock Docker Desktop VM, with no Pod the scheduler could not
  place (#3826, #3825) -- and its `k8s-pod-state` job
  (`scripts/k8s-pod-state-smoke.py`) proves on a real cluster that a Pod which
  is merely starting is reported as pending while an unschedulable or
  unpullable one is a named failure (#3827), including that the pre-fix rule
  called both of them the same thing.
- **Footprint.** After the #3825 right-sizing and #3833's elastic canary
  pool, the default stack (app + data/LLM + observability) requests
  **~1.6 CPU and ~5.5GiB** of memory standing, so it fits the
  4-vCPU/7936Mi a stock Docker Desktop VM offers with room for a rollout to
  borrow — see [Node capacity: what the stack
  reserves](#node-capacity-what-the-stack-reserves) for the per-tier numbers
  and the preflight that checks them. `--skip-observability` drops roughly
  0.5 CPU and 2.4GiB of that; the k8s smoke runs on a node ballasted down to
  that VM's size and prints the allocatable-versus-requests arithmetic on
  every run, so the numbers stay observed rather than remembered.

## Data and LLM tier

Both live **inside the cluster** (#3786), so the deployment is complete on
its own: no host Ollama, no bring-your-own Cassandra, no `hostAliases`
pointing back at the workstation. `nyxgpt ops install --kubernetes`
stops the host's own services in this mode, so anything that depended on
them could not work anyway — the previous configuration pointed the api at
`host.docker.internal:11434`, which does not resolve from a Pod on a Linux
cluster and pointed at a stopped Ollama everywhere else. The result was a
stack whose Pods all reported Running and which could not answer a single
message.

| Component | Manifest | Service | Storage |
| --- | --- | --- | --- |
| Cassandra | `k8s/statefulset-cassandra.yaml` | `cassandra:9042` | 10Gi PVC (`data-cassandra-0`) |
| Ollama | `k8s/statefulset-ollama.yaml` | `ollama:11434` | 20Gi PVC (`models-ollama-0`) |

- **Both are single-replica StatefulSets.** Cassandra's data directory and
  Ollama's model blob store are each owned by exactly one process; neither
  may be shared between two Pods (the same storage argument that rules out
  an Ollama canary pair — see [Ollama canary
  feasibility](#ollama-canary-feasibility)).
- **Readiness means usable, not merely listening.** Cassandra's probe runs a
  CQL query, and Ollama's probe checks that *both* configured models —
  `[nyxgpt] default_model` and `[rag] embedding_model` — are actually present;
  its `postStart` hook pulls them on first start, so a fresh install can
  answer a chat, and a RAG-enabled chat, without anyone pulling a model by
  hand (#3824). The embedding model is gated on even though
  `enable_chat_context` ships false, because RAG is a per-session toggle the
  user can turn on at any moment. Neither Service gets endpoints before
  that's true, and `nyxgpt ops install --kubernetes` waits for both
  Pods to be Ready before it reports the stack healthy.
- **Sessions are shared.** `k8s/configmap.yaml` sets
  `[nyxgpt] session_backend = cassandra`, so every api replica reads and
  writes one session list — including the extra ones a canary rollout borrows
  (#3833) and the replacement Pod every restart creates. With the file
  backend each replica keeps its own, and consecutive requests from one
  browser see different sessions (see
  [session-storage.md](session-storage.md)).
- **RAG** is off by default (matching the native/Compose default in
  `example.config.ini`) because it only helps once you have ingested
  documents — but `[rag] cassandra_hosts` already points at the in-cluster
  Cassandra, so enabling `enable_chat_context` is a one-line ConfigMap
  change.
- **The PVCs live in the cluster.** `nyxgpt ops down --kubernetes` deletes
  the `nyxgpt` namespace — and, when it provisioned the cluster itself, the
  `nyxgpt-local` kind cluster too — which discards those volumes along with
  the chats in them. Unlike the native/Compose/Terraform modes, which share
  `~/.nyxGPT/volumes/`, Kubernetes-mode data does not survive a teardown;
  export anything you want to keep first (`/api/v1/sessions/{name}/export`).

Both hostnames (`cassandra`, `ollama`) are the same ones the Compose and
Terraform deployments use for these components, so the api's config is
identical across every containerized mode.

To point the deployment at an *external* Cassandra or Ollama instead, change
`[rag] cassandra_hosts` / `[ollama] base_url` in `configmap.yaml` and drop the
corresponding manifest from `kustomization.yaml`.

Edit the copy under `~/.nyxGPT/k8s/` — that is the one the wrapped command
applies. Every `nyxgpt ops install --kubernetes` re-syncs that
directory from the package's own manifests, so an edit there survives until the
next install and is then replaced (`secret.yaml` is the exception: it holds the
API key, is never packaged, and is left alone). From a source checkout, edit the
repository's `k8s/` instead and the sync carries the change through — the
package data is a symlink back to it, so a checkout customization is durable
where a `~/.nyxGPT/k8s/` one is not.

## Infrastructure Status card (#3468)

The **Infrastructure Status** admin page (`/admin/infrastructure`, see
[ui.md](ui.md#admin-dashboard)) reports the Kubernetes probe (`GET
/api/v1/infra/status`, backed by `ops.infra_status()`) as one of three honest
states rather than folding every failure into a single "not deployed":

- **NOT DEPLOYED** -- this process has no cluster to talk to at all: no
  kubeconfig current-context *and* no in-cluster ServiceAccount credentials
  (`kubectl` missing from PATH is folded into this same bucket, since there's
  then nothing to talk to it with). By definition, no configured cluster means
  there was never anything to be unreachable. This is also the state once a
  cluster *is* configured, it answers, and the `nyxgpt` namespace simply has
  no Pods.
- **CANNOT DETERMINE** -- a cluster is configured but the `kubectl -n
  nyxgpt get pods` probe itself failed (timeout, connection refused to a
  cluster that's meant to exist, an auth failure). This is reserved for a
  cluster that's genuinely supposed to be there, preserving the original
  #3410 protection against misreporting a flaky-but-real cluster as "not
  deployed."
- **DEPLOYED** -- the probe succeeded and found Pods in the `nyxgpt`
  namespace.

### Served from inside the cluster (#3988)

In Kubernetes mode this page is rendered by the api Pod, about the cluster
that Pod runs in — and the detection gate used to ask `kubectl config
current-context`, which is **empty in a Pod**: in-cluster authentication uses
the mounted ServiceAccount token and `KUBERNETES_SERVICE_HOST`, not a
kubeconfig context. The page therefore reported the deployment it was being
served from as NOT DEPLOYED. Detection now accepts either credential, and the
payload carries `in_cluster` so the page can say which vantage point the answer
came from. `k8s/rbac.yaml`'s Role grants the `list` on
`deployments`/`daemonsets` that the observability card's read needs, so no part
of the page depends on a cluster's default permissions.

The same vantage point makes two other cards **out of scope** rather than
merely unknown, and they now say so:

- **Docker Compose** — a Pod has no host filesystem and no Docker socket, so
  there is no survey to run. It used to report the *container's* own
  `/root/.nyxGPT/docker-compose.yml` to the operator as the reason.
- **Native** — the install identity and its remedies (`nyxgpt up`,
  `nyxgpt ops doctor`) describe the machine this process runs on, which from
  inside a Pod is not the deployment the operator is asking about.

Run `nyxgpt ops status` on the host to survey either of those there.

Each Pod on that card is badged with the same three states the CLI prints,
from `kubernetes.pod_states` in the JSON (#3827): **READY**, **PENDING** (still
scheduling, pulling or creating containers -- amber, because that is a normal
stage of a rollout and not a fault) and **FAILED**, which carries the
scheduler's or kubelet's own reason. The raw `kubectl get pods` line the card
used to echo says `Pending` for both of the last two, which is the same
conflation the install used to print — see [Ready, pending,
failed](#ready-pending-failed).

Below that list, the card also names any Pod **no node would take** (#3825),
and says what to do about it: the badge tells the operator the Pod will not
start, but not that the remedy is a bigger cluster rather than a fix to the
workload. `kubernetes.unschedulable` in the JSON is the FAILED subset of
`kubernetes.pod_states` whose reason is `unschedulable`, read from the same
classification as the badges rather than from a second probe, so the two
halves of the card cannot disagree about a Pod -- and one that has simply not
been placed *yet* is PENDING and is not named here. Reporting only, as with
everything else on this page -- the cure is more memory or CPU on the cluster
VM and a re-run of `nyxgpt ops install --kubernetes`, which checks the
node's capacity against the stack before it applies anything (see [Node
capacity: what the stack reserves](#node-capacity-what-the-stack-reserves)).

The card also names this deployment's own **install mode** (#3834) — what the
two images in the cluster were built from, per [Install
modes](#install-modes-artifact-and---dev) — or `unrecorded` when there is no
marker for it on the machine the dashboard is running on, which is never
presented as the artifact default: that default would be a guess about someone
else's deployment.

The same card carries an **In-cluster observability** section (#3787):
per-workload readiness for the components in [Observability in the
cluster](#observability-in-the-cluster), plus the `nyxgpt ops port-forward
--target observability` command that publishes their UIs on the ports the
dashboard's own observability links use. When the layer isn't deployed it
names the command that deploys it, rather than leaving the operator to
discover that this mode has no observability at all.

This mirrors, but is distinct from, the canary status honesty states
described in [Honest status, mode-aware (#3409)](#honest-status-mode-aware-3409)
below -- that section covers per-track rollout health once something is
deployed, while this one covers whether a cluster is deployed at all.

Once a context is configured, both the card and `nyxgpt ops status`/`doctor`
(#3596) additionally report the current context name and whether it's the
`nyxgpt-local` kind cluster nyxgpt provisioned (`kubernetes.provisioned` in
the JSON) or a bring-your-own cluster -- this is what tells the operator
whether `nyxgpt ops down --kubernetes` will also delete the cluster itself,
not just the deployment inside it (see [Tear
down](#one-command-bring-up-nyxgpt-ops) above and `_down_kubernetes_steps` in
`src/nyxgpt/ops.py`). Self-heal's per-Pod health checks (`kubectl get
pods`/`kubectl delete pod`, see
[self-healing.md#kubernetes-mode](self-healing.md#kubernetes-mode)) work
identically regardless of which cluster backs the current context -- they
operate on whatever `kubectl` is pointed at, kind-provisioned or not.

## Canary Deployment

Canary is the sole deployment model (#3409 retired blue/green -- a separate
two-color pair with instant all-or-nothing cutover -- in favor of canary,
which is a strict superset for traffic purposes: 0%/100% weight reproduces
the same cutover, plus canary adds metrics-gated gradual shift and
auto-rollback blue/green never had).

`k8s/deployment-stable.yaml` and `k8s/deployment-canary.yaml` are two
independent Deployments for `nyxgpt-api`, both labeled
`app: nyxgpt-api-canary-pool` and distinguished by `track: stable`/
`track: canary`. `k8s/service.yaml` and `k8s/service-canary.yaml` both
select `app: nyxgpt-api-canary-pool`, targeting **both** Deployments' Pods
at once -- kube-proxy round-robins Service traffic evenly across every
matching Pod endpoint, so `canary_replicas / pool_replicas` approximates
the canary's share of requests. `k8s/deployment-web-stable.yaml` /
`k8s/deployment-web-canary.yaml` and `k8s/service-web.yaml` /
`k8s/service-web-canary.yaml` mirror the exact same model for `nyxgpt-web`
(label `app: nyxgpt-web-canary-pool`). There is no in-cluster proxy or
ingress to configure for either pair, just `kubectl scale`/`kubectl set
image` (wrapped by `nyxgpt canary`). Neither pair's Deployments have an HPA
attached -- autoscaling would fight the canary tool's replica-count-based
traffic split (see [Scaling behavior](#scaling-behavior)).

**Coverage**: `api` and `web` (#3419) -- pass `--component web` (CLI) or
`component=web` (API/dashboard) to operate on the web pair instead of the
default `api`; every `nyxgpt canary`/`/api/v1/canary/*` command below
accepts it. `ollama` is **not implemented** -- see [Ollama canary
feasibility](#ollama-canary-feasibility) below for the analysis and why.
**Cassandra is explicitly out of scope**: two Cassandras behind a canary
split would mean two divergent datasets, which is a data-migration problem,
not a traffic-split problem. A schema/version-upgrade story for Cassandra
will be designed when a version upgrade actually requires one (a future
issue, not this one).

### The replica pool is borrowed, not standing (#3833)

The stable Deployments rest at **1 replica**. A rollout borrows the replicas
its weight needs, and gives them back:

- `nyxgpt canary start` reads the stable Deployment's **live** replica count,
  plans the smallest pool that can express the requested weight (capped by
  `[canary] total_replicas`, 4 by default), and scales both tracks to it.
  Stable grows before the canary does, so the canary never briefly holds a
  larger share than you asked for; if the canary scale then fails, stable is
  put back, so a failed start leaves nothing inflated behind.
- `nyxgpt canary promote` re-plans the pool for each new weight — a step that
  needs finer granularity grows it, one that needs less lets it shrink.
- `nyxgpt canary promote` at 100% and `nyxgpt canary rollback` both return
  stable to the count it was resting at when the rollout started. Scale stable
  to 3 yourself and it comes back to 3: nothing re-inflates it to a constant.

Replica counts are integers, so most weights are not exactly expressible —
10% needs a 10-wide pool. Rather than silently serving a different weight (or
growing the cluster to honour the number literally), the command reports the
weight it actually rounded to:

```text
[OK] Started canary rollout at 25% (1/4 replicas); 10% is not expressible in a
pool of at most 4 replicas, so it rounded to 25% -- raise `[canary]
total_replicas` for finer steps; nyxgpt-api-stable returns to 1 replica on
promote or rollback
```

`/admin/canary` shows the same numbers: the badge names the pool the rollout
borrowed and the count stable rests at, and every action's result message is
the server's own.

Sizing `[canary] total_replicas` is the cost/granularity trade: `2` keeps a
rollout to one extra Pod (weights round to 50%), `4` makes 25% steps
expressible. Before #3833 the manifests shipped a standing `replicas: 4` pool
that a rollout merely subdivided, so every install — including single-node
local ones where 4 replicas buy no HA — paid 3072Mi of reservations to make a
25% step possible.

### Ollama canary feasibility

Ollama canary was evaluated for this issue and is **not implemented**, by
design rather than by omission. The blocker is storage, not traffic
splitting:

- **`ollama serve` owns a single model store.** Unlike `nyxgpt-api`/
  `nyxgpt-web`, which are stateless behind their Deployments (sessions live
  in the shared in-cluster Cassandra, and the vector store is the only
  per-pod state left -- see [Scaling behavior](#scaling-behavior)), an
  Ollama instance's pulled models live in its own local blob directory that
  it both reads and writes. There is no "read replica" concept for Ollama
  the way there is for a stateless HTTP service. This is an argument against
  a stable/canary *pair*, not against running Ollama in the cluster at all:
  the deployment runs exactly one Ollama StatefulSet (see [Data and LLM
  tier](#data-and-llm-tier)), which has no concurrent-writer problem.
- **A stable/canary pair needs the pair to run genuinely different
  versions** (that's the entire point -- see [The deploy -> gate -> promote
  cycle](#the-deploy---gate---promote-cycle)). For Ollama that means either:
  1. **A shared volume** between the stable and canary Pods, so both see
     the same pulled models. This introduces concurrent writers: if a
     canary rollout pulls or evicts a model while stable is actively
     serving requests against it, there is no documented Ollama guidance
     that concurrent blob-store mutation from two processes is safe, and a
     corrupted or partially-evicted blob would take down *both* tracks at
     once -- the opposite of what canary is for.
  2. **Per-track storage** (each track pulls and keeps its own copy of
     whatever models it's running). This avoids the concurrency problem but
     doubles local disk usage for models that routinely run several
     gigabytes each -- a real cost on the local-first, single-workstation
     target this deployment path is designed for (see
     [VISION.md](../product_management/VISION.md)), not a cloud cluster
     with elastic storage.
- Neither tradeoff is acceptable to ship silently, so this documents the
  infeasibility rather than shipping an unsound split: `nyxgpt canary
  status/deploy/start/... --component ollama` (and `component=ollama` on
  the API/dashboard) refuse with this same explanation
  (`canary.OLLAMA_UNSUPPORTED_REASON`) instead of pretending to work or
  silently no-opping.
- This isn't necessarily permanent: if Ollama gains a supported multi-instance
  or shared-storage story (e.g. a documented safe-concurrent-pull mode, or a
  read-only replica mode), revisit this analysis. Until then, the deployment
  runs exactly one Ollama StatefulSet (see [Data and LLM
  tier](#data-and-llm-tier)) rather than a stable/canary pair.

### The deploy -> gate -> promote cycle

1. **Deploy** the current checkout to canary only -- builds a versioned
   image (`<project version>-<git short sha>`, e.g. `nyxgpt-api:2.0.0-abc1234`
   -- never the mutable `:local` tag), patches only the canary Deployment's
   image, and waits for its rollout. Stable is never touched, even on
   failure:
   ```bash
   nyxgpt canary deploy
   ```
2. **Gate**: start the rollout at a small initial traffic weight, then watch
   the **canary track's own** error-rate/p95-latency metrics (read from the
   Pods labelled `track=canary`, see [Metrics
   source](#metrics-source-the-canary-tracks-own-pods-3829)) against the
   configured thresholds -- `evaluate` automatically rolls the canary back
   if either is breached, and holds rather than passing while the canary has
   taken too little traffic to judge:
   ```bash
   nyxgpt canary start --weight 10
   nyxgpt canary status
   nyxgpt canary evaluate
   ```
3. **Promote**: if `evaluate` reports the canary is safe, increase its
   traffic share:
   ```bash
   nyxgpt canary promote          # adds [canary] step_percent (default 25)
   ```
   Repeat steps 2-3 until `promote` reaches 100%. At that final step,
   `promote` copies the canary's image version onto `nyxgpt-api-stable`,
   waits for stable's rollout to become healthy, then scales canary back to
   0 and stable back to the count it was resting at before the rollout
   borrowed any (see [The replica pool is borrowed, not
   standing](#the-replica-pool-is-borrowed-not-standing-3833)) -- stable now
   runs the promoted version at 100% traffic, and the cycle is complete.
   `promote` refuses to
   shift more traffic to the canary at every step (including this final
   one) unless the canary is currently healthy **and has actually served
   requests** (`--force` overrides the traffic half on an idle cluster --
   see [Metrics
   source](#metrics-source-the-canary-tracks-own-pods-3829)), and if stable's rollout
   onto the new version fails, canary is left running untouched so you can
   retry or roll back.
4. If something is wrong at any point, cut all traffic back to stable
   immediately:
   ```bash
   nyxgpt canary rollback
   ```
   `rollback` scales the canary Deployment to 0 first (removing it from the
   Service's endpoints) before restoring stable, and is not blocked by a
   flaky stable-scale-up -- it's the emergency escape hatch.

### CLI reference

```bash
nyxgpt canary status                 # rollout progress, stable/canary health + version, per-track metrics
nyxgpt canary deploy                 # build a versioned image and deploy it to canary only
nyxgpt canary start [--weight N]     # start a rollout at N% canary traffic (default: 10)
nyxgpt canary evaluate               # check the canary track's metrics vs thresholds; auto-rollback on regression
nyxgpt canary promote [--step N]     # add N percentage points to canary's traffic share (100% promotes)
                       [--force]     # ... even though the canary track has served no traffic (idle cluster)
nyxgpt canary rollback               # cut all traffic back to the stable deployment
nyxgpt canary reset                  # return an IDLE canary Deployment carrying replicas to its resting 0
```

All seven commands accept `--namespace` to override the `[canary] namespace`
config value (see `example.config.ini`); it defaults to `nyxgpt`. They also
all accept `--component {api,web}` (default: `api`) to operate on the
`nyxgpt-web` pair instead -- e.g. `nyxgpt canary deploy --component web`.
`total_replicas` (the ceiling on the pool a rollout may borrow, not a
standing pool -- see [The replica pool is borrowed, not
standing](#the-replica-pool-is-borrowed-not-standing-3833)), `step_percent`,
`error_rate_threshold_percent`, `latency_p95_threshold_ms`, and
`min_requests_for_evaluation` are also configured in `[canary]`.

### Standing an off-contract canary down (#3991)

`k8s/deployment-canary.yaml` and `k8s/deployment-web-canary.yaml` declare
`replicas: 0`: the canary track is idle until `nyxgpt canary start` scales it
up, and `promote`/`rollback` scale it back down. A cluster can still be found
with an **idle canary carrying replicas** — two Pods holding live Service
endpoints outside any rollout — by two routes:

- `canary start` scales the canary up before it scales stable, and a failure on
  that second scale returns before the rollout state is written. The cluster
  has the replicas; the state file says idle.
- The rollout state file is `~/.nyxGPT/canary_state.json`, and the api
  Deployment mounts no volume over it. A rollout started from the dashboard
  records its state inside the serving Pod's ephemeral filesystem, so the next
  Pod replacement takes the state with it and leaves the replicas.

In that state `nyxgpt canary rollback` correctly refuses — "No canary rollout
in progress"; ending a rollout is its contract and there is no rollout to end —
which used to leave a raw `kubectl scale` as the only way back.
`nyxgpt canary reset` is the wrapped path: it scales an idle canary to 0 (and
deflates a still-inflated stable to its resting count), and refuses while a
rollout *is* in progress, pointing at `rollback`/`promote` instead.

`nyxgpt ops install --kubernetes` runs the same reset for both components after
the app tier is up, so a fresh install *establishes* the resting contract
rather than only declaring it. An install never ends a rollout an operator
deliberately started: the refusal above applies there too, and is reported, not
overridden.

### Honest status, mode-aware (#3409)

`nyxgpt canary status` / `/admin/canary` report each track's health as one
of three honest states rather than a binary healthy/unhealthy that treats
"not installed" as an alarm:

- **Not deployed** -- the cluster is unreachable, the Deployment doesn't
  exist yet (run `nyxgpt ops install --kubernetes`), or it exists at 0
  desired replicas (the canary Deployment's normal idle state before a
  rollout starts). Neutral, not an alarm.
- **Unhealthy** -- the Deployment exists with `>0` desired replicas but not
  all its Pods are ready.
- **Healthy** -- fully ready, and the response includes the image version
  each track is running.

A genuine kubectl failure against a reachable cluster (e.g. an RBAC denial)
is reported as its own distinguishable **error** state, never silently
folded into "not deployed" — and kubectl's own sentence travels in the
state's message. Outside Kubernetes mode (native/terraform),
`status`/`/admin/canary` say so explicitly and name which mode provides
canary, instead of inferring "not applicable" from a failed kubectl call.

**Unhealthy and failed rollouts name the Kubernetes reason (#3831).** "0/1
ready" is a symptom, not a diagnosis: the Deployment's own status never says
*why* a Pod isn't serving. When a track is unhealthy, or a rollout doesn't
finish inside its timeout, `canary.py` reads the Pods behind that
Deployment's own selector and appends what Kubernetes said —
`Unschedulable: 0/1 nodes are available: 1 Insufficient memory`,
`ImagePullBackOff`, `CrashLoopBackOff`, a container's termination reason, or
the readiness condition, whichever applies. That reason reaches the CLI, the
`/admin/canary` error card and the API's `409` detail (which also carries the
failing step's kubectl stderr). A healthy track costs no extra call — the
Pods are only queried when there is something to explain. Evidence:
`.github/workflows/canary-pod-reason-smoke.yml` asserts it on a real kind
cluster, against a Pod the scheduler actually refuses.

### SRE/admin dashboard

The status/start/evaluate/promote/rollback actions are available from the web
UI at **Settings → Canary Operations** (`/admin/canary`), backed by `GET/POST
/api/v1/canary/status`, `/start`, `/evaluate`, `/promote`, and `/rollback` on
the FastAPI backend. The page has an `api`/`web` tab (#3419): `GET` takes
`component` as a query param, the `POST` actions take it as a JSON body field
(`{"component": "web"}`); both default to `api` when omitted.

**`deploy` is not among them (#3991).** The page names
`nyxgpt canary deploy` as text and offers no button for it. The build runs in
whatever process serves the request, and in Kubernetes mode that is the
in-cluster api Pod — no checkout, no Docker daemon, and a version that resolves
to `0.0.0`, so the control could only ever fail with `Failed to build/load
nyxgpt-web:0.0.0`. It was removed rather than repaired, under the standing rule
in CLAUDE.md's Definition of Done: a dashboard cannot act on the substrate it is
itself running on (the same rule that removed the AWS Cloud Infrastructure and
Portability screens, #3804/#3803). `POST /api/v1/canary/deploy` remains for an
api process that *can* build — a native install driving a local cluster.

### Metrics source: the canary track's own Pods (#3829)

`evaluate`, `promote` and `status` read the metrics of the Pods labelled
`track=canary` — not the counters of whichever `nyxgpt-api` process is
serving the dashboard/CLI request. Each track's Pods are listed by that
label (both Deployments' Pods share one `app` label and are told apart by
`track`, see `k8s/deployment-stable.yaml` / `deployment-canary.yaml`), and
each Pod's own Prometheus `/metrics` is read through the API server's Pod
proxy — so no Prometheus server is required and the gate works whether or
not the observability layer is installed. It needs `get` on `pods/proxy`,
granted to the `nyxgpt-api` ServiceAccount in `k8s/rbac.yaml`.

What this changes in practice:

- **A canary that has taken no traffic can never be reported "safe to
  promote".** When the canary track has no ready Pods, no readable
  `/metrics`, or fewer than `min_requests_for_evaluation` requests,
  `evaluate` holds and says which of those it is. Before #3829 it compared
  the *serving* Pod's request count against the threshold, so a canary at
  0/1 replicas with zero Service endpoints was green-lit on a stable Pod's
  hundreds of requests, and the gate could not fail.
- **`/health` and `/metrics` requests are excluded** from the judged
  traffic. The kubelet probes `/health` every 10s and Prometheus (plus this
  gate) reads `/metrics`, so counting them would let an idle canary cross
  `min_requests_for_evaluation` on automated traffic alone within minutes.
- **Auto-rollback triggers on the canary's own regression**, and load or
  errors on the stable track neither provoke nor mask one.
- **`promote` refuses a canary track that has measurably served no
  traffic** — a build no request has reached has not been canaried, however
  healthy its Pods look. On a cluster that is simply idle, `nyxgpt canary
  promote --force` (or `{"force": true}` on `POST /api/v1/canary/promote`,
  or the Canary page's "Promote despite no canary traffic" checkbox)
  proceeds anyway and says so in the result.
- **`--component web` is not gated on traffic**: Next.js Pods export no
  `/metrics`, so web canary traffic is not measurable. `evaluate` says that
  plainly instead of reporting a number belonging to something else, and
  `promote` proceeds on Pod health with the caveat stated in its result.

`nyxgpt canary status` and `/admin/canary` show the canary track's vitals —
the exact input `evaluate` gates on — and, while a rollout is in progress,
the stable track's for contrast. Each carries the Pods it was measured from,
or the reason it could not be measured. (The stable track is only measured
during a rollout: it costs one Pod-proxy read per stable Pod on every status
poll, which is not worth spending with no canary to compare against.)

Executed verification for all of the above runs on a real kind cluster in
`scripts/canary-track-metrics-smoke.sh`
(`.github/workflows/canary-track-metrics-smoke.yml`).

### Canary logging & metrics

Every deploy/start/evaluate/promote/rollback decision is logged from
`src/nyxgpt/canary.py` with structured fields (via the logging module's
`extra={}`, rendered as JSON when `[logging] format = json` -- see
[configuration.md](configuration.md#logging-section)): the deploy attempt
and outcome (`canary: deploying <tag> to <component>-canary only`, `canary:
Deployed <tag> to <component>-canary`), rollout start (`canary: starting/
Started rollout at N%`), evaluation results (`canary: evaluate passed`,
`canary: evaluate holding, insufficient data`, `canary: evaluate detected
regression ...; rolling back`), promotion (`canary: promoting rollout from
N% to M%`, `canary: Promoted <version> to <component>-stable ...`), and
rollback (`canary: rolling back/rolled back from N% (trigger=manual|auto)`
-- `trigger` distinguishes an operator-initiated rollback from `evaluate`'s
automatic one). Every log line also carries a `canary_component` field
(`api`/`web`) in its structured `extra`. Every deploy/start/promote/rollback
action is also recorded as an ops lifecycle event
(`nyxgpt_ops_actions_total{command="canary-<action>",service="<component>"}`,
see [self-healing.md's Self-heal restarts vs. operator
actions](self-healing.md#self-heal-restarts-vs-operator-nyxgpt-ops-actions)).

These are exported as Prometheus metrics (scraped from
[`/api/v1/metrics`](api.md#get-metrics)). The original four are `api`-only
and unlabeled by component (unchanged since before #3419, so existing
dashboards/alerts keep working); the `nyxgpt_canary_component_*` metrics
added alongside them carry a `component` label and are populated for every
component (`api` included), so a single query covers both:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `nyxgpt_canary_rollout_active` | Gauge | — | `api`-only: whether a canary rollout is currently in progress (1) or idle (0) |
| `nyxgpt_canary_weight_percent` | Gauge | — | `api`-only: current canary traffic weight percentage (0-100) |
| `nyxgpt_canary_evaluations_total` | Counter | `result` | `api`-only: metric evaluations, by result (`pass`/`insufficient_data`/`regression`) |
| `nyxgpt_canary_events_total` | Counter | `action`, `result` | `api`-only: lifecycle events (`deploy`/`start`/`promote`/`rollback`), by outcome |
| `nyxgpt_canary_track_version_info` | Gauge | `track`, `version` | `api`-only: 1 for the (track, version) currently observed on that track's Deployment |
| `nyxgpt_canary_component_rollout_active` | Gauge | `component` | Whether a canary rollout is currently in progress (1) or idle (0), by component |
| `nyxgpt_canary_component_weight_percent` | Gauge | `component` | Current canary traffic weight percentage (0-100), by component |
| `nyxgpt_canary_component_evaluations_total` | Counter | `component`, `result` | Metric evaluations, by component and result |
| `nyxgpt_canary_component_events_total` | Counter | `component`, `action`, `result` | Lifecycle events, by component, action, and outcome |
| `nyxgpt_canary_component_track_version_info` | Gauge | `component`, `track`, `version` | 1 for the (component, track, version) currently observed on that component's track Deployment |
| `nyxgpt_canary_auto_rollback_total` | Counter | `component` | Rollouts automatically rolled back by `evaluate()` due to a metrics regression -- distinct from `nyxgpt_canary_events_total{action="rollback"}` /`nyxgpt_canary_component_events_total{action="rollback"}`, which also count operator-initiated rollbacks. Backs the "NyxGPT canary auto-rollback" Grafana alert, see [alerting.md](alerting.md) |

The pre-provisioned Grafana **Canary Rollout** dashboard
(`docker/grafana/dashboards/canary.json`, auto-provisioned like the other
dashboards -- see [docker-compose.md's Monitoring
Dashboards](docker-compose.md#monitoring-dashboards)) shows rollout
active/idle, the live traffic split, evaluation results, lifecycle events, a
per-track version table, and a Loki-backed deploy/start/promote/rollback
timeline for `api`, plus three additional panels driven by the
`nyxgpt_canary_component_*` metrics (rollout active, traffic split, and a
stable/canary version table, each broken out by the `component` label) so
`api` and `web` light up side by side. The Loki saved query behind the
timeline (requires the `logging` Compose profile -- see [Log
Aggregation](docker-compose.md#log-aggregation)):

```logql
{job="nyxgpt"} |= `canary:` |~ `deploying|Deployed|starting|started|promoting|Promoted|rolling back|rolled back|regression`
```

`/admin/canary` links directly to both the Grafana dashboard and Grafana
Explore with this query pre-filled (when the `monitoring`/`logging`
profiles are active).

## Scaling behavior

None of the stable/canary Deployments (`api` or `web`) have an HPA attached
-- autoscaling would fight canary's replica-count-based traffic split (see
[Canary Deployment](#canary-deployment)). `nyxgpt-api-stable` and
`nyxgpt-web-stable` rest at **1 replica** each and are the source of truth
for their own steady-state count: a rollout reads it, borrows on top of it,
and gives the borrowed replicas back (#3833 -- see [The replica pool is
borrowed, not standing](#the-replica-pool-is-borrowed-not-standing-3833)).
`[canary] total_replicas` caps how far a rollout may borrow; it does **not**
set the steady-state count, and raising it does not add standing capacity.
There is no `nyxgpt`-wrapped command for changing the steady-state replica
count yet, so if you need more capacity today, scaling the stable Deployment
is a manual `kubectl` escape hatch pending a wrapper (tracked as follow-up
work), not a first-class operation -- a count set that way survives
rollouts, which is the behaviour the fixed pool used to break. Chat
**sessions** are shared across every `api` replica and
survive Pod restarts: they live in the in-cluster Cassandra
(`[nyxgpt] session_backend = cassandra`, #3786 -- see [Data and LLM
tier](#data-and-llm-tier)). The **vector store** still defaults to an
in-container path (`/root/.nyxGPT/vectorstore`), so it is per-Pod and not
persisted across restarts (`nyxgpt-web` itself is fully stateless -- it has
no server-side storage of its own). If you need a shared/persisted vector
store, add a `PersistentVolumeClaim`, mount it at `/root/.nyxGPT`, and
switch the Deployment's access pattern accordingly (not included here, since
this deployment targets a single-user local workflow).
