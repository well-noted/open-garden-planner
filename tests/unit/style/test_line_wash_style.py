"""The Line & Wash painter (Package 4, WP6): paper, washes, ink, massing, labels."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPdfWriter

from open_garden_planner.core.object_types import ObjectType, PathFenceStyle
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.core.render_style_roles import IRRIGATION_LAYER_KEY
from open_garden_planner.models.layer import Layer
from open_garden_planner.services.scene_rendering import render_scene_region, style_background
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.items import CircleItem, PolygonItem, PolylineItem
from open_garden_planner.ui.canvas.style import line_wash as lw
from open_garden_planner.ui.canvas.style.base import STYLE_PAINTERS, styled_label_font

P = QPointF


def _plan() -> CanvasScene:
    scene = CanvasScene(width_cm=2000, height_cm=1400)
    irr = Layer(name="x", permaculture_layer=IRRIGATION_LAYER_KEY, z_order=1)
    scene.set_layers([*scene.layers, irr])
    lay = scene.layers[0].id
    scene.addItem(PolygonItem([P(150, 800), P(650, 800), P(650, 1250), P(150, 1250)],
                              ObjectType.HOUSE, layer_id=lay))
    scene.addItem(PolylineItem([P(700, 750), P(1100, 650)], ObjectType.PATH, layer_id=lay,
                               path_fence_style=PathFenceStyle.PAVED_PATH))
    scene.addItem(PolylineItem([P(300, 150), P(1000, 400)], ObjectType.FENCE, layer_id=irr.id))
    for x, y, r, t in [(400, 450, 150, ObjectType.TREE), (520, 520, 120, ObjectType.TREE),
                       (1500, 400, 80, ObjectType.SHRUB)]:
        scene.addItem(CircleItem(x, y, r, t, layer_id=lay))
    return scene


def _render(scene: CanvasScene, style: RenderStyle) -> QImage:
    scene.set_render_style(style)
    img = QImage(400, 280, QImage.Format.Format_ARGB32)
    img.fill(style_background(style, scene))
    painter = QPainter(img)
    try:
        render_scene_region(scene, painter, QRectF(0, 0, 400, 280),
                            QRectF(0, 0, 2000, 1400), render_style=style)
    finally:
        painter.end()
    return img


def test_registered_with_paper() -> None:
    assert STYLE_PAINTERS[RenderStyle.LINE_AND_WASH].paper_color == QColor("#f4eedf")


def test_grain_tile_is_the_same_in_every_process() -> None:
    code = (
        "import os;os.environ['QT_QPA_PLATFORM']='offscreen';"
        "import hashlib;"
        "from open_garden_planner.ui.canvas.style.line_wash import grain_tile;"
        "t=grain_tile();print(hashlib.md5(bytes(t.constBits().asarray(t.sizeInBytes()))).hexdigest())"
    )
    outs = {
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        ).stdout.strip()
        for seed in (1, 2)
    }
    assert len(outs) == 1 and all(outs)


def test_pigment_is_softer_and_lighter() -> None:
    src = QColor("#228b22")
    out = lw.pigment(src)
    assert out.lightnessF() > src.lightnessF()
    assert out.saturationF() < src.saturationF()


def test_render_is_repeatable_paper_toned_and_inked(qtbot: object) -> None:
    scene = _plan()
    first = _render(scene, RenderStyle.LINE_AND_WASH)
    _render(scene, RenderStyle.ILLUSTRATED)
    second = _render(scene, RenderStyle.LINE_AND_WASH)
    assert first == second
    corner = QColor(first.pixel(2, 2))
    assert corner.red() > corner.blue()  # warm paper
    dark = sum(1 for y in range(0, 280, 2) for x in range(0, 400, 2)
               if QColor(first.pixel(x, y)).lightness() < 90)
    assert dark > 20


def test_overlapping_canopies_of_a_layer_form_one_mass(qtbot: object) -> None:
    scene = _plan()
    scene.set_render_style(RenderStyle.LINE_AND_WASH)
    state = scene.style_state()
    assert state is not None
    canopy = state.data["canopy"]
    assert len(canopy) == 3
    assert len({entry["cluster"] for entry in canopy.values()}) == 2


def test_no_state_is_built_per_item_in_a_paint(qtbot: object) -> None:
    scene = _plan()
    scene.set_render_style(RenderStyle.LINE_AND_WASH)
    builds = scene._style_states.builds
    _render(scene, RenderStyle.LINE_AND_WASH)
    _render(scene, RenderStyle.LINE_AND_WASH)
    assert scene._style_states.builds <= builds + 1


def test_bounding_rect_carries_the_pad(qtbot: object) -> None:
    scene = _plan()
    item = next(i for i in scene.items() if isinstance(i, PolygonItem))
    plain = item.boundingRect()
    scene.set_render_style(RenderStyle.LINE_AND_WASH)
    assert item.boundingRect().width() > plain.width()


def test_a_pdf_painter_is_vector(qtbot: object, tmp_path: Path) -> None:
    writer = QPdfWriter(str(tmp_path / "a.pdf"))
    painter = QPainter(writer)
    try:
        assert not lw.is_raster(painter)
    finally:
        painter.end()


def test_label_hand(qtbot: object) -> None:
    hand = STYLE_PAINTERS[RenderStyle.LINE_AND_WASH].label_font(QFont("Arial", 9))
    assert hand.pointSize() == 9
    assert hand.family() == lw.LABEL_FAMILY or hand.italic()
    item = next(i for i in _plan().items() if isinstance(i, PolygonItem))
    base = QFont("Arial", 9)
    assert styled_label_font(item, base) == base  # illustrated: untouched


def test_style_is_not_a_plan_change(qtbot: object) -> None:
    scene = _plan()
    seen: list[int] = []
    scene.plan_changed.connect(lambda *_: seen.append(1))
    scene.set_render_style(RenderStyle.LINE_AND_WASH)
    assert not seen


def test_default_output_unchanged_after_a_round_trip(qtbot: object) -> None:
    scene = _plan()
    before = _render(scene, RenderStyle.ILLUSTRATED)
    _render(scene, RenderStyle.LINE_AND_WASH)
    assert before == _render(scene, RenderStyle.ILLUSTRATED)
