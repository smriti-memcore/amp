# Agent Memory Protocol (AMP) — Specification v1.2-draft

> **Status — 2026-05-31:** v1.1 (the previous tagged revision) is stable and in production use. This document tracks the in-progress **v1.2-draft** revision, which extends v1.1 with new Appendix C (REST routing + gRPC interface contracts), provenance/lineage reserved metadata keys, and (in follow-up PRs) the `amp.update` and `amp.batch_encode` verbs and metadata filters in `RecallFilters`. v1.1-conformant backends remain conformant; v1.2-draft adds capability without breaking the v1.1 surface.
*By Community, Of Community, For Community*

> **Status:** v1.2-draft — 2026-05-31 (extends v1.1, released 2026-05-22)
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

**Isolating vs non-isolating keys.** Only `agent_id`, `group_id`, `workspace_id`, and `user_id` are *isolating* — at least one of them MUST be present in every request scope. `session_id`, `app_id`, and `org_id` are *refining* keys: they narrow the namespace further but do NOT satisfy the at-least-one-isolating-key requirement on their own. A request whose scope contains only refining keys MUST be rejected with `invalid_request` (HTTP 400 / JSON-RPC -32602).

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

#### 3.2.4 amp.update *(v1.2-draft)*
Mutate the `content` and/or `metadata` of an existing memory in place. The memory's `id`, `scope`, `source`, `status`, and `creation_time` are preserved; only `content` and `metadata` are mutable. Closes the v1.1 gap where a fact correction or a confidence-recomputation forced callers to `forget` + `encode` and lose the original `id` and any graph edges that referenced it.

**Conformance.** Introduced in v1.2-draft. v1.1-conformant backends MAY return `not_supported` (HTTP `501`, JSON-RPC `-32002`). v1.2-conformant backends MUST implement it.

**Annotations** (per §3.4 verb table): `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`. Update is idempotent because applying the same patch twice produces the same end state.

**Metadata merge vs replace.** By default `metadata` is applied as a JSON Merge Patch ([RFC 7396](https://www.rfc-editor.org/rfc/rfc7396)):

1. Keys present in the request are written into the stored metadata bag.
2. Keys absent from the request are preserved.
3. A key whose value is explicit JSON `null` is removed.

This matches the principle of least surprise for a partial-update verb and is what every other "patch" API in the wild does (Stripe, Notion, GitHub). Callers that want wholesale replacement opt in with `metadata_mode: "replace"`, which writes the request value verbatim — silently discarding any keys not present in the patch. Backends MUST NOT default to replace semantics; the data-loss surface is too easy to trigger by accident.

**Content semantics.** Omit `content` to leave it unchanged. An empty string is **not** a delete signal — it is rejected with `invalid_request` because a memory cannot have empty content (use `amp.forget` to delete a row).

**No-op detection.** A well-formed request that produces no observable change (both `content` and `metadata` omitted, or a metadata merge that yields a byte-identical stored bag) returns `status: "no_change"` rather than `"updated"`. Callers can rely on this to short-circuit cache invalidation. Backends that cannot cheaply detect no-change MAY return `"updated"` instead — both are conformant.

**Scope isolation.** Cross-scope updates are rejected as `not_found` (mirroring `amp.forget`), NOT as `invalid_request`. A scope-A caller passing a scope-B id MUST see the same response as if the id never existed — anything else would leak existence information across the partition boundary.

* **Input:**
  ```json
  {
    "scope": {
      "agent_id": "string"
    },
    "id": "string",
    "content": "string (optional)",
    "metadata": {
      "amp.confidence": 0.92,
      "amp.categories": ["preference"],
      "stale_key": null
    },
    "metadata_mode": "merge | replace"
  }
  ```

* **Output:**
  ```json
  {
    "status": "updated | no_change | not_found | not_supported",
    "id": "string"
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

    *Counters on `fail_atomic` rollback.* Because the whole import is reverted, the response counters describe only the trigger row, not the rows that were transiently written and then rolled back: `imported = 0`, `skipped = 0`, `failed = 1`, `errors` contains exactly one entry pointing at the trigger row's `line`. This is the contract regardless of how many rows were applied before the rollback fired.
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

### 3.4 MCP Tool Annotations

Per MCP revision 2025-03-26, every tool declaration carries a `ToolAnnotations` block that hints to the host how to gate, cache, and surface that tool. AMP v1.1 servers MUST publish these annotations on every verb so hosts can apply consistent policy (e.g. require user confirmation for destructive verbs, cache read-only results, batch idempotent calls). Per the MCP spec these are HINTS, not enforced contracts — backends MUST still validate inputs and authorise calls server-side.

| Verb              | readOnlyHint | destructiveHint | idempotentHint | openWorldHint | Rationale |
|-------------------|--------------|-----------------|----------------|---------------|-----------|
| `amp.encode`      | false | false | false | false | Mutates store. Each call creates a new memory `id`, so repeated calls produce distinct rows (not idempotent). |
| `amp.recall`      | true  | false | true  | false | Pure read against the partition. Safe to cache for short windows. |
| `amp.forget`      | false | **true**  | true  | false | Deletes a memory. Idempotent because re-forgetting returns `not_found` rather than erroring; hosts SHOULD still gate the first call. |
| `amp.consolidate` | false | false | false | **true**  | Background graph mutation. Each pass operates on an evolving state; results depend on accumulated episode-buffer contents and (optionally) external LLM calls. |
| `amp.pin`         | false | false | true  | false | Marks a memory permanent. Pinning twice is the same as pinning once. |
| `amp.stats`       | true  | false | true  | false | Pure read of backend counters. |
| `amp.export`      | true  | false | true  | false | Read-only bulk dump (Harness-only). Idempotent: the same cursor against the same backend resumes at the same position. |
| `amp.import`      | false | false | false | false | Mutates store. Not idempotent in the general case — `on_conflict=skip` makes a single MXF document round-trippable, but two distinct documents with overlapping content produce different end-states than the union would. |
| `amp.update`      | false | false | true  | false | *v1.2-draft.* Mutates the content/metadata of a memory in place. Idempotent because applying the same patch twice produces the same end state. |

**Harness-only verbs.** `amp.consolidate`, `amp.stats`, `amp.export`, and `amp.import` are Harness-tier system verbs and SHOULD NOT be projected through the MCP Adapter to the LLM unless the host explicitly grants bulk-read or admin access. The annotations above describe the verbs themselves, not the access policy.

---

### 3.5 Error Handling & Protocol Mappings

To maintain complete parity between the **MCP Adapter Channel** (which uses JSON-RPC error frames) and the **Standalone REST/gRPC Channel** (which uses standard network protocol statuses), AMP v1.1 defines a strict 1-to-1 mapping between canonical error codes, JSON-RPC numbers, and HTTP status codes.

All service-level errors must return a JSON body matching the `AmpErrorData` schema.

| AMP Error Code (`amp_error_code`) | JSON-RPC Code | HTTP Status | Description |
|-----------------------------------|---------------|-------------|-------------|
| `invalid_request`                 | `-32602`      | `400 Bad Request` | Missing required parameters, invalid scope configuration, validation errors, or empty inputs. Backends MAY use `-32600` instead when the inbound JSON-RPC frame itself is malformed at the transport layer (vs a well-formed call with bad params). |
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
| `amp.provenance.message_id` *(v1.2-draft)* | string | Conversation message id that triggered the memory's creation. Provides auditable evidence trace from a stored memory back to the source turn. |
| `amp.provenance.run_id` *(v1.2-draft)* | string | The runtime session or execution-context run id under which the memory was generated. Useful for grouping memories that originated in the same agent loop. |
| `amp.lineage.parent_ids` *(v1.2-draft)* | array[string] | Identifiers of parent memories merged or summarised into this memory during consolidation. Lets callers walk back from a consolidated summary to the raw rows that fed it. |

The three `amp.provenance.*` / `amp.lineage.*` keys are introduced in v1.2-draft. v1.1-conformant backends MAY populate them; v1.2-conformant backends SHOULD populate them when the source information is available. Consumers MUST tolerate their absence.

---

## 5. Backward Compatibility & Deprecations

v1.0 modelled access control as a flat `agent_id` partition with a per-memory `private` boolean and a `visibility ∈ {"private","shared"}` field. v1.1 replaces this entire mechanism with the multi-dimensional `scope` object (§2.3): partitioning is structural, not flag-based. `private` and `visibility` are retained as **deprecated input/output fields** for one minor revision so existing v1.0 clients keep working unchanged. They are scheduled for removal in **AMP v1.2** — backends MAY drop them at any v1.2-conformant release.

### 5.1 The migration question v1.0 leaves on the table

A v1.0 caller writing `private: true` means *"only the calling agent can read this back."* There is no v1.1 field with exactly that meaning — v1.1 has no "private to me" notion, only structural scope partitioning. A faithful v1.1 mapping therefore depends on **what the v1.0 client thought "me" was**, which the protocol never made explicit. Backends MUST choose one of the two mappings below and document the choice in their conformance manifest (e.g. via an `amp.legacy_private_strategy` server-info field).

### 5.2 Two conformant mappings for `private: true`

A v1.1 backend handling a legacy v1.0 `amp.encode` call (one carrying flat `agent_id` and `private` but no `scope`) MUST resolve the encode under one of these two strategies:

**Strategy A — Agent-private (default).**
Map `private: true` to the same `agent_id`-only scope as `private: false`. Both end up as `{"scope": {"agent_id": <agent_id>}}`; the `private` flag is treated purely as a hint and the response echoes `visibility` for v1.0 wire-compat. This is **lossless** for any v1.0 deployment where `agent_id` was already the unit of privacy (single-agent assistants, agent-per-user setups, every v1.0 deployment that didn't use `private` for cross-agent isolation within a shared namespace). It's the recommended default because it never silently elevates scope.

**Strategy B — User-private (opt-in).**
When the v1.0 caller's intent is *"private to the end user inside a shared agent namespace"* (e.g. one customer-support bot serving many users, where `private: true` meant "this fact belongs to this user only"), the backend MUST require the caller to also supply a `user_id` either alongside `agent_id` or via a sticky session binding established at handshake. The encode is then routed to `{"scope": {"agent_id": <agent_id>, "user_id": <user_id>}}` for `private: true` and `{"scope": {"agent_id": <agent_id>}}` for `private: false`. Backends MUST NOT silently invent a `user_id` (e.g. from the caller's IP, JWT subject, or a hash) — that would change ownership semantics under the v1.0 client's feet. If `user_id` cannot be resolved deterministically, the backend MUST fall back to Strategy A and SHOULD log a one-shot warning.

### 5.3 Recall behaviour on legacy fields

A v1.0 caller passing `RecallFilters.visibility = "private"` against a Strategy-A backend MUST be served the same result set as a v1.1 recall with `{"scope": {"agent_id": <agent_id>}}` and no additional filter — i.e. the visibility filter is a no-op, because every memory in that partition is "private" in the v1.0 sense. A Strategy-B backend MUST apply the additional `user_id` constraint. In both cases the recall MUST NOT error on the deprecated filter; ignoring it silently is the wrong behaviour and the spec previously left this ambiguous.

### 5.4 Deprecated-field echo discipline

v1.1-native callers (those that supply `scope` and omit `private`) MUST receive responses that omit `visibility`. v1.0-native callers (those that supply `private` and omit `scope`) MUST receive responses that include `visibility` populated as `"private"` or `"shared"` mirroring the `private` they sent. This means a v1.1 server emits both shapes depending on the call — the deprecation discipline is observable by the caller, not statically determined by the server.

For `MemoryResult` rows emitted during recall, the rule is: include `visibility` only when the caller's recall request itself referenced `visibility` (in `RecallFilters` or the legacy v1.0 client header `X-AMP-Compat: v1.0`). Otherwise omit it. This keeps v1.1-native consumers free of deprecated payload noise.

### 5.5 Deprecation timeline

| Field | Behaviour in v1.1 | Behaviour in v1.2 |
|---|---|---|
| `private` (input to `amp.encode`) | Accepted, mapped per Strategy A or B above | MUST be rejected with `invalid_request` |
| `visibility` (output on `MemoryResult` / `EncodeResponse`) | Echoed only when the caller's request used legacy shapes | MUST NOT be emitted |
| `visibility` (input to `RecallFilters`) | Accepted, applied as a no-op (Strategy A) or `user_id` constraint (Strategy B) | MUST be rejected with `invalid_request` |
| Flat `agent_id` at request root (instead of `scope.agent_id`) | Accepted, promoted to `scope.agent_id` | Accepted (this one stays — flat `agent_id` is a convenience alias, not a separate semantic) |

Backends targeting v1.1 SHOULD log a structured deprecation warning the first time a deprecated field is accepted from a given caller identity, so operators can track v1.0 client migration progress.

### 5.6 Inside collaborative scopes — see Appendix B

The above governs v1.0→v1.1 **migration**. Once on v1.1, the question *"how do I store something private to one user within a shared workspace"* is answered structurally by intersection scoping; see Appendix B.1 for the recipe. The deprecated `private` flag has no role inside a v1.1-native call.

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


---

## Appendix C: Standalone API Channel Interface Contracts

To standardise deployments using the **Standalone REST/gRPC API Channel** (§1.3), conformant servers and harnesses SHOULD follow the network-interface contract mappings below. v1.1-conformant servers MUST support the v1.1 REST endpoints (six rows below, marked *v1.1*); the rows marked *v1.2-draft* describe routes for verbs proposed in v1.2-draft and are not required for v1.1 conformance.

### C.1 HTTP REST Endpoint Mapping

| HTTP Method | Route Path | AMP Verb | Conformance | Description |
|-------------|------------|----------|-------------|-------------|
| `POST`   | `/v1/memories`               | `amp.encode`      | v1.1 | Store a new memory record. Accepts the standard JSON encode payload defined by `EncodeRequest`. |
| `POST`   | `/v1/memories/recall`        | `amp.recall`      | v1.1 | Query relevant memories via semantic / keyword search. `POST` is used so complex scope and filter payloads can travel in the body rather than as query strings. |
| `DELETE` | `/v1/memories/{id}`          | `amp.forget`      | v1.1 | Permanently erase a memory record by `id`. Scope travels as a `ScopeEnvelope` body per §3.3.3 (DELETE-with-body is valid per RFC 9110 §9.3.5). |
| `PUT`    | `/v1/memories/{id}/pin`      | `amp.pin`         | v1.1 | Pin a specific memory so consolidation cannot archive it. |
| `POST`   | `/v1/memories/consolidate`   | `amp.consolidate` | v1.1 | Trigger background memory consolidation for a specified scope partition. |
| `GET`    | `/v1/memories/stats`         | `amp.stats`       | v1.1 | Retrieve count, status, and sizing metrics. Scope travels as discrete query parameters. |
| `POST`   | `/v1/memories/export`        | `amp.export`      | v1.1 (Full) | Bulk export of a scope as MXF NDJSON. Streams `application/x-ndjson`. |
| `POST`   | `/v1/memories/import`        | `amp.import`      | v1.1 (Full) | Bulk import of MXF NDJSON under a scope. Single `application/json` body wrapping the inline NDJSON and policy fields, matching the MCP shape byte-for-byte. |
| `PATCH`  | `/v1/memories/{id}`          | `amp.update`      | v1.2-draft | Mutate a memory's `content` and/or `metadata` for an existing record without changing its `id`. |
| `POST`   | `/v1/memories/batch_encode`  | `amp.batch_encode`| v1.2-draft | Store multiple memories in a single network round-trip. |

The exact request/response shapes are defined by `schema/amp-openapi.yaml`. Backends MAY expose additional vendor-specific endpoints, but those MUST NOT shadow the paths above.

*Authentication & authorization is intentionally out of scope for v1.1; an auth model — including the corresponding `unauthorized` / `forbidden` error codes — is tracked as a v1.2 design item. Until that model lands, backends that need access control SHOULD apply it at the harness layer or via a reverse proxy in front of `/v1/`.*

### C.2 gRPC Protocol Buffer Definition

For low-latency, high-throughput inter-service communication, conformant backends MAY expose the AMP contract as the `MemoryService` gRPC service using the Protobuf v3 definition below. The proto is descriptive — it documents how the JSON Schema in `schema/amp.json` maps to wire-compatible Protobuf — and is NOT separately versioned: a backend exposing the gRPC channel MUST keep its `MemoryService` semantics equivalent to its JSON-Schema-defined behaviour.

The proto includes RPCs for the v1.2-draft `amp.update` verb so backends that do implement it have a canonical gRPC shape to follow. v1.1-only backends MAY omit the `Update` RPC. `amp.batch_encode` is left to a future revision of the proto to avoid lock-in on the batch shape before the verb's schema stabilises.

```protobuf
syntax = "proto3";

package amp.v1;

option go_package = "github.com/agent-memory-protocol/amp/v1;ampv1";
option java_multiple_files = true;
option java_package = "org.agentmemoryprotocol.amp.v1";

// Multi-dimensional scoping keys per spec §2.3. At least one of the
// isolating keys (agent_id, group_id, workspace_id, user_id) MUST be set.
message Scope {
  string agent_id     = 1;
  string group_id     = 2;
  string workspace_id = 3;
  string user_id      = 4;
  string session_id   = 5;
  string app_id       = 6;
  string org_id       = 7;
}

// v1.2-draft: structured metadata predicate. Operator is one of
// {eq, ne, gt, gte, lt, lte, in, contains}. `value` is a JSON-encoded
// string so a single field carries any scalar / array literal.
message MetadataFilter {
  string key       = 1;
  string operator  = 2;
  string value     = 3;
}

message RecallFilters {
  string status            = 1;  // active | pinned | archived
  string source            = 2;
  string timestamp_after   = 3;  // ISO 8601
  string timestamp_before  = 4;  // ISO 8601
  repeated MetadataFilter metadata_filters = 5;  // v1.2-draft
}

message MemoryResult {
  string id            = 1;
  string content       = 2;
  double score         = 3;
  string source        = 4;
  string timestamp     = 5;  // ISO 8601
  string status        = 6;  // active | pinned | archived
  Scope  scope         = 7;
  string metadata_json = 8;  // JSON bag supporting reserved and custom keys
}

message AmpError {
  string amp_error_code = 1;  // invalid_request | not_found | not_supported | backend_error
  string message        = 2;
}

message EncodeRequest {
  Scope  scope         = 1;
  string content       = 2;
  string source        = 3;
  bool   force         = 4;
  string metadata_json = 5;
}

message EncodeResponse {
  string id       = 1;
  string status   = 2;  // stored | duplicate | below_threshold | queued
  string event_id = 3;
}

message RecallRequest {
  Scope         scope   = 1;
  string        query   = 2;
  int32         top_k   = 3;
  RecallFilters filters = 4;
}

message RecallResponse {
  repeated MemoryResult results = 1;
}

message ForgetRequest {
  Scope  scope = 1;
  string id    = 2;
}

message ForgetResponse {
  string status = 1;  // forgotten | not_found
}

message PinRequest {
  Scope  scope = 1;
  string id    = 2;
}

message PinResponse {
  string status = 1;  // pinned | not_found | not_supported
}

message ConsolidateRequest {
  Scope  scope = 1;
  string depth = 2;  // full | light
}

message ConsolidateResponse {
  string status              = 1;  // queued | ok | not_supported
  int32  memories_processed  = 2;
}

message StatsRequest {
  Scope scope = 1;
}

message StatsResponse {
  int32  memory_count         = 1;
  int32  unconsolidated_count = 2;
  string metadata_json        = 3;
}

// v1.2-draft: mutate an existing memory in place. Backends that have
// not yet implemented amp.update MAY omit this RPC.
message UpdateRequest {
  Scope  scope         = 1;
  string id            = 2;
  string content       = 3;  // omit to leave unchanged
  string metadata_json = 4;  // omit to leave unchanged; backend MAY define merge vs replace
}

message UpdateResponse {
  string status = 1;  // updated | not_found | not_supported
}

// Service definition exposing the AMP contract over gRPC.
// All RPCs MUST return AmpError (via standard gRPC status with the AMP
// error code in the trailer) on failure, mirroring §3.5 error mapping.
service MemoryService {
  rpc Encode      (EncodeRequest)      returns (EncodeResponse);
  rpc Recall      (RecallRequest)      returns (RecallResponse);
  rpc Forget      (ForgetRequest)      returns (ForgetResponse);
  rpc Pin         (PinRequest)         returns (PinResponse);
  rpc Consolidate (ConsolidateRequest) returns (ConsolidateResponse);
  rpc Stats       (StatsRequest)       returns (StatsResponse);
  rpc Update      (UpdateRequest)      returns (UpdateResponse);  // v1.2-draft
}
```

A `.proto` file mirroring the definition above will be shipped under `schema/amp.proto` once a reference gRPC server implementation lands. Until then, the spec text in this appendix is normative for any backend exposing the gRPC channel.
