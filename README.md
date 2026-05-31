<div align="center">
  <img src="AMPLogo.png" alt="AMP Logo" width="200"/>
</div>

# Agent Memory Protocol (AMP)
*By Community, Of Community, For Community*

AMP is an open specification that defines a standard interface for persistent memory in AI agent systems.

**AMP v1.1** is a dual-delivery service protocol: a standalone REST API (with OpenAPI 3.0 contract) and a parallel **Model Context Protocol (MCP)** tool adapter, sharing one set of JSON-Schema definitions. Application harnesses run robust context management (prompt injections, background compaction, multi-tenant partitioning) programmatically over the REST channel; the same backend exposes clean cognitive tools directly to the LLM agent over the MCP channel.

📘 Read the spec: [spec/amp-v1.1.md](spec/amp-v1.1.md) • Migration guide: [docs/MIGRATING-v1.0-to-v1.1.md](docs/MIGRATING-v1.0-to-v1.1.md)

---

## What's in v1.1

Eight verbs across two tiers, plus the connective tissue that makes them a *service protocol* rather than a *tool collection*:

### Agent-facing tools (safe to expose to the LLM)

| Verb | Description | readOnly | destructive |
|---|---|---|---|
| `amp.encode` | Store a new memory under a scope, salience-gated | — | — |
| `amp.recall` | Retrieve memories relevant to a natural-language query | ✓ | — |
| `amp.forget` | Permanently delete a memory | — | ✓ |

### Harness-facing system verbs (should NOT be projected to the LLM)

| Verb | Description | readOnly | destructive |
|---|---|---|---|
| `amp.consolidate` | Background compaction, deduction, spaced decay | — | — |
| `amp.pin` | Mark a memory permanent (excluded from consolidation archiving) | — | — |
| `amp.stats` | Backend partition stats and sizing information | ✓ | — |
| `amp.export` | Stream a scope as a Memory Exchange Format (MXF) NDJSON document | ✓ | — |
| `amp.import` | Ingest an MXF document under a scope, with conflict policy | — | — |

Every verb publishes full [MCP 2025-03-26 tool annotations](spec/amp-v1.1.md#34-mcp-tool-annotations) (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) so hosts can apply consistent gating, caching, and confirmation prompts.

### Key features

* **Dual-delivery channels.** Same backend, same scopes, same data — REST or MCP, your choice. The two channels share one canonical schema (`schema/amp.json` + `schema/amp-openapi.yaml`) so a Harness on REST and an Agent on MCP can collaborate on the same store with no adapter layer.
* **Multi-dimensional scoping.** Memories partition across `org_id`, `app_id`, `user_id`, `session_id`, `agent_id`, `group_id`, and `workspace_id` — at least one *isolating* key (`agent_id` / `group_id` / `workspace_id` / `user_id`) required. Hierarchical queries (`{org_id: X}` returns everything under org X) work; strict-AND semantics prevent silent scope-elevation across partitions.
* **Strict error-mapping contract.** §3.5 pins a 1-to-1 mapping between AMP error codes (`invalid_request` / `not_found` / `not_supported` / `backend_error`), JSON-RPC codes (`-32602` / `-32001` / `-32002` / `-32000`), and HTTP statuses (`400` / `404` / `501` / `500`). Both channels emit the same `AmpErrorData` payload.
* **MXF as a callable contract.** `amp.export` / `amp.import` are first-class protocol verbs, not "every implementation rolls its own admin CLI." Round-trip an MXF document between any two Full-conformant backends with no vendor adapter. Conflict policies (`skip` / `overwrite` / `fail_atomic` / `fail_fast`) and scope-remap policies (`strict` / `inherit`) are explicit.
* **Reserved metadata vocabulary.** `amp.ttl`, `amp.confidence`, `amp.entities`, `amp.relations`, `amp.categories`, `amp.summary` — namespaced reserved keys so backends interoperate on common fields without colliding with proprietary metadata.
* **v1.0 backward compatibility.** Flat `agent_id` + `private` + `visibility` continue to work for one minor revision. The two conformant migration mappings (Strategy A — Agent-private; Strategy B — User-private) are defined in [spec §5](spec/amp-v1.1.md#5-backward-compatibility--deprecations); a walkthrough lives in [docs/MIGRATING-v1.0-to-v1.1.md](docs/MIGRATING-v1.0-to-v1.1.md). Removal targeted at v1.2.

---

## Quick Start

### Run the reference server (Full-conformant, MCP stdio)

```bash
# One-shot run with uvx (no Python env management needed):
uvx --from "git+https://github.com/smriti-memcore/amp.git#subdirectory=python/amp-server" amp-server

# Or install into a venv:
pip install "git+https://github.com/smriti-memcore/amp.git#subdirectory=python/amp-server"
amp-server --storage-path ~/.amp
```

See [python/amp-server/README.md](python/amp-server/README.md) for Claude Desktop / Claude Code wiring and the current conformance-level breakdown of the reference impl.

### Run the compliance suite against your backend

```bash
pip install pytest
pytest compliance/test_amp_server.py --server-cmd "python3 your_server.py"
```

67 tests covering scope validation, error mapping, MCP tool annotations, content fidelity, namespace isolation, deprecated-field round-trip, and more. The suite speaks raw MCP over stdio — no mocking, no shortcuts.

### Conformance levels

* **Core** — `amp.encode`, `amp.recall`, `amp.forget`, `amp.stats`. Sufficient for simple backends (Redis with vector search, basic SQLite + FTS, etc.). Core backends MAY respond `not_supported` to any other verb.
* **Full** — All eight verbs. Required for backends with advanced memory lifecycle (decay, background consolidation, graph indexing) and MXF interop.

A backend's conformance level is published in the MCP `initialize` response under `serverInfo.amp_conformance` ∈ `{"core", "full"}`, alongside `serverInfo.amp_version` (string).

---

## Repository structure

```
amp/
├── spec/amp-v1.1.md                        # The specification (normative)
├── spec/amp-v1.0.md                        # Historical — v1.0 (superseded)
├── schema/amp.json                         # JSON Schema for all AMP verbs (MCP channel)
├── schema/amp-openapi.yaml                 # OpenAPI 3.0 contract (REST channel)
├── compliance/test_amp_server.py           # 67-test compliance suite (pytest, raw MCP)
├── docs/MIGRATING-v1.0-to-v1.1.md          # v1.0→v1.1 migration walkthrough
├── examples/minimal_server.py              # Minimal Core-conformant MCP server (v1.0-style — see note below)
└── python/amp-server/                      # Full-conformant Python reference impl
    └── src/amp_server/server.py            # smriti-memcore wrapper
```

> **Note on `examples/minimal_server.py`:** the example still teaches v1.0-style flat `agent_id` patterns and is kept as a backward-compatibility demo. New implementations should follow `python/amp-server/` instead. A v1.1-native minimal example is tracked as a `good-first-issue`.

---

## Adoption checklist

If you're building an AMP-conformant backend:

1. Read [spec/amp-v1.1.md §2 (Data Model) and §3 (Protocol)](spec/amp-v1.1.md).
2. Pick a conformance level (Core or Full) — Core is fine to start; you can add Full verbs incrementally.
3. Implement `tools/list` returning the AMP verbs with the [annotation values from §3.4](spec/amp-v1.1.md#34-mcp-tool-annotations). The compliance suite enforces these.
4. Implement scope validation: every request MUST carry at least one *isolating* identity key; reject with `invalid_request` otherwise.
5. Implement the [§3.5 error mapping](spec/amp-v1.1.md#35-error-handling--protocol-mappings) — same `AmpErrorData` payload on both channels.
6. Run `pytest compliance/test_amp_server.py --server-cmd "..."`. All 67 tests should pass for Full conformance; Core backends will skip Full-only tests (when a verb responds `not_supported`).
7. Open an issue with the **New Implementation** template — we'll list your backend in this README.

---

## Status

**v1.1 — Stable.** Spec and reference implementation released 2026-05-31. Compliance suite at 86 tests, all passing against the reference server.

**v1.2-draft — In progress.** Spec extensions tracked in [spec/amp-v1.1.md](spec/amp-v1.1.md). **Landed so far:** Appendix C (REST routing + gRPC `MemoryService` Protobuf contract), `amp.provenance.*` / `amp.lineage.*` reserved metadata keys, `amp.update` (RFC 7396 merge-patch semantics), and `amp.batch_encode` (multi-row ingest under one shared scope, per-row partial-failure semantics). Compliance suite now at 111 tests, all passing. Metadata filtering in `RecallFilters` is the remaining piece in flight. v1.1-conformant backends remain conformant — v1.2-draft adds capability without breaking the v1.1 surface.

Recent merges:
- [#1](https://github.com/smriti-memcore/amp/pull/1) — v1.1 dual-delivery, scopes, error mapping
- [#4](https://github.com/smriti-memcore/amp/pull/4) — `amp.export` / `amp.import` for MXF portability
- [#3](https://github.com/smriti-memcore/amp/pull/3) — MCP tool annotations + v1.0→v1.1 visibility migration

v1.2 design topics (auth, change feeds, idempotency keys, capability discovery, conflict-resolution in collaborative scopes) are accepted as discussion issues; `amp.update` / `amp.batch_encode` / metadata filters are in active development as v1.2-draft. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

**Quick entry points:**

- **Build a conformant backend** — Redis, Mem0/Zep wrapper, Postgres+pgvector. Open a [New Implementation](https://github.com/smriti-memcore/amp/issues/new?template=new-implementation.md) issue.
- **Improve the compliance suite** — add edge-case tests, port to TypeScript / Go, or write a REST-channel conformance runner (currently MCP-only).
- **Rewrite `examples/minimal_server.py` to v1.1-native** — the current minimal example still teaches v1.0-style flat `agent_id` patterns. A clean Core-conformant rewrite (scope, error mapping, annotations) is tracked as a `good-first-issue`.
- **Propose a v1.2 feature** — open an issue describing the use case and the proposed API surface; spec-text PRs are gated on at least one prototype implementation.

## License

This specification and all code in this repository are released under the [MIT License](LICENSE).

---

*AMP is an independent open specification. It is not affiliated with Anthropic or the MCP project.*
