"""llama-server process lifecycle (plan: backend wrapper #2).

Launch llama-server as a subprocess with computed flags, track the PID, expose
start/stop/restart, health-check the port before the UI connects, and shut down
cleanly on exit. Stdlib-only (no pip deps) so it runs in a clean offline VM.
"""
from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request

from . import platform_layer
from .config import LaunchSpec, build_launch_spec, load_runtime
from .preflight import preflight_launch


class LlamaServer:
    def __init__(self, spec: LaunchSpec):
        self.spec = spec
        self.proc: subprocess.Popen | None = None

    @classmethod
    def from_config(cls, tier: str | None = None, model_id: str | None = None) -> "LlamaServer":
        return cls(build_launch_spec(tier=tier, model_id=model_id))

    @property
    def base_url(self) -> str:
        return f"http://{self.spec.host}:{self.spec.port}"

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> int:
        if self.is_alive():
            raise RuntimeError("server already running")
        preflight_launch(self.spec)  # raises MissingArtifactError with an actionable message
        self.proc = subprocess.Popen(self.spec.argv(), **platform_layer.popen_kwargs())
        return self.proc.pid

    def health_ok(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=2) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def wait_until_healthy(self, timeout: float | None = None, poll_interval: float | None = None) -> bool:
        hc = load_runtime().get("health_check", {})
        timeout = timeout if timeout is not None else hc.get("timeout_seconds", 120)
        poll_interval = poll_interval if poll_interval is not None else hc.get("poll_interval_seconds", 1.0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_alive():
                raise RuntimeError("llama-server exited before becoming healthy")
            if self.health_ok():
                return True
            time.sleep(poll_interval)
        return False

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            platform_layer.terminate(self.proc)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def restart(self) -> int:
        self.stop()
        return self.start()

    def __enter__(self) -> "LlamaServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def stop_running_server() -> dict:
    """Stop whatever llama-server is serving the configured port, regardless of
    who launched it (UI, CLI, or a stray run). We don't keep a handle to the
    live process here -- the only universal way to stop it is by its port. The
    port is reserved for llama-server by design, so the owner is the engine.

    Returns a small status dict (ok / was_running / message) for the caller to
    surface; never raises for the common 'nothing to stop' case.
    """
    port = int(load_runtime().get("port", 8080))
    pids = platform_layer.find_listening_pids(port)
    if not pids:
        return {"ok": True, "was_running": False, "pids": [],
                "message": f"llama-server was not running (nothing on port {port})."}

    killed = [pid for pid in pids if platform_layer.terminate_pid(pid)]
    failed = [pid for pid in pids if pid not in killed]
    if failed:
        return {"ok": False, "was_running": True, "pids": pids, "killed": killed,
                "message": f"Could not stop process {failed} on port {port} "
                           f"(permission?). You may need to end it manually."}
    return {"ok": True, "was_running": True, "pids": pids, "killed": killed,
            "message": f"Stopped llama-server (pid {', '.join(map(str, killed))})."}
