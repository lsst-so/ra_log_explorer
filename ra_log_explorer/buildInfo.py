"""Which build of the application this process is running.

The image build has no ``.git`` to ask (``.dockerignore`` keeps it out of
the context), so the Dockerfile writes the commit into the package as
``_gitHash.txt`` from its ``GIT_HASH`` build arg. A run from a checkout
has no such file and asks git instead. Read once, at import, so a commit
made while a dev server is running doesn't relabel code it hasn't loaded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import __version__

GIT_HASH_FILE = Path(__file__).parent / "_gitHash.txt"


def _readGitHash() -> str | None:
    """The commit this code was built from, or None if nothing can say."""
    try:
        baked = GIT_HASH_FILE.read_text().strip()
    except OSError:
        baked = ""
    if baked:
        return baked
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=GIT_HASH_FILE.parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


VERSION = __version__
GIT_HASH = _readGitHash()
