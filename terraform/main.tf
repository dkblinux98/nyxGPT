# Local-only nyxGPT stack, standing up the same services as docker-compose.yml
# (ollama, cassandra, api, web) via the docker provider. No cloud provider
# modules, no cloud networking/security groups -- see docs/terraform.md.

resource "docker_network" "nyxgpt" {
  name = "nyxgpt-terraform"
}

resource "docker_volume" "ollama_data" {
  name = "nyxgpt_tf_ollama_data"
}

resource "docker_volume" "cassandra_data" {
  name = "nyxgpt_tf_cassandra_data"
}

resource "docker_volume" "nyxgpt_data" {
  name = "nyxgpt_tf_nyxgpt_data"
}

resource "docker_image" "ollama" {
  name = "ollama/ollama:latest"
}

resource "docker_container" "ollama" {
  name    = "nyxgpt-tf-ollama"
  image   = docker_image.ollama.image_id
  restart = "unless-stopped"

  networks_advanced {
    name = docker_network.nyxgpt.name
  }

  ports {
    internal = 11434
    external = var.ollama_port
  }

  volumes {
    volume_name    = docker_volume.ollama_data.name
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

resource "docker_image" "cassandra" {
  name = "cassandra:5.0"
}

resource "docker_container" "cassandra" {
  name    = "nyxgpt-tf-cassandra"
  image   = docker_image.cassandra.image_id
  restart = "unless-stopped"

  networks_advanced {
    name = docker_network.nyxgpt.name
  }

  ports {
    internal = 9042
    external = var.cassandra_port
  }

  volumes {
    volume_name    = docker_volume.cassandra_data.name
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
  }

  ports {
    internal = 8000
    external = var.api_port
  }

  env = [
    "NYXGPT_AUTH_API_KEY=${var.auth_api_key}",
    "NYXGPT_CORS_ORIGINS=${var.cors_origins}",
  ]

  volumes {
    host_path      = "${var.repo_path}/docker/config.docker.ini"
    container_path = "/etc/nyxgpt/config/config.ini"
    read_only      = true
  }

  volumes {
    volume_name    = docker_volume.nyxgpt_data.name
    container_path = "/root/.nyxGPT"
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

  networks_advanced {
    name = docker_network.nyxgpt.name
  }

  ports {
    internal = 3000
    external = var.web_port
  }
}
