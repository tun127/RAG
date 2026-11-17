"""Resolve a Git revision without requiring Git metadata for simulated reports."""

import subprocess
from pathlib import Path

NO_GIT_REVISION = "0" * 40


def code_revision(directory: Path, *, simulation: bool) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=directory,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        if not simulation:
            raise
        return NO_GIT_REVISION
