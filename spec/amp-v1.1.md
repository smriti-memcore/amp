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

To eliminate vendor lock-in, AMP v1.1 specifies the **Memory Exchange Format (MXF)**—a canonical migration format. An AMP-compliant backend **SHOULD** provide export/import tools that output/consume MXF files.

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

