from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from rich.console import Console

from iter.pipeline import Stage, Context

console = Console()


# Linux kernel artifacts
_BZIMAGE_PATHS = [
    Path("arch/x86/boot/bzImage"),
    Path("arch/arm64/boot/Image"),
    Path("arch/arm64/boot/Image.gz"),
]


class KernelBuilder:
    """Reusable kernel build logic — configure and compile a Linux kernel.

    Used by BuildStage (single-kernel iterations) and by Hypervisor/Guest
    (nested virtualization) to avoid duplicating build mechanics.
    """

    def __init__(
        self,
        tree_path: Path,
        build_dir: Path,
        log_path: Path,
        nix: dict | None = None,
    ):
        self.tree_path = tree_path
        self.build_dir = build_dir
        self.log_path = log_path
        self.nix = nix or {}

    def configure(self, kernel_config: dict) -> None:
        """Apply base config and extra CONFIG options."""
        self.build_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._apply_base_config(kernel_config["base_config"])
        self._apply_extra_configs(kernel_config.get("extra_configs", []))

    def build(self, jobs: int = 0) -> Path:
        """Compile the kernel. Returns path to the built image."""
        effective_jobs = jobs or os.cpu_count() or 4
        make = f"make O={self.build_dir} -j{effective_jobs} bzImage"

        if self.nix.get("enabled"):
            flake = self.nix.get("flake", ".#devShell")
            cmd = f"nix develop {flake} --command bash -c '{make}'"
        else:
            cmd = make

        console.print(f"    Building... (log → {self.log_path})")

        with open(self.log_path, "w") as log:
            result = subprocess.run(
                cmd, cwd=self.tree_path, shell=True,
                stdout=log, stderr=subprocess.STDOUT,
            )

        if result.returncode != 0:
            console.print(f"[red]    Build failed. See {self.log_path}[/red]")
            raise RuntimeError("Kernel build failed")

        bzimage = self._find_image()
        if bzimage is None:
            raise FileNotFoundError(f"Kernel image not found in {self.build_dir}")
        return bzimage

    # -- helpers ---------------------------------------------------------------

    def _apply_base_config(self, base_config: str) -> None:
        config_path = Path(base_config)
        if config_path.exists() and base_config.endswith(".config"):
            # User supplied a full .config file
            shutil.copy(config_path, self.build_dir / ".config")
            subprocess.run(
                ["make", f"O={self.build_dir}", "olddefconfig"],
                cwd=self.tree_path, check=True, capture_output=True,
            )
        else:
            # Named target e.g. "defconfig", "kvm_guest.config"
            subprocess.run(
                ["make", f"O={self.build_dir}", base_config],
                cwd=self.tree_path, check=True, capture_output=True,
            )

    def _apply_extra_configs(self, extra: list[str]) -> None:
        if not extra:
            return
        config_file = self.build_dir / ".config"
        for opt in extra:
            # Accept both "CONFIG_KVM=y" and "CONFIG_KVM"
            key = opt.split("=")[0].removeprefix("CONFIG_")
            val = opt.split("=")[1] if "=" in opt else "y"
            flag = "--enable" if val == "y" else "--disable" if val in ("n", "N") else "--set-val"
            args = ["scripts/config", f"--file={config_file}", flag, key]
            if flag == "--set-val":
                args.append(val)
            subprocess.run(args, cwd=self.tree_path, check=False, capture_output=True)

        # Re-resolve dependencies after manual config changes
        subprocess.run(
            ["make", f"O={self.build_dir}", "olddefconfig"],
            cwd=self.tree_path, check=True, capture_output=True,
        )

    def _find_image(self) -> Path | None:
        for rel in _BZIMAGE_PATHS:
            candidate = self.build_dir / rel
            if candidate.exists():
                return candidate
        return None


class BuildStage(Stage):
    """
    Apply kernel config and build bzImage (or equivalent) into
    iterations/<name>/build/ using make O=<build_dir>.

    Writes to ctx.artifacts:
      "bzImage" → Path to the kernel image
      "vmlinux" → Path to the unstripped ELF (for GDB)
    """

    def run(self, ctx: Context) -> None:
        cfg       = ctx.iteration_config
        gc        = ctx.global_config
        tree_path = gc.tree_path(cfg.base["tree"])
        build_dir = gc.iter_dir(cfg.name) / "build"
        log_path  = gc.iter_dir(cfg.name) / "logs" / "build.log"

        builder = KernelBuilder(tree_path, build_dir, log_path, cfg.nix)
        builder.configure(cfg.kernel)
        bzimage = builder.build(cfg.kernel.get("build_jobs", 0))

        ctx.artifacts["bzImage"] = bzimage
        ctx.artifacts["vmlinux"] = build_dir / "vmlinux"
        console.print(f"    [green]Built:[/green] {bzimage.relative_to(gc.root)}")
