"""Build version from git metadata or VERSION file.

Priority:
1. VERSION file (written by deploy.sh before rsync) — works in Docker
2. Live git query — works in dev
3. "unknown" fallback
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _read_version_file() -> str | None:
    if _VERSION_FILE.exists():
        content = _VERSION_FILE.read_text(encoding="utf-8").strip()
        if content:
            return content
    return None


def _git_version() -> str | None:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short=8", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        date = subprocess.check_output(
            ["git", "log", "-1", "--format=%cd", "--date=format-local:%Y-%m-%d %H:%M"],
            stderr=subprocess.DEVNULL,
            text=True,
            env={**__import__("os").environ, "TZ": "Europe/Paris"},
        ).strip()
        dirty = subprocess.call(
            ["git", "diff", "--quiet", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        suffix = "-dirty" if dirty else ""
        return f"{commit}{suffix} ({date})"
    except (FileNotFoundError, subprocess.SubprocessError):
        return None


VERSION = _read_version_file() or _git_version() or "unknown"
