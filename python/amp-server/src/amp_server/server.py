"""
AMP Server — Agent Memory Protocol reference implementation.

Wraps smriti-memcore and exposes it as a Full-conformant AMP server over MCP stdio.
Each agent_id gets an isolated smriti-memcore instance in its own subdirectory.

Usage:
    amp-server                              # storage: ~/.amp
    amp-server --storage-path /my/path     # custom storage root
    AMP_STORAGE_PATH=/my/path amp-server   # via env var
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from smriti_memcore.core import SMRITI, SmritiConfig
from smriti_memcore.models import Memory, MemorySource, MemoryStatus

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "amp-server",
    instructions=(
        "AMP (Agent Memory Protocol) Full-conformant memory server. "
        "Implements amp.encode, amp.recall, amp.forget, amp.consolidate, amp.pin, amp.stats."
    ),
)

_agents: Dict[str, SMRITI] = {}
_storage_base: str = os.environ.get("AMP_STORAGE_PATH", os.path.expanduser("~/.amp"))


def _get_agent(agent_id: str) -> SMRITI:
    if agent_id not in _agents:
        path = os.path.join(_storage_base, agent_id)
        config = SmritiConfig(storage_path=path)
        _agents[agent_id] = SMRITI(config=config)
    return _agents[agent_id]


def _memory_to_result(memory: Memory, score: Optional[float] = None) -> Dict[str, Any]:
    status = memory.status.value if hasattr(memory.status, "value") else str(memory.status)
    source = memory.source.value if hasattr(memory.source, "value") else str(memory.source)
    ts = memory.creation_time
    timestamp = ts.isoformat() if isinstance(ts, datetime) else str(ts)
    return {
        "id": memory.id,
        "content": memory.content,
        "score": score if score is not None else getattr(memory, "retrieval_score", 0.0) or 0.0,
        "source": source,
        "timestamp": timestamp,
        "status": status,
        "metadata": {
            "salience": memory.salience.composite if memory.salience else None,
            "room_id": getattr(memory, "room_id", None),
            "hops": getattr(memory, "hops", 0),
            "reflection_level": getattr(memory, "reflection_level", 0),
            "strength": getattr(memory, "strength", 1.0),
        },
    }


# ── amp.encode ────────────────────────────────────────────────────────────────

@mcp.tool(name="amp.encode", description="Store a new memory for an agent.")
def amp_encode(
    agent_id: str,
    content: str,
    source: str = "direct",
    force: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not content or not content.strip():
        return {"status": "below_threshold"}

    smriti = _get_agent(agent_id)
    try:
        source_enum = MemorySource(source)
    except ValueError:
        source_enum = MemorySource.DIRECT

    # force=True → USER_STATED source + no LLM gate to maximise chance of storage
    if force:
        source_enum = MemorySource.USER_STATED
        memory_id = smriti.encode(content, source=source_enum, use_llm=False)
    else:
        memory_id = smriti.encode(content, source=source_enum, use_llm=True)

    if memory_id is None:
        return {"status": "below_threshold"}
    return {"id": memory_id, "status": "stored"}


# ── amp.recall ────────────────────────────────────────────────────────────────

@mcp.tool(name="amp.recall", description="Retrieve memories relevant to a query.")
def amp_recall(
    agent_id: str,
    query: str,
    top_k: int = 10,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    smriti = _get_agent(agent_id)
    memories = smriti.recall(query, top_k=top_k)

    results = []
    for mem in memories:
        result = _memory_to_result(mem)
        # Apply status filter post-retrieval (smriti already excludes archived by default)
        if filters:
            status_filter = filters.get("status")
            if status_filter and result["status"] != status_filter:
                continue
            source_filter = filters.get("source")
            if source_filter and result["source"] != source_filter:
                continue
        results.append(result)

    return {"results": results}


# ── amp.forget ────────────────────────────────────────────────────────────────

@mcp.tool(name="amp.forget", description="Permanently delete a memory.")
def amp_forget(agent_id: str, id: str) -> Dict[str, Any]:
    smriti = _get_agent(agent_id)
    memory = smriti.palace.get_memory(id)
    if memory is None:
        return {"status": "not_found"}
    smriti.forget(id)
    return {"status": "forgotten"}


# ── amp.consolidate ───────────────────────────────────────────────────────────

@mcp.tool(name="amp.consolidate", description="Trigger backend consolidation.")
def amp_consolidate(agent_id: str, depth: str = "full") -> Dict[str, Any]:
    smriti = _get_agent(agent_id)
    result = smriti.consolidate(depth=depth)
    processed = result.get("episodes_processed", result.get("memories_processed", 0))
    return {"status": "ok", "memories_processed": processed}


# ── amp.pin ───────────────────────────────────────────────────────────────────

@mcp.tool(name="amp.pin", description="Mark a memory as permanent.")
def amp_pin(agent_id: str, id: str) -> Dict[str, Any]:
    smriti = _get_agent(agent_id)
    memory = smriti.palace.get_memory(id)
    if memory is None:
        return {"status": "not_found"}
    smriti.pin(id)
    return {"status": "pinned"}


# ── amp.stats ─────────────────────────────────────────────────────────────────

@mcp.tool(name="amp.stats", description="Return backend statistics for an agent namespace.")
def amp_stats(agent_id: str) -> Dict[str, Any]:
    smriti = _get_agent(agent_id)
    s = smriti.stats()
    palace = s.get("palace", {})
    episode = s.get("episode_buffer", {})
    return {
        "memory_count": palace.get("memory_count", 0),
        "unconsolidated_count": episode.get("unconsolidated", 0),
        "metadata": {
            "room_count": palace.get("room_count", 0),
            "vector_count": s.get("vector_store", {}).get("total_vectors", 0),
            "total_episodes": episode.get("total_episodes", 0),
            "retrieval": s.get("retrieval", {}),
        },
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _storage_base

    parser = argparse.ArgumentParser(description="AMP Server — Agent Memory Protocol over MCP")
    parser.add_argument(
        "--storage-path",
        default=os.environ.get("AMP_STORAGE_PATH", os.path.expanduser("~/.amp")),
        help="Root directory for agent memory storage (default: ~/.amp)",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    _storage_base = args.storage_path
    logging.basicConfig(level=getattr(logging, args.log_level))
    logger.info("AMP Server starting — storage: %s", _storage_base)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
