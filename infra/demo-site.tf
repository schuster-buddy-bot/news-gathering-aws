# Static demo website — public read, separate bucket
resource "aws_s3_bucket" "demo_site" {
  bucket        = "${data.aws_caller_identity.current.account_id}-${var.project}-demo"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "demo_site" {
  bucket                  = aws_s3_bucket.demo_site.id
  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

# Note: public access block must be created before policy
data "aws_iam_policy_document" "demo_public_read" {
  statement {
    sid       = "PublicReadGetObject"
    effect    = "Allow"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.demo_site.arn}/*"]
  }
}

resource "aws_s3_bucket_policy" "demo_site" {
  bucket = aws_s3_bucket.demo_site.id
  policy = data.aws_iam_policy_document.demo_public_read.json
}

resource "aws_s3_bucket_website_configuration" "demo_site" {
  bucket = aws_s3_bucket.demo_site.id

  index_document {
    suffix = "index.html"
  }

  error_document {
    key = "index.html"
  }
}

output "demo_url" {
  value       = "http://${aws_s3_bucket.demo_site.bucket}.s3-website-${data.aws_region.current.name}.amazonaws.com"
  description = "Demo UI URL"
}
