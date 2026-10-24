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

# Every name this stack gives an AWS resource (#4200).
#
# Exposed so a plan-level test can assert that two hosts' stacks name NOTHING
# in common. They have to be independent: one stack per host is what makes two
# pending releases possible, and two stacks sharing a schedule name means the
# second apply retargets the first host's schedule -- leaving a host that is
# still allocated, still inside AWS's 24-hour minimum and still billing, with
# nothing scheduled to release it and no Slack message to say so. Every name
# below derives from `name_prefix`, which `cloud_mac._release_name_prefix`
# builds from the host id.
output "resource_names" {
  description = "Every AWS resource name this stack composes, so a test can prove two hosts share none."
  value = {
    schedule           = aws_scheduler_schedule.release.name
    state_machine      = aws_sfn_state_machine.release.name
    connection         = aws_cloudwatch_event_connection.slack.name
    scheduler_role     = aws_iam_role.scheduler.name
    state_machine_role = aws_iam_role.state_machine.name
  }
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
