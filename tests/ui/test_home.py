"""The home view: resolving a dataId, and starting a fetch with it.

The fetch tests drive the whole path — form submit, job, SSE progress,
parse, view switch — with only ``logcli`` standing in (see
:func:`fakeLogcli`). That is the flow every user takes, and nothing
below the browser is mocked out of it.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import expect

from .corpus import (
    DAY_OBS,
    RANGE_START,
    RANGE_STOP,
    SHARED_ID,
    UNKNOWN_ID,
    StagedCorpus,
    holdsExposure,
)


def test_typing_a_dataId_resolves_its_shutter_close(app: Any) -> None:
    app.goto("/")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(SHARED_ID))
    expect(app.page.locator("#tzero-status")).to_contain_text("shutter close (TAI)")
    expect(app.page.locator("#tzero-status")).to_contain_text("2026-07-12T04:21:59")


def test_a_partial_dataId_is_not_looked_up(app: Any) -> None:
    """13 digits or nothing: firing a lookup per keystroke produces a
    stream of "no record for 20260" that is worse than waiting."""
    app.goto("/")
    calls: list[str] = []
    app.page.on("request", lambda r: calls.append(r.url) if "/api/exposure-time/" in r.url else None)
    app.page.locator("#fetch-form input[name=exposureId]").fill("20260711")
    expect(app.page.locator("#tzero-status")).to_contain_text("keep typing")
    app.page.locator("#fetch-form input[name=exposureId]").fill("202607110044512")
    expect(app.page.locator("#tzero-status")).to_contain_text("too long")
    assert calls == []


def test_no_consdb_token_says_so_and_still_lets_you_proceed(app: Any) -> None:
    """The common local situation: no RSP token on this machine. The
    message has to name the file it wants, and the manual field has to
    appear — a dead end here would make the tool unusable rather than
    inconvenient."""
    app.goto("/")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(UNKNOWN_ID))
    expect(app.page.locator("#tzero-status")).to_contain_text("ConsDB token file")
    expect(app.page.locator("#manual-tzero")).to_be_visible()


def test_a_dataId_consdb_has_no_row_for_offers_a_manual_shutter_close(
    app: Any, siteCatalog: Any, monkeypatch: Any
) -> None:
    """ConsDB is reachable and simply has no such exposure — a different
    failure from "no token", and one the user can work around by typing
    the shutter close they already know."""
    import io
    import json as jsonlib

    from ra_log_explorer import exposureTimes

    siteCatalog.writeSummitToken()
    monkeypatch.setattr(
        exposureTimes,
        "urlopen",
        lambda *a, **k: io.BytesIO(jsonlib.dumps({"columns": [], "data": []}).encode()),
    )
    app.goto("/")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(UNKNOWN_ID))
    expect(app.page.locator("#tzero-status")).to_contain_text("no exposure-time record")
    expect(app.page.locator("#manual-tzero")).to_be_visible()


def test_a_hand_typed_shutter_close_can_drive_a_fetch(
    app: Any, corpus: StagedCorpus, fakeLogcli: list[str]
) -> None:
    """The stand-in path all the way through: type the timestamp, fetch,
    land on the timeline."""
    corpus.dropLiveSidecar()
    app.goto("/")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(UNKNOWN_ID))
    expect(app.page.locator("#manual-tzero")).to_be_visible()
    app.page.locator("input[name=manualTZero]").fill("2026-07-12T04:21:59.502000")
    expect(app.page.locator("#manual-tzero-status")).to_contain_text("overrides ConsDB")
    app.page.locator("#fetch-submit").click()
    expect(app.page.locator("#explore-view")).to_be_visible(timeout=60_000)
    # Labelled as hand-entered, not dressed up as a ConsDB value.
    expect(app.page.locator("#ref-select")).to_contain_text("manual")


def test_fetching_an_exposure_end_to_end(app: Any, corpus: StagedCorpus, fakeLogcli: list[str]) -> None:
    """Submit the form and land on the timeline: job, SSE progress,
    parse, view switch. Nothing here is pre-loaded — the window is
    fetched during the test."""
    corpus.dropLiveSidecar()  # otherwise the window would be sliced, not fetched
    app.goto("/")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(SHARED_ID))
    expect(app.page.locator("#tzero-status")).to_contain_text("shutter close (TAI)")
    app.page.locator("#fetch-submit").click()

    expect(app.page.locator("#fetch-progress")).to_be_visible()
    expect(app.page.locator("#explore-view")).to_be_visible(timeout=60_000)
    expect(app.page.locator("#timeline .tl-row").first).to_be_visible()
    expect(app.page.locator("#expId-display")).to_contain_text(str(SHARED_ID))
    assert "series" in fakeLogcli, "the fetch should have listed pods"
    assert fakeLogcli.count("query") > 1, "and then fetched them"
    # The URL carries the exposure, so the tab can be reloaded or shared.
    assert f"dataId={SHARED_ID}" in app.page.url


def test_a_fetched_window_lands_in_the_cache(app: Any, corpus: StagedCorpus, fakeLogcli: list[str]) -> None:
    """The fetch is only useful if the next person gets it for free."""
    corpus.dropLiveSidecar()
    app.goto("/")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(SHARED_ID))
    expect(app.page.locator("#tzero-status")).to_contain_text("shutter close (TAI)")
    app.page.locator("#fetch-submit").click()
    expect(app.page.locator("#explore-view")).to_be_visible(timeout=60_000)

    listing = app.apiJson("/api/cache")
    windows = [w for w in listing["windows"] if holdsExposure(w, SHARED_ID)]
    assert windows, listing["windows"]
    assert windows[0]["kind"] == "exposure"
    assert windows[0]["podCount"] > 5


def test_fetching_a_night_end_to_end(app: Any, corpus: StagedCorpus, fakeLogcli: list[str]) -> None:
    corpus.dropLiveSidecar()
    app.goto("/")
    app.page.locator("#night-form input[name=dayObs]").fill(str(DAY_OBS))
    app.page.locator("#night-submit").click()
    expect(app.page.locator("#night-view")).to_be_visible(timeout=120_000)
    expect(app.page.locator("#night-stats .night-stat").first).to_be_visible()
    # Night mode filters to the AOS pods at the Loki layer, so the
    # listing query must carry the regex rather than fetching everything.
    assert "series" in fakeLogcli


def test_the_window_pads_are_editable_and_reach_the_fetch(
    app: Any, corpus: StagedCorpus, fakeLogcli: list[str]
) -> None:
    """Widening the window to catch a neighbouring exposure is a real
    investigative move, so the pads are the one thing on this page the
    visitor gets to change."""
    corpus.dropLiveSidecar()
    app.goto("/")
    app.page.locator("#advanced-options summary").click()
    app.page.locator("input[name=windowAfter]").fill("60")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(SHARED_ID))
    expect(app.page.locator("#tzero-status")).to_contain_text("shutter close (TAI)")
    app.page.locator("#fetch-submit").click()
    expect(app.page.locator("#explore-view")).to_be_visible(timeout=60_000)

    windows = app.apiJson("/api/cache")["windows"]
    fetched = [w for w in windows if holdsExposure(w, SHARED_ID)]
    assert fetched, windows
    # t0 + 60 s, not the default t0 + 300 s.
    assert fetched[0]["toIso"].startswith("2026-07-12T04:22:22"), fetched[0]["toIso"]


def test_the_range_form_loads_a_run_of_exposures(
    app: Any, corpus: StagedCorpus, fakeLogcli: list[str]
) -> None:
    corpus.dropLiveSidecar()
    app.goto("/")
    app.page.locator("#advanced-options summary").click()
    app.page.locator("input[name=rangeStart]").fill(str(RANGE_START))
    app.page.locator("input[name=rangeStop]").fill(str(RANGE_STOP))
    expect(app.page.locator("#range-start-status")).to_contain_text("2026-07-12")
    expect(app.page.locator("#range-stop-status")).to_contain_text("2026-07-12")
    app.page.locator("#range-submit").click()
    expect(app.page.locator("#range-nav")).to_be_visible(timeout=120_000)


def test_a_hostile_consdb_string_is_rendered_as_text(app: Any, corpus: StagedCorpus) -> None:
    """``observation_reason`` and friends come from ConsDB, which is not
    a trusted source of markup. Every one of them goes through the
    escaper on its way into a table."""
    from ra_log_explorer import exposureTimes

    nasty = "<img src=x onerror=window.__pwned=1>"
    exposureTimes.storeCachedRecord(
        SHARED_ID,
        {
            "exposure_id": SHARED_ID,
            "obs_end": "2026-07-12T04:21:59.502000",
            "instrument": "lsstcam",
            "img_type": "science",
            "observation_reason": nasty,
        },
        siteName="summit",
    )
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#exposure-info")).to_contain_text(nasty)
    assert app.page.evaluate("() => window.__pwned") is None
    assert app.page.locator("#exposure-info img").count() == 0
