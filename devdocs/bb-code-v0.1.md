# bb-code v0.1

bb-code is a safe local-first coding agent CLI. In v0.1 it is a planning tool only.

## Scope

- Scan a repository for high-signal project context.
- Save context to `.bb/context.md`.
- Ask an Ollama model on the local machine or a trusted network host to generate an implementation plan.
- Save plans to `.bb/plans/`.
- Persist provider settings in `.bb/settings.json`.
- Run reusable self-diagnostics with approval-gated safe repairs.
- Open an optional VS Code-like workspace UI with `build .` or `bb-code ui .`.

## Non-Goals

- No autocomplete.
- No file editing.
- No git integration.
- No background daemon.
- No autonomous execution loops.
- No vendor API requirement.
- No VS Code extension.
- No silent source-code edits.
- Web UI file edits require explicit per-file approval.

## Plan Format

Every generated plan should include:

- Feature Summary
- Relevant Files
- Implementation Steps
- Risks
- Test Plan
- Documentation Updates
- Open Questions

## Buildly Rules

- Prefer Python-first solutions.
- Prefer Docker-first local setup.
- No Makefiles.
- Use `ops/startup.sh` pattern if relevant.
- Always include `/devdocs` updates.
- Keep tests minimal but useful.

## v0.1 Release Checklist

- Package installs with `pip install -e .`.
- `ops/install.sh` installs bb-code from the source checkout into an isolated local virtual environment and links `bb-code` and `build` for use from any directory.
- CLI help renders with `bb-code --help`.
- `bb-code init` creates `.bb/`, `.bb/plans/`, and `.bb/cache/`.
- `bb-code onboard` writes `.bb/settings.json`.
- `bb-code settings show/set/test` handles model settings.
- `bb-code understand` creates `.bb/context.md`.
- `bb-code plan "<task>"` creates a Markdown file in `.bb/plans/`.
- `bb-code diagnose` writes a reusable diagnostic report.
- `bb-code diagnose --repair <key> --yes` applies only listed safe repairs.
- `build .` starts the optional local workspace web UI.
- If the web UI port is busy, the CLI prompts before terminating the existing listener and retrying.
- `bb-code ui .` starts the same UI through the main CLI.
- Ollama connection failures show a clear message.
- Missing Ollama models show an `ollama pull ...` hint.
- Minimal pytest suite passes.

## Settings Module

The settings module persists the default AI provider configuration in `.bb/settings.json`.

Current implemented provider:

- `ollama`

Supported configuration:

- `ollama_url`
- `model`
- `timeout_seconds`

Resolution order:

- command-line option
- environment variable
- `.bb/settings.json`
- built-in default

Environment variables:

- `BB_CODE_PROVIDER`
- `BB_CODE_OLLAMA_URL`
- `BB_CODE_MODEL`

## Self Diagnostic Module

The self diagnostic module is designed to be reusable in other local-first CLI systems. It returns structured diagnostic results with:

- check name
- status
- message
- suggestion
- optional safe repair key

Safe repairs require explicit user approval with `--yes`. Current safe repairs:

- `create-bb-workspace`
- `regenerate-context`

The module can diagnose internal configuration problems and suggest code fixes, but it must not silently edit source code. Any source-code fix workflow should remain user-approved and reviewable.

## Workspace Web UI

The optional web UI is a local stdlib HTTP server that presents a Visual Studio Code-inspired workspace view.

Current capabilities:

- inspect a folder or parent folder containing multiple repositories
- browse and read text files
- search filenames and text
- show git status when the workspace is a git repository
- run diagnostics and pytest from the Run panel
- show repository context
- show self diagnostics
- chat with the configured Ollama model
- use Agent, Plan, Debug, and Hints modes
- show approval-gated suggested edits with per-file Apply buttons
- explain Buildly conventions for Python-first, Docker-first, cloud-native apps
- prompt before killing an existing listener when the requested UI port is already in use

Safety boundary:

- no silent file editing
- no autonomous execution loops
- no background daemon
- no git operations
- no silent process termination
- multi-file changes are suggestions until the user explicitly applies each file edit

## Local Installer

`ops/install.sh` is the preferred machine-level local installer for development builds. It resolves the bb-code source checkout from the script location, so users do not need to run `pip install -e .` from inside a Python project.

Default behavior:

- require Python 3.11+
- create or reuse `~/.bb-code/venv`
- install the source checkout in editable mode
- link `bb-code` and `build` into `~/.local/bin`

Supported overrides:

- `--python PATH` or `BB_CODE_PYTHON`
- `--venv PATH` or `BB_CODE_VENV`
- `--bin-dir PATH` or `BB_CODE_BIN_DIR`

## Deferred Ideas

These are possible later improvements, not v0.1 requirements:

- Richer repository summaries with token-budget controls.
- Plan templates for common Buildly project types.
- Optional Docker local setup helper using `ops/startup.sh`.
- More robust model response validation.
- Additional AI service providers behind the settings abstraction.
- Approval-gated patch generation for self-diagnostic findings.
- Approval-gated web UI patch preview and apply flow.
- Monaco-style editor integration.

The following remain out of scope unless the project direction changes:

- File editing.
- Silent self-modification.
- Git operations.
- Autonomous execution loops.
- Background bb-code services.
- Vendor-required model APIs.
