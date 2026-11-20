"""The 3D Line & Wash style on a real renderer (render styles WP9).

Render tier: opt-in (``OGP_RENDER3D=1``), like ``test_quick3d_render.py``: one driver
process (``quick3d_render_driver.py``, scenario ``npr``) renders a small plan - a red box
east and a blue box north - with the style off and on; the tests judge its frames.
Assertions are about meaning (identity where the style must not reach, ink and paper
where it must), never pixel goldens of the styled look.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from open_garden_planner.core.scene3d.look import LINE_AND_WASH_NPR
from tests.integration.quick3d_render_support import (
    failure_report,
    load_rgb,
    render_tier,
    run_driver,
)

pytestmark = render_tier

Run = tuple[dict[str, Any], Path]


def _hex(colour: str) -> np.ndarray:
    value = colour.lstrip("#")
    return np.array([int(value[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64)


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    out = tmp_path_factory.mktemp("render_style_3d")
    proc, result = run_driver(out, "npr")
    assert proc.returncode == 0 and result.get("status") == "ok", failure_report(proc, out)
    return result, out


def _rgb(run: Run, key: str) -> np.ndarray:
    info = run[0]["scenarios"]["npr"][key]
    assert info["build_failures"] == {}, key
    return load_rgb(run[1] / info["file"])


def test_the_truth_view_ignores_the_style(run: Run) -> None:
    """The truth gates read the top-down view: the style is never drawn there, so its
    pixels are the plain look's, bit for bit."""
    assert np.array_equal(_rgb(run, "truth_plain"), _rgb(run, "truth_styled"))


def test_the_plain_look_is_the_default_frame(run: Run) -> None:
    """Asking for Illustrated draws exactly the frame of no style at all."""
    assert np.array_equal(_rgb(run, "plain"), _rgb(run, "illustrated"))


def test_the_style_is_deterministic(run: Run) -> None:
    """No random source, no clock: the same frame twice."""
    assert np.array_equal(_rgb(run, "styled"), _rgb(run, "styled_again"))


def test_line_and_wash_draws_paper_and_ink(run: Run) -> None:
    plain, styled = _rgb(run, "plain"), _rgb(run, "styled")
    assert plain.shape == styled.shape
    assert np.abs(plain - styled).mean() > 10.0, "the style changed the picture"
    # the sky is the paper (the grain moves it by a few levels at most)
    sky = styled[: styled.shape[0] // 10].reshape(-1, 3)
    paper = _hex(LINE_AND_WASH_NPR.paper_rgb)
    assert np.abs(sky.mean(axis=0) - paper).max() < 8.0
    # ink: pixels near the ink colour, which the plain frame does not have, and they lie
    # where the boxes' outlines are (beside the boxes' saturated faces)
    ink = _hex(LINE_AND_WASH_NPR.ink_rgb)
    inked = np.abs(styled - ink).max(axis=-1) < 40.0
    plain_dark = np.abs(plain - ink).max(axis=-1) < 40.0
    assert inked.sum() > 150, inked.sum()
    assert inked.sum() > 5 * max(1, int(plain_dark.sum()))
    red = (styled[..., 0] > 120) & (styled[..., 1] < 110) & (styled[..., 2] < 110)
    blue = (styled[..., 2] > 120) & (styled[..., 0] < 110)
    near = np.zeros_like(inked)
    for mask in (red, blue):
        ys, xs = np.nonzero(mask)
        assert ys.size > 50, "both boxes are in the frame"
        near[max(0, ys.min() - 4):ys.max() + 5, max(0, xs.min() - 4):xs.max() + 5] = True
    assert inked[near].sum() > 0.6 * inked.sum(), "the ink is on the boxes' outlines"


def test_the_flat_ground_draws_no_ink_rows(run: Run) -> None:
    """The step class (a window frame a few cm proud) is far under the depth buffer's own
    quantisation, which draws whole rows of 'steps' across a flat ground; a step is inked
    only where the colour changes too, so the open ground away from the boxes stays bare."""
    plain, styled = _rgb(run, "plain"), _rgb(run, "styled")
    ink = _hex(LINE_AND_WASH_NPR.ink_rgb)
    dark = np.abs(styled - ink).max(axis=-1) < 70.0
    red = (styled[..., 0] > 120) & (styled[..., 1] < 110) & (styled[..., 2] < 110)
    blue = (styled[..., 2] > 120) & (styled[..., 0] < 110)
    clear = np.ones_like(dark)
    for mask in (red, blue):
        ys, xs = np.nonzero(mask)
        clear[max(0, ys.min() - 60):ys.max() + 61, max(0, xs.min() - 60):xs.max() + 61] = False
    # the ground: below the horizon (the plain frame's sky is its top rows), the boxes and
    # their shadows' surroundings left out
    ground = clear.copy()
    ground[: plain.shape[0] // 3] = False
    assert ground.sum() > 0.2 * ground.size
    rows = (dark & ground).sum(axis=1)
    assert dark[ground].mean() < 0.002, dark[ground].mean()
    assert rows.max() < 0.05 * plain.shape[1], "no row of ink across the ground"
