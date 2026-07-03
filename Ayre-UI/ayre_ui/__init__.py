"""Ayre-UI (component 5) -- the single persistent shell.

v1 bones: a stdlib-only local HTTP bridge that serves the vendored static UI and
exposes a small JSON API wrapping the Setup 'doctor'. No pip deps, no build step,
no CDN -- the same offline posture as Ayre-Setup, so it runs in the clean VM.
"""
