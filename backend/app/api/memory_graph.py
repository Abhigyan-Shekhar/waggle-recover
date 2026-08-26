"""Memory graph API — expose Waggle subgraphs for visualization."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.main import get_adapter
from app.memory.waggle_adapter import WaggleRecoveryMemoryAdapter

router = APIRouter()


@router.get("/node/{node_id}")
async def get_node(
    node_id: str,
    adapter: WaggleRecoveryMemoryAdapter = Depends(get_adapter),
) -> dict[str, Any]:
    node = adapter.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


@router.get("/node/{node_id}/related")
async def get_related(
    node_id: str,
    max_depth: int = 2,
    adapter: WaggleRecoveryMemoryAdapter = Depends(get_adapter),
) -> dict[str, Any]:
    nodes = adapter.get_related_nodes(node_id, max_depth=max_depth)
    return {"nodes": nodes, "count": len(nodes)}


@router.get("/node/{node_id}/graph")
async def get_node_graph(
    node_id: str,
    adapter: WaggleRecoveryMemoryAdapter = Depends(get_adapter),
) -> dict[str, Any]:
    return adapter.get_nodes_and_edges_for_decision(node_id)


@router.post("/query")
async def query_memory(
    body: dict[str, Any],
    adapter: WaggleRecoveryMemoryAdapter = Depends(get_adapter),
) -> dict[str, Any]:
    query = body.get("query", "")
    customer_id = body.get("customer_id", "")
    if not query and customer_id:
        query = f"customer {customer_id} payment recovery"

    nodes = adapter.get_customer_history(
        customer_id=customer_id,
        max_nodes=body.get("max_nodes", 20),
    )
    return {"nodes": nodes, "count": len(nodes), "query": query}
