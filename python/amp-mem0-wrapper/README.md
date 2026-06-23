# amp-mem0-wrapper

AMP (Agent Memory Protocol) wrapper for [Mem0](https://github.com/mem0ai/mem0).

**Transport:** MCP stdio.
**Conformance:** Core (v1.1) + `amp.update` and `amp.batch_encode` from v1.2-draft.

## Install

```bash
pip install "git+https://github.com/smriti-memcore/amp.git#subdirectory=python/amp-mem0-wrapper"
```

Or for local development:
```bash
pip install -e python/amp-mem0-wrapper
```

## Run

By default, the server runs in **Local Mode** using local SQLite + Chroma database.

```bash
amp-mem0-wrapper --storage-path ~/.amp/mem0
```

To run in **Platform Mode** (connecting to the hosted Mem0 platform):
```bash
export MEM0_API_KEY="your-mem0-api-key"
amp-mem0-wrapper
```

## Connect to Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "amp-mem0": {
      "command": "uv",
      "args": [
        "run",
        "--from", "git+https://github.com/smriti-memcore/amp.git#subdirectory=python/amp-mem0-wrapper",
        "amp-mem0-wrapper",
        "--storage-path", "/Users/you/.amp/mem0"
      ]
    }
  }
}
```
