from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rich.console import Console

from iter.pipeline import Stage, Context

console = Console()


def _git(args: list[str], cwd=None, check: bool = True):
    return subprocess.run(
        ["git"] + args, cwd=cwd, check=check, capture_output=True, text=True,
    )


class ExtractPatchesStage(Stage):
    """
    Serialize commits from base/<tree> (above base_commit) into
    iterations/<name>/patches/ and update patches list in config.yaml.

    Used in the Launch flow after the user has committed work to base/.
    """

    def run(self, ctx: Context) -> None:
        cfg = ctx.iteration_config
        gc  = ctx.global_config
        tree_path   = gc.tree_path(cfg.base["tree"])
        patches_dir = gc.iter_dir(cfg.name) / "patches"
        patches_dir.mkdir(exist_ok=True)

        # Clear stale patches
        for p in patches_dir.glob("*.patch"):
            p.unlink()

        result = _git(
            ["format-patch", f"{cfg.base_commit}..HEAD", "-o", str(patches_dir)],
            cwd=tree_path,
        )
        patch_files = [p for p in result.stdout.strip().splitlines() if p]
        console.print(f"    Extracted [green]{len(patch_files)}[/green] patch(es) → {patches_dir}")

        # Persist updated patch list into config.yaml
        cfg.patches = [str(Path(p).relative_to(gc.root)) for p in patch_files]
        from iter.config import ConfigParser
        ConfigParser.save(cfg, gc.iter_config_path(cfg.name))


class ApplyPatchesStage(Stage):
    """
    Reset base/<tree> to base_commit and apply patches via git am.

    On conflict: drops the user into a subshell to resolve, then resumes.
    rollback(): git am --abort + hard reset to base_commit.
    """

    def run(self, ctx: Context) -> None:
        cfg       = ctx.iteration_config
        gc        = ctx.global_config
        tree_path = gc.tree_path(cfg.base["tree"])

        # Always start from the locked commit
        _git(["reset", "--hard", cfg.base_commit], cwd=tree_path)

        if not cfg.patches:
            console.print("    [dim]No patches — skipping.[/dim]")
            return

        patch_paths = [str(gc.root / p) for p in cfg.patches]
        console.print(f"    Applying [cyan]{len(patch_paths)}[/cyan] patch(es)...")

        result = _git(["am", "--3way"] + patch_paths, cwd=tree_path, check=False)
        if result.returncode != 0:
            console.print("[red]    git am failed — conflict detected.[/red]")
            self._resolve_interactively(tree_path)

    def _resolve_interactively(self, tree_path: Path) -> None:
        console.print("\n[yellow]Dropping into a shell. Fix conflicts, then:[/yellow]")
        console.print("  Resolved  → [bold]git add <file> && git am --continue[/bold]")
        console.print("  Skip patch → [bold]git am --skip[/bold]")
        console.print("  Give up    → [bold]git am --abort[/bold]  (pipeline will abort)\n")

        os.system(f'cd "{tree_path}" && $SHELL')

        # If the rebase-apply dir still exists, the user didn't finish
        if (tree_path / ".git" / "rebase-apply").exists():
            raise RuntimeError(
                "Patch conflicts unresolved. Fix and re-run, or remove the patches."
            )

    def rollback(self, ctx: Context) -> None:
        cfg       = ctx.iteration_config
        gc        = ctx.global_config
        tree_path = gc.tree_path(cfg.base["tree"])

        _git(["am", "--abort"], cwd=tree_path, check=False)
        _git(["reset", "--hard", cfg.base_commit], cwd=tree_path, check=False)
        console.print(f"    [dim]base/{cfg.base['tree']} reset to {cfg.base_commit[:8]}[/dim]")
