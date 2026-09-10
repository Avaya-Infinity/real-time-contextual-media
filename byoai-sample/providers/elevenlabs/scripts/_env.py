"""
_env — Shared .env path resolution for the ElevenLabs provisioning scripts

Role:
    Shared .env path resolution for the ElevenLabs provisioning scripts.
    Locates the bridge .env file across dev and VM environments and parses
    KEY=VALUE pairs for use by the provisioning scripts.

Does not own:
    API calls, agent configuration, or any ElevenLabs SDK interaction.
    Runtime bridge configuration — this is provisioning-time tooling only.

Dependencies:
    Standard library only (pathlib, os). No ElevenLabs SDK dependency.

RCMS lifecycle phase:
    None — provisioning-time utility, not invoked during a call.
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_env_path() -> Path:
    """Locate the bridge .env file across dev and VM environments.

    Lookup order:
      1. Explicit override via BRIDGE_ENV_PATH env var (debug / unusual
         deployments)
      2. Repo-relative: <repo_root>/bridge/.env (dev machine, where this file
         lives at providers/elevenlabs/scripts/_env.py — parents[3] is
         the repo root and bridge/.env sits alongside bridge/.env.example)
      3. VM canonical: /opt/bridge-server/.env

    Raises FileNotFoundError if none of the candidates exist.
    """
    override = os.environ.get("BRIDGE_ENV_PATH", "").strip()
    if override:
        p = Path(override)
        if p.exists():
            return p

    candidates = [
        Path(__file__).resolve().parents[3] / "bridge" / ".env",
        Path("/opt/bridge-server/.env"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        "No .env found. Set BRIDGE_ENV_PATH explicitly, or run from a "
        "context where one of these paths exists: "
        + ", ".join(str(c) for c in candidates)
    )


def load_env(path: Path) -> dict[str, str]:
    """Parse a flat KEY=VALUE .env file. Comments and blank lines ignored."""
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env
