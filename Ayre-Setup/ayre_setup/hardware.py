"""Hardware probe -- step 1 of the optimizer (the auto-tuner).

Assembles a `MachineProfile`: the real RAM / VRAM / CPU on THIS machine, plus a
suggested tier, plus a rationale for every value. It is the input layer the fit
calculator and split/context solver build on -- nothing downstream should reason
about hardware without it (the old code hardcoded tier 'mid' and stubbed VRAM).

Design notes:
- Mirrors tiers.json's axis: VRAM is the spine (it picks the tier), RAM is a
  veto/floor check, CPU is informational (speed, not feasibility -- moot under
  effectiveness-over-speed / protect-end-user-hardware).
- Tier *bands* are read from tiers.json, never hardcoded here (variable-first).
- Re-runnable by design: probing is a fresh function call with no caching, so a
  "close other apps, then rescan" flow is just calling probe_machine() again --
  important because available memory is what conservative budgeting uses, and
  because a USB-portable app changes machines.
- Every raw read degrades to a safe default with a warning rather than raising,
  so the probe always returns a usable profile.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from . import platform_layer
from .config import load_tiers

GIB = 1024 ** 3


def _gib(num_bytes: int | None) -> float | None:
    return round(num_bytes / GIB, 2) if num_bytes else (0.0 if num_bytes == 0 else None)


@dataclass
class MachineProfile:
    os_name: str
    cpu_logical: int                      # informational only
    ram_total_bytes: int
    ram_available_bytes: int
    gpus: list[dict]                      # every detected adapter
    primary_vram_total_bytes: int         # the spine: largest dedicated GPU
    primary_vram_free_bytes: int | None   # None when the source can't report free
    primary_gpu_name: str | None
    primary_gpu_vendor: str | None        # spine GPU's vendor -- the axis backends.json selects on
    suggested_tier: str | None
    detected_at: float                    # epoch seconds (for "last scanned" display)
    rationale: dict                       # why each value is what it is
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Convenience GiB mirrors for display / the API, computed not stored.
        d["ram_total_gib"] = _gib(self.ram_total_bytes)
        d["ram_available_gib"] = _gib(self.ram_available_bytes)
        d["primary_vram_total_gib"] = _gib(self.primary_vram_total_bytes)
        d["primary_vram_free_gib"] = _gib(self.primary_vram_free_bytes)
        return d


def _tier_for_vram(vram_total_bytes: int, tiers_cfg: dict) -> tuple[str | None, str]:
    """Pick a tier purely by VRAM band (the spine). RAM veto / floor is the fit
    calculator's job; here we only place the machine on the gradient."""
    vram_gib = (vram_total_bytes or 0) / GIB
    tiers = tiers_cfg.get("tiers", {})
    for name, prof in tiers.items():
        band = prof.get("vram_band_gb")
        if not band or len(band) != 2:
            continue
        lo, hi = band
        lo = lo if lo is not None else 0
        if vram_gib >= lo and (hi is None or vram_gib < hi):
            return name, (f"VRAM {vram_gib:.1f} GiB lands in tier '{name}' "
                          f"band {band} GB (VRAM is the tier spine).")
    return None, (f"VRAM {vram_gib:.1f} GiB matched no configured band; "
                  "no tier suggested (fit calculator must decide feasibility).")


def probe_machine() -> MachineProfile:
    """Detect the current machine. Pure read; safe to call repeatedly (rescan)."""
    os_name = platform_layer.current_os()
    cpu = platform_layer.logical_cpu_count()
    ram_total, ram_avail = platform_layer.memory_bytes()
    gpus = platform_layer.detect_gpus()

    warnings: list[str] = []

    # The spine: the dedicated GPU with the most VRAM. Integrated GPUs that share
    # system RAM still appear, but the largest dedicated card is the offload target.
    primary = max(gpus, key=lambda g: g.get("vram_total_bytes", 0), default=None)
    primary_vram = primary["vram_total_bytes"] if primary else 0
    primary_free = primary.get("vram_free_bytes") if primary else None
    primary_name = primary["name"] if primary else None
    primary_vendor = primary.get("vendor") if primary else None

    if not gpus:
        warnings.append("No GPU detected -- treating as CPU-only (VRAM = 0). "
                        "GPU offload is unavailable on this machine.")
    if primary and primary.get("source") == "wmi":
        warnings.append("VRAM read via WMI AdapterRAM, which caps near 4GB -- the "
                        "real VRAM may be higher. Verify before trusting the split.")
    if primary and primary_free is None:
        warnings.append("This VRAM source can't report FREE VRAM; conservative "
                        "budgeting will assume the card is otherwise idle.")
    if ram_total == 0:
        warnings.append("Could not read system RAM -- memory budgeting will be unreliable.")

    tiers_cfg = load_tiers()
    suggested_tier, tier_reason = _tier_for_vram(primary_vram, tiers_cfg)

    rationale = {
        "axis": "VRAM is the spine (picks tier); RAM is a veto/floor check; CPU is "
                "informational (speed, not feasibility).",
        "ram": f"Total {_gib(ram_total)} GiB, available {_gib(ram_avail)} GiB now. "
               "Conservative budgeting uses AVAILABLE, so closing other apps and "
               "restarting Ayre raises the budget.",
        "vram": (f"Primary GPU '{primary_name}': {_gib(primary_vram)} GiB total"
                 + (f", {_gib(primary_free)} GiB free" if primary_free is not None else
                    ", free unknown")
                 + f" (source: {primary['source']}).") if primary
                else "No GPU -> VRAM 0 GiB (CPU-only).",
        "cpu": f"{cpu} logical processors (informational only).",
        "tier": tier_reason,
        "detection_sources": "nvidia-smi (accurate + free) -> Windows registry "
                             "qwMemorySize (accurate total) -> WMI (fallback, capped).",
    }

    return MachineProfile(
        os_name=os_name,
        cpu_logical=cpu,
        ram_total_bytes=ram_total,
        ram_available_bytes=ram_avail,
        gpus=gpus,
        primary_vram_total_bytes=primary_vram,
        primary_vram_free_bytes=primary_free,
        primary_gpu_name=primary_name,
        primary_gpu_vendor=primary_vendor,
        suggested_tier=suggested_tier,
        detected_at=time.time(),
        rationale=rationale,
        warnings=warnings,
    )
