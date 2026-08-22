# Read back with `terraform output -json` by `nyxgpt.cloud_mac`, which records
# host_id / instance_id / the allocation timestamp / the computed release time
# into ~/.nyxGPT/cloud/state.json. `nyxgpt cloud status` answers the "what is
# still billing?" question from that file alone, with no AWS call.

output "region" {
  description = "Region the host was allocated in."
  value       = var.aws_region
}

output "host_id" {
  description = "Dedicated Host id. The one value the deferred release needs, and the one nothing else can re-derive once state is gone."
  value       = aws_ec2_host.this.id
}

output "instance_id" {
  description = "ID of the Mac instance placed on the host."
  value       = aws_instance.mac.id
}

output "availability_zone" {
  description = "AZ the host and instance live in."
  value       = aws_ec2_host.this.availability_zone
}

output "instance_type" {
  description = "EC2 Mac instance type (also the host's family)."
  value       = aws_instance.mac.instance_type
}

output "public_ip" {
  description = "Address the operator SSHes to."
  value       = aws_instance.mac.public_ip
}

output "private_ip" {
  description = "The Mac's private address inside its VPC."
  value       = aws_instance.mac.private_ip
}

output "vpc_id" {
  description = "ID of the Mac's own VPC."
  value       = aws_vpc.this.id
}

output "security_group_id" {
  description = "Security group holding the single owner-scoped port-22 rule."
  value       = aws_security_group.this.id
}

output "ssh_key_name" {
  description = "EC2 key pair the Mac was launched with."
  value       = local.key_name
}

output "ami_id" {
  description = "macOS AMI the instance booted."
  value       = local.ami_id
}
