from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from bb_code import cli


runner = CliRunner()


def test_init_creates_bb_directories(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["init"], catch_exceptions=False, env={})

    assert result.exit_code == 0
    assert (tmp_path / ".bb").exists()
    assert (tmp_path / ".bb" / "plans").exists()
    assert (tmp_path / ".bb" / "cache").exists()


def test_understand_creates_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")
    (tmp_path / "devdocs").mkdir()

    result = runner.invoke(cli.app, ["understand"], catch_exceptions=False)

    assert result.exit_code == 0
    context = tmp_path / ".bb" / "context.md"
    assert context.exists()
    text = context.read_text(encoding="utf-8")
    assert "Repository Context" in text
    assert "README.md" in text
    assert "devdocs/" in text


def test_plan_creates_plan_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")

    class FakeClient:
        def __init__(self, base_url: str, model: str, timeout_seconds: int) -> None:
            self.base_url = base_url
            self.model = model
            self.timeout_seconds = timeout_seconds

        def generate(self, prompt: str) -> str:
            assert "Add login system" in prompt
            return """## Feature Summary

Add login.

## Relevant Files

- README.md

## Implementation Steps

1. Plan the work.

## Risks

- Authentication scope.

## Test Plan

- Add minimal tests.

## Documentation Updates

- Update /devdocs.

## Open Questions

- Which auth provider?
"""

    monkeypatch.setattr(cli, "OllamaClient", FakeClient)

    result = runner.invoke(cli.app, ["plan", "Add login system"], catch_exceptions=False)

    assert result.exit_code == 0
    plan = tmp_path / ".bb" / "plans" / "add-login-system.md"
    assert plan.exists()
    assert "## Feature Summary" in plan.read_text(encoding="utf-8")


def test_plan_handles_ollama_not_running(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    class BrokenClient:
        def __init__(self, base_url: str, model: str, timeout_seconds: int) -> None:
            pass

        def generate(self, prompt: str) -> str:
            raise cli.OllamaNotRunningError("offline")

    monkeypatch.setattr(cli, "OllamaClient", BrokenClient)

    result = runner.invoke(cli.app, ["plan", "Add login system"])

    assert result.exit_code == 1
    assert "Ollama is not running or is unreachable" in result.output


def test_plan_handles_missing_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    class MissingModelClient:
        def __init__(self, base_url: str, model: str, timeout_seconds: int) -> None:
            self.model = model

        def generate(self, prompt: str) -> str:
            raise cli.ModelNotFoundError("missing")

    monkeypatch.setattr(cli, "OllamaClient", MissingModelClient)

    result = runner.invoke(cli.app, ["plan", "Add login system", "--model", "missing:model"])

    assert result.exit_code == 1
    assert "Model not found" in result.output
    assert "ollama pull missing:model" in result.output


def test_plan_handles_model_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    class TimeoutClient:
        def __init__(self, base_url: str, model: str, timeout_seconds: int) -> None:
            self.timeout_seconds = timeout_seconds

        def generate(self, prompt: str) -> str:
            raise cli.ModelTimeoutError(f"timed out after {self.timeout_seconds}")

    monkeypatch.setattr(cli, "OllamaClient", TimeoutClient)

    result = runner.invoke(cli.app, ["plan", "Add login system", "--timeout", "1"])

    assert result.exit_code == 1
    assert "Ollama timed out" in result.output
    assert "increase the timeout" in result.output


def test_onboard_saves_settings_without_connection_test(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "onboard",
            "--ollama-url",
            "http://alderaan.home:11434",
            "--model",
            "gemma3:latest",
            "--timeout",
            "180",
            "--no-test",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    settings = (tmp_path / ".bb" / "settings.json").read_text(encoding="utf-8")
    assert "alderaan.home" in settings
    assert "gemma3:latest" in settings


def test_settings_show_and_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    set_result = runner.invoke(
        cli.app,
        [
            "settings",
            "set",
            "--ollama-url",
            "http://alderaan.home:11434",
            "--model",
            "gemma3:latest",
        ],
        catch_exceptions=False,
    )
    show_result = runner.invoke(cli.app, ["settings", "show"], catch_exceptions=False)

    assert set_result.exit_code == 0
    assert show_result.exit_code == 0
    assert "alderaan.home" in show_result.output
    assert "gemma3:latest" in show_result.output


def test_diagnose_can_apply_safe_repair(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        ["diagnose", "--repair", "create-bb-workspace", "--yes", "--skip-model"],
        catch_exceptions=False,
    )

    assert result.exit_code in {0, 1}
    assert (tmp_path / ".bb" / "plans").exists()
    assert (tmp_path / ".bb" / "diagnostics.md").exists()


def test_diagnose_repair_requires_approval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        ["diagnose", "--repair", "create-bb-workspace", "--skip-model"],
    )

    assert result.exit_code == 1
    assert "requires explicit approval" in result.output
