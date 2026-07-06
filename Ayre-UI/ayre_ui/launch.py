"""Launch control: start / stop llama-server + the Setup optimizer/fit read-outs.

start_llama shells out to `python -m ayre_setup.cli start` so the CLI stays the single
source of launch logic, gating, and printed rationale; the bridge only kicks it off and
captures the resolved offload split (state.LAUNCH_INFO) for the hardware monitor.
"""
from __future__ import annotations

import subprocess
import sys

from ayre_setup.preflight import run_doctor
from ayre_setup.server import stop_running_server

from .paths import _AYRE_SETUP_DIR
from .llama import _llama_health
from .hwmon import _offload_from_spec
from . import state

# Tracks a UI-initiated launch so we can report 'already launching' and, later,
# drive a Stop control. The launched process IS `python -m ayre_setup.cli start`
# -- the CLI stays the single source of launch logic, gating, and printed
# rationale; the bridge only kicks it off and never reimplements it.
_LAUNCH_PROC: subprocess.Popen | None = None

def fit_check(model: str | None = None, context: int | None = None,
              n_gpu_layers: int | None = None, preview: bool = False) -> dict:
    """Assess a model's hardware fit WITHOUT launching -- powers the Setup view's
    pre-Start warning, so an over-budget pick is flagged the moment it's chosen in
    the dropdown rather than only after Start (where it scrolled past). Runs the
    same path the launch gate uses (build_launch_spec -> evaluate_gate); read-only
    and fail-open, so a probe/GGUF-read hiccup degrades to 'couldn't assess'
    (verdict 'unknown') and never a false block. `model` is a detected .gguf
    filename or None for the tier's auto-pick.

    A3 what-if preview: with `preview` True the persisted manual override is
    IGNORED and exactly `context`/`n_gpu_layers` are evaluated (either may be None
    = that field defers to the active preset) -- so typing a hypothetical in the
    UI's manual-override inputs never mixes with the saved state. Default (no
    preview) reflects precisely what Start would launch: saved preset + saved
    override. The response carries the solver's `fit` numbers + `warnings` so the
    UI can render the live tradeoff (GPU %, context, VRAM/RAM vs budget)."""
    try:
        from ayre_setup.config import build_launch_spec
        from ayre_setup.gate import evaluate_gate
        if preview:
            spec = build_launch_spec(model_id=model, manual_context=context,
                                     manual_n_gpu_layers=n_gpu_layers,
                                     use_saved_override=False)
        else:
            spec = build_launch_spec(model_id=model)
        decision = evaluate_gate(spec)
    except Exception as exc:  # noqa: BLE001 -- any failure is a non-judgement, not a block
        return {"ok": False, "action": "allow", "verdict": "unknown",
                "error": f"Couldn't assess fit: {exc}"}
    # `resolved_model` is the file the launch would actually load -- for model=None
    # ("Auto") this is the optimizer's tier-aware pick, so the UI can name it.
    return {"ok": True, "model": model,
            "resolved_model": spec.model_file.name,
            "fit": spec.fit,                  # solver numbers (None if auto-tune off)
            "warnings": list(spec.warnings),  # solver warnings (clamps, OOM, CPU-bound…)
            **decision.to_dict()}


def optimizer_state() -> dict:
    """The optimizer controls' state for the Setup view (A3): the selectable
    presets (labels + rationale straight from config -- document-tier-reasoning),
    which one is active (per-machine choice, else the shipped default), and the
    saved manual override. Read via Ayre-Setup, which owns the `optimizer` block
    in the shared user_settings.json overlay."""
    try:
        from ayre_setup.config import (get_manual_override, get_preset_override,
                                       load_optimizer)
        sv = load_optimizer().get("solver", {})
        presets = [
            {"key": key, "label": cfg.get("label", key),
             "rationale": cfg.get("rationale", ""),
             "context_cap_tokens": cfg.get("context_cap_tokens"),
             "offload_goal": cfg.get("offload_goal", "fit")}
            for key, cfg in (sv.get("presets", {}) or {}).items()
        ]
        default_preset = sv.get("active_preset") or "max_context"
        saved_preset = get_preset_override()
        ctx, ngl = get_manual_override()
        return {"ok": True, "presets": presets,
                "active_preset": saved_preset or default_preset,
                "default_preset": default_preset,
                "preset_saved": saved_preset is not None,
                "override": {"context_tokens": ctx, "n_gpu_layers": ngl},
                "context_floor_tokens": sv.get("context_floor_tokens")}
    except Exception as exc:  # noqa: BLE001 -- report, never crash the Setup view
        return {"ok": False, "error": f"Couldn't read optimizer config: {exc}"}


def preset_predictions(model: str | None = None) -> dict:
    """Per-preset on-this-hardware outcomes for the Setup optimizer controls: one
    launch-spec resolution per preset (pure preset -- the saved manual override is
    ignored) so the preset hover text/rationale can show what each choice ACTUALLY
    does on the detected hardware (predicted split, context, verdict), not just
    the static config rationale. Doubles as a diagnostic: three identical
    predictions = the solver, not the preset plumbing. Read-only + fail-open like
    fit_check; ~1s per preset (probe + GGUF read), so the UI fetches it async and
    caches per model pick."""
    try:
        from ayre_setup.config import build_launch_spec, load_optimizer
        keys = list(load_optimizer().get("solver", {}).get("presets", {}) or {})
        preds: dict = {}
        resolved = None
        for key in keys:
            spec = build_launch_spec(model_id=model, preset=key,
                                     use_saved_override=False)
            resolved = spec.model_file.name
            f = spec.fit
            preds[key] = None if not f else {
                "n_gpu_layers": f.get("n_gpu_layers"),
                "n_layers_total": f.get("n_layers_total"),
                "context_tokens": f.get("context_tokens"),
                "verdict": f.get("verdict"),
                "vram_used_bytes": f.get("vram_used_bytes"),
                "vram_budget_bytes": f.get("vram_budget_bytes"),
                "ram_used_bytes": f.get("ram_used_bytes"),
                "ram_budget_bytes": f.get("ram_budget_bytes"),
            }
        return {"ok": True, "model": model, "resolved_model": resolved,
                "predictions": preds}
    except Exception as exc:  # noqa: BLE001 -- tooltips degrade, never break Setup
        return {"ok": False, "error": f"Couldn't predict preset outcomes: {exc}"}


def save_optimizer_settings(payload: dict) -> dict:
    """Persist the UI's optimizer choices per-machine (user_settings.json ->
    `optimizer`, the block Ayre-Setup owns). Two independent keys:
      {"preset": "<key>"}                       -- save the preset choice
      {"manual": {"context_tokens": N|null,
                  "n_gpu_layers": K|null}}      -- save the manual override
      {"manual": null}                          -- clear the manual override
    Absent keys are left untouched, so the UI can save each control on its own.
    The solver HONORS a manual value and warns when it's harmful (user-control-
    is-core) -- validation here is shape-only (ints, known preset key)."""
    try:
        from ayre_setup.config import (clear_manual_override, load_optimizer,
                                       set_manual_override, set_preset_override)
        if "preset" in payload:
            preset = payload.get("preset")
            if not isinstance(preset, str):
                return {"ok": False, "error": "preset must be a string."}
            known = sorted(load_optimizer().get("solver", {}).get("presets", {}))
            if preset not in known:
                return {"ok": False,
                        "error": f"Unknown preset '{preset}' -- known: {', '.join(known)}."}
            set_preset_override(preset)
        if "manual" in payload:
            manual = payload.get("manual")
            if manual is None:
                clear_manual_override()
            elif isinstance(manual, dict):
                ctx = manual.get("context_tokens")
                ngl = manual.get("n_gpu_layers")
                # bool is an int subclass -- reject it explicitly.
                if ctx is not None and (isinstance(ctx, bool) or not isinstance(ctx, int) or ctx < 1):
                    return {"ok": False, "error": "context_tokens must be a positive whole number (or null)."}
                if ngl is not None and (isinstance(ngl, bool) or not isinstance(ngl, int) or ngl < 0):
                    return {"ok": False, "error": "n_gpu_layers must be a whole number ≥ 0 (or null)."}
                if ctx is None and ngl is None:
                    clear_manual_override()
                else:
                    set_manual_override(ctx, ngl)
            else:
                return {"ok": False, "error": "manual must be an object or null."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Couldn't save: {exc}"}
    return optimizer_state()


def start_llama(model: str | None = None, force: bool = False) -> dict:
    """Kick off `python -m ayre_setup.cli start` from the UI (Setup Start button).

    `model` (optional) is a detected .gguf filename to launch instead of the
    tier's auto-pick; it is passed through as `--model`. Pre-checks the doctor for
    instant, honest feedback -- a missing engine/config or chat model is reported
    here instead of spawning a process that would just print and exit (the CLI
    re-checks; it stays the authority). Non-blocking: the launch runs in the
    background and the topbar llama-server chip (a live /health ping) flips to
    'up' once the model finishes loading."""
    global _LAUNCH_PROC

    # Already up? Don't double-launch.
    if _llama_health()["healthy"]:
        return {"ok": True, "already_running": True,
                "message": "llama-server is already running."}

    # A launch we started is still booting (a cold model load takes tens of secs).
    if _LAUNCH_PROC is not None and _LAUNCH_PROC.poll() is None:
        return {"ok": True, "launching": True,
                "message": "Already launching -- waiting for llama-server to come up."}

    report = run_doctor()
    if not report.required_ok:
        return {"ok": False,
                "error": "Engine/config missing -- see Setup's Required section."}
    if not report.has_model:
        return {"ok": False,
                "error": "No chat model yet. Drop a .gguf into the models folder, then Start."}

    if model and model not in {p.name for p in report.models}:
        # Only allow an actually-detected model file (the dropdown's source); keeps
        # the passthrough honest and gives a clean error on a stale/odd value.
        return {"ok": False, "error": f"Unknown model '{model}' -- pick one from the list."}

    # Step-4 fit-gate (protect-end-user-hardware): assess the launch BEFORE spawning
    # so the UI can report an over-budget model instead of starting a disk-thrashing
    # load. Fail-open -- a gate-eval hiccup must never block a legitimate launch.
    gate_warning = None
    spec = None
    try:
        from ayre_setup.config import build_launch_spec
        from ayre_setup.gate import evaluate_gate
        spec = build_launch_spec(model_id=model)
        decision = evaluate_gate(spec)
    except Exception:
        decision = None
    if decision is not None and decision.action == "refuse" and not force:
        return {"ok": False, "gate": "refuse", "error": decision.message()}
    if decision is not None and decision.verdict == "over_budget":
        gate_warning = decision.message()

    # Remember the resolved offload split for the hardware monitor. The CLI spawned
    # below resolves its OWN spec a moment later, but both probe before the model
    # loads (same free memory), so this matches what actually launches.
    state.LAUNCH_INFO = _offload_from_spec(spec) if spec is not None else None

    cmd = [sys.executable, "-m", "ayre_setup.cli", "start", "--managed"]
    if model:
        cmd += ["--model", model]
    if force:
        cmd += ["--force"]

    # cwd = the Setup folder so `-m ayre_setup.cli` resolves; inherit stdio so the
    # CLI's launch spec + tier rationale stay visible in the bridge's terminal.
    try:
        _LAUNCH_PROC = subprocess.Popen(cmd, cwd=str(_AYRE_SETUP_DIR))
    except OSError as exc:
        return {"ok": False, "error": f"Could not launch: {exc}"}

    which = f"'{model}'" if model else "the tier's default model"
    msg = (f"Starting llama-server with {which} -- this can take a moment while the "
           "model loads. Watch the llama-server chip.")
    resp = {"ok": True, "launching": True, "pid": _LAUNCH_PROC.pid, "message": msg}
    if gate_warning:
        resp["warning"] = gate_warning
        resp["message"] = msg + "  ⚠ " + gate_warning
    return resp


def stop_llama() -> dict:
    """Stop llama-server from the UI (Setup Stop button), the other half of
    Start. Delegates to ayre_setup's `stop_running_server`, which finds the engine by
    its port -- so this works whether we launched it, the CLI did, or it was
    started in another terminal (the orphan case the handoff noted). Then we reap
    our own CLI wrapper: once its llama-server child dies it exits on its own, but
    we don't leave it hanging if it lingers."""
    global _LAUNCH_PROC

    result = stop_running_server()
    state.LAUNCH_INFO = None  # the split no longer describes anything running

    if _LAUNCH_PROC is not None:
        try:
            # The wrapper's `server.proc.wait()` returns as soon as llama dies, so
            # give it a brief grace period to exit cleanly, then terminate it.
            _LAUNCH_PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if _LAUNCH_PROC.poll() is None:
                _LAUNCH_PROC.terminate()
        except OSError:
            pass
        _LAUNCH_PROC = None

    return result
