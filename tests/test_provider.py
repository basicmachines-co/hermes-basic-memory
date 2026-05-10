"""Tests for BasicMemoryProvider with the MCP actor mocked out."""
from __future__ import annotations

import json
from unittest.mock import MagicMock


def test_is_available_no_mcp(bm, monkeypatch):
    monkeypatch.setattr(bm, "_MCP_AVAILABLE", False)
    p = bm.BasicMemoryProvider()
    assert p.is_available() is False


def test_is_available_no_bm_binary(bm, monkeypatch):
    monkeypatch.setattr(bm, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(bm, "_bm_binary_path", lambda: None)
    p = bm.BasicMemoryProvider()
    assert p.is_available() is False


def test_is_available_happy(bm, monkeypatch):
    monkeypatch.setattr(bm, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(bm, "_bm_binary_path", lambda: "/fake/bm")
    p = bm.BasicMemoryProvider()
    assert p.is_available() is True


def test_name(bm):
    assert bm.BasicMemoryProvider().name == "basic-memory"
    assert bm.BasicMemoryProvider().name == bm.PROVIDER_NAME


def test_get_tool_schemas_uninitialized(bm):
    p = bm.BasicMemoryProvider()
    assert p.get_tool_schemas() == []


def test_get_tool_schemas_initialized(bm):
    p = bm.BasicMemoryProvider()
    p._initialized = True
    schemas = p.get_tool_schemas()
    assert len(schemas) == 7
    # Returned list is a copy (mutating shouldn't affect class state)
    schemas.clear()
    assert len(p.get_tool_schemas()) == 7


def test_handle_tool_call_uninitialized(bm):
    p = bm.BasicMemoryProvider()
    out = json.loads(p.handle_tool_call("bm_search", {"query": "x"}))
    assert "error" in out


def test_handle_tool_call_unknown_tool(bm):
    p = bm.BasicMemoryProvider()
    p._initialized = True
    p._actor = MagicMock()
    out = json.loads(p.handle_tool_call("bm_bogus", {}))
    assert "error" in out


def test_handle_tool_call_dispatches(bm):
    p = bm.BasicMemoryProvider()
    p._initialized = True
    p._project = "proj"
    actor = MagicMock()
    actor.call.return_value = json.dumps({"results": []})
    p._actor = actor

    out = p.handle_tool_call("bm_search", {"query": "hi", "limit": 3})
    actor.call.assert_called_once()
    bm_tool, bm_args = actor.call.call_args[0][:2]
    assert bm_tool == "search_notes"
    assert bm_args == {"project": "proj", "query": "hi", "page_size": 3}


def test_handle_tool_call_missing_arg(bm):
    p = bm.BasicMemoryProvider()
    p._initialized = True
    p._actor = MagicMock()
    out = json.loads(p.handle_tool_call("bm_write", {"title": "x"}))
    assert "error" in out


def test_handle_tool_call_actor_failure(bm):
    p = bm.BasicMemoryProvider()
    p._initialized = True
    p._project = "proj"
    actor = MagicMock()
    actor.call.side_effect = RuntimeError("boom")
    p._actor = actor
    out = json.loads(p.handle_tool_call("bm_search", {"query": "x"}))
    assert "error" in out
    assert p._failure_count == 1


def test_circuit_breaker_opens_after_5_failures(bm, monkeypatch):
    p = bm.BasicMemoryProvider()
    p._initialized = True
    p._project = "proj"
    actor = MagicMock()
    actor.call.side_effect = RuntimeError("boom")
    p._actor = actor

    for _ in range(5):
        p.handle_tool_call("bm_search", {"query": "x"})

    assert p._failure_pause_until > 0
    assert p._is_circuit_open() is True


def test_circuit_breaker_resets_after_pause(bm, monkeypatch):
    p = bm.BasicMemoryProvider()
    p._failure_count = 5
    p._failure_pause_until = 1.0  # already in the past
    monkeypatch.setattr(bm.time, "monotonic", lambda: 9999.0)
    assert p._is_circuit_open() is False
    assert p._failure_count == 0


def test_session_note_title_with_session_id(bm):
    from datetime import datetime, timezone
    p = bm.BasicMemoryProvider()
    p._session_started_at = datetime(2026, 5, 10, 13, 5, tzinfo=timezone.utc)
    p._session_id = "20260510_080249_571920"
    title = p._session_note_title()
    # Date appears once; trailing random component is the disambiguator
    assert title == "Hermes Session 2026-05-10 1305 571920"


def test_session_note_title_no_session_id(bm):
    from datetime import datetime, timezone
    p = bm.BasicMemoryProvider()
    p._session_started_at = datetime(2026, 5, 10, 13, 5, 42, tzinfo=timezone.utc)
    p._session_id = ""
    title = p._session_note_title()
    # Falls back to seconds for disambiguation
    assert title == "Hermes Session 2026-05-10 1305 42"


def test_session_note_title_short_session_id(bm):
    from datetime import datetime, timezone
    p = bm.BasicMemoryProvider()
    p._session_started_at = datetime(2026, 5, 10, 13, 5, tzinfo=timezone.utc)
    p._session_id = "abcdef"
    title = p._session_note_title()
    # No `_` in id → use last 6 chars
    assert title == "Hermes Session 2026-05-10 1305 abcdef"


def test_system_prompt_block_uninitialized(bm):
    p = bm.BasicMemoryProvider()
    assert p.system_prompt_block() == ""


def test_system_prompt_block_mentions_tools(bm):
    p = bm.BasicMemoryProvider()
    p._initialized = True
    p._project = "test-proj"
    p._mode = "local"
    out = p.system_prompt_block()
    assert "bm_search" in out
    assert "test-proj" in out
    assert "local" in out


def test_save_config_writes_json(bm, tmp_path):
    p = bm.BasicMemoryProvider()
    p.save_config({"mode": "local", "project": "x", "capture_per_turn": "true"}, str(tmp_path))
    written = json.loads((tmp_path / "basic-memory.json").read_text())
    assert written["mode"] == "local"
    assert written["project"] == "x"
    assert written["capture_per_turn"] is True  # coerced from "true"


def test_save_config_merges_existing(bm, tmp_path):
    cfg = tmp_path / "basic-memory.json"
    cfg.write_text(json.dumps({"mode": "cloud", "project": "old"}))
    p = bm.BasicMemoryProvider()
    p.save_config({"project": "new"}, str(tmp_path))
    after = json.loads(cfg.read_text())
    assert after["mode"] == "cloud"  # preserved
    assert after["project"] == "new"  # updated


def test_load_config_missing_returns_empty(bm, tmp_path):
    assert bm._load_config(str(tmp_path)) == {}


def test_load_config_corrupt_returns_empty(bm, tmp_path):
    (tmp_path / "basic-memory.json").write_text("{not json")
    assert bm._load_config(str(tmp_path)) == {}


def test_get_config_schema_shape(bm):
    schema = bm.BasicMemoryProvider().get_config_schema()
    keys = {entry["key"] for entry in schema}
    assert {"mode", "project", "project_path",
            "capture_per_turn", "capture_session_end", "capture_folder"}.issubset(keys)


def test_register_appends_to_active_providers(bm):
    fake_ctx = MagicMock()
    bm._active_providers.clear()
    bm.register(fake_ctx)
    fake_ctx.register_memory_provider.assert_called_once()
    assert len(bm._active_providers) == 1
    bm._active_providers.clear()


# ---- Edge cases ----

def test_handle_tool_call_with_none_args(bm):
    """args=None must not crash; should be coerced to {} and surface a missing-arg error."""
    p = bm.BasicMemoryProvider()
    p._initialized = True
    p._actor = MagicMock()
    out = json.loads(p.handle_tool_call("bm_search", None))  # type: ignore[arg-type]
    assert "error" in out


def test_handle_tool_call_unknown_does_not_invoke_actor(bm):
    p = bm.BasicMemoryProvider()
    p._initialized = True
    p._actor = MagicMock()
    p.handle_tool_call("bm_does_not_exist", {"x": 1})
    p._actor.call.assert_not_called()


def test_translate_args_unknown_tool_raises(bm):
    """Unknown tool names should raise KeyError so callers handle it explicitly."""
    import pytest
    with pytest.raises(KeyError):
        bm._translate_args("not_a_tool", {}, "proj")


# ---- Version metadata ----

def test_module_version_present(bm):
    assert hasattr(bm, "__version__")
    assert isinstance(bm.__version__, str)
    # Looks like semver
    parts = bm.__version__.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), bm.__version__


def test_module_version_matches_plugin_yaml(bm):
    """plugin.yaml ships to Hermes; __version__ is what tooling reads. Keep them in sync."""
    import os
    import re
    plugin_yaml = os.path.join(
        os.path.dirname(os.path.abspath(bm.__file__)),
        "plugin.yaml",
    )
    text = open(plugin_yaml).read()
    m = re.search(r"^\s*version\s*:\s*(\S+)\s*$", text, re.MULTILINE)
    assert m is not None, "plugin.yaml is missing a version field"
    assert m.group(1) == bm.__version__, (
        f"plugin.yaml version ({m.group(1)}) doesn't match __version__ ({bm.__version__})"
    )
