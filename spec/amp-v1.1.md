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

