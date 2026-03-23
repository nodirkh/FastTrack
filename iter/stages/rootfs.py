from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

from iter.pipeline import Stage, Context

console = Console()


class RootfsStage(Stage):
    """
    Resolves the rootfs image to use for this iteration.

    Shared:         verifies the image exists, writes path to ctx.artifacts.
    Per-iteration:  builds the image (buildroot / NixOS) then writes the path.

    Writes to ctx.artifacts:
      "rootfs" → Path to the rootfs image (cpio.gz, ext4, qcow2, …)
    """

    def run(self, ctx: Context) -> None:
        cfg    = ctx.iteration_config
        gc     = ctx.global_config
        rootfs = cfg.rootfs

        if rootfs.get("shared"):
            self._use_shared(rootfs, gc, ctx)
        else:
            self._build(rootfs, cfg, gc, ctx)

    def _use_shared(self, rootfs: dict, gc, ctx: Context) -> None:
        path = gc.root / rootfs["path"]
        if not path.exists():
            raise FileNotFoundError(
                f"Shared rootfs not found: {path}\n"
                "Add an image to rootfs/ or update config.yaml."
            )
        console.print(f"    Using shared rootfs: [cyan]{path.name}[/cyan]")
        ctx.artifacts["rootfs"] = path

    def _build(self, rootfs: dict, cfg, gc, ctx: Context) -> None:
        rtype    = rootfs.get("type", "")
        out_dir  = gc.iter_dir(cfg.name) / "build" / "rootfs"
        out_dir.mkdir(parents=True, exist_ok=True)

        dispatch = {
            "buildroot": self._build_buildroot,
            "nixos":     self._build_nixos,
            "initramfs": self._build_initramfs,
            "alpine":    self._fetch_alpine,
        }

        builder = dispatch.get(rtype)
        if builder is None:
            raise ValueError(f"Unknown rootfs type: {rtype!r}")

        image_path = builder(rootfs, cfg, gc, out_dir)
        ctx.artifacts["rootfs"] = image_path

    def _build_buildroot(self, rootfs: dict, cfg, gc, out_dir: Path) -> Path:
        config = rootfs.get("config")
        if not config or not Path(config).exists():
            raise FileNotFoundError(f"Buildroot config not found: {config}")
        console.print("    Building rootfs with [cyan]buildroot[/cyan]...")
        subprocess.run(
            ["make", f"O={out_dir}", f"BR2_DEFCONFIG={config}"],
            check=True,
        )
        image = out_dir / "images" / "rootfs.cpio.gz"
        if not image.exists():
            raise FileNotFoundError(f"Buildroot output not found: {image}")
        return image

    def _build_nixos(self, rootfs: dict, cfg, gc, out_dir: Path) -> Path:
        nix_config = rootfs.get("config")
        if not nix_config or not Path(nix_config).exists():
            raise FileNotFoundError(f"NixOS config not found: {nix_config}")
        console.print("    Building NixOS guest image...")
        subprocess.run(
            ["nixos-rebuild", "build-vm", "-I", f"nixos-config={nix_config}"],
            cwd=out_dir, check=True,
        )
        image = out_dir / "result" / "bin"
        return image

    # Must provide a pre-built cpio.gz or extend this method
    def _build_initramfs(self, rootfs: dict, cfg, gc, out_dir: Path) -> Path:
        image = out_dir / "rootfs.cpio.gz"
        if not image.exists():
            raise FileNotFoundError(
                f"initramfs not found at {image}.\n"
                "Build a busybox initramfs and place it there, or use a shared rootfs."
            )
        return image

    def _fetch_alpine(self, rootfs: dict, cfg, gc, out_dir: Path) -> Path:
        image = out_dir / "alpine.tar.gz"
        if not image.exists():
            raise FileNotFoundError(
                f"Alpine rootfs not found at {image}.\n"
                "Download from https://alpinelinux.org/downloads/ (Mini Root Filesystem)."
            )
        return image
