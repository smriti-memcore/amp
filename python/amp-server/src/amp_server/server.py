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
import base64
import hashlib
import json
import logging
import os
from binascii import Error as binascii_Error
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
        "Implements amp.encode, amp.recall, amp.forget, amp.consolidate, amp.pin, amp.stats, amp.export, amp.import, amp.update."
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
    # Merge user-defined metadata (stored verbatim — including reserved AMP
    # keys like amp.confidence, amp.entities, …) with backend-internal hints
    # (salience, room_id, hops, …). User keys take precedence so caller-
    # supplied metadata survives a recall round-trip cleanly.
    stored_metadata = dict(memory.metadata) if isinstance(memory.metadata, dict) else {}
    backend_metadata = {
        "salience": memory.salience.composite if memory.salience else None,
        "room_id": getattr(memory, "room_id", None),
        "hops": getattr(memory, "hops", 0),
        "reflection_level": getattr(memory, "reflection_level", 0),
        "strength": getattr(memory, "strength", 1.0),
    }
    # Backend hints fill in where the user hasn't supplied a value; user keys
    # win on conflict.
    merged_metadata = {**backend_metadata, **stored_metadata}

    result: Dict[str, Any] = {
        "id": memory.id,
        "content": memory.content,
        "score": score if score is not None else getattr(memory, "retrieval_score", 0.0) or 0.0,
        "source": source,
        "timestamp": timestamp,
        "status": status,
        "scope": scope,
        "metadata": merged_metadata,
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

    # Apply caller-supplied metadata onto the newly-created memory. smriti.encode
    # does not accept a metadata argument, so we patch it post-hoc via the
    # palace. Reserved AMP keys (amp.confidence, amp.entities, etc.) and any
    # user-defined keys are written verbatim; backend-internal keys (salience,
    # room_id, hops, …) on the existing metadata bag are preserved.
    if metadata is not None and isinstance(metadata, dict) and metadata:
        stored = smriti.palace.get_memory(memory_id)
        if stored is not None:
            existing = dict(stored.metadata) if isinstance(stored.metadata, dict) else {}
            existing.update(metadata)
            stored.metadata = existing
            try:
                smriti.palace.save()
            except Exception:
                # Metadata persistence is best-effort on encode; if it fails
                # the row is still stored, so we don't fail the call. A
                # production backend should treat this as a hard error.
                pass

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


# ── amp.update ────────────────────────────────────────────────────────────────
#
# v1.2-draft. Mutate the content and/or metadata of an existing memory in place.
# The memory's id, scope, source, status, and creation_time are preserved -- only
# content and metadata are mutable. By default metadata is applied as a JSON
# Merge Patch (RFC 7396): keys present in the request overwrite; absent keys
# are preserved; explicit JSON null removes a key. Callers opt into wholesale
# replacement via metadata_mode="replace".


def _apply_merge_patch(stored: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """RFC 7396 JSON Merge Patch.

    Returns a new dict (does not mutate `stored`). null in the patch removes a
    key; nested objects merge recursively; arrays and scalars are replaced
    wholesale (per the RFC, arrays are NOT merged element-wise).
    """
    if not isinstance(patch, dict):
        # Per RFC 7396, a non-object patch replaces the target entirely.
        return patch
    result = dict(stored) if isinstance(stored, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _apply_merge_patch(result[key], value)
        else:
            result[key] = value
    return result


@mcp.tool(
    name="amp.update",
    description="Mutate the content and/or metadata of an existing memory in place (v1.2-draft).",
    annotations=types.ToolAnnotations(
        title="Update Memory",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def amp_update(
    id: str,
    agent_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
    content: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    metadata_mode: str = "merge",
) -> Dict[str, Any]:
    if not isinstance(id, str) or not id:
        raise AmpToolError("invalid_request", "id is required")

    # Content semantics: omit to leave unchanged; empty string is rejected per
    # spec section 3.2.4 (use amp.forget to delete a row).
    if content is not None:
        if not isinstance(content, str):
            raise AmpToolError("invalid_request", "content must be a string")
        if content == "":
            raise AmpToolError(
                "invalid_request",
                "content cannot be empty; use amp.forget to delete a memory",
            )

    if metadata is not None and not isinstance(metadata, dict):
        raise AmpToolError("invalid_request", "metadata must be an object")
    if metadata_mode not in ("merge", "replace"):
        raise AmpToolError(
            "invalid_request",
            "metadata_mode must be 'merge' or 'replace'",
        )

    norm_scope = _normalize_scope(scope, agent_id)
    smriti = _get_agent_for_scope(norm_scope)
    memory = smriti.palace.get_memory(id)
    if memory is None:
        # Cross-scope updates land here too (the scope's palace doesn't carry
        # the foreign id); per spec section 3.2.4 we return not_found rather
        # than invalid_request so existence info doesn't leak across scopes.
        return {"status": "not_found"}

    # Snapshot for change detection.
    original_content = memory.content
    original_metadata = dict(memory.metadata) if isinstance(memory.metadata, dict) else {}

    mutated = False

    if content is not None and content != original_content:
        memory.content = content
        mutated = True

    if metadata is not None:
        if metadata_mode == "replace":
            new_metadata = dict(metadata)
        else:  # merge
            new_metadata = _apply_merge_patch(original_metadata, metadata)
        if new_metadata != original_metadata:
            # Direct assignment is safe: Memory is a mutable dataclass and the
            # palace's in-memory map already references this instance.
            memory.metadata = new_metadata
            mutated = True

    # Persist if anything changed. palace.save() is a full snapshot — fine for
    # the reference impl; production backends would do a targeted write.
    if mutated:
        try:
            smriti.palace.save()
        except Exception as exc:
            raise AmpToolError("backend_error", f"palace.save failed: {exc}")
        return {"status": "updated", "id": id}

    return {"status": "no_change", "id": id}


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


# ── amp.export ────────────────────────────────────────────────────────────────
#
# MXF (Memory Exchange Format) is NDJSON — one MemoryResult per line. The
# reference implementation is synchronous: it always emits `ndjson` (possibly
# the empty string) and never the async `event_id` branch. Pagination uses an
# opaque base64-encoded JSON cursor over a stable sort order.
#
# Sort order (per spec §3.3.4): ascending `creation_time`, ties broken by
# ascending `id`. Cursors carry the offset into that order; they remain valid
# across new writes because new rows have a later `creation_time` than any
# row already cursor-skipped.

_DEFAULT_EXPORT_PAGE_SIZE = 10000   # rows
_EXPORT_PAGE_BYTE_CAP = 10 * 1024 * 1024  # 10 MiB per spec recommendation


def _encode_export_cursor(offset: int) -> str:
    payload = json.dumps({"offset": offset}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_export_cursor(cursor: str) -> int:
    """Decode an opaque export cursor back to its offset. Raises AmpToolError
    on any tampering / format error so callers see invalid_request rather
    than backend_error."""
    if not cursor:
        return 0
    # Re-pad for urlsafe_b64decode.
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(payload.decode("utf-8"))
        offset = int(data["offset"])
        if offset < 0:
            raise ValueError("negative offset")
        return offset
    except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError, binascii_Error):
        raise AmpToolError("invalid_request", "cursor is malformed or has been tampered with")


def _row_matches_filters(result: Dict[str, Any], filters: Optional[Dict[str, Any]]) -> bool:
    """Apply the RecallFilters subset that export supports (status, source,
    timestamp_after, timestamp_before). Filtering is post-materialisation —
    cheap on the reference impl, would be pushed into the storage layer on a
    production backend."""
    if not filters:
        return True
    status = filters.get("status")
    if status and result["status"] != status:
        return False
    source = filters.get("source")
    if source and result["source"] != source:
        return False
    ts_after = filters.get("timestamp_after")
    if ts_after and result["timestamp"] < ts_after:
        return False
    ts_before = filters.get("timestamp_before")
    if ts_before and result["timestamp"] >= ts_before:
        return False
    return True


@mcp.tool(
    name="amp.export",
    description="Stream memories for a scope as Memory Exchange Format (MXF) NDJSON.",
    annotations=types.ToolAnnotations(
        title="Export Memories (MXF)",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def amp_export(
    agent_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
    filters: Optional[Dict[str, Any]] = None,
    page_size: Optional[int] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    norm_scope = _normalize_scope(scope, agent_id)
    smriti = _get_agent_for_scope(norm_scope)

    # Materialise the deterministic order: ascending creation_time, ties by id.
    all_mems = list(smriti.palace.memories.values()) if hasattr(smriti.palace, "memories") else []
    all_mems.sort(key=lambda m: (m.creation_time, m.id))

    start = _decode_export_cursor(cursor) if cursor else 0
    if page_size is None or page_size <= 0:
        page_size = _DEFAULT_EXPORT_PAGE_SIZE

    lines: list[str] = []
    bytes_used = 0
    emitted = 0
    next_offset = start
    for idx in range(start, len(all_mems)):
        mem = all_mems[idx]
        row = _memory_to_result(mem, norm_scope)
        if not _row_matches_filters(row, filters):
            next_offset = idx + 1
            continue
        encoded = json.dumps(row, separators=(",", ":"), default=str) + "\n"
        encoded_len = len(encoded.encode("utf-8"))
        # Stop before exceeding the byte cap so the response stays bounded.
        # The current row will be emitted on the next page.
        if emitted > 0 and bytes_used + encoded_len > _EXPORT_PAGE_BYTE_CAP:
            break
        lines.append(encoded)
        bytes_used += encoded_len
        emitted += 1
        next_offset = idx + 1
        if emitted >= page_size:
            break

    response: Dict[str, Any] = {
        "ndjson": "".join(lines),
        "count": emitted,
    }
    if next_offset < len(all_mems):
        response["next_cursor"] = _encode_export_cursor(next_offset)
    return response


# ── amp.import ────────────────────────────────────────────────────────────────
#
# MXF import supports four on_conflict policies (skip / overwrite / fail_atomic /
# fail_fast) and two scope_remap modes (strict / inherit). The reference
# impl backs onto smriti-memcore.palace.place_memory, which does NOT provide
# transactional rollback — so fail_atomic returns not_supported per spec §3.3.5.
#
# Row-level failures (malformed JSON, schema-invalid rows, scope violations)
# are counted in the response's `failed` field with structured errors; they do
# NOT produce an HTTP/JSON-RPC error. Only request-level errors (missing
# top-level scope, unparseable ndjson container, unsupported on_conflict on
# this backend) raise AmpToolError.

_VALID_ON_CONFLICT = {"skip", "overwrite", "fail_atomic", "fail_fast"}
_VALID_SCOPE_REMAP = {"strict", "inherit"}
_IMPORT_ERROR_TRUNCATE_AT = 100


def _validate_import_row_scope(
    row_scope: Optional[Dict[str, Any]],
    request_scope: Dict[str, str],
    scope_remap: str,
) -> tuple[Optional[Dict[str, str]], Optional[str]]:
    """Resolve the effective stored scope for an imported row, or return an
    error message. Returns (effective_scope, None) on success, (None, msg) on
    rejection.

    Rules (spec §3.3.5):
      - Conflict on any key (row has key K=A, request has key K=B): always fail.
      - Row missing a key present in request scope:
          * strict (default): fail with invalid_request.
          * inherit: fill missing keys from request scope.
      - Row carrying extra keys not in request scope: allowed (extras don't
        block the match in either direction).
    """
    if row_scope is None:
        row_scope = {}
    if not isinstance(row_scope, dict):
        return None, "row scope must be an object"

    # Conflict check first — applies in both modes.
    for k, v in request_scope.items():
        if k in row_scope and row_scope[k] != v:
            return None, (
                f"row scope conflicts with request scope on key {k!r}: "
                f"row={row_scope[k]!r} vs request={v!r}"
            )

    # Missing-key check.
    missing = [k for k in request_scope if k not in row_scope]
    if missing:
        if scope_remap == "strict":
            return None, (
                f"row scope omits identity keys present in request scope "
                f"({sorted(missing)}); use scope_remap=inherit to allow"
            )
        # inherit
        effective = dict(row_scope)
        for k in missing:
            effective[k] = request_scope[k]
        return effective, None

    return dict(row_scope), None


@mcp.tool(
    name="amp.import",
    description="Ingest memories from Memory Exchange Format (MXF) NDJSON.",
    annotations=types.ToolAnnotations(
        title="Import Memories (MXF)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def amp_import(
    ndjson: str,
    agent_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
    on_conflict: str = "skip",
    scope_remap: str = "strict",
) -> Dict[str, Any]:
    if not isinstance(ndjson, str):
        raise AmpToolError("invalid_request", "ndjson must be a string")
    if on_conflict not in _VALID_ON_CONFLICT:
        raise AmpToolError("invalid_request", f"on_conflict must be one of {sorted(_VALID_ON_CONFLICT)}")
    if scope_remap not in _VALID_SCOPE_REMAP:
        raise AmpToolError("invalid_request", f"scope_remap must be one of {sorted(_VALID_SCOPE_REMAP)}")

    norm_scope = _normalize_scope(scope, agent_id)

    # fail_atomic requires transactional rollback. smriti-memcore.palace.place_memory
    # has no rollback primitive, so this backend MUST return not_supported per
    # spec §3.3.5 rather than fake atomicity with partial commits.
    if on_conflict == "fail_atomic":
        raise AmpToolError(
            "not_supported",
            "on_conflict=fail_atomic requires transactional rollback, which this "
            "backend (smriti-memcore) does not provide. Use fail_fast for "
            "non-transactional partial-progress semantics."
        )

    smriti = _get_agent_for_scope(norm_scope)
    palace = smriti.palace

    imported = 0
    skipped = 0
    failed = 0
    errors: list[Dict[str, Any]] = []

    def _record_failure(line_no: int, code: str, message: str) -> None:
        nonlocal failed
        failed += 1
        if len(errors) < _IMPORT_ERROR_TRUNCATE_AT:
            errors.append({"line": line_no, "amp_error_code": code, "message": message})

    # NDJSON lines. Trailing newline tolerated. Blank lines ignored.
    raw_lines = ndjson.split("\n")
    for line_no, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            _record_failure(line_no, "invalid_request", f"malformed JSON: {exc.msg}")
            if on_conflict == "fail_fast":
                break
            continue

        if not isinstance(row, dict):
            _record_failure(line_no, "invalid_request", "MXF row must be a JSON object")
            if on_conflict == "fail_fast":
                break
            continue

        row_id = row.get("id")
        content = row.get("content")
        if not isinstance(row_id, str) or not row_id:
            _record_failure(line_no, "invalid_request", "row missing required string field 'id'")
            if on_conflict == "fail_fast":
                break
            continue
        if not isinstance(content, str):
            _record_failure(line_no, "invalid_request", "row missing required string field 'content'")
            if on_conflict == "fail_fast":
                break
            continue

        # Scope reconciliation.
        effective_scope, err = _validate_import_row_scope(row.get("scope"), norm_scope, scope_remap)
        if err is not None:
            _record_failure(line_no, "invalid_request", err)
            if on_conflict == "fail_fast":
                break
            continue

        # Strategy-A storage: the row lands in the request scope's partition.
        # (The reference impl keys storage by request scope only; per-row
        # `effective_scope` is preserved on the response for cross-backend
        # round-trips.) If scope_remap=inherit and the row carried extras not
        # in the request scope, those extras are stored in the row's metadata
        # for fidelity — see _memory_to_result on re-export.

        existing = palace.get_memory(row_id)
        if existing is not None:
            if on_conflict == "skip":
                skipped += 1
                continue
            if on_conflict == "overwrite":
                # Drop the existing row before writing the new one. smriti's
                # forget() is the safe path (cascades through indices).
                try:
                    smriti.forget(row_id)
                except Exception as exc:  # smriti-internal; treat as row failure
                    _record_failure(line_no, "backend_error", f"forget on overwrite failed: {exc}")
                    if on_conflict == "fail_fast":
                        break
                    continue
            elif on_conflict == "fail_fast":
                _record_failure(line_no, "invalid_request", f"id collision: {row_id} already exists")
                break

        # Build a Memory dataclass and place it. We replay the row's own
        # creation_time and status if present to keep MXF round-trips lossless.
        try:
            source_value = row.get("source", "direct")
            try:
                source_enum = MemorySource(source_value)
            except (ValueError, TypeError):
                source_enum = MemorySource.DIRECT
            from smriti_memcore.models import MemoryStatus
            status_value = row.get("status", "active")
            try:
                status_enum = MemoryStatus(status_value)
            except (ValueError, TypeError):
                status_enum = MemoryStatus.ACTIVE
            ts_raw = row.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else datetime.now()
            except ValueError:
                ts = datetime.now()
            row_metadata = row.get("metadata") or {}
            if not isinstance(row_metadata, dict):
                row_metadata = {}
            # Preserve the row's own (possibly richer-than-request) scope on
            # the stored memory's metadata so a re-export is round-trip-faithful.
            row_metadata = dict(row_metadata)
            row_metadata.setdefault("_mxf_scope", effective_scope)

            memory = Memory(
                id=row_id,
                content=content,
                source=source_enum,
                status=status_enum,
                creation_time=ts,
                metadata=row_metadata,
            )
            palace.place_memory(memory)
            imported += 1
        except Exception as exc:
            _record_failure(line_no, "backend_error", f"place_memory failed: {exc}")
            if on_conflict == "fail_fast":
                break
            continue

    response: Dict[str, Any] = {
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
    }
    if errors:
        response["errors"] = errors
    return response



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
