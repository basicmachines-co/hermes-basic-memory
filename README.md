# hermes-basic-memory

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

Hermes Memory Provider plugin that wraps the [basic-memory](https://github.com/basicmachines-co/basic-memory) MCP server. Analog of [openclaw-basic-memory](https://github.com/basicmachines-co/openclaw-basic-memory) for Hermes Agent.

## What it does

Replaces Hermes's "no external provider" memory with a Basic Memory knowledge graph:

- 7 agent tools: `bm_search`, `bm_read`, `bm_write`, `bm_edit`, `bm_context`, `bm_delete`, `bm_move`
- Per-turn capture: appends each user/assistant turn to a running session note
- Session-end summary: writes a separate summary note with relations back to the transcript
- Pre-turn recall: `prefetch(query)` runs a hybrid search and injects results as `<memory-context>`
- Local mode (default) writes to `~/.basic-memory/hermes/`; cloud mode writes to a Basic Memory Cloud project

## Install

Symlink into the Hermes plugins directory (creates the dir if missing):

```bash
mkdir -p ~/.hermes/plugins ~/.hermes/skills
ln -snf ~/code/hermes-basic-memory ~/.hermes/plugins/basic-memory
ln -snf ~/code/hermes-basic-memory/skill ~/.hermes/skills/basic-memory
```

Install the MCP client SDK into the Hermes venv:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python mcp
```

Activate it in `~/.hermes/config.yaml`:

```yaml
memory:
  provider: basic-memory
```

Verify:

```bash
hermes memory status
```

## Configuration

Defaults are reasonable for local use. To tune, write `~/.hermes/basic-memory.json`:

```json
{
  "mode": "local",
  "project": "hermes-jodys-imac",
  "project_path": "~/.basic-memory/hermes/",
  "capture_per_turn": true,
  "capture_session_end": true,
  "capture_folder": "hermes-sessions"
}
```

Or run `hermes memory setup basic-memory` for the wizard.

### Cloud mode

Set `mode: cloud`. Requires `bm cloud login` to have been run on the host. The plugin shells out to `bm mcp` regardless of mode — the BM CLI handles cloud routing.

## Foot-guns

- **`<memory-context>` tags in BM notes**: Hermes's streaming output scrubber strips literal `<memory-context>...</memory-context>` blocks from assistant text. If a note contains those tags and the assistant echoes the note body verbatim, the echoed copy gets eaten mid-stream. Tool results inbound are unaffected. Avoid putting those tags in BM notes; if you must, fence them in code blocks.
- **Single external provider**: Hermes accepts only one external memory provider at a time. Activating basic-memory displaces any other.
- **CLI cold start**: `hermes ask ...` invocations spawn `bm mcp` on every run (~2-5s). Long-running gateway sessions amortize this.

## Development

The plugin is a single-file Python module at `__init__.py`. The Hermes plugin loader expects `register(ctx)` and grep-detects either `register_memory_provider` or `MemoryProvider` in the file.

The repo dir is the deployable package — symlink it directly into `~/.hermes/plugins/basic-memory`.

### Running tests

```bash
# Unit tests (fast, hermetic — no Hermes or bm required)
uv run --with pytest pytest

# Integration tests (gated — exercise every tool against a real bm MCP server)
BM_INTEGRATION=1 uv run --with pytest --with mcp pytest tests/test_integration.py
```

The unit suite stubs out Hermes-internal imports (`agent.memory_provider`, `tools.registry`) so it runs without a Hermes install. `mcp` is optional at unit-test time — its absence just makes `is_available()` return `False`, which the tests verify.

Integration tests require:
- `BM_INTEGRATION=1` (env var gate)
- `bm` CLI on PATH
- `mcp` Python package importable (`uv run --with mcp ...`)

Each integration session creates a unique throwaway BM project (under `tempfile.mkdtemp`) and removes it on teardown, so they never touch your real BM projects.

## License

AGPL-3.0-or-later, matching [basic-memory](https://github.com/basicmachines-co/basic-memory). See [LICENSE](LICENSE).
