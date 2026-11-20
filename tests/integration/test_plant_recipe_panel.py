"""The Properties panel's 3D recipe section: show, edit (one undo step), reset (FR-RECIPE-06)."""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QComboBox, QDoubleSpinBox, QLabel, QPushButton, QSpinBox

from open_garden_planner.core.object_types import ObjectType
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.canvas_view import CanvasView
from open_garden_planner.ui.canvas.items.circle_item import CircleItem
from open_garden_planner.ui.canvas.items.rectangle_item import RectangleItem
from open_garden_planner.ui.panels import PropertiesPanel


@pytest.fixture()
def canvas(qtbot) -> CanvasView:
    view = CanvasView(CanvasScene(width_cm=5000, height_cm=3000))
    qtbot.addWidget(view)
    return view


@pytest.fixture()
def panel(qtbot, canvas: CanvasView) -> PropertiesPanel:
    p = PropertiesPanel(command_manager=canvas.command_manager)
    qtbot.addWidget(p)
    return p


def _field(panel: PropertiesPanel, label_text: str):
    form = panel._form_layout
    for i in range(form.rowCount()):
        label = form.itemAt(i, form.ItemRole.LabelRole)
        field = form.itemAt(i, form.ItemRole.FieldRole)
        if label is None or field is None:
            continue
        w = label.widget()
        if isinstance(w, QLabel) and label_text in w.text():
            return field.widget()
    return None


def _reset_button(panel: PropertiesPanel) -> QPushButton:
    for b in panel.findChildren(QPushButton):
        if "Reset to species default" in b.text():
            return b
    raise AssertionError("no reset button")


def _tree(canvas: CanvasView) -> CircleItem:
    item = CircleItem(300, 300, 150, object_type=ObjectType.TREE)
    item.plant_species = "Quercus robur"
    canvas.scene().addItem(item)
    return item


def _instance_override(item: CircleItem):
    inst = item.metadata.get("plant_instance")
    return inst.get("traits3d_override") if isinstance(inst, dict) else None


def test_section_shows_the_effective_recipe(canvas, panel) -> None:
    item = _tree(canvas)
    item.metadata["plant_species"] = {
        "common_name": "Oak",
        "traits3d_override": {"branching": {"basal_stems": 3}},
    }
    panel.set_selected_items([item])
    stems = _field(panel, "Basal stems")
    assert isinstance(stems, QSpinBox) and stems.value() == 3  # species layer shown
    assert isinstance(_field(panel, "Growth form"), QComboBox)
    assert isinstance(_field(panel, "Fork height"), QDoubleSpinBox)
    assert not _reset_button(panel).isEnabled()


def test_non_recipe_items_get_no_section(canvas, panel) -> None:
    bed = RectangleItem(100, 100, 400, 300, object_type=ObjectType.RAISED_BED)
    canvas.scene().addItem(bed)
    panel.set_selected_items([bed])
    assert _field(panel, "Basal stems") is None


def test_an_edit_is_one_undo_step_and_undo_restores(canvas, panel) -> None:
    item = _tree(canvas)
    panel.set_selected_items([item])
    manager = canvas.command_manager
    stems = _field(panel, "Basal stems")
    depth = manager.undo_depth
    stems.setValue(3)
    assert _instance_override(item) == {"branching": {"basal_stems": 3}}
    assert manager.undo_depth == depth + 1
    assert _reset_button(panel).isEnabled()

    _field(panel, "Fork height").setValue(20)
    assert _instance_override(item)["branching"] == {
        "basal_stems": 3,
        "fork_height_share": pytest.approx(0.2),
    }
    manager.undo()
    assert _instance_override(item) == {"branching": {"basal_stems": 3}}
    manager.undo()
    assert _instance_override(item) is None
    panel.set_selected_items([item])  # the app refreshes the form after history moves
    assert stems.value() == 1  # the widget follows undo
    assert not _reset_button(panel).isEnabled()


def test_reset_clears_the_specimen_and_keeps_the_species(canvas, panel) -> None:
    item = _tree(canvas)
    item.metadata["plant_species"] = {"traits3d_override": {"branching": {"basal_stems": 2}}}
    panel.set_selected_items([item])
    stems = _field(panel, "Basal stems")
    stems.setValue(5)
    assert _instance_override(item) == {"branching": {"basal_stems": 5}}
    depth = canvas.command_manager.undo_depth
    _reset_button(panel).click()
    assert _instance_override(item) is None
    assert canvas.command_manager.undo_depth == depth + 1
    assert stems.value() == 2  # back to the species recipe
    canvas.command_manager.undo()
    panel.set_selected_items([item])
    assert _instance_override(item) == {"branching": {"basal_stems": 5}}
    assert stems.value() == 5


def test_archetype_and_lean_edits_store_valid_fields(canvas, panel) -> None:
    item = _tree(canvas)
    panel.set_selected_items([item])
    combo = _field(panel, "Growth form")
    target = next(i for i in range(combo.count()) if combo.itemData(i) == "mound")
    combo.setCurrentIndex(target)
    _field(panel, "Canopy lean direction").setValue(90)
    _field(panel, "Canopy lean:").setValue(30)
    override = _instance_override(item)
    assert override["archetype"] == "mound"
    assert override["crown"]["lobe_bias_az_deg"] == pytest.approx(90.0)
    assert override["crown"]["lobe_bias_strength"] == pytest.approx(0.3)


def test_the_edit_moves_the_3d_record_signature(canvas, panel) -> None:
    from open_garden_planner.core.scene3d import PARAM_TRAITS3D_OVERRIDE
    from open_garden_planner.ui.view3d.snapshot import snapshot_records

    item = _tree(canvas)
    panel.set_selected_items([item])
    _field(panel, "Basal stems").setValue(4)
    records = snapshot_records(canvas.scene())
    recipes = [r.params.get(PARAM_TRAITS3D_OVERRIDE) for r in records.values()]
    assert any(isinstance(x, str) and '"basal_stems":4' in x for x in recipes)


def _mean_rgb(canvas: CanvasView, item: CircleItem) -> tuple[float, float, float]:
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter

    image = QImage(200, 200, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    canvas.scene().render(painter, QRectF(0, 0, 200, 200), item.sceneBoundingRect())
    painter.end()
    total = [0.0, 0.0, 0.0]
    count = 0
    for y in range(200):
        for x in range(200):
            c = QColor.fromRgba(image.pixel(x, y))
            if c.alpha() > 200:
                total[0] += c.red()
                total[1] += c.green()
                total[2] += c.blue()
                count += 1
    assert count > 1000
    return (total[0] / count, total[1] / count, total[2] / count)


def test_an_overridden_palette_tints_the_2d_sprite(canvas, panel) -> None:
    from open_garden_planner.core.commands import SetPlantRecipeCommand

    item = _tree(canvas)
    before = _mean_rgb(canvas, item)
    canvas.command_manager.execute(SetPlantRecipeCommand(item, {"palette": "redleaf"}))
    after = _mean_rgb(canvas, item)
    assert after[0] > before[0] + 1.0, (before, after)  # redder
    assert after[1] < before[1] - 1.0, (before, after)  # less green
    canvas.command_manager.undo()
    assert _mean_rgb(canvas, item) == pytest.approx(before)


def test_the_users_fill_colour_wins_over_the_recipe_tint(canvas) -> None:
    from PyQt6.QtGui import QColor

    item = _tree(canvas)
    item.metadata["plant_instance"] = {"traits3d_override": {"palette": "redleaf"}}
    assert item._plant_tint() is not None
    item.fill_color = QColor("#3366cc")
    assert item._plant_tint().name() == "#3366cc"


def test_repeated_paints_reuse_the_tint_and_a_recipe_change_invalidates_it(
    canvas, monkeypatch
) -> None:
    """§8.9.14: paint state is kept, not rebuilt in a paint (review P1a)."""
    from open_garden_planner.core.commands import SetPlantRecipeCommand
    from open_garden_planner.core.plant_art import recipe_edit

    item = _tree(canvas)
    calls = [0]
    real = recipe_edit.recipe_tint_hex

    def counting(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(recipe_edit, "recipe_tint_hex", counting)
    item.metadata["plant_species"] = {"common_name": "Oak"}
    for _ in range(5):
        item._plant_tint()
    assert calls[0] == 1  # five paints, one computation

    canvas.command_manager.execute(SetPlantRecipeCommand(item, {"palette": "redleaf"}))
    first = item._plant_tint()
    assert calls[0] == 2  # the recipe change invalidated it
    for _ in range(3):
        assert item._plant_tint() is first
    assert calls[0] == 2
    canvas.command_manager.undo()
    item._plant_tint()
    assert calls[0] == 3  # and so did the undo

    item.plant_species = "Malus domestica"  # a species change
    item._plant_tint()
    assert calls[0] == 4


def test_the_recipe_handlers_refuse_a_locked_layer_or_group_member(canvas, panel) -> None:
    """The panel mirrors the agent path's locked-layer / group refusals (review P1d)."""
    from open_garden_planner.ui.canvas.items.group_item import GroupItem

    item = _tree(canvas)
    manager = canvas.command_manager
    depth = manager.undo_depth
    layer = canvas.scene().layers[0]
    item.layer_id = layer.id
    item.metadata["plant_instance"] = {"traits3d_override": {"palette": "redleaf"}}
    layer.locked = True
    try:
        panel._on_recipe_field_changed(item, ("branching", "basal_stems"), 3)
        panel._on_recipe_reset(item)
    finally:
        layer.locked = False
    assert manager.undo_depth == depth
    assert item.metadata["plant_instance"]["traits3d_override"] == {"palette": "redleaf"}

    # Unlocked, the same call goes through.
    panel._on_recipe_reset(item)
    assert manager.undo_depth == depth + 1

    member = _tree(canvas)
    group = GroupItem()
    canvas.scene().addItem(group)
    group.addToGroup(member)
    before = manager.undo_depth
    panel._on_recipe_field_changed(member, ("branching", "basal_stems"), 3)
    assert manager.undo_depth == before
