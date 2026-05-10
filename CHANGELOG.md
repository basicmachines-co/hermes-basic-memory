# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.5] — 2026-05-10

### Added
- Bundled `SKILL.md` is now auto-registered via `ctx.register_skill("basic-memory", ...)` during plugin load. No more manual symlink to `~/.hermes/skills/`. The skill is opt-in (resolvable via `skill:view basic-memory:basic-memory`); always-on agent guidance still flows through `system_prompt_block()`.

### Changed
- README rewritten for community install. Lead command is now `hermes plugins install basicmachines-co/hermes-basic-memory`. Clone-and-symlink instructions moved to the Development section.
- Added GitHub Actions CI: unit tests on push and PR.
- Added this CHANGELOG.

## [0.1.4] — 2026-05-10

### Added
- `_uv_binary_path()` and `_install_bm_via_uv()`. When `bm` is missing from the host, the plugin runs `uv tool install basic-memory --quiet` once at first `initialize()`. The bm binary lands at `~/.local/bin/bm` — the same canonical path a manual `uv tool install basic-memory` produces, so subsequent manual installs are no-ops rather than creating a second install.
- 8 new unit tests covering `is_available()` with bm/uv combinations, the install subprocess (success / non-zero exit / OSError / no-uv), and `initialize()` install-or-not branching.

### Changed
- `is_available()` now returns `True` when **either** `bm` is on disk **or** `uv` is on disk (we can install the missing CLI ourselves).
- README's prerequisites section: dropped manual basic-memory install requirement; added the one-time ~10s cold-start note.

## [0.1.3] — 2026-05-10

### Fixed
- README's cloud-mode section described the wrong setup (`bm project add ... --cloud --local-path` + `bm cloud bisync`), which gives a local-mode project with file-level cloud sync rather than true cloud routing. Replaced with `bm project set-cloud <name> --workspace <name>`, which flips the project to `ProjectMode.CLOUD` so tool calls route over HTTPS to `<cloud_host>/proxy` directly. No local files involved.
- Documented OAuth / API-key auth options, and the `--workspace` requirement when the user belongs to multiple BM Cloud workspaces.

## [0.1.2] — 2026-05-10

### Changed
- `_default_project()`: `"hermes-memory"` (was `"hermes-{hostname}"`).
- `_default_project_path()`: `~/hermes-memory/` (was `~/.basic-memory/hermes/`). The previous path violated the principle that `~/.basic-memory/` is reserved for BM's app state, not project storage.

### Added
- `_bm_known_projects()` reads bm's `~/.basic-memory/config.json`. `BasicMemoryProvider._verify_project_registered()` uses it to refuse initialization when `mode: cloud` is set against a project that isn't registered with bm. Local mode still auto-creates as before.
- 13 new unit tests for the introspection + bail-out paths.

## [0.1.1] — 2026-05-10

### Added
- `tests/test_actor.py` — 15 tests covering `_BmMcpActor` lifecycle, call dispatch, timeout-with-cancellation, idempotent shutdown.
- `tests/test_capture.py` — 25 tests for `sync_turn` (first-write + append paths), `on_session_end` summary shape, and gating.
- `tests/test_prefetch.py` — 25 tests for `prefetch` / `queue_prefetch` / `_format_prefetch` including forward-compat with unknown response fields.
- `tests/test_integration.py` — 12 gated tests exercising every tool against a real `bm` MCP server (`BM_INTEGRATION=1` + `bm` + `mcp`). Each session uses a throwaway BM project that's torn down on completion.

### Changed
- `_BmMcpActor.call` now refuses calls after `shutdown()` (sets `_running=False`) and cancels the underlying coroutine on timeout instead of leaking it.
- `_format_prefetch` defensively coerces non-string fields and skips non-dict entries.
- Added module-level `__version__`, kept in sync with `plugin.yaml` (verified by a test).

## [0.1.0] — 2026-05-10

### Added
- Initial release of the Hermes Memory Provider plugin for Basic Memory.
- Seven `bm_*` agent tools: `bm_search`, `bm_read`, `bm_write`, `bm_edit`, `bm_context`, `bm_delete`, `bm_move`.
- Per-turn capture (`sync_turn`) and end-of-session summary (`on_session_end`).
- Local mode (default) with auto-created BM project; cloud mode with project-name-based routing.
- Single-file plugin at `__init__.py`, AGPL-3.0-or-later.
- 84-test pytest suite.

[0.1.5]: https://github.com/basicmachines-co/hermes-basic-memory/releases/tag/v0.1.5
[0.1.4]: https://github.com/basicmachines-co/hermes-basic-memory/releases/tag/v0.1.4
[0.1.3]: https://github.com/basicmachines-co/hermes-basic-memory/releases/tag/v0.1.3
[0.1.2]: https://github.com/basicmachines-co/hermes-basic-memory/releases/tag/v0.1.2
[0.1.1]: https://github.com/basicmachines-co/hermes-basic-memory/releases/tag/v0.1.1
[0.1.0]: https://github.com/basicmachines-co/hermes-basic-memory/releases/tag/v0.1.0
