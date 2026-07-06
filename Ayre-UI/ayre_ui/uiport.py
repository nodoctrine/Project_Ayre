"""UI network config: the loopback bind lock + the user-configurable port.

The bind HOST is a hard security lock (loopback only -- the bridge has no auth); only the
PORT is user-configurable. Enabling remote access is a gated future feature (auth + TLS +
scoped bind first). Split from the server-construction code (netserver.py) so the handler
can read port config without importing the server that imports the handler.
"""
from __future__ import annotations

import socket

from ayre_setup.config import load_runtime

from .settings import _load_user_settings, _save_user_settings

DEFAULT_UI_PORT = 2500
PORT_MIN, PORT_MAX = 1000, 9999  # "4-digit localhost port"

# --- SECURITY (#1) Network-exposure lock -----------------------------------
# Ayre binds to loopback ONLY. The bridge has NO authentication on any endpoint:
# whoever can reach the port can chat with the model, upload files, start/stop the
# engine, and poison persistent memory. So the bind host is a HARD security lock,
# not a tunable -- any configured `ui.host` or `--host` value is deliberately
# ignored (see _ui_config / resolve_ui_address / make_server, all forced to this).
# The PORT stays user-configurable; only the HOST is locked.
# Enabling remote access is a gated future feature (auth + TLS + scoped bind first)
# -- see "Remote Access" in the project design notes before unlocking.
_LOOPBACK_BIND_HOST = "127.0.0.1"

def _ui_config() -> dict:
    """Effective UI host/port: runtime.json default, overlaid by the user's choice.
    Host is LOCKED to loopback (security) -- any configured ui.host is ignored; see
    _LOOPBACK_BIND_HOST. Only the port is user-configurable."""
    ui = load_runtime().get("ui", {})
    host = _LOOPBACK_BIND_HOST
    default_port = int(ui.get("port", DEFAULT_UI_PORT))
    port = default_port
    override = _load_user_settings().get("ui", {}).get("port")
    if isinstance(override, int) and PORT_MIN <= override <= PORT_MAX:
        port = override
    return {"host": host, "port": port, "default_port": default_port}


def validate_ui_port(port, current_port: int | None = None) -> str | None:
    """Return a user-facing error string, or None if the port is acceptable.

    Checks: 4-digit range, not the llama-server port, and actually bindable right
    now (the real 'is this one free?' answer). The currently-bound UI port is
    treated as available (it's in use BY us)."""
    if not isinstance(port, int):
        return "Port must be a whole number."
    if not (PORT_MIN <= port <= PORT_MAX):
        return f"Enter a 4-digit port ({PORT_MIN}-{PORT_MAX})."
    if port == current_port:
        return None  # already serving here; no-op
    llama_port = int(load_runtime().get("port", 8080))
    if port == llama_port:
        return f"Port {port} is reserved for llama-server -- pick another."
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return f"Port {port} is already in use -- pick another."
    finally:
        probe.close()
    return None


def save_ui_port(port: int) -> None:
    """Write the chosen port into the user_settings overlay (atomic)."""
    data = _load_user_settings()
    data.setdefault("ui", {})
    data["ui"]["port"] = port
    _save_user_settings(data)
