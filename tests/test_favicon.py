"""The favicon is wired into every page, and actually served.

Easy to get half-right: five separate <head> blocks, and a link tag that points
at a file the static mount does not expose fails silently — the browser just
shows the default icon, and nobody files a bug about a favicon.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from student_bot.config import PROJECT_ROOT

STATIC = PROJECT_ROOT / "src" / "student_bot" / "web" / "static"
ASSETS = ("favicon.svg", "favicon-32.png", "apple-touch-icon.png")


def test_the_assets_exist():
    for name in ASSETS:
        assert (STATIC / name).is_file(), f"{name} missing — run scripts/make_favicon.sh"


def test_the_svg_paints_an_opaque_background():
    """A transparent #000061 seal is close to invisible on a dark tab strip."""
    svg = (STATIC / "favicon.svg").read_text(encoding="utf-8")
    assert re.search(r'<rect[^>]*fill="#ffffff"', svg), "no white backing rect"


def test_the_svg_is_square():
    """Browsers letterbox a non-square icon in ways that clip the crown, so the
    448x502 source is padded rather than scaled."""
    svg = (STATIC / "favicon.svg").read_text(encoding="utf-8")
    vb = re.search(r'viewBox="([\d.\s-]+)"', svg).group(1).split()
    assert vb[2] == vb[3], f"viewBox is not square: {vb}"


def test_every_server_rendered_head_links_the_favicon():
    """All four <head> blocks in app.py, not just the one someone tested."""
    source = (PROJECT_ROOT / "src" / "student_bot" / "web" / "app.py").read_text(encoding="utf-8")
    heads = source.count("<!doctype html>")
    linked = source.count("_FAVICON_LINKS.format(")
    assert heads > 0
    assert linked == heads, f"{heads} head blocks but {linked} carry the favicon"


def test_the_chat_page_links_the_favicon():
    """static/index.html is hand-written and does not share app.py's head."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'rel="icon"' in html
    assert "favicon.svg" in html


@pytest.mark.parametrize(
    "name,content_type",
    [
        ("favicon.svg", "image/svg+xml"),
        ("favicon-32.png", "image/png"),
        ("apple-touch-icon.png", "image/png"),
    ],
)
def test_the_static_mount_serves_them(name, content_type):
    """The link tags are only as good as the mount behind them."""
    from fastapi.testclient import TestClient

    from student_bot.config import get_config
    from student_bot.web.app import create_app

    client = TestClient(create_app(get_config()))
    response = client.get(f"/static/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert len(response.content) > 500


def test_generated_assets_are_committed_not_built():
    """The generator needs rsvg-convert, which the Docker build does not have.
    If these ever stop being committed, the image ships without an icon."""
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    for name in ASSETS:
        assert name not in gitignore
    assert Path(PROJECT_ROOT / "scripts" / "make_favicon.sh").is_file()
