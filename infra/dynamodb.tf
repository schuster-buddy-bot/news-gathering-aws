# Dedup table: one row per seen article (PK = url_hash, no sort key).
# Rows carry a TTL attribute so entries expire automatically.
# Single-key schema note: BatchGetItem requires the full composite key, so a
# simple key schema keeps cross-run dedup to 2-3 API calls instead of
# per-item queries.
resource "aws_dynamodb_table" "articles" {
  name         = "${var.project}-articles"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "url_hash"

  attribute {
    name = "url_hash"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# Report metadata: one item per day (PK = date YYYY-MM-DD).
resource "aws_dynamodb_table" "reports" {
  name         = "${var.project}-reports"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "date"

  attribute {
    name = "date"
    type = "S"
  }
}