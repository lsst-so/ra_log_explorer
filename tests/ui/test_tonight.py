"""The Tonight panel, and live mode behind it.

Most of these stub ``/api/live`` rather than running a poller, because
the panel's job is to make sense of a snapshot and the interesting
snapshots — catching up, a pod's fetch failing, a night that couldn't be
finalised — are states you cannot conjure on demand from real data.

The last test does run the real poller against the real corpus, so the
end of the chain is covered too: the watermark advances, an exposure
crosses from "wait" to "view", and clicking it opens a timeline that was
sliced out of the night rather than fetched.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from playwright.sync_api import expect

from .conftest import routeJson
from .corpus import CAM_T_ZERO_UTC, DAY_OBS, SHARED_ID, StagedCorpus

UTC = dt.timezone.utc


def snapshot(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True,
        "siteName": "summit",
        "dayObs": DAY_OBS,
        "nightStart": "2026-07-11T12:00:00.000000Z",
        "nightEnd": "2026-07-12T12:00:00.000000Z",
        "watermark": "2026-07-12T06:00:00.000000Z",
        "finalised": False,
        "catchingUp": False,
        "pollSeconds": 300.0,
        "nPods": 576,
        "errors": {},
        "incompletePods": {},
        "eventsError": None,
        "consdbError": None,
        "orphanError": None,
        "lastError": None,
        "exposures": [],
    }
    base.update(overrides)
    return base


def exposureRow(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "dataId": SHARED_ID,
        "instrument": "lsstcam",
        "obsEndUtc": CAM_T_ZERO_UTC.isoformat(),
        "readyAtUtc": (CAM_T_ZERO_UTC + dt.timedelta(seconds=300)).isoformat(),
        "ready": True,
        "record": {"img_type": "science", "physical_filter": "g_6", "observation_reason": "triplet"},
    }
    row.update(overrides)
    return row


def test_the_panel_stays_hidden_when_live_mode_is_off(app: Any) -> None:
    """Live mode is a deployment thing; a laptop run must not grow an
    empty panel promising a night it isn't fetching."""
    app.goto("/")
    expect(app.page.locator("#tonight-card")).to_be_hidden()


def test_a_ready_exposure_is_a_link_and_a_waiting_one_is_not(app: Any) -> None:
    waitUntil = dt.datetime(2026, 7, 12, 6, 8, tzinfo=UTC)
    routeJson(
        app.page,
        "**/api/live",
        snapshot(
            exposures=[
                exposureRow(dataId=2026071100446, ready=False, readyAtUtc=waitUntil.isoformat()),
                exposureRow(),
            ]
        ),
    )
    app.goto("/")
    expect(app.page.locator("#tonight-card")).to_be_visible()
    rows = app.page.locator("#tonight-tbody tr")
    expect(rows).to_have_count(2)
    # Newest first, and the not-yet-ready one says how long to wait.
    expect(rows.nth(0)).to_contain_text("ready in ~8 min")
    assert rows.nth(0).locator("a").count() == 0, "a link would open an incomplete window"
    expect(rows.nth(1)).to_contain_text("view")
    assert rows.nth(1).locator("a").count() == 1


def test_the_summary_counts_what_is_viewable(app: Any) -> None:
    routeJson(
        app.page,
        "**/api/live",
        snapshot(exposures=[exposureRow(), exposureRow(dataId=2026071100446, ready=False)]),
    )
    app.goto("/")
    expect(app.page.locator("#tonight-summary")).to_contain_text(f"dayObs {DAY_OBS}")
    expect(app.page.locator("#tonight-summary")).to_contain_text("2 LSSTCam exposures")
    expect(app.page.locator("#tonight-summary")).to_contain_text("1 viewable")


def test_the_status_line_distinguishes_catching_up_from_steady_state(app: Any) -> None:
    routeJson(app.page, "**/api/live", snapshot())
    app.goto("/")
    expect(app.page.locator("#tonight-status")).to_contain_text("refreshes every 300s")

    routeJson(app.page, "**/api/live", snapshot(catchingUp=True))
    app.page.reload()
    expect(app.page.locator("#tonight-status")).to_contain_text("catching up")

    routeJson(app.page, "**/api/live", snapshot(finalised=True))
    app.page.reload()
    expect(app.page.locator("#tonight-status")).to_contain_text("night complete")


def test_every_kind_of_live_problem_reaches_the_banner(app: Any) -> None:
    """Each of these means part of the night is missing or stale, and a
    quiet failure here would leave people trusting an incomplete view."""
    app.goto("/")
    cases = {
        "pods with fetch failures": snapshot(errors={"pod-a": "timeout", "pod-b": "timeout"}),
        "unverifiable chunks": snapshot(incompletePods={"pod-c": "chunk floor"}),
        "ConsDB": snapshot(consdbError="HTTP 503"),
        "an earlier night could not be finalised": snapshot(orphanError="loki down"),
        "last poll cycle failed": snapshot(lastError="Traceback ..."),
    }
    for expected, payload in cases.items():
        routeJson(app.page, "**/api/live", payload)
        app.page.reload()
        banner = app.page.locator("#tonight-banner")
        expect(banner).to_be_visible()
        expect(banner).to_contain_text(expected)

    routeJson(app.page, "**/api/live", snapshot())
    app.page.reload()
    expect(app.page.locator("#tonight-banner")).to_be_hidden()


def test_a_long_night_is_capped_with_an_expander(app: Any) -> None:
    """The panel is for what is happening now; the rest of the night is
    one click away rather than 800 rows of scrolling."""
    rows = [exposureRow(dataId=2026071100400 + i) for i in range(40)]
    routeJson(app.page, "**/api/live", snapshot(exposures=rows))
    app.goto("/")
    shown = app.page.locator("#tonight-tbody tr").count()
    assert 0 < shown < 40
    more = app.page.locator("#tonight-more")
    expect(more).to_be_visible()
    expect(more).to_contain_text(str(40 - shown))
    more.locator("a").click()
    expect(app.page.locator("#tonight-tbody tr")).to_have_count(40)


def test_a_hostile_observation_reason_is_rendered_as_text(app: Any) -> None:
    """ConsDB strings land straight in this table; they are data, not
    markup."""
    nasty = "<img src=x onerror=window.__pwned=1>"
    routeJson(
        app.page,
        "**/api/live",
        snapshot(exposures=[exposureRow(record={"img_type": "science", "observation_reason": nasty})]),
    )
    app.goto("/")
    expect(app.page.locator("#tonight-tbody tr")).to_contain_text(nasty)
    assert app.page.evaluate("() => window.__pwned") is None
    assert app.page.locator("#tonight-tbody img").count() == 0


def test_the_panel_survives_the_api_failing(app: Any) -> None:
    """A poller that has fallen over must not take the home page with
    it — the forms below still work."""
    app.page.route("**/api/live", lambda route: route.fulfill(status=500, body="boom"))
    app.goto("/")
    expect(app.page.locator("#home-view")).to_be_visible()
    expect(app.page.locator("#tonight-card")).to_be_hidden()
    expect(app.page.locator("#fetch-form")).to_be_visible()


def test_live_mode_serves_a_ready_exposure_by_slicing_the_night(
    appFactory: Any, liveCorpus: StagedCorpus, monkeypatch: Any
) -> None:
    """The whole live-mode promise, end to end against the real corpus:
    the poller advances a watermark over the night on disk, an exposure
    becomes viewable once its window is covered, and opening it costs no
    fetch because the night gets sliced instead.
    """
    import json

    from ra_log_explorer import exposureTimes, live

    # The night is already on disk, so the poller's only job here is to
    # advance the watermark over it — no Loki, and nothing to append.
    monkeypatch.setattr(live, "listPods", lambda spec: [])
    monkeypatch.setattr(live, "fetchEventsWindowInto", lambda spec, a, b, fh: (0, True, ""))
    # ConsDB stands in for itself out of the corpus's own records: what
    # the poller would have been told, without a network.
    cached = json.loads((liveCorpus.root / "exposure-times" / "summit.json").read_text())
    # A handful around the exposure under test: the panel shows the most
    # recent 20, and this keeps the one we are watching on screen.
    records = [
        rec for key, rec in cached.items() if ":" in key and abs(int(key.split(":")[1]) - SHARED_ID) <= 2
    ]
    monkeypatch.setattr(
        exposureTimes, "queryExposureRecordsForDayObs", lambda dayObs, token, *, consdbUrl: records
    )
    manager = live.LiveNightManager(
        site=_site(),
        username="ui-test",
        workers=2,
        pollS=60.0,
        lagS=0.0,
        fixedDayObs=DAY_OBS,
    )
    liveCorpus.rewindWatermark(CAM_T_ZERO_UTC - dt.timedelta(hours=1))
    app = appFactory(live=manager)

    # Before the exposure's window is covered: listed, but not viewable.
    manager.tick(now=CAM_T_ZERO_UTC + dt.timedelta(seconds=60))
    app.goto("/")
    row = app.page.locator("#tonight-tbody tr", has_text=str(SHARED_ID))
    expect(row).to_contain_text("ready in")
    assert row.locator("a").count() == 0

    # Once the watermark passes shutter close + the window, it is viewable.
    manager.tick(now=CAM_T_ZERO_UTC + dt.timedelta(seconds=600))
    app.page.reload()
    expect(row).to_contain_text("view")
    row.locator("a").click()

    expect(app.page.locator("#explore-view")).to_be_visible(timeout=60_000)
    expect(app.page.locator("#timeline .tl-row").first).to_be_visible()
    # Sliced out of the night, not fetched: no Loki was reachable at all.
    windows = app.apiJson("/api/cache")["windows"]
    assert any(SHARED_ID in (w.get("exposureIds") or []) for w in windows), windows


def _site() -> Any:
    from ra_log_explorer.sites import Site

    return Site(
        name="summit",
        cluster="yagan",
        namespace="rapid-analysis",
        lokiAddr="https://loki.invalid",
        consdbUrl="https://consdb.invalid/query",
        consdbTokenFile=None,
    )
