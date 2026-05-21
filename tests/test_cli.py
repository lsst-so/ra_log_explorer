"""Tests for `ra_log_explorer.cli` (pure helpers only)."""

from __future__ import annotations

import datetime as dt

import pytest

from ra_log_explorer import cli


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


def test_main_returns_2_when_required_args_missing(capsys: pytest.CaptureFixture[str]) -> None:
    # Implicit run mode without --exposure-id / --t-zero prints help and exits 2.
    rc = cli.main([])
    assert rc == 2
