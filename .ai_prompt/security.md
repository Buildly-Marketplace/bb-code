# Security Guidelines

bb-code runs on the user's machine and can inspect local workspaces, model settings, and optional Kubernetes contexts. Treat that access carefully.

## Secrets

- Never log API keys, tokens, passwords, kubeconfig credentials, OAuth codes, or cloud credentials.
- Redact sensitive values in reports and diagnostics.
- Store only the API key environment variable name, not the API key value.

## Local Files

- Read and write only inside the selected workspace unless the user explicitly switches workspace.
- Generated reports belong in `.bb/reports/`.
- Report rendering must escape HTML before display.

## Kubernetes

- Default behavior is read-only: list contexts, namespaces, pods, deployments, services, and cluster info.
- Mutating operations such as restart, delete, deploy, auth login, or context switching require explicit UI affordances and action-time confirmation.

## Model Calls

- Include only relevant workspace context.
- Do not transmit secrets or local telemetry/history to a remote model.
- Make model failures non-fatal for local diagnostics.
