# Migrating from AMP v1.0 to v1.1

A practical recipe for backend implementors and application authors. The spec normative text lives in [spec/amp-v1.1.md §5](../spec/amp-v1.1.md#5-backward-compatibility--deprecations); this document is the walkthrough.

---

## TL;DR

- v1.0 modelled access control as a flat `agent_id` + per-memory `private` boolean. v1.1 replaces it with structural multi-dimensional `scope` partitioning.
- v1.0 callers keep working unchanged for one minor revision. `private` and `visibility` are deprecated, scheduled for removal in v1.2.
- The trickiest decision: **what does `private: true` mean in v1.1 scope-space?** There are two conformant answers (Strategy A and Strategy B). You MUST pick one, and document the choice.
- For most v1.0 deployments (single-agent assistants, agent-per-user setups), **Strategy A is correct and lossless** — no per-memory rewrites needed.
- Wire-level: nothing changes for v1.0 clients on day one. The migration is observable only when those clients are upgraded.

---

## Step 1: Identify your deployment shape

The first question is what `private: true` *actually meant* in your v1.0 system. v1.0 never made this explicit; you have to look at how you used it. Pick the description that matches:

### Shape 1: Agent-as-tenant (most common)

Every distinct end-user / customer / agent identity got its own `agent_id`. `private: true` was effectively a no-op because there was no one else inside that `agent_id` partition who could read the memory back.

**Examples:**
- A personal note-taking assistant where each user gets `agent_id: user_<id>`.
- A multi-tenant SaaS where `agent_id` is the tenant key.
- A single research bot for one user with `agent_id: "research"`.

→ **Use Strategy A (Agent-private).** Lossless. No per-row migration. Continue below.

### Shape 2: User-private inside a shared agent (less common, more complex)

One `agent_id` namespace served many end users, and `private: true` meant "this fact belongs to this specific end user, not to the agent in general."

**Examples:**
- A customer-support bot with `agent_id: "support"` serving many users; `private: true` flagged user-specific facts.
- A shared workspace assistant where `private: true` meant "personal note inside the team space."

→ **Use Strategy B (User-private).** Requires resolving `user_id` for legacy callers. Continue below.

### Shape 3: You're not sure

Strategy A is the safe default — it never silently elevates ownership. If you pick Strategy A and a downstream consumer later finds a row that should have been user-isolated, they can re-encode it under the more specific scope. The reverse (picking Strategy B and finding rows that got incorrectly tagged with someone else's `user_id`) is much harder to recover from.

→ **Use Strategy A.** Re-evaluate later if you have evidence Shape 2 applies.

---

## Step 2: Implement the chosen strategy in your backend

Both strategies handle the same legacy call shape:

```jsonc
// v1.0 amp.encode call
{
  "agent_id": "research",
  "content": "Likes espresso",
  "private": true     // legacy semantic — what does this mean now?
}
```

### Strategy A — Agent-private (default, lossless)

`private: true` and `private: false` both map to the same `{agent_id}`-only scope. The `private` flag is treated as a hint and echoed back on the response for wire-compat.

```python
def resolve_legacy_encode(req):
    scope = req.get("scope")
    if scope is None and "agent_id" in req:
        # Strategy A: ignore `private`; partition by agent_id alone.
        scope = {"agent_id": req["agent_id"]}
    # ... validate scope, write memory ...

def build_response(req, memory_id):
    resp = {"id": memory_id, "status": "stored"}
    if "private" in req:
        # v1.0-native caller — echo deprecated visibility for wire-compat
        resp["visibility"] = "private" if req["private"] else "shared"
    return resp
```

Recall behaviour:

```python
def resolve_legacy_recall_filters(filters, scope):
    # Strategy A: legacy visibility filter is a no-op.
    # Every memory in this scope is "private" in the v1.0 sense.
    if filters and "visibility" in filters:
        filters = {k: v for k, v in filters.items() if k != "visibility"}
    return filters
```

That's it. **No row rewrites, no scope changes, no data migration.** Existing rows land at `~/.amp/<agent_id>/` (or your equivalent) and stay there.

### Strategy B — User-private (opt-in)

`private: true` routes to `{agent_id, user_id}`; `private: false` stays at `{agent_id}`. Requires the caller to supply `user_id` explicitly OR a session-level binding established at handshake.

```python
def resolve_legacy_encode(req, session_user_id=None):
    scope = req.get("scope")
    if scope is None and "agent_id" in req:
        agent_id = req["agent_id"]
        if req.get("private") is True:
            # Strategy B: private rows go under user-specific scope
            user_id = req.get("user_id") or session_user_id
            if not user_id:
                # CANNOT silently invent — fall back to Strategy A semantics
                logger.warning("Strategy B: cannot resolve user_id, falling back to agent-private")
                scope = {"agent_id": agent_id}
            else:
                scope = {"agent_id": agent_id, "user_id": user_id}
        else:
            # Public rows go to the shared agent partition
            scope = {"agent_id": agent_id}
    # ... validate scope, write memory ...
```

Recall behaviour:

```python
def resolve_legacy_recall_filters(filters, scope, session_user_id=None):
    # Strategy B: legacy visibility filter applies a user_id constraint.
    if filters and filters.get("visibility") == "private":
        user_id = filters.get("user_id") or session_user_id
        if user_id:
            scope = {**scope, "user_id": user_id}
        # else: degrade to Strategy A semantics for this call
    return scope, {k: v for k, v in (filters or {}).items() if k != "visibility"}
```

**Critical:** Strategy B MUST NOT invent `user_id` from JWT subject, IP address, or any other implicit signal. The v1.0 client did not consent to those becoming partition keys; doing so silently changes ownership semantics. If `user_id` can't be resolved deterministically (caller didn't supply it, no session binding), fall back to Strategy A for that call and log a one-shot warning.

---

## Step 3: Publish the strategy choice

Document the choice in your server's `initialize` response (or your README, if you don't control the manifest):

```jsonc
{
  "jsonrpc": "2.0",
  "result": {
    "serverInfo": {
      "name": "your-backend",
      "amp_conformance": "full",
      "amp_version": "1.1",
      "amp_legacy_private_strategy": "agent_private"   // or "user_private"
    }
  }
}
```

The field name `amp_legacy_private_strategy` is a recommendation — not yet normative — pending a v1.2 capability-discovery channel. Until then, putting it in the server-info block is the lowest-friction way to make the choice introspectable.

---

## Step 4: Echo discipline for responses

A v1.1 server emits two response shapes depending on the call:

| Call shape | Response shape |
|---|---|
| v1.1-native (carries `scope`, no `private`) | No `visibility` field |
| v1.0-native (carries `agent_id` + `private`) | `visibility: "private"` or `"shared"` mirroring the input |

This is observable by the caller — your server's behaviour is "mirror the dialect the caller spoke." The reference implementation in `python/amp-server/` does this correctly; if you're writing your own, the simplest pattern is:

```python
def add_legacy_echo(resp, req):
    if "private" in req:
        resp["visibility"] = "private" if req["private"] else "shared"
    return resp
```

Same rule applies to `MemoryResult` rows in `recall` responses: include `visibility` only when the caller's recall request referenced `visibility` (in `RecallFilters` or via a `X-AMP-Compat: v1.0` header convention).

---

## Step 5: Upgrade client code (when ready)

Your v1.0 clients keep working without changes. When you upgrade them:

**Before (v1.0):**
```python
client.encode(agent_id="research", content="...", private=True)
```

**After — Strategy A (most common):**
```python
client.encode(scope={"agent_id": "research"}, content="...")
# `private` is gone. Was a no-op under Strategy A anyway.
```

**After — Strategy B with explicit user routing:**
```python
client.encode(scope={"agent_id": "support", "user_id": "user_42"}, content="...")
# What was `private: true` is now structural: a different scope.
```

For shared workspaces with per-user-private notes (v1.1-native pattern), see [spec/amp-v1.1.md Appendix B.1](../spec/amp-v1.1.md#b1-how-do-we-handle-private-memories-within-collaborative-scopes-in-v11) — the answer is intersection scoping, not the deprecated `private` flag.

---

## Step 6: Watch for the v1.2 deprecation deadline

The deprecated fields are scheduled for **removal in v1.2**. Specifically:

| Field | v1.1 behaviour | v1.2 behaviour |
|---|---|---|
| `private` (input to `amp.encode`) | Accepted, mapped per strategy | MUST be rejected with `invalid_request` |
| `visibility` (output on responses) | Echoed only to v1.0-native callers | MUST NOT be emitted |
| `visibility` (input to `RecallFilters`) | Accepted, no-op or `user_id` constraint | MUST be rejected with `invalid_request` |
| Flat `agent_id` at request root | Accepted, promoted to `scope.agent_id` | Still accepted (convenience alias, not a separate semantic) |

Backends SHOULD log a structured deprecation warning the first time a deprecated field is accepted from a given caller identity, so operators can track v1.0 client migration progress. Something like:

```
[deprecation] caller=cli-7f3a1 sent legacy 'private' flag; will be rejected in AMP v1.2
```

One warning per `(caller_identity, deprecated_field)` pair — don't spam.

---

## FAQ

**Q: Do I need to rewrite existing rows on disk?**
No. Strategy A leaves them exactly where they were. Strategy B leaves them at the agent-only scope (since they were written without a user_id), and new private writes go under the more specific scope.

**Q: What if a row was written `private: true` under v1.0 and the same caller now reads it back without filters?**
Under both strategies, the recall returns it — it's still in the same partition. Under Strategy B, recalls scoped to `{agent_id, user_id}` will *not* find these old rows (they don't carry `user_id`). The recommended fix is to re-encode them with the new scope at a convenient time, or leave them as "legacy public" rows and accept the loss of fidelity.

**Q: Can I switch strategies later?**
Strategy A → Strategy B is doable but requires reasoning about what each existing row's `user_id` *should* have been. Don't.

Strategy B → Strategy A means dropping the `user_id` keys, which silently broadens visibility. Don't do this either.

**Pick once.** If you're not sure, Strategy A.

**Q: What about `amp.export` / `amp.import` round-trips across the v1.0/v1.1 boundary?**
The MXF row schema is v1.1-only. A row exported from a v1.1 backend carries a `scope` block. A v1.0 backend can't import that directly; you'd need a one-shot adapter that maps `scope.agent_id` back to a flat field and drops the other keys.

**Q: Where does the spec normative text live?**
[spec/amp-v1.1.md §5](../spec/amp-v1.1.md#5-backward-compatibility--deprecations). This document is illustrative; if it disagrees with the spec, the spec wins.

---

## See also

- [spec/amp-v1.1.md §2.3](../spec/amp-v1.1.md#23-multi-dimensional-scoping--collaborative-workspaces) — Scope object reference
- [spec/amp-v1.1.md §3.4](../spec/amp-v1.1.md#34-mcp-tool-annotations) — MCP tool annotations
- [spec/amp-v1.1.md §3.5](../spec/amp-v1.1.md#35-error-handling--protocol-mappings) — Error mapping
- [spec/amp-v1.1.md Appendix B.1](../spec/amp-v1.1.md#b1-how-do-we-handle-private-memories-within-collaborative-scopes-in-v11) — Private memories inside collaborative scopes (v1.1-native)
- [python/amp-server/](../python/amp-server/) — Reference implementation (Strategy A by default)
