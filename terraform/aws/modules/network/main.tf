# VPC + public subnets for the single-box nyxGPT substrate.
#
# Deliberately minimal: no NAT gateway, no private subnets, no load balancer.
# Per product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md the substrate is one
# EC2 instance, and per DECISION_PRIVATE_ACCESS_MECHANISM.md the only inbound
# path is SSH from the owner's IP -- so the instance needs a routable address
# (public subnet + internet gateway) and nothing else. Every service on it
# binds 127.0.0.1 and is reached through the SSH tunnel.

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

resource "aws_subnet" "public" {
  count = length(var.public_subnet_cidrs)

  vpc_id     = aws_vpc.this.id
  cidr_block = var.public_subnet_cidrs[count.index]

  # Positional match against public_subnet_cidrs; null lets AWS pick, which
  # keeps this configuration plannable with no AWS API call (no
  # aws_availability_zones data source) -- what the CI plan gate relies on.
  availability_zone = try(var.availability_zones[count.index], null)

  # The instance opts into a public IP explicitly (see the compute module);
  # leaving this false means nothing else placed here gets one by accident.
  map_public_ip_on_launch = false

  tags = { Name = "${var.name_prefix}-public-${count.index + 1}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-public-rt" }
}

resource "aws_route" "default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
