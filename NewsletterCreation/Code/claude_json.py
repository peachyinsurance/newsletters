"""
Shared Claude helper for newsletter section scripts.

Call `call_with_json_output(api_key, system, user_content)` to get parsed
JSON back. Handles retries on API errors AND tolerant JSON extraction
(salvages JSON from prose/empty/code-fenced responses) so a stray Claude
hiccup doesn't crash a pipeline.
"""
import json
import re
import time

import anthropic

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4000
DEFAULT_RETRIES = 3


class ClaudeJSONError(RuntimeError):
    """Raised when Claude's response can't be parsed as JSON after every salvage attempt."""


def _extract_text(response) -> str:
    """Pull the assistant's text content out of an Anthropic response, defensively."""
    if not response or not getattr(response, "content", None):
        return ""
    for block in response.content:
        if getattr(block, "type", "") == "text":
            return block.text or ""
    return ""


def _try_parse_json(s: str):
    """Best-effort JSON parse that tolerates code fences and surrounding prose.
    Returns the parsed object or raises ClaudeJSONError with the raw text.
    """
    if not s or not s.strip():
        raise ClaudeJSONError("empty response from Claude")

    # Strip common code fences
    cleaned = s.strip()
    cleaned = cleaned.removeprefix("```json").removeprefix("```JSON").removeprefix("```")
    cleaned = cleaned.removesuffix("```").strip()

    # Direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Salvage: find the first balanced {...} object or [...] array in the text.
    # Greedy regex grabs from the first opener to the last closer of the same kind.
    for opener, closer in (("[", "]"), ("{", "}")):
        pattern = re.escape(opener) + r"[\s\S]*" + re.escape(closer)
        m = re.search(pattern, cleaned)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue

    # If we got here, no salvage worked.
    raise ClaudeJSONError(
        f"could not parse JSON. First 800 chars of response:\n{s[:800]}"
    )


def call_with_json_output(
    api_key: str,
    system: str,
    user_content: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    retries: int = DEFAULT_RETRIES,
):
    """
    Call Claude with a system + single user message. Return the parsed JSON
    from the assistant's response.

    On API failure: retries with backoff up to `retries` times, then re-raises.
    On parse failure: prints the raw response (truncated) and raises
    ClaudeJSONError so callers can decide whether to skip or abort.
    """
    client = anthropic.Anthropic(api_key=api_key)

    response = None
    for attempt in range(retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
            break
        except Exception as e:
            if attempt < retries - 1:
                print(f"  Claude API error (attempt {attempt + 1}): {e}")
                time.sleep(10 * (attempt + 1))
            else:
                raise

    raw = _extract_text(response)
    if not raw.strip():
        stop_reason = getattr(response, "stop_reason", "unknown")
        raise ClaudeJSONError(
            f"Claude returned no text content (stop_reason={stop_reason})"
        )

    try:
        return _try_parse_json(raw)
    except ClaudeJSONError as e:
        print(f"  ⚠ {e}")
        raise
