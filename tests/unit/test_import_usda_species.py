"""Offline tests for ``scripts/import_usda_species.py`` (fixtures only, no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "import_usda_species.py"

PLANTS_CSV = (
    '"Symbol","Synonym Symbol","Scientific Name with Author","Common Name","Family",'
    '"Duration","Growth Habit","Native Status"\n'
    '"OLEU","","Olea europaea L.","common olive","Oleaceae","Perennial","Tree","L48 (I)"\n'
    '"NEOL","","Nerium oleander L.","oleander","Apocynaceae","Perennial","Shrub","L48 (I)"\n'
    '"CIPA","","Citrus × paradisi Macfad.","grapefruit","Rutaceae","Perennial","Tree","L48 (I)"\n'
    '"BRCAC","","Brassica oleracea L. var. capitata L.","cabbage","Brassicaceae",'
    '"Annual, Biennial","Forb/herb","L48 (I)"\n'
    '"PHFL","","Parkinsonia florida (Benth. ex A. Gray) S. Watson","blue paloverde",'
    '"Fabaceae","Perennial","Tree","L48 (N)"\n'
    '"PHFL","CEFL","Cercidium floridum Benth. ex A. Gray","","","","",""\n'
)
STATE_CSV = (
    '"Symbol","Synonym Symbol","Scientific Name with Author","Common Name","Family"\n'
    '"NEOL","","Nerium oleander L.","oleander","Apocynaceae"\n'
)
GRIN_TSV = "Name\tFamily\tGenus\nTipuana tipu\tFabaceae\tTipuana\n"


@pytest.fixture(scope="module")
def imp() -> ModuleType:
    spec = importlib.util.spec_from_file_location("import_usda_species", _SCRIPT)
    assert spec
    assert spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["import_usda_species"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_normalize_name_strips_authors_and_keeps_rank_and_hybrid(imp: ModuleType) -> None:
    n = imp.normalize_name
    assert n("Olea europaea L.") == "olea europaea"
    assert n("Citrus × paradisi Macfad.") == "citrus x paradisi"
    assert n("Citrus x paradisi") == "citrus x paradisi"
    assert n("Brassica oleracea L. var. capitata L.") == "brassica oleracea"
    assert n("Brassica oleracea var. capitata") == "brassica oleracea var. capitata"
    assert n("Carex alba") == "carex alba"


def test_synonyms_resolve_to_the_accepted_record(imp: ModuleType) -> None:
    plants = imp.parse_plants(PLANTS_CSV)
    assert plants["cercidium floridum"]["symbol"] == "PHFL"
    assert plants["parkinsonia florida"]["native"] == "L48 (N)"


def test_missing_columns_fail_loudly(imp: ModuleType) -> None:
    with pytest.raises(ValueError, match="lacks"):
        imp.parse_plants("a,b\n1,2\n")


def test_merge_never_overwrites_hand_curated_fields(imp: ModuleType) -> None:
    rec = {"scientific_name": "Olea europaea", "family": "Hand", "cycle": "annual",
           "max_height_cm": 1500.0, "genus": "Olea"}
    changed = imp.merge_record(rec, imp.parse_plants(PLANTS_CSV)["olea europaea"], None)
    assert rec["family"] == "Hand"
    assert rec["cycle"] == "annual"
    assert rec["max_height_cm"] == 1500.0
    assert rec["usda_symbol"] == "OLEU"
    assert rec["usda_native_status"] == "L48 (I)"
    assert "family" not in changed
    assert "usda_symbol" in changed


def test_merge_fills_only_absent_taxonomy_and_cycle(imp: ModuleType) -> None:
    rec = {"scientific_name": "Nerium oleander"}
    imp.merge_record(rec, imp.parse_plants(PLANTS_CSV)["nerium oleander"], None)
    assert rec["family"] == "Apocynaceae"
    assert rec["cycle"] == "perennial"


def test_import_owned_keys_refresh_on_rerun(imp: ModuleType) -> None:
    rec = {"scientific_name": "Nerium oleander", "usda_native_status": "stale"}
    imp.merge_record(rec, imp.parse_plants(PLANTS_CSV)["nerium oleander"], None)
    assert rec["usda_native_status"] == "L48 (I)"


def test_state_presence_and_grin(imp: ModuleType) -> None:
    rec = {"scientific_name": "Nerium oleander"}
    imp.merge_record(rec, None, None, imp.parse_state_checklist(STATE_CSV))
    assert rec["usda_state_presence"] == ["AZ"]
    tipu = {"scientific_name": "Tipuana tipu"}
    imp.merge_record(tipu, None, imp.parse_grin(GRIN_TSV)["tipuana tipu"])
    assert tipu["family"] == "Fabaceae"
    assert tipu["genus"] == "Tipuana"


def test_main_dry_run_then_write_is_idempotent(imp: ModuleType, tmp_path: Path) -> None:
    species = tmp_path / "species.json"
    original = {"plants": [{"scientific_name": "Olea europaea", "common_name": "Olive"},
                           {"scientific_name": "Unlisted plant", "common_name": "X"}]}
    species.write_bytes(imp.dumps(original).encode("utf-8"))
    csv_path = tmp_path / "plants.csv"
    csv_path.write_text(PLANTS_CSV, encoding="utf-8")
    base = ["--plants-csv", str(csv_path), "--species-json", str(species)]
    before = species.read_bytes()
    assert imp.main(base) == 0
    assert species.read_bytes() == before  # dry run
    assert imp.main([*base, "--write"]) == 0
    once = species.read_bytes()
    assert imp.main([*base, "--write"]) == 0
    assert species.read_bytes() == once
    data = json.loads(once)
    assert data["plants"][0]["usda_symbol"] == "OLEU"
    assert "usda_symbol" not in data["plants"][1]
    assert b"\r\n" in once


def test_fetch_refuses_other_hosts_and_plain_http(imp: ModuleType, tmp_path: Path) -> None:
    for url in ("https://example.com/x.csv", "http://plants.usda.gov/x.csv"):
        with pytest.raises(ValueError, match="allow-listed"):
            imp.fetch_cached(url, tmp_path)


def test_fetch_is_rate_limited_and_cached(imp: ModuleType, tmp_path: Path) -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"Symbol,Scientific Name\n"

    def opener(request: object, timeout: int) -> _Resp:
        calls.append(request.full_url)  # type: ignore[attr-defined]
        return _Resp()

    url = "https://plants.usda.gov/x.csv"
    first = imp.fetch_cached(url, tmp_path, 3.0, opener=opener, sleep=sleeps.append)
    second = imp.fetch_cached(url, tmp_path, 3.0, opener=opener, sleep=sleeps.append)
    assert first == second
    assert len(calls) == 1
    assert sleeps == [3.0]


def test_species_file_matches_importer_formatting(imp: ModuleType) -> None:
    raw = imp.SPECIES_JSON.read_bytes().decode("utf-8")
    assert imp.dumps(json.loads(raw)) == raw
