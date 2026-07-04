"""Config loading + launch-spec assembly.

Variable-first: the llama-server launch flags are assembled from config
(tiers.json, rerankers.json, runtime.json), never from literals in code.
Assembling the spec is itself a decision, so -- in the spirit of the modular
formula registry -- it is one function with named inputs and an inspectable
output that carries its own rationale (document-tier-reasoning rule).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import platform_layer


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def config_dir() -> Path:
    return platform_layer.ayre_usb_root() / "config"


def models_dir() -> Path:
    return platform_layer.ayre_usb_root() / "models"


def load_tiers() -> dict:
    return _load_json(config_dir() / "tiers.json")


def load_rerankers() -> dict:
    return _load_json(config_dir() / "rerankers.json")


def load_runtime() -> dict:
    return _load_json(config_dir() / "runtime.json")


def load_optimizer() -> dict:
    return _load_json(config_dir() / "optimizer.json")


def load_coaching() -> dict:
    """User-facing coaching copy (config/coaching.json): quant tradeoffs today,
    room for more surfaces later. Optional -- a missing or unparseable file just
    disables coaching, it never blocks a launch or the doctor."""
    try:
        return _load_json(config_dir() / "coaching.json")
    except (OSError, json.JSONDecodeError):
        return {}


def quant_coaching_for(name: str, coaching: dict | None = None) -> dict | None:
    """Match a GGUF filename to a quant coaching tier (coaching.json ->
    quant.tiers) and return {id, label, tone, tip} for the UI, or None when
    coaching is absent or the filename has no recognizable quant marker.

    Detection is filename-based on purpose: the Q-level people write in the name
    (Q4_K_M, IQ3_XS, f16, ...) is exactly what they recognize and shop for, and
    it needs no GGUF parse. `I?Q<digit>` -> the tier whose match_digits covers
    <digit>; F16/BF16/F32 (no integer quant) -> the `full` tier; an IQ*/imatrix
    build appends the shared imatrix note. Content lives in config; only the
    parsing lives here (mirrors gguf.py: code parses, config supplies copy)."""
    coaching = coaching if coaching is not None else load_coaching()
    quant = (coaching or {}).get("quant") or {}
    tiers = quant.get("tiers") or []
    if not tiers:
        return None

    upper = name.upper()

    def as_result(tier: dict, imatrix: bool = False) -> dict:
        tip = tier.get("tip", "")
        note = quant.get("imatrix_note")
        if imatrix and note:
            tip = (tip + note).rstrip()
        return {"id": tier.get("id"), "label": tier.get("label"),
                "tone": tier.get("tone", "neutral"), "tip": tip}

    def unknown() -> dict | None:
        u = quant.get("unknown")
        if not u:
            return None
        return {"id": "unknown", "label": u.get("label"),
                "tone": u.get("tone", "neutral"), "tip": u.get("tip", "")}

    # Integer quant (Q4_K_M, IQ3_XS, ...) -- the common, specific case first.
    m = re.search(r"(I?)Q(\d)", upper)
    if m:
        digit = int(m.group(2))
        for tier in tiers:
            if digit in (tier.get("match_digits") or []):
                return as_result(tier, imatrix=m.group(1) == "I")
        return unknown()

    # No integer quant -- full precision weights (F16 / BF16 / F32)?
    if re.search(r"(?:^|[^A-Z0-9])(?:B?F16|F32)(?:[^A-Z0-9]|$)", upper):
        for tier in tiers:
            if tier.get("id") == "full":
                return as_result(tier)

    return unknown()


def moe_coaching_for(model_info, coaching: dict | None = None) -> dict | None:
    """MoE coaching chip (coaching.json -> moe) for a parsed model: {label, tone,
    tip} with the model's real expert counts filled in, or None when the model is
    dense or coaching is absent. Unlike quant (a filename convention), MoE-ness
    comes from GGUF metadata -- pass a gguf.ModelInfo (or anything carrying
    n_expert / n_expert_used)."""
    n_expert = int(getattr(model_info, "n_expert", 0) or 0)
    if n_expert <= 1:
        return None
    coaching = coaching if coaching is not None else load_coaching()
    moe = (coaching or {}).get("moe") or {}
    if not moe:
        return None
    tip = moe.get("tip", "").format(
        n_expert=n_expert,
        n_expert_used=int(getattr(model_info, "n_expert_used", 0) or 0))
    return {"label": moe.get("label", "MoE"), "tone": moe.get("tone", "info"),
            "tip": tip}


# --- Machine-local user overlay (per-machine, gitignored) -------------------
# The SAME overlay the UI writes (ayre_ui/server.py: config/user_settings.json).
# Ayre-Setup owns the `optimizer` block: a per-machine manual context / GPU-split
# override. Persisting here (not in the committed optimizer.json) means an override
# travels with the box, survives updates, and never lands in the repo.

def user_settings_path() -> Path:
    return config_dir() / "user_settings.json"


def load_user_settings() -> dict:
    """The machine-local overlay (may not exist yet)."""
    p = user_settings_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_user_settings(data: dict) -> None:
    """Atomically persist the overlay (load-merge-save so we never clobber the UI's keys)."""
    p = user_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def get_manual_override() -> tuple[int | None, int | None]:
    """Per-machine (context_tokens, n_gpu_layers) override; either may be None
    (= let the active preset decide that field)."""
    opt = load_user_settings().get("optimizer", {}) or {}
    ctx = opt.get("context_tokens")
    ngl = opt.get("n_gpu_layers")
    ctx = int(ctx) if isinstance(ctx, int) else None
    ngl = int(ngl) if isinstance(ngl, int) else None
    return ctx, ngl


def set_manual_override(context_tokens: int | None, n_gpu_layers: int | None) -> None:
    """Persist a per-machine override. Pass None for a field to defer it to the preset."""
    data = load_user_settings()
    opt = data.setdefault("optimizer", {})
    opt["context_tokens"] = context_tokens
    opt["n_gpu_layers"] = n_gpu_layers
    opt["_comment"] = ("Per-machine optimizer overrides (Ayre-Setup): `preset` = the "
                       "chosen policy; `context_tokens`/`n_gpu_layers` = manual override "
                       "(null = let the preset decide). Set via `cli override` or the UI.")
    save_user_settings(data)


def clear_manual_override() -> None:
    """Remove the per-machine override so the active preset drives context + split again."""
    data = load_user_settings()
    opt = data.get("optimizer")
    if isinstance(opt, dict):
        opt.pop("context_tokens", None)
        opt.pop("n_gpu_layers", None)
        if not any(k for k in opt if k != "_comment"):
            data.pop("optimizer", None)
        save_user_settings(data)


def get_preset_override() -> str | None:
    """Per-machine preset choice (A3: the UI's preset selector persists here).
    None = no per-machine choice; the shipped optimizer.json active_preset applies."""
    preset = (load_user_settings().get("optimizer", {}) or {}).get("preset")
    return preset if isinstance(preset, str) and preset else None


def set_preset_override(preset: str | None) -> None:
    """Persist (or, with None, remove) the per-machine preset choice. Callers
    validate the key against optimizer.json -> solver.presets; the solver also
    falls back to 'max_context' with a warning on an unknown value."""
    data = load_user_settings()
    if preset is None:
        opt = data.get("optimizer")
        if not isinstance(opt, dict):
            return
        opt.pop("preset", None)
        if not any(k for k in opt if k != "_comment"):
            data.pop("optimizer", None)
    else:
        opt = data.setdefault("optimizer", {})
        opt["preset"] = preset
        opt["_comment"] = ("Per-machine optimizer overrides (Ayre-Setup): `preset` = the "
                           "chosen policy; `context_tokens`/`n_gpu_layers` = manual override "
                           "(null = let the preset decide). Set via `cli override` or the UI.")
    save_user_settings(data)


class NoModelError(FileNotFoundError):
    """Raised when a launch is requested but no chat model .gguf is present.

    A missing chat model is NOT a Setup failure (the doctor reports it as a
    friendly 'add a model'), but you obviously cannot `start` a server with
    nothing to load.
    """


def reranker_items(rerankers: dict | None = None) -> list[dict]:
    rerankers = rerankers or load_rerankers()
    return rerankers.get("items", [])


def reranker_filenames(rerankers: dict | None = None) -> set[str]:
    """Filenames registered as rerankers in config/rerankers.json.

    Excluded from chat-model discovery so a reranker .gguf is never mistaken
    for a chat model.
    """
    return {r["file"] for r in reranker_items(rerankers)}


def discover_chat_models(rerankers: dict | None = None) -> list[Path]:
    """Universal-model detection: ANY non-reranker .gguf in models/.

    Drop in whatever GGUF you want and it is found here. Real context/size
    come from GGUF metadata at runtime. Reranker files registered in
    config/rerankers.json are excluded so they never appear as selectable
    chat models.
    """
    reserved = reranker_filenames(rerankers)
    found = [
        p for p in sorted(models_dir().glob("*.gguf"), key=lambda p: p.name.lower())
        if p.name not in reserved
    ]
    return found


@dataclass
class LaunchSpec:
    """A fully resolved, inspectable description of one llama-server launch."""

    binary: Path
    model_file: Path
    context_tokens: int
    kv_precision: str
    n_gpu_layers: int | None
    host: str
    port: int
    extra_args: list[str]
    rationale: dict  # why each value is what it is (logged + displayable)
    warnings: list[str] = field(default_factory=list)
    fit: dict | None = None  # solver fit summary for the step-4 gate; None when auto-tune off or unsolved

    def argv(self) -> list[str]:
        args = [
            str(self.binary),
            "-m", str(self.model_file),
            "-c", str(self.context_tokens),
            "--host", self.host,
            "--port", str(self.port),
            "--cache-type-k", self.kv_precision,
            "--cache-type-v", self.kv_precision,
        ]
        if self.n_gpu_layers is not None:
            args += ["-ngl", str(self.n_gpu_layers)]
        args += self.extra_args
        return args


def _pick_best_fitting_model(
    discovered: list[Path],
    profile,
    kv_precision: str,
    optimizer_cfg: dict,
) -> tuple[Path, str]:
    """Auto-select: largest GGUF that fits without disk-thrash; smallest if none fit.

    Sort by file size descending so we always try the best model first.
    Falls back to the smallest to minimise disk spill and hardware stress when
    nothing fits cleanly (the fit gate will warn the user).
    """
    from .solver import try_solve

    by_size = sorted(discovered, key=lambda p: p.stat().st_size, reverse=True)
    for path in by_size:
        sr = try_solve(path, profile, kv_precision=kv_precision, optimizer_cfg=optimizer_cfg)
        if sr is not None and sr.fits_without_disk:
            return path, f"auto: '{path.name}' is the largest model that fits without disk-thrash"

    # Nothing fits -- use smallest to minimise disk spill and hardware stress.
    smallest = by_size[-1]
    return smallest, (
        f"auto: no model fits without disk-thrash; using smallest '{smallest.name}' "
        "(gate will warn -- consider a smaller/lower-quant model)"
    )


def build_launch_spec(
    tier: str | None = None,
    model_id: str | None = None,
    auto: bool | None = None,
    preset: str | None = None,
    manual_context: int | None = None,
    manual_n_gpu_layers: int | None = None,
    use_saved_override: bool = True,
) -> LaunchSpec:
    """Assemble a LaunchSpec from config.

    tier=None  -> when auto-tune is on, the probed hardware's suggested tier;
                  otherwise 'mid' (the legacy default).
    model_id=None -> auto-select: largest model that fits without disk-thrash
                     when auto-tune is on, else the first discovered model.
    auto=None  -> read optimizer.json -> solver.auto_apply_on_start. When on,
                  the solver refines context + n_gpu_layers for THIS machine
                  (step 3), overriding the tier's seeded/fallback values.
    preset=None -> the per-machine persisted choice (user_settings.json ->
                  optimizer.preset, the UI selector's home), else optimizer.json
                  -> solver.active_preset; pass 'max_context'/'balanced'/'speed'
                  to override the context vs GPU-residency policy for this launch.
    manual_context / manual_n_gpu_layers=None -> A2 manual override. An explicit
                  value here wins; when both are None, the per-machine persisted
                  override (config/user_settings.json -> optimizer) is read. The
                  solver HONORS the value and surfaces warnings if it's harmful
                  (OOM / disk-thrash / CPU-bound). Only used when auto-tune is on.
    use_saved_override=False -> do NOT fall back to the persisted manual override:
                  evaluate exactly the manual values given (even both-None = pure
                  preset). The UI's what-if fit preview (A3) uses this so typing a
                  hypothetical never mixes with the saved state; launches keep the
                  default (True).
    """
    tiers = load_tiers()
    runtime = load_runtime()
    optimizer = load_optimizer()
    discovered = discover_chat_models()

    # Preset: explicit arg wins; else the per-machine persisted choice (the UI
    # selector / `cli override --preset`); else None -> the solver reads
    # optimizer.json's active_preset.
    if preset is None:
        preset = get_preset_override()

    # Manual override: explicit args win; otherwise fall back to the per-machine
    # persisted override so a UI/CLI-saved choice is honored on every launch.
    if use_saved_override and manual_context is None and manual_n_gpu_layers is None:
        manual_context, manual_n_gpu_layers = get_manual_override()

    if auto is None:
        auto = bool(optimizer.get("solver", {}).get("auto_apply_on_start", False))

    # Lazy imports avoid a config<->hardware/solver import cycle.
    probe = None
    if tier is None:
        if auto:
            from .hardware import probe_machine
            probe = probe_machine()
            tier = probe.suggested_tier or "mid"
            tier_reason = f"auto: probed VRAM suggests tier '{tier}'"
        else:
            tier = "mid"
            tier_reason = "defaulted to 'mid' (auto-tune off)"
    else:
        tier_reason = f"explicitly requested tier '{tier}'"
    if tier not in tiers["tiers"]:
        raise ValueError(f"unknown tier '{tier}'; known: {sorted(tiers['tiers'])}")
    profile = tiers["tiers"][tier]

    kv = profile["context"]["kv_precision"]

    if model_id is not None:
        # Explicit pick: a .gguf filename in models/ (add extension if omitted).
        cand = models_dir() / model_id
        if cand.suffix != ".gguf":
            cand = models_dir() / f"{model_id}.gguf"
        if not cand.exists():
            known = [p.name for p in discovered]
            raise ValueError(
                f"model '{model_id}' not found in models/; present: {known}"
            )
        model_file = cand
        model_reason = f"explicitly requested model file '{cand.name}'"
    else:
        if not discovered:
            raise NoModelError(
                "No chat model found in models/. Add a .gguf and try again "
                "(any GGUF that fits your tier works)."
            )
        if auto:
            if probe is None:
                from .hardware import probe_machine
                probe = probe_machine()
            model_file, model_reason = _pick_best_fitting_model(discovered, probe, kv, optimizer)
        else:
            model_file = discovered[0]
            model_reason = f"auto-tune off; using first discovered model '{model_file.name}'"

    ctx = profile["context"]["tokens"]

    seed = profile.get("offload_split_seed", {}).get("n_gpu_layers")
    if seed is not None:
        ngl = seed
        ngl_reason = f"seeded dev layer count from tier '{tier}'"
    else:
        ngl = runtime.get("fallback_n_gpu_layers")
        ngl_reason = (
            f"tier '{tier}' has no seeded n_gpu_layers; using runtime fallback ({ngl}). "
            "Measure the real dev split and seed tiers.json."
        )

    context_reason = f"{ctx} tokens @ {kv} KV, from tier '{tier}' profile"
    optimizer_rationale: dict | str = "auto-tune off"
    warnings: list[str] = []
    fit: dict | None = None

    # Step-3 auto-tune: let the solver size context + the GPU/CPU split for THIS
    # machine, overriding the tier's seed/fallback. Falls back silently to the
    # tier values if the GGUF/probe can't be read (try_solve returns None).
    if auto:
        from .solver import try_solve
        if probe is None:
            from .hardware import probe_machine
            probe = probe_machine()
        solve_errors: list[str] = []
        sr = try_solve(model_file, probe, kv_precision=kv, optimizer_cfg=optimizer,
                       preset=preset, manual_context=manual_context,
                       manual_n_gpu_layers=manual_n_gpu_layers, errors=solve_errors)
        if sr is not None:
            ctx = sr.context_tokens
            ngl = sr.n_gpu_layers
            solved_label = "manual-solved" if sr.manual else "auto-solved"
            ngl_reason = f"{solved_label} split: {sr.rationale['split']}"
            context_reason = f"{solved_label}: {sr.rationale['context']}"
            optimizer_rationale = sr.rationale
            warnings = list(sr.warnings)
            fit = {
                "verdict": sr.verdict,
                "fits_without_disk": sr.fits_without_disk,
                "disk_spill_bytes": sr.disk_spill_bytes,
                "vram_used_bytes": sr.vram_used_bytes,
                "ram_used_bytes": sr.ram_used_bytes,
                "vram_budget_bytes": sr.vram_budget_bytes,
                "ram_budget_bytes": sr.ram_budget_bytes,
                "context_tokens": sr.context_tokens,
                "context_trained_max": sr.context_trained_max,
                "n_gpu_layers": sr.n_gpu_layers,
                "n_layers_total": sr.n_layers_total,
                "manual": sr.manual,
                "limit": sr.limit,
                "preset": sr.preset,
                "preset_label": sr.preset_label,
            }
        else:
            # Say WHY it failed -- a silent fallback to tier defaults makes presets
            # and overrides look broken ("nothing changes the split") with no clue.
            reason = f" ({solve_errors[0]})" if solve_errors else ""
            warnings = ["Auto-tune unavailable (couldn't read the GGUF/hardware"
                        f"{reason}); using tier defaults -- preset and manual "
                        "choices have no effect until this is resolved."]

    binary = (
        platform_layer.ayre_usb_root()
        / runtime["bin_dir"]
        / platform_layer.llama_server_binary_name()
    )

    return LaunchSpec(
        binary=binary,
        model_file=model_file,
        context_tokens=ctx,
        kv_precision=kv,
        n_gpu_layers=ngl,
        host=runtime["host"],
        port=runtime["port"],
        extra_args=list(runtime.get("extra_server_args", [])),
        rationale={
            "tier": tier_reason,
            "model": f"{model_reason} -> {model_file.name}",
            "context": context_reason,
            "n_gpu_layers": ngl_reason,
            "optimizer": optimizer_rationale,
        },
        warnings=warnings,
        fit=fit,
    )
