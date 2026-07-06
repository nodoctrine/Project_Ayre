"""Project folders + per-project workspace files.

Each project is a flat sandbox folder under the workspace root (no subdirectories). Path
containment is enforced here (see _project_path): a project name that would resolve outside
the workspace root is rejected, and read-only GET paths never create directories (F1).
"""
from __future__ import annotations

import datetime
from pathlib import Path

from .memory import _workspace_path, _MEMORY_FILENAME, _MEMORY_DRAFT_FILENAME
from .settings import _load_user_settings, _save_user_settings

def _sanitize_filename(name: str) -> str | None:
    """Strip directory components and reject unsafe names. Returns None on failure."""
    safe = Path(name).name
    if not safe or safe in (".", ".."):
        return None
    forbidden = set('<>:"|?*\x00\\/\r\n')
    if any(c in safe for c in forbidden):
        return None
    return safe


def _workspace_file_list(project: str | None = None) -> list[dict]:
    """Files in the given project (or active project), sorted case-insensitively.
    Read-only: never creates the project dir. This is reachable from an un-guarded
    GET (/api/workspace/files?project=), so it must not mutate the filesystem (F1);
    a missing project simply lists empty."""
    try:
        wp = _project_path(project, create=False)
    except ValueError:
        return []
    if not wp.is_dir():
        return []
    files = []
    for f in sorted(wp.iterdir(), key=lambda x: x.name.lower()):
        if not f.is_file():
            continue
        stat = f.stat()
        modified = datetime.datetime.fromtimestamp(
            stat.st_mtime, tz=datetime.timezone.utc
        ).isoformat()
        files.append({"name": f.name, "size": stat.st_size, "modified": modified})
    return files


def _workspace_upload(name: str, data: bytes, project: str | None = None) -> dict:
    """Write bytes into the project's sandbox. Returns {ok, error?}.
    No size cap here — this is loopback to local disk. The real ceiling is
    the model's context window; huge files won't fit there (RAG, Slice 4)."""
    safe = _sanitize_filename(name)
    if not safe:
        return {"ok": False, "error": "Invalid filename."}
    try:
        (_project_path(project) / safe).write_bytes(data)
        return {"ok": True, "name": safe}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def _workspace_delete(name: str, project: str | None = None) -> dict:
    """Delete a file from the given project (or active project). Returns {ok, error?}."""
    safe = _sanitize_filename(name)
    if not safe:
        return {"ok": False, "error": "Invalid filename."}
    try:
        target = _project_path(project) / safe
    except ValueError:
        return {"ok": False, "error": "Invalid project."}
    if not target.is_file():
        return {"ok": False, "error": "File not found."}
    try:
        target.unlink()
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

_DEFAULT_PROJECT = "Default"

def _sanitize_project_name(name: str) -> str | None:
    """Validate a project folder name. Returns None on failure."""
    name = name.strip()
    if not name or len(name) > 64:
        return None
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if not all(c in allowed for c in name):
        return None
    return name


def _project_path(name: str | None = None, *, create: bool = True) -> Path:
    """Path to a project folder (default: active project). Creates it if needed,
    UNLESS create=False -- paths reachable from an un-guarded GET must not mutate the
    filesystem. A GET carries no CSRF guard, so an attacker-named ?project= must not
    be able to mkdir a directory in the workspace (see Security_Patch_Devlog F1).
    Raises ValueError if the resolved path would escape the workspace root."""
    wp = _workspace_path()
    proj = (name or _active_project()).strip()
    folder = (wp / proj).resolve()
    if folder.parent != wp:
        raise ValueError(f"Invalid project path: {proj!r}")
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def _active_project() -> str:
    """Active project name from user_settings.json (defaults to 'Default')."""
    return _load_user_settings().get("active_project") or _DEFAULT_PROJECT


def _set_active_project(name: str) -> None:
    data = _load_user_settings()
    data["active_project"] = name
    _save_user_settings(data)


def _migrate_flat_workspace() -> None:
    """One-time migration: move root-level workspace files into Default/.
    No-op once Default/ exists."""
    wp = _workspace_path()
    default_dir = wp / _DEFAULT_PROJECT
    if default_dir.exists():
        return
    default_dir.mkdir(exist_ok=True)
    for f in wp.iterdir():
        if f.is_file() and f.name not in (_MEMORY_FILENAME, _MEMORY_DRAFT_FILENAME):
            dest = default_dir / f.name
            if not dest.exists():
                f.rename(dest)


def _list_projects() -> list[dict]:
    """All project subfolders in the workspace, sorted. Always includes Default."""
    _migrate_flat_workspace()
    wp = _workspace_path()
    projects = []
    seen_default = False
    for d in sorted(wp.iterdir(), key=lambda x: x.name.lower()):
        if not d.is_dir():
            continue
        files = []
        for f in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            if not f.is_file():
                continue
            stat = f.stat()
            files.append({
                "name": f.name,
                "size": stat.st_size,
                "modified": datetime.datetime.fromtimestamp(
                    stat.st_mtime, tz=datetime.timezone.utc
                ).isoformat(),
            })
        projects.append({"name": d.name, "file_count": len(files), "files": files})
        if d.name == _DEFAULT_PROJECT:
            seen_default = True
    if not seen_default:
        _project_path(_DEFAULT_PROJECT)  # creates it
        projects.insert(0, {"name": _DEFAULT_PROJECT, "file_count": 0, "files": []})
    return projects


def _create_project(name: str) -> dict:
    """Create a new project subfolder. Returns {ok, name} or {ok: False, error}."""
    safe = _sanitize_project_name(name)
    if not safe:
        return {"ok": False,
                "error": "Invalid name. Use letters, numbers, hyphens, or underscores — no spaces (max 64 chars)."}
    wp = _workspace_path()
    folder = (wp / safe).resolve()
    if folder.parent != wp:
        return {"ok": False, "error": "Invalid project name."}
    if folder.exists():
        return {"ok": False, "error": f"'{safe}' already exists."}
    try:
        folder.mkdir()
        return {"ok": True, "name": safe}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
