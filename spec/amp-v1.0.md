# Agent Memory Protocol (AMP) — Specification v1.0

> **Status:** Draft v1.0 — 2026-05-10
> **Authors:** \<TBA\>
> **Target:** arXiv + public GitHub spec repo

---

## Abstract

Agent Memory Protocol (AMP) is an open specification that defines a standard interface for persistent memory in AI agent systems. Built as a set of tool definitions on top of the Model Context Protocol (MCP), AMP enables any memory backend to serve any MCP-compatible agent through a common contract. An agent developer targeting AMP can switch memory backends — from a simple key-value store to a graph-based episodic memory system — without changing application code. A memory backend implementing AMP can be consumed by any AMP-compatible agent framework without custom integration work.

**Claim:** A six-verb interface — `amp.encode`, `amp.recall`, `amp.forget`, `amp.consolidate`, `amp.pin`, `amp.stats` — is necessary and sufficient to represent the complete memory lifecycle across backends of varying complexity. We validate this claim against four production memory systems and demonstrate it through a full reference implementation.

---

## 1. Motivation

### 1.1 The memory fragmentation problem

Persistent memory is a prerequisite for agents that operate across sessions, collaborate with other agents, or accumulate domain knowledge over time. The ecosystem currently contains several memory systems — each with real engineering depth — but none interoperable. Four concrete examples illustrate the gap:

**Supermemory** exposes an MCP server with three tools: `memory`, `recall`, and `context`. The MCP wrapper is MIT-licensed; the storage and retrieval engine is a proprietary hosted service. Memories live exclusively in Supermemory's infrastructure with no export format, no standard `MemoryResult` schema, and no path to substituting a different backend. Switching away requires full re-ingestion.

**Obsidian Vault** has no official MCP implementation. The community has produced 66+ MCP servers, of which eight have meaningful traction — each exposing different tool names, schemas, and capabilities, ranging from raw file read/write to BM25 keyword search to graph traversal. Several servers bundle their own local embedding models and provide semantic retrieval without requiring an additional plugin; however, there is no consolidation, no memory lifecycle, and nothing is interchangeable between implementations.

**Claude Dream Mode** (Anthropic Managed Agents API, research preview as of 2026) introduces a `POST /v1/dreams` endpoint and a `memory_store` resource that agents can attach to sessions. Dreams run asynchronous consolidation over session transcripts and require two opt-in beta headers. The memory store format is undisclosed, there is no export API, and the capability is exclusively available to Claude models. The consolidation lifecycle Dream implements — merging duplicates, resolving contradictions, producing higher-order knowledge — is exactly the kind of feature AMP's `amp.consolidate` verb should invoke on any conformant backend.

**smriti-memcore** (MIT, PyPI, open source) is a locally-run memory system exposing 12 MCP tools. It implements a complete memory lifecycle: salience-gated ingestion, hybrid FTS5+vector retrieval with RRF fusion, multi-hop Semantic Palace graph traversal, spaced-repetition decay, and background consolidation. Despite being the most fully-featured open implementation, its tool names and schemas are not interchangeable with Supermemory's `recall`, Obsidian's `search_notes`, or Claude's `memory_store`.

The pattern across all four: each system re-invents the same core verbs (store, retrieve, forget, organise) with incompatible schemas and no portability. An agent written for one backend cannot switch to another without code changes.

### 1.2 The explicit gap in MCP

MCP itself acknowledges this boundary. The specification states:

> *"MCP focuses solely on the protocol for context exchange — it does not dictate how AI applications use LLMs or manage the provided context."*

MCP defines three server-side primitives: **Tools** (executable functions), **Resources** (read-only data with URIs), and **Prompts** (parameterised instruction templates). None of these primitives carry memory semantics. There is no standard schema for a memory record, no lifecycle callbacks (consolidate, decay, pin), no agent identity namespacing, and no conformance level distinguishing a simple key-value store from a graph-based episodic memory system.

MCP provides the right transport layer. AMP defines the missing semantic contract on top of it.

### 1.3 What AMP does not define

AMP defines the **interface contract**, not the implementation. It does not specify:

- How memories are stored (vector DB, graph, SQL, in-memory)
- How retrieval ranking works (cosine similarity, BM25, RRF, or any hybrid)
- How consolidation is implemented internally
- Authentication and authorisation (delegated to the MCP transport layer)

---

## 2. Related Work

### 2.1 Agent memory research

**Generative Agents (Park et al., 2023)** introduced explicit memory streams for LLM-based agents with retrieval scored on recency, importance, and relevance. This work established that agents require structured memory management beyond fixed context windows, but its memory access model is tightly coupled to the simulation architecture and not designed for interoperability.

**MemGPT / Letta (Packer et al., 2023)** treats memory as paged context: a fixed "main context" window backed by archival and recall buffers. Memory operations are self-directed — the LLM itself calls internal functions to page in or out content. AMP differs fundamentally: recall is initiated by the calling agent or orchestration framework, making AMP an external protocol layer rather than an internal agent architecture decision.

**Cognitive architectures** (ACT-R, SOAR) distinguish episodic, semantic, and procedural memory with formal retrieval mechanisms, decay functions, and activation spreading. AMP borrows the encode / recall / consolidate lifecycle pattern but deliberately does not mandate which memory types a backend must implement or how decay is computed.

### 2.2 Production memory systems

**Mem0** implements entity-based memory extraction: structured facts are extracted from conversation turns and stored in a hybrid vector + knowledge graph store. Mem0 exposes a Python SDK and REST API designed for direct client consumption. No other backend can implement the same interface; the API is not designed for substitution.

**Zep** focuses on temporal knowledge graphs built from conversation history with entity and edge extraction via LLMs. Like Mem0, it defines a client-specific API and provides no mechanism for swapping the backend.

**Supermemory** and **smriti-memcore** are described in detail in Section 1.1 as primary motivating examples of the fragmentation problem.

### 2.3 Positioning

AMP is the first specification to define memory access as a protocol layer over an existing transport (MCP), separating the interface contract from any particular storage, retrieval, or consolidation implementation. Prior systems define **APIs** — contracts between one client and one backend. AMP defines a **protocol** — a contract any conformant client can use with any conformant backend.

---

## 3. Design Goals

| Goal | Description |
|------|-------------|
| **Minimal surface** | The smallest set of verbs that covers the full memory lifecycle |
| **Transport-agnostic** | Expressed as MCP tool definitions; works over stdio or HTTP/SSE |
| **Extensible without breakage** | Open `metadata` bags on requests and responses allow backends to expose richer features |
| **Tiered conformance** | Simple backends can implement Core only; full-featured backends implement the complete spec |
| **Agent identity** | Multi-agent support is first-class; memories are namespaced by agent identity |
| **No opinion on internals** | Backends may use any storage or retrieval strategy |

---

## 4. Concepts

### 4.1 Memory

A memory is a unit of persistent information stored on behalf of an agent. Every memory has:

- A globally unique `id` (string, assigned by the backend)
- `content` — the text of the memory
- A `status` — one of `active`, `pinned`, or `archived`
- A `source` — where the memory originated (free string; e.g. `user_stated`, `inferred`, `external`)
- A `timestamp` — ISO 8601 creation time
- A `score` — backend-assigned relevance score for the current query (present only in recall responses)
- A `metadata` bag — open JSON object for backend-specific fields

**ID uniqueness and namespace access:** Memory IDs are globally unique — no two memories across any namespace will share the same ID. However, access is enforced by namespace: if `agent-B` calls `amp.forget` or `amp.pin` with an ID that was created under `agent-A`'s namespace, the backend MUST return `not_found` (not an auth error). The ID is globally unique for backend deduplication purposes; the namespace determines what a given caller may see and modify.

### 4.2 Memory lifecycle

```
encode ──→ (below_threshold)    ← rejected; no memory created; no id returned
       └──→ [active] ──────────────────────────────→ pin ──→ [pinned]
                    │                                              │
                    ├──→ consolidate ──→ [archived]               │
                    │    (if superseded)                          │
                    └──→ forget ──────────────────────────────────┘
                                            (removed from backend)
```

`(below_threshold)` and `(removed)` are outcomes of operations, not observable memory statuses. `below_threshold` appears in the `amp.encode` response when the backend rejects a memory due to low salience. `removed` means the memory no longer exists in the backend. Pinned memories can be explicitly deleted via `amp.forget` but are never archived by consolidation.

| Status | Description |
|--------|-------------|
| `active` | Default state after encoding. Eligible for recall and consolidation. |
| `pinned` | Marked as permanent. Not archived by consolidation. Always returned in recall. |
| `archived` | Superseded or cold. Still stored but not returned in default recall results. |

### 4.3 Agent identity

Every AMP request carries an `agent_id` field. This allows a single backend to serve multiple agents with isolated memory namespaces. Backends MUST enforce namespace isolation: an `amp.recall` for `agent_id: "agent-A"` MUST NOT return memories encoded by `agent_id: "agent-B"` unless the backend explicitly implements a shared memory feature and the caller has opted in. Backends SHOULD create a new namespace implicitly on first `amp.encode` for an unknown `agent_id`; they MUST NOT return an error solely because the namespace is new.

**What is `agent_id`?** AMP intentionally leaves this to the caller. `agent_id` is an opaque, caller-defined string used as a partitioning key — it is not derived from the underlying model, model version, or agent harness. Callers may set it to an application identifier (`"my-app"`), a user session (`"user-42-session"`), a logical agent role (`"research-assistant"`), or anything else meaningful to their system. Upgrading a model or switching harnesses does not automatically change `agent_id` — that is the caller's decision. This deliberate openness means AMP does not define identity semantics; it only enforces that whatever string the caller provides is treated as a consistent namespace. See Open Question #10 for further discussion.

### 4.4 Consolidation

Consolidation is an optional background process by which the backend reorganises, deduplicates, or distils raw memories into higher-order knowledge. AMP exposes `amp.consolidate` as a trigger, but the backend decides what consolidation means internally.

**Full conformance and no-op implementations:** Full conformance requires `amp.consolidate` to be a callable, responsive endpoint — the tool must exist and return a valid AMP response. A Full-conformant backend MAY implement consolidation as a no-op (performing no internal reorganisation), in which case it MUST return `status: "ok"` with `memories_processed: 0`. We recommend that Full backends perform meaningful consolidation (deduplication, archival of superseded memories, or summarisation), but the spec does not mandate what that consolidation does internally. Core-conformant backends that have not implemented the endpoint at all MUST return `amp_error_code: not_supported` (see §5.8).

---

## 5. Protocol Specification

All AMP tools follow MCP tool call conventions. Input is a JSON object. Output is a JSON object. Errors follow the MCP JSON-RPC error format with an additional `amp_error_code` field (see Section 5.8).

**Tool naming convention:** AMP tool names use dot notation (`amp.encode`, `amp.recall`, etc.). MCP's tool naming specification (SEP-986) restricts valid characters to `A-Z, a-z, 0-9, underscore, dash, and dot` — forward slashes are not permitted. Dot notation provides the same namespace clarity (`amp.` prefix) while remaining fully compliant with the MCP standard.

### 5.1 MemoryResult — canonical response object

All recall responses return an array of `MemoryResult` objects.

```json
{
  "id": "string",
  "content": "string",
  "score": 0.87,
  "source": "user_stated",
  "timestamp": "2026-05-10T08:00:00Z",
  "status": "active",
  "metadata": {}
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | ✓ | Backend-assigned unique identifier |
| `content` | string | ✓ | Text content of the memory |
| `score` | float | ✓ | Relevance score for the query (0.0–1.0 recommended; backend may use own scale) |
| `source` | string | ✗ | Provenance label |
| `timestamp` | string | ✓ | ISO 8601 creation time |
| `status` | string | ✓ | `active` \| `pinned` \| `archived` |
| `metadata` | object | ✗ | Backend-specific fields (e.g. salience scores, hop distance, room ID) |

### 5.2 amp.encode

Store a new memory for an agent.

**Input**
```json
{
  "agent_id": "string",
  "content": "string",
  "source": "string",
  "force": false,
  "metadata": {}
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `agent_id` | ✓ | Namespace for this memory |
| `content` | ✓ | Text to store |
| `source` | ✗ | Provenance label |
| `force` | ✗ | Boolean. If `true`, bypass salience threshold and store unconditionally. Defaults to `false`. |
| `metadata` | ✗ | Hints to the backend (e.g. salience, modality). |

**Output**
```json
{
  "id": "string",
  "status": "stored"
}
```

`status` values:
- `stored` — accepted and stored; `id` is the new memory identifier
- `duplicate` — content matches an existing memory; `id` refers to the existing one
- `below_threshold` — backend rejected due to low salience; no `id` returned

Agents MUST check the `status` field. A `below_threshold` response is not an error — the backend received the request and made a deliberate decision. To override the threshold, set `force: true`; backends that support forced encoding MUST honour it, returning `stored`.

### 5.3 amp.recall

Retrieve memories relevant to a query.

**Input**
```json
{
  "agent_id": "string",
  "query": "string",
  "top_k": 10,
  "filters": {}
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `agent_id` | ✓ | Namespace to search |
| `query` | ✓ | Natural language or keyword query |
| `top_k` | ✗ | Max results to return (default: backend-defined, recommended default 10) |
| `filters` | ✗ | Filter hints (see schema below) |

**Filters schema** (all fields optional):
```json
{
  "status": "active | pinned | archived",
  "source": "string",
  "timestamp_after": "ISO 8601 datetime",
  "timestamp_before": "ISO 8601 datetime"
}
```

If `filters.status` is absent, backends SHOULD return both `active` and `pinned` memories. `archived` memories MUST NOT appear unless `filters.status` is explicitly set to `"archived"`.

**Output**
```json
{
  "results": [MemoryResult]
}
```

Results MUST be ordered by descending relevance score.

### 5.4 amp.forget

Permanently delete a memory.

**Input**
```json
{
  "agent_id": "string",
  "id": "string"
}
```

**Output**
```json
{
  "status": "forgotten"
}
```

`status` values: `forgotten` (deleted), `not_found` (no memory with that ID in this agent namespace).

### 5.5 amp.consolidate

Trigger backend consolidation. Backends MAY process consolidation asynchronously or synchronously.

**Input**
```json
{
  "agent_id": "string",
  "depth": "full"
}
```

`depth` values: `full` (complete consolidation pass), `light` (minimal/incremental pass). Backends that do not implement consolidation MUST return `status: "not_supported"` rather than an error.

**Output**
```json
{
  "status": "queued | ok",
  "memories_processed": 42
}
```

`status` values:
- `queued` — backend is processing asynchronously; `memories_processed` MUST be omitted
- `ok` — backend has processed synchronously; `memories_processed` SHOULD be included
- `not_supported` — backend does not implement consolidation (valid for Core backends)

### 5.6 amp.pin

Mark a memory as permanent. Pinned memories are never archived by consolidation.

**Input**
```json
{
  "agent_id": "string",
  "id": "string"
}
```

**Output**
```json
{
  "status": "pinned"
}
```

`status` values: `pinned` (success), `not_found`.

### 5.7 amp.stats

Return backend statistics for an agent namespace.

**Input**
```json
{
  "agent_id": "string"
}
```

**Output**
```json
{
  "memory_count": 81,
  "unconsolidated_count": 12,
  "metadata": {}
}
```

`unconsolidated_count` is optional for Core-conformant backends that do not queue memories before consolidation; such backends SHOULD omit the field or return `0`. `metadata` allows backends to surface richer stats (e.g. room count, vector store size, last consolidation time) without breaking the base contract.

### 5.8 Error codes

AMP errors are returned as MCP tool call errors following the JSON-RPC 2.0 error format. Backends MUST use MCP error code `-32000` (server error) for all AMP errors and MUST place `amp_error_code` and `message` in the `data` field:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32000,
    "message": "AMP error: not_found",
    "data": {
      "amp_error_code": "not_found",
      "message": "Memory abc123 not found for agent agent-A"
    }
  }
}
```

| `amp_error_code` | Meaning |
|------------------|---------|
| `invalid_request` | Malformed input — missing required field or wrong type |
| `not_found` | Referenced memory ID does not exist in this agent namespace |
| `not_supported` | Tool not implemented by this backend (valid for Full-only tools on Core backends) |
| `backend_error` | Internal backend failure |

---

## 6. Conformance Levels

Two conformance levels allow backends of different complexity to adopt AMP without implementing features they don't need.

| Tool | Core | Full |
|------|------|------|
| `amp.encode` | ✓ Required | ✓ Required |
| `amp.recall` | ✓ Required | ✓ Required |
| `amp.forget` | ✓ Required | ✓ Required |
| `amp.stats` | ✓ Required | ✓ Required |
| `amp.pin` | ✗ Optional | ✓ Required |
| `amp.consolidate` | ✗ Optional | ✓ Required |

**Core** conformance is sufficient for backends that store and retrieve memories without lifecycle management (e.g. a Redis-backed store, a simple vector DB wrapper).

**Full** conformance is required for backends that support memory lifecycle management, background consolidation, and permanent pinning.

A backend advertising Core conformance MUST return `amp_error_code: not_supported` via the standard MCP error envelope (see §5.8) when `amp.pin` or `amp.consolidate` are called.

Conformance level is declared in the MCP server manifest:

```json
{
  "name": "my-memory-backend",
  "amp_conformance": "full",
  "amp_version": "0.1"
}
```

---

## 7. Reference Implementation

**smriti-memcore** ([PyPI](https://pypi.org/project/smriti-memcore/)) is the reference Full-conformance AMP implementation. It exposes AMP tools over MCP stdio transport and demonstrates how a backend can surface richer features via the `metadata` fields without violating the base contract.

Note: smriti-memcore's current MCP tool names (e.g. `smriti_encode`, `smriti_recall`) differ from the AMP verb names (`amp.encode`, `amp.recall`). A future release will expose AMP verb names as aliases. The mapping below shows the correspondence.

| AMP Tool | smriti-memcore implementation |
|----------|-------------------------------|
| `amp.encode` | `smriti_encode` — salience-gated episodic + semantic encoding with vector embeddings |
| `amp.recall` | `smriti_recall` — hybrid FTS5+RRF retrieval with multi-hop Semantic Palace graph traversal |
| `amp.forget` | `smriti_forget` — removes from vector store, FTS index, and Semantic Palace |
| `amp.consolidate` | `smriti_consolidate` — episodic buffer → Semantic Palace graph consolidation with 8 background processes |
| `amp.pin` | `smriti_pin` — sets `MemoryStatus.PINNED`; excluded from consolidation archival |
| `amp.stats` | `smriti_stats` — returns palace, episode buffer, vector store, and retrieval metrics |

smriti-memcore `metadata` in `MemoryResult` includes: `salience` (5-dimensional score), `room_id` (Semantic Palace cluster), `hops` (graph traversal distance), `reflection_level` (abstraction depth 0–3), `strength` (spaced-repetition weight). These fields enrich AMP without requiring any other conformant backend to implement them.

*Disclosure: Shivam Tyagi is both the primary author of this specification and the author of smriti-memcore. This specification was designed to generalise smriti-memcore's tool set — all design decisions were evaluated against the requirements of other memory backends, with the goal of producing a protocol any backend could implement without smriti-specific assumptions.*

---

## 8. Comparison with Existing Systems

| System | Protocol | MCP support | Open source | Retrieval model | Lifecycle management | Portable |
|--------|----------|-------------|-------------|-----------------|----------------------|----------|
| **AMP** (this spec) | ✓ Open | ✓ Native | ✓ | Backend-defined | ✓ encode/recall/forget/consolidate/pin | ✓ |
| **smriti-memcore** | AMP reference impl | ✓ 12 tools (stdio) | ✓ MIT | Multi-factor + FTS5+RRF hybrid | ✓ Full: decay, spaced-rep, consolidation, pinning | ✓ Local files |
| **Supermemory** | ✗ Proprietary | ✓ 3 tools | Wrapper only (MIT); backend closed | Semantic (undisclosed) | Partial: expiry, contradiction resolution | ✗ Vendor lock-in |
| **Obsidian Vault** | ✗ None | Community only (66+ servers; incompatible) | Vault open; app proprietary | BM25 / string match | ✗ None | Partial (Markdown files only) |
| **Claude Dream Mode** | ✗ Proprietary | ✗ REST only | ✗ Closed | LLM-based (undisclosed) | Partial: Dream consolidation (gated beta) | ✗ Anthropic-only |
| **Mem0** | ✗ Proprietary | ✗ | Partial | Semantic + graph | Partial | ✗ |
| **MemGPT / Letta** | ✗ Proprietary | ✗ | ✓ MIT | Paged context | ✓ | Partial |
| **OpenAI memory** | ✗ Closed | ✗ | ✗ | Undisclosed | ✗ | ✗ |

### Key observations

**No existing system defines a protocol.** Supermemory, Claude Dream, and Mem0 each expose APIs, but an API is not a protocol — there is no shared contract allowing a different backend to be substituted without changing agent code.

**MCP adoption is not interoperability.** Supermemory and smriti-memcore both expose valid MCP servers. An agent cannot call one in place of the other.

**Obsidian illustrates the fragmentation cost.** Eight community MCP servers exist for Obsidian, each with different tool names and capabilities. This duplication of effort is the direct consequence of having no standard interface.

**Claude Dream is the most sophisticated lifecycle system** but is gated behind research-preview access, exclusive to Claude models, and ships with no open schema or export path.

**smriti-memcore is the natural reference implementation.** It is the only system that is simultaneously fully open-source, MCP-native, locally portable, and implements a complete memory lifecycle.

---

## 9. Open Questions

The following design questions are explicitly deferred from v1.0 and will be resolved based on community feedback:

1. **Multi-agent shared memory** — Should AMP define a mechanism for two agents to share a memory namespace, or is that out of scope for the base protocol?
2. **Authentication** — Should AMP define a standard agent authentication header, or fully delegate to the MCP transport layer?
3. **Streaming recall** — Should `amp.recall` support streaming results (useful for large `top_k`) via MCP's streaming tool response?
4. **Memory versioning** — Should AMP define an `amp/update` verb, or is the encode-then-forget pattern sufficient?
5. **Schema versioning** — How should `amp_version` in the server manifest interact with backwards compatibility as the spec evolves?
6. **Cross-backend memory portability** — Should AMP define a memory export/import format so memories can be migrated between backends?
7. **Pagination** — Should `amp.recall` support cursor-based pagination for backends with large memory stores, or is `top_k` sufficient?
8. **Score semantics for exact-match backends** — What should a Core-conformant backend that uses exact-match retrieval return for the `score` field?
9. **Update semantics** — Is the encode-then-forget pattern sufficient for updating a memory, or should AMP define an `amp/update` verb?
10. **Agent identity semantics** — What should determine an `agent_id`? Should AMP provide conventions (e.g. recommend an application-scoped ID over a model-scoped ID) to avoid interoperability issues when the same logical agent is served by different models or harnesses over time? Alternatively, is the namespace partition itself necessary, or would a single global namespace with per-memory ownership metadata be a simpler design?

---

## 10. Next Steps

- [ ] Community review of this draft
- [ ] Resolve open questions in Section 9
- [ ] Publish spec to public GitHub repo
- [ ] Submit to arXiv (cs.AI)
- [x] Reference implementation: `amp-server` (Full-conformant, FastMCP + smriti-memcore, `pip install amp-server`) published to PyPI
- [ ] Reference implementation: update smriti-memcore to expose AMP tool names (`amp.encode`, `amp.recall`, etc.) as aliases alongside existing tool names
- [ ] Reach out to LangChain, LlamaIndex, and Anthropic SDK teams for feedback

---

## Sources

- [MCP Architecture Overview](https://modelcontextprotocol.io/docs/learn/architecture)
- [MCP Server Concepts](https://modelcontextprotocol.io/docs/learn/server-concepts)
- [Supermemory MCP Introduction](https://supermemory.ai/docs/supermemory-mcp/introduction)
- [Supermemory GitHub](https://github.com/supermemoryai/supermemory)
- [smriti-memcore on PyPI](https://pypi.org/project/smriti-memcore/)
- [Park et al., 2023 — Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442)
- [Packer et al., 2023 — MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560)

---

*AMP is an independent open specification. It is not affiliated with Anthropic or the MCP project.*
