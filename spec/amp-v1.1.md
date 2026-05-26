# Agent Memory Protocol (AMP) — Specification v1.1
*By Community, Of Community, For Community*

> **Status:** v1.1 Draft — 2026-05-22
> **Authors:** Shivam Tyagi, Brad Jones, and the Open-Source Community

---

## Abstract

Agent Memory Protocol (AMP) is an open specification that defines a standard interface for persistent memory in AI agent systems. 

**AMP v1.1 evolves the protocol from a pure Model Context Protocol (MCP) tool set into a standalone, backend-agnostic service protocol (HTTP/gRPC first) with an optional MCP tool adapter wrapper.** This architectural separation addresses the core limitations of v1.0 by dividing concerns between:
1. **Agent-Facing Tools:** Standardized cognitive utilities (`encode`, `recall`, `forget`) that the LLM agent discovers and invokes at runtime.
2. **Harness-Facing APIs:** System-level lifecycle hooks (`consolidate`, `stats`, `pin`) executed programmatically by the application orchestration framework (e.g., LangChain, LlamaIndex, or Letta).

Furthermore, v1.1 introduces **multi-dimensional scoping** (`scope` objects containing `org_id`, `app_id`, `user_id`, `session_id`, and `agent_id`), a **Reserved Metadata Vocabulary Registry** to eliminate vendor-specific fragmentation, and a canonical **Memory Exchange Format (MXF)** for frictionless data migration.

---

## 1. Motivation & Architecture

### 1.1 The Integration Boundary Problem
In AMP v1.0, memory was represented solely as a set of MCP tools. While this simplified runtime tool discovery for LLMs, it created severe bottlenecks for the host application:
* **The Compaction Hook Paradox:** Low-level database operations like memory consolidation (`amp.consolidate`) and stats checking (`amp.stats`) were exposed directly to the LLM agent. Triggering these is a background system task, not a conscious cognitive decision.
* **Pre-Prompt Context Injection:** Harnesses often need to inject memories into the system prompt *before* the agent starts executing. Doing this via MCP stdio tool calls requires awkward loopback processes.
* **Metadata Fragmentation:** Because advanced memory backends (graphs, episodic decay) had to squeeze their primitives into a flat text-plus-metadata schema, important semantics became vendor-specific dialects in the `metadata` block, defeating the purpose of interoperability.

### 1.2 The Standalone API Design
To solve this, AMP v1.1 is designed as a **Service API first**. Backends expose a standard HTTP REST or gRPC interface.
The host application (harness) manages multi-tenant sessions, schedules background consolidation, and performs pre-prompt context injection by talking directly to this API. 
The harness then projects an optional **MCP Adapter** containing only the agent-facing tools (`amp.encode`, `amp.recall`, `amp.forget`) to the LLM.

### 1.3 The Dual-Delivery Channel Paradigm
To ensure AMP fits both lightweight local prototypes and high-throughput enterprise deployments, v1.1 establishes a **Dual-Delivery Channel** paradigm. The same semantic memory contract is served via two distinct formats:
1. **The MCP Adapter Channel (STDIO/SSE):** Exposes memory verbs as standard MCP tools (`amp.encode`, `amp.recall`, etc.). This is optimal for single-user environments, quick CLI hacking, or local desktop clients (like Claude Desktop) where the model is directly responsible for memory management.
2. **The Standalone REST/gRPC API Channel:** Exposes memory verbs as network-accessible service endpoints (e.g. `POST /v1/memories`). This is optimal for multi-user, production-grade applications where the host harness is responsible for context injection and autonomic system operations out-of-band.

These channels share the same schemas, ensuring that codebases can start in Local MCP mode and upgrade to Standalone Service mode with zero semantic data translation.


```
┌─────────────────────────────────────────────────────────┐
│                    Application Harness                  │
│  (Manages sessions, runs hooks, injects prompts)       │
└───────────┬─────────────────────────────────┬───────────┘
            │ (Fast gRPC/REST)                │ (MCP stdio/SSE)
            ▼                                 ▼
┌───────────────────────┐          ┌───────────────────────┐
│   Standalone AMP API  │          │   AMP MCP Adapter     │
│   (Service Boundary)  │◄─────────┤     (LLM Tools)       │
└───────────┬───────────┘          └───────────────────────┘
            ▼
┌─────────────────────────────────────────────────────────┐
│                 Conformant Memory Backend               │
│         (smriti-memcore, Zep, Mem0, Supermemory)        │
└─────────────────────────────────────────────────────────┘
```

---

## 2. Core Concepts

### 2.1 Memory
A memory is a unit of persistent information stored on behalf of an agent and user. Every memory has:
* A globally unique `id` (string, assigned by the backend).
* `content` — the text payload of the memory.
* A `status` — one of `active`, `pinned`, or `archived`.
* A `scope` object — defining the partition keys (replacing the flat v1.0 `agent_id`).
* A `source` — where the memory originated (e.g., `user_stated`, `inferred`, `external`).
* A `timestamp` — ISO 8601 creation time.
* A `score` — relevance score (present only in recall responses).
* A `metadata` bag — JSON object conforming to the **Reserved Metadata Vocabulary** where applicable.

### 2.2 Memory Lifecycle
```
encode ──→ (below_threshold)    ← rejected; no memory created
       └──→ [active] ──────────────────────────────→ pin ──→ [pinned]
                    │                                              │
                    ├──→ consolidate ──→ [archived]               │
                    │    (if superseded)                          │
                    └──→ forget ──────────────────────────────────┘
                                            (removed from backend)
```

* `active`: Eligible for recall and consolidation.
* `pinned`: Marked as permanent. Never archived by consolidation.
* `archived`: Superseded or cold knowledge. Kept in store but excluded from default recall.

### 2.3 Multi-Dimensional Scoping & Collaborative Workspaces
Real-world enterprise applications do not isolate memory purely by a single `agent_id`. In collaborative workflows, multiple agents might share a partition, or memory may be scoped directly to a shared user workspace, team, or group.

AMP v1.1 standardizes the `scope` block, mapping cleanly to leading production engines (like Mem0 and Zep). To support collaborative and shared-workspace architectures, `agent_id` is relaxed to be optional, provided that at least one isolating identity key (e.g., `agent_id`, `group_id`, `workspace_id`, or `user_id`) is present.

```json
{
  "org_id": "company-123",
  "app_id": "ios-retail-app",
  "user_id": "user-456",
  "session_id": "session-789",
  "agent_id": "research-assistant",
  "group_id": "support-tier-2",
  "workspace_id": "ws-corporate-finance"
}
```

* `org_id` (string, optional): Tenant partition.
* `app_id` (string, optional): Specific product surface, application surface, or platform (e.g. `slack_bot` vs `mobile_app`).
* `user_id` (string, optional): The end-user interacting with the agent or group.
* `session_id` (string, optional): The specific conversation turn, flow, or run (often mapped to `run_id` in other systems).
* `agent_id` (string, optional): The logical identity of the agent.
* `group_id` (string, optional): Shared team, department, or group of agents.
* `workspace_id` (string, optional): The collaborative shared workspace partition.

Backends MUST isolate queries based on the provided scope. If a query provides `user_id` and `agent_id`, the backend must only return memories matching both parameters.

---

## 3. Protocol Specification

### 3.1 MemoryResult — The Canonical Payload
```json
{
  "id": "mem_abc123",
  "content": "Alice prefers dark mode and drinks green tea.",
  "score": 0.92,
  "source": "user_stated",
  "timestamp": "2026-05-22T08:00:00Z",
  "status": "active",
  "scope": {
    "agent_id": "assistant-v1",
    "user_id": "user_42"
  },
  "visibility": "shared", // [DEPRECATED in v1.1]
  "metadata": {
    "amp.confidence": 0.95,
    "amp.entities": ["Alice", "dark mode", "green tea"],
    "amp.categories": ["preference", "lifestyle"]
  }
}
```

---

### 3.2 Agent-Facing Tools (LLM-Accessible)

#### 3.2.1 amp.encode
Store a new memory.

* **Input:**
  ```json
  {
    "scope": {
      "agent_id": "string",
      "group_id": "string",
      "workspace_id": "string",
      "user_id": "string",
      "session_id": "string",
      "app_id": "string",
      "org_id": "string"
    },
    "content": "string",
    "source": "string",
    "force": false,
    "private": false, // [DEPRECATED in v1.1]
    "metadata": {}
  }
  ```
  *(Note: For backward compatibility, backends MUST accept a flat `"agent_id"` parameter at the root if `"scope"` is absent, mapping it internally to `"scope": {"agent_id": agent_id}`.)*

* **Output:**
  ```json
  {
    "id": "string",
    "status": "stored | duplicate | below_threshold | queued",
    "visibility": "shared", // [DEPRECATED in v1.1]
    "event_id": "string"
  }
  ```
  *(Note: `event_id` is an optional root-level string, populated particularly when `status` is `queued` to allow the calling harness or client to poll or subscribe to async ingestion lifecycle updates.)*

#### 3.2.2 amp.recall
Retrieve relevant memories.

* **Input:**
  ```json
  {
    "scope": {
      "agent_id": "string",
      "group_id": "string",
      "workspace_id": "string",
      "user_id": "string",
      "session_id": "string",
      "app_id": "string",
      "org_id": "string"
    },
    "query": "string",
    "top_k": 10,
    "filters": {
      "status": "active | pinned | archived",
      "visibility": "string", // [DEPRECATED in v1.1]
      "source": "string",
      "timestamp_after": "ISO 8601",
      "timestamp_before": "ISO 8601"
    }
  }
  ```

* **Output:**
  ```json
  {
    "results": [MemoryResult]
  }
  ```

#### 3.2.3 amp.forget
Delete a memory.

* **Input:**
  ```json
  {
    "scope": {
      "agent_id": "string"
    },
    "id": "string"
  }
  ```

* **Output:**
  ```json
  {
    "status": "forgotten | not_found"
  }
  ```

---

### 3.3 Harness-Facing APIs (System-Only / Excluded from LLM Adapter)

#### 3.3.1 amp.consolidate
Trigger memory deduplication, abstraction, decay, and archiving.

* **Input:**
  ```json
  {
    "scope": {
      "agent_id": "string",
      "user_id": "string",
      "org_id": "string"
    },
    "depth": "full | light"
  }
  ```

* **Output:**
  ```json
  {
    "status": "queued | ok | not_supported",
    "memories_processed": 14
  }
  ```

#### 3.3.2 amp.pin
Prevent a memory from being archived during consolidation.

* **Input:**
  ```json
  {
    "scope": {
      "agent_id": "string"
    },
    "id": "string"
  }
  ```

* **Output:**
  ```json
  {
    "status": "pinned | not_found"
  }
  ```

#### 3.3.3 amp.stats
Retrieve counts and backend metrics for a specific partition.

* **Input:**
  ```json
  {
    "scope": {
      "agent_id": "string",
      "group_id": "string",
      "workspace_id": "string",
      "user_id": "string",
      "org_id": "string"
    }
  }
  ```

* **Output:**
  ```json
  {
    "memory_count": 104,
    "unconsolidated_count": 8,
    "metadata": {
      "vector_store_size_bytes": 1048576,
      "last_consolidation_time": "2026-05-22T06:00:00Z"
    }
  }
  ```

#### 3.3.4 amp.export
Stream memories matching the scope as a Memory Exchange Format (MXF) document. The canonical mechanism for backup, migration, and cross-vendor portability. See Appendix A for the wire format.

This verb is **Harness-only** and SHOULD NOT be projected through the MCP Adapter to the LLM unless the host explicitly intends to grant agents bulk-read access. On the REST channel, conforming backends SHOULD stream `application/x-ndjson` rather than buffering the entire export in memory.

**Scope matching semantics.** `amp.export` (and, for consistency, `amp.recall`) match the request `scope` against each stored memory's scope using a **strict AND** rule over the keys the caller provided:

1. Only the keys the caller actually includes in the request `scope` participate in matching. Omitted keys are wildcards (no constraint).
2. For every key the caller did include, the stored memory's scope MUST contain that same key with an equal value. Missing keys on the stored side do NOT match — a memory stored as `{"agent_id": "bot-a"}` is NOT returned by a query for `{"agent_id": "bot-a", "user_id": "user_1"}`.
3. Stored memories MAY carry additional scope keys the caller did not specify; those extras do not block the match.

This is the same contract as `amp.recall` and is intentionally conservative: hierarchical queries are easy (`{"org_id": "X"}` returns every memory under org X regardless of agent/user) but no memory ever surfaces in a scope strictly more specific than the one it was stored under. The looser "match if stored scope is a prefix/ancestor of the requested scope" behaviour is intentionally NOT specified; a future minor revision MAY add it behind an explicit `scope_mode` flag.

* **Input:**
  ```json
  {
    "scope": {
      "agent_id": "string",
      "user_id": "string"
    },
    "filters": {
      "status": "active",
      "source": "user_stated",
      "timestamp_after": "2026-01-01T00:00:00Z"
    },
    "page_size": 10000,
    "cursor": "opaque-continuation-token"
  }
  ```

* **Output (MCP adapter channel):**
  ```json
  {
    "ndjson": "{\"id\":\"mem_001\",...}\n{\"id\":\"mem_002\",...}\n",
    "count": 2,
    "next_cursor": "opaque-continuation-token"
  }
  ```

  When `next_cursor` is present in the response, the caller MUST issue a follow-up `amp.export` call with that token in the input `cursor` field to retrieve the next page. When `next_cursor` is absent (or empty), the export is complete.

* **Output (REST channel):** `application/x-ndjson` stream, one `MemoryResult` row per line. Conformant backends MAY include a final newline-terminated line. The `Content-Length` header SHOULD be omitted for streamed responses; clients MUST parse line-by-line. If pagination is in effect (caller supplied `page_size` or backend chose to chunk), the REST response MUST include an `X-AMP-Next-Cursor` header with the same semantics as the MCP `next_cursor` field.

* **Pagination semantics.** `cursor` is an opaque, backend-defined continuation token. Backends MUST NOT depend on clients parsing it. `page_size` is a HINT — backends MAY emit fewer rows than requested (and SHOULD when memory pressure warrants), but MUST NOT exceed the hint. When `cursor` is omitted, the export starts from the beginning of the deterministic order. The MCP channel MUST cap a single response at a backend-chosen byte limit (recommended ≤ 10 MiB of `ndjson`) and use `next_cursor` to continue; this protects callers from unbounded buffering of multi-gigabyte exports.

* **Conformance:**
  - Full-conformant backends MUST implement `amp.export`.
  - Core-conformant backends MAY return `not_supported` (HTTP `501`, JSON-RPC `-32002`).
  - If `page_size` is absent on the MCP channel, the backend MUST apply its own cap and paginate via `next_cursor`. If `page_size` is absent on the REST channel, the backend MAY stream the entire result set in a single response (the stream itself bounds memory).
  - Rows MUST appear in deterministic order (recommended: ascending `timestamp`, ties broken by `id`) so that resumable exports are tractable. The same `cursor` issued to the same backend MUST resume at the same position even if new rows were written between calls (new rows MAY appear on a later page after the cursor's position, but MUST NOT cause already-emitted rows to be re-emitted).

#### 3.3.5 amp.import
Ingest a Memory Exchange Format (MXF) document into the backend under the given scope. Each input line is a `MemoryResult` row. The complement of `amp.export`; together the two verbs define a closed-loop migration contract between conforming backends.

This verb is **Harness-only** and SHOULD NOT be projected through the MCP Adapter to the LLM.

* **Input:**
  ```json
  {
    "scope": {"agent_id": "bot-a", "user_id": "user_1"},
    "ndjson": "{\"id\":\"mem_001\",\"content\":\"Likes espresso\",...}\n{\"id\":\"mem_002\",...}\n",
    "on_conflict": "skip",
    "scope_remap": "strict"
  }
  ```

* **`on_conflict` policy (controls how the import responds to a row that cannot be cleanly applied).**

  For `skip` and `overwrite` the trigger is specifically an id collision against an existing row. For `fail_atomic` and `fail_fast` the trigger is ANY row-level failure — id collision, malformed JSON, MemoryResult schema validation failure, or violation of the scope rules below. Callers picking one of the `fail_*` modes are saying "stop on the first thing that doesn't fit," not just "stop on the first id conflict."

  - `skip` *(default)*: on id collision, keep the existing row and count toward `skipped`. Idempotent re-import.
  - `overwrite`: on id collision, replace the existing row with the incoming one.
  - `fail_atomic`: abort the entire import on the first row-level failure and roll back every row written so far in the same call. Backends that cannot guarantee atomicity (e.g. no transaction support) MUST return `not_supported` (HTTP `501`, JSON-RPC `-32002`) when this mode is requested — partial commits under a `fail_atomic` label are NOT permitted.
  - `fail_fast`: abort the import on the first row-level failure but DO NOT roll back. Already-imported rows remain committed; the response reports `imported` count for committed rows plus the failing `line` in `errors[0]`. Use this when partial migration is acceptable and the caller will resume via id-keyed diff.

  *Note:* the v1.1 draft of this spec previously named the abort mode `fail` with implementation-defined rollback. That semantics is replaced by the explicit `fail_atomic` / `fail_fast` pair so callers always know which contract they're getting.

* **`scope_remap` policy (when an incoming row's own scope is a strict subset of the request scope):**
  - `strict` *(default)*: rows whose `scope` block omits any identity key present in the request `scope` MUST be counted as `failed` with `amp_error_code: "invalid_request"`. This is the safe default — preserves MXF round-trip semantics and prevents an import from silently elevating a memory's scope.
  - `inherit`: rows with partial scope inherit the missing identity keys from the request `scope`. So a row with `scope: {"agent_id": "bot-a"}` imported into request scope `{"agent_id": "bot-a", "user_id": "user_1"}` becomes a memory in the combined scope. Callers MUST opt into this behaviour explicitly; it changes data ownership and is unsuitable for round-trip migration without intent.

  In both modes, rows whose own `scope` block CONFLICTS with the request `scope` on any key (e.g. row says `agent_id: "bot-b"`, request says `agent_id: "bot-a"`) MUST be counted as `failed` with `amp_error_code: "invalid_request"`. Imports MUST NOT silently cross scope boundaries under either policy.

* **Malformed and schema-invalid rows.** A row that is unparseable JSON, fails `MemoryResult` schema validation, or violates the scope rules above is counted in `failed` with `amp_error_code: "invalid_request"` and a `line` entry; the import continues past it. The only exceptions are `on_conflict: "fail_atomic"` (which rolls back the entire import on the first failure) and `on_conflict: "fail_fast"` (which stops past the failing row but commits everything before it). The HTTP status for a partial-success import is `200`; the response body's `failed` count and `errors` array convey row-level failures. A full-request error (e.g. unparseable request body, missing required field at the request level, invalid scope shape) is a `400` with `AmpErrorData`.

* **Output:**
  ```json
  {
    "imported": 1843,
    "skipped": 12,
    "failed": 0,
    "event_id": "imp_2026-05-25-a1b2c3"
  }
  ```

* **Conformance:**
  - Full-conformant backends MUST implement `amp.import`.
  - Core-conformant backends MAY return `not_supported` (HTTP `501`, JSON-RPC `-32002`).
  - Backends that do not support transactions MUST return `not_supported` when `on_conflict: "fail_atomic"` is requested. They MAY support `fail_fast` instead.
  - The `errors` array MUST be present when `failed > 0`, with at least one entry per failed row; backends MAY truncate to a sensible bound (e.g. first 100 errors).

---

### 3.4 Error Handling & Protocol Mappings

To maintain complete parity between the **MCP Adapter Channel** (which uses JSON-RPC error frames) and the **Standalone REST/gRPC Channel** (which uses standard network protocol statuses), AMP v1.1 defines a strict 1-to-1 mapping between canonical error codes, JSON-RPC numbers, and HTTP status codes.

All service-level errors must return a JSON body matching the `AmpErrorData` schema.

| AMP Error Code (`amp_error_code`) | JSON-RPC Code | HTTP Status | Description |
|-----------------------------------|---------------|-------------|-------------|
| `invalid_request`                 | `-32600` / `-32602` | `400 Bad Request` | Missing required parameters, invalid scope configuration, validation errors, or empty inputs. |
| `not_found`                       | `-32001`      | `404 Not Found` | The specified memory `id` could not be found under the active scope/partition. |
| `not_supported`                   | `-32002`      | `501 Not Implemented` | The backend is a lightweight conformance engine and does not support the requested operation (e.g. `consolidate` or `pin`). |
| `backend_error`                   | `-32000`      | `500 Internal Server Error` | Unhandled database failures, connection dropouts, or downstream API timeouts. |

---

## 4. Reserved Metadata Vocabulary Registry

To resolve the "wild-west" of proprietary metadata formats, AMP v1.1 defines standard optional metadata fields. If a backend supports these properties, they **MUST** use these reserved keys.

| Metadata Key | JSON Type | Description |
|--------------|-----------|-------------|
| `amp.ttl` | integer | Time-to-live in seconds from creation, after which the backend should automatically delete (`forget`) or archive the memory. |
| `amp.confidence` | float | Float value between `0.0` and `1.0` reflecting the system's confidence in this memory's factual correctness. |
| `amp.entities` | array[string] | Extracted entities related to this memory (e.g., `["person:Alice", "concept:React"]`). |
| `amp.relations` | array[object] | Subject-Predicate-Object triplets for graph-based stores. Each object must contain `{"subject": "", "predicate": "", "object": ""}`. |
| `amp.categories` | array[string] | Broad classification categories for organizing memories (e.g., `["location", "preference", "hobby", "social"]`). |
| `amp.summary` | string | A higher-order summarized representation of this memory, generated during consolidation. |

---

## 5. Backward Compatibility & Deprecations

To ensure seamless integration for legacy client applications built against the AMP v1.0 specification, v1.1 retains two core scoping primitives in a deprecated state. Backward-compatible backends MUST gracefully parse and handle these fields:

1. **`private` (boolean, input parameter to `amp.encode`):**
   - **Legacy Semantics:** Used to determine whether a memory was private to an agent or shared across the application.
   - **v1.1 Mapping:** Backends should interpret `private: true` as a directive to scope the memory locally if no explicit `scope` is provided. If `scope` is present, `private` is ignored.

2. **`visibility` (string `["private", "shared"]`, field in `MemoryResult`, `EncodeResponse`, and `RecallFilters`):**
   - **Legacy Semantics:** Controlled accessibility partitioning.
   - **v1.1 Mapping:** `MemoryResult` and `EncodeResponse` payloads emitted by v1.1 servers may populate `visibility: "shared"` (or `"private"`) to avoid breaking client-side parser schemas that enforce this property. Likewise, `RecallFilters` gracefully parses `visibility` but ignores it or translates it to scope constraints.

---

## Appendix A: Memory Exchange Format (MXF)

To eliminate vendor lock-in, AMP v1.1 specifies the **Memory Exchange Format (MXF)** — a canonical migration format. The protocol verbs [`amp.export`](#334-ampexport) and [`amp.import`](#335-ampimport) produce and consume MXF documents respectively; Full-conformant backends MUST implement both. Core-conformant backends MAY return `not_supported` but SHOULD still accept inbound MXF via an out-of-band CLI for one-way migration.

### A.1 File Structure
* **Format:** JSON Lines (NDJSON). Each line is a single JSON object.
* **Extension:** `.mxf` or `.jsonl`

### A.2 Row Schema
Each row is a valid `MemoryResult` representation with a complete `scope` and `status` block:

```json
{"id": "mem_001", "content": "Likes espresso", "status": "active", "source": "user_stated", "timestamp": "2026-05-22T08:00:00Z", "scope": {"agent_id": "bot-a", "user_id": "user_1"}, "metadata": {"amp.entities": ["espresso"], "amp.categories": ["preference"]}}
{"id": "mem_002", "content": "Speaks French", "status": "pinned", "source": "inferred", "timestamp": "2026-05-22T08:05:00Z", "scope": {"agent_id": "bot-a", "user_id": "user_1"}, "metadata": {"amp.confidence": 0.9, "amp.categories": ["language"]}}
```

By conforming to this format, a user can instantly backup memories from Claude Dream Mode or Supermemory and restore them into local `smriti-memcore` without data loss or proprietary conversion.

---

## Appendix B: Frequently Asked Questions (FAQ)

### B.1 How do we handle private memories within collaborative scopes in v1.1?

In collaborative workloads (e.g., shared teams, departments, or workspaces), a user can isolate private, non-shareable memories inside a shared partition using two standard patterns:

1. **Intersection-Based Scope Refinement (Recommended):**
   Combine collaborative scoping keys (such as `workspace_id` or `group_id`) with individual identifying keys (such as `user_id` or `agent_id`) during ingestion. 
   
   Because AMP v1.1 backends isolate queries strictly by the intersection of all provided keys, queries made by other users (with different `user_id` values) will naturally exclude these private memories.
   
   * *Shared Workspace Memory Scope:* `{"workspace_id": "ws-corporate-finance"}`
   * *Alice's Private Workspace Memory Scope:* `{"workspace_id": "ws-corporate-finance", "user_id": "user-alice"}`

2. **Harness-Enforced Access Control (Metadata):**
   Attach access permissions inside the flexible `metadata` JSON bag:
   ```json
   {
     "content": "Personal workspace notes.",
     "scope": { "workspace_id": "ws-corporate-finance" },
     "metadata": {
       "amp.access_level": "private",
       "amp.owner_id": "user-alice"
     }
   }
   ```
   The application hosting harness (which manages sessions at the service boundary) intercepts retrieved memories and filters out any records whose metadata restrictions do not match the current active user before injecting context into the LLM prompt.

