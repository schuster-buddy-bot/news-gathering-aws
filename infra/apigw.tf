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

resource "aws_api_gateway_integration" "get" {
  for_each = local.api_routes

  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = each.value.resource_id
  http_method             = aws_api_gateway_method.get[each.key].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = "arn:aws:apigateway:${data.aws_region.current.name}:lambda:path/2015-03-31/functions/${each.value.function.arn}/invocations"
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

# ─── Deployment + stage ──────────────────────────────────────────────────────

resource "aws_api_gateway_deployment" "api" {
  rest_api_id = aws_api_gateway_rest_api.api.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_integration.get,
      aws_api_gateway_method.get,
      aws_api_gateway_resource.report,
      aws_api_gateway_resource.latest,
      aws_api_gateway_resource.health,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.get,
    aws_api_gateway_method.get,
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