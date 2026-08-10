# `nyxgpt cloud infra apply` reads these back with `terraform output -json` and
# writes security_group_id/region/instance_id/public_ip into
# ~/.nyxGPT/cloud/state.json -- the contract `nyxgpt cloud allow-ip` already
# consumes (see src/nyxgpt/cloud.py). Renaming one breaks that handoff.

output "region" {
  description = "Region the substrate was provisioned in."
  value       = var.aws_region
}

output "vpc_id" {
  description = "ID of the nyxGPT VPC."
  value       = module.network.vpc_id
}

output "public_subnet_ids" {
  description = "IDs of the provisioned public subnets."
  value       = module.network.public_subnet_ids
}

output "security_group_id" {
  description = "Instance security group. `nyxgpt cloud allow-ip` retargets its port-22 rule when the owner's IP changes."
  value       = module.security.security_group_id
}

output "instance_id" {
  description = "ID of the nyxGPT EC2 instance."
  value       = module.compute.instance_id
}

output "instance_type" {
  description = "Instance type of the nyxGPT EC2 instance."
  value       = module.compute.instance_type
}

output "public_ip" {
  description = "Address the owner SSHes to (Elastic IP when allocated)."
  value       = module.compute.public_ip
}

output "private_ip" {
  description = "The instance's private address inside the VPC."
  value       = module.compute.private_ip
}

output "ssh_key_name" {
  description = "EC2 key pair the instance was launched with."
  value       = module.compute.key_name
}

# One object describing what the substrate exposes and how it's hardened.
# Asserted by tests/plan.tftest.hcl and rendered by the admin dashboard's
# Cloud Infrastructure page, so the access model is verifiable in CI and
# visible to the operator rather than only reviewable in HCL.
output "security_posture" {
  description = "Access-model summary: inbound rules, whether anything is world-open, and instance hardening flags."
  value = {
    ingress_rules         = module.security.ingress_rules
    ingress_cidrs         = module.security.ingress_cidrs
    ingress_ports         = module.security.ingress_ports
    ssh_only              = module.security.ingress_ports == [22]
    world_open_ingress    = contains(module.security.ingress_cidrs, "0.0.0.0/0")
    imdsv2_required       = module.compute.imdsv2_required
    root_volume_encrypted = module.compute.root_volume_encrypted
  }
}
