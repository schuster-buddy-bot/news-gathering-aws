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
      resource_id     = aws_api_gateway_rest_api.api.root_resource_id
      path            = "/"
      function        = aws_lambda_function.api
    }
    health = {
      resource_id     = aws_api_gateway_resource.health.id
      path            = "/health"
      function        = aws_lambda_function.api
    }
    latest = {
      resource_id     = aws_api_gateway_resource.latest.id
      path            = "/report/latest"
      function        = aws_lambda_function.api
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
  ]
}

resource "aws_api_gateway_stage" "v1" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  deployment_id = aws_api_gateway_deployment.api.id
  stage_name    = "v1"

  tags = {
    Project = var.project
  }
}