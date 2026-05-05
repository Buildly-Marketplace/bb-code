from __future__ import annotations

import re
from pathlib import Path


BB_DIR = ".bb"
PLANS_DIR = "plans"
CACHE_DIR = "cache"
CONTEXT_FILE = "context.md"


def ensure_bb_dirs(repo_root: Path) -> None:
    """Create the local bb-code workspace inside a repository."""
    bb_dir = repo_root / BB_DIR
    (bb_dir / PLANS_DIR).mkdir(parents=True, exist_ok=True)
    (bb_dir / CACHE_DIR).mkdir(parents=True, exist_ok=True)


def context_path(repo_root: Path) -> Path:
    return repo_root / BB_DIR / CONTEXT_FILE


def plans_path(repo_root: Path) -> Path:
    return repo_root / BB_DIR / PLANS_DIR


def slugify(value: str, max_length: int = 64) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return (slug[:max_length].strip("-") or "plan")


def read_text_safely(path: Path, max_chars: int = 12_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n[truncated]\n"
    return text


def relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
