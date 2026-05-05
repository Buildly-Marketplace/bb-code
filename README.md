# bb-code

bb-code is a local-first CLI that scans a repository and generates a practical implementation plan with an Ollama model running locally or on a trusted network host.

It is intentionally small and safe for v0.1:

- No silent file editing
- No autonomous execution loops
- No background daemon
- No vendor API requirement
- No VS Code extension

## Requirements

- Python 3.11+
- Ollama running locally or on a trusted network host
- The default model installed if you use the default settings:

```bash
ollama pull qwen2.5-coder:7b
```

## Install

Recommended local install:

```bash
/path/to/bb-code/ops/install.sh
```

The installer creates an isolated virtual environment at `~/.bb-code/venv` and links `bb-code` and `build` into `~/.local/bin`, so `build .` works from any repository. If `~/.local/bin` is not on your path:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Use a specific Python 3.11+ executable:

```bash
/path/to/bb-code/ops/install.sh --python /path/to/python3.11
```

Editable install from the project directory:

```bash
pip install -e .
```

For local development:

```bash
pip install -e ".[dev]"
pytest -q
```

If `bb-code` is not on your shell path after installation, run it through the Python environment that installed it or add that environment's `bin/` directory to `PATH`.

## Commands

Initialize bb-code storage in the current repository:

```bash
bb-code init
```

This creates:

```text
.bb/
.bb/plans/
.bb/cache/
```

Onboard a local or remote Ollama model:

```bash
bb-code onboard --ollama-url http://alderaan.home:11434 --model gemma3:latest --timeout 180
```

This writes:

```text
.bb/settings.json
```

Scan the current repository and save context:

```bash
bb-code understand
```

This writes:

```text
.bb/context.md
```

Generate an implementation plan:

```bash
bb-code plan "Add login system"
```

This writes a Markdown plan to:

```text
.bb/plans/add-login-system.md
```

Show or update settings:

```bash
bb-code settings show
bb-code settings set --ollama-url http://alderaan.home:11434 --model gemma3:latest
bb-code settings test
```

Use a different Ollama model:

```bash
bb-code plan "Add login system" --model codellama:7b
```

Use a longer generation timeout:

```bash
bb-code plan "Add login system" --timeout 300
```

Use a different Ollama URL:

```bash
bb-code plan "Add login system" --ollama-url http://localhost:11434
```

Use a remote Ollama host:

```bash
bb-code plan "Add login system" --ollama-url http://alderaan.home:11434 --model gemma3
```

You can also set defaults with environment variables:

```bash
export BB_CODE_OLLAMA_URL=http://alderaan.home:11434
export BB_CODE_MODEL=gemma3:latest
bb-code plan "Add login system"
```

Run self-diagnostics:

```bash
bb-code diagnose
```

Include an error message:

```bash
bb-code diagnose --error "Model not found: gemma3"
```

Apply a listed safe repair with explicit approval:

```bash
bb-code diagnose --repair regenerate-context --yes
```

The self-diagnostic module can create missing bb-code workspace files and regenerate context. It does not silently edit source code. Code fixes are reported as suggestions/plans for user review.

Open the optional VS Code-like web UI:

```bash
build .
```

If the default port is already in use, bb-code shows the listening process and asks before stopping it. You can also choose a different port:

```bash
build . --port 8790
```

Equivalent bb-code command:

```bash
bb-code ui .
```

The web UI includes:

- Explorer for one folder or a parent folder containing multiple repos
- Search across filenames and text
- Source control status
- Run panel for diagnostics and tests
- Editor-style file viewing
- Python and JavaScript syntax highlighting
- Lightweight lint diagnostics for Python and JavaScript files
- Agent chat on the right
- Agent, Plan, Debug, and Hints modes
- Scrollable chat responses with a loading indicator while the model is thinking
- Approval-gated suggested edits with per-file Apply buttons
- Diagnostics and repository context panels
- Buildly guidance for Python-first, Docker-first, cloud-native app work

The UI can inspect multiple files and suggest multi-file changes. It does not silently edit files; suggested edits require an explicit Apply click for each file.

## Repository Scanning

`bb-code understand` looks for:

- `README.md`
- `bb_code/`
- `devdocs/`
- `pyproject.toml`
- `requirements.txt`
- `package.json`
- `Dockerfile`
- `docker-compose.yml`
- `src/`
- `app/`
- `tests/`

## Plan Format

Plans include these required sections:

- Feature Summary
- Relevant Files
- Implementation Steps
- Risks
- Test Plan
- Documentation Updates
- Open Questions

## Error Handling

If Ollama is not running, bb-code prints a clear startup message and exits.

If the configured model is missing, bb-code prints the `ollama pull ...` command to install it.

If a model call times out, bb-code suggests using a smaller model or increasing `--timeout`.
