"""Georeference persistence, undo and hostile-file hardening (ADR-079)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_garden_planner.core.commands import (
    CommandManager,
    SetGeoreferenceCommand,
    SetPlanNorthCommand,
)
from open_garden_planner.core.georef import GeoReference, ReferencePoint
from open_garden_planner.core.project import FILE_VERSION, ProjectData, ProjectManager
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene


def _geo(**kw) -> GeoReference:
    base = {"origin_lat": 48.1, "origin_lon": 11.5, "source": "manual"}
    base.update(kw)
    return GeoReference(**base)


def _save(pm: ProjectManager, tmp_path: Path) -> Path:
    target = tmp_path / "p.ogp"
    pm.save(CanvasScene(width_cm=500, height_cm=500), target)
    return target


def _load(path: Path) -> ProjectManager:
    pm = ProjectManager()
    pm.load(CanvasScene(width_cm=500, height_cm=500), path)
    return pm


class TestRoundTrip:
    def test_no_georef_writes_no_key_and_keeps_file_version(self, qtbot, tmp_path):
        raw = json.loads(_save(ProjectManager(), tmp_path).read_text(encoding="utf-8"))
        assert "georef" not in raw and raw["version"] == FILE_VERSION

    def test_round_trip_keeps_everything_and_rotation_follows_north(self, qtbot, tmp_path):
        pm = ProjectManager()
        pm.set_north_angle(35.0)
        geo = _geo(
            parcel_boundary=((48.1, 11.5), (48.11, 11.5), (48.11, 11.51)),
            reference_points=(ReferencePoint("A", 48.1, 11.5, 0, 0),),
            fit_rms_m=0.04,
            source="fit",
        )
        pm.set_georef(geo)
        target = _save(pm, tmp_path)
        raw = json.loads(target.read_text(encoding="utf-8"))
        assert "rotation_deg" not in raw["georef"]  # one north
        assert raw["north_angle_deg"] == 35.0
        again = _load(target)
        assert again.georef == geo.with_rotation(35.0)
        assert again.georef.rotation_deg == again.north_angle_deg == 35.0

    def test_old_file_without_key_loads_as_none(self, qtbot, tmp_path):
        raw = json.loads(_save(ProjectManager(), tmp_path).read_text(encoding="utf-8"))
        raw.pop("georef", None)
        path = tmp_path / "old.ogp"
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert _load(path).georef is None

    def test_changing_north_turns_georef_without_a_second_store(self, qtbot):
        pm = ProjectManager()
        pm.set_georef(_geo())
        pm.set_north_angle(90)
        assert pm.georef.rotation_deg == 90.0

    def test_signal_and_dirty(self, qtbot):
        pm = ProjectManager()
        seen = []
        pm.georef_changed.connect(seen.append)
        pm.set_georef(_geo())
        assert pm.is_dirty and len(seen) == 1 and seen[0].origin_lat == 48.1
        pm.set_georef(_geo())  # same value: no signal
        assert len(seen) == 1
        pm.set_georef(None)
        assert seen[-1] is None

    def test_new_project_resets(self, qtbot):
        pm = ProjectManager()
        pm.set_georef(_geo())
        pm.new_project()
        assert pm.georef is None

    def test_snapshot_and_season_rollover_carry_it(self, qtbot):
        pm = ProjectManager()
        pm.set_georef(_geo())
        snap = pm.snapshot_dict(CanvasScene(width_cm=500, height_cm=500))
        assert snap["georef"]["origin_lat"] == 48.1

    def test_set_rejects_wrong_type(self, qtbot):
        with pytest.raises(TypeError):
            ProjectManager().set_georef({"origin_lat": 1})  # type: ignore[arg-type]


class TestHostileFiles:
    @pytest.mark.parametrize(
        "block",
        [
            "text", 5, [], {"origin_lat": "x", "origin_lon": 1},
            {"origin_lat": 10, "origin_lon": 10, "scale_meters_per_unit": 0},
            {"origin_lat": 95, "origin_lon": 10},
            {"origin_lat": True, "origin_lon": 10},
        ],
    )
    def test_bad_block_is_ignored_and_plan_opens(self, qtbot, tmp_path, block):
        raw = json.loads(_save(ProjectManager(), tmp_path).read_text(encoding="utf-8"))
        raw["georef"] = block
        path = tmp_path / "bad.ogp"
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert _load(path).georef is None

    def test_non_finite_literal_is_refused_like_every_numeric(self, qtbot, tmp_path):
        from open_garden_planner.core.plan_io import PlanValidationError

        raw = json.loads(_save(ProjectManager(), tmp_path).read_text(encoding="utf-8"))
        raw["georef"] = {"origin_lat": float("nan"), "origin_lon": 10}
        path = tmp_path / "nan.ogp"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(PlanValidationError):
            _load(path)

    def test_bad_optional_parts_are_dropped_on_load(self, qtbot, tmp_path):
        raw = json.loads(_save(ProjectManager(), tmp_path).read_text(encoding="utf-8"))
        raw["georef"] = {
            "origin_lat": 10, "origin_lon": 10,
            "reference_points": [{"lat": 99, "lon": 1, "x": 0, "y": 0}, "x"],
            "parcel_boundary": [[500, 1], [1, 1], "junk"],
        }
        path = tmp_path / "opt.ogp"
        path.write_text(json.dumps(raw), encoding="utf-8")
        geo = _load(path).georef
        assert geo is not None and geo.reference_points == () and geo.parcel_boundary == ((1.0, 1.0),)

    def test_project_data_from_dict_is_sanitised(self):
        data = ProjectData.from_dict({"georef": {"origin_lat": "x"}})
        assert data.georef is None


class TestUndo:
    def test_one_step_with_north(self, qtbot):
        pm = ProjectManager()
        pm.set_north_angle(10)
        mgr = CommandManager()
        mgr.execute(SetGeoreferenceCommand(pm, _geo(), 80.0))
        assert pm.georef.rotation_deg == 80.0 and pm.north_angle_deg == 80.0
        assert len(mgr._undo_stack) == 1
        mgr.undo()
        assert pm.georef is None and pm.north_angle_deg == 10.0
        mgr.redo()
        assert pm.georef.origin_lat == 48.1 and pm.north_angle_deg == 80.0

    def test_keeps_north_when_not_given(self, qtbot):
        pm = ProjectManager()
        mgr = CommandManager()
        mgr.execute(SetPlanNorthCommand(pm, 20))
        mgr.execute(SetGeoreferenceCommand(pm, _geo()))
        assert pm.north_angle_deg == 20.0
        mgr.undo()
        assert pm.georef is None and pm.north_angle_deg == 20.0

    def test_invalid_north_refused_before_the_stack(self, qtbot):
        with pytest.raises(ValueError):
            SetGeoreferenceCommand(ProjectManager(), _geo(), float("nan"))
