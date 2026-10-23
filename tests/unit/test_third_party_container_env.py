"""Every env var set on a third-party container has a reader in that image (#4212).

#4212's defect: both GlitchTip containers carried `CELERY_WORKER_AUTOSCALE`
and `CELERY_WORKER_MAX_TASKS_PER_CHILD` on both substrates, and the pinned
`glitchtip/glitchtip:6.2.0` image has ZERO readers for either -- no `celery`
binary, no importable `celery` module, and no match for the names anywhere
under `/code`, `django_vtasks` or `/usr/local/bin`.

Dead configuration is not inert, it is *misleading*. `CELERY_WORKER_AUTOSCALE`
against the worker's memory limit was one of three candidate causes named
while diagnosing #4208, and it could not have caused anything; and the values
it stated were wrong about the running system in the other direction too -- a
reader concludes a 1-3 worker pool where `runworker` builds a 20-lane one.
Finding that out costs a trip into the image.

So the decision "does this image read this variable" gets ONE home, the
`ENV_READERS` table below, and both substrates are checked against it.
Correcting the two manifests separately would have left two places to diverge
(CLAUDE.md, "one source per decision"); the table is also where the *reason*
lives, so a future reader gets the answer without opening a container.

Two shapes of the defect are guarded, because the second one looks like the
fix for the first:

1. the variable has no reader in the image at all (`CELERY_*`);
2. the variable names a real setting of a library the image ships, but the
   image's own Django settings already assign it, so `django-vtasks`'
   environment fallback never fires (`VTASKS_QUEUES`). Setting it would read
   as tuning and tune nothing.

These are static checks over the manifests. That the *image* behaves as the
table claims is proved by running it: `scripts/glitchtip-env-reader-check.sh`,
run by `.github/workflows/glitchtip-worker-smoke.yml`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
K8S_DIR = REPO_ROOT / "k8s"

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "Pod"}

# Images nyxGPT builds itself. Their env vars are read by nyxGPT's own code,
# which this repo can simply be grepped for, so they are out of this guard's
# subject: the point of the table is third-party images whose readers live
# inside a container.
OWN_IMAGE_PREFIXES = ("nyxgpt-api", "nyxgpt-web")

# image repository (without tag) -> {env var: why that image reads it}
#
# Adding an entry is a claim about the inside of a container. Verify it the way
# #4212 did -- `docker run --rm --entrypoint sh <image> -c 'grep -rl <VAR> /'`,
# or by running the image with the variable set and observing the difference --
# and put the evidence in the reason string. "It looks like a setting that
# image would have" is exactly the reasoning that shipped the `CELERY_*` pair.
ENV_READERS: dict[str, dict[str, str]] = {
    "glitchtip/glitchtip": {
        "DATABASE_URL": "glitchtip/settings.py reads it via environ-style db config",
        "SECRET_KEY": "Django's own SECRET_KEY, read by glitchtip/settings.py",
        "REDIS_URL": "the django-vtasks broker URL, read by glitchtip/settings.py",
        "PORT": "the bundled web server's listen port",
        "EMAIL_URL": "glitchtip/settings.py email backend URL",
        "GLITCHTIP_DOMAIN": "glitchtip/settings.py, used in absolute links it emits",
        "DEFAULT_FROM_EMAIL": "Django's DEFAULT_FROM_EMAIL, read by glitchtip/settings.py",
        "ENABLE_USER_REGISTRATION": "glitchtip/settings.py self-registration switch (#3565)",
        # Verified by execution rather than by grep, because the grep alone
        # cannot tell shape 1 from shape 2 above: with VTASKS_CONCURRENCY=4 in
        # the environment the effective setting is 4, while with
        # VTASKS_QUEUES=ingest it stays ["default", "ingest"].
        "VTASKS_HEALTH_CHECK_FILE": "runworker's heartbeat file (django_vtasks/conf.py env fallback)",
        "VTASKS_METRICS_PORT": "the port runworker serves its Prometheus page on (#4208)",
    },
    "postgres": {
        "POSTGRES_USER": "docker-entrypoint.sh initdb role",
        "POSTGRES_PASSWORD": "docker-entrypoint.sh initdb password",
        "POSTGRES_DB": "docker-entrypoint.sh initdb database name",
        "PGDATA": "docker-entrypoint.sh data directory",
    },
    "redis": {},
    "cassandra": {
        # docker-entrypoint.sh:76 builds the name dynamically --
        # `var="CASSANDRA_${yaml^^}"` over a list that includes `cluster_name`
        # -- so a literal grep for the variable name finds nothing. This is the
        # case the table's reason strings exist for.
        "CASSANDRA_CLUSTER_NAME": "docker-entrypoint.sh maps CASSANDRA_* onto cassandra.yaml keys",
        "MAX_HEAP_SIZE": "/etc/cassandra/cassandra-env.sh JVM heap sizing",
        "HEAP_NEWSIZE": "/etc/cassandra/cassandra-env.sh JVM young-generation sizing",
    },
    "ollama/ollama": {
        # Not read by the ollama binary. Read by the pre-pull shell this repo
        # supplies as the container's own command (docker-compose.yml) and
        # postStart hook + readiness probe (k8s/statefulset-ollama.yaml), which
        # is a reader inside the container just the same -- and `nyxgpt ops
        # env-sync` is what writes them from config.ini (#3824).
        "NYXGPT_DEFAULT_MODEL": "the model pre-pull shell nyxGPT supplies as this container's command",
        "NYXGPT_EMBEDDING_MODEL": "the same pre-pull shell, for the RAG embedding model",
    },
    "grafana/grafana": {
        "GF_SECURITY_ADMIN_PASSWORD": "Grafana's GF_<SECTION>_<KEY> config override mechanism",
        "GF_AUTH_ANONYMOUS_ENABLED": "same mechanism, [auth.anonymous] enabled",
        "GF_INSTALL_PLUGINS": "/run.sh in the image installs each plugin named here",
        "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH": "same mechanism, [dashboards]",
    },
    "grafana/loki": {},
    "grafana/promtail": {
        # Load-bearing, and provably so: promtail prefers $HOSTNAME over
        # os.Hostname() when filtering targets by the `__host__` label, which
        # is how the DaemonSet keeps each promtail to its own node's Pods
        # (promtail.yaml's `__meta_kubernetes_pod_node_name` -> `__host__`
        # relabel). Measured with a static file target labelled
        # `__host__: fakenode`: with HOSTNAME=fakenode promtail logs "Adding
        # target" and tails it; with HOSTNAME=othernode the target is dropped.
        "HOSTNAME": "promtail's __host__ target filter prefers $HOSTNAME over os.Hostname()",
    },
    "prom/prometheus": {},
    "otel/opentelemetry-collector-contrib": {},
    "jaegertracing/all-in-one": {
        # Redundant with the flag's own default (true) and kept explicit on
        # purpose; it IS read, which is what keeps it out of #4212's class.
        # Measured: unset -> 3 OTLP lines in the startup log,
        # COLLECTOR_OTLP_ENABLED=false -> 0.
        "COLLECTOR_OTLP_ENABLED": "maps onto --collector.otlp.enabled (viper env binding)",
    },
    "alpine/socat": {},
}

# Variables that must never be set on these images, with the reason. Unlike an
# absent table entry (which means "nobody has checked"), these are checked and
# wrong, so the guard names them rather than waiting for someone to add them.
FORBIDDEN_ENV: dict[str, dict[str, str]] = {
    "glitchtip/glitchtip": {
        "CELERY_WORKER_AUTOSCALE": (
            "the pinned image has no celery binary, no importable celery module and no "
            "reader for this name; it misdescribed a 20-lane runworker pool as 1-3 "
            "workers and was mistaken for a live setting while diagnosing #4208. The "
            "knob the image reads is VTASKS_CONCURRENCY (#4212)."
        ),
        "CELERY_WORKER_MAX_TASKS_PER_CHILD": (
            "same -- no reader in the image, and runworker has no per-child task "
            "recycling to configure (#4212)."
        ),
        "VTASKS_QUEUES": (
            "glitchtip/settings.py assigns VTASKS_QUEUES, and VtasksSettings.__getattr__ "
            "consults the environment only when the Django setting is None, so this "
            "would be read by nothing. Measured: VTASKS_QUEUES=ingest leaves the "
            "effective value at ['default', 'ingest'] (#4212)."
        ),
    },
}


def _image_repo(image: str) -> str:
    """`glitchtip/glitchtip:6.2.0` -> `glitchtip/glitchtip`."""
    # Strip the tag only -- a digest or a registry host with a port would need
    # more care, and nothing in this repo uses either.
    head, _, tail = image.rpartition(":")
    return head if head and "/" not in tail else image


def _compose_env_keys(environment: Any) -> list[str]:
    """Compose accepts both the mapping and the `KEY=value` list form."""
    if environment is None:
        return []
    if isinstance(environment, dict):
        return list(environment)
    return [str(entry).split("=", 1)[0] for entry in environment]


def _compose_containers() -> Iterator[tuple[str, str, list[str]]]:
    """(where, image, env keys) for each third-party Compose service."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    for name, service in (compose.get("services") or {}).items():
        image = service.get("image")
        if not image or image.startswith(OWN_IMAGE_PREFIXES):
            continue
        yield (
            f"docker-compose.yml service {name}",
            image,
            _compose_env_keys(service.get("environment")),
        )


def _k8s_containers() -> Iterator[tuple[str, str, list[str]]]:
    """(where, image, env keys) for each third-party container under k8s/."""
    for path in sorted(K8S_DIR.rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(doc, dict) or doc.get("kind") not in WORKLOAD_KINDS:
                continue
            spec = doc["spec"].get("template", {}).get("spec") or doc["spec"]
            for key in ("initContainers", "containers"):
                for container in spec.get(key) or []:
                    image = container.get("image", "")
                    if not image or image.startswith(OWN_IMAGE_PREFIXES):
                        continue
                    yield (
                        f"{path.relative_to(REPO_ROOT)} "
                        f"{doc['metadata']['name']}/{container['name']}",
                        image,
                        [e["name"] for e in (container.get("env") or [])],
                    )


def _all_containers() -> list[tuple[str, str, list[str]]]:
    return [*_compose_containers(), *_k8s_containers()]


@pytest.mark.unit
def test_both_substrates_are_actually_scanned():
    """A sweep that silently matched nothing would pass every test below."""
    compose = list(_compose_containers())
    k8s = list(_k8s_containers())
    assert compose, "no third-party Compose services found -- the parser broke"
    assert k8s, "no third-party k8s containers found -- the parser broke"
    assert any(
        "glitchtip-worker" in where for where, _, _ in compose
    ), "the Compose service #4212 is about is not in the scan"
    assert any(
        "glitchtip-worker" in where for where, _, _ in k8s
    ), "the k8s workload #4212 is about is not in the scan"


@pytest.mark.unit
@pytest.mark.parametrize(
    "where,image,env_keys",
    [pytest.param(*c, id=c[0]) for c in _all_containers()],
)
def test_every_env_var_on_a_third_party_container_has_a_reader(
    where: str, image: str, env_keys: list[str]
):
    """#4212: config carried for a reader that does not exist is misleading.

    A new variable fails here until someone has checked the inside of the
    image and recorded WHY it is read. That check is the whole point: the
    `CELERY_*` pair passed every plausibility test a reviewer could apply
    from outside the container.
    """
    repo = _image_repo(image)
    assert repo in ENV_READERS, (
        f"{where} runs {image}, which is not in ENV_READERS. Add it (with the "
        "reason each variable is read, verified against the image) so nothing "
        "accrues configuration nobody has checked for a reader -- #4212."
    )
    readers = ENV_READERS[repo]
    unknown = [key for key in env_keys if key not in readers]
    assert not unknown, (
        f"{where} sets {unknown} on {image}, and nothing records that this "
        "image reads them. Either prove a reader exists and add it to "
        "ENV_READERS with the evidence, or delete the variable: a value the "
        "image ignores states behaviour the running system does not have "
        "(#4212)."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "where,image,env_keys",
    [pytest.param(*c, id=c[0]) for c in _all_containers()],
)
def test_known_unread_variables_stay_gone(where: str, image: str, env_keys: list[str]):
    forbidden = FORBIDDEN_ENV.get(_image_repo(image), {})
    for key in env_keys:
        assert key not in forbidden, f"{where} sets {key}: {forbidden[key]}"


@pytest.mark.unit
def test_terraform_sets_no_unread_variable_on_a_third_party_container():
    """The third substrate, found by #4212's sweep rather than named by it.

    `terraform/main.tf` runs Cassandra and Ollama from the same pinned public
    images Compose and Kubernetes do, so it can grow the same defect. It
    carries exactly one third-party variable today
    (`CASSANDRA_CLUSTER_NAME`, read by that image's entrypoint), and the
    manifest-level checks above cannot see it because HCL is not YAML.

    This is deliberately coarser than those checks: it collects the names out
    of every `env = [...]` list and requires each to be read by SOME image in
    the table, rather than by the image of the enclosing resource, because
    pairing resource to image needs an HCL parser this repo does not depend
    on. Names belonging to nyxGPT's own images are excluded -- their readers
    are in this repository and greppable.
    """
    known = {var for readers in ENV_READERS.values() for var in readers}
    seen: set[str] = set()
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "terraform").rglob("*.tf")):
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r"env\s*=\s*\[(.*?)\]", text, re.DOTALL):
            for name in re.findall(r'"([A-Z_][A-Z0-9_]*)=', block):
                seen.add(name)
                if name.startswith(("NYXGPT_", "NEXT_PUBLIC_")):
                    continue
                if name not in known:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {name}")
    # A regex sweep that matched nothing would pass silently, which is the
    # failure mode this whole module is about.
    assert "CASSANDRA_CLUSTER_NAME" in seen, (
        "the terraform env sweep found no CASSANDRA_CLUSTER_NAME, so it is "
        f"matching nothing and proving nothing (saw: {sorted(seen)})"
    )
    assert not offenders, (
        "Terraform sets these on a container whose image nothing records as "
        "reading them -- prove a reader and add it to ENV_READERS, or delete "
        f"the variable (#4212): {offenders}"
    )


@pytest.mark.unit
def test_no_substrate_names_celery_anywhere():
    """The literal regression, over the raw text rather than the parse.

    `CELERY_*` reached both substrates as env vars AND as the
    `run-celery-with-beat.sh` command, and a reader who found either
    reasonably concluded the worker was Celery-based. Prose explaining that it
    is NOT is allowed -- that is what docs/self-healing.md and the comments on
    the service do -- so this only rejects Celery appearing as *configuration*:
    an env var name, or a command. Terraform is included because it is the
    third substrate that runs these images.
    """
    offenders: list[str] = []
    sources = [
        COMPOSE_FILE,
        *sorted(K8S_DIR.rglob("*.yaml")),
        *sorted((REPO_ROOT / "terraform").rglob("*.tf")),
    ]
    for path in sources:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Comments may discuss the history -- `#` in YAML and HCL, `//` in
            # HCL's other comment form.
            if re.match(r"\s*(#|//)", line):
                continue
            if re.search(r"CELERY_|run-celery", line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Celery is named as configuration on a substrate, and the pinned "
        "glitchtip image contains no Celery at all (#4212):\n" + "\n".join(offenders)
    )
