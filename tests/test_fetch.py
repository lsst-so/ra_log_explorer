"""Tests for `ra_log_explorer.fetch`."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from ra_log_explorer import fetch

# ----- humanBytes ---------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0.0 B"),
        (512, "512.0 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024 * 1024, "1.0 MiB"),
        (5 * 1024 * 1024 * 1024, "5.0 GiB"),
    ],
)
def test_humanBytes(n: int, expected: str) -> None:
    assert fetch.humanBytes(n).strip() == expected


# ----- _parseIso ----------------------------------------------------------


def test_parseIso_handles_z_suffix() -> None:
    t = fetch._parseIso("2026-05-20T08:45:39Z")
    assert t == dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)


def test_parseIso_handles_microseconds() -> None:
    t = fetch._parseIso("2026-05-20T08:45:39.267000Z")
    assert t == dt.datetime(2026, 5, 20, 8, 45, 39, 267000, tzinfo=dt.timezone.utc)


def test_parseIso_handles_explicit_offset() -> None:
    t = fetch._parseIso("2026-05-20T09:45:39+01:00")
    assert t == dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)


def test_parseIso_treats_naive_as_utc() -> None:
    t = fetch._parseIso("2026-05-20T08:45:39")
    assert t == dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)


# ----- findSupersetCache --------------------------------------------------


def _writeCache(
    root: Path, cluster: str, namespace: str, fromIso: str, toIso: str, *, partial: bool = False
) -> Path:
    fromSlug = fromIso.replace(":", "").replace(".", "_")
    toSlug = toIso.replace(":", "").replace(".", "_")
    d = root / cluster / namespace / f"{fromSlug}__{toSlug}"
    (d / "pods").mkdir(parents=True)
    (d / "_meta.json").write_text(
        json.dumps(
            {
                "spec": {
                    "lokiAddr": "x",
                    "username": "u",
                    "cluster": cluster,
                    "namespace": namespace,
                    "fromIso": fromIso,
                    "toIso": toIso,
                    "workers": 8,
                    "lineLimit": 50000,
                },
                "pod_count": 0,
                "total_bytes": 0,
                "pod_bytes": {},
                "errors": {},
                "window_in_past": True,
                "fromCache": False,
                "cacheReuse": "none",
            }
        )
    )
    if partial:
        (d / fetch.PARTIAL_FLAG).write_text("")
    return d


def test_findSupersetCache_returns_None_when_no_cache(tmpCacheRoot: Path) -> None:
    assert (
        fetch.findSupersetCache("yagan", "rapid-analysis", "2026-05-20T08:46:00Z", "2026-05-20T08:46:30Z")
        is None
    )


def test_findSupersetCache_finds_strict_superset(tmpCacheRoot: Path) -> None:
    _writeCache(tmpCacheRoot, "yagan", "rapid-analysis", "2026-05-20T08:00:00Z", "2026-05-20T09:00:00Z")
    found = fetch.findSupersetCache("yagan", "rapid-analysis", "2026-05-20T08:30:00Z", "2026-05-20T08:35:00Z")
    assert found is not None


def test_findSupersetCache_accepts_exact_match_as_superset(tmpCacheRoot: Path) -> None:
    # An equal window is a (trivial) superset; the function returns it.
    d = _writeCache(tmpCacheRoot, "yagan", "rapid-analysis", "2026-05-20T08:00:00Z", "2026-05-20T08:05:00Z")
    found = fetch.findSupersetCache("yagan", "rapid-analysis", "2026-05-20T08:00:00Z", "2026-05-20T08:05:00Z")
    assert found == d


def test_findSupersetCache_returns_None_when_not_contained(tmpCacheRoot: Path) -> None:
    _writeCache(tmpCacheRoot, "yagan", "rapid-analysis", "2026-05-20T08:00:00Z", "2026-05-20T08:05:00Z")
    # Window straddles the cached one
    assert (
        fetch.findSupersetCache("yagan", "rapid-analysis", "2026-05-20T07:55:00Z", "2026-05-20T08:10:00Z")
        is None
    )


def test_findSupersetCache_skips_partial(tmpCacheRoot: Path) -> None:
    _writeCache(
        tmpCacheRoot,
        "yagan",
        "rapid-analysis",
        "2026-05-20T08:00:00Z",
        "2026-05-20T09:00:00Z",
        partial=True,
    )
    assert (
        fetch.findSupersetCache("yagan", "rapid-analysis", "2026-05-20T08:30:00Z", "2026-05-20T08:35:00Z")
        is None
    )


def test_findSupersetCache_skips_no_meta(tmpCacheRoot: Path) -> None:
    d = tmpCacheRoot / "yagan" / "rapid-analysis" / "weird-dir"
    d.mkdir(parents=True)
    assert (
        fetch.findSupersetCache("yagan", "rapid-analysis", "2026-05-20T08:30:00Z", "2026-05-20T08:35:00Z")
        is None
    )


def test_findSupersetCache_picks_smallest_superset(tmpCacheRoot: Path) -> None:
    # Bigger superset
    _writeCache(tmpCacheRoot, "yagan", "rapid-analysis", "2026-05-20T07:00:00Z", "2026-05-20T10:00:00Z")
    # Smaller (tighter) superset
    tight = _writeCache(
        tmpCacheRoot, "yagan", "rapid-analysis", "2026-05-20T08:00:00Z", "2026-05-20T09:00:00Z"
    )
    found = fetch.findSupersetCache("yagan", "rapid-analysis", "2026-05-20T08:30:00Z", "2026-05-20T08:35:00Z")
    assert found == tight


def test_findSupersetCache_handles_corrupt_meta(tmpCacheRoot: Path) -> None:
    d = tmpCacheRoot / "yagan" / "rapid-analysis" / "broken"
    (d / "pods").mkdir(parents=True)
    (d / "_meta.json").write_text("not valid json")
    assert (
        fetch.findSupersetCache("yagan", "rapid-analysis", "2026-05-20T08:00:00Z", "2026-05-20T08:01:00Z")
        is None
    )


def test_findSupersetCache_isolates_by_cluster_and_namespace(tmpCacheRoot: Path) -> None:
    _writeCache(tmpCacheRoot, "elsewhere", "rapid-analysis", "2026-05-20T07:00:00Z", "2026-05-20T10:00:00Z")
    _writeCache(tmpCacheRoot, "yagan", "other-ns", "2026-05-20T07:00:00Z", "2026-05-20T10:00:00Z")
    # Asked for yagan/rapid-analysis; the cached ones don't match either tuple.
    assert (
        fetch.findSupersetCache("yagan", "rapid-analysis", "2026-05-20T08:30:00Z", "2026-05-20T08:35:00Z")
        is None
    )


# ----- loadPodLogPath / cacheDuSizeBytes ----------------------------------


def test_loadPodLogPath(tmp_path: Path) -> None:
    assert fetch.loadPodLogPath(tmp_path, "some-pod") == tmp_path / "pods" / "some-pod.jsonl"


def test_cacheDuSizeBytes_sums_recursively(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "1.txt").write_text("hello")
    (tmp_path / "a" / "2.txt").write_text("worlds!")
    (tmp_path / "b.txt").write_text("x")
    assert fetch.cacheDuSizeBytes(tmp_path) == len("hello") + len("worlds!") + len("x")


def test_cacheDuSizeBytes_empty_tree(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert fetch.cacheDuSizeBytes(tmp_path) == 0
