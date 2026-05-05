from __future__ import annotations

from bb_code.model_router import ModelError, OllamaClient, OpenAICompatibleClient


def test_ollama_client_lists_models(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"models": [{"name": "gemma3:latest"}, {"name": "qwen2.5-coder:7b"}]}

    def fake_get(url, **kwargs):
        assert url == "http://ollama.test/api/tags"
        return Response()

    monkeypatch.setattr("bb_code.model_router.requests.get", fake_get)
    client = OllamaClient(base_url="http://ollama.test", model="gemma3:latest")

    assert client.list_models() == ["gemma3:latest", "qwen2.5-coder:7b"]


def test_openai_compatible_client_requires_api_key_env(monkeypatch) -> None:
    monkeypatch.delenv("REMOTE_API_KEY", raising=False)
    client = OpenAICompatibleClient(
        base_url="https://api.example.com/v1",
        model="example",
        api_key_env_var="REMOTE_API_KEY",
    )

    try:
        client.generate("hello")
    except ModelError as exc:
        assert "REMOTE_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected missing API key to fail")


def test_openai_compatible_client_posts_chat_completion(monkeypatch) -> None:
    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setenv("REMOTE_API_KEY", "test-key-value")
    monkeypatch.setattr("bb_code.model_router.requests.post", fake_post)
    client = OpenAICompatibleClient(
        base_url="https://api.example.com/v1",
        model="example",
        api_key_env_var="REMOTE_API_KEY",
    )

    assert client.generate("hello") == "ok"
    assert calls[0][0] == "https://api.example.com/v1/chat/completions"
    assert calls[0][1]["json"]["model"] == "example"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-key-value"


def test_openai_compatible_client_lists_models(monkeypatch) -> None:
    calls = []

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}]}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setenv("REMOTE_API_KEY", "test-key-value")
    monkeypatch.setattr("bb_code.model_router.requests.get", fake_get)
    client = OpenAICompatibleClient(
        base_url="https://api.example.com/v1",
        model="gpt-4.1",
        api_key_env_var="REMOTE_API_KEY",
    )

    assert client.list_models() == ["gpt-4.1", "gpt-4.1-mini"]
    assert calls[0][0] == "https://api.example.com/v1/models"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-key-value"
