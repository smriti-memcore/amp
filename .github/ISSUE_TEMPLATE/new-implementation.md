---
name: New Conformant Implementation
about: I've built an AMP-conformant backend
title: "New implementation: <backend name>"
labels: new-implementation
assignees: ''
---

## Backend

**Name:** 
**Storage / retrieval:** <!-- e.g. Redis, Chroma, Pinecone, SQLite FTS5 -->
**Conformance level:** <!-- Core or Full -->
**Language:** 
**Repo / link:** 

## Compliance

<!-- Paste the output of: pytest compliance/test_amp_server.py --server-cmd "..." -->

```
<pytest output here>
```

## Notable design decisions

<!-- Anything interesting about how you implemented the AMP verbs, or tradeoffs you hit -->

## Wants from the spec

<!-- Any ambiguities you hit, or things the spec should clarify for your backend type -->
