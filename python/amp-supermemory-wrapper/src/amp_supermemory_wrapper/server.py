"""
AMP SuperMemory Wrapper — Agent Memory Protocol server wrapping SuperMemory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests
from mcp.server.fastmcp import FastMCP
import mcp.types as types
from mcp.shared.exceptions import McpError

# ── §3.5 Error mapping ────────────────────────────────────────────────────────
_AMP_TO_JSONRPC = {
    "invalid_request": -32602,
    "not_found": -32001,
    "not_supported": -32002,
    "backend_error": -32000,
}

# Local in-memory cache to bridge the indexing latency of SuperMemory's async pipeline
_local_cache: Dict[str, Dict[str, Any]] = {}
# Tracks recently deleted document IDs to hide them from recall during async index updates
_deleted_ids: set[str] = set()


def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    import time
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, **kwargs)
            return resp
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == max_retries - 1:
                raise exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError("Request failed")


class AmpToolError(Exception):
    def __init__(self, amp_error_code: str, message: str):
        if amp_error_code not in _AMP_TO_JSONRPC:
            raise ValueError(f"unknown amp_error_code: {amp_error_code}")
        self.amp_error_code = amp_error_code
        self.message = message
        super().__init__(f"[{amp_error_code}] {message}")


# Monkeypatch types.Implementation to inject AMP conformance fields into the
# server capabilities handshake. NOTE: this relies on FastMCP/MCP internals;
# if types.Implementation gains __slots__ or strict kwarg validation in a future
# release, replace with a proper subclass or ServerCapabilities override.
original_impl_init = types.Implementation.__init__

def custom_impl_init(self, *args, **kwargs):
    kwargs.setdefault("amp_conformance", "core")
    kwargs.setdefault("amp_version", "1.1")
    original_impl_init(self, *args, **kwargs)

types.Implementation.__init__ = custom_impl_init


mcp = FastMCP(
    "amp-supermemory-wrapper",
    instructions=(
        "AMP (Agent Memory Protocol) Core-conformant memory server wrapping SuperMemory. "
        "Implements amp.encode, amp.recall, amp.forget, amp.stats, amp.batch_encode."
    ),
)


# Monkeypatch CallToolRequest handler to translate AmpToolError results into
# proper JSON-RPC error frames using the §3.5 mapping table. NOTE: this accesses
# the private `_mcp_server.request_handlers` dict — if FastMCP changes its
# internal request routing this line will need updating.
original_call_tool_handler = mcp._mcp_server.request_handlers[types.CallToolRequest]


def _extract_amp_error_from_text(text: str) -> Optional[Tuple[str, str]]:
    if not text:
        return None

    marker = "Error executing tool"
    if text.startswith(marker):
        colon = text.find(":")
        if colon != -1:
            text = text[colon + 1 :].lstrip()

    if text.startswith("["):
        end = text.find("]")
        if end >= 2:
            code = text[1:end]
            if code in _AMP_TO_JSONRPC:
                message = text[end + 1 :].lstrip()
                return code, message

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
ISOLATING_KEYS = ("agent_id", "group_id", "workspace_id", "user_id")
NON_ISOLATING_KEYS = ("session_id", "app_id", "org_id")
ALL_SCOPE_KEYS = ISOLATING_KEYS + NON_ISOLATING_KEYS


def _normalize_scope(
    scope: Optional[Dict[str, Any]],
    agent_id: Optional[str],
) -> Dict[str, str]:
    if scope is not None:
        if not isinstance(scope, dict):
            raise AmpToolError("invalid_request", "scope must be an object")
        unknown = set(scope.keys()) - set(ALL_SCOPE_KEYS)
        if unknown:
            raise AmpToolError(
                "invalid_request",
                f"scope contains unknown keys: {sorted(unknown)}",
            )
        normalized = {k: v for k, v in scope.items() if v is not None and v != ""}
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

    return {k: str(v) for k, v in normalized.items()}


def _scope_namespace_key(scope: Dict[str, str]) -> str:
    if list(scope.keys()) == ["agent_id"]:
        return scope["agent_id"]
    canonical = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"scope-{digest}"


# ── SuperMemory API Helpers ───────────────────────────────────────────────────
def _get_api_key() -> str:
    key = os.environ.get("SUPERMEMORY_API_KEY")
    if not key:
        raise AmpToolError(
            "invalid_request",
            "SUPERMEMORY_API_KEY environment variable is not set. "
            "Please configure it to use this server."
        )
    return key


def _get_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type": "application/json"
    }


# ── Recall Post-Filtering Helper ──────────────────────────────────────────────
def _parse_iso8601(value: Any, *, field: str) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AmpToolError("invalid_request", f"{field} must be an ISO 8601 string")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AmpToolError(
            "invalid_request", f"{field} is not a valid ISO 8601 timestamp: {exc}"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _eval_metadata_filter(stored: Any, op: str, value: Any) -> bool:
    if op == "eq":
        if isinstance(stored, bool) != isinstance(value, bool):
            return False
        return stored == value
    if op == "ne":
        if isinstance(stored, bool) != isinstance(value, bool):
            return False
        stored_is_num = isinstance(stored, (int, float)) and not isinstance(stored, bool)
        value_is_num = isinstance(value, (int, float)) and not isinstance(value, bool)
        stored_is_str = isinstance(stored, str)
        value_is_str = isinstance(value, str)
        if (stored_is_num != value_is_num) or (stored_is_str != value_is_str):
            return False
        return stored != value
    if op in ("gt", "gte", "lt", "lte"):
        if isinstance(stored, bool) or isinstance(value, bool):
            return False
        stored_kind = "num" if isinstance(stored, (int, float)) else ("str" if isinstance(stored, str) else None)
        value_kind = "num" if isinstance(value, (int, float)) else ("str" if isinstance(value, str) else None)
        if stored_kind is None or stored_kind != value_kind:
            return False
        try:
            if op == "gt":
                return stored > value
            if op == "gte":
                return stored >= value
            if op == "lt":
                return stored < value
            if op == "lte":
                return stored <= value
        except Exception:
            return False
    if op == "in":
        return stored in value
    if op == "contains":
        if isinstance(stored, list):
            return value in stored
        if isinstance(stored, str):
            return value in stored
    return False


def _apply_post_filters(
    results: list[dict],
    filters: Optional[dict],
) -> list[dict]:
    if not filters:
        return results

    filtered = []
    ts_after = _parse_iso8601(filters.get("timestamp_after"), field="filters.timestamp_after")
    ts_before = _parse_iso8601(filters.get("timestamp_before"), field="filters.timestamp_before")
    status_filter = filters.get("status")
    source_filter = filters.get("source")
    metadata_filters = filters.get("metadata_filters")

    if metadata_filters is not None:
        _validate_metadata_filters(metadata_filters)

    for item in results:
        # 1. Status Filter
        if status_filter:
            if item.get("status") != status_filter:
                continue
        else:
            if item.get("status") == "archived":
                continue

        # 2. Source Filter
        if source_filter and item.get("source") != source_filter:
            continue

        # 3. Timestamp Filter
        raw_ts = item.get("timestamp")
        if raw_ts:
            candidate = raw_ts[:-1] + "+00:00" if raw_ts.endswith("Z") else raw_ts
            try:
                row_ts = datetime.fromisoformat(candidate)
                if row_ts.tzinfo is None:
                    row_ts = row_ts.replace(tzinfo=timezone.utc)
                if ts_after is not None and not (row_ts > ts_after):
                    continue
                if ts_before is not None and not (row_ts < ts_before):
                    continue
            except ValueError:
                continue

        # 4. Metadata filters (strict-AND)
        if metadata_filters:
            item_metadata = item.get("metadata") or {}
            match = True
            for pred in metadata_filters:
                key = pred["key"]
                op = pred["operator"]
                val = pred["value"]
                if key not in item_metadata:
                    match = False
                    break
                if not _eval_metadata_filter(item_metadata[key], op, val):
                    match = False
                    break
            if not match:
                continue

        filtered.append(item)

    return filtered


def _validate_metadata_filters(filters: Any) -> None:
    if not isinstance(filters, list):
        raise AmpToolError(
            "invalid_request",
            "filters.metadata_filters must be an array of MetadataFilter objects",
        )
    if len(filters) > 32:
        raise AmpToolError(
            "invalid_request",
            "filters.metadata_filters length exceeds maxItems=32",
        )
    for idx, entry in enumerate(filters):
        if not isinstance(entry, dict):
            raise AmpToolError(
                "invalid_request",
                f"filters.metadata_filters[{idx}] must be an object",
            )
        for required in ("key", "operator", "value"):
            if required not in entry:
                raise AmpToolError(
                    "invalid_request",
                    f"filters.metadata_filters[{idx}].{required} is required",
                )
        key = entry["key"]
        op = entry["operator"]
        value = entry["value"]
        if not isinstance(key, str) or not key:
            raise AmpToolError(
                "invalid_request",
                f"filters.metadata_filters[{idx}].key must be a non-empty string",
            )
        if op not in ("eq", "ne", "gt", "gte", "lt", "lte", "in", "contains"):
            raise AmpToolError(
                "invalid_request",
                f"filters.metadata_filters[{idx}].operator '{op}' is not valid",
            )
        if op == "in":
            if not isinstance(value, list):
                raise AmpToolError(
                    "invalid_request",
                    f"filters.metadata_filters[{idx}].value must be an array when operator='in'",
                )
            for elem_idx, elem in enumerate(value):
                if not (isinstance(elem, (str, int, float)) and not isinstance(elem, bool)):
                    if not isinstance(elem, bool):
                        raise AmpToolError(
                            "invalid_request",
                            f"filters.metadata_filters[{idx}].value[{elem_idx}] must be scalar",
                        )
        else:
            if not (isinstance(value, (str, int, float)) or isinstance(value, bool)):
                raise AmpToolError(
                    "invalid_request",
                    f"filters.metadata_filters[{idx}].value must be scalar",
                )


def _validate_metadata_bag(metadata: Any, *, field: str) -> None:
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        raise AmpToolError("invalid_request", f"{field} must be an object")
    try:
        encoded = json.dumps(metadata, ensure_ascii=False)
    except Exception as exc:
        raise AmpToolError(
            "invalid_request", f"{field} is not JSON-serialisable: {exc}"
        )
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise AmpToolError(
            "invalid_request",
            f"{field} exceeds 64 KiB cap",
        )


# ── amp.encode ────────────────────────────────────────────────────────────────
@mcp.tool(
    name="amp.encode",
    description="Store a new memory in SuperMemory.",
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
    private: Optional[bool] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise AmpToolError("invalid_request", "content must be a non-empty string")

    if metadata is not None:
        _validate_metadata_bag(metadata, field="metadata")

    norm_scope = _normalize_scope(scope, agent_id)
    scope_key = _scope_namespace_key(norm_scope)

    meta = {}
    meta["amp.source"] = source
    meta["amp.status"] = "active"
    meta["_amp_scope"] = json.dumps(norm_scope)
    if metadata is not None:
        meta["amp.metadata_json"] = json.dumps(metadata)

    import random
    # Force uniqueness of identical content by appending invisible zero-width spaces
    content_to_send = content + ("\u200b" * random.randint(1, 100))

    payload = {
        "content": content_to_send,
        "containerTag": scope_key,
        "metadata": meta
    }

    try:
        resp = _request_with_retry(
            "POST",
            "https://api.supermemory.ai/v3/documents",
            headers=_get_headers(),
            json=payload,
            timeout=15
        )
        if resp.status_code != 200:
            raise AmpToolError("backend_error", f"SuperMemory API error: {resp.text}")
        data = resp.json()
    except requests.RequestException as exc:
        raise AmpToolError("backend_error", f"Network failure: {exc}")

    # Parse created document ID
    mem_id = data.get("id") or data.get("docId")
    if not mem_id:
        # Fallback to a hash if SuperMemory doesn't return ID
        mem_id = hashlib.md5(content.encode("utf-8")).hexdigest()

    response = {"id": mem_id, "status": "stored"}
    if private is not None:
        response["visibility"] = "private" if private else "shared"

    # Save to local cache to bridge indexing latency
    _local_cache[mem_id] = {
        "id": mem_id,
        "content": content,
        "score": 1.0,
        "source": source,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "scope": norm_scope,
        "metadata": metadata or {},
    }

    return response


# ── amp.recall ────────────────────────────────────────────────────────────────
@mcp.tool(
    name="amp.recall",
    description="Retrieve memories relevant to a query from SuperMemory.",
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
    norm_scope = _normalize_scope(scope, agent_id)
    scope_key = _scope_namespace_key(norm_scope)

    # SuperMemory v3 search limit must be between 1 and 100
    limit = min(top_k * 10, 100)

    payload = {
        "q": query,
        "containerTags": [scope_key],
        "limit": limit
    }

    try:
        resp = _request_with_retry(
            "POST",
            "https://api.supermemory.ai/v3/search",
            headers=_get_headers(),
            json=payload,
            timeout=15
        )
        if resp.status_code != 200:
            raise AmpToolError("backend_error", f"SuperMemory API error: {resp.text}")
        data = resp.json()
    except requests.RequestException as exc:
        raise AmpToolError("backend_error", f"Network failure: {exc}")

    raw_results = data.get("results") or []

    # Map to AMP results
    amp_results = []
    seen_ids = set()
    for item in raw_results:
        content = item.get("memory") or item.get("chunk") or item.get("content") or ""
        # Strip any zero-width spaces we added for uniqueness before returning
        content = content.replace("\u200b", "")
        m_id = item.get("id") or ""
        if m_id in _deleted_ids:
            continue
        score = item.get("similarity") or 0.0
        meta = item.get("metadata") or {}

        status = meta.get("amp.status") or "active"
        source = meta.get("amp.source") or "direct"
        ts = item.get("updatedAt") or item.get("createdAt") or datetime.now(timezone.utc).isoformat()

        orig_scope_str = meta.get("_amp_scope")
        orig_scope = norm_scope
        if orig_scope_str:
            try:
                orig_scope = json.loads(orig_scope_str)
            except Exception:
                pass

        user_metadata = {}
        if "amp.metadata_json" in meta:
            try:
                user_metadata = json.loads(meta["amp.metadata_json"])
            except Exception:
                pass
        else:
            user_metadata = {k: v for k, v in meta.items() if k not in {
                "user_id", "agent_id", "run_id", "hash", "data", "created_at", "updated_at",
                "text_lemmatized", "actor_id", "role", "_amp_scope", "amp.source", "amp.status"
            }}

        amp_results.append({
            "id": m_id,
            "content": content,
            "score": score,
            "source": source,
            "timestamp": ts,
            "status": status,
            "scope": orig_scope,
            "metadata": user_metadata,
        })
        seen_ids.add(m_id)

    # Blend in-memory cached items that have not been indexed yet
    for m_id, cached in list(_local_cache.items()):
        if _scope_namespace_key(cached["scope"]) == scope_key:
            if m_id not in seen_ids:
                query_normalized = query.lower()
                if query_normalized == "*" or query_normalized in cached["content"].lower():
                    amp_results.append(cached)

    # Apply filters
    filtered_results = _apply_post_filters(amp_results, filters)

    # Sort descending by score
    filtered_results.sort(key=lambda m: m["score"], reverse=True)

    return {"results": filtered_results[:top_k]}


# ── amp.forget ────────────────────────────────────────────────────────────────
@mcp.tool(
    name="amp.forget",
    description="Permanently delete a memory from SuperMemory.",
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
    norm_scope = _normalize_scope(scope, agent_id)
    scope_key = _scope_namespace_key(norm_scope)

    # Enforce existence and scope-isolation check before deleting
    try:
        resp = _request_with_retry(
            "GET",
            f"https://api.supermemory.ai/v3/documents/{id}",
            headers=_get_headers(),
            timeout=10
        )
        if resp.status_code != 200:
            return {"status": "not_found"}
        doc = resp.json()
        
        # Check if containerTag matches scope_key
        doc_tag = doc.get("containerTag")
        doc_tags = doc.get("containerTags") or []
        if doc_tag != scope_key and scope_key not in doc_tags:
            return {"status": "not_found"}
    except Exception:
        return {"status": "not_found"}

    try:
        del_resp = _request_with_retry(
            "DELETE",
            f"https://api.supermemory.ai/v3/documents/{id}",
            headers=_get_headers(),
            timeout=15
        )
        if del_resp.status_code == 404:
            return {"status": "not_found"}
        elif del_resp.status_code != 200:
            # If the document is still processing/indexing, SuperMemory returns a 400 with a custom message.
            # Catch this and mark it forgotten locally since the deletion request will finalize.
            if "still processing" in del_resp.text:
                _local_cache.pop(id, None)
                _deleted_ids.add(id)
                return {"status": "forgotten"}
            raise AmpToolError("backend_error", f"SuperMemory delete failed: {del_resp.text}")
        
        # Clear from local cache if present
        _local_cache.pop(id, None)
        _deleted_ids.add(id)
        return {"status": "forgotten"}
    except requests.RequestException as exc:
        raise AmpToolError("backend_error", f"Network failure: {exc}")


# ── amp.stats ─────────────────────────────────────────────────────────────────
@mcp.tool(
    name="amp.stats",
    description="Return SuperMemory statistics for the given scope.",
    annotations=types.ToolAnnotations(
        title="Stats",
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
    scope_key = _scope_namespace_key(norm_scope)

    payload = {
        "q": "*",
        "containerTags": [scope_key]
    }

    try:
        resp = _request_with_retry(
            "POST",
            "https://api.supermemory.ai/v3/search",
            headers=_get_headers(),
            json=payload,
            timeout=15
        )
        if resp.status_code != 200:
            raise AmpToolError("backend_error", f"SuperMemory list failed: {resp.text}")
        data = resp.json()
    except requests.RequestException as exc:
        raise AmpToolError("backend_error", f"Network failure: {exc}")

    count = data.get("total", 0)

    # Add count of unindexed local cache items under the same scope
    indexed_ids = {item.get("documentId") or item.get("id") for item in data.get("results", [])}
    for m_id, cached in _local_cache.items():
        if _scope_namespace_key(cached["scope"]) == scope_key:
            if m_id not in indexed_ids:
                count += 1

    return {
        "memory_count": count,
        "unconsolidated_count": 0,
        "metadata": {}
    }


# ── amp.batch_encode ──────────────────────────────────────────────────────────
@mcp.tool(
    name="amp.batch_encode",
    description="Store multiple memories in a single round-trip in SuperMemory.",
    annotations=types.ToolAnnotations(
        title="Batch Encode Memories",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def amp_batch_encode(
    entries: list[dict],
    agent_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(entries, list):
        raise AmpToolError("invalid_request", "entries must be an array")
    if len(entries) > 1000:
        raise AmpToolError("invalid_request", "batch size exceeds 1000")

    norm_scope = _normalize_scope(scope, agent_id)
    scope_key = _scope_namespace_key(norm_scope)

    results = []
    counts = {"stored": 0, "below_threshold": 0, "duplicate": 0, "failed": 0}

    for entry in entries:
        if not isinstance(entry, dict):
            results.append({"status": "invalid_request", "message": "entry must be an object"})
            counts["failed"] += 1
            continue

        allowed = {"content", "source", "force", "metadata"}
        extra = set(entry.keys()) - allowed
        if extra:
            results.append({
                "status": "invalid_request",
                "message": f"unsupported row keys: {sorted(extra)}"
            })
            counts["failed"] += 1
            continue

        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            results.append({"status": "invalid_request", "message": "content must be non-empty"})
            counts["failed"] += 1
            continue

        source = entry.get("source", "direct")
        if source not in ("direct", "user_stated", "inferred", "external"):
            results.append({
                "status": "invalid_request",
                "message": f"source '{source}' is not a valid MemorySource enum value"
            })
            counts["failed"] += 1
            continue

        force = entry.get("force", False)
        if not isinstance(force, bool):
            results.append({"status": "invalid_request", "message": "force must be a boolean"})
            counts["failed"] += 1
            continue

        meta = entry.get("metadata")
        if meta is not None:
            try:
                _validate_metadata_bag(meta, field="metadata")
            except AmpToolError as exc:
                results.append({"status": "invalid_request", "message": exc.message})
                counts["failed"] += 1
                continue

        row_meta = {}
        row_meta["amp.source"] = entry.get("source", "direct")
        row_meta["amp.status"] = "active"
        row_meta["_amp_scope"] = json.dumps(norm_scope)
        if meta is not None:
            row_meta["amp.metadata_json"] = json.dumps(meta)

        import random
        # Force uniqueness of duplicate contents
        content_to_send = content + ("\u200b" * random.randint(1, 100))

        payload = {
            "content": content_to_send,
            "containerTag": scope_key,
            "metadata": row_meta
        }

        try:
            resp = _request_with_retry(
                "POST",
                "https://api.supermemory.ai/v3/documents",
                headers=_get_headers(),
                json=payload,
                timeout=15
            )
            if resp.status_code != 200:
                results.append({"status": "backend_error", "message": resp.text})
                counts["failed"] += 1
            else:
                data = resp.json()
                mem_id = data.get("id") or data.get("docId") or hashlib.md5(content.encode("utf-8")).hexdigest()
                results.append({"id": mem_id, "status": "stored"})
                counts["stored"] += 1

                # Save to local cache
                _local_cache[mem_id] = {
                    "id": mem_id,
                    "content": content,
                    "score": 1.0,
                    "source": row_meta["amp.source"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "active",
                    "scope": norm_scope,
                    "metadata": meta or {},
                }
        except Exception as exc:
            results.append({"status": "backend_error", "message": str(exc)})
            counts["failed"] += 1

    return {"results": results, "summary": counts}


# Full conformance dummy verbs
@mcp.tool(
    name="amp.pin",
    description="Mark a memory as permanent (not_supported on SuperMemory core wrapper).",
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
    return {"status": "not_supported"}


@mcp.tool(
    name="amp.consolidate",
    description="Trigger consolidation (not_supported on SuperMemory core wrapper).",
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
    return {"status": "not_supported"}


# amp.update is NOT registered as a tool since we do not support update operations
def amp_update(
    id: str,
    agent_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
    content: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    metadata_mode: str = "merge",
) -> Dict[str, Any]:
    return {"status": "not_supported"}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
