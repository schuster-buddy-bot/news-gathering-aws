resource "aws_s3_bucket" "reports" {
  bucket        = "${data.aws_caller_identity.current.account_id}-${var.project}-reports"
  force_destroy = true # dev convenience: allow clean teardown
}

resource "aws_s3_bucket_public_access_block" "reports" {
  bucket                  = aws_s3_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "reports" {
  bucket = aws_s3_bucket.reports.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "reports" {
  bucket = aws_s3_bucket.reports.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id

  # Reports: expire after retention period (14 days default)
  rule {
    id     = "expire-old-reports"
    status = "Enabled"

    filter {
      prefix = "reports/"
    }

    expiration {
      days = var.report_retention_days
    }
  }

  # Article archive: expire after retention period
  rule {
    id     = "expire-old-archive"
    status = "Enabled"

    filter {
      prefix = "archive/"
    }

    expiration {
      days = var.report_retention_days
    }
  }

  # Config: never expire (sources.json, filters.json, interests.json)
  # No rule = no expiration — config/ persists indefinitely
}