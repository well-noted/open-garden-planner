"""The phone's geometry in ``static/geo.js`` against the desktop's ``core/georef.py``.

The page does its own plan-to-GPS conversion, so a drift between the two would place a saved
plant on the wrong spot. Runs the real module under Node; skipped where Node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from open_garden_planner.core.georef import GeoReference
from open_garden_planner.field_remote import projection, static

NODE = shutil.which("node")
GEO_JS = Path(static.__file__).with_name("static") / "geo.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is not installed")


def run_js(script: str) -> object:
    code = f"import * as g from {json.dumps(GEO_JS.as_uri())};\n{script}"
    out = subprocess.run(
        [NODE or "node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)


def test_canvas_to_gps_and_back_match_the_desktop_projection() -> None:
    geo = GeoReference(origin_lat=33.5, origin_lon=-112.0, scale_meters_per_unit=0.01, rotation_deg=23.0)
    summary = projection.georef_summary({"georef": geo.to_dict(), "north_angle_deg": 23.0})
    for x, y in [(0.0, 0.0), (1234.5, -987.0), (-2500.0, 4000.0)]:
        lat, lon = geo.canvas_to_gps(x, y)
        got = run_js(
            f"const s = {json.dumps(summary)}; const r = g.canvasToGps(s, {x}, {y});"
            f"const b = g.gpsToCanvas(s, r.lat, r.lon); console.log(JSON.stringify([r.lat, r.lon, b.x, b.y]));"
        )
        assert got[0] == pytest.approx(lat, abs=1e-9) and got[1] == pytest.approx(lon, abs=1e-9)  # type: ignore[index]
        assert got[2] == pytest.approx(x, abs=0.01) and got[3] == pytest.approx(y, abs=0.01)  # type: ignore[index]


def test_trilateration_finds_the_spot_and_reports_the_mirror_and_the_misfit() -> None:
    # known spots 10 m apart, a point 4 m / 6 m off the line: two spots fit unless a third is given
    r = run_js(
        "const a = [{x:0,y:0},{x:1000,y:0}]; const d = [5, 8.062];"
        "console.log(JSON.stringify(g.trilaterate(a, d, 0.01)));"
    )
    assert r["pos"]["x"] == pytest.approx(300, abs=3) and abs(r["pos"]["y"]) == pytest.approx(400, abs=3)  # type: ignore[index]
    assert r["ambiguous"] is True and r["alt"]["y"] == pytest.approx(-r["pos"]["y"], abs=0.5)  # type: ignore[index]
    third = run_js(
        "const a = [{x:0,y:0},{x:1000,y:0},{x:0,y:1000}]; const d = [5, 8.062, 6.708];"
        "console.log(JSON.stringify(g.trilaterate(a, d, 0.01)));"
    )
    assert third["ambiguous"] is False and third["pos"]["y"] > 0 and third["rms_m"] < 0.05  # type: ignore[index]
    bad = run_js("console.log(JSON.stringify(g.trilaterate([{x:0,y:0},{x:1000,y:0}], [1, 2], 0.01)));")
    assert bad["error"] == "noreach"  # type: ignore[index]
