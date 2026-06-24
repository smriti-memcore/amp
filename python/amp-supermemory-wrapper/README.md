# amp-supermemory-wrapper

AMP (Agent Memory Protocol) wrapper for [SuperMemory](https://supermemory.ai/).

**Transport:** MCP stdio.
**Conformance:** Core (v1.1) + `amp.update` and `amp.batch_encode` from v1.2-draft.

## Install

```bash
pip install "git+https://github.com/smriti-memcore/amp.git#subdirectory=python/amp-supermemory-wrapper"
```

Or for local development:
```bash
pip install -e python/amp-supermemory-wrapper
```

## Run

This wrapper requires a SuperMemory API Key:

```bash
export SUPERMEMORY_API_KEY="your-supermemory-api-key"
amp-supermemory-wrapper
```

## Connect to Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "amp-supermemory": {
      "command": "uv",
      "args": [
        "run",
        "--from", "git+https://github.com/smriti-memcore/amp.git#subdirectory=python/amp-supermemory-wrapper",
        "amp-supermemory-wrapper"
      ],
      "env": {
        "SUPERMEMORY_API_KEY": "your-supermemory-api-key"
      }
    }
  }
}
```
