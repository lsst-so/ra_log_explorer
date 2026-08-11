"""The page-level instrument pin.

An exposure id is only unique within one instrument: the last five
digits are a sequence number that restarts at 1 each night, per
instrument. So on any night LSSTCam and LATISS both observe — most of
them — the same 13-digit id names two different exposures, with shutter
closes an hour apart. The corpus is a real such night, and
:data:`SHARED_ID` is a real such id.

That makes the pin the highest-stakes control in the UI: get it wrong
and the app shows a plausible timeline for the wrong exposure, which is
worse than showing nothing. These tests use the real pair throughout —
the two shutter closes really are 04:21:22 and 05:24:53 — so a
regression that quietly resolves the wrong one has to change an
assertion to pass.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import expect

from .corpus import CAM_T_ZERO_UTC, LATISS_T_ZERO_UTC, SHARED_ID, StagedCorpus


def switch(app: Any, instrument: str) -> None:
    app.page.locator(f"#instrument-switch button[data-instrument={instrument}]").click()


def activeInstrument(app: Any) -> str:
    return str(app.page.locator("#instrument-switch button.active").get_attribute("data-instrument"))


def test_lsstcam_is_the_default(app: Any) -> None:
    app.goto("/")
    expect(app.page.locator("#instrument-switch button.active")).to_have_count(1)
    assert activeInstrument(app) == "lsstcam"


def test_switching_marks_the_button_and_rewrites_the_url(app: Any) -> None:
    app.goto("/")
    switch(app, "latiss")
    assert activeInstrument(app) == "latiss"
    assert "instrument=latiss" in app.page.url
    switch(app, "lsstcam")
    assert activeInstrument(app) == "lsstcam"
    assert "instrument=lsstcam" in app.page.url


def test_the_choice_survives_a_reload(app: Any) -> None:
    """Kept in localStorage, so coming back to the app lands where you
    left it rather than silently back on LSSTCam."""
    app.goto("/")
    switch(app, "latiss")
    app.page.goto(app.url("/"))  # no query string at all
    expect(app.page.locator("#instrument-switch button.active")).to_have_attribute(
        "data-instrument", "latiss"
    )


def test_an_explicit_url_beats_the_remembered_choice(app: Any) -> None:
    app.goto("/")
    switch(app, "latiss")
    app.goto("/?instrument=lsstcam")
    assert activeInstrument(app) == "lsstcam"


def test_night_mode_is_hidden_for_latiss(app: Any) -> None:
    """AOS runs on LSSTCam's corner wavefront sensors and nowhere else,
    so the night card has nothing to offer a LATISS user."""
    app.goto("/")
    expect(app.page.locator("#night-card")).to_be_visible()
    switch(app, "latiss")
    expect(app.page.locator("#night-card")).to_be_hidden()
    switch(app, "lsstcam")
    expect(app.page.locator("#night-card")).to_be_visible()


def test_the_same_id_resolves_to_a_different_shutter_close_per_instrument(
    app: Any, corpus: StagedCorpus
) -> None:
    """The core of it. One typed id, two answers, an hour apart — and
    switching has to throw the first away rather than carry it over."""
    app.goto("/")
    status = app.page.locator("#tzero-status")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(SHARED_ID))
    expect(status).to_contain_text("04:21:59")  # LSSTCam obs_end (TAI)

    switch(app, "latiss")
    expect(status).to_contain_text("05:25:30")  # LATISS obs_end (TAI), same id

    switch(app, "lsstcam")
    expect(status).to_contain_text("04:21:59")


def test_fetching_the_same_id_under_each_instrument_gives_different_exposures(
    app: Any, corpus: StagedCorpus
) -> None:
    """End to end: the pin has to reach all the way to which window is
    opened, not just to which timestamp is displayed."""
    corpus.stageExposure(SHARED_ID, "lsstcam")
    corpus.stageExposure(SHARED_ID, "latiss")

    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#explore-view")).to_be_visible()
    camInfo = app.page.locator("#exposure-info").inner_text()
    camPods = app.page.locator("#timeline .tl-podname").all_inner_texts()

    app.goto(f"/?dataId={SHARED_ID}&instrument=latiss")
    expect(app.page.locator("#explore-view")).to_be_visible()
    latissInfo = app.page.locator("#exposure-info").inner_text()
    latissPods = app.page.locator("#timeline .tl-podname").all_inner_texts()

    assert "lsstcam" in camInfo and "latiss" in latissInfo
    assert camInfo != latissInfo
    # Different exposures an hour apart: no pod can have worked on both.
    assert camPods and latissPods
    assert set(camPods) != set(latissPods)


def test_an_instrument_pinned_view_shows_no_other_instruments_pods(app: Any, corpus: StagedCorpus) -> None:
    """The window is fetched namespace-wide, so it genuinely contains the
    other instrument's pods — and the direction matters: the corpus's
    LSSTCam window really holds six LATISS pods (busy an hour before
    their own same-numbered exposure), where the LATISS window holds no
    LSSTCam lines at all. Asserting on the LATISS side passed even with
    the filter deleted; this side cannot."""
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#explore-view")).to_be_visible()
    pods = app.page.locator("#timeline .tl-podname").all_inner_texts()
    assert pods, "the LSSTCam exposure should have pods of its own"
    # Pod names carry their instrument; the pinned view keeps only its own
    # (plus instrument-neutral ones, which carry neither name).
    assert not any("latiss" in p for p in pods), pods
    # The timeline only lists pods attributed to the exposure, so the
    # exclusion is fully observable in podsAll — the whole-window pod
    # inventory, where the LATISS pods would otherwise appear.
    payload = app.apiJson(f"/api/summary?dataId={SHARED_ID}&instrument=lsstcam")
    assert payload["loaded"] is True
    allPods = [p["pod"] for p in payload["podsAll"]]
    assert any("lsstcam" in p for p in allPods)
    assert not [p for p in allPods if "latiss" in p], allPods


def test_a_fetch_keeps_the_instrument_in_the_url(
    app: Any, appFactory: Any, corpus: StagedCorpus, fakeLogcli: list[str]
) -> None:
    """The URL is what a reload or a pasted link replays, and a dataId
    alone does not name an exposure. Finishing a fetch rewrites the URL
    to the loaded state — it must keep the pin, or a LATISS view
    reopened tomorrow resolves to its LSSTCam twin: same 13 digits,
    different exposure, a shutter close an hour away, and nothing on
    screen to say so."""
    # Stage the LSSTCam twin first: it is what a bare ?dataId=… reopen
    # would find, so its presence is what makes this test able to fail.
    corpus.stageExposure(SHARED_ID, "lsstcam")
    corpus.dropLiveSidecar()
    app.goto("/")
    switch(app, "latiss")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(SHARED_ID))
    expect(app.page.locator("#tzero-status")).to_contain_text("05:25:30")
    app.page.locator("#fetch-submit").click()
    expect(app.page.locator("#explore-view")).to_be_visible(timeout=60_000)
    assert f"dataId={SHARED_ID}" in app.page.url
    assert "instrument=latiss" in app.page.url

    # And the round trip: a fresh process (same cache volume — another
    # visitor to the deployment) opening that URL lands on the LATISS
    # exposure, not the twin.
    other = appFactory()
    app.page.goto(app.page.url.replace(app.origin, other.origin))
    expect(app.page.locator("#explore-view")).to_be_visible()
    expect(app.page.locator("#exposure-info")).to_contain_text("latiss")


def test_switching_clears_a_resolved_lookup_rather_than_carrying_it_over(app: Any) -> None:
    """A stale t0 from the other instrument is the exact failure this
    control exists to prevent, so the switch must invalidate it."""
    app.goto("/")
    field = app.page.locator("#fetch-form input[name=exposureId]")
    field.fill(str(SHARED_ID))
    expect(app.page.locator("#tzero-status")).to_contain_text("shutter close")
    resolvedFirst = app.page.locator("#tzero-status").inner_text()
    switch(app, "latiss")
    expect(app.page.locator("#tzero-status")).not_to_have_text(resolvedFirst)


def test_the_tonight_panel_lists_only_the_pinned_instrument(app: Any) -> None:
    """Both instruments observe the same night; mixing them in one table
    would show the same id twice with no way to tell them apart."""
    rows = [
        {
            "dataId": SHARED_ID,
            "instrument": "lsstcam",
            "obsEndUtc": CAM_T_ZERO_UTC.isoformat(),
            "readyAtUtc": CAM_T_ZERO_UTC.isoformat(),
            "ready": True,
            "record": {"img_type": "science", "physical_filter": "g_6"},
        },
        {
            "dataId": SHARED_ID,
            "instrument": "latiss",
            "obsEndUtc": LATISS_T_ZERO_UTC.isoformat(),
            "readyAtUtc": LATISS_T_ZERO_UTC.isoformat(),
            "ready": True,
            "record": {"img_type": "science", "physical_filter": "empty~holo4_003"},
        },
    ]
    from .conftest import routeJson

    routeJson(
        app.page,
        "**/api/live",
        {
            "enabled": True,
            "dayObs": 20260711,
            "watermark": "2026-07-12T06:00:00.000000Z",
            "pollSeconds": 300,
            "exposures": rows,
        },
    )
    app.goto("/")
    expect(app.page.locator("#tonight-card")).to_be_visible()
    # One row, not two: the panel has no instrument column because the
    # whole page is pinned, so a mixed table would show the same id twice
    # with nothing to tell them apart. The filter identifies which.
    expect(app.page.locator("#tonight-tbody tr")).to_have_count(1)
    expect(app.page.locator("#tonight-tbody tr")).to_contain_text("g_6")
    switch(app, "latiss")
    expect(app.page.locator("#tonight-tbody tr")).to_have_count(1)
    expect(app.page.locator("#tonight-tbody tr")).to_contain_text("holo4_003")


def test_a_tonight_link_carries_its_instrument(app: Any) -> None:
    from .conftest import routeJson

    routeJson(
        app.page,
        "**/api/live",
        {
            "enabled": True,
            "dayObs": 20260711,
            "watermark": "2026-07-12T06:00:00.000000Z",
            "pollSeconds": 300,
            "exposures": [
                {
                    "dataId": SHARED_ID,
                    "instrument": "lsstcam",
                    "obsEndUtc": CAM_T_ZERO_UTC.isoformat(),
                    "readyAtUtc": CAM_T_ZERO_UTC.isoformat(),
                    "ready": True,
                    "record": {},
                }
            ],
        },
    )
    app.goto("/")
    href = app.page.locator("#tonight-tbody tr a").first.get_attribute("href")
    assert href is not None
    assert f"dataId={SHARED_ID}" in href and "instrument=lsstcam" in href
