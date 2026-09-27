resource "aws_api_gateway_rest_api" "api" {
  name        = "${var.project}-api"
  description = "News pipeline report API"
}

# ─── Resources ───────────────────────────────────────────────────────────────

resource "aws_api_gateway_resource" "report" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "report"
}

resource "aws_api_gateway_resource" "latest" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.report.id
  path_part   = "latest"
}

resource "aws_api_gateway_resource" "health" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "health"
}

resource "aws_api_gateway_resource" "search" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "search"
}

# ─── Methods + AWS_PROXY integrations ────────────────────────────────────────

locals {
  api_routes = {
    root = {
      resource_id      = aws_api_gateway_rest_api.api.root_resource_id
      path             = "/"
      function         = aws_lambda_function.api
      api_key_required = false
    }
    health = {
      resource_id      = aws_api_gateway_resource.health.id
      path             = "/health"
      function         = aws_lambda_function.api
      api_key_required = false
    }
    latest = {
      resource_id      = aws_api_gateway_resource.latest.id
      path             = "/report/latest"
      function         = aws_lambda_function.api
      api_key_required = true
    }
  }
}

# /search gets a dedicated method (declared querystring params) + integration
# instead of the shared for_each map — keeps the proxy route self-documenting.

resource "aws_api_gateway_method" "get" {
  for_each = local.api_routes

  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = each.value.resource_id
  http_method   = "GET"
  authorization = "NONE"

  api_key_required = each.value.api_key_required

  request_parameters = {
    "method.request.header.Accept" = false
  }
}

# Query-string passthrough for /search (q, limit) — REST proxy forwards the
# full query string, but declare intent explicitly for documentation value.
resource "aws_api_gateway_method" "search_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.search.id
  http_method   = "GET"
  authorization = "NONE"

  api_key_required = true

  request_parameters = {
    "method.request.querystring.q"     = false
    "method.request.querystring.limit" = false
  }
}

resource "aws_api_gateway_integration" "get" {
  for_each = local.api_routes

  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = each.value.resource_id
  http_method             = aws_api_gateway_method.get[each.key].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = "arn:aws:apigateway:${data.aws_region.current.name}:lambda:path/2015-03-31/functions/${each.value.function.arn}/invocations"
}

resource "aws_api_gateway_integration" "search_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.search.id
  http_method             = aws_api_gateway_method.search_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = "arn:aws:apigateway:${data.aws_region.current.name}:lambda:path/2015-03-31/functions/${aws_lambda_function.search.arn}/invocations"
}

# ─── Lambda permissions for API Gateway ──────────────────────────────────────

resource "aws_lambda_permission" "apigw" {
  for_each = local.api_routes

  statement_id  = "AllowAPIGatewayInvoke-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = each.value.function.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/GET${each.value.path == "/" ? "/" : each.value.path}"
}

resource "aws_lambda_permission" "apigw_search" {
  statement_id  = "AllowAPIGatewayInvoke-search"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.search.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/GET/search"
}

# ─── CORS preflight (OPTIONS mock integrations) ──────────────────────────────
# Browsers send a preflight OPTIONS before cross-origin GETs that carry
# x-api-key. Mock integrations answer 200 + CORS headers without a Lambda
# round-trip. Covers every route the demo UI calls, including root.

locals {
  cors_routes = {
    root   = { resource_id = aws_api_gateway_rest_api.api.root_resource_id }
    health = { resource_id = aws_api_gateway_resource.health.id }
    latest = { resource_id = aws_api_gateway_resource.latest.id }
    search = { resource_id = aws_api_gateway_resource.search.id }
  }

  cors_allow_headers = "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,x-api-key'"
  cors_allow_methods = "'GET,POST,OPTIONS'"
  cors_allow_origin  = "'*'"
}

resource "aws_api_gateway_method" "options" {
  for_each = local.cors_routes

  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = each.value.resource_id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "options" {
  for_each = local.cors_routes

  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = each.value.resource_id
  http_method = aws_api_gateway_method.options[each.key].http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "options" {
  for_each = local.cors_routes

  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = each.value.resource_id
  http_method = aws_api_gateway_method.options[each.key].http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

resource "aws_api_gateway_integration_response" "options" {
  for_each = local.cors_routes

  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = each.value.resource_id
  http_method = aws_api_gateway_method.options[each.key].http_method
  status_code = aws_api_gateway_method_response.options[each.key].status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = local.cors_allow_headers
    "method.response.header.Access-Control-Allow-Methods" = local.cors_allow_methods
    "method.response.header.Access-Control-Allow-Origin"  = local.cors_allow_origin
  }
}

# ─── Deployment + stage ──────────────────────────────────────────────────────

resource "aws_api_gateway_deployment" "api" {
  rest_api_id = aws_api_gateway_rest_api.api.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_integration.get,
      aws_api_gateway_method.get,
      aws_api_gateway_integration.search_get,
      aws_api_gateway_method.search_get,
      aws_api_gateway_resource.report,
      aws_api_gateway_resource.latest,
      aws_api_gateway_resource.health,
      aws_api_gateway_resource.search,
      aws_api_gateway_integration.options,
      aws_api_gateway_method.options,
      aws_api_gateway_method_response.options,
      aws_api_gateway_integration_response.options,
      aws_api_gateway_resource.demo,
      aws_api_gateway_resource.demo_first,
      aws_api_gateway_resource.demo_second,
      aws_api_gateway_method.demo,
      aws_api_gateway_integration.demo,
      aws_api_gateway_method.demo_options,
      aws_api_gateway_integration.demo_options,
      aws_api_gateway_method_response.demo_options,
      aws_api_gateway_integration_response.demo_options,
      aws_api_gateway_gateway_response.config_error_429,
      aws_api_gateway_gateway_response.server_error_429,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.get,
    aws_api_gateway_method.get,
    aws_api_gateway_integration.search_get,
    aws_api_gateway_method.search_get,
    aws_api_gateway_integration_response.options,
    aws_api_gateway_integration.demo,
    aws_api_gateway_integration_response.demo_options,
    aws_api_gateway_gateway_response.config_error_429,
    aws_api_gateway_gateway_response.server_error_429,
  ]
}

resource "aws_api_gateway_stage" "v1" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  deployment_id = aws_api_gateway_deployment.api.id
  stage_name    = "v1"

  # Access logging (audit trail — see demo-api.tf log format)
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw_access.arn
    format          = local.apigw_access_log_format
  }

  # account-level CloudWatch role must exist before logging is enabled
  depends_on = [aws_api_gateway_account.cloudwatch]

  tags = {
    Project = var.project
  }
}

# ─── API key + usage plan (Issue #5) ────────────────────────────────
# "/search" and "/report/latest" require x-api-key; "/health" and "/" stay public.

resource "aws_api_gateway_api_key" "demo" {
  name    = "${var.project}-demo-key"
  enabled = true
}

resource "aws_api_gateway_usage_plan" "main" {
  name        = "${var.project}-usage-plan"
  description = "100 req/min per key: burst bucket 100, sustained 2 rps (=120/min)"

  api_stages {
    api_id = aws_api_gateway_rest_api.api.id
    stage  = aws_api_gateway_stage.v1.stage_name
  }

  throttle_settings {
    rate_limit  = 2
    burst_limit = 100
  }

  tags = {
    Project = var.project
  }
}

resource "aws_api_gateway_usage_plan_key" "demo" {
  usage_plan_id = aws_api_gateway_usage_plan.main.id
  key_id        = aws_api_gateway_api_key.demo.id
  key_type      = "API_KEY"
}