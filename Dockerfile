FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + Compose plugin, installed from Docker's static binaries
# rather than apt (no full docker-ce apt repo/GPG setup needed for a CLI
# client). This lets the self-heal watchdog (src/nyxgpt/self_heal.py) drive
# the host's Docker daemon over the socket mounted into this container at
# runtime (see the `api` service in docker-compose.yml) -- the watchdog has
# no way to restart sibling containers otherwise. See docs/self-healing.md
# for the security tradeoffs of exposing that socket.
ARG DOCKER_CLI_VERSION=25.0.5
ARG DOCKER_COMPOSE_VERSION=2.24.6
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) docker_arch=x86_64 ;; \
        arm64) docker_arch=aarch64 ;; \
        *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker-cli.tgz; \
    tar -xzf /tmp/docker-cli.tgz -C /tmp; \
    mv /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker-cli.tgz /tmp/docker; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL "https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_VERSION}/docker-compose-linux-${docker_arch}" -o /usr/local/lib/docker/cli-plugins/docker-compose; \
    chmod +x /usr/local/bin/docker /usr/local/lib/docker/cli-plugins/docker-compose; \
    docker --version; \
    docker compose version

# kubectl, installed from the current stable release channel rather than a
# pinned version. Lets deploy.py/canary.py (see docs/kubernetes.md) drive
# blue/green and canary operations against the cluster this Pod belongs to
# when the image is deployed via k8s/ -- k8s/rbac.yaml grants the Pod's
# ServiceAccount the RBAC these operations need. Unused (and harmless) under
# the docker-compose deployment mode, which has no cluster for it to reach;
# deploy.py/canary.py detect that case via NYXGPT_COMPOSE_FILE and report it
# instead of shelling out. See #3184.
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) kubectl_arch=amd64 ;; \
        arm64) kubectl_arch=arm64 ;; \
        *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    kubectl_version="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"; \
    curl -fsSL "https://dl.k8s.io/release/${kubectl_version}/bin/linux/${kubectl_arch}/kubectl" -o /usr/local/bin/kubectl; \
    chmod +x /usr/local/bin/kubectl; \
    kubectl version --client

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh /usr/local/bin/nyxgpt-entrypoint.sh
RUN chmod +x /usr/local/bin/nyxgpt-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["nyxgpt-entrypoint.sh"]
