from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .web_ui import serve_workspace


app = typer.Typer(
    help="Open the bb-code VS Code-like workspace UI.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def build(
    path: Annotated[Path, typer.Argument(help="Workspace folder to open.")] = Path("."),
    host: Annotated[str, typer.Option("--host", help="Host to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to bind.")] = 8787,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the browser automatically."),
    ] = True,
) -> None:
    """Serve a local web UI for a workspace."""
    serve_workspace(path, host=host, port=port, open_browser=open_browser)
