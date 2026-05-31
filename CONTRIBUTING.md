# Contributing to AMP

AMP is an open specification — contributions to the spec, compliance suite, documentation, and conformant implementations are all welcome.

## Ways to Contribute

### Build a Conformant Backend

The highest-value contribution is a new AMP-conformant memory backend. If you build one:

1. Read [spec/amp-v1.1.md](spec/amp-v1.1.md) §2 (data model) and §3 (protocol).
2. Pick a conformance level — **Core** (4 verbs) is fine to start; **Full** (8 verbs) is required for MXF interop and consolidation.
3. Run the compliance suite against it (see below).
4. Open an issue using the **New Implementation** template.
5. We'll link it from the README under "Conformant Backends."

#### Running the compliance suite

```bash
pip install pytest
pytest compliance/test_amp_server.py --server-cmd "python3 your_server.py"
```

The suite runs the full MCP handshake and exercises 67 tests including scope validation, error mapping (`§3.5`), MCP tool annotations (`§3.4`), content fidelity, namespace isolation, deprecated-field round-trip, and `amp.export`/`amp.import` semantics (when advertised). Tests for Full-only verbs auto-skip on Core backends that respond `not_supported`.

### Improve the Compliance Suite

- Add edge-case tests not yet covered (off-by-one on `top_k`, unicode in `content`, very large `metadata`, conflicting deprecated + native fields…).
- Write a **REST-channel conformance runner**. The suite today is MCP-only; the REST channel defined in `schema/amp-openapi.yaml` has no automated conformance check. A schemathesis or Dredd run against a reference server would close this gap.
- Port the suite to **TypeScript** or **Go** so non-Python server authors can self-test.

### Report a Spec Wording Issue

If any part of the spec is ambiguous, contradictory, or unclear, open an issue using the **Bug / Spec Wording** template. Clear language matters as much as correct logic — the v1.1 revision rewrote §5 (visibility migration) specifically because the v1.0 wording was a one-sentence handwave that produced different end-states across backends.

### Propose a v1.2 Feature

v1.1 is stable; v1.2 is open for design. The known gaps (none blocking adoption, all worth solving) are:

| Topic | Status | One-line scope |
|---|---|---|
| **Authentication & authorization** | Open (deferred from PR #7) | API keys / OAuth / JWT story for the REST channel; per-scope ACLs. Until this lands, the `unauthorized` / `forbidden` error codes proposed in PR #7 are intentionally **not** in the spec — codes without a model behind them would let every backend invent its own. |
| **`amp.update`** | v1.2-draft, **landed (reference impl)** | Amend a memory's content or metadata without losing its `id` or graph edges. Schema + spec §3.2.4 + reference-server implementation + 12 compliance tests. RFC 7396 JSON Merge Patch semantics for metadata by default; opt-in `metadata_mode=replace` for wholesale replacement. |
| **`amp.batch_encode`** | v1.2-draft, in active development | Multi-row encode in a single round-trip. Reference-server implementation + compliance tests required before merge. |
| **Metadata filters in `RecallFilters`** | v1.2-draft, in active development | `MetadataFilter` predicate (`eq` / `ne` / `gt` / `gte` / `lt` / `lte` / `in` / `contains`) over reserved-vocabulary and user-defined keys. Reference-server implementation + compliance tests required before merge. |
| **Idempotency keys** | Open | `Idempotency-Key` header / field on `amp.encode` so retries don't duplicate. |
| **Change feeds / subscriptions** | Open | SSE or webhook channel so agents in shared scopes can react to writes by other agents. |
| **Capability discovery** | Open | `GET /v1/capabilities` exposing per-verb support (finer-grained than `amp_conformance: core|full`). |
| **gRPC stubs / `.proto` shipping** | Partially landed (Appendix C contract); `schema/amp.proto` + reference server still open | Spec already pins the Protobuf v3 `MemoryService` contract; a shipped `.proto` and a reference gRPC server remain follow-up work. |
| **MCP tool annotations beyond the four hints** | Open | Once the MCP spec adds more, e.g. cost / latency hints. |
| **MXF embedding portability** | Open | Decide whether embeddings travel in MXF, and how to negotiate model compatibility. |
| **Collaborative scope conflict resolution** | Open | Two agents writing to the same `workspace_id` — LWW? CRDT? Causality? |

Process:

1. Open a **discussion issue first** — describe the use case, the proposed API surface, and at least one alternative you considered.
2. Wait for maintainer feedback before writing a spec PR.
3. PRs that change normative spec language (`MUST`, `SHOULD`, `MAY`) require **at least one prototype implementation** in the same PR (or a follow-up linked PR) to demonstrate feasibility.

## What's Available to Pick Up

| Item | Difficulty | Label |
|------|-----------|-------|
| Redis Core-conformant backend | Easy | `good-first-issue` |
| Mem0 / Zep wrapper | Medium | `new-implementation` |
| Postgres + pgvector Core backend | Medium | `new-implementation` |
| Add `amp.export` / `amp.import` to `python/amp-server/` | Done — landed in [PR #6 follow-up](https://github.com/smriti-memcore/amp/pulls?q=is%3Apr+server-export-import) | — |
| Rewrite `examples/minimal_server.py` to v1.1 native | Easy | `good-first-issue` |
| FastAPI front-end exposing the REST channel from `python/amp-server/` | Medium | `new-implementation` |
| TypeScript compliance runner | Medium | `tooling` |
| Go compliance runner | Medium | `tooling` |
| REST-channel conformance suite (schemathesis / Dredd) | Medium | `tooling` |
| Any v1.2 topic from the table above | Hard | `v1.2-design` |

## Repository Structure

```
amp/
├── spec/amp-v1.1.md                # The specification (normative)
├── spec/amp-v1.0.md                # Historical — v1.0 (superseded)
├── schema/amp.json                 # JSON Schema for all AMP verbs (MCP channel)
├── schema/amp-openapi.yaml         # OpenAPI 3.0 contract (REST channel)
├── compliance/test_amp_server.py   # 67-test compliance suite (pytest, raw MCP)
├── docs/MIGRATING-v1.0-to-v1.1.md  # v1.0→v1.1 migration walkthrough
├── examples/minimal_server.py      # Minimal MCP server (v1.0-style; pending rewrite)
└── python/amp-server/              # Full-conformant Python reference impl
    └── src/amp_server/server.py    # smriti-memcore wrapper
```

## Code Style

- **Python:** standard library where possible; `mcp` SDK for production servers.
- **Tests:** pytest; no mocking of the MCP wire protocol — tests speak raw JSON-RPC over stdio.
- **Spec:** Markdown; use `MUST` / `SHOULD` / `MAY` per RFC 2119. Reserved metadata keys are namespaced under `amp.*`.
- **Error model:** every protocol error MUST carry an `amp_error_code` payload per §3.5. Don't invent ad-hoc error shapes.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
