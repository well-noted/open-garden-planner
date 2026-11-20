"""Render styles: the drawing style a plan is shown or exported in.

The style is a user preference with a per-output override, never plan data
(Package 4 design, owner decision Q1). Qt-free, so the settings, the Agent API
and the painters share one list.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class RenderStyle(StrEnum):
    """The drawing styles. The value is the machine id used in settings and tools."""

    ILLUSTRATED = "illustrated"
    CAD = "cad"
    LINE_AND_WASH = "line_and_wash"


RENDER_STYLES: Final = tuple(RenderStyle)


#: What the Agent API tells a client about each style (``list_render_styles``).
#: Machine ids and English labels are not UI text and are not translated. ``svg_vector``:
#: ``"full"`` when an SVG/PDF carries the style as vector paths alone, ``"approximate"`` when
#: part of the picture (Line & Wash's paper grain) is left out of a vector output.
RENDER_STYLE_INFO: Final[dict[RenderStyle, dict[str, object]]] = {
    RenderStyle.ILLUSTRATED: {
        "label_en": "Illustrated", "svg_vector": "full", "supports_3d": True, "options": [],
    },
    RenderStyle.CAD: {
        "label_en": "CAD", "svg_vector": "full", "supports_3d": True, "options": ["callouts"],
    },
    RenderStyle.LINE_AND_WASH: {
        "label_en": "Line & Wash", "svg_vector": "approximate", "supports_3d": True,
        "options": [],
    },
}


def render_style_catalogue() -> list[dict[str, object]]:
    """The styles as the Agent API lists them: ``{id, label_en, svg_vector, supports_3d,
    options}`` in ``RENDER_STYLES`` order."""
    return [{"id": style.value, **RENDER_STYLE_INFO[style]} for style in RENDER_STYLES]


def _lookup(value: object) -> RenderStyle | None:
    if isinstance(value, RenderStyle):
        return value
    if isinstance(value, str):
        try:
            return RenderStyle(value.strip().lower())
        except ValueError:
            return None
    return None


def normalise_render_style(
    value: object, default: RenderStyle = RenderStyle.ILLUSTRATED
) -> RenderStyle:
    """Lenient parse for settings: anything unknown falls back to ``default``."""
    found = _lookup(value)
    return default if found is None else found


def parse_render_style(value: object) -> RenderStyle:
    """Strict parse for the Agent API: raises ``ValueError`` listing valid ids."""
    found = _lookup(value)
    if found is None:
        valid = ", ".join(style.value for style in RENDER_STYLES)
        raise ValueError(f"Unknown render_style {value!r}; valid values: {valid}")
    return found
