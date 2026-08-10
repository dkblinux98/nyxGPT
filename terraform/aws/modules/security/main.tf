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

locals {
  # The only port this module will ever open inbound -- see the header above.
  ssh_port = 22
}

resource "aws_security_group" "instance" {
  name_prefix = "${var.name_prefix}-instance-"
  description = "nyxGPT instance: SSH from the owner IP only; everything else is loopback-bound and tunneled."
  vpc_id      = var.vpc_id

  tags = { Name = "${var.name_prefix}-instance-sg" }

  lifecycle {
    create_before_destroy = true

    # Belt and braces with the owner_ip_cidr variable validation in the root
    # module: this module is usable on its own, and no caller may hand it a
    # world-open SSH source.
    precondition {
      condition     = var.ssh_ingress_cidr != "0.0.0.0/0"
      error_message = "Refusing to provision an SSH rule open to 0.0.0.0/0 -- scope it to the owner's IP (see DECISION_PRIVATE_ACCESS_MECHANISM.md)."
    }
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.instance.id
  description       = "Owner workstation SSH (the only inbound path; refresh with `nyxgpt cloud allow-ip`)"

  cidr_ipv4   = var.ssh_ingress_cidr
  ip_protocol = "tcp"
  from_port   = local.ssh_port
  to_port     = local.ssh_port

  tags = { Name = "${var.name_prefix}-ssh-ingress" }
}

resource "aws_vpc_security_group_egress_rule" "outbound" {
  for_each = toset(var.egress_cidrs)

  security_group_id = aws_security_group.instance.id
  description       = "Outbound for OS updates, published nyxGPT artifacts, container images, and model pulls"

  cidr_ipv4   = each.value
  ip_protocol = "-1"

  tags = { Name = "${var.name_prefix}-egress" }
}
