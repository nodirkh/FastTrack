from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import questionary
import yaml
from rich.console import Console

console = Console()

ROOT = Path(__file__).parent.parent

TREE_URLS = {
    "upstream": "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
    "stable":   "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
    "kvm": "https://git.kernel.org/pub/scm/virt/kvm/kvm.git",
}

QEMU_DEFAULT_URL = "https://gitlab.com/qemu-project/qemu.git"


# ---------------------------------------------------------------------------
# GlobalConfig
# ---------------------------------------------------------------------------

@dataclass
class GlobalConfig:
    """Project-level paths and settings, constant across all iterations."""
    root: Path = field(default_factory=lambda: ROOT)

    @property
    def base_dir(self) -> Path:
        return self.root / "base"

    @property
    def iterations_dir(self) -> Path:
        return self.root / "iterations"

    @property
    def rootfs_dir(self) -> Path:
        return self.root / "rootfs"

    @property
    def qemu_dir(self) -> Path:
        return self.base_dir / "qemu"

    def tree_path(self, tree: str) -> Path:
        return self.base_dir / tree

    def tree_url(self, tree: str) -> str:
        return TREE_URLS[tree]

    def iter_dir(self, name: str) -> Path:
        return self.iterations_dir / name

    def iter_config_path(self, name: str) -> Path:
        return self.iter_dir(name) / "config.yaml"


# ---------------------------------------------------------------------------
# IterationConfig
# ---------------------------------------------------------------------------

@dataclass
class IterationConfig:
    """Per-iteration config, locked at creation time."""
    name: str
    created_at: str
    base_commit: str
    base: dict[str, str]          # {"tree": "upstream", "ref": "v6.14-rc5"}
    patches: list[str]            # relative paths from project root
    kernel: dict[str, Any]        # base_config, extra_configs, build_jobs
    rootfs: dict[str, Any]        # shared, path | type, config
    emulation: dict[str, Any]     # type, memory, cpus, extra_args
    nix: dict[str, Any]           # enabled, flake?
    nested: dict[str, Any] | None = None  # hypervisor + guest config (None = single-VM mode)


# ---------------------------------------------------------------------------
# ConfigParser
# ---------------------------------------------------------------------------

class ConfigParser:
    @staticmethod
    def load(path: Path) -> IterationConfig:
        with open(path) as f:
            data = yaml.safe_load(f)
        return IterationConfig(**data)

    @staticmethod
    def save(config: IterationConfig, path: Path) -> None:
        with open(path, "w") as f:
            yaml.dump(config.__dict__, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# ConfigBuilder  (interactive wizard → produces a locked IterationConfig)
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path | None = None, check: bool = True):
    return subprocess.run(
        ["git"] + args, cwd=cwd, check=check, capture_output=True, text=True,
    )


class ConfigBuilder:

    def __init__(self, gc: GlobalConfig):
        self.gc = gc

    def build(self) -> IterationConfig | None:
        """Run the interactive wizard. Returns None if the user cancels."""
        name = questionary.text(
            "Iteration name:",
            validate=lambda v: True if v.strip() else "Name cannot be empty",
        ).ask()
        if not name:
            return None

        if self.gc.iter_dir(name).exists():
            console.print(f"[red]Iteration '{name}' already exists.[/red]")
            return None

        tree = questionary.select("Base tree:", choices=["upstream", "stable"]).ask()
        if not tree:
            return None

        ref, base_commit = self._ask_ref(tree)
        if ref is None:
            return None

        kernel    = self._ask_kernel()
        rootfs    = self._ask_rootfs()
        emulation = self._ask_emulation()
        nix       = self._ask_nix()

        if any(v is None for v in (kernel, rootfs, emulation, nix)):
            return None

        return IterationConfig(
            name=name,
            created_at=datetime.now().isoformat(timespec="seconds"),
            base_commit=base_commit,
            base={"tree": tree, "ref": ref},
            patches=[],
            kernel=kernel,
            rootfs=rootfs,
            emulation=emulation,
            nix=nix,
        )

    # -- helpers ---------------------------------------------------------------

    def _ask_ref(self, tree: str) -> tuple[str | None, str | None]:
        tree_path = self.gc.tree_path(tree)
        tags = _git(["tag", "--sort=-version:refname"], cwd=tree_path).stdout.strip().splitlines()[:30]
        ref = questionary.select(
            "Base ref / tag:", choices=tags + ["HEAD (latest commit)"]
        ).ask()
        if not ref:
            return None, None
        if ref == "HEAD (latest commit)":
            ref = "HEAD"
        commit = _git(["rev-parse", ref], cwd=tree_path).stdout.strip()
        return ref, commit

    def _ask_kernel(self) -> dict | None:
        base_config = questionary.select(
            "Kernel base config:",
            choices=["defconfig", "kvm_guest.config", "custom path"],
        ).ask()
        if not base_config:
            return None
        if base_config == "custom path":
            base_config = questionary.path("Path to .config:").ask()
            if not base_config:
                return None

        extra_raw = questionary.text(
            "Extra CONFIG options (space-separated, e.g. CONFIG_KVM=y):", default=""
        ).ask()
        extra = extra_raw.split() if extra_raw and extra_raw.strip() else []
        return {"base_config": base_config, "extra_configs": extra, "build_jobs": 0}

    def _ask_rootfs(self) -> dict | None:
        use_shared = questionary.confirm("Use a shared rootfs from rootfs/?", default=True).ask()
        if use_shared is None:
            return None

        if use_shared:
            files = (
                [f.name for f in self.gc.rootfs_dir.iterdir() if f.is_file()]
                if self.gc.rootfs_dir.exists() else []
            )
            if not files:
                console.print("[yellow]No images in rootfs/ — add one and update config.yaml later.[/yellow]")
                path = questionary.text("Rootfs path (relative to project root):", default="rootfs/").ask()
            else:
                choice = questionary.select("Select rootfs image:", choices=files).ask()
                if not choice:
                    return None
                path = f"rootfs/{choice}"
            return {"shared": True, "path": path}

        rootfs_type = questionary.select(
            "Rootfs type:", choices=["initramfs", "buildroot", "alpine", "nixos"]
        ).ask()
        if not rootfs_type:
            return None
        return {"shared": False, "type": rootfs_type, "config": None}

    def _ask_emulation(self) -> dict | None:
        emu_type = questionary.select(
            "Emulation target:", choices=["qemu-x86_64", "qemu-aarch64"]
        ).ask()
        if not emu_type:
            return None
        memory = questionary.text("Memory:", default="2G").ask()
        cpus   = questionary.text("vCPUs:", default="4").ask()
        return {"type": emu_type, "memory": memory, "cpus": int(cpus), "extra_args": []}

    def _ask_nix(self) -> dict | None:
        use_nix = questionary.confirm("Use Nix dev shell for building?", default=False).ask()
        if use_nix is None:
            return None
        cfg: dict[str, Any] = {"enabled": use_nix}
        if use_nix:
            flake = questionary.text("Flake ref:", default=".#devShell").ask()
            if not flake:
                return None
            cfg["flake"] = flake
        return cfg
