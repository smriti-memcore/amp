"""
AMP Minimal Example Server

A Core-conformant AMP server using in-memory storage.
Implements: amp.encode, amp.recall, amp.forget, amp.stats

Run with:
    python examples/minimal_server.py

Connect any MCP client to this process via stdio.
"""

import json
import sys
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional


# ── In-memory store ───────────────────────────────────────────────────────────

_store: Dict[str, Dict] = {}  # agent_id → {memory_id: memory_dict}


def _ns(agent_id: str) -> Dict:
    if agent_id not in _store:
        _store[agent_id] = {}
    return _store[agent_id]


def _encode(agent_id: str, content: str, source: Optional[str] = None, force: bool = False, private: bool = False) -> Dict:
    # Note: 'private' and 'visibility' are included for v1.0 backwards compatibility testing
    if not content or not content.strip():
        return {"status": "below_threshold"}
    mem_id = str(uuid.uuid4())
    _ns(agent_id)[mem_id] = {
        "id": mem_id,
        "content": content,
        "source": source or "direct",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "score": 1.0,
        "metadata": {},
    }
    visibility = "private" if private else "shared"
    return {"id": mem_id, "status": "stored", "visibility": visibility}


def _recall(agent_id: str, query: str, top_k: int = 10, filters: Optional[Dict] = None) -> Dict:
    ns = _ns(agent_id)
    filters = filters or {}
    status_filter = filters.get("status")
    query_lower = query.lower()
    results = []
    for mem in ns.values():
        if status_filter:
            if mem["status"] != status_filter:
                continue
        else:
            if mem["status"] == "archived":
                continue
        score = _score(mem["content"], query_lower)
        if score > 0:
            results.append({**mem, "visibility": "shared", "score": score})
    results.sort(key=lambda m: m["score"], reverse=True)
    return {"results": results[:top_k]}


def _score(content: str, query_lower: str) -> float:
    tokens = query_lower.split()
    if not tokens:
        return 0.0
    content_lower = content.lower()
    hits = sum(1 for t in tokens if t in content_lower)
    return hits / len(tokens)


def _forget(agent_id: str, memory_id: str) -> Dict:
    ns = _ns(agent_id)
    if memory_id in ns:
        del ns[memory_id]
        return {"status": "forgotten"}
    return {"status": "not_found"}


def _stats(agent_id: str) -> Dict:
    ns = _ns(agent_id)
    count = sum(1 for m in ns.values() if m["status"] in ("active", "pinned"))
    return {"memory_count": count, "unconsolidated_count": 0, "metadata": {}}


# ── MCP dispatch ─────────────────────────────────────────────────────────────

def _tool_result(data: Dict) -> Dict:
    return {
        "content": [{"type": "text", "text": json.dumps(data)}],
        "isError": False,
    }


def _error(code: int, message: str, amp_code: str = "backend_error") -> Dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "data": {"amp_error_code": amp_code, "message": message},
        }
    }


TOOLS = [
    {
        "name": "amp.encode",
        "description": "Store a new memory for an agent.",
        "inputSchema": {
            "type": "object",
            "required": ["agent_id", "content"],
            "properties": {
                "agent_id": {"type": "string"},
                "content": {"type": "string"},
                "source": {"type": "string"},
                "force": {"type": "boolean", "default": False},
                "private": {"type": "boolean", "default": False},
                "metadata": {"type": "object"},
            },
        },
    },
    {
        "name": "amp.recall",
        "description": "Retrieve memories relevant to a query.",
        "inputSchema": {
            "type": "object",
            "required": ["agent_id", "query"],
            "properties": {
                "agent_id": {"type": "string"},
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 10},
                "filters": {"type": "object"},
            },
        },
    },
    {
        "name": "amp.forget",
        "description": "Permanently delete a memory.",
        "inputSchema": {
            "type": "object",
            "required": ["agent_id", "id"],
            "properties": {
                "agent_id": {"type": "string"},
                "id": {"type": "string"},
            },
        },
    },
    {
        "name": "amp.stats",
        "description": "Return backend statistics for an agent namespace.",
        "inputSchema": {
            "type": "object",
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string"}
            },
        },
    },
    {
        "name": "amp.pin",
        "description": "Mark a memory as permanent (not_supported on this Core server).",
        "inputSchema": {
            "type": "object",
            "required": ["agent_id", "id"],
            "properties": {
                "agent_id": {"type": "string"},
                "id": {"type": "string"},
            },
        },
    },
    {
        "name": "amp.consolidate",
        "description": "Trigger consolidation (not_supported on this Core server).",
        "inputSchema": {
            "type": "object",
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string"},
                "depth": {"type": "string", "enum": ["full", "light"]},
            },
        },
    },
]


def handle_request(req: Dict) -> Dict:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    def respond(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def error(code, message, amp_code="backend_error"):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": code,
                "message": message,
                "data": {"amp_error_code": amp_code, "message": message},
            },
        }

    if method == "initialize":
        return respond({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "amp-minimal-example",
                "version": "1.0",
                "amp_conformance": "core",
                "amp_version": "1.0",
            },
        })

    if method == "tools/list":
        return respond({"tools": TOOLS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments", {})

        if tool_name == "amp.encode":
            if "agent_id" not in args or "content" not in args:
                return error(-32000, "Missing required field", "invalid_request")
            result = _encode(
                args["agent_id"],
                args["content"],
                args.get("source"),
                args.get("force", False),
                args.get("private", False),
            )
            return respond(_tool_result(result))

        if tool_name == "amp.recall":
            if "agent_id" not in args or "query" not in args:
                return error(-32000, "Missing required field", "invalid_request")
            result = _recall(
                args["agent_id"],
                args["query"],
                args.get("top_k", 10),
                args.get("filters"),
            )
            return respond(_tool_result(result))

        if tool_name == "amp.forget":
            if "agent_id" not in args or "id" not in args:
                return error(-32000, "Missing required field", "invalid_request")
            result = _forget(args["agent_id"], args["id"])
            return respond(_tool_result(result))

        if tool_name == "amp.stats":
            if "agent_id" not in args:
                return error(-32000, "Missing required field", "invalid_request")
            result = _stats(args["agent_id"])
            return respond(_tool_result(result))

        if tool_name in ("amp.pin", "amp.consolidate"):
            return respond(_tool_result({"status": "not_supported"}))

        return error(-32601, f"Unknown tool: {tool_name}", "backend_error")

    if method == "notifications/initialized":
        return None  # no response for notifications

    return error(-32601, f"Method not found: {method}", "backend_error")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
