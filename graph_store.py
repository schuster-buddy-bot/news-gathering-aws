"""graph_store.py — DynamoDB adjacency-list graph store.

Stores normalized entities and directed edges in the ``news-pipeline-graph``
DynamoDB table using the adjacency-list pattern:

    PK = NODE#<normalized_entity>
    SK = META#                     (entity metadata)
    SK = EDGE#<normalized_target>#<relation>     (outgoing edge)

    GSI1PK = NODE#<normalized_target>
    GSI1SK = EDGE#<normalized_source>#<relation>    (reverse edge)

Items carry a TTL attribute so the graph self-cleans in sync with the
article retention period (30 days by default).

Public API:
    upsert_entity(entity_name, entity_type, source_url, source_date)
    upsert_edge(subject, relation, object, source_url, source_date)
    upsert_triplets(triplets, source_url, source_date)
    get_neighbors(entity, max_hops=2, max_nodes=100)
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from decimal import Decimal
from typing import Any

import boto3

from ere import Triplet, normalize_entity

logger = logging.getLogger(__name__)

GRAPH_TABLE_NAME = os.environ.get("GRAPH_TABLE", "news-pipeline-graph")
GRAPH_TTL_DAYS = int(os.environ.get("GRAPH_TTL_DAYS", "30"))

# Module-level DynamoDB resource (reused across warm invocations)
ddb_resource = boto3.resource("dynamodb")
_table = None


def _table_instance():
    """Return the cached graph table resource (lazy, thread-safe-ish in Lambda)."""
    global _table
    if _table is None:
        _table = ddb_resource.Table(GRAPH_TABLE_NAME)
    return _table


def _now() -> int:
    return int(time.time())


def _ttl_epoch() -> int:
    return _now() + GRAPH_TTL_DAYS * 86400


def _make_meta_item(entity_name: str, entity_type: str, source_url: str, source_date: str) -> dict[str, Any]:
    """Build a DynamoDB entity-metadata item."""
    normalized = normalize_entity(entity_name)
    return {
        "PK": f"NODE#{normalized}",
        "SK": "META#",
        "entity_type": entity_type,
        "entity_display_name": entity_name.strip(),
        "source_url": source_url,
        "source_date": source_date,
        "ttl": _ttl_epoch(),
        "updated_at": _now(),
    }


def _make_edge_item(
    subject: str,
    relation: str,
    object: str,
    source_url: str,
    source_date: str,
    confidence: float = 1.0,
) -> dict[str, Any]:
    """Build a DynamoDB edge item plus reverse-index attributes for GSI1."""
    s_norm = normalize_entity(subject)
    o_norm = normalize_entity(object)
    return {
        "PK": f"NODE#{s_norm}",
        "SK": f"EDGE#{o_norm}#{relation}",
        "GSI1PK": f"NODE#{o_norm}",
        "GSI1SK": f"EDGE#{s_norm}#{relation}",
        "entity_type": "edge",
        "relation": relation,
        "source_url": source_url,
        "source_date": source_date,
        "confidence": Decimal(str(confidence)),
        "ttl": _ttl_epoch(),
        "updated_at": _now(),
    }


def upsert_entity(
    entity_name: str,
    entity_type: str,
    source_url: str,
    source_date: str,
) -> None:
    """Write or refresh entity metadata.

    Existing entity_type is preserved if already set (do not overwrite with a
    different type from a later article).  TTL and display name are refreshed.
    """
    table = _table_instance()
    normalized = normalize_entity(entity_name)
    pk = f"NODE#{normalized}"

    try:
        table.update_item(
            Key={"PK": pk, "SK": "META#"},
            UpdateExpression="SET entity_display_name = :dn, source_url = :url, source_date = :sd, #t = :ttl, updated_at = :ts, entity_type = if_not_exists(entity_type, :et)",
            ExpressionAttributeNames={"#t": "ttl"},
            ExpressionAttributeValues={
                ":dn": entity_name.strip(),
                ":url": source_url,
                ":sd": source_date,
                ":ttl": _ttl_epoch(),
                ":ts": _now(),
                ":et": entity_type,
            },
        )
    except Exception as e:  # noqa: BLE001 — graph storage must never break the pipeline
        logger.warning("upsert_entity failed for %r: %s", entity_name, e)


def upsert_edge(
    subject: str,
    relation: str,
    object: str,
    source_url: str,
    source_date: str,
    confidence: float = 1.0,
) -> None:
    """Write or refresh a directed edge."""
    table = _table_instance()
    try:
        table.put_item(Item=_make_edge_item(subject, relation, object, source_url, source_date, confidence))
    except Exception as e:  # noqa: BLE001
        logger.warning("upsert_edge failed for %s-%s-%s: %s", subject, relation, object, e)


def _chunk(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def upsert_triplets(
    triplets: list[Triplet],
    source_url: str,
    source_date: str,
) -> tuple[int, int]:
    """Batch upsert all entities and edges from an article's triplets.

    Args:
        triplets: List of ``Triplet`` objects.
        source_url: Source article URL.
        source_date: Source article date (YYYY-MM-DD).

    Returns:
        Tuple of (entity_count, edge_count) written.
    """
    if not triplets:
        return 0, 0

    table = _table_instance()
    entities: dict[str, str] = {}
    edge_items: list[dict[str, Any]] = []

    for t in triplets:
        s_norm = normalize_entity(t.subject)
        o_norm = normalize_entity(t.object)
        if not s_norm or not o_norm:
            continue
        entities[s_norm] = t.subject_type
        entities[o_norm] = t.object_type
        edge_items.append(
            _make_edge_item(t.subject, t.relation, t.object, source_url, source_date)
        )

    # Entity metadata items (deduplicated by normalized key)
    entity_items = [
        _make_meta_item(display_name, entity_type, source_url, source_date)
        for display_name, entity_type in entities.items()
    ]

    written_entities = 0
    written_edges = 0

    # batch_write_item has a hard limit of 25 items per request.
    all_items = [{"PutRequest": {"Item": item}} for item in entity_items + edge_items]
    for chunk in _chunk(all_items, 25):
        try:
            table.batch_writer().put_item  # noqa: B018
        except Exception:
            pass

    # Use batch_write_item directly for cleaner error handling.
    for chunk in _chunk(all_items, 25):
        try:
            resp = ddb_resource.batch_write_item(RequestItems={GRAPH_TABLE_NAME: chunk})
            written_entities += sum(
                1 for req in chunk
                if req["PutRequest"]["Item"]["SK"] == "META#"
            )
            written_edges += sum(
                1 for req in chunk
                if req["PutRequest"]["Item"]["SK"].startswith("EDGE#")
            )
            unprocessed = resp.get("UnprocessedKeys", {}).get(GRAPH_TABLE_NAME, [])
            if unprocessed:
                logger.warning("batch_write_item left %s unprocessed items", len(unprocessed))
        except Exception as e:  # noqa: BLE001
            logger.warning("upsert_triplets batch failed: %s", e)

    return written_entities, written_edges


def get_neighbors(
    entity: str,
    max_hops: int = 2,
    max_nodes: int = 100,
) -> dict[str, Any]:
    """BFS traversal from an entity, returning nodes and edges.

    Args:
        entity: Starting entity (display or normalized form).
        max_hops: Maximum graph distance to explore.
        max_nodes: Maximum distinct nodes to collect.

    Returns:
        Dict with ``nodes`` and ``edges`` lists.
    """
    table = _table_instance()
    start = normalize_entity(entity)
    visited: set[str] = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    edges: list[dict[str, Any]] = []

    while queue and len(visited) < max_nodes:
        current, hop = queue.popleft()
        if hop >= max_hops:
            continue

        pk = f"NODE#{current}"
        try:
            resp = table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :edge)",
                ExpressionAttributeValues={":pk": pk, ":edge": "EDGE#"},
                ProjectionExpression="SK, source_url, source_date, confidence",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("get_neighbors query failed for %r: %s", current, e)
            continue

        for item in resp.get("Items", []):
            sk = item["SK"]
            parts = sk.split("#", 2)
            if len(parts) < 3:
                continue
            target = parts[1]
            relation = parts[2]
            confidence = item.get("confidence", Decimal("1.0"))
            if isinstance(confidence, Decimal):
                confidence = float(confidence)
            edges.append({
                "source": current,
                "target": target,
                "relation": relation,
                "source_url": item.get("source_url"),
                "source_date": item.get("source_date"),
                "confidence": confidence,
            })
            if target not in visited and len(visited) < max_nodes:
                visited.add(target)
                queue.append((target, hop + 1))

    nodes = list(visited)
    return {"nodes": nodes, "edges": edges}
