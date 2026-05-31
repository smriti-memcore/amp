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


# -- amp.batch_encode (spec section 3.2.5, v1.2-draft) -----------------------

class TestBatchEncodeBasic:
    """v1.2-draft amp.batch_encode. Auto-skips on backends that don't advertise it."""

    def test_batch_encode_skipped_if_not_advertised(self, client):
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("backend does not advertise amp.batch_encode (v1.2-draft optional)")

    def test_batch_encode_all_succeed(self, client, agent_id):
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        entries = [
            {"content": f"batch row 1 xbat_{uuid.uuid4().hex[:6]}", "force": True},
            {"content": f"batch row 2 xbat_{uuid.uuid4().hex[:6]}", "force": True},
            {"content": f"batch row 3 xbat_{uuid.uuid4().hex[:6]}", "force": True},
        ]
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id, "entries": entries,
        })
        assert "error" not in resp, f"all-succeed batch must not surface a top-level error: {resp}"
        assert len(resp["results"]) == len(entries), (
            f"results MUST have length {len(entries)}, got {len(resp['results'])}"
        )
        for r in resp["results"]:
            assert r["status"] == "stored"
            assert "id" in r
        summary = resp.get("summary") or {}
        assert summary.get("stored") == 3
        assert summary.get("failed") == 0

    def test_batch_encode_empty_array_returns_empty_results(self, client, agent_id):
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id, "entries": [],
        })
        assert "error" not in resp
        assert resp["results"] == []

    def test_batch_encode_mixed_success_and_failure(self, client, agent_id):
        """Empty content rows MUST land with status=invalid_request in their
        index slot; the surrounding rows MUST still be stored. Tests the
        partial-failure contract from spec section 3.2.5."""
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        token = f"xmix_{uuid.uuid4().hex[:6]}"
        entries = [
            {"content": f"good row 0 {token}", "force": True},
            {"content": ""},  # MUST land in invalid_request
            {"content": f"good row 2 {token}", "force": True},
            {"content": None},  # also invalid
            {"content": f"good row 4 {token}", "force": True},
        ]
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id, "entries": entries,
        })
        assert "error" not in resp
        assert len(resp["results"]) == len(entries)
        assert resp["results"][0]["status"] == "stored"
        assert resp["results"][1]["status"] == "invalid_request"
        assert "message" in resp["results"][1]
        assert resp["results"][2]["status"] == "stored"
        assert resp["results"][3]["status"] == "invalid_request"
        assert resp["results"][4]["status"] == "stored"
        summary = resp["summary"]
        assert summary["stored"] == 3
        assert summary["failed"] == 2

    def test_batch_encode_preserves_order(self, client, agent_id):
        """results[i] MUST correspond to entries[i] for all i. Verified by
        encoding rows with sentinel content tokens and checking the ids map
        back to the same indices."""
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        token = uuid.uuid4().hex[:6]
        entries = [
            {"content": f"position {i} sentinel xord_{token}_{i}", "force": True}
            for i in range(5)
        ]
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id, "entries": entries,
        })
        assert "error" not in resp
        # Recall each sentinel and confirm the id from results[i] matches.
        for i in range(5):
            rec = client.call_tool("amp.recall", {
                "agent_id": agent_id, "query": f"xord_{token}_{i}",
            })
            results = rec.get("results", [])
            matched = [m for m in results if f"xord_{token}_{i}" in m.get("content", "")]
            assert matched, f"position-{i} memory must be recallable; got {results}"
            assert matched[0]["id"] == resp["results"][i]["id"], (
                f"order violation at index {i}: batch returned id "
                f"{resp['results'][i]['id']!r} but recall found {matched[0]['id']!r}"
            )

    def test_batch_encode_summary_counts_sum_to_length(self, client, agent_id):
        """Per spec section 3.2.5, the sum of the four summary fields equals
        results.length."""
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        entries = [
            {"content": f"sum check xsum_{uuid.uuid4().hex[:6]}", "force": True}
            for _ in range(3)
        ] + [
            {"content": ""},  # failed
            {"content": ""},  # failed
        ]
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id, "entries": entries,
        })
        s = resp["summary"]
        assert s["stored"] + s["below_threshold"] + s["duplicate"] + s["failed"] == len(resp["results"]), (
            f"summary counts MUST sum to results.length per spec section 3.2.5: {s} vs {len(resp['results'])}"
        )

    def test_batch_encode_no_scope_returns_invalid_request(self, client):
        """Request-level error: missing scope (and missing agent_id) MUST
        produce an AmpError frame, not a results[] response."""
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client.call_tool("amp.batch_encode", {
            "entries": [{"content": "scopeless"}],
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_batch_encode_non_array_entries_returns_invalid_request(self, client, agent_id):
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id, "entries": "not an array",
        })
        assert "error" in resp

    def test_batch_encode_oversize_returns_invalid_request(self, client, agent_id):
        """Backends MUST reject batches over their maxItems cap with
        invalid_request. The reference impl caps at 1000."""
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        # 1001 entries exceeds the schema's maxItems=1000.
        entries = [{"content": f"oversize row {i}"} for i in range(1001)]
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id, "entries": entries,
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_batch_encode_rows_share_scope(self, client):
        """All entries in one batch land under the top-level request scope.
        Recall under the same scope MUST find all stored rows; recall under
        a different scope MUST NOT."""
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        scope_a = {"agent_id": f"bat-scope-a-{uuid.uuid4().hex[:8]}"}
        scope_b = {"agent_id": f"bat-scope-b-{uuid.uuid4().hex[:8]}"}
        token = f"xshr_{uuid.uuid4().hex[:6]}"
        resp = client.call_tool("amp.batch_encode", {
            "scope": scope_a,
            "entries": [
                {"content": f"scope share row 1 {token}", "force": True},
                {"content": f"scope share row 2 {token}", "force": True},
            ],
        })
        assert "error" not in resp
        # Recall under scope A: rows must be present.
        rec_a = client.call_tool("amp.recall", {"scope": scope_a, "query": token})
        found_a = [m for m in rec_a.get("results", []) if token in m.get("content", "")]
        assert len(found_a) == 2, f"both rows must be recallable under scope A: {found_a}"
        # Recall under scope B: rows MUST NOT be present.
        rec_b = client.call_tool("amp.recall", {"scope": scope_b, "query": token})
        found_b = [m for m in rec_b.get("results", []) if token in m.get("content", "")]
        assert len(found_b) == 0, f"rows MUST NOT leak across scopes: {found_b}"

    def test_batch_encode_per_row_metadata_persists(self, client, agent_id):
        """A row's metadata patch MUST land on the stored memory and survive
        a recall round-trip (verifies the same metadata-persistence path
        amp.encode uses)."""
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        token = f"xbme_{uuid.uuid4().hex[:6]}"
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id,
            "entries": [{
                "content": f"row with metadata {token}",
                "force": True,
                "metadata": {"amp.confidence": 0.77, "tag_b": "beta"},
            }],
        })
        assert resp["results"][0]["status"] == "stored"
        mem_id = resp["results"][0]["id"]
        rec = client.call_tool("amp.recall", {"agent_id": agent_id, "query": token})
        matched = [m for m in rec.get("results", []) if m["id"] == mem_id]
        assert matched, "row must be recallable"
        meta = matched[0].get("metadata") or {}
        assert meta.get("amp.confidence") == 0.77
        assert meta.get("tag_b") == "beta"

    def test_batch_encode_force_false_can_below_threshold(self, client, agent_id):
        """A row with force=false (default) and a low-salience content can
        legitimately come back as below_threshold. Test ensures the per-row
        status is correctly surfaced rather than being conflated with
        invalid_request."""
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        # We can't reliably force below_threshold across backends, so we
        # accept either stored or below_threshold for force=false rows -- the
        # important assertion is that BOTH are recognised as valid (not
        # invalid_request).
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id,
            "entries": [{"content": "the"}],  # very short, low salience
        })
        assert "error" not in resp
        status = resp["results"][0]["status"]
        assert status in ("stored", "below_threshold"), (
            f"force=false low-salience row MUST be stored or below_threshold, "
            f"not invalid_request; got {status}"
        )


class TestBatchEncodeAnnotations:
    """When advertised, amp.batch_encode MUST publish the section 3.4 annotation values."""

    def test_batch_encode_annotations_match_spec(self, client):
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client._send("tools/list", {})
        tools = {t["name"]: t for t in resp.get("result", {}).get("tools", [])}
        ann = tools["amp.batch_encode"].get("annotations") or {}
        assert ann.get("readOnlyHint") is False
        assert ann.get("destructiveHint") is False
        assert ann.get("idempotentHint") is False
        assert ann.get("openWorldHint") is False


class TestMetadataFiltersBasic:
    """v1.2-draft amp.recall filters.metadata_filters[] per spec §3.2.2.1."""

    def _seed(self, client, agent_id, rows):
        """Encode each (tag, metadata) row; return list of memory ids."""
        ids = []
        for tag, md in rows:
            enc = client.call_tool("amp.encode", {
                "agent_id": agent_id,
                "content": f"metadata filter test {tag}",
                "force": True,
                "metadata": md,
            })
            assert enc.get("status") == "stored", f"seed failed: {enc}"
            ids.append(enc["id"])
        return ids

    def _recall_tags(self, client, agent_id, metadata_filters):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "metadata filter test",
            "top_k": 50,
            "filters": {"metadata_filters": metadata_filters},
        })
        assert "error" not in resp, f"recall error: {resp}"
        # Extract the per-row tag we encoded so tests can assert membership.
        out = []
        for r in resp["results"]:
            content = r.get("content") or ""
            if content.startswith("metadata filter test "):
                out.append(content[len("metadata filter test "):])
        return out

    def test_eq_filter_matches_only_equal_rows(self, client, agent_id):
        if not _has_verb(client, "amp.recall"):
            pytest.skip("amp.recall not advertised")
        self._seed(client, agent_id, [
            ("eq_a", {"tag": "alpha"}),
            ("eq_b", {"tag": "beta"}),
        ])
        tags = self._recall_tags(client, agent_id, [
            {"key": "tag", "operator": "eq", "value": "alpha"},
        ])
        assert "eq_a" in tags
        assert "eq_b" not in tags

    def test_ne_filter_excludes_equal_rows(self, client, agent_id):
        self._seed(client, agent_id, [
            ("ne_a", {"tag": "alpha"}),
            ("ne_b", {"tag": "beta"}),
        ])
        tags = self._recall_tags(client, agent_id, [
            {"key": "tag", "operator": "ne", "value": "alpha"},
        ])
        assert "ne_a" not in tags
        assert "ne_b" in tags

    def test_numeric_order_filters(self, client, agent_id):
        self._seed(client, agent_id, [
            ("num_lo", {"amp.confidence": 0.3}),
            ("num_mid", {"amp.confidence": 0.6}),
            ("num_hi", {"amp.confidence": 0.9}),
        ])
        gt = self._recall_tags(client, agent_id, [
            {"key": "amp.confidence", "operator": "gt", "value": 0.5},
        ])
        assert "num_lo" not in gt
        assert "num_mid" in gt and "num_hi" in gt

        gte = self._recall_tags(client, agent_id, [
            {"key": "amp.confidence", "operator": "gte", "value": 0.6},
        ])
        assert "num_lo" not in gte
        assert "num_mid" in gte and "num_hi" in gte

        lt = self._recall_tags(client, agent_id, [
            {"key": "amp.confidence", "operator": "lt", "value": 0.6},
        ])
        assert "num_lo" in lt
        assert "num_mid" not in lt and "num_hi" not in lt

        lte = self._recall_tags(client, agent_id, [
            {"key": "amp.confidence", "operator": "lte", "value": 0.6},
        ])
        assert "num_lo" in lte and "num_mid" in lte
        assert "num_hi" not in lte

    def test_in_filter_against_array_value(self, client, agent_id):
        self._seed(client, agent_id, [
            ("in_a", {"priority": "low"}),
            ("in_b", {"priority": "mid"}),
            ("in_c", {"priority": "high"}),
        ])
        tags = self._recall_tags(client, agent_id, [
            {"key": "priority", "operator": "in", "value": ["low", "high"]},
        ])
        assert "in_a" in tags and "in_c" in tags
        assert "in_b" not in tags

    def test_contains_substring_on_string_value(self, client, agent_id):
        self._seed(client, agent_id, [
            ("cs_a", {"label": "alpha-beta-gamma"}),
            ("cs_b", {"label": "delta-epsilon"}),
        ])
        tags = self._recall_tags(client, agent_id, [
            {"key": "label", "operator": "contains", "value": "beta"},
        ])
        assert "cs_a" in tags
        assert "cs_b" not in tags

    def test_contains_element_on_array_value(self, client, agent_id):
        self._seed(client, agent_id, [
            ("ca_a", {"amp.categories": ["pref", "ui"]}),
            ("ca_b", {"amp.categories": ["fact", "history"]}),
        ])
        tags = self._recall_tags(client, agent_id, [
            {"key": "amp.categories", "operator": "contains", "value": "pref"},
        ])
        assert "ca_a" in tags
        assert "ca_b" not in tags

    def test_and_composition_strict(self, client, agent_id):
        self._seed(client, agent_id, [
            ("and_a", {"tag": "alpha", "amp.confidence": 0.8}),
            ("and_b", {"tag": "alpha", "amp.confidence": 0.2}),
            ("and_c", {"tag": "beta", "amp.confidence": 0.9}),
        ])
        tags = self._recall_tags(client, agent_id, [
            {"key": "tag", "operator": "eq", "value": "alpha"},
            {"key": "amp.confidence", "operator": "gte", "value": 0.5},
        ])
        assert "and_a" in tags
        assert "and_b" not in tags  # low confidence
        assert "and_c" not in tags  # wrong tag

    def test_missing_key_is_a_miss_for_every_operator(self, client, agent_id):
        # Even ne — a missing key must NOT match "ne anything" per §3.2.2.1.
        self._seed(client, agent_id, [
            ("mk_a", {"tag": "alpha"}),
            ("mk_b", {}),  # no tag key at all
        ])
        ne = self._recall_tags(client, agent_id, [
            {"key": "tag", "operator": "ne", "value": "alpha"},
        ])
        assert "mk_a" not in ne
        assert "mk_b" not in ne  # missing key => filter miss, even for ne

        eq = self._recall_tags(client, agent_id, [
            {"key": "tag", "operator": "eq", "value": "alpha"},
        ])
        assert "mk_a" in eq
        assert "mk_b" not in eq

    def test_type_mismatch_is_silent_miss(self, client, agent_id):
        self._seed(client, agent_id, [
            ("tm_str", {"tag": "not-a-number"}),
            ("tm_num", {"tag": 42}),
        ])
        # gt with numeric value: string-typed rows must drop out silently.
        tags = self._recall_tags(client, agent_id, [
            {"key": "tag", "operator": "gt", "value": 10},
        ])
        assert "tm_str" not in tags
        assert "tm_num" in tags

    def test_composes_with_status_filter(self, client, agent_id):
        # v1.1 filters (status here) and v1.2 metadata_filters AND together.
        self._seed(client, agent_id, [
            ("comp_a", {"tag": "alpha"}),
        ])
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "metadata filter test",
            "top_k": 50,
            "filters": {
                "status": "active",
                "metadata_filters": [
                    {"key": "tag", "operator": "eq", "value": "alpha"},
                ],
            },
        })
        assert "error" not in resp, resp
        tags = [
            (r.get("content") or "")[len("metadata filter test "):]
            for r in resp["results"]
            if (r.get("content") or "").startswith("metadata filter test ")
        ]
        assert "comp_a" in tags

    def test_empty_metadata_filters_array_is_noop(self, client, agent_id):
        self._seed(client, agent_id, [
            ("empty_a", {"tag": "alpha"}),
        ])
        tags = self._recall_tags(client, agent_id, [])
        assert "empty_a" in tags


class TestMetadataFiltersErrors:
    """Request-level validation surface — these raise AmpError, not silent miss."""

    def test_unknown_operator_is_invalid_request(self, client, agent_id):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"metadata_filters": [
                {"key": "tag", "operator": "regex", "value": "^a"},
            ]},
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_in_with_scalar_value_is_invalid_request(self, client, agent_id):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"metadata_filters": [
                {"key": "tag", "operator": "in", "value": "alpha"},
            ]},
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_eq_with_array_value_is_invalid_request(self, client, agent_id):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"metadata_filters": [
                {"key": "tag", "operator": "eq", "value": ["alpha", "beta"]},
            ]},
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_missing_required_field_is_invalid_request(self, client, agent_id):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"metadata_filters": [
                {"key": "tag", "operator": "eq"},  # no value
            ]},
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_metadata_filters_not_an_array_is_invalid_request(self, client, agent_id):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"metadata_filters": {"key": "tag", "operator": "eq", "value": "a"}},
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"


class TestRecallTopKOversampling:
    """Regression for the v1.2 oversampling rule (spec §3.2.2.1 'top_k semantics
    under post-retrieval filtering'): a selective filter MUST NOT eat into the
    caller's top_k budget. PR F fix.
    """

    def test_top_k_one_with_metadata_filter_returns_matching_lower_rank(self, client, agent_id):
        if not _has_verb(client, "amp.recall"):
            pytest.skip("amp.recall not advertised")
        # Seed two memories. Both contain the same lexical token so they both
        # show up in the recall candidate set; only one passes the filter.
        # Backends that slice top_k=1 BEFORE filtering will return zero rows;
        # conformant backends oversample and return the matching one.
        client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": "oversample regression token xoversample first row",
            "force": True,
            "metadata": {"oversample_tag": "no"},
        })
        client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": "oversample regression token xoversample second row",
            "force": True,
            "metadata": {"oversample_tag": "yes"},
        })
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "xoversample",
            "top_k": 1,
            "filters": {"metadata_filters": [
                {"key": "oversample_tag", "operator": "eq", "value": "yes"},
            ]},
        })
        assert "error" not in resp, resp
        results = resp["results"]
        assert len(results) == 1, (
            f"top_k=1 must return the rank-2 match when rank-1 fails the filter; "
            f"got {len(results)} results -- backend is slicing before filtering"
        )
        meta = results[0].get("metadata") or {}
        assert meta.get("oversample_tag") == "yes"


class TestMetadataFiltersNeTypeMismatch:
    """Regression: `ne` MUST return False on type mismatch (silent miss),
    matching the documented behaviour of every other operator in §3.2.2.1.
    PR F fix.
    """

    def _seed(self, client, agent_id, rows):
        for tag, md in rows:
            enc = client.call_tool("amp.encode", {
                "agent_id": agent_id,
                "content": f"ne mismatch test {tag}",
                "force": True,
                "metadata": md,
            })
            assert enc.get("status") == "stored", enc

    def _recall_tags(self, client, agent_id, filters):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "ne mismatch test",
            "top_k": 50,
            "filters": {"metadata_filters": filters},
        })
        assert "error" not in resp, resp
        return [
            (r.get("content") or "")[len("ne mismatch test "):]
            for r in resp["results"]
            if (r.get("content") or "").startswith("ne mismatch test ")
        ]

    def test_ne_string_vs_number_is_miss(self, client, agent_id):
        if not _has_verb(client, "amp.recall"):
            pytest.skip("amp.recall not advertised")
        self._seed(client, agent_id, [
            ("ne_str", {"thing": "42"}),
            ("ne_num", {"thing": 42}),
        ])
        # ne against a number must miss the string row (type mismatch -> miss),
        # not include it. The numeric row also misses because 42 ne 42 is false.
        tags = self._recall_tags(client, agent_id, [
            {"key": "thing", "operator": "ne", "value": 42},
        ])
        assert "ne_str" not in tags, (
            "type-mismatched row leaked through `ne` filter -- silent-miss rule violated"
        )
        assert "ne_num" not in tags  # 42 ne 42 -> false

    def test_ne_bool_vs_number_is_miss(self, client, agent_id):
        self._seed(client, agent_id, [
            ("ne_bool", {"thing": True}),
            ("ne_one", {"thing": 1}),
        ])
        # ne against True must NOT match the integer 1 (bool/int do not cross-compare).
        tags = self._recall_tags(client, agent_id, [
            {"key": "thing", "operator": "ne", "value": True},
        ])
        # Both miss: bool row because True ne True is false; int row because of type-family mismatch.
        assert "ne_bool" not in tags
        assert "ne_one" not in tags


class TestUpdateMergePatchNestedNullDelete:
    """RFC 7396 §2: when the patch contains a nested object and the target's
    key is absent (or non-object), the algorithm recurses with `{}` as the
    sub-target. Nested null-deletes against absent keys MUST be dropped, NOT
    materialised as `null`. PR F fix.
    """

    def test_nested_null_against_absent_key_drops_null(self, client, agent_id):
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        enc = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": f"merge nested null xmnn_{uuid.uuid4().hex[:6]}",
            "force": True,
            "metadata": {"kept": "value"},
        })
        assert enc["status"] == "stored", enc
        upd = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": enc["id"],
            # `parent` is not in the stored bag. RFC 7396 says: recurse with {}
            # as the sub-target, then null-delete drops to no-op. Final stored
            # value should be `{"parent": {}}` (the now-empty sub-object that
            # the spec lets backends keep), with NO `null` leaking through.
            "metadata": {"parent": {"absent_child": None}},
        })
        assert upd["status"] in ("updated", "no_change"), upd

        rec = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "xmnn",
            "top_k": 10,
        })
        matched = [r for r in rec["results"] if r["id"] == enc["id"]]
        assert matched, "memory not found after update"
        meta = matched[0].get("metadata") or {}
        # `kept` must still be present.
        assert meta.get("kept") == "value", f"sibling key lost; metadata={meta}"
        # `parent` must NOT contain `absent_child: null`. Either `parent` is
        # absent entirely (also conformant: nothing was added) or is `{}`.
        parent = meta.get("parent")
        if parent is not None:
            assert isinstance(parent, dict), f"parent must be an object; got {parent!r}"
            assert "absent_child" not in parent, (
                f"nested null-delete leaked through as literal null; parent={parent!r}"
            )

    def test_nested_null_against_non_object_target_recurses_with_empty(self, client, agent_id):
        # If the existing value at `key` is a non-dict (e.g. a string), RFC 7396
        # still says: target = {}, then merge. Any null in the nested patch is
        # a no-op against {}.
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        enc = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": f"merge nested non-object xmno_{uuid.uuid4().hex[:6]}",
            "force": True,
            "metadata": {"slot": "was-a-string"},
        })
        assert enc["status"] == "stored", enc
        upd = client.call_tool("amp.update", {
            "agent_id": agent_id, "id": enc["id"],
            "metadata": {"slot": {"new_child": "added", "ghost": None}},
        })
        assert upd["status"] in ("updated", "no_change"), upd

        rec = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "xmno",
            "top_k": 10,
        })
        matched = [r for r in rec["results"] if r["id"] == enc["id"]]
        assert matched
        meta = matched[0].get("metadata") or {}
        slot = meta.get("slot")
        assert isinstance(slot, dict), f"slot must be an object after merge; got {slot!r}"
        assert slot.get("new_child") == "added"
        assert "ghost" not in slot, (
            f"nested null-delete leaked through; slot={slot!r}"
        )


class TestRecallTimestampFilters:
    """v1.1 filters.timestamp_after / timestamp_before MUST actually filter
    rows. Previously a silent gap (validated by the spec but ignored by the
    reference server). PR F fix.
    """

    def test_timestamp_after_in_future_returns_nothing(self, client, agent_id):
        if not _has_verb(client, "amp.recall"):
            pytest.skip("amp.recall not advertised")
        client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": "timestamp filter test xtsf future",
            "force": True,
        })
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "xtsf",
            "top_k": 50,
            "filters": {"timestamp_after": "2099-01-01T00:00:00Z"},
        })
        assert "error" not in resp, resp
        matching = [
            r for r in resp["results"]
            if "xtsf" in (r.get("content") or "")
        ]
        assert matching == [], (
            f"timestamp_after in the future MUST exclude all rows; got {matching}"
        )

    def test_timestamp_before_in_past_returns_nothing(self, client, agent_id):
        client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": "timestamp filter test xtsfb past",
            "force": True,
        })
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "xtsfb",
            "top_k": 50,
            "filters": {"timestamp_before": "2000-01-01T00:00:00Z"},
        })
        assert "error" not in resp, resp
        matching = [
            r for r in resp["results"]
            if "xtsfb" in (r.get("content") or "")
        ]
        assert matching == [], (
            f"timestamp_before in the past MUST exclude all rows; got {matching}"
        )

    def test_timestamp_after_in_past_returns_match(self, client, agent_id):
        # Sanity: a permissive after-bound MUST let the row through.
        client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": "timestamp filter test xtsfp permissive",
            "force": True,
        })
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "xtsfp",
            "top_k": 50,
            "filters": {"timestamp_after": "2000-01-01T00:00:00Z"},
        })
        assert "error" not in resp, resp
        matching = [
            r for r in resp["results"]
            if "xtsfp" in (r.get("content") or "")
        ]
        assert matching, "permissive timestamp_after should not exclude rows"

    def test_invalid_timestamp_is_invalid_request(self, client, agent_id):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"timestamp_after": "not-a-timestamp"},
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"


# ── PR G hardening tests ─────────────────────────────────────────────────────


class TestBatchEncodeRowStrictness:
    """PR G: per-row strictness in amp.batch_encode (spec §3.2.5
    'Per-row strictness'). Bad rows surface as results[i].status=invalid_request
    while the request itself still succeeds.
    """

    def _row_status(self, resp, idx=0):
        assert "error" not in resp, resp
        return resp["results"][idx].get("status")

    def test_force_must_be_strict_bool_string_rejected(self, client, agent_id):
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id,
            "entries": [
                {"content": "stricter force xsf string", "force": "true"},
            ],
        })
        assert self._row_status(resp) == "invalid_request", (
            f"force='true' must NOT be coerced; got {resp['results'][0]}"
        )

    def test_force_must_be_strict_bool_int_rejected(self, client, agent_id):
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id,
            "entries": [
                {"content": "stricter force xsfi int", "force": 1},
            ],
        })
        assert self._row_status(resp) == "invalid_request", (
            f"force=1 (int) must NOT be coerced to True; got {resp['results'][0]}"
        )

    def test_forbidden_per_row_scope_key_rejected(self, client, agent_id):
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id,
            "entries": [
                {
                    "content": "forbidden scope key xfsk",
                    "force": True,
                    "scope": {"agent_id": "other"},
                },
            ],
        })
        assert self._row_status(resp) == "invalid_request", (
            "per-row `scope` MUST be rejected, not silently dropped"
        )

    def test_forbidden_per_row_private_key_rejected(self, client, agent_id):
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id,
            "entries": [
                {
                    "content": "forbidden private key xfpk",
                    "force": True,
                    "private": True,
                },
            ],
        })
        assert self._row_status(resp) == "invalid_request"

    def test_unknown_row_key_rejected(self, client, agent_id):
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id,
            "entries": [
                {
                    "content": "unknown row key xurk",
                    "force": True,
                    "nonsense_field": 42,
                },
            ],
        })
        assert self._row_status(resp) == "invalid_request"

    def test_invalid_source_enum_is_row_invalid_request(self, client, agent_id):
        # Used to silently fall back to MemorySource.DIRECT.
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id,
            "entries": [
                {
                    "content": "bad source enum xbse",
                    "source": "definitely-not-a-valid-source",
                    "force": True,
                },
            ],
        })
        assert self._row_status(resp) == "invalid_request"

    def test_mixed_strictness_other_rows_succeed(self, client, agent_id):
        # Row 0 is bad (string force), row 1 is fine. Both must be reported in order.
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id,
            "entries": [
                {"content": "mixed row a xmra", "force": "yes"},
                {"content": "mixed row b xmrb", "force": True},
            ],
        })
        assert "error" not in resp, resp
        assert len(resp["results"]) == 2
        assert resp["results"][0]["status"] == "invalid_request"
        assert resp["results"][1]["status"] == "stored"
        # Summary must reflect both buckets.
        summary = resp.get("summary") or {}
        assert summary.get("failed", 0) >= 1
        assert summary.get("stored", 0) >= 1


class TestMetadataFiltersValueShape:
    """PR G: strict MetadataFilter.value typing -- null and non-scalar
    elements rejected as request-level invalid_request.
    """

    def _assert_invalid_request(self, resp):
        assert "error" in resp, f"expected error, got {resp}"
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request", (
            f"expected invalid_request, got {data}"
        )

    def test_null_value_rejected_for_eq(self, client, agent_id):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"metadata_filters": [
                {"key": "tag", "operator": "eq", "value": None},
            ]},
        })
        self._assert_invalid_request(resp)

    def test_null_value_rejected_for_in(self, client, agent_id):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"metadata_filters": [
                {"key": "tag", "operator": "in", "value": None},
            ]},
        })
        self._assert_invalid_request(resp)

    def test_in_with_null_element_rejected(self, client, agent_id):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"metadata_filters": [
                {"key": "tag", "operator": "in", "value": ["alpha", None]},
            ]},
        })
        self._assert_invalid_request(resp)

    def test_in_with_nested_array_element_rejected(self, client, agent_id):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"metadata_filters": [
                {"key": "tag", "operator": "in", "value": ["alpha", ["beta"]]},
            ]},
        })
        self._assert_invalid_request(resp)

    def test_in_with_object_element_rejected(self, client, agent_id):
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"metadata_filters": [
                {"key": "tag", "operator": "in", "value": [{"k": "v"}]},
            ]},
        })
        self._assert_invalid_request(resp)


class TestMetadataFiltersMaxItems:
    """PR G: filters.metadata_filters length cap (schema maxItems=32)."""

    def test_more_than_max_is_invalid_request(self, client, agent_id):
        too_many = [
            {"key": f"k{i}", "operator": "eq", "value": f"v{i}"}
            for i in range(33)  # 33 > schema cap of 32
        ]
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything",
            "filters": {"metadata_filters": too_many},
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"
        msg = (resp["error"].get("message") or "") + " " + (data.get("message") or "")
        assert "maxItems" in msg or "32" in msg, (
            f"error message should mention the cap; got {msg}"
        )

    def test_exactly_max_is_accepted(self, client, agent_id):
        # 32 predicates against a non-matching key. The recall MUST succeed
        # (returns whatever rows the backend has, filtered to empty in
        # practice). The check is that the request itself isn't rejected.
        many = [
            {"key": f"never_matches_{i}", "operator": "eq", "value": "x"}
            for i in range(32)
        ]
        resp = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "anything xmaxitems",
            "filters": {"metadata_filters": many},
        })
        assert "error" not in resp, resp


class TestMetadataBagSizeCap:
    """PR G: 64 KiB metadata-bag cap on amp.encode / amp.update / amp.batch_encode."""

    def _huge_metadata(self):
        # ~80 KiB of string content -- comfortably over the 64 KiB cap.
        big_string = "x" * (80 * 1024)
        return {"big": big_string}

    def test_encode_rejects_oversized_metadata(self, client, agent_id):
        resp = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": "oversized metadata encode xome",
            "force": True,
            "metadata": self._huge_metadata(),
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"

    def test_update_rejects_oversized_metadata_pre_save(self, client, agent_id):
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        enc = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": "oversized metadata update target xomu",
            "force": True,
            "metadata": {"keep": "kept"},
        })
        assert enc["status"] == "stored"
        resp = client.call_tool("amp.update", {
            "agent_id": agent_id,
            "id": enc["id"],
            "metadata": self._huge_metadata(),
        })
        assert "error" in resp
        data = resp["error"].get("data") or {}
        assert data.get("amp_error_code") == "invalid_request"
        # And the original bag must be untouched.
        rec = client.call_tool("amp.recall", {
            "agent_id": agent_id,
            "query": "xomu",
            "top_k": 10,
        })
        matched = [r for r in rec["results"] if r["id"] == enc["id"]]
        assert matched
        meta = matched[0].get("metadata") or {}
        assert meta.get("keep") == "kept", "pre-save rejection must leave original bag intact"
        assert "big" not in meta

    def test_batch_encode_rejects_oversized_row_metadata(self, client, agent_id):
        if not _has_verb(client, "amp.batch_encode"):
            pytest.skip("amp.batch_encode not advertised")
        resp = client.call_tool("amp.batch_encode", {
            "agent_id": agent_id,
            "entries": [
                {
                    "content": "oversized batch row xobr",
                    "force": True,
                    "metadata": self._huge_metadata(),
                },
            ],
        })
        assert "error" not in resp, resp
        assert resp["results"][0]["status"] == "invalid_request"


class TestUpdateMetadataBagAcceptedUnderCap:
    """PR G sanity: bags under the cap still encode + update normally."""

    def test_update_just_under_cap_accepted(self, client, agent_id):
        if not _has_verb(client, "amp.update"):
            pytest.skip("amp.update not advertised")
        enc = client.call_tool("amp.encode", {
            "agent_id": agent_id,
            "content": "under cap update xucu",
            "force": True,
        })
        assert enc["status"] == "stored"
        # ~32 KiB string -- under the 64 KiB cap.
        ok_bag = {"detail": "y" * (32 * 1024)}
        upd = client.call_tool("amp.update", {
            "agent_id": agent_id,
            "id": enc["id"],
            "metadata": ok_bag,
        })
        assert upd["status"] in ("updated", "no_change"), upd
