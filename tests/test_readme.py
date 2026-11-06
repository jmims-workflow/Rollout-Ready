"""The README keeps its Part of a set section, stays short, and every relative link works."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
MAX_PROSE_WORDS = 1000

PART_OF_A_SET = """## Part of a set

Rollout Ready is one of three apps that follow an AI tool through its life at a company:

* [Sandbox to Shipped](https://github.com/jmims-workflow/Sandbox-to-Shipped): should we build or approve this AI tool?
* **Rollout Ready** (this repo): is a change to the tool's instructions ready to roll out, and which version is in use now?
* [AI Adoption & Impact Tracker](https://github.com/jmims-workflow/ai-adoption-tracker): once it's live, are people using it, and is it helping?

They are separate apps with no shared code. Together they cover deciding, testing and measuring.
"""

LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)[^)]*\)|<img[^>]*\bsrc=\"([^\"]+)\"")


def prose_words(text: str) -> int:
    """Words of prose, not counting Part of a set, code blocks, tables, images or badges."""
    text = re.sub(r"^## Part of a set\n.*?(?=^## |\Z)", "", text, flags=re.S | re.M)
    text = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") or s.startswith("<img") or s.startswith("![") or s.startswith("[!["):
            continue
        kept.append(line)
    return len(" ".join(kept).split())


def test_part_of_a_set_is_word_for_word():
    assert PART_OF_A_SET in README.read_text(encoding="utf-8")


def test_readme_prose_under_1000_words():
    n = prose_words(README.read_text(encoding="utf-8"))
    assert n < MAX_PROSE_WORDS, f"README has {n} words of prose; keep it under {MAX_PROSE_WORDS} and move detail to docs/"


def test_relative_links_resolve():
    files = [README, *sorted((ROOT / "docs").rglob("*.md"))]
    broken = []
    for md in files:
        text = re.sub(r"^```.*?^```", "", md.read_text(encoding="utf-8"), flags=re.S | re.M)
        for m in LINK_RE.finditer(text):
            target = m.group(1) or m.group(2)
            if re.match(r"[a-z]+:", target) or target.startswith("#"):
                continue  # web links, mailto and same-page anchors
            path = target.split("#", 1)[0]
            if not (md.parent / path).exists():
                broken.append(f"{md.relative_to(ROOT)}: {target}")
    assert not broken, "Broken relative links: " + ", ".join(broken)
