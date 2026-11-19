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
