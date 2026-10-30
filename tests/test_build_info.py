"""Where the admin page's git hash comes from: the image, or the checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ra_log_explorer import buildInfo


def test_a_baked_hash_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "_gitHash.txt").write_text("abc123\n")
    monkeypatch.setattr(buildInfo, "GIT_HASH_FILE", tmp_path / "_gitHash.txt")
    assert buildInfo._readGitHash() == "abc123"


def test_a_checkout_asks_git() -> None:
    """No baked file in a checkout, so the answer is the repo's HEAD."""
    assert not buildInfo.GIT_HASH_FILE.exists()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=buildInfo.GIT_HASH_FILE.parent, capture_output=True, text=True
    ).stdout.strip()
    assert buildInfo._readGitHash() == head


def test_an_empty_baked_hash_outside_a_repo_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image built without the build arg writes an empty file, and has
    no git to fall back on: that must read as unknown, not as ''."""
    (tmp_path / "_gitHash.txt").write_text("\n")
    monkeypatch.setattr(buildInfo, "GIT_HASH_FILE", tmp_path / "_gitHash.txt")
    assert buildInfo._readGitHash() is None
