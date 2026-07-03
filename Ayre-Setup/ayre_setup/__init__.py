"""Ayre-Setup: installs and starts a local model on the destination machine.

v1 skeleton scope: the llama-server process-lifecycle wrapper (backend wrapper #2)
plus the config-driven launch-spec assembly. The hard parts deferred for now --
VRAM/RAM auto-detection + the layer-split calculator (wrapper #1) -- are seeded
from config until built. No machine-specific values live in code.
"""
