"""Two tabs at once — the shape people actually work in.

Somebody watching a night keeps the night open in one tab and opens
exposures out of it into others: every drilldown link and every admin
cache row is written to be opened that way, and the topbar's own links
carry `target="_blank"`. Nothing about that is supposed to interact —
the server keys its loaded states by (dataId, instrument) / dayObs /
range, and each browser tab keeps its own history — but "supposed to"
is what these tests replace, especially now that the app pushes history
entries of its own.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from ra_log_explorer import server as serverModule

from .corpus import DAY_OBS, RANGE_STOP, SHARED_ID, StagedCorpus


def openTab(app: Any, path: str = "/") -> Page:
    """A second tab on the same app, in the same browser context.

    Same context on purpose: two tabs of one browser share cookies and
    localStorage, which is exactly the coupling these tests are checking
    doesn't turn into shared *views*.
    """
    page = app.page.context.new_page()
    page.goto(f"{app.origin}{app.basePath}{path}")
    return page


def test_two_tabs_hold_two_exposures_at_once(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageExposure(SHARED_ID, "lsstcam")
    corpus.stageExposure(RANGE_STOP, "lsstcam")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#expId-display")).to_contain_text(str(SHARED_ID))

    second = openTab(app, f"/?dataId={RANGE_STOP}&instrument=lsstcam")
    expect(second.locator("#expId-display")).to_contain_text(str(RANGE_STOP))
    # The first tab is untouched by the second one loading, and stays
    # right through a reload — the server holds both states, so neither
    # tab has to re-fetch to keep what it is showing.
    expect(app.page.locator("#expId-display")).to_contain_text(str(SHARED_ID))
    app.page.reload()
    expect(app.page.locator("#expId-display")).to_contain_text(str(SHARED_ID))
    second.close()


def test_a_night_tab_and_an_exposure_tab_coexist(app: Any, corpus: StagedCorpus) -> None:
    """The drilldown pattern: keep the night, open the exposure beside it."""
    corpus.stageNight()
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app.goto(f"/?dayObs={DAY_OBS}")
    expect(app.page.locator("#night-view")).to_be_visible()

    second = openTab(app, f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(second.locator("#explore-view")).to_be_visible()
    expect(app.page.locator("#night-view")).to_be_visible()
    expect(app.page.locator("#night-dayobs-display")).to_contain_text(str(DAY_OBS))
    second.close()


def test_the_same_id_in_two_tabs_keeps_each_instrument(app: Any, corpus: StagedCorpus) -> None:
    """The twins, side by side: one id, two instruments, two tabs. The
    instrument pin is also kept in localStorage — shared between tabs —
    so this is where a view could quietly adopt the other tab's."""
    corpus.stageExposure(SHARED_ID, "lsstcam")
    corpus.stageExposure(SHARED_ID, "latiss")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#exposure-info")).to_contain_text("lsstcam")

    second = openTab(app, f"/?dataId={SHARED_ID}&instrument=latiss")
    expect(second.locator("#exposure-info")).to_contain_text("latiss")
    expect(app.page.locator("#exposure-info")).to_contain_text("lsstcam")
    app.page.reload()
    expect(app.page.locator("#exposure-info")).to_contain_text("lsstcam")
    second.close()


def test_back_in_one_tab_leaves_the_other_alone(app: Any, corpus: StagedCorpus) -> None:
    """History is per-tab, and the entries the app now pushes must not
    change that."""
    corpus.stageExposure(RANGE_STOP, "lsstcam")
    second = openTab(app, f"/?dataId={RANGE_STOP}&instrument=lsstcam")
    expect(second.locator("#explore-view")).to_be_visible()

    app.goto("/")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(SHARED_ID))
    expect(app.page.locator("#tzero-status")).to_contain_text("shutter close (TAI)")
    app.page.locator("#fetch-submit").click()
    expect(app.page.locator("#explore-view")).to_be_visible(timeout=60_000)
    app.page.go_back()
    expect(app.page.locator("#home-view")).to_be_visible()

    # The other tab neither followed it home nor lost its exposure.
    expect(second.locator("#explore-view")).to_be_visible()
    expect(second.locator("#expId-display")).to_contain_text(str(RANGE_STOP))
    assert f"dataId={RANGE_STOP}" in second.url
    second.close()


def test_a_cache_row_opens_its_view_in_a_real_new_tab(appFactory: Any, corpus: StagedCorpus) -> None:
    """The admin table's links are ``target="_blank"``, so following one
    is a browser-level navigation the app never sees. Under a base path,
    because a link that dropped the prefix would open a tab on whatever
    else shares the hostname."""
    corpus.stageExposure(SHARED_ID, "latiss")
    app = appFactory(basePath="/log-explorer")
    app.goto("/?admin=1")
    link = app.page.locator("#cache-tbody a.cache-key-link")
    expect(link).to_have_count(1)

    with app.page.context.expect_page() as opened:
        link.first.click()
    tab = opened.value
    tab.wait_for_load_state()
    assert tab.url.startswith(f"{app.origin}/log-explorer/?"), tab.url
    expect(tab.locator("#explore-view")).to_be_visible()
    expect(tab.locator("#exposure-info")).to_contain_text("latiss")
    # And the tab it was opened from is still the admin view.
    expect(app.page.locator("#admin-view")).to_be_visible()
    tab.close()


def test_a_tab_whose_state_was_evicted_still_answers_back(
    app: Any, corpus: StagedCorpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server holds eight loaded states per kind, so a ninth tab
    evicts the oldest. The evicted tab is not broken — its URL carries
    the routing key — but that means Back onto a view can land on a state
    the server no longer has in memory, and the rebuild from cache has to
    carry it. Squeezed to one state here so two tabs are enough.
    """
    monkeypatch.setattr(serverModule, "_MAX_LOADED_STATES", 1)
    corpus.stageExposure(SHARED_ID, "lsstcam")
    corpus.stageExposure(RANGE_STOP, "lsstcam")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#expId-display")).to_contain_text(str(SHARED_ID))
    app.page.locator("#back-home").click()
    expect(app.page.locator("#home-view")).to_be_visible()

    # A second tab, which evicts the first tab's state from memory.
    second = openTab(app, f"/?dataId={RANGE_STOP}&instrument=lsstcam")
    expect(second.locator("#expId-display")).to_contain_text(str(RANGE_STOP))
    assert len(app.ctx.exposureStates) == 1, "the second tab was meant to evict the first tab's state"

    app.page.go_back()
    expect(app.page.locator("#explore-view")).to_be_visible()
    expect(app.page.locator("#expId-display")).to_contain_text(str(SHARED_ID))
    expect(app.page.locator("#timeline .tl-row").first).to_be_visible()
    second.close()
