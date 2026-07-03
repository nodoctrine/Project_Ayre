"""Fit / footprint calculator -- step 2 of the optimizer.

Answers the question the disk-thrash on the laptop forced: *does this model, at
this context, fit in this machine's memory budget -- or will it spill to disk?*

Two layers, kept separate on purpose (modular-formula-registry):
  - `compute_fit(FitInputs) -> FitResult` is a PURE function: numbers in, verdict
    out, no I/O. Individually reviewable and unit-testable.
  - `estimate_fit(...)` is a thin assembler that gathers those numbers from the
    machine probe + optimizer.json + the model file, then calls the pure core.

Footprint model:  footprint = weights + KV cache + compute/runtime overhead
  - weights    ~= the GGUF file size on disk (good proxy for resident weights)
  - KV cache    = context_tokens * per_token_kv_bytes_fp16 * (kv_bpe / 2.0)
  - overhead    = a flat configured estimate for llama.cpp scratch buffers

Budget (conservative / user-chosen):
  - ram_budget  = available_RAM - max(headroom_gib, headroom_fraction*total_RAM)
  - vram_budget = (free VRAM, else total) - vram_headroom

Verdict ladder (best placement that fits):
  fits_in_vram  -> whole model resident in VRAM (fastest, fully offloaded)
  fits_in_ram   -> fits in system RAM alone (CPU-only viable, no disk)
  fits_split    -> needs VRAM + RAM together (an offload split; still no disk)
  over_budget   -> exceeds VRAM+RAM -> weights stream from disk = THRASH

The actual GPU/CPU split that realizes a 'fits_split' is the solver's job
(step 3); this layer only decides feasibility and quantifies the gap.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import load_optimizer, load_tiers
from .hardware import MachineProfile

GIB = 1024 ** 3


def _gib(num_bytes) -> float | None:
    if num_bytes is None:
        return None
    return round(num_bytes / GIB, 2)


@dataclass(frozen=True)
class FitInputs:
    """Everything the pure calculator needs -- already-resolved numbers."""
    model_bytes: int
    context_tokens: int
    per_token_kv_bytes_fp16: int
    kv_bytes_per_element: float       # for the chosen KV precision
    compute_overhead_bytes: int
    vram_budget_bytes: int
    ram_budget_bytes: int


@dataclass(frozen=True)
class FitResult:
    weights_bytes: int
    kv_bytes: int
    overhead_bytes: int
    footprint_bytes: int
    vram_budget_bytes: int
    ram_budget_bytes: int
    combined_budget_bytes: int
    verdict: str                      # fits_in_vram | fits_in_ram | fits_split | over_budget
    fits_without_disk: bool           # the headline: no disk-thrash?
    deficit_bytes: int                # how far OVER the combined budget (0 if it fits)
    headroom_bytes: int               # surplus under the combined budget (0 if over)
    rationale: dict
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("weights", "kv", "overhead", "footprint"):
            d[f"{k}_gib"] = _gib(getattr(self, f"{k}_bytes"))
        for k in ("vram_budget", "ram_budget", "combined_budget", "deficit", "headroom"):
            d[f"{k}_gib"] = _gib(getattr(self, f"{k}_bytes"))
        return d


def compute_fit(inp: FitInputs) -> FitResult:
    """Pure core: footprint vs budget -> verdict. No I/O."""
    kv_bytes = int(inp.context_tokens * inp.per_token_kv_bytes_fp16 * (inp.kv_bytes_per_element / 2.0))
    weights = inp.model_bytes
    overhead = inp.compute_overhead_bytes
    footprint = weights + kv_bytes + overhead

    vram_budget = max(0, inp.vram_budget_bytes)
    ram_budget = max(0, inp.ram_budget_bytes)
    combined = vram_budget + ram_budget

    if footprint <= vram_budget:
        verdict = "fits_in_vram"
    elif footprint <= ram_budget:
        verdict = "fits_in_ram"
    elif footprint <= combined:
        verdict = "fits_split"
    else:
        verdict = "over_budget"

    fits_without_disk = footprint <= combined
    deficit = max(0, footprint - combined)
    headroom = max(0, combined - footprint)

    rationale = {
        "footprint": (f"weights {_gib(weights)} + KV {_gib(kv_bytes)} "
                      f"({inp.context_tokens} tok @ {inp.kv_bytes_per_element} B/elem) "
                      f"+ overhead {_gib(overhead)} = {_gib(footprint)} GiB."),
        "budget": (f"VRAM {_gib(vram_budget)} + RAM {_gib(ram_budget)} = "
                   f"{_gib(combined)} GiB usable (after headroom)."),
        "verdict": _verdict_reason(verdict, _gib(footprint), _gib(vram_budget),
                                   _gib(ram_budget), _gib(combined),
                                   _gib(deficit), _gib(headroom)),
    }

    warnings = []
    if verdict == "over_budget":
        warnings.append(
            f"Footprint exceeds VRAM+RAM by {_gib(deficit)} GiB -- weights would stream "
            "from disk on every token. Inference will be very slow. Use a smaller or "
            "lower-quant model, or lower the context.")
    elif verdict == "fits_split" and headroom < 0.5 * GIB:
        warnings.append(
            f"Only {_gib(headroom)} GiB of slack -- fits, but close GPU-heavy apps "
            "before launching for best results.")

    return FitResult(
        weights_bytes=weights, kv_bytes=kv_bytes, overhead_bytes=overhead,
        footprint_bytes=footprint, vram_budget_bytes=vram_budget,
        ram_budget_bytes=ram_budget, combined_budget_bytes=combined,
        verdict=verdict, fits_without_disk=fits_without_disk,
        deficit_bytes=deficit, headroom_bytes=headroom,
        rationale=rationale, warnings=warnings,
    )


def _verdict_reason(verdict, fp, vram, ram, combined, deficit, headroom) -> str:
    return {
        "fits_in_vram": f"{fp} GiB fits entirely in {vram} GiB VRAM -> fully GPU-resident (ideal).",
        "fits_in_ram": f"{fp} GiB fits in {ram} GiB RAM -> CPU-only viable with no disk spill.",
        "fits_split": f"{fp} GiB fits across VRAM+RAM ({combined} GiB) with {headroom} GiB to spare -> run as an offload split, no disk.",
        "over_budget": f"{fp} GiB exceeds VRAM+RAM ({combined} GiB) by {deficit} GiB -> would spill to disk (thrash).",
    }[verdict]


def _ram_budget(profile: MachineProfile, opt: dict) -> tuple[int, str]:
    b = opt["budget"]
    basis = b.get("basis", "available")
    total = profile.ram_total_bytes
    base = profile.ram_available_bytes if basis == "available" else total
    headroom = max(b["ram_headroom_gib"] * GIB, b["ram_headroom_fraction"] * total)
    budget = max(0, int(base - headroom))
    label = "OS buffer" if basis == "total" else "headroom"
    reason = (f"RAM budget {_gib(budget)} = {basis} {_gib(int(base))} - {label} "
              f"{_gib(int(headroom))} (max of {b['ram_headroom_gib']} GiB / "
              f"{int(b['ram_headroom_fraction']*100)}% of total).")
    return budget, reason


def _vram_budget(profile: MachineProfile, opt: dict) -> tuple[int, str]:
    b = opt["budget"]
    headroom = b["vram_headroom_gib"] * GIB
    if profile.primary_vram_free_bytes is not None:
        base, base_label = profile.primary_vram_free_bytes, "free"
    else:
        base, base_label = profile.primary_vram_total_bytes, "total"
    budget = max(0, int(base - headroom))
    reason = (f"VRAM budget {_gib(budget)} = {base_label} {_gib(base)} - headroom "
              f"{b['vram_headroom_gib']} GiB.")
    return budget, reason


def estimate_fit(
    model_path: Path,
    context_tokens: int,
    kv_precision: str,
    profile: MachineProfile,
    *,
    optimizer_cfg: dict | None = None,
    per_token_kv_bytes_fp16: int | None = None,
) -> FitResult:
    """Assemble inputs from the probe + optimizer.json + the model file, then run
    the pure calculator. `per_token_kv_bytes_fp16` overrides the config default
    (e.g. once read from GGUF metadata)."""
    opt = optimizer_cfg or load_optimizer()
    fp = opt["footprint"]

    model_bytes = model_path.stat().st_size

    kv_table = fp["kv_bytes_per_element"]
    warnings: list[str] = []
    if kv_precision in kv_table:
        kv_bpe = float(kv_table[kv_precision])
    else:
        kv_bpe = float(kv_table.get("f16", 2.0))
        warnings.append(f"Unknown KV precision '{kv_precision}'; assumed f16 ({kv_bpe} B/elem).")

    per_tok_kv = per_token_kv_bytes_fp16 or int(fp["default_per_token_kv_bytes_fp16"])
    if per_token_kv_bytes_fp16 is None:
        warnings.append("Per-token KV is the config default (Qwen3-30B-A3B seed), "
                        "not read from this model's GGUF -- estimate may be off for other models.")

    ram_budget, ram_reason = _ram_budget(profile, opt)
    vram_budget, vram_reason = _vram_budget(profile, opt)

    result = compute_fit(FitInputs(
        model_bytes=model_bytes,
        context_tokens=context_tokens,
        per_token_kv_bytes_fp16=per_tok_kv,
        kv_bytes_per_element=kv_bpe,
        compute_overhead_bytes=int(fp["compute_overhead_gib"] * GIB),
        vram_budget_bytes=vram_budget,
        ram_budget_bytes=ram_budget,
    ))

    # Enrich rationale/warnings with the assembler-level context.
    result.rationale["model"] = f"{model_path.name}: weights ~= file size {_gib(model_bytes)} GiB."
    result.rationale["ram_budget"] = ram_reason
    result.rationale["vram_budget"] = vram_reason
    result.warnings[:0] = warnings
    return result
