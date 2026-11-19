"""Field positioning core (Field Remote design, section 2): pure Python + numpy.

No Qt, no network, no clock: time comes from the samples. Positions are local
east/north metres around the plan's georeference origin; the canvas conversion
happens once, at the edge, in :mod:`.engine`.

Accuracy figures here are hypotheses until the on-device measurement protocol
has run; nothing in this package claims sub-decimeter absolute accuracy.
"""

from .engine import PositioningEngine, TrackResult
from .types import FixMode, GnssSample, ImuSample, PositionEstimate, VioSample

__all__ = [
    "FixMode",
    "GnssSample",
    "ImuSample",
    "PositionEstimate",
    "PositioningEngine",
    "TrackResult",
    "VioSample",
]
