"""Hardware monitor: the running model's GPU/CPU offload split + live temps/util.

The offload split (which layers ran where) is a launch-time decision llama-server does not
report back, so it is read from state.LAUNCH_INFO (the launch WE kicked off). Live temps /
utilization are read through throttled subprocesses (nvidia-smi; a WMI CPU-temp probe that
is dropped for good once a box proves it has no thermal zone).
"""
from __future__ import annotations

import time

from ayre_setup import platform_layer

from .llama import _llama_health
from . import state

def _offload_from_spec(spec) -> dict:
    """Pull the offload split out of a resolved LaunchSpec for the hardware monitor.
    Total layer count comes from the solver's fit summary when auto-tune ran, else
    a direct GGUF read (so it's known even with auto-tune off)."""
    total = None
    if spec.fit and isinstance(spec.fit.get("n_layers_total"), int):
        total = spec.fit["n_layers_total"]
    if total is None:
        total = _model_layers(spec.model_file.name)
    return {"model": spec.model_file.name,
            "n_gpu_layers": spec.n_gpu_layers,
            "n_layers_total": total,
            "context_tokens": spec.context_tokens}


_MODEL_LAYERS: dict = {}  # filename -> total transformer layers (GGUF read, memoized)


def _model_layers(name: str | None) -> int | None:
    """Total layer count for a model file in models/, read from GGUF metadata once
    and cached. None if the file is gone or unreadable."""
    if not name:
        return None
    if name in _MODEL_LAYERS:
        return _MODEL_LAYERS[name]
    total = None
    try:
        from ayre_setup.config import models_dir
        from ayre_setup.gguf import read_model_info
        path = models_dir() / name
        if path.exists():
            total = read_model_info(path).n_layers
    except Exception:  # noqa: BLE001 -- a bad read just means "unknown", never a crash
        total = None
    _MODEL_LAYERS[name] = total
    return total


def _offload_state(health: dict) -> dict | None:
    """The running model's GPU/CPU layer split for the monitor. Total layers is
    always derivable from the loaded model; the GPU layer count comes from the
    launch WE kicked off (state.LAUNCH_INFO) and is None ('split unknown') when the model
    was started elsewhere or the bridge has since restarted. None when down."""
    if not health.get("healthy"):
        return None
    loaded = health.get("model")
    total = _model_layers(loaded)
    n_gpu = None
    source = "unknown"
    if state.LAUNCH_INFO and (not loaded or state.LAUNCH_INFO.get("model") == loaded):
        n_gpu = state.LAUNCH_INFO.get("n_gpu_layers")
        total = state.LAUNCH_INFO.get("n_layers_total") or total
        source = "launch"
    if total is None and n_gpu is None:
        return None
    return {"model": loaded, "n_gpu_layers": n_gpu, "n_layers_total": total,
            "context_tokens": health.get("n_ctx"), "source": source}


# The live hardware sample (GPU/CPU temperature + utilization) is read through
# subprocesses (nvidia-smi; a PowerShell WMI query for CPU temp), so it's throttled
# behind a short TTL. The CPU *temperature* probe is dropped for good once a machine
# proves it has no thermal zone -- otherwise a recurring poll would spawn PowerShell
# every few seconds for nothing. CPU *load* has no such cost (ctypes/proc) and always
# runs. GPU temp+util come from a single nvidia-smi call (platform_layer.gpu_stats).
_HW_TTL_SECONDS = 4.0
_HW_CACHE: dict = {"at": 0.0,
                   "data": {"gpu_c": None, "cpu_c": None, "gpu_pct": None, "cpu_pct": None}}
_CPU_TEMP_AVAILABLE: bool | None = None  # None=untried, False=unsupported (stop probing)


def _read_hw_cached() -> dict:
    """{gpu_c, cpu_c, gpu_pct, cpu_pct}: GPU/CPU temperature (deg C) and utilization
    (%), None where unavailable. Throttled to one real probe per _HW_TTL_SECONDS so
    frequent polling never hammers nvidia-smi / the CPU-temp PowerShell call."""
    global _CPU_TEMP_AVAILABLE
    now = time.monotonic()
    if now - _HW_CACHE["at"] < _HW_TTL_SECONDS:
        return _HW_CACHE["data"]
    gpus = platform_layer.gpu_stats()
    gpu = gpus[0] if gpus else {}
    cpu_c = None
    if _CPU_TEMP_AVAILABLE is not False:  # skip once we know this box can't report it
        cpu_c = platform_layer.cpu_temperature_c()
        _CPU_TEMP_AVAILABLE = cpu_c is not None
    data = {"gpu_c": gpu.get("temp_c"), "cpu_c": cpu_c,
            "gpu_pct": gpu.get("util_pct"),
            "cpu_pct": platform_layer.cpu_utilization_pct()}
    _HW_CACHE.update(at=now, data=data)
    return data


def _telemetry_state() -> dict:
    """Live hardware monitor payload: is the engine up, the model's offload split (a
    launch-time decision: which layers run where), and the current live GPU/CPU
    temperature + utilization. Polled on its own cadence by the chat view."""
    health = _llama_health()
    hw = _read_hw_cached()
    return {"up": bool(health.get("healthy")),
            "offload": _offload_state(health),
            "temps": {"gpu_c": hw["gpu_c"], "cpu_c": hw["cpu_c"]},
            "load": {"gpu_pct": hw["gpu_pct"], "cpu_pct": hw["cpu_pct"]}}
