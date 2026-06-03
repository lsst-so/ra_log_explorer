"""Tests for `ra_log_explorer.appSettings`."""

from __future__ import annotations

from pathlib import Path

import pytest

from ra_log_explorer import appSettings


def test_loadAppSettings_returns_defaults_when_no_file(tmpCacheRoot: Path) -> None:
    s = appSettings.loadAppSettings()
    assert s.maxCacheBytes == appSettings.DEFAULT_MAX_CACHE_BYTES


def test_save_then_load_roundtrips(tmpCacheRoot: Path) -> None:
    appSettings.saveAppSettings(appSettings.AppSettings(maxCacheBytes=123456789))
    loaded = appSettings.loadAppSettings()
    assert loaded.maxCacheBytes == 123456789


def _plantSettings(text: str) -> None:
    """Write the given raw text to the settings file, creating the
    parent directory as needed (the settings file now lives at
    ``<config-dir>/settings.json``, which is not auto-created by the
    cache machinery)."""
    path = appSettings.settingsPath()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_loadAppSettings_falls_back_on_corrupt_file(tmpCacheRoot: Path) -> None:
    # Plant a non-JSON file at the settings path — load should not raise.
    _plantSettings("not valid json")
    s = appSettings.loadAppSettings()
    assert s.maxCacheBytes == appSettings.DEFAULT_MAX_CACHE_BYTES


def test_loadAppSettings_falls_back_on_missing_field(tmpCacheRoot: Path) -> None:
    # An empty JSON object should yield default fields, not crash.
    _plantSettings("{}")
    s = appSettings.loadAppSettings()
    assert s.maxCacheBytes == appSettings.DEFAULT_MAX_CACHE_BYTES


def test_loadAppSettings_ignores_unknown_fields(tmpCacheRoot: Path) -> None:
    """A future version may add fields; an older client should still read
    its known field without choking on the extras."""
    import json as _json

    _plantSettings(_json.dumps({"maxCacheBytes": 42, "futureKnob": "abc", "another": 1}))
    s = appSettings.loadAppSettings()
    assert s.maxCacheBytes == 42


def test_saveAppSettings_creates_settings_dir_if_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """saveAppSettings claims to create the parent dir if needed — pin
    that so a fresh install (no ``~/.config/ra_log_explorer/`` yet)
    works on first settings write."""
    monkeypatch.setenv("RA_LOG_EXPLORER_CONFIG_DIR", str(tmp_path / "fresh-config-dir"))
    monkeypatch.setenv("RA_LOG_EXPLORER_CACHE", str(tmp_path / "cache"))
    appSettings.saveAppSettings(appSettings.AppSettings(maxCacheBytes=7))
    assert appSettings.loadAppSettings().maxCacheBytes == 7


def test_save_then_load_roundtrips_cacheDir(tmpCacheRoot: Path) -> None:
    """The cacheDir override roundtrips through save/load."""
    appSettings.saveAppSettings(appSettings.AppSettings(maxCacheBytes=42, cacheDir="/tmp/some-cache"))
    loaded = appSettings.loadAppSettings()
    assert loaded.cacheDir == "/tmp/some-cache"
    # And clearing it works.
    appSettings.saveAppSettings(appSettings.AppSettings(maxCacheBytes=42, cacheDir=None))
    assert appSettings.loadAppSettings().cacheDir is None
