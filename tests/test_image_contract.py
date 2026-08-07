"""Pins in the Dockerfile that the Phalanx chart depends on.

The chart lives in another repository (`applications/log-explorer/` in
Phalanx) and hard-codes several facts about this image: the port it
listens on, the UID it runs as, and that it takes its configuration from
the environment rather than baked-in build args. Change one of them here
and the deployment breaks in a way nothing else in this suite would
notice — the image builds, the tests pass, and the pod either never
becomes Ready or cannot write to its own volumes.

These are deliberately shallow string checks. They are not testing that
the image works; the container smoke test in architecture/testing.md does
that. They exist so that *changing* one of these values is a conscious
act that reminds you to change the chart too.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


def _dockerfile() -> str:
    return DOCKERFILE.read_text()


def test_listens_on_the_port_the_chart_routes_to() -> None:
    """The chart's containerPort, Service targetPort and NetworkPolicy all
    say 8080. Moving it here silently breaks all three."""
    body = _dockerfile()
    assert "EXPOSE 8080" in body
    assert '"--port", "8080"' in body


def test_binds_all_interfaces() -> None:
    """The app defaults to 127.0.0.1, which is right on a laptop and fatal
    in a pod — the kubelet's probe and the Service both arrive on the pod
    IP, so a loopback-only bind is unreachable and never becomes Ready."""
    assert '"--host", "0.0.0.0"' in _dockerfile()


def test_does_not_try_to_open_a_browser() -> None:
    """There is no browser in a container; the CLI opens one by default."""
    assert '"--no-browser"' in _dockerfile()


def test_runs_as_the_uid_the_chart_declares() -> None:
    """The chart sets runAsUser/runAsGroup/fsGroup to 1000, and fsGroup is
    what makes the cache volume writable. A different UID here means the
    pod cannot write its own cache."""
    body = _dockerfile()
    assert re.search(r"useradd --uid 1000\b", body)
    assert "USER 1000" in body


def test_pins_logcli_rather_than_tracking_latest() -> None:
    """The fetch path works around grafana/loki#17270 by reasoning about
    exactly when logcli paginates, so the version is part of the contract,
    not an implementation detail. It is also checksum-verified per
    architecture, since an unpinned or unverified download would make the
    image non-reproducible."""
    body = _dockerfile()
    assert re.search(r"ARG LOGCLI_VERSION=\d+\.\d+\.\d+", body)
    assert "sha256sum -c -" in body
    for arch in ("amd64", "arm64"):
        assert re.search(rf"ARG LOGCLI_SHA256_{arch}=[0-9a-f]{{64}}", body), arch


def test_bakes_in_no_environment() -> None:
    """One image serves both BTS and the summit. Anything site-specific
    fixed at build time would make the image environment-bound and mean
    two builds where there should be one."""
    body = _dockerfile()
    envLines = [ln.strip() for ln in body.splitlines() if ln.strip().startswith("ENV ")]
    assert envLines == [], f"image bakes in environment: {envLines}"
