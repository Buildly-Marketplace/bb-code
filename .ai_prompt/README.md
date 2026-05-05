# AI Assistant Instructions for bb-code

This folder contains Buildly Marketplace instructions for AI assistants working on bb-code.

## Instruction Files

| File | Purpose |
|------|---------|
| [overview.md](./overview.md) | Project purpose, architecture, and important directories |
| [coding-standards.md](./coding-standards.md) | Python and UI coding conventions |
| [testing-guidelines.md](./testing-guidelines.md) | Test expectations and patterns |
| [data-management.md](./data-management.md) | Local workspace state and report data rules |
| [documentation-standards.md](./documentation-standards.md) | Documentation requirements |
| [security.md](./security.md) | Local, model, and Kubernetes safety rules |

## Quick Context

**Project:** bb-code  
**Stack:** Python 3.11+, Typer, Rich, local HTTP UI, pytest  
**Model providers:** Ollama and OpenAI-compatible APIs  
**Cloud tooling:** Optional read-only kubectl and gcloud diagnostics  

bb-code is local-first. It should inspect and write only the selected workspace unless a user explicitly chooses another workspace. Kubernetes and cloud diagnostics must be read-only by default.
