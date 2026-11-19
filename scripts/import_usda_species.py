"""Merge USDA taxonomy, duration, growth habit and native status into the species DB.

Build-time tool only. The app never calls USDA at runtime, so PRIVACY.md and the
dependency set are unchanged. Provenance and terms: resources/data/PROVENANCE.md
("USDA sources"); how-to: docs/08-crosscutting-concepts/ section 8.39.

What the sources give (and do not):
  * USDA PLANTS (plants.usda.gov, NRCS): accepted name, synonyms, family, duration,
    growth habit, native status per jurisdiction (L48/AK/HI/PR/VI/CAN/...). Native
    status is NOT per US state; state presence comes from the state checklist.
    No edibility, water, sun, pH, height or hardiness data.
  * ARS GRIN taxonomy (ars-grin.gov): accepted name, family, genus, synonyms. No
    hardiness or bundled-ready edibility fields.

Inputs are LOCAL files you downloaded from the official download pages, or files
fetched with --fetch (https only, allow-listed hosts, rate limited, cached outside
the repo). Raw responses are never committed.

Merge rule: a hand-curated key is never overwritten. Only absent ``family``/``genus``/
``cycle`` are filled, and the import-owned ``usda_*`` keys are (re)written.

    python scripts/import_usda_species.py --plants-csv plants.csv [--state-csv az.csv]
        [--grin-tsv taxonomy.tsv] [--write]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SPECIES_JSON = (
    Path(__file__).resolve().parents[1]
    / "src/open_garden_planner/resources/data/plant_species.json"
)
ALLOWED_HOSTS = frozenset({
    "plants.usda.gov",
    "plants.sc.egov.usda.gov",
    "plantsservices.sc.egov.usda.gov",
    "npgsweb.ars-grin.gov",
    "www.ars-grin.gov",
})
USER_AGENT = "open-garden-planner-data-import"
DEFAULT_DELAY_S = 2.0
IMPORT_OWNED = ("usda_symbol", "usda_duration", "usda_growth_habit",
                "usda_native_status", "usda_state_presence")

# Header spellings seen in the PLANTS and GRIN downloads (case-insensitive).
_PLANTS_HEADERS = {
    "symbol": ("symbol", "accepted symbol"),
    "synonym_symbol": ("synonym symbol",),
    "name": ("scientific name with author", "scientific name"),
    "common": ("common name", "national common name"),
    "family": ("family",),
    "duration": ("duration",),
    "habit": ("growth habit",),
    "native": ("native status",),
}
_GRIN_HEADERS = {
    "name": ("name", "taxon", "species name", "taxonomy_species name"),
    "family": ("family", "family name"),
    "genus": ("genus", "genus name"),
    "accepted": ("accepted name", "accepted_name"),
}
_DURATION_TO_CYCLE = {"annual": "annual", "biennial": "biennial", "perennial": "perennial"}


_RANKS = frozenset({"var.", "subsp.", "ssp.", "f.", "x"})


def normalize_name(name: str) -> str:
    """Lower-case binomial (plus infraspecific rank) without authors or hybrid sign."""
    text = name.replace("×", " x ").replace(" ", " ").strip().lower()
    words = text.split()
    kept: list[str] = []
    for index, word in enumerate(words):
        if index >= 2 and word not in _RANKS and kept[-1] not in _RANKS:
            break
        kept.append("subsp." if word == "ssp." else word)
    return " ".join(kept)


def _columns(header: list[str], wanted: dict[str, tuple[str, ...]]) -> dict[str, int]:
    lowered = [h.strip().lower() for h in header]
    found: dict[str, int] = {}
    for key, spellings in wanted.items():
        for spelling in spellings:
            if spelling in lowered:
                found[key] = lowered.index(spelling)
                break
    return found


def _rows(text: str, delimiter: str | None) -> tuple[list[str], list[list[str]]]:
    text = text.lstrip("﻿")
    if delimiter is None:
        delimiter = "\t" if text.split("\n", 1)[0].count("\t") > text.split("\n", 1)[0].count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        raise ValueError("empty input")
    return rows[0], rows[1:]


def parse_plants(text: str) -> dict[str, dict[str, str]]:
    """Accepted-name records keyed by normalised name; synonyms resolve to them."""
    header, rows = _rows(text, None)
    cols = _columns(header, _PLANTS_HEADERS)
    if "symbol" not in cols or "name" not in cols:
        raise ValueError(f"PLANTS file lacks Symbol/Scientific Name columns: {header}")

    def cell(row: list[str], key: str) -> str:
        i = cols.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    accepted: dict[str, dict[str, str]] = {}
    by_symbol: dict[str, dict[str, str]] = {}
    synonyms: list[tuple[str, str]] = []
    for row in rows:
        if not row:
            continue
        symbol, synonym_symbol = cell(row, "symbol"), cell(row, "synonym_symbol")
        name = normalize_name(cell(row, "name"))
        if not name:
            continue
        if synonym_symbol:
            synonyms.append((name, symbol))
            continue
        record = {"symbol": symbol, "name": name, "common": cell(row, "common"),
                  "family": cell(row, "family"), "duration": cell(row, "duration"),
                  "habit": cell(row, "habit"), "native": cell(row, "native")}
        accepted[name] = record
        by_symbol[symbol] = record
    for name, symbol in synonyms:
        if symbol in by_symbol and name not in accepted:
            accepted[name] = by_symbol[symbol]
    return accepted


def parse_state_checklist(text: str) -> set[str]:
    """Normalised names present in a state checklist (state *presence* only)."""
    return set(parse_plants(text))


def parse_grin(text: str) -> dict[str, dict[str, str]]:
    header, rows = _rows(text, None)
    cols = _columns(header, _GRIN_HEADERS)
    if "name" not in cols:
        raise ValueError(f"GRIN file lacks a name column: {header}")
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        values = {key: (row[i].strip() if i < len(row) else "") for key, i in cols.items()}
        name = normalize_name(values["name"])
        if name:
            out[name] = {"family": values.get("family", ""), "genus": values.get("genus", "")}
    return out


def _first_duration(duration: str) -> str | None:
    for part in re.split(r"[,;]", duration):
        cycle = _DURATION_TO_CYCLE.get(part.strip().lower())
        if cycle:
            return cycle
    return None


def merge_record(
    record: dict[str, Any],
    plants: dict[str, Any] | None,
    grin: dict[str, Any] | None,
    state_names: set[str] | None = None,
    state: str = "AZ",
) -> list[str]:
    """Merge into ``record`` in place; return the keys that changed."""
    changed: list[str] = []

    def put(key: str, value: Any, *, owned: bool = False) -> None:
        if value in (None, "", []):
            return
        if (owned or key not in record) and record.get(key) != value:
            record[key] = value
            changed.append(key)

    if plants:
        put("family", plants["family"])
        cycle = _first_duration(plants["duration"])
        if cycle:
            put("cycle", cycle)
        put("usda_symbol", plants["symbol"], owned=True)
        put("usda_duration", plants["duration"], owned=True)
        put("usda_growth_habit", plants["habit"], owned=True)
        put("usda_native_status", plants["native"], owned=True)
    if grin:
        put("family", grin["family"])
        put("genus", grin["genus"])
    if state_names is not None and normalize_name(record["scientific_name"]) in state_names:
        put("usda_state_presence", [state], owned=True)
    return changed


def merge_all(
    data: dict[str, Any],
    plants: dict[str, Any],
    grin: dict[str, Any],
    state_names: set[str] | None,
) -> tuple[dict[str, list[str]], list[str]]:
    changes: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for record in data["plants"]:
        key = normalize_name(record["scientific_name"])
        p, g = plants.get(key), grin.get(key)
        if not p and not g:
            unmatched.append(record["scientific_name"])
            continue
        changed = merge_record(record, p, g, state_names)
        if changed:
            changes[record["scientific_name"]] = changed
    return changes, unmatched


def dumps(data: dict[str, Any]) -> str:
    """The file's own formatting: 2-space indent, CRLF, trailing newline."""
    return json.dumps(data, indent=2, ensure_ascii=False).replace("\n", "\r\n") + "\r\n"


def fetch_cached(url: str, cache_dir: Path, delay_s: float = DEFAULT_DELAY_S,
                 *, opener: Any = None, sleep: Any = time.sleep) -> str:
    """GET ``url`` once; later runs read the cache. Rate limited, https, allow-listed."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"refusing to fetch {url}: https and an allow-listed USDA host only")
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".txt")
    if target.exists():
        return target.read_text(encoding="utf-8")
    sleep(delay_s)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with (opener or urllib.request.urlopen)(request, timeout=60) as response:
        body = response.read().decode("utf-8", errors="replace")
    target.write_text(body, encoding="utf-8")
    return body


def _load(arg: str | None, url: str | None, cache_dir: Path, delay: float) -> str | None:
    if arg:
        return Path(arg).read_text(encoding="utf-8-sig")
    if url:
        return fetch_cached(url, cache_dir, delay)
    return None


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plants-csv", help="USDA PLANTS checklist/characteristics CSV (local file)")
    ap.add_argument("--plants-url", help="https URL of that file (cached, rate limited)")
    ap.add_argument("--state-csv", help="PLANTS state checklist CSV for presence in --state")
    ap.add_argument("--state", default="AZ")
    ap.add_argument("--grin-tsv", help="GRIN taxonomy export (local file)")
    ap.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "ogp-usda-import"),
                    help="raw-response cache; keep it outside the repo")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY_S)
    ap.add_argument("--species-json", default=str(SPECIES_JSON))
    ap.add_argument("--write", action="store_true", help="write the file (default: dry run)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    cache = Path(args.cache_dir)
    plants_text = _load(args.plants_csv, args.plants_url, cache, args.delay)
    grin_text = _load(args.grin_tsv, None, cache, args.delay)
    state_text = _load(args.state_csv, None, cache, args.delay)
    if plants_text is None and grin_text is None:
        ap.error("give --plants-csv/--plants-url and/or --grin-tsv")
    path = Path(args.species_json)
    data = json.loads(path.read_text(encoding="utf-8"))
    plants = parse_plants(plants_text) if plants_text else {}
    grin = parse_grin(grin_text) if grin_text else {}
    state_names = parse_state_checklist(state_text) if state_text else None
    changes, unmatched = merge_all(data, plants, grin, state_names)
    print(f"matched {len(data['plants']) - len(unmatched)}/{len(data['plants'])}; "
          f"{len(changes)} records changed; {len(unmatched)} unmatched")
    for name, keys in changes.items():
        print(f"  {name}: {', '.join(keys)}")
    if args.write:
        path.write_bytes(dumps(data).encode("utf-8"))
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
