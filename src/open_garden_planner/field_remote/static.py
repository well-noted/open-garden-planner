"""The phone page, served from memory (design 1.5: "static files from an in-memory dict").

The files under ``static/`` are read once at import. A request is matched against this fixed
table by exact path, so no request value ever reaches the file system. They hold no secret and
no plan data, so they are served without a token (the page must load before it can read the
token from the URL fragment); every ``/api/`` route still needs the header.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_DIR = Path(__file__).with_name("static")
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
#: Size budget for everything the phone downloads (design 4.1: 150 KB).
BUDGET_BYTES = 150 * 1024


@dataclass(frozen=True)
class Asset:
    body: bytes
    content_type: str


def _load() -> dict[str, Asset]:
    table: dict[str, Asset] = {}
    if not _DIR.is_dir():  # a broken bundle must not stop the desktop app
        return table
    for path in sorted(_DIR.iterdir()):
        kind = _TYPES.get(path.suffix)
        if kind is None or not path.is_file():
            continue
        url = "/" if path.name == "index.html" else f"/static/{path.name}"
        table[url] = Asset(path.read_bytes(), kind)
    return table


ASSETS: dict[str, Asset] = _load()
