# Agent Memory Protocol (AMP)

AMP is an open specification that defines a standard interface for persistent memory in AI agent systems. Built as a set of tool definitions on top of the [Model Context Protocol (MCP)](https://modelcontextprotocol.io), AMP enables any memory backend to serve any MCP-compatible agent through a common contract.

**The problem:** Every AI memory system today re-invents the same verbs — store, retrieve, forget, organise — with incompatible schemas. An agent written for Supermemory cannot switch to smriti-memcore or Mem0 without code changes. MCP adoption alone is not interoperability.

**AMP's answer:** A six-verb interface that covers the complete memory lifecycle across backends of any complexity.

## The Six Verbs

| Verb | Description |
|------|-------------|
| `amp/encode` | Store a new memory for an agent |
| `amp/recall` | Retrieve memories relevant to a query |
| `amp/forget` | Permanently delete a memory |
| `amp/consolidate` | Trigger backend consolidation/reorganisation |
| `amp/pin` | Mark a memory as permanent (never archived) |
| `amp/stats` | Return backend statistics |

## Conformance Levels

**Core** — `amp/encode`, `amp/recall`, `amp/forget`, `amp/stats`. Sufficient for simple backends (Redis, basic vector DB).

**Full** — All six verbs. Required for backends with memory lifecycle management (consolidation, decay, pinning).

## Repository Structure

```
amp/
├── spec/amp-v1.0.md        # The specification
├── schema/amp.json         # JSON Schema for all AMP tools
├── compliance/             # Compliance test suite (pytest)
│   └── test_amp_server.py
├── examples/               # Example implementations
│   └── minimal_server.py   # Minimal Core-conformant MCP server
└── python/                 # Python reference implementation
    └── amp-server/         # smriti-memcore AMP wrapper (coming soon)
```

## Quick Start

### Running the minimal example server

```bash
python examples/minimal_server.py
```

No dependencies — pure Python stdlib. This starts a Core-conformant AMP server over MCP stdio. Connect it to any MCP client.

### Running the compliance suite against your server

```bash
pip install pytest
pytest compliance/test_amp_server.py --server-cmd "python your_server.py"
```

### Implementing your own backend

1. Implement the four Core verbs (`amp/encode`, `amp/recall`, `amp/forget`, `amp/stats`) as MCP tools
2. Declare conformance in your MCP server manifest:
   ```json
   {
     "name": "my-memory-backend",
     "amp_conformance": "core",
     "amp_version": "1.0"
   }
   ```
3. Run the compliance suite to verify

## Reference Implementation

[smriti-memcore](https://pypi.org/project/smriti-memcore/) is the reference Full-conformance AMP implementation. It provides hybrid FTS5+vector retrieval with RRF fusion, multi-hop Semantic Palace graph traversal, spaced-repetition decay, and background consolidation.

The current smriti-memcore tool names (`smriti_encode`, `smriti_recall`, etc.) will gain AMP aliases (`amp/encode`, `amp/recall`, etc.) in a future release.

## Specification

See [spec/amp-v1.0.md](spec/amp-v1.0.md) for the full specification.

## Status

v1.0 — 2026-05-14. Ready for community implementation and feedback.

Open questions and next steps are tracked in [spec/amp-v1.0.md § Open Questions](spec/amp-v1.0.md#9-open-questions).

## License

This specification and all code in this repository are released under the [MIT License](LICENSE).

---

*AMP is an independent open specification. It is not affiliated with Anthropic or the MCP project.*
