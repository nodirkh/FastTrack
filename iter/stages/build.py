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

        build_dir.mkdir(exist_ok=True)
        log_path.parent.mkdir(exist_ok=True)

        self._apply_base_config(cfg.kernel["base_config"], tree_path, build_dir)
        self._apply_extra_configs(cfg.kernel.get("extra_configs", []), tree_path, build_dir)

        console.print(f"    Building... (log → {log_path.relative_to(gc.root)})")
        self._build(cfg, tree_path, build_dir, log_path)

        bzimage = self._find_image(build_dir)
        if bzimage is None:
            raise FileNotFoundError(f"Kernel image not found in {build_dir} after build")

        ctx.artifacts["bzImage"] = bzimage
        ctx.artifacts["vmlinux"] = build_dir / "vmlinux"
        console.print(f"    [green]Built:[/green] {bzimage.relative_to(gc.root)}")

    # -- helpers ---------------------------------------------------------------

    def _apply_base_config(self, base_config: str, tree_path: Path, build_dir: Path) -> None:
        config_path = Path(base_config)
        if config_path.exists() and base_config.endswith(".config"):
            # User supplied a full .config file
            shutil.copy(config_path, build_dir / ".config")
            subprocess.run(
                ["make", f"O={build_dir}", "olddefconfig"],
                cwd=tree_path, check=True, capture_output=True,
            )
        else:
            # Named target e.g. "defconfig", "kvm_guest.config"
            subprocess.run(
                ["make", f"O={build_dir}", base_config],
                cwd=tree_path, check=True, capture_output=True,
            )

    def _apply_extra_configs(self, extra: list[str], tree_path: Path, build_dir: Path) -> None:
        if not extra:
            return
        config_file = build_dir / ".config"
        for opt in extra:
            # Accept both "CONFIG_KVM=y" and "CONFIG_KVM"
            key = opt.split("=")[0].removeprefix("CONFIG_")
            val = opt.split("=")[1] if "=" in opt else "y"
            flag = "--enable" if val == "y" else "--disable" if val in ("n", "N") else "--set-val"
            args = ["scripts/config", f"--file={config_file}", flag, key]
            if flag == "--set-val":
                args.append(val)
            subprocess.run(args, cwd=tree_path, check=False, capture_output=True)

        # Re-resolve dependencies after manual config changes
        subprocess.run(
            ["make", f"O={build_dir}", "olddefconfig"],
            cwd=tree_path, check=True, capture_output=True,
        )

    def _build(self, cfg, tree_path: Path, build_dir: Path, log_path: Path) -> None:
        jobs = cfg.kernel.get("build_jobs", 0) or os.cpu_count() or 4
        make = f"make O={build_dir} -j{jobs} bzImage"

        nix = cfg.nix
        if nix.get("enabled"):
            flake = nix.get("flake", ".#devShell")
            cmd = f"nix develop {flake} --command bash -c '{make}'"
        else:
            cmd = make

        with open(log_path, "w") as log:
            result = subprocess.run(
                cmd, cwd=tree_path, shell=True,
                stdout=log, stderr=subprocess.STDOUT,
            )

        if result.returncode != 0:
            console.print(f"[red]    Build failed. See {log_path}[/red]")
            raise RuntimeError("Kernel build failed")

    def _find_image(self, build_dir: Path) -> Path | None:
        for rel in _BZIMAGE_PATHS:
            candidate = build_dir / rel
            if candidate.exists():
                return candidate
        return None
