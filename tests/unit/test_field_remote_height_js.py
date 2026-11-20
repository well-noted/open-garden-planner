"""The pure helpers of the tree-height mode in ``static/survey.js``, run under Node.

They build the request, word the answer and build the record. The record body they produce is
fed back to the real server models, so a drift between the page and the schema fails here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from open_garden_planner.core.positioning import tree_height as th
from open_garden_planner.field_remote import static
from open_garden_planner.field_remote.routes import RecordIn
from open_garden_planner.services import field_records as fr
from tests.unit.test_tree_height import aim, scene

NODE = shutil.which("node")
SURVEY_JS = Path(static.__file__).with_name("static") / "survey.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is not installed")


def run_js(script: str) -> Any:
    code = f"import * as s from {json.dumps(SURVEY_JS.as_uri())};\n{script}"
    out = subprocess.run(
        [NODE or "node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)


def test_parse_metres_accepts_a_comma_and_refuses_nonsense() -> None:
    got = run_js(
        "console.log(JSON.stringify(['12.5','12,5',' 8 ','0','-3','1e3','abc','','12.5.1','99999',null].map(s.parseMetres)));"
    )
    assert got == [12.5, 12.5, 8, None, None, None, None, None, None, None, None]


def test_tap_sample_and_request_carry_only_what_exists() -> None:
    got = run_js(
        "const pose = {x:1,y:2,z:3,qx:0,qy:0.5,qz:0,qw:0.8660254};"
        "const t1 = s.tapSample(pose, 'tracking', 100.5);"
        "console.log(JSON.stringify({t1,"
        " both: s.heightRequest({tap1:t1, tap2:t1, ground:{x:1,y:0,z:-5,extra:9}, tapeM:null}),"
        " tape: s.heightRequest({tap1:t1, tap2:t1, ground:null, tapeM:12.5}),"
        " tilt: s.heightRequest({tilt1:{beta:85,gamma:1}, tilt2:{beta:140,gamma:2}, tapeM:15})}));"
    )
    assert got["t1"] == {"position": [1, 2, 3], "quaternion": [0, 0.5, 0, 0.8660254], "tracking": "tracking", "t": 100.5}
    assert set(got["both"]) == {"tap1", "tap2", "ground"} and got["both"]["ground"] == {"x": 1, "y": 0, "z": -5}
    assert set(got["tape"]) == {"tap1", "tap2", "manual_distance_m"}
    assert got["tilt"] == {
        "tilt1": {"beta_deg": 85, "gamma_deg": 1}, "tilt2": {"beta_deg": 140, "gamma_deg": 2}, "manual_distance_m": 15,
    }


def _answer(**kw: Any) -> dict[str, Any]:
    viewer, base, top = scene(10.0, 0.0)
    tap1 = th.TapSample(viewer, aim(viewer, base), "tracking", 100.0)
    tap2 = th.TapSample(viewer, aim(viewer, top), "tracking", 102.0)
    out = th.compute_tree_height(tap1, tap2, ground=base, **kw)
    out["trunk_plan"] = {"x_cm": 2000.0, "y_cm": 1000.0, "lat": 48.0001, "lon": 11.0002}
    return out


def test_the_result_text_labels_the_range_as_assumed_and_words_the_warnings() -> None:
    result = _answer()
    result["warnings"] = ["crown_pitch_steep", "never_heard_of_it"]
    got = run_js(f"console.log(JSON.stringify(s.describeHeight({json.dumps(result)})));")
    assert got["headline"] == "Height about 12.0 m"
    assert "assumed errors, not a measured accuracy" in got["range"] and "Between" in got["range"]
    assert got["warnings"][0].startswith("The crown is steeply above you")
    assert got["warnings"][1] == "never_heard_of_it"  # an unknown code is shown, not hidden
    assert any("Distance to the trunk: 10.0 m" in line for line in got["lines"])


def test_the_record_body_is_accepted_by_the_server_and_the_store() -> None:
    result = _answer()
    viewer, base, top = scene(10.0, 0.0)
    req = {
        "tap1": {"position": list(viewer), "quaternion": list(aim(viewer, base)), "tracking": "tracking", "t": 100.0},
        "tap2": {"position": list(viewer), "quaternion": list(aim(viewer, top)), "tracking": "tracking", "t": 102.0},
        "ground": {"x": base[0], "y": base[1], "z": base[2]},
        "manual_distance_m": 10.0,
    }
    got = run_js(
        f"console.log(JSON.stringify(s.heightRecordBody({json.dumps(result)}, {json.dumps(req)},"
        " {key:'tree-height-key-1', name:'Oak', link:'obj_7'})));"
    )
    body = RecordIn.model_validate(got)  # the phone-facing model (strict, extra=forbid)
    assert body.kind == "measurement" and body.height_m == result["height_m"] and body.linked_object_id == "obj_7"
    stored = fr.FieldRecord.from_dict({**got, "source": "field_remote"}, record_id="r1")  # the store's own checks
    fr.check_kind_rules(stored)
    assert stored.geometry and stored.geometry["x_cm"] == 2000.0 and stored.fix_mode == "vio_fused"
    assert stored.height_data and stored.height_data["inputs"]["manual_distance_m"] == 10.0
    assert stored.height_data["assumed"]["note"].startswith("ASSUMED, NOT MEASURED")  # type: ignore[index]


def test_the_record_body_without_an_ar_lock_or_names_has_no_position() -> None:
    result = _answer()
    result["trunk_plan"] = None
    got = run_js(
        f"console.log(JSON.stringify(s.heightRecordBody({json.dumps(result)}, {{tilt1:{{beta_deg:85,gamma_deg:0}},"
        " tilt2:{beta_deg:140,gamma_deg:0}}, {key:'tree-height-key-2', name:'', link:''})));"
    )
    assert "geometry" not in got and "plant_name" not in got and "linked_object_id" not in got
    assert got["height_data"]["inputs"]["tilt1"] == [85, 0]
    RecordIn.model_validate(got)
    fr.check_kind_rules(fr.FieldRecord.from_dict({**got, "source": "field_remote"}, record_id="r2"))
