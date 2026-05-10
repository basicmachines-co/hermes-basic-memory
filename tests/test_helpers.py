"""Unit tests for the pure helpers in __init__.py."""
import json

import pytest


# ---- _truncate ----

def test_truncate_short_passes_through(bm):
    assert bm._truncate("hello", 10) == "hello"


def test_truncate_long_gets_ellipsis(bm):
    out = bm._truncate("a" * 100, 10)
    assert out.endswith("...")
    assert len(out) == 10


def test_truncate_non_string_coerced(bm):
    assert bm._truncate(42, 10) == "42"


def test_truncate_none(bm):
    assert bm._truncate(None, 10) == ""


# ---- _join_message_content ----

def test_join_string_content(bm):
    assert bm._join_message_content("hello") == "hello"


def test_join_list_of_dicts(bm):
    parts = [{"text": "a"}, {"text": "b"}, {"content": "c"}]
    assert bm._join_message_content(parts) == "a\nb\nc"


def test_join_list_of_strings(bm):
    parts = ["a", "b"]
    assert bm._join_message_content(parts) == "a\nb"


def test_join_mixed(bm):
    parts = ["a", {"text": "b"}, {"foo": "bar"}, "c"]
    assert bm._join_message_content(parts) == "a\nb\nc"


def test_join_none(bm):
    assert bm._join_message_content(None) == ""


# ---- _coerce_bool ----

@pytest.mark.parametrize("value,expected", [
    (True, True),
    (False, False),
    ("true", True),
    ("True", True),
    ("YES", True),
    ("1", True),
    ("y", True),
    ("false", False),
    ("False", False),
    ("NO", False),
    ("0", False),
    ("n", False),
])
def test_coerce_bool(bm, value, expected):
    assert bm._coerce_bool(value) is expected


def test_coerce_bool_non_bool_passes_through(bm):
    assert bm._coerce_bool(42) == 42
    assert bm._coerce_bool("hello") == "hello"


# ---- _extract_mcp_text ----

def test_extract_mcp_text_passes_json_through(bm, fake_result):
    payload = json.dumps({"permalink": "foo/bar", "title": "T"})
    out = bm._extract_mcp_text(fake_result([payload]))
    assert json.loads(out)["permalink"] == "foo/bar"


def test_extract_mcp_text_wraps_markdown(bm, fake_result):
    md = "# Created note\npermalink: foo/bar"
    out = bm._extract_mcp_text(fake_result([md]))
    parsed = json.loads(out)
    assert parsed["text"] == md


def test_extract_mcp_text_joins_multiple_blocks(bm, fake_result):
    out = bm._extract_mcp_text(fake_result(["a", "b"]))
    parsed = json.loads(out)
    assert parsed["text"] == "a\nb"


def test_extract_mcp_text_empty(bm, fake_result):
    out = bm._extract_mcp_text(fake_result([]))
    assert json.loads(out) == {"ok": True}


def test_extract_mcp_text_error(bm, fake_result):
    out = bm._extract_mcp_text(fake_result(["something broke"], is_error=True))
    assert "error" in json.loads(out)


# ---- _extract_permalink ----

def test_extract_permalink_from_bare_json(bm):
    text = json.dumps({"permalink": "proj/folder/note", "title": "T"})
    assert bm._extract_permalink(text, "fb") == "proj/folder/note"


def test_extract_permalink_from_wrapped_json(bm):
    text = json.dumps({"text": json.dumps({"permalink": "proj/folder/note"})})
    assert bm._extract_permalink(text, "fb") == "proj/folder/note"


def test_extract_permalink_from_wrapped_markdown(bm):
    md = (
        "# Created note\n"
        "project: hermes-jodys-imac\n"
        "file_path: x/y.md\n"
        "permalink: hermes-jodys-imac/folder/slug-name\n"
        "checksum: unknown\n"
    )
    text = json.dumps({"text": md})
    assert bm._extract_permalink(text, "fb") == "hermes-jodys-imac/folder/slug-name"


def test_extract_permalink_from_raw_markdown(bm):
    md = (
        "# Created note\npermalink: proj/folder/slug\n"
    )
    # Raw, not wrapped — strategy 4 path
    assert bm._extract_permalink(md, "fb") == "proj/folder/slug"


def test_extract_permalink_no_match(bm):
    assert bm._extract_permalink('{"foo":"bar"}', "fallback-title") == "fallback-title"


def test_extract_permalink_empty(bm):
    assert bm._extract_permalink("", "fb") == "fb"


def test_extract_permalink_invalid(bm):
    assert bm._extract_permalink("not json or markdown", "fb") == "fb"


def test_extract_permalink_strips_trailing_punct(bm):
    md = "# Created note\npermalink: proj/folder/slug,"
    assert bm._extract_permalink(md, "fb") == "proj/folder/slug"


# ---- _translate_args ----

def test_translate_search(bm):
    tool, args = bm._translate_args("bm_search", {"query": "hi", "limit": 7}, "proj")
    assert tool == "search_notes"
    assert args == {"project": "proj", "query": "hi", "page_size": 7}


def test_translate_search_no_limit(bm):
    tool, args = bm._translate_args("bm_search", {"query": "hi"}, "proj")
    assert tool == "search_notes"
    assert args == {"project": "proj", "query": "hi"}


def test_translate_read(bm):
    tool, args = bm._translate_args("bm_read", {"identifier": "x/y"}, "proj")
    assert tool == "read_note"
    assert args == {"project": "proj", "identifier": "x/y"}


def test_translate_write(bm):
    tool, args = bm._translate_args(
        "bm_write",
        {"title": "T", "content": "C", "folder": "F", "tags": ["a", "b"]},
        "proj",
    )
    assert tool == "write_note"
    assert args == {
        "project": "proj",
        "title": "T",
        "content": "C",
        "directory": "F",
        "tags": ["a", "b"],
    }


def test_translate_write_no_tags(bm):
    tool, args = bm._translate_args(
        "bm_write",
        {"title": "T", "content": "C", "folder": "F"},
        "proj",
    )
    assert tool == "write_note"
    assert "tags" not in args
    assert args["directory"] == "F"


def test_translate_edit_minimal(bm):
    tool, args = bm._translate_args(
        "bm_edit",
        {"identifier": "x", "operation": "append", "content": "more"},
        "proj",
    )
    assert tool == "edit_note"
    assert args == {
        "project": "proj",
        "identifier": "x",
        "operation": "append",
        "content": "more",
    }


def test_translate_edit_find_replace(bm):
    tool, args = bm._translate_args(
        "bm_edit",
        {
            "identifier": "x",
            "operation": "find_replace",
            "content": "new",
            "find_text": "old",
        },
        "proj",
    )
    assert args["find_text"] == "old"


def test_translate_edit_replace_section(bm):
    tool, args = bm._translate_args(
        "bm_edit",
        {
            "identifier": "x",
            "operation": "replace_section",
            "content": "new",
            "section": "## Notes",
        },
        "proj",
    )
    assert args["section"] == "## Notes"


def test_translate_context(bm):
    tool, args = bm._translate_args(
        "bm_context", {"url": "memory://x", "depth": 2}, "proj"
    )
    assert tool == "build_context"
    assert args == {"project": "proj", "url": "memory://x", "depth": 2}


def test_translate_delete(bm):
    tool, args = bm._translate_args("bm_delete", {"identifier": "x"}, "proj")
    assert tool == "delete_note"
    assert args == {"project": "proj", "identifier": "x"}


def test_translate_move(bm):
    tool, args = bm._translate_args(
        "bm_move", {"identifier": "x", "new_folder": "archive/2026"}, "proj"
    )
    assert tool == "move_note"
    assert args == {
        "project": "proj",
        "identifier": "x",
        "destination_folder": "archive/2026",
    }


# ---- _default_project / _hostname ----

def test_default_project_format(bm):
    p = bm._default_project()
    assert p.startswith("hermes-")
    # Hostnames are lowercased and stripped
    assert " " not in p


def test_hostname_lowercased(bm, monkeypatch):
    monkeypatch.setattr(bm.socket, "gethostname", lambda: "Some.Long.Host")
    assert bm._hostname() == "some"


# ---- TOOL_SCHEMAS ----

def test_tool_schemas_complete(bm):
    names = {s["name"] for s in bm.TOOL_SCHEMAS}
    expected = {"bm_search", "bm_read", "bm_write", "bm_edit",
                "bm_context", "bm_delete", "bm_move"}
    assert names == expected


def test_tool_schemas_have_descriptions(bm):
    for s in bm.TOOL_SCHEMAS:
        assert s["description"], f"{s['name']} missing description"
        assert "parameters" in s
        assert s["parameters"]["type"] == "object"


def test_hermes_to_bm_complete(bm):
    assert set(bm._HERMES_TO_BM.keys()) == {s["name"] for s in bm.TOOL_SCHEMAS}
