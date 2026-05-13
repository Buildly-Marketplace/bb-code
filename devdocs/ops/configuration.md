# Configuration Reference

bb-code is configured through CLI flags, environment variables, and `.bb/settings.json`.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `BB_CODE_PROVIDER` | Model provider: `ollama` or `openai-compatible` |
| `BB_CODE_OLLAMA_URL` | Ollama base URL |
| `BB_CODE_API_BASE_URL` | OpenAI-compatible API base URL |
| `BB_CODE_API_KEY_ENV_VAR` | Environment variable containing the remote API key |
| `BB_CODE_MODEL` | Default model name |
| `BB_CODE_TIMEOUT_SECONDS` | Model request timeout |
| `BB_CODE_VENV` | Local virtual environment path for ops scripts |
| `HOST` | Web UI bind host for `ops/startup.sh` |
| `PORT` | Web UI port for `ops/startup.sh` |

## Local Settings

Use the Settings panel in the web UI, or:

```bash
bb-code settings set --provider ollama --ollama-url http://localhost:11434 --model qwen2.5-coder:7b
bb-code settings test
```

Settings are saved to `.bb/settings.json` in the launch workspace so they survive browser refreshes and server restarts.

## Kubernetes Diagnostics

If `kubectl` and `gcloud` are on `PATH`, bb-code can include read-only cluster diagnostics in platform reports. It does not switch contexts, authenticate, restart workloads, delete pods, or deploy images by default.

## ForgeOps / k8s-monitor Integration

bb-code tracks ForgeOps as an optional submodule:

```bash
git submodule update --init --recursive integrations/k8s-monitor
```

The web UI detects the submodule and shows a `K8s Monitor` Run-panel link to the default ForgeOps dashboard at `http://127.0.0.1:8000/`. bb-code does not start ForgeOps automatically because ForgeOps may read Kubernetes credentials and cluster resources.
