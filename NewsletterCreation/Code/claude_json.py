"""
Shared Claude helper for newsletter section scripts.

Call `call_with_json_output(api_key, system, user_content)` to get a parsed
JSON response back. Handles retries on API errors and strips markdown code
fences before parsing.
"""
import json
import time

import anthropic

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4000
DEFAULT_RETRIES = 3


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
    from the assistant's response. Retries with exponential-ish backoff on
    API errors; re-raises after the final attempt. Raises on JSON parse error.
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

    raw = next(block.text for block in response.content if block.type == "text")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(clean)
