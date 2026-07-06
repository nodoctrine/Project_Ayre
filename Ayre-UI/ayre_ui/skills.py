"""Custom skill storage + prompt-injection structure-safety helpers.

Skill text (title / description / workflow) is injected into the system prompt, so no field
may forge the prompt's own delimiters (_sanitize_skill_field). A skill's workflow becomes
instructions only when the user names the skill in a message (_skill_invocation_pattern).
"""
from __future__ import annotations

import json
import re

from ayre_setup.config import load_runtime

from .paths import _SKILLS_PATH

_SKILL_TITLE_MAX_WORDS = 5    # keep in sync with the UI counters in app.js
_SKILL_DESC_MAX_WORDS = 30
_SKILLS_MAX_COUNT_DEFAULT = 50  # overridable in config/runtime.json -> skills.max_count

def _load_skills() -> list[dict]:
    """Global custom skills from config/skills.json. Empty list on any error."""
    if not _SKILLS_PATH.exists():
        return []
    try:
        return json.loads(_SKILLS_PATH.read_text(encoding="utf-8")).get("skills", [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_skills(skills: list[dict]) -> None:
    """Atomically write the skills list back to config/skills.json."""
    _SKILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SKILLS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"skills": skills}, indent=2), encoding="utf-8")
    tmp.replace(_SKILLS_PATH)


def _skills_max_count() -> int:
    """Cap on stored custom skills. The manifest (title + description of every skill)
    is injected into EVERY chat turn, so unbounded skill count is the one growth
    vector the per-field word caps don't close -- same backstop role as
    _memory_max_chars. Read from config/runtime.json -> skills.max_count."""
    cfg = load_runtime().get("skills", {}) or {}
    v = cfg.get("max_count", _SKILLS_MAX_COUNT_DEFAULT)
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = _SKILLS_MAX_COUNT_DEFAULT
    return max(1, v)

# Skill text enters the system prompt, so no field may be able to forge the
# prompt's own structure (Security_Practices.md §9 -- same reasoning as
# filenames-as-DATA). Titles/descriptions live inside the <custom-skills>
# catalog block; invoked workflows live inside a [SKILL INVOKED]…[END SKILL
# WORKFLOW] block. Any field containing one of these marker prefixes could
# fake a block boundary, so it is rejected at save time.
_SKILL_FORGE_MARKERS = ("<custom-skills", "</custom-skills", "<files", "</files",
                        "[skill invoked", "[end skill workflow",
                        "[memory", "[end of memory")


def _sanitize_skill_field(text: str, *, single_line: bool) -> tuple[str | None, str | None]:
    """Validate one skill field for prompt-injection structure safety. Returns
    (cleaned_text, None) on success or (None, user-facing error) on rejection.
    Titles/descriptions are single-line and additionally forbid < and > (the
    _sanitize_filename policy: they render inside an angle-bracket data block).
    Workflows keep newlines and angle brackets (they may legitimately hold code),
    but the literal marker strings are still rejected."""
    cleaned = "".join(c for c in text if c == "\t" or c == "\n" or ord(c) >= 32)
    if single_line:
        cleaned = " ".join(cleaned.split())
        if "<" in cleaned or ">" in cleaned:
            return None, "Titles and descriptions cannot contain < or >."
    lowered = cleaned.lower()
    for marker in _SKILL_FORGE_MARKERS:
        if marker in lowered:
            return None, (f"This text can't include the sequence {marker!r} — it collides "
                          "with Ayre's internal prompt markers.")
    if not cleaned.strip():
        return None, "This field is empty after cleanup."
    return cleaned.strip(), None


def _skill_invocation_pattern(title: str) -> re.Pattern:
    """Exact-phrase, case-insensitive matcher for a skill title in a user message.
    Word-boundary anchored so short titles stop false-positiving as substrings
    ("Sum" no longer fires on "summarize"). (?<!\\w)/(?!\\w) instead of \\b so a
    title that starts or ends on punctuation still anchors; internal whitespace
    matches any whitespace run."""
    body = r"\s+".join(re.escape(w) for w in title.split())
    return re.compile(r"(?<!\w)" + body + r"(?!\w)", re.IGNORECASE)
