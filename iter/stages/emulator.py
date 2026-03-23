from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from iter.pipeline import Stage, Context

console = Console()

# Kernel boot parameters appended to -append
_DEFAULT_BOOT_PARAMS = [
    "console=ttyS0",
    "nokaslr",
    "root=/dev/ram",
    "rdinit=/init",
]

# Map emulation target → QEMU binary
_QEMU_BIN = {
    "qemu-x86_64":  "qemu-system-x86_64",
    "qemu-aarch64": "qemu-system-aarch64",
}

# Default hardware accelerator per host platform
_DEFAULT_ACCEL = {
    "Linux":  "kvm",
    "Darwin": "hvf",   # Hypervisor.framework on macOS
}


class EmulatorStage(Stage):
    """
    Launch QEMU with the kernel image and rootfs resolved by previous stages.

    Reads from ctx.artifacts:
      "bzImage" → kernel image path
      "rootfs"  → rootfs image path
    """

    def run(self, ctx: Context) -> None:
        cfg    = ctx.iteration_config
        gc     = ctx.global_config
        emu    = cfg.emulation

        bzimage = ctx.artifacts.get("bzImage")
        rootfs  = ctx.artifacts.get("rootfs")

        if bzimage is None:
            raise RuntimeError("bzImage not found in artifacts — did BuildStage run?")
        if rootfs is None:
            raise RuntimeError("rootfs not found in artifacts — did RootfsStage run?")

        cmd = self._build_command(emu, bzimage, rootfs, cfg)
        self._print_command(cmd, gc)
        subprocess.run(cmd)   # interactive — inherits stdio

    # -- command builder -------------------------------------------------------

    def _build_command(
        self,
        emu: dict,
        bzimage: Path,
        rootfs: Path,
        cfg,
    ) -> list[str]:
        qemu_bin = _QEMU_BIN.get(emu["type"], emu["type"])
        accel    = _DEFAULT_ACCEL.get(platform.system(), "tcg")

        cmd: list[str] = [
            qemu_bin,
            "-nographic",
            "-accel", accel,
            "-cpu",   "host",
            "-m",     emu["memory"],
            "-smp",   str(emu["cpus"]),
            "-kernel", str(bzimage),
        ]

        # Attach rootfs — format detected by suffix
        suffix = rootfs.suffix.lower()
        if suffix in (".gz", ".cpio"):
            cmd += ["-initrd", str(rootfs)]
        elif suffix in (".img", ".ext4", ".qcow2"):
            cmd += ["-drive", f"file={rootfs},format={'qcow2' if suffix == '.qcow2' else 'raw'},if=virtio"]
        else:
            # Default: try initrd
            cmd += ["-initrd", str(rootfs)]

        # Boot parameters
        boot_params = list(_DEFAULT_BOOT_PARAMS)
        cmd += ["-append", " ".join(boot_params)]

        # GDB stub — always expose it; user connects with: gdb vmlinux → target remote :1234
        cmd += ["-s"]

        # Any extra args from config
        cmd += emu.get("extra_args", [])

        return cmd

    def _print_command(self, cmd: list[str], gc) -> None:
        pretty = " \\\n  ".join(cmd)
        console.print(Panel(
            f"[dim]{pretty}[/dim]",
            title="[bold]Launching QEMU[/bold]",
            expand=False,
        ))
        console.print("[dim]GDB stub listening on :1234  (gdb vmlinux → target remote :1234)[/dim]")
        console.print("[dim]QEMU monitor: Ctrl-a c[/dim]\n")
