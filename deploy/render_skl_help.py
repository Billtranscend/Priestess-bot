"""Render plugins/skl_help/help.html to the static image sent by /skl帮助.

Run from anywhere after editing help.html (no bot restart needed; the plugin reads the image
per request):

    .venv/bin/python deploy/render_skl_help.py

Uses the Chromium that nonebot-plugin-htmlrender installed under data/nonebot_plugin_htmlrender
unless PLAYWRIGHT_BROWSERS_PATH is already set.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BROWSERS = ROOT / "data" / "nonebot_plugin_htmlrender"
if BROWSERS.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS))

from playwright.sync_api import sync_playwright  # noqa: E402

from plugins import ef_theme  # noqa: E402

PLUGIN_DIR = ROOT / "plugins" / "skl_help"
SOURCE = PLUGIN_DIR / "help.html"
OUTPUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else PLUGIN_DIR / "help.webp"

# help.html keeps a placeholder for the theme fonts: their file:// URLs depend on the checkout path.
html = SOURCE.read_text("utf-8").replace("/*EF_FONTS*/", ef_theme.fonts_css())
rendered = PLUGIN_DIR / ".help.rendered.html"
rendered.write_text(html, "utf-8")
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 800}, device_scale_factor=ef_theme.PAGE_SCALE)
        page.goto(rendered.as_uri(), wait_until="load")
        screenshot = page.screenshot(type="png", full_page=True)
        browser.close()
finally:
    rendered.unlink(missing_ok=True)

OUTPUT.write_bytes(ef_theme.to_webp(screenshot, ef_theme.PAGE_QUALITY))

os.chmod(OUTPUT, 0o600)
print(f"{OUTPUT} {OUTPUT.stat().st_size // 1024} KB")
