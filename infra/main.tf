terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }

  # Remote state — S3 backend with DynamoDB locking.
  # The bucket + table are created by a one-time bootstrap (see scripts/bootstrap-tfstate.sh).
  # CI and local both use this backend so state stays in sync.
  backend "s3" {
    bucket         = "000911984950-news-pipeline-tfstate"
    key            = "news-pipeline/terraform.tfstate"
    region         = "eu-central-1"
    dynamodb_table = "news-pipeline-tfstate-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(var.tags, { Project = var.project })
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}