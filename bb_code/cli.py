from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .model_router import (
    PROVIDER_DEFAULTS,
    SUPPORTED_PROVIDERS,
    ModelError,
    ModelNotFoundError,
    ModelTimeoutError,
    OllamaClient,
    OllamaNotRunningError,
    create_client,
)
from .diagnostics import SelfDiagnosticEngine
from .planner import create_plan
from .repo_context import scan_repository
from .settings import AgentSettings, load_settings, resolve_settings, save_settings
from .utils import context_path, ensure_bb_dirs, relative
from .web_ui import serve_workspace


app = typer.Typer(
    help="bb-code: a safe local-first planning CLI for coding tasks.",
    no_args_is_help=True,
)
settings_app = typer.Typer(help="Manage bb-code model and provider settings.")
app.add_typer(settings_app, name="settings")
console = Console()


@app.command()
def init() -> None:
    """Create the local bb-code workspace."""
    repo_root = Path.cwd()
    ensure_bb_dirs(repo_root)
    console.print("[green]Created local bb-code workspace:[/green]")
    console.print(f"- {relative(repo_root / '.bb', repo_root)}/")
    console.print(f"- {relative(repo_root / '.bb' / 'plans', repo_root)}/")
    console.print(f"- {relative(repo_root / '.bb' / 'cache', repo_root)}/")


@app.command()
def onboard(
    ollama_url: Annotated[
        str,
        typer.Option("--ollama-url", help="Local or remote Ollama base URL."),
    ] = "http://localhost:11434",
    model: Annotated[
        str,
        typer.Option("--model", "-m", help="Default Ollama model."),
    ] = "qwen2.5-coder:7b",
    timeout: Annotated[
        int,
        typer.Option("--timeout", help="Default model timeout in seconds."),
    ] = 120,
    test_connection: Annotated[
        bool,
        typer.Option("--test/--no-test", help="Test the configured model after saving."),
    ] = True,
) -> None:
    """Create .bb workspace and save model settings."""
    repo_root = Path.cwd()
    settings = AgentSettings(
        provider="ollama",
        ollama_url=ollama_url,
        model=model,
        timeout_seconds=timeout,
    )
    target = save_settings(repo_root, settings)
    console.print(f"[green]Saved settings to[/green] {relative(target, repo_root)}")
    if test_connection:
        _test_settings(settings)


@settings_app.command("show")
def show_settings() -> None:
    """Show effective bb-code settings."""
    repo_root = Path.cwd()
    settings = resolve_settings(repo_root)
    console.print(Panel(Text(settings.to_json()), title="bb-code Settings", expand=False))


@settings_app.command("set")
def set_settings(
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help=f"Model provider. One of: {', '.join(SUPPORTED_PROVIDERS)}.",
        ),
    ] = None,
    ollama_url: Annotated[
        str | None,
        typer.Option("--ollama-url", help="Local or remote Ollama base URL."),
    ] = None,
    api_base_url: Annotated[
        str | None,
        typer.Option("--api-base-url", help="Remote API base URL (openai, anthropic, openai-compatible)."),
    ] = None,
    api_key_env_var: Annotated[
        str | None,
        typer.Option("--api-key-env-var", help="Environment variable holding the remote API key."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Default model name for the selected provider."),
    ] = None,
    timeout: Annotated[
        int | None,
        typer.Option("--timeout", help="Default model timeout in seconds."),
    ] = None,
) -> None:
    """Update persisted bb-code settings."""
    if provider is not None and provider not in SUPPORTED_PROVIDERS:
        console.print(f"[red]Unsupported provider:[/red] {provider}")
        console.print(f"Choose one of: {', '.join(SUPPORTED_PROVIDERS)}")
        raise typer.Exit(code=1)
    repo_root = Path.cwd()
    current = load_settings(repo_root)
    updated = current.with_overrides(
        provider=provider,
        ollama_url=ollama_url,
        api_base_url=api_base_url,
        api_key_env_var=api_key_env_var,
        model=model,
        timeout_seconds=timeout,
    )
    target = save_settings(repo_root, updated)
    console.print(f"[green]Saved settings to[/green] {relative(target, repo_root)}")


@settings_app.command("test")
def test_settings() -> None:
    """Test the configured model provider."""
    settings = resolve_settings(Path.cwd())
    _test_settings(settings)


@settings_app.command("providers")
def list_providers() -> None:
    """List supported model providers and how to configure each one."""
    for name in SUPPORTED_PROVIDERS:
        defaults = PROVIDER_DEFAULTS[name]
        env_var = defaults["api_key_env_var"] or "(none — local)"
        console.print(f"[bold]{name}[/bold]")
        console.print(f"  default model: {defaults['model']}")
        console.print(f"  default base url: {defaults['base_url'] or '(none — set --api-base-url)'}")
        console.print(f"  API key env var: {env_var}")


@app.command()
def understand() -> None:
    """Scan the current repository and save .bb/context.md."""
    repo_root = Path.cwd()
    ensure_bb_dirs(repo_root)
    repo_context = scan_repository(repo_root)
    markdown = repo_context.to_markdown()
    target = context_path(repo_root)
    target.write_text(markdown, encoding="utf-8")

    console.print(Panel(Text(markdown), title="Repository Summary", expand=False))
    console.print(f"[green]Saved context to[/green] {relative(target, repo_root)}")


@app.command()
def plan(
    task_description: Annotated[str, typer.Argument(help="Task to plan.")],
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Ollama model name."),
    ] = None,
    ollama_url: Annotated[
        str | None,
        typer.Option("--ollama-url", help="Ollama base URL."),
    ] = None,
    timeout: Annotated[
        int | None,
        typer.Option("--timeout", help="Ollama generation timeout in seconds."),
    ] = None,
) -> None:
    """Generate an implementation plan using the configured Ollama model."""
    repo_root = Path.cwd()
    ensure_bb_dirs(repo_root)

    target_context = context_path(repo_root)
    if not target_context.exists():
        repo_context = scan_repository(repo_root)
        target_context.write_text(repo_context.to_markdown(), encoding="utf-8")
        console.print(f"[yellow]Generated missing context at[/yellow] {relative(target_context, repo_root)}")

    settings = resolve_settings(
        repo_root,
        ollama_url=ollama_url,
        model=model,
        timeout_seconds=timeout,
    )
    context_markdown = target_context.read_text(encoding="utf-8")
    client = create_client(settings.provider_config_for_mode("plan"))

    try:
        target = create_plan(repo_root, task_description, context_markdown, client)
    except OllamaNotRunningError:
        console.print("[red]Ollama is not running or is unreachable.[/red]")
        console.print(f"Start Ollama or confirm it is available at {settings.ollama_url}.")
        raise typer.Exit(code=1)
    except ModelNotFoundError:
        console.print(f"[red]Model not found:[/red] {settings.model}")
        console.print(f"Install it with: ollama pull {settings.model}")
        raise typer.Exit(code=1)
    except ModelTimeoutError as exc:
        console.print(f"[red]Ollama timed out:[/red] {exc}")
        console.print("Try a smaller model or increase the timeout with --timeout.")
        raise typer.Exit(code=1)
    except ModelError as exc:
        console.print(f"[red]Planning failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"[green]Saved plan to[/green] {relative(target, repo_root)}")


@app.command()
def diagnose(
    error: Annotated[
        str | None,
        typer.Option("--error", help="Error text to include in the diagnostic report."),
    ] = None,
    error_file: Annotated[
        Path | None,
        typer.Option("--error-file", help="Path to a file containing error output."),
    ] = None,
    write_report: Annotated[
        bool,
        typer.Option("--write-report/--no-write-report", help="Save report to .bb/diagnostics.md."),
    ] = True,
    check_model: Annotated[
        bool,
        typer.Option("--check-model/--skip-model", help="Check configured model availability."),
    ] = True,
    repair: Annotated[
        str | None,
        typer.Option("--repair", help="Apply a listed safe repair key."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Approve the requested safe repair."),
    ] = False,
) -> None:
    """Run reusable self-diagnostics and optional safe repairs."""
    repo_root = Path.cwd()
    settings = resolve_settings(repo_root)
    engine = SelfDiagnosticEngine(repo_root, settings)

    if repair:
        if not yes:
            console.print("[red]Repair requires explicit approval.[/red]")
            console.print(f"Re-run with: bb-code diagnose --repair {repair} --yes")
            raise typer.Exit(code=1)
        result = engine.apply_safe_repair(repair)
        console.print(f"[{_status_color(result.status)}]{result.status.upper()}[/] {result.message}")
        if result.status == "fail":
            raise typer.Exit(code=1)

    error_context = _load_error_context(error, error_file)
    report = engine.run(error_context=error_context, check_model=check_model)
    markdown = report.to_markdown()
    console.print(Panel(Text(markdown), title="Self Diagnostic", expand=False))

    if write_report:
        ensure_bb_dirs(repo_root)
        target = repo_root / ".bb" / "diagnostics.md"
        target.write_text(markdown, encoding="utf-8")
        console.print(f"[green]Saved diagnostic report to[/green] {relative(target, repo_root)}")

    if report.has_failures:
        raise typer.Exit(code=1)


@app.command()
def ui(
    path: Annotated[Path, typer.Argument(help="Workspace folder to open.")] = Path("."),
    host: Annotated[str, typer.Option("--host", help="Host to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to bind.")] = 8787,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the browser automatically."),
    ] = True,
) -> None:
    """Open the optional VS Code-like workspace web UI."""
    serve_workspace(path, host=host, port=port, open_browser=open_browser)


def _test_settings(settings: AgentSettings) -> None:
    if settings.provider not in SUPPORTED_PROVIDERS:
        console.print(f"[red]Unsupported provider:[/red] {settings.provider}")
        console.print(f"Choose one of: {', '.join(SUPPORTED_PROVIDERS)}")
        raise typer.Exit(code=1)
    config = settings.provider_config()
    try:
        client = create_client(config)
    except ModelError as exc:
        console.print(f"[red]Provider test failed:[/red] {exc}")
        raise typer.Exit(code=1)
    try:
        if isinstance(client, OllamaClient):
            client.ensure_model_available()
        else:
            client.generate("Reply with exactly: ok")
    except OllamaNotRunningError:
        console.print("[red]Ollama is not running or is unreachable.[/red]")
        console.print(f"Confirm it is available at {settings.ollama_url}.")
        raise typer.Exit(code=1)
    except ModelNotFoundError:
        console.print(f"[red]Model not found:[/red] {settings.model}")
        console.print(f"Install it with: ollama pull {settings.model}")
        raise typer.Exit(code=1)
    except ModelTimeoutError as exc:
        console.print(f"[red]Ollama timed out:[/red] {exc}")
        raise typer.Exit(code=1)
    except ModelError as exc:
        console.print(f"[red]Provider test failed:[/red] {exc}")
        raise typer.Exit(code=1)
    endpoint = config.base_url
    console.print(f"[green]Connected to[/green] {endpoint} [green]with model[/green] {config.model}")


def _load_error_context(error: str | None, error_file: Path | None) -> str:
    parts: list[str] = []
    if error:
        parts.append(error)
    if error_file:
        try:
            parts.append(error_file.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            parts.append(f"Could not read error file `{error_file}`: {exc}")
    return "\n\n".join(part.strip() for part in parts if part.strip())


def _status_color(status: str) -> str:
    if status == "pass":
        return "green"
    if status == "warn":
        return "yellow"
    return "red"


if __name__ == "__main__":
    app()
