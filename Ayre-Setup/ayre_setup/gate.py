"""Fit-gate -- step 4 of the optimizer (the protect-end-user-hardware boundary).

Turns the solver's verdict (carried on a LaunchSpec's `fit` summary) into a launch
decision: when a model would stream weights from disk (verdict over_budget ->
thrash), say so in plain language BEFORE the launch commits, and either
warn-and-proceed or refuse -- config-driven via optimizer.json -> gate. Pure and
inspectable: a spec in, a GateDecision out; the CLI and the bridge decide how to
surface it (console text vs. a JSON field the UI renders).
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import load_optimizer
from .fit import _gib


@dataclass
class GateDecision:
    action: str       # "allow" | "warn" | "refuse"
    verdict: str      # solver verdict this was based on ("unknown" if no fit data)
    headline: str     # one-line plain-language summary
    detail: str       # the "this will cost your machine X" explanation ("" if none)
    suggestion: str   # how to get a clean fit ("" if none)

    @property
    def ok_to_launch(self) -> bool:
        return self.action != "refuse"

    def message(self) -> str:
        """Headline + detail + suggestion as one surfacing-ready string."""
        return " ".join(p for p in (self.headline, self.detail, self.suggestion) if p)

    def to_dict(self) -> dict:
        """Serialisable form for the bridge's /api/fit (the UI's pre-launch warning).
        Keeps headline/detail/suggestion split so the UI can lay them out, plus the
        joined `message` for plain-text consumers."""
        return {
            "action": self.action,
            "verdict": self.verdict,
            "headline": self.headline,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "message": self.message(),
            "ok_to_launch": self.ok_to_launch,
        }


def _gib_s(num_bytes) -> str:
    g = _gib(num_bytes)
    return "n/a" if g is None else f"{g:.2f} GiB"


def evaluate_gate(spec, optimizer_cfg: dict | None = None) -> GateDecision:
    """Decide whether `spec` is safe to launch on this machine.

    Reads the solver's fit summary attached to the spec (LaunchSpec.fit). With no
    fit data (auto-tune off, or the GGUF/probe couldn't be read) the gate cannot
    judge, so it ALLOWS with a note rather than blocking a legitimate launch.
    """
    opt = optimizer_cfg or load_optimizer()
    mode = opt.get("gate", {}).get("on_over_budget", "warn")
    if mode not in ("warn", "refuse", "allow"):
        mode = "warn"

    fit = getattr(spec, "fit", None)
    if not fit:
        return GateDecision(
            action="allow", verdict="unknown",
            headline="Fit not assessed -- launching without a hardware-fit check.",
            detail="The optimizer couldn't size this model (auto-tune off, or the GGUF "
                   "couldn't be read), so disk-thrash risk wasn't evaluated.",
            suggestion="Run `cli solve` to inspect the fit, or enable auto-tune in optimizer.json.")

    verdict = fit.get("verdict", "unknown")
    if verdict != "over_budget":
        return GateDecision(
            action="allow", verdict=verdict,
            headline="Fits within your memory budget -- no disk-thrash expected.",
            detail="", suggestion="")

    spill = int(fit.get("disk_spill_bytes", 0) or 0)
    ctx = fit.get("context_tokens")
    combined = int(fit.get("vram_budget_bytes", 0) or 0) + int(fit.get("ram_budget_bytes", 0) or 0)

    # A manual override can blow VRAM (won't allocate -> load-time OOM) rather than
    # RAM (disk-thrash). Frame the pre-launch warning honestly for each case.
    limit = fit.get("limit") if fit.get("manual") else None
    if limit == "vram":
        ngl = fit.get("n_gpu_layers")
        detail = (
            f"You pinned {ngl} layers to the GPU at a {ctx}-token context, which needs more "
            f"VRAM than is safely available. llama-server will most likely fail to load "
            f"(a VRAM out-of-memory error), so the model won't start at all.")
        suggestion = ("To load it: lower n_gpu_layers, reduce the context, or clear the "
                      "manual override to let the optimizer size the split for you.")
        if mode == "allow":
            headline = "Over VRAM budget (allowed by config) -- load may OOM."
        elif mode == "refuse":
            headline = "Refused: this manual split would exceed VRAM and fail to load (OOM)."
        else:
            headline = "Heads up -- this manual split likely exceeds VRAM and won't load (OOM)."
        return GateDecision(action=mode, verdict=verdict,
                            headline=headline, detail=detail, suggestion=suggestion)

    over_both = " It also exceeds VRAM, so the split may fail to load." if limit == "both" else ""
    detail = (
        f"This model is over your current memory budget: about {_gib_s(spill)} of weights "
        f"can't fit RAM+VRAM and will stream from disk on every token (disk-thrash). "
        f"Expect slow replies, heavy SSD wear, and only a small {ctx}-token context window "
        f"(which is what cuts long answers off).{over_both}")
    suggestion = (
        f"To run cleanly: close other apps to free RAM, or use a smaller / "
        f"more-quantized GGUF -- aim for a model file well under ~{_gib_s(combined)} "
        f"(your usable RAM+VRAM right now), which leaves room for context.")

    if mode == "allow":
        headline = "Over budget (allowed by config) -- this will use disk heavily."
    elif mode == "refuse":
        headline = "Refused: this model would stream from disk and thrash your machine."
    else:
        headline = "Heads up -- this model is over budget and will use disk heavily."
    return GateDecision(action=mode, verdict=verdict,
                        headline=headline, detail=detail, suggestion=suggestion)
