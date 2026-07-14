terraform {
  required_version = ">= 1.5.0"

  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.0"
    }
  }

  # Local-only state, no cloud backend. The path below is relative to this
  # directory; point it at a fixed location under ~/.nyxGPT instead (`~`
  # does not expand inside a backend block) with:
  #   terraform init -backend-config="path=$HOME/.nyxGPT/terraform/terraform.tfstate"
  # See docs/terraform.md#state-management.
  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "docker" {}
