"""CLI entry for the Ayre-UI shell.

    python -m ayre_ui                 # serve + open the browser
    python -m ayre_ui --no-browser    # serve only (e.g. headless VM)
    python -m ayre_ui --port P

Loopback-ONLY: the bind host is hard-locked to 127.0.0.1 in code (security; see
server.py -> _LOOPBACK_BIND_HOST) and cannot be changed via config or --host. The
port comes from config/runtime.json -> ui.port (variable-first) unless overridden
here.
"""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from .server import make_server, resolve_ui_address


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ayre-ui")
    parser.add_argument("--host", default=None,
                        help="(ignored) bind host is LOCKED to loopback for security; "
                             "see Remote Access in Build_Test_Future.md")
    parser.add_argument("--port", default=None, type=int, help="bind port (default: config ui.port)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = parser.parse_args(argv)

    host, port = resolve_ui_address(args.host, args.port)
    try:
        httpd = make_server(host=host, port=port)
    except OSError as exc:
        # Clean, actionable message instead of a raw traceback when the port is
        # busy or reserved (on Windows this is often WinError 10013/10048).
        print(f"Ayre-UI could not start: port {port} on {host} is unavailable.")
        print(f"  reason: {exc}")
        print("  Another program may be using it (possibly an Ayre that is already")
        print("  running), or the OS has reserved that port.")
        print("\nUse a different port:")
        print("  - run:  python -m ayre_ui --port 3000      (any 4-digit port)")
        print("  - or edit  config/runtime.json -> ui.port  and relaunch")
        print("  - or, if Ayre is already open, change it in Settings -> Connection")
        return 1

    host, port = httpd.server_address
    # Bind stays on the explicit loopback IP; show the friendlier hostname in the
    # URL we print and open (127.0.0.1 and localhost are the same loopback).
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::1") else host
    url = f"http://{display_host}:{port}/"
    print(f"Ayre is running -- open the UI at {url}")
    print("  Keep this window open while you use Ayre; it is Ayre's local server.")
    print("  Use the Stop button in the UI to unload the model (Ayre keeps running).")
    print("  Press Ctrl+C here to quit Ayre completely (this also closes the web UI).")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping ...")
    finally:
        httpd.server_close()
        print("  stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
