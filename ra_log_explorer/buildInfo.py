"""Which build of the application this process is running.

Both values come from git: the version is ``git describe --tags``, so a
release tag names itself and anything after it reads as
``v0.2.0-30-ge16e6c9``, with no version number to remember to bump. The
image build has no ``.git`` to ask (``.dockerignore`` keeps it out of the
context), so the workflow computes them and the Dockerfile writes them
into the package as ``_version.txt`` and ``_gitHash.txt``. A run from a
checkout has no such files and asks git itself. Read once, at import, so
a commit made while a dev server is running doesn't relabel code it
hasn't loaded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
VERSION_FILE = PACKAGE_DIR / "_version.txt"
GIT_HASH_FILE = PACKAGE_DIR / "_gitHash.txt"


def _readBuildValue(bakedFile: Path, gitArgs: list[str]) -> str | None:
    """The value the image build baked in, else git's answer, else None."""
    try:
        baked = bakedFile.read_text().strip()
    except OSError:
        baked = ""
    if baked:
        return baked
    try:
        result = subprocess.run(
            ["git", *gitArgs],
            cwd=bakedFile.parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


VERSION = _readBuildValue(VERSION_FILE, ["describe", "--tags", "--always", "--dirty"])
GIT_HASH = _readBuildValue(GIT_HASH_FILE, ["rev-parse", "HEAD"])
