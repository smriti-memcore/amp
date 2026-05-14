# amp-server

AMP (Agent Memory Protocol) Full-conformant reference server, backed by [smriti-memcore](https://pypi.org/project/smriti-memcore/).

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

## What it provides

All six AMP verbs over MCP stdio transport:

| Tool | Description |
|------|-------------|
| `amp.encode` | Store a memory (salience-gated, with `force` override) |
| `amp.recall` | Hybrid FTS5+vector retrieval with multi-hop graph traversal |
| `amp.forget` | Permanently delete a memory |
| `amp.consolidate` | Run smriti-memcore consolidation pipeline |
| `amp.pin` | Mark a memory as permanent |
| `amp.stats` | Return memory count, episode buffer, retrieval stats |

## Multi-agent support

Each `agent_id` gets an isolated storage directory under the storage root:

```
~/.amp/
├── agent-A/    ← smriti-memcore instance for agent A
├── agent-B/    ← smriti-memcore instance for agent B
└── ...
```

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
