"""
Nested virtualization support.

Enables two-level VM stacks:
  Host (L0) → QEMU → Hypervisor (L1) → QEMU → Guest (L2) → payload

Hypervisor and Guest each have independent kernel configs and rootfs.
Both build from the same kernel tree (the iteration's base tree) but
into separate build directories with separate .config files.

Guest artifacts are shared into L1 via 9p virtfs.  A tiny cpio overlay
is appended to each rootfs to inject automated init scripts so the whole
nested launch happens unattended — output from both levels flows to the
host terminal tagged with [L1] / [L2] prefixes.

Requirements:
  - Both rootfs images must be initramfs format (cpio / cpio.gz).
  - The hypervisor rootfs must include QEMU, OR configure emulation.build
    on the guest to auto-build a static QEMU placed in the 9p share.
  - For KVM nested virt on the host: enable nested in the kvm module
    (echo 1 > /sys/module/kvm_intel/parameters/nested).
"""

from __future__ import annotations

import gzip
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel

from iter.config import QEMU_DEFAULT_URL
from iter.payload import Payload, create_payload
from iter.pipeline import Stage, Context
from iter.stages.build import KernelBuilder
from iter.stages.qemu import QemuBuilder

console = Console()

# ── Constants ─────────────────────────────────────────────────────────────

_QEMU_BIN = {
    "qemu-x86_64":  "qemu-system-x86_64",
    "qemu-aarch64": "qemu-system-aarch64",
}

_HOST_ACCEL = {
    "Linux":  "kvm",
    "Darwin": "hvf",
}

# Kernel configs auto-added to the hypervisor build so 9p sharing works.
_HYPERVISOR_9P_CONFIGS = [
    "CONFIG_NET_9P=y",
    "CONFIG_NET_9P_VIRTIO=y",
    "CONFIG_9P_FS=y",
    "CONFIG_VIRTIO=y",
    "CONFIG_VIRTIO_PCI=y",
]


# ── Minimal cpio newc generator ───────────────────────────────────────────
# Portable Python implementation — no external cpio binary needed.

def _cpio_entry(name: str, data: bytes, mode: int) -> bytes:
    """Build one cpio 'newc' entry (header + name + data, padded)."""
    name_bytes = name.encode("utf-8") + b"\x00"
    namesize = len(name_bytes)
    filesize = len(data)
    nlink = 2 if (mode & 0o170000) == 0o040000 else 1

    hdr = (
        f"070701"
        f"{0:08X}"          # ino
        f"{mode:08X}"
        f"{0:08X}"          # uid
        f"{0:08X}"          # gid
        f"{nlink:08X}"
        f"{0:08X}"          # mtime
        f"{filesize:08X}"
        f"{0:08X}"          # devmajor
        f"{0:08X}"          # devminor
        f"{0:08X}"          # rdevmajor
        f"{0:08X}"          # rdevminor
        f"{namesize:08X}"
        f"{0:08X}"          # check
    ).encode("ascii")

    buf = bytearray(hdr + name_bytes)
    buf += b"\x00" * ((4 - len(buf) % 4) % 4)      # pad header+name
    buf += data
    buf += b"\x00" * ((4 - filesize % 4) % 4)       # pad data
    return bytes(buf)


def _make_cpio(files: dict[str, str | bytes]) -> bytes:
    """Create a cpio newc archive.

    *files*: ``{path: content}`` — string values are UTF-8 encoded,
    all files are created as executable (0o100755), intermediate
    directories are added automatically.
    """
    buf = bytearray()

    # Collect intermediate directories
    dirs: set[str] = set()
    for p in files:
        parts = p.strip("/").split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))

    for d in sorted(dirs):
        buf += _cpio_entry(d, b"", mode=0o040755)

    for path, content in sorted(files.items()):
        raw = content.encode("utf-8") if isinstance(content, str) else content
        buf += _cpio_entry(path.lstrip("/"), raw, mode=0o100755)

    buf += _cpio_entry("TRAILER!!!", b"", mode=0)
    return bytes(buf)


def _concat_initramfs(base: Path, overlay: bytes, output: Path) -> None:
    """Append a cpio overlay (gzip-compressed) to a base initramfs.

    Linux unpacks concatenated cpio archives in order so overlay files
    take precedence over the base.
    """
    with open(output, "wb") as out:
        with open(base, "rb") as f:
            shutil.copyfileobj(f, out)
        out.write(gzip.compress(overlay))


# ── Guest (L2) ────────────────────────────────────────────────────────────

class Guest:
    """Nested guest (L2) — builds a kernel, resolves a rootfs, holds the payload."""

    def __init__(self, config: dict[str, Any], gc, iter_cfg):
        self.config = config
        self.gc = gc
        self.iter_cfg = iter_cfg
        self._bzimage: Path | None = None
        self._rootfs: Path | None = None
        self._payload: Payload | None = create_payload(
            config.get("payload"), gc, iter_cfg,
        )

    # ── properties ────────────────────────────────────────────────────────

    @property
    def build_path(self) -> Path:
        return self.gc.iter_dir(self.iter_cfg.name) / "build" / "guest"

    @property
    def log_path(self) -> Path:
        return self.gc.iter_dir(self.iter_cfg.name) / "logs" / "guest-build.log"

    @property
    def payload(self) -> Payload | None:
        return self._payload

    @property
    def emulation(self) -> dict[str, Any]:
        return self.config.get("emulation", {
            "type": "qemu-x86_64", "memory": "1G", "cpus": 2, "extra_args": [],
        })

    @property
    def bzimage(self) -> Path:
        if self._bzimage is None:
            raise RuntimeError("Guest kernel not built yet — call build() first")
        return self._bzimage

    @property
    def rootfs(self) -> Path:
        if self._rootfs is None:
            raise RuntimeError("Guest rootfs not resolved — call resolve_rootfs() first")
        return self._rootfs

    # ── actions ───────────────────────────────────────────────────────────

    def build(self, ctx: Context) -> Path:
        """Build the guest kernel. Returns bzImage path."""
        tree_path = self.gc.tree_path(self.iter_cfg.base["tree"])

        console.print("  [bold cyan]Guest kernel[/bold cyan]")
        builder = KernelBuilder(
            tree_path, self.build_path, self.log_path, self.iter_cfg.nix,
        )
        builder.configure(self.config["kernel"])
        self._bzimage = builder.build(self.config["kernel"].get("build_jobs", 0))

        ctx.artifacts["guest_bzImage"] = self._bzimage
        ctx.artifacts["guest_vmlinux"] = self.build_path / "vmlinux"
        console.print(f"    [green]Guest kernel:[/green] {self._bzimage.name}")
        return self._bzimage

    def resolve_rootfs(self, ctx: Context) -> Path:
        """Resolve the guest rootfs image (must be cpio/cpio.gz)."""
        rootfs_cfg = self.config.get("rootfs", {})
        if rootfs_cfg.get("shared"):
            path = self.gc.root / rootfs_cfg["path"]
            if not path.exists():
                raise FileNotFoundError(f"Guest rootfs not found: {path}")
            _check_initramfs_format(path)
            self._rootfs = path
            ctx.artifacts["guest_rootfs"] = path
            console.print(f"    Guest rootfs: [cyan]{path.name}[/cyan]")
            return path
        raise ValueError(
            "Per-iteration guest rootfs build not yet supported — use a shared rootfs"
        )

    def build_qemu(self, ctx: Context) -> Path | None:
        """Build a custom QEMU for the guest (runs inside L1).

        Defaults to ``--static`` so the binary works in any L1 rootfs
        without shared-library dependencies.  Returns the binary path,
        or *None* if ``emulation.build`` is not configured.
        """
        emu_build = self.emulation.get("build")
        if not emu_build:
            return None

        gc = self.gc
        emu_type = self.emulation.get("type", "qemu-x86_64")
        build_dir = gc.iter_dir(self.iter_cfg.name) / "build" / "qemu-guest"
        log_path  = gc.iter_dir(self.iter_cfg.name) / "logs" / "qemu-guest-build.log"

        url = emu_build.get("repo", QEMU_DEFAULT_URL)
        ref = emu_build["ref"]

        configure_args = list(emu_build.get("configure_args", []))
        if emu_build.get("static", True) and "--static" not in configure_args:
            configure_args.append("--static")

        console.print("  [bold cyan]Guest QEMU[/bold cyan]")
        builder = QemuBuilder(gc.qemu_dir, build_dir, log_path)
        builder.sync(url, ref)
        builder.configure(configure_args, emu_type)
        builder.build()

        binary = builder.binary(emu_type)
        ctx.artifacts["guest_qemu"] = binary
        console.print(f"    [green]Guest QEMU:[/green] {binary.name}")
        return binary


# ── Hypervisor (L1) ──────────────────────────────────────────────────────

class Hypervisor:
    """Hypervisor (L1) — builds a KVM-capable kernel, launches the nested stack."""

    def __init__(self, config: dict[str, Any], gc, iter_cfg):
        self.config = config
        self.gc = gc
        self.iter_cfg = iter_cfg
        self._bzimage: Path | None = None
        self._rootfs: Path | None = None

    # ── properties ────────────────────────────────────────────────────────

    @property
    def build_path(self) -> Path:
        return self.gc.iter_dir(self.iter_cfg.name) / "build" / "hypervisor"

    @property
    def log_path(self) -> Path:
        return self.gc.iter_dir(self.iter_cfg.name) / "logs" / "hypervisor-build.log"

    @property
    def emulation(self) -> dict[str, Any]:
        return self.config.get("emulation", {
            "type": "qemu-x86_64", "memory": "4G", "cpus": 4, "extra_args": [],
        })

    @property
    def bzimage(self) -> Path:
        if self._bzimage is None:
            raise RuntimeError("Hypervisor kernel not built — call build() first")
        return self._bzimage

    @property
    def rootfs(self) -> Path:
        if self._rootfs is None:
            raise RuntimeError("Hypervisor rootfs not resolved — call resolve_rootfs() first")
        return self._rootfs

    # ── actions ───────────────────────────────────────────────────────────

    def build(self, ctx: Context) -> Path:
        """Build the hypervisor kernel (9p configs auto-added)."""
        tree_path = self.gc.tree_path(self.iter_cfg.base["tree"])

        # Merge in 9p configs required for guest artifact sharing
        kernel_config = dict(self.config["kernel"])
        extra = list(kernel_config.get("extra_configs", []))
        for opt in _HYPERVISOR_9P_CONFIGS:
            key = opt.split("=")[0]
            if not any(key in e for e in extra):
                extra.append(opt)
        kernel_config["extra_configs"] = extra

        console.print("  [bold cyan]Hypervisor kernel[/bold cyan]")
        builder = KernelBuilder(
            tree_path, self.build_path, self.log_path, self.iter_cfg.nix,
        )
        builder.configure(kernel_config)
        self._bzimage = builder.build(kernel_config.get("build_jobs", 0))

        ctx.artifacts["hypervisor_bzImage"] = self._bzimage
        ctx.artifacts["hypervisor_vmlinux"] = self.build_path / "vmlinux"
        console.print(f"    [green]Hypervisor kernel:[/green] {self._bzimage.name}")
        return self._bzimage

    def resolve_rootfs(self, ctx: Context) -> Path:
        """Resolve hypervisor rootfs (must be cpio/cpio.gz)."""
        rootfs_cfg = self.config.get("rootfs", {})
        if rootfs_cfg.get("shared"):
            path = self.gc.root / rootfs_cfg["path"]
            if not path.exists():
                raise FileNotFoundError(
                    f"Hypervisor rootfs not found: {path}\n"
                    "Tip: the hypervisor rootfs should include QEMU, or\n"
                    "configure emulation.build on the guest to auto-include\n"
                    "a static binary via the 9p share."
                )
            _check_initramfs_format(path)
            self._rootfs = path
            ctx.artifacts["hypervisor_rootfs"] = path
            console.print(f"    Hypervisor rootfs: [cyan]{path.name}[/cyan]")
            return path
        raise ValueError(
            "Per-iteration hypervisor rootfs build not yet supported — use a shared rootfs"
        )

    def build_qemu(self, ctx: Context) -> Path | None:
        """Build a custom QEMU for the host (L0 → L1).

        Returns the binary path, or *None* if ``emulation.build`` is not
        configured.
        """
        emu_build = self.emulation.get("build")
        if not emu_build:
            return None

        gc = self.gc
        emu_type = self.emulation.get("type", "qemu-x86_64")
        build_dir = gc.iter_dir(self.iter_cfg.name) / "build" / "qemu-hypervisor"
        log_path  = gc.iter_dir(self.iter_cfg.name) / "logs" / "qemu-hypervisor-build.log"

        url = emu_build.get("repo", QEMU_DEFAULT_URL)
        ref = emu_build["ref"]

        configure_args = list(emu_build.get("configure_args", []))
        if emu_build.get("static", False) and "--static" not in configure_args:
            configure_args.append("--static")

        console.print("  [bold cyan]Hypervisor QEMU[/bold cyan]")
        builder = QemuBuilder(gc.qemu_dir, build_dir, log_path)
        builder.sync(url, ref)
        builder.configure(configure_args, emu_type)
        builder.build()

        binary = builder.binary(emu_type)
        ctx.artifacts["hypervisor_qemu"] = binary
        console.print(f"    [green]Hypervisor QEMU:[/green] {binary.name}")
        return binary

    def launch(self, ctx: Context, guest: Guest, use_tmux: bool = False) -> None:
        """Launch the full nested stack: L1 QEMU with guest inside.

        1. Prepare a 9p share dir with guest artifacts + payload + launch script.
        2. Append a tiny cpio overlay to the hypervisor rootfs (nested-init).
        3. Build and exec the L1 QEMU command (optionally inside tmux).
        """
        gc  = self.gc
        cfg = self.iter_cfg
        emu = self.emulation
        payload = guest.payload

        hypervisor_bzimage = ctx.artifacts["hypervisor_bzImage"]
        hypervisor_rootfs  = ctx.artifacts["hypervisor_rootfs"]

        needs_linux = not payload or payload.needs_guest_linux

        # ── 1. Prepare the 9p share directory ─────────────────────────────
        share_dir = gc.iter_dir(cfg.name) / "nested-share"
        if share_dir.exists():
            shutil.rmtree(share_dir)
        share_dir.mkdir(parents=True)

        # Guest kernel + rootfs (linux-mode payloads and no-payload)
        if needs_linux:
            guest_bzimage = ctx.artifacts["guest_bzImage"]
            guest_rootfs  = ctx.artifacts["guest_rootfs"]

            shutil.copy2(guest_bzimage, share_dir / "bzImage")

            overlay_files = payload.rootfs_overlay() if payload else None
            if overlay_files:
                overlay_cpio = _make_cpio(overlay_files)
                _concat_initramfs(guest_rootfs, overlay_cpio, share_dir / "rootfs")
            else:
                shutil.copy2(guest_rootfs, share_dir / "rootfs")

        # Custom guest QEMU → copy to share so L1 can use it
        guest_qemu: Path | None = ctx.artifacts.get("guest_qemu")
        if guest_qemu:
            shutil.copy2(guest_qemu, share_dir / guest_qemu.name)

        # Payload-specific artifacts (e.g. kvm-unit-tests .flat binaries)
        if payload:
            payload.inject(share_dir)

        # Guest launch script
        guest_emu = guest.emulation
        if guest_qemu:
            qemu_bin_guest = f"/nested/{guest_qemu.name}"
        else:
            qemu_bin_guest = _QEMU_BIN.get(
                guest_emu.get("type", "qemu-x86_64"), "qemu-system-x86_64",
            )
        extra = " ".join(guest_emu.get("extra_args", []))

        if payload:
            run_script = payload.run_guest_script(
                qemu_bin=qemu_bin_guest,
                memory=guest_emu.get("memory", "1G"),
                cpus=guest_emu.get("cpus", 2),
                extra_args=extra,
            )
        else:
            run_script = _make_run_guest_script(
                qemu_bin=qemu_bin_guest,
                memory=guest_emu.get("memory", "1G"),
                cpus=guest_emu.get("cpus", 2),
                rdinit="/init",
                extra_args=extra,
            )
        (share_dir / "run-guest.sh").write_text(run_script)

        # ── 2. Hypervisor rootfs with nested-init overlay ─────────────────
        nested_init = _make_nested_init()
        overlay = _make_cpio({"sbin/nested-init": nested_init})
        combined_rootfs = gc.iter_dir(cfg.name) / "build" / "hypervisor-rootfs.img"
        combined_rootfs.parent.mkdir(parents=True, exist_ok=True)
        _concat_initramfs(hypervisor_rootfs, overlay, combined_rootfs)

        # ── 3. Build L1 QEMU command ─────────────────────────────────────
        host_qemu: Path | None = ctx.artifacts.get("hypervisor_qemu")
        if host_qemu:
            qemu_bin_l1 = str(host_qemu)
        else:
            qemu_bin_l1 = _QEMU_BIN.get(
                emu.get("type", "qemu-x86_64"), "qemu-system-x86_64",
            )
        accel = _HOST_ACCEL.get(platform.system(), "tcg")

        # QEMU monitor on a unix socket (accessible from tmux or manually)
        monitor_sock = gc.iter_dir(cfg.name) / "monitor.sock"
        if monitor_sock.exists():
            monitor_sock.unlink()

        cmd: list[str] = [
            qemu_bin_l1,
            "-nographic",
            "-accel", accel,
            "-cpu",   "host",
            "-m",     emu.get("memory", "4G"),
            "-smp",   str(emu.get("cpus", 4)),
            "-kernel", str(hypervisor_bzimage),
            "-initrd", str(combined_rootfs),
            "-append", "console=ttyS0 nokaslr root=/dev/ram rdinit=/sbin/nested-init",
            # 9p share — guest artifacts visible at mount_tag "nested"
            "-virtfs",
            f"local,path={share_dir},mount_tag=nested,security_model=none,readonly=on",
            # QEMU monitor on unix socket
            "-monitor", f"unix:{monitor_sock},server,nowait",
            # GDB stub
            "-s",
        ]
        cmd += emu.get("extra_args", [])

        # ── 4. Launch ─────────────────────────────────────────────────────
        self._print_launch_info(cmd, guest, monitor_sock)

        if use_tmux:
            from iter.tmux import LaunchContext, tmux_launch
            serial_log = gc.iter_dir(cfg.name) / "logs" / "serial.log"
            log_files = sorted(
                f for f in (gc.iter_dir(cfg.name) / "logs").glob("*.log")
                if f.name != "serial.log"
            )
            tmux_launch(LaunchContext(
                qemu_cmd=cmd,
                iter_dir=gc.iter_dir(cfg.name),
                session_name=f"ft-{cfg.name}",
                monitor_sock=monitor_sock,
                serial_log=serial_log,
                log_files=log_files,
                iteration_name=cfg.name,
                payload_description=(
                    guest.payload.description if guest.payload else None
                ),
            ))
        else:
            subprocess.run(cmd)   # interactive — inherits stdio

    def _print_launch_info(
        self, cmd: list[str], guest: Guest, monitor_sock: Path | None = None,
    ) -> None:
        pretty = " \\\n  ".join(cmd)
        console.print(Panel(
            f"[dim]{pretty}[/dim]",
            title="[bold]Launching Nested VM Stack[/bold]",
            expand=False,
        ))
        console.print(
            "[dim]L1 GDB:   target remote :1234[/dim]"
        )
        if monitor_sock:
            console.print(
                f"[dim]Monitor:  socat -,raw,echo=0 unix-connect:{monitor_sock}[/dim]"
            )
        console.print("[dim]Exit:     Ctrl-a x[/dim]")
        if guest.payload:
            console.print(f"[dim]Payload: {guest.payload.description}[/dim]")
        if platform.system() == "Darwin":
            console.print(
                "[yellow]Note: macOS HVF does not support nested virt — "
                "L1→L2 will fall back to TCG (slow).[/yellow]"
            )
        console.print()


# ── Script generators ─────────────────────────────────────────────────────

def _make_nested_init() -> str:
    """L1 init script: mounts 9p share, launches the guest, drops to shell."""
    return """\
#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

mount -t proc     proc     /proc 2>/dev/null
mount -t sysfs    sysfs    /sys  2>/dev/null
mount -t devtmpfs devtmpfs /dev  2>/dev/null

echo ""
echo "============================================="
echo "  [L1] Hypervisor booted — $(uname -r)"
echo "============================================="
echo ""

# Mount guest artifacts via 9p
mkdir -p /nested
if ! mount -t 9p -o trans=virtio,version=9p2000.L nested /nested 2>/dev/null; then
    echo "[L1] ERROR: Failed to mount 9p share"
    echo "[L1] Ensure CONFIG_9P_FS=y in the hypervisor kernel"
    echo "[L1] Dropping to shell..."
    exec /bin/sh
fi

if [ ! -f /nested/run-guest.sh ]; then
    echo "[L1] ERROR: /nested/run-guest.sh not found"
    echo "[L1] Dropping to shell..."
    exec /bin/sh
fi

if [ -c /dev/kvm ]; then
    echo "[L1] KVM available"
else
    echo "[L1] WARNING: /dev/kvm not available — guest will use TCG (slow)"
fi

echo "[L1] Launching nested guest..."
echo ""

sh /nested/run-guest.sh
RC=$?

echo ""
echo "============================================="
echo "  [L1] Guest exited (rc=$RC)"
echo "============================================="
echo ""
echo "[L1] Dropping to shell (Ctrl-a x to exit QEMU)"
exec /bin/sh
"""


def _make_run_guest_script(
    qemu_bin: str,
    memory: str,
    cpus: int,
    rdinit: str,
    extra_args: str,
) -> str:
    """L1 helper: launch the L2 guest QEMU."""
    return f"""\
#!/bin/sh
# Auto-generated — launch nested guest (L2)

QEMU_BIN="{qemu_bin}"

if ! command -v "$QEMU_BIN" >/dev/null 2>&1; then
    echo "[L1] ERROR: $QEMU_BIN not found in hypervisor rootfs"
    echo "[L1] The hypervisor rootfs must include QEMU."
    exit 1
fi

ACCEL=""
if [ -c /dev/kvm ]; then
    ACCEL="-enable-kvm -cpu host"
fi

$QEMU_BIN \\
    -nographic \\
    $ACCEL \\
    -m {memory} \\
    -smp {cpus} \\
    -kernel /nested/bzImage \\
    -initrd /nested/rootfs \\
    -append "console=ttyS0 nokaslr root=/dev/ram rdinit={rdinit}" \\
    {extra_args}
"""


# ── Helpers ───────────────────────────────────────────────────────────────

def _check_initramfs_format(path: Path) -> None:
    """Raise if *path* doesn't look like a cpio / cpio.gz initramfs."""
    suffix = path.suffix.lower()
    # Accept .gz, .cpio, .cpio.gz (via .gz), .img treated as initrd
    if suffix in (".gz", ".cpio"):
        return
    # Some rootfs images are just named rootfs.img but are actually cpio
    if suffix == ".img":
        return
    raise ValueError(
        f"Nested mode requires initramfs (cpio/cpio.gz) rootfs, got: {path.name}\n"
        "Disk images (.ext4, .qcow2) are not supported for nested virtualization."
    )


# ── Pipeline stages ──────────────────────────────────────────────────────

class NestedBuildStage(Stage):
    """Build kernels, resolve rootfs, and prepare payloads for nested VMs."""

    def run(self, ctx: Context) -> None:
        cfg = ctx.iteration_config
        gc  = ctx.global_config
        nested = cfg.nested
        if not nested:
            raise RuntimeError("No nested config in iteration — is this a nested iteration?")

        hypervisor = Hypervisor(nested["hypervisor"], gc, cfg)
        guest = Guest(nested["guest"], gc, cfg)
        payload = guest.payload

        # ── Kernels ───────────────────────────────────────────────────────
        console.print("\n[bold]Building nested VM kernels[/bold]")
        hypervisor.build(ctx)

        needs_linux = not payload or payload.needs_guest_linux
        if needs_linux:
            guest.build(ctx)
        else:
            console.print("  [dim](guest kernel skipped — bare-metal payload)[/dim]")

        # ── QEMU ─────────────────────────────────────────────────────────
        if hypervisor.emulation.get("build") or guest.emulation.get("build"):
            console.print("\n[bold]Building QEMU[/bold]")
            hypervisor.build_qemu(ctx)
            guest.build_qemu(ctx)

        # ── Rootfs ───────────────────────────────────────────────────────
        console.print("\n[bold]Resolving rootfs images[/bold]")
        hypervisor.resolve_rootfs(ctx)
        if needs_linux:
            guest.resolve_rootfs(ctx)
        else:
            console.print("  [dim](guest rootfs skipped — bare-metal payload)[/dim]")

        # ── Payload ──────────────────────────────────────────────────────
        if payload:
            console.print("\n[bold]Building payload[/bold]")
            payload_build = gc.iter_dir(cfg.name) / "build" / "payload"
            payload_log   = gc.iter_dir(cfg.name) / "logs" / "payload-build.log"
            payload.build(payload_build, payload_log)

        # Pass objects to the emulator stage
        ctx.artifacts["_hypervisor"] = hypervisor
        ctx.artifacts["_guest"] = guest


class NestedEmulatorStage(Stage):
    """Launch the nested VM stack: Host → Hypervisor (L1) → Guest (L2)."""

    def run(self, ctx: Context) -> None:
        hypervisor: Hypervisor | None = ctx.artifacts.get("_hypervisor")
        guest: Guest | None = ctx.artifacts.get("_guest")
        if not hypervisor or not guest:
            raise RuntimeError(
                "Hypervisor/Guest objects not found — did NestedBuildStage run?"
            )
        use_tmux = ctx.artifacts.get("_use_tmux", False)
        hypervisor.launch(ctx, guest, use_tmux=use_tmux)
