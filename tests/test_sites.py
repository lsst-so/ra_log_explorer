"""Tests for the site catalog loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from ra_log_explorer import sites


def _writeCatalog(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_loadSites_reads_packaged_catalog() -> None:
    """The checked-in ``sites.toml`` parses cleanly and exposes the
    two sites every deployment ships with today (summit + bts).
    Pinning this so the file can't get truncated / corrupted without a
    test failure flagging it.
    """
    catalog, default = sites.loadSites()
    names = sorted(s.name for s in catalog)
    assert names == ["bts", "summit"]
    assert default in names
    bts = sites.siteByName(catalog, "bts")
    summit = sites.siteByName(catalog, "summit")
    # The cluster ↔ ConsDB pairing is the whole point of the catalog;
    # crossing them would silently send BTS queries to summit (or vice
    # versa), so pin the pairing.
    assert summit.cluster == "yagan"
    assert "usdf-rsp" in summit.consdbUrl
    assert bts.cluster == "manke"
    assert "base-lsp" in bts.consdbUrl


def test_loadSites_uses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``RA_LOG_EXPLORER_SITES_FILE`` redirects the loader; useful for
    ops who want to ship a different catalog without touching the
    package, and (right here) for tests too."""
    p = _writeCatalog(
        tmp_path / "alt.toml",
        'default_site = "x"\n'
        '[[site]]\nname = "x"\ncluster = "c1"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = "~/t"\n',
    )
    monkeypatch.setenv(sites.SITES_FILE_ENV, str(p))
    catalog, default = sites.loadSites()
    assert default == "x"
    assert [s.name for s in catalog] == ["x"]


def test_loadSites_explicit_path_beats_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    envP = _writeCatalog(
        tmp_path / "env.toml",
        'default_site = "envSite"\n'
        '[[site]]\nname = "envSite"\ncluster = "c"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = "~/t"\n',
    )
    overP = _writeCatalog(
        tmp_path / "over.toml",
        'default_site = "overSite"\n'
        '[[site]]\nname = "overSite"\ncluster = "c"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = "~/t"\n',
    )
    monkeypatch.setenv(sites.SITES_FILE_ENV, str(envP))
    catalog, default = sites.loadSites(overP)
    assert default == "overSite"
    assert [s.name for s in catalog] == ["overSite"]


def test_loadSites_expands_tilde_in_token_file(tmp_path: Path) -> None:
    """``~`` in ``consdbTokenFile`` is resolved against the server's HOME
    at load time so callers never have to expand it themselves."""
    p = _writeCatalog(
        tmp_path / "sites.toml",
        'default_site = "s"\n'
        '[[site]]\nname = "s"\ncluster = "c"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = "~/secret/t"\n',
    )
    catalog, _ = sites.loadSites(p)
    assert catalog[0].consdbTokenFile == Path.home() / "secret" / "t"


def test_loadSites_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(sites.SitesConfigError):
        sites.loadSites(tmp_path / "absent.toml")


def test_loadSites_raises_for_malformed_toml(tmp_path: Path) -> None:
    p = _writeCatalog(tmp_path / "broken.toml", "this is = not [ valid toml\n")
    with pytest.raises(sites.SitesConfigError):
        sites.loadSites(p)


def test_loadSites_raises_for_empty_site_list(tmp_path: Path) -> None:
    p = _writeCatalog(tmp_path / "empty.toml", 'default_site = "x"\n')
    with pytest.raises(sites.SitesConfigError):
        sites.loadSites(p)


def test_loadSites_raises_for_unknown_default_site(tmp_path: Path) -> None:
    p = _writeCatalog(
        tmp_path / "missingDefault.toml",
        'default_site = "ghost"\n'
        '[[site]]\nname = "a"\ncluster = "c"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = "~/t"\n',
    )
    with pytest.raises(sites.SitesConfigError):
        sites.loadSites(p)


def test_loadSites_raises_for_missing_field(tmp_path: Path) -> None:
    """A site entry missing a required string field is a config bug
    we'd rather surface at startup than discover at first ConsDB call."""
    p = _writeCatalog(
        tmp_path / "partial.toml",
        'default_site = "a"\n' '[[site]]\nname = "a"\ncluster = "c"\nnamespace = "ns"\n'
        # consdbUrl missing
        'lokiAddr = "https://l"\nconsdbTokenFile = "~/t"\n',
    )
    with pytest.raises(sites.SitesConfigError):
        sites.loadSites(p)


def test_loadSites_raises_for_duplicate_site_name(tmp_path: Path) -> None:
    """Duplicate names would make ``siteByName`` ambiguous; reject up front."""
    p = _writeCatalog(
        tmp_path / "dupes.toml",
        'default_site = "a"\n'
        '[[site]]\nname = "a"\ncluster = "c1"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = "~/t1"\n'
        '[[site]]\nname = "a"\ncluster = "c2"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x2"\nconsdbTokenFile = "~/t2"\n',
    )
    with pytest.raises(sites.SitesConfigError):
        sites.loadSites(p)


def test_loadSites_raises_for_duplicate_cluster(tmp_path: Path) -> None:
    """Two sites on one cluster make ``siteByCluster`` ambiguous, which
    would silently misroute a rehydrated cache (keyed by cluster path
    component) to the wrong ConsDB; reject the catalog up front."""
    p = _writeCatalog(
        tmp_path / "dupeCluster.toml",
        'default_site = "a"\n'
        '[[site]]\nname = "a"\ncluster = "shared"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = "~/t1"\n'
        '[[site]]\nname = "b"\ncluster = "shared"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x2"\nconsdbTokenFile = "~/t2"\n',
    )
    with pytest.raises(sites.SitesConfigError, match="duplicate cluster"):
        sites.loadSites(p)


def test_siteByName_and_siteByCluster_roundtrip(tmp_path: Path) -> None:
    p = _writeCatalog(
        tmp_path / "roundtrip.toml",
        'default_site = "a"\n'
        '[[site]]\nname = "a"\ncluster = "c1"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = "~/t"\n'
        '[[site]]\nname = "b"\ncluster = "c2"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://y"\nconsdbTokenFile = "~/t"\n',
    )
    catalog, _ = sites.loadSites(p)
    assert sites.siteByName(catalog, "a").cluster == "c1"
    assert sites.siteByCluster(catalog, "c2").name == "b"


def test_siteByName_raises_for_unknown(tmp_path: Path) -> None:
    p = _writeCatalog(
        tmp_path / "x.toml",
        'default_site = "a"\n'
        '[[site]]\nname = "a"\ncluster = "c"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = "~/t"\n',
    )
    catalog, _ = sites.loadSites(p)
    with pytest.raises(sites.SitesConfigError):
        sites.siteByName(catalog, "ghost")


def test_siteByCluster_raises_for_unknown(tmp_path: Path) -> None:
    p = _writeCatalog(
        tmp_path / "x.toml",
        'default_site = "a"\n'
        '[[site]]\nname = "a"\ncluster = "c"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = "~/t"\n',
    )
    catalog, _ = sites.loadSites(p)
    with pytest.raises(sites.SitesConfigError):
        sites.siteByCluster(catalog, "ghost")


def test_loadSites_allows_a_site_with_no_token_file(tmp_path: Path) -> None:
    """A ConsDB reached in-cluster needs no bearer token, so the field is
    optional and its absence must load as ``None`` rather than raising."""
    p = _writeCatalog(
        tmp_path / "x.toml",
        'default_site = "incluster"\n'
        '[[site]]\nname = "incluster"\ncluster = "manke"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "http://consdb-pq.consdb:8080/consdb/query"\n',
    )
    catalog, _ = sites.loadSites(p)
    assert catalog[0].consdbTokenFile is None


def test_loadSites_treats_blank_token_file_as_absent(tmp_path: Path) -> None:
    """An empty string is what a Helm template renders for "no token"; it
    must mean the same thing as omitting the key, not a path of ``''``."""
    p = _writeCatalog(
        tmp_path / "x.toml",
        'default_site = "a"\n'
        '[[site]]\nname = "a"\ncluster = "c"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = ""\n',
    )
    catalog, _ = sites.loadSites(p)
    assert catalog[0].consdbTokenFile is None


def test_loadSites_rejects_non_string_token_file(tmp_path: Path) -> None:
    p = _writeCatalog(
        tmp_path / "x.toml",
        'default_site = "a"\n'
        '[[site]]\nname = "a"\ncluster = "c"\nnamespace = "ns"\n'
        'lokiAddr = "https://l"\nconsdbUrl = "https://x"\nconsdbTokenFile = 7\n',
    )
    with pytest.raises(sites.SitesConfigError):
        sites.loadSites(p)
