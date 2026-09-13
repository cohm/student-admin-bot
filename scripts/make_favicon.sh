#!/usr/bin/env bash
#
# Regenerate the favicon from the KTH logo already in the web static directory.
#
# Not run at build time — the outputs are committed, because they change only
# when the logo does and a build should not need a rasteriser. This exists so
# the generation is reproducible rather than remembered.
#
#   scripts/make_favicon.sh
#
# Needs rsvg-convert (brew install librsvg).
#
# WHY A WHITE SQUARE IS PAINTED IN
#   The seal is a single flat colour, #000061. Left transparent, it is close to
#   invisible against a dark browser tab strip — which is most of them now.
#
# WHY THE ARTWORK IS PADDED, NOT SCALED
#   KTH_logo_RGB_bla.svg is 448x502, taller than it is wide. A favicon is
#   square, and browsers letterbox a non-square icon in ways that clip the
#   crown. Padding the width symmetrically keeps the seal circular.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATIC="$REPO_ROOT/src/student_bot/web/static"
SOURCE="$STATIC/KTH_logo_RGB_bla.svg"

command -v rsvg-convert >/dev/null || {
  echo "rsvg-convert not found (brew install librsvg)" >&2
  exit 1
}
[[ -f "$SOURCE" ]] || { echo "missing $SOURCE" >&2; exit 1; }

python3 - "$SOURCE" "$STATIC/favicon.svg" <<'PY'
import re
import sys
from pathlib import Path

source, dest = Path(sys.argv[1]), Path(sys.argv[2])
raw = source.read_text(encoding="utf-8")

vb = re.search(r'viewBox="([\d.\s-]+)"', raw).group(1).split()
w, h = float(vb[2]), float(vb[3])
side = max(w, h)
dx, dy = (side - w) / 2, (side - h) / 2

inner = raw[raw.index(">", raw.index("<svg")) + 1 : raw.rindex("</svg>")]
styles = "\n".join(re.findall(r"<style[^>]*>.*?</style>", inner, re.S))
body = re.sub(r"<style[^>]*>.*?</style>", "", inner, flags=re.S).strip()

dest.write_text(
    f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side:g} {side:g}">
<!-- KTH seal, generated from {source.name} by scripts/make_favicon.sh.
     The white square is deliberate: the seal is #000061, and a transparent
     favicon is close to invisible against a dark browser tab strip. -->
<title>KTH</title>
{styles}
<rect width="{side:g}" height="{side:g}" fill="#ffffff"/>
<g transform="translate({dx:g} {dy:g})">
{body}
</g>
</svg>
''',
    encoding="utf-8",
)
print(f"favicon.svg      viewBox 0 0 {side:g} {side:g}")
PY

rsvg-convert -w 32  -h 32  "$STATIC/favicon.svg" -o "$STATIC/favicon-32.png"
rsvg-convert -w 180 -h 180 "$STATIC/favicon.svg" -o "$STATIC/apple-touch-icon.png"

echo "favicon-32.png   $(wc -c < "$STATIC/favicon-32.png") bytes"
echo "apple-touch-icon.png $(wc -c < "$STATIC/apple-touch-icon.png") bytes"
echo
echo "Bump the ?v= query in _FAVICON_LINKS (web/app.py) and static/index.html"
echo "if the artwork changed — browsers cache favicons aggressively."
