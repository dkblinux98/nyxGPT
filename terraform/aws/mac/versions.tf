# Provider pins for the EC2 Mac root module (#3995).
#
# This directory is a **root** module, not a child module of ../main.tf, and
# that is the whole point: an allocated Dedicated Host cannot be released
# inside its 24-hour minimum, so a `terraform destroy` that tried would
# half-fail and take the rest of the teardown with it. Keeping the host in a
# state file of its own lets `nyxgpt cloud destroy` terminate the Mac, forget
# the host (`terraform state rm`), and hand the release to the one-shot
# schedule in ../mac-release -- with the substrate teardown unaffected either
# way. See src/nyxgpt/cloud_mac.py and docs/cloud.md, "EC2 Mac targets".
#
# Never run raw `terraform` here: `nyxgpt cloud deploy --os macos` and
# `nyxgpt cloud destroy` drive it (CLAUDE.md's wrapper requirement).

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Local state, at a path `nyxgpt cloud deploy` passes with
  # `-backend-config=path=` -- a backend block cannot spell `~/.nyxGPT`. This
  # state is deliberately NOT the substrate's S3 state: the two are torn down
  # independently, which is the isolation this module exists for.
  backend "local" {
    path = "mac.tfstate"
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile != "" ? var.aws_profile : null

  # Offline-plan escape hatches, all false by default so real runs still fail
  # fast on bad credentials. CI's plan-level gate sets them true with dummy
  # credentials so `terraform validate` needs no AWS account.
  skip_credentials_validation = var.skip_credentials_validation
  skip_requesting_account_id  = var.skip_requesting_account_id
  skip_metadata_api_check     = var.skip_metadata_api_check

  default_tags {
    tags = local.tags
  }
}
