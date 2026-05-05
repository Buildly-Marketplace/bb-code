from __future__ import annotations

from pathlib import Path

from bb_code.settings import AgentSettings, load_settings, resolve_settings, save_settings


def test_save_and_load_settings(tmp_path: Path) -> None:
    settings = AgentSettings(
        provider="ollama",
        ollama_url="http://alderaan.home:11434",
        api_base_url="https://api.example.com/v1",
        api_key_env_var="EXAMPLE_API_KEY",
        model="gemma3:latest",
        timeout_seconds=180,
    )

    save_settings(tmp_path, settings)

    loaded = load_settings(tmp_path)
    assert loaded == settings


def test_resolve_settings_prefers_env_over_file(tmp_path: Path, monkeypatch) -> None:
    save_settings(
        tmp_path,
        AgentSettings(ollama_url="http://local:11434", model="local-model"),
    )
    monkeypatch.setenv("BB_CODE_OLLAMA_URL", "http://remote:11434")
    monkeypatch.setenv("BB_CODE_MODEL", "remote-model")

    settings = resolve_settings(tmp_path)

    assert settings.ollama_url == "http://remote:11434"
    assert settings.model == "remote-model"


def test_resolve_settings_prefers_cli_over_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BB_CODE_MODEL", "env-model")

    settings = resolve_settings(tmp_path, model="cli-model")

    assert settings.model == "cli-model"


def test_resolve_settings_reads_timeout_from_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BB_CODE_TIMEOUT_SECONDS", "240")

    settings = resolve_settings(tmp_path)

    assert settings.timeout_seconds == 240


def test_agent_settings_loads_remote_api_fields(tmp_path: Path) -> None:
    save_settings(
        tmp_path,
        AgentSettings(
            provider="openai-compatible",
            api_base_url="https://api.example.com/v1",
            api_key_env_var="REMOTE_API_KEY",
            model="gpt-example",
        ),
    )

    settings = load_settings(tmp_path)

    assert settings.provider == "openai-compatible"
    assert settings.api_base_url == "https://api.example.com/v1"
    assert settings.api_key_env_var == "REMOTE_API_KEY"
