# Deferred release of the EC2 Mac Dedicated Host, with a Slack report (#3995).
#
# An allocated Dedicated Host bills a 24-hour minimum and **cannot be released
# inside that window** -- AWS rejects the call. A naive `aws_ec2_host` in the
# teardown's own state file therefore half-fails `terraform destroy`, which is
# exactly the trap that leaves an operator billing for something they believe
# is gone. So the release is deferred: `nyxgpt cloud destroy` terminates the
# Mac immediately, forgets the host, and applies this module to fire once,
# later, on its own.
#
# **Why Step Functions and not a Lambda or AWS Chatbot.** SNS cannot deliver to
# a Slack incoming webhook -- an HTTPS subscription requires the endpoint to
# answer the SubscribeURL confirmation handshake, which Slack does not do. The
# usual ways out are AWS Chatbot (a manual workspace authorization the owner
# declined) or a Lambda (code to ship, version and maintain). Step Functions'
# HTTP Task calls any HTTPS API using an EventBridge Connection for auth, and
# an API_KEY connection takes an arbitrary header name -- so `Authorization:
# Bearer <the bot token nyxGPT already has>` is enough to call
# chat.postMessage directly. No Lambda, no Chatbot, no Slack-side setup.
#
# **Slack returns HTTP 200 on failure.** `invalid_auth`, `channel_not_found`
# and friends come back 200 with `"ok": false`, so branching on status codes
# alone reports success for a message that never arrived. Every Slack call
# below is followed by a Choice on `$.ResponseBody.ok`.
#
# **ReleaseHosts returns 200 on failure too**, for the same class of reason:
# a host still being scrubbed after its instance was terminated comes back
# with the host in `Unsuccessful`, not as an exception -- so a `Retry` block
# would never fire. The scrub is absorbed by an explicit Wait/count loop.

locals {
  tags = merge(
    {
      Project   = "nyxGPT"
      ManagedBy = "terraform"
      Component = "cloud-mac-host-release"
    },
    var.tags,
  )

  slack_endpoint = "https://slack.com/api/chat.postMessage"

  release_success_text = join(" ", [
    ":white_check_mark: nyxGPT released EC2 Mac Dedicated Host ${var.host_id}",
    "in ${var.aws_region}. It has stopped billing.",
    "This is the deferred half of `nyxgpt cloud destroy` -- the Mac instance",
    "was terminated at teardown; the host could not be released until its",
    "24-hour minimum closed.",
  ])

  release_failure_text = join(" ", [
    ":rotating_light: nyxGPT could NOT release EC2 Mac Dedicated Host ${var.host_id}",
    "in ${var.aws_region}, and it is *still billing*.",
    "Re-run `nyxgpt cloud destroy --yes` to reschedule the release, or check",
    "the host's state -- `nyxgpt cloud status` reports what nyxGPT still",
    "believes is outstanding.",
  ])

  # One state machine, four outcomes: released, released-but-Slack-failed,
  # not-released, not-released-and-Slack-failed. The last two are Fail states
  # so a silent non-release shows up as a failed execution rather than as
  # nothing at all.
  release_definition = jsonencode({
    Comment = "Release nyxGPT's EC2 Mac Dedicated Host once its 24-hour minimum has closed, and report the outcome to Slack."
    StartAt = "ReleaseHost"
    States = {
      ReleaseHost = {
        Type       = "Task"
        Resource   = "arn:aws:states:::aws-sdk:ec2:releaseHosts"
        Parameters = { HostIds = [var.host_id] }
        ResultPath = "$.release"
        # Transient API problems only -- throttling, a 5xx. The scrub window
        # is not an exception and is handled by the Choice below.
        Retry = [{
          ErrorEquals     = ["States.ALL"]
          IntervalSeconds = 30
          MaxAttempts     = 5
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.error"
          Next        = "NotifyFailure"
        }]
        Next = "ReleaseAccepted"
      }

      ReleaseAccepted = {
        Type = "Choice"
        Choices = [
          {
            Variable  = "$.release.Successful[0]"
            IsPresent = true
            Next      = "NotifySuccess"
          },
          {
            Variable        = "$.attempts"
            NumericLessThan = var.scrub_max_attempts
            Next            = "WaitForScrub"
          },
        ]
        Default = "NotifyFailure"
      }

      WaitForScrub = {
        Type    = "Wait"
        Seconds = var.scrub_wait_seconds
        Next    = "CountAttempt"
      }

      # Step Functions has no loop counter, so one is carried in the state
      # document and incremented with the States.MathAdd intrinsic.
      CountAttempt = {
        Type = "Pass"
        Parameters = {
          "attempts.$" = "States.MathAdd($.attempts, 1)"
          hostId       = var.host_id
        }
        Next = "ReleaseHost"
      }

      NotifySuccess = {
        Type     = "Task"
        Resource = "arn:aws:states:::http:invoke"
        Parameters = {
          ApiEndpoint    = local.slack_endpoint
          Method         = "POST"
          Authentication = { ConnectionArn = aws_cloudwatch_event_connection.slack.arn }
          Headers        = { "Content-Type" = "application/json" }
          RequestBody = {
            channel = var.slack_channel
            text    = local.release_success_text
          }
        }
        Retry = [{
          ErrorEquals     = ["States.ALL"]
          IntervalSeconds = 10
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "SlackUndelivered"
        }]
        Next = "SlackAcceptedSuccess"
      }

      SlackAcceptedSuccess = {
        Type = "Choice"
        Choices = [{
          Variable      = "$.ResponseBody.ok"
          BooleanEquals = true
          Next          = "Released"
        }]
        Default = "SlackUndelivered"
      }

      Released = { Type = "Succeed" }

      NotifyFailure = {
        Type     = "Task"
        Resource = "arn:aws:states:::http:invoke"
        Parameters = {
          ApiEndpoint    = local.slack_endpoint
          Method         = "POST"
          Authentication = { ConnectionArn = aws_cloudwatch_event_connection.slack.arn }
          Headers        = { "Content-Type" = "application/json" }
          RequestBody = {
            channel = var.slack_channel
            text    = local.release_failure_text
          }
        }
        Retry = [{
          ErrorEquals     = ["States.ALL"]
          IntervalSeconds = 10
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "SlackUndelivered"
        }]
        Next = "SlackAcceptedFailure"
      }

      SlackAcceptedFailure = {
        Type = "Choice"
        Choices = [{
          Variable      = "$.ResponseBody.ok"
          BooleanEquals = true
          Next          = "ReleaseFailed"
        }]
        Default = "SlackUndelivered"
      }

      ReleaseFailed = {
        Type  = "Fail"
        Error = "HostReleaseFailed"
        Cause = "ReleaseHosts did not release ${var.host_id}; the operator was told on Slack."
      }

      SlackUndelivered = {
        Type  = "Fail"
        Error = "SlackNotificationFailed"
        Cause = "The release outcome for ${var.host_id} could not be delivered to Slack channel ${var.slack_channel}."
      }
    }
  })
}

data "aws_caller_identity" "current" {}

# --- Slack credentials, without a Slack app --------------------------------

# EventBridge copies this value into a Secrets Manager secret it owns
# (`events!connection/<name>/...`). Nothing is asked of the operator: nyxGPT
# already reads `monitoring.slack_bot_token` from ~/.nyxGPT/config.ini, so the
# teardown creates the connection from config. Acknowledged by the owner.
resource "aws_cloudwatch_event_connection" "slack" {
  name               = "${var.name_prefix}-slack"
  description        = "nyxGPT: Slack chat.postMessage credentials for the deferred Dedicated Host release."
  authorization_type = "API_KEY"

  auth_parameters {
    api_key {
      # An API_KEY connection sends `<key>: <value>`, and the key name is
      # arbitrary -- which is what lets a Slack bearer token be carried by a
      # mechanism designed for `X-Api-Key`-style headers.
      key   = "Authorization"
      value = var.slack_authorization_header
    }
  }
}

# --- The state machine and its role ----------------------------------------

data "aws_iam_policy_document" "state_machine_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "state_machine" {
  statement {
    sid       = "ReleaseTheHost"
    actions   = ["ec2:ReleaseHosts", "ec2:DescribeHosts"]
    resources = ["*"]
  }

  # Resource "*" with a condition rather than the state machine's own ARN:
  # naming the ARN here would make the role depend on the state machine that
  # depends on the role. The condition is the real constraint -- this role can
  # POST to Slack and to nothing else.
  statement {
    sid       = "CallSlack"
    actions   = ["states:InvokeHTTPEndpoint"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "states:HTTPMethod"
      values   = ["POST"]
    }

    condition {
      test     = "StringLike"
      variable = "states:HTTPEndpoint"
      values   = ["https://slack.com/*"]
    }
  }

  statement {
    sid       = "ReadTheConnectionCredentials"
    actions   = ["events:RetrieveConnectionCredentials"]
    resources = [aws_cloudwatch_event_connection.slack.arn]
  }

  statement {
    sid       = "ReadTheManagedSecret"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_cloudwatch_event_connection.slack.secret_arn]
  }
}

resource "aws_iam_role" "state_machine" {
  name               = "${var.name_prefix}-release-sfn"
  description        = "nyxGPT: releases the deferred EC2 Mac Dedicated Host and reports to Slack."
  assume_role_policy = data.aws_iam_policy_document.state_machine_assume.json
}

resource "aws_iam_role_policy" "state_machine" {
  name   = "${var.name_prefix}-release-sfn"
  role   = aws_iam_role.state_machine.id
  policy = data.aws_iam_policy_document.state_machine.json
}

resource "aws_sfn_state_machine" "release" {
  name       = "${var.name_prefix}-release"
  role_arn   = aws_iam_role.state_machine.arn
  definition = local.release_definition

  # STANDARD, not EXPRESS: the scrub loop can wait hours, which is far past
  # the five-minute EXPRESS ceiling.
  type = "STANDARD"
}

# --- The one-shot schedule and its role ------------------------------------

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    # Confused-deputy guard: only this account's schedules may assume it.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.release.arn]
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name_prefix}-release-scheduler"
  description        = "nyxGPT: starts the deferred Dedicated Host release exactly once."
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${var.name_prefix}-release-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "release" {
  name        = "${var.name_prefix}-release"
  description = "One-shot: release nyxGPT's EC2 Mac Dedicated Host ${var.host_id} once its 24-hour minimum has closed."

  # OFF, not a flexible window: the whole point of the timestamp is that AWS
  # rejects a release before it, so firing *early* within a window would burn
  # the schedule on a call that cannot succeed.
  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "at(${var.release_at})"
  schedule_expression_timezone = "UTC"

  # Deletes itself after firing, so a completed release leaves no orphan
  # schedule behind for someone to find and wonder about.
  action_after_completion = "DELETE"

  target {
    arn      = aws_sfn_state_machine.release.arn
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      hostId   = var.host_id
      attempts = 0
    })

    # The maximums EventBridge Scheduler accepts. This covers the scheduler
    # failing to *start* the execution (throttling, a transient IAM
    # propagation error); the scrub itself is absorbed inside the state
    # machine, which is where a 200-with-Unsuccessful can actually be seen.
    retry_policy {
      maximum_event_age_in_seconds = 86400
      maximum_retry_attempts       = 185
    }
  }
}
