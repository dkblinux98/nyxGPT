variable "aws_region" {
  description = "Region the Dedicated Host and Mac instance are created in."
  type        = string
}

variable "aws_profile" {
  description = "Named AWS CLI profile to authenticate with (empty = the default chain)."
  type        = string
  default     = ""
}

variable "name_prefix" {
  description = "Prefix for every resource name and Name tag."
  type        = string
  default     = "nyxgpt-mac"
}

variable "availability_zone" {
  description = "AZ to allocate the Dedicated Host in. Mac capacity is per-AZ, so this is queried (ec2 describe-instance-type-offerings) rather than assumed -- see cloud_mac.mac_capable_azs."
  type        = string
}

variable "mac_instance_type" {
  description = "EC2 Mac instance type (mac1.metal, mac2.metal, mac2-m2.metal, mac2-m2pro.metal, ...)."
  type        = string
  default     = "mac2.metal"

  validation {
    condition     = can(regex("^mac[0-9]+(-[a-z0-9]+)?\\.metal$", var.mac_instance_type))
    error_message = "mac_instance_type must be an EC2 Mac type such as mac2.metal or mac2-m2.metal -- macOS is only ever bare metal on EC2."
  }
}

variable "mac_ami_id" {
  description = "Pin a specific macOS AMI. Empty resolves the newest amzn-ec2-macos-* AMI for the type's architecture."
  type        = string
  default     = ""
}

variable "owner_ip_cidr" {
  description = "The single CIDR allowed to reach TCP 22. Never 0.0.0.0/0 -- SSH is the only open port and the only access path (DECISION_PRIVATE_ACCESS_MECHANISM.md)."
  type        = string

  validation {
    condition     = var.owner_ip_cidr != "0.0.0.0/0"
    error_message = "owner_ip_cidr must not be 0.0.0.0/0 -- the Mac's only open port is SSH and it is scoped to the operator."
  }
}

variable "ssh_key_name" {
  description = "Name of an existing EC2 key pair to launch with. Mutually exclusive with ssh_public_key."
  type        = string
  default     = ""
}

variable "ssh_public_key" {
  description = "OpenSSH public key material to register as a new key pair. Mutually exclusive with ssh_key_name."
  type        = string
  default     = ""

  validation {
    condition     = var.ssh_public_key == "" || var.ssh_key_name == ""
    error_message = "Set exactly one of ssh_key_name (an existing pair) or ssh_public_key (material to register)."
  }
}

variable "root_volume_size" {
  description = "Root EBS volume size in GiB."
  type        = number
  default     = 200
}

variable "vpc_cidr" {
  description = "CIDR of the Mac's own VPC. Its own, not the substrate's: this module is torn down on a different schedule (see versions.tf)."
  type        = string
  default     = "10.44.0.0/16"
}

variable "subnet_cidr" {
  description = "CIDR of the single public subnet the Mac is placed in."
  type        = string
  default     = "10.44.1.0/24"
}

variable "tags" {
  description = "Extra tags merged into every resource."
  type        = map(string)
  default     = {}
}

variable "skip_credentials_validation" {
  description = "Provider escape hatch for offline plans in CI. Never set on a real run."
  type        = bool
  default     = false
}

variable "skip_requesting_account_id" {
  description = "Provider escape hatch for offline plans in CI. Never set on a real run."
  type        = bool
  default     = false
}

variable "skip_metadata_api_check" {
  description = "Provider escape hatch for offline plans in CI. Never set on a real run."
  type        = bool
  default     = false
}
