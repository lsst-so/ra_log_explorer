"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture
def headNodeJsonl() -> Path:
    """Path to the head-node sample fixture (read-only)."""
    return DATA_DIR / "head_node_sample.jsonl"


@pytest.fixture
def sfmWorkerJsonl() -> Path:
    return DATA_DIR / "sfm_worker_sample.jsonl"


@pytest.fixture
def aosWorkerJsonl() -> Path:
    return DATA_DIR / "aos_worker_sample.jsonl"


@pytest.fixture
def tracebackJsonl() -> Path:
    return DATA_DIR / "traceback_sample.jsonl"


@pytest.fixture
def tmpCacheRoot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect `cache_root()` to a per-test scratch directory.

    Reset the cache between tests so superset-cache lookups can't leak
    fixtures from one test into another.
    """
    root = tmp_path / "cache_root"
    root.mkdir()
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(root))
    yield root
    # Cleanup is automatic via tmp_path, but be paranoid in case the
    # test under examination clobbers our env var.
    if str(root) == os.environ.get("RA_LOG_EXPLORER_CACHE", ""):
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def fakeCachedWindow(tmpCacheRoot: Path) -> Path:
    """Create a fully-formed cache directory for a sample window and return its path.

    The directory has a valid `_meta.json` containing a `FetchSpec`-shaped
    `spec` block, no `.partial` flag, and one fake pod JSONL file so superset
    discovery tests have something realistic to walk.
    """
    fromIso = "2026-05-20T08:45:34.267000Z"
    toIso = "2026-05-20T08:50:39.267000Z"
    fromSlug = fromIso.replace(":", "").replace(".", "_")
    toSlug = toIso.replace(":", "").replace(".", "_")
    windowDir = tmpCacheRoot / "yagan" / "rapid-analysis" / f"{fromSlug}__{toSlug}"
    (windowDir / "pods").mkdir(parents=True)
    (windowDir / "pods" / "fake-pod.jsonl").write_text("")
    (windowDir / "_meta.json").write_text(
        '{"spec": {"lokiAddr": "x", "username": "u", "cluster": "yagan", '
        '"namespace": "rapid-analysis", '
        f'"fromIso": "{fromIso}", "toIso": "{toIso}", '
        '"workers": 8, "lineLimit": 50000}, '
        '"pod_count": 1, "total_bytes": 0, "pod_bytes": {}, '
        '"errors": {}, "window_in_past": true, "fromCache": false, '
        '"cacheReuse": "none"}'
    )
    return windowDir
