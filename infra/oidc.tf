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

    # Scoped to this repo's main branch + aws-deploy environment
    # New GitHub OIDC format includes org/repo IDs and environment
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:schuster-buddy-bot/news-gathering-aws:ref:refs/heads/main",
        "repo:schuster-buddy-bot@304074075/news-gathering-aws@1389742109:environment:aws-deploy",
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

  # Lambda — full management for terraform apply
  statement {
    sid    = "LambdaManage"
    effect = "Allow"
    actions = [
      "lambda:*",
    ]
    resources = [
      "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project}-*",
    ]
  }

  # IAM — pass role + manage role policies for Lambda functions
  statement {
    sid    = "IAMManage"
    effect = "Allow"
    actions = [
      "iam:PassRole",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetOpenIDConnectProvider",
      "iam:ListOpenIDConnectProviders",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
      "iam:TagRole",
      "iam:UntagRole",
    ]
    # NOTE: github_actions role intentionally excluded — prevents
    # self-modification / privilege escalation (critic finding 2026-10-08)
    resources = [
      aws_iam_role.pipeline.arn,
      aws_iam_role.api.arn,
      aws_iam_role.search.arn,
      aws_iam_role.demo.arn,
      aws_iam_role.apigw_cloudwatch.arn,
    ]
  }

  # IAM — broad read (Terraform needs to inspect existing resources)
  statement {
    sid    = "IAMRead"
    effect = "Allow"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:GetOpenIDConnectProvider",
      "iam:ListOpenIDConnectProviders",
    ]
    resources = ["*"]
  }

  # S3 — all project buckets (reports, demo, tfstate)
  statement {
    sid    = "S3Manage"
    effect = "Allow"
    actions = [
      "s3:*",
    ]
    resources = [
      aws_s3_bucket.reports.arn,
      "${aws_s3_bucket.reports.arn}/*",
      "arn:aws:s3:::000911984950-news-pipeline-demo",
      "arn:aws:s3:::000911984950-news-pipeline-demo/*",
    ]
  }

  # API Gateway — management scoped to project REST API only
  statement {
    sid    = "APIGatewayManage"
    effect = "Allow"
    actions = [
      "apigateway:*",
    ]
    resources = [
      "arn:aws:apigateway:${data.aws_region.current.name}::/restapis/${aws_api_gateway_rest_api.api.id}",
      "arn:aws:apigateway:${data.aws_region.current.name}::/restapis/${aws_api_gateway_rest_api.api.id}/*",
    ]
  }

  # API Gateway — read access for Terraform state (api key + account)
  # Scoped to project API key only; account read is account-wide by AWS design
  statement {
    sid    = "APIGatewayRead"
    effect = "Allow"
    actions = [
      "apigateway:GET",
    ]
    resources = [
      "arn:aws:apigateway:${data.aws_region.current.name}::/apikeys/${aws_api_gateway_api_key.demo.id}",
      "arn:aws:apigateway:${data.aws_region.current.name}::/account",
    ]
  }

  # CloudWatch Logs — full management for project log groups
  statement {
    sid    = "CloudWatchLogsManage"
    effect = "Allow"
    actions = [
      "logs:*",
    ]
    resources = [
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project}-*",
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project}-*:*",
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/apigw/${var.project}-*",
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/apigw/${var.project}-*:*",
    ]
  }

  # CloudWatch Logs — describe (needs * resource)
  statement {
    sid       = "CloudWatchLogsDescribe"
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  # SSM — read + update project parameters
  statement {
    sid    = "SSMManage"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:PutParameter",
    ]
    resources = [
      "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/${var.project}/*",
    ]
  }

  # SSM — DescribeParameters (AWS requires * resource, cannot be scoped)
  statement {
    sid       = "SSMDescribe"
    effect    = "Allow"
    actions   = ["ssm:DescribeParameters"]
    resources = ["*"]
  }

  # SQS — manage DLQ
  statement {
    sid    = "SQSManage"
    effect = "Allow"
    actions = [
      "sqs:*",
    ]
    resources = [
      "arn:aws:sqs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:${var.project}-*",
    ]
  }

  # Tag reading — Terraform reads tags on all managed resources during plan
  # ListTagsForResource is needed across services; AWS doesn't support
  # resource-level scoping for many of these list operations
  statement {
    sid    = "TagRead"
    effect = "Allow"
    actions = [
      "ssm:ListTagsForResource",
      "sqs:ListQueueTags",
      "lambda:ListTags",
      "dynamodb:ListTagsOfResource",
      # NOTE: apigateway:GET excluded — it grants read access to ALL API keys
      # in the account (including plaintext key values). Tag reading for API
      # Gateway is covered by the scoped APIGatewayManage statement above.
      "events:ListTagsForResource",
      "cloudwatch:ListTagsForResource",
    ]
    resources = ["*"]
  }

  # DynamoDB — full management for project tables
  statement {
    sid    = "DynamoDBManage"
    effect = "Allow"
    actions = [
      "dynamodb:*",
    ]
    resources = [
      aws_dynamodb_table.articles.arn,
      aws_dynamodb_table.reports.arn,
    ]
  }

  # EventBridge — manage the schedule rule
  statement {
    sid    = "EventBridgeManage"
    effect = "Allow"
    actions = [
      "events:*",
    ]
    resources = [
      "arn:aws:events:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:rule/${var.project}-*",
    ]
  }

  # CloudWatch alarms — Terraform manages project alarms
  statement {
    sid    = "CloudWatchAlarms"
    effect = "Allow"
    actions = [
      "cloudwatch:DescribeAlarms",
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:DeleteAlarms",
    ]
    resources = [
      "arn:aws:cloudwatch:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:alarm:${var.project}-*",
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