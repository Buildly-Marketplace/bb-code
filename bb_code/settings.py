from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .model_router import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, PROVIDER_DEFAULTS, ProviderConfig
from .utils import BB_DIR, ensure_bb_dirs


SETTINGS_FILE = "settings.json"

# Work types a mode/command can be assigned its own provider for, at the user's
# discretion. Anything not present here falls back to the top-level `provider`.
WORK_MODES: tuple[str, ...] = ("agent", "plan", "debug", "hints")


@dataclass(frozen=True)
class AgentSettings:
    provider: str = "ollama"
    ollama_url: str = DEFAULT_OLLAMA_URL
    api_base_url: str = ""
    api_key_env_var: str = "OPENAI_API_KEY"
    model: str = DEFAULT_MODEL
    timeout_seconds: int = 120
    # Optional per-provider overrides (base_url/model/api_key_env_var), keyed by
    # provider name. Only needed for a provider that isn't the default one above,
    # e.g. {"anthropic": {"model": "claude-opus-5"}}.
    providers: dict[str, dict[str, str]] = field(default_factory=dict)
    # Optional per-mode provider selection, e.g. {"agent": "anthropic", "plan": "ollama"}.
    # A mode with no entry here uses the default `provider`.
    mode_providers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSettings":
        return cls(
            provider=str(data.get("provider", "ollama")),
            ollama_url=str(data.get("ollama_url", DEFAULT_OLLAMA_URL)),
            api_base_url=str(data.get("api_base_url", "")),
            api_key_env_var=str(data.get("api_key_env_var", "OPENAI_API_KEY")),
            model=str(data.get("model", DEFAULT_MODEL)),
            timeout_seconds=int(data.get("timeout_seconds", 120)),
            providers=_parse_providers(data.get("providers")),
            mode_providers=_parse_mode_providers(data.get("mode_providers")),
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
        providers: dict[str, dict[str, str]] | None = None,
        mode_providers: dict[str, str] | None = None,
    ) -> "AgentSettings":
        return AgentSettings(
            provider=provider or self.provider,
            ollama_url=ollama_url or self.ollama_url,
            api_base_url=api_base_url if api_base_url is not None else self.api_base_url,
            api_key_env_var=api_key_env_var or self.api_key_env_var,
            model=model or self.model,
            timeout_seconds=timeout_seconds or self.timeout_seconds,
            providers=providers if providers is not None else self.providers,
            mode_providers=mode_providers if mode_providers is not None else self.mode_providers,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    def provider_for_mode(self, mode: str) -> str:
        """Which provider handles a given work mode, honoring per-mode overrides."""
        return self.mode_providers.get(mode) or self.provider

    def provider_config(self, provider: str | None = None) -> ProviderConfig:
        """Effective connection config for `provider` (defaults to the primary provider)."""
        target = provider or self.provider
        defaults = PROVIDER_DEFAULTS.get(target, {})
        if target == self.provider:
            base_url = (self.ollama_url if target == "ollama" else self.api_base_url) or defaults.get("base_url", "")
            model = self.model or defaults.get("model", "")
            api_key_env_var = self.api_key_env_var or defaults.get("api_key_env_var", "")
        else:
            base_url = defaults.get("base_url", "")
            model = defaults.get("model", "")
            api_key_env_var = defaults.get("api_key_env_var", "")
        overrides = self.providers.get(target, {})
        base_url = overrides.get("base_url") or base_url
        model = overrides.get("model") or model
        api_key_env_var = overrides.get("api_key_env_var") or api_key_env_var
        return ProviderConfig(
            provider=target,
            base_url=base_url,
            model=model,
            api_key_env_var=api_key_env_var,
            timeout_seconds=self.timeout_seconds,
        )

    def provider_config_for_mode(self, mode: str) -> ProviderConfig:
        return self.provider_config(self.provider_for_mode(mode))


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


def _parse_providers(raw: Any) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        return {}
    providers: dict[str, dict[str, str]] = {}
    for name, overrides in raw.items():
        if isinstance(overrides, dict):
            providers[str(name)] = {str(key): str(value) for key, value in overrides.items()}
    return providers


def _parse_mode_providers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(mode): str(provider) for mode, provider in raw.items()}


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
