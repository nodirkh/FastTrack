"""
Payload system for nested virtualization.

A Payload is something to execute inside the nested VM stack.
Two execution modes:

- **linux**: Runs inside a guest Linux (needs kernel + rootfs).
  The payload is injected into the rootfs via cpio overlay.
- **bare**: Replaces the guest kernel entirely.  kvm-unit-tests
  .flat binaries are loaded directly by QEMU as ``-kernel``.

Built-in types:
  - script:          Inline shell commands (linux mode)
  - binary:          Pre-built executable (linux mode)
  - kvm-unit-tests:  Build & run kvm-unit-tests (bare mode)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console()


# ── Abstract base ────────────────────────────────────────────────────────

class Payload(ABC):
    """Abstract base for all payload types."""

    @property
    @abstractmethod
    def needs_guest_linux(self) -> bool:
        """True if the payload runs inside a guest Linux kernel."""

    @abstractmethod
    def build(self, build_dir: Path, log_path: Path) -> None:
        """Build or prepare the payload.  Called during NestedBuildStage."""

    @abstractmethod
    def inject(self, share_dir: Path) -> None:
        """Copy payload artifacts into the 9p share directory."""

    @abstractmethod
    def rootfs_overlay(self) -> dict[str, str | bytes] | None:
        """Files to add to the L2 rootfs via cpio overlay.

        Returns ``{path: content}`` (str or bytes) or *None*.
        Only meaningful for linux-mode payloads.
        """

    @abstractmethod
    def run_guest_script(
        self,
        qemu_bin: str,
        memory: str,
        cpus: int,
        extra_args: str,
    ) -> str:
        """Generate the shell script that L1 executes to run this payload.

        For linux-mode payloads this launches one QEMU with the guest kernel.
        For bare-mode payloads this may launch QEMU multiple times.
        """

    @property
    def description(self) -> str:
        """One-line description for display."""
        return self.__class__.__name__


# ── ScriptPayload ────────────────────────────────────────────────────────

class ScriptPayload(Payload):
    """Inline shell commands to run inside the L2 Linux guest."""

    def __init__(self, script: str):
        self.script = script

    @property
    def needs_guest_linux(self) -> bool:
        return True

    def build(self, build_dir: Path, log_path: Path) -> None:
        pass

    def inject(self, share_dir: Path) -> None:
        pass

    def rootfs_overlay(self) -> dict[str, str]:
        return {"sbin/payload-init": self._init_script()}

    def run_guest_script(
        self, qemu_bin: str, memory: str, cpus: int, extra_args: str,
    ) -> str:
        return _linux_guest_script(
            qemu_bin=qemu_bin, memory=memory, cpus=cpus,
            rdinit="/sbin/payload-init", extra_args=extra_args,
        )

    @property
    def description(self) -> str:
        first = self.script.strip().splitlines()[0]
        return f"script: {first}"

    def _init_script(self) -> str:
        return f"""\
#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

mount -t proc     proc     /proc 2>/dev/null
mount -t sysfs    sysfs    /sys  2>/dev/null
mount -t devtmpfs devtmpfs /dev  2>/dev/null

echo ""
echo "============================================="
echo "  [L2] Guest booted — $(uname -r)"
echo "============================================="
echo ""
echo "[L2] Running payload..."
echo ""

{self.script}

echo ""
echo "============================================="
echo "  [L2] Payload complete"
echo "============================================="

poweroff -f
"""


# ── BinaryPayload ────────────────────────────────────────────────────────

class BinaryPayload(Payload):
    """Pre-built binary embedded in the L2 rootfs overlay and executed."""

    def __init__(self, path: str | Path, args: list[str] | None = None):
        self.path = Path(path)
        self.args = args or []

    @property
    def needs_guest_linux(self) -> bool:
        return True

    def build(self, build_dir: Path, log_path: Path) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"Payload binary not found: {self.path}")

    def inject(self, share_dir: Path) -> None:
        pass  # binary is embedded in rootfs overlay

    def rootfs_overlay(self) -> dict[str, str | bytes]:
        return {
            "sbin/payload-init": self._init_script(),
            "opt/payload": self.path.read_bytes(),
        }

    def run_guest_script(
        self, qemu_bin: str, memory: str, cpus: int, extra_args: str,
    ) -> str:
        return _linux_guest_script(
            qemu_bin=qemu_bin, memory=memory, cpus=cpus,
            rdinit="/sbin/payload-init", extra_args=extra_args,
        )

    @property
    def description(self) -> str:
        return f"binary: {self.path.name}"

    def _init_script(self) -> str:
        args_str = " ".join(self.args)
        return f"""\
#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

mount -t proc     proc     /proc 2>/dev/null
mount -t sysfs    sysfs    /sys  2>/dev/null
mount -t devtmpfs devtmpfs /dev  2>/dev/null

echo ""
echo "============================================="
echo "  [L2] Guest booted — $(uname -r)"
echo "============================================="
echo ""
echo "[L2] Running /opt/payload {args_str}"
echo ""

chmod +x /opt/payload
/opt/payload {args_str}
RC=$?

echo ""
echo "============================================="
echo "  [L2] Payload complete (rc=$RC)"
echo "============================================="

poweroff -f
"""


# ── KvmUnitTestsPayload ─────────────────────────────────────────────────

class KvmUnitTestsPayload(Payload):
    """Clone, build, and run kvm-unit-tests.

    Produces ``.flat`` binaries that QEMU loads directly as ``-kernel``
    (no guest Linux needed).  Each test runs in its own short-lived
    QEMU instance inside L1.
    """

    DEFAULT_REPO = "https://gitlab.com/kvm-unit-tests/kvm-unit-tests.git"

    # kvm-unit-tests arch → source subdirectory
    _ARCH_DIRS = {
        "x86_64":  "x86",
        "aarch64": "arm",
        "arm64":   "arm",
    }

    def __init__(
        self,
        repo: str | None = None,
        ref: str = "master",
        arch: str = "x86_64",
        tests: list[str] | None = None,
        configure_args: list[str] | None = None,
    ):
        self.repo = repo or self.DEFAULT_REPO
        self.ref = ref
        self.arch = arch
        self.tests = tests          # None → all
        self.configure_args = configure_args or []
        self._built_tests: list[Path] = []

    @property
    def needs_guest_linux(self) -> bool:
        return False

    def build(self, build_dir: Path, log_path: Path) -> None:
        build_dir.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        src_dir = build_dir / "src"

        console.print("  [bold cyan]kvm-unit-tests[/bold cyan]")

        # Clone / fetch
        if not (src_dir / ".git").exists():
            console.print(f"    Cloning from {self.repo}...")
            subprocess.run(
                ["git", "clone", self.repo, str(src_dir)],
                check=True, capture_output=True,
            )
        else:
            console.print("    Fetching updates...")
            subprocess.run(
                ["git", "fetch", "origin"],
                cwd=src_dir, check=True, capture_output=True,
            )

        subprocess.run(
            ["git", "checkout", self.ref],
            cwd=src_dir, check=True, capture_output=True,
        )

        # Configure
        cmd_cfg = ["./configure", f"--arch={self.arch}"] + self.configure_args
        console.print(f"    Configuring (arch={self.arch})...")
        with open(log_path, "w") as log:
            r = subprocess.run(cmd_cfg, cwd=src_dir, stdout=log, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            console.print(f"[red]    configure failed. See {log_path}[/red]")
            raise RuntimeError("kvm-unit-tests configure failed")

        # Build
        jobs = os.cpu_count() or 4
        console.print(f"    Building ({jobs} jobs)...")
        with open(log_path, "a") as log:
            r = subprocess.run(
                ["make", f"-j{jobs}"], cwd=src_dir,
                stdout=log, stderr=subprocess.STDOUT,
            )
        if r.returncode != 0:
            console.print(f"[red]    Build failed. See {log_path}[/red]")
            raise RuntimeError("kvm-unit-tests build failed")

        # Discover .flat test binaries
        arch_dir = src_dir / self._ARCH_DIRS.get(self.arch, self.arch)
        flats = sorted(arch_dir.glob("*.flat"))
        if self.tests:
            wanted = set(self.tests)
            flats = [f for f in flats if f.stem in wanted]

        if not flats:
            raise FileNotFoundError(f"No .flat test binaries found in {arch_dir}")

        self._built_tests = flats
        console.print(f"    [green]Built {len(flats)} tests[/green]")

    def inject(self, share_dir: Path) -> None:
        tests_dir = share_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        for flat in self._built_tests:
            shutil.copy2(flat, tests_dir / flat.name)

    def rootfs_overlay(self) -> None:
        return None  # bare-metal, no guest rootfs

    def run_guest_script(
        self, qemu_bin: str, memory: str, cpus: int, extra_args: str,
    ) -> str:
        return f"""\
#!/bin/sh
# Auto-generated — run kvm-unit-tests (bare-metal mode)

QEMU_BIN="{qemu_bin}"
PASSED=0
FAILED=0
SKIPPED=0

if ! command -v "$QEMU_BIN" >/dev/null 2>&1; then
    echo "[L1] ERROR: $QEMU_BIN not found"
    exit 1
fi

ACCEL=""
if [ -c /dev/kvm ]; then
    ACCEL="-enable-kvm -cpu host"
fi

echo "============================================="
echo "  [L1] Running kvm-unit-tests"
echo "============================================="
echo ""

for test in /nested/tests/*.flat; do
    name=$(basename "$test" .flat)
    echo "--- [$name] ---"

    $QEMU_BIN \\
        -nographic \\
        $ACCEL \\
        -m {memory} \\
        -smp {cpus} \\
        -kernel "$test" \\
        -append "console=ttyS0" \\
        {extra_args}
    RC=$?

    if [ $RC -eq 0 ]; then
        echo "--- [$name] PASS ---"
        PASSED=$((PASSED + 1))
    elif [ $RC -eq 77 ]; then
        echo "--- [$name] SKIP ---"
        SKIPPED=$((SKIPPED + 1))
    else
        echo "--- [$name] FAIL (rc=$RC) ---"
        FAILED=$((FAILED + 1))
    fi
    echo ""
done

echo "============================================="
echo "  [L1] Results: $PASSED passed, $FAILED failed, $SKIPPED skipped"
echo "============================================="
"""

    @property
    def description(self) -> str:
        if self.tests:
            return f"kvm-unit-tests: {', '.join(self.tests)}"
        return "kvm-unit-tests: all"


# ── Helper for linux-mode payloads ───────────────────────────────────────

def _linux_guest_script(
    qemu_bin: str,
    memory: str,
    cpus: int,
    rdinit: str,
    extra_args: str,
) -> str:
    """Standard L2 QEMU launch script for linux-mode payloads."""
    return f"""\
#!/bin/sh
# Auto-generated — launch nested guest (L2) with payload

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


# ── Factory ──────────────────────────────────────────────────────────────

def create_payload(
    config: dict[str, Any] | str | None,
    gc: Any = None,
    iter_cfg: Any = None,
) -> Payload | None:
    """Create a Payload from config.

    Accepts:
      - ``None``  → no payload
      - ``str``   → ScriptPayload (backward compatible)
      - ``dict``  → dispatch on ``config["type"]``
    """
    if config is None:
        return None

    if isinstance(config, str):
        return ScriptPayload(config)

    ptype = config.get("type", "script")

    if ptype == "script":
        return ScriptPayload(config["run"])

    if ptype == "binary":
        path = config["path"]
        if gc:
            path = str(gc.root / path)
        return BinaryPayload(path=path, args=config.get("args"))

    if ptype == "kvm-unit-tests":
        return KvmUnitTestsPayload(
            repo=config.get("repo"),
            ref=config.get("ref", "master"),
            arch=config.get("arch", "x86_64"),
            tests=config.get("tests"),
            configure_args=config.get("configure_args"),
        )

    raise ValueError(f"Unknown payload type: {ptype!r}")
