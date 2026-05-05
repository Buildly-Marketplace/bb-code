from __future__ import annotations

from pathlib import Path

from bb_code.repo_context import scan_repository


def test_scan_repository_finds_expected_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("typer\n", encoding="utf-8")
    (tmp_path / "devdocs").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / ".bb").mkdir()

    context = scan_repository(tmp_path)

    assert tmp_path / "README.md" in context.files
    assert tmp_path / "requirements.txt" in context.files
    assert tmp_path / "devdocs" in context.directories
    assert tmp_path / "tests" in context.directories


def test_context_markdown_uses_longer_fence_for_nested_markdown(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("```bash\necho hello\n```\n", encoding="utf-8")

    markdown = scan_repository(tmp_path).to_markdown()

    assert "````markdown" in markdown
    assert "```bash" in markdown
