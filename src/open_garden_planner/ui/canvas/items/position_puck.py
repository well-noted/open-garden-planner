"""The phone's position on the plan: accuracy halo, heading cone, fix badge (Field Remote, WP-C).

An overlay, not a plan object: it carries no ``item_id``, no layer and no stacking rank, so
``ProjectManager`` never serialises it, the command stack never sees it, and exports hide it
(``ExportService._hide_overlay_items``). ``CanvasScene.show_position`` owns the one instance.

Drawing: the halo is a circle in scene units (the reported accuracy, so it grows with zoom
like the ground it describes). The dot, the heading cone and the badge are in screen pixels
(``ItemIgnoresTransformations`` on a child), which also keeps the text upright under the
canvas Y-flip. The halo says what the position *claims*; it is the filtered sigma, not a
validated accuracy (design 2.1).
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsObject, QStyleOptionGraphicsItem, QWidget

#: Short badge text per ``fix_mode`` (technical acronyms, not translated).
_BADGE = {
    "standard": "GPS",
    "anchor_calibrated": "CAL",
    "vio_fused": "VIO",
    "rtk_float": "RTK~",
    "rtk_fixed": "RTK",
}
_DOT_RADIUS_PX = 6.0
_CONE_LENGTH_PX = 34.0
_CONE_HALF_ANGLE_DEG = 28.0
_MAX_HALO_CM = 1_000_000.0
_COLOR = QColor(20, 120, 255)
_Z = 9_800


class _PuckMarker(QGraphicsItem):
    """Dot, heading cone and badge in screen pixels (origin = the puck's position)."""

    def __init__(self, parent: QGraphicsItem) -> None:
        super().__init__(parent)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.heading_canvas_deg: float | None = None
        self.badge_text = ""

    def boundingRect(self) -> QRectF:
        reach = _CONE_LENGTH_PX + 4.0
        return QRectF(-reach, -reach, 2 * reach + 120.0, 2 * reach + 22.0)

    def paint(self, painter: QPainter, _option: QStyleOptionGraphicsItem | None, _widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.heading_canvas_deg is not None:
            # Screen y points down; the scene's canvas angle is counter-clockwise from +x.
            theta = math.radians(self.heading_canvas_deg)
            tip = QPointF(math.cos(theta) * _CONE_LENGTH_PX, -math.sin(theta) * _CONE_LENGTH_PX)
            half = math.radians(_CONE_HALF_ANGLE_DEG)
            left = QPointF(math.cos(theta + half) * _CONE_LENGTH_PX * 0.8, -math.sin(theta + half) * _CONE_LENGTH_PX * 0.8)
            right = QPointF(math.cos(theta - half) * _CONE_LENGTH_PX * 0.8, -math.sin(theta - half) * _CONE_LENGTH_PX * 0.8)
            cone = QPainterPath(QPointF(0.0, 0.0))
            cone.lineTo(left)
            cone.lineTo(tip)
            cone.lineTo(right)
            cone.closeSubpath()
            fill = QColor(_COLOR)
            fill.setAlpha(110)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(fill))
            painter.drawPath(cone)
        painter.setPen(QPen(QColor(255, 255, 255), 2.0))
        painter.setBrush(QBrush(_COLOR))
        painter.drawEllipse(QPointF(0.0, 0.0), _DOT_RADIUS_PX, _DOT_RADIUS_PX)
        if self.badge_text:
            font = QFont()
            font.setPixelSize(11)
            font.setBold(True)
            painter.setFont(font)
            width = painter.fontMetrics().horizontalAdvance(self.badge_text) + 10
            box = QRectF(_DOT_RADIUS_PX + 4.0, -_DOT_RADIUS_PX - 14.0, float(width), 16.0)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(255, 255, 255, 220)))
            painter.drawRoundedRect(box, 3.0, 3.0)
            painter.setPen(QPen(QColor(20, 60, 140)))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self.badge_text)


class PositionPuckItem(QGraphicsObject):
    """The overlay for one phone position; moved and updated in place, never re-created."""

    def __init__(self) -> None:
        super().__init__()
        self.setZValue(_Z)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)  # never steals a click from the plan
        self._halo_cm = 0.0
        self._marker = _PuckMarker(self)
        self.fix_mode = "standard"
        self.sigma_m: float | None = None

    @property
    def halo_radius_cm(self) -> float:
        return self._halo_cm

    @property
    def badge_text(self) -> str:
        return self._marker.badge_text

    @property
    def heading_canvas_deg(self) -> float | None:
        return self._marker.heading_canvas_deg

    def update_from(
        self,
        x_cm: float,
        y_cm: float,
        *,
        sigma_m: float | None,
        heading_canvas_deg: float | None,
        fix_mode: str,
    ) -> None:
        """Move the puck and refresh halo, cone and badge. Scene units are centimetres."""
        self.setPos(x_cm, y_cm)
        radius = 0.0 if sigma_m is None or not math.isfinite(sigma_m) else min(max(sigma_m, 0.0) * 100.0, _MAX_HALO_CM)
        if radius != self._halo_cm:
            self.prepareGeometryChange()
            self._halo_cm = radius
        self.fix_mode = fix_mode
        self.sigma_m = sigma_m
        label = _BADGE.get(fix_mode, "GPS")
        if sigma_m is not None and math.isfinite(sigma_m):
            label = f"{label} ±{sigma_m:.1f} m"
        self._marker.badge_text = label
        self._marker.heading_canvas_deg = heading_canvas_deg
        self._marker.update()
        self.update()

    def boundingRect(self) -> QRectF:
        r = self._halo_cm
        return QRectF(-r, -r, 2 * r, 2 * r)

    def paint(self, painter: QPainter, _option: QStyleOptionGraphicsItem | None, _widget: QWidget | None = None) -> None:
        if self._halo_cm <= 0.0:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(_COLOR)
        fill.setAlpha(36)
        edge = QPen(QColor(_COLOR), 0.0)  # cosmetic hairline
        edge.setCosmetic(True)
        painter.setPen(edge)
        painter.setBrush(QBrush(fill))
        painter.drawEllipse(QPointF(0.0, 0.0), self._halo_cm, self._halo_cm)
