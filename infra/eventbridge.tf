resource "aws_cloudwatch_event_rule" "daily" {
  name                = "${var.project}-daily-0500utc"
  description         = "Daily news pipeline run at 05:00 UTC (07:00 CEST)"
  schedule_expression = "cron(0 5 * * ? *)"
}

resource "aws_cloudwatch_event_target" "pipeline" {
  rule      = aws_cloudwatch_event_rule.daily.name
  target_id = "${var.project}-daily"
  arn       = aws_lambda_function.pipeline.arn
}

resource "aws_lambda_permission" "allow_events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pipeline.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily.arn
}