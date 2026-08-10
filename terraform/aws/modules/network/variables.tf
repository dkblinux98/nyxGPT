variable "name_prefix" {
  description = "Name prefix applied to every resource in this module."
  type        = string
}

variable "vpc_cidr" {
  description = "IPv4 CIDR block for the VPC."
  type        = string
}

variable "public_subnet_cidrs" {
  description = "IPv4 CIDR blocks for the public subnets, in order."
  type        = list(string)
}

variable "availability_zones" {
  description = "Availability zones positionally matched to public_subnet_cidrs. Missing entries let AWS choose."
  type        = list(string)
  default     = []
}
