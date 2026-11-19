"""Scene-wide render-style state and its cache (Package 4, WP2; design 2.4).

``StyleState`` holds what a style computes for the whole scene rather than for
one item (palette and pens per role, canopy clusters, callout layout, the hatch
budget). It is keyed ``(style, paint_revision, band)`` and kept in an LRU of two,
so an export in another style does not evict the state the screen is using.

It is built by the scene when something it depends on moves (a style switch, a
paint revision bump, a LOD band change, an export's prewarm) and **never inside
``paint()``**: ``StyleStateCache.lookup`` only reads. A miss makes the item paint
the plain base geometry; the scene then schedules the build.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.ui.canvas.style.base import painter_for

#: States kept per scene: the screen's and an export's.
STATE_CACHE_SIZE = 2

StateKey = tuple[RenderStyle, int, int]


@dataclass(frozen=True)
class StyleState:
    """The scene-wide state of ``style`` at paint ``revision`` and LOD ``band``.

    ``data`` is what the style's painter built (``StylePainter.build_state``):
    empty for a style that has none. Frozen: a state is replaced, never edited.
    """

    style: RenderStyle
    revision: int
    band: int
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> StateKey:
        return (self.style, self.revision, self.band)


class StyleStateCache:
    """An LRU of ``STATE_CACHE_SIZE`` states, keyed ``(style, revision, band)``."""

    def __init__(self, size: int = STATE_CACHE_SIZE) -> None:
        self._size = size
        self._states: OrderedDict[StateKey, StyleState] = OrderedDict()
        #: How many states were built: the instrument tests count (never timed).
        self.builds = 0

    def __len__(self) -> int:
        return len(self._states)

    def lookup(self, style: RenderStyle, revision: int, band: int) -> StyleState | None:
        """The kept state, or None. Never builds: this is what a paint calls."""
        key = (style, revision, band)
        state = self._states.get(key)
        if state is not None:
            self._states.move_to_end(key)
        return state

    def ensure(self, scene: Any, style: RenderStyle, revision: int, band: int) -> StyleState:
        """The state for the key, built from the style's painter on a miss."""
        state = self.lookup(style, revision, band)
        if state is not None:
            return state
        painter = painter_for(style)
        data = painter.build_state(scene, band) if painter is not None else {}
        state = StyleState(style, revision, band, dict(data))
        self.builds += 1
        self._states[state.key] = state
        while len(self._states) > self._size:
            self._states.popitem(last=False)
        return state

    def clear(self) -> None:
        self._states.clear()
