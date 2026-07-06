"""Machine-local user-settings overlay I/O (config/user_settings.json).

Gitignored, NOT the committed runtime.json. The persistence home for user-chosen Settings
(UI port, memory / RAG / tool toggles, active project, optimizer choices); survives git pull.
"""
from __future__ import annotations

import json

from .paths import _USER_SETTINGS_PATH

def _load_user_settings() -> dict:
    """The machine-local overlay (may not exist yet)."""
    if _USER_SETTINGS_PATH.exists():
        try:
            return json.loads(_USER_SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_user_settings(data: dict) -> None:
    """Atomically persist the user-settings overlay."""
    data.setdefault(
        "_comment",
        "Machine-local Ayre user preferences (gitignored). Overrides defaults in "
        "config/runtime.json; survives git pull / updates. Edited by the UI.",
    )
    _USER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _USER_SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_USER_SETTINGS_PATH)
