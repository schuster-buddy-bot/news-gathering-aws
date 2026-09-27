# ─── Lambda deployment packages ──────────────────────────────────────────────
# Run ../build.sh first: it populates build/pipeline and build/api.

data "archive_file" "pipeline" {
  type        = "zip"
  source_dir  = "${path.module}/../build/pipeline"
  output_path = "${path.module}/../build/news-pipeline.zip"
}

data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../build/api"
  output_path = "${path.module}/../build/news-api.zip"
}

# ─── Pipeline function ───────────────────────────────────────────────────────

resource "aws_lambda_function" "pipeline" {
  function_name    = "${var.project}-pipeline"
  role             = aws_iam_role.pipeline.arn
  handler          = "lambda_handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 900
  memory_size      = 512
  filename         = data.archive_file.pipeline.output_path
  source_code_hash = data.archive_file.pipeline.output_base64sha256

  environment {
    variables = {
      CONFIG_BUCKET             = aws_s3_bucket.reports.bucket
      ARTICLES_TABLE            = aws_dynamodb_table.articles.name
      REPORTS_TABLE             = aws_dynamodb_table.reports.name
      OLLAMA_ENDPOINT           = "https://ollama.com/api/chat"
      SSM_API_KEY_PARAM         = aws_ssm_parameter.ollama_api_key.name
      SSM_MODEL_PARAM           = aws_ssm_parameter.ollama_model.name
      SSM_EMBEDDING_MODEL_PARAM = aws_ssm_parameter.embedding_model.name
      EMBEDDING_ENABLED         = "true"
      MAX_SUMMARIZE             = tostring(var.max_summarize)
      ARTICLE_TTL_DAYS          = tostring(var.article_ttl_days)
      REPORTS_TTL_DAYS          = tostring(var.report_ttl_days)
      LOG_LEVEL                 = "INFO"
    }
  }

  depends_on = [aws_iam_role_policy.pipeline_logs]
}

# ─── API function ────────────────────────────────────────────────────────────

resource "aws_lambda_function" "api" {
  function_name    = "${var.project}-api"
  role             = aws_iam_role.api.arn
  handler          = "api_handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 10
  memory_size      = 256
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  environment {
    variables = {
      REPORTS_TABLE       = aws_dynamodb_table.reports.name
      CONFIG_BUCKET       = aws_s3_bucket.reports.bucket
      PRESIGN_TTL_SECONDS = tostring(var.presign_ttl_seconds)
      LOG_LEVEL           = "INFO"
    }
  }

  depends_on = [aws_iam_role_policy.api_logs]
}

# ─── Search function (GET /search?q=...) ──────────────────────────────
# Shares the API deployment ZIP (api_handler.py + search_handler.py + embeddings.py).

resource "aws_lambda_function" "search" {
  function_name    = "${var.project}-search"
  role             = aws_iam_role.search.arn
  handler          = "search_handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 256
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  environment {
    variables = {
      ARTICLES_TABLE            = aws_dynamodb_table.articles.name
      SSM_EMBEDDING_MODEL_PARAM = aws_ssm_parameter.embedding_model.name
      LOG_LEVEL                 = "INFO"
    }
  }

  depends_on = [aws_iam_role_policy.search_logs]
}

# ─── Log groups (explicit retention, created before first invoke) ────────────

resource "aws_cloudwatch_log_group" "pipeline" {
  name              = "/aws/lambda/${aws_lambda_function.pipeline.function_name}"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${aws_lambda_function.api.function_name}"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "search" {
  name              = "/aws/lambda/${aws_lambda_function.search.function_name}"
  retention_in_days = 14
}