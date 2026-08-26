# EC2 Mac Dedicated Host + the Mac instance placed on it (#3995).
#
# Before this module, `nyxgpt cloud deploy --os macos` refused unless the
# operator produced a Mac themselves with raw `aws ec2 allocate-hosts` /
# `run-instances --placement Tenancy=host`. That refusal's stated reason was
# economic -- nyxGPT would be spending the operator's money on a host it could
# not then release -- and it stopped being true once the release was deferred
# to a one-shot schedule (../mac-release). What remains is a consent problem,
# and consent is a prompt: the CLI prices the host live and requires a typed
# confirmation before anything here is applied.
#
# Two shapes here are deliberate and load-bearing:
#
#  * **Its own VPC**, not the substrate's. The substrate (../main.tf) provisions
#    a default-tenancy Linux box that a macOS deploy has no use for, and its
#    lifecycle is `nyxgpt cloud destroy`'s to end synchronously. This module
#    is torn down with a host left behind on purpose, so it owns everything it
#    needs and shares no resource whose deletion could block on the other.
#  * **`aws_ec2_host` in this state file only.** `nyxgpt cloud destroy` runs
#    `terraform state rm aws_ec2_host.this` before destroying, so Terraform
#    terminates the instance and deletes the network without ever calling
#    ReleaseHosts -- which AWS would reject inside the 24-hour minimum.

locals {
  tags = merge(
    {
      Project   = "nyxGPT"
      ManagedBy = "terraform"
      Component = "cloud-mac-host"
    },
    var.tags,
  )

  # EC2 Mac AMIs carry a Mac-specific architecture value: Intel `mac1.metal`
  # is x86_64_mac, every Apple Silicon `mac2*` is arm64_mac. Picking the wrong
  # one does not fail at plan time -- it fails at RunInstances, twenty minutes
  # into a host that is already billing.
  mac_architecture = startswith(var.mac_instance_type, "mac1.") ? "x86_64_mac" : "arm64_mac"

  ami_id = var.mac_ami_id != "" ? var.mac_ami_id : try(data.aws_ami.macos[0].id, "")

  # Register a key pair only when the operator handed us material. With
  # `ssh_key_name` they already have one and creating a second would be a
  # duplicate name collision on the next deploy.
  create_key_pair = var.ssh_public_key != ""
  key_name        = local.create_key_pair ? aws_key_pair.this[0].key_name : var.ssh_key_name
}

data "aws_ami" "macos" {
  count = var.mac_ami_id == "" ? 1 : 0

  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["amzn-ec2-macos-*"]
  }

  filter {
    name   = "architecture"
    values = [local.mac_architecture]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

# --- Network -----------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.name_prefix}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-igw" }
}

resource "aws_subnet" "this" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true

  tags = { Name = "${var.name_prefix}-subnet" }
}

resource "aws_route_table" "this" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${var.name_prefix}-rt" }
}

resource "aws_route_table_association" "this" {
  subnet_id      = aws_subnet.this.id
  route_table_id = aws_route_table.this.id
}

# Same access model as the substrate's security module: TCP 22 from the
# operator's address and nothing else. The app, web UI and every other port
# bind 127.0.0.1 on the Mac and are reached over `nyxgpt cloud tunnel`.
resource "aws_security_group" "this" {
  name        = "${var.name_prefix}-sg"
  description = "nyxGPT EC2 Mac: SSH from the operator only; everything else is tunneled."
  vpc_id      = aws_vpc.this.id

  ingress {
    # No apostrophe, and that is not a style choice: EC2 validates
    # security-group RULE descriptions against
    # ^[0-9A-Za-z_ .:/()#,@\[\]+=&;{}!$*-]*$ , which excludes `'`. The
    # apostrophe in "operator's" here made every `nyxgpt cloud deploy --os
    # macos` fail at apply -- after the operator had read the cost disclosure
    # and typed `allocate` to consent to a non-refundable 24-hour charge.
    # Guarded by tests/unit/test_terraform_aws_descriptions.py.
    description = "SSH from the operator workstation address (the only access path)."
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.owner_ip_cidr]
  }

  egress {
    description = "Outbound for Homebrew, the remote tap and PyPI."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name_prefix}-sg" }
}

resource "aws_key_pair" "this" {
  count = local.create_key_pair ? 1 : 0

  key_name   = "${var.name_prefix}-key"
  public_key = var.ssh_public_key
}

# --- The billed resources ----------------------------------------------

# The Dedicated Host. Allocation starts a 24-hour minimum that AWS bills
# whether or not an instance is on it, and ReleaseHosts is rejected until that
# window closes -- which is why `nyxgpt cloud destroy` removes this resource
# from state rather than destroying it, and why ../mac-release exists.
resource "aws_ec2_host" "this" {
  instance_type     = var.mac_instance_type
  availability_zone = var.availability_zone

  # `off`: only instances that name this host id may land on it. `on` would
  # let any untargeted instance of the same type in the account be placed
  # here, which is a surprising way to inherit someone else's workload on a
  # host nyxGPT is about to schedule for release.
  auto_placement = "off"

  tags = {
    Name = "${var.name_prefix}-host"
    # Read back by `nyxgpt cloud status` and by the deferred-release stack, so
    # a host that outlives its state file is still identifiable as ours.
    "nyxgpt:release-deferred" = "true"
  }
}

resource "aws_instance" "mac" {
  ami           = local.ami_id
  instance_type = var.mac_instance_type

  # Tenancy `host` plus an explicit host id: the instance is pinned to the
  # host allocated above rather than to whatever capacity EC2 finds.
  tenancy = "host"
  host_id = aws_ec2_host.this.id

  subnet_id                   = aws_subnet.this.id
  vpc_security_group_ids      = [aws_security_group.this.id]
  key_name                    = local.key_name
  associate_public_ip_address = true

  root_block_device {
    volume_size = var.root_volume_size
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  tags = { Name = "${var.name_prefix}-instance" }

  # `ami` forces replacement, and on this resource replacement is not the
  # cheap thing it is elsewhere. Destroying a Mac instance starts the host's
  # **scrub** -- AWS wipes the bare metal, which takes on the order of an hour
  # -- and nothing can be placed on the host until it finishes. So a
  # replacement here is not "briefly down": it is the operator's Mac deleted,
  # its disk gone, and the create half of the replace failing against a host
  # that is still scrubbing.
  #
  # Amazon publishes new `amzn-ec2-macos-*` images regularly, and
  # `data.aws_ami.macos` above is `most_recent = true`. Without this, the next
  # `nyxgpt cloud deploy --os macos` on an existing host -- documented as safe
  # to re-run, and which reconciles rather than allocating -- would resolve a
  # newer AMI id and silently plan exactly that destroy/create.
  #
  # `ignore_changes` applies only to updates, so a *new* instance still boots
  # the AMI resolved above (or `--mac-ami-id`). Moving an existing Mac to a
  # newer macOS stays possible and stays deliberate: `nyxgpt cloud destroy`,
  # then deploy again. cloud_mac also records the booted AMI and feeds it back
  # on reconcile, so in the normal case there is no diff for this to suppress
  # -- belt and braces, because the record cannot exist for a host allocated
  # before it did.
  lifecycle {
    ignore_changes = [ami]
  }
}
