from __future__ import annotations

import json
import os
import re
import shutil
import signal
import errno
import subprocess
import sys
import threading
import tomllib
import webbrowser
import ast
from datetime import datetime
import html as html_lib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .diagnostics import SelfDiagnosticEngine
from .model_router import ModelError, OllamaClient, OpenAICompatibleClient
from .repo_context import scan_repository
from .settings import AgentSettings, load_settings, resolve_settings, save_settings
from .utils import BB_DIR, ensure_bb_dirs, read_text_safely, relative


SKIP_DIRS = {
    ".bb",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    "node_modules",
}

TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".css",
    ".html",
    ".sh",
    ".dockerfile",
}

UI_STATE_FILE = "web_state.json"
REPORTS_DIR = "reports"
K8S_MONITOR_RELATIVE_PATH = Path("integrations") / "k8s-monitor"
K8S_MONITOR_REPO_URL = "https://github.com/Buildly-Marketplace/k8s-monitor"
K8S_MONITOR_DEFAULT_URL = "http://127.0.0.1:8000/"
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOGO_PATH = Path(__file__).resolve().parents[1] / "forge-logo.png"


class WorkspaceSession:
    def __init__(self, root: Path) -> None:
        self._lock = threading.Lock()
        self._state_root = self._resolve_workspace(root)
        self._base_root, self._root = self._load_saved_roots(self._state_root)

    @property
    def root(self) -> Path:
        with self._lock:
            return self._root

    @property
    def base_root(self) -> Path:
        with self._lock:
            return self._base_root

    @property
    def settings_root(self) -> Path:
        return self._state_root

    def switch(self, path: str) -> dict[str, Any]:
        root = self._resolve_workspace(Path(path).expanduser())
        with self._lock:
            self._base_root = root
            self._root = root
            self._save_state_locked()
        return collect_session_workspace(self)

    def switch_repo(self, rel_path: str) -> dict[str, Any]:
        with self._lock:
            root = _safe_path(self._base_root, rel_path)
            if not root.exists():
                raise FileNotFoundError(rel_path)
            if not root.is_dir():
                raise ValueError("Repository path must be a directory")
            self._root = root
            self._save_state_locked()
        return collect_session_workspace(self)

    def snapshot(self) -> tuple[Path, Path]:
        with self._lock:
            return self._base_root, self._root

    def _save_state_locked(self) -> None:
        ensure_bb_dirs(self._state_root)
        state_path = self._state_root / BB_DIR / UI_STATE_FILE
        state_path.write_text(
            json.dumps(
                {
                    "base_root": str(self._base_root),
                    "active_repo": relative(self._root, self._base_root),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def _load_saved_roots(cls, state_root: Path) -> tuple[Path, Path]:
        state_path = state_root / BB_DIR / UI_STATE_FILE
        if not state_path.exists():
            return state_root, state_root
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return state_root, state_root
        if not isinstance(data, dict):
            return state_root, state_root
        base_root = cls._resolve_optional_workspace(str(data.get("base_root", ""))) or state_root
        active_repo = str(data.get("active_repo", "."))
        try:
            active_root = _safe_path(base_root, active_repo)
        except ValueError:
            active_root = base_root
        if not active_root.exists() or not active_root.is_dir():
            active_root = base_root
        return base_root, active_root

    @staticmethod
    def _resolve_workspace(path: Path) -> Path:
        root = path.resolve()
        if not root.exists():
            raise FileNotFoundError(str(path))
        if not root.is_dir():
            raise ValueError("Workspace path must be a directory")
        return root

    @classmethod
    def _resolve_optional_workspace(cls, path: str) -> Path | None:
        if not path:
            return None
        try:
            return cls._resolve_workspace(Path(path).expanduser())
        except (OSError, ValueError):
            return None


def serve_workspace(
    workspace: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    session = WorkspaceSession(workspace)
    try:
        server = ThreadingHTTPServer((host, port), _make_handler(session))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        server = _handle_port_in_use(session, host, port)
        if server is None:
            return
    url = f"http://{host}:{server.server_port}"
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    print(f"bb-code web UI running at {url}")
    print(f"Workspace: {session.root}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


def _handle_port_in_use(session: WorkspaceSession, host: str, port: int) -> ThreadingHTTPServer | None:
    listeners = find_port_listeners(port)
    print(f"Port {port} is already in use.")
    if listeners:
        print("Current listener:")
        for listener in listeners:
            command = f" {listener['command']}" if listener.get("command") else ""
            print(f"- PID {listener['pid']}{command}")
    else:
        print("Could not identify the process using the port.")

    if not _confirm("Kill the existing process and retry? [y/N] "):
        print("Not starting bb-code web UI.")
        return None

    if not listeners:
        print("No process was identified to kill. Try a different port with --port.")
        return None

    for listener in listeners:
        pid = int(listener["pid"])
        print(f"Stopping PID {pid}...")
        os.kill(pid, signal.SIGTERM)

    try:
        return _retry_server_bind(session, host, port)
    except OSError as exc:
        print(f"Port {port} is still unavailable: {exc}")
        print("Try a different port with --port.")
        return None


def _retry_server_bind(session: WorkspaceSession, host: str, port: int) -> ThreadingHTTPServer:
    import time

    last_error: OSError | None = None
    for _ in range(20):
        try:
            return ThreadingHTTPServer((host, port), _make_handler(session))
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    if last_error is not None:
        raise last_error
    raise OSError("Port did not become available")


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def find_port_listeners(port: int) -> list[dict[str, str]]:
    try:
        completed = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if completed.returncode != 0:
        return []

    listeners: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in completed.stdout.splitlines():
        if not line.startswith("p"):
            continue
        pid = line[1:].strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        listeners.append({"pid": pid, "command": _process_command(pid)})
    return listeners


def _process_command(pid: str) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-p", pid, "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def application_root() -> Path:
    return Path(__file__).resolve().parents[1]


def k8s_monitor_integration_status(app_root: Path | None = None) -> dict[str, Any]:
    root = app_root or application_root()
    path = root / K8S_MONITOR_RELATIVE_PATH
    installed = path.exists() and path.is_dir() and (path / "main.py").exists()
    status: dict[str, Any] = {
        "name": "ForgeOps / k8s-monitor",
        "installed": installed,
        "path": relative(path, root),
        "repository": K8S_MONITOR_REPO_URL,
        "dashboardUrl": K8S_MONITOR_DEFAULT_URL,
        "docsUrl": f"{K8S_MONITOR_REPO_URL}#readme",
        "installCommand": "git submodule update --init --recursive integrations/k8s-monitor",
        "startCommands": [
            "cd integrations/k8s-monitor && python main.py",
            "cd integrations/k8s-monitor && docker compose -f ops/docker-compose.yml up",
        ],
        "notes": [
            "bb-code links to ForgeOps when the submodule is installed.",
            "bb-code does not start ForgeOps automatically because it can read Kubernetes credentials.",
        ],
    }
    if not installed:
        return status

    buildly_yaml = path / "BUILDLY.yaml"
    if buildly_yaml.exists():
        status["manifest"] = relative(buildly_yaml, root)
    status["commit"] = _git_value(path, ["rev-parse", "--short", "HEAD"])
    status["branch"] = _git_value(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    status["healthUrl"] = K8S_MONITOR_DEFAULT_URL.rstrip("/") + "/health"
    return status


def _git_value(path: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def collect_workspace(root: Path) -> dict[str, Any]:
    return {
        "root": str(root),
        "baseRoot": str(root),
        "activeRepo": ".",
        "name": root.name,
        "repos": find_repositories(root),
        "tree": build_tree(root),
        "settings": resolve_settings(root).to_json(),
    }


def collect_session_workspace(session: WorkspaceSession) -> dict[str, Any]:
    base_root, root = session.snapshot()
    return {
        "root": str(root),
        "baseRoot": str(base_root),
        "activeRepo": relative(root, base_root),
        "name": root.name,
        "repos": find_repositories(base_root),
        "tree": build_tree(root),
        "settings": resolve_settings(root).to_json(),
    }


def find_repositories(root: Path, limit: int = 30) -> list[dict[str, str]]:
    repos: list[dict[str, str]] = []
    for path in [root, *sorted(p for p in root.iterdir() if p.is_dir())]:
        if len(repos) >= limit:
            break
        if path.name in SKIP_DIRS or path.name.startswith(".pytest"):
            continue
        if any((path / marker).exists() for marker in (".git", "pyproject.toml", "package.json", "README.md")):
            repos.append({"name": path.name, "path": relative(path, root)})
    return repos


def build_tree(root: Path, max_entries: int = 300) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def walk(directory: Path, depth: int) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        if len(entries) >= max_entries or depth > 4:
            return children
        for child in sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            if len(entries) >= max_entries:
                break
            if child.name in SKIP_DIRS:
                continue
            rel = relative(child, root)
            node = {
                "name": child.name,
                "path": rel,
                "type": "dir" if child.is_dir() else "file",
            }
            entries.append(node)
            if child.is_dir():
                node["children"] = walk(child, depth + 1)
            children.append(node)
        return children

    return walk(root, 0)


def read_workspace_file(root: Path, rel_path: str) -> dict[str, str]:
    path = _safe_path(root, rel_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(rel_path)
    if not _is_text_file(path):
        return {"path": relative(path, root), "content": "[binary or unsupported file]", "language": "text"}
    return {
        "path": relative(path, root),
        "content": read_text_safely(path, max_chars=40_000),
        "language": _language(path),
    }


def write_workspace_file(root: Path, rel_path: str, content: str) -> dict[str, str]:
    path = _safe_path(root, rel_path)
    if path.exists() and not path.is_file():
        raise ValueError("Path is not a file")
    if path.name in SKIP_DIRS or any(part in SKIP_DIRS for part in path.relative_to(root).parts):
        raise ValueError("Refusing to write skipped workspace path")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": relative(path, root), "status": "updated"}


def search_workspace(root: Path, query: str, max_results: int = 80) -> dict[str, Any]:
    if not query.strip():
        return {"results": []}
    needle = query.lower()
    results: list[dict[str, Any]] = []
    for path in _iter_text_files(root):
        if len(results) >= max_results:
            break
        rel_path = relative(path, root)
        if needle in path.name.lower():
            results.append({"path": rel_path, "line": 0, "text": path.name})
        content = read_text_safely(path, max_chars=60_000)
        for index, line in enumerate(content.splitlines(), start=1):
            if len(results) >= max_results:
                break
            if needle in line.lower():
                results.append({"path": rel_path, "line": index, "text": line.strip()[:240]})
    return {"results": results}


def git_status(root: Path) -> dict[str, str]:
    try:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "output": str(exc)}
    if completed.returncode != 0:
        return {"status": "unavailable", "output": completed.stderr.strip() or "Not a git repository."}
    return {"status": "ok", "output": completed.stdout.strip() or "Working tree clean."}


def run_tests(root: Path) -> dict[str, str]:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "output": str(exc)}
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part.strip())
    return {"status": "pass" if completed.returncode == 0 else "fail", "output": output}


def kubectl_diagnostics(root: Path) -> dict[str, Any]:
    kubectl_path = shutil.which("kubectl")
    gcloud_path = shutil.which("gcloud")
    diagnostics: dict[str, Any] = {
        "tools": {
            "kubectl": kubectl_path or "not found",
            "gcloud": gcloud_path or "not found",
        },
        "contexts": [],
        "currentContext": "",
        "cluster": {},
        "workloads": [],
        "services": [],
        "pods": [],
        "repoMatches": [],
        "githubActions": scan_github_actions_deployments(root),
        "notes": [],
    }

    if gcloud_path:
        diagnostics["gcloud"] = {
            "project": _run_tool([gcloud_path, "config", "get-value", "project"]),
            "account": _run_tool([gcloud_path, "config", "get-value", "account"]),
        }
    else:
        diagnostics["notes"].append("gcloud was not found on PATH.")

    if not kubectl_path:
        diagnostics["notes"].append("kubectl was not found on PATH.")
        return diagnostics

    contexts = _run_tool([kubectl_path, "config", "get-contexts", "-o", "name"])
    diagnostics["contexts"] = [line.strip() for line in contexts.get("stdout", "").splitlines() if line.strip()]
    current = _run_tool([kubectl_path, "config", "current-context"])
    diagnostics["currentContext"] = current.get("stdout", "").strip()
    if current.get("status") != "ok":
        diagnostics["notes"].append("kubectl has no current context or cannot read kubeconfig.")
        return diagnostics

    diagnostics["cluster"] = _compact_tool_result(_run_tool([kubectl_path, "cluster-info"]))
    namespaces = _kubectl_json(kubectl_path, ["get", "namespaces", "-o", "json"])
    pods = _kubectl_json(kubectl_path, ["get", "pods", "-A", "-o", "json"])
    deployments = _kubectl_json(kubectl_path, ["get", "deployments", "-A", "-o", "json"])
    services = _kubectl_json(kubectl_path, ["get", "services", "-A", "-o", "json"])

    diagnostics["namespaces"] = [
        item.get("metadata", {}).get("name", "")
        for item in namespaces.get("items", [])
        if item.get("metadata", {}).get("name")
    ]
    diagnostics["pods"] = summarize_pods(pods)
    diagnostics["workloads"] = summarize_deployments(deployments)
    diagnostics["services"] = summarize_services(services)
    diagnostics["repoMatches"] = match_workloads_to_repos(root, diagnostics["workloads"], diagnostics["pods"])
    return diagnostics


def _run_tool(command: list[str], timeout: int = 20) -> dict[str, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "stdout": "", "stderr": str(exc)}
    return {
        "status": "ok" if completed.returncode == 0 else "fail",
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _compact_tool_result(result: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in result.items() if value}


def _kubectl_json(kubectl_path: str, args: list[str]) -> dict[str, Any]:
    result = _run_tool([kubectl_path, *args])
    if result.get("status") != "ok":
        return {"items": [], "error": result.get("stderr") or result.get("stdout", "")}
    try:
        data = json.loads(result.get("stdout", "") or "{}")
    except json.JSONDecodeError:
        return {"items": [], "error": "kubectl returned invalid JSON."}
    return data if isinstance(data, dict) else {"items": []}


def summarize_pods(data: dict[str, Any]) -> list[dict[str, Any]]:
    pods: list[dict[str, Any]] = []
    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        pods.append(
            {
                "namespace": metadata.get("namespace", ""),
                "name": metadata.get("name", ""),
                "phase": status.get("phase", ""),
                "node": spec.get("nodeName", ""),
                "owners": [
                    {"kind": owner.get("kind", ""), "name": owner.get("name", "")}
                    for owner in metadata.get("ownerReferences", [])
                ],
                "labels": metadata.get("labels", {}),
                "images": [container.get("image", "") for container in spec.get("containers", []) if container.get("image")],
                "restarts": sum(
                    int(container.get("restartCount", 0))
                    for container in status.get("containerStatuses", [])
                    if isinstance(container, dict)
                ),
            }
        )
    return pods


def summarize_deployments(data: dict[str, Any]) -> list[dict[str, Any]]:
    workloads: list[dict[str, Any]] = []
    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        pod_spec = spec.get("template", {}).get("spec", {})
        workloads.append(
            {
                "kind": "Deployment",
                "namespace": metadata.get("namespace", ""),
                "name": metadata.get("name", ""),
                "replicas": spec.get("replicas", 0),
                "available": status.get("availableReplicas", 0),
                "labels": metadata.get("labels", {}),
                "selector": spec.get("selector", {}).get("matchLabels", {}),
                "images": [container.get("image", "") for container in pod_spec.get("containers", []) if container.get("image")],
            }
        )
    return workloads


def summarize_services(data: dict[str, Any]) -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        services.append(
            {
                "namespace": metadata.get("namespace", ""),
                "name": metadata.get("name", ""),
                "type": spec.get("type", ""),
                "selector": spec.get("selector", {}),
                "ports": [
                    {
                        "port": port.get("port"),
                        "targetPort": port.get("targetPort"),
                        "protocol": port.get("protocol", ""),
                    }
                    for port in spec.get("ports", [])
                ],
            }
        )
    return services


def match_workloads_to_repos(root: Path, workloads: list[dict[str, Any]], pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repo_names = [path.name for path in root.iterdir() if path.is_dir() and path.name not in SKIP_DIRS]
    matches: list[dict[str, Any]] = []
    for workload in workloads:
        haystack = " ".join([workload.get("name", ""), *workload.get("images", [])]).lower()
        matched = [name for name in repo_names if name.lower().replace("_", "-") in haystack or name.lower() in haystack]
        if matched:
            matches.append(
                {
                    "workload": f"{workload.get('namespace')}/{workload.get('name')}",
                    "repos": matched,
                    "images": workload.get("images", []),
                }
            )
    for pod in pods:
        haystack = " ".join([pod.get("name", ""), *pod.get("images", [])]).lower()
        matched = [name for name in repo_names if name.lower().replace("_", "-") in haystack or name.lower() in haystack]
        if matched:
            matches.append(
                {
                    "pod": f"{pod.get('namespace')}/{pod.get('name')}",
                    "repos": matched,
                    "images": pod.get("images", []),
                }
            )
    return matches[:80]


def scan_github_actions_deployments(root: Path) -> list[dict[str, Any]]:
    workflows: list[dict[str, Any]] = []
    for workflow in sorted(root.rglob(".github/workflows/*")):
        if not workflow.is_file() or workflow.suffix.lower() not in {".yml", ".yaml"}:
            continue
        if any(part in SKIP_DIRS for part in workflow.relative_to(root).parts):
            continue
        content = read_text_safely(workflow, max_chars=80_000)
        lowered = content.lower()
        signals = [
            signal
            for signal in ("kubectl", "gcloud", "helm", "kustomize", "docker", "artifact registry", "gke", "cloud run")
            if signal in lowered
        ]
        if signals:
            workflows.append(
                {
                    "path": relative(workflow, root),
                    "signals": signals,
                    "snippet": "\n".join(
                        line.strip()
                        for line in content.splitlines()
                        if any(signal in line.lower() for signal in signals)
                    )[:1200],
                }
            )
    return workflows[:80]


def lint_workspace_file(root: Path, rel_path: str) -> dict[str, Any]:
    path = _safe_path(root, rel_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(rel_path)
    if not _is_text_file(path):
        return {"path": relative(path, root), "language": "text", "diagnostics": []}

    content = read_text_safely(path, max_chars=500_000)
    language = _language(path)
    diagnostics: list[dict[str, Any]] = []
    was_truncated = content.endswith("\n\n[truncated]\n")

    if was_truncated:
        diagnostics.append(
            {
                "severity": "hint",
                "line": 1,
                "message": "File is too large for syntax linting in the web UI.",
            }
        )
    elif language == "python":
        try:
            ast.parse(content, filename=relative(path, root))
        except SyntaxError as exc:
            diagnostics.append(
                {
                    "severity": "error",
                    "line": exc.lineno or 1,
                    "message": exc.msg,
                }
            )
    elif language in {"javascript", "typescript"}:
        diagnostics.extend(_lint_javascript_like(content))

    for line_number, line in enumerate(content.splitlines(), start=1):
        if line.rstrip() != line:
            diagnostics.append(
                {
                    "severity": "warning",
                    "line": line_number,
                    "message": "Trailing whitespace.",
                }
            )
        if len(line) > 120:
            diagnostics.append(
                {
                    "severity": "hint",
                    "line": line_number,
                    "message": "Line is longer than 120 characters.",
                }
            )

    return {"path": relative(path, root), "language": language, "diagnostics": diagnostics}


def settings_payload(root: Path) -> dict[str, Any]:
    settings = resolve_settings(root)
    return {
        "settings": json.loads(settings.to_json()),
        "path": relative(settings_path_for_display(root), root),
        "apiKeyAvailable": bool(os.getenv(settings.api_key_env_var)),
    }


def settings_path_for_display(root: Path) -> Path:
    return root / BB_DIR / "settings.json"


def update_settings(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    current = load_settings(root)
    provider = str(payload.get("provider", current.provider)).strip() or "ollama"
    if provider not in {"ollama", "openai-compatible"}:
        raise ValueError("Unsupported provider")
    timeout = int(payload.get("timeout_seconds", current.timeout_seconds))
    if timeout < 1:
        raise ValueError("Timeout must be positive")
    settings = AgentSettings(
        provider=provider,
        ollama_url=str(payload.get("ollama_url", current.ollama_url)).strip(),
        api_base_url=str(payload.get("api_base_url", current.api_base_url)).strip(),
        api_key_env_var=str(payload.get("api_key_env_var", current.api_key_env_var)).strip() or "OPENAI_API_KEY",
        model=str(payload.get("model", current.model)).strip() or current.model,
        timeout_seconds=timeout,
    )
    save_settings(root, settings)
    return settings_payload(root)


def test_model_settings(root: Path, payload: dict[str, Any]) -> dict[str, str]:
    settings = AgentSettings.from_dict(payload) if payload else resolve_settings(root)
    try:
        client = model_client(settings)
        response = client.generate("Reply with exactly: ok")
    except ModelError as exc:
        return {"status": "fail", "output": str(exc)}
    return {"status": "pass", "output": response}


def list_model_settings(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    settings = AgentSettings.from_dict(payload) if payload else resolve_settings(root)
    try:
        client = model_client(settings)
        models = client.list_models()
    except ModelError as exc:
        return {"status": "fail", "models": [], "output": str(exc)}
    output = f"Found {len(models)} model{'s' if len(models) != 1 else ''}."
    return {"status": "pass", "models": models, "output": output}


def model_client(settings: AgentSettings) -> OllamaClient | OpenAICompatibleClient:
    if settings.provider == "ollama":
        return OllamaClient(
            base_url=settings.ollama_url,
            model=settings.model,
            timeout_seconds=settings.timeout_seconds,
        )
    if settings.provider == "openai-compatible":
        if not settings.api_base_url:
            raise ModelError("Remote API base URL is required for openai-compatible provider.")
        return OpenAICompatibleClient(
            base_url=settings.api_base_url,
            model=settings.model,
            api_key_env_var=settings.api_key_env_var,
            timeout_seconds=settings.timeout_seconds,
        )
    raise ModelError(f"Unsupported provider `{settings.provider}`.")


def _lint_javascript_like(content: str) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    closing = {value: key for key, value in pairs.items()}
    stack: list[tuple[str, int]] = []
    in_string: str | None = None
    escaped = False

    for line_number, line in enumerate(content.splitlines(), start=1):
        for char in line:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == in_string:
                    in_string = None
                continue
            if char in {"'", '"', "`"}:
                in_string = char
            elif char in pairs:
                stack.append((char, line_number))
            elif char in closing:
                if not stack or stack[-1][0] != closing[char]:
                    diagnostics.append(
                        {
                            "severity": "error",
                            "line": line_number,
                            "message": f"Unmatched `{char}`.",
                        }
                    )
                else:
                    stack.pop()
    for char, line_number in stack[-5:]:
        diagnostics.append(
            {
                "severity": "error",
                "line": line_number,
                "message": f"Unclosed `{char}`.",
            }
        )
    return diagnostics


def build_agent_prompt(root: Path, message: str, mode: str, open_files: list[str]) -> str:
    context = scan_repository(root).to_markdown()
    explorer_context = build_explorer_context(root)
    cloud_inventory = collect_cloud_native_inventory(root)
    files: list[str] = []
    for rel_path in open_files[:6]:
        try:
            file_data = read_workspace_file(root, rel_path)
        except (FileNotFoundError, ValueError):
            continue
        files.append(f"### {file_data['path']}\n```{file_data['language']}\n{file_data['content']}\n```")

    mode_instruction = {
        "plan": "Return an implementation plan with risks, tests, and documentation updates.",
        "debug": "Diagnose the issue, identify likely causes, and suggest safe next checks.",
        "hints": "Provide concise inline code hints without rewriting whole files.",
        "agent": "Act as an approval-gated coding agent: propose concrete multi-file changes and explain what each change fixes.",
    }.get(mode, "Help the user safely understand and improve the codebase.")

    return f"""You are bb-code inside a VS Code-like local web UI.

Safety rules:
- Do not claim that you edited files.
- Do not propose background daemons or autonomous loops.
- For code changes, provide reviewable suggestions or patch-style snippets.
- In Agent mode, be specific about which files should change and why.
- Reply to the chat in plain text only. Do not wrap the main chat response in JSON or Markdown fences.
- You can read and reason about the file contents included below. Do not say you cannot access them.
- If a user asks about an opened file, use the provided file contents directly.
- Never apologize that you cannot access external files when the relevant file contents are already included below.
- Ask for user approval before any destructive or broad change.
- Explain the Buildly way: Python-first, Docker-first local setup, no Makefiles, ops/startup.sh when relevant, devdocs updates, minimal useful tests.
- Handle cloud native apps by considering Docker, services, env vars, health checks, and deployment boundaries.
- Treat the Explorer context as authoritative. If it lists folders or repos, they exist even when the generic repository scan says no standard files were found.
- In multi-repo or microservice workspaces, treat top-level folders as candidate services and inspect/ask for the relevant service files before proposing new scaffolding.
- If the user names a service such as buildly-core, focus on that existing service instead of suggesting a new empty app structure.

Mode: {mode}
Mode instruction: {mode_instruction}

Workspace context:
{context}

Explorer context:
{explorer_context}

Cloud-native service inventory:
```json
{json.dumps(cloud_inventory, indent=2)}
```

Open files:
{chr(10).join(files) if files else "No files opened."}

User message:
{message}
"""


def build_explorer_context(root: Path) -> str:
    workspace = collect_workspace(root)
    lines = [
        f"Explorer root: `{workspace['root']}`",
        f"Workspace name: `{workspace['name']}`",
        "",
        "Visible repos/services:",
    ]
    repos = workspace.get("repos", [])
    if repos:
        for repo in repos:
            lines.append(f"- `{repo['path']}` ({repo['name']})")
    else:
        lines.append("- No repo markers found, but top-level folders below may still be services.")

    lines.extend(["", "Top-level Explorer entries:"])
    for node in workspace.get("tree", [])[:80]:
        suffix = "/" if node.get("type") == "dir" else ""
        lines.append(f"- `{node['path']}{suffix}`")
        for child in node.get("children", [])[:12]:
            child_suffix = "/" if child.get("type") == "dir" else ""
            lines.append(f"  - `{child['path']}{child_suffix}`")
    return "\n".join(lines)


def build_edit_suggestion_prompt(root: Path, message: str, open_files: list[str]) -> str:
    files: list[str] = []
    for rel_path in open_files[:6]:
        try:
            file_data = read_workspace_file(root, rel_path)
        except (FileNotFoundError, ValueError):
            continue
        files.append(f"### {file_data['path']}\n```{file_data['language']}\n{file_data['content']}\n```")

    return f"""You are bb-code preparing approval-gated file edits.

Return JSON only. No Markdown. No prose outside JSON.

Schema:
{{
  "edits": [
    {{
      "path": "relative/path",
      "summary": "short human-readable change summary",
      "find": "exact text to replace",
      "replace": "replacement text"
    }}
  ],
  "notes": ["short note"]
}}

Rules:
- Only suggest edits for files included below.
- Prefer exact find/replace edits for small changes.
- For insertions, use "find" as the nearby exact text and "replace" as that same text plus the insertion.
- If a full file rewrite is truly needed, use "content" with the full replacement file content.
- Keep changes minimal and directly related to the user request.
- Do not include destructive changes.
- If no safe edit is possible, return {{"edits": [], "notes": ["reason"]}}.

User request:
{message}

Open files:
{chr(10).join(files) if files else "No files opened."}
"""


def chat_with_model(root: Path, payload: dict[str, Any], settings_root: Path | None = None) -> dict[str, Any]:
    settings = resolve_settings(settings_root or root)
    client = model_client(settings)
    prompt = build_agent_prompt(
        root,
        str(payload.get("message", "")),
        str(payload.get("mode", "agent")),
        [str(item) for item in payload.get("openFiles", []) if isinstance(item, str)],
    )
    try:
        response = normalize_chat_response(client.generate(prompt))
    except ModelError as exc:
        return {"role": "assistant", "content": f"Model error: {exc}", "edits": []}

    edits: list[dict[str, str]] = []
    if str(payload.get("mode", "agent")) == "agent":
        edit_prompt = build_edit_suggestion_prompt(
            root,
            str(payload.get("message", "")),
            [str(item) for item in payload.get("openFiles", []) if isinstance(item, str)],
        )
        try:
            edit_response = client.generate(edit_prompt)
            edits = parse_edit_suggestions(root, edit_response)
        except ModelError:
            edits = []
    return {"role": "assistant", "content": response, "edits": edits}


def normalize_chat_response(raw: str) -> str:
    text = raw.strip()
    match = re.search(r"(?s)```(?:json)?\s*(\{.*\})\s*```", text)
    if match:
        text = match.group(1)
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return raw.strip()
        response = data.get("response") or data.get("content") or data.get("message")
        if isinstance(response, str):
            return response.strip()
        notes = data.get("notes")
        if isinstance(notes, list) and all(isinstance(item, str) for item in notes):
            return "\n".join(item.strip() for item in notes if item.strip())
        return "The model returned structured JSON instead of a plain chat response, so bb-code hid the raw object. Try the request again or switch to Plan, Debug, or Hints mode."
    return raw.strip()


def parse_edit_suggestions(root: Path, raw: str) -> list[dict[str, str]]:
    text = raw.strip()
    match = re.search(r"(?s)```(?:json)?\s*(\{.*\})\s*```", text)
    if match:
        text = match.group(1)
    elif "{" in text and "}" in text:
        text = text[text.find("{") : text.rfind("}") + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    edits = data.get("edits", [])
    if not isinstance(edits, list):
        return []
    parsed: list[dict[str, str]] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        path = str(edit.get("path", ""))
        summary = str(edit.get("summary", "Suggested edit"))
        if not path:
            continue
        try:
            safe_path = _safe_path(root, path)
        except ValueError:
            continue
        if not safe_path.exists() or not safe_path.is_file() or not _is_text_file(safe_path):
            continue
        original = read_text_safely(safe_path, max_chars=200_000)
        content = _materialize_edit_content(original, edit)
        if content is None or _looks_like_truncated_replacement(original, content):
            continue
        entry = {"path": relative(safe_path, root), "summary": summary, "content": content, "original": original}
        raw_find = edit.get("find")
        raw_replace = edit.get("replace")
        if isinstance(raw_find, str) and isinstance(raw_replace, str) and raw_find:
            entry["find"] = raw_find
            entry["replace"] = raw_replace
        parsed.append(entry)
    return parsed


def _materialize_edit_content(original: str, edit: dict[str, Any]) -> str | None:
    content = edit.get("content")
    if isinstance(content, str):
        return content

    find = edit.get("find")
    replace = edit.get("replace")
    if isinstance(find, str) and isinstance(replace, str) and find:
        if find not in original:
            return None
        return original.replace(find, replace, 1)
    return None


def _looks_like_truncated_replacement(original: str, replacement: str) -> bool:
    if len(original) < 400:
        return False
    if len(replacement) >= len(original) * 0.5:
        return False
    original_lines = {line.strip() for line in original.splitlines() if line.strip()}
    replacement_lines = {line.strip() for line in replacement.splitlines() if line.strip()}
    if not replacement_lines:
        return True
    overlap = len(original_lines & replacement_lines)
    return overlap < max(3, len(replacement_lines) // 2)


def run_diagnostics(root: Path) -> dict[str, str]:
    settings = resolve_settings(root)
    report = SelfDiagnosticEngine(root, settings).run(check_model=False)
    return {"markdown": report.to_markdown()}


def run_diagnostics_with_settings(root: Path, settings_root: Path) -> dict[str, str]:
    settings = resolve_settings(settings_root)
    report = SelfDiagnosticEngine(root, settings).run(check_model=False)
    return {"markdown": report.to_markdown()}


SERVICE_MARKERS = {
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Dockerfile",
    "docker-compose.yml",
    "compose.yml",
    "app.py",
    "main.py",
}


def collect_cloud_native_inventory(root: Path) -> dict[str, Any]:
    services: list[dict[str, Any]] = []
    model_names: dict[str, list[str]] = {}
    dependency_names: dict[str, list[str]] = {}
    cache_hits: list[dict[str, str]] = []
    gateway_hits: list[dict[str, str]] = []

    for folder in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda item: item.name.lower()):
        if folder.name in SKIP_DIRS or folder.name.startswith("."):
            continue
        manifests = [marker for marker in SERVICE_MARKERS if (folder / marker).exists()]
        text_files = [path for path in _iter_service_files(folder)[:80]]
        dependencies = _collect_dependencies(folder)
        models = _collect_model_names(text_files)
        caches = _collect_keyword_hits(text_files, {"cache", "redis", "memcached", "ttl"})
        gateways = _collect_keyword_hits(text_files, {"gateway", "router", "nginx", "traefik", "ingress", "cors"})

        if manifests or dependencies or models or caches or gateways or text_files:
            service = {
                "name": folder.name,
                "path": relative(folder, root),
                "manifests": manifests,
                "dependencies": dependencies,
                "models": models,
                "cacheSignals": caches[:8],
                "gatewaySignals": gateways[:8],
            }
            services.append(service)

            for dependency in dependencies:
                dependency_names.setdefault(dependency, []).append(folder.name)
            for model in models:
                model_names.setdefault(model, []).append(folder.name)
            cache_hits.extend({"service": folder.name, **hit} for hit in caches[:8])
            gateway_hits.extend({"service": folder.name, **hit} for hit in gateways[:8])

    return {
        "workspace": str(root),
        "services": services,
        "sharedDependencies": {
            name: sorted(set(service_names))
            for name, service_names in sorted(dependency_names.items())
            if len(set(service_names)) > 1
        },
        "duplicateModels": {
            name: sorted(set(service_names))
            for name, service_names in sorted(model_names.items())
            if len(set(service_names)) > 1
        },
        "cacheSignals": cache_hits[:40],
        "gatewaySignals": gateway_hits[:40],
    }


def build_cloud_native_audit_prompt(root: Path) -> str:
    inventory = collect_cloud_native_inventory(root)
    return f"""You are bb-code auditing a multi-repo or cloud-native workspace.

Treat each top-level folder as a potential microservice. Use the inventory below to produce a concise architecture audit with these sections:

- Service map
- Library and dependency mapping
- Model duplication and consolidation risks
- All-repo model ERD, inferred from discovered model names
- Gateway, routing, and cache efficiency review
- Suggested target architecture
- Highest-priority next checks

Be specific about service names and files where signals exist. If evidence is incomplete, say what to inspect next.

Inventory JSON:
{json.dumps(inventory, indent=2)}
"""


def run_cloud_native_audit(root: Path, settings_root: Path | None = None) -> dict[str, Any]:
    inventory = collect_cloud_native_inventory(root)
    settings = resolve_settings(settings_root or root)
    client = model_client(settings)
    try:
        report = normalize_chat_response(client.generate(build_cloud_native_audit_prompt(root)))
        status = "pass"
    except ModelError as exc:
        report = f"Model error: {exc}\n\nLocal inventory was still collected for review."
        status = "model-error"
    return {"status": status, "report": report, "inventory": inventory}


DATABASE_KEYWORDS = {
    "postgres": {"postgres", "postgresql", "psycopg", "asyncpg", "pgvector"},
    "mysql": {"mysql", "pymysql", "mysqlclient", "mariadb"},
    "sqlite": {"sqlite", "sqlite3"},
    "mongodb": {"mongodb", "mongo", "pymongo", "motor"},
    "redis": {"redis", "django-redis", "cacheops"},
    "memcached": {"memcached", "pymemcache"},
    "elasticsearch": {"elasticsearch", "opensearch"},
    "kafka": {"kafka", "confluent"},
    "rabbitmq": {"rabbitmq", "amqp", "pika"},
    "celery": {"celery"},
}


def collect_database_and_model_inventory(root: Path) -> dict[str, Any]:
    services: list[dict[str, Any]] = []
    all_models: dict[str, list[str]] = {}
    all_databases: dict[str, list[str]] = {}

    for folder in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda item: item.name.lower()):
        if folder.name in SKIP_DIRS or folder.name.startswith("."):
            continue
        text_files = _iter_service_files(folder)[:120]
        dependencies = _collect_dependencies(folder)
        dependency_text = " ".join(dependencies).lower()
        database_signals: dict[str, list[dict[str, str]]] = {}
        for database, keywords in DATABASE_KEYWORDS.items():
            hits = _collect_keyword_hits(text_files, keywords)
            if any(keyword in dependency_text for keyword in keywords) or hits:
                database_signals[database] = hits[:6]
                all_databases.setdefault(database, []).append(folder.name)

        models = _collect_model_names(text_files)
        for model in models:
            all_models.setdefault(model, []).append(folder.name)

        if models or database_signals:
            services.append(
                {
                    "name": folder.name,
                    "path": relative(folder, root),
                    "models": models,
                    "databases": sorted(database_signals),
                    "databaseSignals": database_signals,
                }
            )

    return {
        "services": services,
        "databases": {name: sorted(set(names)) for name, names in sorted(all_databases.items())},
        "duplicateModels": {
            name: sorted(set(names)) for name, names in sorted(all_models.items()) if len(set(names)) > 1
        },
        "models": {name: sorted(set(names)) for name, names in sorted(all_models.items())},
    }


def generate_platform_report(
    root: Path,
    settings_root: Path | None = None,
    *,
    include_ai: bool = True,
) -> dict[str, Any]:
    inventory = collect_cloud_native_inventory(root)
    database_inventory = collect_database_and_model_inventory(root)
    kubernetes = kubectl_diagnostics(root)
    github_actions = kubernetes.get("githubActions") or scan_github_actions_deployments(root)
    findings = analyze_platform_findings(inventory, database_inventory, kubernetes, github_actions)
    ai_report = ""
    status = "pass"
    if include_ai:
        try:
            settings = resolve_settings(settings_root or root)
            ai_report = normalize_chat_response(
                model_client(settings).generate(
                    build_platform_report_prompt(root, inventory, database_inventory, kubernetes, findings)
                )
            )
        except ModelError as exc:
            ai_report = f"Model enhancement unavailable: {exc}"
            status = "model-error"

    reports = render_platform_reports(
        root=root,
        inventory=inventory,
        database_inventory=database_inventory,
        kubernetes=kubernetes,
        github_actions=github_actions,
        findings=findings,
        ai_report=ai_report,
    )
    report_dir = root / BB_DIR / REPORTS_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, str]] = []
    for filename, content in reports.items():
        path = report_dir / filename
        path.write_text(content, encoding="utf-8")
        written.append({"name": filename, "path": relative(path, root), "content": content})

    return {
        "status": status,
        "summary": {
            "services": len(inventory.get("services", [])),
            "databases": len(database_inventory.get("databases", {})),
            "kubernetesWorkloads": len(kubernetes.get("workloads", [])),
            "kubernetesPods": len(kubernetes.get("pods", [])),
            "findings": len(findings.get("performance", []))
            + len(findings.get("refactor", []))
            + len(findings.get("bugs", [])),
        },
        "reports": written,
        "findings": findings,
    }


def build_platform_report_prompt(
    root: Path,
    inventory: dict[str, Any],
    database_inventory: dict[str, Any],
    kubernetes: dict[str, Any],
    findings: dict[str, list[dict[str, str]]],
) -> str:
    payload = {
        "workspace": str(root),
        "serviceInventory": inventory,
        "databaseAndModelInventory": database_inventory,
        "kubernetes": {
            "tools": kubernetes.get("tools", {}),
            "currentContext": kubernetes.get("currentContext", ""),
            "contexts": kubernetes.get("contexts", []),
            "namespaces": kubernetes.get("namespaces", []),
            "workloads": kubernetes.get("workloads", [])[:120],
            "pods": kubernetes.get("pods", [])[:120],
            "services": kubernetes.get("services", [])[:120],
            "repoMatches": kubernetes.get("repoMatches", [])[:120],
            "notes": kubernetes.get("notes", []),
        },
        "localFindings": findings,
    }
    return f"""You are bb-code writing a platform diagnostic and refactor report.

Use the evidence below to produce concise, evidence-backed recommendations for a single app or microservice platform.
Cover performance, database/model design, gateway/cache efficiency, Kubernetes deployment health, bug risks, and refactor opportunities.
Do not invent resources that are not present. If kubectl data is missing, say exactly what is missing.
Write the report directly. Do not ask follow-up questions.

Evidence JSON:
{json.dumps(payload, indent=2)}
"""


def analyze_platform_findings(
    inventory: dict[str, Any],
    database_inventory: dict[str, Any],
    kubernetes: dict[str, Any],
    github_actions: list[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    findings: dict[str, list[dict[str, str]]] = {"performance": [], "refactor": [], "bugs": []}
    for pod in kubernetes.get("pods", []):
        restarts = int(pod.get("restarts", 0) or 0)
        phase = str(pod.get("phase", ""))
        name = f"{pod.get('namespace')}/{pod.get('name')}"
        if restarts:
            findings["performance"].append(
                {"title": "Pod restarts detected", "detail": f"{name} has {restarts} restart(s)."}
            )
        if phase and phase != "Running":
            findings["bugs"].append({"title": "Pod is not running", "detail": f"{name} is in phase {phase}."})

    for workload in kubernetes.get("workloads", []):
        replicas = int(workload.get("replicas", 0) or 0)
        available = int(workload.get("available", 0) or 0)
        if replicas > available:
            findings["bugs"].append(
                {
                    "title": "Deployment availability gap",
                    "detail": f"{workload.get('namespace')}/{workload.get('name')} has {available}/{replicas} replicas available.",
                }
            )

    for service in kubernetes.get("services", []):
        if not service.get("selector") and service.get("type") != "ExternalName":
            findings["bugs"].append(
                {
                    "title": "Service has no selector",
                    "detail": f"{service.get('namespace')}/{service.get('name')} may not route to pods.",
                }
            )

    for model, services in inventory.get("duplicateModels", {}).items():
        findings["refactor"].append(
            {"title": "Duplicate model name", "detail": f"{model} appears in {', '.join(services)}."}
        )
    for model, services in database_inventory.get("duplicateModels", {}).items():
        detail = f"{model} appears in {', '.join(services)}."
        if not any(item["detail"] == detail for item in findings["refactor"]):
            findings["refactor"].append({"title": "Duplicate data model", "detail": detail})

    for service in inventory.get("services", []):
        manifests = set(service.get("manifests", []))
        if service.get("dependencies") and "Dockerfile" not in manifests:
            findings["refactor"].append(
                {
                    "title": "Service lacks Dockerfile marker",
                    "detail": f"{service.get('name')} has dependencies but no Dockerfile was found at the service root.",
                }
            )
        if service.get("gatewaySignals") and not service.get("cacheSignals"):
            findings["performance"].append(
                {
                    "title": "Gateway path without cache signal",
                    "detail": f"{service.get('name')} has gateway/routing signals but no cache signal in the scanned files.",
                }
            )

    if kubernetes.get("tools", {}).get("kubectl") == "not found":
        findings["bugs"].append({"title": "kubectl unavailable", "detail": "Install kubectl or add it to PATH for live cluster diagnostics."})
    elif not kubernetes.get("currentContext"):
        findings["bugs"].append({"title": "No Kubernetes context", "detail": "kubectl did not report a current context."})

    if not github_actions:
        findings["refactor"].append(
            {
                "title": "No deployment workflow signals",
                "detail": "No GitHub Actions workflows with kubectl, gcloud, helm, Docker, GKE, or Cloud Run signals were found.",
            }
        )
    if not database_inventory.get("services"):
        findings["refactor"].append(
            {"title": "No database/model map found", "detail": "No database or model signals were discovered in top-level services."}
        )
    return findings


def render_platform_reports(
    *,
    root: Path,
    inventory: dict[str, Any],
    database_inventory: dict[str, Any],
    kubernetes: dict[str, Any],
    github_actions: list[dict[str, Any]],
    findings: dict[str, list[dict[str, str]]],
    ai_report: str,
) -> dict[str, str]:
    generated = datetime.now().isoformat(timespec="seconds")
    diagnostic = "\n".join(
        [
            "# Platform Diagnostic Report",
            "",
            f"Generated: {generated}",
            f"Workspace: `{root}`",
            "",
            "## Summary",
            f"- Services scanned: {len(inventory.get('services', []))}",
            f"- Databases detected: {len(database_inventory.get('databases', {}))}",
            f"- Kubernetes context: {kubernetes.get('currentContext') or 'none'}",
            f"- Workloads: {len(kubernetes.get('workloads', []))}",
            f"- Pods: {len(kubernetes.get('pods', []))}",
            f"- Services: {len(kubernetes.get('services', []))}",
            f"- GitHub deployment workflows: {len(github_actions)}",
            "",
            "## Service Map",
            *_service_lines(inventory),
            "",
            "## Kubernetes",
            *_kubernetes_lines(kubernetes),
            "",
            "## Databases And Models",
            *_database_lines(database_inventory),
            "",
            "## Deployment Automation",
            *_github_action_lines(github_actions),
            "",
            "## AI Review",
            ai_report or "AI review was not requested or no configured model was available.",
        ]
    )
    refactor = "\n".join(
        [
            "# Suggested Refactor Report",
            "",
            f"Generated: {generated}",
            "",
            "## Refactor Opportunities",
            *_finding_lines(findings.get("refactor", [])),
            "",
            "## Performance Opportunities",
            *_finding_lines(findings.get("performance", [])),
            "",
            "## Dependency Map",
            *_mapping_lines(inventory.get("sharedDependencies", {}), empty="No shared dependencies were detected."),
            "",
            "## Duplicate Models",
            *_mapping_lines(
                {**inventory.get("duplicateModels", {}), **database_inventory.get("duplicateModels", {})},
                empty="No duplicate model names were detected.",
            ),
        ]
    )
    bugs = "\n".join(
        [
            "# Bug Risk Report",
            "",
            f"Generated: {generated}",
            "",
            "## Bug Risks",
            *_finding_lines(findings.get("bugs", [])),
            "",
            "## Cluster Notes",
            *_plain_lines(kubernetes.get("notes", []), empty="No cluster diagnostic notes."),
        ]
    )
    return {
        "platform-diagnostic.md": diagnostic + "\n",
        "suggested-refactor-report.md": refactor + "\n",
        "bug-risk-report.md": bugs + "\n",
    }


def _service_lines(inventory: dict[str, Any]) -> list[str]:
    services = inventory.get("services", [])
    if not services:
        return ["No services were discovered."]
    return [
        f"- `{service.get('path')}`: manifests={', '.join(service.get('manifests', [])) or 'none'}; "
        f"dependencies={len(service.get('dependencies', []))}; models={', '.join(service.get('models', [])) or 'none'}"
        for service in services
    ]


def _kubernetes_lines(kubernetes: dict[str, Any]) -> list[str]:
    lines = [
        f"- kubectl: {kubernetes.get('tools', {}).get('kubectl', 'not found')}",
        f"- gcloud: {kubernetes.get('tools', {}).get('gcloud', 'not found')}",
        f"- current context: {kubernetes.get('currentContext') or 'none'}",
    ]
    lines.extend(
        f"- deployment `{item.get('namespace')}/{item.get('name')}`: {item.get('available')}/{item.get('replicas')} available; images={', '.join(item.get('images', [])) or 'none'}"
        for item in kubernetes.get("workloads", [])[:80]
    )
    lines.extend(
        f"- pod `{item.get('namespace')}/{item.get('name')}`: phase={item.get('phase')}; restarts={item.get('restarts')}"
        for item in kubernetes.get("pods", [])[:80]
    )
    return lines


def _database_lines(database_inventory: dict[str, Any]) -> list[str]:
    lines = ["### Databases", *_mapping_lines(database_inventory.get("databases", {}), empty="No database signals found.")]
    lines.extend(["", "### Models"])
    lines.extend(_mapping_lines(database_inventory.get("models", {}), empty="No model signals found."))
    return lines


def _github_action_lines(workflows: list[dict[str, Any]]) -> list[str]:
    if not workflows:
        return ["No GitHub Actions deployment signals found."]
    return [f"- `{workflow.get('path')}`: {', '.join(workflow.get('signals', []))}" for workflow in workflows]


def _mapping_lines(mapping: dict[str, list[str]], *, empty: str) -> list[str]:
    if not mapping:
        return [empty]
    return [f"- `{name}`: {', '.join(values)}" for name, values in sorted(mapping.items())]


def _finding_lines(findings: list[dict[str, str]]) -> list[str]:
    if not findings:
        return ["No findings in this category."]
    return [f"- **{item.get('title', 'Finding')}**: {item.get('detail', '')}" for item in findings]


def _plain_lines(items: list[str], *, empty: str) -> list[str]:
    if not items:
        return [empty]
    return [f"- {item}" for item in items]


def _iter_service_files(folder: Path) -> list[Path]:
    paths: list[Path] = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(folder).parts):
            continue
        if _is_text_file(path):
            paths.append(path)
    return sorted(paths)


def _collect_dependencies(folder: Path) -> list[str]:
    dependencies: set[str] = set()
    requirements = folder / "requirements.txt"
    if requirements.exists():
        for line in read_text_safely(requirements, max_chars=20_000).splitlines():
            name = re.split(r"[<>=~!;\s]", line.strip(), maxsplit=1)[0]
            if name and not name.startswith("#"):
                dependencies.add(name)

    package_json = folder / "package.json"
    if package_json.exists():
        try:
            data = json.loads(read_text_safely(package_json, max_chars=80_000))
        except json.JSONDecodeError:
            data = {}
        for section in ("dependencies", "devDependencies"):
            values = data.get(section, {})
            if isinstance(values, dict):
                dependencies.update(str(name) for name in values)

    pyproject = folder / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(read_text_safely(pyproject, max_chars=80_000))
        except tomllib.TOMLDecodeError:
            data = {}
        project = data.get("project", {}) if isinstance(data, dict) else {}
        if isinstance(project, dict):
            for dependency in project.get("dependencies", []):
                if isinstance(dependency, str):
                    name = re.split(r"[<>=~!;\s\[]", dependency.strip(), maxsplit=1)[0]
                    if name:
                        dependencies.add(name)
            optional = project.get("optional-dependencies", {})
            if isinstance(optional, dict):
                for values in optional.values():
                    if isinstance(values, list):
                        for dependency in values:
                            if isinstance(dependency, str):
                                name = re.split(r"[<>=~!;\s\[]", dependency.strip(), maxsplit=1)[0]
                                if name:
                                    dependencies.add(name)
    return sorted(dependencies)


def _collect_model_names(paths: list[Path]) -> list[str]:
    models: set[str] = set()
    model_base_names = {"BaseModel", "SQLModel", "Model", "DeclarativeBase"}
    for path in paths:
        if _language(path) != "python":
            continue
        try:
            tree = ast.parse(read_text_safely(path, max_chars=120_000), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = {_ast_name(base) for base in node.bases}
            if model_base_names & base_names or node.name.endswith(("Model", "Schema", "Entity")):
                models.add(node.name)
    return sorted(models)


def _ast_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _ast_name(node.value)
    return ""


def _collect_keyword_hits(paths: list[Path], keywords: set[str]) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for path in paths:
        content = read_text_safely(path, max_chars=80_000)
        for index, line in enumerate(content.splitlines(), start=1):
            lowered = line.lower()
            if any(keyword in lowered for keyword in keywords):
                hits.append({"path": path.name, "line": str(index), "text": line.strip()[:180]})
                break
        if len(hits) >= 20:
            break
    return hits


def _make_handler(session: WorkspaceSession) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                root = session.root
                if parsed.path == "/":
                    self._send_html(APP_HTML)
                elif parsed.path == "/manifest.json":
                    self._send_static_file("manifest.json", "application/manifest+json")
                elif parsed.path == "/service-worker.js":
                    self._send_static_file("service-worker.js", "application/javascript; charset=utf-8")
                elif parsed.path in {"/icon.png", "/apple-touch-icon.png", "/favicon.ico"}:
                    self._send_logo_file()
                elif parsed.path == "/api/workspace":
                    self._send_json(collect_session_workspace(session))
                elif parsed.path == "/api/context":
                    self._send_json({"markdown": scan_repository(root).to_markdown()})
                elif parsed.path == "/api/diagnostics":
                    self._send_json(run_diagnostics_with_settings(root, session.settings_root))
                elif parsed.path == "/api/settings":
                    self._send_json(settings_payload(session.settings_root))
                elif parsed.path == "/api/file":
                    rel_path = parse_qs(parsed.query).get("path", [""])[0]
                    self._send_json(read_workspace_file(root, rel_path))
                elif parsed.path == "/api/search":
                    query = parse_qs(parsed.query).get("q", [""])[0]
                    self._send_json(search_workspace(root, query))
                elif parsed.path == "/api/git":
                    self._send_json(git_status(root))
                elif parsed.path == "/api/lint":
                    rel_path = parse_qs(parsed.query).get("path", [""])[0]
                    self._send_json(lint_workspace_file(root, rel_path))
                elif parsed.path == "/api/integrations/k8s-monitor":
                    self._send_json(k8s_monitor_integration_status())
                elif parsed.path == "/reports":
                    rel_path = parse_qs(parsed.query).get("path", [""])[0]
                    markdown = read_report_file(root, rel_path)
                    if parse_qs(parsed.query).get("raw", [""])[0] == "1":
                        self._send_markdown(markdown)
                    else:
                        self._send_html(render_report_html(rel_path, markdown))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, json.JSONDecodeError):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            try:
                root = session.root
                if parsed.path == "/api/workspace":
                    self._send_json(session.switch(str(payload.get("path", ""))))
                elif parsed.path == "/api/repo":
                    self._send_json(session.switch_repo(str(payload.get("path", ""))))
                elif parsed.path == "/api/settings":
                    self._send_json(update_settings(session.settings_root, payload))
                elif parsed.path == "/api/settings/test":
                    self._send_json(test_model_settings(session.settings_root, payload))
                elif parsed.path == "/api/settings/models":
                    self._send_json(list_model_settings(session.settings_root, payload))
                elif parsed.path == "/api/chat":
                    self._send_json(chat_with_model(root, payload, session.settings_root))
                elif parsed.path == "/api/apply-edit":
                    self._send_json(
                        write_workspace_file(root, str(payload.get("path", "")), str(payload.get("content", "")))
                    )
                elif parsed.path == "/api/run-tests":
                    self._send_json(run_tests(root))
                elif parsed.path == "/api/cloud-audit":
                    self._send_json(run_cloud_native_audit(root, session.settings_root))
                elif parsed.path == "/api/kubectl-diagnostics":
                    self._send_json(kubectl_diagnostics(root))
                elif parsed.path == "/api/platform-report":
                    self._send_json(generate_platform_report(root, session.settings_root))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, data: dict[str, Any]) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_markdown(self, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_static_file(self, filename: str, content_type: str) -> None:
            path = STATIC_DIR / filename
            if not path.exists() or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_logo_file(self) -> None:
            if not LOGO_PATH.exists() or not LOGO_PATH.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = LOGO_PATH.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def read_report_file(root: Path, rel_path: str) -> str:
    path = _safe_path(root, rel_path)
    reports_root = (root / BB_DIR / REPORTS_DIR).resolve()
    if reports_root not in [path, *path.parents]:
        raise ValueError("Report path must be inside .bb/reports")
    if path.suffix.lower() != ".md":
        raise ValueError("Only Markdown reports can be opened")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(rel_path)
    return read_text_safely(path, max_chars=1_000_000)


def render_report_html(rel_path: str, markdown: str) -> str:
    title = Path(rel_path).name or "Report"
    raw_href = html_lib.escape(f"/reports?path={_url_escape(rel_path)}&raw=1", quote=True)
    body = markdown_to_html(markdown)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html_lib.escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; --bg: #1e1e1e; --panel: #252526; --line: #3c3c3c; --text: #d4d4d4; --muted: #9da3aa; --accent: #d7ba7d; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .page {{ max-width: 980px; margin: 0 auto; padding: 28px 24px 56px; }}
    .toolbar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 24px; padding-bottom: 12px; border-bottom: 1px solid var(--line); }}
    .path {{ color: var(--muted); overflow-wrap: anywhere; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ color: white; text-decoration: underline; }}
    h1, h2, h3 {{ line-height: 1.25; margin: 24px 0 10px; color: white; }}
    h1 {{ font-size: 28px; margin-top: 0; }}
    h2 {{ font-size: 20px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }}
    h3 {{ font-size: 16px; }}
    p {{ margin: 10px 0; }}
    ul {{ padding-left: 24px; }}
    li {{ margin: 4px 0; }}
    code {{ background: #2d2d30; color: #ce9178; padding: 1px 4px; border-radius: 3px; }}
    pre {{ overflow: auto; padding: 14px 16px; background: #181818; border: 1px solid var(--line); border-radius: 6px; }}
    pre code {{ background: transparent; color: #d4d4d4; padding: 0; }}
    hr {{ border: 0; border-top: 1px solid var(--line); margin: 24px 0; }}
  </style>
</head>
<body>
  <main class="page">
    <div class="toolbar"><div class="path">{html_lib.escape(rel_path)}</div><a href="{raw_href}">Raw Markdown</a></div>
    {body}
  </main>
</body>
</html>
"""


def markdown_to_html(markdown: str) -> str:
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    in_code = False
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{render_inline_markdown(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                blocks.append(f"<pre><code>{html_lib.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines.clear()
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        if stripped == "---":
            flush_paragraph()
            flush_list()
            blocks.append("<hr />")
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{render_inline_markdown(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        if bullet:
            flush_paragraph()
            list_items.append(render_inline_markdown(bullet.group(1)))
            continue
        paragraph.append(stripped)

    if in_code:
        blocks.append(f"<pre><code>{html_lib.escape(chr(10).join(code_lines))}</code></pre>")
    flush_paragraph()
    flush_list()
    return "\n".join(blocks)


def render_inline_markdown(text: str) -> str:
    escaped = html_lib.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def _url_escape(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _safe_path(root: Path, rel_path: str) -> Path:
    path = (root / rel_path).resolve()
    if root not in [path, *path.parents]:
        raise ValueError("Path escapes workspace")
    return path


def _is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in {"Dockerfile", "docker-compose.yml"}


def _iter_text_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if _is_text_file(path):
            paths.append(path)
    return sorted(paths)


def _language(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".py": "python",
        ".md": "markdown",
        ".toml": "toml",
        ".json": "json",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".css": "css",
        ".html": "html",
        ".sh": "bash",
    }.get(suffix, "text")


APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>bb-code workspace</title>
    <link rel="manifest" href="/manifest.json">
    <link rel="icon" href="/icon.png" type="image/png">
    <link rel="apple-touch-icon" href="/apple-touch-icon.png">
    <meta name="theme-color" content="#1e1e1e">
  <style>
    :root {
      color-scheme: dark;
      --bg: #1e1e1e;
      --panel: #252526;
      --panel-2: #2d2d30;
      --line: #3c3c3c;
      --text: #d4d4d4;
      --muted: #9da3aa;
      --blue: #007acc;
      --green: #89d185;
      --yellow: #cca700;
      --red: #f48771;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      overflow: hidden;
    }
    .shell { display: grid; grid-template-rows: 32px minmax(0, 1fr) 22px; height: 100vh; min-height: 0; overflow: hidden; }
    .titlebar {
      display: flex; align-items: center; gap: 12px; padding: 0 10px;
      background: #323233; border-bottom: 1px solid #191919; color: #c8c8c8;
    }
    .titlebar strong { color: white; font-weight: 600; }
    .workspace-form { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; }
    .workspace-form input {
      width: 100%; min-width: 180px; background: #1b1b1b; color: var(--text); border: 1px solid #4a4a4a;
      border-radius: 3px; padding: 4px 7px; font: inherit;
    }
    .workspace-form button {
      flex: 0 0 auto; background: #3a3d41; color: #ddd; border: 0; border-radius: 3px; padding: 5px 9px; cursor: pointer;
    }
    .workspace-form button:hover { background: #4a4d51; color: white; }
    .main {
      position: relative; display: grid; grid-template-columns: 48px minmax(220px, 280px) minmax(0, 1fr);
      padding-right: 390px; min-height: 0; height: 100%; overflow: hidden;
    }
    .activity { background: #333333; border-right: 1px solid #1b1b1b; display: flex; flex-direction: column; align-items: center; padding-top: 8px; gap: 8px; }
    .icon { width: 34px; height: 34px; border: 0; color: #ccc; background: transparent; border-radius: 4px; font-size: 17px; cursor: pointer; }
    .icon.active, .icon:hover { background: #424242; color: white; }
    .sidebar { background: var(--panel); border-right: 1px solid var(--line); min-width: 0; display: grid; grid-template-rows: auto auto 1fr; }
    .side-head { padding: 10px 12px 8px; text-transform: uppercase; font-size: 11px; color: #bbbbbb; letter-spacing: .4px; }
    .repo-list { border-bottom: 1px solid var(--line); padding: 0 8px 8px; color: var(--muted); }
    .repo-button {
      width: 100%; text-align: left; background: transparent; color: #cccccc; border: 0; border-radius: 3px;
      padding: 4px 6px; cursor: pointer; font: inherit;
    }
    .repo-button:hover, .repo-button.active { background: #37373d; color: white; }
    .tree { overflow: auto; padding: 4px 4px 20px; }
    .side-tool { padding: 8px; border-bottom: 1px solid var(--line); display: grid; gap: 8px; }
    .side-tool input {
      width: 100%; background: #1b1b1b; color: var(--text); border: 1px solid var(--line);
      border-radius: 3px; padding: 7px 8px; font: inherit;
    }
    .side-tool select {
      width: 100%; background: #1b1b1b; color: var(--text); border: 1px solid var(--line);
      border-radius: 3px; padding: 7px 8px; font: inherit;
    }
    .side-tool label { display: grid; gap: 4px; color: var(--muted); }
    .settings-status { color: var(--muted); white-space: pre-wrap; }
    .result { padding: 6px 8px; border-bottom: 1px solid #303030; cursor: pointer; }
    .result:hover { background: #37373d; }
    .result .path { color: #d7ba7d; }
    .result .line { color: var(--muted); font-size: 12px; }
    .report-links { display: grid; gap: 6px; padding: 8px; }
    .report-links a { color: #d7ba7d; text-decoration: none; }
    .report-links a:hover { color: white; text-decoration: underline; }
    .node { padding: 3px 8px; border-radius: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }
    .node:hover { background: #37373d; }
    .node.file { color: #cccccc; }
    .node.dir { color: #e7e7e7; font-weight: 500; }
    .editor { min-width: 0; min-height: 0; height: 100%; overflow: hidden; display: grid; grid-template-rows: 35px minmax(0, 1fr) 180px; }
    .tabs { display: flex; align-items: end; background: var(--panel-2); border-bottom: 1px solid var(--line); overflow: hidden; }
    .tab { padding: 9px 12px; background: var(--bg); border-right: 1px solid var(--line); max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    pre {
      margin: 0; padding: 14px 16px; overflow: auto; background: #1e1e1e; color: #dcdcaa;
      font: 12.5px/1.55 "SFMono-Regular", Consolas, Menlo, monospace;
    }
    #editor { min-height: 0; max-width: 100%; white-space: pre; tab-size: 4; }
    .tok-key { color: #569cd6; }
    .tok-str { color: #ce9178; }
    .tok-com { color: #6a9955; }
    .tok-num { color: #b5cea8; }
    .tok-fn { color: #dcdcaa; }
    .tok-op { color: #d4d4d4; }
    .bottom { border-top: 1px solid var(--line); background: #181818; display: grid; grid-template-rows: 32px 1fr; min-height: 0; }
    .panel-tabs { display: flex; border-bottom: 1px solid var(--line); }
    .panel-tabs button { background: transparent; color: var(--muted); border: 0; padding: 7px 12px; cursor: pointer; }
    .panel-tabs button.active { color: white; border-bottom: 1px solid var(--blue); }
    .panel-body { overflow: auto; padding: 10px 12px; white-space: pre-wrap; color: #cfcfcf; }
    .chat {
      position: absolute; top: 0; right: 0; bottom: 0; width: 390px; z-index: 2;
      background: var(--panel); border-left: 1px solid var(--line); display: grid;
      grid-template-rows: auto auto minmax(0, 1fr); min-width: 0; min-height: 0; overflow: hidden; padding-bottom: 142px;
    }
    .chat-head { padding: 10px 12px; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; align-items: center; }
    .modebar { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; padding: 8px; border-bottom: 1px solid var(--line); }
    .modebar button, .send, .small {
      background: #0e639c; color: white; border: 0; border-radius: 3px; padding: 7px 9px; cursor: pointer;
    }
    .modebar button { background: #3a3d41; color: #ddd; }
    .modebar button.active { background: var(--blue); color: white; }
    .messages { overflow-y: auto; min-height: 0; max-height: 100%; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
    .msg { border: 1px solid var(--line); border-radius: 6px; padding: 9px 10px; white-space: pre-wrap; }
    .user { background: #12324a; }
    .assistant { background: #262626; }
    .loading { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); }
    .spinner { width: 12px; height: 12px; border: 2px solid #555; border-top-color: var(--blue); border-radius: 50%; animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .edit-card { border: 1px solid #4a4a4a; border-radius: 6px; padding: 8px; background: #1f2a30; display: grid; gap: 6px; }
    .edit-card code { color: #d7ba7d; }
    .edit-card button { flex-shrink: 0; }
    .diff-view { max-height: 280px; overflow: auto; border: 1px solid var(--line); border-radius: 3px; margin: 2px 0; background: #181818; }
    .diff-line { padding: 1px 10px; white-space: pre; font: 11.5px/1.45 "SFMono-Regular", Consolas, Menlo, monospace; }
    .diff-add { background: #1a3326; color: #89d185; }
    .diff-remove { background: #3a1e1e; color: #f48771; }
    .diff-same { color: #555; }
    .diff-hunk { color: #7ab; background: #1a2530; font-size: 11px; font-style: italic; }
    .edit-status { font-size: 11px; font-weight: 600; }
    .edit-batch { display: flex; gap: 8px; align-items: center; padding: 4px 0 8px; }
    details > summary { cursor: pointer; color: var(--muted); font-size: 11px; padding: 2px 0; user-select: none; outline: none; list-style: none; }
    details > summary::before { content: "\25B8 "; }
    details[open] > summary::before { content: "\25BE "; }
    .composer { position: absolute; left: 0; right: 0; bottom: 0; height: 142px; z-index: 3; background: var(--panel); border-top: 1px solid var(--line); padding: 8px; display: grid; grid-template-rows: minmax(0, 1fr) auto; gap: 8px; min-height: 0; overflow: hidden; }
    textarea {
      width: 100%; height: 84px; min-height: 0; resize: none; border: 1px solid var(--line);
      background: #1b1b1b; color: var(--text); border-radius: 4px; padding: 8px; font: inherit;
    }
    .status { background: #007acc; color: white; padding: 2px 10px; display: flex; align-items: center; gap: 14px; }
    .muted { color: var(--muted); }
    @media (max-width: 980px) {
      .main { grid-template-columns: 48px minmax(180px, 240px) minmax(0, 1fr); padding-right: 340px; }
      .chat { width: 340px; }
    }
  </style>
</head>
<body>
    <div class="shell">
        <div class="titlebar">
            <strong>bb-code</strong>
            <span id="cwdDisplay" style="color:var(--muted);font-size:12px;padding-left:10px;max-width:28vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">Loading...</span>
            <select id="recentWorkspaces" aria-label="Recent workspaces" style="max-width:24vw;background:#1b1b1b;color:var(--text);border:1px solid #4a4a4a;border-radius:3px;padding:4px 7px;font:inherit;">
                <option value="">Recent workspaces</option>
            </select>
            <button id="openRecentBtn" type="button" style="background:#3a3d41;color:#ddd;border:0;border-radius:3px;padding:5px 9px;cursor:pointer;">Open</button>
            <button id="openEditorBtn" type="button" style="background:#3a3d41;color:#ddd;border:0;border-radius:3px;padding:5px 9px;cursor:pointer;">Open in Editor</button>
            <button id="installBtn" type="button" style="display:none;background:#007acc;color:white;border:0;border-radius:3px;padding:5px 10px;cursor:pointer;font-size:13px;">Install App</button>
            <form id="workspaceForm" class="workspace-form">
                <input id="workspacePath" aria-label="Workspace path" placeholder="Workspace path" />
                <button type="submit">Open</button>
            </form>
        </div>
        <div id="updateBanner" class="status" style="display:none;justify-content:space-between;position:sticky;top:0;z-index:4;background:#f48771;color:#1e1e1e;">
            <span>Update available</span>
            <button id="reloadAppBtn" type="button" style="background:#1e1e1e;color:#fff;border:0;border-radius:3px;padding:4px 10px;cursor:pointer;">Reload</button>
        </div>
        <div class="main">
      <div class="activity"><button class="icon active" title="Explorer" data-view="explorer">▤</button><button class="icon" title="Search" data-view="search">⌕</button><button class="icon" title="Source" data-view="source">⑂</button><button class="icon" title="Run" data-view="run">▷</button><button class="icon" title="Settings" data-view="settings">⚙</button></div>
      <aside class="sidebar">
        <div id="sideHead" class="side-head">Explorer</div>
        <div id="sideTool" class="repo-list"><div class="muted">Repos</div><div id="repos"></div></div>
        <div id="tree" class="tree"></div>
      </aside>
      <section class="editor">
        <div class="tabs"><div id="activeTab" class="tab">Welcome</div></div>
        <pre id="editor">// Open a file from Explorer, then ask bb-code for planning, debug help, or inline hints.</pre>
        <div class="bottom">
          <div class="panel-tabs"><button class="active" data-panel="diagnostics">Diagnostics</button><button data-panel="lint">Lint</button><button data-panel="context">Context</button><button data-panel="buildly">Buildly Way</button></div>
          <div id="panelBody" class="panel-body">Loading diagnostics...</div>
        </div>
      </section>
      <aside class="chat">
        <div class="chat-head"><strong>Agent Chat</strong><span class="muted">approval-gated</span></div>
        <div class="modebar"><button class="active" data-mode="agent">Agent</button><button data-mode="plan">Plan</button><button data-mode="debug">Debug</button><button data-mode="hints">Hints</button></div>
        <div id="messages" class="messages"><div class="msg assistant">I can inspect multiple files, explain the Buildly way, plan changes, debug errors, and suggest inline hints. I will not silently edit files.</div></div>
        <div class="composer"><textarea id="prompt" placeholder="Ask about this workspace..."></textarea><button id="send" class="send">Send</button></div>
      </aside>
    </div>
    <div class="status"><span id="status">Ready</span><span>Local web UI</span><span>No silent edits</span></div>
  </div>
  <script>
    const state = { root: "", baseRoot: "", activeRepo: ".", openFiles: [], activeFile: null, activeLanguage: "text", mode: "agent", panels: {}, tree: [], repos: [], lint: null, settings: null };
    const $ = (id) => document.getElementById(id);

    async function api(path, options) {
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.json();
    }

    function renderTree(nodes, depth = 0) {
      return nodes.map(node => {
        const pad = `padding-left:${8 + depth * 12}px`;
        const label = node.type === "dir" ? `${node.collapsed ? "▸" : "▾"} ${node.name}` : `  ${node.name}`;
        const row = `<div class="node ${node.type}" style="${pad}" data-path="${node.path}" data-type="${node.type}">${label}</div>`;
        return row + (node.children && !node.collapsed ? renderTree(node.children, depth + 1) : "");
      }).join("");
    }

        function workspaceRecentKey() { return "bb-code:recent-workspaces"; }

        function loadRecentWorkspaces() {
            try {
                const value = JSON.parse(localStorage.getItem(workspaceRecentKey()) || "[]");
                return Array.isArray(value) ? value.filter(item => typeof item === "string" && item.trim()) : [];
            } catch {
                return [];
            }
        }

        function saveRecentWorkspaces(workspaces) {
            localStorage.setItem(workspaceRecentKey(), JSON.stringify(workspaces.slice(0, 8)));
        }

        function rememberWorkspace(path) {
            if (!path) return;
            const recent = loadRecentWorkspaces().filter(item => item !== path);
            recent.unshift(path);
            saveRecentWorkspaces(recent);
            renderRecentWorkspaces();
        }

        function renderRecentWorkspaces() {
            const select = $("recentWorkspaces");
            if (!select) return;
            const recent = loadRecentWorkspaces();
            select.innerHTML = `<option value="">Recent workspaces</option>${recent.map(path => `<option value="${escapeHtml(path)}">${escapeHtml(path)}</option>`).join("")}`;
        }

        async function openCurrentFileInEditor() {
            if (!state.activeFile) {
                setStatus("No file open");
                return;
            }
            const absolutePath = `${state.root.replace(/\/$/, "")}/${state.activeFile}`;
            window.open(encodeURI(`vscode://file${absolutePath}`), "_blank", "noopener");
            setStatus(`Opening ${state.activeFile} in editor`);
        }

        async function loadWorkspace(data) {
            data = data || await api("/api/workspace");
            state.root = data.root;
            state.baseRoot = data.baseRoot || data.root;
            state.activeRepo = data.activeRepo || ".";
            state.tree = data.tree;
            state.repos = data.repos;
            state.openFiles = [];
            state.activeFile = null;
            state.activeLanguage = "text";
            state.panels = {};
            state.lint = null;
            $("workspacePath").value = data.root;
            $("cwdDisplay").textContent = data.root;
            rememberWorkspace(data.root);
            $("activeTab").textContent = "Welcome";
            $("editor").textContent = "// Open a file from Explorer, then ask bb-code for planning, debug help, or inline hints.";
            showExplorer();
            await loadPanel("diagnostics");
            setStatus(`Workspace ${data.name}`);
        }

        let deferredPrompt = null;
        let waitingServiceWorker = null;
        const installBtn = $("installBtn");
        const updateBanner = $("updateBanner");
        const reloadAppBtn = $("reloadAppBtn");
        const openEditorBtn = $("openEditorBtn");
        const recentWorkspaces = $("recentWorkspaces");
        const openRecentBtn = $("openRecentBtn");

        function showUpdateAvailable() {
            updateBanner.style.display = "flex";
        }

        if ("serviceWorker" in navigator) {
            window.addEventListener("load", async () => {
                try {
                    const registration = await navigator.serviceWorker.register("/service-worker.js");
                    registration.addEventListener("updatefound", () => {
                        const worker = registration.installing;
                        if (!worker) return;
                        waitingServiceWorker = worker;
                        worker.addEventListener("statechange", () => {
                            if (worker.state === "installed" && navigator.serviceWorker.controller) {
                                showUpdateAvailable();
                            }
                        });
                    });
                } catch (error) {
                    console.warn("Service worker registration failed", error);
                }
            });
            navigator.serviceWorker.addEventListener("controllerchange", () => window.location.reload());
        }

        window.addEventListener("beforeinstallprompt", event => {
            event.preventDefault();
            deferredPrompt = event;
            installBtn.style.display = "inline-block";
        });

        window.addEventListener("appinstalled", () => {
            deferredPrompt = null;
            installBtn.style.display = "none";
        });

        installBtn.addEventListener("click", async () => {
            if (!deferredPrompt) return;
            installBtn.disabled = true;
            deferredPrompt.prompt();
            const choice = await deferredPrompt.userChoice;
            if (choice.outcome !== "accepted") {
                installBtn.disabled = false;
            } else {
                installBtn.style.display = "none";
            }
            deferredPrompt = null;
        });

        reloadAppBtn.addEventListener("click", () => {
            if (waitingServiceWorker) {
                waitingServiceWorker.postMessage({ type: "SKIP_WAITING" });
            } else {
                window.location.reload();
            }
        });
        openEditorBtn.addEventListener("click", openCurrentFileInEditor);
        openRecentBtn.addEventListener("click", () => {
            if (recentWorkspaces.value) switchWorkspacePath(recentWorkspaces.value);
        });
        recentWorkspaces.addEventListener("change", () => {
            if (recentWorkspaces.value) switchWorkspacePath(recentWorkspaces.value);
        });

        async function switchWorkspacePath(path) {
            if (!path) return;
            setStatus(`Opening workspace ${path}`);
            try {
                const data = await api("/api/workspace", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ path })
                });
                await loadWorkspace(data);
                setStatus(`Workspace ${data.name}`);
            } catch (error) {
                setStatus("Workspace error");
                addMessage("assistant", `Could not open workspace: ${error}`);
            }
        }

    async function switchRepo(path) {
      setStatus(`Opening repo ${path}`);
      try {
        const data = await api("/api/repo", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path })
        });
        await loadWorkspace(data);
        setStatus(`Repo ${data.activeRepo}`);
      } catch (error) {
        setStatus("Repo error");
        addMessage("assistant", `Could not open repo: ${error}`);
      }
    }

    async function switchWorkspace(event) {
      event.preventDefault();
      const path = $("workspacePath").value.trim();
      if (!path) return;
      setStatus("Opening workspace...");
      try {
        const data = await api("/api/workspace", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path })
        });
        await loadWorkspace(data);
        addMessage("assistant", `Workspace changed to ${data.root}`);
      } catch (error) {
        setStatus("Workspace error");
        addMessage("assistant", `Could not open workspace: ${error}`);
      }
    }

    function bindFileClicks(container) {
      container.onclick = async (event) => {
        const row = event.target.closest("[data-path]");
        if (!row) return;
        if (row.dataset.type === "dir") {
          toggleDirectory(row.dataset.path);
          return;
        }
        await openFile(row.dataset.path);
      };
    }

    function toggleDirectory(path) {
      const node = findTreeNode(state.tree, path);
      if (!node || node.type !== "dir") return;
      node.collapsed = !node.collapsed;
      showExplorer();
      setStatus(`${node.collapsed ? "Collapsed" : "Expanded"} ${node.path}`);
    }

    function findTreeNode(nodes, path) {
      for (const node of nodes) {
        if (node.path === path) return node;
        if (node.children) {
          const found = findTreeNode(node.children, path);
          if (found) return found;
        }
      }
      return null;
    }

    function setSideView(name) {
      document.querySelectorAll(".activity .icon").forEach(btn => btn.classList.toggle("active", btn.dataset.view === name));
      if (name === "explorer") showExplorer();
      if (name === "search") showSearch();
      if (name === "source") showSource();
      if (name === "run") showRun();
      if (name === "settings") showSettings();
    }

    function showExplorer() {
      $("sideHead").textContent = "Explorer";
      $("sideTool").className = "repo-list";
      $("sideTool").innerHTML = `<div class="muted">Repos</div>${state.repos.map(repo => `<button class="repo-button ${repo.path === state.activeRepo ? "active" : ""}" data-repo-path="${escapeHtml(repo.path)}">${escapeHtml(repo.name)}</button>`).join("")}`;
      $("sideTool").onclick = async (event) => {
        const button = event.target.closest("[data-repo-path]");
        if (!button) return;
        await switchRepo(button.dataset.repoPath);
      };
      $("tree").innerHTML = renderTree(state.tree);
      bindFileClicks($("tree"));
    }

    function showSearch() {
      $("sideHead").textContent = "Search";
      $("sideTool").className = "side-tool";
      $("sideTool").innerHTML = `<input id="searchBox" placeholder="Search files" /><button id="searchButton" class="small">Search</button>`;
      $("tree").innerHTML = `<div class="muted" style="padding:8px">Enter a term to search filenames and text.</div>`;
      $("searchButton").addEventListener("click", runSearch);
      $("searchBox").addEventListener("keydown", event => { if (event.key === "Enter") runSearch(); });
      $("searchBox").focus();
    }

    async function runSearch() {
      const query = $("searchBox").value.trim();
      setStatus(`Searching ${query}`);
      const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
      $("tree").innerHTML = data.results.length
        ? data.results.map(result => `<div class="result" data-path="${result.path}"><div class="path">${result.path}</div><div class="line">${result.line ? `Line ${result.line}: ` : ""}${escapeHtml(result.text)}</div></div>`).join("")
        : `<div class="muted" style="padding:8px">No results.</div>`;
      bindFileClicks($("tree"));
      setStatus("Ready");
    }

    async function showSource() {
      $("sideHead").textContent = "Source Control";
      $("sideTool").className = "side-tool";
      $("sideTool").innerHTML = `<button id="refreshGit" class="small">Refresh</button>`;
      $("tree").innerHTML = `<div class="muted" style="padding:8px">Loading git status...</div>`;
      $("refreshGit").addEventListener("click", showSource);
      const data = await api("/api/git");
      $("tree").innerHTML = `<pre style="background:transparent;padding:8px;color:#d4d4d4">${escapeHtml(data.output)}</pre>`;
    }

    function showRun() {
      $("sideHead").textContent = "Run and Debug";
      $("sideTool").className = "side-tool";
      $("sideTool").innerHTML = `<button id="runDiagnostics" class="small">Run Diagnostics</button><button id="runTests" class="small">Run Tests</button><button id="runCloudAudit" class="small">Cloud Audit</button><button id="runKubectlDiagnostics" class="small">Kubernetes Diagnostics</button><button id="runPlatformReport" class="small">Platform Report</button><button id="showK8sMonitor" class="small">K8s Monitor</button>`;
      $("tree").innerHTML = `<div class="muted" style="padding:8px">Run diagnostics, tests, cloud audits, read-only Kubernetes checks, saved platform reports, or open installed integrations. No arbitrary commands are executed.</div>`;
      $("runDiagnostics").addEventListener("click", async () => {
        await loadPanel("diagnostics");
        $("tree").innerHTML = `<div class="muted" style="padding:8px">Diagnostics refreshed in the bottom panel.</div>`;
      });
      $("runTests").addEventListener("click", runTests);
      $("runCloudAudit").addEventListener("click", runCloudAudit);
      $("runKubectlDiagnostics").addEventListener("click", runKubectlDiagnostics);
      $("runPlatformReport").addEventListener("click", runPlatformReport);
      $("showK8sMonitor").addEventListener("click", showK8sMonitor);
    }

    async function runTests() {
      setStatus("Running tests...");
      $("tree").innerHTML = `<div class="muted" style="padding:8px">Running pytest...</div>`;
      const data = await api("/api/run-tests", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      $("tree").innerHTML = `<pre style="background:transparent;padding:8px;color:#d4d4d4">${escapeHtml(data.output || data.status)}</pre>`;
      setStatus(`Tests ${data.status}`);
    }

    async function runCloudAudit() {
      setStatus("Running cloud audit...");
      $("tree").innerHTML = `<div class="muted" style="padding:8px">Auditing top-level folders as services...</div>`;
      try {
        const data = await api("/api/cloud-audit", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        const inventory = data.inventory || {};
        const summary = `Services found: ${(inventory.services || []).length}\nDuplicate models: ${Object.keys(inventory.duplicateModels || {}).length}\nShared dependencies: ${Object.keys(inventory.sharedDependencies || {}).length}`;
        $("tree").innerHTML = `<pre style="background:transparent;padding:8px;color:#d4d4d4">${escapeHtml(summary)}</pre>`;
        $("panelBody").textContent = `${data.report}\n\n--- Inventory ---\n${JSON.stringify(inventory, null, 2)}`;
        setStatus(`Cloud audit ${data.status}`);
      } catch (error) {
        $("tree").innerHTML = `<div class="muted" style="padding:8px">Cloud audit failed.</div>`;
        $("panelBody").textContent = `Cloud audit failed: ${error}`;
        setStatus("Cloud audit error");
      }
    }

    async function runKubectlDiagnostics() {
      setStatus("Running Kubernetes diagnostics...");
      $("tree").innerHTML = `<div class="muted" style="padding:8px">Reading kubectl contexts and cluster resources...</div>`;
      try {
        const data = await api("/api/kubectl-diagnostics", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        const summary = [
          `kubectl: ${data.tools && data.tools.kubectl ? data.tools.kubectl : "not found"}`,
          `gcloud: ${data.tools && data.tools.gcloud ? data.tools.gcloud : "not found"}`,
          `current context: ${data.currentContext || "none"}`,
          `contexts: ${(data.contexts || []).length}`,
          `namespaces: ${(data.namespaces || []).length}`,
          `deployments: ${(data.workloads || []).length}`,
          `pods: ${(data.pods || []).length}`,
          `services: ${(data.services || []).length}`,
          `repo matches: ${(data.repoMatches || []).length}`,
          `github action deploy signals: ${(data.githubActions || []).length}`
        ].join("\n");
        $("tree").innerHTML = `<pre style="background:transparent;padding:8px;color:#d4d4d4">${escapeHtml(summary)}</pre>`;
        $("panelBody").textContent = renderKubectlReport(data);
        setStatus("Kubernetes diagnostics ready");
      } catch (error) {
        $("tree").innerHTML = `<div class="muted" style="padding:8px">Kubernetes diagnostics failed.</div>`;
        $("panelBody").textContent = `Kubernetes diagnostics failed: ${error}`;
        setStatus("Kubernetes diagnostics error");
      }
    }

    function renderKubectlReport(data) {
      return [
        "# Kubernetes Diagnostics",
        "",
        "## Tools",
        `kubectl: ${data.tools && data.tools.kubectl ? data.tools.kubectl : "not found"}`,
        `gcloud: ${data.tools && data.tools.gcloud ? data.tools.gcloud : "not found"}`,
        "",
        "## Contexts",
        `Current: ${data.currentContext || "none"}`,
        ...(data.contexts || []).map(context => `- ${context}`),
        "",
        "## Workloads",
        ...(data.workloads || []).map(item => `- ${item.namespace}/${item.name} replicas=${item.replicas} available=${item.available} images=${(item.images || []).join(", ")}`),
        "",
        "## Pods",
        ...(data.pods || []).slice(0, 80).map(item => `- ${item.namespace}/${item.name} phase=${item.phase} restarts=${item.restarts} images=${(item.images || []).join(", ")}`),
        "",
        "## Services",
        ...(data.services || []).map(item => `- ${item.namespace}/${item.name} type=${item.type} selector=${JSON.stringify(item.selector || {})}`),
        "",
        "## Repo Matches",
        ...(data.repoMatches || []).map(item => `- ${(item.workload || item.pod)} -> ${(item.repos || []).join(", ")} images=${(item.images || []).join(", ")}`),
        "",
        "## GitHub Actions Deployment Signals",
        ...(data.githubActions || []).map(item => `- ${item.path}: ${(item.signals || []).join(", ")}\n${item.snippet || ""}`),
        "",
        "## Notes",
        ...((data.notes || []).length ? data.notes.map(note => `- ${note}`) : ["- No notes."])
      ].join("\n");
    }

    async function runPlatformReport() {
      setStatus("Building platform report...");
      $("tree").innerHTML = `<div class="muted" style="padding:8px">Scanning services, models, deployment workflows, and Kubernetes diagnostics...</div>`;
      try {
        const data = await api("/api/platform-report", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        const summary = data.summary || {};
        $("tree").innerHTML = `<pre style="background:transparent;padding:8px;color:#d4d4d4">${escapeHtml([
          `status: ${data.status}`,
          `services: ${summary.services || 0}`,
          `databases: ${summary.databases || 0}`,
          `workloads: ${summary.kubernetesWorkloads || 0}`,
          `pods: ${summary.kubernetesPods || 0}`,
          `findings: ${summary.findings || 0}`,
          "",
          "Reports:",
          ...(data.reports || []).map(report => `- ${report.path}`)
        ].join("\n"))}</pre>`;
        $("tree").innerHTML += renderReportLinks(data.reports || []);
        $("panelBody").innerHTML = renderPlatformReport(data);
        setStatus("Platform report ready");
      } catch (error) {
        $("tree").innerHTML = `<div class="muted" style="padding:8px">Platform report failed.</div>`;
        $("panelBody").textContent = `Platform report failed: ${error}`;
        setStatus("Platform report error");
      }
    }

    function renderPlatformReport(data) {
      const reports = data.reports || [];
      if (!reports.length) return "No reports were generated.";
      return [
        renderReportLinks(reports),
        `<pre style="background:transparent;padding:8px;color:#d4d4d4">${escapeHtml(reports.map(report => `# ${report.path}\n\n${report.content || ""}`).join("\n\n---\n\n"))}</pre>`
      ].join("");
    }

    function renderReportLinks(reports) {
      if (!reports.length) return "";
      return `<div class="report-links">${reports.map(report => {
        const href = `/reports?path=${encodeURIComponent(report.path)}`;
        return `<a href="${href}" target="_blank" rel="noopener">${escapeHtml(report.name || report.path)}</a>`;
      }).join("")}</div>`;
    }

    async function showK8sMonitor() {
      setStatus("Checking k8s-monitor integration...");
      $("tree").innerHTML = `<div class="muted" style="padding:8px">Checking integrations/k8s-monitor...</div>`;
      try {
        const data = await api("/api/integrations/k8s-monitor");
        $("tree").innerHTML = renderK8sMonitorSummary(data);
        $("panelBody").innerHTML = renderK8sMonitorPanel(data);
        setStatus(data.installed ? "K8s monitor installed" : "K8s monitor not installed");
      } catch (error) {
        $("tree").innerHTML = `<div class="muted" style="padding:8px">K8s monitor check failed.</div>`;
        $("panelBody").textContent = `K8s monitor check failed: ${error}`;
        setStatus("K8s monitor error");
      }
    }

    function renderK8sMonitorSummary(data) {
      const lines = [
        data.name || "ForgeOps / k8s-monitor",
        `installed: ${data.installed ? "yes" : "no"}`,
        `path: ${data.path || "integrations/k8s-monitor"}`,
        data.commit ? `commit: ${data.commit}` : "",
        data.branch ? `branch: ${data.branch}` : ""
      ].filter(Boolean).join("\n");
      const links = data.installed
        ? `<div class="report-links"><a href="${escapeHtml(data.dashboardUrl)}" target="_blank" rel="noopener">Open ForgeOps dashboard</a><a href="${escapeHtml(data.docsUrl)}" target="_blank" rel="noopener">ForgeOps docs</a></div>`
        : "";
      return `<pre style="background:transparent;padding:8px;color:#d4d4d4">${escapeHtml(lines)}</pre>${links}`;
    }

    function renderK8sMonitorPanel(data) {
      const startCommands = (data.startCommands || []).map(command => `- ${command}`).join("\n");
      const notes = (data.notes || []).map(note => `- ${note}`).join("\n");
      const markdown = [
        "# ForgeOps / k8s-monitor Integration",
        "",
        `Installed: ${data.installed ? "yes" : "no"}`,
        `Path: ${data.path || "integrations/k8s-monitor"}`,
        `Repository: ${data.repository || ""}`,
        data.commit ? `Commit: ${data.commit}` : "",
        "",
        "## Links",
        data.installed ? `- Dashboard: ${data.dashboardUrl}` : "- Dashboard link appears after the submodule is installed.",
        `- Docs: ${data.docsUrl || ""}`,
        "",
        "## Install",
        `- ${data.installCommand || "git submodule update --init --recursive integrations/k8s-monitor"}`,
        "",
        "## Start",
        startCommands || "- Start command unavailable.",
        "",
        "## Notes",
        notes || "- No notes."
      ].filter(line => line !== "").join("\n");
      return `<pre style="background:transparent;padding:8px;color:#d4d4d4">${escapeHtml(markdown)}</pre>`;
    }

    async function showSettings() {
      $("sideHead").textContent = "Settings";
      $("sideTool").className = "side-tool";
      $("tree").innerHTML = `<div class="muted" style="padding:8px">Loading model settings...</div>`;
      const data = await api("/api/settings");
      state.settings = data.settings;
      renderSettingsForm(data);
      await refreshModels(false);
    }

    function renderSettingsForm(data, modelData = null) {
      const settings = data.settings || {};
      const models = modelData && modelData.status === "pass" ? modelData.models || [] : [];
      const modelControl = models.length
        ? `<select id="settingsModel">${models.map(model => `<option value="${escapeHtml(model)}" ${model === settings.model ? "selected" : ""}>${escapeHtml(model)}</option>`).join("")}</select>`
        : `<input id="settingsModel" value="${escapeHtml(settings.model || "")}" />`;
      $("sideTool").innerHTML = `
        <label>Provider
          <select id="settingsProvider">
            <option value="ollama" ${settings.provider === "ollama" ? "selected" : ""}>Ollama</option>
            <option value="openai-compatible" ${settings.provider === "openai-compatible" ? "selected" : ""}>OpenAI-compatible API</option>
          </select>
        </label>
        <label>Model${modelControl}</label>
        <label>Ollama URL<input id="settingsOllamaUrl" value="${escapeHtml(settings.ollama_url || "")}" /></label>
        <label>Remote API Base<input id="settingsApiBaseUrl" placeholder="https://api.openai.com/v1" value="${escapeHtml(settings.api_base_url || "")}" /></label>
        <label>API Key Env Var<input id="settingsApiKeyEnvVar" value="${escapeHtml(settings.api_key_env_var || "OPENAI_API_KEY")}" /></label>
        <label>Timeout Seconds<input id="settingsTimeout" type="number" min="1" value="${escapeHtml(settings.timeout_seconds || 120)}" /></label>
        <button id="refreshModels" class="small">Refresh Models</button>
        <button id="saveSettings" class="small">Save Settings</button>
        <button id="testSettings" class="small">Test Connection</button>
      `;
      const modelStatus = modelData ? `\nModel discovery: ${modelData.status}\n${modelData.output || ""}` : "";
      $("tree").innerHTML = `<pre class="settings-status" style="background:transparent;padding:8px;color:#d4d4d4">Saved at ${escapeHtml(data.path)}\nAPI key env available: ${data.apiKeyAvailable ? "yes" : "no"}${escapeHtml(modelStatus)}</pre>`;
      $("settingsProvider").addEventListener("change", () => refreshModels(true));
      $("settingsOllamaUrl").addEventListener("change", () => refreshModels(true));
      $("settingsApiBaseUrl").addEventListener("change", () => refreshModels(true));
      $("settingsApiKeyEnvVar").addEventListener("change", () => refreshModels(true));
      $("refreshModels").addEventListener("click", () => refreshModels(true));
      $("saveSettings").addEventListener("click", saveSettings);
      $("testSettings").addEventListener("click", testSettings);
    }

    function collectSettingsForm() {
      return {
        provider: $("settingsProvider").value,
        model: $("settingsModel").value.trim(),
        ollama_url: $("settingsOllamaUrl").value.trim(),
        api_base_url: $("settingsApiBaseUrl").value.trim(),
        api_key_env_var: $("settingsApiKeyEnvVar").value.trim(),
        timeout_seconds: Number($("settingsTimeout").value || 120)
      };
    }

    async function saveSettings() {
      setStatus("Saving settings...");
      const data = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collectSettingsForm())
      });
      state.settings = data.settings;
      renderSettingsForm(data);
      setStatus("Settings saved");
      await refreshModels(false);
    }

    async function testSettings() {
      setStatus("Testing settings...");
      const data = await api("/api/settings/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collectSettingsForm())
      });
      $("tree").innerHTML = `<pre style="background:transparent;padding:8px;color:#d4d4d4">${escapeHtml(data.status)}\n${escapeHtml(data.output)}</pre>`;
      setStatus(`Settings ${data.status}`);
    }

    async function refreshModels(showLoading) {
      if (showLoading) setStatus("Loading models...");
      const payload = collectSettingsForm();
      const modelData = await api("/api/settings/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      state.settings = payload;
      renderSettingsForm({ settings: payload, path: ".bb/settings.json", apiKeyAvailable: modelData.status === "pass" || payload.provider === "ollama" }, modelData);
      setStatus(modelData.status === "pass" ? "Models loaded" : "Model discovery failed");
    }

    async function openFile(path) {
      setStatus(`Opening ${path}`);
      const data = await api(`/api/file?path=${encodeURIComponent(path)}`);
      state.activeFile = data.path;
      state.activeLanguage = data.language || "text";
      if (!state.openFiles.includes(data.path)) state.openFiles.push(data.path);
      $("activeTab").textContent = data.path;
      $("editor").innerHTML = highlightCode(data.content, state.activeLanguage);
      await loadLint(data.path);
      setStatus(`Opened ${data.path}`);
    }

    async function loadPanel(name) {
      document.querySelectorAll(".panel-tabs button").forEach(btn => btn.classList.toggle("active", btn.dataset.panel === name));
      if (name === "diagnostics") state.panels[name] = (await api("/api/diagnostics")).markdown;
      if (name === "lint") state.panels[name] = renderLintPanel();
      if (name === "context") state.panels[name] = (await api("/api/context")).markdown;
      if (name === "buildly") state.panels[name] = "Buildly way:\n\n- Python-first implementation\n- Docker-first local setup\n- No Makefiles\n- Use ops/startup.sh when useful\n- Keep /devdocs current\n- Minimal but useful tests\n- Cloud native means explicit services, env vars, health checks, and deployment boundaries\n- AI assists planning/debugging/hints, with user approval for changes";
      $("panelBody").textContent = state.panels[name];
    }

    async function loadLint(path) {
      try {
        state.lint = await api(`/api/lint?path=${encodeURIComponent(path)}`);
      } catch (error) {
        state.lint = { diagnostics: [{ severity: "error", line: 1, message: String(error) }] };
      }
      const lintButton = document.querySelector('.panel-tabs button[data-panel="lint"]');
      if (lintButton && lintButton.classList.contains("active")) await loadPanel("lint");
    }

    function renderLintPanel() {
      if (!state.activeFile) return "Open a Python or JavaScript file to see lint and syntax diagnostics.";
      if (!state.lint || !state.lint.diagnostics) return "No lint data.";
      if (!state.lint.diagnostics.length) return `${state.activeFile}\n\nNo lint issues found.`;
      return `${state.activeFile}\n\n` + state.lint.diagnostics.map(item => `${item.severity.toUpperCase()} line ${item.line}: ${item.message}`).join("\n");
    }

    function addMessage(role, text) {
      const div = document.createElement("div");
      div.className = `msg ${role}`;
      div.textContent = text;
      $("messages").appendChild(div);
      $("messages").scrollTop = $("messages").scrollHeight;
    }

    async function sendMessage() {
      const message = $("prompt").value.trim();
      if (!message) return;
      $("prompt").value = "";
      addMessage("user", message);
      const loading = addLoadingMessage();
      setStatus("Thinking...");
      try {
        const response = await api("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message, mode: state.mode, openFiles: state.openFiles })
        });
        loading.remove();
        addMessage("assistant", response.content);
        if (response.edits && response.edits.length) addEditSuggestions(response.edits);
        setStatus("Ready");
      } catch (error) {
        loading.remove();
        addMessage("assistant", `Request failed: ${error}`);
        setStatus("Error");
      }
    }

    function addLoadingMessage() {
      const div = document.createElement("div");
      div.className = "msg assistant loading";
      div.innerHTML = `<span class="spinner"></span><span>Thinking with ${state.mode} mode...</span>`;
      $("messages").appendChild(div);
      $("messages").scrollTop = $("messages").scrollHeight;
      return div;
    }

    function addEditSuggestions(edits) {
      if (!edits || !edits.length) return;
      const n = edits.length;
      const msg = document.createElement("div");
      msg.className = "msg assistant";
      msg.innerHTML = `<div style="margin-bottom:8px">I have identified <strong>${n}</strong> code change${n > 1 ? "s" : ""} to implement. Review before applying?</div><div class="edit-actions" style="display:flex;gap:8px"><button class="small review-btn">Review Changes</button><button class="small decline-btn" style="background:#3a3d41;color:#aaa">No Thanks</button></div>`;
      msg.querySelector(".review-btn").addEventListener("click", () => { msg.remove(); showEditDiffs(edits); });
      msg.querySelector(".decline-btn").addEventListener("click", () => {
        msg.querySelector(".edit-actions").remove();
        msg.querySelector("div").textContent = `Declined ${n} suggested change${n > 1 ? "s" : ""}.`;
      });
      $("messages").appendChild(msg);
      $("messages").scrollTop = $("messages").scrollHeight;
    }

    function showEditDiffs(edits) {
      const container = document.createElement("div");
      container.className = "msg assistant";
      const pending = new Set(edits.map((_, i) => i));
      const batchBar = document.createElement("div");
      batchBar.className = "edit-batch";
      batchBar.innerHTML = `<span style="color:var(--muted)">${edits.length} change${edits.length > 1 ? "s" : ""}</span><button class="small approve-all-btn">Approve All</button><button class="small skip-all-btn" style="background:#3a3d41;color:#aaa">Skip All</button>`;
      container.appendChild(batchBar);
      const cards = edits.map((edit, index) => {
        const diffSrc = edit.find !== undefined
          ? { old: edit.find, upd: edit.replace }
          : { old: edit.original || "", upd: edit.content || "" };
        const diffLines = collapseContext(computeLineDiff(diffSrc.old, diffSrc.upd));
        const card = document.createElement("div");
        card.className = "edit-card";
        card.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;min-width:0"><code style="color:#d7ba7d;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">${escapeHtml(edit.path)}</code><span class="edit-status" style="color:var(--muted);flex-shrink:0">pending</span></div><div style="font-size:11px;color:var(--muted)">${escapeHtml(edit.summary)}</div><details><summary>Show diff</summary><div class="diff-view">${renderDiff(diffLines)}</div></details><div style="display:flex;gap:6px"><button class="small approve-btn">Approve</button><button class="small skip-btn" style="background:#3a3d41;color:#aaa">Skip</button></div>`;
        card.querySelector(".approve-btn").addEventListener("click", () => applyEditChange(edit, card, pending, index));
        card.querySelector(".skip-btn").addEventListener("click", () => skipEditChange(card, pending, index));
        container.appendChild(card);
        return card;
      });
      batchBar.querySelector(".approve-all-btn").addEventListener("click", async () => {
        batchBar.querySelector(".approve-all-btn").disabled = true;
        batchBar.querySelector(".skip-all-btn").disabled = true;
        for (let i = 0; i < edits.length; i++) {
          if (pending.has(i)) await applyEditChange(edits[i], cards[i], pending, i);
        }
      });
      batchBar.querySelector(".skip-all-btn").addEventListener("click", () => {
        [...pending].forEach(i => skipEditChange(cards[i], pending, i));
        batchBar.querySelector(".approve-all-btn").disabled = true;
        batchBar.querySelector(".skip-all-btn").disabled = true;
      });
      $("messages").appendChild(container);
      $("messages").scrollTop = $("messages").scrollHeight;
    }

    async function applyEditChange(edit, card, pending, index) {
      if (!pending.has(index)) return;
      const statusEl = card.querySelector(".edit-status");
      statusEl.textContent = "applying\u2026";
      statusEl.style.color = "var(--yellow)";
      try {
        await api("/api/apply-edit", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: edit.path, content: edit.content })
        });
        pending.delete(index);
        card.style.opacity = "0.75";
        statusEl.textContent = "\u2713 applied";
        statusEl.style.color = "var(--green)";
        card.querySelector(".approve-btn").disabled = true;
        card.querySelector(".skip-btn").disabled = true;
        await openFile(edit.path);
        setStatus(`Applied ${edit.path}`);
      } catch (err) {
        statusEl.textContent = "error";
        statusEl.style.color = "var(--red)";
        setStatus(`Failed to apply ${edit.path}`);
      }
    }

    function skipEditChange(card, pending, index) {
      if (!pending.has(index)) return;
      pending.delete(index);
      card.style.opacity = "0.5";
      const statusEl = card.querySelector(".edit-status");
      statusEl.textContent = "\u2717 skipped";
      statusEl.style.color = "var(--muted)";
      card.querySelector(".approve-btn").disabled = true;
      card.querySelector(".skip-btn").disabled = true;
    }

    function computeLineDiff(oldText, newText) {
      const a = (oldText || "").split("\n");
      const b = (newText || "").split("\n");
      if (a.length > 400 || b.length > 400) {
        return [
          { type: "remove", text: `(${a.length} lines removed)` },
          { type: "add", text: `(${b.length} lines added \u2014 apply to see full result)` }
        ];
      }
      const m = a.length, n = b.length;
      const dp = Array.from({ length: m + 1 }, () => new Int32Array(n + 1));
      for (let i = 1; i <= m; i++)
        for (let j = 1; j <= n; j++)
          dp[i][j] = a[i-1] === b[j-1] ? dp[i-1][j-1] + 1 : Math.max(dp[i-1][j], dp[i][j-1]);
      const diff = [];
      let i = m, j = n;
      while (i > 0 || j > 0) {
        if (i > 0 && j > 0 && a[i-1] === b[j-1]) { diff.unshift({ type: "same", text: a[i-1] }); i--; j--; }
        else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) { diff.unshift({ type: "add", text: b[j-1] }); j--; }
        else { diff.unshift({ type: "remove", text: a[i-1] }); i--; }
      }
      return diff;
    }

    function collapseContext(diffLines, ctx = 3) {
      const changed = diffLines.reduce((acc, l, i) => { if (l.type !== "same") acc.push(i); return acc; }, []);
      if (!changed.length) return [{ type: "same", text: "(no changes detected)" }];
      const show = new Set();
      changed.forEach(i => { for (let c = Math.max(0, i - ctx); c <= Math.min(diffLines.length - 1, i + ctx); c++) show.add(c); });
      const result = [];
      let last = -1;
      diffLines.forEach((line, i) => {
        if (!show.has(i)) return;
        if (last >= 0 && i > last + 1) result.push({ type: "hunk", text: "@@ \u2026 @@" });
        result.push(line);
        last = i;
      });
      return result;
    }

    function renderDiff(diffLines) {
      return diffLines.map(({ type, text }) => {
        const prefix = type === "add" ? "+" : type === "remove" ? "-" : " ";
        const cls = `diff-line ${type === "add" ? "diff-add" : type === "remove" ? "diff-remove" : type === "hunk" ? "diff-hunk" : "diff-same"}`;
        return `<div class="${cls}">${escapeHtml(prefix + " " + text)}</div>`;
      }).join("");
    }

    function setStatus(text) { $("status").textContent = text; }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[char]));
    }

    function highlightCode(value, language) {
      if (language === "python") return highlightWithRules(value, pythonRules());
      if (["javascript", "typescript"].includes(language)) return highlightWithRules(value, javascriptRules());
      if (language === "json") return highlightWithRules(value, jsonRules());
      return escapeHtml(value);
    }

    function highlightWithRules(code, rules) {
      let output = "";
      let index = 0;
      while (index < code.length) {
        let best = null;
        for (const rule of rules) {
          rule.regex.lastIndex = index;
          const match = rule.regex.exec(code);
          if (match && match.index === index) {
            best = { text: match[0], className: rule.className };
            break;
          }
        }
        if (best) {
          output += `<span class="${best.className}">${escapeHtml(best.text)}</span>`;
          index += best.text.length;
        } else {
          output += escapeHtml(code[index]);
          index += 1;
        }
      }
      return output;
    }

    function pythonRules() {
      return [
        { regex: /#[^\n]*/gy, className: "tok-com" },
        { regex: /(?:r|u|f|b|fr|rf|br|rb)?'{3}[\s\S]*?'{3}/giy, className: "tok-str" },
        { regex: /(?:r|u|f|b|fr|rf|br|rb)?"{3}[\s\S]*?"{3}/giy, className: "tok-str" },
        { regex: /(?:r|u|f|b|fr|rf|br|rb)?'(?:\\.|[^'\\])*'/giy, className: "tok-str" },
        { regex: /(?:r|u|f|b|fr|rf|br|rb)?"(?:\\.|[^"\\])*"/giy, className: "tok-str" },
        { regex: /\b(def|class|return|if|elif|else|for|while|try|except|finally|with|as|import|from|pass|raise|True|False|None|in|is|and|or|not)\b/gy, className: "tok-key" },
        { regex: /\b[0-9]+(?:\.[0-9]+)?\b/gy, className: "tok-num" }
      ];
    }

    function javascriptRules() {
      return [
        { regex: /\/\/[^\n]*/gy, className: "tok-com" },
        { regex: /\/\*[\s\S]*?\*\//gy, className: "tok-com" },
        { regex: /'(?:\\.|[^'\\])*'/gy, className: "tok-str" },
        { regex: /"(?:\\.|[^"\\])*"/gy, className: "tok-str" },
        { regex: /`(?:\\.|[^`\\])*`/gy, className: "tok-str" },
        { regex: /\b(function|const|let|var|return|if|else|for|while|try|catch|finally|await|async|import|from|export|class|new|true|false|null|undefined)\b/gy, className: "tok-key" },
        { regex: /\b[0-9]+(?:\.[0-9]+)?\b/gy, className: "tok-num" }
      ];
    }

    function jsonRules() {
      return [
        { regex: /"(?:\\.|[^"\\])*"(?=\s*:)/gy, className: "tok-key" },
        { regex: /"(?:\\.|[^"\\])*"/gy, className: "tok-str" },
        { regex: /\b-?[0-9]+(?:\.[0-9]+)?\b/gy, className: "tok-num" },
        { regex: /\b(true|false|null)\b/gy, className: "tok-key" }
      ];
    }

    document.querySelectorAll(".activity .icon").forEach(btn => btn.addEventListener("click", () => setSideView(btn.dataset.view)));
    document.querySelectorAll(".modebar button").forEach(btn => btn.addEventListener("click", () => {
      state.mode = btn.dataset.mode;
      document.querySelectorAll(".modebar button").forEach(item => item.classList.toggle("active", item === btn));
    }));
    document.querySelectorAll(".panel-tabs button").forEach(btn => btn.addEventListener("click", () => loadPanel(btn.dataset.panel)));
    $("send").addEventListener("click", sendMessage);
    $("workspaceForm").addEventListener("submit", switchWorkspace);
    $("prompt").addEventListener("keydown", event => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") sendMessage();
    });
        document.addEventListener("keydown", event => {
            if (!(event.metaKey || event.ctrlKey)) return;
            const key = event.key.toLowerCase();
            if (key === "o") {
                event.preventDefault();
                $("workspacePath").focus();
            } else if (key === "e") {
                event.preventDefault();
                openCurrentFileInEditor();
            } else if (key === "r") {
                event.preventDefault();
                loadPanel("diagnostics");
            } else if (key === "enter" && document.activeElement !== $("prompt")) {
                event.preventDefault();
                sendMessage();
            }
        });

        renderRecentWorkspaces();

    loadWorkspace().catch(err => {
      $("editor").textContent = String(err);
      setStatus("Error");
    });
  </script>
</body>
</html>
"""
