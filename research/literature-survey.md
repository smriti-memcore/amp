# AMP Literature Survey

*Research compiled May 2026. Covers production memory systems, academic proposals, and naming conflicts relevant to the Agent Memory Protocol.*

---

## 1. Production Memory Systems

### Mem0
- **Repo**: [mem0ai/mem0](https://github.com/mem0ai/mem0) — MIT, 47k+ stars
- **Operations**: `add`, `search`, `get`, `get_all`, `update`, `delete`, `delete_all`, `history` (8 verbs)
- **Transport**: REST (v3 API) + Python/TypeScript/Go SDK; no MCP transport
- **Memory record schema**: `{messages[], user_id, agent_id, run_id, app_id, metadata}` — namespace fields are first-class, async returns `event_id`
- **Lifecycle**: None explicit — no consolidation, pinning, or decay verbs
- **Multi-tenancy**: Strong — `user_id + agent_id + run_id` as composable namespace keys
- **Schema versioning**: API versioned (v3), but no portable spec document
- **Conformance levels**: None
- **Notable**: Richest multi-tenancy model of any system surveyed; extraction-first approach (aggressive summarisation) scores poorly on narrative recall benchmarks (21.7% on LoCoMo vs AMP reference impl)
- **Sources**: [Mem0 API Reference](https://docs.mem0.ai/api-reference), [Add Memories endpoint](https://docs.mem0.ai/api-reference/memory/add-memories), [Mem0 GitHub](https://github.com/mem0ai/mem0)

---

### Zep / Graphiti
- **Repo**: [getzep/zep](https://github.com/getzep/zep) — Apache 2.0 community edition; cloud proprietary
- **Operations**: `thread.add_messages()`, `graph.add()`, `thread.get_user_context()`, CRUD on graph nodes — no named verb set; operations are SDK method calls
- **Transport**: REST + Python/TypeScript/Go SDKs; no MCP
- **Memory record schema**: Temporal knowledge graph — facts with validity date ranges, entity nodes, typed edges
- **Lifecycle**: Automatic temporal invalidation of stale facts (Graphiti engine); no explicit `consolidate`, `pin`, or `forget` verbs
- **Multi-tenancy**: User-scoped threads + group subgraphs
- **Schema versioning**: None
- **Conformance levels**: None
- **Notable**: Best temporal reasoning of any system surveyed — Graphiti automatically tracks when facts become invalid (e.g. "user lives in NYC" superseded by "user moved to London"). No standard transport.
- **Sources**: [Zep Agent Memory](https://www.getzep.com/product/agent-memory/), [Zep GitHub](https://github.com/getzep/zep)

---

### Letta / MemGPT
- **Repo**: [letta-ai/letta](https://github.com/letta-ai/letta) — Apache 2.0
- **Operations** (agent-callable LLM tools): `core_memory_append`, `core_memory_replace`, `archival_memory_insert`, `archival_memory_search`, `recall_memory_search`
- **REST API**: Stateful endpoints via `client.agents.passages.*`; memory block schema = `{label, description, value, char_limit}`
- **Transport**: REST + Python SDK; no MCP
- **Lifecycle**: Tiered storage architecture (core / archival / recall) — no explicit consolidation verb; tiering is implicit
- **Multi-tenancy**: Per-agent memory blocks, separate archival stores per agent
- **Schema versioning**: None
- **Conformance levels**: None
- **Also**: `ai-memory-sdk` (simplified wrapper) adds `initialize_subject`, `get_memory`, `add_messages`, `search`, `delete_block` — not a standard spec
- **Sources**: [Letta GitHub](https://github.com/letta-ai/letta), [Letta Archival Memory docs](https://docs.letta.com/guides/agents/archival-memory/), [ai-memory-sdk](https://github.com/letta-ai/ai-memory-sdk)

---

### Cognee
- **Repo**: [topoteretes/cognee](https://github.com/topoteretes/cognee) — Apache 2.0
- **Operations**: `add` (ingest), `cognify` (build knowledge graph), `search`, `forget`
- **Transport**: Python API primarily; `cognee-mcp` wrapper available but wraps a proprietary graph pipeline, not a standard protocol
- **Lifecycle**: `forget` exists; no pinning or explicit consolidation
- **Multi-tenancy**: Dataset-level ownership with permissions
- **Schema versioning**: None
- **Conformance levels**: None
- **Sources**: [Cognee GitHub](https://github.com/topoteretes/cognee), [Cognee MCP blog](https://www.cognee.ai/blog/cognee-news/introducing-cognee-mcp)

---

### Supermemory
- **Repo**: [supermemoryai/supermemory](https://github.com/supermemoryai/supermemory) — MIT wrapper, proprietary backend
- **MCP tools**: `memory`, `recall`, `context` (3 tools)
- **Memory record schema**: `{id, status, content, customId, containerTag, metadata}`
- **Transport**: MCP (Cloudflare Workers + Durable Objects) + REST v3 API
- **Lifecycle**: Automatic extraction, update, and contradiction resolution — no callable `consolidate`, `pin`
- **Multi-tenancy**: `x-sm-project` header + auto-generated user profiles
- **Schema versioning**: None; "MCP v1 deprecated" noted in repo with no spec document
- **Conformance levels**: None
- **Notable**: Only competitor with native MCP transport like AMP — but the backend is proprietary (Cloudflare, no export API)
- **Sources**: [Supermemory GitHub](https://github.com/supermemoryai/supermemory), [Supermemory MCP GitHub](https://github.com/supermemoryai/supermemory-mcp)

---

### Anthropic Dream Mode
- **Docs**: [Anthropic Managed Agents / Dreams](https://platform.claude.com/docs/en/managed-agents/dreams) — proprietary, beta
- **Operations**: `POST /v1/dreams` (create), `GET /v1/dreams/{id}` (retrieve), `POST /v1/dreams/{id}/cancel`, `POST /v1/dreams/{id}/archive`, `GET /v1/dreams` (list)
- **Memory record schema**: Dream resource = `{id, status, inputs[], outputs[], model, instructions, usage, created_at, ended_at}` — operates on memory stores, not individual records
- **Transport**: REST only; gated behind two beta headers (`managed-agents-2026-04-01`, `dreaming-2026-04-21`)
- **Lifecycle**: Consolidation is the entire feature (deduplication, contradiction resolution, insight surfacing) — but as a batch job, not an agent-callable mid-session verb
- **Multi-tenancy**: Workspace-scoped; no per-agent isolation, no memory export API
- **Schema versioning**: Date-stamped beta headers (closest to versioning of any system, but still single-vendor)
- **Conformance levels**: None
- **Open source**: No
- **Notable**: Closest competitor to AMP's `consolidate` verb conceptually, but proprietary, batch-only, and Claude-exclusive
- **Sources**: [Anthropic Dreams docs](https://platform.claude.com/docs/en/managed-agents/dreams), [Anthropic Memory Tool docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)

---

### LangGraph / LangMem
- **Repo**: [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) — MIT
- **BaseStore operations**: `get`, `put`, `delete`, `search` (key-value with hierarchical namespaces + optional vector search)
- **LangMem higher-level**: `create_memory_manager()`, `create_memory_store_manager()` — configurable insert/update/delete triggers via Pydantic custom schemas
- **Transport**: In-process Python only; no MCP, no standard transport
- **Memory record schema**: `Item{content, kind}` base, extensible via Pydantic; custom schemas user-defined
- **Lifecycle**: TTL support, versioned history of changes; no explicit `consolidate` or `pin` verb
- **Multi-tenancy**: Hierarchical namespace strings (e.g. `(user_id, "preferences")`)
- **Schema versioning**: None
- **Conformance levels**: None (operation modes insert/update/delete can be toggled independently)
- **Sources**: [LangMem Memory API Reference](https://langchain-ai.github.io/langmem/reference/memory/), [LangGraph Memory docs](https://docs.langchain.com/oss/python/langgraph/memory)

---

## 2. Academic Proposals

### Portable Agent Memory — Ravindran (Microsoft, 2026)
- **Paper**: arXiv:2605.11032 — "Portable Agent Memory: A Protocol for Provenance-Verified Memory Transfer Across Heterogeneous LLM Agents" (May 10, 2026)
- **Author**: Santhosh Kumar Ravindran, Microsoft Corporation
- **GitHub**: [santhoshravindran7/portable-agent-memory](https://github.com/santhoshravindran7/portable-agent-memory) — Python, Apache 2.0, 8 stars
- **Proposal**: Five-component memory model (episodic, semantic, procedural, working, identity); Merkle-DAG provenance graph for tamper-evident verification; capability-scoped access tokens; injection-resistant re-hydration pipeline. Tested across GPT-4, Claude, Gemini, and Llama.
- **Transport**: None specified — focuses on serialisation and transfer format, not a callable interface
- **Conformance levels**: None
- **Key difference from AMP**: Addresses a complementary problem — cryptographic provenance (who created/modified a memory) and secure cross-system transfer, not transport-layer interoperability. No lifecycle verbs, no MCP, no conformance levels. Significantly more complex for a security use case AMP currently does not address.
- **Relevance**: Strong independent validation that memory portability across heterogeneous agents is an unsolved problem. The provenance and security primitives proposed here (Merkle-DAG, access tokens) could inform a future AMP extension for verified memory transfer.

### SAMEP — Secure Agent Memory Exchange Protocol
- **Paper**: arXiv:2507.10562 (July 2025)
- **Proposal**: Distributed memory repository with vector-based semantic search and AES-256-GCM access controls; claims compatibility with MCP and A2A protocols
- **Status**: Preprint only; no public implementation found; full paper PDF not parseable at time of review
- **Relevance**: Names the same problem space; proposes security primitives AMP does not currently address

### MemOS / MemCube
- **Paper**: arXiv submit/6596874 (July 2025); backed by MemTensor
- **Proposal**: "MemCube" abstraction standardising memory representation, lifecycle management, cross-modal fusion, and dynamic memory state transitions — framed as a memory OS
- **Status**: No conformance levels; no verb set defined; no public implementation
- **Relevance**: Overlaps with AMP's lifecycle framing but is an architecture proposal, not a transport protocol

### Multi-Agent Memory Architecture Survey
- **Paper**: arXiv:2603.10062 (2026)
- **Proposal**: Survey paper; explicitly identifies "standard access protocol (permissions, scope, granularity)" as under-specified for shared agent memory
- **Status**: Names the problem AMP solves; proposes no solution
- **Relevance**: Useful citation for motivating AMP in the white paper

---

## 3. Naming Conflicts

### akshayaggarwal99/amp — "AMP: The Agent Memory Protocol"
- **URL**: [github.com/akshayaggarwal99/amp](https://github.com/akshayaggarwal99/amp)
- **Created**: December 13, 2025 — 5 months prior to smriti-memcore/amp (May 2026)
- **PyPI**: `amp-memory`
- **License**: MIT
- **Approach**: 3-layer brain (STM / LTM / Graph), visual dashboard (Galaxy View, Force Mode), semantic query UI; benchmarks 81.6% recall vs Mem0's 21.7% on LoCoMo
- **Key difference from our AMP**: No formal conformance levels, no verb specification, no portable protocol — it is an implementation, not a spec. Our AMP defines a transport-level protocol that any backend can implement; theirs is a single system.
- **Prior claim**: Has prior publication date. No trademark registered. Not an academic paper.
- **Recommendation for white paper**: Cite in Related Work section and differentiate clearly — *"Concurrently, Aggarwal [GitHub, 2025] proposed a system also named AMP focused on a layered STM/LTM/graph architecture with a visual dashboard. Unlike that system, this AMP defines an interoperability protocol with formal conformance levels, not a single implementation."*

### agentmessaging/protocol — "Agent Messaging Protocol (AMP)"
- **URL**: [github.com/agentmessaging/protocol](https://github.com/agentmessaging/protocol)
- **Focus**: Agent-to-agent secure messaging, not memory — different problem domain, same acronym
- **Relevance**: Low — different space; disambiguation in abstract sufficient

---

## 4. Comparison Matrix

| Dimension | **AMP (ours)** | **Mem0** | **Zep** | **Letta** | **Cognee** | **Supermemory** | **Anthropic Dreams** | **LangGraph/LangMem** |
|---|---|---|---|---|---|---|---|---|
| Verbs | 6 (encode, recall, forget, consolidate, pin, stats) | 8 | ~4 SDK methods | 5 agent tools | 4 | 3 MCP tools | 5 batch ops | 4 BaseStore |
| Canonical record schema | Yes (`MemoryResult`) | Partial | Temporal graph | Block schema | Graph nodes | Partial | Store-level only | Extensible Pydantic |
| Transport | MCP | REST + SDK | REST + SDK | REST + SDK | Python + MCP wrapper | MCP + REST | REST (beta) | In-process Python |
| Lifecycle mgmt (consolidate/pin) | Full (`consolidate`, `pin`, decay) | None | Auto temporal | Tiered architecture | `forget` only | Auto (no verbs) | Batch (not callable) | TTL + history |
| Schema versioning | `amp_version: 1.0` in spec | API v3 | None | None | None | None | Date-stamped headers | None |
| Conformance levels | Yes (Core / Full) | None | None | None | None | None | None | None |
| Agent namespacing | `agent_id` on all verbs | `user_id + agent_id + run_id` | User threads | Per-agent blocks | Dataset-level | Project header | Workspace-scoped | Hierarchical strings |
| Open source + portable | Yes (MIT, local files) | Yes (MIT) | Community (Apache 2.0) | Yes (Apache 2.0) | Yes (Apache 2.0) | MIT wrapper, proprietary backend | No | Yes (MIT) |

---

## 5. AMP's Differentiators

1. **Only protocol with formal conformance levels.** Core/Full split allows lightweight backends to adopt without implementing lifecycle operations.
2. **`consolidate` and `pin` as first-class agent-callable verbs.** All competitors with consolidation-like behaviour (Mem0, LangMem, Anthropic Dreams) either do it automatically or in batch — none expose it as a mid-session callable verb.
3. **Transport anchored to MCP from day one.** Competitors with MCP support treat it as a wrapper over a proprietary backend. AMP defines MCP as the transport itself.
4. **Canonical `MemoryResult` schema with open metadata bag.** Every competitor uses ad-hoc schemas. AMP defines a stable, versionable record schema.
5. **Portability.** Reference implementation stores to local files (`palace.json`); no vendor lock-in.

## 6. AMP's Gaps vs Competitors

- **Multi-tenancy**: Mem0's `user_id + agent_id + run_id` is richer than AMP's single `agent_id` (Open Question #10)
- **Update verb**: Mem0 and LangMem have explicit `update`; AMP uses encode-then-forget (Open Question #4/9)
- **Streaming recall**: No pagination for large `top_k` (Open Question #7)
- **Schema versioning in records**: `MemoryResult` has no `schema_version` field (Open Question #5)

---

*Research by Shivam Tyagi, May 2026. All URLs verified at time of writing.*
