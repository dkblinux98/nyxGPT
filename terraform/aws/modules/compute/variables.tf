variable "name_prefix" {
  description = "Name prefix applied to every resource in this module."
  type        = string
}

variable "subnet_id" {
  description = "Public subnet the instance is launched into."
  type        = string
}

variable "security_group_id" {
  description = "Security group attached to the instance (SSH-only, owner-IP-scoped)."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type."
  type        = string
}

variable "root_volume_size" {
  description = "Root EBS volume size in GiB."
  type        = number
}

variable "ami_id" {
  description = "AMI to launch. Empty resolves ami_ssm_parameter instead."
  type        = string
  default     = ""
}

variable "ami_ssm_parameter" {
  description = "SSM public parameter naming the AMI to launch when ami_id is empty."
  type        = string
}

variable "ssh_key_name" {
  description = "Existing EC2 key pair name. Mutually exclusive with ssh_public_key."
  type        = string
  default     = ""
}

variable "ssh_public_key" {
  description = "OpenSSH public key material for a key pair this module creates. Mutually exclusive with ssh_key_name."
  type        = string
  default     = ""
}

variable "assign_elastic_ip" {
  description = "Allocate an Elastic IP so the instance address survives stop/start."
  type        = bool
  default     = true
}
