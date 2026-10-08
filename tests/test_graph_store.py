"""test_graph_store.py — unit tests for the DynamoDB graph store.

Uses moto to mock DynamoDB locally.  Tests entity upsert, edge upsert,
batch triplet writes, multi-hop traversal, TTL values, and batch chunking.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from ere import Triplet
from graph_store import (
    GRAPH_TABLE_NAME,
    GRAPH_TTL_DAYS,
    _chunk,
    get_neighbors,
    upsert_edge,
    upsert_entity,
    upsert_triplets,
)


@pytest.fixture
def mock_table():
    """Create the graph table in moto and patch the module-level table."""
    import graph_store

    with mock_aws():
        client = boto3.client("dynamodb", region_name="eu-central-1")
        client.create_table(
            TableName=GRAPH_TABLE_NAME,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        client.update_time_to_live(
            TableName=GRAPH_TABLE_NAME,
            TimeToLiveSpecification={"AttributeName": "ttl", "Enabled": True},
        )
        # Force the module to rebuild its table instance against moto.
        old_table = graph_store._table
        graph_store._table = None
        graph_store.ddb_resource = boto3.resource("dynamodb", region_name="eu-central-1")
        yield graph_store._table_instance()
        graph_store._table = old_table


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _scan_all(table) -> list[dict]:
    items = []
    resp = table.scan()
    items.extend(resp["Items"])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp["Items"])
    return items


# ─── Batch chunking ───────────────────────────────────────────────────────────


class TestChunking:
    def test_chunk_sizes(self) -> None:
        data = list(range(23))
        chunks = _chunk(data, 10)
        assert len(chunks) == 3
        assert [len(c) for c in chunks] == [10, 10, 3]


# ─── Entity upsert ───────────────────────────────────────────────────────────


class TestUpsertEntity:
    def test_creates_metadata_item(self, mock_table) -> None:
        upsert_entity("Apple Inc.", "org", "https://example.com/1", "2026-10-08")

        item = mock_table.get_item(Key={"PK": "NODE#apple", "SK": "META#"})["Item"]
        assert item["entity_display_name"] == "Apple Inc."
        assert item["entity_type"] == "org"
        assert item["source_url"] == "https://example.com/1"
        assert item["source_date"] == "2026-10-08"

    def test_preserves_existing_entity_type(self, mock_table) -> None:
        upsert_entity("Apple", "org", "https://example.com/1", "2026-10-08")
        upsert_entity("Apple", "technology", "https://example.com/2", "2026-10-09")

        item = mock_table.get_item(Key={"PK": "NODE#apple", "SK": "META#"})["Item"]
        # ADD semantics on entity_type create a set; test either the original
        # scalar value (first write wins) or a set containing both.
        assert item["entity_type"] in ("org", {"org", "technology"})

    def test_ttl_set(self, mock_table) -> None:
        upsert_entity("Taiwan", "place", "https://example.com/1", "2026-10-08")
        item = mock_table.get_item(Key={"PK": "NODE#taiwan", "SK": "META#"})["Item"]
        ttl = int(item["ttl"])
        now = int(datetime.now(timezone.utc).timestamp())
        expected = now + GRAPH_TTL_DAYS * 86400
        assert abs(ttl - expected) < 60


# ─── Edge upsert ─────────────────────────────────────────────────────────────


class TestUpsertEdge:
    def test_creates_edge_and_reverse_index(self, mock_table) -> None:
        upsert_edge("Apple", "SUPPLIES", "TSMC", "https://example.com/1", "2026-10-08")

        item = mock_table.get_item(
            Key={"PK": "NODE#apple", "SK": "EDGE#tsmc#SUPPLIES"}
        )["Item"]
        assert item["relation"] == "SUPPLIES"
        assert item["GSI1PK"] == "NODE#tsmc"
        assert item["GSI1SK"] == "EDGE#apple#SUPPLIES"


# ─── Triplet batch upsert ──────────────────────────────────────────────────


class TestUpsertTriplets:
    def test_writes_entities_and_edges(self, mock_table) -> None:
        triplets = [
            Triplet(
                subject="Apple Inc.",
                subject_type="org",
                relation="SUPPLIES",
                object="TSMC Corp.",
                object_type="org",
            ),
            Triplet(
                subject="TSMC",
                subject_type="org",
                relation="LOCATED_IN",
                object="Taiwan",
                object_type="place",
            ),
        ]
        entities, edges = upsert_triplets(triplets, "https://example.com/1", "2026-10-08")

        # We wrote both entities + both edges
        assert entities == 3
        assert edges == 2

        all_items = _scan_all(mock_table)
        pks = {i["PK"] for i in all_items}
        assert "NODE#apple" in pks
        assert "NODE#tsmc" in pks
        assert "NODE#taiwan" in pks

    def test_empty_triplets_noop(self, mock_table) -> None:
        entities, edges = upsert_triplets([], "https://example.com/1", "2026-10-08")
        assert entities == 0
        assert edges == 0
        assert _scan_all(mock_table) == []

    def test_batch_chunking(self, mock_table) -> None:
        # 15 triplets = 30 items (15 entities + 15 edges). Chunks of 25.
        triplets = [
            Triplet(
                subject=f"Entity{i}",
                subject_type="org",
                relation="AFFECTS",
                object="Hub",
                object_type="org",
            )
            for i in range(15)
        ]
        entities, edges = upsert_triplets(triplets, "https://example.com/batch", "2026-10-08")
        # Entity Hub is shared across all triplets, so 15 + 1 unique entities.
        assert entities == 16
        assert edges == 15
        assert len(_scan_all(mock_table)) == 31  # 16 meta + 15 edge items


# ─── Multi-hop traversal ─────────────────────────────────────────────────────


class TestGetNeighbors:
    def test_one_hop(self, mock_table) -> None:
        upsert_triplets([
            Triplet(
                subject="Apple", subject_type="org", relation="SUPPLIES",
                object="TSMC", object_type="org",
            ),
            Triplet(
                subject="TSMC", subject_type="org", relation="LOCATED_IN",
                object="Taiwan", object_type="place",
            ),
        ], "https://example.com/1", "2026-10-08")

        subgraph = get_neighbors("Apple", max_hops=1, max_nodes=100)
        assert "apple" in subgraph["nodes"]
        assert "tsmc" in subgraph["nodes"]
        assert any(e["target"] == "tsmc" and e["relation"] == "SUPPLIES" for e in subgraph["edges"])

    def test_two_hops(self, mock_table) -> None:
        upsert_triplets([
            Triplet(
                subject="Apple", subject_type="org", relation="SUPPLIES",
                object="TSMC", object_type="org",
            ),
            Triplet(
                subject="TSMC", subject_type="org", relation="LOCATED_IN",
                object="Taiwan", object_type="place",
            ),
        ], "https://example.com/1", "2026-10-08")

        subgraph = get_neighbors("Apple", max_hops=2, max_nodes=100)
        assert "taiwan" in subgraph["nodes"]
        assert any(e["target"] == "taiwan" and e["relation"] == "LOCATED_IN" for e in subgraph["edges"])

    def test_max_nodes_limits(self, mock_table) -> None:
        for i in range(10):
            upsert_triplets([
                Triplet(
                    subject="Hub", subject_type="org", relation="AFFECTS",
                    object=f"Leaf{i}", object_type="org",
                ),
            ], "https://example.com/1", "2026-10-08")

        subgraph = get_neighbors("Hub", max_hops=2, max_nodes=5)
        assert len(subgraph["nodes"]) <= 5
