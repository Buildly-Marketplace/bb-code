# Project Overview for AI Assistants

## What is bb-code?

bb-code is a Buildly Marketplace local coding workspace and AI assistant. It helps developers inspect repositories, plan changes, configure local or remote LLMs, and generate cloud-native diagnostic/refactor reports.

## Core Capabilities

- Local-first CLI: `bb-code`
- Browser workspace UI: `build .`
- Repository context scanning and planning
- Safe diagnostics and repair suggestions
- Model settings for Ollama and OpenAI-compatible APIs
- Multi-repo explorer context
- Read-only Kubernetes diagnostics
- Platform reports written to `.bb/reports/` and rendered as HTML

## Important Directories

| Path | Purpose |
|------|---------|
| `bb_code/` | Application package |
| `tests/` | pytest suite |
| `ops/` | Local install and startup scripts |
| `devdocs/` | Developer documentation |
| `.ai_prompt/` | AI assistant instructions |
| `.github/workflows/` | Marketplace and CI workflows |

## Design Principles

1. Local-first by default.
2. No silent source edits.
3. Read-only cloud and Kubernetes diagnostics unless a future action is explicitly confirmation-gated.
4. Keep workspace state in `.bb/`.
5. Prefer small, testable Python modules.
6. Treat top-level folders as candidate services in multi-repo workspaces.
