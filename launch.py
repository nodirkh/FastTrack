#!/usr/bin/env python3
"""
launch.py - Unified entry point for the kernel iteration manager.

  python launch.py

Prompts to:
  1. Create a new iteration
  2. Extract patches from the current base/ working state
  3. Run an existing iteration (apply patches → build → boot)
"""

import sys
from pathlib import Path

from argparse import ArgumentParser

import questionary
from rich.console import Console
from rich.table import Table

from iter.config import GlobalConfig, ConfigParser, ConfigBuilder
from iter.pipeline import Pipeline, Context
from iter.stages.base import BaseStage
from iter.stages.patcher import ExtractPatchesStage, ApplyPatchesStage
from iter.stages.build import BuildStage
from iter.stages.rootfs import RootfsStage
from iter.stages.emulator import EmulatorStage
from iter.nested import NestedBuildStage, NestedEmulatorStage

console = Console()

ASCII_BANNER = r'''
 ________ ________  ________  _________
|\  _____\\   __  \|\   ____\|\___   ___\
\ \  \__/\ \  \|\  \ \  \___|\|___ \  \_|
 \ \   __\\ \   __  \ \_____  \   \ \  \
  \ \  \_| \ \  \ \  \|____|\  \   \ \  \
   \ \__\   \ \__\ \__\____\_\  \   \ \__\
    \|__|    \|__|\|__|\_________\   \|__|
 _________  ________  \|_________|______  ___  __
|\___   ___\\   __  \|\   __  \|\   ____\|\  \|\  \
\|___ \  \_\ \  \|\  \ \  \|\  \ \  \___|\ \  \/  /|_
     \ \  \ \ \   _  _\ \   __  \ \  \    \ \   ___  \
      \ \  \ \ \  \\  \\ \  \ \  \ \  \____\ \  \\ \  \
       \ \__\ \ \__\\ _\\ \__\ \__\ \_______\ \__\\ \__\
        \|__|  \|__|\|__|\|__|\|__|\|_______|\|__| \|__|
'''


class LaunchConsole:

    def __init__(self):
        self.gc = GlobalConfig()
        self.parser = ArgumentParser()

        self.parser.add_argument("-c", "--config", action="store_true", description="path to global config", default=".")

    # -- entry -----------------------------------------------------------------

    def run(self) -> None:
        console.print(f"[cyan]{ASCII_BANNER}[/cyan]")

        args = self.parser.parse_args()

        if not Path.joinpath(args.config, "config.global.yaml").exists():
            console.print(f"[yellow]Global config not found. Initializing...[/yellow]")
            


        # Always sync base trees first
        console.print("\n[bold]Base trees[/bold]")
        Pipeline([BaseStage()]).run(Context(global_config=self.gc))

        iterations = self._list_iterations()
        self._show_table(iterations)

        choices = [
            "Create new iteration",
            "Extract patches  (serialize base/ commits → iteration)",
            "Run iteration    (apply patches → build → boot)",
        ]
        action = questionary.select("What would you like to do?", choices=choices).ask()
        if action is None:
            sys.exit(0)

        if action.startswith("Create"):
            self._create()
        elif action.startswith("Extract"):
            self._extract(iterations)
        elif action.startswith("Run"):
            self._run(iterations)

    # -- actions ---------------------------------------------------------------

    def _create(self) -> None:
        console.print()
        cfg = ConfigBuilder(self.gc).build()
        if cfg is None:
            return

        # Create iteration directory structure
        iter_dir = self.gc.iter_dir(cfg.name)
        iter_dir.mkdir(parents=True)
        (iter_dir / "patches").mkdir()
        (iter_dir / "build").mkdir()
        (iter_dir / "logs").mkdir()

        ConfigParser.save(cfg, self.gc.iter_config_path(cfg.name))

        console.print(f"\n[green]Iteration [bold]{cfg.name}[/bold] created.[/green]")
        console.print(f"  Commit your changes to [cyan]base/{cfg.base['tree']}/[/cyan], then:")
        console.print(f"  Extract patches  → run [cyan]launch.py[/cyan] and pick 'Extract patches'")
        console.print(f"  Run              → run [cyan]launch.py[/cyan] and pick 'Run iteration'")

        if questionary.confirm("\nRun this iteration now?", default=False).ask():
            self._run_iteration(cfg.name)

    def _extract(self, iterations: list[str]) -> None:
        if not iterations:
            console.print("[yellow]No iterations yet — create one first.[/yellow]")
            return

        name = questionary.select("Extract patches for:", choices=iterations).ask()
        if not name:
            return

        cfg = ConfigParser.load(self.gc.iter_config_path(name))
        ctx = Context(global_config=self.gc, iteration_name=name, iteration_config=cfg)

        console.print(f"\n[bold]Extracting patches for [cyan]{name}[/cyan][/bold]")
        Pipeline([ExtractPatchesStage()]).run(ctx)

    def _run(self, iterations: list[str]) -> None:
        if not iterations:
            console.print("[yellow]No iterations yet — create one first.[/yellow]")
            return

        name = questionary.select("Select iteration to run:", choices=iterations).ask()
        if not name:
            return

        self._run_iteration(name)

    def _run_iteration(self, name: str) -> None:
        cfg = ConfigParser.load(self.gc.iter_config_path(name))
        ctx = Context(global_config=self.gc, iteration_name=name, iteration_config=cfg)

        console.print(f"\n[bold]Running iteration [cyan]{name}[/cyan][/bold]")

        if cfg.nested:
            console.print("[dim]  (nested virtualization enabled)[/dim]")

            # Offer tmux multi-window mode when available
            from iter.tmux import TmuxSession
            if TmuxSession.available():
                use_tmux = questionary.confirm(
                    "Launch in tmux? (serial, monitor, logs, L1/L2 in separate windows)",
                    default=True,
                ).ask()
                ctx.artifacts["_use_tmux"] = bool(use_tmux)

            Pipeline([
                ApplyPatchesStage(),
                NestedBuildStage(),
                NestedEmulatorStage(),
            ]).run(ctx)
        else:
            Pipeline([
                ApplyPatchesStage(),
                BuildStage(),
                RootfsStage(),
                EmulatorStage(),
            ]).run(ctx)

    # -- display ---------------------------------------------------------------

    def _list_iterations(self) -> list[str]:
        self.gc.iterations_dir.mkdir(exist_ok=True)
        return sorted(d.name for d in self.gc.iterations_dir.iterdir() if d.is_dir())

    def _show_table(self, iterations: list[str]) -> None:
        if not iterations:
            console.print("\n[dim]No iterations yet.[/dim]\n")
            return

        table = Table(
            title="Iterations",
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Name",      style="cyan", no_wrap=True)
        table.add_column("Tree")
        table.add_column("Ref")
        table.add_column("Patches",   justify="right")
        table.add_column("Rootfs")
        table.add_column("Emulation")
        table.add_column("Created",   style="dim")

        for name in iterations:
            config_path = self.gc.iter_config_path(name)
            if not config_path.exists():
                table.add_row(name, *["?"] * 6)
                continue

            cfg    = ConfigParser.load(config_path)
            rootfs = cfg.rootfs
            rootfs_label = (
                Path(rootfs["path"]).name if rootfs.get("shared") else rootfs.get("type", "?")
            )
            table.add_row(
                name,
                cfg.base.get("tree", "?"),
                cfg.base.get("ref", "?"),
                str(len(cfg.patches)),
                rootfs_label,
                cfg.emulation.get("type", "?"),
                cfg.created_at[:10],
            )

        console.print()
        console.print(table)
        console.print()


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    LaunchConsole().run()
