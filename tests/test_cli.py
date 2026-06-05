"""Tests for `ra_log_explorer.cli` (pure helpers only)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Callable

import pytest

from ra_log_explorer import cli
from ra_log_explorer.config import FetchSpec


def test_parseIsoUtc_z_suffix() -> None:
    t = cli._parseIsoUtc("2026-05-20T08:45:39Z")
    assert t == dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)


def test_parseIsoUtc_no_offset_assumed_utc() -> None:
    t = cli._parseIsoUtc("2026-05-20T08:45:39")
    assert t == dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)


def test_parseIsoUtc_explicit_offset() -> None:
    t = cli._parseIsoUtc("2026-05-20T09:45:39+01:00")
    assert t == dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)


def test_parseIsoUtc_microseconds() -> None:
    t = cli._parseIsoUtc("2026-05-20T08:45:39.267000")
    assert t == dt.datetime(2026, 5, 20, 8, 45, 39, 267000, tzinfo=dt.timezone.utc)


def test_parseIsoUtc_explicit_negative_offset() -> None:
    """The "tz detection" check looks at ``s[10:]`` for ``-``; a
    negative offset must be honoured, not get a UTC default appended."""
    t = cli._parseIsoUtc("2026-05-20T05:45:39-03:00")
    # 05:45 UTC-3 == 08:45 UTC
    assert t == dt.datetime(2026, 5, 20, 8, 45, 39, tzinfo=dt.timezone.utc)


def test_isoForLogcli_ends_with_Z() -> None:
    t = dt.datetime(2026, 5, 20, 8, 45, 39, 267000, tzinfo=dt.timezone.utc)
    s = cli._isoForLogcli(t)
    assert s.endswith("Z")
    assert s.startswith("2026-05-20T08:45:39")


def test_isoForLogcli_converts_offset_to_utc() -> None:
    t = dt.datetime(2026, 5, 20, 9, 45, 39, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    s = cli._isoForLogcli(t)
    # 09:45 +01:00 == 08:45 UTC
    assert "08:45:39" in s
    assert s.endswith("Z")


def test_TAI_minus_UTC_constant_is_37_seconds() -> None:
    # Pin the constant; if leap seconds ever resume, the change here will
    # also need to land in cli.py + the architecture docs.
    assert cli.TAI_MINUS_UTC_S == 37.0


def test_build_parser_run_accepts_required_args() -> None:
    p = cli.build_parser()
    ns = p.parse_args(["run", "--exposure-id", "2026051900722", "--t-zero", "2026-05-20T08:46:16.267"])
    assert ns.exposure_id == 2026051900722
    assert ns.t_zero == "2026-05-20T08:46:16.267"
    assert ns.t_zero_utc is False


def test_build_parser_accepts_t_zero_utc_flag() -> None:
    p = cli.build_parser()
    ns = p.parse_args(
        [
            "run",
            "--exposure-id",
            "2026051900722",
            "--t-zero",
            "2026-05-20T08:45:39.267",
            "--t-zero-utc",
        ]
    )
    assert ns.t_zero_utc is True


def test_build_parser_cache_info() -> None:
    p = cli.build_parser()
    ns = p.parse_args(["cache", "info"])
    assert ns.fn is cli.cmdCacheInfo


def test_build_parser_cache_flush_with_yes() -> None:
    p = cli.build_parser()
    ns = p.parse_args(["cache", "flush", "--yes"])
    assert ns.fn is cli.cmdCacheFlush
    assert ns.yes is True


def test_warnIfIncompleteFetch_silent_when_complete(capsys: pytest.CaptureFixture[str]) -> None:
    cli._warnIfIncompleteFetch({"errors": {}}, "/tmp/cache")
    assert capsys.readouterr().err == ""


def test_warnIfIncompleteFetch_shouts_and_lists_failed_pods(
    capsys: pytest.CaptureFixture[str],
) -> None:
    meta = {"errors": {"aos-worker-7": "logcli timed out", "sfm-runner-1": "rc=1: 502"}}
    cli._warnIfIncompleteFetch(meta, "/tmp/cache")
    err = capsys.readouterr().err
    assert "INCOMPLETE FETCH" in err
    assert "2 pod(s) failed" in err
    assert "aos-worker-7" in err and "sfm-runner-1" in err
    assert "--force-refresh" in err


def test_warnIfIncompleteFetch_caps_the_pod_list(capsys: pytest.CaptureFixture[str]) -> None:
    meta = {"errors": {f"pod-{i}": "boom" for i in range(20)}}
    cli._warnIfIncompleteFetch(meta, "/tmp/cache")
    err = capsys.readouterr().err
    assert "20 pod(s) failed" in err
    assert "and 8 more" in err  # 20 - 12 shown


def test_cmdRun_rejects_partial_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Supplying --exposure-id without --t-zero (or vice-versa) is an error."""
    # We test cmdRun directly rather than main(): main([]) would start the
    # server (home mode), which we don't want to do in a unit test.
    args = cli.build_parser().parse_args(["run", "--exposure-id", "2026051900722"])
    rc = cli.cmdRun(args)
    assert rc == 2
    captured = capsys.readouterr()
    assert "must be supplied together" in captured.err


# ----- cmdCacheInfo + cmdCacheFlush --------------------------------------


def test_cmdCacheInfo_empty_cache_prints_empty_marker(
    tmpCacheRoot: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = cli.build_parser().parse_args(["cache", "info"])
    rc = cli.cmdCacheInfo(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cache root" in out
    assert "(empty)" in out


def test_cmdCacheInfo_lists_windows(
    tmpCacheRoot: Path, fakeCachedWindow: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = cli.build_parser().parse_args(["cache", "info"])
    rc = cli.cmdCacheInfo(args)
    assert rc == 0
    out = capsys.readouterr().out
    # The fixture has a complete _meta.json, so the listing tags it "ok".
    assert "[ok ]" in out
    assert fakeCachedWindow.name in out
    # Cluster / namespace from the fixture should appear in the path tail.
    assert "yagan" in out and "rapid-analysis" in out


def test_cmdCacheInfo_tags_partial_windows(tmpCacheRoot: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A window dir with no ``_meta.json`` (e.g. a half-finished
    fetch) should show up as ``partial`` rather than being silently
    hidden or crashing the listing."""
    d = tmpCacheRoot / "yagan" / "rapid-analysis" / "abandoned"
    d.mkdir(parents=True)
    args = cli.build_parser().parse_args(["cache", "info"])
    rc = cli.cmdCacheInfo(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[partial]" in out
    assert "abandoned" in out


def test_cmdCacheInfo_lists_night_caches_under_pods_subdir(
    tmpCacheRoot: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Night-mode caches live one level deeper than exposure caches —
    ``<window>/pods=<slug>/_meta.json`` rather than
    ``<window>/_meta.json``. The CLI ``cache info`` listing must
    surface them as proper entries (with the ``[ok]`` tag) and not
    mislabel them as ``[partial]`` just because the outer window dir
    has no ``_meta.json`` of its own.
    """
    import json as _json

    windowDir = tmpCacheRoot / "yagan" / "rapid-analysis" / "2026-05-21T120000Z__2026-05-22T120000Z"
    nightDir = windowDir / "pods=__aos__"
    (nightDir / "pods").mkdir(parents=True)
    (nightDir / "_meta.json").write_text(
        _json.dumps(
            {
                "spec": {
                    "lokiAddr": "x",
                    "username": "u",
                    "cluster": "yagan",
                    "namespace": "rapid-analysis",
                    "fromIso": "2026-05-21T12:00:00Z",
                    "toIso": "2026-05-22T12:00:00Z",
                    "workers": 8,
                    "podRegex": ".*aos.*",
                },
                "pod_count": 1,
                "total_bytes": 0,
                "errors": {},
            }
        )
    )
    args = cli.build_parser().parse_args(["cache", "info"])
    rc = cli.cmdCacheInfo(args)
    assert rc == 0
    out = capsys.readouterr().out
    # The night cache must be visible — its ``pods=__aos__`` subdir
    # path should appear on a row tagged ``[ok ]``.
    assert "pods=__aos__" in out, f"night cache not listed in:\n{out}"
    nightRow = next(line for line in out.splitlines() if "pods=__aos__" in line)
    assert "[ok ]" in nightRow, f"night cache mislabeled partial: {nightRow}"


def test_cmdCacheInfo_window_with_meta_and_partial_inner_shows_only_outer(
    tmpCacheRoot: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If an outer window has its own ``_meta.json`` (an exposure-mode
    cache) and a sibling night cache subdir exists but is partial
    (no ``_meta.json``), the listing shows only the exposure row.
    """
    import json as _json

    windowDir = tmpCacheRoot / "yagan" / "rapid-analysis" / "ex"
    (windowDir / "pods").mkdir(parents=True)
    (windowDir / "_meta.json").write_text(_json.dumps({"spec": {}}))
    # Plant a partial sibling night cache under the same window.
    inner = windowDir / "pods=__aos__"
    inner.mkdir()
    args = cli.build_parser().parse_args(["cache", "info"])
    rc = cli.cmdCacheInfo(args)
    assert rc == 0
    out = capsys.readouterr().out
    # The exposure row appears once, and the partial inner is not
    # surfaced (it has no ``_meta.json``).
    assert "[ok ]" in out
    assert "pods=__aos__" not in out


def test_cmdCacheInfo_coexisting_outer_and_inner_caches_both_listed(
    tmpCacheRoot: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exposure cache (outer window has ``_meta.json``) and a night
    cache (inner ``pods=<slug>/_meta.json``) can coexist under the
    same window dir — the architecture explicitly allows this. The
    CLI listing must surface BOTH rows, matching what the server's
    /api/cache endpoint returns.
    """
    import json as _json

    windowDir = tmpCacheRoot / "yagan" / "rapid-analysis" / "ex"
    (windowDir / "pods").mkdir(parents=True)
    (windowDir / "_meta.json").write_text(_json.dumps({"spec": {}}))
    inner = windowDir / "pods=__aos__"
    (inner / "pods").mkdir(parents=True)
    (inner / "_meta.json").write_text(_json.dumps({"spec": {"podRegex": ".*aos.*"}}))
    args = cli.build_parser().parse_args(["cache", "info"])
    rc = cli.cmdCacheInfo(args)
    assert rc == 0
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if "[ok ]" in line]
    # One row for the outer window, one for the inner pods= subdir.
    assert len(rows) == 2, f"expected 2 ok rows, got {len(rows)} in:\n{out}"
    assert any("pods=__aos__" in r for r in rows)
    assert any("/ex" in r and "pods=" not in r for r in rows)


def test_cmdCacheFlush_with_yes_deletes_root(
    tmpCacheRoot: Path, fakeCachedWindow: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = cli.build_parser().parse_args(["cache", "flush", "--yes"])
    rc = cli.cmdCacheFlush(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cache flushed" in out
    assert not tmpCacheRoot.exists()


def test_cmdCacheFlush_without_yes_respects_no_input(
    tmpCacheRoot: Path,
    fakeCachedWindow: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Answering anything but 'y' to the prompt must leave the cache
    intact — the prompt is the only safety net for ``--yes`` being
    omitted."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    args = cli.build_parser().parse_args(["cache", "flush"])
    rc = cli.cmdCacheFlush(args)
    assert rc == 0
    out = capsys.readouterr().out
    # No "Cache flushed" message — the prompt was declined.
    assert "Cache flushed" not in out
    # Cache contents survived.
    assert fakeCachedWindow.exists()


# ----- main() dispatch + cmdRun home mode --------------------------------


def test_main_no_args_dispatches_to_cmdRun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing no subcommand should land in cmdRun (the default).
    We stub ``serve`` so we don't actually open a socket."""
    serveCalls: list[tuple] = []

    def fakeServe(host: str, port: int, ctx: cli.ServerContext) -> None:
        serveCalls.append((host, port))

    monkeypatch.setattr(cli, "serve", fakeServe)
    monkeypatch.setattr(cli, "webbrowser", type("S", (), {"open": lambda _u: None})())
    rc = cli.main(["--port", "0", "--no-browser"])
    assert rc == 0
    assert serveCalls == [("127.0.0.1", 0)]


def test_eagerFetch_builds_state_with_tai_to_utc_conversion(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI --t-zero flag defaults to TAI. ``_eagerFetchAndBuildState``
    must apply the 37-second offset before forming the fetch window —
    a subtle bug here would skew every fetched window by ~37s and
    drop the actual shutter close from the window."""
    from ra_log_explorer import cli
    from ra_log_explorer import parse as _parse

    # Stub fetchAll: just create a fake cache dir + return it.
    def fakeFetchAll(
        spec: FetchSpec,
        progress: Callable[[str, int, int], None] | None = None,
        forceRefresh: bool = False,
    ) -> tuple[Path, dict[str, Any]]:
        d = tmpCacheRoot / "fake"
        (d / "pods").mkdir(parents=True)
        return d, {"pod_count": 0, "total_bytes": 0, "elapsed_s": 0.0, "cacheReuse": "none"}

    monkeypatch.setattr(cli, "fetchAll", fakeFetchAll)
    monkeypatch.setattr(_parse, "summarizeAll", lambda _d: [])

    args = cli.build_parser().parse_args(
        [
            "run",
            "--exposure-id",
            "2026051900722",
            "--t-zero",
            "2026-05-20T08:46:16.267",  # TAI by default
            "--no-serve",
            "--no-browser",
        ]
    )
    state = cli._eagerFetchAndBuildState(args, cli._resolveSite(args))
    # 08:46:16.267 TAI - 37s = 08:45:39.267 UTC.
    assert state.tZero.hour == 8 and state.tZero.minute == 45 and state.tZero.second == 39
    assert state.expId == 2026051900722
    # Reference points carry a "TAI input" label so the UI can show
    # which scale the user typed in.
    assert state.referencePoints[0]["source"] == "shutter close"


def test_eagerFetch_utc_flag_skips_tai_conversion(
    tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --t-zero-utc the input is taken as UTC and no offset is
    applied. Regressing this would silently shift every UTC-mode
    fetch by 37s."""
    from ra_log_explorer import cli
    from ra_log_explorer import parse as _parse

    def fakeFetchAll(
        spec: FetchSpec,
        progress: Callable[[str, int, int], None] | None = None,
        forceRefresh: bool = False,
    ) -> tuple[Path, dict[str, Any]]:
        d = tmpCacheRoot / "fake-utc"
        (d / "pods").mkdir(parents=True)
        return d, {"pod_count": 0, "total_bytes": 0, "elapsed_s": 0.0, "cacheReuse": "none"}

    monkeypatch.setattr(cli, "fetchAll", fakeFetchAll)
    monkeypatch.setattr(_parse, "summarizeAll", lambda _d: [])
    args = cli.build_parser().parse_args(
        [
            "run",
            "--exposure-id",
            "1",
            "--t-zero",
            "2026-05-20T08:45:39.267",
            "--t-zero-utc",
            "--no-serve",
            "--no-browser",
        ]
    )
    state = cli._eagerFetchAndBuildState(args, cli._resolveSite(args))
    # Same numeric value: no conversion applied.
    assert state.tZero.second == 39


def test_cmdRun_home_mode_starts_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """No --exposure-id + no --t-zero ⇒ home mode: cmdRun should just
    start the server with an empty ServerContext."""
    captured: dict[str, "cli.ServerContext"] = {}

    def fakeServe(host: str, port: int, ctx: cli.ServerContext) -> None:
        captured["ctx"] = ctx

    monkeypatch.setattr(cli, "serve", fakeServe)
    monkeypatch.setattr(cli, "webbrowser", type("S", (), {"open": lambda _u: None})())
    args = cli.build_parser().parse_args(["run", "--no-browser"])
    rc = cli.cmdRun(args)
    assert rc == 0
    # No states loaded — the user lands on the home view.
    ctx = captured["ctx"]
    assert len(ctx.exposureStates) == 0
    assert len(ctx.nightStates) == 0


def test_eagerFetch_force_refresh_propagates(tmpCacheRoot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--force-refresh`` must reach `fetchAll` as `forceRefresh=True`.
    Without it, cached results would silently override the user's
    explicit refresh request."""
    from ra_log_explorer import cli
    from ra_log_explorer import parse as _parse

    captured: dict[str, bool] = {}

    def fakeFetchAll(
        spec: FetchSpec,
        progress: Callable[[str, int, int], None] | None = None,
        forceRefresh: bool = False,
    ) -> tuple[Path, dict[str, Any]]:
        captured["forceRefresh"] = forceRefresh
        d = tmpCacheRoot / "fake-force"
        (d / "pods").mkdir(parents=True)
        return d, {"pod_count": 0, "total_bytes": 0, "elapsed_s": 0.0, "cacheReuse": "none"}

    monkeypatch.setattr(cli, "fetchAll", fakeFetchAll)
    monkeypatch.setattr(_parse, "summarizeAll", lambda _d: [])
    args = cli.build_parser().parse_args(
        [
            "run",
            "--exposure-id",
            "2026051900722",
            "--t-zero",
            "2026-05-20T08:46:16.267",
            "--force-refresh",
            "--no-serve",
            "--no-browser",
        ]
    )
    cli._eagerFetchAndBuildState(args, cli._resolveSite(args))
    assert captured["forceRefresh"] is True


def test_cmdCacheFlush_on_empty_cache_root_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the cache root doesn't exist at all, flush has nothing to do
    and shouldn't prompt the user (or, worse, crash)."""
    # Point cache_root at a path that doesn't exist, *without* creating it.
    fakeRoot = tmp_path / "does-not-exist"
    monkeypatch.setattr(cli, "cache_root", lambda: fakeRoot)
    args = cli.build_parser().parse_args(["cache", "flush", "--yes"])
    rc = cli.cmdCacheFlush(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing to flush" in out
