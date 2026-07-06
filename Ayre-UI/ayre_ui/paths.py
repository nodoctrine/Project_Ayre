"""Filesystem anchors for the Ayre-UI bridge -- the one place roots are derived.

The sys.path seam that makes the sibling ayre_setup / ayre_rag packages importable lives
in this package's __init__ (runs first); this module derives the paths everything else
builds on.
"""
from __future__ import annotations

from pathlib import Path

from ayre_setup import platform_layer

# On-disk location of this package's own files (shipped static assets live next to the code).
_AYRE_UI_DIR = Path(__file__).resolve().parents[1]      # .../Ayre-UI
STATIC_DIR = _AYRE_UI_DIR / "static"

# Authoritative Ayre-USB root: honors the AYRE_USB_ROOT override, identical to every other
# component. Was an independent parents[1].parent derivation that ignored the override
# (Major_Refactor_Plan.md §8.1). All DATA paths (config overlay, workspace, models) hang off it.
_AYRE_USB_ROOT = platform_layer.ayre_usb_root()
_AYRE_SETUP_DIR = _AYRE_USB_ROOT / "Ayre-Setup"         # cwd for the spawned `ayre_setup.cli start`
_USER_SETTINGS_PATH = _AYRE_USB_ROOT / "config" / "user_settings.json"
_SKILLS_PATH = _AYRE_USB_ROOT / "config" / "skills.json"
