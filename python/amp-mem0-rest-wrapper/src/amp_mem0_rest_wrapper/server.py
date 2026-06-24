"""
AMP Mem0 REST Wrapper — Agent Memory Protocol REST server wrapping Mem0.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mem0 import MemoryClient, Memory
from mem0.llms.base import LLMBase
from mem0.utils.factory import LlmFactory, EmbedderFactory
from mem0.embeddings.mock import MockEmbeddings
from mem0.memory.main import lemmatize_for_bm25

# Setup Logger
logger = logging.getLogger("amp-mem0-rest-wrapper")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Agent Memory Protocol (AMP) v1.2-draft REST Wrapper for Mem0",
    version="1.2.0-draft",
)

# ── Global configuration state ────────────────────────────────────────────────
_mem0_client = None
_storage_path = os.environ.get("AMP_STORAGE_PATH", os.path.expanduser("~/.amp/mem0"))
_config_path = os.environ.get("MEM0_CONFIG_PATH")


# ── AMP Error Handling & Mapping ──────────────────────────────────────────────
class AmpRestError(Exception):
    def __init__(self, amp_error_code: str, message: str):
        if amp_error_code not in ("invalid_request", "not_found", "not_supported", "backend_error"):
            raise ValueError(f"unknown amp_error_code: {amp_error_code}")
        self.amp_error_code = amp_error_code
        self.message = message
        super().__init__(f"[{amp_error_code}] {message}")


_AMP_TO_HTTP = {
    "invalid_request": 400,
    "not_found": 404,
    "not_supported": 501,
    "backend_error": 500,
}


@app.exception_handler(AmpRestError)
async def amp_error_handler(request: Request, exc: AmpRestError):
    http_code = _AMP_TO_HTTP[exc.amp_error_code]
    return JSONResponse(
        status_code=http_code,
        content={"amp_error_code": exc.amp_error_code, "message": exc.message},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Format pydantic validation errors nicely matching invalid_request
    message = str(exc)
    return JSONResponse(
        status_code=400,
        content={"amp_error_code": "invalid_request", "message": f"Validation Error: {message}"},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Internal Server Error")
    return JSONResponse(
        status_code=500,
        content={"amp_error_code": "backend_error", "message": f"Internal Error: {str(exc)}"},
    )


# ── Scope normalization ────────────────────────────────────────────────────────
ISOLATING_KEYS = ("agent_id", "group_id", "workspace_id", "user_id")
NON_ISOLATING_KEYS = ("session_id", "app_id", "org_id")
ALL_SCOPE_KEYS = ISOLATING_KEYS + NON_ISOLATING_KEYS


def _normalize_scope(
    scope: Optional[Dict[str, Any]],
    agent_id: Optional[str],
) -> Dict[str, str]:
    if scope is not None:
        if not isinstance(scope, dict):
            raise AmpRestError("invalid_request", "scope must be an object")
        unknown = set(scope.keys()) - set(ALL_SCOPE_KEYS)
        if unknown:
            raise AmpRestError(
                "invalid_request",
                f"scope contains unknown keys: {sorted(unknown)}",
            )
        for k, v in scope.items():
            if isinstance(v, (dict, list)):
                raise AmpRestError(
                    "invalid_request",
                    f"scope key '{k}' contains nested structure which is not allowed",
                )
        normalized = {k: v for k, v in scope.items() if v is not None and v != ""}
        if agent_id:
            existing = normalized.get("agent_id")
            if existing and existing != agent_id:
                raise AmpRestError(
                    "invalid_request",
                    "agent_id provided both as scope.agent_id and top-level disagree",
                )
            normalized.setdefault("agent_id", agent_id)
    elif agent_id:
        normalized = {"agent_id": agent_id}
    else:
        raise AmpRestError(
            "invalid_request",
            "either scope or agent_id is required",
        )

    if not any(normalized.get(k) for k in ISOLATING_KEYS):
        raise AmpRestError(
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


def merge_patch(target: Any, patch: Any) -> Any:
    if isinstance(patch, dict):
        if not isinstance(target, dict):
            target = {}
        result = dict(target)
        for k, v in patch.items():
            if v is None:
                result.pop(k, None)
            else:
                result[k] = merge_patch(result.get(k), v)
        return result
    else:
        return patch


# ── Mem0 Client Initialization ────────────────────────────────────────────────
def _get_mem0():
    global _mem0_client
    if _mem0_client is not None:
        return _mem0_client

    api_key = os.environ.get("MEM0_API_KEY")
    if api_key:
        _mem0_client = MemoryClient(api_key=api_key)
        logger.info("Initialized Mem0 platform client via API key.")
    else:
        # Load from config-path if provided
        config = None
        if _config_path and os.path.exists(_config_path):
            logger.info(f"Loading custom Mem0 configuration from {_config_path}")
            with open(_config_path, "r") as f:
                if _config_path.endswith((".yaml", ".yml")):
                    config = yaml.safe_load(f)
                else:
                    config = json.load(f)
        
        if config:
            _mem0_client = Memory.from_config(config)
            logger.info("Initialized local/custom Mem0 client using config file.")
        else:
            # Fallback to Mock LLM and Embedder to avoid external OpenAI calls in local default mode
            class MockLLM(LLMBase):
                def generate_response(self, messages, tools=None, tool_choice="auto", **kwargs):
                    user_msg = ""
                    for m in messages:
                        if m.get("role") == "user":
                            user_msg = m.get("content") or ""
                    
                    lines = user_msg.split("\n")
                    user_lines = []
                    for line in lines:
                        if line.lstrip().startswith("user:"):
                            user_lines.append(line.lstrip()[5:].strip())
                    if user_lines:
                        text = " ".join(user_lines)
                    else:
                        text = user_msg.strip()
                    
                    return json.dumps({"memory": [{"text": text, "event": "ADD"}]})

            original_llm_create = LlmFactory.create
            def custom_llm_create(provider_name: str, config=None, **kwargs):
                if provider_name == "openai":
                    return MockLLM(config)
                return original_llm_create(provider_name, config, **kwargs)
            LlmFactory.create = custom_llm_create

            original_embedder_create = EmbedderFactory.create
            def custom_embedder_create(provider_name, config, vector_config=None):
                if provider_name == "openai":
                    return MockEmbeddings()
                return original_embedder_create(provider_name, config, vector_config)
            EmbedderFactory.create = custom_embedder_create

            os.makedirs(_storage_path, exist_ok=True)
            default_config = {
                "llm": {
                    "provider": "openai",
                    "config": {"model": "mock-model"}
                },
                "embedder": {
                    "provider": "openai",
                    "config": {"model": "mock-model", "embedding_model_dims": 10}
                },
                "vector_store": {
                    "provider": "chroma",
                    "config": {"path": os.path.join(_storage_path, "chroma")}
                },
                "history_db_path": os.path.join(_storage_path, "history.db")
            }
            _mem0_client = Memory.from_config(default_config)
            logger.info(f"Initialized local fallback Mem0 client with storage path {_storage_path}.")

            # Monkeypatch Memory._update_memory to support metadata deletion and replace mode.
            def custom_update_memory(self, memory_id, data, existing_embeddings, metadata=None):
                try:
                    existing_memory = self.vector_store.get(vector_id=memory_id)
                except Exception:
                    raise ValueError(f"Error getting memory with ID {memory_id}. Please provide a valid 'memory_id'")

                if existing_memory is None:
                    raise ValueError(f"Memory with id {memory_id} not found. Please provide a valid 'memory_id'")

                prev_value = existing_memory.payload.get("data")

                SYSTEM_KEYS = {
                    "user_id", "agent_id", "run_id", "hash", "data",
                    "created_at", "updated_at", "text_lemmatized", "actor_id", "role",
                    "_amp_scope", "amp.source", "amp.status", "amp.metadata_json"
                }

                new_metadata = {}
                for k, v in existing_memory.payload.items():
                    if k in SYSTEM_KEYS:
                        new_metadata[k] = deepcopy(v)

                if metadata is not None:
                    new_metadata.update(metadata)

                new_metadata["data"] = data
                new_metadata["hash"] = hashlib.md5(data.encode()).hexdigest()
                new_metadata["text_lemmatized"] = lemmatize_for_bm25(data)
                new_metadata["created_at"] = existing_memory.payload.get("created_at")
                new_metadata["updated_at"] = datetime.now(timezone.utc).isoformat()

                if "actor_id" in existing_memory.payload:
                    new_metadata["actor_id"] = existing_memory.payload["actor_id"]

                if data in existing_embeddings:
                    embeddings = existing_embeddings[data]
                else:
                    embeddings = self.embedding_model.embed(data, "update")

                self.vector_store.update(
                    vector_id=memory_id,
                    vector=embeddings,
                    payload=new_metadata,
                )

                self.db.add_history(
                    memory_id,
                    prev_value,
                    data,
                    "UPDATE",
                    created_at=new_metadata["created_at"],
                    updated_at=new_metadata["updated_at"],
                    actor_id=new_metadata.get("actor_id"),
                    role=new_metadata.get("role"),
                )

                session_filters = {k: new_metadata[k] for k in ("user_id", "agent_id", "run_id") if new_metadata.get(k)}
                self._remove_memory_from_entity_store(memory_id, session_filters)
                self._link_entities_for_memory(memory_id, data, session_filters)

                return memory_id

            Memory._update_memory = custom_update_memory

    return _mem0_client


def _is_platform_mode() -> bool:
    return os.environ.get("MEM0_API_KEY") is not None


# ── Recall Post-Filtering Helper ──────────────────────────────────────────────
def _parse_iso8601(value: Any, *, field: str) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AmpRestError("invalid_request", f"{field} must be an ISO 8601 string")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AmpRestError(
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
    results: List[dict],
    filters: Optional[dict],
) -> List[dict]:
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
        if status_filter:
            if item.get("status") != status_filter:
                continue
        else:
            if item.get("status") == "archived":
                continue

        if source_filter and item.get("source") != source_filter:
            continue

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
        raise AmpRestError(
            "invalid_request",
            "filters.metadata_filters must be an array of MetadataFilter objects",
        )
    if len(filters) > 32:
        raise AmpRestError(
            "invalid_request",
            "filters.metadata_filters length exceeds maxItems=32",
        )
    for idx, entry in enumerate(filters):
        if not isinstance(entry, dict):
            raise AmpRestError(
                "invalid_request",
                f"filters.metadata_filters[{idx}] must be an object",
            )
        for required in ("key", "operator", "value"):
            if required not in entry:
                raise AmpRestError(
                    "invalid_request",
                    f"filters.metadata_filters[{idx}].{required} is required",
                )
        key = entry["key"]
        op = entry["operator"]
        value = entry["value"]
        if not isinstance(key, str) or not key:
            raise AmpRestError(
                "invalid_request",
                f"filters.metadata_filters[{idx}].key must be a non-empty string",
            )
        if op not in ("eq", "ne", "gt", "gte", "lt", "lte", "in", "contains"):
            raise AmpRestError(
                "invalid_request",
                f"filters.metadata_filters[{idx}].operator '{op}' is not valid",
            )
        if op == "in":
            if not isinstance(value, list):
                raise AmpRestError(
                    "invalid_request",
                    f"filters.metadata_filters[{idx}].value must be an array when operator='in'",
                )
            for elem_idx, elem in enumerate(value):
                if not (isinstance(elem, (str, int, float)) and not isinstance(elem, bool)):
                    if not isinstance(elem, bool):
                        raise AmpRestError(
                            "invalid_request",
                            f"filters.metadata_filters[{idx}].value[{elem_idx}] must be scalar",
                        )
        else:
            if not (isinstance(value, (str, int, float)) or isinstance(value, bool)):
                raise AmpRestError(
                    "invalid_request",
                    f"filters.metadata_filters[{idx}].value must be scalar",
                )


def _validate_metadata_bag(metadata: Any, *, field: str) -> None:
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        raise AmpRestError("invalid_request", f"{field} must be an object")
    try:
        encoded = json.dumps(metadata, ensure_ascii=False)
    except Exception as exc:
        raise AmpRestError(
            "invalid_request", f"{field} is not JSON-serialisable: {exc}"
        )
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise AmpRestError(
            "invalid_request",
            f"{field} exceeds 64 KiB cap",
        )


# ── Pydantic Request/Response Models ──────────────────────────────────────────
class ScopeModel(BaseModel):
    agent_id: Optional[str] = None
    group_id: Optional[str] = None
    workspace_id: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    app_id: Optional[str] = None
    org_id: Optional[str] = None


class EncodeRequestModel(BaseModel):
    scope: Optional[ScopeModel] = None
    agent_id: Optional[str] = None
    content: str
    source: str = "direct"
    force: bool = False
    metadata: Optional[Dict[str, Any]] = None


class EncodeResponseModel(BaseModel):
    id: Optional[str] = None
    status: str


class MetadataFilterModel(BaseModel):
    key: str
    operator: str
    value: Any


class RecallFiltersModel(BaseModel):
    status: Optional[str] = None
    source: Optional[str] = None
    timestamp_after: Optional[str] = None
    timestamp_before: Optional[str] = None
    metadata_filters: Optional[List[MetadataFilterModel]] = None


class RecallRequestModel(BaseModel):
    scope: Optional[ScopeModel] = None
    agent_id: Optional[str] = None
    query: str
    top_k: int = 10
    filters: Optional[RecallFiltersModel] = None


class MemoryResultModel(BaseModel):
    id: str
    content: str
    score: float
    source: str
    timestamp: str
    status: str
    scope: ScopeModel
    metadata: Dict[str, Any]


class RecallResponseModel(BaseModel):
    results: List[MemoryResultModel]


class ScopeEnvelopeModel(BaseModel):
    scope: Optional[ScopeModel] = None
    agent_id: Optional[str] = None


class ForgetResponseModel(BaseModel):
    status: str


class UpdateRequestModel(BaseModel):
    scope: Optional[ScopeModel] = None
    agent_id: Optional[str] = None
    content: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    metadata_mode: str = "merge"


class UpdateResponseModel(BaseModel):
    status: str
    id: str


class BatchEntryModel(BaseModel):
    content: str
    source: str = "direct"
    force: bool = False
    metadata: Optional[Dict[str, Any]] = None


class BatchEncodeRequestModel(BaseModel):
    scope: Optional[ScopeModel] = None
    agent_id: Optional[str] = None
    entries: List[BatchEntryModel]


class BatchEntryResultModel(BaseModel):
    id: Optional[str] = None
    status: str
    message: Optional[str] = None


class BatchEncodeResponseModel(BaseModel):
    results: List[BatchEntryResultModel]
    summary: Dict[str, int]


# ── REST API Routes ───────────────────────────────────────────────────────────

@app.post("/v1/memories", response_model=EncodeResponseModel)
async def encode_memory(req: EncodeRequestModel):
    if not req.content.strip():
        raise AmpRestError("invalid_request", "content must be a non-empty string")

    if req.source not in ("direct", "user_stated", "inferred", "external"):
        raise AmpRestError("invalid_request", f"source '{req.source}' is not a valid MemorySource enum value")

    if req.metadata is not None:
        _validate_metadata_bag(req.metadata, field="metadata")

    scope_dict = req.scope.dict(exclude_none=True) if req.scope else None
    norm_scope = _normalize_scope(scope_dict, req.agent_id)
    scope_key = _scope_namespace_key(norm_scope)

    client = _get_mem0()

    meta = {
        "amp.source": req.source,
        "amp.status": "active",
        "_amp_scope": json.dumps(norm_scope)
    }
    if req.metadata is not None:
        meta["amp.metadata_json"] = json.dumps(req.metadata)

    try:
        res = client.add(req.content, user_id=scope_key, metadata=meta, infer=not req.force)
    except Exception as exc:
        raise AmpRestError("backend_error", f"Mem0 add failed: {exc}")

    mem_id = None
    if isinstance(res, list) and res:
        mem_id = res[0].get("id")
    elif isinstance(res, dict):
        results_list = res.get("results")
        if isinstance(results_list, list) and results_list:
            mem_id = results_list[0].get("id")
        else:
            mem_id = res.get("id") or res.get("event_id")

    if not mem_id:
        return {"status": "below_threshold"}

    return {"id": mem_id, "status": "stored"}


@app.post("/v1/memories/recall", response_model=RecallResponseModel)
async def recall_memories(req: RecallRequestModel):
    scope_dict = req.scope.dict(exclude_none=True) if req.scope else None
    norm_scope = _normalize_scope(scope_dict, req.agent_id)
    scope_key = _scope_namespace_key(norm_scope)

    client = _get_mem0()
    limit = min(req.top_k * 10, 200)

    try:
        if _is_platform_mode():
            res = client.search(req.query, user_id=scope_key, limit=limit)
        else:
            res = client.search(req.query, filters={"user_id": scope_key}, limit=limit)
    except Exception as exc:
        raise AmpRestError("backend_error", f"Mem0 search failed: {exc}")

    raw_results = []
    if isinstance(res, list):
        raw_results = res
    elif isinstance(res, dict):
        raw_results = res.get("results") or []

    amp_results = []
    for item in raw_results:
        content = item.get("memory") or item.get("content") or ""
        m_id = item.get("id") or ""
        score = item.get("score") or item.get("similarity") or 0.0
        meta = item.get("metadata") or {}
        status = meta.get("amp.status") or "active"
        source = meta.get("amp.source") or "direct"
        ts = item.get("created_at") or item.get("updated_at") or datetime.now(timezone.utc).isoformat()

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

    filters_dict = req.filters.dict(exclude_none=True) if req.filters else None
    filtered_results = _apply_post_filters(amp_results, filters_dict)
    filtered_results.sort(key=lambda m: m["score"], reverse=True)

    return {"results": filtered_results[:req.top_k]}


@app.delete("/v1/memories/{id}", response_model=ForgetResponseModel)
async def forget_memory(id: str, req: ScopeEnvelopeModel):
    scope_dict = req.scope.dict(exclude_none=True) if req.scope else None
    norm_scope = _normalize_scope(scope_dict, req.agent_id)
    scope_key = _scope_namespace_key(norm_scope)

    client = _get_mem0()

    # Scope isolation check (Existence-leak discipline: return 200 not_found if missing/wrong scope)
    try:
        item = client.get(id)
        if not item:
            return {"status": "not_found"}
        stored_user_id = item.get("user_id") or item.get("metadata", {}).get("user_id")
        if stored_user_id != scope_key:
            return {"status": "not_found"}
    except Exception:
        return {"status": "not_found"}

    try:
        client.delete(id)
        return {"status": "forgotten"}
    except Exception as exc:
        raise AmpRestError("backend_error", f"Mem0 delete failed: {exc}")


@app.patch("/v1/memories/{id}", response_model=UpdateResponseModel)
async def update_memory(id: str, req: UpdateRequestModel):
    if req.content is not None and (not isinstance(req.content, str) or not req.content.strip()):
        raise AmpRestError("invalid_request", "content must be a non-empty string")

    scope_dict = req.scope.dict(exclude_none=True) if req.scope else None
    norm_scope = _normalize_scope(scope_dict, req.agent_id)
    scope_key = _scope_namespace_key(norm_scope)

    client = _get_mem0()

    # Verify existence and scope isolation (200 not_found if missing)
    try:
        item = client.get(id)
        if not item:
            return {"status": "not_found", "id": id}
        stored_user_id = item.get("user_id") or item.get("metadata", {}).get("user_id")
        if stored_user_id != scope_key:
            return {"status": "not_found", "id": id}
    except Exception:
        return {"status": "not_found", "id": id}

    existing_content = item.get("memory") or item.get("content") or ""
    raw_meta = item.get("metadata") or {}
    existing_metadata = {}
    if "amp.metadata_json" in raw_meta:
        try:
            existing_metadata = json.loads(raw_meta["amp.metadata_json"])
        except Exception:
            pass
    else:
        existing_metadata = {k: v for k, v in raw_meta.items() if k not in {
            "user_id", "agent_id", "run_id", "hash", "data", "created_at", "updated_at",
            "text_lemmatized", "actor_id", "role", "_amp_scope", "amp.source", "amp.status"
        }}

    new_content = req.content if req.content is not None else existing_content

    if req.metadata is not None:
        _validate_metadata_bag(req.metadata, field="metadata")
        if req.metadata_mode not in ("merge", "replace"):
            raise AmpRestError("invalid_request", f"metadata_mode must be 'merge' or 'replace'")
            
        if req.metadata_mode == "merge":
            new_user_metadata = merge_patch(existing_metadata, req.metadata)
        else:
            new_user_metadata = dict(req.metadata)
    else:
        new_user_metadata = existing_metadata

    if new_content == existing_content and new_user_metadata == existing_metadata:
        return {"status": "no_change", "id": id}

    meta_for_update = {}
    meta_for_update["amp.metadata_json"] = json.dumps(new_user_metadata)
    for k in ("amp.source", "amp.status", "_amp_scope"):
        if k in raw_meta:
            meta_for_update[k] = raw_meta[k]

    try:
        try:
            client.update(id, data=new_content, metadata=meta_for_update)
        except TypeError:
            client.update(id, text=new_content, metadata=meta_for_update)
    except Exception as exc:
        raise AmpRestError("backend_error", f"Mem0 update failed: {exc}")

    return {"status": "updated", "id": id}


@app.get("/v1/memories/stats")
async def stats_memory(
    agent_id: Optional[str] = None,
    group_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    app_id: Optional[str] = None,
    org_id: Optional[str] = None,
):
    scope = {
        "agent_id": agent_id,
        "group_id": group_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "session_id": session_id,
        "app_id": app_id,
        "org_id": org_id,
    }
    cleaned_scope = {k: v for k, v in scope.items() if v is not None}
    norm_scope = _normalize_scope(cleaned_scope, None)
    scope_key = _scope_namespace_key(norm_scope)

    client = _get_mem0()

    try:
        if _is_platform_mode():
            res = client.get_all(user_id=scope_key)
        else:
            res = client.get_all(filters={"user_id": scope_key})
    except Exception as exc:
        raise AmpRestError("backend_error", f"Mem0 get_all failed: {exc}")

    count = 0
    if isinstance(res, dict):
        count = res.get("count") or len(res.get("results", []))
    elif isinstance(res, list):
        count = len(res)

    return {
        "memory_count": count,
        "unconsolidated_count": 0,
        "metadata": {}
    }


@app.put("/v1/memories/{id}/pin")
async def pin_memory(id: str, req: ScopeEnvelopeModel):
    # Mem0 core wrapper does not support pinning
    return {"status": "not_supported"}


@app.post("/v1/memories/consolidate")
async def consolidate_memory(req: ScopeEnvelopeModel):
    raise AmpRestError("not_supported", "Consolidation not supported by this backend")


@app.post("/v1/memories/batch_encode", response_model=BatchEncodeResponseModel)
async def batch_encode_memories(req: BatchEncodeRequestModel):
    if len(req.entries) > 1000:
        raise AmpRestError("invalid_request", "batch size exceeds 1000")

    scope_dict = req.scope.dict(exclude_none=True) if req.scope else None
    norm_scope = _normalize_scope(scope_dict, req.agent_id)
    scope_key = _scope_namespace_key(norm_scope)

    results = []
    counts = {"stored": 0, "below_threshold": 0, "duplicate": 0, "failed": 0}

    client = _get_mem0()

    for entry in req.entries:
        if not entry.content.strip():
            results.append({"status": "invalid_request", "message": "content must be non-empty"})
            counts["failed"] += 1
            continue

        if entry.source not in ("direct", "user_stated", "inferred", "external"):
            results.append({
                "status": "invalid_request",
                "message": f"source '{entry.source}' is not a valid MemorySource enum value"
            })
            counts["failed"] += 1
            continue

        if entry.metadata is not None:
            try:
                _validate_metadata_bag(entry.metadata, field="metadata")
            except AmpRestError as exc:
                results.append({"status": "invalid_request", "message": exc.message})
                counts["failed"] += 1
                continue

        row_meta = {
            "amp.source": entry.source,
            "amp.status": "active",
            "_amp_scope": json.dumps(norm_scope)
        }
        if entry.metadata is not None:
            row_meta["amp.metadata_json"] = json.dumps(entry.metadata)

        try:
            res = client.add(entry.content, user_id=scope_key, metadata=row_meta, infer=not entry.force)
            mem_id = None
            if isinstance(res, list) and res:
                mem_id = res[0].get("id")
            elif isinstance(res, dict):
                results_list = res.get("results")
                if isinstance(results_list, list) and results_list:
                    mem_id = results_list[0].get("id")
                else:
                    mem_id = res.get("id") or res.get("event_id")

            if not mem_id:
                results.append({"status": "below_threshold"})
                counts["below_threshold"] += 1
            else:
                results.append({"id": mem_id, "status": "stored"})
                counts["stored"] += 1
        except Exception as exc:
            results.append({"status": "backend_error", "message": str(exc)})
            counts["failed"] += 1

    return {"results": results, "summary": counts}


# ── CLI Entrypoint ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AMP Mem0 REST Wrapper Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    parser.add_argument("--storage-path", help="Local storage directory path")
    parser.add_argument("--config-path", help="Custom Mem0 configuration file path (YAML or JSON)")
    args, unknown = parser.parse_known_args()

    if args.storage_path:
        global _storage_path
        _storage_path = os.path.abspath(args.storage_path)

    if args.config_path:
        global _config_path
        _config_path = os.path.abspath(args.config_path)

    # Pre-initialize Mem0 so that config or mock client loads immediately
    _get_mem0()

    logger.info(f"Starting AMP Mem0 REST Wrapper Server on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
