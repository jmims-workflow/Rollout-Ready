"""Fails if any text file in the repo contains an em dash."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EM_DASH = chr(0x2014)
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "build", "dist", ".venv", "runs"}


def text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.parts):
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # images and other binary files


def test_no_em_dashes_anywhere():
    found = [f"{p.relative_to(ROOT)}:{n}" for p, text in text_files()
             for n, line in enumerate(text.splitlines(), 1) if EM_DASH in line]
    assert not found, "Em dashes found (use a comma, colon or period instead): " + ", ".join(found)
