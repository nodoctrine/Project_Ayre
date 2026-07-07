"""Platform seam -- the single place OS-specific branches live.

v1 targets Windows. Every function here is shaped so a Linux / macOS port swaps
implementations *in this module only*, leaving Setup/Framework/RAG/UI untouched
(plan: "## Cross-platform planning -> The platform-layer seam").
"""
from __future__ import annotations

import os
import platform
import signal
import subprocess
from pathlib import Path


# ============================================================================
# CONTENTS
#   1 · OS identity & paths        - current_os, ayre_usb_root
#   2 · Process launch & lifecycle - llama_server_binary_name, popen_kwargs,
#                                    terminate, find_listening_pids, terminate_pid
#   3 · Hardware probe seam        - memory_bytes, logical_cpu_count, detect_gpus
#   4 · Live temperature seam      - gpu_stats, cpu_temperature_c (+ wrappers)
#   5 · Live utilization seam      - cpu_utilization_pct
# ============================================================================

# Subprocess wall-clock caps (seconds). Every external probe is bounded so a
# hung tool can't stall Setup; PowerShell gets a longer budget for its slower
# cold start. Variable-first: tune here, not at the call sites.
_PORT_QUERY_TIMEOUT_S = 10       # netstat / lsof (Stop's port-owner lookup)
_GPU_QUERY_TIMEOUT_S = 10        # nvidia-smi (detect + live stats)
_POWERSHELL_TIMEOUT_S = 15       # powershell / Get-CimInstance (WMI, thermal)

# CPU-temperature sanity window (degrees C). Readings outside this band are
# rejected as placeholder / garbage rather than shown. Shared by the Windows
# (ACPI) and POSIX (/sys) temperature paths.
_CPU_TEMP_MIN_C = 0
_CPU_TEMP_MAX_C = 150

# Windows "Display adapters" device-class key (a fixed OS-defined GUID) -- the
# registry path detect_gpus() walks for per-adapter VRAM.
_DISPLAY_ADAPTER_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"


# --- 1 · OS identity & paths ------------------------------------------------
def current_os() -> str:
    """'Windows', 'Linux', or 'Darwin'."""
    return platform.system()


def ayre_usb_root() -> Path:
    """The Ayre-USB top-level folder.

    Honors an explicit AYRE_USB_ROOT override (the USB may mount at any drive
    letter / path); otherwise derives from this file's location:
    <root>/Ayre-Setup/ayre_setup/platform_layer.py -> parents[2] == <root>.
    """
    override = os.environ.get("AYRE_USB_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2]


# --- 2 · Process launch & lifecycle -----------------------------------------
def llama_server_binary_name() -> str:
    """Platform-correct llama-server executable name."""
    return "llama-server.exe" if current_os() == "Windows" else "llama-server"


def popen_kwargs() -> dict:
    """OS-specific Popen kwargs for launching llama-server.

    Windows has no POSIX signals; a new process group lets us terminate the
    server (and only it) cleanly. POSIX gets its own session for the same reason.
    """
    if current_os() == "Windows":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate(proc: subprocess.Popen) -> None:
    """Request a clean shutdown in an OS-appropriate way."""
    if proc.poll() is not None:
        return
    proc.terminate()


def find_listening_pids(port: int) -> list[int]:
    """PIDs of the process(es) LISTENING on a local TCP port.

    Used by Stop: the bridge holds a handle to the CLI *wrapper*, not to
    llama-server itself (its grandchild, in its own process group), and the
    server may also have been launched independently from a terminal. Finding
    the port owner is the one path that stops it in every case. OS-specific, so
    it lives here behind the platform seam.
    """
    if current_os() == "Windows":
        return _find_listening_pids_windows(port)
    return _find_listening_pids_posix(port)


def _find_listening_pids_windows(port: int) -> list[int]:
    """Port owners via `netstat -ano` (Windows)."""
    try:
        # SECURITY: fixed argv, no shell=True; bounded timeout; [] on failure.
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=_PORT_QUERY_TIMEOUT_S,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        # Proto  Local-Address  Foreign-Address  State  PID
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].rsplit(":", 1)[-1] == str(port):
                try:
                    pids.add(int(parts[-1]))
                except ValueError:
                    pass
    return sorted(pids)


def _find_listening_pids_posix(port: int) -> list[int]:
    """Port owners via `lsof` -- the most portable one-liner (POSIX; best-effort,
    Linux/macOS are post-v1)."""
    try:
        # SECURITY: argv-list, no shell=True; `port` is an int, not shell-
        # interpolated; bounded timeout; [] on failure.
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=_PORT_QUERY_TIMEOUT_S,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: set[int] = set()
    for line in out.split():
        try:
            pids.add(int(line))
        except ValueError:
            pass
    return sorted(pids)


def terminate_pid(pid: int) -> bool:
    """Terminate a process by PID. True if the signal was delivered.

    On Windows os.kill(pid, SIGTERM) maps to TerminateProcess -- the same hard
    stop proc.terminate() already uses for llama-server, which holds no state to
    flush. POSIX gets a normal SIGTERM.
    """
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, ProcessLookupError):
        return False


# --- 3 · Hardware probe seam ------------------------------------------------
# Raw, OS-specific numbers for the optimizer's machine profile. Kept here (not
# in hardware.py) so a Linux/macOS port swaps only these. Stdlib-only: ctypes
# for RAM, external `nvidia-smi` / Windows registry / WMI for VRAM. Every
# function degrades to a safe default (0 / []) rather than raising, so a probe
# on an exotic box still returns *something* with a warning attached upstream.

def _windows_mem() -> tuple[int, int]:
    """(total_bytes, available_bytes) via GlobalMemoryStatusEx."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return 0, 0
    return int(stat.ullTotalPhys), int(stat.ullAvailPhys)


def _posix_mem() -> tuple[int, int]:
    """(total_bytes, available_bytes); available best-effort from /proc/meminfo."""
    total = 0
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        total = 0
    available = 0
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024  # kB -> bytes
                    break
    except (OSError, ValueError, IndexError):
        available = 0
    return total, available


def memory_bytes() -> tuple[int, int]:
    """(total_bytes, available_bytes) of system RAM."""
    if current_os() == "Windows":
        return _windows_mem()
    return _posix_mem()


def logical_cpu_count() -> int:
    """Logical CPU count (0 if the platform won't report it)."""
    return os.cpu_count() or 0


def _vendor_from_name(name: str) -> str:
    """Best-effort GPU vendor label from an adapter name string."""
    n = (name or "").lower()
    if any(k in n for k in ("nvidia", "geforce", "rtx", "gtx", "quadro", "tesla")):
        return "NVIDIA"
    if any(k in n for k in ("amd", "radeon", "rx ")):
        return "AMD"
    if "intel" in n:
        return "Intel"
    return "Unknown"


def _gpus_nvidia_smi() -> list[dict]:
    """NVIDIA via nvidia-smi -- the only source that also reports FREE VRAM
    (what conservative budgeting wants)."""
    try:
        # SECURITY: fixed argv, no shell=True; bounded timeout; [] on failure.
        res = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_GPU_QUERY_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    gpus = []
    for line in res.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total = int(round(float(parts[1]))) * 1024 * 1024  # MiB -> bytes
            free = int(round(float(parts[2]))) * 1024 * 1024
        except ValueError:
            continue
        gpus.append({"name": parts[0], "vendor": "NVIDIA",
                     "vram_total_bytes": total, "vram_free_bytes": free,
                     "source": "nvidia-smi"})
    return gpus


def _gpus_windows_registry() -> list[dict]:
    """Any vendor via the display-adapter class key. qwMemorySize is the
    accurate total VRAM (unlike WMI's AdapterRAM, capped at 4GB). No free."""
    import winreg

    base = rf"SYSTEM\CurrentControlSet\Control\Class\{_DISPLAY_ADAPTER_CLASS_GUID}"
    gpus = []
    try:
        cls = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return []
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(cls, i)
        except OSError:
            break
        i += 1
        if not sub.isdigit():
            continue
        try:
            sk = winreg.OpenKey(cls, sub)
        except OSError:
            continue
        try:
            name = winreg.QueryValueEx(sk, "DriverDesc")[0]
        except OSError:
            winreg.CloseKey(sk)
            continue
        vram = None
        try:
            raw = winreg.QueryValueEx(sk, "HardwareInformation.qwMemorySize")[0]
            vram = int.from_bytes(raw, "little") if isinstance(raw, bytes) else int(raw)
        except (OSError, ValueError, TypeError):
            vram = None
        winreg.CloseKey(sk)
        if vram and vram > 0:
            gpus.append({"name": name, "vendor": _vendor_from_name(name),
                         "vram_total_bytes": vram, "vram_free_bytes": None,
                         "source": "registry"})
    winreg.CloseKey(cls)
    return gpus


def _gpus_windows_wmi() -> list[dict]:
    """Last resort: WMI AdapterRAM. NOTE: a uint32, so it caps/wraps above 4GB
    -- flagged unreliable upstream. Used only when nothing better answered."""
    try:
        # SECURITY: fixed argv, no shell=True; static PS command, no interpolated
        # input; bounded timeout; [] on failure.
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_VideoController | "
             "ForEach-Object { \"$($_.Name)|$($_.AdapterRAM)\" }"],
            capture_output=True, text=True, timeout=_POWERSHELL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    gpus = []
    for line in res.stdout.splitlines():
        if "|" not in line:
            continue
        name, _, ram = line.rpartition("|")
        name = name.strip()
        try:
            vram = int(ram.strip())
        except ValueError:
            vram = 0
        gpus.append({"name": name, "vendor": _vendor_from_name(name),
                     "vram_total_bytes": vram, "vram_free_bytes": None,
                     "source": "wmi"})
    return gpus


def detect_gpus() -> list[dict]:
    """Vendor-agnostic GPU list. Tries the most accurate source first and stops
    at the first that answers: nvidia-smi (accurate + free) -> Windows registry
    (accurate total) -> WMI (unreliable fallback). Each entry:
    {name, vendor, vram_total_bytes, vram_free_bytes|None, source}."""
    gpus = _gpus_nvidia_smi()
    if gpus:
        return gpus
    if current_os() == "Windows":
        gpus = _gpus_windows_registry()
        if gpus:
            return gpus
        return _gpus_windows_wmi()
    return []


# --- 4 · Live temperature seam ----------------------------------------------
# For the UI hardware monitor (protect-end-user-hardware: a glanceable thermal
# read while a load runs). Best-effort + stdlib-only; any unavailable reading is
# None so the UI shows "--" honestly instead of guessing. OS-specific, so it lives
# behind the platform seam alongside the other probes.

def _num_or_none(parts: list[str], i: int) -> int | None:
    """parts[i] rounded to an int, or None if missing / non-numeric ('[N/A]' shows
    up for fields some adapters don't report)."""
    try:
        return int(round(float(parts[i])))
    except (ValueError, IndexError):
        return None


def gpu_stats() -> list[dict]:
    """Per-GPU live stats from ONE nvidia-smi call: {temp_c, util_pct} (NVIDIA only;
    same command on Windows + Linux). [] when nvidia-smi is absent or no NVIDIA GPU.
    Querying temperature + utilization together keeps the monitor to a single
    subprocess per poll. Either field is None if its column came back non-numeric."""
    try:
        # SECURITY: fixed argv, no shell=True; bounded timeout; [] on failure.
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_GPU_QUERY_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    out: list[dict] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        out.append({"temp_c": _num_or_none(parts, 0), "util_pct": _num_or_none(parts, 1)})
    return out


def gpu_temperatures_c() -> list[int]:
    """Per-GPU temperature in degrees C (NVIDIA via nvidia-smi). Thin wrapper over
    gpu_stats() for callers that only need temperature."""
    return [g["temp_c"] for g in gpu_stats() if g.get("temp_c") is not None]


def cpu_temperature_c() -> int | None:
    """Best-effort CPU/package temperature in degrees C. None when the platform
    doesn't expose it (common on consumer Windows laptops -- the ACPI thermal zone
    is often 'not supported' or admin-gated; callers must degrade to '--')."""
    if current_os() == "Windows":
        return _cpu_temp_windows()
    return _cpu_temp_posix()


def _cpu_temp_windows() -> int | None:
    """ACPI thermal zone via WMI (CurrentTemperature is tenths of a Kelvin). Many
    machines return nothing here; that's expected and surfaces as None."""
    try:
        # SECURITY: fixed argv, no shell=True; static PS command, no interpolated
        # input; bounded timeout; None on failure.
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance -Namespace root/wmi -ClassName "
             "MSAcpi_ThermalZoneTemperature -ErrorAction Stop | "
             "Select-Object -First 1).CurrentTemperature"],
            capture_output=True, text=True, timeout=_POWERSHELL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (res.stdout or "").strip()
    try:
        tenths_kelvin = int(raw)
    except ValueError:
        return None
    celsius = tenths_kelvin / 10.0 - 273.15
    if celsius <= _CPU_TEMP_MIN_C or celsius > _CPU_TEMP_MAX_C:  # reject garbage
        return None
    return int(round(celsius))


def _cpu_temp_posix() -> int | None:
    """Linux: first sane reading from /sys/class/thermal (millidegrees C)."""
    import glob as _glob
    for zone in sorted(_glob.glob("/sys/class/thermal/thermal_zone*/temp")):
        try:
            with open(zone, encoding="utf-8") as fh:
                celsius = int(fh.read().strip()) / 1000.0
        except (OSError, ValueError):
            continue
        if _CPU_TEMP_MIN_C < celsius <= _CPU_TEMP_MAX_C:
            return int(round(celsius))
    return None


# --- 5 · Live utilization seam ----------------------------------------------
# CPU/GPU *load* for the monitor's LOAD row -- the "how hard is each chip working
# right now" the offload split can't show (the split is a launch-time decision; a
# model with few CPU layers can still peg the CPU). GPU% rides along on gpu_stats()
# (one nvidia-smi call). CPU% is a delta between successive samples of cumulative
# busy/idle counters (no subprocess: ctypes GetSystemTimes on Windows, /proc/stat on
# Linux), so the FIRST call only sets a baseline and returns None -- the next real
# call (a poll later) reports the average load over that interval.

_WIN_CPU_PREV: tuple[int, int, int] | None = None   # (idle, kernel, user) 100ns ticks
_POSIX_CPU_PREV: tuple[int, int] | None = None       # (total, idle) jiffies


def cpu_utilization_pct() -> int | None:
    """System-wide CPU load as an int percent 0-100, or None until a baseline exists
    (first call) / on an unreadable platform. Averaged over the gap since the last
    call, which the monitor polls on a fixed cadence."""
    if current_os() == "Windows":
        return _cpu_util_windows()
    return _cpu_util_posix()


def _cpu_util_windows() -> int | None:
    """Delta of GetSystemTimes (kernel time INCLUDES idle here): busy = (dKernel +
    dUser) - dIdle over their sum. ctypes only -- no process spawned per poll."""
    import ctypes
    global _WIN_CPU_PREV

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    idle, kern, user = FILETIME(), FILETIME(), FILETIME()
    ok = ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    if not ok:
        return None

    def q(ft: "FILETIME") -> int:
        return (ft.high << 32) | ft.low

    cur = (q(idle), q(kern), q(user))
    prev, _WIN_CPU_PREV = _WIN_CPU_PREV, cur
    if prev is None:
        return None  # baseline only; the next call has a delta to measure
    d_idle, d_kern, d_user = (cur[0] - prev[0], cur[1] - prev[1], cur[2] - prev[2])
    total = d_kern + d_user  # kernel already counts idle, so this is all CPU time
    if total <= 0:
        return None
    return max(0, min(100, int(round(100.0 * (total - d_idle) / total))))


def _cpu_util_posix() -> int | None:
    """Delta of the aggregate 'cpu' line in /proc/stat: busy = total - (idle+iowait)."""
    global _POSIX_CPU_PREV
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            line = fh.readline()
    except OSError:
        return None
    if not line.startswith("cpu "):
        return None
    try:
        vals = [int(x) for x in line.split()[1:]]
    except ValueError:
        return None
    if len(vals) < 5:
        return None
    idle = vals[3] + vals[4]  # idle + iowait
    total = sum(vals)
    prev, _POSIX_CPU_PREV = _POSIX_CPU_PREV, (total, idle)
    if prev is None:
        return None
    d_total, d_idle = total - prev[0], idle - prev[1]
    if d_total <= 0:
        return None
    return max(0, min(100, int(round(100.0 * (d_total - d_idle) / d_total))))
