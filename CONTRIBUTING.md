# Contributing to AMP

AMP is an open specification — contributions to the spec, compliance suite, documentation, and conformant implementations are all welcome.

## Ways to Contribute

### Discuss an Open Question

Section 9 of the spec lists open design questions that will be resolved through community input. Each question has a corresponding GitHub issue labeled [`open-question`](https://github.com/smriti-memcore/amp/labels/open-question). Add your perspective there.

### Report a Spec Wording Issue

If any part of the spec is ambiguous, contradictory, or unclear, open an issue using the **Bug / Spec Wording** template. Clear language matters as much as correct logic.

### Build a Conformant Backend

The highest-value contribution is a new AMP-conformant memory backend. If you build one:

1. Run the compliance suite against it (see below)
2. Open an issue using the **New Implementation** template
3. We'll link it from the README under "Conformant Backends"

#### Running the compliance suite

```bash
pip install pytest
pytest compliance/test_amp_server.py --server-cmd "python3 your_server.py"
```

The suite runs the full MCP handshake and tests all Core verbs. Pass `--full` (not yet implemented — see open items) to test Full-conformance verbs too.

### Improve the Compliance Suite

- Add test cases for edge cases not yet covered
- Add a `--full` flag to gate Full-conformance tests separately from Core
- Port the suite to TypeScript or Go for non-Python server authors

### Propose a Spec Change

1. Open a discussion issue first — describe the problem and proposed change
2. Wait for maintainer feedback before writing a PR
3. PRs that change normative spec language (`MUST`, `SHOULD`, `MAY`) require at least one implementation to demonstrate feasibility

## What's Available to Pick Up

| Item | Difficulty | Label |
|------|-----------|-------|
| Redis Core-conformant backend | Easy | `good-first-issue` |
| Mem0 / Zep wrapper | Medium | `new-implementation` |
| TypeScript compliance runner | Medium | `tooling` |
| Go compliance runner | Medium | `tooling` |
| Discuss open questions §9 | Any | `open-question` |
| `amp-server` PyPI publish | Easy | `tooling` |
| `amp.update` verb proposal | Hard | `open-question` |

## Repository Structure

```
amp/
├── spec/amp-v1.0.md        # The specification (normative)
├── schema/amp.json         # JSON Schema for all AMP tools
├── compliance/             # Compliance test suite (pytest)
│   └── test_amp_server.py
├── examples/               # Example implementations
│   └── minimal_server.py   # Minimal Core-conformant server (stdlib only)
└── python/                 # Python reference implementation
    └── amp-server/         # smriti-memcore AMP wrapper (Full-conformant)
```

## Code Style

- Python: standard library where possible; `mcp` SDK for production servers
- Tests: pytest; no mocking of the MCP wire protocol — tests speak raw JSON-RPC over stdio
- Spec: Markdown; use `MUST` / `SHOULD` / `MAY` per RFC 2119

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
