variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "eu-central-1"
}

variable "project" {
  description = "Project name used as prefix for all resource names."
  type        = string
  default     = "news-pipeline"
}

variable "ollama_model" {
  description = "Ollama model used for article summarization."
  type        = string
  default     = "deepseek-v4.1-flash"
}

variable "embedding_model" {
  description = "Initial embedding provider for SSM /news-pipeline/embedding-model: 'bedrock:<model-id>' (AWS Bedrock, e.g. bedrock:amazon.titan-embed-text-v2:0), 'local-hashed[-N]' (stdlib hashing fallback) or an Ollama model for /api/embed. NOTE: live value is managed manually (ignore_changes) — updates via 'aws ssm put-parameter --overwrite'. Must match the corpus (articles' embedding_model attribute)."
  type        = string
  default     = "local-hashed-256"
}

variable "article_ttl_days" {
  description = "DynamoDB TTL for dedup (seen-article) entries."
  type        = number
  default     = 14
}

variable "report_retention_days" {
  description = "S3 lifecycle expiration for report PDFs and digest archives."
  type        = number
  default     = 14
}

variable "report_ttl_days" {
  description = "DynamoDB TTL for report metadata entries (kept longer than S3 PDFs for API history)."
  type        = number
  default     = 30
}

variable "presign_ttl_seconds" {
  description = "Validity window for presigned report download URLs."
  type        = number
  default     = 3600
}

variable "max_summarize" {
  description = "Number of top articles to AI-summarize per run."
  type        = number
  default     = 10
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
}