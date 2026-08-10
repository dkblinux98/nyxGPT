# The single EC2 instance that is the whole nyxGPT cloud substrate
# (product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md, P6-7/#3506: EC2
# single-box, no EKS, no managed node group, no load balancer).
#
# This module provisions the box and nothing on it: OS-level provisioning and
# the actual stack deploy are P6-12 (#3512) and P6-11 (#3513), which reach the
# instance over the SSH path this module opens.

locals {
  # Resolved AMI: an explicit ami_id wins, otherwise the SSM public parameter
  # for the current Amazon Linux 2023 image. The data source is conditional so
  # a pinned ami_id makes this configuration plannable with no AWS API call --
  # what the CI plan gate relies on.
  ami_id = var.ami_id != "" ? var.ami_id : data.aws_ssm_parameter.ami[0].value

  created_key_name = var.ssh_public_key != "" ? aws_key_pair.this[0].key_name : null
  key_name         = var.ssh_key_name != "" ? var.ssh_key_name : local.created_key_name
}

data "aws_ssm_parameter" "ami" {
  count = var.ami_id == "" ? 1 : 0

  name = var.ami_ssm_parameter
}

resource "aws_key_pair" "this" {
  count = var.ssh_public_key != "" ? 1 : 0

  key_name   = "${var.name_prefix}-key"
  public_key = var.ssh_public_key

  tags = { Name = "${var.name_prefix}-key" }
}

resource "aws_instance" "this" {
  ami           = local.ami_id
  instance_type = var.instance_type
  subnet_id     = var.subnet_id
  key_name      = local.key_name

  vpc_security_group_ids = [var.security_group_id]

  # The owner SSHes straight to the box (no bastion, no NAT gateway) -- the
  # security group is what keeps that reachable only from the owner's IP.
  associate_public_ip_address = true

  # IMDSv2 required: token-less metadata requests are refused, and the hop
  # limit keeps a container on the box from reaching instance credentials.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size
    encrypted             = true
    delete_on_termination = true

    tags = { Name = "${var.name_prefix}-root" }
  }

  tags = { Name = "${var.name_prefix}-instance" }

  lifecycle {
    # Cross-variable "exactly one of" check for callers using this module
    # directly. The root module validates the same rule on its own variables
    # (variables.tf's ssh_key_name), which is what fails first -- and what the
    # plan-level test asserts on -- for the normal `nyxgpt cloud infra` path.
    precondition {
      condition     = (var.ssh_key_name != "") != (var.ssh_public_key != "")
      error_message = "Set exactly one of ssh_key_name (an existing EC2 key pair) or ssh_public_key (material for a new one) -- an instance with neither is unreachable, and nyxGPT's only access path is SSH."
    }
  }
}

# A stable address across stop/start: the SSH tunnel target and the
# owner-IP-scoped SG rule both reference this instance, and a new public IP on
# every restart would break the documented `nyxgpt cloud tunnel` invocation.
resource "aws_eip" "this" {
  count = var.assign_elastic_ip ? 1 : 0

  domain   = "vpc"
  instance = aws_instance.this.id

  tags = { Name = "${var.name_prefix}-eip" }
}
