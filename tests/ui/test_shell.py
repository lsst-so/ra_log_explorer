"""The shell: which view a URL lands on, the base path, the FAQ overlay.

The routing decisions all live in ``app.js``'s bootstrap, and they are
the difference between a deep link working and a colleague being dumped
on the home page. The base-path tests are here because that is the one
piece of deployment shape a laptop run never exercises — every asset URL
and every API call has to carry the prefix, and a leftover
``__BASE_PATH__`` is a blank page in production.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import expect

from .corpus import DAY_OBS, RANGE_START, RANGE_STOP, SHARED_ID, UNKNOWN_ID, StagedCorpus


def test_root_shows_the_home_view(app: Any) -> None:
    app.goto("/")
    expect(app.page.locator("#home-view")).to_be_visible()
    for other in ("#explore-view", "#night-view", "#admin-view"):
        expect(app.page.locator(other)).to_be_hidden()


def test_dataId_deep_link_opens_the_explore_view(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#explore-view")).to_be_visible()
    expect(app.page.locator("#expId-display")).to_contain_text(str(SHARED_ID))
    expect(app.page.locator("#home-view")).to_be_hidden()


def test_dataId_deep_link_falls_back_to_home_when_nothing_is_cached(app: Any) -> None:
    """The link still works, it just can't skip the fetch — so it lands on
    home with the form filled in rather than on an empty explore view."""
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#home-view")).to_be_visible()
    expect(app.page.locator("#fetch-form input[name=exposureId]")).to_have_value(str(SHARED_ID))


def test_dayObs_deep_link_opens_the_night_view(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageNight()
    app.goto(f"/?dayObs={DAY_OBS}")
    expect(app.page.locator("#night-view")).to_be_visible()
    expect(app.page.locator("#night-dayobs-display")).to_contain_text(str(DAY_OBS))


def test_range_deep_link_opens_the_range_navigator(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageRange(RANGE_START, RANGE_STOP)
    app.goto(f"/?rangeStart={RANGE_START}&rangeStop={RANGE_STOP}")
    expect(app.page.locator("#explore-view")).to_be_visible()
    expect(app.page.locator("#range-nav")).to_be_visible()


def test_admin_link_opens_the_cache_browser(app: Any) -> None:
    app.goto("/")
    app.page.locator("#admin-link").click()
    expect(app.page.locator("#admin-view")).to_be_visible()
    expect(app.page.locator("#cache-table")).to_be_visible()


def test_autoFetch_deep_link_always_lands_on_home(app: Any, corpus: StagedCorpus) -> None:
    """``autoFetch=1`` means "fetch this for me", so it must go through
    the home form even when the state is already loadable — otherwise the
    night view's drilldown links would silently show a stale window."""
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam&autoFetch=1")
    expect(app.page.locator("#home-view")).to_be_visible()


def test_back_home_returns_to_the_home_view(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#explore-view")).to_be_visible()
    app.page.locator("#back-home").click()
    expect(app.page.locator("#home-view")).to_be_visible()
    expect(app.page.locator("#explore-view")).to_be_hidden()


def test_unknown_dataId_leaves_the_user_on_home(app: Any) -> None:
    app.goto(f"/?dataId={UNKNOWN_ID}&instrument=lsstcam")
    expect(app.page.locator("#home-view")).to_be_visible()


# ----- browser history ------------------------------------------------------
#
# Views are swapped inside one document, so the entries Back walks are
# only the ones the app pushes. When it pushed none, Back from any view
# left the application entirely and landed on whatever the tab happened
# to hold before it — which deployed reads as the app throwing you at a
# dead URL.


def test_back_from_a_fetched_exposure_returns_to_home(app: Any) -> None:
    """The reported bug: Back has to stay inside the app."""
    app.goto("/")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(SHARED_ID))
    expect(app.page.locator("#tzero-status")).to_contain_text("shutter close (TAI)")
    app.page.locator("#fetch-submit").click()
    expect(app.page.locator("#explore-view")).to_be_visible(timeout=60_000)
    assert f"dataId={SHARED_ID}" in app.page.url

    app.page.go_back()
    expect(app.page.locator("#home-view")).to_be_visible()
    expect(app.page.locator("#explore-view")).to_be_hidden()
    assert f"dataId={SHARED_ID}" not in app.page.url

    # And forward again, because a Back you can't undo is its own bug.
    app.page.go_forward()
    expect(app.page.locator("#explore-view")).to_be_visible()
    expect(app.page.locator("#expId-display")).to_contain_text(str(SHARED_ID))


def test_back_undoes_the_explore_view_s_home_button(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#explore-view")).to_be_visible()
    app.page.locator("#back-home").click()
    expect(app.page.locator("#home-view")).to_be_visible()

    app.page.go_back()
    expect(app.page.locator("#explore-view")).to_be_visible()
    expect(app.page.locator("#expId-display")).to_contain_text(str(SHARED_ID))


def test_back_undoes_the_night_view_s_home_button(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageNight()
    app.goto(f"/?dayObs={DAY_OBS}")
    expect(app.page.locator("#night-view")).to_be_visible()
    app.page.locator("#night-back-home").click()
    expect(app.page.locator("#home-view")).to_be_visible()

    app.page.go_back()
    expect(app.page.locator("#night-view")).to_be_visible()
    expect(app.page.locator("#night-dayobs-display")).to_contain_text(str(DAY_OBS))


def test_a_drilldown_leaves_no_autoFetch_entry_behind(app: Any, corpus: StagedCorpus) -> None:
    """``autoFetch=1`` means "fetch this for me", so the entry it lands on
    must be rewritten rather than added to: Back onto it would fire the
    fetch again instead of returning where the user came from."""
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app.goto("/")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam&autoFetch=1")
    expect(app.page.locator("#explore-view")).to_be_visible(timeout=60_000)
    assert "autoFetch" not in app.page.url, "the loaded view must not keep asking to be re-fetched"

    app.page.go_back()
    expect(app.page.locator("#home-view")).to_be_visible()
    assert "dataId" not in app.page.url


# ----- base path -----------------------------------------------------------


def test_everything_resolves_under_a_base_path(appFactory: Any, corpus: StagedCorpus) -> None:
    """Deployed, the app is served under a prefix. Assets, API calls and
    deep links all have to carry it; a single one that doesn't is a
    broken page in production and invisible on a laptop."""
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app = appFactory(basePath="/log-explorer")
    # Only the app's own origin: the webfont CDN is deliberately blocked
    # by the harness, and its failure is not the app's.
    failed: list[str] = []

    def note(url: str, label: str) -> None:
        if url.startswith(app.origin):
            failed.append(f"{label} {url}")

    app.page.on("requestfailed", lambda r: note(r.url, "failed"))
    app.page.on("response", lambda r: note(r.url, str(r.status)) if r.status >= 400 else None)

    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#explore-view")).to_be_visible()
    expect(app.page.locator("#timeline .tl-row").first).to_be_visible()
    assert failed == [], f"requests failed under the base path: {failed}"

    html = app.page.content()
    assert "__BASE_PATH__" not in html, "an unsubstituted placeholder is a blank page in the browser"
    assert app.page.evaluate("window.BASE_PATH") == "/log-explorer"
    assert app.page.evaluate("window.apiUrl('/api/site')") == "/log-explorer/api/site"


def test_paths_outside_the_base_path_are_not_served(appFactory: Any) -> None:
    """They belong to whatever else shares the hostname."""
    app = appFactory(basePath="/log-explorer")
    resp = app.page.goto(f"{app.origin}/")
    assert resp is not None and resp.status == 404


def test_a_whole_fetch_runs_under_a_base_path(
    appFactory: Any, corpus: StagedCorpus, fakeLogcli: list[str]
) -> None:
    """The fetch flow — POST /api/fetch, the SSE progress stream, the
    summary that follows — has to be prefixed too.

    The static test above only covers a view that was staged in advance.
    Every URL in the submit path goes through ``apiUrl()`` today, but a
    hand-built ``/api/fetch`` or ``new EventSource('/api/…')`` added
    later would pass the entire suite and break only in the deployment,
    which is the only place anyone runs this.
    """
    corpus.dropLiveSidecar()
    app = appFactory(basePath="/log-explorer")
    failed: list[str] = []
    app.page.on(
        "response",
        lambda r: (
            failed.append(f"{r.status} {r.url}") if r.url.startswith(app.origin) and r.status >= 400 else None
        ),
    )
    app.goto("/")
    app.page.locator("#fetch-form input[name=exposureId]").fill(str(SHARED_ID))
    expect(app.page.locator("#tzero-status")).to_contain_text("shutter close")
    app.page.locator("#fetch-submit").click()
    # The progress stream is the part that only exists during a fetch.
    expect(app.page.locator("#explore-view")).to_be_visible(timeout=60_000)
    expect(app.page.locator("#timeline .tl-row").first).to_be_visible()
    assert failed == [], f"requests failed under the base path: {failed}"
    assert "/log-explorer/?" in app.page.url or app.page.url.startswith(
        f"{app.origin}/log-explorer"
    ), app.page.url


def test_history_entries_carry_the_base_path(appFactory: Any, corpus: StagedCorpus) -> None:
    """A pushed entry that dropped the prefix would send Back to whatever
    else shares the hostname — which is the shape of the bug this
    replaced, in the one mode a laptop run never exercises."""
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app = appFactory(basePath="/log-explorer")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    expect(app.page.locator("#explore-view")).to_be_visible()
    app.page.locator("#back-home").click()
    expect(app.page.locator("#home-view")).to_be_visible()
    # Home re-stamps its instrument pin on arrival, so the query isn't
    # empty; the prefix and the dropped exposure are the point.
    assert app.page.url.startswith(f"{app.origin}/log-explorer/")
    assert "dataId" not in app.page.url

    app.page.go_back()
    expect(app.page.locator("#explore-view")).to_be_visible()
    assert app.page.url.startswith(f"{app.origin}/log-explorer/?")


# ----- the images -----------------------------------------------------------


def test_the_logo_renders_in_every_view_s_topbar(app: Any, corpus: StagedCorpus) -> None:
    """`naturalWidth` is zero for a broken image, and a broken image is
    what a wrong path or a wrong Content-Type produces — the page looks
    fine otherwise, with a gap where the mark should be. Checked per
    view because the header is four separate blocks of markup rather
    than one shared component, so three of them can be right."""
    corpus.stageExposure(SHARED_ID, "lsstcam")
    corpus.stageNight()
    views = {
        "/": "#home-view",
        "/?admin=1": "#admin-view",
        f"/?dataId={SHARED_ID}&instrument=lsstcam": "#explore-view",
        f"/?dayObs={DAY_OBS}": "#night-view",
    }
    for path, view in views.items():
        app.goto(path)
        expect(app.page.locator(f"{view} .brand-mark")).to_be_visible()
        width = app.page.evaluate(f"document.querySelector('{view} .brand-mark').naturalWidth")
        assert width == 323, f"{view} drew a broken mark (naturalWidth={width})"


def test_the_tab_icon_is_declared_and_loads(app: Any) -> None:
    app.goto("/")
    assert app.page.locator("link[rel=icon]").get_attribute("href") == "/static/favicon.png"
    resp = app.page.request.get(app.url("/static/favicon.png"))
    assert resp.status == 200
    assert resp.headers["content-type"] == "image/png"


def test_the_images_resolve_under_a_base_path(appFactory: Any) -> None:
    """Both are named in the HTML with the prefix substituted in, which
    is the one thing a laptop run can't tell you."""
    app = appFactory(basePath="/log-explorer")
    app.goto("/")
    expect(app.page.locator("#home-view .brand-mark")).to_be_visible()
    assert app.page.evaluate("document.querySelector('#home-view .brand-mark').naturalWidth") == 323
    assert app.page.locator("link[rel=icon]").get_attribute("href") == "/log-explorer/static/favicon.png"
    assert app.page.request.get(app.url("/static/logo.png")).status == 200


# ----- the "what is this?" overlay ------------------------------------------


def test_faq_overlay_opens_and_closes(app: Any) -> None:
    app.goto("/")
    overlay = app.page.locator("#faq-overlay")
    expect(overlay).to_be_hidden()
    app.page.locator("#home-view .faq-toggle").click()
    expect(overlay).to_be_visible()
    app.page.locator("#faq-close").click()
    expect(overlay).to_be_hidden()


def test_faq_overlay_closes_on_escape_and_on_backdrop_click(app: Any) -> None:
    app.goto("/")
    overlay = app.page.locator("#faq-overlay")
    app.page.locator("#home-view .faq-toggle").click()
    expect(overlay).to_be_visible()
    app.page.keyboard.press("Escape")
    expect(overlay).to_be_hidden()

    app.page.locator("#home-view .faq-toggle").click()
    expect(overlay).to_be_visible()
    # Click the backdrop, not the panel: the panel must swallow its own
    # clicks or reading the text would dismiss it.
    overlay.click(position={"x": 5, "y": 5})
    expect(overlay).to_be_hidden()
    app.page.locator("#home-view .faq-toggle").click()
    app.page.locator("#faq-panel h2").click()
    expect(overlay).to_be_visible()


def test_faq_overlay_reachable_from_the_explore_view(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageExposure(SHARED_ID, "lsstcam")
    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    app.page.locator("#explore-view .faq-toggle").click()
    expect(app.page.locator("#faq-overlay")).to_be_visible()
