from __future__ import annotations

from pathlib import Path

from bb_code.diagnostics import SelfDiagnosticEngine
from bb_code.settings import AgentSettings


def test_diagnostics_report_missing_workspace_and_context(tmp_path: Path) -> None:
    engine = SelfDiagnosticEngine(tmp_path, AgentSettings())

    report = engine.run(check_model=False)

    names = {result.name: result for result in report.results}
    assert names["bb workspace"].status == "fail"
    assert names["bb workspace"].repair_key == "create-bb-workspace"
    assert names["Repository context"].repair_key == "regenerate-context"


def test_diagnostics_safe_repairs_create_workspace_and_context(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")
    engine = SelfDiagnosticEngine(tmp_path, AgentSettings())

    workspace = engine.apply_safe_repair("create-bb-workspace")
    context = engine.apply_safe_repair("regenerate-context")

    assert workspace.status == "pass"
    assert context.status == "pass"
    assert (tmp_path / ".bb" / "plans").exists()
    assert (tmp_path / ".bb" / "context.md").exists()


def test_diagnostic_report_includes_error_context(tmp_path: Path) -> None:
    engine = SelfDiagnosticEngine(tmp_path, AgentSettings())

    report = engine.run(error_context="Traceback: boom", check_model=False)

    markdown = report.to_markdown()
    assert "Traceback: boom" in markdown
