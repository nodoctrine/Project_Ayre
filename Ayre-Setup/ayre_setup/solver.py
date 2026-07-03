"""Split + context solver -- step 3 of the optimizer.

Given the machine (probe) and the model (GGUF shape), pick the operating point
for the selected PRESET inside a SAFE hardware budget. The preset (config
`solver.presets`, chosen by `solver.active_preset` or the `preset` arg) sets a
context cap and an offload goal:

  hard constraint : fit without disk-thrash (weights must not stream from disk)
  Max Context     : context = trained max; maximize the GPU layer count that fits,
                    splitting GPU+RAM (quality-first, may be mostly CPU-resident)
  Balanced        : same fit strategy but a capped (smaller) context, so more
                    layers stay GPU-resident
  Speed           : require ALL layers on GPU (full residency for throughput) and
                    step context down toward the floor until the model fits VRAM;
                    degrade to a split (with a warning) if it never does

Output is a `SolveResult`: the n_gpu_layers split + context + KV precision to
launch with, the verdict, and a full rationale. When even the floor context
won't fit (an over-RAM model on a small box -- this laptop's 30B), it returns a
BEST-EFFORT config (max offload + floor context) flagged over_budget with the
estimated disk-spill, rather than refusing -- the refuse/warn policy is step 4.

Per-layer memory model (approximate, documented):
  weights_per_layer = model_file_bytes / n_layers      (block share; coarse)
  kv_per_layer      = context * per_token_kv_fp16 * (kv_bpe/2) / n_layers
  vram_used         = n_gpu * (weights_per_layer + kv_per_layer) + gpu_overhead
  ram_used          = (n_layers - n_gpu) * (weights_per_layer + kv_per_layer)
                      + cpu_overhead
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import load_optimizer
from .fit import GIB, _gib, _ram_budget, _vram_budget
from .gguf import GGUFError, read_model_info
from .hardware import MachineProfile


@dataclass
class SolveResult:
    n_gpu_layers: int
    n_layers_total: int
    context_tokens: int
    kv_precision: str
    verdict: str                  # fits_in_vram | fits_in_ram | fits_split | over_budget
    fits_without_disk: bool
    vram_used_bytes: int
    ram_used_bytes: int
    vram_budget_bytes: int
    ram_budget_bytes: int
    disk_spill_bytes: int         # weights that would stream from disk (0 if it fits)
    context_trained_max: int
    model_info: dict
    rationale: dict
    preset: str = "max_context"        # active preset key (stable id) that produced this plan
    preset_label: str = "Max Context"  # human-facing name for the UI/CLI
    manual: bool = False               # True when a user context/n_gpu_layers override was applied
    limit: str | None = None           # which budget the plan blows: None | "vram" | "ram" | "both"
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("vram_used", "ram_used", "vram_budget", "ram_budget", "disk_spill"):
            d[f"{k}_gib"] = _gib(getattr(self, f"{k}_bytes"))
        return d


def _place(n_layers, n_gpu, weights_per_layer, kv_per_layer, gpu_overhead, cpu_overhead):
    """VRAM/RAM used for a given offload count."""
    n_gpu = max(0, min(n_layers, n_gpu))
    cpu_layers = n_layers - n_gpu
    vram_used = int(n_gpu * (weights_per_layer + kv_per_layer) + gpu_overhead)
    ram_used = int(cpu_layers * (weights_per_layer + kv_per_layer) + cpu_overhead)
    return n_gpu, vram_used, ram_used


def _max_gpu_layers(n_layers, vram_for_model, weights_per_layer, kv_per_layer) -> int:
    per_layer = weights_per_layer + kv_per_layer
    if per_layer <= 0:
        return n_layers
    return max(0, min(n_layers, int(vram_for_model // per_layer)))


def _gpu_overhead_bytes(sv: dict, ctx: int) -> int:
    """VRAM reserved for llama.cpp's CUDA compute/scratch buffers -- context-aware
    (caveat B4). Compute buffers grow with sequence length, so a flat estimate
    under-reserves at giant context and can VRAM-OOM at load. Model: a flat base plus
    a per-1K-context term. Falls back to the legacy flat `gpu_overhead_gib` if present."""
    base = float(sv.get("gpu_overhead_base_gib", sv.get("gpu_overhead_gib", 0.6)))
    per_1k = float(sv.get("gpu_overhead_per_1k_ctx_gib", 0.0))
    return int((base + per_1k * (ctx / 1024.0)) * GIB)


def solve(
    model_path: Path,
    profile: MachineProfile,
    *,
    kv_precision: str = "q8_0",
    optimizer_cfg: dict | None = None,
    model_info=None,
    preset: str | None = None,
    manual_context: int | None = None,
    manual_n_gpu_layers: int | None = None,
) -> SolveResult:
    opt = optimizer_cfg or load_optimizer()
    sv = opt["solver"]
    fpc = opt["footprint"]

    warnings: list[str] = []
    if model_info is None:
        model_info = read_model_info(model_path)   # may raise GGUFError; caller handles
    mi = model_info
    model_bytes = model_path.stat().st_size

    kv_table = fpc["kv_bytes_per_element"]
    if kv_precision in kv_table:
        kv_factor = float(kv_table[kv_precision]) / 2.0
    else:
        kv_factor = float(kv_table.get("f16", 2.0)) / 2.0
        warnings.append(f"Unknown KV precision '{kv_precision}'; assumed f16.")

    vram_budget, vram_reason = _vram_budget(profile, opt)
    ram_budget, ram_reason = _ram_budget(profile, opt)
    cpu_overhead = int(fpc["compute_overhead_gib"] * GIB)

    n_layers = mi.n_layers
    weights_per_layer = model_bytes / n_layers
    per_token_kv = mi.per_token_kv_bytes_fp16

    # Resolve the active preset (user-selectable policy: context cap + offload goal).
    preset_name = (preset or sv.get("active_preset") or "max_context").lower()
    presets = sv.get("presets", {})
    preset_cfg = presets.get(preset_name)
    if preset_cfg is None:
        warnings.append(f"Unknown optimizer preset '{preset_name}'; using 'max_context'.")
        preset_name = "max_context"
        preset_cfg = presets.get("max_context", {})
    offload_goal = preset_cfg.get("offload_goal", "fit")   # "fit" | "full_gpu"
    preset_label = preset_cfg.get("label", preset_name)
    preset_reason = preset_cfg.get("rationale", "")

    # Context candidates: largest (preset cap or trained max) down to the floor.
    trained_max = mi.n_ctx_train
    cap = preset_cfg.get("context_cap_tokens") or trained_max
    if not sv.get("allow_yarn_extension", False):
        cap = min(cap, trained_max)
    floor = min(int(sv["context_floor_tokens"]), cap)
    step = int(sv.get("context_search_step_tokens", 2048))

    candidates = []
    c = cap
    while c > floor:
        candidates.append(c)
        c -= step
    candidates.append(floor)

    def kv_per_layer_for(ctx: int) -> float:
        return (ctx * per_token_kv * kv_factor) / n_layers

    def _search(goal: str):
        """First candidate (largest context down) that meets `goal` within budget.
        'fit'      -> max layers that fit VRAM at that context (a GPU+RAM split).
        'full_gpu' -> require ALL layers on GPU; skip contexts where they won't fit.
        gpu_overhead is recomputed per context (B4). Returns
        (ctx, n_gpu, vram_used, ram_used, gpu_overhead) or None."""
        for ctx in candidates:
            gpu_overhead = _gpu_overhead_bytes(sv, ctx)
            vram_for_model = max(0, vram_budget - gpu_overhead)
            kvpl = kv_per_layer_for(ctx)
            fit_gpu = _max_gpu_layers(n_layers, vram_for_model, weights_per_layer, kvpl)
            if goal == "full_gpu":
                if fit_gpu < n_layers:
                    continue                       # whole model won't fit on GPU here
                n_gpu = n_layers
            else:
                n_gpu = fit_gpu
            n_gpu, vram_used, ram_used = _place(
                n_layers, n_gpu, weights_per_layer, kvpl, gpu_overhead, cpu_overhead)
            if ram_used <= ram_budget and vram_used <= vram_budget:
                return (ctx, n_gpu, vram_used, ram_used, gpu_overhead)
        return None

    manual = manual_context is not None or manual_n_gpu_layers is not None
    limit: str | None = None
    speed_downgraded = False

    if manual:
        # ---- Manual override: honor the user's explicit context/split, evaluate the
        # REAL fit, and WARN about harm rather than silently 'fixing' it
        # (user-control-is-core). A field left unset inherits the active preset's
        # choice: an unset context uses the preset's window; unset layers auto-fit the
        # most that VRAM allows at that context.
        if manual_context is not None:
            ctx = int(manual_context)
            # A pinned context is bounded by the MODEL (trained max, unless YaRN
            # extension is enabled) -- never by the preset's context cap: presets
            # only affect unpinned fields (user-control-is-core).
            if not sv.get("allow_yarn_extension", False) and ctx > trained_max:
                warnings.append(
                    f"Manual context {ctx} exceeds the model's trained max "
                    f"({trained_max}); capped to {trained_max} (YaRN extension is off).")
                ctx = trained_max
            if ctx < floor:
                warnings.append(
                    f"Manual context {ctx} is below the configured floor ({floor}); "
                    "very short windows cut long answers off. Using it as requested.")
        else:
            base = _search(offload_goal) or _search("fit")
            ctx = base[0] if base else floor

        gpu_overhead = _gpu_overhead_bytes(sv, ctx)
        vram_for_model = max(0, vram_budget - gpu_overhead)
        kvpl = kv_per_layer_for(ctx)
        fit_gpu_max = _max_gpu_layers(n_layers, vram_for_model, weights_per_layer, kvpl)

        if manual_n_gpu_layers is not None:
            req = int(manual_n_gpu_layers)
            n_gpu = max(0, min(n_layers, req))
            if n_gpu != req:
                warnings.append(
                    f"Requested n_gpu_layers {req} is out of range; clamped to {n_gpu} "
                    f"(model has {n_layers} layers).")
        else:
            n_gpu = fit_gpu_max

        n_gpu, vram_used, ram_used = _place(
            n_layers, n_gpu, weights_per_layer, kvpl, gpu_overhead, cpu_overhead)

        vram_over = max(0, vram_used - vram_budget)
        ram_over = max(0, ram_used - ram_budget)
        disk_spill = ram_over   # CPU-side weights that don't fit RAM stream from disk
        if vram_over > 0 and ram_over > 0:
            limit = "both"
        elif vram_over > 0:
            limit = "vram"
        elif ram_over > 0:
            limit = "ram"

        if limit in ("vram", "both"):
            warnings.append(
                f"VRAM over budget: {n_gpu}/{n_layers} layers on GPU need ~{_gib(vram_used)} "
                f"GiB at {ctx} context, but only ~{_gib(vram_budget)} GiB is safely available "
                f"(~{_gib(vram_over)} GiB over) -- llama-server will likely fail to load "
                "(VRAM OOM). Lower n_gpu_layers or context.")
        if limit in ("ram", "both"):
            warnings.append(
                f"RAM over budget: the {n_layers - n_gpu} CPU-side layers need "
                f"~{_gib(ram_used)} GiB but only ~{_gib(ram_budget)} GiB is budgeted "
                f"(~{_gib(ram_over)} GiB over) -- those weights will stream from disk "
                "(thrash: slow replies + SSD wear). Raise n_gpu_layers or lower context.")

        if limit is not None:
            verdict = "over_budget"
        elif n_gpu >= n_layers:
            verdict = "fits_in_vram"
        elif n_gpu == 0:
            verdict = "fits_in_ram"
        else:
            verdict = "fits_split"

        # CPU-bound coaching: only when the user PINNED fewer layers than would fit and
        # the GPU-resident fraction dropped below the configured threshold (else it's
        # the machine, not the choice).
        if (verdict != "over_budget" and manual_n_gpu_layers is not None
                and n_gpu < fit_gpu_max):
            pct_now = round(100 * n_gpu / n_layers) if n_layers else 0
            warn_pct = int(sv.get("manual_override", {}).get("cpu_bound_warn_pct", 60))
            if pct_now < warn_pct:
                warnings.append(
                    f"GPU underused: only {n_gpu}/{n_layers} layers ({pct_now}%) are on GPU "
                    f"though up to {fit_gpu_max} would fit at {ctx} context -- the CPU is "
                    "doing most of the decode (slower). Raise n_gpu_layers toward "
                    f"{fit_gpu_max} for more speed.")
    else:
        chosen = _search(offload_goal)
        if chosen is None and offload_goal == "full_gpu":
            # Speed couldn't keep the whole model GPU-resident -- degrade to a split.
            chosen = _search("fit")
            speed_downgraded = chosen is not None

        if chosen is not None:
            ctx, n_gpu, vram_used, ram_used, gpu_overhead = chosen
            disk_spill = 0
            if n_gpu >= n_layers:
                verdict = "fits_in_vram"
            elif n_gpu == 0:
                verdict = "fits_in_ram"
            else:
                verdict = "fits_split"
        else:
            # Best-effort: floor context, max offload, accept the spill (user runs anyway).
            ctx = floor
            gpu_overhead = _gpu_overhead_bytes(sv, ctx)
            vram_for_model = max(0, vram_budget - gpu_overhead)
            kvpl = kv_per_layer_for(ctx)
            n_gpu = _max_gpu_layers(n_layers, vram_for_model, weights_per_layer, kvpl)
            n_gpu, vram_used, ram_used = _place(
                n_layers, n_gpu, weights_per_layer, kvpl, gpu_overhead, cpu_overhead)
            disk_spill = max(0, ram_used - ram_budget)
            verdict = "over_budget"

    fits_without_disk = verdict != "over_budget"
    offload_pct = round(100 * n_gpu / n_layers) if n_layers else 0

    rationale = {
        "model": (f"{model_path.name}: {mi.arch}, {n_layers} layers, "
                  f"weights ~{_gib(model_bytes)} (~{_gib(int(weights_per_layer))}/layer), "
                  f"KV {per_token_kv} B/tok @FP16 (from GGUF)."),
        "vram_budget": vram_reason,
        "ram_budget": ram_reason,
        "context": (f"{ctx} tokens (trained max {trained_max}"
                    + (f", preset cap {cap}" if cap < trained_max else "")
                    + f", floor {floor})"
                    + ("" if (manual or ctx >= cap) else " -- reduced to fit")),
        "split": (f"n_gpu_layers={n_gpu}/{n_layers} ({offload_pct}% on GPU): "
                  f"VRAM use ~{_gib(vram_used)} / budget {_gib(vram_budget)}, "
                  f"RAM use ~{_gib(ram_used)} / budget {_gib(ram_budget)}."),
        "preset": f"{preset_label} (offload goal: {offload_goal}). {preset_reason}",
        "verdict": _verdict_reason(verdict, n_gpu, n_layers, _gib(disk_spill),
                                   manual=manual, limit=limit),
    }
    if manual:
        overridden = []
        overridden.append("context set" if manual_context is not None else "context from preset")
        overridden.append("n_gpu_layers set" if manual_n_gpu_layers is not None
                          else "n_gpu_layers auto-fit")
        rationale["manual"] = (
            f"Manual override ({'; '.join(overridden)}) on top of the {preset_label} preset. "
            "Honored as requested; fit evaluated and any adverse effects flagged in warnings.")

    if speed_downgraded:
        warnings.append(
            f"Speed preset: the whole model won't fit in VRAM even at the floor context "
            f"({floor}), so {n_gpu}/{n_layers} layers are on GPU and the rest on CPU (a "
            "split, like Balanced) -- full-GPU throughput isn't achievable here. For "
            "full-GPU speed, use a smaller or lower-quant model.")

    if verdict == "over_budget" and not manual:
        # Manual over-budget already carries its own precise VRAM/RAM warning above.
        warnings.append(
            f"Even at the floor context ({floor}) with {n_gpu}/{n_layers} layers on GPU, "
            f"~{_gib(disk_spill)} GiB of weights will stream from disk -- inference will "
            "be very slow. Use a smaller or lower-quant model.")
    elif verdict == "fits_split":
        slack = min(vram_budget - vram_used, ram_budget - ram_used)
        if slack < 0.5 * GIB:
            warnings.append(f"Fits, but only ~{_gib(slack)} GiB slack -- close GPU-heavy "
                            "apps before launching for best results.")

    return SolveResult(
        n_gpu_layers=n_gpu, n_layers_total=n_layers, context_tokens=ctx,
        kv_precision=kv_precision, verdict=verdict, fits_without_disk=fits_without_disk,
        vram_used_bytes=vram_used, ram_used_bytes=ram_used,
        vram_budget_bytes=vram_budget, ram_budget_bytes=ram_budget,
        disk_spill_bytes=disk_spill, context_trained_max=trained_max,
        model_info=mi.to_dict(), rationale=rationale, preset=preset_name,
        preset_label=preset_label, manual=manual, limit=limit, warnings=warnings,
    )


def _verdict_reason(verdict, n_gpu, n_layers, spill_gib, *, manual=False, limit=None) -> str:
    if verdict == "over_budget" and manual:
        if limit == "vram":
            return (f"Manual config: {n_gpu}/{n_layers} layers on GPU exceed the VRAM "
                    "budget -> likely load-time OOM (server won't start).")
        if limit == "both":
            return (f"Manual config exceeds BOTH budgets: VRAM (OOM risk) and RAM "
                    f"(~{spill_gib} GiB of weights spill to disk).")
        return (f"Manual config: {n_gpu}/{n_layers} on GPU, but ~{spill_gib} GiB of "
                "CPU-side weights spill to disk (thrash).")
    return {
        "fits_in_vram": f"All {n_layers} layers fit in VRAM -> fully GPU-resident.",
        "fits_in_ram": "Fits in RAM with no GPU offload needed (no disk spill).",
        "fits_split": f"Fits across GPU+RAM with {n_gpu}/{n_layers} layers offloaded -> no disk spill.",
        "over_budget": f"Best effort: {n_gpu}/{n_layers} on GPU at floor context, "
                       f"but ~{spill_gib} GiB still spills to disk.",
    }[verdict]


def try_solve(model_path: Path, profile: MachineProfile, errors: list | None = None,
              **kw) -> SolveResult | None:
    """Solve, but return None instead of raising if the GGUF can't be read -- so
    the launch path can fall back to tier config rather than failing. Pass an
    `errors` list to receive WHY it failed (surfaced in the launch warning /
    Setup fit banner, so a silent fallback is diagnosable -- without it a broken
    probe/GGUF read just looks like 'presets do nothing')."""
    try:
        return solve(model_path, profile, **kw)
    except (GGUFError, OSError, KeyError, ValueError) as exc:
        if errors is not None:
            errors.append(f"{type(exc).__name__}: {exc}")
        return None
