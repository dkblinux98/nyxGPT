# AWS substrate for a cloud nyxGPT deployment (P6-8, #3509).
#
# Shape is fixed by two approved decision records, not by taste:
#   * product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md (#3506) -- one EC2
#     instance. No EKS, no managed node group, no ALB/NLB.
#   * product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md (#3503) -- the
#     only open port is TCP 22, scoped to the owner's IP; the app, web UI, and
#     every observability endpoint bind 127.0.0.1 and are reached over an SSH
#     tunnel.
#
# Never run raw `terraform` against this directory -- `nyxgpt cloud infra
# {plan,apply,destroy,status}` drives it (CLAUDE.md's wrapper requirement) and
# is what records the security-group id and region that `nyxgpt cloud allow-ip`
# later reads from ~/.nyxGPT/cloud/state.json. See docs/cloud.md.

locals {
  tags = merge(
    {
      Project   = "nyxGPT"
      ManagedBy = "terraform"
      Component = "cloud-substrate"
    },
    var.tags,
  )
}

module "network" {
  source = "./modules/network"

  name_prefix         = var.name_prefix
  vpc_cidr            = var.vpc_cidr
  public_subnet_cidrs = var.public_subnet_cidrs
  availability_zones  = var.availability_zones
}

module "security" {
  source = "./modules/security"

  name_prefix      = var.name_prefix
  vpc_id           = module.network.vpc_id
  ssh_ingress_cidr = var.owner_ip_cidr
  egress_cidrs     = var.egress_cidrs
}

module "compute" {
  source = "./modules/compute"

  name_prefix       = var.name_prefix
  subnet_id         = module.network.instance_subnet_id
  security_group_id = module.security.security_group_id
  instance_type     = var.instance_type
  root_volume_size  = var.root_volume_size
  ami_id            = var.ami_id
  ami_ssm_parameter = var.ami_ssm_parameter
  ssh_key_name      = var.ssh_key_name
  ssh_public_key    = var.ssh_public_key
  assign_elastic_ip = var.assign_elastic_ip
}
