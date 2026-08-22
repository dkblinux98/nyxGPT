# Provider pins for the deferred Dedicated Host release (#3995).
#
# A second root module, applied by `nyxgpt cloud destroy` *before* it tears the
# Mac down and kept in its own state file. It contains nothing that costs money
# while it waits: a one-shot EventBridge schedule, a Step Functions state
# machine that runs for a few seconds when that schedule fires, an EventBridge
# connection and two IAM roles.
#
# `~> 6.0` rather than the substrate's `~> 5.0`, and the difference is not
# housekeeping: `action_after_completion` on `aws_scheduler_schedule` -- the
# argument that makes the schedule delete itself after firing instead of
# lingering as an orphan nobody knows to clean up -- does not exist in the 5.x
# line (verified against 5.100.0, which rejects it as an unsupported
# argument). Independent root modules have independent provider caches, so the
# two pins do not have to agree and the substrate's is left alone.
#
# Never run raw `terraform` here: `nyxgpt cloud destroy` drives it.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "local" {
    path = "mac-release.tfstate"
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile != "" ? var.aws_profile : null

  skip_credentials_validation = var.skip_credentials_validation
  skip_requesting_account_id  = var.skip_requesting_account_id
  skip_metadata_api_check     = var.skip_metadata_api_check

  default_tags {
    tags = local.tags
  }
}
