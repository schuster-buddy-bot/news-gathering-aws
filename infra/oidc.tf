# oidc.tf — GitHub Actions OIDC federation for keyless AWS deploys
#
# Replaces static long-lived access keys (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
# in GitHub Secrets) with short-lived STS tokens assumed via OIDC.
#
# Requires the GitHub repo setting "Actions > General > Workflow permissions >
# Read and write permissions" + the OIDC provider ARN added as a custom role
# in the repo's "Add custom OIDC role" setting (or auto-discovered via the
# role's trust policy subject claim).

# ─── OIDC Provider for GitHub Actions ─────────────────────────────────────────

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4ed98bb0327c965147c08d2c5b0e06f9c4"]
}

# ─── IAM Role for GitHub Actions CI/CD ───────────────────────────────────────

data "aws_iam_policy_document" "github_actions_assume" {
  statement {
    sid     = "GitHubActionsAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Scoped to this repo — covers push, workflow_run, and dispatch events
    # New GitHub OIDC format includes org/repo IDs and environment
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:schuster-buddy-bot/news-gathering-aws:*",
        "repo:schuster-buddy-bot@*/news-gathering-aws@*:environment:aws-deploy",
      ]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = "${var.project}-github-actions-role"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume.json
  description        = "Role for GitHub Actions CI/CD - Terraform deploy, Lambda updates, S3 sync"
}

# ─── Permissions: Terraform state + infra management ─────────────────────────

data "aws_iam_policy_document" "github_actions" {
  # Terraform state — S3 backend
  statement {
    sid    = "TerraformStateS3"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
      "s3:DeleteObject",
    ]
    resources = [
      "arn:aws:s3:::000911984950-news-pipeline-tfstate",
      "arn:aws:s3:::000911984950-news-pipeline-tfstate/*",
    ]
  }

  # Terraform state lock — DynamoDB
  statement {
    sid    = "TerraformStateLock"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
    ]
    resources = [
      "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/news-pipeline-tfstate-locks",
    ]
  }

  # Lambda — update function code
  statement {
    sid    = "LambdaUpdateCode"
    effect = "Allow"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
      "lambda:GetFunction",
      "lambda:ListFunctions",
      "lambda:PublishVersion",
      "lambda:UpdateAlias",
    ]
    resources = [
      "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project}-*",
    ]
  }

  # IAM — pass role for Lambda functions
  statement {
    sid     = "IAMPassRole"
    effect  = "Allow"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.pipeline.arn,
      aws_iam_role.api.arn,
      aws_iam_role.search.arn,
    ]
  }

  # S3 — demo site sync + report bucket management
  statement {
    sid    = "S3DemoSiteSync"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.reports.arn,
      "${aws_s3_bucket.reports.arn}/*",
    ]
  }

  # API Gateway — create deployments
  statement {
    sid    = "APIGatewayDeploy"
    effect = "Allow"
    actions = [
      "apigateway:POST",
      "apigateway:GET",
      "apigateway:PATCH",
    ]
    resources = ["arn:aws:apigateway:${data.aws_region.current.name}::*"]
  }

  # CloudWatch Logs — for Lambda
  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
    ]
    resources = [
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project}-*",
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project}-*:*",
    ]
  }

  # Full Terraform apply needs broad IAM read (to inspect existing resources)
  statement {
    sid    = "IAMRead"
    effect = "Allow"
    actions = [
      "iam:GetRole",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:GetOpenIDConnectProvider",
      "iam:ListOpenIDConnectProviders",
    ]
    resources = ["*"]
  }

  # SSM — read parameters (Terraform imports these as data sources)
  statement {
    sid    = "SSMRead"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:DescribeParameters",
    ]
    resources = [
      "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/${var.project}/*",
    ]
  }

  # DynamoDB — Terraform manages these tables
  statement {
    sid    = "DynamoDBManage"
    effect = "Allow"
    actions = [
      "dynamodb:DescribeTable",
      "dynamodb:DescribeTimeToLive",
      "dynamodb:UpdateTimeToLive",
      "dynamodb:Scan",
      "dynamodb:GetItem",
    ]
    resources = [
      aws_dynamodb_table.articles.arn,
      aws_dynamodb_table.reports.arn,
    ]
  }

  # EventBridge — Terraform manages the schedule rule
  statement {
    sid    = "EventBridgeManage"
    effect = "Allow"
    actions = [
      "events:DescribeRule",
      "events:ListTargets",
      "events:PutRule",
      "events:PutTargets",
    ]
    resources = [
      "arn:aws:events:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:rule/${var.project}-*",
    ]
  }
}

resource "aws_iam_role_policy" "github_actions" {
  name   = "${var.project}-github-actions-policy"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_actions.json
}

# ─── Outputs ─────────────────────────────────────────────────────────────────

output "github_actions_role_arn" {
  value       = aws_iam_role.github_actions.arn
  description = "ARN of the IAM role for GitHub Actions OIDC. Use as role-to-assume in workflows."
}