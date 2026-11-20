"""Gravel colours: four presets that read as their colour with the stones' own variation,
and a luminance-map tint (#502) that every other pattern keeps exactly."""

import numpy as np
import pytest
from PyQt6.QtGui import QColor

from open_garden_planner.core.fill_patterns import (
    GRAVEL_COLORS,
    FillPattern,
    _load_texture,
    _luminance,
    _tint_texture,
    gravel_color_key,
    tinted_pattern_pixels,
)


def _rgb(pattern: FillPattern, color: QColor) -> np.ndarray:
    width, height, data = tinted_pattern_pixels(pattern, color)  # type: ignore[misc]
    return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4)[:, :, :3].astype(float)


@pytest.mark.parametrize(("key", "_name", "hex_rgb"), GRAVEL_COLORS)
def test_gravel_mean_colour_is_the_preset(qtbot: object, key: str, _name: str, hex_rgb: str) -> None:
    target = QColor(hex_rgb)
    mean = _rgb(FillPattern.GRAVEL, target).reshape(-1, 3).mean(axis=0)
    assert np.abs(mean - [target.red(), target.green(), target.blue()]).max() < 6.0


def test_four_gravel_colours_are_clearly_different(qtbot: object) -> None:
    means = [_rgb(FillPattern.GRAVEL, QColor(h)).reshape(-1, 3).mean(axis=0)
             for _k, _n, h in GRAVEL_COLORS]
    for i in range(len(means)):
        for j in range(i + 1, len(means)):
            assert np.linalg.norm(means[i] - means[j]) > 25.0


def test_gravel_keeps_the_stones_hue_variation(qtbot: object) -> None:
    """Under the luminance map every pixel's chroma ratio is the fill's; here the
    stones differ (rust next to grey) within one fill."""
    color = QColor("#8f8c85")
    kept = _rgb(FillPattern.GRAVEL, color)
    ratio = kept[:, :, 0] / np.maximum(kept[:, :, 2], 1.0)       # R / B
    plain = (color.red() / color.blue())
    assert ratio.std() > 0.04
    assert abs(plain - 1.0) < 0.1                                  # the fill itself is near neutral


def test_other_patterns_keep_the_plain_luminance_map(qtbot: object) -> None:
    color = QColor(120, 160, 90)
    for pattern in (FillPattern.BRICK, FillPattern.PEBBLES, FillPattern.GRASS):
        texture = _load_texture(pattern)
        assert texture is not None
        lum = _luminance(texture.toImage())
        scale = lum / max(float(lum.mean()), 1.0)
        got = _rgb(pattern, color)
        expect = np.clip(np.array([color.red(), color.green(), color.blue()]) * scale[:, :, None], 0, 255)
        assert np.abs(got - np.floor(expect)).max() <= 1.0, pattern.name
        direct = _tint_texture(texture, color, pattern).toImage()
        assert direct.width() == texture.width()


def test_gravel_color_key_maps_presets_and_leaves_others() -> None:
    for key, _name, hex_rgb in GRAVEL_COLORS:
        assert gravel_color_key(QColor(hex_rgb)) == key
    assert gravel_color_key(QColor("#2a6fb0")) is None
    assert gravel_color_key(QColor("#8a98a8")) is None      # the old blue-grey plan fill
