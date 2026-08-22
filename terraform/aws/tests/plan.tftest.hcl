# Plan-level test suite for the AWS substrate (P6-8 AC: "`terraform validate`
# + a plan-level test in CI"). Every run block is `command = plan`, so this
# creates nothing and needs no AWS account -- the provider's skip_* escape
# hatches plus a pinned ami_id keep it fully offline. Run it with
# `nyxgpt cloud infra test` (wrapper) or in CI via
# .github/workflows/terraform-aws-validate.yml.

variables {
  aws_region    = "us-east-1"
  owner_ip_cidr = "198.51.100.7/32"

  # Pinned so the AL2023 SSM data source is never instantiated -- see the
  # compute module's `local.ami_id`.
  ami_id = "ami-0123456789abcdef0"

  # Syntactically valid throwaway key material; nothing is provisioned.
  ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBl6yQ1nyxgptplanonlytestkeymaterial00000 nyxgpt-plan-test"

  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
}

run "access_model_is_ssh_only_from_the_owner_ip" {
  command = plan

  assert {
    condition     = output.security_posture.ingress_ports == [22]
    error_message = "The substrate must open exactly one inbound port (22) -- see DECISION_PRIVATE_ACCESS_MECHANISM.md."
  }

  assert {
    condition     = output.security_posture.ingress_cidrs == ["198.51.100.7/32"]
    error_message = "The SSH rule must be scoped to the owner IP passed in, and nothing else."
  }

  assert {
    condition     = output.security_posture.world_open_ingress == false
    error_message = "No ingress rule may allow 0.0.0.0/0."
  }

  assert {
    condition     = output.security_posture.ssh_only
    error_message = "App/observability ports must never be opened in the security group -- they are loopback-bound and tunneled."
  }

  assert {
    condition     = length(output.security_posture.ingress_rules) == 1
    error_message = "Exactly one inbound rule is expected on the instance security group."
  }
}

run "instance_is_hardened_and_single_box" {
  command = plan

  assert {
    condition     = output.security_posture.imdsv2_required
    error_message = "Instance metadata must require IMDSv2 session tokens."
  }

  assert {
    condition     = output.security_posture.root_volume_encrypted
    error_message = "The root EBS volume must be encrypted at rest."
  }

  assert {
    condition     = output.instance_type == "m5.xlarge"
    error_message = "Default instance type should match DECISION_AWS_COMPUTE_SUBSTRATE.md's sizing."
  }

  assert {
    condition     = length(output.public_subnet_ids) == 1
    error_message = "The default substrate is one public subnet holding one instance."
  }
}

run "refuses_a_world_open_ssh_source" {
  command = plan

  variables {
    owner_ip_cidr = "0.0.0.0/0"
  }

  expect_failures = [var.owner_ip_cidr]
}

run "refuses_an_overly_broad_ssh_source" {
  command = plan

  variables {
    owner_ip_cidr = "10.0.0.0/8"
  }

  expect_failures = [var.owner_ip_cidr]
}

run "refuses_an_instance_with_no_ssh_key" {
  command = plan

  variables {
    ssh_key_name   = ""
    ssh_public_key = ""
  }

  expect_failures = [var.ssh_key_name]
}

run "refuses_both_ssh_key_inputs_at_once" {
  command = plan

  variables {
    ssh_key_name = "an-existing-pair"
    # ssh_public_key stays set by the file-level variables block.
  }

  expect_failures = [var.ssh_key_name]
}

run "accepts_an_existing_key_pair_by_name" {
  command = plan

  variables {
    ssh_key_name   = "an-existing-pair"
    ssh_public_key = ""
  }

  assert {
    condition     = output.ssh_key_name == "an-existing-pair"
    error_message = "An existing EC2 key pair name should be attached to the instance as-is."
  }
}
