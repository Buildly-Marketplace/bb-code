from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .utils import read_text_safely, relative


IMPORTANT_FILES = (
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Dockerfile",
    "docker-compose.yml",
)

IMPORTANT_DIRS = (
    "bb_code",
    "devdocs",
    "src",
    "app",
    "tests",
)

SKIP_DIRS = {
    ".bb",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    "node_modules",
}

CODE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".cs",
    ".rb",
    ".php",
    ".sh",
}


@dataclass(frozen=True)
class RepoContext:
    root: Path
    files: list[Path]
    directories: list[Path]

    def to_markdown(self) -> str:
        lines: list[str] = [
            "# Repository Context",
            "",
            f"Root: `{self.root}`",
            "",
            "## Project Summary",
            "",
            _infer_summary(self.root, self.files),
            "",
            "## Important Files",
            "",
        ]

        if self.files:
            for path in self.files:
                lines.append(f"- `{relative(path, self.root)}`")
        else:
            lines.append("- No standard project files found.")

        lines.extend(["", "## Important Directories", ""])
        if self.directories:
            for path in self.directories:
                lines.append(f"- `{relative(path, self.root)}/`")
        else:
            lines.append("- No standard source, docs, or test directories found.")

        lines.extend(["", "## File Excerpts", ""])
        for path in self.files:
            content = read_text_safely(path)
            if not content.strip():
                continue
            lang = _language_hint(path)
            fence = _fence_for(content)
            lines.extend(
                [
                    f"### `{relative(path, self.root)}`",
                    "",
                    f"{fence}{lang}",
                    content.strip(),
                    fence,
                    "",
                ]
            )

        lines.extend(["## Directory File Samples", ""])
        for directory in self.directories:
            samples = _sample_files(directory, self.root)
            lines.append(f"### `{relative(directory, self.root)}/`")
            lines.append("")
            if samples:
                for path in samples:
                    lines.append(f"- `{relative(path, self.root)}`")
            else:
                lines.append("- No readable sample files found.")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"


def scan_repository(repo_root: Path) -> RepoContext:
    root = repo_root.resolve()
    files = [root / name for name in IMPORTANT_FILES if (root / name).is_file()]
    directories = [root / name for name in IMPORTANT_DIRS if (root / name).is_dir()]
    return RepoContext(root=root, files=files, directories=directories)


def _infer_summary(root: Path, files: list[Path]) -> str:
    names = {path.name for path in files}
    parts: list[str] = []

    if "pyproject.toml" in names or "requirements.txt" in names:
        parts.append("Python project")
    if "package.json" in names:
        parts.append("JavaScript/TypeScript project")
    if "Dockerfile" in names or "docker-compose.yml" in names:
        parts.append("Docker-enabled")

    if not parts:
        return (
            "No standard manifest files were found. The repository may be minimal, "
            "new, or use a custom structure."
        )

    return f"{root.name} appears to be a {', '.join(parts)} repository."


def _sample_files(directory: Path, root: Path, limit: int = 40) -> list[Path]:
    samples: list[Path] = []
    for path in sorted(directory.rglob("*")):
        if len(samples) >= limit:
            break
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in CODE_SUFFIXES or path.name.lower().endswith(".md"):
            samples.append(path)
    return samples


def _language_hint(path: Path) -> str:
    if path.name == "pyproject.toml":
        return "toml"
    if path.name == "package.json":
        return "json"
    if path.suffix == ".md":
        return "markdown"
    if path.suffix == ".txt":
        return "text"
    if path.suffix in {".yml", ".yaml"}:
        return "yaml"
    return ""


def _fence_for(content: str) -> str:
    longest = 0
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            longest = max(longest, len(stripped) - len(stripped.lstrip("`")))
    return "`" * max(3, longest + 1)
