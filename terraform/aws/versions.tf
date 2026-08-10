# AWS provider + backend wiring for the cloud substrate (P6-8, #3509).
#
# `required_version` is >= 1.9.0 rather than the local stack's >= 1.5.0
# (../versions.tf) for two features this configuration depends on: the
# plan-level test suite in tests/*.tftest.hcl (`terraform test`, 1.6) and
# cross-variable `validation` blocks, which let the "exactly one SSH key
# input" rule fail during variable validation where a test can assert on it
# (1.9). `nyxgpt cloud infra` installs Terraform from the HashiCorp tap, so
# operators get a new enough version without doing anything.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Local state, same as the local Docker stack (../versions.tf). `nyxgpt
  # cloud infra` points this at a fixed, ops-managed location outside the
  # config directory (`~/.nyxGPT/cloud/terraform.tfstate`) via
  # `terraform init -backend-config=path=...`, since `~` does not expand
  # inside a backend block. An S3 backend with DynamoDB locking replaces this
  # in P6-9 (#3510).
  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "aws" {
  region = var.aws_region

  # `nyxgpt cloud infra` never passes credentials here -- they resolve from
  # the standard AWS chain (`~/.aws/credentials` profile / environment /
  # instance role) that `nyxgpt cloud credentials-setup` populates.
  profile = var.aws_profile != "" ? var.aws_profile : null

  # Offline-plan escape hatches, all false by default so real runs still
  # fail fast on bad credentials. CI's plan-level gate sets them true (with
  # dummy static credentials) so `terraform validate`/`terraform test` can
  # produce a plan without an AWS account -- see
  # .github/workflows/terraform-aws-validate.yml.
  skip_credentials_validation = var.skip_credentials_validation
  skip_requesting_account_id  = var.skip_requesting_account_id
  skip_metadata_api_check     = var.skip_metadata_api_check

  default_tags {
    tags = local.tags
  }
}
