"""Shared mutable state for the Ayre-UI bridge.

The two module globals that genuinely cross module boundaries (the Python analog of the
front-end's window.Ayre shared namespace). Every other module-level mutable stays private
to the module that owns it.

  last_draft_content  -- written by tools (save_memory), reset by memory promote/discard
  LAUNCH_INFO         -- set by launch.start_llama, cleared by stop_llama, read by hwmon
"""
from __future__ import annotations

last_draft_content: str | None = None      # dedup: skip consecutive identical save_memory draft appends

# The resolved offload split of the launch WE kicked off (model, n_gpu_layers,
# total layers, context). llama-server doesn't report n_gpu_layers back, so this
# captured-at-launch value is the only truth for the hardware monitor's split.
# Cleared on Stop; cross-checked against the loaded model name before display, so
# a CLI-launched model or a bridge restart honestly shows "split unknown".
LAUNCH_INFO: dict | None = None
