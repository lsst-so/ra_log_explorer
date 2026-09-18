"""Per-deployment site catalog.

A *site* pairs a Loki cluster (where the logs live) with the ConsDB
endpoint that owns the shutter-close times for that cluster's data.
The pairing matters: the summit cluster (``yagan``) writes against the
real-camera ConsDB on USDF, while the Base Test Stand cluster
(``manke``) writes against its own simulated-data ConsDB on
``base-lsp.lsst.codes``. The same dataId can resolve to a different
``obs_end`` depending on which side you ask.

The canonical list of sites lives in the checked-in
:data:`PACKAGED_SITES_FILE` (``sites.toml`` alongside this module).
Set ``RA_LOG_EXPLORER_SITES_FILE`` to point at a different file for
ops overrides; tests pass an explicit path to :func:`loadSites`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

SITES_FILE_ENV = "RA_LOG_EXPLORER_SITES_FILE"
PACKAGED_SITES_FILE = Path(__file__).parent / "sites.toml"

# What the page calls itself, per site — the browser tab, and the words
# beside the logo in every view's topbar. Keyed by site name and held
# here rather than required in the catalog because a deployment's catalog
# is rendered by the Phalanx chart in another repository, which knows
# nothing of titles: requiring the field would leave both deployed
# instances showing a name nobody calls them. A catalog entry may still
# set `title` outright, which is where a site added later should say what
# it wants to be called.
SITE_TITLES = {
    "summit": "Summit Log Explorer",
    "bts": "Base Log Explorer",
}
# For a site with neither an entry above nor a `title` in the catalog.
# Deliberately plain: a wrong name is worse than no name.
DEFAULT_TITLE = "Log Explorer"


@dataclass(frozen=True)
class Site:
    """One deployment's (Loki cluster, ConsDB) pairing.

    ``consdbTokenFile`` is the on-disk path the server reads the bearer
    token from when calling ``consdbUrl``. It's resolved with ``~``
    expansion against the *server's* HOME at load time. ``None`` means
    the endpoint takes no token at all — which is the case when
    ``consdbUrl`` is a cluster-internal Service address, reached inside
    the same cluster and so never passing through Gafaelfawr.
    """

    name: str  # short slug — "summit", "bts" — also used as the cache-key
    cluster: str  # Loki `cluster` label
    namespace: str  # Loki `namespace` label
    lokiAddr: str  # Loki HTTP base URL
    consdbUrl: str  # ConsDB POST endpoint (full URL, includes /query)
    consdbTokenFile: Path | None  # absolute, ~ already expanded; None = no auth
    title: str  # what the page calls itself; see SITE_TITLES


class SitesConfigError(RuntimeError):
    """The on-disk site catalog is missing or malformed."""


def sitesFilePath(override: Path | None = None) -> Path:
    """Resolve which file to load the site catalog from.

    Resolution order: explicit ``override`` (used by tests), then
    the ``RA_LOG_EXPLORER_SITES_FILE`` env var, then the packaged
    :data:`PACKAGED_SITES_FILE` shipped with the source tree.
    """
    if override is not None:
        return override
    fromEnv = os.environ.get(SITES_FILE_ENV)
    if fromEnv:
        return Path(fromEnv).expanduser()
    return PACKAGED_SITES_FILE


def loadSites(path: Path | None = None) -> tuple[list[Site], str]:
    """Read the site catalog. Returns ``(sites, defaultSiteName)``.

    Raises :exc:`SitesConfigError` if the file is missing, malformed,
    has no sites, has a duplicate site name or cluster, or names a
    ``default_site`` that no entry defines.
    """
    p = sitesFilePath(path)
    try:
        with p.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as e:
        raise SitesConfigError(f"Sites catalog not found: {p}") from e
    except tomllib.TOMLDecodeError as e:
        raise SitesConfigError(f"Sites catalog at {p} is not valid TOML: {e}") from e
    rawSites = raw.get("site")
    if not isinstance(rawSites, list) or not rawSites:
        raise SitesConfigError(f"Sites catalog at {p} has no [[site]] entries.")
    sites: list[Site] = []
    names: set[str] = set()
    clusters: set[str] = set()
    for i, entry in enumerate(rawSites):
        if not isinstance(entry, dict):
            raise SitesConfigError(f"Sites catalog entry #{i} is not a table.")
        sites.append(_siteFromDict(entry, p, i))
        if sites[-1].name in names:
            raise SitesConfigError(f"Sites catalog has duplicate site name {sites[-1].name!r} in {p}.")
        names.add(sites[-1].name)
        # `siteByCluster` (used to map a cache dir's cluster path
        # component back to its site when rehydrating from disk) assumes
        # each cluster belongs to exactly one site. Enforce it here so a
        # mis-edited catalog fails loudly at load instead of silently
        # routing a rehydrated cache to the wrong ConsDB.
        if sites[-1].cluster in clusters:
            raise SitesConfigError(
                f"Sites catalog has duplicate cluster {sites[-1].cluster!r} in {p}; "
                "each Loki cluster must map to exactly one site."
            )
        clusters.add(sites[-1].cluster)
    default = raw.get("default_site")
    if not isinstance(default, str) or default not in names:
        raise SitesConfigError(
            f"Sites catalog at {p} needs a top-level `default_site` " f"naming one of: {sorted(names)}."
        )
    return sites, default


def _siteFromDict(entry: dict, path: Path, idx: int) -> Site:
    """Validate one ``[[site]]`` table and construct a :class:`Site`."""
    fields = ("name", "cluster", "namespace", "lokiAddr", "consdbUrl")
    missing = [f for f in fields if not isinstance(entry.get(f), str) or not entry[f]]
    if missing:
        raise SitesConfigError(
            f"Sites catalog entry #{idx} in {path} is missing required string field(s): {missing}."
        )
    # Optional: omit it (or leave it blank) for a ConsDB that needs no
    # bearer token, e.g. an in-cluster Service address.
    rawToken = entry.get("consdbTokenFile")
    if rawToken is not None and not isinstance(rawToken, str):
        raise SitesConfigError(f"Sites catalog entry #{idx} in {path} has a non-string consdbTokenFile.")
    # Also optional, and for the same reason as consdbTokenFile: the
    # chart-rendered catalog has no field for it.
    rawTitle = entry.get("title")
    if rawTitle is not None and not isinstance(rawTitle, str):
        raise SitesConfigError(f"Sites catalog entry #{idx} in {path} has a non-string title.")
    return Site(
        name=entry["name"],
        cluster=entry["cluster"],
        namespace=entry["namespace"],
        lokiAddr=entry["lokiAddr"],
        consdbUrl=entry["consdbUrl"],
        consdbTokenFile=Path(rawToken).expanduser() if rawToken else None,
        title=rawTitle or SITE_TITLES.get(entry["name"], DEFAULT_TITLE),
    )


def siteByName(sites: list[Site], name: str) -> Site:
    """Return the site with this short name. Raises if not found."""
    for s in sites:
        if s.name == name:
            return s
    raise SitesConfigError(f"No site named {name!r}; known: {[s.name for s in sites]}.")


def siteByCluster(sites: list[Site], cluster: str) -> Site:
    """Return the site whose Loki cluster matches. Raises if not found.

    Each cluster maps to exactly one site (the (cluster, ConsDB) pairing
    is the whole point of the catalog), so the first match is correct.
    """
    for s in sites:
        if s.cluster == cluster:
            return s
    raise SitesConfigError(f"No site for cluster {cluster!r}; known clusters: {[s.cluster for s in sites]}.")
