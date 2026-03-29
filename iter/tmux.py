"""
Tmux session manager for FastTrack nested VM launches.

Creates a multi-window tmux session:
  vm       — QEMU serial console (interactive)
  monitor  — QEMU monitor (via unix socket + socat)
  logs     — tail -F on build / payload logs
  L1       — filtered serial output: hypervisor only
  L2       — filtered serial output: guest / payload only

Serial output is captured to a log file via the ``script`` command
so the watcher can filter by [L1] / [L2] tags.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

console = Console()


# ── TmuxSession (thin wrapper) ───────────────────────────────────────────

class TmuxSession:
    """Thin wrapper around the tmux CLI."""

    def __init__(self, name: str):
        self.name = name

    # -- availability / state ──────────────────────────────────────────────

    @staticmethod
    def available() -> bool:
        return shutil.which("tmux") is not None

    def exists(self) -> bool:
        if not self.available():
            return False
        return subprocess.run(
            ["tmux", "has-session", "-t", self.name],
            capture_output=True,
        ).returncode == 0

    # -- session management ────────────────────────────────────────────────

    def create(self, window_name: str = "main", cmd: str = "") -> None:
        """Create a new **detached** session."""
        args = ["tmux", "new-session", "-d", "-s", self.name, "-n", window_name]
        if cmd:
            args.append(cmd)
        subprocess.run(args, check=True)

    def set_option(self, option: str, value: str) -> None:
        subprocess.run(
            ["tmux", "set-option", "-t", self.name, option, value],
            check=True,
        )

    def kill(self) -> None:
        subprocess.run(
            ["tmux", "kill-session", "-t", self.name],
            capture_output=True,
        )

    def attach(self) -> None:
        """Attach to the session (blocks until detach)."""
        subprocess.run(["tmux", "attach-session", "-t", self.name])

    # -- windows / panes ───────────────────────────────────────────────────

    def new_window(self, name: str, cmd: str = "") -> None:
        args = ["tmux", "new-window", "-t", self.name, "-n", name]
        if cmd:
            args.append(cmd)
        subprocess.run(args, check=True)

    def split_h(self, target: str, cmd: str = "", percent: int = 50) -> None:
        """Split horizontally (top / bottom)."""
        args = [
            "tmux", "split-window", "-v",
            "-t", f"{self.name}:{target}", "-p", str(percent),
        ]
        if cmd:
            args.append(cmd)
        subprocess.run(args, check=True)

    def split_v(self, target: str, cmd: str = "", percent: int = 50) -> None:
        """Split vertically (left / right)."""
        args = [
            "tmux", "split-window", "-h",
            "-t", f"{self.name}:{target}", "-p", str(percent),
        ]
        if cmd:
            args.append(cmd)
        subprocess.run(args, check=True)

    def send_keys(self, target: str, keys: str, enter: bool = True) -> None:
        args = ["tmux", "send-keys", "-t", f"{self.name}:{target}", keys]
        if enter:
            args.append("Enter")
        subprocess.run(args, check=True)

    def select_window(self, name: str) -> None:
        subprocess.run(
            ["tmux", "select-window", "-t", f"{self.name}:{name}"],
            check=True,
        )

    def select_layout(self, target: str, layout: str) -> None:
        subprocess.run(
            ["tmux", "select-layout", "-t", f"{self.name}:{target}", layout],
            check=True,
        )


# ── Launch context ───────────────────────────────────────────────────────

@dataclass
class LaunchContext:
    """Everything needed to create the FastTrack tmux session."""
    qemu_cmd: list[str]
    iter_dir: Path
    session_name: str
    monitor_sock: Path
    serial_log: Path
    log_files: list[Path] = field(default_factory=list)
    iteration_name: str = ""
    payload_description: str | None = None


# ── Main launcher ────────────────────────────────────────────────────────

def tmux_launch(lctx: LaunchContext) -> None:
    """Create a tmux session with the full FastTrack layout and attach."""

    s = TmuxSession(lctx.session_name)

    if s.exists():
        console.print(f"[yellow]Killing existing tmux session '{lctx.session_name}'...[/yellow]")
        s.kill()

    # Ensure paths exist
    lctx.serial_log.parent.mkdir(parents=True, exist_ok=True)
    lctx.serial_log.touch()
    for lf in lctx.log_files:
        lf.parent.mkdir(parents=True, exist_ok=True)
        lf.touch()

    # ── Generate the VM launcher script ───────────────────────────────
    launch_script = lctx.iter_dir / "launch-vm.sh"
    _write_launch_script(launch_script, lctx.qemu_cmd, lctx.iteration_name)

    # Wrap with `script` for serial logging
    vm_cmd = _wrap_with_script(launch_script, lctx.serial_log)
    vm_cmd += "; echo ''; echo 'VM exited.  Ctrl-b d to detach.'; exec bash"

    # ── Window 0: vm (serial console) ─────────────────────────────────
    s.create(window_name="vm", cmd=vm_cmd)
    s.set_option("remain-on-exit", "on")

    # ── Window 1: monitor ─────────────────────────────────────────────
    has_socat = shutil.which("socat") is not None
    if has_socat:
        sock = lctx.monitor_sock
        monitor_cmd = (
            f"echo 'Waiting for QEMU monitor socket...'; "
            f"while [ ! -S {sock} ]; do sleep 0.5; done; "
            f"echo 'Connected.'; echo ''; "
            f"socat -,raw,echo=0 unix-connect:{sock}"
        )
    else:
        monitor_cmd = (
            "echo 'socat not found — install it for QEMU monitor access:'; "
            "echo '  brew install socat   (macOS)'; "
            "echo '  apt install socat    (Linux)'; "
            "exec bash"
        )
    s.new_window("monitor", monitor_cmd)

    # ── Window 2: logs ────────────────────────────────────────────────
    if lctx.log_files:
        files_str = " ".join(str(f) for f in lctx.log_files)
        s.new_window("logs", f"tail -F {files_str}")

    # ── Window 3 & 4: L1 / L2 filtered serial ────────────────────────
    python = _find_python()
    watcher = Path(__file__).parent / "watcher.py"

    if watcher.exists():
        serial = str(lctx.serial_log)
        s.new_window("L1", f"{python} {watcher} {serial} --level L1")
        s.new_window("L2", f"{python} {watcher} {serial} --level L2")

    # ── Focus on vm and attach ────────────────────────────────────────
    s.select_window("vm")

    console.print()
    console.print(f"[bold green]tmux session:[/bold green] {lctx.session_name}")
    console.print("[dim]  Windows: vm | monitor | logs | L1 | L2[/dim]")
    console.print("[dim]  Switch:  Ctrl-b <number>  or  Ctrl-b n/p[/dim]")
    console.print("[dim]  Detach:  Ctrl-b d    Kill: Ctrl-b & (y)[/dim]")
    if lctx.payload_description:
        console.print(f"[dim]  Payload: {lctx.payload_description}[/dim]")
    console.print()

    s.attach()


# ── Helpers ──────────────────────────────────────────────────────────────

def _write_launch_script(
    path: Path, qemu_cmd: list[str], iteration_name: str,
) -> None:
    """Write a shell script that execs the QEMU command."""
    cmd_str = " \\\n    ".join(shlex.quote(str(a)) for a in qemu_cmd)
    path.write_text(
        f"#!/bin/sh\n"
        f"echo '━━━ FastTrack: {iteration_name} ━━━'\n"
        f"echo ''\n"
        f"exec {cmd_str}\n"
    )
    path.chmod(0o755)


def _wrap_with_script(launch_script: Path, serial_log: Path) -> str:
    """Wrap a command with ``script`` for serial logging.

    macOS:  script -q <file> <cmd>
    Linux:  script -qfc <cmd> <file>
    """
    ls = str(launch_script)
    sl = str(serial_log)
    if platform.system() == "Darwin":
        return f"script -q {shlex.quote(sl)} {shlex.quote(ls)}"
    else:
        return f"script -qfc {shlex.quote(ls)} {shlex.quote(sl)}"


def _find_python() -> str:
    """Find the project's venv python, falling back to python3."""
    venv = Path(__file__).parent.parent / "venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    return "python3"
