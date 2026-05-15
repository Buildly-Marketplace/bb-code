from __future__ import annotations

from pathlib import Path

import pytest

from bb_code.web_ui import (
    APP_HTML,
    WorkspaceSession,
    build_agent_prompt,
    build_cloud_native_audit_prompt,
    build_edit_suggestion_prompt,
    build_explorer_context,
    build_tree,
    collect_cloud_native_inventory,
    collect_database_and_model_inventory,
    collect_session_workspace,
    collect_workspace,
    find_port_listeners,
    generate_platform_report,
    k8s_monitor_integration_status,
    lint_workspace_file,
    markdown_to_html,
    normalize_chat_response,
    parse_edit_suggestions,
    read_report_file,
    read_workspace_file,
    render_report_html,
    scan_github_actions_deployments,
    summarize_deployments,
    summarize_pods,
    search_workspace,
    write_workspace_file,
)


def test_collect_workspace_includes_repos_and_tree(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")

    workspace = collect_workspace(tmp_path)

    assert workspace["name"] == tmp_path.name
    assert workspace["repos"][0]["name"] == tmp_path.name
    assert any(node["name"] == "app" for node in workspace["tree"])


def test_app_html_keeps_chat_composer_bounded() -> None:
    assert "padding-right: 390px" in APP_HTML
    assert "position: absolute; top: 0; right: 0; bottom: 0; width: 390px" in APP_HTML
    assert "padding-bottom: 142px" in APP_HTML
    assert "position: absolute; left: 0; right: 0; bottom: 0; height: 142px" in APP_HTML
    assert "resize: none" in APP_HTML
    assert "grid-template-rows: 32px minmax(0, 1fr) 22px" in APP_HTML


def test_app_html_exposes_model_discovery_controls() -> None:
    assert "Refresh Models" in APP_HTML
    assert "/api/settings/models" in APP_HTML


def test_app_html_exposes_kubectl_diagnostics() -> None:
    assert "Kubernetes Diagnostics" in APP_HTML
    assert "/api/kubectl-diagnostics" in APP_HTML


def test_app_html_exposes_k8s_monitor_integration() -> None:
    assert "K8s Monitor" in APP_HTML
    assert "/api/integrations/k8s-monitor" in APP_HTML
    assert "Open ForgeOps dashboard" in APP_HTML


def test_app_html_exposes_platform_report() -> None:
    assert "Platform Report" in APP_HTML
    assert "/api/platform-report" in APP_HTML
    assert "/reports?path=" in APP_HTML


def test_app_html_exposes_auto_apply_toggle() -> None:
    assert "autoApplyToggle" in APP_HTML
    assert "Auto-apply" in APP_HTML


def test_workspace_session_switches_active_root(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (second / "README.md").write_text("# Second\n", encoding="utf-8")
    session = WorkspaceSession(first)

    workspace = session.switch(str(second))

    assert session.root == second.resolve()
    assert workspace["root"] == str(second.resolve())
    assert workspace["tree"][0]["name"] == "README.md"


def test_workspace_session_switch_repo_keeps_container_repos(tmp_path: Path) -> None:
    api = tmp_path / "api"
    worker = tmp_path / "worker"
    api.mkdir()
    worker.mkdir()
    (api / "pyproject.toml").write_text("[project]\nname = 'api'\n", encoding="utf-8")
    (worker / "package.json").write_text('{"name":"worker"}\n', encoding="utf-8")
    session = WorkspaceSession(tmp_path)

    workspace = session.switch_repo("worker")

    assert workspace["root"] == str(worker.resolve())
    assert workspace["baseRoot"] == str(tmp_path.resolve())
    assert workspace["activeRepo"] == "worker"
    assert [repo["name"] for repo in workspace["repos"]] == ["api", "worker"]


def test_k8s_monitor_integration_status_detects_missing_submodule(tmp_path: Path) -> None:
    status = k8s_monitor_integration_status(tmp_path)

    assert status["installed"] is False
    assert status["path"] == "integrations/k8s-monitor"
    assert "git submodule update" in status["installCommand"]


def test_k8s_monitor_integration_status_detects_installed_submodule(tmp_path: Path) -> None:
    integration = tmp_path / "integrations" / "k8s-monitor"
    integration.mkdir(parents=True)
    (integration / "main.py").write_text("print('forgeops')\n", encoding="utf-8")
    (integration / "BUILDLY.yaml").write_text("name: ForgeOps\n", encoding="utf-8")

    status = k8s_monitor_integration_status(tmp_path)

    assert status["installed"] is True
    assert status["manifest"] == "integrations/k8s-monitor/BUILDLY.yaml"
    assert status["dashboardUrl"] == "http://127.0.0.1:8000/"


def test_collect_session_workspace_reports_active_repo(tmp_path: Path) -> None:
    api = tmp_path / "api"
    api.mkdir()
    (api / "README.md").write_text("# API\n", encoding="utf-8")
    session = WorkspaceSession(tmp_path)
    session.switch_repo("api")

    workspace = collect_session_workspace(session)

    assert workspace["activeRepo"] == "api"
    assert workspace["tree"][0]["name"] == "README.md"


def test_workspace_session_restores_saved_workspace(tmp_path: Path) -> None:
    container = tmp_path / "container"
    api = container / "api"
    api.mkdir(parents=True)
    (api / "README.md").write_text("# API\n", encoding="utf-8")
    session = WorkspaceSession(tmp_path)
    session.switch(str(container))
    session.switch_repo("api")

    restored = WorkspaceSession(tmp_path)
    workspace = collect_session_workspace(restored)

    assert workspace["baseRoot"] == str(container.resolve())
    assert workspace["root"] == str(api.resolve())
    assert workspace["activeRepo"] == "api"


def test_workspace_session_rejects_missing_path(tmp_path: Path) -> None:
    session = WorkspaceSession(tmp_path)

    with pytest.raises(FileNotFoundError):
        session.switch(str(tmp_path / "missing"))


def test_workspace_session_rejects_file_path(tmp_path: Path) -> None:
    file_path = tmp_path / "README.md"
    file_path.write_text("# Example\n", encoding="utf-8")
    session = WorkspaceSession(tmp_path)

    with pytest.raises(ValueError):
        session.switch(str(file_path))


def test_collect_cloud_native_inventory_maps_services_dependencies_and_models(tmp_path: Path) -> None:
    users = tmp_path / "users"
    billing = tmp_path / "billing"
    users.mkdir()
    billing.mkdir()
    (users / "pyproject.toml").write_text(
        '[project]\nname = "users"\ndependencies = ["fastapi>=0.1", "redis"]\n',
        encoding="utf-8",
    )
    (users / "models.py").write_text(
        "from pydantic import BaseModel\n\nclass UserModel(BaseModel):\n    id: int\n# redis cache\n",
        encoding="utf-8",
    )
    (billing / "package.json").write_text(
        '{"dependencies":{"fastapi":"1.0.0","express":"4.0.0"}}',
        encoding="utf-8",
    )
    (billing / "models.py").write_text(
        "from pydantic import BaseModel\n\nclass UserModel(BaseModel):\n    id: int\n# gateway router\n",
        encoding="utf-8",
    )

    inventory = collect_cloud_native_inventory(tmp_path)

    assert [service["name"] for service in inventory["services"]] == ["billing", "users"]
    assert inventory["duplicateModels"] == {"UserModel": ["billing", "users"]}
    assert inventory["sharedDependencies"] == {"fastapi": ["billing", "users"]}
    assert inventory["cacheSignals"][0]["service"] == "users"
    assert inventory["gatewaySignals"][0]["service"] == "billing"


def test_summarize_kubernetes_resources() -> None:
    pods = summarize_pods(
        {
            "items": [
                {
                    "metadata": {
                        "namespace": "default",
                        "name": "api-123",
                        "labels": {"app": "api"},
                        "ownerReferences": [{"kind": "ReplicaSet", "name": "api-abc"}],
                    },
                    "spec": {"nodeName": "node-1", "containers": [{"image": "registry/api:sha"}]},
                    "status": {"phase": "Running", "containerStatuses": [{"restartCount": 2}]},
                }
            ]
        }
    )
    deployments = summarize_deployments(
        {
            "items": [
                {
                    "metadata": {"namespace": "default", "name": "api", "labels": {"app": "api"}},
                    "spec": {
                        "replicas": 2,
                        "selector": {"matchLabels": {"app": "api"}},
                        "template": {"spec": {"containers": [{"image": "registry/api:sha"}]}},
                    },
                    "status": {"availableReplicas": 1},
                }
            ]
        }
    )

    assert pods[0]["restarts"] == 2
    assert pods[0]["owners"] == [{"kind": "ReplicaSet", "name": "api-abc"}]
    assert deployments[0]["available"] == 1
    assert deployments[0]["images"] == ["registry/api:sha"]


def test_scan_github_actions_deployment_signals(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "deploy.yml").write_text(
        "jobs:\n  deploy:\n    steps:\n      - run: gcloud container clusters get-credentials prod\n      - run: kubectl apply -f k8s/\n",
        encoding="utf-8",
    )

    workflows = scan_github_actions_deployments(tmp_path)

    assert workflows[0]["path"] == ".github/workflows/deploy.yml"
    assert "gcloud" in workflows[0]["signals"]
    assert "kubectl" in workflows[0]["signals"]


def test_collect_database_and_model_inventory_detects_signals(tmp_path: Path) -> None:
    api = tmp_path / "api"
    api.mkdir()
    (api / "requirements.txt").write_text("psycopg\nredis\n", encoding="utf-8")
    (api / "models.py").write_text(
        "from django.db import models\n\nclass Order(models.Model):\n    pass\n",
        encoding="utf-8",
    )

    inventory = collect_database_and_model_inventory(tmp_path)

    assert inventory["databases"]["postgres"] == ["api"]
    assert inventory["databases"]["redis"] == ["api"]
    assert inventory["models"]["Order"] == ["api"]


def test_generate_platform_report_writes_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = tmp_path / "api"
    api.mkdir()
    (api / "requirements.txt").write_text("fastapi\npsycopg\n", encoding="utf-8")
    (api / "models.py").write_text(
        "from pydantic import BaseModel\n\nclass UserModel(BaseModel):\n    id: int\n",
        encoding="utf-8",
    )

    def fake_kubectl(_: Path) -> dict[str, object]:
        return {
            "tools": {"kubectl": "/usr/bin/kubectl", "gcloud": "not found"},
            "contexts": ["dev"],
            "currentContext": "dev",
            "namespaces": ["default"],
            "workloads": [
                {
                    "namespace": "default",
                    "name": "api",
                    "replicas": 2,
                    "available": 1,
                    "images": ["registry/api:sha"],
                }
            ],
            "pods": [{"namespace": "default", "name": "api-123", "phase": "Running", "restarts": 1}],
            "services": [],
            "repoMatches": [],
            "githubActions": [],
            "notes": [],
        }

    monkeypatch.setattr("bb_code.web_ui.kubectl_diagnostics", fake_kubectl)

    report = generate_platform_report(tmp_path, include_ai=False)

    assert report["status"] == "pass"
    assert report["summary"]["services"] == 1
    assert (tmp_path / ".bb" / "reports" / "platform-diagnostic.md").exists()
    assert "Deployment availability gap" in (tmp_path / ".bb" / "reports" / "bug-risk-report.md").read_text(
        encoding="utf-8"
    )


def test_read_report_file_allows_markdown_report(tmp_path: Path) -> None:
    report_dir = tmp_path / ".bb" / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "platform-diagnostic.md").write_text("# Report\n", encoding="utf-8")

    assert read_report_file(tmp_path, ".bb/reports/platform-diagnostic.md") == "# Report\n"


def test_read_report_file_rejects_non_report_path(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Secret-ish workspace file\n", encoding="utf-8")

    with pytest.raises(ValueError):
        read_report_file(tmp_path, "README.md")


def test_markdown_to_html_renders_report_blocks() -> None:
    html = markdown_to_html("# Report\n\n## Summary\n- **Risk**: use `cache`\n\n```python\nprint('ok')\n```\n")

    assert "<h1>Report</h1>" in html
    assert "<h2>Summary</h2>" in html
    assert "<strong>Risk</strong>" in html
    assert "<code>cache</code>" in html
    assert "<pre><code>print(&#x27;ok&#x27;)</code></pre>" in html


def test_render_report_html_includes_raw_markdown_link() -> None:
    html = render_report_html(".bb/reports/platform-diagnostic.md", "# Report\n")

    assert "text/markdown" not in html
    assert "Raw Markdown" in html
    assert "/reports?path=.bb%2Freports%2Fplatform-diagnostic.md&amp;raw=1" in html
    assert "<h1>Report</h1>" in html


def test_build_cloud_native_audit_prompt_includes_required_sections(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    prompt = build_cloud_native_audit_prompt(tmp_path)

    assert "Service map" in prompt
    assert "All-repo model ERD" in prompt
    assert "Gateway, routing, and cache efficiency review" in prompt
    assert '"name": "api"' in prompt


def test_build_explorer_context_lists_top_level_services(tmp_path: Path) -> None:
    (tmp_path / "buildly-core").mkdir()
    (tmp_path / "buildly-core" / "README.md").write_text("# Gateway\n", encoding="utf-8")
    (tmp_path / "sensor_service").mkdir()
    (tmp_path / "sensor_service" / "pyproject.toml").write_text("[project]\nname = 'sensor'\n", encoding="utf-8")

    context = build_explorer_context(tmp_path)

    assert "`buildly-core` (buildly-core)" in context
    assert "`sensor_service` (sensor_service)" in context
    assert "`buildly-core/`" in context


def test_read_workspace_file_blocks_path_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_workspace_file(tmp_path, "../outside.txt")


def test_read_workspace_file_returns_content(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")

    data = read_workspace_file(tmp_path, "README.md")

    assert data["path"] == "README.md"
    assert data["language"] == "markdown"
    assert "# Example" in data["content"]


def test_build_tree_skips_heavy_directories(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("", encoding="utf-8")

    tree = build_tree(tmp_path)

    names = {node["name"] for node in tree}
    assert "src" in names
    assert "node_modules" not in names


def test_build_agent_prompt_includes_buildly_safety_and_open_files(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")

    prompt = build_agent_prompt(tmp_path, "Help", "debug", ["app.py"])

    assert "Buildly way" in prompt
    assert "Do not claim that you edited files" in prompt
    assert "Do not say you cannot access them" in prompt
    assert "app.py" in prompt
    assert "print('hi')" in prompt


def test_build_agent_prompt_treats_explorer_context_as_authoritative(tmp_path: Path) -> None:
    (tmp_path / "buildly-core").mkdir()
    (tmp_path / "buildly-core" / "README.md").write_text("# Gateway\n", encoding="utf-8")

    prompt = build_agent_prompt(tmp_path, "Inspect buildly-core", "agent", [])

    assert "Treat the Explorer context as authoritative" in prompt
    assert "`buildly-core` (buildly-core)" in prompt
    assert "Cloud-native service inventory" in prompt


def test_search_workspace_finds_python_file_contents(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def health_check():\n    return True\n", encoding="utf-8")

    results = search_workspace(tmp_path, "health_check")

    assert results["results"]
    assert results["results"][0]["path"] == "app.py"


def test_write_workspace_file_updates_text_file(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('old')\n", encoding="utf-8")

    result = write_workspace_file(tmp_path, "app.py", "print('new')\n")

    assert result["status"] == "updated"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('new')\n"


def test_lint_workspace_file_reports_python_syntax_error(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    result = lint_workspace_file(tmp_path, "app.py")

    assert result["language"] == "python"
    assert result["diagnostics"][0]["severity"] == "error"
    assert "invalid syntax" in result["diagnostics"][0]["message"]


def test_lint_workspace_file_reports_javascript_unclosed_bracket(tmp_path: Path) -> None:
    (tmp_path / "app.js").write_text("function start() {\n  return true;\n", encoding="utf-8")

    result = lint_workspace_file(tmp_path, "app.js")

    assert result["language"] == "javascript"
    assert any(item["message"] == "Unclosed `{`." for item in result["diagnostics"])


def test_normalize_chat_response_extracts_response_from_fenced_json() -> None:
    raw = '```json\n{"response":"Use the opened Python file contents.","edits":[]}\n```'

    assert normalize_chat_response(raw) == "Use the opened Python file contents."


def test_normalize_chat_response_hides_unstructured_json_object() -> None:
    raw = '```json\n{"status":"Ready","openFiles":["app.py"]}\n```'

    result = normalize_chat_response(raw)

    assert "structured JSON" in result
    assert "openFiles" not in result


def test_find_port_listeners_returns_pid_and_command(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Completed:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "lsof":
            return Completed(0, "p123\n")
        return Completed(0, "python -m bb_code.web_cli\n")

    monkeypatch.setattr("bb_code.web_ui.subprocess.run", fake_run)

    listeners = find_port_listeners(8787)

    assert listeners == [{"pid": "123", "command": "python -m bb_code.web_cli"}]
    assert calls[0] == ["lsof", "-nP", "-iTCP:8787", "-sTCP:LISTEN", "-Fp"]


def test_build_edit_suggestion_prompt_includes_python_open_file(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")

    prompt = build_edit_suggestion_prompt(tmp_path, "Change greeting", ["app.py"])

    assert "Return JSON only" in prompt
    assert "app.py" in prompt
    assert "print('hi')" in prompt


def test_parse_edit_suggestions_returns_safe_existing_text_file(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('old')\n", encoding="utf-8")
    raw = '{"edits":[{"path":"app.py","summary":"Update greeting","content":"print(\\\"new\\\")\\n"}]}'

    edits = parse_edit_suggestions(tmp_path, raw)

    assert edits[0]["path"] == "app.py"
    assert edits[0]["summary"] == "Update greeting"
    assert edits[0]["content"] == 'print("new")\n'
    assert edits[0]["original"] == "print('old')\n"


def test_parse_edit_suggestions_extracts_json_from_prose(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('old')\n", encoding="utf-8")
    raw = 'Here are edits:\n```json\n{"edits":[{"path":"app.py","summary":"Update","content":"print(1)\\n"}]}\n```'

    edits = parse_edit_suggestions(tmp_path, raw)

    assert edits[0]["path"] == "app.py"
    assert edits[0]["content"] == "print(1)\n"


def test_parse_edit_suggestions_materializes_find_replace(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Before\nAnchor\nAfter\n", encoding="utf-8")
    raw = json_dumps(
        {
            "edits": [
                {
                    "path": "README.md",
                    "summary": "Insert local UI note",
                    "find": "Anchor\n",
                    "replace": "Anchor\nThe UI is local-only.\n",
                }
            ]
        }
    )

    edits = parse_edit_suggestions(tmp_path, raw)

    assert edits[0]["content"] == "Before\nAnchor\nThe UI is local-only.\nAfter\n"


def test_parse_edit_suggestions_rejects_path_escape(tmp_path: Path) -> None:
    raw = '{"edits":[{"path":"../app.py","summary":"Bad","content":"x"}]}'

    edits = parse_edit_suggestions(tmp_path, raw)

    assert edits == []


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value)


def test_parse_edit_suggestions_rejects_suspicious_tiny_replacement(tmp_path: Path) -> None:
    original = "\n".join(f"line {index}" for index in range(100))
    (tmp_path / "README.md").write_text(original, encoding="utf-8")
    raw = '{"edits":[{"path":"README.md","summary":"Bad truncate","content":"The UI is local-only.\\n"}]}'

    edits = parse_edit_suggestions(tmp_path, raw)

    assert edits == []
