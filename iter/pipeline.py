from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from iter.config import GlobalConfig, IterationConfig

from rich.console import Console

console = Console()

# pass info from stage to stage
@dataclass
class Context:
    """Shared mutable state threaded through all stages in a pipeline run."""
    global_config: GlobalConfig
    iteration_name: str | None = None
    iteration_config: IterationConfig | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)


class Stage(ABC):
    """Base class for all pipeline stages."""

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def run(self, ctx: Context) -> None:
        """Execute this stage. Raise on failure."""
        ...

    # used to undo git am mostly
    def rollback(self, ctx: Context) -> None:
        """Undo side-effects of run()"""
        pass


class Pipeline:
    """Runs a sequence of stages, rolling back completed ones on failure."""

    def __init__(self, stages: list[Stage]):
        self.stages = stages

    def run(self, ctx: Context) -> None:
        completed: list[Stage] = []

        for stage in self.stages:
            console.print(f"  [dim]▶ {stage.name}[/dim]")
            try:
                stage.run(ctx)
                completed.append(stage)
            except Exception as exc:
                console.print(f"\n[red]✗ {stage.name} failed:[/red] {exc}")
                if completed:
                    console.print("[yellow]Rolling back...[/yellow]")
                    for s in reversed(completed):
                        try:
                            s.rollback(ctx)
                        except Exception as rb_exc:
                            console.print(f"[red]  Rollback failed for {s.name}: {rb_exc}[/red]")
                raise
