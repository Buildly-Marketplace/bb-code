from __future__ import annotations

import platform
import subprocess
import sys
import os
from dataclasses import dataclass
from pathlib import Path

from .model_router import ModelError, OllamaClient
from .repo_context import scan_repository
from .settings import AgentSettings, settings_path
from .utils import context_path, ensure_bb_dirs, relative


@dataclass(frozen=True)
class DiagnosticResult:
    name: str
    status: str
    message: str
    suggestion: str = ""
    repair_key: str = ""


@dataclass(frozen=True)
class DiagnosticReport:
    results: list[DiagnosticResult]
    error_context: str = ""

    @property
    def has_failures(self) -> bool:
        return any(result.status == "fail" for result in self.results)

    def to_markdown(self) -> str:
        lines = ["# bb-code Diagnostic Report", ""]
        if self.error_context:
            lines.extend(["## Error Context", "", "```text", self.error_context.strip(), "```", ""])
        lines.append("## Checks")
        lines.append("")
        for result in self.results:
            lines.append(f"### {result.name}")
            lines.append("")
            lines.append(f"- Status: `{result.status}`")
            lines.append(f"- Message: {result.message}")
            if result.suggestion:
                lines.append(f"- Suggestion: {result.suggestion}")
            if result.repair_key:
                lines.append(f"- Safe repair: `{result.repair_key}`")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


class SelfDiagnosticEngine:
    """Reusable diagnostic runner for bb-code-style local CLI systems."""

    def __init__(self, repo_root: Path, settings: AgentSettings) -> None:
        self.repo_root = repo_root
        self.settings = settings

    def run(self, error_context: str = "", check_model: bool = True) -> DiagnosticReport:
        results = [
            self._check_python_version(),
            self._check_workspace(),
            self._check_settings_file(),
            self._check_context_file(),
            self._check_repository_shape(),
            self._check_pytest_available(),
        ]
        if check_model and self.settings.provider == "ollama":
            results.append(self._check_ollama())
        elif self.settings.provider == "openai-compatible":
            results.append(self._check_remote_api_settings())
        elif self.settings.provider != "ollama":
            results.append(
                DiagnosticResult(
                    "AI provider",
                    "warn",
                    f"Provider `{self.settings.provider}` is not supported.",
                    "Use `ollama` or `openai-compatible`.",
                )
            )
        return DiagnosticReport(results=results, error_context=error_context)

    def apply_safe_repair(self, repair_key: str) -> DiagnosticResult:
        if repair_key == "create-bb-workspace":
            ensure_bb_dirs(self.repo_root)
            return DiagnosticResult(
                "Safe repair",
                "pass",
                "Created .bb workspace directories.",
            )
        if repair_key == "regenerate-context":
            ensure_bb_dirs(self.repo_root)
            context_path(self.repo_root).write_text(
                scan_repository(self.repo_root).to_markdown(),
                encoding="utf-8",
            )
            return DiagnosticResult(
                "Safe repair",
                "pass",
                "Regenerated .bb/context.md.",
            )
        return DiagnosticResult(
            "Safe repair",
            "fail",
            f"Unknown or unsafe repair `{repair_key}`.",
            "Run diagnostics again and choose a listed safe repair.",
        )

    def _check_python_version(self) -> DiagnosticResult:
        version = platform.python_version_tuple()
        major, minor = int(version[0]), int(version[1])
        if (major, minor) >= (3, 11):
            return DiagnosticResult("Python version", "pass", f"Python {platform.python_version()} is supported.")
        return DiagnosticResult(
            "Python version",
            "fail",
            f"Python {platform.python_version()} is below the required 3.11.",
            "Use Python 3.11+.",
        )

    def _check_workspace(self) -> DiagnosticResult:
        bb_dir = self.repo_root / ".bb"
        if bb_dir.exists() and (bb_dir / "plans").exists() and (bb_dir / "cache").exists():
            return DiagnosticResult("bb workspace", "pass", ".bb workspace directories exist.")
        return DiagnosticResult(
            "bb workspace",
            "fail",
            ".bb workspace directories are missing.",
            "Run `bb-code init` or approve the safe repair.",
            "create-bb-workspace",
        )

    def _check_settings_file(self) -> DiagnosticResult:
        path = settings_path(self.repo_root)
        if path.exists():
            return DiagnosticResult("Settings file", "pass", f"Settings found at `{relative(path, self.repo_root)}`.")
        return DiagnosticResult(
            "Settings file",
            "warn",
            "No .bb/settings.json file found.",
            "Run `bb-code onboard` or `bb-code settings set`.",
        )

    def _check_context_file(self) -> DiagnosticResult:
        path = context_path(self.repo_root)
        if path.exists():
            return DiagnosticResult("Repository context", "pass", ".bb/context.md exists.")
        return DiagnosticResult(
            "Repository context",
            "warn",
            ".bb/context.md does not exist.",
            "Run `bb-code understand` or approve the safe repair.",
            "regenerate-context",
        )

    def _check_repository_shape(self) -> DiagnosticResult:
        context = scan_repository(self.repo_root)
        if context.files or context.directories:
            return DiagnosticResult(
                "Repository shape",
                "pass",
                f"Found {len(context.files)} important files and {len(context.directories)} important directories.",
            )
        return DiagnosticResult(
            "Repository shape",
            "warn",
            "No standard project files or directories were found.",
            "Add README.md, pyproject.toml, devdocs/, src/, app/, or tests/ as relevant.",
        )

    def _check_pytest_available(self) -> DiagnosticResult:
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "--version"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return DiagnosticResult(
                "pytest",
                "warn",
                "Could not run `python -m pytest --version`.",
                "Install dev dependencies with `pip install -e \".[dev]\"`.",
            )
        if completed.returncode == 0:
            return DiagnosticResult("pytest", "pass", completed.stdout.strip())
        return DiagnosticResult(
            "pytest",
            "warn",
            "pytest is not available in the active Python environment.",
            "Install dev dependencies with `pip install -e \".[dev]\"`.",
        )

    def _check_ollama(self) -> DiagnosticResult:
        client = OllamaClient(
            base_url=self.settings.ollama_url,
            model=self.settings.model,
            timeout_seconds=self.settings.timeout_seconds,
        )
        try:
            client.ensure_model_available()
        except ModelError as exc:
            return DiagnosticResult(
                "Ollama connection",
                "fail",
                str(exc),
                "Run `bb-code settings test` after checking URL/model settings.",
            )
        return DiagnosticResult(
            "Ollama connection",
            "pass",
            f"`{self.settings.model}` is available at {self.settings.ollama_url}.",
        )

    def _check_remote_api_settings(self) -> DiagnosticResult:
        if not self.settings.api_base_url:
            return DiagnosticResult(
                "Remote AI provider",
                "fail",
                "OpenAI-compatible provider is selected without an API base URL.",
                "Set the remote API base URL in settings.",
            )
        if not os.getenv(self.settings.api_key_env_var):
            return DiagnosticResult(
                "Remote AI provider",
                "warn",
                f"API key environment variable `{self.settings.api_key_env_var}` is not set.",
                "Export the API key before using remote model calls.",
            )
        return DiagnosticResult(
            "Remote AI provider",
            "pass",
            f"Remote API settings are configured for {self.settings.api_base_url}.",
        )
