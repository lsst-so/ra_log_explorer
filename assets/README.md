# Source art

The full-resolution originals the app's images are derived from. This
directory is *not* part of the Python package and is excluded from the
container build context: what the app serves are the downscaled copies
in [`ra_log_explorer/static/`](../ra_log_explorer/static/). Static files
go out with `Cache-Control: no-store` — deliberately, so a redeploy and
a local edit both take effect on the next reload — which means every
page load re-fetches them, and a megabyte of PNG each time is not a
nicety anyone would thank us for.

Regenerate from the repo root after changing the art; nothing does it
for you, and `tests/test_packaging.py` will fail if the `<img>` tags in
`timeline.html` still declare the old size.

**`log_explorer_icon.png`** (1085×1085) → `static/favicon.png`
(256×256, 7 KB), the browser-tab icon:

```bash
magick assets/log_explorer_icon.png -resize 256x256 -strip -colors 64 \
  -define png:compression-level=9 ra_log_explorer/static/favicon.png
```

**`log_explorer_logo.png`** (2444×1226) → `static/logo.png` (323×160,
30 KB), the lockup at the top-left of every view:

```bash
magick assets/log_explorer_logo.png -alpha off -fuzz 2% -trim +repage \
  -resize x160 -strip -colors 128 -dither FloydSteinberg \
  -define png:compression-level=9 ra_log_explorer/static/logo.png
```

`-alpha off` because the source carries an alpha channel that is fully
opaque — white behind the drawing, not transparency — and flattening it
away keeps the palette reduction from spending colours on it. The
`-trim` drops the white margin around the art, which is what lets the
bar be sized to the drawing rather than to the padding. The palette
reduction takes it from ~250 KB to 30 KB, and 160 px of height covers a
3× display of a 56 px mark.

That 56 px is the size the topbar draws it at, and it is chosen from
below: at 48 px the *Vera C. Rubin Observatory* wordmark stops reading
as words. Changing it means changing `#topbar .brand-mark`'s height in
`style.css` — and the height of every view's header with it.
