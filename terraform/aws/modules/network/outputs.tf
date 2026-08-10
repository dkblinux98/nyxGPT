output "vpc_id" {
  description = "ID of the nyxGPT VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "IPv4 CIDR block of the nyxGPT VPC."
  value       = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  description = "IDs of the public subnets, in the same order as public_subnet_cidrs."
  value       = aws_subnet.public[*].id
}

output "instance_subnet_id" {
  description = "The subnet the single-box instance is placed in (the first public subnet)."
  value       = aws_subnet.public[0].id
}
