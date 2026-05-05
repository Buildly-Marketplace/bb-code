# Coding Standards for Buildly Forge

These standards apply to bb-code.

## Python

- Use Python 3.11+.
- Keep functions typed when signatures are non-trivial.
- Prefer small modules and pure helper functions for report generation and parsing.
- Keep imports ordered as standard library, third-party, then local package imports.
- Avoid broad refactors when a targeted fix is enough.

## Local Web UI

- Preserve the current dependency-light local HTTP server unless a larger framework is intentionally introduced.
- Keep browser UI state deterministic and stored under `.bb/`.
- Escape rendered user/workspace content before inserting it into HTML.
- Do not add hidden destructive actions.

## Cloud And Kubernetes

- Default diagnostics must be read-only.
- Shelling out to `kubectl` or `gcloud` must have timeouts and structured error handling.
- Do not print kubeconfig contents, tokens, secrets, or credentials.

## Buildly Marketplace

- Keep `BUILDLY.yaml`, `README.md`, `CODE_OF_CONDUCT.md`, `LICENSE`, `.ai_prompt/`, `devdocs/`, `ops/`, and `.github/workflows/` current when behavior changes.
