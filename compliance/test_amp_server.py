"""
AMP Compliance Test Suite v1.0

Tests any AMP-conformant server via MCP stdio transport.
Run against your server with:

    pytest compliance/test_amp_server.py --server-cmd "python3 your_server.py"

Or against the bundled minimal example:

    pytest compliance/test_amp_server.py --server-cmd "python3 examples/minimal_server.py"

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
            stderr=subprocess.DEVNULL,
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
        resp = self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "amp-compliance-tester", "version": "1.0"},
        })
        self.server_info = resp.get("result", {}).get("serverInfo", {})
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
        enc_resp = encode(client, agent_a, "Secret information for agent A only xns001", force=True)
        assert enc_resp.get("status") == "stored", "Encode must succeed for isolation test to be meaningful"
        leaked_id = enc_resp["id"]
        resp = recall(client, agent_b, "Secret information agent A xns001")
        ids = [m["id"] for m in resp.get("results", [])]
        assert leaked_id not in ids, (
            f"Namespace isolation failure: agent_b recalled memory {leaked_id} belonging to agent_a"
        )


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


# ── Extended encode tests ─────────────────────────────────────────────────────

class TestEncodeExtended:
    def test_encode_whitespace_only_returns_below_threshold(self, client, agent_id):
        resp = client.call_tool("amp.encode", {"agent_id": agent_id, "content": "   "})
        assert "error" in resp or resp.get("status") == "below_threshold", \
            "Whitespace-only content should be treated the same as empty"

    def test_encode_with_source_param_accepted(self, client, agent_id):
        resp = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": "Memory with explicit source label",
            "source": "user_stated",
            "force": True,
        })
        assert "error" not in resp
        assert resp.get("status") == "stored"

    def test_encode_content_is_retrievable(self, client, agent_id):
        token = f"xtok_{uuid.uuid4().hex[:8]}"
        encode(client, agent_id, f"The user likes {token} very much", force=True)
        resp = recall(client, agent_id, token)
        contents = [m["content"] for m in resp.get("results", [])]
        assert any(token in c for c in contents), \
            "Encoded content must be retrievable by a matching recall query"

    def test_encode_same_content_twice_creates_unique_ids(self, client, agent_id):
        r1 = client.call_tool("amp.encode", {"agent_id": agent_id, "content": "Duplicate content xdup99", "force": True})
        r2 = client.call_tool("amp.encode", {"agent_id": agent_id, "content": "Duplicate content xdup99", "force": True})
        if r1.get("status") == "stored" and r2.get("status") == "stored":
            assert r1["id"] != r2["id"], "Each amp.encode call must produce a unique memory ID"


# ── Extended recall tests ──────────────────────────────────────────────────────

class TestRecallExtended:
    def test_recall_results_sorted_by_score_descending(self, client, agent_id):
        encode(client, agent_id, "cats dogs birds fish xsort1", force=True)
        encode(client, agent_id, "cats only xsort2", force=True)
        resp = recall(client, agent_id, "cats dogs birds fish")
        scores = [m["score"] for m in resp.get("results", [])]
        assert scores == sorted(scores, reverse=True), \
            "Recall results must be ordered by score descending"

    def test_recall_score_is_numeric_and_nonnegative(self, client, agent_id):
        encode(client, agent_id, "score validation memory xscorechk", force=True)
        resp = recall(client, agent_id, "xscorechk")
        for mem in resp.get("results", []):
            assert isinstance(mem["score"], (int, float)), "score must be numeric"
            assert mem["score"] >= 0, "score must be non-negative"

    def test_recall_top_k_one_returns_at_most_one(self, client, agent_id):
        for i in range(3):
            encode(client, agent_id, f"topk test item number {i} xtopk", force=True)
        resp = recall(client, agent_id, "xtopk topk test", top_k=1)
        assert len(resp.get("results", [])) <= 1, "top_k=1 must return at most 1 result"

    def test_recall_with_status_filter_active(self, client, agent_id):
        encode(client, agent_id, "status filter active memory xsfilter", force=True)
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "xsfilter",
            "filters": {"status": "active"},
        })
        assert "error" not in resp
        for mem in resp.get("results", []):
            assert mem["status"] == "active", \
                "Status filter active must exclude non-active memories"


# ── Extended stats tests ───────────────────────────────────────────────────────

class TestStatsExtended:
    def test_stats_count_decreases_after_forget(self, client, agent_id):
        resp = encode(client, agent_id, "Memory to forget for stats xstatdec", force=True)
        assert resp.get("status") == "stored"
        before = stats(client, agent_id)["memory_count"]
        forget(client, agent_id, resp["id"])
        after = stats(client, agent_id)["memory_count"]
        assert after < before, "memory_count must decrease after forgetting a memory"

    def test_stats_unconsolidated_count_is_valid_if_present(self, client, agent_id):
        resp = stats(client, agent_id)
        if "unconsolidated_count" in resp:
            assert isinstance(resp["unconsolidated_count"], int)
            assert resp["unconsolidated_count"] >= 0


# ── Tools list tests ───────────────────────────────────────────────────────────

class TestRecallFilters:
    def test_recall_source_filter_accepted(self, client, agent_id):
        encode(client, agent_id, "source filter test memory xsrcflt", force=True)
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "xsrcflt",
            "filters": {"source": "direct"},
        })
        assert "error" not in resp
        assert "results" in resp

    def test_recall_timestamp_after_filter_accepted(self, client, agent_id):
        encode(client, agent_id, "timestamp filter test memory xtsflt", force=True)
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "xtsflt",
            "filters": {"timestamp_after": "2020-01-01T00:00:00Z"},
        })
        assert "error" not in resp
        assert "results" in resp

    def test_recall_timestamp_before_filter_accepted(self, client, agent_id):
        encode(client, agent_id, "timestamp before test memory xtsbflt", force=True)
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "xtsbflt",
            "filters": {"timestamp_before": "2099-01-01T00:00:00Z"},
        })
        assert "error" not in resp
        assert "results" in resp


class TestNamespaceAccess:
    def test_new_namespace_does_not_error(self, client):
        fresh_agent = f"fresh-agent-{uuid.uuid4().hex}"
        resp = encode(client, fresh_agent, "First memory for brand new namespace", force=True)
        assert "error" not in resp, \
            "Backend MUST NOT return an error for an unknown (new) agent_id"

    def test_cross_namespace_forget_returns_not_found(self, client):
        agent_a = f"ns-a-{uuid.uuid4().hex[:6]}"
        agent_b = f"ns-b-{uuid.uuid4().hex[:6]}"
        enc = encode(client, agent_a, "Agent A private memory xcrossns", force=True)
        assert enc.get("status") == "stored"
        mem_id = enc["id"]
        resp = forget(client, agent_b, mem_id)
        assert "error" not in resp
        assert resp.get("status") == "not_found", (
            "Forgetting another agent's memory ID MUST return not_found, not an auth error"
        )


class TestMissingRequiredFields:
    def test_forget_missing_agent_id_returns_error(self, client):
        resp = client.call_tool("amp.forget", {"id": "some-id"})
        assert "error" in resp, "amp.forget with missing agent_id must return an error"

    def test_forget_missing_id_returns_error(self, client, agent_id):
        resp = client.call_tool("amp.forget", {"agent_id": agent_id})
        assert "error" in resp, "amp.forget with missing id must return an error"

    def test_stats_missing_agent_id_returns_error(self, client):
        resp = client.call_tool("amp.stats", {})
        assert "error" in resp, "amp.stats with missing agent_id must return an error"

    def test_invalid_request_maps_to_minus_32602(self, client, agent_id):
        """Per spec §3.5, invalid_request → JSON-RPC -32602 (Invalid params).

        Backends may also use -32600 for malformed JSON-RPC frames, but a
        well-formed call with a missing required parameter is a parameter
        validation error and so maps to -32602.
        """
        resp = client.call_tool("amp.encode", {"agent_id": agent_id})  # missing content
        assert "error" in resp, "missing required parameter must surface an error"
        code = resp["error"].get("code")
        assert code in (-32602, -32600), (
            f"invalid_request MUST map to JSON-RPC -32602 (or -32600 for transport-level "
            f"malformed frames); got {code}"
        )
        # The structured AMP payload MUST also be present on the data field.
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request", (
            f"error.data.amp_error_code MUST be 'invalid_request'; got {data!r}"
        )


class TestServerManifest:
    def test_initialize_response_has_amp_conformance(self, client):
        assert "amp_conformance" in client.server_info, \
            "Server manifest MUST include amp_conformance field"
        assert client.server_info["amp_conformance"] in ("core", "full"), \
            "amp_conformance must be 'core' or 'full'"

    def test_initialize_response_has_amp_version(self, client):
        assert "amp_version" in client.server_info, \
            "Server manifest MUST include amp_version field"
        assert isinstance(client.server_info["amp_version"], str)
        assert client.server_info["amp_version"] != "", \
            "amp_version must be a non-empty string"


class TestConsolidateExtended:
    def test_consolidate_no_depth_param_defaults_gracefully(self, client, agent_id):
        resp = client.call_tool("amp.consolidate", {"agent_id": agent_id})
        assert "error" not in resp
        assert resp.get("status") in ("queued", "ok", "not_supported"), \
            "consolidate without depth param must use default and return a valid status"


class TestContentFidelity:
    def test_recalled_content_matches_encoded_exactly(self, client, agent_id):
        content = f"Exact content fidelity test xfidelity_{uuid.uuid4().hex[:8]}"
        enc = encode(client, agent_id, content, force=True)
        assert enc.get("status") == "stored"
        resp = recall(client, agent_id, f"xfidelity")
        matched = [m for m in resp.get("results", []) if m.get("id") == enc["id"]]
        assert matched, "Encoded memory must be retrievable by its content token"
        assert matched[0]["content"] == content, \
            "Recalled content must exactly match the content that was encoded"


class TestToolsList:
    def test_tools_list_contains_core_verbs(self, client):
        resp = client._send("tools/list", {})
        tools = resp.get("result", {}).get("tools", [])
        names = {t["name"] for t in tools}
        core_verbs = {"amp.encode", "amp.recall", "amp.forget", "amp.stats"}
        missing = core_verbs - names
        assert not missing, f"Server is missing required core AMP tools: {missing}"

    def test_tools_list_all_entries_have_required_fields(self, client):
        resp = client._send("tools/list", {})
        tools = resp.get("result", {}).get("tools", [])
        assert tools, "tools/list must return a non-empty list"
        for tool in tools:
            assert "name" in tool, f"Tool entry missing 'name': {tool}"
            assert "description" in tool, f"Tool {tool.get('name')} missing 'description'"
            assert "inputSchema" in tool, f"Tool {tool.get('name')} missing 'inputSchema'"

    def test_tools_list_input_schemas_have_agent_id(self, client):
        resp = client._send("tools/list", {})
        tools = resp.get("result", {}).get("tools", [])
        amp_tools = [t for t in tools if t["name"].startswith("amp.")]
        for tool in amp_tools:
            props = tool.get("inputSchema", {}).get("properties", {})
            assert "agent_id" in props, \
                f"Tool {tool['name']} inputSchema must include agent_id property"


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


# -- v1.1 Scope & Error-Mapping Tests --------------------------------------

class TestScopeValidation:
    """Coverage for the v1.1 multi-dimensional Scope block (spec section 2.3)."""

    def test_encode_with_scope_object_succeeds(self, client):
        """A v1.1-native caller passing `scope` (no legacy agent_id) must work."""
        scope = {"agent_id": f"scope-only-{uuid.uuid4().hex[:8]}"}
        resp = client.call_tool("amp.encode", {
            "scope": scope,
            "content": "scope-object encode path xscope1",
            "force": True,
        })
        assert "error" not in resp, f"scope-only encode must succeed: {resp}"
        assert resp.get("status") == "stored"
        assert "id" in resp

    def test_recall_with_scope_object_finds_memory(self, client):
        """Round-trip: encode under scope X, recall under scope X."""
        scope = {"agent_id": f"scope-rt-{uuid.uuid4().hex[:8]}"}
        token = f"xscope_rt_{uuid.uuid4().hex[:8]}"
        enc = client.call_tool("amp.encode", {
            "scope": scope, "content": f"scope round trip {token}", "force": True,
        })
        assert enc.get("status") == "stored"
        rec = client.call_tool("amp.recall", {"scope": scope, "query": token})
        assert "error" not in rec
        assert any(m.get("id") == enc["id"] for m in rec.get("results", []))

    def test_scope_without_isolating_key_returns_invalid_request(self, client):
        """Per section 2.3, at least one of {agent_id, group_id, workspace_id, user_id}
        MUST be present. session_id / app_id / org_id alone are not isolating.
        """
        resp = client.call_tool("amp.encode", {
            "scope": {"session_id": "sess-1", "app_id": "app-1"},
            "content": "should fail validation",
        })
        assert "error" in resp, "scope with only non-isolating keys must fail"
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_no_scope_and_no_agent_id_returns_invalid_request(self, client):
        resp = client.call_tool("amp.encode", {"content": "scopeless"})
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_scope_isolation(self, client):
        """A memory stored under scope A MUST NOT be visible under scope B."""
        scope_a = {"agent_id": f"iso-a-{uuid.uuid4().hex[:8]}"}
        scope_b = {"agent_id": f"iso-b-{uuid.uuid4().hex[:8]}"}
        token = f"xiso_{uuid.uuid4().hex[:8]}"
        enc = client.call_tool("amp.encode", {
            "scope": scope_a, "content": f"isolated under A {token}", "force": True,
        })
        assert enc.get("status") == "stored"
        rec = client.call_tool("amp.recall", {"scope": scope_b, "query": token})
        assert all(m.get("id") != enc["id"] for m in rec.get("results", [])), \
            "scope B must not see memories stored under scope A"

    def test_recall_result_carries_scope_field(self, client):
        scope = {"agent_id": f"scope-result-{uuid.uuid4().hex[:8]}"}
        token = f"xresult_{uuid.uuid4().hex[:8]}"
        enc = client.call_tool("amp.encode", {
            "scope": scope, "content": f"scope echo {token}", "force": True,
        })
        rec = client.call_tool("amp.recall", {"scope": scope, "query": token})
        matched = [m for m in rec.get("results", []) if m.get("id") == enc["id"]]
        assert matched, "encoded memory must be retrievable"
        assert "scope" in matched[0], \
            "MemoryResult MUST carry a 'scope' field (spec section 3.3.2 / MemoryResult schema)"


class TestErrorMapping:
    """Spec section 3.5 -- strict 1:1 mapping between amp_error_code and JSON-RPC code."""

    def test_not_found_returned_as_status_or_minus_32001(self, client, agent_id):
        resp = client.call_tool("amp.forget", {"agent_id": agent_id, "id": "does-not-exist"})
        assert resp.get("status") == "not_found" or (
            "error" in resp and resp["error"].get("code") == -32001
        ), f"missing id must return status=not_found or JSON-RPC -32001; got {resp}"

    def test_error_data_carries_amp_error_code(self, client, agent_id):
        resp = client.call_tool("amp.encode", {"agent_id": agent_id})  # missing content
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert "amp_error_code" in data, \
            f"error.data must include amp_error_code per section 3.5; got data={data!r}"
        assert data["amp_error_code"] in (
            "invalid_request", "not_found", "not_supported", "backend_error"
        )


class TestDeprecatedVisibilityEcho:
    """Spec section 5 -- deprecated `private`/`visibility` fields are echoed only when
    the legacy parameter is actually supplied. v1.1-native callers should
    receive responses free of deprecated noise."""

    def test_v11_native_response_omits_visibility(self, client, agent_id):
        resp = encode(client, agent_id, "no-visibility echo test xvis1", force=True)
        assert resp.get("status") == "stored"
        assert "visibility" not in resp, (
            "v1.1-native encode (no `private` param) MUST NOT echo deprecated visibility"
        )

    def test_legacy_private_true_echoes_private_visibility(self, client, agent_id):
        resp = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": "legacy private echo xvis2",
            "private": True,
            "force": True,
        })
        assert resp.get("status") == "stored"
        assert resp.get("visibility") == "private", (
            "legacy `private: true` must round-trip as visibility='private' per section 5"
        )

    def test_legacy_private_false_echoes_shared_visibility(self, client, agent_id):
        resp = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": "legacy shared echo xvis3",
            "private": False,
            "force": True,
        })
        assert resp.get("status") == "stored"
        assert resp.get("visibility") == "shared"


# -- MCP Tool Annotations (spec section 3.4) ---------------------------------

# Expected annotation values per spec section 3.4 verb table. Kept in this file
# rather than imported so the compliance suite remains self-contained — any
# backend claiming v1.1 conformance must publish these exact hints.
EXPECTED_ANNOTATIONS = {
    "amp.encode":      {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "amp.recall":      {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "amp.forget":      {"readOnlyHint": False, "destructiveHint": True,  "idempotentHint": True,  "openWorldHint": False},
    "amp.consolidate": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    "amp.pin":         {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "amp.stats":       {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
}


class TestToolAnnotations:
    """MCP 2025-03-26 tool annotations (spec section 3.4).

    AMP v1.1 servers MUST publish ToolAnnotations on every verb so hosts can
    consistently gate destructive verbs, cache read-only results, and surface
    catalogs. Per the MCP spec these are hints, not enforced contracts —
    backends still validate server-side — but the published values themselves
    are conformance-relevant: a server claiming amp.recall is NOT read-only,
    or amp.forget is NOT destructive, would mislead every host that consumes
    the manifest.
    """

    def _tools_by_name(self, client):
        resp = client._send("tools/list", {})
        tools = resp.get("result", {}).get("tools", [])
        return {t["name"]: t for t in tools}

    def test_every_amp_tool_publishes_annotations(self, client):
        tools = self._tools_by_name(client)
        for verb in EXPECTED_ANNOTATIONS:
            assert verb in tools, f"tools/list missing required verb: {verb}"
            ann = tools[verb].get("annotations")
            assert ann is not None, (
                f"{verb} MUST publish a ToolAnnotations block per spec section 3.4"
            )

    def test_annotation_hint_values_match_spec(self, client):
        tools = self._tools_by_name(client)
        for verb, expected in EXPECTED_ANNOTATIONS.items():
            ann = tools[verb].get("annotations") or {}
            for hint, want in expected.items():
                got = ann.get(hint)
                assert got == want, (
                    f"{verb}.{hint} MUST be {want} per spec section 3.4 verb table; "
                    f"got {got!r}"
                )

    def test_only_amp_forget_is_destructive(self, client):
        """Destructive hint is the most safety-critical annotation; the table
        in section 3.4 names only amp.forget. A backend that flags any other
        verb destructive will cause hosts to gate it behind a confirmation
        prompt that should not exist."""
        tools = self._tools_by_name(client)
        for verb in EXPECTED_ANNOTATIONS:
            destructive = (tools[verb].get("annotations") or {}).get("destructiveHint")
            if verb == "amp.forget":
                assert destructive is True, "amp.forget MUST be flagged destructive"
            else:
                assert destructive is False, (
                    f"{verb} MUST NOT be flagged destructive (only amp.forget is)"
                )

    def test_read_only_verbs(self, client):
        """recall, stats (and, when present, export) are the only read-only verbs."""
        tools = self._tools_by_name(client)
        for verb, expected in EXPECTED_ANNOTATIONS.items():
            ro = (tools[verb].get("annotations") or {}).get("readOnlyHint")
            assert ro == expected["readOnlyHint"], (
                f"{verb}.readOnlyHint mismatch: want {expected['readOnlyHint']}, got {ro}"
            )

    def test_export_and_import_annotations_when_present(self, client):
        """amp.export and amp.import are Full-conformance verbs; backends that
        ship them MUST publish the section 3.4 annotation values."""
        tools = self._tools_by_name(client)
        if "amp.export" in tools:
            ann = tools["amp.export"].get("annotations") or {}
            assert ann.get("readOnlyHint") is True
            assert ann.get("destructiveHint") is False
            assert ann.get("idempotentHint") is True
            assert ann.get("openWorldHint") is False
        if "amp.import" in tools:
            ann = tools["amp.import"].get("annotations") or {}
            assert ann.get("readOnlyHint") is False
            assert ann.get("destructiveHint") is False
            assert ann.get("idempotentHint") is False
            assert ann.get("openWorldHint") is False


# -- MXF export/import (spec sections 3.3.4 / 3.3.5) -------------------------

def _has_verb(client, verb):
    """Helper: check whether a verb is advertised. Tests below skip themselves
    on backends that respond with no tool entry — Core backends are not
    required to implement export/import."""
    resp = client._send("tools/list", {})
    names = {t["name"] for t in resp.get("result", {}).get("tools", [])}
    return verb in names


class TestExportBasic:
    def test_export_skipped_if_not_advertised(self, client, agent_id):
        if not _has_verb(client, "amp.export"):
            pytest.skip("backend does not advertise amp.export (Core conformance OK)")

    def test_export_empty_scope_returns_zero(self, client):
        if not _has_verb(client, "amp.export"):
            pytest.skip("amp.export not advertised")
        # Fresh scope with nothing in it.
        scope = {"agent_id": f"export-empty-{uuid.uuid4().hex[:8]}"}
        resp = client.call_tool("amp.export", {"scope": scope})
        assert "error" not in resp
        assert resp.get("count") == 0
        assert resp.get("ndjson") == ""
        assert "next_cursor" not in resp

    def test_export_emits_ndjson_rows(self, client, agent_id):
        if not _has_verb(client, "amp.export"):
            pytest.skip("amp.export not advertised")
        # Seed three memories.
        for i in range(3):
            enc = encode(client, agent_id, f"export seed memory xexp_{uuid.uuid4().hex[:6]} #{i}", force=True)
            assert enc.get("status") == "stored"
        resp = client.call_tool("amp.export", {"agent_id": agent_id})
        assert "error" not in resp
        assert resp.get("count", 0) >= 3, f"export must return at least the 3 seeded rows: {resp}"
        ndjson = resp.get("ndjson", "")
        assert ndjson, "ndjson MUST be present and non-empty when count > 0 (spec section 3.3.4 oneOf)"
        rows = [json.loads(line) for line in ndjson.rstrip("\n").split("\n") if line.strip()]
        assert len(rows) == resp["count"], "row count and `count` field must agree"
        for row in rows:
            for field in ("id", "content", "score", "timestamp", "status", "scope"):
                assert field in row, f"MXF row missing required field {field!r}: {row}"

    def test_export_ndjson_field_always_present_sync(self, client, agent_id):
        """Per spec section 3.3.4 oneOf, a synchronous response MUST have
        `ndjson` and MUST NOT have `event_id`. The empty-string case is
        legal when count=0."""
        if not _has_verb(client, "amp.export"):
            pytest.skip("amp.export not advertised")
        resp = client.call_tool("amp.export", {"agent_id": agent_id})
        assert "ndjson" in resp, "sync export MUST include ndjson per spec section 3.3.4"
        assert "event_id" not in resp, "sync and async are mutually exclusive per spec section 3.3.4"

    def test_export_respects_page_size(self, client):
        if not _has_verb(client, "amp.export"):
            pytest.skip("amp.export not advertised")
        scope = {"agent_id": f"export-page-{uuid.uuid4().hex[:8]}"}
        for i in range(5):
            client.call_tool("amp.encode", {
                "scope": scope, "content": f"paged memory xpage_{i}_{uuid.uuid4().hex[:6]}", "force": True,
            })
        resp = client.call_tool("amp.export", {"scope": scope, "page_size": 2})
        assert "error" not in resp
        assert resp.get("count", 0) <= 2, f"page_size cap violated: {resp}"
        assert resp.get("next_cursor"), "next_cursor MUST be present when more rows remain"

    def test_export_cursor_resumes(self, client):
        if not _has_verb(client, "amp.export"):
            pytest.skip("amp.export not advertised")
        scope = {"agent_id": f"export-cursor-{uuid.uuid4().hex[:8]}"}
        ids = []
        for i in range(5):
            enc = client.call_tool("amp.encode", {
                "scope": scope, "content": f"cursor memory xcur_{i}_{uuid.uuid4().hex[:6]}", "force": True,
            })
            ids.append(enc["id"])
        seen_ids = []
        cursor = None
        for _ in range(10):  # bounded loop
            args = {"scope": scope, "page_size": 2}
            if cursor:
                args["cursor"] = cursor
            resp = client.call_tool("amp.export", args)
            assert "error" not in resp
            page_rows = [json.loads(l) for l in resp.get("ndjson", "").rstrip("\n").split("\n") if l.strip()]
            seen_ids.extend(r["id"] for r in page_rows)
            cursor = resp.get("next_cursor")
            if not cursor:
                break
        # Every seeded id must appear exactly once across all pages.
        for memory_id in ids:
            assert seen_ids.count(memory_id) == 1, (
                f"cursor pagination must emit each row exactly once; "
                f"id={memory_id} count={seen_ids.count(memory_id)}"
            )

    def test_export_malformed_cursor_returns_invalid_request(self, client, agent_id):
        if not _has_verb(client, "amp.export"):
            pytest.skip("amp.export not advertised")
        resp = client.call_tool("amp.export", {"agent_id": agent_id, "cursor": "this-is-not-a-real-cursor"})
        assert "error" in resp, "tampered cursor MUST surface as an error"
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request", (
            f"malformed cursor MUST map to invalid_request per spec section 3.5; got {data!r}"
        )


class TestImportBasic:
    def test_import_skipped_if_not_advertised(self, client, agent_id):
        if not _has_verb(client, "amp.import"):
            pytest.skip("backend does not advertise amp.import (Core conformance OK)")

    def test_import_empty_ndjson_returns_zero_counts(self, client, agent_id):
        if not _has_verb(client, "amp.import"):
            pytest.skip("amp.import not advertised")
        resp = client.call_tool("amp.import", {"agent_id": agent_id, "ndjson": ""})
        assert "error" not in resp
        assert resp.get("imported") == 0
        assert resp.get("skipped") == 0
        assert resp.get("failed") == 0

    def test_import_roundtrip_with_export(self, client):
        """The headline use-case: export from scope A, import into scope B,
        confirm the rows land in B and are recallable."""
        if not (_has_verb(client, "amp.import") and _has_verb(client, "amp.export")):
            pytest.skip("amp.export and amp.import both required")
        scope_a = {"agent_id": f"rt-src-{uuid.uuid4().hex[:8]}"}
        scope_b = {"agent_id": f"rt-dst-{uuid.uuid4().hex[:8]}"}
        token = f"xrt_{uuid.uuid4().hex[:8]}"
        # Seed source.
        for i in range(3):
            client.call_tool("amp.encode", {
                "scope": scope_a, "content": f"roundtrip memory {token} #{i}", "force": True,
            })
        # Export from A.
        exp = client.call_tool("amp.export", {"scope": scope_a})
        assert "error" not in exp
        assert exp["count"] == 3
        # Re-route the rows: their scope says scope_a, we're importing into scope_b.
        # That's a conflict under strict mode; rewrite the scope to match the destination.
        rows = [json.loads(l) for l in exp["ndjson"].rstrip("\n").split("\n") if l.strip()]
        for row in rows:
            row["scope"] = scope_b
        rewritten_ndjson = "\n".join(json.dumps(r) for r in rows) + "\n"
        # Import into B.
        imp = client.call_tool("amp.import", {"scope": scope_b, "ndjson": rewritten_ndjson})
        assert "error" not in imp
        assert imp["imported"] == 3, f"all 3 rows must import cleanly: {imp}"
        assert imp["failed"] == 0
        # Recall from B: must surface the token.
        rec = client.call_tool("amp.recall", {"scope": scope_b, "query": token})
        assert "error" not in rec
        assert len(rec.get("results", [])) >= 3, (
            f"imported rows must be recallable from destination scope: {rec}"
        )

    def test_import_skip_is_idempotent(self, client):
        if not _has_verb(client, "amp.import"):
            pytest.skip("amp.import not advertised")
        scope = {"agent_id": f"imp-skip-{uuid.uuid4().hex[:8]}"}
        row = {
            "id": f"mxf-{uuid.uuid4().hex}",
            "content": f"idempotent row xidem_{uuid.uuid4().hex[:6]}",
            "score": 0.0,
            "timestamp": "2026-05-30T12:00:00",
            "status": "active",
            "scope": scope,
        }
        ndjson = json.dumps(row) + "\n"
        first = client.call_tool("amp.import", {"scope": scope, "ndjson": ndjson, "on_conflict": "skip"})
        assert first["imported"] == 1 and first["skipped"] == 0 and first["failed"] == 0
        second = client.call_tool("amp.import", {"scope": scope, "ndjson": ndjson, "on_conflict": "skip"})
        assert second["imported"] == 0, "skip MUST be idempotent on re-import"
        assert second["skipped"] == 1
        assert second["failed"] == 0

    def test_import_overwrite_replaces_existing(self, client):
        if not _has_verb(client, "amp.import"):
            pytest.skip("amp.import not advertised")
        scope = {"agent_id": f"imp-ow-{uuid.uuid4().hex[:8]}"}
        memory_id = f"mxf-{uuid.uuid4().hex}"
        v1 = {
            "id": memory_id, "content": "version one xov1", "score": 0.0,
            "timestamp": "2026-05-30T12:00:00", "status": "active", "scope": scope,
        }
        v2 = {
            "id": memory_id, "content": "version two xov2", "score": 0.0,
            "timestamp": "2026-05-30T12:00:00", "status": "active", "scope": scope,
        }
        client.call_tool("amp.import", {"scope": scope, "ndjson": json.dumps(v1) + "\n"})
        ow = client.call_tool("amp.import", {
            "scope": scope, "ndjson": json.dumps(v2) + "\n", "on_conflict": "overwrite"
        })
        assert "error" not in ow
        assert ow["imported"] == 1, "overwrite MUST replace, not skip"
        assert ow["skipped"] == 0
        # Recall should return the v2 content.
        rec = client.call_tool("amp.recall", {"scope": scope, "query": "xov2"})
        contents = [m["content"] for m in rec.get("results", [])]
        assert any("xov2" in c for c in contents), f"overwrite must produce v2 content; got {contents}"

    def test_import_cross_scope_row_is_failed(self, client):
        """A row whose scope conflicts with the request scope MUST be counted
        as failed (invalid_request) under both strict and inherit modes — spec
        section 3.3.5 strict-AND."""
        if not _has_verb(client, "amp.import"):
            pytest.skip("amp.import not advertised")
        scope_a = {"agent_id": f"imp-conf-a-{uuid.uuid4().hex[:8]}"}
        scope_b = {"agent_id": f"imp-conf-b-{uuid.uuid4().hex[:8]}"}
        bad_row = {
            "id": f"mxf-{uuid.uuid4().hex}",
            "content": "wrong scope xconflict",
            "score": 0.0,
            "timestamp": "2026-05-30T12:00:00",
            "status": "active",
            "scope": scope_a,  # row claims A
        }
        resp = client.call_tool("amp.import", {
            "scope": scope_b,                     # request says B
            "ndjson": json.dumps(bad_row) + "\n",
        })
        assert "error" not in resp
        assert resp["imported"] == 0
        assert resp["failed"] == 1, "cross-scope row MUST land in failed, not imported"
        assert resp["errors"][0]["amp_error_code"] == "invalid_request"

    def test_import_malformed_json_line_counted_as_failed(self, client):
        if not _has_verb(client, "amp.import"):
            pytest.skip("amp.import not advertised")
        scope = {"agent_id": f"imp-mal-{uuid.uuid4().hex[:8]}"}
        good_row = {
            "id": f"mxf-{uuid.uuid4().hex}", "content": "good row xgood", "score": 0.0,
            "timestamp": "2026-05-30T12:00:00", "status": "active", "scope": scope,
        }
        ndjson = json.dumps(good_row) + "\n" + "this is not json\n"
        resp = client.call_tool("amp.import", {"scope": scope, "ndjson": ndjson})
        assert "error" not in resp
        assert resp["imported"] == 1
        assert resp["failed"] == 1
        assert resp["errors"][0]["line"] == 2  # 1-indexed
        assert resp["errors"][0]["amp_error_code"] == "invalid_request"

    def test_import_scope_remap_strict_rejects_partial_row(self, client):
        if not _has_verb(client, "amp.import"):
            pytest.skip("amp.import not advertised")
        request_scope = {
            "agent_id": f"imp-rm-{uuid.uuid4().hex[:8]}",
            "user_id": "user-42",
        }
        # Row carries only agent_id; user_id is missing.
        partial_row = {
            "id": f"mxf-{uuid.uuid4().hex}", "content": "partial scope xpart",
            "score": 0.0, "timestamp": "2026-05-30T12:00:00", "status": "active",
            "scope": {"agent_id": request_scope["agent_id"]},
        }
        resp = client.call_tool("amp.import", {
            "scope": request_scope,
            "ndjson": json.dumps(partial_row) + "\n",
            "scope_remap": "strict",
        })
        assert "error" not in resp
        assert resp["imported"] == 0
        assert resp["failed"] == 1
        assert resp["errors"][0]["amp_error_code"] == "invalid_request"

    def test_import_scope_remap_inherit_accepts_partial_row(self, client):
        if not _has_verb(client, "amp.import"):
            pytest.skip("amp.import not advertised")
        request_scope = {
            "agent_id": f"imp-inh-{uuid.uuid4().hex[:8]}",
            "user_id": "user-43",
        }
        partial_row = {
            "id": f"mxf-{uuid.uuid4().hex}", "content": "inherit scope xinh",
            "score": 0.0, "timestamp": "2026-05-30T12:00:00", "status": "active",
            "scope": {"agent_id": request_scope["agent_id"]},
        }
        resp = client.call_tool("amp.import", {
            "scope": request_scope,
            "ndjson": json.dumps(partial_row) + "\n",
            "scope_remap": "inherit",
        })
        assert "error" not in resp, f"inherit mode MUST accept partial-scope rows: {resp}"
        assert resp["imported"] == 1
        assert resp["failed"] == 0

    def test_import_fail_atomic_returns_not_supported_on_nontransactional_backend(self, client, agent_id):
        """Spec section 3.3.5: backends without transactional rollback MUST
        return not_supported when fail_atomic is requested. The smriti-memcore
        reference impl is non-transactional and MUST take this path."""
        if not _has_verb(client, "amp.import"):
            pytest.skip("amp.import not advertised")
        resp = client.call_tool("amp.import", {
            "agent_id": agent_id,
            "ndjson": "",
            "on_conflict": "fail_atomic",
        })
        if "error" in resp:
            data = resp["error"].get("data") or {}
            # Either not_supported (non-transactional backend) or implementation
            # actually supports it (transactional backend) — both are conformant.
            assert data.get("amp_error_code") in ("not_supported",), (
                f"non-transactional backends MUST return not_supported per spec "
                f"section 3.3.5; got {data!r}"
            )
            assert resp["error"].get("code") == -32002, (
                f"not_supported MUST map to JSON-RPC -32002 per spec section 3.5; "
                f"got {resp['error'].get('code')}"
            )

    def test_import_invalid_on_conflict_is_invalid_request(self, client, agent_id):
        if not _has_verb(client, "amp.import"):
            pytest.skip("amp.import not advertised")
        resp = client.call_tool("amp.import", {
            "agent_id": agent_id, "ndjson": "", "on_conflict": "lolwhat",
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_import_fail_fast_stops_at_first_failure(self, client):
        if not _has_verb(client, "amp.import"):
            pytest.skip("amp.import not advertised")
        scope = {"agent_id": f"imp-ff-{uuid.uuid4().hex[:8]}"}
        row1 = {
            "id": f"mxf-{uuid.uuid4().hex}", "content": "first good xff1", "score": 0.0,
            "timestamp": "2026-05-30T12:00:00", "status": "active", "scope": scope,
        }
        row3 = {
            "id": f"mxf-{uuid.uuid4().hex}", "content": "third good xff3", "score": 0.0,
            "timestamp": "2026-05-30T12:00:00", "status": "active", "scope": scope,
        }
        ndjson = json.dumps(row1) + "\nnot json line 2\n" + json.dumps(row3) + "\n"
        resp = client.call_tool("amp.import", {
            "scope": scope, "ndjson": ndjson, "on_conflict": "fail_fast",
        })
        assert "error" not in resp
        # row1 imported, row2 fails, row3 NOT processed (fail_fast aborts).
        assert resp["imported"] == 1, f"fail_fast must commit row1 before stopping: {resp}"
        assert resp["failed"] == 1
        # The remaining row should NOT have been counted as imported/skipped.
        assert resp["imported"] + resp["skipped"] + resp["failed"] == 2


# -- amp.update (spec section 3.2.4, v1.2-draft) -----------------------------

class TestUpdateBasic:
    """v1.2-draft amp.update. Backends that don't implement it MUST respond
    not_supported; tests below auto-skip when the verb isn't advertised."""

    def test_update_skipped_if_not_advertised(self, client):
        if not _has_verb(client, "amp.update"):
            pytest.skip("backend does not advertise amp.update (v1.2-draft optional)")

    def test_update_content_only(self, client, agent_id):
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        enc = encode(client, agent_id, f"original content xup_{uuid.uuid4().hex[:6]}", force=True)
        assert enc["status"] == "stored"
        new_content = f"updated content xup2_{uuid.uuid4().hex[:6]}"
        resp = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": enc["id"], "content": new_content,
        })
        assert "error" not in resp
        assert resp.get("status") == "updated"
        assert resp.get("id") == enc["id"]
        # Recall should now find the new content.
        rec = client.call_tool("amp.recall", {
            "agent_id": agent_id, "query": new_content.split()[1],
        })
        matched = [m for m in rec.get("results", []) if m["id"] == enc["id"]]
        assert matched and matched[0]["content"] == new_content, (
            f"updated content must be reflected in recall: {matched}"
        )

    def test_update_metadata_merge_default(self, client, agent_id):
        """RFC 7396 merge: keys in patch overwrite, absent keys preserved."""
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        # Seed with two metadata keys.
        enc = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": f"merge test xmrg_{uuid.uuid4().hex[:6]}",
            "force": True,
            "metadata": {"amp.confidence": 0.5, "tag_a": "alpha"},
        })
        assert enc["status"] == "stored"
        # Patch only confidence; tag_a MUST survive.
        upd = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": enc["id"],
            "metadata": {"amp.confidence": 0.9},
        })
        assert upd.get("status") == "updated"
        # Recall and inspect the stored metadata.
        rec = client.call_tool("amp.recall", {
            "agent_id": agent_id, "query": "merge test xmrg",
        })
        matched = [m for m in rec.get("results", []) if m["id"] == enc["id"]]
        assert matched, "memory must still be retrievable after update"
        # The backend may surface metadata under "metadata" with backend
        # extensions mixed in; assert only that our two keys are present and
        # have the correct values.
        meta = matched[0].get("metadata") or {}
        assert meta.get("amp.confidence") == 0.9, (
            f"merge MUST overwrite confidence to 0.9; got {meta}"
        )
        assert meta.get("tag_a") == "alpha", (
            f"merge MUST preserve tag_a (absent from patch); got {meta}"
        )

    def test_update_metadata_null_removes_key(self, client, agent_id):
        """RFC 7396: explicit JSON null in the patch removes the key."""
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        enc = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": f"null delete xnul_{uuid.uuid4().hex[:6]}",
            "force": True,
            "metadata": {"stale_key": "to_be_removed", "keep_key": "stays"},
        })
        assert enc["status"] == "stored"
        upd = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": enc["id"],
            "metadata": {"stale_key": None},
        })
        assert upd.get("status") == "updated"
        rec = client.call_tool("amp.recall", {
            "agent_id": agent_id, "query": "null delete xnul",
        })
        matched = [m for m in rec.get("results", []) if m["id"] == enc["id"]]
        assert matched
        meta = matched[0].get("metadata") or {}
        assert "stale_key" not in meta, (
            f"null in merge patch MUST remove the key; got {meta}"
        )
        assert meta.get("keep_key") == "stays"

    def test_update_metadata_replace_mode(self, client, agent_id):
        """metadata_mode=replace MUST discard keys absent from the patch."""
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        enc = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": f"replace test xrep_{uuid.uuid4().hex[:6]}",
            "force": True,
            "metadata": {"keep_under_merge": "would-survive", "amp.confidence": 0.4},
        })
        assert enc["status"] == "stored"
        upd = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": enc["id"],
            "metadata": {"only_key": "present"},
            "metadata_mode": "replace",
        })
        assert upd.get("status") == "updated"
        rec = client.call_tool("amp.recall", {
            "agent_id": agent_id, "query": "replace test xrep",
        })
        matched = [m for m in rec.get("results", []) if m["id"] == enc["id"]]
        assert matched
        meta = matched[0].get("metadata") or {}
        assert meta.get("only_key") == "present"
        assert "keep_under_merge" not in meta, (
            f"replace mode MUST discard absent keys; got {meta}"
        )

    def test_update_not_found_for_unknown_id(self, client, agent_id):
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        resp = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": "does-not-exist", "content": "anything",
        })
        assert "error" not in resp
        assert resp.get("status") == "not_found"

    def test_update_cross_scope_returns_not_found(self, client):
        """Cross-scope updates MUST return not_found (NOT invalid_request)
        per spec section 3.2.4: existence info must not leak across scopes."""
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        scope_a = {"agent_id": f"upd-iso-a-{uuid.uuid4().hex[:8]}"}
        scope_b = {"agent_id": f"upd-iso-b-{uuid.uuid4().hex[:8]}"}
        enc = client.call_tool("amp.encode", {
            "scope": scope_a, "content": "scope A row xupi", "force": True,
        })
        resp = client.call_tool("amp.update", {
            "scope": scope_b, "id": enc["id"], "content": "trespass",
        })
        assert "error" not in resp
        assert resp.get("status") == "not_found", (
            f"cross-scope update MUST be not_found, not invalid_request; got {resp}"
        )

    def test_update_no_change_when_patch_is_noop(self, client, agent_id):
        """Well-formed request that produces no observable change SHOULD return
        no_change (backends MAY return updated instead -- both conformant per
        spec section 3.2.4)."""
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        enc = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": f"noop test xnoo_{uuid.uuid4().hex[:6]}",
            "force": True,
            "metadata": {"k": "v"},
        })
        # Patch with the same content + same metadata key -> no-op.
        resp = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": enc["id"],
            "content": enc.get("content") or None,  # may be absent in response
            "metadata": {"k": "v"},
        })
        assert "error" not in resp
        assert resp.get("status") in ("no_change", "updated"), (
            f"no-op update MUST return no_change or updated; got {resp}"
        )

    def test_update_empty_content_rejected(self, client, agent_id):
        """Empty string content MUST be invalid_request -- use amp.forget to delete."""
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        enc = encode(client, agent_id, f"empty content reject xemp_{uuid.uuid4().hex[:6]}", force=True)
        resp = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": enc["id"], "content": "",
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_update_invalid_metadata_mode(self, client, agent_id):
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        enc = encode(client, agent_id, f"bad mode xmod_{uuid.uuid4().hex[:6]}", force=True)
        resp = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": enc["id"],
            "metadata": {"k": "v"}, "metadata_mode": "lolwhat",
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_update_is_idempotent(self, client, agent_id):
        """Applying the same patch twice MUST produce the same end state.
        First call: updated. Second call: no_change (or updated if the backend
        doesn't optimise no-ops)."""
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        enc = encode(client, agent_id, f"idempotent xidu_{uuid.uuid4().hex[:6]}", force=True)
        new_content = f"new content xidu2_{uuid.uuid4().hex[:6]}"
        first = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": enc["id"], "content": new_content,
        })
        assert first.get("status") == "updated"
        second = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": enc["id"], "content": new_content,
        })
        assert second.get("status") in ("no_change", "updated")
        # Either way, the recall must still show new_content.
        rec = client.call_tool("amp.recall", {
            "agent_id": agent_id, "query": new_content.split()[2],
        })
        matched = [m for m in rec.get("results", []) if m["id"] == enc["id"]]
        assert matched and matched[0]["content"] == new_content


class TestUpdateAnnotations:
    """When advertised, amp.update MUST publish the §3.4 annotation values."""

    def test_update_annotations_match_spec(self, client):
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        resp = client._send("tools/list", {})
        tools = {t["name"]: t for t in resp.get("result", {}).get("tools", [])}
        ann = tools["amp.update"].get("annotations") or {}
        assert ann.get("readOnlyHint") is False
        assert ann.get("destructiveHint") is False
        assert ann.get("idempotentHint") is True
        assert ann.get("openWorldHint") is False
