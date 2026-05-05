from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .model_router import DEFAULT_MODEL, DEFAULT_OLLAMA_URL
from .utils import BB_DIR, ensure_bb_dirs


SETTINGS_FILE = "settings.json"


@dataclass(frozen=True)
class AgentSettings:
    provider: str = "ollama"
    ollama_url: str = DEFAULT_OLLAMA_URL
    api_base_url: str = ""
    api_key_env_var: str = "OPENAI_API_KEY"
    model: str = DEFAULT_MODEL
    timeout_seconds: int = 120

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSettings":
        return cls(
            provider=str(data.get("provider", "ollama")),
            ollama_url=str(data.get("ollama_url", DEFAULT_OLLAMA_URL)),
            api_base_url=str(data.get("api_base_url", "")),
            api_key_env_var=str(data.get("api_key_env_var", "OPENAI_API_KEY")),
            model=str(data.get("model", DEFAULT_MODEL)),
            timeout_seconds=int(data.get("timeout_seconds", 120)),
        )

    def with_overrides(
        self,
        *,
        provider: str | None = None,
        ollama_url: str | None = None,
        api_base_url: str | None = None,
        api_key_env_var: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> "AgentSettings":
        return AgentSettings(
            provider=provider or self.provider,
            ollama_url=ollama_url or self.ollama_url,
            api_base_url=api_base_url if api_base_url is not None else self.api_base_url,
            api_key_env_var=api_key_env_var or self.api_key_env_var,
            model=model or self.model,
            timeout_seconds=timeout_seconds or self.timeout_seconds,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def settings_path(repo_root: Path) -> Path:
    return repo_root / BB_DIR / SETTINGS_FILE


def load_settings(repo_root: Path) -> AgentSettings:
    path = settings_path(repo_root)
    if not path.exists():
        return AgentSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AgentSettings()
    if not isinstance(data, dict):
        return AgentSettings()
    return AgentSettings.from_dict(data)


def save_settings(repo_root: Path, settings: AgentSettings) -> Path:
    ensure_bb_dirs(repo_root)
    path = settings_path(repo_root)
    path.write_text(settings.to_json(), encoding="utf-8")
    return path


def resolve_settings(
    repo_root: Path,
    *,
    provider: str | None = None,
    ollama_url: str | None = None,
    model: str | None = None,
    timeout_seconds: int | None = None,
) -> AgentSettings:
    settings = load_settings(repo_root)
    return settings.with_overrides(
        provider=provider or os.getenv("BB_CODE_PROVIDER"),
        ollama_url=ollama_url or os.getenv("BB_CODE_OLLAMA_URL"),
        api_base_url=os.getenv("BB_CODE_API_BASE_URL"),
        api_key_env_var=os.getenv("BB_CODE_API_KEY_ENV_VAR"),
        model=model or os.getenv("BB_CODE_MODEL"),
        timeout_seconds=timeout_seconds or _env_int("BB_CODE_TIMEOUT_SECONDS"),
    )


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
