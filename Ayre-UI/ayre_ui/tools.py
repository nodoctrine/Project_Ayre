"""Model-facing tools: definitions, per-tool toggles, and execution.

Every tool is gated twice (Security_Patch_Devlog #7/#9): the toggle / handoff-button gate
decides which tools are OFFERED (_active_tools), and _execute_tool re-checks the same gates
so a hallucinated call to a disabled/unoffered tool still refuses. write_file stages content
for a user Allow/Deny instead of writing immediately (default on).
"""
from __future__ import annotations

import datetime
import json
import re
import secrets
import threading
import time

from ayre_setup.config import load_runtime

from .settings import _load_user_settings, _save_user_settings
from .memory import (_memory_enabled, _memory_draft_path, _memory_draft_content,
                     _memory_content, _memory_max_chars, _MEMORY_FILENAME)
from .projects import (_project_path, _active_project, _workspace_file_list,
                       _sanitize_filename)
from . import state

_last_handoff_write_time: float = 0.0   # dedup: block save_handoff calls within cooldown window
# Write-File confirmation gate (Next_Features_July_1st.md Tier 2 ★): write_file stages
# its content here instead of writing immediately; the user must Allow it in the UI before
# the file is created. token -> {project, safe, content, char_count, created_at}. Lives in
# memory only (lost on restart -> a stale Allow simply reports "no longer pending"). Guarded
# by a lock because the confirm/deny endpoints run on a different thread (ThreadingHTTPServer)
# than the chat-proxy turn that staged the write.
_pending_writes: dict[str, dict] = {}
_pending_writes_lock = threading.Lock()
_PENDING_WRITES_MAX = 32                 # cap staged-but-unconfirmed writes (anti-runaway; a looping model can't fill memory)
_WRITE_PREVIEW_CHARS = 4000              # chars of staged content shown in the Allow/Deny card (the full content is still written on confirm)
_HANDOFF_COOLDOWN_DEFAULT = 180         # seconds; overridable in Settings

# Handoff files are immutable from the model's perspective once created.
# The pattern matches the name save_handoff produces: PROJECTNAME-HANDOFF_YYYY-MM-DD_HH-MM.md
_HANDOFF_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]+-HANDOFF_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}\.md$")

def _handoff_cooldown() -> int:
    """Seconds between allowed save_handoff writes. Read from user_settings."""
    v = _load_user_settings().get("handoff_cooldown_seconds", _HANDOFF_COOLDOWN_DEFAULT)
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = _HANDOFF_COOLDOWN_DEFAULT
    return max(30, min(v, 3600))


def _save_handoff_cooldown(seconds: int) -> None:
    data = _load_user_settings()
    data["handoff_cooldown_seconds"] = seconds
    _save_user_settings(data)


def _handoff_min_turns() -> int:
    """checkIfEmpty floor for the Handoff button: the minimum number of substantive
    assistant replies that must exist before a handoff can be generated. A ship-wide
    default from config/runtime.json -> handoff.min_substantive_turns (not per-user,
    unlike the cooldown). The browser enforces it before firing so an empty session
    spends no model turn; this call just delivers the configured value via /api/system."""
    cfg = load_runtime().get("handoff", {}) or {}
    try:
        v = int(cfg.get("min_substantive_turns", 1))
    except (TypeError, ValueError):
        return 1
    return max(0, min(v, 100))

# Tool definitions exposed to the model. Kept minimal for v1: save_memory
# (persistent notes across sessions), save_handoff (timestamped project note),
# and read_file (workspace file access).
_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Propose new facts for the user's long-term memory. IMPORTANT: this does NOT "
                "save memory directly — it stages a DRAFT the user must review and approve "
                "before it takes effect. Memory is USER-LEVEL (shared across ALL projects). "
                "Propose ONLY new facts not already saved; the server stages your content below "
                "a timestamp separator for the user to review. Do NOT re-state facts already in "
                "memory. Good to propose: user preferences, user role, persistent project "
                "decisions, ongoing context. Do NOT propose: conversation logs, model reasoning, "
                "session work (use save_handoff for that), or ephemeral task details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The new fact(s) to propose for memory. Only new information — do not repeat what is already saved. Staged as a draft for the user to review and approve.",
                    }
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": (
                "Read the current memory contents. Memory is USER-LEVEL — shared across ALL "
                "projects, not specific to the current session. "
                "IMPORTANT: Always call this tool when asked what you remember or about previous "
                "sessions — never answer from the conversation context alone, even if memory was "
                "injected at session start. "
                "Only call this mid-conversation to refresh; memory is loaded automatically at "
                "the start of each new session."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_handoff",
            "description": (
                "Save a session handoff note as a timestamped file in the active project folder. "
                "Call this ONCE at the end of a conversation to record what was worked on, "
                "decisions made, and where things left off. "
                "The file is automatically named handoff_YYYY-MM-DD_HH-MM.md — do not specify a path. "
                "Focus on THIS session's work only. Do NOT re-state facts from memory, user "
                "preferences, or background context — those are already preserved in memory.md "
                "and do not belong in a session handoff."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The handoff note (plain text or markdown). Write as much as needed to capture the session accurately — do not pad or truncate to hit an arbitrary word count.",
                    }
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List all files in the currently active project. "
                "Call this when the user asks what files are saved, or before reading a file "
                "when you are not sure of the exact filename. "
                "Returns names, sizes, and last-modified dates."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from the active project (a sandboxed flat folder on this machine — "
                "no subdirectories). Use list_files first if you are unsure of the exact filename. "
                "Cannot access files outside the active project."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Filename within the active project (e.g. 'notes.txt'). Cannot use '..' to escape.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write text content to a named file in the active project. "
                "Creates the file if it does not exist; overwrites if it does. "
                "Use this for documents, notes, ideas, or any named artifact the user asks you to create. "
                "Cannot write to memory.md (use save_memory for persistent session notes) "
                "and cannot write outside the active project."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Filename to write (e.g. 'game_idea.md'). Plain name only — no directories, no '..'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full text content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
]

_TOOL_META: dict[str, dict] = {
    "save_memory": {
        "label": "Propose Memory",
        "description": "Lets Ayre propose long-term facts and preferences as a draft for you to review and approve before anything is saved.",
        "warning": "Ayre won't be able to propose new memory for you to review.",
    },
    "save_handoff": {
        "label": "Save Handoff",
        "description": "Saves a timestamped handoff note to the active project folder at the end of a session.",
        "warning": "Ayre won't save handoff notes to your project folder.",
    },
    "read_memory": {
        "label": "Read Memory",
        "description": "Reads back saved memory mid-conversation when you ask what Ayre remembers.",
        "warning": "Ayre won't be able to recall previous session notes during a conversation.",
    },
    "list_files": {
        "label": "List Files",
        "description": "Lists all files in the active project when asked, so Ayre knows what's available to read.",
        "warning": "Ayre won't be able to see what files are in your active project.",
    },
    "read_file": {
        "label": "Read File",
        "description": "Reads a file's contents from the active project so Ayre can work with it in the conversation.",
        "warning": "Ayre won't be able to read any files from your active project.",
    },
    "write_file": {
        "label": "Write File",
        "description": "Creates or overwrites a named file in the active project.",
        "warning": "Ayre won't be able to create or save files to your active project.",
    },
}

def _tool_enabled(name: str) -> bool:
    """Is this specific tool enabled? Defaults to True if not configured."""
    return bool(_load_user_settings().get("tools", {}).get(name, {}).get("enabled", True))


def _set_tool_enabled(name: str, enabled: bool) -> None:
    data = _load_user_settings()
    data.setdefault("tools", {}).setdefault(name, {})["enabled"] = bool(enabled)
    _save_user_settings(data)


def _write_confirm_enabled() -> bool:
    """Is the Write-File confirmation gate on? Default ON (safety / user-control-is-core).
    Variable-first: the shipped default lives in runtime.json -> tools.write_file.confirm;
    a per-machine user choice in user_settings -> tools.write_file.confirm overrides it.
    When ON, write_file STAGES content for the user to Allow/Deny instead of writing
    immediately (no silent disk writes -- protect-end-user-hardware)."""
    rt_default = bool(load_runtime().get("tools", {}).get("write_file", {}).get("confirm", True))
    override = _load_user_settings().get("tools", {}).get("write_file", {}).get("confirm")
    return bool(override) if isinstance(override, bool) else rt_default


def _set_write_confirm(enabled: bool) -> None:
    data = _load_user_settings()
    data.setdefault("tools", {}).setdefault("write_file", {})["confirm"] = bool(enabled)
    _save_user_settings(data)


def _tools_state() -> list:
    """Current state of all tools for the Tools panel."""
    out = []
    for name, meta in _TOOL_META.items():
        row = {
            "name": name,
            "label": meta["label"],
            "description": meta["description"],
            "warning": meta["warning"],
            "enabled": _tool_enabled(name),
        }
        # write_file carries a secondary control: the confirmation gate (Allow/Deny
        # before each write). Surfaced so the Tools panel can render its sub-toggle.
        if name == "write_file":
            row["confirm"] = _write_confirm_enabled()
        out.append(row)
    return out


def _active_tools(allow_handoff: bool = False) -> list:
    """Tool definitions to expose to the model.
    Respects per-tool toggles; memory tools also require the memory chip to be on.
    SECURITY (#7): save_handoff is offered ONLY on a Handoff-button turn (allow_handoff=True): a button
    press is the unambiguous trust signal, so the model can never write a handoff on its
    own judgment or from injected 'do a handoff'-style data (Security_Patch_Devlog #7).
    It still also honours the per-tool toggle."""
    mem_ok = _memory_enabled()
    result = []
    for t in _TOOL_DEFINITIONS:
        name = t["function"]["name"]
        if not _tool_enabled(name):
            continue
        if name in ("save_memory", "read_memory") and not mem_ok:
            continue
        if name == "save_handoff" and not allow_handoff:
            continue
        result.append(t)
    return result

# Per-tool hint lines injected into the system prompt so the model knows it can
# actually call these tools. write_file's hint is the most critical: without an
# explicit instruction, models assume they cannot write files.
_TOOL_HINTS: dict[str, str] = {
    "write_file": (
        "write_file(path, content) — creates or overwrites a file in the active project. "
        "Call this whenever the user asks you to write, save, or create a file. "
        "Write the EXACT content the user provided or that you generated for them — "
        "never substitute, summarize, or use content from memory or previous sessions. "
        "Do NOT output the file content in the chat — call the tool instead."
    ),
    "read_file": "read_file(path) — reads a file from the active project. Return the EXACT contents — never summarize, paraphrase, or interpret.",
    "list_files": "list_files() — lists all files in the active project.",
    "read_memory": (
        "read_memory() — reads saved memory (user-level, shared across ALL projects). "
        "ALWAYS call this tool when the user asks what you remember — never answer from conversation context alone."
    ),
    "save_memory": (
        "save_memory(content) — PROPOSES a new fact for user-level memory (shared across ALL projects) "
        "as a draft the USER must review and approve; it does NOT save directly. "
        "Propose ONLY new facts — do not re-state what is already in memory. "
        "For: user preferences, role, persistent decisions. NOT session work (use save_handoff)."
    ),
    "save_handoff": (
        "save_handoff(content) — saves a timestamped session summary to the project folder. "
        "Call this ONCE at the end of a session. "
        "Session work only — do not re-state memory or user preferences."
    ),
}


# Markdown-rendering guidance for the chat. The UI's renderer — like EVERY CommonMark
# renderer — cannot disambiguate same-length nested code fences, so a ``` block wrapped
# inside another ``` block renders with its regions inverted (headings land inside the
# box, code lands outside) plus a stray empty block. Steer the model away from emitting
# that shape so its output stays renderable. See the fenced-code notes in static/app.js.
_FORMATTING_RULE = (
    "\n\nFormatting your replies: put code in a fenced ``` block tagged with its language. "
    "NEVER place a ``` code block inside another ``` block — same-length nested fences "
    "render incorrectly. To show the contents of a markdown or text file that itself "
    "contains ``` fences, output those fenced blocks directly WITHOUT wrapping them in an "
    "outer ``` fence (if you must wrap, use a ~~~ tilde fence as the outer wrapper). After "
    "writing a file, confirm briefly — do not paste the whole file body back into the chat."
)

def _safe_parse_args(arguments_str: str) -> dict:
    if not arguments_str:
        return {}
    try:
        return json.loads(arguments_str)
    except (json.JSONDecodeError, ValueError):
        return {}


def _stage_write(project: str, safe: str, content: str) -> dict:
    """Stage a write_file call for the user to confirm. Returns a tool result that tells the
    model the write is PENDING (so it never claims success). Dedups an identical pending
    (project, filename, content) so a looping model produces ONE card, not many."""
    char_count = len(content)
    with _pending_writes_lock:
        for rec in _pending_writes.values():
            if rec["project"] == project and rec["safe"] == safe and rec["content"] == content:
                # Already awaiting approval -- no write_pending flag => no second card.
                return {"ok": True, "result": (
                    f"The write to '{safe}' is already staged and waiting for the user to "
                    "approve it. Do not call write_file again — wait for the user.")}
        if len(_pending_writes) >= _PENDING_WRITES_MAX:
            return {"ok": False, "result": (
                "Too many writes are already waiting for the user to approve. Ask the user to "
                "approve or deny the pending ones before requesting another.")}
        token = secrets.token_urlsafe(16)
        _pending_writes[token] = {
            "project": project, "safe": safe, "content": content,
            "char_count": char_count, "created_at": time.time(),
        }
    return {"ok": True,
            "result": (
                f"The write to '{safe}' ({char_count} chars) in project '{project}' has been "
                "staged and is waiting for the user to approve it in Ayre. It has NOT been "
                f"written yet. Tell the user you've requested to write '{safe}' and that they "
                "need to approve it; do not claim the file already exists."),
            "write_pending": True,
            "pending": {"token": token, "path": safe, "char_count": char_count, "project": project,
                        "preview": content[:_WRITE_PREVIEW_CHARS],
                        "truncated": char_count > _WRITE_PREVIEW_CHARS}}


def _confirm_pending_write(token: str) -> dict:
    """User approved a staged write -> perform it now. Re-runs the filename guards as defense
    in depth (active project / disk state may have changed since the write was staged)."""
    with _pending_writes_lock:
        rec = _pending_writes.pop(token, None)
    if rec is None:
        return {"ok": False, "error": "This write request is no longer pending."}
    safe = _sanitize_filename(rec["safe"])
    if not safe or safe == _MEMORY_FILENAME:
        return {"ok": False, "error": "This file can no longer be written here."}
    try:
        dest = _project_path(rec["project"]) / safe
    except ValueError:
        return {"ok": False, "error": "The target project is no longer available."}
    if _HANDOFF_FILENAME_RE.match(safe) and dest.exists():
        return {"ok": False, "error": "Handoff files are immutable and cannot be overwritten."}
    try:
        dest.write_text(rec["content"], encoding="utf-8")
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"Could not write: {exc}"}
    return {"ok": True, "filename": safe, "project": rec["project"], "char_count": rec["char_count"]}


def _deny_pending_write(token: str) -> dict:
    """User denied a staged write -> discard it. Idempotent: a double-click (or a token that
    already fired) still returns ok so the UI settles cleanly."""
    with _pending_writes_lock:
        _pending_writes.pop(token, None)
    return {"ok": True}


def _execute_tool(name: str, arguments: dict, *, allow_handoff: bool = False) -> dict:
    """Execute a tool call; return {"ok": bool, "result": str}."""
    try:
        # SECURITY (#9) Defense in depth: the per-tool toggle gates which
        # tools are OFFERED to the model (_active_tools), but a model can hallucinate a
        # call to a tool it wasn't offered -- so a disabled tool must also refuse to
        # EXECUTE. Mirrors the save_handoff / memory re-checks below. Tools absent from
        # the toggle set default to enabled, so this only blocks ones the user explicitly
        # turned off in the Tools panel.
        if not _tool_enabled(name):
            return {"ok": False, "result": f"The {name!r} tool is turned off in Settings."}
        if name == "save_memory":
            content = arguments.get("content", "")
            if not isinstance(content, str):
                return {"ok": False, "result": "content must be a string"}
            if not _memory_enabled():
                return {"ok": False, "result": "Memory is disabled — enable it in the chat header first."}
            content = content.strip()
            if not content:
                return {"ok": False, "result": "content must not be empty"}
            if content == state.last_draft_content:
                return {"ok": True, "result": "Already proposed — skipped duplicate."}
            # Write to the DRAFT, never to confirmed memory. The draft is staged for the
            # user to review/edit/approve in the UI; it is never injected into the prompt
            # and cannot be read back by the model. Accumulate so multiple proposals in a
            # session are preserved until the user reviews them together. This is the
            # security boundary: a model can propose, only a human can confirm.
            dp = _memory_draft_path()
            try:
                dp.parent.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
                existing = _memory_draft_content()
                combined = (existing.rstrip() + f"\n\n--- {timestamp} ---\n" + content) if existing else content
                cap = _memory_max_chars()
                if len(combined) > cap:
                    return {"ok": False, "result": (
                        f"The memory draft would exceed the {cap}-character limit. Propose a "
                        "shorter note, or ask the user to review and clear the pending draft first.")}
                dp.write_text(combined, encoding="utf-8")
            except OSError as exc:
                return {"ok": False, "result": f"Could not stage memory draft: {exc}"}
            state.last_draft_content = content
            return {"ok": True,
                    "result": ("Saved as a draft for the user to review. It is NOT yet in "
                               "memory and only takes effect once the user approves it. "
                               "Do not repeat this proposal."),
                    "draft_pending": True}

        if name == "save_handoff":
            global _last_handoff_write_time
            # SECURITY (#7) Defense in depth: save_handoff is only OFFERED on
            # a Handoff-button turn, but a model can hallucinate a call to a tool it wasn't
            # offered -- so refuse to execute unless this turn is button-flagged.
            if not allow_handoff:
                return {"ok": False, "result": "save_handoff runs only from the Handoff button."}
            content = arguments.get("content", "")
            if not isinstance(content, str):
                return {"ok": False, "result": "content must be a string"}
            # Cooldown guard: reasoning models loop and call this repeatedly.
            # Return success (not an error) so the model stops retrying.
            now_ts = time.time()
            if now_ts - _last_handoff_write_time < _handoff_cooldown():
                return {"ok": True, "result": "Handoff already saved — skipped duplicate write."}
            now = datetime.datetime.utcnow()
            project_slug = _active_project().replace(" ", "-")
            filename = f"{project_slug}-HANDOFF_{now.strftime('%Y-%m-%d_%H-%M')}.md"
            try:
                proj_path = _project_path()
            except ValueError as exc:
                return {"ok": False, "result": str(exc)}
            dest = proj_path / filename
            dest.write_text(content, encoding="utf-8")
            _last_handoff_write_time = now_ts
            return {"ok": True, "result": f"Handoff saved as '{dest.name}' in '{_active_project()}' ({len(content)} chars)."}

        if name == "list_files":
            files = _workspace_file_list()
            if not files:
                return {"ok": True, "result": f"The active project '{_active_project()}' has no files yet."}
            lines = [f"{f['name']}  ({f['size']} bytes, modified {f['modified']})" for f in files]
            return {"ok": True, "result": "\n".join(lines)}

        if name == "read_memory":
            if not _memory_enabled():
                return {"ok": False, "result": "Memory is disabled — enable it in the chat header first."}
            content = _memory_content()
            if content is None:
                return {"ok": True, "result": "No memory saved yet."}
            return {"ok": True, "result": content}

        if name == "read_file":
            rel = (arguments.get("path") or "").strip()
            if not rel:
                return {"ok": False, "result": "path must be a non-empty string"}
            try:
                wp = _project_path()
            except ValueError as exc:
                return {"ok": False, "result": str(exc)}
            target = (wp / rel).resolve()
            try:
                target.relative_to(wp)
            except ValueError:
                return {"ok": False, "result": "Access denied: path is outside the active project."}
            if not target.is_file():
                return {"ok": False, "result": f"File not found: {rel}"}
            try:
                raw = target.read_text(encoding="utf-8", errors="replace")
                return {
                    "ok": True,
                    "result": (
                        "[Return the following file contents VERBATIM to the user — "
                        "do not summarize, paraphrase, or interpret. Present exactly as shown.]\n\n"
                        + raw
                    ),
                    "display": f"Read: {rel} ({len(raw)} chars)",
                }
            except OSError as exc:
                return {"ok": False, "result": f"Could not read: {exc}"}

        if name == "write_file":
            rel = (arguments.get("path") or "").strip()
            content = arguments.get("content", "")
            if not rel:
                return {"ok": False, "result": "path must be a non-empty string"}
            if not isinstance(content, str):
                return {"ok": False, "result": "content must be a string"}
            safe = _sanitize_filename(rel)
            if not safe:
                return {"ok": False, "result": f"Invalid filename: {rel!r}"}
            if safe == _MEMORY_FILENAME:
                return {"ok": False, "result": "Cannot write to memory.md via write_file — use save_memory instead."}
            if _HANDOFF_FILENAME_RE.match(safe) and (_project_path() / safe).exists():
                return {"ok": False, "result": f"'{safe}' is a handoff file — handoffs are immutable once saved and cannot be modified by the model."}
            project = _active_project()
            # Confirmation gate (default ON, Tier 2 ★): stage the write for the user to
            # Allow/Deny instead of touching disk now. The model is told the write is
            # PENDING so it narrates "I've requested..." rather than claiming the file
            # exists. Gate OFF preserves the original immediate-write behaviour.
            if _write_confirm_enabled():
                return _stage_write(project, safe, content)
            try:
                (_project_path() / safe).write_text(content, encoding="utf-8")
                return {"ok": True, "result": f"Written to {safe} in '{project}' ({len(content)} chars)."}
            except (OSError, ValueError) as exc:
                return {"ok": False, "result": f"Could not write: {exc}"}

        return {"ok": False, "result": f"Unknown tool: {name}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "result": f"Tool error: {exc}"}
