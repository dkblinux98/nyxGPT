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

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh /usr/local/bin/nyxgpt-entrypoint.sh
RUN chmod +x /usr/local/bin/nyxgpt-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["nyxgpt-entrypoint.sh"]
