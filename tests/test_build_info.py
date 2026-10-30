"""Where the admin page's version and git hash come from: the image, or the checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ra_log_explorer import buildInfo


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=buildInfo.PACKAGE_DIR, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_a_baked_value_wins(tmp_path: Path) -> None:
    (tmp_path / "_version.txt").write_text("v1.2.3\n")
    assert buildInfo._readBuildValue(tmp_path / "_version.txt", ["describe"]) == "v1.2.3"


def test_a_checkout_asks_git() -> None:
    """No baked files in a checkout, so the answers are the repo's."""
    assert not buildInfo.VERSION_FILE.exists()
    assert not buildInfo.GIT_HASH_FILE.exists()
    assert buildInfo.GIT_HASH == _git("rev-parse", "HEAD")
    assert buildInfo.VERSION == _git("describe", "--tags", "--always", "--dirty")


def test_an_empty_baked_value_outside_a_repo_is_unknown(tmp_path: Path) -> None:
    """An image built without the build arg writes an empty file, and has
    no git to fall back on: that must read as unknown, not as ''."""
    (tmp_path / "_gitHash.txt").write_text("\n")
    assert buildInfo._readBuildValue(tmp_path / "_gitHash.txt", ["rev-parse", "HEAD"]) is None


def test_no_git_at_all_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The image has no git binary; that must not be an error."""
    monkeypatch.setenv("PATH", str(tmp_path))
    assert buildInfo._readBuildValue(tmp_path / "_gitHash.txt", ["rev-parse", "HEAD"]) is None
