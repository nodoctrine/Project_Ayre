"""CLI entry for the Ayre-Setup server wrapper.

    python -m ayre_setup.cli start [--tier mid] [--model <id>] [--preset speed]
                                   [--context N] [--n-gpu-layers K] [--dry-run]
    python -m ayre_setup.cli stop
    python -m ayre_setup.cli probe
    python -m ayre_setup.cli fit [--model F] [--context N] [--kv-precision q8_0]
    python -m ayre_setup.cli solve [--model F] [--kv-precision q8_0] [--preset balanced]
                                   [--context N] [--n-gpu-layers K]
    python -m ayre_setup.cli override [--preset P] [--context N] [--n-gpu-layers K] [--clear]
    python -m ayre_setup.cli check

`check` is the two-tier doctor (REFRAMED 2026-06-15): REQUIRED artifacts that
ship in the GitHub download vs. the CHAT MODEL the user adds. Missing required ->
fail; missing model -> friendly 'add a model' (Setup still succeeds). Setup never
downloads anything; that is the Updater's job.

`start --dry-run` resolves and prints the launch spec + argv without launching.
"""
from __future__ import annotations

import argparse
import sys

from .config import (
    LaunchSpec,
    NoModelError,
    _pick_best_fitting_model,
    build_launch_spec,
    clear_manual_override,
    discover_chat_models,
    get_manual_override,
    get_preset_override,
    load_optimizer,
    load_tiers,
    models_dir,
    set_manual_override,
    set_preset_override,
    user_settings_path,
)
from .fit import estimate_fit
from .gate import evaluate_gate
from .hardware import probe_machine
from .solver import solve
from .preflight import (
    ADD_MODEL_HINT,
    RAG_DEGRADED_HINT,
    REQUIRED_MISSING_HINT,
    MissingArtifactError,
    run_doctor,
)
from .server import LlamaServer, stop_running_server


# ============================================================================
# CONTENTS
#   Fallback defaults · output helpers (_print_spec / _print_gate / _gib / ...)
#   Commands: start · probe · fit · solve · override · stop · check
#   main() - argparse subcommand table + dispatch
# ============================================================================

# Last-resort defaults when a tier profile in tiers.json is missing/malformed.
# The tier normally supplies both (profile["context"]["tokens"] / ["kv_precision"]);
# these apply only if that lookup falls through, so `fit`/`solve` still produce a
# usable estimate instead of crashing on a bad/incomplete tier.
_FALLBACK_CONTEXT_TOKENS = 16384
_FALLBACK_KV_PRECISION = "q8_0"


# --- output helpers ---------------------------------------------------------
def _print_spec(spec: LaunchSpec) -> None:
    print("Launch spec")
    print("  binary       :", spec.binary)
    print("  model        :", spec.model_file)
    print("  context      :", spec.context_tokens, "@", spec.kv_precision, "KV")
    print("  n_gpu_layers :", spec.n_gpu_layers)
    print("  endpoint     :", f"http://{spec.host}:{spec.port}")
    print("  argv         :", " ".join(spec.argv()))
    print("Rationale")
    for key, value in spec.rationale.items():
        if isinstance(value, dict):
            print(f"  {key:13}:")
            for k2, v2 in value.items():
                print(f"      {k2:11}: {v2}")
        else:
            print(f"  {key:13}:", value)
    if spec.warnings:
        print("Warnings")
        for w in spec.warnings:
            print(f"  ! {w}")


def _print_gate(decision) -> None:
    print("Fit gate")
    print(f"  verdict      : {decision.verdict}")
    print(f"  action       : {decision.action}")
    print(f"  {decision.headline}")
    if decision.detail:
        print(f"  {decision.detail}")
    if decision.suggestion:
        print(f"  {decision.suggestion}")


def _print_add_model() -> None:
    print(ADD_MODEL_HINT.format(models_dir=models_dir()))


# --- command: start (resolve a launch spec, gate it, run llama-server) ------
def cmd_start(args: argparse.Namespace) -> int:
    # Gate on REQUIRED (engine + config) only. Missing rerankers are non-blocking
    # (RAG degrades, chat works); a missing model is a clean 'add a model' message.
    report = run_doctor()
    if not report.required_ok:
        print("Cannot start -- the engine/config is missing:")
        for s in report.required_missing:
            print(f"  [{s.kind:8}] {s.id:30} {s.path}")
        print("\n" + REQUIRED_MISSING_HINT)
        return 1
    if not report.has_model:
        _print_add_model()
        return 0
    if not report.rag_ok:
        missing = ", ".join(s.id for s in report.rag_missing)
        print(f"Note: launching with RAG reranking degraded (missing: {missing}).")
        print("      Chat works; RAG retrieval reranking is unavailable until added.\n")

    try:
        auto = False if getattr(args, "no_auto", False) else None
        spec = build_launch_spec(tier=args.tier, model_id=args.model, auto=auto,
                                 preset=args.preset,
                                 manual_context=args.context,
                                 manual_n_gpu_layers=args.n_gpu_layers)
    except NoModelError:
        _print_add_model()
        return 0
    except ValueError as exc:
        print(f"Cannot resolve launch: {exc}")
        return 1
    _print_spec(spec)

    # Step-4 fit-gate: turn the solver verdict into the protect-end-user-hardware
    # boundary before we commit to loading anything.
    decision = evaluate_gate(spec)
    _print_gate(decision)

    if args.dry_run:
        print("Presence")
        print("  binary present:", spec.binary.exists())
        print("  model present :", spec.model_file.exists())
        print("\n[dry-run] not launching.")
        return 0

    # In 'refuse' mode, block an over-budget load unless forced. Managed (UI)
    # launches are gated by the bridge before we're spawned, so they pass through.
    if (decision.action == "refuse" and not getattr(args, "force", False)
            and not getattr(args, "managed", False)):
        print("\nNot launching: the fit gate is set to 'refuse' for over-budget models.")
        print("Re-run with --force to override, or pick a model that fits.")
        return 1

    server = LlamaServer(spec)
    print("\nStarting llama-server ...")
    try:
        pid = server.start()
    except NoModelError:
        _print_add_model()
        return 0
    except MissingArtifactError as exc:
        print(f"\n{exc}")
        return 1
    print(f"  pid {pid}; waiting for /health ...")
    try:
        if not server.wait_until_healthy():
            print("  did not become healthy within timeout; stopping.")
            return 1
        print(f"  healthy at {server.base_url}")
        if getattr(args, "managed", False):
            print("  managed by Ayre -- use the Stop button in the UI to unload the model.")
        else:
            print("Press Ctrl+C to stop.")
        try:
            server.proc.wait()
        except KeyboardInterrupt:
            print("\nStopping ...")
    finally:
        server.stop()
        print("  stopped.")
    return 0


# Shared GiB formatter for the probe/fit/solve inspector output below.
def _gib(num_bytes) -> str:
    return "n/a" if num_bytes is None else f"{num_bytes / (1024 ** 3):.2f} GiB"


# --- command: probe (report hardware + suggested tier) ----------------------
def cmd_probe(args: argparse.Namespace) -> int:
    # Step 1 of the optimizer: report the real hardware + suggested tier, with the
    # rationale for every value (so a tester sees WHY their machine landed where).
    p = probe_machine()
    print("Hardware probe")
    print(f"  OS           : {p.os_name}")
    print(f"  CPU          : {p.cpu_logical} logical (informational)")
    print(f"  RAM          : {_gib(p.ram_total_bytes)} total, {_gib(p.ram_available_bytes)} available")
    if p.gpus:
        print("  GPU(s)       :")
        for g in p.gpus:
            free = _gib(g.get("vram_free_bytes")) if g.get("vram_free_bytes") is not None else "free unknown"
            print(f"    - {g['name']} [{g['vendor']}] {_gib(g['vram_total_bytes'])} total, {free}  (via {g['source']})")
    else:
        print("  GPU(s)       : none detected (CPU-only)")
    print(f"  Primary VRAM : {_gib(p.primary_vram_total_bytes)} (the tier spine)")
    print(f"  Suggested tier: {p.suggested_tier or '(none)'}")
    print("Rationale")
    for key, value in p.rationale.items():
        print(f"  {key:18}: {value}")
    if p.warnings:
        print("Warnings")
        for w in p.warnings:
            print(f"  ! {w}")
    return 0


# --- command: fit (estimate memory footprint vs budget) --------------------
def cmd_fit(args: argparse.Namespace) -> int:
    # Step 2 of the optimizer: estimate whether the chosen model+context fits in
    # this machine's memory budget, or would spill to disk. Reuses the live probe.
    profile = probe_machine()
    models = discover_chat_models()
    if not models:
        _print_add_model()
        return 0

    if args.model:
        model_path = next((m for m in models if m.name == args.model), None)
        if model_path is None:
            print(f"Model '{args.model}' not found in models/. Detected: {[m.name for m in models]}")
            return 1
    else:
        model_path = models[0]

    tiers = load_tiers().get("tiers", {})
    tier = args.tier or profile.suggested_tier
    tprof = tiers.get(tier, {})
    tctx = tprof.get("context", {})
    context = args.context or tctx.get("tokens", _FALLBACK_CONTEXT_TOKENS)
    kv = args.kv_precision or tctx.get("kv_precision", _FALLBACK_KV_PRECISION)

    res = estimate_fit(model_path, context, kv, profile)

    print(f"Fit estimate -- {model_path.name}")
    print(f"  machine      : VRAM {_gib(profile.primary_vram_total_bytes)}"
          f" (free {_gib(profile.primary_vram_free_bytes)}), "
          f"RAM {_gib(profile.ram_total_bytes)} (avail {_gib(profile.ram_available_bytes)})")
    print(f"  config       : tier '{tier}', context {context} tok @ {kv} KV")
    print(f"  weights      : {_gib(res.weights_bytes)}")
    print(f"  KV cache     : {_gib(res.kv_bytes)}")
    print(f"  overhead     : {_gib(res.overhead_bytes)}")
    print(f"  FOOTPRINT    : {_gib(res.footprint_bytes)}")
    print(f"  budget       : VRAM {_gib(res.vram_budget_bytes)} + RAM {_gib(res.ram_budget_bytes)}"
          f" = {_gib(res.combined_budget_bytes)} usable")
    print(f"  VERDICT      : {res.verdict}  (fits without disk: {res.fits_without_disk})")
    if res.deficit_bytes:
        print(f"  over by      : {_gib(res.deficit_bytes)}")
    else:
        print(f"  slack        : {_gib(res.headroom_bytes)}")
    print("Rationale")
    for key, value in res.rationale.items():
        print(f"  {key:12}: {value}")
    if res.warnings:
        print("Warnings")
        for w in res.warnings:
            print(f"  ! {w}")
    return 0


# --- command: solve (show the GPU/CPU split + context the solver picks) -----
def cmd_solve(args: argparse.Namespace) -> int:
    # Step 3 inspector: show the GPU/CPU split + context the solver would launch
    # with on THIS machine, and why. This is exactly what `start` auto-applies.
    profile = probe_machine()
    models = discover_chat_models()
    if not models:
        _print_add_model()
        return 0

    tiers = load_tiers().get("tiers", {})
    tier = args.tier or profile.suggested_tier
    kv = args.kv_precision or tiers.get(tier, {}).get("context", {}).get("kv_precision", _FALLBACK_KV_PRECISION)

    if args.model:
        model_path = next((m for m in models if m.name == args.model), None)
        if model_path is None:
            print(f"Model '{args.model}' not found. Detected: {[m.name for m in models]}")
            return 1
    else:
        model_path, selection_reason = _pick_best_fitting_model(models, profile, kv, load_optimizer())
        print(f"(auto-selected: {selection_reason})\n")

    # Manual override: explicit flags win; otherwise preview the per-machine persisted
    # override so `solve` shows exactly what `start` would apply. Same for the preset:
    # no --preset -> the persisted per-machine choice (else config active_preset).
    m_ctx, m_ngl = args.context, args.n_gpu_layers
    from_flags = m_ctx is not None or m_ngl is not None
    if not from_flags:
        m_ctx, m_ngl = get_manual_override()
    preset = args.preset or get_preset_override()

    sr = solve(model_path, profile, kv_precision=kv, preset=preset,
               manual_context=m_ctx, manual_n_gpu_layers=m_ngl)

    tag = "  [preset: {}]".format(sr.preset_label)
    if sr.manual:
        src = "flags" if from_flags else "saved override"
        tag += f"  [manual: {src}]"
    print(f"Solver -- {model_path.name}{tag}")
    print(f"  split        : n_gpu_layers {sr.n_gpu_layers}/{sr.n_layers_total}"
          f"  ({round(100*sr.n_gpu_layers/sr.n_layers_total)}% on GPU)")
    print(f"  context      : {sr.context_tokens} tok @ {sr.kv_precision} KV"
          f"  (trained max {sr.context_trained_max})")
    print(f"  VRAM use     : {_gib(sr.vram_used_bytes)} / budget {_gib(sr.vram_budget_bytes)}")
    print(f"  RAM use      : {_gib(sr.ram_used_bytes)} / budget {_gib(sr.ram_budget_bytes)}")
    print(f"  VERDICT      : {sr.verdict}  (fits without disk: {sr.fits_without_disk})")
    if sr.disk_spill_bytes:
        print(f"  disk spill   : {_gib(sr.disk_spill_bytes)}  (would stream from disk)")
    print("Rationale")
    for key, value in sr.rationale.items():
        print(f"  {key:12}: {value}")
    if sr.warnings:
        print("Warnings")
        for w in sr.warnings:
            print(f"  ! {w}")
    return 0


# --- command: override (persist/show/clear per-machine optimizer choices) ---
def _fmt_override(ctx, ngl, preset=None) -> None:
    default_note = "(shipped default: optimizer.json active_preset)"
    print(f"  preset         : {preset if preset is not None else default_note}")
    print(f"  context_tokens : {ctx if ctx is not None else '(preset decides)'}")
    print(f"  n_gpu_layers   : {ngl if ngl is not None else '(auto-fit to VRAM)'}")


def cmd_override(args: argparse.Namespace) -> int:
    # A2/A3: persist per-machine optimizer choices -- the preset AND/OR a manual
    # context / GPU-split override (or clear/show them). Stored in
    # config/user_settings.json (gitignored, survives updates). `start` and the
    # UI honor them; run `cli solve` to preview the fit + any adverse-effect warnings.
    if args.clear:
        clear_manual_override()
        set_preset_override(None)
        print("Overrides cleared -- the shipped default preset drives context + split.")
        return 0

    if args.preset is not None:
        known = sorted(load_optimizer().get("solver", {}).get("presets", {}))
        if args.preset not in known:
            print(f"Unknown preset '{args.preset}'; known: {', '.join(known)}")
            return 1
        set_preset_override(args.preset)

    cur_ctx, cur_ngl = get_manual_override()
    cur_preset = get_preset_override()
    if args.context is None and args.n_gpu_layers is None:
        if args.preset is not None:
            print("Preset saved (per-machine):")
            _fmt_override(cur_ctx, cur_ngl, cur_preset)
        elif cur_ctx is None and cur_ngl is None and cur_preset is None:
            print("No override set -- the shipped default preset drives context + split.")
        else:
            print("Override (per-machine):")
            _fmt_override(cur_ctx, cur_ngl, cur_preset)
        print(f"Stored in: {user_settings_path()}")
        return 0

    new_ctx = args.context if args.context is not None else cur_ctx
    new_ngl = args.n_gpu_layers if args.n_gpu_layers is not None else cur_ngl
    set_manual_override(new_ctx, new_ngl)
    print("Override saved (per-machine):")
    _fmt_override(new_ctx, new_ngl, cur_preset)
    print("\nPreview it with `cli solve`; launch with it via `cli start` or the UI.")
    return 0


# --- command: stop (kill whatever holds the llama-server port) --------------
def cmd_stop(args: argparse.Namespace) -> int:
    # Stops whatever is serving the llama-server port -- works whether the engine
    # was launched here, from the UI, or in another terminal (we find it by port).
    result = stop_running_server()
    print(result["message"])
    return 0 if result["ok"] else 1


# --- command: check (two-tier doctor: required artifacts + chat model) ------
def cmd_check(args: argparse.Namespace) -> int:
    report = run_doctor()

    print("REQUIRED -- engine + config (nothing runs without these)")
    for s in report.required:
        mark = "OK     " if s.present else "MISSING"
        print(f"  [{mark}] {s.kind:8} {s.id:30} {s.path}")

    print("\nBUNDLED RAG -- rerankers (ship in the full download; non-blocking)")
    for s in report.rag:
        mark = "OK     " if s.present else "ABSENT "
        print(f"  [{mark}] {s.kind:8} {s.id:30} {s.path}")

    print("\nCHAT MODEL (you add this -- any GGUF that fits your tier)")
    if report.has_model:
        for p in report.models:
            print(f"  [DETECTED] {p.name}")
    else:
        print("  [none yet]")

    print()
    if not report.required_ok:
        print("Setup INCOMPLETE -- engine/config missing:")
        for s in report.required_missing:
            print(f"  - {s.kind} {s.id}")
        print("\n" + REQUIRED_MISSING_HINT)
        return 1

    # Required engine + config present -> Setup succeeds (exit 0) from here on,
    # whatever the RAG/model state. Surface, never block.
    if not report.rag_ok:
        missing = ", ".join(s.id for s in report.rag_missing)
        print(f"RAG reranking degraded -- rerankers absent: {missing}")
        print(RAG_DEGRADED_HINT.format(models_dir=models_dir()) + "\n")

    if not report.has_model:
        print("Engine + config present. No chat model yet:\n")
        _print_add_model()
        return 0

    ready = "Ayre is ready: engine + config present and a chat model is detected."
    if not report.rag_ok:
        ready += " (RAG reranking degraded until rerankers are added.)"
    print(ready)
    return 0


# --- entrypoint (argparse subcommand table + dispatch) ----------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ayre-setup")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="launch llama-server from config")
    p_start.add_argument("--tier", default=None, help="hardware tier (low/mid/high/ultra/max); default mid")
    p_start.add_argument("--model", default=None, help="a .gguf filename in models/ to launch; default = auto-selected (largest fitting model when auto-tune is on, else first discovered)")
    p_start.add_argument("--dry-run", action="store_true", help="print resolved spec + argv, do not launch")
    p_start.add_argument("--no-auto", action="store_true", help="disable the optimizer; use tier seed/fallback values")
    p_start.add_argument("--managed", action="store_true", help="launched by the Ayre bridge/UI; stop is driven by the UI, so don't advertise Ctrl+C")
    p_start.add_argument("--force", action="store_true", help="override the fit gate (launch even if over-budget / gate=refuse)")
    p_start.add_argument("--preset", default=None, help="optimizer preset: max_context | balanced | speed; default = config active_preset")
    p_start.add_argument("--context", default=None, type=int, help="manual override: context window in tokens (honored + warned if harmful); default = preset/persisted override")
    p_start.add_argument("--n-gpu-layers", dest="n_gpu_layers", default=None, type=int, help="manual override: layers to keep on GPU (honored + warned if harmful); default = preset/persisted override")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="stop the running llama-server (found by its port)")
    p_stop.set_defaults(func=cmd_stop)

    p_probe = sub.add_parser("probe", help="detect hardware (RAM/VRAM/CPU) + suggested tier")
    p_probe.set_defaults(func=cmd_probe)

    p_fit = sub.add_parser("fit", help="estimate whether a model+context fits memory (vs disk-spill)")
    p_fit.add_argument("--model", default=None, help="model filename in models/; default = first detected")
    p_fit.add_argument("--tier", default=None, help="tier whose context to assume; default = probed suggestion")
    p_fit.add_argument("--context", default=None, type=int, help="context tokens to test; default = tier's")
    p_fit.add_argument("--kv-precision", default=None, help="KV cache precision (f16/q8_0/q4_0); default = tier's")
    p_fit.set_defaults(func=cmd_fit)

    p_solve = sub.add_parser("solve", help="show the GPU/CPU split + context the optimizer would launch with")
    p_solve.add_argument("--model", default=None, help="model filename in models/; default = first detected")
    p_solve.add_argument("--tier", default=None, help="tier for KV-precision default; default = probed suggestion")
    p_solve.add_argument("--kv-precision", default=None, help="KV cache precision; default = tier's")
    p_solve.add_argument("--preset", default=None, help="optimizer preset: max_context | balanced | speed; default = config active_preset")
    p_solve.add_argument("--context", default=None, type=int, help="manual override: context window in tokens to evaluate (default = preset, or the saved override)")
    p_solve.add_argument("--n-gpu-layers", dest="n_gpu_layers", default=None, type=int, help="manual override: GPU layer count to evaluate (default = auto-fit, or the saved override)")
    p_solve.set_defaults(func=cmd_solve)

    p_override = sub.add_parser("override", help="save/show/clear the per-machine preset choice + manual context/GPU-split override")
    p_override.add_argument("--preset", default=None, help="optimizer preset to persist per-machine: max_context | balanced | speed")
    p_override.add_argument("--context", default=None, type=int, help="context window in tokens to pin (persisted per-machine)")
    p_override.add_argument("--n-gpu-layers", dest="n_gpu_layers", default=None, type=int, help="GPU layer count to pin (persisted per-machine)")
    p_override.add_argument("--clear", action="store_true", help="remove the saved overrides (back to the shipped default preset)")
    p_override.set_defaults(func=cmd_override)

    p_check = sub.add_parser("check", help="two-tier doctor: required artifacts + detected chat model")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
