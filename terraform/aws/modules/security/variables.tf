variable "name_prefix" {
  description = "Name prefix applied to every resource in this module."
  type        = string
}

variable "vpc_id" {
  description = "VPC the security group belongs to."
  type        = string
}

variable "ssh_ingress_cidr" {
  description = "The single source CIDR allowed to reach TCP 22. Never 0.0.0.0/0."
  type        = string

  validation {
    condition     = can(cidrhost(var.ssh_ingress_cidr, 0)) && var.ssh_ingress_cidr != "0.0.0.0/0"
    error_message = "ssh_ingress_cidr must be a valid IPv4 CIDR and must not be 0.0.0.0/0."
  }
}

variable "egress_cidrs" {
  description = "Outbound destinations allowed from the instance."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}
