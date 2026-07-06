"""Workspace root + persistent memory (confirmed memory.md and the pending draft).

memory.md is human-approved and injected as a system message; memory_draft.md is
model-proposed and NEVER injected or read back by the model -- the promote step is the
trust boundary a human crosses. Also owns the workspace-root path both memory and projects
hang off, and the memory size thresholds (soft warning + hard cap).
"""
from __future__ import annotations

import datetime
from pathlib import Path

from ayre_setup.config import load_runtime

from .paths import _AYRE_USB_ROOT
from .settings import _load_user_settings, _save_user_settings
from . import state

def _workspace_path() -> Path:
    """Sandboxed folder the model can read/write (variable-first: path in runtime.json).
    Created on first access. The path is TRUSTED operator config: it resolves relative to
    _AYRE_USB_ROOT, but an absolute or '..' value in runtime.json may resolve elsewhere
    (intentional — e.g. a workspace on another drive). It is never attacker- or
    model-controlled; the per-project / per-file containment below is enforced against
    whatever this returns, so traversal via tool input still cannot escape it."""
    cfg = load_runtime().get("workspace", {}) or {}
    rel = cfg.get("path") or "ayre_workspace"
    p = (_AYRE_USB_ROOT / rel).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p

_MEMORY_FILENAME = "memory.md"              # confirmed, human-approved memory (injected as system)
_MEMORY_DRAFT_FILENAME = "memory_draft.md"  # model-proposed memory, pending user review; NEVER injected

_MEMORY_WARNING_CHARS_DEFAULT = 1500    # chars; overridable in Settings
_MEMORY_MAX_CHARS_DEFAULT = 100000      # hard cap on memory + draft size; overridable in config/runtime.json -> memory.max_chars

def _memory_path() -> Path:
    return _workspace_path() / _MEMORY_FILENAME


def _memory_draft_path() -> Path:
    return _workspace_path() / _MEMORY_DRAFT_FILENAME


def _memory_enabled() -> bool:
    return bool(_load_user_settings().get("memory", {}).get("enabled", True))


def _set_memory_enabled(enabled: bool) -> None:
    data = _load_user_settings()
    data.setdefault("memory", {})["enabled"] = bool(enabled)
    _save_user_settings(data)

def _memory_warning_chars() -> int:
    """Char count that triggers a 'memory is getting long' warning. Read from user_settings."""
    v = _load_user_settings().get("memory_warning_chars", _MEMORY_WARNING_CHARS_DEFAULT)
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = _MEMORY_WARNING_CHARS_DEFAULT
    return max(200, min(v, 50000))


def _save_memory_warning_chars(n: int) -> None:
    data = _load_user_settings()
    data["memory_warning_chars"] = n
    _save_user_settings(data)


def _memory_max_chars() -> int:
    """Hard ceiling (chars) on confirmed memory AND the staged draft. Read from
    config/runtime.json -> memory.max_chars (variable-first); default if absent.
    This is the safety backstop against runaway growth of always-injected memory;
    the soft warning threshold (_memory_warning_chars) is the separate user nudge."""
    cfg = load_runtime().get("memory", {}) or {}
    v = cfg.get("max_chars", _MEMORY_MAX_CHARS_DEFAULT)
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = _MEMORY_MAX_CHARS_DEFAULT
    return max(1000, v)

def _memory_content() -> str | None:
    """Return the current memory file contents, or None if no file exists."""
    p = _memory_path()
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _memory_draft_content() -> str | None:
    """Return the pending draft contents, or None if no draft exists. The draft is
    model-proposed and is NEVER injected into the prompt or readable by the model --
    it only surfaces in the UI for the user to review, edit, and promote."""
    p = _memory_draft_path()
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _memory_state() -> dict:
    enabled = _memory_enabled()
    content = _memory_content() if enabled else None
    draft = _memory_draft_content()
    return {
        "enabled": enabled,
        "has_content": content is not None,
        "char_count": len(content) if content else 0,
        "has_draft": draft is not None,
        "draft_char_count": len(draft) if draft else 0,
    }


def _clear_memory() -> dict:
    """Delete the CONFIRMED memory file (user-initiated, from the memory popover).
    Independent of the enabled toggle and of any pending draft -- clearing removes the
    saved (promoted) notes either way. Idempotent: clearing with nothing saved is a
    successful no-op."""
    p = _memory_path()
    existed = p.exists()
    if existed:
        try:
            p.unlink()
        except OSError as exc:
            return {"ok": False, "error": f"Could not clear memory: {exc}", **_memory_state()}
    return {"ok": True, "cleared": existed, **_memory_state()}


def _promote_draft(content: str) -> dict:
    """Copy USER-REVIEWED draft content into confirmed memory (user-initiated, from the
    review panel). `content` is what the user approved -- it may be edited from the raw
    model proposal. Appends below a timestamp separator (same accumulation model as the
    old auto-save), clears the draft, and resets the draft dedup guard. Because a human
    approved this exact text, it is trusted and is legitimately injected as a system
    message thereafter -- this is the trust boundary that closes the memory-injection
    hole (a model can stage a draft but can never write confirmed memory)."""
    content = (content or "").strip()
    if not content:
        return {"ok": False, "error": "Nothing to save -- the reviewed memory is empty.",
                **_memory_state()}
    cap = _memory_max_chars()
    mp = _memory_path()
    try:
        mp.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        existing = _memory_content()
        combined = (existing.rstrip() + f"\n\n--- {timestamp} ---\n" + content) if existing else content
        if len(combined) > cap:
            return {"ok": False,
                    "error": (f"Saving this would exceed the {cap:,}-character memory limit "
                              f"(would be {len(combined):,}). Trim the draft, or clear some saved memory first."),
                    **_memory_state()}
        mp.write_text(combined, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not save memory: {exc}", **_memory_state()}
    dp = _memory_draft_path()
    if dp.exists():
        try:
            dp.unlink()
        except OSError:
            pass  # draft promoted into memory; a lingering draft file is non-fatal
    state.last_draft_content = None
    total = len(combined)
    threshold = _memory_warning_chars()
    warning = None
    if total > threshold:
        warning = (f"Memory is getting long ({total} chars, limit {threshold}). "
                   f"Consider pruning it at: {mp}")
    return {"ok": True, "promoted_chars": len(content), "total_chars": total,
            "warning": warning, **_memory_state()}


def _discard_draft() -> dict:
    """Delete the pending draft without saving (user-initiated). Resets the draft dedup
    guard so a later identical proposal is not skipped. Idempotent."""
    dp = _memory_draft_path()
    existed = dp.exists()
    if existed:
        try:
            dp.unlink()
        except OSError as exc:
            return {"ok": False, "error": f"Could not discard draft: {exc}", **_memory_state()}
    state.last_draft_content = None
    return {"ok": True, "discarded": existed, **_memory_state()}
