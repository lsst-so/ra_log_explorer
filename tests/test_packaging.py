"""What the wheel ships — the half of the deployment a laptop never sees.

The container installs the package (`pip install --no-deps .`) instead of
running out of the source tree, so `[tool.setuptools.package-data]` in
`pyproject.toml` decides what exists in production. An asset the HTML
asks for that the globs don't match works perfectly on a laptop and 404s
in the deployment — a `static/img/logo.png` under a `static/*` glob is
exactly that trap, since the glob doesn't cross a directory separator.
"""

from __future__ import annotations

import re
import struct
import tomllib
from pathlib import Path

from ra_log_explorer import config

PACKAGE = Path(config.__file__).resolve().parent
REPO = PACKAGE.parent

# Static files go out with Cache-Control: no-store (deliberately — it is
# what makes a redeploy and a local edit both take effect on reload), so
# every page load pays for every one of them.
MAX_STATIC_BYTES = 200 * 1024


def _packageDataGlobs() -> list[str]:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    return list(pyproject["tool"]["setuptools"]["package-data"]["ra_log_explorer"])


def _shippedFiles() -> set[str]:
    """The files the globs actually select, by the same rules setuptools uses."""
    shipped: set[str] = set()
    for pattern in _packageDataGlobs():
        shipped |= {p.relative_to(PACKAGE).as_posix() for p in PACKAGE.glob(pattern) if p.is_file()}
    return shipped


def _assetsTheTemplateAsksFor() -> set[str]:
    html = (PACKAGE / "templates" / "timeline.html").read_text()
    return set(re.findall(r"__BASE_PATH__/(static/[\w.\-/]+)", html))


def test_every_asset_the_page_asks_for_is_shipped() -> None:
    referenced = _assetsTheTemplateAsksFor()
    assert referenced, "found no asset references at all — has the template stopped using __BASE_PATH__?"
    shipped = _shippedFiles()
    for rel in sorted(referenced):
        assert (PACKAGE / rel).is_file(), f"{rel} is referenced by the template but is not on disk"
        assert rel in shipped, (
            f"{rel} is not matched by package-data {_packageDataGlobs()}, so an installed "
            "package would not have it — the deployment would 404 it while a laptop run serves it"
        )


def test_the_images_are_referenced_and_shipped() -> None:
    """Named outright, because they are the two assets nothing else
    would miss: the page renders without them, just wrong."""
    referenced = _assetsTheTemplateAsksFor()
    assert "static/favicon.png" in referenced
    assert "static/logo.png" in referenced


def test_static_assets_stay_small_enough_to_send_on_every_load() -> None:
    """The source art in `assets/` is about a megabyte apiece; what lives
    here are the downscaled copies. This is what notices if a full-size
    original is ever committed over one of them."""
    oversized = {
        p.name: p.stat().st_size
        for p in (PACKAGE / "static").iterdir()
        if p.is_file() and p.stat().st_size > MAX_STATIC_BYTES
    }
    assert not oversized, f"static assets over {MAX_STATIC_BYTES // 1024} KiB: {oversized}"


def _pngSize(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def test_image_tags_declare_the_real_intrinsic_size() -> None:
    """`width`/`height` on an `<img>` reserve its box before the bytes
    arrive; without them — or with them unparseable — the page reflows as
    each image lands. They also go stale the moment the art is
    regenerated at a different size, silently, because nothing renders
    differently until someone is on a slow connection. Hence a check
    rather than an eyeball.
    """
    html = (PACKAGE / "templates" / "timeline.html").read_text()
    checked = 0
    for tag in re.findall(r"<img\b[^>]*>", html, flags=re.S):
        src = re.search(r'src="__BASE_PATH__/(static/[\w.\-/]+)"', tag)
        if src is None:
            continue
        rel = src.group(1)
        declared = []
        for attr in ("width", "height"):
            m = re.search(rf'{attr}="([^"]*)"', tag)
            assert m is not None, f"{rel}: no {attr} on {tag.strip()}"
            assert m.group(1).isdigit(), f"{rel}: {attr}={m.group(1)!r} is not a number"
            declared.append(int(m.group(1)))
        actual = _pngSize(PACKAGE / rel)
        assert (
            tuple(declared) == actual
        ), f"{rel} is {actual[0]}x{actual[1]}, tag says {declared[0]}x{declared[1]}"
        checked += 1
    assert checked, "no prefixed <img> tags found — has the template stopped using __BASE_PATH__?"
