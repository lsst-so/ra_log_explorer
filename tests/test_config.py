"""Tests for `ra_log_explorer.config`."""

from __future__ import annotations

import re
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


def test_NIGHT_AOS_POD_REGEX_constant_is_stable() -> None:
    """The night-mode regex is part of the on-disk cache key (it slugs
    into ``pods=<slug>/``). Pin it so a refactor doesn't silently
    invalidate every existing night cache.
    """
    assert config.NIGHT_AOS_POD_REGEX == ".*aos.*"


def test_dayObsStartUtc_dayObsEndUtc_round_trip_24h() -> None:
    """The two helpers must produce exactly 24 hours apart for the same
    dayObs — the night window relies on this.
    """
    start = config.dayObsStartUtc(20260521)
    end = config.dayObsEndUtc(20260521)
    assert end - start == __import__("datetime").timedelta(hours=24)


def test_dayObsStartUtc_alignment_at_year_boundary() -> None:
    """A dayObs whose midnight-UTC-12 rolls into the next calendar year
    should still resolve correctly — the helper subtracts no calendar
    arithmetic of its own, just adds 12h to the local-midnight UTC.
    """
    import datetime as _dt

    start = config.dayObsStartUtc(20251231)
    assert start == _dt.datetime(2025, 12, 31, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # The end therefore lands on Jan 1.
    end = config.dayObsEndUtc(20251231)
    assert end == _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)


# ----- base path -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("/", ""),
        ("///", ""),
        ("/log-explorer", "/log-explorer"),
        ("log-explorer", "/log-explorer"),
        ("/log-explorer/", "/log-explorer"),
        ("  /log-explorer/  ", "/log-explorer"),
        ("/a/b", "/a/b"),
    ],
)
def test_normalizeBasePath(raw: str | None, expected: str) -> None:
    """Anything a human or a Helm value might supply collapses to the one
    canonical form the router and the HTML template both assume."""
    assert config.normalizeBasePath(raw) == expected


def test_defaultBasePath_reads_and_normalizes_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.BASE_PATH_ENV, "log-explorer/")
    assert config.defaultBasePath() == "/log-explorer"
    monkeypatch.delenv(config.BASE_PATH_ENV)
    assert config.defaultBasePath() == ""


# ----- environment-driven configuration ------------------------------------


def test_envInt_and_envFloat_return_the_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RA_LOG_EXPLORER_TEST_KNOB", raising=False)
    assert config._envInt("RA_LOG_EXPLORER_TEST_KNOB", 7) == 7
    assert config._envFloat("RA_LOG_EXPLORER_TEST_KNOB", 1.5) == 1.5
    # An empty or whitespace-only value is what an unset Helm value renders
    # as; it must mean "unset", not "malformed".
    monkeypatch.setenv("RA_LOG_EXPLORER_TEST_KNOB", "   ")
    assert config._envInt("RA_LOG_EXPLORER_TEST_KNOB", 7) == 7
    assert config._envFloat("RA_LOG_EXPLORER_TEST_KNOB", 1.5) == 1.5


def test_envInt_and_envFloat_read_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RA_LOG_EXPLORER_TEST_KNOB", "16")
    assert config._envInt("RA_LOG_EXPLORER_TEST_KNOB", 7) == 16
    assert config._envFloat("RA_LOG_EXPLORER_TEST_KNOB", 1.5) == 16.0


def test_envInt_and_envFloat_raise_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd deployment value must stop the container, not silently
    revert to the built-in default — a setting that quietly never took
    effect is far harder to notice than one that refuses to start."""
    monkeypatch.setenv("RA_LOG_EXPLORER_TEST_KNOB", "eight")
    with pytest.raises(config.ConfigError, match="RA_LOG_EXPLORER_TEST_KNOB"):
        config._envInt("RA_LOG_EXPLORER_TEST_KNOB", 7)
    with pytest.raises(config.ConfigError, match="RA_LOG_EXPLORER_TEST_KNOB"):
        config._envFloat("RA_LOG_EXPLORER_TEST_KNOB", 1.5)
    # A float is not an int; the worker count must not silently truncate.
    monkeypatch.setenv("RA_LOG_EXPLORER_TEST_KNOB", "8.5")
    with pytest.raises(config.ConfigError):
        config._envInt("RA_LOG_EXPLORER_TEST_KNOB", 7)


def test_module_constants_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The knobs the UI used to expose are now read once, at import, from
    the environment. Reload the module under a patched environment to pin
    that each name is actually wired to its variable."""
    import importlib

    monkeypatch.setenv("RA_LOG_EXPLORER_WORKERS", "3")
    monkeypatch.setenv("RA_LOG_EXPLORER_WINDOW_BEFORE_S", "12.5")
    monkeypatch.setenv("RA_LOG_EXPLORER_WINDOW_AFTER_S", "600")
    monkeypatch.setenv("RA_LOG_EXPLORER_MAX_CACHE_BYTES", "1234567")
    monkeypatch.setenv("RA_LOG_EXPLORER_LIVE_POLL_S", "123")
    monkeypatch.setenv("RA_LOG_EXPLORER_LIVE_LAG_S", "45")
    monkeypatch.setenv("LOKI_USERNAME", "omega")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.DEFAULT_WORKERS == 3
        assert reloaded.DEFAULT_WINDOW_BEFORE_S == 12.5
        assert reloaded.DEFAULT_WINDOW_AFTER_S == 600.0
        assert reloaded.MAX_CACHE_BYTES == 1234567
        # Distinct values so swapping the two live names would be caught.
        assert reloaded.LIVE_POLL_S == 123.0
        assert reloaded.LIVE_LAG_S == 45.0
        assert reloaded.DEFAULT_USERNAME == "omega"
    finally:
        # Other modules hold references to this module object; leaving it
        # reloaded under a patched environment would leak into them.
        monkeypatch.undo()
        importlib.reload(config)


def test_cacheRoot_ignores_a_stale_settings_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`cache_root` used to consult a persisted settings JSON that the UI
    wrote. That file is gone; one left over from an older version must not
    still be able to redirect where a deployment writes its cache."""
    monkeypatch.delenv("RA_LOG_EXPLORER_CACHE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    stale = tmp_path / ".config" / "ra_log_explorer"
    stale.mkdir(parents=True)
    (stale / "settings.json").write_text('{"cacheDir": "/somewhere/else"}')
    assert config.cache_root() == tmp_path / ".cache" / "ra_log_explorer"


def test_the_set_of_environment_variables_is_pinned() -> None:
    """Tripwire for the cross-repo contract.

    Every one of these is set by the Phalanx chart in a *different*
    repository (`applications/log-explorer/`). Adding one here without
    adding it there gives a production deployment that silently runs on
    the built-in default — a setting that appears to do nothing, which is
    the sort of thing nobody notices for months. Renaming one without
    renaming it there is the same failure with an extra step.

    So: if this test fails, the change is fine, but it is not finished
    until the chart matches. Update the list, then update
    `values.yaml`, `templates/deployment.yaml`, and the Configuration
    tables in `README.md` and `architecture/architecture.md`.
    """
    package = Path(config.__file__).parent
    found: set[str] = set()
    for module in sorted(package.glob("*.py")):
        found |= set(re.findall(r'"(RA_LOG_EXPLORER_[A-Z_]+|LOKI_[A-Z_]+)"', module.read_text()))
    assert found == {
        "RA_LOG_EXPLORER_BASE_PATH",
        "RA_LOG_EXPLORER_CACHE",
        "RA_LOG_EXPLORER_LIVE_LAG_S",
        "RA_LOG_EXPLORER_LIVE_POLL_S",
        "RA_LOG_EXPLORER_MAX_CACHE_BYTES",
        "RA_LOG_EXPLORER_SITES_FILE",
        "RA_LOG_EXPLORER_WINDOW_AFTER_S",
        "RA_LOG_EXPLORER_WINDOW_BEFORE_S",
        "RA_LOG_EXPLORER_WORKERS",
        "LOKI_PASSWORD",
        "LOKI_USERNAME",
    }


def test_defaults_are_usable_with_no_environment_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A laptop run needs no configuration. Every variable is optional
    and the built-in defaults have to add up to a working local server,
    or the development mode stops being a development convenience."""
    import importlib

    for name in (
        "RA_LOG_EXPLORER_BASE_PATH",
        "RA_LOG_EXPLORER_CACHE",
        "RA_LOG_EXPLORER_MAX_CACHE_BYTES",
        "RA_LOG_EXPLORER_WINDOW_AFTER_S",
        "RA_LOG_EXPLORER_WINDOW_BEFORE_S",
        "RA_LOG_EXPLORER_WORKERS",
        "LOKI_USERNAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        c = importlib.reload(config)
        assert c.defaultBasePath() == ""
        assert c.DEFAULT_WORKERS > 0
        assert c.DEFAULT_WINDOW_AFTER_S > c.DEFAULT_WINDOW_BEFORE_S >= 0
        assert c.MAX_CACHE_BYTES > 0
        assert c.DEFAULT_USERNAME
        assert c.cache_root() == tmp_path / ".cache" / "ra_log_explorer"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
