"""Tests for `ra_log_explorer.config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from ra_log_explorer import config


def test_cacheRoot_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(target))
    root = config.cache_root()
    assert root == target
    assert root.exists()


def test_cacheRoot_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Override HOME so we don't pollute the real ~/.cache. The default code path
    # builds Path.home() / .cache / ra_log_explorer.
    monkeypatch.delenv("RA_LOG_EXPLORER_CACHE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = config.cache_root()
    assert root == tmp_path / ".cache" / "ra_log_explorer"
    assert root.exists()


def test_windowCachePath_is_deterministic_and_does_no_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p1 = config.windowCachePath(
        "yagan", "rapid-analysis", "2026-05-20T08:45:34.267Z", "2026-05-20T08:50:39.267Z"
    )
    p2 = config.windowCachePath(
        "yagan", "rapid-analysis", "2026-05-20T08:45:34.267Z", "2026-05-20T08:50:39.267Z"
    )
    assert p1 == p2
    # The returned path must NOT have been created on disk by windowCachePath.
    assert not p1.exists()


def test_windowCachePath_slug_replaces_colons_and_dots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p = config.windowCachePath("yagan", "ns", "2026-05-20T08:45:34.267Z", "2026-05-20T08:50:39.267Z")
    # ':' must be gone, '.' must have become '_'
    assert ":" not in p.name
    assert "." not in p.name
    assert "__" in p.name  # window separator


def test_windowCachePath_different_windows_distinct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p1 = config.windowCachePath("yagan", "ns", "2026-05-20T08:45:00Z", "2026-05-20T08:50:00Z")
    p2 = config.windowCachePath("yagan", "ns", "2026-05-20T08:45:00Z", "2026-05-20T08:51:00Z")
    assert p1 != p2


def test_ensureWindowCacheDir_creates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p = config.ensureWindowCacheDir("yagan", "ns", "2026-05-20T08:45:00Z", "2026-05-20T08:50:00Z")
    assert p.exists()
    assert p.is_dir()


def test_ensureWindowCacheDir_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p1 = config.ensureWindowCacheDir("yagan", "ns", "2026-05-20T08:45:00Z", "2026-05-20T08:50:00Z")
    p2 = config.ensureWindowCacheDir("yagan", "ns", "2026-05-20T08:45:00Z", "2026-05-20T08:50:00Z")
    assert p1 == p2
    assert p1.exists()


def test_FetchSpec_is_frozen() -> None:
    spec = config.FetchSpec(
        lokiAddr="x",
        username="u",
        cluster="c",
        namespace="n",
        fromIso="2026-05-20T08:00:00Z",
        toIso="2026-05-20T08:05:00Z",
    )
    with pytest.raises(Exception):  # noqa: PT011 - dataclasses raises FrozenInstanceError
        spec.cluster = "other"  # type: ignore[misc]


def test_windowCachePath_podRegex_nests_under_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Night-mode (podRegex set) layouts must live one directory deeper
    than exposure-mode layouts. If they didn't, an unfiltered fetch and a
    filtered fetch over the same time window would clobber each other.
    """
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    bare = config.windowCachePath("yagan", "ns", "2026-05-20T08:00:00Z", "2026-05-20T08:05:00Z")
    night = config.windowCachePath(
        "yagan", "ns", "2026-05-20T08:00:00Z", "2026-05-20T08:05:00Z", podRegex=".*aos.*"
    )
    # The night path nests an extra component below the exposure path.
    assert night.parent == bare
    assert night.name.startswith("pods=")


def test_windowCachePath_podRegex_slug_is_filesystem_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regex slug shouldn't contain anything the filesystem dislikes —
    no slashes, no leading dots that would shadow hidden-file conventions,
    just alnum + underscore."""
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p = config.windowCachePath(
        "yagan", "ns", "2026-05-20T08:00:00Z", "2026-05-20T08:05:00Z", podRegex=".*aos.*"
    )
    slug = p.name[len("pods=") :]
    # Special chars folded to underscores.
    assert "/" not in slug and "*" not in slug and "." not in slug
    # But it survives long enough to keep distinct regexes distinct.
    other = config.windowCachePath(
        "yagan", "ns", "2026-05-20T08:00:00Z", "2026-05-20T08:05:00Z", podRegex=".*sfm.*"
    )
    assert p != other


def test_ensureWindowCacheDir_honours_podRegex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensureWindowCacheDir must actually create the night-mode nested
    directory; otherwise the writer in fetchAll would crash on first use."""
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path))
    p = config.ensureWindowCacheDir(
        "yagan", "ns", "2026-05-20T08:00:00Z", "2026-05-20T08:05:00Z", podRegex=".*aos.*"
    )
    assert p.exists() and p.is_dir()
    assert p.name.startswith("pods=")
