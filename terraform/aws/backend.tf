# Where the AWS substrate's Terraform state lives (P6-9, #3510).
#
# This file exists on its own -- rather than as a `backend` block inside
# versions.tf -- because it is the one part of the configuration nyxGPT
# rewrites. `nyxgpt cloud state migrate` replaces this file in the synced,
# ops-managed copy under ~/.nyxGPT/cloud/terraform/ with the S3 flavor (see
# `nyxgpt.cloud_state.render_backend_tf`) and re-runs init with `-migrate-state`,
# so the switch never has to text-edit a file that also carries the provider
# and version pins.
#
# The default below is local state, which is what a fresh install gets: a
# single operator on one machine needs no bucket, no lock table, and no AWS
# permissions beyond what provisioning already requires. `nyxgpt cloud infra`
# points the path at ~/.nyxGPT/cloud/terraform.tfstate via
# `-backend-config=path=...` (a `~` does not expand inside a backend block),
# which is outside the synced directory so re-materializing the configuration
# can never clobber state.
#
# Run `nyxgpt cloud state migrate` to move to the shared S3 backend with
# DynamoDB locking -- required as soon as more than one operator or CI runner
# applies the same substrate. See docs/cloud.md ("Remote state").

terraform {
  backend "local" {
    path = "terraform.tfstate"
  }
}
