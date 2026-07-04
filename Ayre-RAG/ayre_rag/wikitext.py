"""Strip MediaWiki wikitext to clean, paragraph-separated plain text.

Crude-but-honest (plan §6a): regex-based, deliberately lossy on deeply-nested
templates rather than pretending to be a full wikitext parser. What survives is the
prose a reader would see; markup, citations, tables, and file/category plumbing are
removed. The chunker downstream only needs readable paragraphs, and BM25 ranks on
words -- a stray artifact costs a little index noise, never correctness.

Order matters (each step assumes the previous ran):
  1. HTML comments (may hold stray braces/half-tags that would corrupt later steps).
  2. templates {{…}} and tables {|…|} via a nesting-aware brace scanner -- BEFORE
     refs, because a <ref> between a template's braces carries a }} that, removed
     first, would orphan the outer template (the {{Infobox leak).
  3. <ref>…</ref> / <ref …/> citations not already inside a removed template.
  4. wikilinks resolved innermost-first: [[File/Image/Category:…]] dropped whole,
     [[t|display]] -> display, [[t]] -> t. External [url text] -> text.
  5. residual HTML tags, bold/italic quotes, == headings ==, list markers.
  6. HTML entities unescaped; whitespace collapsed to blank-line-separated paragraphs.
"""
from __future__ import annotations

import html
import re

# --- precompiled patterns (module-level: strip_wikitext runs millions of times) ---
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_REF_PAIR = re.compile(r"<ref\b[^>]*>.*?</ref>", re.DOTALL | re.IGNORECASE)
_REF_SELF = re.compile(r"<ref\b[^>]*/>", re.IGNORECASE)
# innermost wikilink: [[ … ]] containing no further brackets.
_WIKILINK = re.compile(r"\[\[([^\[\]]*)\]\]")
_FILE_NS = re.compile(r"^\s*(?:File|Image|Category)\s*:", re.IGNORECASE)
_EXT_LINK_TEXT = re.compile(r"\[(?:https?:|ftp:|//)[^\s\]]+\s+([^\]]*)\]", re.IGNORECASE)
_EXT_LINK_BARE = re.compile(r"\[(?:https?:|ftp:|//)[^\]]*\]", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_BOLD_ITALIC = re.compile(r"'{2,5}")
_HEADING = re.compile(r"^\s*={2,}\s*(.*?)\s*={2,}\s*$", re.MULTILINE)
_MAGIC_WORD = re.compile(r"__[A-Z]+__")
_LIST_MARKER = re.compile(r"^[*#:;]+\s*", re.MULTILINE)
_TABLE_LEFTOVER = re.compile(r"^\s*[|!].*$", re.MULTILINE)  # orphaned table rows/cells
# collapse runs of space / tab / non-breaking space (\xa0, what html.unescape emits
# for &nbsp;) into a single space:
_HORIZ_SPACE = re.compile(r"[^\S\n]+")
_TRIM_LINES = re.compile(r" *\n *")
_BLANKS = re.compile(r"\n{3,}")

_MAX_UNWIND = 12  # nesting passes for wikilinks before giving up (lossy, honest)


def _strip_balanced(text: str, opener: str, closer: str) -> str:
    """Remove balanced `opener…closer` spans (e.g. '{{'…'}}' templates or '{|'…'|}'
    tables), handling arbitrary NESTING in a single linear pass.

    A regex `\\{\\{[^{}]*\\}\\}` innermost-loop is fragile: a single stray '{' or '}'
    inside a template parameter (math, CSS, an emoticon) breaks its character class
    and the whole outer template survives (this is exactly why '{{Infobox country'
    leaked into lead chunks). This scanner tracks nesting depth on the two-char
    delimiters only, so stray single braces are harmless. An UNMATCHED opener is kept
    literally (advance one char) rather than eating the rest of the article, so a
    malformed page degrades to a little markup noise, never a lost article."""
    out: list[str] = []
    i, n = 0, len(text)
    ol = len(opener)
    while i < n:
        if text.startswith(opener, i):
            depth = 1
            j = i + ol
            while j < n and depth > 0:
                if text.startswith(opener, j):
                    depth += 1
                    j += ol
                elif text.startswith(closer, j):
                    depth -= 1
                    j += len(closer)
                else:
                    j += 1
            if depth == 0:
                i = j              # skip the whole balanced span
                continue
            out.append(text[i])    # unmatched opener: keep one char, carry on
            i += 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _resolve_wikilink(m: "re.Match[str]") -> str:
    inner = m.group(1)
    if _FILE_NS.match(inner):
        return ""  # File/Image/Category link (+caption) dropped wholesale
    # display text is the segment after the last pipe ([[target|a|display]] -> display)
    return inner.rsplit("|", 1)[-1]


def strip_wikitext(raw: str | None) -> str:
    """Return clean paragraph-separated text for `raw` wikitext (may be empty)."""
    if not raw:
        return ""
    text = raw

    # 1 · HTML comments FIRST (they may hold anything -- stray braces, half refs --
    #     that would otherwise unbalance the brace scan below).
    text = _COMMENT.sub("", text)

    # 2 · templates {{…}} and tables {|…|}, via a nesting-aware scanner, BEFORE refs.
    #     Order is load-bearing: in raw wikitext the braces are balanced (a full
    #     template is well-formed), but a <ref>…</ref> sitting between a template's
    #     {{ and }} often carries a brace, so removing refs first deletes a }} and
    #     leaves the outer template (e.g. an {{Infobox}}) unmatched -- exactly the
    #     leak that put '{{Infobox country' into lead chunks. Stripping the balanced
    #     templates first (which also swallow any refs inside them) cut the leak from
    #     ~2% of chunks to ~0.01% on a Simple English sample. Templates first (may
    #     wrap a table), then tables (may wrap a template), then templates once more
    #     to catch a template exposed by removing its table.
    text = _strip_balanced(text, "{{", "}}")
    text = _strip_balanced(text, "{|", "|}")
    text = _strip_balanced(text, "{{", "}}")

    # 3 · citations: any <ref> NOT already inside a removed template.
    text = _REF_PAIR.sub("", text)
    text = _REF_SELF.sub("", text)

    # 4 · wikilinks, innermost-first (inner links resolve to text, then an outer
    #     File:… caption becomes innermost and is dropped)
    for _ in range(_MAX_UNWIND):
        new = _WIKILINK.sub(_resolve_wikilink, text)
        if new == text:
            break
        text = new
    text = _EXT_LINK_TEXT.sub(r"\1", text)
    text = _EXT_LINK_BARE.sub("", text)

    # 5 · residual markup
    text = _HTML_TAG.sub("", text)
    text = _BOLD_ITALIC.sub("", text)
    text = _HEADING.sub(r"\1", text)
    text = _MAGIC_WORD.sub("", text)
    text = _TABLE_LEFTOVER.sub("", text)
    text = _LIST_MARKER.sub("", text)

    # 6 · entities + whitespace normalization -> blank-line-separated paragraphs
    text = html.unescape(text)
    text = _HORIZ_SPACE.sub(" ", text)
    text = _TRIM_LINES.sub("\n", text)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()
