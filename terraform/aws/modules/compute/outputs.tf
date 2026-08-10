output "instance_id" {
  description = "ID of the nyxGPT instance."
  value       = aws_instance.this.id
}

output "instance_type" {
  description = "Instance type actually planned/applied."
  value       = aws_instance.this.instance_type
}

output "public_ip" {
  description = "Public address to SSH to: the Elastic IP when one is allocated, else the instance's ephemeral public IP."
  value       = var.assign_elastic_ip ? aws_eip.this[0].public_ip : aws_instance.this.public_ip
}

output "private_ip" {
  description = "The instance's private address inside the VPC."
  value       = aws_instance.this.private_ip
}

output "key_name" {
  description = "EC2 key pair the instance was launched with."
  value       = aws_instance.this.key_name
}

output "imdsv2_required" {
  description = "True when instance metadata requires a session token (IMDSv2). Asserted by the plan-level test and surfaced on the admin dashboard."
  value       = aws_instance.this.metadata_options[0].http_tokens == "required"
}

output "root_volume_encrypted" {
  description = "True when the root EBS volume is encrypted at rest. Asserted by the plan-level test and surfaced on the admin dashboard."
  value       = aws_instance.this.root_block_device[0].encrypted
}
