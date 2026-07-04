"""Three-tier presence check -- the 'doctor'.

REFRAMED 2026-06-15 ('Setup Offline Reframe'); reranker tier split out 2026-06-17.
Setup is 100% offline; it never fetches anything. The doctor just reports what is
on disk, split into three tiers by what each blocks:

  REQUIRED   -- the engine + config: the llama-server binary (+CUDA DLLs) and
                config (tiers.json, runtime.json). Without these NOTHING runs
                (llama.cpp *is* the inference engine). Missing -> HARD ERROR.
  BUNDLED RAG -- the BGE rerankers. They DO ship in the full download (RAG infra),
                but their absence must NOT block launch: chat / Memory / agents
                work fine without them; only RAG retrieval reranking degrades.
                Non-blocking by design (so a phased/partial install still chats).
  CHAT MODEL -- the ONE thing the user adds (universal-model-support): ANY
                non-reranker .gguf in models/. Missing is NOT an error -- Setup
                succeeds and shows a friendly 'add a model to boot' alert.

The bare-bones minimum to run is therefore: the engine + config (REQUIRED) plus a
chat model the user drops in. Rerankers ship with the full product but are not on
the critical path to a working UI + local model.

All download/network logic lives in the (deferred) Updater, never here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    LaunchSpec,
    NoModelError,
    config_dir,
    discover_chat_models,
    load_coaching,
    load_rerankers,
    models_dir,
    moe_coaching_for,
    quant_coaching_for,
    reranker_items,
)
from .gguf import GGUFError, read_model_info


ADD_MODEL_HINT = (
    "Ayre is set up. Add a model to boot.\n"
    "Drop any GGUF chat model into:\n"
    "  {models_dir}\n"
    "Any GGUF that fits your hardware tier works -- which model is your choice."
)

REQUIRED_MISSING_HINT = (
    "The engine + config must be present to run anything. The llama-server binary "
    "(+CUDA DLLs) is too large for git and ships on the Ayre Releases page; config "
    "comes with the git clone. If you cloned from GitHub, grab the binary assets "
    "from the Release and see USB_PREP.md. If this is a USB drive, it may "
    "be incompletely assembled. (Setup never downloads anything.)"
)

RAG_DEGRADED_HINT = (
    "RAG reranking is unavailable until the BGE reranker(s) are added.\n"
    "Chat, Memory, and agents work fine without them -- only RAG retrieval is\n"
    "affected (no rerank / relevance-threshold gating). Rerankers ship in the full\n"
    "Ayre download (Releases page); drop them into:\n"
    "  {models_dir}"
)


class MissingArtifactError(FileNotFoundError):
    """Raised when a REQUIRED artifact is absent. Message is user-facing."""


@dataclass
class ArtifactStatus:
    kind: str   # 'binary' | 'config' | 'reranker'
    id: str
    path: Path
    present: bool

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "id": self.id,
            "path": str(self.path),
            "present": self.present,
        }


@dataclass
class DoctorReport:
    """Result of the three-tier presence check -- inspectable + displayable.

    Only `required` (engine + config) gates launch. `rag` (rerankers) is bundled
    but non-blocking; `models` is the user-added chat model.
    """

    required: list[ArtifactStatus] = field(default_factory=list)
    rag: list[ArtifactStatus] = field(default_factory=list)        # bundled rerankers
    models: list[Path] = field(default_factory=list)              # detected chat models

    @property
    def required_missing(self) -> list[ArtifactStatus]:
        return [s for s in self.required if not s.present]

    @property
    def required_ok(self) -> bool:
        return not self.required_missing

    @property
    def rag_missing(self) -> list[ArtifactStatus]:
        return [s for s in self.rag if not s.present]

    @property
    def rag_ok(self) -> bool:
        """All bundled rerankers present (full RAG). Absence is non-blocking."""
        return not self.rag_missing

    @property
    def has_model(self) -> bool:
        return bool(self.models)

    def to_dict(self) -> dict:
        """JSON-serializable view -- the shape Ayre-UI's /api/doctor returns.

        Chat models and rerankers-on-disk are both included in `models`, but
        rerankers carry selectable=false and a reason so the UI can show them
        as non-launchable entries rather than hiding them entirely.
        """
        rerankers_cfg = load_rerankers()
        reranker_map = {r["file"]: r["reason"] for r in reranker_items(rerankers_cfg)}

        # Reranker GGUFs that are physically present in models/
        present_rerankers = [
            {"name": filename, "path": str(models_dir() / filename),
             "selectable": False, "reason": reason}
            for filename, reason in reranker_map.items()
            if (models_dir() / filename).exists()
        ]

        # Attach the coaching chips (config/coaching.json) so the Setup view can
        # flag each model's tradeoffs: `quant` from the filename, `moe` from the
        # GGUF metadata (a header-only read -- fast, and this method only runs
        # for /api/doctor, not the 8s /api/system poll). Either key is omitted
        # when it doesn't apply / can't be read -- the UI simply shows no chip.
        coaching = load_coaching()
        chat_models = []
        for p in self.models:
            entry = {"name": p.name, "path": str(p), "selectable": True}
            quant = quant_coaching_for(p.name, coaching)
            if quant:
                entry["quant"] = quant
            try:
                moe = moe_coaching_for(read_model_info(p), coaching)
            except (GGUFError, OSError):
                moe = None                 # unreadable GGUF -> just no chip
            if moe:
                entry["moe"] = moe
            chat_models.append(entry)

        return {
            "required": [s.to_dict() for s in self.required],
            "required_missing": [s.to_dict() for s in self.required_missing],
            "required_ok": self.required_ok,
            "rag": [s.to_dict() for s in self.rag],
            "rag_missing": [s.to_dict() for s in self.rag_missing],
            "rag_ok": self.rag_ok,
            "models": chat_models + present_rerankers,
            "has_model": self.has_model,
            "hints": {
                "required_missing": REQUIRED_MISSING_HINT,
                "rag_degraded": RAG_DEGRADED_HINT.format(models_dir=models_dir()),
                "add_model": ADD_MODEL_HINT.format(models_dir=models_dir()),
            },
        }


def binary_path() -> Path:
    """The llama-server binary this machine would launch. Routes through the
    (OS, GPU vendor) -> build seam (config.resolve_binary_path) so the doctor and
    the launch-spec builder never disagree on which file is 'the' binary. The
    doctor passes no vendor -> the per-OS default build (a GPU probe just to locate
    a file would be wasteful; on v1's single Windows/NVIDIA build the default is
    the shipping binary regardless)."""
    from .config import resolve_binary_path
    path, _ = resolve_binary_path()
    return path


def required_artifacts() -> list[ArtifactStatus]:
    """The engine + config -- the only tier whose absence blocks launch."""
    statuses: list[ArtifactStatus] = []

    b = binary_path()
    statuses.append(
        ArtifactStatus("binary", b.name, b, b.exists())
    )

    for name in ("tiers.json", "runtime.json"):
        p = config_dir() / name
        statuses.append(ArtifactStatus("config", name, p, p.exists()))

    return statuses


def rag_artifacts(rerankers: dict | None = None) -> list[ArtifactStatus]:
    """The bundled BGE rerankers -- shipped with the full download, but non-blocking.
    Absent -> RAG reranking degrades; chat / Memory / agents are unaffected."""
    rerankers = rerankers or load_rerankers()
    statuses: list[ArtifactStatus] = []
    for r in reranker_items(rerankers):
        p = models_dir() / r["file"]
        statuses.append(ArtifactStatus("reranker", r["id"], p, p.exists()))
    return statuses


def run_doctor() -> DoctorReport:
    """The three-tier presence check. Pure: reads disk, decides nothing."""
    rerankers = load_rerankers()
    return DoctorReport(
        required=required_artifacts(),
        rag=rag_artifacts(rerankers),
        models=discover_chat_models(rerankers),
    )


def preflight_launch(spec: LaunchSpec) -> None:
    """Launch-time backstop. Required-missing -> hard error; absent model -> the
    'add a model' message (NoModelError) -- NOT a required-artifact failure."""
    if not spec.binary.exists():
        raise MissingArtifactError(
            "Cannot launch -- the llama-server binary is missing:\n"
            f"  [binary] {spec.binary}\n\n" + REQUIRED_MISSING_HINT
        )
    if not spec.model_file.exists():
        raise NoModelError(
            ADD_MODEL_HINT.format(models_dir=models_dir())
        )
