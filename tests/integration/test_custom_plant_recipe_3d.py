"""Custom 3D plant recipes end to end: command, snapshot, forge worker, files, MCP tools."""

from __future__ import annotations

import asyncio
import datetime
import json
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.object_types import ObjectType
from open_garden_planner.core.phenology import evaluate_phenology
from open_garden_planner.core.plant_forge import ForgeCache
from open_garden_planner.core.scene3d import PARAM_TRAITS3D_OVERRIDE
from open_garden_planner.models.plant_data import PlantSpeciesData
from open_garden_planner.services import plant_library as plant_library_module
from open_garden_planner.ui.canvas.items import CircleItem, RectangleItem
from open_garden_planner.ui.view3d.forge_worker import ForgeWorker
from open_garden_planner.ui.view3d.snapshot import snapshot_records
from tests.agent_api_helpers import start_server

TOKEN = "recipe-transport-token-abcdef123456"
OAK = "Quercus robur"
V_FORK: dict[str, Any] = {
    "crown": {"shape": "vase", "lobe_bias_az_deg": 225.0, "lobe_bias_strength": 0.25},
    "branching": {"basal_stems": 2, "fork_height_share": 0.18, "fork_spread_deg": 34.0},
}
BUSH: dict[str, Any] = {"archetype": "mound", "branching": {"basal_stems": 3}}


@pytest.fixture()
def app(qtbot: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    get_settings().show_welcome_on_startup = False
    monkeypatch.setattr(
        GardenPlannerApp, "_confirm_discard_changes", lambda _self: True, raising=False
    )
    # An isolated custom plant library: never the user's real one.
    library = plant_library_module.PlantLibrary.__new__(plant_library_module.PlantLibrary)
    library._plants = {}  # type: ignore[attr-defined]
    library._library_path = tmp_path / "custom_plants.json"  # type: ignore[attr-defined]
    monkeypatch.setattr(plant_library_module, "_library_instance", library)
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    yield win
    win._stop_agent_api()


def _tree(win: Any, species: str | None = OAK) -> Any:
    item = CircleItem(300, 300, 150, object_type=ObjectType.TREE)
    win.canvas_scene.addItem(item)
    if species is not None:
        win._do_agent_set_species(str(item.item_id), species, True)
    return item


def _record(win: Any, item: Any) -> Any:
    return snapshot_records(win.canvas_scene)[str(item.item_id)]


def _depth(win: Any) -> int:
    return int(win.canvas_view.command_manager.undo_depth)


class _Sink:
    """The slice of ``EngineSink`` the forge worker drives."""

    def __init__(self) -> None:
        self.geometry: dict[str, tuple[Any, ...]] = {}
        self.replaced = 0

    def begin(self) -> None: ...
    def commit(self) -> None: ...

    def replace_geometry(self, item_id: str, parts: Any) -> None:
        self.geometry[item_id] = tuple(parts)
        self.replaced += 1


def _bytes(parts: Any) -> bytes:
    return b"".join(np.ascontiguousarray(p.mesh.positions).tobytes() for p in parts)


# --- the command ---------------------------------------------------------------------


def test_setting_a_recipe_is_one_undo_step_that_the_snapshot_sees(app: Any) -> None:
    item = _tree(app)
    before_record = _record(app, item)
    assert PARAM_TRAITS3D_OVERRIDE not in before_record.params
    depth = _depth(app)

    result = app._do_agent_set_plant_recipe(str(item.item_id), V_FORK, "instance")

    assert _depth(app) == depth + 1
    assert result["action"] == "set_plant_recipe" and result["undo_description"]
    assert item.metadata["plant_instance"]["traits3d_override"] == V_FORK
    record = _record(app, item)
    assert json.loads(record.params[PARAM_TRAITS3D_OVERRIDE]) == V_FORK
    assert record.geometry_sig != before_record.geometry_sig  # the plant must be re-forged
    assert record.same_ground_geometry(before_record)  # ... but no ground tile is repainted

    app.canvas_view.command_manager.undo()
    assert "traits3d_override" not in item.metadata.get("plant_instance", {})
    assert _record(app, item).geometry_sig == before_record.geometry_sig
    app.canvas_view.command_manager.redo()
    assert item.metadata["plant_instance"]["traits3d_override"] == V_FORK


def test_clearing_and_noops(app: Any) -> None:
    item = _tree(app)
    pid = str(item.item_id)
    with pytest.raises(ValueError, match="nothing to change"):
        app._do_agent_set_plant_recipe(pid, None, "instance")  # nothing to clear
    app._do_agent_set_plant_recipe(pid, V_FORK, "instance")
    depth = _depth(app)
    with pytest.raises(ValueError, match="nothing to change"):
        app._do_agent_set_plant_recipe(pid, dict(V_FORK), "instance")
    assert _depth(app) == depth
    app._do_agent_set_plant_recipe(pid, None, "instance")
    assert "traits3d_override" not in item.metadata["plant_instance"]
    assert PARAM_TRAITS3D_OVERRIDE not in _record(app, item).params


@pytest.mark.parametrize(
    "bad",
    [
        {"archetype": "bogus"},
        {"crown": {"droop": float("nan")}},
        {"crown": {"droop": True}},
        {"branching": {"basal_stems": 9}},
        {"unknown": 1},
        "text",
    ],
)
def test_an_invalid_recipe_leaves_the_plan_and_the_undo_stack_alone(app: Any, bad: Any) -> None:
    item = _tree(app)
    depth = _depth(app)
    with pytest.raises(ValueError):
        app._do_agent_set_plant_recipe(str(item.item_id), bad, "instance")
    assert _depth(app) == depth
    assert "traits3d_override" not in (item.metadata.get("plant_instance") or {})


def test_non_plants_locked_layers_and_bad_targets_are_refused(app: Any) -> None:
    item = _tree(app)
    pid = str(item.item_id)
    bed = RectangleItem(0, 0, 200, 100)
    bed.object_type = ObjectType.RAISED_BED
    app.canvas_scene.addItem(bed)
    with pytest.raises(ValueError, match="not a plant"):
        app._do_agent_set_plant_recipe(str(bed.item_id), V_FORK, "instance")
    with pytest.raises(ValueError, match="target"):
        app._do_agent_set_plant_recipe(pid, V_FORK, "everywhere")
    with pytest.raises(ValueError, match="valid object id"):
        app._do_agent_set_plant_recipe("not-a-uuid", V_FORK, "instance")

    layer = app.canvas_scene.layers[0]
    item.layer_id = layer.id
    layer.locked = True
    try:
        with pytest.raises(ValueError, match="locked"):
            app._do_agent_set_plant_recipe(pid, V_FORK, "instance")
    finally:
        layer.locked = False
    assert "traits3d_override" not in (item.metadata.get("plant_instance") or {})


def test_species_target_needs_a_species_and_layers_under_the_specimen(app: Any) -> None:
    bare = _tree(app, species=None)
    with pytest.raises(ValueError, match="species"):
        app._do_agent_set_plant_recipe(str(bare.item_id), V_FORK, "species")

    item = _tree(app)
    pid = str(item.item_id)
    app._do_agent_set_plant_recipe(pid, {"crown": {"droop": 0.6, "shape": "weeping"}}, "species")
    assert item.metadata["plant_species"]["traits3d_override"]["crown"]["droop"] == 0.6
    app._do_agent_set_plant_recipe(pid, {"crown": {"droop": 0.1}, "deciduous": False}, "instance")
    layered = json.loads(_record(app, item).params[PARAM_TRAITS3D_OVERRIDE])
    # the specimen wins field by field; the species still supplies what it did not override
    assert layered == {"crown": {"droop": 0.1, "shape": "weeping"}, "deciduous": False}


def test_library_target_mirrors_into_the_custom_library_in_one_undo_step(app: Any) -> None:
    item = _tree(app)
    pid = str(item.item_id)
    library = plant_library_module.get_plant_library()
    with pytest.raises(ValueError, match="custom plant"):
        app._do_agent_set_plant_recipe(pid, BUSH, "library")  # not in the library yet

    plant = PlantSpeciesData.from_dict(item.metadata["plant_species"])
    plant_id = library.add_plant(plant)
    depth = _depth(app)
    app._do_agent_set_plant_recipe(pid, BUSH, "library")

    assert _depth(app) == depth + 1
    assert item.metadata["plant_species"]["traits3d_override"] == BUSH
    stored = library.get_plant(plant_id)
    assert stored is not None and stored.traits3d_override == BUSH
    saved = json.loads(Path(library._library_path).read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert saved["plants"][plant_id]["traits3d_override"] == BUSH

    app.canvas_view.command_manager.undo()
    assert "traits3d_override" not in item.metadata["plant_species"]
    again = library.get_plant(plant_id)
    assert again is not None and again.traits3d_override is None


def test_undoing_the_library_target_restores_the_librarys_own_prior_recipe(app: Any) -> None:
    item = _tree(app)
    pid = str(item.item_id)
    library = plant_library_module.get_plant_library()
    prior = {"crown": {"droop": 0.7}}
    plant = PlantSpeciesData.from_dict(item.metadata["plant_species"])
    plant.traits3d_override = prior
    plant_id = library.add_plant(plant)

    app._do_agent_set_plant_recipe(pid, BUSH, "library")
    assert library.get_plant(plant_id).traits3d_override == BUSH  # type: ignore[union-attr]
    # The user edits another field of the library entry before undoing.
    edited = library.get_plant(plant_id)
    edited.common_name = "Edited"  # type: ignore[union-attr]
    library.update_plant(plant_id, edited)  # type: ignore[arg-type]

    app.canvas_view.command_manager.undo()
    back = library.get_plant(plant_id)
    assert back is not None and back.traits3d_override == prior
    assert back.common_name == "Edited"  # the user's edit survives the undo
    app.canvas_view.command_manager.redo()
    assert library.get_plant(plant_id).traits3d_override == BUSH  # type: ignore[union-attr]


def test_a_recipe_with_an_unhashable_value_in_a_file_never_breaks_the_snapshot(app: Any) -> None:
    item = _tree(app)
    item.metadata["plant_species"]["traits3d_override"] = {"palette": ["x"]}
    assert PARAM_TRAITS3D_OVERRIDE not in _record(app, item).params


# --- the 3D rebuild ---------------------------------------------------------------------


def test_editing_a_recipe_re_forges_the_plant_and_undo_restores_the_old_mesh(app: Any) -> None:
    item = _tree(app)
    pid = str(item.item_id)
    sink = _Sink()
    worker = ForgeWorker(sink, cache=ForgeCache())

    worker.submit_snapshot(snapshot_records(app.canvas_scene).values())
    assert worker.drain() == [pid]
    original = _bytes(sink.geometry[pid])

    # Same plan again: nothing to do (the cache and applied-state hold).
    worker.submit_snapshot(snapshot_records(app.canvas_scene).values())
    assert worker.pending_count == 0

    app._do_agent_set_plant_recipe(pid, V_FORK, "instance")
    worker.submit_snapshot(snapshot_records(app.canvas_scene).values())
    assert worker.pending_count == 1  # the recipe edit alone queued a rebuild
    assert worker.drain() == [pid]
    forked = _bytes(sink.geometry[pid])
    assert forked != original

    app.canvas_view.command_manager.undo()
    worker.submit_snapshot(snapshot_records(app.canvas_scene).values())
    assert worker.drain() == [pid]
    assert _bytes(sink.geometry[pid]) == original  # back to the bundled oak, from the cache
    assert sink.replaced == 3


def test_a_recipe_can_turn_a_tree_into_a_bush_and_changes_the_season(app: Any) -> None:
    item = _tree(app)
    pid = str(item.item_id)
    winter = datetime.date(2026, 1, 15)
    assert evaluate_phenology(_record(app, item), winter).leaf_density == 0.0  # bare oak
    app._do_agent_set_plant_recipe(pid, {"deciduous": False, **BUSH}, "instance")
    state = evaluate_phenology(_record(app, item), winter)
    assert state.leaf_density == 1.0  # the recipe says evergreen
    sink = _Sink()
    worker = ForgeWorker(sink, cache=None)
    worker.submit_snapshot(snapshot_records(app.canvas_scene).values())
    assert worker.drain() == [pid] and sink.geometry[pid]


def test_a_corrupt_recipe_in_a_file_never_breaks_the_view(app: Any) -> None:
    item = _tree(app)
    pid = str(item.item_id)
    item.metadata.setdefault("plant_instance", {})["traits3d_override"] = {"archetype": "bogus"}
    assert PARAM_TRAITS3D_OVERRIDE not in _record(app, item).params  # dropped on the way in
    item.metadata["plant_instance"]["traits3d_override"] = {"crown": {"droop": float("nan")}}
    assert PARAM_TRAITS3D_OVERRIDE not in _record(app, item).params
    worker = ForgeWorker(_Sink(), cache=None)
    worker.submit_snapshot(snapshot_records(app.canvas_scene).values())
    assert worker.drain() == [pid]


# --- files ------------------------------------------------------------------------------


def test_recipes_survive_a_save_and_load(app: Any, tmp_path: Path) -> None:
    item = _tree(app)
    pid = str(item.item_id)
    app._do_agent_set_plant_recipe(pid, V_FORK, "instance")
    app._do_agent_set_plant_recipe(pid, {"leaf": {"length_cm": 20.0}}, "species")
    before = _record(app, item).params[PARAM_TRAITS3D_OVERRIDE]
    path = tmp_path / "recipes.ogp"
    app._project_manager.save(app.canvas_scene, path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    meta = next(o for o in stored["objects"] if o.get("id") == pid or o.get("item_id") == pid)["metadata"]
    assert meta["plant_instance"]["traits3d_override"] == V_FORK
    assert meta["plant_species"]["traits3d_override"] == {"leaf": {"length_cm": 20.0}}

    app._project_manager.new_project()
    app.canvas_scene.clear()
    app._project_manager.load(app.canvas_scene, path)
    reloaded = next(
        i for i in app.canvas_scene.items()
        if getattr(i, "object_type", None) is ObjectType.TREE
    )
    assert _record(app, reloaded).params[PARAM_TRAITS3D_OVERRIDE] == before


# --- the MCP surface ---------------------------------------------------------------------


def _call(server: Any, qtbot: Any, calls: list[tuple[str, dict[str, Any]]],
          token: str | None = TOKEN) -> list[Any]:
    out: list[Any] = []
    state: dict[str, Any] = {}

    async def body() -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with (
            streamablehttp_client(server.url, headers=headers) as (r, w, _),
            ClientSession(r, w) as session,
        ):
            await session.initialize()
            for name, args in calls:
                out.append(await session.call_tool(name, args))

    def run() -> None:
        try:
            asyncio.run(body())
        except Exception as exc:
            state["error"] = exc
        finally:
            state["done"] = True

    threading.Thread(target=run, name="mcp-recipe-test-client").start()
    qtbot.waitUntil(lambda: state.get("done", False), timeout=20000)
    assert state.get("error") is None, state.get("error")
    return out


def test_recipe_tools_over_the_real_transport(app: Any, qtbot: Any) -> None:
    item = _tree(app)
    pid = str(item.item_id)
    server, _ = start_server(app._build_agent_providers(), write_token=TOKEN, writes_enabled=True)
    try:
        depth = _depth(app)
        catalogue, setter = _call(server, qtbot, [
            ("list_plant_archetypes", {}),
            ("set_plant_recipe", {"item_id": pid, "recipe": V_FORK}),
        ])
        cat = catalogue.structuredContent
        assert len(cat["archetypes"]) == 13 and "mound" in cat["archetypes"]
        assert cat["recipe_schema"]["branching"]["basal_stems"]["max"] == 5
        assert setter.isError is False and setter.structuredContent["action"] == "set_plant_recipe"
        assert item.metadata["plant_instance"]["traits3d_override"] == V_FORK
        assert _depth(app) == depth + 1

        bad = _call(server, qtbot, [
            ("set_plant_recipe", {"item_id": pid, "recipe": {"archetype": "nope"}}),
            ("set_plant_recipe", {"item_id": pid, "recipe": {"crown": {"droop": True}}}),
            ("set_plant_recipe", {"item_id": pid, "recipe": V_FORK, "target": "galaxy"}),
            ("set_plant_recipe", {"item_id": "nope", "recipe": V_FORK}),
        ])
        assert all(c.isError for c in bad)
        assert _depth(app) == depth + 1

        (clear,) = _call(server, qtbot, [("set_plant_recipe", {"item_id": pid, "recipe": None})])
        assert clear.isError is False
        assert "traits3d_override" not in item.metadata["plant_instance"]
    finally:
        server.stop()


def test_set_plant_recipe_needs_the_write_token(app: Any, qtbot: Any) -> None:
    item = _tree(app)
    server, _ = start_server(app._build_agent_providers(), write_token=TOKEN, writes_enabled=True)
    try:
        (denied,) = _call(server, qtbot, [("set_plant_recipe",
                                           {"item_id": str(item.item_id), "recipe": V_FORK})],
                          token="wrong-token")
        assert denied.isError
        assert "traits3d_override" not in (item.metadata.get("plant_instance") or {})
        # the read tool needs no token and is there with writes disabled too
        reader, _ = start_server(app._build_agent_providers(), write_token=None, writes_enabled=False)
        try:
            (listed,) = _call(reader, qtbot, [("list_plant_archetypes", {})], token=None)
            assert listed.isError is False
        finally:
            reader.stop()
    finally:
        server.stop()


# --- review fixes: ambiguity, persistence, undo hygiene, plan-wide species -----------


def test_library_target_refuses_an_unnamed_or_ambiguous_species(app: Any) -> None:
    item = _tree(app)
    pid = str(item.item_id)
    library = plant_library_module.get_plant_library()
    first = PlantSpeciesData.from_dict(item.metadata["plant_species"])
    second = PlantSpeciesData.from_dict(item.metadata["plant_species"])
    second.source_id = ""
    library.add_plant(first)
    library.add_plant(second)
    depth = _depth(app)
    with pytest.raises(ValueError, match="match"):
        app._do_agent_set_plant_recipe(pid, BUSH, "library")
    assert _depth(app) == depth and "traits3d_override" not in item.metadata["plant_species"]

    nameless = _tree(app)
    nameless.metadata["plant_species"] = {"scientific_name": "", "common_name": ""}
    with pytest.raises(ValueError, match="no scientific or common name"):
        app._do_agent_set_plant_recipe(str(nameless.item_id), BUSH, "library")


def test_library_target_refuses_when_the_library_cannot_be_saved(app: Any, tmp_path: Path) -> None:
    item = _tree(app)
    pid = str(item.item_id)
    library = plant_library_module.get_plant_library()
    plant_id = library.add_plant(PlantSpeciesData.from_dict(item.metadata["plant_species"]))
    library._library_path = tmp_path  # a directory: opening it for writing raises OSError
    depth = _depth(app)
    with pytest.raises(ValueError, match="cannot be saved"):
        app._do_agent_set_plant_recipe(pid, BUSH, "library")
    assert _depth(app) == depth
    assert library.get_plant(plant_id).traits3d_override is None  # type: ignore[union-attr]
    library._save()  # other callers still swallow the failure
    with pytest.raises(OSError):
        library.save_checked()


def test_undo_removes_a_plant_instance_dict_the_command_created(app: Any) -> None:
    item = _tree(app)
    item.metadata.pop("plant_instance", None)
    app._do_agent_set_plant_recipe(str(item.item_id), V_FORK, "instance")
    assert item.metadata["plant_instance"]["traits3d_override"] == V_FORK
    app.canvas_view.command_manager.undo()
    assert "plant_instance" not in item.metadata
    app.canvas_view.command_manager.redo()
    assert item.metadata["plant_instance"]["traits3d_override"] == V_FORK

    # a container that already held other data is kept
    other = _tree(app)
    other.metadata["plant_instance"] = {"planted": "2026-05-01"}
    app._do_agent_set_plant_recipe(str(other.item_id), V_FORK, "instance")
    app.canvas_view.command_manager.undo()
    assert other.metadata["plant_instance"] == {"planted": "2026-05-01"}


def test_plan_species_target_reaches_every_plant_of_the_species_in_one_undo_step(
    app: Any,
) -> None:
    a, b = _tree(app), _tree(app)
    other_species = _tree(app, species="Malus domestica")
    c, d = _tree(app), _tree(app)  # same species, but only touched by 'species' below
    depth = _depth(app)
    result = app._do_agent_set_plant_recipe(str(a.item_id), BUSH, "plan_species")
    assert _depth(app) == depth + 1 and result["action"] == "set_plant_recipe"
    for item in (a, b, c, d):
        assert item.metadata["plant_species"]["traits3d_override"] == BUSH
    assert "traits3d_override" not in other_species.metadata["plant_species"]

    # plain 'species' stays this plant's own snapshot
    app._do_agent_set_plant_recipe(str(c.item_id), V_FORK, "species")
    assert d.metadata["plant_species"]["traits3d_override"] == BUSH
    assert c.metadata["plant_species"]["traits3d_override"] == V_FORK

    app.canvas_view.command_manager.undo()  # the single-plant 'species' step
    assert c.metadata["plant_species"]["traits3d_override"] == BUSH
    app.canvas_view.command_manager.undo()  # the whole plan-wide step at once
    for item in (a, b, c, d):
        assert "traits3d_override" not in item.metadata["plant_species"]
