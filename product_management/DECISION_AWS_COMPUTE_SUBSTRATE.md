# Decision Record: AWS Compute Substrate — EC2 Single-Box vs EKS (P6-7)

**Issue:** #3506 · **Status:** Approved by owner, 2026-08-04
**Author:** developer-agent · **Date:** 2026-08-03
**Blocks:** P6-8 (Terraform AWS modules), P6-11 (`nyxgpt cloud deploy`), P6-12 (target-OS provisioning)

## Problem

Phase 6 needs a cloud compute target so the Terraform AWS modules (P6-8),
`nyxgpt cloud deploy` (P6-11), and OS provisioning (P6-12) have something
concrete to build against. [`PHASE_6_PLAN.md`](PHASE_6_PLAN.md#p6-7--feat-decision---aws-compute-substrate-ec2-single-box-vs-eks)
frames the choice as EC2 single-box vs. EKS and asks for a recommendation
weighing cost, ops burden, fit with the existing canary/k8s substrate
(#3409/#3419), and the private-access mechanism decided in P6-4 (#3503).

## Constraints from existing decisions

- **Native-first, one container, restated for the cloud** (2026-07-15,
  [`PHASE_6_PLAN.md`](PHASE_6_PLAN.md#standing-owner-decisions-unchanged-restated)):
  the standing local-deployment model is everything native on the host plus
  Cassandra as the sole container. Nothing in Phase 6 overturns this for the
  cloud target — the substrate decision should extend, not abandon, that
  shape.
- **Private-to-the-workstation access, SSH tunnel to a loopback bind**
  ([`DECISION_PRIVATE_ACCESS_MECHANISM.md`](DECISION_PRIVATE_ACCESS_MECHANISM.md),
  P6-4/#3503): the API, web UI, and every observability endpoint bind to
  `127.0.0.1` on the instance; the security group opens only TCP `22`,
  scoped to the owner's IP. The compute substrate only needs to expose SSH —
  it does not need a load balancer, ingress controller, or any
  publicly-routable service endpoint. Any substrate feature built for
  handling public/external traffic (EKS's typical `Service:
  LoadBalancer`/ALB Ingress story) is unnecessary here.
- **Existing canary/k8s substrate is local-cluster shaped**
  ([`docs/kubernetes.md`](../docs/kubernetes.md)): `nyxgpt ops install
  --kubernetes --local` targets kind/minikube/k3s/Docker-Desktop's cluster —
  a single-node (or small) local cluster, not a managed multi-node cloud
  control plane. The stable/canary Deployment pairs for `api` and `web`
  (#3409/#3419) are plain Kubernetes manifests (`k8s/*.yaml`) with no
  dependency on any AWS-managed control-plane feature (no IRSA, no ALB
  Ingress, no EKS-specific CNI assumption). Ollama is explicitly **not**
  containerized/canaried (storage-concurrency infeasibility, documented in
  `docs/kubernetes.md`'s [Ollama canary
  feasibility](../docs/kubernetes.md#ollama-canary-feasibility) section) and
  Cassandra canary is explicitly out of scope — so whatever cloud substrate
  is chosen only needs to run `api`/`web` Pods plus a host-level Ollama and
  Cassandra, exactly like today's local topology.
- **Repo-less portability** (CLAUDE.md, 2026-08-01): the substrate must be
  provisionable and operable from published artifacts only — no repo
  checkout on the client or the target instance. A substrate that demands
  heavyweight day-2 tooling (Helm charts, a GitOps controller, cluster
  add-ons) to reach parity with what `nyxgpt ops install --kubernetes
  --local` already does today works against this.
- **Single owner, single deployment**: nyxGPT's [`VISION.md`](VISION.md) is a
  local-first, single-user tool. Phase 6's cloud target is "the owner's one
  private deployment reachable over a locked path," not a multi-tenant
  service needing horizontal scale-out, zero-downtime rolling infra
  upgrades, or a fleet of nodes. There is exactly one workload to place.

## Options considered

### 1. EC2 single-box (plain instance, Docker/Compose or a local-style k8s on top)

A single EC2 instance runs the stack the same way `nyxgpt ops install`
already does locally: native processes for `api`/`web` (or the existing
Compose/Terraform-local Docker path) plus the Cassandra container, all
bound to `127.0.0.1`, reachable only via the P6-4 SSH tunnel.

- **Pros:** Directly reuses everything already built — `nyxgpt ops install`,
  the Compose profiles, and (if canary is wanted) the *existing*
  `k8s/*.yaml` manifests apply unmodified to a single-node cluster
  (k3s/kind) installed on that one box, exactly mirroring
  `--kubernetes --local` but with the "local" machine being the EC2
  instance instead of the workstation. No new manifests, no new deployment
  code path — `nyxgpt cloud deploy` becomes "provision one instance,
  bootstrap it, run the same install logic remotely." Cost is one
  instance's hourly rate (e.g. an `m5.large` at ~$0.096/hr ≈ $70/mo
  on-demand, less with a Savings Plan) — no control-plane charge, no
  multi-node minimum. Ops burden is a single host to patch, monitor, and
  self-heal — the existing self-heal watchdog and `nyxgpt ops
  doctor`/`status` machinery already assume "one machine," so it applies
  unchanged. Matches the native-first/one-container standing decision and
  the P6-4 access model exactly (nothing to expose but SSH).
- **Cons:** No built-in high availability — an instance failure takes the
  whole deployment down until self-heal/replacement completes. Scaling is
  vertical (a bigger instance type) rather than horizontal. If canary is
  desired, a single-node k8s distro (k3s) must be provisioned and kept
  patched as an extra layer on top of the box — a small addition, not a new
  subsystem, since it reuses the existing manifests and `ops.py`
  kind/minikube-loading pattern already handles the "load image into local
  cluster" step generically.

### 2. Amazon EKS (managed Kubernetes control plane + node group)

A managed EKS cluster (control plane + one or more worker nodes, typically
via a managed node group or Fargate profile) hosts the `api`/`web`
stable/canary Deployments.

- **Pros:** Production-grade multi-node orchestration, managed control-plane
  upgrades, native fit for horizontal scale-out and zero-downtime node
  replacement, and the closest cloud analogue to "real" Kubernetes if the
  project ever needs multi-node capacity.
- **Cons:** A standing **$0.10/hr (~$73/mo) control-plane charge** exists
  purely for cluster membership, before any compute — for a single-workload
  deployment this is pure overhead with no corresponding benefit (there is
  nothing to orchestrate across nodes; `api`/`web` are the only
  containerized components and Ollama/Cassandra stay off-cluster). A
  production-shaped node group is normally ≥2 nodes for HA, meaning at
  least 2x the compute spend of option 1 for a workload that's a single
  owner's private instance, not a fault-tolerant multi-tenant service.
  EKS's idiomatic networking story (ALB/NLB Ingress, IRSA for pod-level AWS
  API access, VPC CNI ENI limits) is built around *publicly or
  internally routable* Services — directly at odds with the P6-4 decision
  that nothing but SSH is ever exposed; adopting it would mean either
  fighting the platform's defaults (running everything ClusterIP-only
  through a bastion, as option 1 does natively) or paying for
  infrastructure (a load balancer) the access model explicitly forbids
  using for its intended purpose. New ongoing ops burden: control-plane
  version deprecation cycles (~14 months per EKS version) force periodic
  upgrades, add-on version matrices (VPC CNI, CoreDNS, kube-proxy) need
  tracking, and IAM/OIDC provider setup for IRSA is new surface with no
  local-deployment precedent to reuse — none of which the current
  `nyxgpt ops`/self-heal code understands, so it would all be new,
  cluster-specific automation on top of what already exists for the local
  k8s path. This is disproportionate ops burden for one workload with no
  multi-node requirement.

## Decision

**EC2 single-box (option 1), with the existing local Kubernetes manifests
(`k8s/*.yaml`) optionally layered on top via a single-node cluster (k3s) for
canary, rather than a managed EKS control plane.**

This wins on every constraint above:

- **Cost:** one instance vs. a mandatory ~$73/mo control-plane charge plus a
  multi-node group — EKS is strictly more expensive for a workload that is,
  by design, a single deployment.
- **Ops burden:** reuses `nyxgpt ops install`, self-heal, and
  `doctor`/`status` unchanged (all already "one machine" shaped) instead of
  introducing a new cluster-lifecycle subsystem (control-plane upgrades,
  add-on version tracking, IRSA/OIDC) that nothing in the codebase
  understands today.
- **Fit with the canary/k8s substrate:** the *existing* `k8s/*.yaml`
  stable/canary manifests for `api`/`web` (#3409/#3419) are already
  cluster-flavor-agnostic (no ALB Ingress, no IRSA, no cloud-specific CNI
  dependency) — they apply to a single-node k3s cluster on the EC2 box with
  zero changes, exactly reproducing `nyxgpt ops install --kubernetes
  --local`'s existing code path remotely. EKS would require *new*,
  cloud-specific manifests/ops code to be idiomatic, duplicating work
  that's already done.
- **Fit with the P6-4 private-access mechanism:** the SSH-tunnel-to-loopback
  model needs the substrate to expose nothing but port 22. A single EC2
  instance satisfies this trivially. EKS's idiomatic access patterns
  (managed load balancers) actively work against this model; using EKS
  while still honoring P6-4 would mean paying for and provisioning
  networking infrastructure that is then deliberately left unused for its
  designed purpose.

EKS is not rejected as generally inferior technology — it is the right tool
for a multi-node, publicly-routed, horizontally-scaled service. nyxGPT's
Phase 6 cloud target is none of those things: one owner, one private
deployment, reachable over one locked SSH path. EC2 single-box is the
substrate that matches the workload the project actually has, at a fraction
of the cost and with an operational model the codebase already implements.

## What this means for downstream issues

- **P6-8 (Terraform AWS modules):** provisions a VPC, subnets, and a single
  EC2 instance (plus its security group, per P6-4's owner-IP-scoped SSH-only
  rule) — no EKS module, no managed node group, no ALB/NLB resources.
- **P6-11 (`nyxgpt cloud deploy`):** the deploy step bootstraps the instance
  the same way `nyxgpt ops install` does locally (native processes +
  Cassandra container), with an option to additionally install a
  single-node k3s cluster and apply the existing `k8s/*.yaml` manifests
  when canary rollout is wanted on the cloud deployment — no new deployment
  code path, just the existing `--kubernetes` install mode pointed at the
  remote box over the P6-4 SSH path instead of a local cluster.
- **P6-12 (target-OS provisioning):** provisions the single instance's OS
  (Linux AMI or EC2 Mac) — no cluster-node AMI/bootstrap concerns.
- If a genuine multi-node/HA requirement emerges later (outside this
  project's current single-owner shape), that would be a new decision
  record re-opening this question — this record does not attempt to
  future-proof against a requirement that does not exist today.

## Owner review

**Approved by the owner on issue #3506, 2026-08-04**, as written: EC2
single-box, with the existing `k8s/*.yaml` manifests optionally layered on
a single-node k3s cluster for canary — no EKS. P6-8, P6-11, and P6-12 may
now proceed against this substrate, together with the P6-4 private-access
mechanism approved the same day (#3503: SSH tunnel + owner-IP-scoped
port-22 security group).

---

## Revision: instance sizing raised to `m5.xlarge` (#3992, 2026-08-22)

**Status:** Amends the sizing example above. The substrate decision itself —
EC2 single-box, no EKS — is unchanged; only how big that one box is.

### What changed

The shipped default instance type is now **`m5.xlarge`** (4 vCPU / 16 GiB),
not `m5.large` (2 vCPU / 8 GiB). The `m5.large` figure in option 1's Pros
above was an illustrative cost example written before anything ran on the
substrate; it became the Terraform default and the CLI fallback, and no
measurement ever confirmed it fit.

### Evidence

Owner acceptance of the cloud path on 2026-08-22 (`nyxgpt cloud deploy --dev`,
EC2 Amazon Linux 2023, all observability profiles on) found the `m5.large`
instance freezing interactively under ordinary use — SSH banner-exchange
timeouts, a dead tunnel, an unresponsive web UI — while EC2 status checks
stayed green. Three distinct triggers reproduced it: a web route compile, a
plain page navigation, and a RAG document ingest. Each recovery needed a
reboot, itself ~10 minutes because the wedged OS could not service the ACPI
request.

The memory census taken on the live instance:

| Consumer | Footprint |
|---|---|
| Cassandra JVM (untuned defaults) | ~4.3 GiB (57% of the box) |
| Next web tier (dev server) | ~1.1 GiB |
| Observability stack (Grafana, Loki, Prometheus, GlitchTip ×4, Jaeger, otel, promtail) | the bulk of the remainder |
| Ollama | small resident, spikes under embedding inference |

7.2 of 7.6 GiB was consumed minutes after boot, with no swap provisioned. Any
real allocation then drove the box into reclaim thrash: interactively dead
without the OOM killer ever firing (the previous-boot kernel journal recorded
zero OOM events), which is exactly why the failure was invisible to EC2 status
checks and left no kill trail to diagnose. An emergency 4 GiB swapfile added
mid-session immediately absorbed 1.7 GiB and converted the next spike (a
document ingest at load 17 on 2 vCPUs) from a freeze into a slow-but-alive
box — the ingest still failed on the embedding call's fixed timeout, CPU-
starved on 2 vCPUs. Both halves of the shortfall are therefore real: memory
and cores.

### Decision

Ship `m5.xlarge` as the default. The owner resized the live deployment to it
(`nyxgpt cloud infra apply --instance-type m5.xlarge`) as the operational fix
before filing #3992, and chose sizing as the remedy rather than the
alternatives — Cassandra heap tuning for small instances, swap provisioning in
the bootstrap, and embedding-timeout retry are all deliberately **not** taken
here (owner decision, 2026-08-22).

### Cost

Roughly **$140/month** on-demand (~$0.192/hr) versus ~$70/month for
`m5.large`, plus EBS and the Elastic IP as before. This doubles the compute
line and is still well under option 2's floor: EKS's ~$73/mo control-plane
charge is levied *before* any compute, and its idiomatic ≥2-node group would
sit on top of that. Nothing in the EC2-vs-EKS comparison turns over.

### Existing deployments are not resized

`nyxgpt cloud infra plan`/`apply` persists the resolved settings to
`~/.nyxGPT/cloud/infra.json`, and `resolve_settings` prefers a remembered
`instance_type` over the built-in default. An already-provisioned substrate
therefore keeps its size across a nyxGPT upgrade; taking the new default on an
existing instance is an explicit `nyxgpt cloud infra apply --instance-type
m5.xlarge`, which stops, resizes and restarts the instance (root volume and
Elastic IP preserved). Resizing someone's running deployment as a side effect
of upgrading would be an unannounced outage, so the default only governs
substrates that do not exist yet.
