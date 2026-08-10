output "security_group_id" {
  description = "ID of the instance security group. `nyxgpt cloud allow-ip` retargets this group's port-22 rule."
  value       = aws_security_group.instance.id
}

output "ingress_rules" {
  description = "Every inbound rule this module provisions, as {protocol, from_port, to_port, cidrs} -- the shape the plan-level test and the admin dashboard assert the access model against."
  value = [
    for rule in aws_security_group.instance.ingress : {
      protocol  = rule.protocol
      from_port = rule.from_port
      to_port   = rule.to_port
      cidrs     = rule.cidr_blocks
    }
  ]
}

output "ingress_cidrs" {
  description = "Source CIDRs allowed inbound, for the no-0.0.0.0/0 assertion."
  value       = flatten([for rule in aws_security_group.instance.ingress : rule.cidr_blocks])
}

output "ingress_ports" {
  description = "Ports open inbound, for the SSH-only assertion."
  value       = [for rule in aws_security_group.instance.ingress : rule.from_port]
}
