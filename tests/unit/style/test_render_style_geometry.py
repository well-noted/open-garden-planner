"""Pure geometry of the render styles (Package 4, WP3)."""

from __future__ import annotations

import ast
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QPainterPath, QPolygonF

from open_garden_planner.core.render_style import (
    RENDER_STYLES,
    RenderStyle,
    normalise_render_style,
    parse_render_style,
)
from open_garden_planner.ui.canvas.style import geometry as g

P = QPointF


def _square(x=0.0, y=0.0, s=100.0):
    return [P(x, y), P(x + s, y), P(x + s, y + s), P(x, y + s)]


def _total_length(segments):
    return sum(math.hypot(b.x() - a.x(), b.y() - a.y()) for a, b in segments)


def _xy(points):
    return [(p.x(), p.y()) for p in points]


class TestEnum:
    def test_ids(self):
        assert [s.value for s in RENDER_STYLES] == ["illustrated", "cad", "line_and_wash"]

    def test_lenient_falls_back(self):
        assert normalise_render_style("bogus") is RenderStyle.ILLUSTRATED
        assert normalise_render_style(None, RenderStyle.CAD) is RenderStyle.CAD
        assert normalise_render_style(" CAD ") is RenderStyle.CAD

    def test_strict_lists_valid_ids(self):
        with pytest.raises(ValueError, match="illustrated, cad, line_and_wash"):
            parse_render_style("pencil")
        assert parse_render_style("line_and_wash") is RenderStyle.LINE_AND_WASH


class TestHatch:
    def test_square_horizontal_exact(self):
        segs = g.hatch_segments([_square(0, 0, 100)], 0.0, 10.0)
        # Lines at y = 0..90; the half-open rule drops the line on the top edge.
        assert len(segs) == 10
        assert all(abs(a.x()) < 1e-9 and abs(b.x() - 100) < 1e-9 for a, b in segs)

    def test_segments_stay_inside_concave_polygon(self):
        l_shape = [P(0, 0), P(100, 0), P(100, 40), P(40, 40), P(40, 100), P(0, 100)]
        segs = g.hatch_segments([l_shape], 45.0, 7.0)
        path = QPainterPath()
        path.addPolygon(QPolygonF(l_shape))
        path.closeSubpath()
        assert segs
        for a, b in segs:
            assert path.contains(P((a.x() + b.x()) / 2, (a.y() + b.y()) / 2))

    def test_hole_is_excluded(self):
        outer, hole = _square(0, 0, 100), _square(25, 25, 50)
        with_hole = g.hatch_segments([outer, hole], 0.0, 5.0)
        solid = g.hatch_segments([outer], 0.0, 5.0)
        # 10 lines cross the 50 cm hole.
        assert _total_length(solid) - _total_length(with_hole) == pytest.approx(500, rel=0.15)
        for a, b in with_hole:
            if 25 < a.y() < 75:
                assert not (a.x() < 50 < b.x())

    def test_phase_invariant_under_translation(self):
        base = g.hatch_segments([_square(0, 0, 100)], 45.0, 10.0)
        moved = g.hatch_segments([_square(13.0, 29.0, 100)], 45.0, 10.0)
        theta = math.radians(45.0)
        nx, ny = -math.sin(theta), math.cos(theta)
        for a, _b in base + moved:
            k = (nx * a.x() + ny * a.y()) / 10.0
            assert k == pytest.approx(round(k), abs=1e-9)

    def test_budget_returns_none(self):
        assert g.hatch_segments([_square(0, 0, 1000)], 0.0, 1.0, max_segments=50) is None

    def test_bad_spacing(self):
        with pytest.raises(ValueError):
            g.hatch_segments([_square()], 0.0, 0.0)

    def test_degenerate_ring_gives_nothing(self):
        assert g.hatch_segments([[P(0, 0), P(1, 1)]], 0.0, 1.0) == []


class TestWobble:
    def test_deterministic(self):
        pts = [P(0, 0), P(300, 0), P(300, 200)]
        a = [_xy(s) for s in g.wobble_polyline(pts, 123, 2)]
        assert a == [_xy(s) for s in g.wobble_polyline(pts, 123, 2)]
        assert a != [_xy(s) for s in g.wobble_polyline(pts, 124, 2)]

    def test_one_open_subpath_per_segment(self):
        assert len(g.wobble_polyline([P(0, 0), P(100, 0), P(100, 100), P(0, 100)], 1, 0)) == 3

    def test_amplitude_and_overshoot_bounds(self):
        band = 1
        amp = g.paper_mm_to_cm(0.35, band)
        over = g.paper_mm_to_cm(1.2, band)
        (sub,) = g.wobble_polyline([P(0, 0), P(500, 0)], 99, band)
        assert len(sub) > 5
        assert max(abs(p.y()) for p in sub) <= amp + 1e-9
        assert -over - 1e-9 <= sub[0].x() <= 0
        assert 500 <= sub[-1].x() <= 500 + over + 1e-9

    def test_zero_length_segment_skipped(self):
        assert g.wobble_polyline([P(1, 1), P(1, 1)], 5, 0) == []

    def test_closed_does_not_quite_close(self):
        ring = [P(math.cos(a) * 200, math.sin(a) * 200)
                for a in (i * math.tau / 32 for i in range(32))]
        out = g.wobble_closed(ring, 7, 0)
        assert out[-1] != out[0]
        assert math.hypot(out[0].x() - out[-1].x(), out[0].y() - out[-1].y()) < 0.1 * 200 * math.tau

    def test_closed_deterministic(self):
        ring = _square(0, 0, 200)
        assert _xy(g.wobble_closed(ring, 3, 1)) == _xy(g.wobble_closed(ring, 3, 1))

    def test_seed_is_process_stable(self):
        code = (
            "from open_garden_planner.ui.canvas.style import geometry as g;"
            "s = g.style_seed('abc','tree');"
            "print(s, g.value_noise_1d(3.7, s))"
        )
        outs = {
            subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True, check=True,
                env={**os.environ, "PYTHONHASHSEED": str(h)},
            ).stdout
            for h in ("1", "2", "random")
        }
        assert len(outs) == 1
        assert outs.pop().split()[0] == str(g.style_seed("abc", "tree"))

    def test_noise_range(self):
        vals = [g.value_noise_1d(x / 7.0, 42) for x in range(500)]
        assert min(vals) >= -1.0 and max(vals) <= 1.0
        assert max(vals) - min(vals) > 0.5


class TestBand:
    def test_octaves(self):
        assert g.band_for_scale(1.0) == 0
        assert g.band_for_scale(1.99) == 0
        assert g.band_for_scale(2.0) == 1
        assert g.band_for_scale(0.4) == -2

    def test_mm_to_cm_halves_per_band(self):
        assert g.paper_mm_to_cm(1.0, 1) == pytest.approx(g.paper_mm_to_cm(1.0, 0) / 2)


class TestScallopRingCluster:
    @pytest.mark.parametrize("reverse", [False, True])
    def test_scallops_bulge_outward_any_winding(self, reverse):
        ring = _square(0, 0, 200)
        if reverse:
            ring.reverse()
        rect = g.scallop_outline(ring, 11, 0).boundingRect()
        assert rect.left() < 0 and rect.top() < 0
        assert rect.right() > 200 and rect.bottom() > 200

    def test_edge_ring_is_band_inside_shape(self):
        shape = QPainterPath()
        shape.addRect(0, 0, 100, 100)
        ring = g.edge_ring(shape, 5.0)
        assert ring.boundingRect().width() == pytest.approx(100.0, abs=1e-6)
        assert ring.contains(P(2, 50)) and not ring.contains(P(50, 50))
        hits = sum(ring.contains(P(x + 0.5, y + 0.5)) for x in range(100) for y in range(100))
        assert hits / 10000 == pytest.approx(1 - 90 * 90 / 10000, abs=0.02)

    def test_clusters(self):
        circles = [(0, 0, 10), (15, 0, 10), (500, 500, 10), (30, 0, 10), (505, 505, 10)]
        assert g.cluster_circles(circles) == [[0, 1, 3], [2, 4]]
        assert g.cluster_circles([]) == []
        assert g.cluster_circles([(0, 0, 5), (20, 0, 5)]) == [[0], [1]]

    def test_united_is_one_mass(self):
        assert len(g.united_circles([(0, 0, 10), (12, 0, 10)]).toSubpathPolygons()) == 1
        assert g.united_circles([]).isEmpty()


def test_style_package_is_deterministic_by_construction():
    """No hash(), random or time in ui/canvas/style (design 5.1)."""
    for src in Path(g.__file__).parent.glob("*.py"):
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "hash", f"{src.name}: hash() is per-process"
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for mod in mods:
                assert mod.split(".")[0] not in {"random", "time", "secrets"}, src.name
