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

variable "article_ttl_days" {
  description = "DynamoDB TTL for dedup (seen-article) entries."
  type        = number
  default     = 90
}

variable "report_retention_days" {
  description = "S3 lifecycle expiration for report PDFs and digest archives."
  type        = number
  default     = 90
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