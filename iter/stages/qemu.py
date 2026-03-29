"""QEMU build from source."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rich.console import Console

console = Console()

_QEMU_TARGETS = {
    "qemu-x86_64":  "x86_64-softmmu", # platform mappings
    "qemu-aarch64": "aarch64-softmmu",
}


class QemuBuilder:
    """Clone, configure, and compile QEMU from source.

    Supports out-of-tree builds: *source_dir* holds the git checkout,
    *build_dir* holds the configure/make output (separate per build).
    """

    def __init__(self, source_dir: Path, build_dir: Path, log_path: Path):
        self.source_dir = source_dir
        self.build_dir = build_dir
        self.log_path = log_path

    def sync(self, url: str, ref: str) -> None:
        """Clone (if needed), fetch, and checkout *ref*."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        if not (self.source_dir / ".git").exists():
            console.print(f"    Cloning QEMU from {url}...")
            self.source_dir.mkdir(parents=True, exist_ok=True)
            self._run_git(["clone", url, str(self.source_dir)])
        else:
            console.print("    Fetching QEMU updates...")
            self._run_git(["fetch", "--tags", "origin"], cwd=self.source_dir)

        console.print(f"    Checking out {ref}...")
        self._run_git(["checkout", ref], cwd=self.source_dir)

        # QEMU submodules (slirp, capstone, etc.)
        self._run_git(
            ["submodule", "update", "--init", "--recursive"],
            cwd=self.source_dir, check=False,
        )

    def configure(self, args: list[str], emu_type: str = "qemu-x86_64") -> None:
        """Run QEMU's ``configure`` in *build_dir*."""
        self.build_dir.mkdir(parents=True, exist_ok=True)

        target = _QEMU_TARGETS.get(emu_type, "x86_64-softmmu")
        cmd = [str(self.source_dir / "configure")]
        if not any("--target-list" in a for a in args):
            cmd.append(f"--target-list={target}")
        cmd += args

        console.print(f"    Configuring QEMU... (log → {self.log_path})")
        with open(self.log_path, "w") as log:
            result = subprocess.run(
                cmd, cwd=self.build_dir,
                stdout=log, stderr=subprocess.STDOUT,
            )
        if result.returncode != 0:
            console.print(f"[red]    QEMU configure failed. See {self.log_path}[/red]")
            raise RuntimeError("QEMU configure failed")

    def build(self, jobs: int = 0) -> None:
        """Compile QEMU."""
        effective_jobs = jobs or os.cpu_count() or 4

        console.print(f"    Building QEMU ({effective_jobs} jobs)...")
        with open(self.log_path, "a") as log:
            result = subprocess.run(
                ["make", f"-j{effective_jobs}"],
                cwd=self.build_dir,
                stdout=log, stderr=subprocess.STDOUT,
            )
        if result.returncode != 0:
            console.print(f"[red]    QEMU build failed. See {self.log_path}[/red]")
            raise RuntimeError("QEMU build failed")

    def binary(self, emu_type: str = "qemu-x86_64") -> Path:
        """Return the path to the built ``qemu-system-*`` binary."""
        target = _QEMU_TARGETS.get(emu_type, "x86_64-softmmu")
        arch = target.split("-")[0]
        name = f"qemu-system-{arch}"
        candidate = self.build_dir / name
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"QEMU binary not found: {candidate}")

    # -- internal ----------------------------------------------------------

    def _run_git(
        self,
        args: list[str],
        cwd: Path | None = None,
        check: bool = True,
    ):
        subprocess.run(
            ["git"] + args, cwd=cwd, check=check,
            capture_output=True, text=True,
        )
