"""CAD leader-line callouts (Package 4, WP5; design 2.2 "Leader-line callouts").

A callout names a plant (its common name, else the object's name, else the type)
with a leader running to an anchor dot on the plant's centre. It is generated
view decoration: **never a ``CalloutItem``, never saved, never selectable**. One
scene-level ``StyleCalloutLayer`` draws the leaders and owns the text children.

Two halves:

* ``compute_callouts`` is the layout: a deterministic greedy placement, built
  into ``StyleState.data["callouts"]`` by ``CadPainter.build_state`` when the
  scene's paint revision, style or LOD band moves, never inside a paint. Slots
  are the 8 compass directions at ``SLOT_MM`` of paper from the anchor, tried
  from a start slot taken from the crc32 seed of the item id; a slot is rejected
  when its box meets an already placed box, a building outline or another
  plant's anchor dot, and the next ring (``RING_STEP_MM`` further out) is tried
  before a plant is left without a callout. No ``hash()``, no random, no time.
* ``sync_callout_layer`` puts the layout on the scene: the layer exists only while
  the scene paints in CAD with ``cad_callouts`` on, and it is added and removed
  below ``CanvasScene.addItem`` so it is no plan change (no revision bump, no
  undo, no .ogp byte).

Text is a ``QGraphicsSimpleTextItem`` child with ``ItemIgnoresTransformations``
(constant pixel size), so ``retarget_constant_size_text`` sizes it for an export
like any object label. The layout reserves boxes from a paper-mm text model, not
from font metrics, so it does not depend on the installed font.

Coordinates: the plan is painted Y-up, so a box is ``QRectF(left, bottom, w, h)``
with ``bottom`` the smaller scene y, and the text child sits at ``(left, top)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from PyQt6.QtCore import QCoreApplication, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QStyleOptionGraphicsItem,
    QWidget,
)

from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.core.render_style_roles import StyleRole, role_for_type_name
from open_garden_planner.ui.canvas.style.base import (
    OUTPUT_DPI,
    SCREEN_DPI,
    pen_for_painter,
    px_per_mm,
)
from open_garden_planner.ui.canvas.style.geometry import style_seed, unit_hash

if TYPE_CHECKING:
    from open_garden_planner.ui.canvas.style.state import StyleState

#: Leader slot distance from the anchor, paper mm (design: 12 mm).
SLOT_MM: Final = 12.0
#: Further rings tried when all 8 slots of a ring collide.
RING_STEP_MM: Final = 6.0
MAX_RINGS: Final = 3
#: Horizontal landing between the leader's end and the text, paper mm.
LANDING_MM: Final = 2.0
#: The text box model (paper mm): a generous glyph advance and line height, so a
#: box over-reserves for any common font and for an export's retargeted size.
CHAR_MM: Final = 2.1
LINE_MM: Final = 3.8
BOX_PAD_MM: Final = 1.0
#: Anchor dot diameter and leader weight, paper mm.
DOT_MM: Final = 1.6
LEADER_MM: Final = 0.18
#: Longest label kept, characters (the rest is an ellipsis).
MAX_CHARS: Final = 28
#: Constant-size text font, points (an export retargets it).
TEXT_POINT_SIZE: Final = 8
#: Above plants and dimension lines, below the overlays.
LAYER_Z: Final = 9_000.0

INK: Final = QColor("#1b1b1b")

_PLANT_ROLES: Final = frozenset(
    {StyleRole.PLANT_TREE, StyleRole.PLANT_SHRUB, StyleRole.PLANT_GROUND}
)
_SQRT_HALF: Final = 0.7071067811865476
#: The 8 compass slots, counter-clockwise from east (y up).
_SLOTS: Final = (
    (1.0, 0.0), (_SQRT_HALF, _SQRT_HALF), (0.0, 1.0), (-_SQRT_HALF, _SQRT_HALF),
    (-1.0, 0.0), (-_SQRT_HALF, -_SQRT_HALF), (0.0, -1.0), (_SQRT_HALF, -_SQRT_HALF),
)

@dataclass(frozen=True)
class CalloutPlacement:
    """One callout: where it points, its leader and its text box (scene cm)."""

    item_id: str
    text: str
    anchor: QPointF
    #: The leader polyline, from the anchor to the text's edge.
    leader: tuple[QPointF, ...]
    #: The reserved box (``QRectF`` min-corner form, y up) and the text's top-left.
    box: QRectF
    text_pos: QPointF


def _mm_to_cm(mm: float, band: int) -> float:
    dpi = OUTPUT_DPI.get()
    per_mm = (SCREEN_DPI if dpi is None else dpi) / 25.4
    return mm * per_mm / (2.0**band)


def fallback_label(role: StyleRole) -> str:
    """The label of a plant with no common name and no object name."""
    if role is StyleRole.PLANT_TREE:
        return QCoreApplication.translate("StyleCallouts", "Tree")
    if role is StyleRole.PLANT_SHRUB:
        return QCoreApplication.translate("StyleCallouts", "Shrub")
    return QCoreApplication.translate("StyleCallouts", "Plant")


def _label_for(item: Any, role: StyleRole) -> str:
    species = getattr(item, "metadata", {}).get("plant_species")
    text = ""
    if isinstance(species, dict):
        text = str(species.get("common_name") or "").strip()
    if not text:
        text = str(getattr(item, "name", "") or "").strip()
    if not text:
        text = fallback_label(role)
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 1].rstrip() + "…"
    return text


def _box_for(
    anchor: QPointF, slot: tuple[float, float], dist_cm: float, text: str, band: int
) -> tuple[QRectF, tuple[QPointF, ...], QPointF]:
    """The text box, the leader and the text's top-left for one slot."""
    width = _mm_to_cm(CHAR_MM * len(text) + 2 * BOX_PAD_MM, band)
    height = _mm_to_cm(LINE_MM + 2 * BOX_PAD_MM, band)
    landing = _mm_to_cm(LANDING_MM, band)
    end = QPointF(anchor.x() + slot[0] * dist_cm, anchor.y() + slot[1] * dist_cm)
    if slot[0] > 1e-9:
        left = end.x() + landing
        points = (anchor, end, QPointF(left, end.y()))
    elif slot[0] < -1e-9:
        left = end.x() - landing - width
        points = (anchor, end, QPointF(end.x() - landing, end.y()))
    else:
        left = end.x() - width / 2.0
        points = (anchor, end)
    # N/S slots hang the box above/below the leader end; E/W centre it on it.
    if slot[0] == 0.0:
        bottom = end.y() if slot[1] > 0 else end.y() - height
    else:
        bottom = end.y() - height / 2.0
    box = QRectF(left, bottom, width, height)
    pad = _mm_to_cm(BOX_PAD_MM, band)
    text_pos = QPointF(left + pad, bottom + height - pad)
    return box, points, text_pos


def _is_plant(item: Any) -> bool:
    name = getattr(getattr(item, "object_type", None), "name", None)
    return role_for_type_name(name) in _PLANT_ROLES


def compute_callouts(scene: Any, band: int) -> tuple[CalloutPlacement, ...]:
    """The callouts of ``scene`` at LOD ``band``: pure and deterministic.

    Plants are placed in a fixed order (top to bottom, left to right, then id),
    each at the first free slot from its seeded start slot, so the same plan at
    the same band always gives the same layout.
    """
    plants: list[tuple[Any, QPointF, str]] = []
    obstacles: list[QRectF] = []
    for item in scene.items():
        if isinstance(item, (StyleCalloutLayer, _CalloutText)):
            continue
        name = getattr(getattr(item, "object_type", None), "name", None)
        role = role_for_type_name(name)
        if role is StyleRole.BUILDING:
            obstacles.append(item.sceneBoundingRect().normalized())
        elif role in _PLANT_ROLES and item.isVisible():
            c = item.sceneBoundingRect().center()
            plants.append((item, QPointF(c), str(getattr(item, "item_id", ""))))
    plants.sort(key=lambda p: (-round(p[1].y(), 3), round(p[1].x(), 3), p[2]))

    dot = _mm_to_cm(DOT_MM + 2 * BOX_PAD_MM, band)
    dots = [QRectF(a.x() - dot / 2, a.y() - dot / 2, dot, dot) for _, a, _ in plants]
    blocked = obstacles + dots
    placed: list[CalloutPlacement] = []
    for item, anchor, item_id in plants:
        text = _label_for(item, role_for_type_name(item.object_type.name))
        seed = style_seed(item_id, "callout")
        start = int(unit_hash(seed, 0) * len(_SLOTS)) % len(_SLOTS)
        found: CalloutPlacement | None = None
        for ring in range(MAX_RINGS):
            dist = _mm_to_cm(SLOT_MM + ring * RING_STEP_MM, band)
            for k in range(len(_SLOTS)):
                slot = _SLOTS[(start + k) % len(_SLOTS)]
                box, leader, text_pos = _box_for(anchor, slot, dist, text, band)
                if any(box.intersects(o) for o in blocked):
                    continue
                if any(box.intersects(p.box) for p in placed):
                    continue
                found = CalloutPlacement(item_id, text, anchor, leader, box, text_pos)
                break
            if found is not None:
                break
        if found is not None:
            placed.append(found)
    return tuple(placed)


def overlapping_pairs(callouts: tuple[CalloutPlacement, ...]) -> list[tuple[str, str]]:
    """Ids of callout pairs whose boxes meet: empty on any layout this module made."""
    out: list[tuple[str, str]] = []
    for i, a in enumerate(callouts):
        for b in callouts[i + 1:]:
            if a.box.intersects(b.box):
                out.append((a.item_id, b.item_id))
    return out


class _CalloutText(QGraphicsSimpleTextItem):
    """A callout's text: constant pixel size, and invisible to hit-testing."""

    def shape(self) -> QPainterPath:
        return QPainterPath()

    def contains(self, point: QPointF) -> bool:  # noqa: ARG002
        return False


class StyleCalloutLayer(QGraphicsItem):
    """The one scene-level item that draws every callout (design 2.2).

    Not selectable, not movable, empty ``shape()``: it is skipped by hit-testing
    and selection. It is never saved: nothing serialises a scene item that is
    not a plan object, and ``CanvasScene`` adds it below ``addItem``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setZValue(LAYER_Z)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._callouts: tuple[CalloutPlacement, ...] = ()
        self._key: tuple[Any, ...] | None = None
        self._rect = QRectF()
        self._texts: list[_CalloutText] = []

    @property
    def callouts(self) -> tuple[CalloutPlacement, ...]:
        return self._callouts

    @property
    def state_key(self) -> tuple[Any, ...] | None:
        return self._key

    def set_layout(self, callouts: tuple[CalloutPlacement, ...], key: tuple[Any, ...]) -> None:
        """Adopt a layout (called by the scene when a state is built, never a paint)."""
        self.prepareGeometryChange()
        self._callouts = callouts
        self._key = key
        rect = QRectF()
        for c in callouts:
            rect = rect.united(c.box)
            for p in c.leader:
                rect = rect.united(QRectF(p.x(), p.y(), 0.0, 0.0))
        self._rect = rect.adjusted(-50.0, -50.0, 50.0, 50.0) if not rect.isNull() else rect
        while len(self._texts) > len(callouts):
            gone = self._texts.pop()
            gone.setParentItem(None)
            scene = gone.scene()
            if scene is not None:
                scene.removeItem(gone)
        while len(self._texts) < len(callouts):
            text = _CalloutText(self)
            text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            text.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            text.setBrush(INK)
            font = QFont()
            font.setPointSize(TEXT_POINT_SIZE)
            text.setFont(font)
            self._texts.append(text)
        for text, c in zip(self._texts, callouts, strict=True):
            text.setText(c.text)
            text.setPos(c.text_pos)
        self.update()

    def boundingRect(self) -> QRectF:
        return QRectF(self._rect)

    def shape(self) -> QPainterPath:
        return QPainterPath()

    def contains(self, point: QPointF) -> bool:  # noqa: ARG002
        return False

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem | None = None,  # noqa: ARG002
        widget: QWidget | None = None,  # noqa: ARG002
    ) -> None:
        if not self._callouts:
            return
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            lead = pen_for_painter(painter, LEADER_MM, INK)
            lead.setCapStyle(Qt.PenCapStyle.FlatCap)
            lead.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            dot = QPen(INK)
            dot.setCosmetic(True)
            dot.setCapStyle(Qt.PenCapStyle.RoundCap)
            dot.setWidthF(max(2.0, DOT_MM * px_per_mm(painter)))
            for c in self._callouts:
                path = QPainterPath(c.leader[0])
                for p in c.leader[1:]:
                    path.lineTo(p)
                painter.setPen(lead)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
                painter.setPen(dot)
                painter.drawPoint(c.anchor)
        finally:
            painter.restore()


def sync_callout_layer(scene: Any, state: StyleState | None) -> None:
    """Make ``scene``'s callout layer match ``state`` (None: no layer).

    Idempotent for the same state key. Adds and removes the layer with
    ``QGraphicsScene`` directly: it is no plan item, so ``CanvasScene.addItem``'s
    bookkeeping (paint revision, shadows, layers) must not run for it.
    """
    layer: StyleCalloutLayer | None = getattr(scene, "_callout_layer", None)
    wanted: tuple[CalloutPlacement, ...] | None = None
    if state is not None and state.style is RenderStyle.CAD:
        found = state.data.get("callouts")
        if found is not None:
            wanted = found
    if wanted is None:
        if layer is not None:
            QGraphicsScene.removeItem(scene, layer)
            scene._callout_layer = None
        return
    if layer is None:
        layer = StyleCalloutLayer()
        QGraphicsScene.addItem(scene, layer)
        scene._callout_layer = layer
    if layer.state_key != state.key:  # type: ignore[union-attr]
        layer.set_layout(wanted, state.key)  # type: ignore[union-attr]
