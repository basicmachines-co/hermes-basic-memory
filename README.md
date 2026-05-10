# hermes-basic-memory

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

Hermes Memory Provider plugin that gives [Hermes Agent](https://github.com/NousResearch/hermes-agent) a persistent knowledge graph backed by [Basic Memory](https://github.com/basicmachines-co/basic-memory).

The plugin replaces Hermes's "no external memory provider" with a real graph: search-before-answer recall, per-turn capture, end-of-session summaries, and seven `bm_*` tools the agent can call directly. Local mode by default; one CLI flip switches to true cloud routing through Basic Memory Cloud.

## Install

```bash
hermes plugins install basicmachines-co/hermes-basic-memory
```

Then activate it in `~/.hermes/config.yaml`:

```yaml
memory:
  provider: basic-memory
```

If you run the gateway, restart it (`hermes gateway restart`). Done.

The plugin self-installs the `basic-memory` CLI on first init via `uv tool install basic-memory` (one-time ~10s pause if it isn't already present). The bm binary lands at `~/.local/bin/bm` — the same location a manual `uv tool install basic-memory` would produce, so a later manual install or upgrade is a no-op rather than a second install.

### Prerequisites

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [`uv`](https://docs.astral.sh/uv/) on PATH (used for the bootstrap install)
- The `mcp` Python package in the Hermes venv. If `hermes plugins install` doesn't auto-install it (it follows `pip_dependencies` in `plugin.yaml`), run:
  ```bash
  uv pip install --python ~/.hermes/hermes-agent/venv/bin/python mcp
  ```

### Verify

```bash
hermes memory status
```

Expected:
```
  Provider:  basic-memory
  Plugin:    installed ✓
  Status:    available ✓
```

## What the agent gets

Seven tools (curated subset of Basic Memory's MCP surface):

| Tool | Use |
|---|---|
| `bm_search` | Semantic + full-text search; **call this before answering** |
| `bm_read` | Fetch a note by title, permalink, or `memory://` URL |
| `bm_write` | Create a new note (capture decisions, meeting notes, insights) |
| `bm_edit` | Append, prepend, find/replace, replace-section |
| `bm_context` | Navigate via `memory://` URLs to find related notes |
| `bm_delete` | Delete a note |
| `bm_move` | Move a note to a different folder |

Plus automatic capture:
- **Per turn**: every user/assistant exchange appends to a running session-transcript note
- **End of session**: a separate summary note is written, linked back to the transcript via a `summary_of` relation

A bundled skill (`skill:view basic-memory:basic-memory`) gives the agent a longer reference doc on top of the always-on `system_prompt_block`.

## Configuration

Defaults are reasonable for local use:

| Key | Default | Notes |
|---|---|---|
| `mode` | `local` | `local` (in-process) or `cloud` (route through BM Cloud API) |
| `project` | `hermes-memory` | BM project name |
| `project_path` | `~/hermes-memory/` | Local mode only — where session notes land |
| `capture_folder` | `hermes-sessions` | Folder within the project for session notes |
| `capture_per_turn` | `true` | Append every turn to a session transcript |
| `capture_session_end` | `true` | Write a summary note when the session ends |

To override, write `~/.hermes/basic-memory.json` or run `hermes memory setup basic-memory`:

```json
{
  "mode": "local",
  "project": "hermes-memory",
  "project_path": "~/hermes-memory/",
  "capture_per_turn": true,
  "capture_session_end": true,
  "capture_folder": "hermes-sessions"
}
```

In local mode the plugin auto-creates the BM project on first init via `bm project add`. In cloud mode it doesn't — you create the cloud-routed project yourself (see below) and the plugin verifies it's registered before initializing.

### Cloud mode

When `mode: cloud`, tool calls route directly through the BM cloud API — no local file mirror, no bisync. You set this up once with the BM CLI:

```bash
# Authenticate (OAuth) or save an API key
bm cloud login                     # OAuth — interactive
# OR for headless/automation:
bm cloud create-key "hermes"
bm cloud set-key bmc_...

# Create the project, then flip it to cloud routing.
# --workspace is required if you belong to more than one workspace
# (otherwise BM auto-resolves the only one available).
bm project add hermes-memory-cloud
bm project set-cloud hermes-memory-cloud --workspace Personal

# Point the plugin at it
cat > ~/.hermes/basic-memory.json <<EOF
{
  "mode": "cloud",
  "project": "hermes-memory-cloud",
  "capture_per_turn": true,
  "capture_session_end": true,
  "capture_folder": "hermes-sessions"
}
EOF

hermes gateway restart
```

Tool calls now route from `bm mcp` → `<cloud_host>/proxy` over HTTPS using your OAuth token (or API key). Notes never touch local disk.

**Don't confuse cloud mode with `bm cloud bisync`.** Bisync is rclone-style two-way file sync between a *local* project and cloud storage, intended for keeping local working copies. For agent-driven capture you want true cloud routing (`set-cloud`), not bisync.

## Updating / removing

```bash
hermes plugins update basic-memory
hermes plugins remove basic-memory     # then revert memory.provider in config.yaml
```

## Foot-guns

- **`<memory-context>` tags in notes**: Hermes's streaming output scrubber strips literal `<memory-context>...</memory-context>` blocks from assistant text. If a note contains those tags and the assistant echoes the body verbatim, the echoed copy gets eaten mid-stream. Tool results inbound are unaffected. Avoid those tags in BM notes; if you must include them, fence in a code block.
- **Single external provider**: Hermes accepts only one external memory provider at a time. Activating basic-memory displaces any other.
- **CLI cold start**: `hermes -z ...` invocations spawn `bm mcp` per run (~2-5s). Long-running gateway sessions amortize this.
- **Multiple cloud workspaces**: if your BM Cloud account belongs to more than one workspace, `bm project set-cloud` must be invoked with `--workspace <name>`. Otherwise tool calls fail with "Multiple workspaces are available".

## Development

The plugin is a single-file Python module at `__init__.py`. The Hermes plugin loader expects `register(ctx)` and grep-detects either `register_memory_provider` or `MemoryProvider` in the file.

For local development (point Hermes at your working tree instead of going through `hermes plugins install`):

```bash
git clone https://github.com/basicmachines-co/hermes-basic-memory ~/code/hermes-basic-memory
mkdir -p ~/.hermes/plugins
ln -snf ~/code/hermes-basic-memory ~/.hermes/plugins/basic-memory
```

### Running tests

```bash
# Unit tests (fast, hermetic — no Hermes or bm required)
uv run --with pytest pytest

# Integration tests (gated — exercise every tool against a real bm MCP server)
BM_INTEGRATION=1 uv run --with pytest --with mcp pytest tests/test_integration.py
```

The unit suite stubs out Hermes-internal imports (`agent.memory_provider`, `tools.registry`) so it runs without a Hermes install. `mcp` is optional at unit-test time — its absence just makes `is_available()` return False, which the tests verify.

Integration tests require `BM_INTEGRATION=1`, `bm` CLI on PATH, and `mcp` Python package importable. Each session creates a unique throwaway BM project (under `tempfile.mkdtemp`) and removes it on teardown, so they never touch your real BM projects.

## License

AGPL-3.0-or-later, matching [basic-memory](https://github.com/basicmachines-co/basic-memory). See [LICENSE](LICENSE).
