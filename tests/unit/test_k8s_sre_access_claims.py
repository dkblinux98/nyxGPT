"""What the product says about reaching the SRE tier, and the BYO evidence (#3986).

Two facts from the last round of #3986 live here, because both of them are the
kind that a later edit restores by accident.

**1. The falsified claim, in all three of its homes.** Before the SRE-tier
publish, every surface that mentioned reaching Grafana/Prometheus/Jaeger/
GlitchTip in Kubernetes mode said the same thing: the Services are ClusterIP,
so a `nyxgpt ops port-forward` is the only way in. The publish made that false
on a cluster nyxGPT provisioned -- the install maps those four ports on the
host and verifies them -- and the review found the sentence still in
`docs/ops.md`. It was in two more places: the admin dashboard's own
observability card (the Definition-of-Done surface, telling an operator to open
a terminal they do not need) and `docs/ui.md`'s description of that card. One
fact written in three files is the #3811 shape, so the guard checks all three
rather than the one that was reported.

The phrases are asserted ABSENT, and the replacement asserted present: a
deletion that left the section saying nothing would pass a
"does-it-mention-publishing" test, and say nothing to the operator.

Not in scope here, and deliberately: `docs/cloud.md` and `cloud_deploy.py` say
the same thing about the **AWS k3s** target, where it is still true (#3503 --
the only open port on that instance is 22, which is why that deploy installs a
supervised access bridge instead). The guard names files, not a repo-wide grep,
for exactly that reason.

**2. The bring-your-own-cluster evidence exists and is on the right topology.**
#3986's AC4 is that on a cluster whose host ports are not nyxGPT's to map, the
install establishes the access path ITSELF. The owner could not test it --
their re-test says "BYO -- inspection only -- I did not stand up a second,
non-nyxGPT cluster" -- and inspection is what #3775 says is not evidence.

The job that proves it cannot share a cluster with the published-NodePort job:
a kind node created from nyxGPT's rendered config holds 3001/8080/9090/16686
for its whole life (extraPortMappings are fixed at creation), so a forward can
never bind them there. So the guard is about topology, not just existence: the
BYO job must create its cluster with no `--config`, under a name that is not
the reserved `nyxgpt-local`, and must not be the job that renders nyxGPT's own
kind config.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nyxgpt import ops

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

OPS_DOC = REPO_ROOT / "docs" / "ops.md"
UI_DOC = REPO_ROOT / "docs" / "ui.md"
INFRA_PAGE = REPO_ROOT / "web" / "src" / "app" / "admin" / "infrastructure" / "page.tsx"
OPS_MODULE = REPO_ROOT / "src" / "nyxgpt" / "ops.py"
OBSERVABILITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "k8s-observability-smoke.yml"

# Every spelling the falsified fact had in the three files it lived in. Each one
# is a quote, not a paraphrase -- a guard against prose has to name the prose.
FALSIFIED_CLAIMS = (
    "the only way to reach the observability UIs",
    "whose Services stay ClusterIP",
    "The observability Services are ClusterIP-only",
    "observability Services are ClusterIP-only",
)

CLAIM_FILES = (OPS_DOC, UI_DOC, INFRA_PAGE)


@pytest.mark.parametrize("path", CLAIM_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_surface_still_says_a_forward_is_the_only_way_in(path: Path) -> None:
    """The sentence the SRE-tier publish falsified, in each file that carried it."""
    text = path.read_text(encoding="utf-8")
    for claim in FALSIFIED_CLAIMS:
        assert claim not in text, (
            f"{path.relative_to(REPO_ROOT)} still claims {claim!r}. The install "
            "publishes Grafana 3001, Prometheus 9090, Jaeger 16686 and GlitchTip "
            "8080 on the host where nyxGPT provisioned the cluster (#3986), so a "
            "forward is the bring-your-own answer, not the only one."
        )


def test_the_ops_doc_says_what_is_true_instead() -> None:
    """Deleting the claim is not correcting it.

    The section's job is to tell an operator which command they need; a
    paragraph that merely stopped being wrong would pass the test above while
    leaving them to find out by trying.
    """
    text = OPS_DOC.read_text(encoding="utf-8")
    section = text.split("## `nyxgpt ops port-forward`", 1)[1].split("\n## ", 1)[0]
    # Markdown wraps at ~78 columns, so an asserted phrase can straddle a
    # newline. Compare against the prose, not against the line breaks.
    section = " ".join(section.replace("*", "").split())

    # The published truth, and the ports it is published on.
    for port in ("3001", "9090", "16686", "8080"):
        assert port in section, f"the port-forward section never names the published port {port}"
    assert "bring-your-own" in section

    # The Minor the huddle folded into this edit: the comma-separated form and
    # what the command does with a target the cluster already publishes.
    assert "comma-separated" in section
    assert "dropped from the plan" in section
    assert "republished" in section


def test_the_dashboard_card_is_handed_both_wrapped_commands() -> None:
    """The card cannot name a path the api never sends it.

    Both strings come from `ops`, and both must stay `nyxgpt` commands: the
    card is the one surface an operator reads without a terminal, so a raw
    `kubectl` here would be an Operational Command Wrapping violation on the
    screen the requirement exists for.
    """
    page = INFRA_PAGE.read_text(encoding="utf-8")
    assert "observability.port_forward_command" in page
    assert "observability.publish_command" in page


def test_the_card_renders_no_raw_command_of_its_own() -> None:
    """The cause is named; the command that causes it is not.

    The repair pointer's sentence used to read "if a `kubectl apply` has
    stripped the published ports" -- a raw command string rendering into the
    card, on the one surface Operational Command Wrapping exists for, and
    straight through the two `/kubectl/` negative assertions the web suite
    holds (`infrastructure.test.tsx`). Rewording the cause keeps the guard at
    full strength instead of weakening it to admit the sentence.

    Scoped to the JSX that renders, which is why the `kubectl exec` in the
    `#3990` explanatory comment above it is not a hit.
    """
    page = INFRA_PAGE.read_text(encoding="utf-8")
    rendered = [
        line
        for line in page.splitlines()
        if "kubectl" in line and not line.lstrip().startswith(("*", "/*", "//"))
    ]
    for line in rendered:
        assert "<code>" not in line, (
            "the infrastructure card renders a raw command string "
            f"({line.strip()!r}) -- name the cause, not the command (#3986)"
        )


def test_no_reporting_command_asserts_a_forward_unconditionally() -> None:
    """The same claim class in the product's own output (#4135).

    #4126 swept the docs and the dashboard card; two `print`s in `ops.py` were
    not swept with them, and they are the ones the operator reads on a cluster
    nyxGPT provisioned -- `ops status`'s observability header and `nyxgpt up
    --kubernetes`'s closing line, both naming a forward for UIs the install had
    just published on the host and probed.

    Quoted, not paraphrased, for the same reason the guard above is: the
    defect is a sentence. Both are now printed from
    `_k8s_observability_host_access`, which asks the live Service, so a
    regression here is a re-introduced literal.
    """
    text = OPS_MODULE.read_text(encoding="utf-8")
    for claim in (
        "Kubernetes observability (in-cluster -- reach the UIs with",
        "`nyxgpt ops port-forward --target observability` publishes all four",
    ):
        assert claim not in text, (
            f"src/nyxgpt/ops.py prints {claim!r} unconditionally -- ask "
            "`_k8s_observability_host_access` instead, which keys the answer on what the "
            "live Service carries (#4135)"
        )
    assert "_k8s_observability_host_access(" in text


def _workflow_jobs() -> dict:
    return yaml.safe_load(OBSERVABILITY_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_a_bring_your_own_cluster_job_exists() -> None:
    """AC4's executed evidence (#3775) is a job, not a note in the PR body."""
    jobs = _workflow_jobs()
    assert "k8s-observability-byo-smoke" in jobs, (
        "no bring-your-own-cluster job -- #3986's AC4 (the install establishes "
        "the access path where host ports are not nyxGPT's to map) is the one "
        "criterion the owner could not test, and inspection is not evidence"
    )


def test_the_byo_job_creates_a_cluster_nyxgpt_did_not_provision() -> None:
    """The topology is the whole point of the job.

    From nyxGPT's own rendered config the node holds the four SRE host ports
    for its lifetime, and `kubectl port-forward` can never bind them -- so a
    `--config` here, or the reserved cluster name, would quietly turn this back
    into the published-NodePort job and prove nothing about the forward.
    """
    job = _workflow_jobs()["k8s-observability-byo-smoke"]
    bodies = "\n".join(step.get("run", "") for step in job["steps"])

    create = [line for line in bodies.splitlines() if "kind create cluster" in line]
    assert create, "the BYO job never creates a cluster"
    for line in create:
        assert "--config" not in line, (
            "the BYO job creates its cluster from a kind config -- a node with "
            "nyxGPT's extraPortMappings holds the SRE host ports, so the "
            "managed forward cannot bind and the job proves nothing (#3986)"
        )

    assert job["env"]["BYO_CLUSTER"] != ops.KIND_CLUSTER_NAME, (
        f"the BYO cluster is named {ops.KIND_CLUSTER_NAME!r}, the name nyxGPT "
        "reserves for clusters it created -- `ops down --kubernetes` would "
        "delete it, and nothing would be bring-your-own about it"
    )


def test_the_byo_job_runs_no_forward_of_its_own() -> None:
    """A job that opens its own tunnel cannot see an install that opens none.

    The same blind spot `test_k8s_local_smoke_port_forward.py` guards in the
    local smoke: the only `kubectl port-forward` allowed anywhere near this
    evidence is the one `ops` starts and supervises.
    """
    job = _workflow_jobs()["k8s-observability-byo-smoke"]
    for step in job["steps"]:
        body = step.get("run", "")
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "::error::" in stripped:
                continue
            # `pgrep -af "kubectl.*port-forward"` is how the job ASSERTS on the
            # supervised child; running one is what it must not do.
            if "kubectl" in stripped and "port-forward" in stripped:
                assert "pgrep" in stripped, (
                    "the BYO job runs a raw kubectl port-forward: "
                    f"{stripped!r} -- the access path under test is the one the "
                    "deploy establishes by itself (#3986 AC4)"
                )


def test_the_byo_job_asserts_the_precondition_from_the_products_own_table() -> None:
    """ "These ports are not mapped" has to be asked, or the job can pass vacuously.

    And asked of `K8S_OBSERVABILITY_HOST_PORT_MAPPINGS`, so a change to the
    published ports cannot drift away from the precondition that makes this
    topology different from the other job's.
    """
    job = _workflow_jobs()["k8s-observability-byo-smoke"]
    bodies = "\n".join(step.get("run", "") for step in job["steps"])
    assert "K8S_OBSERVABILITY_HOST_PORT_MAPPINGS" in bodies
    # And the forward it then proves is the managed one, ended by the wrapped
    # command -- the release half of "ops down releases what the install
    # established".
    assert "nyxgpt ops port-forward --stop" in bodies
    assert ops.PORT_FORWARD_STATUS_RUNNING_SENTINEL in bodies
