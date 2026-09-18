"""The admin view: what is on the cache volume, and getting rid of it.

The cache is shared by everyone using a deployment and is the thing that
fills a PVC, so this page is how an operator sees what is there and
reclaims it. Deletions really delete: each test checks the directory is
gone from disk, not just from the table.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import expect

from .corpus import DAY_OBS, RANGE_START, RANGE_STOP, SHARED_ID, StagedCorpus


def openAdmin(app: Any) -> None:
    app.goto("/?admin=1")
    expect(app.page.locator("#admin-view")).to_be_visible()


def test_cached_windows_are_listed_with_their_kind(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageExposure(SHARED_ID, "lsstcam")
    corpus.stageNight()
    corpus.stageRange(RANGE_START, RANGE_STOP)
    openAdmin(app)
    rows = app.page.locator("#cache-tbody tr")
    expect(rows).to_have_count(3)
    # CSS upper-cases the badges.
    kinds = sorted(t.lower() for t in app.page.locator("#cache-tbody .cache-kind").all_inner_texts())
    assert kinds == ["exposure", "night", "range"], kinds
    expect(app.page.locator("#cache-summary")).to_contain_text("3")
    # A total row, so "how much is this costing me" is one glance.
    expect(app.page.locator("#cache-tfoot .cache-total-row")).to_be_visible()


def test_each_row_links_back_to_the_run_it_holds(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageExposure(SHARED_ID, "latiss")
    corpus.stageNight()
    corpus.stageRange(RANGE_START, RANGE_STOP)
    openAdmin(app)
    # Wait for the rows the links live in: the table is filled by an
    # async /api/cache call, so reading the links straight after the view
    # appears samples an empty tbody often enough to matter.
    links = app.page.locator("#cache-tbody a.cache-key-link")
    expect(links).to_have_count(3)
    hrefs = [a.get_attribute("href") for a in links.all()]
    joined = " ".join(h or "" for h in hrefs)
    # The exposure link carries the instrument the window was fetched
    # under. Without it the id is ambiguous — this very id exists on
    # LSSTCam too, an hour away — and the link would open that one.
    assert f"dataId={SHARED_ID}&instrument=latiss" in joined, joined
    assert f"dayObs={DAY_OBS}" in joined
    assert f"rangeStart={RANGE_START}" in joined and f"rangeStop={RANGE_STOP}" in joined


def test_the_two_instruments_twins_each_get_their_own_link(app: Any, corpus: StagedCorpus) -> None:
    """Both instruments' exposure of one id can be cached at once — two
    windows an hour apart sharing 13 digits. The listing has to say which
    is which and link each to its own run; a bare id would render two
    identical-looking rows whose links both open the same exposure."""
    corpus.stageExposure(SHARED_ID, "lsstcam")
    corpus.stageExposure(SHARED_ID, "latiss")
    openAdmin(app)
    links = app.page.locator("#cache-tbody a.cache-key-link")
    expect(links).to_have_count(2)
    hrefs = [a.get_attribute("href") or "" for a in links.all()]
    assert any(f"dataId={SHARED_ID}&instrument=lsstcam" in h for h in hrefs), hrefs
    assert any(f"dataId={SHARED_ID}&instrument=latiss" in h for h in hrefs), hrefs
    # And visibly, not just in the href — the two rows are otherwise
    # indistinguishable to anyone reading the page.
    labels = sorted(t.strip() for t in links.all_inner_texts())
    assert labels == [f"{SHARED_ID} (latiss)", f"{SHARED_ID} (lsstcam)"], labels
    # Following one lands on that instrument's exposure, not the twin.
    latissHref = next(h for h in hrefs if "latiss" in h)
    app.page.goto(app.origin + latissHref)
    expect(app.page.locator("#explore-view")).to_be_visible()
    expect(app.page.locator("#exposure-info")).to_contain_text("latiss")


def test_deleting_one_window_removes_it_from_disk(app: Any, corpus: StagedCorpus) -> None:
    cacheDir = corpus.stageExposure(SHARED_ID, "lsstcam")
    corpus.stageNight()
    openAdmin(app)
    expect(app.page.locator("#cache-tbody tr")).to_have_count(2)
    app.page.on("dialog", lambda d: d.accept())
    row = app.page.locator("#cache-tbody tr", has_text=str(SHARED_ID))
    row.locator("button.delete").click()
    expect(app.page.locator("#cache-tbody tr")).to_have_count(1)
    assert not cacheDir.exists(), "the row went but the bytes stayed"


def test_declining_the_confirmation_keeps_the_window(app: Any, corpus: StagedCorpus) -> None:
    cacheDir = corpus.stageExposure(SHARED_ID, "lsstcam")
    openAdmin(app)
    app.page.on("dialog", lambda d: d.dismiss())
    app.page.locator("#cache-tbody button.delete").first.click()
    expect(app.page.locator("#cache-tbody tr")).to_have_count(1)
    assert cacheDir.exists()


def test_flushing_the_cache_empties_the_table(app: Any, corpus: StagedCorpus) -> None:
    exposureDir = corpus.stageExposure(SHARED_ID, "lsstcam")
    nightDir = corpus.stageNight()
    openAdmin(app)
    app.page.on("dialog", lambda d: d.accept())
    app.page.locator("#cache-delete-all").click()
    expect(app.page.locator("#cache-tbody tr")).to_have_count(0)
    assert not exposureDir.exists() and not nightDir.exists()
    # And the button has nothing left to do.
    expect(app.page.locator("#cache-delete-all")).to_be_disabled()


def test_an_empty_cache_says_so(app: Any) -> None:
    openAdmin(app)
    expect(app.page.locator("#cache-tbody tr")).to_have_count(0)
    expect(app.page.locator("#cache-delete-all")).to_be_disabled()


def test_deleting_a_loaded_window_boots_its_view_home(app: Any, corpus: StagedCorpus) -> None:
    """The view is served out of memory but points at a directory. Once
    that is gone the view is a lie, so the next look must land on home
    rather than on a timeline nothing backs."""
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#explore-view")).to_be_visible()

    openAdmin(app)
    app.page.on("dialog", lambda d: d.accept())
    app.page.locator("#cache-tbody button.delete").first.click()
    expect(app.page.locator("#cache-tbody tr")).to_have_count(0)

    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#home-view")).to_be_visible()


def test_the_live_night_dir_is_not_offered_for_deletion(app: Any, liveCorpus: StagedCorpus) -> None:
    """While the poller owns a night it has no ``_meta.json``, so it is
    deliberately absent from the listing — deleting it from under a
    running poller is not something to make one click away."""
    liveCorpus.stageExposure(SHARED_ID, "lsstcam")
    openAdmin(app)
    rows = app.page.locator("#cache-tbody tr")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text(str(SHARED_ID))
