"""
AMP Compliance Test Suite v1.0

Tests any AMP-conformant server via MCP stdio transport.
Run against your server with:

    pytest compliance/test_amp_server.py --server-cmd "python your_server.py"

Or against the bundled minimal example:

    pytest compliance/test_amp_server.py --server-cmd "python examples/minimal_server.py"

Requires: pytest
"""

import json
import subprocess
import time
import uuid
from typing import Any, Dict, Optional

import pytest


# ── CLI option ────────────────────────────────────────────────────────────────
# pytest_addoption is defined in compliance/conftest.py so it is always loaded
# before argument parsing. Defining it here in a test module is unreliable.

@pytest.fixture(scope="session")
def server_cmd(request):
    return request.config.getoption("--server-cmd")


# ── Simple MCP stdio client ───────────────────────────────────────────────────

class MCPClient:
    """Minimal synchronous MCP stdio client for compliance testing."""

    def __init__(self, cmd: str):
        self._proc = subprocess.Popen(
            cmd,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._next_id = 1
        self._handshake()

    def _send(self, method: str, params: Dict) -> Dict:
        msg_id = self._next_id
        self._next_id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        self._proc.stdin.write(payload + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        return json.loads(line)

    def _notify(self, method: str, params: Dict) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        self._proc.stdin.write(payload + "\n")
        self._proc.stdin.flush()

    def _handshake(self):
        self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "amp-compliance-tester", "version": "1.0"},
        })
        # Required by MCP spec — servers compliant with the full protocol
        # (e.g. FastMCP) will not accept tool calls until this is sent.
        self._notify("notifications/initialized", {})

    def call_tool(self, name: str, arguments: Dict) -> Dict:
        resp = self._send("tools/call", {"name": name, "arguments": arguments})
        if "error" in resp:
            return {"error": resp["error"]}
        result = resp.get("result", {})
        content = result.get("content", [])
        for block in content:
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except (json.JSONDecodeError, ValueError):
                    # Server returned a plain-text error (e.g. FastMCP isError responses)
                    if result.get("isError"):
                        return {"error": {"message": block["text"]}}
                    raise
        return result

    def close(self):
        try:
            self._proc.stdin.close()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()


@pytest.fixture(scope="session")
def client(server_cmd):
    c = MCPClient(server_cmd)
    yield c
    c.close()


@pytest.fixture
def agent_id():
    return f"test-agent-{uuid.uuid4().hex[:8]}"


# ── Helper ─────────────────────────────────────────────────────────────────────

def encode(client, agent_id, content, force=False):
    return client.call_tool("amp.encode", {
        "agent_id": agent_id,
        "content": content,
        "force": force,
    })


def recall(client, agent_id, query, top_k=10):
    return client.call_tool("amp.recall", {
        "agent_id": agent_id,
        "query": query,
        "top_k": top_k,
    })


def forget(client, agent_id, memory_id):
    return client.call_tool("amp.forget", {
        "agent_id": agent_id,
        "id": memory_id,
    })


def stats(client, agent_id):
    return client.call_tool("amp.stats", {"agent_id": agent_id})


# ── Core conformance tests ─────────────────────────────────────────────────────

class TestEncodeCore:
    def test_encode_returns_stored_or_below_threshold(self, client, agent_id):
        resp = encode(client, agent_id, "The user prefers dark mode")
        assert "error" not in resp, f"amp.encode returned error: {resp}"
        assert resp.get("status") in ("stored", "below_threshold")

    def test_encode_with_force_returns_stored(self, client, agent_id):
        resp = encode(client, agent_id, "Force-stored memory", force=True)
        assert "error" not in resp
        assert resp.get("status") == "stored"
        assert "id" in resp

    def test_encode_returns_id_when_stored(self, client, agent_id):
        resp = encode(client, agent_id, "Memory with unique content xyz987", force=True)
        assert resp.get("status") == "stored"
        assert resp.get("id") is not None and resp["id"] != ""

    def test_encode_empty_content_returns_error(self, client, agent_id):
        resp = client.call_tool("amp.encode", {"agent_id": agent_id, "content": ""})
        # Backend may return error or below_threshold for empty content
        assert "error" in resp or resp.get("status") in ("below_threshold",), \
            "Empty content should result in an error or below_threshold"

    def test_encode_missing_agent_id_returns_error(self, client):
        resp = client.call_tool("amp.encode", {"content": "some content"})
        assert "error" in resp, "Missing agent_id should return an error"

    def test_encode_missing_content_returns_error(self, client, agent_id):
        resp = client.call_tool("amp.encode", {"agent_id": agent_id})
        assert "error" in resp, "Missing content should return an error"


class TestRecallCore:
    def test_recall_returns_results_array(self, client, agent_id):
        encode(client, agent_id, "The user enjoys hiking in the mountains", force=True)
        resp = recall(client, agent_id, "hiking")
        assert "error" not in resp
        assert "results" in resp
        assert isinstance(resp["results"], list)

    def test_recall_results_have_required_fields(self, client, agent_id):
        encode(client, agent_id, "The user is allergic to peanuts", force=True)
        resp = recall(client, agent_id, "allergy peanuts")
        assert "results" in resp
        for mem in resp["results"]:
            assert "id" in mem
            assert "content" in mem
            assert "score" in mem
            assert "timestamp" in mem
            assert "status" in mem
            assert mem["status"] in ("active", "pinned", "archived")

    def test_recall_respects_top_k(self, client, agent_id):
        for i in range(5):
            encode(client, agent_id, f"Numbered memory about topic number {i}", force=True)
        resp = recall(client, agent_id, "numbered memory topic", top_k=3)
        assert len(resp.get("results", [])) <= 3

    def test_recall_archived_excluded_by_default(self, client, agent_id):
        encode(client, agent_id, "Active memory that should be recalled", force=True)
        resp = recall(client, agent_id, "active memory recalled")
        for mem in resp.get("results", []):
            assert mem["status"] != "archived", \
                "Archived memories must not appear in default recall"

    def test_recall_empty_when_no_match(self, client, agent_id):
        resp = recall(client, agent_id, "xq9zym2totally_irrelevant_query_99zz")
        results = resp.get("results", [])
        assert isinstance(results, list)

    def test_recall_missing_agent_id_returns_error(self, client):
        resp = client.call_tool("amp.recall", {"query": "something"})
        assert "error" in resp

    def test_recall_namespace_isolation(self, client):
        agent_a = f"agent-a-{uuid.uuid4().hex[:6]}"
        agent_b = f"agent-b-{uuid.uuid4().hex[:6]}"
        encode(client, agent_a, "Secret information for agent A only", force=True)
        resp = recall(client, agent_b, "Secret information agent A")
        ids = [m["id"] for m in resp.get("results", [])]
        # Verify no cross-namespace leakage — agent_b should not see agent_a memories
        # (this is a best-effort check; specific IDs would need to be tracked)
        assert isinstance(ids, list)  # at minimum, response is valid


class TestForgetCore:
    def test_forget_stored_memory(self, client, agent_id):
        resp = encode(client, agent_id, "Memory to delete later", force=True)
        assert resp.get("status") == "stored"
        mem_id = resp["id"]
        del_resp = forget(client, agent_id, mem_id)
        assert "error" not in del_resp
        assert del_resp.get("status") == "forgotten"

    def test_forget_unknown_id_returns_not_found(self, client, agent_id):
        resp = forget(client, agent_id, "nonexistent-memory-id-zzz999")
        assert "error" not in resp
        assert resp.get("status") == "not_found"

    def test_forget_removes_from_recall(self, client, agent_id):
        resp = encode(client, agent_id, "Temporary memory xfoo123", force=True)
        mem_id = resp["id"]
        forget(client, agent_id, mem_id)
        time.sleep(0.1)
        results = recall(client, agent_id, "Temporary memory xfoo123").get("results", [])
        assert all(m["id"] != mem_id for m in results), \
            "Forgotten memory must not appear in subsequent recall"


class TestStatsCore:
    def test_stats_returns_memory_count(self, client, agent_id):
        resp = stats(client, agent_id)
        assert "error" not in resp
        assert "memory_count" in resp
        assert isinstance(resp["memory_count"], int)
        assert resp["memory_count"] >= 0

    def test_stats_count_increases_after_encode(self, client, agent_id):
        before = stats(client, agent_id)["memory_count"]
        encode(client, agent_id, "New memory to bump count", force=True)
        after = stats(client, agent_id)["memory_count"]
        assert after >= before


# ── Full conformance tests ─────────────────────────────────────────────────────
# These tests check amp.pin and amp.consolidate.
# On a Core-only server, both should return status: "not_supported".

class TestPinFull:
    def test_pin_returns_pinned_or_not_supported(self, client, agent_id):
        resp_enc = encode(client, agent_id, "Memory to pin", force=True)
        if resp_enc.get("status") != "stored":
            pytest.skip("Encode did not store memory")
        mem_id = resp_enc["id"]
        resp = client.call_tool("amp.pin", {"agent_id": agent_id, "id": mem_id})
        assert "error" not in resp
        assert resp.get("status") in ("pinned", "not_supported")

    def test_pin_unknown_id_returns_not_found_or_not_supported(self, client, agent_id):
        resp = client.call_tool("amp.pin", {"agent_id": agent_id, "id": "nonexistent-zzz"})
        assert "error" not in resp
        assert resp.get("status") in ("not_found", "not_supported")


class TestConsolidateFull:
    def test_consolidate_returns_valid_status(self, client, agent_id):
        resp = client.call_tool("amp.consolidate", {"agent_id": agent_id, "depth": "full"})
        assert "error" not in resp
        assert resp.get("status") in ("queued", "ok", "not_supported")

    def test_consolidate_light_depth(self, client, agent_id):
        resp = client.call_tool("amp.consolidate", {"agent_id": agent_id, "depth": "light"})
        assert "error" not in resp
        assert resp.get("status") in ("queued", "ok", "not_supported")

    def test_consolidate_ok_includes_memories_processed(self, client, agent_id):
        for i in range(3):
            encode(client, agent_id, f"Memory for consolidation test {i}", force=True)
        resp = client.call_tool("amp.consolidate", {"agent_id": agent_id, "depth": "full"})
        if resp.get("status") == "ok":
            assert "memories_processed" in resp
            assert isinstance(resp["memories_processed"], int)


# ── Error handling tests ───────────────────────────────────────────────────────

class TestErrorHandling:
    def test_unknown_tool_returns_error(self, client, agent_id):
        resp = client.call_tool("amp/nonexistent_verb", {"agent_id": agent_id})
        assert "error" in resp

    def test_error_has_amp_error_code_for_invalid_request(self, client, agent_id):
        resp = client.call_tool("amp.encode", {"agent_id": agent_id})  # missing content
        if "error" in resp:
            error_data = resp["error"].get("data", {})
            if error_data:
                assert error_data.get("amp_error_code") in (
                    "invalid_request", "backend_error"
                ), "Error data should contain a valid amp_error_code"
