"""Which drawing role an item plays in a render style (Package 4, WP4).

One table keyed by the object type's name (so this module stays Qt-free), plus
the layer rule for irrigation: a style painter switches on the role, so a new
object type needs exactly one row here.

Irrigation is a property of the item's LAYER, never of its type and never of the
layer's translated display name (owner decision Q2). A layer is the irrigation
layer when its stable key ``Layer.permaculture_layer`` (a free string the plan
file already carries, so no new field) equals ``IRRIGATION_LAYER_KEY``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Final


class StyleRole(Enum):
    """What an item is, as far as a drawing style is concerned."""

    BUILDING = "building"
    ROOF = "roof"
    HARDSCAPE = "hardscape"
    PATH = "path"
    FENCE_WALL = "fence_wall"
    LAWN = "lawn"
    MULCH = "mulch"
    BED = "bed"
    PLANT_TREE = "plant_tree"
    PLANT_SHRUB = "plant_shrub"
    PLANT_GROUND = "plant_ground"
    WATER = "water"
    IRRIGATION = "irrigation"
    ANNOTATION = "annotation"
    OTHER = "other"


#: The stable key of the irrigation layer (``Layer.permaculture_layer``).
IRRIGATION_LAYER_KEY: Final = "irrigation"

_ROLE_BY_TYPE_NAME: Final[dict[str, StyleRole]] = {
    "HOUSE": StyleRole.BUILDING,
    "GARAGE_SHED": StyleRole.BUILDING,
    "TOOL_SHED": StyleRole.BUILDING,
    "GREENHOUSE": StyleRole.BUILDING,
    "ROOF_RIDGE": StyleRole.ROOF,
    "TERRACE_PATIO": StyleRole.HARDSCAPE,
    "DRIVEWAY": StyleRole.HARDSCAPE,
    "PATH": StyleRole.PATH,
    "FENCE": StyleRole.FENCE_WALL,
    "WALL": StyleRole.FENCE_WALL,
    "HEDGE_POLYGON": StyleRole.FENCE_WALL,
    "HEDGE_SECTION": StyleRole.FENCE_WALL,
    "LAWN": StyleRole.LAWN,
    "MULCH": StyleRole.MULCH,
    "GARDEN_BED": StyleRole.BED,
    "RAISED_BED": StyleRole.BED,
    "CONTAINER": StyleRole.BED,
    "CONTAINER_ROUND": StyleRole.BED,
    "WALL_PLANTER": StyleRole.BED,
    "TREE": StyleRole.PLANT_TREE,
    "SHRUB": StyleRole.PLANT_SHRUB,
    "PERENNIAL": StyleRole.PLANT_GROUND,
    "POND_POOL": StyleRole.WATER,
    "RAIN_BARREL": StyleRole.WATER,
    "WATER_TAP": StyleRole.WATER,
    "BIRD_BATH": StyleRole.WATER,
    "GENERIC_TEXT": StyleRole.ANNOTATION,
    "GENERIC_CALLOUT": StyleRole.ANNOTATION,
    "GARDEN_JOURNAL_PIN": StyleRole.ANNOTATION,
}


def role_for_type_name(type_name: str | None) -> StyleRole:
    """The role of an object type, by its enum name (OTHER when unlisted)."""
    return _ROLE_BY_TYPE_NAME.get(type_name or "", StyleRole.OTHER)


def is_irrigation_layer(layer: Any) -> bool:
    """Whether ``layer`` is the irrigation layer, by its stable key."""
    return getattr(layer, "permaculture_layer", None) == IRRIGATION_LAYER_KEY


def role_for_item(item: Any, irrigation_layer_ids: frozenset[Any] = frozenset()) -> StyleRole:
    """The role of a canvas item: IRRIGATION for an item on an irrigation layer
    (``irrigation_layer_ids`` is the set of those layers' ids), else by type."""
    if getattr(item, "layer_id", None) in irrigation_layer_ids:
        return StyleRole.IRRIGATION
    object_type = getattr(item, "object_type", None)
    return role_for_type_name(getattr(object_type, "name", None))
