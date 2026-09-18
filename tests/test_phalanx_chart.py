"""Check this application against the Helm chart that deploys it.

The chart lives in a different repository — `applications/log-explorer/`
in [Phalanx](https://github.com/lsst-sqre/phalanx) — which means the two
halves of the deployment can drift apart with nothing to notice. The
other tests in this suite guard that with pins (`test_config.py`'s
environment-variable list, `test_image_contract.py`'s Dockerfile
invariants); those catch a change made *here* without a matching change
there. These tests close the loop from the other side: they render the
real chart and check the result against what this code actually does.

They need a Phalanx checkout and `helm`. Point `$PHALANX_REPO` at the
checkout, or leave it and let the search below find one. Everything
skips cleanly when neither is available, so a laptop without Phalanx and
a CI job without it behave the same.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ra_log_explorer import config, sites

yaml = pytest.importorskip("yaml")

CHART_PATH = "applications/log-explorer"


def _envVarsThisCodeReads() -> set[str]:
    """Every environment variable name the package mentions.

    Scans the whole package, not just `config.py`: `LOKI_PASSWORD` is
    consumed in `fetch.py` where logcli is spawned, and
    `RA_LOG_EXPLORER_SITES_FILE` is declared in `sites.py`. Looking in one
    module would quietly under-report and make the comparison below
    vacuous.
    """
    package = Path(config.__file__).parent
    names: set[str] = set()
    for module in sorted(package.glob("*.py")):
        names |= set(re.findall(r'"(RA_LOG_EXPLORER_[A-Z_]+|LOKI_[A-Z_]+)"', module.read_text()))
    return names


def _numericEnvVars() -> dict[str, type]:
    """The variables `config.py` parses as a number, and with which parser.

    Read out of the source rather than listed here, because a hand-written
    list is the thing that goes stale: a knob added to `config.py` and to
    the chart but not to the list is exactly the one that never gets
    checked. `RA_LOG_EXPLORER_LIVE_POLL_S` and `..._LIVE_LAG_S` were
    already in that position.
    """
    source = Path(config.__file__).read_text()
    found: dict[str, type] = {}
    for fn, name in re.findall(r'_env(Int|Float)\(\s*"(RA_LOG_EXPLORER_[A-Z_]+)"', source):
        found[name] = int if fn == "Int" else float
    assert found, "no numeric settings found — the pattern above has drifted from config.py"
    return found


def _findPhalanx() -> Path | None:
    """Locate a Phalanx checkout containing this chart, or ``None``."""
    candidates = []
    fromEnv = os.environ.get("PHALANX_REPO")
    if fromEnv:
        candidates.append(Path(fromEnv).expanduser())
    here = Path(__file__).resolve().parent.parent
    candidates += [
        here.parent / "phalanx",
        Path.home() / "lsst" / "phalanx",
        Path.home() / "phalanx",
    ]
    for c in candidates:
        if (c / CHART_PATH / "Chart.yaml").is_file():
            return c
    return None


# CI sets this in the job that clones Phalanx and installs helm. There,
# "the chart wasn't found" is the failure these tests exist to catch —
# a chart that moved or a clone that silently landed on the wrong branch
# would otherwise skip every assertion and report a green job, which is
# indistinguishable from having checked the contract. Locally the
# variable is unset and skipping stays the right behaviour.
_REQUIRE_ENV = "RA_LOG_EXPLORER_REQUIRE_CHART"


def _missingChart(reason: str) -> None:
    if os.environ.get(_REQUIRE_ENV):
        pytest.fail(f"{reason} (with {_REQUIRE_ENV} set, this is a failure rather than a skip)")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def phalanx() -> Path:
    repo = _findPhalanx()
    if repo is None:
        _missingChart(
            f"no Phalanx checkout with {CHART_PATH} found; "
            "set $PHALANX_REPO to run the cross-repo chart tests"
        )
    if shutil.which("helm") is None:
        _missingChart("helm is not on PATH; cannot render the chart")
    assert repo is not None
    return repo


def _render(repo: Path, environment: str) -> list[dict]:
    """Render the chart for one environment, as Argo CD would."""
    out = subprocess.run(
        [
            "helm",
            "template",
            "log-explorer",
            str(repo / CHART_PATH),
            "--values",
            str(repo / CHART_PATH / "values.yaml"),
            "--values",
            str(repo / CHART_PATH / f"values-{environment}.yaml"),
            # Argo CD injects these; the chart `required`s the host.
            "--set",
            "global.host=example.lsst.codes",
            "--set",
            f"global.vaultSecretsPath=secret/phalanx/{environment}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _kind(docs: list[dict], kind: str) -> dict:
    matches = [d for d in docs if d.get("kind") == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
    return matches[0]


def _container(docs: list[dict]) -> dict:
    return _kind(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]


def _env(docs: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in _container(docs)["env"]}


@pytest.fixture(scope="module")
def baseDocs(phalanx: Path) -> list[dict]:
    return _render(phalanx, "base")


@pytest.fixture(scope="module")
def summitDocs(phalanx: Path) -> list[dict]:
    return _render(phalanx, "summit")


# ----- the site catalog ----------------------------------------------------


@pytest.mark.parametrize(
    "environment,siteName,cluster", [("base", "bts", "manke"), ("summit", "summit", "yagan")]
)
def test_the_rendered_catalog_loads(
    phalanx: Path, environment: str, siteName: str, cluster: str, tmp_path: Path
) -> None:
    """The ConfigMap the chart renders must parse as a site catalog.

    It is templated YAML producing TOML that this application reads with
    tomllib at startup, so a malformed result is a crash-looping pod and
    nothing sooner. Feeding the real rendering through the real loader is
    the only check that covers the whole path.
    """
    docs = _render(phalanx, environment)
    toml = _kind(docs, "ConfigMap")["data"]["sites.toml"]
    path = tmp_path / "sites.toml"
    path.write_text(toml)

    catalog, default = sites.loadSites(path)
    assert default == siteName
    # Exactly one: an instance on manke *is* BTS and one on yagan *is* the
    # summit. A second entry would imply a choice that doesn't exist.
    assert len(catalog) == 1
    assert catalog[0].name == siteName
    assert catalog[0].cluster == cluster
    # In-cluster ConsDB, so no bearer token and nothing to mount.
    assert catalog[0].consdbTokenFile is None
    assert catalog[0].consdbUrl.startswith("http://")


def test_each_environment_points_at_its_own_consdb(baseDocs: list[dict], summitDocs: list[dict]) -> None:
    """BTS and the summit must not share a ConsDB *or* a cluster label.

    The same 13-digit dataId exists at both with different obs_end values,
    so crossing them returns answers that look plausible rather than
    obviously wrong — the worst possible failure for this tool.
    """
    baseToml = _kind(baseDocs, "ConfigMap")["data"]["sites.toml"]
    summitToml = _kind(summitDocs, "ConfigMap")["data"]["sites.toml"]
    assert 'cluster = "manke"' in baseToml
    assert 'cluster = "yagan"' in summitToml
    assert baseToml != summitToml


# ----- the configuration contract ------------------------------------------


def test_the_chart_sets_every_variable_this_code_reads(baseDocs: list[dict]) -> None:
    """The failure this exists for is silent: a variable the application
    reads that the chart never sets leaves production running on the
    built-in default, where the symptom is a setting that appears to do
    nothing at all."""
    read = _envVarsThisCodeReads()
    missing = read - set(_env(baseDocs))
    assert not missing, f"chart does not set: {sorted(missing)} — update applications/log-explorer"


def test_the_chart_sets_nothing_this_code_ignores(baseDocs: list[dict]) -> None:
    """The mirror image, and a milder problem: a variable set in the chart
    that nothing reads is dead configuration somebody will later try to
    tune and find inert."""
    stale = {
        n for n in _env(baseDocs) if n.startswith(("RA_LOG_EXPLORER_", "LOKI_"))
    } - _envVarsThisCodeReads()
    assert not stale, f"chart sets variables nothing reads: {sorted(stale)}"


def test_the_base_path_is_one_value_not_two(baseDocs: list[dict]) -> None:
    """The path the ingress routes and the path the app serves come from
    the same Helm value. If they could drift, the app would 404 every
    request the ingress sent it — and the readiness probe first."""
    basePath = _env(baseDocs)["RA_LOG_EXPLORER_BASE_PATH"]["value"]
    ingressPath = _kind(baseDocs, "GafaelfawrIngress")["template"]["spec"]["rules"][0]["http"]["paths"][0]
    assert ingressPath["path"] == basePath
    assert config.normalizeBasePath(basePath) == basePath, "chart supplies a non-canonical base path"


def test_the_probe_hits_a_route_this_server_answers(baseDocs: list[dict]) -> None:
    """`/healthz` under the base path is the readiness probe. If this and
    the router ever disagreed the pod would never become Ready, and the
    deployment would wedge with no obvious cause."""
    basePath = _env(baseDocs)["RA_LOG_EXPLORER_BASE_PATH"]["value"]
    probe = _container(baseDocs)["readinessProbe"]["httpGet"]["path"]
    assert probe == f"{basePath}/healthz"


def test_the_cache_ceiling_fits_inside_the_volume(baseDocs: list[dict]) -> None:
    """The eviction ceiling is derived from the claim's size. If it ever
    exceeded it the application would happily fill the volume and then
    fail to write, rather than evicting as designed."""
    ceiling = int(_env(baseDocs)["RA_LOG_EXPLORER_MAX_CACHE_BYTES"]["value"])
    claim = _kind(baseDocs, "PersistentVolumeClaim")["spec"]["resources"]["requests"]["storage"]
    assert claim.endswith("Gi")
    volumeBytes = int(claim[:-2]) * 1024**3
    assert 0 < ceiling < volumeBytes, f"ceiling {ceiling} vs volume {volumeBytes}"


def test_the_cache_env_var_points_at_the_mounted_volume(baseDocs: list[dict]) -> None:
    """A cache root that isn't the mount point would put the cache on the
    read-only root filesystem — which fails at the first write, long after
    the pod has reported itself healthy."""
    cacheRoot = _env(baseDocs)["RA_LOG_EXPLORER_CACHE"]["value"]
    mounts = {m["name"]: m["mountPath"] for m in _container(baseDocs)["volumeMounts"]}
    assert mounts["cache"] == cacheRoot


def test_the_sites_file_env_var_points_at_the_mounted_configmap(baseDocs: list[dict]) -> None:
    sitesFile = _env(baseDocs)["RA_LOG_EXPLORER_SITES_FILE"]["value"]
    mounts = {m["name"]: m["mountPath"] for m in _container(baseDocs)["volumeMounts"]}
    assert sitesFile.startswith(mounts["sites"] + "/")


def test_a_writable_temp_dir_is_mounted(baseDocs: list[dict]) -> None:
    """Chunk fetches stage through the *system* temp dir (deliberately —
    a crash must not strand a chunk file beside the real pod files), and
    the root filesystem is read-only. This is a contract no env var
    carries, so the env-name pin can't see it: drop the /tmp volume from
    the chart and the pod comes up healthy and then fails on the first
    fetch."""
    mounts = {m["mountPath"] for m in _container(baseDocs)["volumeMounts"]}
    assert "/tmp" in mounts
    assert _container(baseDocs)["securityContext"]["readOnlyRootFilesystem"] is True


def test_numeric_settings_are_values_this_code_will_accept(baseDocs: list[dict]) -> None:
    """`_envInt` / `_envFloat` raise rather than falling back — and a
    *blank* value counts as malformed, which is exactly what a mistyped
    Helm reference (`value: {{ .Values.typo }}`) renders to. So a chart
    value of the wrong shape is a crash-looping pod, and every numeric
    knob has to be parsed here the way the application will parse it, not
    just the ones somebody remembered to list."""
    env = _env(baseDocs)
    for name, parse in sorted(_numericEnvVars().items()):
        raw = env[name]["value"]
        assert raw and raw.strip() == raw, f"{name} renders to {raw!r}; the app rejects blank"
        assert parse(raw) >= 0, name
    # The two with a floor as well as a shape: zero workers fetches
    # nothing, and a zero ceiling evicts the cache as fast as it is built.
    assert int(env["RA_LOG_EXPLORER_WORKERS"]["value"]) > 0
    assert int(env["RA_LOG_EXPLORER_MAX_CACHE_BYTES"]["value"]) > 0


# ----- the image contract --------------------------------------------------


def test_the_chart_routes_to_the_port_the_image_exposes(baseDocs: list[dict]) -> None:
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    port = _container(baseDocs)["ports"][0]["containerPort"]
    assert f"EXPOSE {port}" in dockerfile
    assert f'"--port", "{port}"' in dockerfile
    assert _kind(baseDocs, "Service")["spec"]["ports"][0]["port"] == port


def test_the_chart_runs_as_the_uid_the_image_creates(baseDocs: list[dict]) -> None:
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    uid = _kind(baseDocs, "Deployment")["spec"]["template"]["spec"]["securityContext"]["runAsUser"]
    assert f"useradd --uid {uid} " in dockerfile
    assert f"USER {uid}" in dockerfile


def test_the_chart_deploys_the_image_this_repo_builds(baseDocs: list[dict]) -> None:
    workflow = (Path(__file__).resolve().parent.parent / ".github/workflows/build.yaml").read_text()
    image = _container(baseDocs)["image"]
    assert image.startswith("ghcr.io/lsst-so/ra_log_explorer:"), image
    assert "lsst-sqre/build-and-push-to-ghcr" in workflow
