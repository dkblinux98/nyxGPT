output "security_group_id" {
  description = "ID of the instance security group. `nyxgpt cloud allow-ip` retargets this group's port-22 rule."
  value       = aws_security_group.instance.id
}

output "ingress_rules" {
  description = "Every inbound rule this module provisions, as {protocol, from_port, to_port, cidr} -- the shape the plan-level test and the admin dashboard assert the access model against."
  value = [
    {
      protocol  = aws_vpc_security_group_ingress_rule.ssh.ip_protocol
      from_port = aws_vpc_security_group_ingress_rule.ssh.from_port
      to_port   = aws_vpc_security_group_ingress_rule.ssh.to_port
      cidr      = aws_vpc_security_group_ingress_rule.ssh.cidr_ipv4
    }
  ]
}

output "ingress_cidrs" {
  description = "Source CIDRs allowed inbound, for the no-0.0.0.0/0 assertion."
  value       = [aws_vpc_security_group_ingress_rule.ssh.cidr_ipv4]
}

output "ingress_ports" {
  description = "Ports open inbound, for the SSH-only assertion."
  value       = [aws_vpc_security_group_ingress_rule.ssh.from_port]
}
