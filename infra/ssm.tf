# Secrets are managed via SSM Parameter Store.
#
# The API key parameter is created with a placeholder and then set manually
# (aws ssm put-parameter --overwrite). `ignore_changes = [value]` keeps
# Terraform from clobbering the real key on the next apply.
resource "aws_ssm_parameter" "ollama_api_key" {
  name  = "/${var.project}/ollama-api-key"
  type  = "SecureString"
  value = "PLACEHOLDER_SET_BY_MASTER"

  lifecycle {
    ignore_changes = [value]
  }
}

# Non-secret: model name is managed by Terraform.
resource "aws_ssm_parameter" "ollama_model" {
  name  = "/${var.project}/ollama-model"
  type  = "String"
  value = var.ollama_model
}