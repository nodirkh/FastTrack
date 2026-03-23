from __future__ import annotations

import subprocess

from rich.console import Console

from iter.pipeline import Stage, Context

console = Console()


def _git(args: list[str], cwd=None, check: bool = True):
    return subprocess.run(
        ["git"] + args, cwd=cwd, check=check, capture_output=True, text=True,
    )


class BaseStage(Stage):
    """Ensures both base trees (upstream + stable) are cloned and up to date."""

    def run(self, ctx: Context) -> None:
        gc = ctx.global_config
        gc.base_dir.mkdir(parents=True, exist_ok=True)

        for name in ("upstream", "stable"):
            path = gc.tree_path(name)
            if not path.exists():
                console.print(f"    [yellow]Cloning {name}[/yellow] (first run — this takes a while...)")
                _git(["clone", "--depth=1", gc.tree_url(name), str(path)])
                _git(["fetch", "--unshallow"], cwd=path, check=False)
            else:
                console.print(f"    Fetching [cyan]{name}[/cyan]...")
                _git(["fetch", "--all", "--tags"], cwd=path)

        console.print("    [green]Base trees up to date.[/green]")
