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
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
import mcp.types as types
from mcp.shared.exceptions import McpError
from smriti_memcore.core import SMRITI, SmritiConfig
from smriti_memcore.models import Memory, MemorySource


logger = logging.getLogger(__name__)

mcp = FastMCP(
    "amp-server",
    instructions=(
        "AMP (Agent Memory Protocol) Full-conformant memory server. "
        "Implements amp.encode, amp.recall, amp.forget, amp.consolidate, amp.pin, amp.stats."
    ),
)

# Monkeypatch types.Implementation to automatically inject amp_conformance and amp_version fields.
original_impl_init = types.Implementation.__init__

def custom_impl_init(self, *args, **kwargs):
    kwargs.setdefault("amp_conformance", "full")
    kwargs.setdefault("amp_version", "1.1")
    original_impl_init(self, *args, **kwargs)

types.Implementation.__init__ = custom_impl_init

# Monkeypatch CallToolRequest handler to convert tool errors into standard JSON-RPC level errors with code -32000.
original_call_tool_handler = mcp._mcp_server.request_handlers[types.CallToolRequest]

async def custom_call_tool_handler(req: types.CallToolRequest) -> types.ServerResult:
    res = await original_call_tool_handler(req)
    if hasattr(res, "root") and isinstance(res.root, types.CallToolResult) and res.root.isError:
        error_message = res.root.content[0].text if res.root.content else "Unknown tool error"
        raise McpError(types.ErrorData(code=-32000, message=error_message))
    return res

mcp._mcp_server.request_handlers[types.CallToolRequest] = custom_call_tool_handler

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
        "visibility": "shared",  # Deprecated in v1.1
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
    private: bool = False,  # Deprecated in v1.1
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
    visibility = "private" if private else "shared"
    return {"id": memory_id, "status": "stored", "visibility": visibility}


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
    
    # Calculate active and pinned memories count dynamically
    memories = smriti.palace.memories.values() if hasattr(smriti.palace, "memories") else []
    memory_count = sum(
        1 for m in memories
        if getattr(m.status, "value", str(m.status)).lower() in ("active", "pinned")
    )

    return {
        "memory_count": memory_count,
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
