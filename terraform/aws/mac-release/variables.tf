variable "aws_region" {
  description = "Region the Dedicated Host lives in. The schedule, state machine and connection are created here too."
  type        = string
}

variable "aws_profile" {
  description = "Named AWS CLI profile to authenticate with (empty = the default chain)."
  type        = string
  default     = ""
}

variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
  default     = "nyxgpt-mac"
}

variable "host_id" {
  description = "Dedicated Host to release when the schedule fires."
  type        = string

  validation {
    condition     = can(regex("^h-[0-9a-f]+$", var.host_id))
    error_message = "host_id must be an EC2 Dedicated Host id (h-...)."
  }
}

variable "release_at" {
  description = "When to attempt the release, as an EventBridge Scheduler at() timestamp in UTC (YYYY-MM-DDTHH:MM:SS). Computed by cloud_mac.release_time as allocation + 24h + a scrub buffer."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$", var.release_at))
    error_message = "release_at must be YYYY-MM-DDTHH:MM:SS with no timezone suffix -- EventBridge Scheduler's at() expression takes a naive timestamp and schedule_expression_timezone says which zone it is in."
  }
}

variable "slack_channel" {
  description = "Slack channel id the outcome is posted to (config.ini monitoring.slack_channel)."
  type        = string
}

variable "slack_authorization_header" {
  description = "Full value of the Authorization header sent to Slack: 'Bearer xoxb-...'. EventBridge stores this in a Secrets Manager secret it manages itself."
  type        = string
  sensitive   = true
}

variable "scrub_wait_seconds" {
  description = "Seconds to wait between release attempts while the host is still being scrubbed."
  type        = number
  default     = 300
}

variable "scrub_max_attempts" {
  description = "How many times to re-attempt a release the host scrub is still blocking. 48 x 300s covers a four-hour scrub."
  type        = number
  default     = 48
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
