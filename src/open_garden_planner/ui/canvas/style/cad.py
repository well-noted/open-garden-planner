"""The CAD (architectural drafting) render style painter (Package 4, WP4).

White paper, one ink colour, line weights in paper millimetres, exact vector
hatching. Colour survives only on irrigation (one colour in v1, owner Q2) and a
pale tint on water. It draws:

* shapes (``paint_shape``): buildings 45-degree hatched, roofs 135-degree hatched,
  beds and lawns a light grey, water a tint with two ripple lines, everything else
  an ink outline;
* lines (``paint_path``): paths and fences from the existing walk geometry
  (joints, stones, posts, courses) as fine ink, irrigation dashed in its colour;
* plant canopies (``paint_canopy``): a circle at the item's CURRENT size (owner
  Q3), an inner polygon and a centre mark for a tree, a regular polygon for a
  shrub.

Everything is vector and a pure function of the item, the LOD band and the dpi:
the hatch is computed in scene coordinates, so its phase is anchored to the
scene origin and cannot crawl when a shape moves or the view pans. No clip and no
pattern brush, so SVG and PDF carry the same lines. Callouts are WP5, not here.
No ``hash``, ``random`` or time (an AST test enforces it).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Final

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QStyleOptionGraphicsItem,
)

from open_garden_planner.core.object_types import PATH_FENCE_STYLES, PathFenceStyle
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.core.render_style_roles import (
    StyleRole,
    is_irrigation_layer,
    role_for_item,
    role_for_type_name,
)
from open_garden_planner.ui.canvas.style.base import (
    OUTPUT_DPI,
    SCREEN_DPI,
    STYLE_PAINTERS,
    CanopySpec,
    pen_for_painter,
    px_per_mm,
)
from open_garden_planner.ui.canvas.style.geometry import (
    hatch_segments,
    style_seed,
    unit_hash,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Paper and ink (design 2.2).
PAPER: Final = QColor("#ffffff")
INK: Final = QColor("#1b1b1b")
#: The one irrigation colour (owner Q2: no main / lateral / drip subtype in v1).
IRRIGATION_COLOR: Final = QColor("#1f5fbf")
#: Water: a pale blue, about a 10 % tint of the irrigation blue.
WATER_TINT: Final = QColor("#e3ecf8")
BED_GREY: Final = QColor("#f0f0f0")  # 6 % grey
LAWN_GREY: Final = QColor("#f5f5f5")  # 4 % grey
HATCH_FALLBACK_GREY: Final = QColor("#e6e6e6")  # 10 % grey, when a hatch is over budget

#: Outline weights, paper mm.
W_BUILDING: Final = 0.50
W_STANDARD: Final = 0.35
W_BED: Final = 0.25
W_LIGHT: Final = 0.18
W_FINE: Final = 0.13

#: Hatch families: (angle in degrees, line weight mm, spacing mm).
HATCH_BUILDING: Final = (45.0, 0.18, 2.0)
HATCH_ROOF: Final = (135.0, 0.18, 1.2)
#: Most hatch lines one item may need; above it the item degrades to a flat grey.
HATCH_MAX_SEGMENTS: Final = 3000
#: Longest ring a hatch is attempted for (scanline cost grows with it).
HATCH_MAX_VERTICES: Final = 400

#: The pad of ink beyond an item's own bounding rect, paper mm.
BOUNDING_PAD_MM: Final = 0.8
#: Most walk elements (joints, stones, posts) a line draws, however long it is.
MAX_DETAIL_ELEMENTS: Final = 4000

#: Irrigation dash (paper mm): the design's "unknown subtype" row.
IRRIGATION_DASH_MM: Final = (4.0, 2.0)
IRRIGATION_WEIGHT_MM: Final = 0.25
LAWN_DASH_MM: Final = (3.0, 1.5)

_ELLIPSE_POINTS: Final = 48
_FULL_TURN: Final = 360 * 16


# ------------------------------------------------------------------ helpers
def _band(item: Any) -> int:
    return int(getattr(item.scene(), "style_band", 0))


def _mm_to_cm(mm: float, band: int) -> float:
    """Plan cm covering ``mm`` of paper at LOD ``band`` (dpi aware: an export's
    paper millimetre is ``dpi / 25.4`` device pixels)."""
    dpi = OUTPUT_DPI.get()
    per_mm = (SCREEN_DPI if dpi is None else dpi) / 25.4
    return mm * per_mm / (2.0**band)


def _irrigation_ids(scene: Any) -> frozenset[Any]:
    layers = getattr(scene, "layers", None) or []
    return frozenset(layer.id for layer in layers if is_irrigation_layer(layer))


def _role(item: Any) -> StyleRole:
    return role_for_item(item, _irrigation_ids(item.scene()))


def _ink_pen(painter: QPainter, mm: float, color: QColor = INK) -> QPen:
    pen = pen_for_painter(painter, mm, color)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    return pen


def _dashed(pen: QPen, painter: QPainter, dash_mm: tuple[float, float]) -> QPen:
    """``pen`` with a dash pattern in paper mm (the pattern is in pen widths)."""
    width = max(pen.widthF(), 1e-6)
    per_mm = px_per_mm(painter)
    pen.setDashPattern([dash_mm[0] * per_mm / width, dash_mm[1] * per_mm / width])
    return pen


def _rect_ring(rect: QRectF) -> list[QPointF]:
    return [rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()]


def _ellipse_ring(rect: QRectF) -> list[QPointF]:
    cx, cy = rect.center().x(), rect.center().y()
    rx, ry = rect.width() / 2.0, rect.height() / 2.0
    return [
        QPointF(cx + rx * math.cos(2.0 * math.pi * i / _ELLIPSE_POINTS),
                cy + ry * math.sin(2.0 * math.pi * i / _ELLIPSE_POINTS))
        for i in range(_ELLIPSE_POINTS)
    ]


def _shape_of(item: Any) -> tuple[QPainterPath, list[QPointF]] | None:
    """The item's outline (path and ring, item coordinates) when it is a plain
    rectangle, whole ellipse or polygon; None for anything else (a pie)."""
    if isinstance(item, QGraphicsRectItem):
        rect = item.rect()
        path = QPainterPath()
        path.addRect(rect)
        return path, _rect_ring(rect)
    if isinstance(item, QGraphicsEllipseItem):
        if item.spanAngle() == 0 or abs(item.spanAngle()) % _FULL_TURN != 0:
            return None
        path = QPainterPath()
        path.addEllipse(item.rect())
        return path, _ellipse_ring(item.rect())
    if isinstance(item, QGraphicsPolygonItem):
        polygon = item.polygon()
        ring = [polygon.at(i) for i in range(polygon.count())]
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        path = QPainterPath()
        path.addPolygon(QPolygonF(ring))
        path.closeSubpath()
        return path, ring
    return None


def _hatch_for(
    item: Any, ring: Sequence[QPointF], family: tuple[float, float, float], band: int
) -> list[tuple[QPointF, QPointF]] | None:
    """The item's hatch segments in item coordinates, memoised on the item.

    Computed in scene coordinates (phase anchored to the scene origin) and mapped
    back. None when the item is over budget: the caller degrades to a flat grey.
    The memo key is the ring in scene coordinates, the family, the band and the
    dpi, so a moved, resized or re-zoomed item recomputes and an unchanged one
    (a repaint, a pan) does not.
    """
    if len(ring) > HATCH_MAX_VERTICES:
        return None
    angle, _weight, spacing_mm = family
    spacing_cm = _mm_to_cm(spacing_mm, band)
    scene_ring = [item.mapToScene(p) for p in ring]
    key = (
        tuple((round(p.x(), 3), round(p.y(), 3)) for p in scene_ring),
        angle, round(spacing_cm, 6),
    )
    cached = getattr(item, "_cad_hatch_cache", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    found = hatch_segments([scene_ring], angle, spacing_cm, max_segments=HATCH_MAX_SEGMENTS)
    segments = (
        None if found is None
        else [(item.mapFromScene(a), item.mapFromScene(b)) for a, b in found]
    )
    item._cad_hatch_cache = (key, segments)
    return segments


def _segments_path(segments: Sequence[tuple[QPointF, QPointF]]) -> QPainterPath:
    path = QPainterPath()
    for a, b in segments:
        path.moveTo(a)
        path.lineTo(b)
    return path


def _inset(shape: QPainterPath, width_cm: float) -> QPainterPath:
    """``shape`` shrunk by ``width_cm``: the shape minus its own edge band."""
    stroker = QPainterPathStroker()
    stroker.setWidth(2.0 * width_cm)
    return shape.subtracted(stroker.createStroke(shape))


def _regular_polygon(
    center: QPointF, radius: float, sides: int, rotation: float
) -> QPolygonF:
    return QPolygonF([
        QPointF(center.x() + radius * math.cos(rotation + 2.0 * math.pi * i / sides),
                center.y() + radius * math.sin(rotation + 2.0 * math.pi * i / sides))
        for i in range(sides)
    ])


def _centre_mark(painter: QPainter, center: QPointF, band: int, *, cross: bool) -> None:
    """A 1.0 mm dot (a cosmetic round-cap point) and, for a tree, a 1.5 mm crosshair."""
    dot = pen_for_painter(painter, 1.0, INK)
    dot.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(dot)
    painter.drawPoint(center)
    if cross:
        half = _mm_to_cm(0.75, band)
        painter.setPen(_ink_pen(painter, W_FINE))
        painter.drawLine(QPointF(center.x() - half, center.y()), QPointF(center.x() + half, center.y()))
        painter.drawLine(QPointF(center.x(), center.y() - half), QPointF(center.x(), center.y() + half))


# ------------------------------------------------------------------- painter
class CadPainter:
    """The CAD ``StylePainter`` (see the module docstring)."""

    #: The colour outputs and the canvas are filled with under the drawing.
    paper_color: QColor = PAPER

    # -- shapes -------------------------------------------------------------
    def paint_shape(
        self, item: Any, painter: QPainter,
        option: QStyleOptionGraphicsItem,  # noqa: ARG002
    ) -> bool:
        geometry = _shape_of(item)
        if geometry is None:
            return False
        role = _role(item)
        if role is StyleRole.ANNOTATION:
            return False
        path, ring = geometry
        band = _band(item)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            if role is StyleRole.IRRIGATION:
                self._irrigation_shape(painter, path)
            elif role in (StyleRole.BUILDING, StyleRole.ROOF):
                self._hatched(item, painter, path, ring, band, role)
            elif role is StyleRole.WATER:
                self._water(painter, path, band)
            elif role is StyleRole.LAWN:
                painter.setBrush(LAWN_GREY)
                painter.setPen(_dashed(_ink_pen(painter, W_LIGHT), painter, LAWN_DASH_MM))
                painter.drawPath(path)
            elif role is StyleRole.BED:
                painter.setBrush(BED_GREY)
                painter.setPen(_ink_pen(painter, W_BED))
                painter.drawPath(path)
            else:  # hardscape, fence/wall polygons, plants as shapes, furniture, ...
                painter.setBrush(PAPER)
                weight = W_STANDARD if role in (
                    StyleRole.HARDSCAPE, StyleRole.PATH, StyleRole.FENCE_WALL) else W_BED
                painter.setPen(_ink_pen(painter, weight))
                painter.drawPath(path)
        finally:
            painter.restore()
        return True

    def _irrigation_shape(self, painter: QPainter, path: QPainterPath) -> None:
        pen = _dashed(_ink_pen(painter, IRRIGATION_WEIGHT_MM, IRRIGATION_COLOR),
                      painter, IRRIGATION_DASH_MM)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(pen)
        painter.drawPath(path)

    def _hatched(
        self, item: Any, painter: QPainter, path: QPainterPath,
        ring: Sequence[QPointF], band: int, role: StyleRole,
    ) -> None:
        family = HATCH_BUILDING if role is StyleRole.BUILDING else HATCH_ROOF
        segments = _hatch_for(item, ring, family, band)
        painter.setBrush(PAPER if segments is not None else HATCH_FALLBACK_GREY)
        painter.setPen(_ink_pen(painter, W_BUILDING if role is StyleRole.BUILDING else W_STANDARD))
        painter.drawPath(path)
        if segments:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(_ink_pen(painter, family[1]))
            painter.drawPath(_segments_path(segments))

    def _water(self, painter: QPainter, path: QPainterPath, band: int) -> None:
        painter.setBrush(WATER_TINT)
        painter.setPen(_ink_pen(painter, W_STANDARD))
        painter.drawPath(path)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_ink_pen(painter, W_FINE))
        for step_mm in (1.2, 2.4):
            ripple = _inset(path, _mm_to_cm(step_mm, band))
            if not ripple.isEmpty():
                painter.drawPath(ripple)

    # -- lines --------------------------------------------------------------
    def paint_path(
        self, path_paint: Any, painter: QPainter, style: PathFenceStyle
    ) -> bool:
        owner = path_paint.owner
        if owner is None:  # the 3D ground bake: not a canvas paint
            return False
        band = _band(owner)
        role = _role(owner)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if role is StyleRole.IRRIGATION:
                painter.setPen(_dashed(
                    _ink_pen(painter, IRRIGATION_WEIGHT_MM, IRRIGATION_COLOR),
                    painter, IRRIGATION_DASH_MM))
                painter.drawPath(path_paint.path())
            elif style is PathFenceStyle.NONE:
                painter.setPen(_ink_pen(painter, W_STANDARD))
                painter.drawPath(path_paint.path())
            else:
                self._styled_line(path_paint, painter, style, band)
        finally:
            painter.restore()
        return True

    def _ribbon(self, path_paint: Any, painter: QPainter, weight_mm: float, *, fill: bool) -> None:
        width = path_paint.pen().widthF()
        stroker = QPainterPathStroker()
        stroker.setWidth(width)
        stroker.setCapStyle(Qt.PenCapStyle.FlatCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.BevelJoin)
        ribbon = stroker.createStroke(path_paint.path())
        painter.setBrush(PAPER if fill else Qt.BrushStyle.NoBrush)
        painter.setPen(_ink_pen(painter, weight_mm))
        painter.drawPath(ribbon)

    def _elements(self, path_paint: Any, spacing: float) -> list[tuple[int, Any]]:
        """The walk elements, bounded: a line too long for ``MAX_DETAIL_ELEMENTS``
        gets none (the outline alone), never a stall."""
        if spacing <= 0 or path_paint.path().length() / spacing > MAX_DETAIL_ELEMENTS:
            return []
        out: list[tuple[int, Any]] = []
        for index, element in path_paint._walk_indexed(spacing):
            out.append((index, element))
            if len(out) >= MAX_DETAIL_ELEMENTS:
                break
        return out

    def _styled_line(
        self, path_paint: Any, painter: QPainter, style: PathFenceStyle, band: int
    ) -> None:
        width = path_paint.pen().widthF()
        category = PATH_FENCE_STYLES[style].category
        fine = _ink_pen(painter, W_FINE)
        if category == "path":
            self._ribbon(path_paint, painter, W_STANDARD, fill=True)
            painter.setPen(fine)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            half = width / 2.0
            if style is PathFenceStyle.PAVED_PATH:
                for _i, (pt, _dx, _dy, nx, ny) in self._elements(path_paint, width * 0.8):
                    painter.drawLine(QPointF(pt.x() + nx * half, pt.y() + ny * half),
                                     QPointF(pt.x() - nx * half, pt.y() - ny * half))
            elif style is PathFenceStyle.WOODEN_BOARDWALK:
                for _i, (pt, _dx, _dy, nx, ny) in self._elements(path_paint, width * 0.25):
                    painter.drawLine(QPointF(pt.x() + nx * half, pt.y() + ny * half),
                                     QPointF(pt.x() - nx * half, pt.y() - ny * half))
            elif style is PathFenceStyle.STEPPING_STONES:
                stone_w, stone_h = width * 0.8, width * 0.6
                for _i, (pt, dx, dy, _nx, _ny) in self._elements(path_paint, width * 1.8):
                    painter.save()
                    painter.translate(pt.x(), pt.y())
                    painter.rotate(math.degrees(math.atan2(dy, dx)))
                    painter.drawRoundedRect(
                        QRectF(-stone_w / 2, -stone_h / 2, stone_w, stone_h), 2.0, 2.0)
                    painter.restore()
            else:  # gravel and dirt: sparse stipple, positions from the seeded walk
                dot = pen_for_painter(painter, W_LIGHT, INK)
                dot.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(dot)
                seed = style_seed(str(style.name), "stipple")
                for i, (pt, _dx, _dy, nx, ny) in self._elements(path_paint, width * 0.35):
                    across = (unit_hash(seed, i * 2) - 0.5) * width * 0.8
                    if unit_hash(seed, i * 2 + 1) < 0.5:
                        painter.drawPoint(QPointF(pt.x() + nx * across, pt.y() + ny * across))
            return
        # fences and walls
        if style is PathFenceStyle.STONE_WALL:
            self._ribbon(path_paint, painter, W_STANDARD, fill=True)
            painter.setPen(fine)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            half = width / 2.0
            for i, (pt, _dx, _dy, nx, ny) in self._elements(path_paint, width * 0.6):
                reach = half * (0.55 if i % 2 else 1.0)
                painter.drawLine(QPointF(pt.x() + nx * half, pt.y() + ny * half),
                                 QPointF(pt.x() + nx * (half - reach), pt.y() + ny * (half - reach)))
            return
        if style is PathFenceStyle.HEDGE_FENCE:
            self._ribbon(path_paint, painter, W_BED, fill=False)
            return
        weight = W_BED if style is PathFenceStyle.CHAIN_LINK else W_STANDARD
        painter.setPen(_ink_pen(painter, weight))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path_paint.path())
        post = max(width * 0.6, _mm_to_cm(1.2, band))
        spacing = max(width * (3.0 if style is not PathFenceStyle.CHAIN_LINK else 4.0),
                      _mm_to_cm(8.0, band))
        painter.setBrush(PAPER)
        painter.setPen(_ink_pen(painter, W_LIGHT))
        for _i, (pt, dx, dy, _nx, _ny) in self._elements(path_paint, spacing):
            painter.save()
            painter.translate(pt.x(), pt.y())
            painter.rotate(math.degrees(math.atan2(dy, dx)))
            painter.drawRect(QRectF(-post / 2, -post / 2, post, post))
            painter.restore()

    # -- canopies -----------------------------------------------------------
    def paint_canopy(self, item: Any, painter: QPainter, spec: Any) -> bool:
        if not isinstance(spec, CanopySpec) or spec.radius_cm <= 0:
            return False
        band = _band(item)
        role = role_for_type_name(getattr(getattr(item, "object_type", None), "name", None))
        c, r = spec.center, spec.radius_cm
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if role is StyleRole.PLANT_TREE:
                painter.setPen(_ink_pen(painter, W_BED))
                painter.drawEllipse(c, r, r)
                painter.setPen(_ink_pen(painter, W_FINE))
                painter.drawPolygon(_regular_polygon(c, r * 0.85, 16, 0.0))
                _centre_mark(painter, c, band, cross=True)
            elif role is StyleRole.PLANT_SHRUB:
                painter.setPen(_ink_pen(painter, W_BED))
                turn = unit_hash(style_seed(spec.item_id, "shrub"), 0) * 2.0 * math.pi / 9.0
                painter.drawPolygon(_regular_polygon(c, r, 9, turn))
                _centre_mark(painter, c, band, cross=False)
            else:
                # Ground cover and anything else drawn as a plant: no canopy, only
                # a faint circle so the plant stays findable (recorded assumption).
                painter.setPen(_dashed(_ink_pen(painter, W_FINE), painter, LAWN_DASH_MM))
                painter.drawEllipse(c, r, r)
                _centre_mark(painter, c, band, cross=False)
        finally:
            painter.restore()
        return True

    # -- the rest of the protocol -------------------------------------------
    def paint_label_decor(self, item: Any, painter: QPainter) -> None:  # noqa: ARG002
        return None

    def bounding_pad_cm(self, item: Any, band: int) -> float:  # noqa: ARG002
        return _mm_to_cm(BOUNDING_PAD_MM, band)

    def build_state(self, scene: Any, band: int) -> dict[str, Any]:  # noqa: ARG002
        return {"irrigation_layer_ids": _irrigation_ids(scene)}


def register() -> None:
    """Put the CAD painter in ``STYLE_PAINTERS`` (idempotent)."""
    STYLE_PAINTERS[RenderStyle.CAD] = CadPainter()


register()
