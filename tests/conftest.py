"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from ra_log_explorer import sites

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
def truncatedTracebackJsonl() -> Path:
    """Real-world slice from an AOS worker on 20260602 where the log
    forwarder dropped the tail of one chained traceback.

    The slice is a single `self.consdbClient.insert(...)` retry burst:
    four chained tracebacks (`ConnectionRefusedError` →
    `NewConnectionError` → `MaxRetryError` → final `requests.exceptions
    .ConnectionError`). The first three complete normally; the fourth's
    body stops mid-frame, then a `Starting to process …` INFO line from
    another logger interrupts. Pins the `<truncated>` sentinel behaviour
    end-to-end on real upstream data.
    """
    return DATA_DIR / "s-lsstcam-run-aos-worker-aosworkerset-8-truncated.jsonl"


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


@dataclass(frozen=True)
class FakeSiteCatalog:
    """Handle returned by :func:`siteCatalog` for tests that need to plant
    tokens at the right path. ``writeSummitToken`` and ``writeBtsToken``
    drop a string at the site's token file so a ConsDB lookup can proceed
    without flaky path-handling.
    """

    catalog: list[sites.Site]
    defaultName: str
    summitTokenFile: Path
    btsTokenFile: Path
    sitesFile: Path

    def writeSummitToken(self, token: str = "fake-summit-token") -> None:
        self.summitTokenFile.parent.mkdir(parents=True, exist_ok=True)
        self.summitTokenFile.write_text(token)

    def writeBtsToken(self, token: str = "fake-bts-token") -> None:
        self.btsTokenFile.parent.mkdir(parents=True, exist_ok=True)
        self.btsTokenFile.write_text(token)


@pytest.fixture
def siteCatalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSiteCatalog]:
    """Write a per-test ``sites.toml`` and point the loader at it.

    The packaged ``sites.toml`` references token files in the developer's
    home dir, which neither CI nor a hermetic test should depend on.
    This fixture builds an equivalent two-site catalog (summit + bts)
    whose token-file paths land inside ``tmp_path`` and sets
    ``RA_LOG_EXPLORER_SITES_FILE`` so :func:`sites.loadSites` picks it
    up. Token files start absent; tests that need a working ConsDB call
    invoke ``writeSummitToken`` / ``writeBtsToken``.
    """
    summitTok = tmp_path / "tokens" / "summit.txt"
    btsTok = tmp_path / "tokens" / "bts.txt"
    sitesFile = tmp_path / "sites.toml"
    sitesFile.write_text(f"""default_site = "summit"

[[site]]
name = "summit"
cluster = "yagan"
namespace = "rapid-analysis"
lokiAddr = "https://loki-query.ls.lsst.org"
consdbUrl = "https://summit-consdb.example/consdb/query"
consdbTokenFile = "{summitTok}"

[[site]]
name = "bts"
cluster = "manke"
namespace = "rapid-analysis"
lokiAddr = "https://loki-query.ls.lsst.org"
consdbUrl = "https://bts-consdb.example/consdb/query"
consdbTokenFile = "{btsTok}"
""")
    monkeypatch.setenv(sites.SITES_FILE_ENV, str(sitesFile))
    catalog, default = sites.loadSites()
    yield FakeSiteCatalog(
        catalog=catalog,
        defaultName=default,
        summitTokenFile=summitTok,
        btsTokenFile=btsTok,
        sitesFile=sitesFile,
    )


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
        '"workers": 8}, '
        '"pod_count": 1, "total_bytes": 0, "pod_bytes": {}, '
        '"errors": {}, "window_in_past": true, "fromCache": false, '
        '"cacheReuse": "none"}'
    )
    return windowDir
