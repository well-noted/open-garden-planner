"""Render styles WP9: the 3D Line & Wash look as data - the NprSpec table, the look's
overrides (``styled_look``), its mapping onto the scene and the look controller's style.
Engine-free; what the GPU draws is ``tests/integration/test_render_style_3d.py``."""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace
from typing import Any

import pytest

from open_garden_planner.core.scene3d import SunState
from open_garden_planner.core.scene3d.look import (
    LINE_AND_WASH_NPR,
    NEUTRAL_DAY,
    LookSpec,
    NprSpec,
    look_for,
    npr_for,
    styled_look,
)
from open_garden_planner.ui.view3d.quick3d.look import npr_properties, scene_properties

NOON = SunState(elevation_deg=55.0, azimuth_deg=180.0)


# ── the table ──────────────────────────────────────────────────────────────


def test_the_table_holds_every_value_of_the_style_in_sane_ranges() -> None:
    spec = LINE_AND_WASH_NPR
    shares = (spec.edge_share, spec.grain_share)
    assert all(0.0 < share < 0.01 for share in shares), "shares of the picture's height"
    assert 0.0 < spec.crease_thresh < spec.silhouette_thresh < 1.0
    for name in ("crease_ink", "ink_spread", "busy_cut", "edge_darken", "wash_lift",
                 "wash_saturation", "grain_strength", "ground_flatten", "saturation_scale",
                 "wind_scale", "shadow_factor_scale"):
        assert 0.0 <= getattr(spec, name) <= 1.0, name
    assert 2.0 <= spec.bands <= 8.0 and 0.0 <= spec.band_soft <= 0.5
    assert 0.0 < spec.fog_near_cm < spec.fog_far_cm
    for colour in (spec.paper_rgb, spec.ink_rgb):
        assert len(colour) == 7 and colour.startswith("#")
        int(colour[1:], 16)
    # the paper is light, the ink dark
    assert sum(int(spec.paper_rgb[i:i + 2], 16) for i in (1, 3, 5)) > 3 * 200
    assert sum(int(spec.ink_rgb[i:i + 2], 16) for i in (1, 3, 5)) < 3 * 60


@pytest.mark.parametrize(("style", "drawn"), [
    ("illustrated", False), ("cad", False), ("line_and_wash", True),
    ("LINE_AND_WASH", True), ("nonsense", False), (None, False),
])
def test_only_line_and_wash_draws_the_npr_look(style: object, drawn: bool) -> None:
    """Design Q6: CAD has no 3D mode in v1 and stays plain 3D."""
    assert (npr_for(style) is LINE_AND_WASH_NPR) is drawn


# ── the look's overrides ───────────────────────────────────────────────────


@pytest.mark.parametrize("style", ["illustrated", "cad", None])
def test_a_plain_style_returns_the_very_same_look(style: object) -> None:
    look = look_for(NOON, "high")
    assert styled_look(look, style) is look


def test_line_and_wash_overrides_art_direction_only() -> None:
    look = look_for(NOON, "ultra")
    styled = styled_look(look, "line_and_wash")
    spec = LINE_AND_WASH_NPR
    assert styled.npr == spec and styled.unstyled is look
    # the post effects that would break the one-slot chain or fight the style are off
    assert not (styled.preset.glow or styled.preset.reflections or styled.preset.fog)
    assert not styled.preset.lawn_blades
    assert styled.mood.saturation == pytest.approx(look.mood.saturation * spec.saturation_scale)
    assert styled.wind_strength == pytest.approx(look.wind_strength * spec.wind_scale)
    assert styled.atmosphere.ao_strength_gain == pytest.approx(
        look.atmosphere.ao_strength_gain * spec.ao_strength_scale)
    # truth is untouched: the light's colour and exposure, the moon, the sky, the shadows
    # (their softening is the scene's, while the effect is drawn - never the truth view's)
    assert styled.shadow == look.shadow
    assert styled.sky == look.sky
    assert styled.moon_travel_scene == look.moon_travel_scene
    assert (styled.mood.sun_color, styled.mood.exposure) == (look.mood.sun_color,
                                                             look.mood.exposure)
    assert styled.preset.name == look.preset.name
    assert styled.preset.shadow_map_quality == look.preset.shadow_map_quality


def test_a_styled_look_goes_back_to_its_plain_look() -> None:
    look = look_for(NOON, "high")
    styled = styled_look(look, "line_and_wash")
    assert styled_look(styled, "line_and_wash") is styled
    assert styled_look(styled, "illustrated") is look
    assert styled_look(styled, "cad") is look


def test_every_look_for_is_plain() -> None:
    assert NEUTRAL_DAY.npr is None and NEUTRAL_DAY.unstyled is None
    assert look_for(NOON).npr is None


# ── the mapping onto the scene ─────────────────────────────────────────────


def test_the_plain_look_switches_the_effect_off_and_keeps_the_ground() -> None:
    props = npr_properties(None)
    assert props["nprStyle"] is False and props["groundFlatten"] == 0.0


def test_the_styled_mapping_is_the_table() -> None:
    props = npr_properties(LINE_AND_WASH_NPR)
    spec = LINE_AND_WASH_NPR
    assert props["nprStyle"] is True
    assert props["groundFlatten"] == spec.ground_flatten
    assert props["nprEdgeShare"] == spec.edge_share and props["nprBands"] == spec.bands
    assert props["nprShadowScale"] == spec.shadow_factor_scale
    assert props["nprPaper"] == pytest.approx(
        tuple(int(spec.paper_rgb[i:i + 2], 16) / 255.0 for i in (1, 3, 5)))
    # every table value the effect reads is mapped (the look overrides are not effect inputs)
    overrides = {"saturation_scale", "wind_scale", "ao_strength_scale"}
    assert len(props) == len([f for f in fields(NprSpec) if f.name not in overrides]) + 1


def test_the_style_changes_only_the_overridden_scene_properties() -> None:
    look = look_for(NOON, "high")
    plain, styled = scene_properties(look), scene_properties(styled_look(look, "line_and_wash"))
    changed = {name for name in plain if plain[name] != styled[name]}
    allowed = {"nprStyle", "groundFlatten", "glow", "reflections", "fog", "lawnBlades",
               "saturation", "windStrength", "windSway", "aoStrengthGain"}
    assert changed <= allowed and {"nprStyle", "groundFlatten"} <= changed
    # the truth view's shadow is the plain look's
    for name in ("shadowFactor", "pcfFactor", "shadowBias", "shadowMapQuality", "sunColor"):
        assert plain[name] == styled[name], name


# ── the look controller ────────────────────────────────────────────────────


class _Host:
    def __init__(self) -> None:
        self.looks: list[LookSpec] = []

    def set_look(self, look: LookSpec) -> None:
        self.looks.append(look)

    def set_sky(self, _sky: Any) -> None: ...

    def set_wind_running(self, _running: bool) -> None: ...

    def rest_wind(self) -> None: ...

    def health(self) -> SimpleNamespace:
        return SimpleNamespace(frames_presented=0)


def test_the_look_controller_draws_the_render_style(qtbot: Any) -> None:  # noqa: ARG001
    from open_garden_planner.ui.view3d.look_controller import LookController

    host = _Host()
    controller = LookController(host, software=True, quality="high",
                                render_style="line_and_wash")
    controller.set_sun(NOON)
    assert controller.render_style == "line_and_wash"
    assert host.looks[-1].npr is LINE_AND_WASH_NPR
    controller.set_render_style("illustrated")
    assert host.looks[-1].npr is None and host.looks[-1].unstyled is None
    sent = len(host.looks)
    controller.set_render_style("cad")   # CAD stays plain 3D: nothing new to send
    assert len(host.looks) == sent
    controller.set_render_style("line_and_wash")
    assert host.looks[-1].npr is LINE_AND_WASH_NPR
    controller.shutdown()
