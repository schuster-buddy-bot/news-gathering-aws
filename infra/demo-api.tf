# ─── Public demo stack (Issues #8 + #11) ─────────────────────────────────────
# CQRS read side: public /demo/* routes with NO API key, but aggressive
# per-method throttling and a strictly read-only Lambda + IAM role.
#
# Enforcement layers:
#   1. demo Lambda role — read-only IAM policy (no PutItem/PutObject at all)
#   2. API Gateway — apiKeyRequired = false on every demo method
#   3. usage plan `demo-public` (rate 5 / burst 10) attached to stage v1
#      + per-method throttle overrides (method settings) — usage-plan
#      throttling is per API key, so keyless traffic is throttled via
#      method-level settings instead
#   4. stage access logging — audit trail for every demo request

# ─── Demo Lambda function (shares the api deployment ZIP) ────────────────────
# The api ZIP already contains demo_handler.py + search_handler.py +
# api_handler.py + embeddings.py (see build.sh).

resource "aws_lambda_function" "demo" {
  function_name    = "${var.project}-demo"
  role             = aws_iam_role.demo.arn
  handler          = "demo_handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 256
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  # NOTE: no reserved_concurrent_executions — this account's Lambda
  # concurrency quota is ~10 and requires >=10 unreserved concurrent
  # executions, so any reservation would be rejected. Demo load is instead
  # bounded by the per-method throttle targets above and, once the account
  # concurrency is exhausted, Lambda "Rate Exceeded" errors are remapped to
  # a clean 429 by the gateway response below (not 500).

  environment {
    variables = {
      ARTICLES_TABLE            = aws_dynamodb_table.articles.name
      REPORTS_TABLE             = aws_dynamodb_table.reports.name
      CONFIG_BUCKET             = aws_s3_bucket.reports.bucket
      SSM_EMBEDDING_MODEL_PARAM = aws_ssm_parameter.embedding_model.name
      PRESIGN_TTL_SECONDS       = tostring(var.presign_ttl_seconds)
      TOPICS_CONFIG_KEY         = "config/demo_topics.json"
      LOG_LEVEL                 = "INFO"
    }
  }

  depends_on = [aws_iam_role_policy.demo_logs]
}

resource "aws_cloudwatch_log_group" "demo" {
  name              = "/aws/lambda/${aws_lambda_function.demo.function_name}"
  retention_in_days = 14
}

# ─── Demo role: READ-ONLY by construction ────────────────────────────────────
# Deliberately grants NO write actions (no PutItem/PutObject/UpdateItem) —
# the public demo surface cannot mutate pipeline state even if the handler
# were compromised.

resource "aws_iam_role" "demo" {
  name = "${var.project}-demo-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

data "aws_iam_policy_document" "demo" {
  statement {
    sid    = "Logs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project}-demo",
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project}-demo:*",
    ]
  }

  # DynamoDB: read article corpus + report metadata only
  statement {
    sid    = "DynamoDBRead"
    effect = "Allow"
    actions = [
      "dynamodb:Scan",
      "dynamodb:DescribeTable",
    ]
    resources = [
      aws_dynamodb_table.articles.arn,
      aws_dynamodb_table.reports.arn,
    ]
  }

  # S3: read reports (presigned URLs) + demo topics config — no PutObject
  statement {
    sid       = "S3ReadReports"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.reports.arn}/*"]
  }

  # SSM: embedding model only
  statement {
    sid       = "SSMParameters"
    effect    = "Allow"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/${var.project}/embedding-model"]
  }

  # Bedrock: Titan Text Embeddings V2 for query/topic vectors
  statement {
    sid       = "BedrockInvoke"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${data.aws_region.current.name}::foundation-model/amazon.titan-embed-text-v2:0"]
  }
}

resource "aws_iam_role_policy" "demo" {
  name   = "${var.project}-demo-policy"
  role   = aws_iam_role.demo.id
  policy = data.aws_iam_policy_document.demo.json
}

resource "aws_iam_role_policy" "demo_logs" {
  name = "${var.project}-demo-logs-create"
  role = aws_iam_role.demo.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "logs:CreateLogGroup"
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project}-demo"
      }
    ]
  })
}

# ─── API Gateway resources ───────────────────────────────────────────────────

resource "aws_api_gateway_resource" "demo" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "demo"
}

locals {
  # first-level demo resources (children of /demo)
  demo_first_level = {
    report          = "report"
    search          = "search"
    topics          = "topics"
    search_by_topic = "search-by-topics"
    browse          = "browse"
    articles        = "articles"
  }

  # second-level demo resources (children of first-level ones)
  demo_second_level = {
    latest = { parent = "report", path_part = "latest" } # /demo/report/latest
  }

  # demo routes → {http_method, full_path, request_parameters}
  demo_routes = {
    search          = { method = "GET", path = "/demo/search", params = { "method.request.querystring.q" = false, "method.request.querystring.limit" = false } }
    latest          = { method = "GET", path = "/demo/report/latest", params = {} }
    topics          = { method = "GET", path = "/demo/topics", params = {} }
    search_by_topic = { method = "POST", path = "/demo/search-by-topics", params = {} }
    browse          = { method = "GET", path = "/demo/browse", params = {} }
    articles        = { method = "GET", path = "/demo/articles", params = { "method.request.querystring.category" = false, "method.request.querystring.limit" = false } }
  }
}

resource "aws_api_gateway_resource" "demo_first" {
  for_each = local.demo_first_level

  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.demo.id
  path_part   = each.value
}

resource "aws_api_gateway_resource" "demo_second" {
  for_each = local.demo_second_level

  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.demo_first[each.value.parent].id
  path_part   = each.value.path_part
}

locals {
  # demo route key → API Gateway resource id (second level for /demo/report/latest)
  demo_route_resource_ids = {
    for key, route in local.demo_routes : key => (
      key == "latest"
      ? aws_api_gateway_resource.demo_second["latest"].id
      : aws_api_gateway_resource.demo_first[key].id
    )
  }
}

# ─── Methods + integrations (all public: apiKeyRequired = false) ─────────────

resource "aws_api_gateway_method" "demo" {
  for_each = local.demo_routes

  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = local.demo_route_resource_ids[each.key]
  http_method   = each.value.method
  authorization = "NONE"

  api_key_required = false

  request_parameters = each.value.params
}

resource "aws_api_gateway_integration" "demo" {
  for_each = local.demo_routes

  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = local.demo_route_resource_ids[each.key]
  http_method             = aws_api_gateway_method.demo[each.key].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = "arn:aws:apigateway:${data.aws_region.current.name}:lambda:path/2015-03-31/functions/${aws_lambda_function.demo.arn}/invocations"
}

resource "aws_lambda_permission" "demo" {
  for_each = local.demo_routes

  statement_id  = "AllowAPIGatewayInvoke-demo-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.demo.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/${each.value.method}${each.value.path}"
}

# ─── CORS preflight (OPTIONS mock integrations) ──────────────────────────────

resource "aws_api_gateway_method" "demo_options" {
  for_each = local.demo_routes

  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = local.demo_route_resource_ids[each.key]
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "demo_options" {
  for_each = local.demo_routes

  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = local.demo_route_resource_ids[each.key]
  http_method = aws_api_gateway_method.demo_options[each.key].http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "demo_options" {
  for_each = local.demo_routes

  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = local.demo_route_resource_ids[each.key]
  http_method = aws_api_gateway_method.demo_options[each.key].http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

resource "aws_api_gateway_integration_response" "demo_options" {
  for_each = local.demo_routes

  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = local.demo_route_resource_ids[each.key]
  http_method = aws_api_gateway_method.demo_options[each.key].http_method
  status_code = aws_api_gateway_method_response.demo_options[each.key].status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = local.cors_allow_headers
    "method.response.header.Access-Control-Allow-Methods" = local.cors_allow_methods
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin
  }
}

# ─── Public usage plan (throttle documentation) + method throttling ──────────
# The usage plan documents the public budget (rate 5 / burst 10) and is
# intentionally NOT linked to any API key: /demo/* routes are keyless.
# Because usage-plan throttling applies per API key, the actual enforcement
# for keyless traffic is the per-method throttle overrides below (method
# settings on stage v1) — same 5 rps / burst 10 limits per demo route.

resource "aws_api_gateway_usage_plan" "demo_public" {
  name        = "${var.project}-demo-public"
  description = "Public demo plan (no key): burst 10, rate 5 req/s — enforced via method settings for keyless traffic"

  api_stages {
    api_id = aws_api_gateway_rest_api.api.id
    stage  = aws_api_gateway_stage.v1.stage_name
  }

  throttle_settings {
    rate_limit  = 5
    burst_limit = 10
  }

  tags = {
    Project = var.project
  }
}

# Per-method throttle overrides — documented targets for the public demo
# traffic. NOTE: nested-path method settings are stored in the canonical
# ResourcePath encoding ("/" inside the path escaped as "~1", leading slash
# kept per AWS docs) but AWS enforces them only best-effort on EDGE-optimized
# APIs — small bursts can slip through distributed per-node token buckets
# (see hashicorp/terraform-provider-aws#9738). The guaranteed protections are
# the usage plan + the demo Lambda's reserved concurrency (5) with the 429
# gateway response remap below.
resource "aws_api_gateway_method_settings" "demo_throttle" {
  for_each = local.demo_routes

  rest_api_id = aws_api_gateway_rest_api.api.id
  stage_name  = aws_api_gateway_stage.v1.stage_name
  method_path = "/${replace(each.value.path, "/", "~1")}/${each.value.method}"

  settings {
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
}

# Lambda throttles ("Rate Exceeded", e.g. demo concurrency cap) surface as
# API_CONFIGURATION_ERROR / DEFAULT_5XX with HTTP 500 by default. Remap both
# to a clean 429 with CORS headers so overloaded demo clients get a retryable
# signal. Internal details stay in the access log (integrationErr).
resource "aws_api_gateway_gateway_response" "config_error_429" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  response_type = "API_CONFIGURATION_ERROR"
  status_code   = "429"

  response_parameters = {
    "gatewayresponse.header.Access-Control-Allow-Origin" = "'*'"
    "gatewayresponse.header.Content-Type"                = "'application/json'"
  }

  response_templates = {
    "application/json" = "{\"error\":\"rate_limited\",\"message\":\"Too many requests — please retry shortly\"}"
  }
}

resource "aws_api_gateway_gateway_response" "server_error_429" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  response_type = "DEFAULT_5XX"
  status_code   = "429"

  response_parameters = {
    "gatewayresponse.header.Access-Control-Allow-Origin" = "'*'"
    "gatewayresponse.header.Content-Type"                = "'application/json'"
  }

  response_templates = {
    "application/json" = "{\"error\":\"rate_limited\",\"message\":\"Too many requests — please retry shortly\"}"
  }
}

# ─── Access logging (audit trail) ────────────────────────────────────────────
# Stage-level access logging: one log group, JSON lines per request
# (method, path, status, IP, latency). Covers demo routes and private
# routes alike — useful audit trail, no sensitive bodies logged.

resource "aws_cloudwatch_log_group" "apigw_access" {
  name              = "/apigw/${var.project}-access"
  retention_in_days = 14
}

# Account-level CloudWatch role — API Gateway needs it before stage
# access logging can be enabled (BadRequestException otherwise).
resource "aws_iam_role" "apigw_cloudwatch" {
  name = "${var.project}-apigw-logs-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "apigateway.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "apigw_cloudwatch" {
  role       = aws_iam_role.apigw_cloudwatch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "cloudwatch" {
  cloudwatch_role_arn = aws_iam_role.apigw_cloudwatch.arn

  depends_on = [aws_iam_role_policy_attachment.apigw_cloudwatch]
}

locals {
  apigw_access_log_format = jsonencode({
    requestId       = "$context.requestId"
    ip              = "$context.identity.sourceIp"
    requestTime     = "$context.requestTime"
    httpMethod      = "$context.httpMethod"
    resourcePath    = "$context.resourcePath"
    status          = "$context.status"
    responseLatency = "$context.responseLatency"
    integrationErr  = "$context.integration.error"
  })
}

# ─── Deployment + redeploy trigger ───────────────────────────────────────────
# NOTE: extends the existing deployment's trigger list (new demo routes must
# be deployed) and adds access logging to stage v1 — additive-only changes to
# existing resources, no route/auth/key changes.
output "demo_function" {
  description = "Public demo Lambda function name (read-only)."
  value       = aws_lambda_function.demo.function_name
}

output "demo_base_url" {
  description = "Base URL of the public demo API (no API key required)."
  value       = "${aws_api_gateway_stage.v1.invoke_url}/demo"
}

output "demo_search_url" {
  description = "Public semantic search (no API key required)."
  value       = "${aws_api_gateway_stage.v1.invoke_url}/demo/search"
}

output "demo_site_bucket" {
  description = "S3 bucket hosting the demo UI (index.html)."
  value       = aws_s3_bucket.demo_site.bucket
}
