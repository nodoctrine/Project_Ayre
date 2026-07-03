# Ayre-Setup (component 1)

Installs and starts a local model on the destination machine. Offline-capable,
stdlib-only (no pip deps), so it runs in a clean VM.

## What's built

- **Three-tier doctor** (`preflight.py`): required (engine + config) / bundled
  rerankers (non-blocking) / user-added chat model. Powers `check` and the
  `start` gate.
- **Optimizer pipeline** — wrapper **#1**: hardware probe (`hardware.py`) → fit
  estimate (`fit.py`) → split/context solver (`solver.py`) → fit gate (`gate.py`),
  reading GGUF metadata (`gguf.py`). Auto-applied on `start` when
  `config/optimizer.json -> solver.auto_apply_on_start` is true (it is); inspect
  via `cli probe | fit | solve`.
- **Process lifecycle** — wrapper **#2** (`server.py`): launch `llama-server` with
  config-assembled flags, track PID, start/stop/restart, health-check, clean
  shutdown. Plus the **config-driven launch-spec assembly** (`config.py`).
- **Model library** — wrapper **#3**: chat models auto-discovered from `models/`
  (`discover_chat_models()`), rerankers excluded via `config/rerankers.json`.

Fallback path: if the GGUF/probe can't be read, the split falls back to
`config/tiers.json -> <tier>.offload_split_seed.n_gpu_layers` (unset for mid →
`runtime.json -> fallback_n_gpu_layers`).

## Package layout

    ayre_setup/
      platform_layer.py   OS seam: binary name, launch flags, terminate, GPU/RAM probe, root resolution
      config.py           loads tiers/rerankers/runtime/optimizer; assembles an inspectable LaunchSpec
      hardware.py         hardware probe -> machine profile + suggested tier
      gguf.py             reads GGUF metadata (layers, KV dims, trained context) without loading the model
      fit.py              footprint + three-way memory-budget fit estimate
      solver.py           GPU/CPU split + right-sized context solver (effectiveness-first)
      gate.py             step-4 fit gate (warn / refuse / allow on over-budget)
      preflight.py        three-tier doctor (required / bundled rerankers / chat model)
      server.py           LlamaServer: start/stop/restart, health-check, clean shutdown
      cli.py              check / probe / fit / solve / start / stop

## Usage

Run from this directory (so the `ayre_setup` package is importable):

    # Verify required artifacts are present (the three-tier 'doctor' check):
    python -m ayre_setup.cli check

    # Verify the assembled launch command without launching anything:
    python -m ayre_setup.cli start --dry-run

    # Launch the mid-tier default model (needs the binary + .gguf present):
    python -m ayre_setup.cli start

    # Other tier / explicit model:
    python -m ayre_setup.cli start --tier low
    python -m ayre_setup.cli start --model qwen3-8b-q4_k_m

## What it needs to actually launch

- `llama-server(.exe)` placed at `<Ayre-USB>/bin/` (see `runtime.json -> bin_dir`).
- Any non-reranker `.gguf` placed in `<Ayre-USB>/models/` — auto-discovered (no
  manifest); rerankers are identified via `config/rerankers.json`.

`AYRE_USB_ROOT` env var overrides root detection (the USB may mount anywhere).
