---
name: basic-memory
description: Use the Basic Memory knowledge graph for persistent memory across sessions. Search before answering; capture decisions, meetings, and insights as notes.
category: memory
---

# Basic Memory Knowledge Graph

You have access to a persistent knowledge graph backed by Basic Memory. The graph survives across sessions and is shared with other tools (Claude Desktop, Obsidian, the `bm` CLI). Use the `bm_*` tools below to recall and capture information.

## Use `bm_*`, not the `bm` CLI

**Always invoke the `bm_*` tools directly. Do not shell out to the `bm` CLI for note operations.**

The `bm_*` tools route through a persistent MCP connection — roughly 0.1 seconds per call. Running `bm` from the shell spawns a fresh Python process per call (1-2 seconds of cold-start every time) and bypasses Hermes's automatic per-turn capture, so the session-transcript and summary notes won't reflect what you did.

The CLI is fine when you genuinely need a feature these wrappers don't expose (rare). Otherwise, prefer:

| Use case | Tool (not CLI) |
|---|---|
| Search the graph | `bm_search` |
| Read a note | `bm_read` |
| Create / update a note | `bm_write` / `bm_edit` |
| Navigate relations | `bm_context` |
| Move / delete | `bm_move` / `bm_delete` |

## Tool reference

### `bm_search` — search the graph
Use **before** answering questions about prior decisions, projects, meetings, or anything that might already be documented.

```
bm_search({ query: "auth strategy decision", limit: 5 })
```

### `bm_read` — fetch a note's full content
After search shows a relevant note, read it for context.

```
bm_read({ identifier: "decisions/auth-strategy" })
bm_read({ identifier: "memory://projects/api-redesign" })
```

### `bm_context` — navigate via memory:// URLs
Returns the target note plus related notes via traversed relations.

```
bm_context({ url: "memory://projects/api-redesign", depth: 1 })
```

### `bm_write` — capture new knowledge
When the user shares a decision, meeting outcome, or insight worth keeping, capture it. Use clear titles and a folder.

```
bm_write({
  title: "API Authentication Decision",
  folder: "decisions",
  content: "# API Authentication\n\n## Context\n...\n\n## Decision\n..."
})
```

Recommended folders: `projects/`, `decisions/`, `meetings/`, `concepts/`, `weekly/`.

### `bm_edit` — incremental updates
Operations: `append`, `prepend`, `find_replace` (requires `find_text`), `replace_section` (requires `section`).

```
bm_edit({
  identifier: "projects/api-redesign",
  operation: "append",
  content: "\n## Update 2026-05-09\nDeployed to staging."
})
```

### `bm_delete` / `bm_move` — maintenance
Use sparingly. `bm_move` takes `new_folder`.

## When to use each tool

| Situation | Tool |
|---|---|
| User asks about a topic that might already be documented | `bm_search` first, then `bm_read` |
| User exposes a decision, plan, or meeting outcome | offer to `bm_write` |
| Updating prior work | `bm_edit` (append for time-ordered logs, replace_section for living docs) |
| Exploring related concepts | `bm_context` |

## Note structure

Use consistent markdown:

```markdown
# Clear Title

## Context
Background and current situation.

## Key Points
- Main insights
- Important details

## Observations
- [decision] We chose PostgreSQL for ACID guarantees
- [insight] Users prefer social login
- [risk] Deployment lacks rollback path

## Relations
- relates_to [[Other Note Title]]
- depends_on [[Database Choice]]

## Next Steps
- [ ] Implement
- [ ] Document
```

## Memory URLs

`memory://projects/api-redesign` — direct reference. Used in `bm_context`, `bm_read`. The `memory://` prefix is optional for `bm_read`.

## Behavior guidelines

1. **Search before answering.** If the user asks "what did we decide about X?", run `bm_search` first.
2. **Offer to capture.** When the user shares decisions or meeting outcomes, ask: "Should I save this as a note?"
3. **Suggest connections.** When a search returns related notes, surface them so the user knows what already exists.
4. **Don't over-capture.** Auto-capture is already running per turn. Don't create a `bm_write` for every response — only for substantive content the user wants preserved.
5. **Sensitive info.** Don't capture credentials or personal data without confirmation.

## Footgun

If a note's body contains literal `<memory-context>...</memory-context>` tags, Hermes's streaming output scrubber will eat those tags (and the text between paired ones) when you echo the note verbatim back to the user. Tool *inputs* are unaffected. If you must include such content, fence it in a code block.
