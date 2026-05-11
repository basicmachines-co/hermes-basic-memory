"""
Tests for the plugin-owned /bm-* slash commands.

Covers:
- registration through ctx.register_command (with hasattr fallback)
- per-handler behavior: usage text, uninitialized provider, happy path,
  and exception → plain-text error.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from .conftest import FakeSession, make_scripted_actor


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_EXPECTED_COMMANDS = {
    "bm-search",
    "bm-read",
    "bm-context",
    "bm-recent",
    "bm-status",
    "bm-remember",
    "bm-project",
    "bm-workspace",
}


def test_register_wires_up_all_slash_commands(bm):
    ctx = MagicMock()
    bm._active_providers.clear()
    bm.register(ctx)
    names = {call.args[0] for call in ctx.register_command.call_args_list}
    assert names == _EXPECTED_COMMANDS
    bm._active_providers.clear()


def test_register_command_calls_include_description_and_args_hint(bm):
    ctx = MagicMock()
    bm._active_providers.clear()
    bm.register(ctx)
    for call in ctx.register_command.call_args_list:
        name, handler = call.args[0], call.args[1]
        kwargs = call.kwargs
        assert callable(handler)
        assert "description" in kwargs and kwargs["description"]
        assert "args_hint" in kwargs  # may be empty string for no-arg commands
    bm._active_providers.clear()


def test_register_tolerates_old_hermes_without_register_command(bm):
    """Plugins must not crash on Hermes < v0.11.0 (no register_command)."""
    class _OldCtx:
        def __init__(self):
            self.memory_calls = []

        def register_memory_provider(self, provider):
            self.memory_calls.append(provider)

    ctx = _OldCtx()
    bm._active_providers.clear()
    bm.register(ctx)  # must not raise
    assert len(ctx.memory_calls) == 1
    bm._active_providers.clear()


def test_register_swallows_register_command_errors(bm, caplog):
    """If one register_command call fails, the others — and provider
    registration — still proceed."""
    ctx = MagicMock()
    ctx.register_command.side_effect = ValueError("name collision with builtin")
    bm._active_providers.clear()
    with caplog.at_level("WARNING"):
        bm.register(ctx)
    ctx.register_memory_provider.assert_called_once()
    assert ctx.register_command.call_count == len(_EXPECTED_COMMANDS)
    assert "register_command" in caplog.text
    bm._active_providers.clear()


# ---------------------------------------------------------------------------
# Per-handler tests
# ---------------------------------------------------------------------------


def _ready_provider(bm, session: FakeSession | None = None):
    """Build a provider in 'initialized' state with a scripted actor."""
    provider = bm.BasicMemoryProvider()
    actor = make_scripted_actor(bm, session=session)
    actor.start()
    provider._actor = actor
    provider._initialized = True
    provider._project = "test-proj"
    return provider, actor


def _handlers_by_name(bm, provider):
    return {name: handler for name, handler, _, _ in bm._build_slash_commands(provider)}


# ---- Usage strings ----

@pytest.mark.parametrize(
    "name,args",
    [
        ("bm-search", ""),
        ("bm-search", "help"),
        ("bm-read", ""),
        ("bm-read", "-h"),
        ("bm-context", ""),
        ("bm-remember", ""),
        ("bm-remember", "--help"),
        # Commands that take no args use 'help' to surface their usage line
        ("bm-recent", "help"),
        ("bm-status", "help"),
        ("bm-project", "help"),
        ("bm-workspace", "help"),
    ],
)
def test_usage_returned_for_empty_or_help_args(bm, name, args):
    provider = bm.BasicMemoryProvider()  # not initialized — usage path shouldn't need it
    handlers = _handlers_by_name(bm, provider)
    out = handlers[name](args)
    assert isinstance(out, str)
    assert out.lower().startswith("usage:")


# ---- Uninitialized provider ----

@pytest.mark.parametrize("name,args", [
    ("bm-search", "hello"),
    ("bm-read", "some/note"),
    ("bm-context", "memory://x"),
    ("bm-recent", ""),
    ("bm-remember", "a thought"),
    ("bm-project", ""),
])
def test_handler_uninitialized_returns_message(bm, name, args):
    provider = bm.BasicMemoryProvider()
    handlers = _handlers_by_name(bm, provider)
    out = handlers[name](args)
    assert "not initialized" in out
    assert name in out  # message includes command name


# ---- /bm-status ----

def test_bm_status_renders_provider_state(bm, monkeypatch):
    provider = bm.BasicMemoryProvider()
    provider._mode = "local"
    provider._project = "demo"
    provider._project_path = "/tmp/demo"
    provider._capture_per_turn = True
    provider._capture_session_end = False
    provider._capture_folder = "transcripts"
    provider._remember_folder = "inbox"
    monkeypatch.setattr(bm, "_bm_binary_path", lambda: "/fake/bin/bm")
    out = _handlers_by_name(bm, provider)["bm-status"]("")
    assert "demo" in out
    assert "/tmp/demo" in out
    assert "/fake/bin/bm" in out
    assert "Initialized: no" in out
    assert "transcripts" in out and "inbox" in out


# ---- /bm-search ----

def test_bm_search_happy_path(bm):
    session = FakeSession()
    session.stub(
        "search_notes",
        lambda args: {
            "results": [
                {"title": "Decisions", "permalink": "decisions/foo", "content": "we chose X"},
                {"title": "Plan", "permalink": "plans/p1", "preview": "next quarter"},
            ]
        },
    )
    provider, actor = _ready_provider(bm, session)
    try:
        out = _handlers_by_name(bm, provider)["bm-search"]("widgets")
        assert "Decisions" in out
        assert "decisions/foo" in out
        assert "we chose X" in out
        # Args sent to BM
        call = session.calls[-1]
        assert call[0] == "search_notes"
        assert call[1]["query"] == "widgets"
        assert call[1]["project"] == "test-proj"
        assert call[1]["output_format"] == "json"
    finally:
        actor.shutdown()


def test_bm_search_empty_results(bm):
    session = FakeSession(default_response={"results": []})
    provider, actor = _ready_provider(bm, session)
    try:
        out = _handlers_by_name(bm, provider)["bm-search"]("missing")
        assert "No results" in out
        assert "missing" in out
    finally:
        actor.shutdown()


def test_bm_search_actor_exception_returns_plain_string(bm):
    session = FakeSession()
    def _boom(_args):
        raise RuntimeError("MCP transport closed")
    session.stub("search_notes", _boom)
    provider, actor = _ready_provider(bm, session)
    try:
        out = _handlers_by_name(bm, provider)["bm-search"]("anything")
        assert isinstance(out, str)
        assert out.startswith("bm-search:")
        assert "MCP transport closed" in out
    finally:
        actor.shutdown()


# ---- /bm-read ----

def test_bm_read_returns_text_body(bm):
    """BM's read_note returns markdown wrapped in {"text": "..."} once our
    extractor wraps the non-JSON response. The handler should unwrap and
    return the bare markdown."""
    session = FakeSession()
    session.stub(
        "read_note",
        # FakeSession serializes whatever the handler returns; emit the JSON
        # the wrapper would produce for a markdown response.
        lambda args: {"text": "# Foo\n\nbody text"},
    )
    provider, actor = _ready_provider(bm, session)
    try:
        out = _handlers_by_name(bm, provider)["bm-read"]("foo")
        assert out == "# Foo\n\nbody text"
        assert session.calls[-1][1]["identifier"] == "foo"
    finally:
        actor.shutdown()


# ---- /bm-recent ----

def test_bm_recent_default_timeframe(bm):
    session = FakeSession(default_response={"results": []})
    provider, actor = _ready_provider(bm, session)
    try:
        out = _handlers_by_name(bm, provider)["bm-recent"]("")
        assert "7d" in out
        assert session.calls[-1][1]["timeframe"] == "7d"
    finally:
        actor.shutdown()


def test_bm_recent_custom_timeframe(bm):
    session = FakeSession()
    session.stub(
        "recent_activity",
        lambda args: {"results": [{"title": "Recent thing", "permalink": "x/y"}]},
    )
    provider, actor = _ready_provider(bm, session)
    try:
        out = _handlers_by_name(bm, provider)["bm-recent"]("2 weeks")
        assert "2 weeks" in out
        assert "Recent thing" in out
        assert session.calls[-1][1]["timeframe"] == "2 weeks"
    finally:
        actor.shutdown()


# ---- /bm-remember ----

def test_bm_remember_derives_title_from_first_line(bm):
    captured = {}
    def _write(args):
        captured.update(args)
        return {"permalink": "bm-remember/note-perm"}
    session = FakeSession()
    session.stub("write_note", _write)
    provider, actor = _ready_provider(bm, session)
    provider._remember_folder = "bm-remember"
    try:
        out = _handlers_by_name(bm, provider)["bm-remember"](
            "Quarterly OKR review notes\n\nWe agreed to ship X."
        )
        assert "Saved:" in out
        assert "bm-remember/note-perm" in out
        assert captured["title"] == "Quarterly OKR review notes"
        assert captured["directory"] == "bm-remember"
        assert "manual-capture" in captured["tags"]
    finally:
        actor.shutdown()


def test_bm_remember_long_first_line_truncated_to_80(bm):
    session = FakeSession(default_response={"permalink": "x"})
    provider, actor = _ready_provider(bm, session)
    try:
        long_line = "A" * 200
        _handlers_by_name(bm, provider)["bm-remember"](long_line)
        title = session.calls[-1][1]["title"]
        assert len(title) == 80
    finally:
        actor.shutdown()


def test_bm_remember_uses_configured_folder(bm):
    session = FakeSession(default_response={"permalink": "x"})
    provider, actor = _ready_provider(bm, session)
    provider._remember_folder = "scratch"
    try:
        _handlers_by_name(bm, provider)["bm-remember"]("hello")
        assert session.calls[-1][1]["directory"] == "scratch"
    finally:
        actor.shutdown()


# ---- /bm-project ----

def test_bm_project_lists_and_marks_active(bm):
    session = FakeSession()
    session.stub(
        "list_memory_projects",
        lambda args: {
            "projects": [
                {"name": "other-proj"},
                {"name": "test-proj"},
            ]
        },
    )
    provider, actor = _ready_provider(bm, session)
    try:
        out = _handlers_by_name(bm, provider)["bm-project"]("")
        assert "other-proj" in out
        assert "test-proj" in out
        # Active project line includes the marker
        active_line = next(
            line for line in out.splitlines() if "test-proj" in line
        )
        assert "active" in active_line
    finally:
        actor.shutdown()


# ---- /bm-workspace ----

def test_bm_workspace_local_mode_message(bm):
    provider = bm.BasicMemoryProvider()
    provider._mode = "local"
    out = _handlers_by_name(bm, provider)["bm-workspace"]("")
    assert "Cloud" in out
    assert "local" in out


def test_bm_workspace_cloud_mode_lists(bm):
    session = FakeSession()
    session.stub(
        "list_workspaces",
        lambda args: {
            "workspaces": [
                {"name": "Personal", "workspace_type": "personal",
                 "role": "owner", "is_default": True},
                {"name": "Acme", "workspace_type": "team", "role": "member"},
            ]
        },
    )
    provider, actor = _ready_provider(bm, session)
    provider._mode = "cloud"
    try:
        out = _handlers_by_name(bm, provider)["bm-workspace"]("")
        assert "Personal" in out
        assert "Acme" in out
        assert "default" in out
    finally:
        actor.shutdown()


# ---- _unwrap_json_or_text helper ----

def test_unwrap_passes_through_raw_string(bm):
    assert bm._unwrap_json_or_text("plain text") == "plain text"


def test_unwrap_returns_inner_json_when_text_wraps_json(bm):
    outer = json.dumps({"text": json.dumps({"a": 1})})
    assert bm._unwrap_json_or_text(outer) == {"a": 1}


def test_unwrap_returns_text_value_when_inner_is_markdown(bm):
    outer = json.dumps({"text": "# Heading\n\nbody"})
    assert bm._unwrap_json_or_text(outer) == "# Heading\n\nbody"


def test_unwrap_returns_dict_when_top_level_json(bm):
    outer = json.dumps({"results": [1, 2]})
    assert bm._unwrap_json_or_text(outer) == {"results": [1, 2]}


# ---- _remember_title ----

def test_remember_title_strips_markdown_heading(bm):
    assert bm._remember_title("# Decisions\n\nbody") == "Decisions"


def test_remember_title_skips_blank_lines(bm):
    assert bm._remember_title("\n\nFirst real line\nrest") == "First real line"


def test_remember_title_falls_back_to_timestamp(bm):
    title = bm._remember_title("   \n\n")
    assert title.startswith("Note ")
