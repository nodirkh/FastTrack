#!/usr/bin/env python3
"""
Watch and filter serial output from nested VMs.

Usage:
    python watcher.py <serial.log>               # all output, colorized
    python watcher.py <serial.log> --level L1     # hypervisor only
    python watcher.py <serial.log> --level L2     # guest / payload only

Designed to run inside a tmux pane, tailing the serial log captured
by the ``script`` command.  Strips terminal control characters before
matching, colorizes output by VM level.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

# ANSI / terminal control sequence pattern (covers CSI, OSC, charset)
_CTRL_RE = re.compile(
    r"\x1b\[[0-9;]*[A-Za-z]"   # CSI sequences
    r"|\x1b\][^\x07]*\x07"     # OSC sequences
    r"|\x1b[()][012AB]"        # charset switches
    r"|[\x00-\x08\x0e\x0f]"   # misc control chars (but keep \t \n \r)
)

# Colors
_CYAN   = "\033[36m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"


def _strip_ctrl(s: str) -> str:
    """Remove ANSI escapes and control characters for clean matching."""
    return _CTRL_RE.sub("", s)


def watch(log_path: Path, level: str | None = None) -> None:
    print(f"{_DIM}Watching {log_path}")
    if level:
        print(f"Filtering for [{level}]{_RESET}")
    else:
        print(f"Showing all output{_RESET}")
    print()

    while not log_path.exists():
        time.sleep(0.2)

    with open(log_path, errors="replace") as f:
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.05)
                continue

            # Normalize line endings from `script` output
            line = line.replace("\r\n", "\n").rstrip("\r")

            clean = _strip_ctrl(line)

            # Level filter
            if level and f"[{level}]" not in clean:
                continue

            # Colorize by level
            if "[L1]" in clean:
                sys.stdout.write(f"{_CYAN}{clean}{_RESET}")
            elif "[L2]" in clean:
                sys.stdout.write(f"{_GREEN}{clean}{_RESET}")
            elif "ERROR" in clean or "FAIL" in clean:
                sys.stdout.write(f"{_YELLOW}{clean}{_RESET}")
            elif "=====" in clean:
                sys.stdout.write(f"{_BOLD}{clean}{_RESET}")
            elif "PASS" in clean:
                sys.stdout.write(f"{_GREEN}{clean}{_RESET}")
            else:
                sys.stdout.write(clean)

            # Ensure newline
            if not clean.endswith("\n"):
                sys.stdout.write("\n")

            sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch nested VM serial output")
    parser.add_argument("log", type=Path, help="Path to serial log file")
    parser.add_argument(
        "--level", choices=["L1", "L2"],
        help="Show only lines from this VM level",
    )
    args = parser.parse_args()

    try:
        watch(args.log, args.level)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
