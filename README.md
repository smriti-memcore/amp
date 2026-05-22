<div align="center">
  <img src="AMPLogo.png" alt="AMP Logo" width="200"/>
</div>

# Agent Memory Protocol (AMP)
*By Community, Of Community, For Community*

AMP is an open specification that defines a standard interface for persistent memory in AI agent systems. 

**AMP v1.1** defines a standalone, backend-agnostic service protocol (HTTP REST/gRPC first) with an optional **Model Context Protocol (MCP)** tool adapter wrapper. This separation enables application harnesses to run robust context management (prompt injections, background compaction, multi-tenant partitioning) programmatically, while exposing clean, cognitive tools directly to the LLM agent.

## Core Architecture & Verb Separation

AMP v1.1 separates concerns between cognitive agent operations and autonomic database operations:

### 1. Agent-Facing Tools (Exposed to the LLM)
Standardized tools that the LLM agent discovers and executes at runtime to read and write its memories.

| Verb | Description |
|------|-------------|
| `amp.encode` | Store a new memory for an agent, evaluating salience. |
| `amp.recall` | Retrieve memories relevant to a natural language query. |
| `amp.forget` | Permanently delete a memory, cascading deletion through indices. |

### 2. Harness-Facing APIs (System-Only Hooks)
REST/gRPC endpoints managed programmatically by the orchestrator/application framework (e.g. LangChain, LlamaIndex, Letta) out-of-band.

| Verb | Description |
|------|-------------|
| `amp.consolidate` | Trigger background compaction, deduction, and spaced decay. |
| `amp.pin` | Mark a memory as permanent (protecting it from consolidation archiving). |
| `amp.stats` | Return backend partition stats and sizing information. |

---

## Key Features in v1.1

* **Multi-Dimensional Scoping:** Partition memory namespaces across standard enterprise boundaries: `org_id`, `user_id`, `session_id`, and `agent_id`.
* **Reserved Metadata Vocabulary:** Standardize common properties like `amp.ttl`, `amp.confidence`, `amp.entities`, and `amp.relations` to eliminate proprietary vendor fragmentation.
* **Memory Exchange Format (MXF):** A canonical NDJSON-based backup and restore structure to enable seamless data portability between different memory backends.

---

## Repository Structure

```
amp/
├── spec/amp-v1.1.md        # The Specification (v1.1)
├── schema/amp.json         # JSON Schema for all AMP tools (supporting scoping & reserved keys)
├── schema/amp-openapi.yaml # OpenAPI 3.0 REST Specification for Standalone API
├── compliance/             # Compliance test suite (pytest)
│   └── test_amp_server.py
├── examples/               # Example implementations
│   └── minimal_server.py   # Minimal Core-conformant MCP server
└── python/                 # Python reference implementation
    └── amp-server/         # smriti-memcore AMP wrapper (Full-conformant)
```

## Quick Start

### Running the minimal example server (MCP standard)

```bash
python3 examples/minimal_server.py
```

No external dependencies — pure Python stdlib. The server speaks the MCP wire protocol manually over stdio.

### Running the compliance suite

```bash
pip install pytest
pytest compliance/test_amp_server.py --server-cmd "python3 your_server.py"
```

The compliance suite speaks raw MCP over stdio to verify that your adapter handles all Core-conformant or Full-conformant interactions.

### Conformance Levels

* **Core** — `amp.encode`, `amp.recall`, `amp.forget`, `amp.stats`. Sufficient for simple backends (Redis, basic vector DB).
* **Full** — All six verbs. Required for backends with advanced memory lifecycle management (decay, background consolidation, graph indexing).

---

## Status

v1.1 Draft — 2026-05-22. Ready for community review and feedback.

Open questions and next steps are tracked in [spec/amp-v1.1.md § Open Questions](spec/amp-v1.1.md#9-open-questions).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

**Quick entry points:**

- **Discuss an open question** — each of the 10 open questions in [spec §9](spec/amp-v1.0.md#9-open-questions) has a GitHub issue labeled [`open-question`](https://github.com/smriti-memcore/amp/labels/open-question)
- **Build a conformant backend** — run the compliance suite and open a [New Implementation](https://github.com/smriti-memcore/amp/issues/new?template=new-implementation.md) issue
- **Improve the compliance suite** — add edge-case tests or port to TypeScript/Go
- **Good first issue** — a Redis Core-conformant backend is a well-scoped starting point

## License

This specification and all code in this repository are released under the [MIT License](LICENSE).

---

*AMP is an independent open specification. It is not affiliated with Anthropic or the MCP project.*
