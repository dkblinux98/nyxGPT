"""Which substrate a `nyxgpt` run is about, decided once and in one place (#4184).

Every command in this product can be run on a machine that holds more than
one deployment's worth of evidence: a Kubernetes host carries the `kubectl`
that answers for fourteen Pods *and* a `docker-compose.yml` nothing uses *and*
an `install-mode` marker a native install left behind months ago. A check that
picks its subject from whichever of those answers first does not report on the
deployment -- it reports on whichever record happened to be nearest, and that
is the defect class #4184 was filed for:

* `self_heal.component_survey()` inferred the substrate from what the cluster
  answered, so during a `--kubernetes` install -- before a single Pod exists --
  it fell through to `docker compose ps`, logged its exit 125 and named a
  Compose file that instance was never meant to have (#4184 finding 1);
* `ops doctor` printed `Install mode (native api/web): artifact ...` on an
  instance whose api/web are Pods, because the only question it asked was
  "is a native unit registered here", never "is the native install what
  serves" (finding 2);
* `_probe_running_api_runtime` authenticated with `~/.nyxGPT/config.ini`'s
  `[auth] api_key` on a deployment whose key lives in the cluster Secret, got
  HTTP 401 and reported nothing (finding 3);
* `ops session-backend` printed "Restart the API to pick this up" mid-deploy,
  before any api existed to restart (finding 4).

Four surfaces, one rule broken: **a probe or record must not answer for a
substrate the run was not about.** So the substrate is decided here, by one
function, from evidence the caller gathers, in a fixed precedence:

1. **What the run DECLARED** (`declared_as`, or the `NYXGPT_SUBSTRATE`
   environment variable). An `ops install --kubernetes` knows its substrate
   before it has created anything, and `cloud deploy` knows it for every
   `nyxgpt` invocation in its provisioning script. Inference cannot know it:
   at that moment nothing answers, which is indistinguishable from a native
   box. This arm is what makes the surveys run as *side effects* of other
   steps -- the session-backend write, the install, the deploy -- route the
   same way as `doctor` and `self-heal`.
2. **Being in the deployment** -- this process is a Pod.
3. **What the cluster answers**: the core tier (api/web/Cassandra/Ollama) is
   running as Pods. This is #4137's rule and it stays: `kubectl` on a k3s host
   reaches the cluster perfectly well, and the host is neither in-cluster nor
   Compose.
4. **What holds this host's ports**: a Kubernetes install is recorded here and
   the access bridge owns :8000/:3000. The cluster is serving this machine even
   on a pass where the Pod read itself failed.
5. **What is deployed on the host**: Terraform containers, then a Compose core
   tier, then registered native services -- the same precedence
   `ops.infra_status` has always reported as `mode`.
6. **Nothing answered** (`SUBSTRATE_UNKNOWN`). A real state on a half-built
   box, and deliberately not a synonym for "native": guidance aimed at a
   native install that is not there is finding 4.

Deliberately NOT cached across calls. "Decide once per run" is satisfied by
each command gathering its evidence once and passing it here; a process-level
cache would also freeze the answer inside the long-lived api server, which
serves `infra_status` for hours across installs and teardowns.

This module is a leaf on purpose -- it imports nothing from `ops`,
`self_heal` or `cloud_*`, so both sides of the `ops` -> `self_heal` import
edge can reach the same decision function rather than keeping a copy each
(CLAUDE.md, one source per decision).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

__all__ = [
    "BY_CLUSTER",
    "BY_DECLARATION",
    "BY_ENVIRONMENT",
    "BY_HOST",
    "BY_HOST_PORTS",
    "BY_IN_CLUSTER",
    "BY_NOTHING",
    "KNOWN_SUBSTRATES",
    "SUBSTRATE_COMPOSE",
    "SUBSTRATE_ENV_VAR",
    "SUBSTRATE_KUBERNETES",
    "SUBSTRATE_NATIVE",
    "SUBSTRATE_TERRAFORM",
    "SUBSTRATE_UNKNOWN",
    "Evidence",
    "SubstrateDecision",
    "clear_declaration",
    "decide",
    "declared",
    "declared_as",
]

#: The environment variable a parent process uses to declare the substrate to
#: every `nyxgpt` it runs. This is the mechanism `cloud deploy`'s provisioning
#: script uses: it exports the choice once, so the session-backend write, the
#: install and `self-heal enable` all route the same way -- rather than each
#: re-inferring it from a box that is still being built.
SUBSTRATE_ENV_VAR = "NYXGPT_SUBSTRATE"

SUBSTRATE_KUBERNETES = "kubernetes"
SUBSTRATE_COMPOSE = "compose"
SUBSTRATE_TERRAFORM = "terraform"
SUBSTRATE_NATIVE = "native"

#: Nothing on this machine answers for a deployment. A state in its own right
#: -- a box mid-install, or one that has been torn down -- and never collapsed
#: into `native`, because the actions a native install affords (restart the
#: api, read its config) are exactly what is not available here.
SUBSTRATE_UNKNOWN = ""

KNOWN_SUBSTRATES = frozenset(
    {SUBSTRATE_KUBERNETES, SUBSTRATE_COMPOSE, SUBSTRATE_TERRAFORM, SUBSTRATE_NATIVE}
)

# How the decision was reached. Carried on the decision and rendered by the
# surfaces, because "Kubernetes" alone is not a reviewable answer: an operator
# reading a scope statement needs to know whether it came from the cluster or
# from a flag someone passed.
BY_DECLARATION = "declared by this run"
BY_ENVIRONMENT = f"declared by the parent process ({SUBSTRATE_ENV_VAR})"
BY_IN_CLUSTER = "this process is running in the cluster"
BY_CLUSTER = "the cluster answers for the core tier"
BY_HOST_PORTS = "the Kubernetes access bridge holds this host's ports"
BY_HOST = "the host survey"
BY_NOTHING = "nothing on this machine answers for a deployment"


@dataclass(frozen=True)
class SubstrateDecision:
    """The substrate this run is about, and how that was decided."""

    substrate: str
    source: str

    @property
    def kubernetes(self) -> bool:
        """True when the deployment in question runs as Kubernetes Pods."""
        return self.substrate == SUBSTRATE_KUBERNETES

    @property
    def native(self) -> bool:
        """True when the deployment in question is this host's native install."""
        return self.substrate == SUBSTRATE_NATIVE

    @property
    def known(self) -> bool:
        """True when some substrate answered; False on a box that holds no deployment."""
        return self.substrate != SUBSTRATE_UNKNOWN

    @property
    def declared(self) -> bool:
        """True when the run was TOLD its substrate rather than inferring it.

        The surfaces use this to phrase a scope statement honestly: a declared
        Kubernetes substrate with no Pods yet is an install in progress, which
        is a different sentence from a cluster that is serving.
        """
        return self.source in (BY_DECLARATION, BY_ENVIRONMENT)

    @property
    def detail(self) -> str:
        """`"kubernetes (the cluster answers for the core tier)"` -- for an operator-facing line."""
        if not self.known:
            return self.source
        return f"{self.substrate} ({self.source})"


@dataclass(frozen=True)
class Evidence:
    """What a caller managed to observe about this machine.

    Every field defaults to "no evidence", which is what lets a caller pass
    only the half it holds: `self_heal.component_survey` knows what the cluster
    answered and nothing about Terraform containers, while `ops.infra_status`
    holds the whole survey. Both reach the same `decide()` rather than keeping
    a copy of the precedence (CLAUDE.md, one source per decision) -- a caller
    that cannot see an arm simply cannot select it.
    """

    #: This process is running inside the cluster it is describing (#3988).
    in_cluster: bool = False
    #: The cluster runs the core tier -- api/web/Cassandra/Ollama (#4137).
    cluster_core_pods: bool = False
    #: A Kubernetes install is recorded on this machine.
    kubernetes_recorded: bool = False
    #: The Kubernetes access bridge holds this host's :8000/:3000.
    kubernetes_owns_host_ports: bool = False
    #: Terraform-managed core containers are running here.
    terraform_deployed: bool = False
    #: A core component is Compose-managed here.
    compose_core: bool = False
    #: A native api/web service is registered with this host's service manager.
    native_registered: bool = False


@contextmanager
def declared_as(substrate: str, *, export: bool = True) -> Iterator[SubstrateDecision]:
    """Declare, for the duration of this block, that the work is about `substrate`.

    Called by the entrypoints that KNOW -- `ops install --kubernetes` and its
    siblings -- before they have created anything for inference to find. With
    `export`, the choice also reaches every `nyxgpt` subprocess started inside
    the block through `NYXGPT_SUBSTRATE`.

    **Scoped, never sticky, and that is a correctness requirement rather than
    tidiness.** The same functions run inside the long-lived api server: the
    SRE dashboard calls `install_kubernetes_local` in the process that also
    serves `infra_status` and runs the self-heal watchdog, so a declaration
    that outlived the install would route every later survey in that process
    to a cluster for as long as the api stayed up -- the #4184 defect rebuilt
    out of its own fix. Restoring the previous value (rather than clearing)
    keeps a nested or outer declaration intact.

    An unknown substrate is refused rather than ignored: a typo that silently
    became "no declaration" would reintroduce exactly the inference this exists
    to override.
    """
    if substrate not in KNOWN_SUBSTRATES:
        raise ValueError(
            f"unknown substrate {substrate!r} -- expected one of {sorted(KNOWN_SUBSTRATES)}"
        )
    global _DECLARED
    previous = _DECLARED
    previous_env = os.environ.get(SUBSTRATE_ENV_VAR)
    _DECLARED = substrate
    if export:
        os.environ[SUBSTRATE_ENV_VAR] = substrate
    try:
        yield SubstrateDecision(substrate=substrate, source=BY_DECLARATION)
    finally:
        _DECLARED = previous
        if export:
            if previous_env is None:
                os.environ.pop(SUBSTRATE_ENV_VAR, None)
            else:
                os.environ[SUBSTRATE_ENV_VAR] = previous_env


def clear_declaration(*, export: bool = True) -> None:
    """Drop any declaration in force: the process-local one and the variable.

    For a test that must be sure it is exercising INFERENCE, and for a session
    that inherited `NYXGPT_SUBSTRATE` from a shell where an operator exported
    it by hand.
    """
    global _DECLARED
    _DECLARED = SUBSTRATE_UNKNOWN
    if export:
        os.environ.pop(SUBSTRATE_ENV_VAR, None)


#: This run's declaration, if any. Process-local; the environment variable is
#: the inter-process form of the same fact.
_DECLARED: str = SUBSTRATE_UNKNOWN


def declared() -> SubstrateDecision | None:
    """This run's declared substrate, or None if nothing declared one.

    The one arm of the decision that needs no evidence at all, which is why it
    is also usable from modules that cannot survey anything: reading an
    environment variable costs nothing and cannot fail.

    An unrecognised `NYXGPT_SUBSTRATE` value is ignored rather than honoured.
    The variable is read on every command, and a typo that routed every check
    to a substrate that does not exist would be far worse than falling back to
    inference.
    """
    if _DECLARED in KNOWN_SUBSTRATES:
        return SubstrateDecision(substrate=_DECLARED, source=BY_DECLARATION)
    env = os.environ.get(SUBSTRATE_ENV_VAR, "").strip().lower()
    if env in KNOWN_SUBSTRATES:
        return SubstrateDecision(substrate=env, source=BY_ENVIRONMENT)
    return None


def decide(evidence: Evidence | None = None) -> SubstrateDecision:
    """The substrate this run is about, by the precedence in this module's docstring.

    Pure: every input is in `evidence` or in the declaration, so the same
    machine state always yields the same answer and a test needs no cluster.
    """
    decision = declared()
    if decision is not None:
        return decision
    ev = evidence or Evidence()
    if ev.in_cluster:
        return SubstrateDecision(SUBSTRATE_KUBERNETES, BY_IN_CLUSTER)
    if ev.cluster_core_pods:
        return SubstrateDecision(SUBSTRATE_KUBERNETES, BY_CLUSTER)
    if ev.kubernetes_recorded and ev.kubernetes_owns_host_ports:
        # The cluster is serving this host's ports even though the Pod read
        # came back empty -- a throttled API server, a kubeconfig this session
        # cannot read (#4137's step 8). Reporting "native" here would hand the
        # operator a host reading for a deployment that is answering on :8000.
        return SubstrateDecision(SUBSTRATE_KUBERNETES, BY_HOST_PORTS)
    if ev.terraform_deployed:
        return SubstrateDecision(SUBSTRATE_TERRAFORM, BY_HOST)
    if ev.compose_core:
        return SubstrateDecision(SUBSTRATE_COMPOSE, BY_HOST)
    if ev.native_registered:
        return SubstrateDecision(SUBSTRATE_NATIVE, BY_HOST)
    return SubstrateDecision(SUBSTRATE_UNKNOWN, BY_NOTHING)
