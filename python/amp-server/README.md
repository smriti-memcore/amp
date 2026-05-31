# amp-server

AMP (Agent Memory Protocol) reference server, backed by [smriti-memcore](https://pypi.org/project/smriti-memcore/).

**Transport:** MCP stdio (REST channel TBD — see *Limitations* below).
**Conformance:** Six of eight v1.1 verbs implemented today; `amp.export` / `amp.import` defined in the spec/schema but not yet ported into this server. See [#3](https://github.com/smriti-memcore/amp/issues/new?labels=good-first-issue) for the open task.

## Install

```bash
pip install "git+https://github.com/smriti-memcore/amp.git#subdirectory=python/amp-server"
```

## Run

```bash
amp-server                              # storage: ~/.amp
amp-server --storage-path /my/path     # custom storage root
AMP_STORAGE_PATH=/my/path amp-server   # via environment variable
```

## Verbs supported

| Verb | Description | Conformance |
|------|-------------|-------------|
| `amp.encode` | Store a memory (salience-gated, with `force` override) | Core |
| `amp.recall` | Hybrid FTS5 + vector retrieval with multi-hop graph traversal | Core |
| `amp.forget` | Permanently delete a memory | Core |
| `amp.stats` | Memory count, episode buffer, retrieval stats | Core |
| `amp.consolidate` | Run smriti-memcore consolidation pipeline | Full |
| `amp.pin` | Mark a memory as permanent | Full |
| `amp.export` | Bulk export to MXF NDJSON | *Not yet implemented (planned)* |
| `amp.import` | Bulk import from MXF NDJSON | *Not yet implemented (planned)* |

The server advertises `amp_conformance: "full"` in its `initialize` response. The two unimplemented verbs will respond `not_supported` (HTTP 501 / JSON-RPC -32002) until the underlying smriti-memcore export pipeline is wired in.

## v1.1 scopes

Every verb accepts a structured `scope` block (preferred) **or** a legacy flat `agent_id` (v1.0-compat). At least one *isolating* identity key — `agent_id`, `group_id`, `workspace_id`, or `user_id` — must be present; missing or refining-key-only scopes are rejected with `invalid_request` (JSON-RPC -32602).

```jsonc
// v1.1-native: structured scope
{
  "scope": {"agent_id": "research-bot", "user_id": "user_42"},
  "content": "User prefers concise summaries.",
  "force": true
}

// v1.0-compat: flat agent_id (auto-promoted to scope.agent_id)
{
  "agent_id": "research-bot",
  "content": "User prefers concise summaries.",
  "force": true
}
```

The two forms produce identical storage when both name the same `agent_id` only. Mixing them in a single call (e.g. `scope.agent_id="A"` + top-level `agent_id="B"`) returns `invalid_request`.

## Multi-agent storage layout

Each unique scope gets an isolated smriti-memcore instance:

```
~/.amp/
├── agent-A/                           ← legacy {agent_id: "agent-A"} scope
├── agent-B/                           ← legacy {agent_id: "agent-B"} scope
├── scope-3f9c1a2b8d7e4f5a/            ← {agent_id:..., user_id:...} scope (sha256-prefixed)
├── scope-7a1b3c5d9e2f4a8b/            ← {workspace_id:..., user_id:...} scope
└── ...
```

Single-key `{agent_id: "..."}` scopes (the v1.0 shape) keep their existing directory name — your v1.0 data lives at the same path post-upgrade. Multi-key scopes get a `scope-<sha256-prefix>` directory, deterministic per scope shape, so two callers with identical scopes always land in the same store and two callers with differing scopes never collide.

## Error mapping (spec §3.5)

Every error returns a JSON-RPC frame with structured `AmpErrorData` in the `data` field:

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "error": {
    "code": -32602,
    "message": "content must be a non-empty string",
    "data": {"amp_error_code": "invalid_request", "message": "content must be a non-empty string"}
  }
}
```

Mapping:

| amp_error_code | JSON-RPC | HTTP |
|---|---|---|
| `invalid_request` | -32602 | 400 |
| `not_found` | -32001 | 404 |
| `not_supported` | -32002 | 501 |
| `backend_error` | -32000 | 500 |

## Tool annotations

`tools/list` publishes [MCP 2025-03-26 ToolAnnotations](https://spec.modelcontextprotocol.io) on every verb so hosts can apply consistent gating:

| Verb | readOnly | destructive | idempotent | openWorld |
|---|---|---|---|---|
| `amp.encode` | false | false | false | false |
| `amp.recall` | true | false | true | false |
| `amp.forget` | false | **true** | true | false |
| `amp.consolidate` | false | false | false | **true** |
| `amp.pin` | false | false | true | false |
| `amp.stats` | true | false | true | false |

## Connect to Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "amp": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/smriti-memcore/amp.git#subdirectory=python/amp-server",
        "amp-server",
        "--storage-path", "/Users/you/.amp"
      ]
    }
  }
}
```

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/). `uvx` handles the Python environment automatically — Claude Desktop does not inherit your shell's `PATH`, so using `uvx` avoids needing to find the absolute path to the installed binary.

## Connect to Claude Code

```bash
claude mcp add amp -- uvx --from "git+https://github.com/smriti-memcore/amp.git#subdirectory=python/amp-server" amp-server --storage-path ~/.amp
```

## Limitations

- **MCP stdio only.** The REST channel defined in `schema/amp-openapi.yaml` is not yet implemented by this server. A small FastAPI front-end that calls the same scope/encode/recall plumbing is straightforward to add — open an issue if you'd like to take it on.
- **`amp.export` / `amp.import` not implemented.** Defined in the v1.1 spec/schema; planned for a follow-up release.
- **Single-process storage.** Each scope owns its own smriti-memcore instance loaded into memory. Suitable for single-tenant deployments and development; horizontal scaling is a v1.2 concern.

## Running the compliance suite against this server

```bash
pip install pytest
pytest compliance/test_amp_server.py --server-cmd "$(which amp-server)"
```

67 tests; all pass on the current implementation. Tests that exercise `amp.export` / `amp.import` are gated on tool presence and skip silently when the verbs aren't advertised.
