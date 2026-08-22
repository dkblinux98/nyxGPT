output "host_id" {
  description = "The Dedicated Host this schedule releases."
  value       = var.host_id
}

output "release_at" {
  description = "UTC timestamp the schedule fires at."
  value       = var.release_at
}

output "schedule_name" {
  description = "Name of the one-shot schedule. It deletes itself after firing (action_after_completion = DELETE), so a plan that shows it as missing is the success case, not drift."
  value       = aws_scheduler_schedule.release.name
}

output "state_machine_arn" {
  description = "State machine the schedule starts."
  value       = aws_sfn_state_machine.release.arn
}

output "slack_channel" {
  description = "Channel the outcome is posted to."
  value       = var.slack_channel
}

output "connection_secret_arn" {
  description = "Secrets Manager secret EventBridge created to hold the Slack Authorization header. Destroyed with the connection."
  value       = aws_cloudwatch_event_connection.slack.secret_arn
}
