"""Tests for `ra_log_explorer.fetch`."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from ra_log_explorer import fetch
from ra_log_explorer.config import FetchSpec

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


# ----- addExposureToCache / getCacheExposureIds ---------------------------


def test_getCacheExposureIds_returns_empty_when_no_sidecar(tmp_path: Path) -> None:
    assert fetch.getCacheExposureIds(tmp_path) == []


def test_addExposureToCache_then_get_roundtrips(tmp_path: Path) -> None:
    fetch.addExposureToCache(tmp_path, 2026051900722)
    assert fetch.getCacheExposureIds(tmp_path) == [2026051900722]


def test_addExposureToCache_accumulates_distinct_ids_sorted(tmp_path: Path) -> None:
    # Same cache, multiple triggering dataIds (the superset-reuse case).
    fetch.addExposureToCache(tmp_path, 2026051900723)
    fetch.addExposureToCache(tmp_path, 2026051900722)
    fetch.addExposureToCache(tmp_path, 2026051900724)
    assert fetch.getCacheExposureIds(tmp_path) == [
        2026051900722,
        2026051900723,
        2026051900724,
    ]


def test_addExposureToCache_dedupes_repeated_ids(tmp_path: Path) -> None:
    fetch.addExposureToCache(tmp_path, 2026051900722)
    fetch.addExposureToCache(tmp_path, 2026051900722)
    fetch.addExposureToCache(tmp_path, 2026051900722)
    assert fetch.getCacheExposureIds(tmp_path) == [2026051900722]


def test_addExposureToCache_on_missing_dir_is_a_noop(tmp_path: Path) -> None:
    # No raise.
    fetch.addExposureToCache(tmp_path / "does-not-exist", 2026051900722)


def test_getCacheExposureIds_skips_unparseable_lines(tmp_path: Path) -> None:
    (tmp_path / fetch.EXPOSURE_IDS_NAME).write_text("2026051900722\nnot-a-number\n2026051900723\n")
    assert fetch.getCacheExposureIds(tmp_path) == [2026051900722, 2026051900723]


# ----- markCacheViewed / getCacheLastViewed -------------------------------


def test_markCacheViewed_writes_iso_timestamp(tmp_path: Path) -> None:
    fetch.markCacheViewed(tmp_path)
    p = tmp_path / fetch.LAST_VIEWED_NAME
    assert p.exists()
    # Should round-trip to a datetime via _parseIso (which the
    # getCacheLastViewed helper uses internally).
    assert fetch.getCacheLastViewed(tmp_path) is not None


def test_markCacheViewed_with_explicit_when(tmp_path: Path) -> None:
    when = dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=dt.timezone.utc)
    fetch.markCacheViewed(tmp_path, when=when)
    assert fetch.getCacheLastViewed(tmp_path) == when


def test_getCacheLastViewed_returns_None_when_missing(tmp_path: Path) -> None:
    assert fetch.getCacheLastViewed(tmp_path) is None


def test_getCacheLastViewed_returns_None_when_unparseable(tmp_path: Path) -> None:
    (tmp_path / fetch.LAST_VIEWED_NAME).write_text("not a timestamp")
    assert fetch.getCacheLastViewed(tmp_path) is None


def test_markCacheViewed_on_missing_dir_is_a_noop(tmp_path: Path) -> None:
    # No raise.
    fetch.markCacheViewed(tmp_path / "does-not-exist")


# ----- evictToFit ----------------------------------------------------------


def _plantWindowWithBody(
    root: Path,
    cluster: str,
    namespace: str,
    fromIso: str,
    toIso: str,
    bodyBytes: int,
    *,
    lastViewed: dt.datetime | None,
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
                "pod_count": 1,
                "total_bytes": bodyBytes,
                "pod_bytes": {},
                "errors": {},
                "window_in_past": True,
                "fromCache": False,
                "cacheReuse": "none",
            }
        )
    )
    (d / "pods" / "fake.jsonl").write_bytes(b"x" * bodyBytes)
    if lastViewed is not None:
        fetch.markCacheViewed(d, when=lastViewed)
    return d


def test_evictToFit_noop_when_under_limit(tmpCacheRoot: Path) -> None:
    _plantWindowWithBody(
        tmpCacheRoot,
        "yagan",
        "rapid-analysis",
        "2026-05-20T08:00:00Z",
        "2026-05-20T08:05:00Z",
        bodyBytes=1000,
        lastViewed=dt.datetime(2026, 5, 21, tzinfo=dt.timezone.utc),
    )
    removed = fetch.evictToFit(maxBytes=10 * 1000)
    assert removed == []


def test_evictToFit_removes_oldest_until_under_limit(tmpCacheRoot: Path) -> None:
    base = dt.datetime(2026, 5, 21, tzinfo=dt.timezone.utc)
    a = _plantWindowWithBody(
        tmpCacheRoot,
        "yagan",
        "rapid-analysis",
        "2026-05-20T08:00:00Z",
        "2026-05-20T08:05:00Z",
        bodyBytes=2000,
        lastViewed=base - dt.timedelta(hours=2),  # oldest
    )
    b = _plantWindowWithBody(
        tmpCacheRoot,
        "yagan",
        "rapid-analysis",
        "2026-05-20T09:00:00Z",
        "2026-05-20T09:05:00Z",
        bodyBytes=2000,
        lastViewed=base - dt.timedelta(hours=1),
    )
    c = _plantWindowWithBody(
        tmpCacheRoot,
        "yagan",
        "rapid-analysis",
        "2026-05-20T10:00:00Z",
        "2026-05-20T10:05:00Z",
        bodyBytes=2000,
        lastViewed=base,  # newest
    )
    # Limit at 3000 bytes — we have ~6000 bytes total (3 caches × 2KB each
    # plus some meta), so eviction should drop the two oldest.
    removed = fetch.evictToFit(maxBytes=3000)
    assert a in removed
    assert b in removed
    assert c not in removed
    assert not a.exists()
    assert c.exists()


def test_evictToFit_skips_exempt(tmpCacheRoot: Path) -> None:
    base = dt.datetime(2026, 5, 21, tzinfo=dt.timezone.utc)
    a = _plantWindowWithBody(
        tmpCacheRoot,
        "yagan",
        "rapid-analysis",
        "2026-05-20T08:00:00Z",
        "2026-05-20T08:05:00Z",
        bodyBytes=2000,
        lastViewed=base - dt.timedelta(hours=2),
    )
    b = _plantWindowWithBody(
        tmpCacheRoot,
        "yagan",
        "rapid-analysis",
        "2026-05-20T09:00:00Z",
        "2026-05-20T09:05:00Z",
        bodyBytes=2000,
        lastViewed=base - dt.timedelta(hours=1),
    )
    # Even though A is the LRU, the exempt set spares it. B gets the chop.
    removed = fetch.evictToFit(maxBytes=3000, exempt=[a])
    assert a in [c for c in [a] if c.exists()]
    assert b in removed
    assert not b.exists()
    assert a.exists()


# ----- _run_logcli + listPods + _fetchOnePod (subprocess-mocked) ----------


class _FakeCompleted:
    """Stand-in for the ``subprocess.CompletedProcess`` we capture."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _stubSpec() -> FetchSpec:
    return FetchSpec(
        lokiAddr="https://loki",
        username="u",
        cluster="yagan",
        namespace="rapid-analysis",
        fromIso="2026-05-20T08:00:00Z",
        toIso="2026-05-20T08:10:00Z",
    )


def test_run_logcli_includes_user_and_addr_in_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_run_logcli`` must call the logcli binary with the connection flags
    threaded in, not via env vars that could leak.
    """
    captured: dict[str, list[str]] = {}

    def fakeRun(cmd: list[str], **kw: Any) -> _FakeCompleted:
        captured["cmd"] = list(cmd)
        return _FakeCompleted(stdout=b"hello")

    monkeypatch.setenv("LOKI_PASSWORD", "x")
    monkeypatch.setattr(fetch.subprocess, "run", fakeRun)
    out = fetch._run_logcli(_stubSpec(), ["series", '{cluster="yagan"}'])
    assert out == b"hello"
    assert captured["cmd"][0] == "logcli"
    assert "--username=u" in captured["cmd"]
    assert "--addr=https://loki" in captured["cmd"]
    assert captured["cmd"][-2:] == ["series", '{cluster="yagan"}']


def test_run_logcli_raises_when_LOKI_PASSWORD_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOKI_PASSWORD", raising=False)
    with pytest.raises(fetch.FetchError, match="LOKI_PASSWORD"):
        fetch._run_logcli(_stubSpec(), ["series"])


def test_run_logcli_raises_FetchError_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fakeRun(*_a: Any, **_kw: Any) -> _FakeCompleted:
        raise FileNotFoundError("logcli")

    monkeypatch.setenv("LOKI_PASSWORD", "x")
    monkeypatch.setattr(fetch.subprocess, "run", fakeRun)
    with pytest.raises(fetch.FetchError, match="not found on PATH"):
        fetch._run_logcli(_stubSpec(), ["series"])


def test_run_logcli_raises_FetchError_with_stderr_excerpt_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fakeRun(*_a: Any, **_kw: Any) -> _FakeCompleted:
        raise fetch.subprocess.CalledProcessError(
            returncode=1, cmd=["logcli"], output=b"", stderr=b"bad query: parse error\n"
        )

    monkeypatch.setenv("LOKI_PASSWORD", "x")
    monkeypatch.setattr(fetch.subprocess, "run", fakeRun)
    with pytest.raises(fetch.FetchError, match="bad query: parse error"):
        fetch._run_logcli(_stubSpec(), ["series"])


def test_run_logcli_raises_FetchError_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fakeRun(*_a: Any, **_kw: Any) -> _FakeCompleted:
        raise fetch.subprocess.TimeoutExpired(cmd=["logcli"], timeout=1.0)

    monkeypatch.setenv("LOKI_PASSWORD", "x")
    monkeypatch.setattr(fetch.subprocess, "run", fakeRun)
    with pytest.raises(fetch.FetchError, match="timed out"):
        fetch._run_logcli(_stubSpec(), ["series"])


def test_matcher_default_has_cluster_and_namespace_only() -> None:
    spec = _stubSpec()
    assert fetch._matcher(spec) == '{cluster="yagan",namespace="rapid-analysis"}'


def test_matcher_pins_pod_when_given() -> None:
    m = fetch._matcher(_stubSpec(), pod="s-lsstcam-run-sfm-runner-0")
    assert 'pod="s-lsstcam-run-sfm-runner-0"' in m


def test_matcher_uses_podRegex_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from ra_log_explorer.config import FetchSpec

    spec = FetchSpec(
        lokiAddr="x",
        username="u",
        cluster="yagan",
        namespace="rapid-analysis",
        fromIso="x",
        toIso="y",
        podRegex=".*aos.*",
    )
    m = fetch._matcher(spec)
    assert 'pod=~".*aos.*"' in m


def test_listPods_parses_series_output(monkeypatch: pytest.MonkeyPatch) -> None:
    seriesOutput = (
        b'{cluster="yagan", namespace="rapid-analysis", pod="aos-worker-3"}\n'
        b'{cluster="yagan", namespace="rapid-analysis", pod="sfm-runner-1"}\n'
        b'{cluster="yagan", namespace="rapid-analysis", pod="aos-worker-3"}\n'
        # also a line with no `pod=` label — silently dropped.
        b'{cluster="yagan", namespace="rapid-analysis"}\n'
    )

    def fakeRunLogcli(*_a: Any, **_kw: Any) -> bytes:
        return seriesOutput

    monkeypatch.setattr(fetch, "_run_logcli", fakeRunLogcli)
    pods = fetch.listPods(_stubSpec())
    assert pods == ["aos-worker-3", "sfm-runner-1"]  # deduped + sorted


def test_listPods_handles_malformed_series_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unterminated `pod="...` should be ignored, not crash.
    seriesOutput = (
        b"random garbage\n"
        b'{cluster="yagan", pod="ok-pod"}\n'
        b'{cluster="yagan", pod="\n'  # no closing quote on this line
    )

    def fakeRunLogcli(*_a: Any, **_kw: Any) -> bytes:
        return seriesOutput

    monkeypatch.setattr(fetch, "_run_logcli", fakeRunLogcli)
    pods = fetch.listPods(_stubSpec())
    assert pods == ["ok-pod"]


def test_fetchOnePod_writes_stdout_to_outPath_and_returns_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def fakeRunLogcli(_spec: FetchSpec, extraArgs: list[str], timeout: float | None = None) -> bytes:
        captured["extraArgs"] = list(extraArgs)
        captured["timeout"] = timeout
        return b'{"timestamp":"...", "line":"hi\\n"}\n'

    monkeypatch.setattr(fetch, "_run_logcli", fakeRunLogcli)
    out = tmp_path / "pod.jsonl"
    pod, nbytes = fetch._fetchOnePod(_stubSpec(), "s-lsstcam-run-aos-worker-0", out)
    assert pod == "s-lsstcam-run-aos-worker-0"
    assert nbytes == len(out.read_bytes())
    assert out.read_text().startswith('{"timestamp"')
    # The query must use forward order + JSONL output (the file format the
    # parser assumes). Regressing either of those silently breaks the
    # downstream pipeline, so they're worth pinning here.
    assert "--forward" in captured["extraArgs"]
    assert "jsonl" in captured["extraArgs"]
    # Per-pod fetches get a longer timeout than series queries.
    assert captured["timeout"] == 600.0


def test_fetchAll_happy_path_invokes_listPods_and_fetchOnePod(
    monkeypatch: pytest.MonkeyPatch, tmpCacheRoot: Path
) -> None:
    """End-to-end happy path with subprocess fully mocked: listPods returns
    two pods, ``_fetchOnePod`` writes a tiny payload for each, and
    ``fetchAll`` returns a meta block containing both."""

    def fakeListPods(_spec: FetchSpec) -> list[str]:
        return ["aos-0", "sfm-0"]

    fetched: list[str] = []

    def fakeFetchOne(_spec: FetchSpec, pod: str, outPath: Path) -> tuple[str, int]:
        body = f'{{"pod":"{pod}"}}\n'.encode()
        outPath.write_bytes(body)
        fetched.append(pod)
        return pod, len(body)

    # Force `windowInPast` false-positive to trigger a fresh fetch
    # (window is well in the past relative to "now").
    monkeypatch.setattr(fetch, "listPods", fakeListPods)
    monkeypatch.setattr(fetch, "_fetchOnePod", fakeFetchOne)
    spec = _stubSpec()
    cacheDir, meta = fetch.fetchAll(spec)
    assert sorted(fetched) == ["aos-0", "sfm-0"]
    assert meta["pod_count"] == 2
    assert set(meta["pod_bytes"]) == {"aos-0", "sfm-0"}
    assert meta["fromCache"] is False
    assert meta["cacheReuse"] == "none"
    # Files actually landed in the cacheDir/pods subdir.
    assert (cacheDir / "pods" / "aos-0.jsonl").exists()
    assert (cacheDir / "pods" / "sfm-0.jsonl").exists()
    # Partial flag was cleared at the end of a successful run.
    assert not (cacheDir / fetch.PARTIAL_FLAG).exists()


def test_fetchAll_collects_per_pod_errors_without_bailing(
    monkeypatch: pytest.MonkeyPatch, tmpCacheRoot: Path
) -> None:
    """One pod failing shouldn't fail the whole fetch — errors are
    captured per-pod in the meta block."""

    def fakeListPods(_spec: FetchSpec) -> list[str]:
        return ["ok-pod", "bad-pod"]

    def fakeFetchOne(_spec: FetchSpec, pod: str, outPath: Path) -> tuple[str, int]:
        if pod == "bad-pod":
            raise fetch.FetchError("simulated")
        outPath.write_bytes(b"{}\n")
        return pod, 3

    monkeypatch.setattr(fetch, "listPods", fakeListPods)
    monkeypatch.setattr(fetch, "_fetchOnePod", fakeFetchOne)
    _, meta = fetch.fetchAll(_stubSpec())
    assert "bad-pod" in meta["errors"]
    assert "simulated" in meta["errors"]["bad-pod"]
    assert meta["pod_bytes"]["bad-pod"] == 0
    assert meta["pod_bytes"]["ok-pod"] == 3


def test_fetchAll_progress_callback_fires_per_pod(
    monkeypatch: pytest.MonkeyPatch, tmpCacheRoot: Path
) -> None:
    def fakeListPods(_spec: FetchSpec) -> list[str]:
        return ["a", "b", "c"]

    def fakeFetchOne(_spec: FetchSpec, pod: str, outPath: Path) -> tuple[str, int]:
        outPath.write_bytes(b"x")
        return pod, 1

    seen: list[tuple[str, int, int]] = []

    def progress(pod: str, i: int, total: int) -> None:
        seen.append((pod, i, total))

    monkeypatch.setattr(fetch, "listPods", fakeListPods)
    monkeypatch.setattr(fetch, "_fetchOnePod", fakeFetchOne)
    fetch.fetchAll(_stubSpec(), progress=progress)
    assert len(seen) == 3
    # Final tick's index == total.
    assert seen[-1][1] == 3 and seen[-1][2] == 3


def test_fetchAll_reuses_superset_when_no_exact_match(
    monkeypatch: pytest.MonkeyPatch, tmpCacheRoot: Path
) -> None:
    """If we ask for [B, C] and the cache has [A, D] with A<B<C<D, we
    should reuse the wider window rather than firing a fresh fetch."""
    # Plant a superset cache covering an hour-long window.
    superset = fetch.ensureWindowCacheDir(
        "yagan", "rapid-analysis", "2026-05-20T08:00:00Z", "2026-05-20T09:00:00Z"
    )
    (superset / "_meta.json").write_text(
        json.dumps(
            {
                "spec": {
                    "lokiAddr": "x",
                    "username": "u",
                    "cluster": "yagan",
                    "namespace": "rapid-analysis",
                    "fromIso": "2026-05-20T08:00:00Z",
                    "toIso": "2026-05-20T09:00:00Z",
                    "workers": 8,
                    "lineLimit": 50000,
                    "podRegex": None,
                },
                "pod_count": 1,
                "total_bytes": 0,
                "pod_bytes": {},
                "errors": {},
                "window_in_past": True,
                "fromCache": False,
                "cacheReuse": "none",
            }
        )
    )
    from ra_log_explorer.config import FetchSpec

    requestedSpec = FetchSpec(
        lokiAddr="x",
        username="u",
        cluster="yagan",
        namespace="rapid-analysis",
        fromIso="2026-05-20T08:30:00Z",  # narrower window, fully inside superset
        toIso="2026-05-20T08:35:00Z",
    )

    def shouldNotBeCalled(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("listPods/fetchOnePod called despite superset hit")

    monkeypatch.setattr(fetch, "listPods", shouldNotBeCalled)
    monkeypatch.setattr(fetch, "_fetchOnePod", shouldNotBeCalled)
    out, meta = fetch.fetchAll(requestedSpec)
    assert out == superset
    assert meta["cacheReuse"] == "superset"
    assert meta["fromCache"] is True
    assert meta["cacheReusePath"].endswith(superset.name)


def test_fetchAll_returns_exact_cache_hit_without_fetching(
    monkeypatch: pytest.MonkeyPatch, tmpCacheRoot: Path
) -> None:
    """When an exact-spec cache already exists for a past window, no new
    fetch is started."""
    spec = _stubSpec()
    cacheDir = fetch.ensureWindowCacheDir(spec.cluster, spec.namespace, spec.fromIso, spec.toIso)
    (cacheDir / "_meta.json").write_text(
        json.dumps(
            {"spec": dt_asdict(spec), "pod_count": 1, "total_bytes": 0, "errors": {}},
        )
    )

    def shouldNotBeCalled(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("listPods/fetchOnePod called even though cache exists")

    monkeypatch.setattr(fetch, "listPods", shouldNotBeCalled)
    monkeypatch.setattr(fetch, "_fetchOnePod", shouldNotBeCalled)
    out, meta = fetch.fetchAll(spec)
    assert out == cacheDir
    assert meta["cacheReuse"] == "exact"
    assert meta["fromCache"] is True


def dt_asdict(spec: FetchSpec) -> dict[str, Any]:
    """Small shim so the cache _meta.json round-trips."""
    from dataclasses import asdict

    return asdict(spec)


def test_findSupersetCache_does_not_match_across_podRegex(tmpCacheRoot: Path) -> None:
    """An exposure-mode cache (podRegex=None) must NOT be a valid superset
    for a night-mode (podRegex set) fetch, and vice versa. The on-disk pod
    sets are different — pretending they aren't would silently serve up
    incomplete or unrelated data.
    """
    # Plant an unfiltered (exposure-mode) cache covering an hour.
    _writeCache(
        tmpCacheRoot,
        "yagan",
        "rapid-analysis",
        "2026-05-20T08:00:00Z",
        "2026-05-20T09:00:00Z",
    )
    # Ask for a night-mode fetch (.*aos.*) within that hour — must miss.
    assert (
        fetch.findSupersetCache(
            "yagan",
            "rapid-analysis",
            "2026-05-20T08:30:00Z",
            "2026-05-20T08:35:00Z",
            podRegex=".*aos.*",
        )
        is None
    )


def test_findSupersetCache_matches_filtered_to_filtered(tmpCacheRoot: Path) -> None:
    """A night-mode cache (podRegex=X) is a valid superset for another
    night-mode request with the same regex, but only nests one level
    deeper than the exposure-mode layout — make sure the discovery
    walker actually reaches it.
    """
    fromIso, toIso = "2026-05-20T07:00:00Z", "2026-05-20T10:00:00Z"
    fromSlug = fromIso.replace(":", "").replace(".", "_")
    toSlug = toIso.replace(":", "").replace(".", "_")
    # Mirror the on-disk layout: <root>/<cluster>/<ns>/<window>/pods=<slug>/
    nightDir = tmpCacheRoot / "yagan" / "rapid-analysis" / f"{fromSlug}__{toSlug}" / "pods=_aos_"
    (nightDir / "pods").mkdir(parents=True)
    (nightDir / "_meta.json").write_text(
        json.dumps(
            {
                "spec": {
                    "lokiAddr": "x",
                    "username": "u",
                    "cluster": "yagan",
                    "namespace": "rapid-analysis",
                    "fromIso": fromIso,
                    "toIso": toIso,
                    "workers": 8,
                    "lineLimit": 50000,
                    "podRegex": ".*aos.*",
                },
                "pod_count": 1,
                "total_bytes": 0,
                "pod_bytes": {},
                "errors": {},
                "window_in_past": True,
                "fromCache": False,
                "cacheReuse": "none",
            }
        )
    )
    found = fetch.findSupersetCache(
        "yagan", "rapid-analysis", "2026-05-20T08:30:00Z", "2026-05-20T08:35:00Z", podRegex=".*aos.*"
    )
    assert found == nightDir


def test_fetchAll_refetches_when_window_extends_into_future(
    monkeypatch: pytest.MonkeyPatch, tmpCacheRoot: Path
) -> None:
    """If the requested window's end is in the future, we must NOT serve
    the cache — a snapshot from earlier would miss every line that lands
    between cache time and "now". This is the safety net for night-mode
    runs against the current dayObs."""
    spec = FetchSpec(
        lokiAddr="x",
        username="u",
        cluster="yagan",
        namespace="rapid-analysis",
        # toIso 100 years in the future ⇒ window_in_past=False ⇒ refetch.
        fromIso="2026-05-20T08:00:00Z",
        toIso="2126-05-20T09:00:00Z",
    )
    # Plant a complete-looking cache at the same location.
    cacheDir = fetch.ensureWindowCacheDir(spec.cluster, spec.namespace, spec.fromIso, spec.toIso)
    (cacheDir / "_meta.json").write_text(
        json.dumps({"spec": dt_asdict(spec), "pod_count": 5, "total_bytes": 0, "errors": {}})
    )

    listPodsCalled: list[bool] = []

    def fakeListPods(_spec: FetchSpec) -> list[str]:
        listPodsCalled.append(True)
        return []

    monkeypatch.setattr(fetch, "listPods", fakeListPods)
    fetch.fetchAll(spec)
    assert listPodsCalled == [True], "fetchAll must refetch when the window ends in the future"


def test_fetchAll_writes_partial_flag_while_running(
    monkeypatch: pytest.MonkeyPatch, tmpCacheRoot: Path
) -> None:
    """The `.partial` flag is the only signal that a cache dir was
    interrupted mid-fetch. listPods runs first, fetchAll writes
    `.partial` before then; we observe it from inside listPods to pin
    the ordering."""
    spec = _stubSpec()
    partialSeen: list[bool] = []

    def fakeListPods(_spec: FetchSpec) -> list[str]:
        # By now, fetchAll has already written `.partial`.
        cacheDir = fetch.windowCachePath(_spec.cluster, _spec.namespace, _spec.fromIso, _spec.toIso)
        partialSeen.append((cacheDir / fetch.PARTIAL_FLAG).exists())
        return []

    monkeypatch.setattr(fetch, "listPods", fakeListPods)
    fetch.fetchAll(spec)
    assert partialSeen == [True]
    # And the flag is gone after a successful run.
    cacheDir = fetch.windowCachePath(spec.cluster, spec.namespace, spec.fromIso, spec.toIso)
    assert not (cacheDir / fetch.PARTIAL_FLAG).exists()


def test_evictToFit_handles_nested_night_caches(tmpCacheRoot: Path) -> None:
    """Night-mode caches live at <root>/<cluster>/<ns>/<window>/pods=<slug>/.
    The LRU walker must find and evict them — otherwise the cache grows
    forever in night-mode use, which defeats the size cap entirely.
    """
    base = dt.datetime(2026, 5, 21, tzinfo=dt.timezone.utc)
    windowOuter = tmpCacheRoot / "yagan" / "rapid-analysis" / "win-x"
    nightInner = windowOuter / "pods=_aos_"
    (nightInner / "pods").mkdir(parents=True)
    (nightInner / "_meta.json").write_text(
        json.dumps(
            {
                "spec": {
                    "lokiAddr": "x",
                    "username": "u",
                    "cluster": "yagan",
                    "namespace": "rapid-analysis",
                    "fromIso": "2026-05-20T08:00:00Z",
                    "toIso": "2026-05-20T08:05:00Z",
                    "workers": 8,
                    "lineLimit": 50000,
                    "podRegex": ".*aos.*",
                },
                "pod_count": 1,
                "total_bytes": 0,
                "pod_bytes": {},
                "errors": {},
                "window_in_past": True,
                "fromCache": False,
                "cacheReuse": "none",
            }
        )
    )
    (nightInner / "pods" / "fake.jsonl").write_bytes(b"x" * 5000)
    fetch.markCacheViewed(nightInner, when=base - dt.timedelta(days=1))
    removed = fetch.evictToFit(maxBytes=1000)
    # The night cache was the only thing on disk; it must be the one
    # that got evicted.
    assert nightInner in removed
    assert not nightInner.exists()


def test_evictToFit_prunes_empty_parent_dirs(tmpCacheRoot: Path) -> None:
    """When the only cache under a (cluster, ns) tree is evicted, the
    empty parent directories should be pruned too — otherwise the
    cache root accumulates empty cluster/ns scaffolding forever.
    """
    base = dt.datetime(2026, 5, 21, tzinfo=dt.timezone.utc)
    d = _plantWindowWithBody(
        tmpCacheRoot,
        "yagan",
        "rapid-analysis",
        "2026-05-20T08:00:00Z",
        "2026-05-20T08:05:00Z",
        bodyBytes=5000,
        lastViewed=base - dt.timedelta(days=1),
    )
    removed = fetch.evictToFit(maxBytes=0)  # evict everything
    assert d in removed
    # The cluster/ namespace/ scaffolding should also be gone.
    assert not (tmpCacheRoot / "yagan" / "rapid-analysis").exists()
    assert not (tmpCacheRoot / "yagan").exists()
    # The root itself stays so subsequent fetches still work.
    assert tmpCacheRoot.exists()


def test_evictToFit_prunes_empty_window_parent_for_night_cache(tmpCacheRoot: Path) -> None:
    """A night cache lives at ``<root>/<cluster>/<ns>/<window>/pods=<slug>/``.
    When evicted, the now-empty ``<window>/`` and the ``<cluster>/<ns>/``
    scaffolding should also be pruned.
    """
    import json as _json

    base = dt.datetime(2026, 5, 21, tzinfo=dt.timezone.utc)
    windowOuter = tmpCacheRoot / "yagan" / "rapid-analysis" / "win-x"
    nightInner = windowOuter / "pods=__aos__"
    (nightInner / "pods").mkdir(parents=True)
    (nightInner / "_meta.json").write_text(
        _json.dumps(
            {
                "spec": {
                    "lokiAddr": "x",
                    "username": "u",
                    "cluster": "yagan",
                    "namespace": "rapid-analysis",
                    "fromIso": "2026-05-20T08:00:00Z",
                    "toIso": "2026-05-20T08:05:00Z",
                    "workers": 8,
                    "lineLimit": 50000,
                    "podRegex": ".*aos.*",
                },
                "pod_count": 1,
                "total_bytes": 0,
                "pod_bytes": {},
                "errors": {},
                "window_in_past": True,
                "fromCache": False,
                "cacheReuse": "none",
            }
        )
    )
    (nightInner / "pods" / "fake.jsonl").write_bytes(b"x" * 5000)
    fetch.markCacheViewed(nightInner, when=base - dt.timedelta(days=1))
    fetch.evictToFit(maxBytes=0)
    # The pods=<slug> inner dir gets removed first; then the outer
    # window dir is empty, so it goes too; then the namespace dir;
    # then the cluster dir.
    assert not nightInner.exists()
    assert not windowOuter.exists()


def test_evictToFit_treats_unviewed_as_oldest(tmpCacheRoot: Path) -> None:
    base = dt.datetime(2026, 5, 21, tzinfo=dt.timezone.utc)
    unviewed = _plantWindowWithBody(
        tmpCacheRoot,
        "yagan",
        "rapid-analysis",
        "2026-05-20T08:00:00Z",
        "2026-05-20T08:05:00Z",
        bodyBytes=2000,
        lastViewed=None,  # never opened
    )
    viewed = _plantWindowWithBody(
        tmpCacheRoot,
        "yagan",
        "rapid-analysis",
        "2026-05-20T09:00:00Z",
        "2026-05-20T09:05:00Z",
        bodyBytes=2000,
        lastViewed=base - dt.timedelta(days=365),  # very old, but recorded
    )
    # With limit 3000 we must evict one. The never-opened cache should
    # go first — it's the "safest to drop" by design.
    removed = fetch.evictToFit(maxBytes=3000)
    assert unviewed in removed
    assert viewed.exists()
