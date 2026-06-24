We just released the v1.1 specification for the Agent Memory Protocol (AMP).

When we launched v1.0, the goal was simple: standardize how LLMs talk to memory backends using Model Context Protocol (MCP) tools. But putting memory tools directly in the LLM's loop created a few major issues:
- LLMs shouldn't be running background maintenance tasks like memory consolidation or fetching database stats. That wastes tokens and adds latency.
- Host applications (harnesses) often need to inject memories into the prompt *before* the agent runs, which is awkward if memory only exists as stdio tool calls.
- Storing metadata was a mess because every backend used different keys, breaking cross-engine compatibility.

AMP v1.1 addresses these bottlenecks by shifting from "just MCP tools" to a standalone, service-first architecture. 

Here is what changes:

1. Standalone Service boundary: The protocol now defines first-class HTTP REST and gRPC API contracts. The host application handles session management, background tasks, and context injection, while presenting a clean, tool-only MCP adapter to the agent.
2. Cognitive vs. Autonomic separation: LLMs only see cognitive tools (encode, recall, forget). Background maintenance verbs (consolidate, pin, stats) are handled out-of-band by the harness.
3. Multi-dimensional scoping: Scoping is no longer flat. You can partition memory by organization, application, workspace, user, and agent. This makes it trivial to support collaborative agents and shared team spaces.
4. Memory Exchange Format (MXF): An open, NDJSON-based format to easily export or import memory logs, making it simple to migrate between backends (Zep, Mem0, smriti-memcore, etc.) without vendor lock-in.
5. Standardized Metadata Vocabulary: Common namespaces for things like TTL, confidence scores, entities, and lineage links, so your query filters remain uniform.

### What is coming next in v1.2 (Draft in progress)
We are currently drafting the v1.2 specification to introduce in-place memory mutations (`amp.update`), bulk ingestion (`amp.batch_encode`), and structured metadata filtering. You can check the active design branches on GitHub if you'd like to participate in the spec.

You can read the full spec, migration guide, and inspect the updated JSON Schemas here:
https://github.com/smriti-memcore/amp

Let me know what you think. If you are building memory backends or developer frameworks, I'd love to hear how this fits into your stack.

#AIAgents #GenerativeAI #LLMs #ModelContextProtocol #SoftwareEngineering #OpenSource #gRPC #RESTAPI #AIInfrastructure #VectorDatabase
