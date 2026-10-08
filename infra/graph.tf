# GraphRAG knowledge graph storage: DynamoDB adjacency-list table.
#
# Adjacency-list pattern:
#   PK = NODE#<normalized_entity>
#   SK = META#                              (entity metadata)
#   SK = EDGE#<target>#<relation>           (outgoing edge)
#
# Reverse traversal uses GSI1:
#   GSI1PK = NODE#<target>
#   GSI1SK = EDGE#<source>#<relation>
#
# TTL attribute auto-expires items to keep the graph in sync with the
# article retention window (30 days).  PAY_PER_REQUEST keeps the table at
# $0/month when idle.
resource "aws_dynamodb_table" "graph" {
  name         = "${var.project}-graph"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "GSI1PK"
    type = "S"
  }

  attribute {
    name = "GSI1SK"
    type = "S"
  }

  global_secondary_index {
    name            = "GSI1"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}
