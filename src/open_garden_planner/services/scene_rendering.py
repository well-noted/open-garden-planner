"""Shared scene-region renderer used by PNG export and Paper Space viewports.

Both consumers need to:
  - hide selection handles / construction geometry / overlay badges
  - tweak text sizes so labels stay readable in the output
  - paint a region of the source ``QGraphicsScene`` into a target
    rectangle on an arbitrary painter, with the correct Y-flip applied
  - restore everything afterwards even on failure

The export service used to do all of this inline; the paper-space
viewport item needs the same logic but rasterising to a pixmap. This
module is the one place that knows how to do it, so the two callers
stay tiny.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final, Protocol

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsTextItem,
)

from open_garden_planner.core.render_style import RenderStyle

#: What every picture that is stored or shown beside the plan is drawn in: an
#: embedded thumbnail, a Paper Space viewport thumbnail. A thumbnail goes into
#: the file, so it never follows the render style (a view preference would leak
#: into the .ogp bytes; Package 4 design 1.1).
THUMBNAIL_STYLE: Final = RenderStyle.ILLUSTRATED

#: The paper of each style that has a fixed one. Illustrated paints the scene's
#: own (theme-dependent) canvas colour, so it has none here.
_STYLE_PAPER: Final = {
    RenderStyle.CAD: "#ffffff",
    RenderStyle.LINE_AND_WASH: "#f4eedf",
}


def style_background(style: RenderStyle, scene: Any = None) -> QColor:
    """The colour an output of ``style`` is filled with before the scene is
    painted: the style's paper, or for illustrated the scene's ``CANVAS_COLOR``
    (white for a scene without one) — what each caller hard-coded until now."""
    paper = _STYLE_PAPER.get(style)
    if paper is not None:
        return QColor(paper)
    canvas = getattr(scene, "CANVAS_COLOR", None)
    return QColor(canvas) if canvas is not None else QColor("white")


def effective_style(scene: Any, style: RenderStyle | None) -> RenderStyle:
    """``style``, or the scene's own when ``None`` (illustrated for a scene that
    has none)."""
    if style is not None:
        return style
    return getattr(scene, "render_style", RenderStyle.ILLUSTRATED)


def paint_style_paper(
    painter: QPainter,
    target_rect: QRectF,
    source_rect: QRectF,
    style: RenderStyle,
    px_per_cm: float,
) -> None:
    """Paint the paper texture of ``style`` under the scene. No style has one
    yet (the Line & Wash grain is Package 4, WP6): the seam exists so the
    callers are wired once."""


@contextmanager
def styled_scene(
    scene: QGraphicsScene,
    style: RenderStyle | None,
    px_per_cm: float | None = None,
) -> Iterator[None]:
    """Paint ``scene`` in ``style`` for the duration, then restore its own.

    Every ``scene.render`` call site (PNG, SVG, PDF, print, the Agent API) goes
    through this, so the styles of two outputs cannot diverge. ``None`` keeps the
    scene's current style. A scene that has no render style (a plain
    ``QGraphicsScene``) is rendered as it is. ``px_per_cm`` is the target's
    resolution, for the scene-wide state prewarmed for its LOD band.
    """
    setter = getattr(scene, "set_render_style", None)
    if style is None or setter is None:
        yield
        return
    previous = scene.render_style  # type: ignore[attr-defined]
    setter(style)
    try:
        prewarm = getattr(scene, "prewarm_render_style", None)
        if prewarm is not None and px_per_cm is not None:
            prewarm(px_per_cm)
        yield
    finally:
        setter(previous)


class FontCarrier(Protocol):
    """An item whose text size is its font: what an export retargets."""

    def font(self) -> QFont: ...

    def setFont(self, font: QFont) -> None: ...


@contextmanager
def _hidden_overlay_items(scene: QGraphicsScene) -> Iterator[None]:
    """Temporarily hide selection handles, badges and diagnostic decorations.

    One list of what a picture of the plan leaves out, for every export path:
    ``ExportService._hide_overlay_items`` owns it (SVG, PDF report and print
    call it directly), and PNG, the thumbnails and the Agent API's render tool
    reach it through here. Until #786 this function kept a second copy of the
    type list, which had already fallen behind (no lock badge).
    """
    from open_garden_planner.services.export_service import ExportService

    hidden, previously_selected = ExportService._hide_overlay_items(scene)
    try:
        yield
    finally:
        ExportService._restore_overlay_items(hidden, previously_selected)


@contextmanager
def _hidden_construction_items(scene: QGraphicsScene) -> Iterator[None]:
    """Temporarily hide construction geometry (helper lines / circles)."""
    from open_garden_planner.ui.canvas.items.construction_item import (
        ConstructionCircleItem,
        ConstructionLineItem,
    )

    hidden: list[object] = []
    for item in scene.items():
        if (
            isinstance(item, (ConstructionLineItem, ConstructionCircleItem))
            and item.isVisible()
        ):
            item.setVisible(False)
            hidden.append(item)
    try:
        yield
    finally:
        for item in hidden:
            item.setVisible(True)


def draws_at_constant_pixel_size(item: QGraphicsItem) -> bool:
    """True when *item* is drawn in device pixels, whatever the scale of the plan.

    An item with ``ItemIgnoresTransformations`` — an object label, a dimension
    text, a badge — and every child of one keep their pixel size under any zoom
    and in any ``scene.render()``. A text note or a callout does not: its font
    is in plan centimetres and it scales with the plan like any other shape.
    """
    flag = QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
    current: QGraphicsItem | None = item
    while current is not None:
        if current.flags() & flag:
            return True
        current = current.parentItem()
    return False


def retarget_constant_size_text(
    scene: QGraphicsScene, point_size: int
) -> list[tuple[FontCarrier, QFont]]:
    """Give every constant-pixel-size text of *scene* the font size *point_size*.

    Only text that ``draws_at_constant_pixel_size``: outside a view nothing
    scales it, so its point size has to be chosen for the output. Text the user
    sized in plan units is left alone — forcing one point size on it made a
    note four times too large on one plan and unreadable on another (#789).

    Qt's two text items, and a bed's succession badge: it is content of the
    plan drawn at a constant pixel size, and it is sized by its font alone, so
    it gets the object labels' size in the output (#836).

    Returns ``(item, original font)`` pairs for ``restore_text_fonts``.
    """
    from open_garden_planner.ui.canvas.items.garden_item import SuccessionBadgeItem

    carriers = (QGraphicsSimpleTextItem, QGraphicsTextItem, SuccessionBadgeItem)
    saved: list[tuple[FontCarrier, QFont]] = []
    for item in scene.items():
        if not isinstance(item, carriers):
            continue
        if not draws_at_constant_pixel_size(item):
            continue
        saved.append((item, QFont(item.font())))
        font = QFont(item.font())
        font.setPointSize(max(4, point_size))
        item.setFont(font)
    return saved


def restore_text_fonts(saved: list[tuple[FontCarrier, QFont]]) -> None:
    """Undo ``retarget_constant_size_text``."""
    for item, original in saved:
        item.setFont(original)


@contextmanager
def _scaled_text(scene: QGraphicsScene, point_size: int) -> Iterator[None]:
    """Temporarily resize the constant-pixel-size text items to ``point_size``.

    Labels in the source scene are set up for screen viewing; in an export the
    same pixel size would dominate a small picture and vanish in a large one.
    Callers pass an explicit point size so the same helper works at any scale.
    """
    saved = retarget_constant_size_text(scene, point_size)
    try:
        yield
    finally:
        restore_text_fonts(saved)


def compute_export_font_size(scale: float, dpi: float) -> int:
    """Return the point size text items should use when rendered for export.

    For 1:100 scale at 150 DPI, 10pt is the reference. Smaller scale
    (more zoomed out) needs slightly larger fonts so labels stay
    readable. Used by ``ExportService.export_to_png`` so all rendering
    bookkeeping goes through ``render_scene_region``.
    """
    import math

    reference_scale = 0.01  # 1:100
    reference_font_size = 10
    reference_dpi = 150

    scale_ratio = scale / reference_scale if reference_scale > 0 else 1.0
    base = reference_font_size * math.sqrt(max(scale_ratio, 1e-9))
    base = max(6.0, min(14.0, base))
    target = int(base * (dpi / reference_dpi))
    return max(4, target)


def render_scene_region(
    scene: QGraphicsScene,
    painter: QPainter,
    target_rect: QRectF,
    source_rect: QRectF,
    *,
    hide_overlays: bool = True,
    hide_construction: bool = True,
    text_point_size: int | None = None,
    y_flip: bool = True,
    render_style: RenderStyle | None = None,
) -> None:
    """Render ``source_rect`` of ``scene`` into ``target_rect`` on ``painter``.

    The source scene's Y axis points down in Qt coordinates but the
    application paints it Y-up; ``y_flip=True`` applies the standard
    pre-flip used by all visual outputs (PNG, PDF, viewport thumbnails).

    Args:
        scene: Source ``QGraphicsScene``.
        painter: Active painter to draw into.
        target_rect: Destination rectangle in painter coords.
        source_rect: Source rectangle in scene coords (the area to render).
        hide_overlays: If True, hide selection handles and soil badges
            for the duration of the render.
        hide_construction: If True, hide construction-geometry items.
        text_point_size: If set, give the constant-pixel-size text items
            (labels, dimension texts) this point size; a text note keeps the
            size it has on the plan. Pass ``None`` (default) to leave the
            source scene's fonts untouched — useful when the caller has
            already prepared them.
        y_flip: If True, apply the standard Y-flip so scene "up" maps to
            target "up".
        render_style: The drawing style of this output; ``None`` is the scene's
            current one (``styled_scene``).
    """
    px_per_cm = target_rect.width() / source_rect.width() if source_rect.width() else None
    overlay_ctx = (
        _hidden_overlay_items(scene)
        if hide_overlays
        else _noop_context()
    )
    construction_ctx = (
        _hidden_construction_items(scene)
        if hide_construction
        else _noop_context()
    )
    text_ctx = (
        _scaled_text(scene, text_point_size)
        if text_point_size is not None
        else _noop_context()
    )

    style_ctx = styled_scene(scene, render_style, px_per_cm)

    with style_ctx, overlay_ctx, construction_ctx, text_ctx, _uncached_items(scene):
        painter.save()
        if y_flip:
            painter.translate(target_rect.x(), target_rect.y() + target_rect.height())
            painter.scale(1.0, -1.0)
            local_target = QRectF(0, 0, target_rect.width(), target_rect.height())
            scene.render(painter, local_target, source_rect)
        else:
            scene.render(painter, target_rect, source_rect)
        painter.restore()


def render_thumbnail_region(
    scene: QGraphicsScene,
    painter: QPainter,
    target_rect: QRectF,
    source_rect: QRectF,
    **kwargs: Any,
) -> None:
    """``render_scene_region`` for a thumbnail: always ``THUMBNAIL_STYLE``."""
    render_scene_region(
        scene, painter, target_rect, source_rect, render_style=THUMBNAIL_STYLE, **kwargs
    )


@contextmanager
def _uncached_items(scene: QGraphicsScene) -> Iterator[None]:
    """Temporarily disable item caching during scene rendering."""
    from open_garden_planner.services.export_service import ExportService

    cached = ExportService._disable_item_caches(scene)
    try:
        yield
    finally:
        ExportService._restore_item_caches(cached)


@contextmanager
def _noop_context() -> Iterator[None]:
    yield
