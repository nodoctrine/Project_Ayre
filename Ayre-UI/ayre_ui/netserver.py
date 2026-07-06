"""Server construction + the single-instance guard + running-instance detection.

make_server binds loopback unconditionally (the security lock, enforced at the actual bind).
_SingleInstanceHTTPServer refuses a second bind on Windows (SO_EXCLUSIVEADDRUSE) so a
duplicate launch fails loudly instead of silently double-binding. cli.py imports make_server /
resolve_ui_address / detect_running_instance from here.
"""
from __future__ import annotations

import socket
import sys
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from ayre_setup.config import load_runtime

from .uiport import _ui_config, _LOOPBACK_BIND_HOST
from .handler import AyreUIHandler

def resolve_ui_address(host: str | None = None, port: int | None = None) -> tuple[str, int]:
    """Effective (host, port). The PORT is user-configurable (explicit arg, else the
    config/overlay value). The HOST is LOCKED to loopback for security -- any
    configured ui.host or --host argument is intentionally ignored so the bridge can
    never be network-exposed without the gated Remote Access work. See
    _LOOPBACK_BIND_HOST. Exposed so the launcher can name the port in a clean error
    if the bind fails."""
    cfg = _ui_config()
    return (
        _LOOPBACK_BIND_HOST,
        int(port) if port is not None else cfg["port"],
    )


# --- Single-instance guard (Windows duplicate-bridge bug) -----------------
# http.server.HTTPServer defaults allow_reuse_address = 1 (SO_REUSEADDR), which
# ThreadingHTTPServer inherits. On Windows SO_REUSEADDR lets a SECOND process
# silently bind a port a live process already holds -- so a stray old bridge and a
# fresh launch can BOTH "run" on :2500 with no crash or warning, and a user who
# restarts Ayre may keep hitting a stale zombie ("my change isn't working"). Two
# layers close that:
#   1. _SingleInstanceHTTPServer drops SO_REUSEADDR on Windows and sets
#      SO_EXCLUSIVEADDRUSE instead, so the OS REFUSES a second bind -> make_server
#      raises OSError -> the CLI's clean "port unavailable" message + the launcher's
#      `if errorlevel 1 pause` fire as designed. This is the guarantee, and it also
#      settles the two-simultaneous-launches race. (Unix keeps SO_REUSEADDR: there it
#      does NOT allow stealing a live port, and dropping it would break the normal
#      restart-during-TIME_WAIT behaviour -- so the fix is Windows-scoped.)
#   2. detect_running_instance() (called by the CLI before bind) recognises an Ayre
#      bridge already on the port via its `Server: AyreUI/...` header and lets the CLI
#      print a specific "already running -- open http://localhost:PORT/" message.
#      Purely for a friendlier diagnosis; a missed detection still degrades safely to
#      layer 1's bind error.
_INSTANCE_PROBE_TIMEOUT_SECONDS = 0.5  # default; overridable via runtime.json -> ui.instance_probe_timeout_seconds


class _SingleInstanceHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that refuses to co-bind a port another live process holds.
    See the single-instance-guard note above for why this is Windows-scoped."""
    # Windows: don't inherit HTTPServer's SO_REUSEADDR (it enables the silent
    # double-bind). Unix: keep it (restart-friendly, no hijack risk there).
    allow_reuse_address = (sys.platform != "win32")

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Must be set BEFORE bind(): grants exclusive ownership of (host, port)
            # so no other socket -- this process or another -- can also bind it.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _instance_probe_timeout() -> float:
    """Startup-probe budget in seconds (variable-first; runtime.json -> ui)."""
    ui = load_runtime().get("ui", {}) or {}
    v = ui.get("instance_probe_timeout_seconds", _INSTANCE_PROBE_TIMEOUT_SECONDS)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return _INSTANCE_PROBE_TIMEOUT_SECONDS
    return v if v > 0 else _INSTANCE_PROBE_TIMEOUT_SECONDS


def detect_running_instance(port: int | None = None) -> str | None:
    """Is something already serving the UI port on loopback? Returns:
      "ayre"  -> an Ayre-UI bridge is already running there (its Server header says so),
      "other" -> some non-Ayre program holds the port,
      None    -> the port is free.
    Best-effort and fast: a bounded loopback connect, then a HEAD to read the Server
    header. Never raises. The exclusive-bind guard is the real guarantee; this only
    produces the clearer message, and a false 'free' still degrades to the bind error."""
    host = _LOOPBACK_BIND_HOST
    _, port = resolve_ui_address(None, port)
    timeout = _instance_probe_timeout()
    # Cheap connect first: nothing listening -> the port is free.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect((host, port))
    except OSError:
        return None
    finally:
        probe.close()
    # Something is listening -- identify whether it's us via the Server header.
    try:
        req = urllib.request.Request(f"http://{host}:{port}/", method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            server = resp.headers.get("Server", "") or ""
    except urllib.error.HTTPError as exc:  # a live server that 4xx/5xx'd a HEAD
        server = (exc.headers.get("Server", "") if exc.headers else "") or ""
    except Exception:  # noqa: BLE001 -- any hiccup: let the bind guard have the final say
        return "other"
    return "ayre" if "AyreUI" in server else "other"


def make_server(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    _, port = resolve_ui_address(host, port)
    # Bind loopback unconditionally -- the security lock, enforced at the actual
    # bind so it holds even if a caller passes a host. See _LOOPBACK_BIND_HOST.
    # _SingleInstanceHTTPServer refuses a second bind on Windows (SO_EXCLUSIVEADDRUSE)
    # so a duplicate launch fails loudly here instead of silently double-binding.
    return _SingleInstanceHTTPServer((_LOOPBACK_BIND_HOST, port), AyreUIHandler)
