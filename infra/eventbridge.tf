# ─── Schedule ────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_event_rule" "daily" {
  name                = "${var.project}-daily-0500utc"
  description         = "Daily news pipeline run at 05:00 UTC (07:00 CEST)"
  schedule_expression = "cron(0 5 * * ? *)"
}

# ─── Dead-letter queue ───────────────────────────────────────────────────────

# Captures pipeline invocations that exhaust EventBridge's retry budget
# (e.g. Lambda throttled or down), so failed daily runs are auditable.
resource "aws_sqs_queue" "pipeline_dlq" {
  name                      = "${var.project}-dlq"
  message_retention_seconds = 1209600 # 14 days (max) — keep failed events inspectable
}

# EventBridge writes DLQ messages under its own service principal, so the
# queue's resource policy must allow it (least privilege: this rule only).
resource "aws_sqs_queue_policy" "pipeline_dlq" {
  queue_url = aws_sqs_queue.pipeline_dlq.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowEventBridgeDLQ"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.pipeline_dlq.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.daily.arn
          }
        }
      }
    ]
  })
}

# ─── Target ──────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_event_target" "pipeline" {
  rule      = aws_cloudwatch_event_rule.daily.name
  target_id = "${var.project}-daily"
  arn       = aws_lambda_function.pipeline.arn

  # Failed invocations land in the DLQ after retries are exhausted.
  dead_letter_config {
    arn = aws_sqs_queue.pipeline_dlq.arn
  }
}

resource "aws_lambda_permission" "allow_events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pipeline.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily.arn
}

# ─── Alarms ──────────────────────────────────────────────────────────────────

# Fires when the pipeline Lambda itself reports errors (pipeline failures
# surface here even when the invocation retry succeeds on a later schedule).
resource "aws_cloudwatch_metric_alarm" "pipeline_errors" {
  alarm_name          = "${var.project}-pipeline-errors"
  alarm_description   = "Pipeline Lambda reported >=1 Errors in a 5-minute period"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching" # empty periods are healthy, not failures

  dimensions = {
    FunctionName = aws_lambda_function.pipeline.function_name
  }
}