"""Shared house-voice loader.

`Skills/newsletter_voice` (the Andrew Filar voice guide) is the single
source of truth for the house writing voice across every newsletter
section. Each section composes it ahead of its own section skill via
with_voice() so the voice is applied to every Claude generation call.

Division of authority:
  * The voice guide governs HOW to write — tone, rhythm, sentence music,
    and the things to never do (including never using em dashes).
  * The SECTION INSTRUCTIONS that follow govern WHAT to write, the format,
    length/word count, any section persona, and the exact output shape
    (often JSON). They always win on length, format, structure, and schema.
"""
from pathlib import Path

# voice_helper.py lives in NewsletterCreation/Code/, so three parents up is
# the repo root, where Skills/ lives.
_VOICE_PATH = Path(__file__).resolve().parent.parent.parent / "Skills" / "newsletter_voice"

_PRECEDENCE = (
    "[HOUSE VOICE — APPLIED AUTOMATICALLY]\n"
    "The guide below is the house writing voice. Apply its tone, rhythm, and "
    "rules (including: never use em dashes) to HOW you write. This is an "
    "automated pipeline: do not ask questions and do not wait for input.\n"
    "The SECTION INSTRUCTIONS after the guide are authoritative for WHAT to "
    "write, the format, length / word count, any section persona, and the "
    "exact output shape (often JSON). Where the voice guide and the section "
    "instructions disagree on length, format, structure, or output schema, "
    "follow the SECTION INSTRUCTIONS.\n"
)


def load_voice() -> str:
    """Return the house voice guide text, or '' if the file is missing."""
    return _VOICE_PATH.read_text(encoding="utf-8") if _VOICE_PATH.exists() else ""


def with_voice(section_skill: str) -> str:
    """Compose the house voice ahead of a section skill for use as `system=`.

    Falls back to the section skill unchanged if the voice file is missing,
    so a misplaced or renamed voice file can never break generation.
    """
    voice = load_voice()
    if not voice:
        return section_skill
    return (
        f"{_PRECEDENCE}\n"
        f"===== HOUSE VOICE GUIDE =====\n\n{voice}\n\n"
        f"===== SECTION INSTRUCTIONS "
        f"(authoritative for content, format, and output schema) =====\n\n"
        f"{section_skill}"
    )
