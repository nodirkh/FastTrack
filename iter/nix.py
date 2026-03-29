"""Nix integration helpers shared across build stages."""

from __future__ import annotations

import shlex


def nix_wrap(cmd: str, nix: dict | None) -> str:
    """Wrap a shell command with ``nix develop`` if nix is enabled.

    *nix* is the ``nix`` dict from IterationConfig (``{"enabled": bool, "flake": str}``).
    Returns *cmd* unchanged when nix is disabled or *nix* is None.
    """
    if not nix or not nix.get("enabled"):
        return cmd
    flake = shlex.quote(nix.get("flake", ".#devShell"))
    return f"nix develop {flake} --command bash -c {shlex.quote(cmd)}"
