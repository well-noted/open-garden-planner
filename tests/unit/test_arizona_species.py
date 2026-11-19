"""The Arizona (lower Sonoran Desert) species added for Mesa gardens."""

import pytest

from open_garden_planner.services.bundled_species_db import (
    get_species_db,
    get_species_entry,
    lookup_species,
)

ARIZONA_SPECIES = {
    "tipuana tipu": "Tipu Tree",
    "bauhinia variegata": "Orchid Tree",
    "nerium oleander": "Oleander",
    "punica granatum": "Pomegranate",
    "phoenix dactylifera": "Date Palm",
    "ziziphus jujuba": "Jujube",
    "eriobotrya japonica": "Loquat",
    "citrus x paradisi": "Grapefruit Tree",
    "citrus x aurantiifolia": "Lime Tree",
    "citrus reticulata": "Mandarin Tree",
    "prosopis velutina": "Velvet Mesquite",
    "opuntia ficus-indica": "Prickly Pear",
    "carnegiea gigantea": "Saguaro",
}
TAG = "arizona_lower_desert"


def _arizona() -> dict:
    return {k: r for k, r in get_species_db().items() if TAG in r.get("regions", [])}


def test_arizona_set_size() -> None:
    assert len(_arizona()) >= 40


@pytest.mark.parametrize("sci", sorted(ARIZONA_SPECIES))
def test_scientific_and_common_name_resolve(sci: str) -> None:
    record = lookup_species(sci)
    assert record is not None
    assert lookup_species(ARIZONA_SPECIES[sci]) is record


@pytest.mark.parametrize(
    ("alias", "sci"),
    [
        ("orchid tree", "Bauhinia variegata"),
        ("Phanera variegata", "Bauhinia variegata"),
        ("tipu", "Tipuana tipu"),
        ("mesquite", "Prosopis velutina"),
        ("grapefruit", "Citrus x paradisi"),
        ("black-eyed pea", "Vigna unguiculata"),
        ("palo verde", "Parkinsonia florida"),
    ],
)
def test_common_name_aliases(alias: str, sci: str) -> None:
    record = lookup_species(alias)
    assert record is not None
    assert record["scientific_name"] == sci


def test_every_arizona_record_is_complete_and_sane() -> None:
    required = (
        "family", "genus", "cycle", "min_height_cm", "max_height_cm", "min_spread_cm",
        "max_spread_cm", "sun_requirement", "water_needs", "hardiness_zone_min",
        "hardiness_zone_max", "ph_min", "ph_max", "edible_parts", "nutrient_demand",
        "n_demand", "p_demand", "k_demand", "permaculture_layer",
    )
    for sci, rec in _arizona().items():
        for key in required:
            assert rec.get(key) not in (None, ""), f"{sci}: {key}"
        assert rec["min_height_cm"] <= rec["max_height_cm"], sci
        assert rec["min_spread_cm"] <= rec["max_spread_cm"], sci
        assert rec["hardiness_zone_min"] <= rec["hardiness_zone_max"], sci
        assert rec["ph_min"] <= rec["ph_max"], sci
        assert rec["edible"] == bool(rec["edible_parts"]), sci
        assert rec["genus"] == rec["scientific_name"].split()[0], sci
        for field in rec.get("uncertain_fields", []):
            assert field in rec, f"{sci}: uncertain field {field} not on record"


def test_toxic_landscape_plants_are_not_edible() -> None:
    for sci in ("nerium oleander", "lantana camara", "caesalpinia pulcherrima"):
        assert get_species_entry(sci)["edible"] is False


def test_desert_vegetables_have_a_calendar() -> None:
    for sci in ("abelmoschus esculentus", "vigna unguiculata", "brassica juncea"):
        rec = get_species_entry(sci)
        assert rec is not None
        assert rec["direct_sow_start"] <= rec["direct_sow_end"]
        assert rec["harvest_start"] <= rec["harvest_end"]
    # Cool-season greens are sown in autumn: well after the spring frost.
    assert get_species_entry("brassica juncea")["direct_sow_start"] >= 26
