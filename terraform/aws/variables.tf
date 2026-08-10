variable "aws_region" {
  description = "AWS region the nyxGPT substrate is provisioned in."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "Named AWS profile to authenticate with. Empty uses the default credential chain (see `nyxgpt cloud credentials-setup`)."
  type        = string
  default     = ""
}

variable "name_prefix" {
  description = "Name prefix for every provisioned resource. Matches the local stack's `nyxgpt-tf-*` container naming convention (see docs/terraform.md)."
  type        = string
  default     = "nyxgpt-tf"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,30}$", var.name_prefix))
    error_message = "name_prefix must be 2-31 lowercase alphanumeric/dash characters starting with alphanumeric."
  }
}

variable "vpc_cidr" {
  description = "IPv4 CIDR block for the nyxGPT VPC."
  type        = string
  default     = "10.42.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block."
  }
}

variable "public_subnet_cidrs" {
  description = "IPv4 CIDR blocks for the public subnets. The instance lands in the first one; extra entries exist so a later multi-AZ need doesn't require re-shaping the module."
  type        = list(string)
  default     = ["10.42.1.0/24"]

  validation {
    condition     = length(var.public_subnet_cidrs) > 0
    error_message = "public_subnet_cidrs must contain at least one CIDR block (the instance's subnet)."
  }

  validation {
    condition     = alltrue([for c in var.public_subnet_cidrs : can(cidrhost(c, 0))])
    error_message = "Every entry in public_subnet_cidrs must be a valid IPv4 CIDR block."
  }
}

variable "availability_zones" {
  description = "Availability zones for the public subnets, positionally matched to public_subnet_cidrs. Empty lets AWS choose, which keeps the configuration plannable without an AWS API call."
  type        = list(string)
  default     = []
}

variable "owner_ip_cidr" {
  description = "The owner workstation's public IP as a CIDR, and the ONLY source allowed to reach the instance (SSH, port 22). Refresh it with `nyxgpt cloud allow-ip` when the owner's IP changes."
  type        = string

  validation {
    condition     = can(cidrhost(var.owner_ip_cidr, 0))
    error_message = "owner_ip_cidr must be a valid IPv4 CIDR block, e.g. 198.51.100.7/32."
  }

  # The hard rule from product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md
  # (P6-4/#3503): the deployment is private to the owner's workstation, so a
  # world-open SSH rule is refused at plan time rather than reviewed after the
  # fact. /16 is the widest accepted prefix -- anything broader is a
  # fat-fingered CIDR, not an owner IP.
  validation {
    condition     = var.owner_ip_cidr != "0.0.0.0/0" && tonumber(split("/", var.owner_ip_cidr)[1]) >= 16
    error_message = "owner_ip_cidr must not be 0.0.0.0/0 and must be no broader than a /16 -- nyxGPT deployments are private to the owner's workstation."
  }
}

variable "egress_cidrs" {
  description = "Destinations the instance may reach outbound (package/artifact downloads, container images, model pulls). Egress only -- the no-0.0.0.0/0 rule in DECISION_PRIVATE_ACCESS_MECHANISM.md is about ingress."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "instance_type" {
  description = "EC2 instance type for the single-box substrate (product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md)."
  type        = string
  default     = "m5.large"
}

variable "root_volume_size" {
  description = "Root EBS volume size in GiB. Sized for Ollama models plus Cassandra data on one box."
  type        = number
  default     = 100

  validation {
    condition     = var.root_volume_size >= 30
    error_message = "root_volume_size must be at least 30 GiB to hold the OS, container images, and at least one model."
  }
}

variable "ami_id" {
  description = "AMI to launch. Empty resolves the current Amazon Linux 2023 AMI from ami_ssm_parameter at plan time."
  type        = string
  default     = ""
}

variable "ami_ssm_parameter" {
  description = "SSM public parameter naming the AMI to launch when ami_id is empty."
  type        = string
  default     = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

variable "ssh_key_name" {
  description = "Name of an existing EC2 key pair to attach. Mutually exclusive with ssh_public_key; exactly one of the two is required."
  type        = string
  default     = ""

  # SSH is the only way in (DECISION_PRIVATE_ACCESS_MECHANISM.md), so an
  # instance launched with no key -- or with an ambiguous pair of key inputs --
  # is a provisioning bug worth catching before anything is created.
  validation {
    condition     = (var.ssh_key_name != "") != (var.ssh_public_key != "")
    error_message = "Set exactly one of ssh_key_name (an existing EC2 key pair) or ssh_public_key (material for a new one) -- SSH is the only path to a nyxGPT instance."
  }
}

variable "ssh_public_key" {
  description = "OpenSSH public key material to register as a new key pair. Mutually exclusive with ssh_key_name; exactly one of the two is required."
  type        = string
  default     = ""
}

variable "assign_elastic_ip" {
  description = "Allocate an Elastic IP so the instance address survives stop/start -- keeps the SSH tunnel target and the owner-IP-scoped SG rule stable."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Extra tags merged into every resource on top of the module's own."
  type        = map(string)
  default     = {}
}

variable "skip_credentials_validation" {
  description = "Provider escape hatch for offline plan/test runs in CI. Never set this for a real deploy."
  type        = bool
  default     = false
}

variable "skip_requesting_account_id" {
  description = "Provider escape hatch for offline plan/test runs in CI. Never set this for a real deploy."
  type        = bool
  default     = false
}

variable "skip_metadata_api_check" {
  description = "Provider escape hatch for offline plan/test runs in CI. Never set this for a real deploy."
  type        = bool
  default     = false
}
