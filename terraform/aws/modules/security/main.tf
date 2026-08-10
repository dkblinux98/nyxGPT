# The one security group in front of the nyxGPT instance.
#
# product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md (P6-4/#3503):
#   "The EC2 instance's security group denies all inbound traffic except TCP
#    22, and that rule's source is restricted to the owner's current public IP
#    [...] No app or observability port is ever opened in the security group,
#    at any scope."
#
# So there is exactly one ingress rule here, and there is no variable that can
# add another: opening 8000/3000/3001 is not a configuration option, it's a
# different access model. The app, web UI, and every observability endpoint
# bind 127.0.0.1 on the instance and are reached over `nyxgpt cloud tunnel`.
#
# Inline `ingress`/`egress` blocks (rather than the newer
# aws_vpc_security_group_*_rule resources) are deliberate, and so is
# `ignore_changes = [ingress]` -- see the lifecycle block below.

locals {
  # The only port this module will ever open inbound -- see the header above.
  ssh_port = 22
}

resource "aws_security_group" "instance" {
  name_prefix = "${var.name_prefix}-instance-"
  description = "nyxGPT instance: SSH from the owner IP only; everything else is loopback-bound and tunneled."
  vpc_id      = var.vpc_id

  ingress {
    # AWS restricts rule descriptions to a fixed character set -- no backticks.
    description = "Owner workstation SSH (the only inbound path; refresh with nyxgpt cloud allow-ip)"
    protocol    = "tcp"
    from_port   = local.ssh_port
    to_port     = local.ssh_port
    cidr_blocks = [var.ssh_ingress_cidr]
  }

  egress {
    description = "Outbound for OS updates, published nyxGPT artifacts, container images, and model pulls"
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = var.egress_cidrs
  }

  tags = { Name = "${var.name_prefix}-instance-sg" }

  lifecycle {
    create_before_destroy = true

    # `nyxgpt cloud allow-ip` (docs/cloud.md, #3630) retargets this port-22
    # rule directly through the EC2 API when the owner's public IP changes --
    # it has to, because that is the lockout-recovery path and the owner
    # cannot reach the instance to do anything else. Terraform must not fight
    # it: without this, a later apply reverts the rule to whatever CIDR was in
    # tfvars, silently re-locking the owner out. (With the separate
    # aws_vpc_security_group_ingress_rule resource it is worse still --
    # allow-ip revokes the Terraform-managed rule, so the next apply tries to
    # recreate a rule AWS already has and fails with
    # InvalidPermission.Duplicate.)
    #
    # Consequence, documented in docs/cloud.md: after the group exists,
    # `nyxgpt cloud allow-ip` -- not a re-apply -- is how the SSH source
    # changes. Egress is still Terraform-managed and reconciled normally.
    ignore_changes = [ingress]

    # Belt and braces with the owner_ip_cidr variable validation in the root
    # module: this module is usable on its own, and no caller may hand it a
    # world-open SSH source.
    precondition {
      condition     = var.ssh_ingress_cidr != "0.0.0.0/0"
      error_message = "Refusing to provision an SSH rule open to 0.0.0.0/0 -- scope it to the owner's IP (see DECISION_PRIVATE_ACCESS_MECHANISM.md)."
    }
  }
}
