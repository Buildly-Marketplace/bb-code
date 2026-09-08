from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

import requests


DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:7b"


class ModelError(RuntimeError):
    """Base class for local model errors."""


class OllamaNotRunningError(ModelError):
    """Raised when the Ollama server cannot be reached."""


class ModelNotFoundError(ModelError):
    """Raised when the requested local model is unavailable."""


class ModelTimeoutError(ModelError):
    """Raised when the local model request times out."""


@dataclass(frozen=True)
class OllamaClient:
    base_url: str = DEFAULT_OLLAMA_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: int = 120

    def generate(self, prompt: str) -> str:
        self.ensure_model_available()
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.2,
                "num_ctx": 8192,
            },
        }
        try:
            response = requests.post(
                f"{self.base_url.rstrip('/')}/api/generate",
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.Timeout as exc:
            raise ModelTimeoutError(
                f"Ollama did not finish generation within {self.timeout_seconds} seconds."
            ) from exc
        except requests.RequestException as exc:
            raise OllamaNotRunningError(
                f"Could not connect to Ollama at {self.base_url}."
            ) from exc

        if response.status_code == 404:
            raise ModelNotFoundError(
                f"Model `{self.model}` was not found by Ollama."
            )
        if response.status_code >= 400:
            raise ModelError(
                f"Ollama returned HTTP {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        output = str(data.get("response", "")).strip()
        if not output:
            raise ModelError("Ollama returned an empty response.")
        return output

    def ensure_model_available(self) -> None:
        available = set(self.list_models())
        if self.model not in available:
            raise ModelNotFoundError(
                f"Model `{self.model}` is not installed in Ollama."
            )

    def list_models(self) -> list[str]:
        try:
            response = requests.get(
                f"{self.base_url.rstrip('/')}/api/tags",
                timeout=5,
            )
        except requests.Timeout as exc:
            raise ModelTimeoutError("Ollama did not respond to the model list request.")
        except requests.RequestException as exc:
            raise OllamaNotRunningError(
                f"Could not connect to Ollama at {self.base_url}."
            ) from exc

        if response.status_code >= 400:
            raise ModelError(
                f"Ollama returned HTTP {response.status_code}: {response.text[:300]}"
            )

        models = response.json().get("models", [])
        return sorted(str(model.get("name", "")) for model in models if model.get("name"))


@dataclass(frozen=True)
class OpenAICompatibleClient:
    base_url: str
    model: str
    api_key_env_var: str = "OPENAI_API_KEY"
    timeout_seconds: int = 120

    def generate(self, prompt: str) -> str:
        api_key = os.getenv(self.api_key_env_var)
        if not api_key:
            raise ModelError(f"Environment variable `{self.api_key_env_var}` is not set.")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        try:
            response = requests.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=self.timeout_seconds,
            )
        except requests.Timeout as exc:
            raise ModelTimeoutError(
                f"Remote model did not finish generation within {self.timeout_seconds} seconds."
            ) from exc
        except requests.RequestException as exc:
            raise ModelError(f"Could not connect to remote API at {self.base_url}.") from exc

        if response.status_code >= 400:
            raise ModelError(f"Remote API returned HTTP {response.status_code}: {response.text[:300]}")

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise ModelError("Remote API returned no choices.")
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        output = str(message.get("content", "")).strip()
        if not output:
            raise ModelError("Remote API returned an empty response.")
        return output

    def list_models(self) -> list[str]:
        api_key = os.getenv(self.api_key_env_var)
        if not api_key:
            raise ModelError(f"Environment variable `{self.api_key_env_var}` is not set.")
        try:
            response = requests.get(
                f"{self.base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
        except requests.Timeout as exc:
            raise ModelTimeoutError("Remote API did not respond to the model list request.") from exc
        except requests.RequestException as exc:
            raise ModelError(f"Could not connect to remote API at {self.base_url}.") from exc

        if response.status_code >= 400:
            raise ModelError(f"Remote API returned HTTP {response.status_code}: {response.text[:300]}")

        data = response.json().get("data", [])
        models: list[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    models.append(str(item["id"]))
        return sorted(models)


@dataclass(frozen=True)
class AnthropicClient:
    base_url: str = "https://api.anthropic.com"
    model: str = "claude-sonnet-5"
    api_key_env_var: str = "ANTHROPIC_API_KEY"
    timeout_seconds: int = 120
    anthropic_version: str = "2023-06-01"

    def generate(self, prompt: str) -> str:
        api_key = self._require_api_key()
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            response = requests.post(
                f"{self.base_url.rstrip('/')}/v1/messages",
                json=payload,
                headers=self._headers(api_key),
                timeout=self.timeout_seconds,
            )
        except requests.Timeout as exc:
            raise ModelTimeoutError(
                f"Remote model did not finish generation within {self.timeout_seconds} seconds."
            ) from exc
        except requests.RequestException as exc:
            raise ModelError(f"Could not connect to remote API at {self.base_url}.") from exc

        if response.status_code >= 400:
            raise ModelError(f"Remote API returned HTTP {response.status_code}: {response.text[:300]}")

        data = response.json()
        content = data.get("content", [])
        text_parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        output = "".join(text_parts).strip()
        if not output:
            raise ModelError("Remote API returned an empty response.")
        return output

    def list_models(self) -> list[str]:
        api_key = self._require_api_key()
        try:
            response = requests.get(
                f"{self.base_url.rstrip('/')}/v1/models",
                headers=self._headers(api_key),
                timeout=10,
            )
        except requests.Timeout as exc:
            raise ModelTimeoutError("Remote API did not respond to the model list request.") from exc
        except requests.RequestException as exc:
            raise ModelError(f"Could not connect to remote API at {self.base_url}.") from exc

        if response.status_code >= 400:
            raise ModelError(f"Remote API returned HTTP {response.status_code}: {response.text[:300]}")

        data = response.json().get("data", [])
        models: list[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    models.append(str(item["id"]))
        return sorted(models)

    def _require_api_key(self) -> str:
        api_key = os.getenv(self.api_key_env_var)
        if not api_key:
            raise ModelError(f"Environment variable `{self.api_key_env_var}` is not set.")
        return api_key

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": self.anthropic_version,
            "content-type": "application/json",
        }


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    base_url: str
    model: str
    api_key_env_var: str
    timeout_seconds: int = 120


PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "ollama": {"base_url": DEFAULT_OLLAMA_URL, "model": DEFAULT_MODEL, "api_key_env_var": ""},
    "openai-compatible": {"base_url": "", "model": DEFAULT_MODEL, "api_key_env_var": "OPENAI_API_KEY"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4.1", "api_key_env_var": "OPENAI_API_KEY"},
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-5",
        "api_key_env_var": "ANTHROPIC_API_KEY",
    },
}

SUPPORTED_PROVIDERS: tuple[str, ...] = tuple(PROVIDER_DEFAULTS)


def create_client(config: ProviderConfig) -> "OllamaClient | OpenAICompatibleClient | AnthropicClient":
    if config.provider == "ollama":
        return OllamaClient(
            base_url=config.base_url or DEFAULT_OLLAMA_URL,
            model=config.model or DEFAULT_MODEL,
            timeout_seconds=config.timeout_seconds,
        )
    if config.provider == "anthropic":
        return AnthropicClient(
            base_url=config.base_url or PROVIDER_DEFAULTS["anthropic"]["base_url"],
            model=config.model or PROVIDER_DEFAULTS["anthropic"]["model"],
            api_key_env_var=config.api_key_env_var or PROVIDER_DEFAULTS["anthropic"]["api_key_env_var"],
            timeout_seconds=config.timeout_seconds,
        )
    if config.provider in ("openai", "openai-compatible"):
        if not config.base_url:
            raise ModelError(f"Remote API base URL is required for provider `{config.provider}`.")
        return OpenAICompatibleClient(
            base_url=config.base_url,
            model=config.model,
            api_key_env_var=config.api_key_env_var or "OPENAI_API_KEY",
            timeout_seconds=config.timeout_seconds,
        )
    raise ModelError(f"Unsupported provider `{config.provider}`.")
