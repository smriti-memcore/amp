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
import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

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

# ── §3.5 Error mapping ────────────────────────────────────────────────────────
# AmpErrorCode → JSON-RPC code (per spec/amp-v1.1.md §3.5)
#   invalid_request → -32602 (Invalid params — preferred over -32600 which is reserved
#                              for transport-level malformed JSON-RPC frames)
#   not_found       → -32001
#   not_supported   → -32002
#   backend_error   → -32000

_AMP_TO_JSONRPC = {
    "invalid_request": -32602,
    "not_found": -32001,
    "not_supported": -32002,
    "backend_error": -32000,
}


class AmpToolError(Exception):
    """Raised from inside a tool to signal a structured AMP error.

    The handler-level interceptor (custom_call_tool_handler) translates this
    into an McpError whose JSON-RPC code matches the §3.5 mapping table and
    whose `data` field carries the AmpErrorData payload.
    """

    def __init__(self, amp_error_code: str, message: str):
        if amp_error_code not in _AMP_TO_JSONRPC:
            raise ValueError(f"unknown amp_error_code: {amp_error_code}")
        self.amp_error_code = amp_error_code
        self.message = message
        super().__init__(f"[{amp_error_code}] {message}")


# Monkeypatch types.Implementation to automatically inject amp_conformance and amp_version fields.
# WARNING: Monkeypatching the mcp SDK is brittle and may break in future SDK updates.
# Consider opening an issue/PR upstream or using a wrapper decorator instead.
original_impl_init = types.Implementation.__init__

def custom_impl_init(self, *args, **kwargs):
    kwargs.setdefault("amp_conformance", "full")
    kwargs.setdefault("amp_version", "1.1")
    original_impl_init(self, *args, **kwargs)

types.Implementation.__init__ = custom_impl_init

# Monkeypatch CallToolRequest handler to translate tool exceptions/error results
# into JSON-RPC error frames using the §3.5 mapping table. Tool functions that
# raise AmpToolError emit the structured error; any other failure mode falls
# back to backend_error (-32000).
original_call_tool_handler = mcp._mcp_server.request_handlers[types.CallToolRequest]


def _extract_amp_error_from_text(text: str) -> Optional[Tuple[str, str]]:
    """Parse a tool error message of the form '[<code>] <message>'.

    FastMCP catches tool exceptions and turns them into a CallToolResult with
    isError=True; the original exception text becomes the content block. We
    encode (code, message) into that text via AmpToolError.__str__ so we can
    recover the structured shape here without depending on FastMCP internals.

    FastMCP prefixes the text with 'Error executing tool <name>: '; strip that
    before scanning. We also recognise pydantic input-validation errors thrown
    by FastMCP's own argument parsing (which happen BEFORE the tool body
    runs) and map them to invalid_request since they correspond to malformed
    or incomplete inputs.
    """
    if not text:
        return None

    # FastMCP wraps tool errors as "Error executing tool <name>: <original>".
    # Strip that prefix so the original [code] marker is at the front.
    marker = "Error executing tool"
    if text.startswith(marker):
        colon = text.find(":")
        if colon != -1:
            text = text[colon + 1 :].lstrip()

    # Explicit AmpToolError marker.
    if text.startswith("["):
        end = text.find("]")
        if end >= 2:
            code = text[1:end]
            if code in _AMP_TO_JSONRPC:
                message = text[end + 1 :].lstrip()
                return code, message

    # Pydantic validation errors raised by FastMCP's argument parser before
    # the tool body executes. These are always parameter-shape problems →
    # invalid_request per §3.5.
    if "validation error" in text.lower() or "field required" in text.lower():
        return "invalid_request", text

    return None


async def custom_call_tool_handler(req: types.CallToolRequest) -> types.ServerResult:
    res = await original_call_tool_handler(req)
    if hasattr(res, "root") and isinstance(res.root, types.CallToolResult) and res.root.isError:
        raw = res.root.content[0].text if res.root.content else "Unknown tool error"
        parsed = _extract_amp_error_from_text(raw)
        if parsed is not None:
            amp_code, message = parsed
        else:
            amp_code, message = "backend_error", raw
        jsonrpc_code = _AMP_TO_JSONRPC[amp_code]
        raise McpError(
            types.ErrorData(
                code=jsonrpc_code,
                message=message,
                data={"amp_error_code": amp_code, "message": message},
            )
        )
    return res


mcp._mcp_server.request_handlers[types.CallToolRequest] = custom_call_tool_handler

# ── Scope handling ────────────────────────────────────────────────────────────
# v1.1 introduces a multi-dimensional Scope object. Backends must accept either
# `scope` (preferred) or the legacy flat `agent_id`. At least one isolating
# identity key — {agent_id, group_id, workspace_id, user_id} — must be present.
# Non-isolating keys ({session_id, app_id, org_id}) refine the namespace but
# do not satisfy the at-least-one-isolating-key requirement.

ISOLATING_KEYS = ("agent_id", "group_id", "workspace_id", "user_id")
NON_ISOLATING_KEYS = ("session_id", "app_id", "org_id")
ALL_SCOPE_KEYS = ISOLATING_KEYS + NON_ISOLATING_KEYS


def _normalize_scope(
    scope: Optional[Dict[str, Any]],
    agent_id: Optional[str],
) -> Dict[str, str]:
    """Resolve (scope, agent_id) into a validated v1.1 Scope dict.

    Precedence: explicit `scope` wins; otherwise legacy `agent_id` is promoted
    to {"agent_id": agent_id}. Raises AmpToolError(invalid_request) when neither
    is supplied or when no isolating key is present.
    """
    if scope is not None:
        if not isinstance(scope, dict):
            raise AmpToolError("invalid_request", "scope must be an object")
        # Reject unknown keys for forward-compatibility hygiene.
        unknown = set(scope.keys()) - set(ALL_SCOPE_KEYS)
        if unknown:
            raise AmpToolError(
                "invalid_request",
                f"scope contains unknown keys: {sorted(unknown)}",
            )
        normalized = {k: v for k, v in scope.items() if v is not None and v != ""}
        # Promote any legacy agent_id alongside scope (rare; the spec's anyOf
        # accepts either, not both — but tolerate the redundant case when the
        # values agree).
        if agent_id:
            existing = normalized.get("agent_id")
            if existing and existing != agent_id:
                raise AmpToolError(
                    "invalid_request",
                    "agent_id provided both as scope.agent_id and top-level disagree",
                )
            normalized.setdefault("agent_id", agent_id)
    elif agent_id:
        normalized = {"agent_id": agent_id}
    else:
        raise AmpToolError(
            "invalid_request",
            "either scope or agent_id is required",
        )

    if not any(normalized.get(k) for k in ISOLATING_KEYS):
        raise AmpToolError(
            "invalid_request",
            "scope must include at least one isolating identity key "
            f"({', '.join(ISOLATING_KEYS)})",
        )

    # Coerce all values to str to keep storage paths deterministic.
    return {k: str(v) for k, v in normalized.items()}


def _scope_namespace_key(scope: Dict[str, str]) -> str:
    """Derive a stable, filesystem-safe namespace key from a normalized scope.

    Single-key scopes that only set `agent_id` produce the bare agent_id (so
    legacy v1.0 callers keep their existing storage directory). Multi-key
    scopes get a deterministic hash with a `scope-` prefix so storage paths
    never collide with agent_id-only namespaces.
    """
    if list(scope.keys()) == ["agent_id"]:
        return scope["agent_id"]
    # Sort to make the key independent of insertion order.
    canonical = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"scope-{digest}"


_agents: Dict[str, SMRITI] = {}

_storage_base: str = os.environ.get("AMP_STORAGE_PATH", os.path.expanduser("~/.amp"))


def _get_agent_for_scope(scope: Dict[str, str]) -> SMRITI:
    key = _scope_namespace_key(scope)
    if key not in _agents:
        path = os.path.join(_storage_base, key)
        config = SmritiConfig(storage_path=path)
        _agents[key] = SMRITI(config=config)
    return _agents[key]


def _memory_to_result(
    memory: Memory,
    scope: Dict[str, str],
    score: Optional[float] = None,
    legacy_visibility: Optional[str] = None,
) -> Dict[str, Any]:
    status = memory.status.value if hasattr(memory.status, "value") else str(memory.status)
    source = memory.source.value if hasattr(memory.source, "value") else str(memory.source)
    ts = memory.creation_time
    timestamp = ts.isoformat() if isinstance(ts, datetime) else str(ts)
    result: Dict[str, Any] = {
        "id": memory.id,
        "content": memory.content,
        "score": score if score is not None else getattr(memory, "retrieval_score", 0.0) or 0.0,
        "source": source,
        "timestamp": timestamp,
        "status": status,
        "scope": scope,
        "metadata": {
            "salience": memory.salience.composite if memory.salience else None,
            "room_id": getattr(memory, "room_id", None),
            "hops": getattr(memory, "hops", 0),
            "reflection_level": getattr(memory, "reflection_level", 0),
            "strength": getattr(memory, "strength", 1.0),
        },
    }
    # Echo the deprecated `visibility` field only when the legacy parameter
    # was actually supplied on the encode call — keeps v1.1-native responses
    # free of deprecated noise.
    if legacy_visibility is not None:
        result["visibility"] = legacy_visibility
    return result


# ── amp.encode ────────────────────────────────────────────────────────────────

@mcp.tool(
    name="amp.encode",
    description="Store a new memory for an agent.",
    annotations=types.ToolAnnotations(
        title="Encode Memory",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def amp_encode(
    content: str,
    agent_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
    source: str = "direct",
    force: bool = False,
    private: Optional[bool] = None,  # Deprecated in v1.1
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        # Per §3.5, empty/missing content is invalid_request, not below_threshold.
        # Below_threshold remains the salience-gate outcome (handled below).
        raise AmpToolError("invalid_request", "content must be a non-empty string")

    norm_scope = _normalize_scope(scope, agent_id)
    smriti = _get_agent_for_scope(norm_scope)

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

    response: Dict[str, Any] = {"id": memory_id, "status": "stored"}
    # Echo the deprecated visibility only when the legacy `private` parameter
    # was actually supplied (spec §5 — deprecated fields are echoed on demand).
    if private is not None:
        response["visibility"] = "private" if private else "shared"
    return response


# ── amp.recall ────────────────────────────────────────────────────────────────

@mcp.tool(
    name="amp.recall",
    description="Retrieve memories relevant to a query.",
    annotations=types.ToolAnnotations(
        title="Recall Memories",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def amp_recall(
    query: str,
    agent_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
    top_k: int = 10,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        raise AmpToolError("invalid_request", "query must be a non-empty string")

    norm_scope = _normalize_scope(scope, agent_id)
    smriti = _get_agent_for_scope(norm_scope)
    memories = smriti.recall(query, top_k=top_k)

    results = []
    for mem in memories:
        result = _memory_to_result(mem, norm_scope)
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

@mcp.tool(
    name="amp.forget",
    description="Permanently delete a memory.",
    annotations=types.ToolAnnotations(
        title="Forget Memory",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def amp_forget(
    id: str,
    agent_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(id, str) or not id:
        raise AmpToolError("invalid_request", "id is required")
    norm_scope = _normalize_scope(scope, agent_id)
    smriti = _get_agent_for_scope(norm_scope)
    memory = smriti.palace.get_memory(id)
    if memory is None:
        return {"status": "not_found"}
    smriti.forget(id)
    return {"status": "forgotten"}


# ── amp.consolidate ───────────────────────────────────────────────────────────

@mcp.tool(
    name="amp.consolidate",
    description="Trigger backend consolidation.",
    annotations=types.ToolAnnotations(
        title="Consolidate Memories",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def amp_consolidate(
    agent_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
    depth: str = "full",
) -> Dict[str, Any]:
    norm_scope = _normalize_scope(scope, agent_id)
    smriti = _get_agent_for_scope(norm_scope)
    result = smriti.consolidate(depth=depth)
    processed = result.get("episodes_processed", result.get("memories_processed", 0))
    return {"status": "ok", "memories_processed": processed}


# ── amp.pin ───────────────────────────────────────────────────────────────────

@mcp.tool(
    name="amp.pin",
    description="Mark a memory as permanent.",
    annotations=types.ToolAnnotations(
        title="Pin Memory",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def amp_pin(
    id: str,
    agent_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(id, str) or not id:
        raise AmpToolError("invalid_request", "id is required")
    norm_scope = _normalize_scope(scope, agent_id)
    smriti = _get_agent_for_scope(norm_scope)
    memory = smriti.palace.get_memory(id)
    if memory is None:
        return {"status": "not_found"}
    smriti.pin(id)
    return {"status": "pinned"}


# ── amp.stats ─────────────────────────────────────────────────────────────────

@mcp.tool(
    name="amp.stats",
    description="Return backend statistics for an agent namespace.",
    annotations=types.ToolAnnotations(
        title="Backend Statistics",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def amp_stats(
    agent_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    norm_scope = _normalize_scope(scope, agent_id)
    smriti = _get_agent_for_scope(norm_scope)
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
