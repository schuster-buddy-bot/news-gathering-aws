output "api_base_url" {
  description = "Base URL of the news report API (stage v1)."
  value       = aws_api_gateway_stage.v1.invoke_url
}

output "health_url" {
  description = "Health endpoint URL."
  value       = "${aws_api_gateway_stage.v1.invoke_url}/health"
}

output "latest_report_url" {
  description = "Latest report endpoint URL (requires x-api-key)."
  value       = "${aws_api_gateway_stage.v1.invoke_url}/report/latest"
}

output "search_url" {
  description = "Semantic search endpoint URL (requires x-api-key)."
  value       = "${aws_api_gateway_stage.v1.invoke_url}/search"
}

output "api_key_ssm_name" {
  description = "SSM parameter holding the API key (SecureString)."
  value       = aws_ssm_parameter.api_key.name
}

output "s3_bucket" {
  description = "S3 bucket holding config + reports."
  value       = aws_s3_bucket.reports.bucket
}

output "pipeline_function" {
  description = "Pipeline Lambda function name."
  value       = aws_lambda_function.pipeline.function_name
}

output "api_function" {
  description = "API Lambda function name."
  value       = aws_lambda_function.api.function_name
}

output "articles_table" {
  description = "DynamoDB dedup table name."
  value       = aws_dynamodb_table.articles.name
}

output "reports_table" {
  description = "DynamoDB reports table name."
  value       = aws_dynamodb_table.reports.name
}

output "event_schedule" {
  description = "EventBridge schedule expression."
  value       = aws_cloudwatch_event_rule.daily.schedule_expression
}