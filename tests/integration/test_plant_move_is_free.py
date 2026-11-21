"""A plant's spacing ring never limits where it can be moved (canvas-edge clamp).

The owner could not drag a plant next to a fence that stands at the plan's edge:
the canvas clamp used ``sceneBoundingRect()``, which includes the spacing ring,
so a plant whose ring crossed the plan edge was shoved back by the ring's radius
(PF-CAN keep-out). The clamp now uses the geometric frame (``scene_frame_rect``).
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest

from open_garden_planner.core.object_types import ObjectType
from open_garden_planner.core.tools import ToolType
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.canvas_view import CanvasView
from open_garden_planner.ui.canvas.items import CircleItem, RectangleItem


@pytest.fixture()
def shown(qtbot):
    scene = CanvasScene(1500, 1200)
    view = CanvasView(scene)
    qtbot.addWidget(view)
    view.resize(900, 640)
    view.show()
    qtbot.waitExposed(view)
    view.set_snap_enabled(False)
    view.set_object_snap_enabled(False)
    view.set_zoom(1)
    view.set_active_tool(ToolType.SELECT)
    return view, scene


def _centre(item) -> QPointF:
    return item.mapToScene(item.rect().center())


def _drag(view, item, target: QPointF) -> None:
    view.centerOn(_centre(item))
    start = view.mapFromScene(_centre(item))
    end = view.mapFromScene(target)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    for i in range(1, 6):
        QTest.mouseMove(view.viewport(), QPoint(start.x() + (end.x() - start.x()) * i // 5, start.y() + (end.y() - start.y()) * i // 5))
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)


@pytest.mark.parametrize("spread", [None, 800])
@pytest.mark.parametrize("agent_placed", [False, True])
def test_drag_plant_against_fence_at_plan_edge(shown, spread, agent_placed):
    view, scene = shown
    fence = RectangleItem(1480, 300, 20, 600, object_type=ObjectType.FENCE)
    scene.addItem(fence)
    plant = CircleItem(1200, 600, 50, object_type=ObjectType.TREE)
    if spread:
        plant.metadata["plant_species"] = {"max_spread_cm": spread}
    if agent_placed:
        plant.metadata["placed_by"] = "agent"
    scene.addItem(plant)
    scene.set_spacing_circles_visible(True)
    plant.setSelected(True)
    target = QPointF(1425.0, 600.0)  # the plant's body touches the fence
    _drag(view, plant, target)
    assert _centre(plant).x() == pytest.approx(target.x(), abs=1.5)
    assert _centre(plant).y() == pytest.approx(target.y(), abs=1.5)
    manager = scene.get_command_manager()
    manager.undo()
    assert _centre(plant).x() == pytest.approx(1200.0, abs=1.5)
    manager.redo()
    assert _centre(plant).x() == pytest.approx(target.x(), abs=1.5)


def test_plant_cannot_leave_the_plan_by_its_body(shown):
    view, scene = shown
    plant = CircleItem(1200, 600, 50, object_type=ObjectType.TREE)
    plant.metadata["plant_species"] = {"max_spread_cm": 800}
    scene.addItem(plant)
    plant.setSelected(True)
    _drag(view, plant, QPointF(1490.0, 600.0))
    assert _centre(plant).x() == pytest.approx(1450.0, abs=1.5)  # body flush to the edge
