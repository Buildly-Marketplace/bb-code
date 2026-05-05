# Testing Guidelines

All meaningful bb-code behavior changes should include tests.

## Required Tests

| Change | Expected coverage |
|--------|-------------------|
| CLI command | Typer CLI test |
| Settings behavior | Settings unit test |
| Model routing | Mocked provider test |
| Web endpoint | Function and HTML exposure test |
| Report generation | Pure function test with fake kubectl data |
| Kubernetes parsing | JSON summarizer test |
| Security/path handling | Escape and path traversal tests |

## Commands

```bash
python -m pytest -q
```

Use dependency injection or monkeypatching for external tools. Tests must not require a live cluster, GitHub account, Ollama server, or remote API.
