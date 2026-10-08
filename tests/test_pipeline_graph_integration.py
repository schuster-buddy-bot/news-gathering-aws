"""test_pipeline_graph_integration.py — E2E pipeline + graph integration.

Runs the pipeline with mocked RSS feeds, a fake LLM, and a moto-backed
DynamoDB/S3.  Verifies that extracted triplets end up in the graph table
and that pipeline metrics reflect ERE success.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from ere import ArticleExtraction, Triplet


@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    """Ensure env vars needed at import time are set."""
    bucket = "test-config-bucket"
    monkeypatch.setenv("CONFIG_BUCKET", bucket)
    monkeypatch.setenv("ARTICLES_TABLE", "test-articles")
    monkeypatch.setenv("REPORTS_TABLE", "test-reports")
    monkeypatch.setenv("GRAPH_TABLE", "test-graph")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("OLLAMA_ENDPOINT", "https://ollama.example.com/api/chat")
    monkeypatch.setenv("SSM_API_KEY_PARAM", "/news-pipeline/ollama-api-key")
    monkeypatch.setenv("SSM_MODEL_PARAM", "/news-pipeline/ollama-model")
    return bucket


@pytest.fixture
def fake_llm():
    """Return a LangChain-like fake whose with_structured_output returns
    a deterministic extraction for every article."""
    extraction = ArticleExtraction(
        summary="Apple and TSMC expand chip partnership.",
        triplets=[
            Triplet(
                subject="Apple", subject_type="org", relation="SUPPLIES",
                object="TSMC", object_type="org",
            ),
            Triplet(
                subject="TSMC", subject_type="org", relation="LOCATED_IN",
                object="Taiwan", object_type="place",
            ),
        ],
    )

    class StructuredFake:
        def __init__(self, extraction):
            self.extraction = extraction
            self.call_count = 0

        def invoke(self, prompt):
            return {"parsed": self.extraction, "raw": self.extraction.model_dump()}

    class LLMFake:
        def with_structured_output(self, schema, **kwargs):
            return StructuredFake(extraction)

    return LLMFake()


@pytest.fixture
def run_with_mocks(env_setup, fake_llm):
    """Yield a helper that boots moto, mocks SSM/S3/RSS, and runs the pipeline."""

    def _run(sources_json, filters_json):
        # Import pipeline modules INSIDE the helper so env vars set by the
        # autouse fixture are respected at import time.
        import graph_store
        import lambda_handler
        graph_store.GRAPH_TABLE_NAME = os.environ["GRAPH_TABLE"]
        graph_store._table = None
        with mock_aws():
            os.environ["AWS_DEFAULT_REGION"] = "eu-central-1"
            bucket = os.environ["CONFIG_BUCKET"]
            # Rebuild boto3 clients inside moto context.
            s3 = boto3.client("s3", region_name="eu-central-1")
            ssm = boto3.client("ssm", region_name="eu-central-1")
            ddb = boto3.client("dynamodb", region_name="eu-central-1")

            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
            )
            s3.put_object(
                Bucket=bucket, Key="config/sources.json",
                Body=json.dumps(sources_json).encode(),
            )
            s3.put_object(
                Bucket=bucket, Key="config/filters.json",
                Body=json.dumps(filters_json).encode(),
            )

            # Tables match Terraform schema.
            for name in (os.environ["ARTICLES_TABLE"], os.environ["REPORTS_TABLE"], os.environ["GRAPH_TABLE"]):
                is_graph = name == os.environ["GRAPH_TABLE"]
                hash_key = "PK" if is_graph else "url_hash" if name == os.environ["ARTICLES_TABLE"] else "date"
                attrs = [
                    {"AttributeName": hash_key, "AttributeType": "S"},
                ]
                keys = [
                    {"AttributeName": hash_key, "KeyType": "HASH"},
                ]
                if is_graph:
                    attrs.extend([
                        {"AttributeName": "SK", "AttributeType": "S"},
                        {"AttributeName": "GSI1PK", "AttributeType": "S"},
                        {"AttributeName": "GSI1SK", "AttributeType": "S"},
                    ])
                    keys.append({"AttributeName": "SK", "KeyType": "RANGE"})
                    gsi = [{
                        "IndexName": "GSI1",
                        "KeySchema": [
                            {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                            {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    }]
                else:
                    gsi = None

                kwargs = {
                    "TableName": name,
                    "BillingMode": "PAY_PER_REQUEST",
                    "AttributeDefinitions": attrs,
                    "KeySchema": keys,
                }
                if gsi:
                    kwargs["GlobalSecondaryIndexes"] = gsi
                ddb.create_table(**kwargs)

            ssm.put_parameter(
                Name="/news-pipeline/ollama-api-key",
                Value="test-key",
                Type="SecureString",
            )
            ssm.put_parameter(
                Name="/news-pipeline/ollama-model",
                Value="test-model",
                Type="String",
            )
            ssm.put_parameter(
                Name="/news-pipeline/embedding-model",
                Value="local-hashed-256",
                Type="String",
            )
            ssm.put_parameter(
                Name="/news-pipeline/llm-provider",
                Value="ollama",
                Type="String",
            )

            # Patch the pipeline module's clients to use moto.
            lambda_handler.s3_client = s3
            lambda_handler.ssm_client = ssm
            lambda_handler.ddb_resource = boto3.resource("dynamodb", region_name="eu-central-1")
            lambda_handler.ARTICLES_TABLE = lambda_handler.ddb_resource.Table(os.environ["ARTICLES_TABLE"])
            lambda_handler.REPORTS_TABLE = lambda_handler.ddb_resource.Table(os.environ["REPORTS_TABLE"])
            graph_store._table = None
            graph_store.ddb_resource = lambda_handler.ddb_resource

            # Patch llm_factory.get_llm to return our fake directly.
            with patch("lambda_handler.llm_factory.get_llm", return_value=fake_llm):
                with patch("lambda_handler.fetch_rss") as mock_fetch:
                    def _fake_fetch(url, *args, **kwargs):
                        for src in sources_json["sources"]:
                            if src["url"] == url:
                                return [{
                                    "title": f"Article from {src['name']}",
                                    "url": f"https://example.com/{src['name']}",
                                    "description": "Apple and TSMC expand chip partnership in Taiwan.",
                                    "published": datetime.now(timezone.utc).isoformat(),
                                    "fingerprint": f"fp-{src['name']}",
                                }], {"status": "200"}
                        return [], {"status": "200"}

                    mock_fetch.side_effect = _fake_fetch
                    # Clear the SSM cache so moto values are fetched fresh.
                    lambda_handler._SSM_CACHE.clear()
                    result = lambda_handler.run_pipeline(force=True)
                    # Scan the graph table while still inside the moto context.
                    table = graph_store._table_instance()
                    items = table.scan()["Items"]
                    return result, items

    return _run


class TestPipelineGraphIntegration:
    def test_pipeline_populates_graph_table(self, run_with_mocks) -> None:
        sources = {
            "sources": [
                {"name": "TechDaily", "url": "https://td.example/feed", "category": "major", "enabled": True},
            ]
        }
        filters = {"include_keywords": ["Apple"], "exclude_keywords": []}

        result, items = run_with_mocks(sources, filters)

        assert result["ai_summaries"] == 1
        assert result["ere_success_count"] == 1
        assert result["triplet_count"] == 2
        assert result["graph_node_count"] >= 2

        # Verify the graph table actually has the nodes and edges.
        pks = {i["PK"] for i in items}
        assert "NODE#apple" in pks
        assert "NODE#tsmc" in pks
        assert "NODE#taiwan" in pks
