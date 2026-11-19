"""The seams between an item and a render style (Package 4, WP2).

An item never branches on the style itself. It asks ``style_of(item)`` which
style its scene is painted in, and hands the drawing to that style's
``StylePainter`` (looked up in ``STYLE_PAINTERS``); a painter that does not style
a shape returns False and the illustrated code paints as it always did. The
registry is empty until the CAD and Line & Wash painters land (WP4, WP6), so
until then nothing changes.

Scene-wide state (palette, callout layout, canopy clusters) is not built here
or in a paint: ``StylePainter.build_state`` is called by ``ui/canvas/style/state.py``
when the scene's paint revision, style or LOD band moves (§8.9 "never rebuilt
inside a paint").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QPainter, QPen

from open_garden_planner.core.render_style import RenderStyle, normalise_render_style

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QStyleOptionGraphicsItem

    from open_garden_planner.core.object_types import PathFenceStyle

#: Device pixels per paper millimetre at 96 dpi (design 2.1).
SCREEN_DPI = 96.0


class StylePainter(Protocol):
    """One render style's drawing strategy (design 1.2).

    ``paint_shape``, ``paint_path`` and ``paint_canopy`` return True when they drew
    the item and False for one they do not style (the illustrated code then
    paints). ``bounding_pad_cm`` is how far the style's ink reaches beyond the
    item's own bounding rect, at LOD ``band``. ``build_state`` returns the
    scene-wide entries of a ``StyleState`` (empty when the style has none).
    """

    def paint_shape(
        self, item: Any, painter: QPainter, option: QStyleOptionGraphicsItem
    ) -> bool: ...

    def paint_path(
        self, path_paint: Any, painter: QPainter, style: PathFenceStyle
    ) -> bool: ...

    def paint_canopy(self, item: Any, painter: QPainter, spec: Any) -> bool: ...

    def paint_label_decor(self, item: Any, painter: QPainter) -> None: ...

    def bounding_pad_cm(self, item: Any, band: int) -> float: ...

    def build_state(self, scene: Any, band: int) -> dict[str, Any]: ...


#: The painter of each non-illustrated style. Illustrated has none: it is the
#: item's own code.
STYLE_PAINTERS: dict[RenderStyle, StylePainter] = {}


def style_of(item: Any) -> RenderStyle:
    """The render style ``item``'s scene is painted in (illustrated when the
    item is in no scene, or in one without a style)."""
    scene = item.scene()
    return normalise_render_style(getattr(scene, "render_style", None))


def painter_for(style: RenderStyle) -> StylePainter | None:
    """The painter of ``style``; None for illustrated and for a style whose
    painter has not been registered."""
    if style is RenderStyle.ILLUSTRATED:
        return None
    return STYLE_PAINTERS.get(style)


def bounding_pad_cm(item: Any, style: RenderStyle, band: int) -> float:
    """How far ``style``'s ink reaches beyond ``item``'s own bounding rect, in
    plan cm at LOD ``band``. 0 for illustrated, and while no painter is registered."""
    painter = painter_for(style)
    if painter is None:
        return 0.0
    return max(0.0, float(painter.bounding_pad_cm(item, band)))


def styled_bounding_rect(item: Any, rect: QRectF) -> QRectF:
    """``rect`` (an item's own bounding rect) grown by its style's pad.

    ``shape()`` is not grown: hit-testing and selection stay style-independent
    (design 2.4). An item that adopts this calls ``prepareGeometryChange()`` from
    its ``style_geometry_changed()`` hook, which ``CanvasScene.set_render_style``
    calls on a style switch.
    """
    scene = item.scene()
    style = normalise_render_style(getattr(scene, "render_style", None))
    if style is RenderStyle.ILLUSTRATED:
        return rect
    band = int(getattr(scene, "style_band", 0))
    pad = bounding_pad_cm(item, style, band)
    if pad <= 0.0:
        return rect
    return rect.adjusted(-pad, -pad, pad, pad)


def pen_for_painter(
    painter: QPainter, mm: float, color: QColor, *, dpi: float | None = None
) -> QPen:
    """A cosmetic pen ``mm`` paper millimetres wide, in ``color``.

    Line weights are paper units, so they stay the same when the view zooms, as
    in a CAD tool (design 2.1). On screen a weight is ``mm`` at 96 dpi times the
    device pixel ratio, at least 1 device pixel; an export passes its own ``dpi``
    and gets ``mm * dpi / 25.4`` device pixels. The pen is cosmetic, so its width
    is in device pixels whatever the painter's scale.
    """
    ratio = 1.0
    device = painter.device()
    if device is not None:
        ratio = max(1.0, float(device.devicePixelRatio()))
    per_mm = (SCREEN_DPI * ratio if dpi is None else float(dpi)) / 25.4
    pen = QPen(color)
    pen.setWidthF(max(1.0, mm * per_mm))
    pen.setCosmetic(True)
    return pen
