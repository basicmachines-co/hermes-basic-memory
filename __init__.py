"""
basic-memory — Hermes Memory Provider plugin

Wraps the basic-memory MCP server (`bm mcp`) to provide knowledge-graph-backed
memory for Hermes. Analog of openclaw-basic-memory.

Architecture:
- `_BmMcpActor` owns a long-lived asyncio loop in a daemon thread that holds
  the MCP `ClientSession` open across the agent's lifetime. Sync hooks dispatch
  through `asyncio.run_coroutine_threadsafe`.
- `BasicMemoryProvider` implements Hermes's `MemoryProvider` ABC: tools,
  prefetch, sync_turn (per-turn capture), on_session_end (summary).

The plugin loader text-greps for `register_memory_provider` or `MemoryProvider`
to detect this file as a memory provider — both tokens are present below.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from typing import Any, Dict, List, Optional, Tuple

# Hermes ABC + helpers — these resolve because Hermes adds its tree to sys.path
# when loading plugins (same pattern as plugins/memory/mem0/__init__.py:21).
from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

__version__ = "0.1.5"

logger = logging.getLogger("hermes.memory.basic-memory")


# ---------------------------------------------------------------------------
# MCP SDK import — soft. If unavailable, is_available() returns False.
# ---------------------------------------------------------------------------

_MCP_AVAILABLE = False
_MCP_IMPORT_ERROR: Optional[BaseException] = None
try:
    from mcp import ClientSession, StdioServerParameters  # type: ignore
    from mcp.client.stdio import stdio_client  # type: ignore
    _MCP_AVAILABLE = True
except Exception as _e:  # pragma: no cover
    _MCP_IMPORT_ERROR = _e


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVIDER_NAME = "basic-memory"

# Hermes-side tool names → BM MCP tool names. Curated subset of BM's surface.
_HERMES_TO_BM: Dict[str, str] = {
    "bm_search": "search_notes",
    "bm_read": "read_note",
    "bm_write": "write_note",
    "bm_edit": "edit_note",
    "bm_context": "build_context",
    "bm_delete": "delete_note",
    "bm_move": "move_note",
}

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "bm_search",
        "description": (
            "Search the Basic Memory knowledge graph for notes, decisions, observations. "
            "Use BEFORE answering questions about prior work — context may already exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms (semantic + full-text)."},
                "limit": {"type": "integer", "description": "Max results (default 10).", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "bm_read",
        "description": "Read a specific note by title, permalink, or memory:// URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Note title, permalink, or memory:// URL."},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "bm_write",
        "description": (
            "Create a new note in the knowledge graph. Use clear titles and a folder "
            "(e.g. 'projects', 'decisions', 'meetings')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string", "description": "Markdown body."},
                "folder": {"type": "string", "description": "Folder path within the project."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags."},
            },
            "required": ["title", "content", "folder"],
        },
    },
    {
        "name": "bm_edit",
        "description": (
            "Edit an existing note. Operations: append, prepend, find_replace, replace_section. "
            "find_replace requires find_text. replace_section requires section."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "operation": {
                    "type": "string",
                    "enum": ["append", "prepend", "find_replace", "replace_section"],
                },
                "content": {"type": "string"},
                "find_text": {"type": "string", "description": "Required for find_replace."},
                "section": {"type": "string", "description": "Required for replace_section."},
            },
            "required": ["identifier", "operation", "content"],
        },
    },
    {
        "name": "bm_context",
        "description": (
            "Navigate the knowledge graph from a memory:// URL or note identifier. "
            "Returns the target note plus related notes via traversed relations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "memory:// URL or note identifier."},
                "depth": {"type": "integer", "description": "Relation traversal depth (default 1).", "default": 1},
            },
            "required": ["url"],
        },
    },
    {
        "name": "bm_delete",
        "description": "Delete a note from the knowledge graph.",
        "parameters": {
            "type": "object",
            "properties": {"identifier": {"type": "string"}},
            "required": ["identifier"],
        },
    },
    {
        "name": "bm_move",
        "description": "Move a note to a different folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "new_folder": {"type": "string"},
            },
            "required": ["identifier", "new_folder"],
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bm_binary_path() -> Optional[str]:
    """Find the bm CLI without making network calls. Used by is_available()."""
    candidates = [
        os.path.expanduser("~/.local/bin/bm"),
        "/opt/homebrew/bin/bm",
        "/usr/local/bin/bm",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return which("bm")


def _uv_binary_path() -> Optional[str]:
    """Find the uv CLI. Used to bootstrap-install basic-memory when bm is missing."""
    candidates = [
        os.path.expanduser("~/.local/bin/uv"),
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return which("uv")


def _install_bm_via_uv(timeout: float = 180.0) -> Optional[str]:
    """
    Bootstrap-install basic-memory via `uv tool install`.

    Idempotent — re-runs are no-ops when the tool is already installed, so this
    converges with later manual `uv tool install basic-memory` calls and avoids
    the two-installations-sharing-one-config-dir foot-gun.

    Returns the resolved bm path on success, or None if uv is unavailable or
    the install failed.
    """
    uv = _uv_binary_path()
    if not uv:
        return None
    try:
        result = subprocess.run(
            [uv, "tool", "install", "basic-memory", "--quiet"],
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except Exception as e:
        logger.warning("basic-memory: `uv tool install basic-memory` failed: %s", e)
        return None
    if result.returncode != 0:
        # uv prints to stderr; capture the tail so the operator can debug.
        stderr_tail = (result.stderr or b"").decode("utf-8", errors="replace")[-400:]
        logger.warning(
            "basic-memory: `uv tool install basic-memory` exited %s: %s",
            result.returncode,
            stderr_tail.strip(),
        )
        return None
    return _bm_binary_path()


def _hostname() -> str:
    return socket.gethostname().split(".")[0].lower().replace(" ", "-")


def _default_project() -> str:
    # Each machine gets its own local project with this name. Cloud setups
    # use a different name (e.g. hermes-memory-cloud) so the two don't
    # collide in BM's per-workspace project registry.
    return "hermes-memory"


def _default_project_path() -> str:
    # ~/.basic-memory/ is reserved for BM's own application state; user
    # project files live in user space, parallel to ~/basic-memory/.
    return os.path.expanduser("~/hermes-memory/")


def _config_path(hermes_home: str) -> Path:
    return Path(hermes_home) / "basic-memory.json"


def _bm_config_path() -> Path:
    """Location of bm's own project registry."""
    return Path.home() / ".basic-memory" / "config.json"


def _bm_known_projects() -> Optional[Dict[str, Any]]:
    """
    Read bm's project registry. Returns None if the file is absent or
    unparseable — callers should treat that as "can't prove anything"
    rather than "project is missing".
    """
    path = _bm_config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    projects = data.get("projects")
    return projects if isinstance(projects, dict) else None


def _load_config(hermes_home: str) -> Dict[str, Any]:
    p = _config_path(hermes_home)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as e:
        logger.warning("could not parse %s: %s — using defaults", p, e)
        return {}


def _truncate(s: Any, n: int) -> str:
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


def _join_message_content(parts: Any) -> str:
    if isinstance(parts, str):
        return parts
    if isinstance(parts, list):
        out: List[str] = []
        for p in parts:
            if isinstance(p, dict):
                t = p.get("text") or p.get("content")
                if isinstance(t, str):
                    out.append(t)
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(out)
    return str(parts) if parts is not None else ""


def _coerce_bool(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        if v.lower() in ("true", "1", "yes", "y"):
            return True
        if v.lower() in ("false", "0", "no", "n"):
            return False
    return v


def _extract_mcp_text(result: Any) -> str:
    """
    Extract text from an MCP CallToolResult.

    Returns a JSON string for the agent. If the result is itself JSON, returns it
    as-is. Otherwise wraps the text in `{"text": "..."}` for downstream parsing.
    """
    is_error = bool(getattr(result, "isError", False))
    parts: List[str] = []
    for c in getattr(result, "content", None) or []:
        text = getattr(c, "text", None)
        if isinstance(text, str):
            parts.append(text)
    text = "\n".join(parts).strip()
    if is_error:
        return tool_error(text or "MCP tool returned error")
    if not text:
        return json.dumps({"ok": True})
    # If it's already JSON, pass through verbatim
    try:
        json.loads(text)
        return text
    except Exception:
        return json.dumps({"text": text})


_PERMALINK_JSON_RE = re.compile(r'"permalink"\s*:\s*"([^"]+)"')
_PERMALINK_MD_RE = re.compile(r"^\s*permalink\s*:\s*(\S+)\s*$", re.MULTILINE)


def _extract_permalink(text: str, fallback: str) -> str:
    """
    Extract a note permalink from any plausible BM response shape:

    1. Bare JSON dict with `permalink` key (output_format=json path)
    2. `{"text": "..."}` wrapping inner JSON or markdown
    3. Raw markdown response text (output_format=text default)

    Falls back to the supplied fallback when nothing matches.
    """
    if not isinstance(text, str) or not text:
        return fallback

    # Strategy 1: parse outer as JSON
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            if isinstance(d.get("permalink"), str):
                return d["permalink"]
            inner = d.get("text")
            if isinstance(inner, str):
                # Strategy 2: inner is JSON
                try:
                    d2 = json.loads(inner)
                    if isinstance(d2, dict) and isinstance(d2.get("permalink"), str):
                        return d2["permalink"]
                except Exception:
                    pass
                # Strategy 3a: inner is markdown with `permalink: ...` line
                m = _PERMALINK_MD_RE.search(inner)
                if m:
                    return m.group(1).rstrip(",.;")
                # Strategy 3b: inner contains JSON substring with permalink
                m = _PERMALINK_JSON_RE.search(inner)
                if m:
                    return m.group(1)
    except Exception:
        pass

    # Strategy 4: best-effort regex on raw text (covers exotic shapes)
    m = _PERMALINK_JSON_RE.search(text)
    if m:
        return m.group(1)
    m = _PERMALINK_MD_RE.search(text)
    if m:
        return m.group(1).rstrip(",.;")
    return fallback


# ---------------------------------------------------------------------------
# MCP actor — single asyncio loop in a daemon thread, owns ClientSession
# ---------------------------------------------------------------------------

class _BmMcpActor:
    """
    Owns the lifetime of one MCP ClientSession to the bm MCP server.

    Why this exists: Hermes calls memory provider hooks synchronously from
    a sync code path (memory_manager.py invokes provider.sync_turn / .prefetch
    / .handle_tool_call directly). MCP `ClientSession` is asyncio-bound and
    not thread-safe across event loops. So we run one asyncio loop in a
    daemon thread and ferry calls in via run_coroutine_threadsafe.
    """

    def __init__(self, server_argv: List[str], env: Optional[Dict[str, str]] = None):
        self._server_argv = list(server_argv)
        self._env = dict(env) if env is not None else os.environ.copy()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional["ClientSession"] = None
        self._ready = threading.Event()
        self._init_error: Optional[BaseException] = None
        self._stop_future: Optional[asyncio.Future] = None
        self._tools_cache: List[Dict[str, Any]] = []
        self._running = False

    def start(self, timeout: float = 25.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="bm-mcp-actor"
        )
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            self._running = False
            raise TimeoutError(
                f"basic-memory MCP server didn't initialize within {timeout}s"
            )
        if self._init_error is not None:
            self._running = False
            raise RuntimeError(
                f"basic-memory MCP server failed to start: {self._init_error}"
            )

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except BaseException as e:
            if self._init_error is None:
                self._init_error = e
            self._ready.set()
            logger.exception("basic-memory MCP actor terminated with error")
        finally:
            self._running = False
            try:
                loop.close()
            except Exception:
                pass

    async def _main(self) -> None:
        params = StdioServerParameters(
            command=self._server_argv[0],
            args=self._server_argv[1:],
            env=self._env,
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._stop_future = asyncio.get_running_loop().create_future()
                    try:
                        listing = await session.list_tools()
                        self._tools_cache = [
                            {
                                "name": getattr(t, "name", ""),
                                "description": getattr(t, "description", "") or "",
                            }
                            for t in getattr(listing, "tools", []) or []
                        ]
                    except Exception as e:
                        logger.warning("list_tools failed: %s", e)
                        self._tools_cache = []
                    self._ready.set()
                    await self._stop_future  # blocks until shutdown
        except BaseException as e:
            if self._init_error is None:
                self._init_error = e
            self._ready.set()
            raise

    def call(self, tool_name: str, arguments: Dict[str, Any], timeout: float = 30.0) -> str:
        if not self._running:
            raise RuntimeError("basic-memory MCP actor not running")
        if self._loop is None or self._session is None:
            raise RuntimeError("basic-memory MCP actor not started")
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool_name, arguments),
            self._loop,
        )
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # Cancel the coroutine on the actor loop so we don't leak
            # a stuck call_tool. cancel() on a run_coroutine_threadsafe
            # future propagates cancellation into the wrapped coroutine.
            future.cancel()
            raise
        return _extract_mcp_text(result)

    def list_tools(self) -> List[Dict[str, Any]]:
        return list(self._tools_cache)

    def shutdown(self, timeout: float = 5.0) -> None:
        self._running = False
        if self._loop is not None and self._stop_future is not None:
            try:
                self._loop.call_soon_threadsafe(
                    lambda: (self._stop_future and not self._stop_future.done())
                    and self._stop_future.set_result(None)
                )
            except Exception:
                # Loop may already be closed; safe to ignore.
                pass
        if self._thread is not None:
            try:
                self._thread.join(timeout=timeout)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Argument translation: Hermes-side tool args → BM MCP tool args
# ---------------------------------------------------------------------------

def _translate_args(
    hermes_tool: str, args: Dict[str, Any], project: str
) -> Tuple[str, Dict[str, Any]]:
    bm_tool = _HERMES_TO_BM[hermes_tool]
    out: Dict[str, Any] = {"project": project}
    if hermes_tool == "bm_search":
        out["query"] = args["query"]
        if "limit" in args and args["limit"] is not None:
            out["page_size"] = int(args["limit"])
    elif hermes_tool == "bm_read":
        out["identifier"] = args["identifier"]
    elif hermes_tool == "bm_write":
        out["title"] = args["title"]
        out["content"] = args["content"]
        out["directory"] = args["folder"]
        if args.get("tags"):
            out["tags"] = list(args["tags"])
    elif hermes_tool == "bm_edit":
        out["identifier"] = args["identifier"]
        out["operation"] = args["operation"]
        out["content"] = args["content"]
        if args.get("find_text") is not None:
            out["find_text"] = args["find_text"]
        if args.get("section") is not None:
            out["section"] = args["section"]
    elif hermes_tool == "bm_context":
        out["url"] = args["url"]
        if args.get("depth") is not None:
            out["depth"] = int(args["depth"])
    elif hermes_tool == "bm_delete":
        out["identifier"] = args["identifier"]
    elif hermes_tool == "bm_move":
        out["identifier"] = args["identifier"]
        out["destination_folder"] = args["new_folder"]
    return bm_tool, out


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class BasicMemoryProvider(MemoryProvider):
    """Hermes Memory Provider backed by the basic-memory MCP server."""

    def __init__(self) -> None:
        self._actor: Optional[_BmMcpActor] = None
        self._project: str = _default_project()
        self._mode: str = "local"
        self._project_path: str = _default_project_path()
        self._capture_per_turn: bool = True
        self._capture_session_end: bool = True
        self._capture_folder: str = "hermes-sessions"
        self._session_id: str = ""
        self._hermes_home: str = ""
        self._session_note_id: Optional[str] = None
        self._session_started_at: Optional[datetime] = None
        self._sync_thread: Optional[threading.Thread] = None
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_lock = threading.Lock()
        self._pending_prefetch: str = ""
        self._failure_count: int = 0
        self._failure_pause_until: float = 0.0
        self._initialized: bool = False
        self._first_user_msg: Optional[str] = None

    # ---- Identity ----
    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        # Discovery hot path. NEVER make network calls or spawn subprocesses here.
        # We report available when either bm is present already OR uv is present
        # (we bootstrap-install bm via `uv tool install` at initialize() time).
        if not _MCP_AVAILABLE:
            return False
        if _bm_binary_path():
            return True
        if _uv_binary_path():
            return True
        return False

    # ---- Lifecycle ----
    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id or ""
        self._hermes_home = kwargs.get("hermes_home") or os.path.expanduser("~/.hermes")
        cfg = _load_config(self._hermes_home)
        self._mode = cfg.get("mode") or "local"
        self._project = cfg.get("project") or _default_project()
        self._project_path = os.path.expanduser(
            cfg.get("project_path") or _default_project_path()
        )
        self._capture_per_turn = bool(_coerce_bool(cfg.get("capture_per_turn", True)))
        self._capture_session_end = bool(_coerce_bool(cfg.get("capture_session_end", True)))
        self._capture_folder = cfg.get("capture_folder") or "hermes-sessions"

        # Bootstrap-install bm via uv if it's not already on disk. One-time cost
        # on a fresh machine; idempotent no-op once basic-memory is installed.
        if not _bm_binary_path():
            if _uv_binary_path() is None:
                logger.error(
                    "basic-memory: bm CLI not found and uv is not installed. "
                    "Install uv (https://docs.astral.sh/uv/) or run "
                    "`pip install basic-memory` manually. Provider will not initialize."
                )
                return
            logger.info(
                "basic-memory: bm CLI not found — installing basic-memory via "
                "`uv tool install` (one-time bootstrap)"
            )
            if _install_bm_via_uv() is None:
                logger.error(
                    "basic-memory: auto-install via uv failed. Run "
                    "`uv tool install basic-memory` manually to debug. "
                    "Provider will not initialize."
                )
                return

        if self._mode == "local":
            self._ensure_local_project()

        if not self._verify_project_registered():
            self._log_missing_project()
            return

        try:
            argv = self._server_argv()
        except Exception as e:
            logger.error("basic-memory: cannot determine server argv: %s", e)
            return

        actor = _BmMcpActor(argv)
        try:
            actor.start(timeout=25.0)
        except Exception as e:
            logger.error("basic-memory: MCP server failed to start: %s", e)
            return
        self._actor = actor

        tools = actor.list_tools()
        names = {t["name"] for t in tools}
        missing = [bm for bm in _HERMES_TO_BM.values() if bm not in names]
        if missing:
            logger.warning(
                "basic-memory: BM MCP missing expected tools: %s (got: %s)",
                missing,
                sorted(names),
            )

        self._session_started_at = datetime.now(timezone.utc)
        self._initialized = True
        logger.info(
            "basic-memory provider ready: mode=%s project=%s tools=%d",
            self._mode,
            self._project,
            len(tools),
        )

    def _ensure_local_project(self) -> None:
        bm = _bm_binary_path()
        if not bm:
            return
        os.makedirs(self._project_path, exist_ok=True)
        try:
            subprocess.run(
                [bm, "project", "add", self._project, self._project_path],
                check=False,
                capture_output=True,
                timeout=15,
            )
        except Exception as e:
            logger.debug("bm project add: %s", e)

    def _verify_project_registered(self) -> bool:
        """
        Confirm the configured project is registered with bm.

        Returns False only when we can prove the project is missing
        (bm config exists, parses, and the project name is absent).
        Otherwise returns True — including when bm's config doesn't exist
        yet — so we don't false-positive-reject on first-run setups.
        """
        projects = _bm_known_projects()
        if projects is None:
            return True
        return self._project in projects

    def _log_missing_project(self) -> None:
        if self._mode == "cloud":
            hint = (
                f"`bm project add {self._project} --cloud` "
                "(optionally with --local-path) first"
            )
        else:
            hint = f"`bm project add {self._project} {self._project_path}` first"
        logger.error(
            "basic-memory: project %r is not registered with bm. Run %s. "
            "Provider will not initialize.",
            self._project,
            hint,
        )

    def _server_argv(self) -> List[str]:
        bm = _bm_binary_path()
        if not bm:
            raise RuntimeError("bm CLI not found on PATH")
        # `bm mcp` works the same in local and cloud modes — bm reads
        # ~/.basic-memory/config.json to know whether the active project is local
        # or cloud-routed. We pass --project on each tool call.
        return [bm, "mcp"]

    def shutdown(self) -> None:
        if self._actor is not None:
            try:
                self._actor.shutdown(timeout=5.0)
            except Exception as e:
                logger.debug("actor shutdown: %s", e)
        self._actor = None
        self._initialized = False

    # ---- Tool surface ----
    def system_prompt_block(self) -> str:
        if not self._initialized:
            return ""
        return (
            "## Basic Memory Knowledge Graph\n"
            "You have a persistent knowledge graph. Tools:\n"
            "- bm_search: search before answering — context may already exist\n"
            "- bm_read: fetch a note by title, permalink, or memory:// URL\n"
            "- bm_context: navigate via memory:// URLs to find related notes\n"
            "- bm_write: capture decisions, insights, meeting notes worth preserving\n"
            "- bm_edit: append updates or fix existing notes (append/prepend/find_replace/replace_section)\n"
            "- bm_delete / bm_move: maintenance operations\n"
            f"Active project: `{self._project}` ({self._mode})"
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if not self._initialized:
            return []
        return [dict(s) for s in TOOL_SCHEMAS]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if not self._initialized or self._actor is None:
            return tool_error("basic-memory provider not initialized")
        if tool_name not in _HERMES_TO_BM:
            return tool_error(f"Unknown tool: {tool_name}")
        try:
            bm_tool, bm_args = _translate_args(tool_name, args or {}, self._project)
        except KeyError as e:
            return tool_error(f"{tool_name}: missing required arg {e}")
        try:
            return self._actor.call(bm_tool, bm_args, timeout=30.0)
        except Exception as e:
            self._record_failure(e)
            logger.exception("bm tool call failed: %s", tool_name)
            return tool_error(f"{tool_name}: {e}")

    # ---- Recall (retrieve step) ----
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        with self._prefetch_lock:
            cached = self._pending_prefetch
            self._pending_prefetch = ""
        if cached:
            return cached
        if not self._initialized or self._actor is None or self._is_circuit_open():
            return ""
        try:
            raw = self._actor.call(
                "search_notes",
                {
                    "project": self._project,
                    "query": query,
                    "page_size": 5,
                    "output_format": "json",
                },
                timeout=3.0,
            )
            return self._format_prefetch(raw)
        except Exception as e:
            self._record_failure(e)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._initialized or self._actor is None or self._is_circuit_open():
            return
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            return

        def _bg() -> None:
            try:
                raw = self._actor.call(  # type: ignore[union-attr]
                    "search_notes",
                    {
                        "project": self._project,
                        "query": query,
                        "page_size": 5,
                        "output_format": "json",
                    },
                    timeout=10.0,
                )
                with self._prefetch_lock:
                    self._pending_prefetch = self._format_prefetch(raw)
            except Exception as e:
                self._record_failure(e)
                logger.debug("queue_prefetch bg failed: %s", e)

        self._prefetch_thread = threading.Thread(target=_bg, daemon=True, name="bm-prefetch")
        self._prefetch_thread.start()

    def _format_prefetch(self, raw_json: str) -> str:
        try:
            data = json.loads(raw_json)
        except Exception:
            return ""
        # BM may return JSON directly or wrapped {"text": "..."}
        if isinstance(data, dict) and "text" in data and "results" not in data:
            try:
                data = json.loads(data["text"])
            except Exception:
                return ""
        results = data.get("results") if isinstance(data, dict) else None
        if not results:
            return ""
        lines = ["## Basic Memory Recall"]
        for r in list(results)[:5]:
            if not isinstance(r, dict):
                continue
            title = str(r.get("title") or "(untitled)")
            permalink = str(r.get("permalink") or "")
            preview_raw = r.get("content") or r.get("preview") or ""
            if not isinstance(preview_raw, str):
                preview_raw = str(preview_raw)
            preview = re.sub(r"\s+", " ", preview_raw)[:200]
            lines.append(f"- **{title}** (`{permalink}`) — {preview}")
        return "\n".join(lines)

    # ---- Per-turn capture (extract step) ----
    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        if (
            not self._capture_per_turn
            or not self._initialized
            or self._actor is None
            or self._is_circuit_open()
        ):
            return
        if self._first_user_msg is None and user_content:
            self._first_user_msg = _truncate(user_content, 240)
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=3.0)

        def _bg() -> None:
            try:
                self._capture_turn(user_content, assistant_content)
            except Exception as e:
                self._record_failure(e)
                logger.warning("basic-memory sync_turn failed: %s", e)

        self._sync_thread = threading.Thread(target=_bg, daemon=True, name="bm-sync-turn")
        self._sync_thread.start()

    def _capture_turn(self, user: str, asst: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        block = (
            f"### {ts}\n\n"
            f"**User:** {_truncate(user, 4000)}\n\n"
            f"**Assistant:** {_truncate(asst, 4000)}\n"
        )
        if not self._session_note_id:
            title = self._session_note_title()
            content = (
                f"# {title}\n\n"
                f"Live transcript of Hermes session "
                f"`{self._session_id or '(no-id)'}`. "
                f"Auto-captured by basic-memory provider.\n\n"
                f"## Turns\n\n{block}\n"
            )
            args = {
                "project": self._project,
                "title": title,
                "directory": self._capture_folder,
                "content": content,
                "tags": ["hermes-session", _hostname()],
                "output_format": "json",
            }
            raw = self._actor.call("write_note", args, timeout=15.0)  # type: ignore[union-attr]
            self._session_note_id = _extract_permalink(raw, fallback=title)
        else:
            args = {
                "project": self._project,
                "identifier": self._session_note_id,
                "operation": "append",
                "content": "\n" + block,
            }
            self._actor.call("edit_note", args, timeout=15.0)  # type: ignore[union-attr]

    def _session_note_title(self) -> str:
        ts = (self._session_started_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H%M")
        # Hermes session IDs encode the date in the prefix (e.g. 20260510_080249_571920).
        # Use the trailing random component for disambiguation; falling back to seconds
        # only when there is no session id at all.
        sid = self._session_id or ""
        suffix = sid.rsplit("_", 1)[-1] if "_" in sid else sid[-6:]
        if not suffix:
            suffix = (self._session_started_at or datetime.now(timezone.utc)).strftime("%S")
        return f"Hermes Session {ts} {suffix}"

    # ---- Session-end summary ----
    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if (
            not self._capture_session_end
            or not self._initialized
            or self._actor is None
        ):
            return
        try:
            self._write_summary(messages)
        except Exception as e:
            logger.warning("basic-memory on_session_end summary failed: %s", e)

    def _write_summary(self, messages: List[Dict[str, Any]]) -> None:
        user_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
        asst_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "assistant"]
        first_user = self._first_user_msg or ""
        if not first_user and user_msgs:
            first_user = _truncate(_join_message_content(user_msgs[0].get("content")), 240)
        last_asst = ""
        if asst_msgs:
            last_asst = _truncate(_join_message_content(asst_msgs[-1].get("content")), 600)
        ts = (self._session_started_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H%M")
        sid = self._session_id or ""
        suffix = sid.rsplit("_", 1)[-1] if "_" in sid else sid[-6:]
        if not suffix:
            suffix = (self._session_started_at or datetime.now(timezone.utc)).strftime("%S")
        title = f"Hermes Session Summary {ts} {suffix}"
        lines = [
            f"# {title}",
            "",
            f"Session: `{self._session_id or '(no-id)'}`",
            f"Started: {ts}",
            f"Turns: {len(user_msgs)} user / {len(asst_msgs)} assistant",
            "",
            "## Opened with",
            "",
            first_user or "_(no user message)_",
            "",
            "## Last assistant turn",
            "",
            last_asst or "_(no assistant message)_",
            "",
            "## Notes",
            "",
            "_Auto-summary by basic-memory provider — refine as needed._",
            "",
        ]
        if self._session_note_id:
            lines += [
                "## Relations",
                "",
                f"- summary_of [[{self._session_note_id}]]",
                "",
            ]
        args = {
            "project": self._project,
            "title": title,
            "directory": self._capture_folder,
            "content": "\n".join(lines),
            "tags": ["hermes-session-summary", _hostname()],
            "output_format": "json",
        }
        try:
            self._actor.call("write_note", args, timeout=15.0)  # type: ignore[union-attr]
        except Exception as e:
            logger.warning("basic-memory: summary write failed: %s", e)

    # ---- Config wizard ----
    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "mode",
                "description": "Backend mode: local (writes to ~/.basic-memory/) or cloud",
                "default": "local",
                "choices": ["local", "cloud"],
            },
            {
                "key": "project",
                "description": "Basic Memory project name",
                "default": _default_project(),
            },
            {
                "key": "project_path",
                "description": "Filesystem path for the project (local mode)",
                "default": _default_project_path(),
            },
            {
                "key": "capture_per_turn",
                "description": "Auto-append every turn to a session transcript note",
                "default": "true",
                "choices": ["true", "false"],
            },
            {
                "key": "capture_session_end",
                "description": "Write a session summary note at end of session",
                "default": "true",
                "choices": ["true", "false"],
            },
            {
                "key": "capture_folder",
                "description": "BM folder where session notes land",
                "default": "hermes-sessions",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        coerced: Dict[str, Any] = {k: _coerce_bool(v) for k, v in values.items()}
        path = _config_path(hermes_home)
        existing: Dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except Exception:
                pass
        existing.update(coerced)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2))

    # ---- Circuit breaker ----
    def _record_failure(self, exc: BaseException) -> None:
        self._failure_count += 1
        if self._failure_count >= 5 and self._failure_pause_until == 0.0:
            self._failure_pause_until = time.monotonic() + 120.0
            logger.warning(
                "basic-memory circuit open for 120s after %d failures (last: %s)",
                self._failure_count,
                exc,
            )

    def _is_circuit_open(self) -> bool:
        if self._failure_pause_until and time.monotonic() < self._failure_pause_until:
            return True
        if self._failure_pause_until and time.monotonic() >= self._failure_pause_until:
            self._failure_count = 0
            self._failure_pause_until = 0.0
        return False


# ---------------------------------------------------------------------------
# atexit safety net (mirrors plugins/memory/openviking pattern)
# ---------------------------------------------------------------------------

_active_providers: List[BasicMemoryProvider] = []


def _atexit_cleanup() -> None:
    for p in list(_active_providers):
        try:
            p.shutdown()
        except Exception:
            pass


atexit.register(_atexit_cleanup)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx: Any) -> None:
    """Register basic-memory as a memory provider plugin."""
    provider = BasicMemoryProvider()
    _active_providers.append(provider)
    ctx.register_memory_provider(provider)

    # Bundle the user-facing skill so `hermes plugins install` wires it up
    # with the rest of the plugin. The skill is opt-in via
    # `skill:view basic-memory:basic-memory` — it's not in the auto-loaded
    # `<available_skills>` index. Always-on agent guidance still flows
    # through `system_prompt_block()`.
    skill_path = Path(__file__).resolve().parent / "skill" / "SKILL.md"
    if skill_path.exists() and hasattr(ctx, "register_skill"):
        try:
            ctx.register_skill(
                "basic-memory",
                skill_path,
                description=(
                    "Reference for using bm_* tools and the Basic Memory "
                    "knowledge graph (search-before-answer, capture decisions, "
                    "navigate via memory:// URLs)."
                ),
            )
        except Exception as e:
            logger.warning("basic-memory: register_skill failed: %s", e)
