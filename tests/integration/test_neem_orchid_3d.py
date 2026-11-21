"""Neem and orchid tree end to end: plan item -> species snapshot -> recipe -> forged 3D plant."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.object_types import ObjectType
from open_garden_planner.core.phenology import evaluate_phenology
from open_garden_planner.core.plant_art.traits3d import resolve_traits
from open_garden_planner.core.plant_forge import LOD0, ForgeCache, forge_species_plant
from open_garden_planner.core.scene3d import PARAM_TRAITS3D_OVERRIDE
from open_garden_planner.services import plant_library as plant_library_module
from open_garden_planner.ui.canvas.items import CircleItem
from open_garden_planner.ui.view3d.snapshot import snapshot_records


@pytest.fixture()
def app(qtbot: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    get_settings().show_welcome_on_startup = False
    monkeypatch.setattr(
        GardenPlannerApp, "_confirm_discard_changes", lambda _self: True, raising=False
    )
    library = plant_library_module.PlantLibrary.__new__(plant_library_module.PlantLibrary)
    library._plants = {}  # type: ignore[attr-defined]
    library._library_path = tmp_path / "custom_plants.json"  # type: ignore[attr-defined]
    monkeypatch.setattr(plant_library_module, "_library_instance", library)
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    yield win
    win._stop_agent_api()


def _tree(win: Any, species: str, x: float) -> Any:
    item = CircleItem(x, 300, 300, object_type=ObjectType.TREE)
    win.canvas_scene.addItem(item)
    win._do_agent_set_species(str(item.item_id), species, True)
    return item


def _record(win: Any, item: Any) -> Any:
    return snapshot_records(win.canvas_scene)[str(item.item_id)]


@pytest.mark.parametrize(
    ("species", "shape", "form"),
    [("Azadirachta indica", "compound", "panicle"), ("Bauhinia variegata", "bilobed", "cup")],
)
def test_a_placed_tree_carries_its_species_recipe_into_the_forge(
    app: Any, species: str, shape: str, form: str
) -> None:
    item = _tree(app, species, 600.0)
    record = _record(app, item)
    recipe = json.loads(record.params[PARAM_TRAITS3D_OVERRIDE])
    assert recipe["leaf"]["shape"] == shape

    traits = resolve_traits("TREE", species, override=recipe)
    assert traits is not None and traits.leaf.shape == shape and traits.blossom_form == form

    parts = forge_species_plant(
        "TREE", str(record.params.get("species_name") or species), species,
        height_cm=float(record.height_cm), radius_cm=300.0, seed=1, lod=LOD0,
        cache=ForgeCache(), traits_override_json=json.dumps(recipe, sort_keys=True,
                                                            separators=(",", ":")),
    )
    positions = np.concatenate([p.mesh.positions for p in parts])
    assert np.isfinite(positions).all()
    assert positions[:, 2].max() == pytest.approx(float(record.height_cm), rel=0.03)
    assert {"wood", "foliage"} <= {p.name for p in parts}


def test_a_plan_saved_before_the_recipe_existed_still_draws_it(app: Any) -> None:
    item = _tree(app, "Bauhinia variegata", 600.0)
    snapshot = dict(item.metadata["plant_species"])
    snapshot.pop("traits3d_override", None)          # an older plan's species snapshot
    item.metadata["plant_species"] = snapshot
    assert PARAM_TRAITS3D_OVERRIDE in _record(app, item).params

    snapshot["traits3d_override"] = None             # the owner cleared it: that stays cleared
    item.metadata["plant_species"] = snapshot
    assert PARAM_TRAITS3D_OVERRIDE not in _record(app, item).params


def test_the_two_trees_are_in_season_where_the_data_says(app: Any) -> None:
    orchid = _record(app, _tree(app, "Bauhinia variegata", 600.0))
    neem = _record(app, _tree(app, "Azadirachta indica", 1500.0))
    spring = datetime.date(2026, 3, 10)
    summer = datetime.date(2026, 7, 20)
    o_spring = evaluate_phenology(orchid, spring, latitude_deg=33.4)
    o_summer = evaluate_phenology(orchid, summer, latitude_deg=33.4)
    n_spring = evaluate_phenology(neem, spring, latitude_deg=33.4)
    n_winter = evaluate_phenology(neem, datetime.date(2026, 12, 20), latitude_deg=33.4)
    assert o_spring.bloom > 0.5 and o_spring.leaf_density < 0.3 and o_summer.bloom == 0.0
    assert o_summer.leaf_density > 0.9
    assert n_winter.leaf_density > 0.9               # the neem keeps its leaves
    assert n_spring.bloom > 0.5


def _bare_species(item: Any) -> dict:
    snapshot = dict(item.metadata["plant_species"])
    snapshot.pop("traits3d_override", None)          # an older plan's species snapshot
    item.metadata["plant_species"] = snapshot
    return snapshot


def test_snapshot_panel_and_agent_agree_for_a_pre_recipe_neem(app: Any) -> None:
    from open_garden_planner.core.plant_art import recipe_edit as re_
    from open_garden_planner.services.bundled_species_db import lookup_species
    from open_garden_planner.ui.panels import PropertiesPanel

    item = _tree(app, "Azadirachta indica", 600.0)
    _bare_species(item)
    bundled = lookup_species("Azadirachta indica")["traits3d_override"]

    # 3D snapshot
    assert json.loads(_record(app, item).params[PARAM_TRAITS3D_OVERRIDE]) == bundled
    # properties panel: the same recipe goes into the panel's model
    panel = PropertiesPanel(command_manager=app.canvas_view.command_manager)
    inputs = panel._recipe_inputs(item)
    assert inputs is not None and inputs[3] == bundled
    view = re_.effective_recipe(*inputs)
    assert view is not None and view.species_overridden
    # agent: the plant already has the recipe 3D draws, so re-setting it is a no-op
    with pytest.raises(ValueError, match="nothing to change"):
        app._do_agent_set_plant_recipe(str(item.item_id), dict(bundled), "species")


def test_a_present_empty_key_means_no_fallback_everywhere(app: Any) -> None:
    from open_garden_planner.ui.panels import PropertiesPanel

    item = _tree(app, "Azadirachta indica", 600.0)
    snapshot = _bare_species(item)
    for empty in (None, {}):
        snapshot["traits3d_override"] = empty
        item.metadata["plant_species"] = snapshot
        assert PARAM_TRAITS3D_OVERRIDE not in _record(app, item).params
        inputs = PropertiesPanel(command_manager=app.canvas_view.command_manager)._recipe_inputs(item)
        assert inputs is not None and not inputs[3]
