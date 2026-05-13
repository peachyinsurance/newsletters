#!/usr/bin/env python3
"""
Sync workflow `newsletter:` dropdown options with NEWSLETTERS_DICT.

GitHub Actions doesn't support truly dynamic `type: choice` options — the
list is fixed at workflow parse time. This script reads the canonical
NEWSLETTERS_DICT from newsletters_config.py and rewrites every workflow
YAML's `newsletter:` input's `options:` block to match.

Run manually after adding/removing a newsletter:
    python NewsletterCreation/Code/sync_workflow_dropdowns.py

Or wire as a pre-commit hook / CI step so YAMLs are always in sync with
the config dict — see .github/workflows/sync_dropdowns.yml.
"""
import os
import re
import sys
from pathlib import Path

sys.path.append(os.path.dirname(__file__))
from newsletters_config import newsletter_names

ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# Match an entire `newsletter:` input block including its options list.
# Captures the block prefix (description/default/type) and the options
# block so we can rewrite the options without disturbing description text.
PATTERN = re.compile(
    r"(      newsletter:\n"
    r"        description: '[^']+'\n"
    r"        required: false\n"
    r"        default: '[^']+'\n"
    r"        type: choice\n)"
    r"        options:\n"
    r"(?:          - '[^']+'\n)+",
    re.MULTILINE,
)


def render_options(names: list[str]) -> str:
    """Produce the YAML options block. 'all' first, then each newsletter."""
    lines = ["        options:\n", "          - 'all'\n"]
    for n in names:
        lines.append(f"          - '{n}'\n")
    return "".join(lines)


def sync_file(path: Path, names: list[str]) -> bool:
    """Rewrite a workflow file's newsletter dropdown options. Returns True
    if the file was modified."""
    text = path.read_text()
    if "      newsletter:" not in text:
        return False
    new_options = render_options(names)
    new_text, n = PATTERN.subn(lambda m: m.group(1) + new_options, text)
    if n and new_text != text:
        path.write_text(new_text)
        return True
    return False


def main() -> int:
    names = newsletter_names()
    print(f"Syncing dropdowns to: {names}")
    changed = []
    for yml in sorted(WORKFLOWS_DIR.glob("*.yml")):
        if sync_file(yml, names):
            changed.append(yml.name)
    if changed:
        print(f"  ✓ Updated {len(changed)} workflows:")
        for c in changed:
            print(f"      - {c}")
    else:
        print("  · No changes needed (all workflows already in sync)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
