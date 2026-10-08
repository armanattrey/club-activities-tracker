"""Build the static frontend with its hosted API origin injected."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
API_BASE_URL = os.environ.get("API_BASE_URL", "").rstrip("/")
parsed = urlparse(API_BASE_URL)
if parsed.scheme not in {"http", "https"} or not parsed.netloc:
    raise SystemExit("API_BASE_URL must be the hosted API's HTTP(S) origin")

source = ROOT / "frontend" / "index.html"
html = source.read_text()
needle = '<div id="app"></div><script>'
if needle not in html:
    raise SystemExit("Could not find frontend bootstrap insertion point")
html = html.replace(
    needle,
    '<div id="app"></div><script src="/config.js"></script><script>',
    1,
)

output = ROOT / "dist" / "public-frontend"
output.mkdir(parents=True, exist_ok=True)
(output / "index.html").write_text(html)
(output / "config.js").write_text(
    "window.CLUB_TRACKER_API = " + json.dumps(API_BASE_URL) + ";\n"
)
