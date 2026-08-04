# Local-only nyxGPT stack, standing up the same services as docker-compose.yml
# (ollama, cassandra, api, web) via the docker provider. No cloud provider
# modules, no cloud networking/security groups -- see docs/terraform.md.

resource "docker_network" "nyxgpt" {
  name = "nyxgpt-terraform"
}

# Container data lives in plain host bind mounts under ~/.nyxGPT/volumes/
# (see docs/docker-compose.md#volumes), not Terraform-managed docker_volume
# resources -- ollama/cassandra/nyxgpt-data reuse the exact same host paths
# docker-compose.yml and (cassandra only) the native `nyxgpt ops install`
# Cassandra container mount, so pulled models and Cassandra/chat data survive
# switching deployment modes instead of each mode keeping its own copy.
# `pathexpand` is Terraform's `~`-expansion function (Docker's `host_path`
# doesn't expand `~` itself). Pre-#3346 `nyxgpt_tf_*` docker_volume data is
# migrated into these paths by `nyxgpt ops migrate-volumes` (also run
# automatically by `nyxgpt ops install --terraform --local`) before this
# config's `terraform apply` would otherwise destroy the now-removed
# docker_volume resources.

# Keep this pin identical to docker-compose.yml's `ollama` service image --
# see docs/docker-compose.md#image-pinning for the policy and how to bump
# both together.
resource "docker_image" "ollama" {
  name = "ollama/ollama:0.32.4"
}

resource "docker_container" "ollama" {
  name    = "nyxgpt-tf-ollama"
  image   = docker_image.ollama.image_id
  restart = "unless-stopped"

  networks_advanced {
    name = docker_network.nyxgpt.name
    aliases = ["ollama"]
  }

  ports {
    internal = 11434
    external = var.ollama_port
  }

  volumes {
    host_path      = pathexpand("~/.nyxGPT/volumes/ollama")
    container_path = "/root/.ollama"
  }

  healthcheck {
    test         = ["CMD-SHELL", "ollama list >/dev/null 2>&1"]
    interval     = "10s"
    timeout      = "5s"
    retries      = 10
    start_period = "30s"
  }
}

# Keep this pin identical to docker-compose.yml's `cassandra` service image
# and src/nyxgpt/ops.py's CASSANDRA_IMAGE -- see
# docs/docker-compose.md#image-pinning for the policy and how to bump all
# three together.
resource "docker_image" "cassandra" {
  name = "cassandra:5.0.8"
}

resource "docker_container" "cassandra" {
  name    = "nyxgpt-tf-cassandra"
  image   = docker_image.cassandra.image_id
  restart = "unless-stopped"

  # Must match the cluster name stamped into the shared cassandra_data volume
  # (docker-compose.yml sets the same, and ops.py's CASSANDRA_CLUSTER_NAME) --
  # Cassandra refuses to start against data saved under a different cluster
  # name, so without this it crash-loops as the image default "Test Cluster".
  env = ["CASSANDRA_CLUSTER_NAME=nyxgpt"]

  networks_advanced {
    name = docker_network.nyxgpt.name
    aliases = ["cassandra"]
  }

  ports {
    internal = 9042
    external = var.cassandra_port
  }

  volumes {
    host_path      = pathexpand("~/.nyxGPT/volumes/cassandra")
    container_path = "/var/lib/cassandra"
  }

  healthcheck {
    test         = ["CMD-SHELL", "cqlsh -e 'describe cluster' >/dev/null 2>&1"]
    interval     = "15s"
    timeout      = "10s"
    retries      = 20
    start_period = "60s"
  }
}

resource "docker_image" "api" {
  name = "nyxgpt-api:${var.api_image_tag}"

  build {
    context    = var.repo_path
    dockerfile = "Dockerfile"
  }
}

resource "docker_container" "api" {
  name    = "nyxgpt-tf-api"
  image   = docker_image.api.image_id
  restart = "unless-stopped"

  depends_on = [docker_container.ollama, docker_container.cassandra]

  networks_advanced {
    name = docker_network.nyxgpt.name
    aliases = ["api"]
  }

  ports {
    internal = 8000
    external = var.api_port
  }

  env = [
    "NYXGPT_AUTH_API_KEY=${var.auth_api_key}",
    "NYXGPT_CORS_ORIGINS=${var.cors_origins}",
    # Tells self_heal.py's `_resolve_compose_file()` which compose file to run
    # `docker compose ps`/`up -d` against -- the same env var the Compose
    # `api` service sets for itself (see docker-compose.yml and
    # docs/self-healing.md). Without it, this container's module-relative and
    # config.ini fallbacks in `_resolve_compose_file()` both miss (no repo
    # checkout inside the image, and `~/.nyxGPT/volumes/nyxgpt-data` -- this
    # container's `/root/.nyxGPT` mount below -- isn't the host's real
    # `~/.nyxGPT/config.ini`), so every `docker compose ps` call in Terraform
    # mode silently failed and the observability tier (Grafana/Loki/Jaeger/
    # GlitchTip) never appeared on the Self-Heal or Infrastructure Status
    # pages (#3588).
    "NYXGPT_COMPOSE_FILE=/etc/nyxgpt/docker-compose.yml",
  ]

  volumes {
    host_path      = "${var.repo_path}/docker/config.docker.ini"
    container_path = "/etc/nyxgpt/config/config.ini"
    read_only      = true
  }

  volumes {
    host_path      = pathexpand("~/.nyxGPT/volumes/nyxgpt-data")
    container_path = "/root/.nyxGPT"
  }

  # Mirrors the Compose `api` service's own `./docker-compose.yml:/etc/nyxgpt/
  # docker-compose.yml:ro` mount -- see the NYXGPT_COMPOSE_FILE env var above
  # for why this container needs its own copy rather than resolving one from
  # its own module path or config.ini. docker-compose.yml pins its Compose
  # project name (`name: nyxgpt`), so `docker compose -f
  # /etc/nyxgpt/docker-compose.yml ps` resolves to the same project
  # regardless of the host checkout directory's name, matching whatever
  # started the observability containers on the host.
  volumes {
    host_path      = "${var.repo_path}/docker-compose.yml"
    container_path = "/etc/nyxgpt/docker-compose.yml"
    read_only      = true
  }

  # Lets the self-heal watchdog (src/nyxgpt/self_heal.py), which runs inside
  # this container, drive the host's Docker daemon to health-check/restart
  # its sibling nyxgpt-tf-* containers (ollama/cassandra/web) -- the same
  # mechanism docker-compose.yml's `api` service already uses for Compose
  # deployments. The Docker CLI is already baked into this image (see
  # Dockerfile); this is what lets it actually reach the daemon. See
  # docs/self-healing.md#docker-access-from-inside-the-api-container for the
  # security tradeoffs of exposing this socket.
  volumes {
    host_path      = "/var/run/docker.sock"
    container_path = "/var/run/docker.sock"
  }

  healthcheck {
    test         = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)\" || exit 1"]
    interval     = "10s"
    timeout      = "5s"
    retries      = 10
    start_period = "20s"
  }
}

resource "docker_image" "web" {
  name = "nyxgpt-web:${var.web_image_tag}"

  build {
    context    = "${var.repo_path}/web"
    dockerfile = "Dockerfile"
    build_args = {
      NEXT_PUBLIC_API_BASE_URL = var.web_api_base_url
    }
  }
}

resource "docker_container" "web" {
  name    = "nyxgpt-tf-web"
  image   = docker_image.web.image_id
  restart = "unless-stopped"

  depends_on = [docker_container.api]

  # The Next.js server-side proxy routes reach the API over the docker network
  # (not localhost, which inside this container is the web itself). Mirrors the
  # `web` service env in docker-compose.yml. `api` resolves via that container's
  # network alias. NYXGPT_AUTH_API_KEY is forwarded for parity; it's only used
  # when the derived config has auth enabled.
  env = [
    "NYXGPT_API_BASE_URL=http://api:8000",
    "NYXGPT_AUTH_API_KEY=${var.auth_api_key}",
  ]

  networks_advanced {
    name = docker_network.nyxgpt.name
    aliases = ["web"]
  }

  ports {
    internal = 3000
    external = var.web_port
  }
}
