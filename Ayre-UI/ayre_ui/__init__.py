"""Ayre-UI (component 5) -- the single persistent shell.

v1 bones: a stdlib-only local HTTP bridge that serves the vendored static UI and
exposes a small JSON API wrapping the Setup 'doctor'. No pip deps, no build step,
no CDN -- the same offline posture as Ayre-Setup, so it runs in the clean VM.

Package bootstrap (below): makes the sibling ayre_setup / ayre_rag packages importable
with no install step (v1 runs from the folder, offline). This is the ONE place the UI
reaches into Setup + RAG; it lives in __init__ so it runs before any submodule imports
those packages (mirrors the bundled python's ._pth path entries).
"""
import sys
from pathlib import Path

_ON_DISK_ROOT = Path(__file__).resolve().parents[2]   # .../Ayre-USB (physical tree location)
for _seam_dir in (_ON_DISK_ROOT / "Ayre-Setup", _ON_DISK_ROOT / "Ayre-RAG"):
    _p = str(_seam_dir)
    if _p not in sys.path:
        sys.path.insert(0, _p)
