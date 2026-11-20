"""Unit tests for services/field_records.py (ADR-080): the sidecar record store."""

from __future__ import annotations

import io
import json
import threading

import pytest
from PIL import Image

from open_garden_planner.services import field_records as fr


def _png(size: int = 4) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), "red").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def plan(tmp_path):
    return tmp_path / "garden.ogp"


def test_paths_sit_beside_the_plan(plan) -> None:
    j, d = fr.sidecar_paths(plan)
    assert j == plan.with_name("garden.fieldnotes.json")
    assert d == plan.with_name("garden.fieldnotes")


def test_unsaved_plan_is_refused() -> None:
    with pytest.raises(fr.FieldRecordError, match="Save the plan"):
        fr.sidecar_paths(None)


def test_missing_file_is_an_empty_list(plan) -> None:
    res = fr.load_records(plan)
    assert res.records == [] and not res.damaged and not res.warnings


def test_add_record_with_photo_roundtrip(plan) -> None:
    rec = fr.add_record(
        plan,
        {"plant_name": "Apple", "age": "3 years", "notes": "grafted", "lat": 48.1, "lon": 11.5,
         "accuracy_m": 3.5, "bearing_deg": 360, "fix_mode": "gps"},
        _png(),
    )
    assert rec.photo == f"photos/{rec.id}.png" and rec.bearing_deg == 0.0
    loaded = fr.load_records(plan)
    assert [r.to_dict() for r in loaded.records] == [rec.to_dict()]
    assert fr.read_photo(plan, loaded.records[0]) == _png()
    assert (plan.with_name("garden.fieldnotes") / rec.photo).is_file()
    data = json.loads(plan.with_name("garden.fieldnotes.json").read_text("utf-8"))
    assert data["version"] == fr.FIELD_RECORDS_VERSION


def test_no_temp_files_left_behind(plan) -> None:
    fr.add_record(plan, {"plant_name": "x"}, _png())
    names = [p.name for p in plan.parent.rglob("*")]
    assert not any(n.endswith(".tmp") for n in names)


@pytest.mark.parametrize(
    "bad",
    [
        {"lat": 10.0},  # lon missing
        {"lat": 100.0, "lon": 0.0},
        {"lat": float("nan"), "lon": 1.0},
        {"bearing_deg": -1},
        {"bearing_deg": 361},
        {"accuracy_m": -2},
        {"fix_mode": "telepathy"},
        {"notes": "x" * (fr.MAX_TEXT + 1)},
        {"plant_name": 5},
        {"timestamp": "x"},
    ],
)
def test_invalid_records_refused_and_nothing_written(plan, bad) -> None:
    with pytest.raises(fr.FieldRecordError):
        fr.add_record(plan, bad, _png())
    assert not plan.with_name("garden.fieldnotes.json").exists()
    assert not plan.with_name("garden.fieldnotes").exists()


def test_photo_must_be_a_real_image_and_capped(plan, monkeypatch) -> None:
    with pytest.raises(fr.FieldRecordError, match="not a supported image"):
        fr.add_record(plan, {}, b"MZ not an image")
    with pytest.raises(fr.FieldRecordError, match="empty"):
        fr.add_record(plan, {}, b"")
    monkeypatch.setattr(fr, "MAX_PHOTO_BYTES", 10)
    with pytest.raises(fr.FieldRecordError, match="larger"):
        fr.add_record(plan, {}, _png())
    assert fr.load_records(plan).records == []


def test_gif_is_refused_because_only_listed_formats_are_stored(plan) -> None:
    buf = io.BytesIO()
    Image.new("P", (2, 2)).save(buf, "GIF")
    with pytest.raises(fr.FieldRecordError, match="not supported"):
        fr.add_record(plan, {}, buf.getvalue())


def test_client_cannot_choose_the_photo_path(plan) -> None:
    for evil in ("../../evil.png", "photos/../x.png", "/etc/passwd", "photos/a b.png", "C:/x.png"):
        with pytest.raises(fr.FieldRecordError, match="photo must be"):
            fr.FieldRecord.from_dict({"id": "a", "photo": evil})
    rec = fr.add_record(plan, {"photo": "../../evil.png"}, _png())  # ignored, store names it
    assert rec.photo == f"photos/{rec.id}.png"


def test_hostile_ids_refused() -> None:
    for evil in ("../x", "a/b", "", "x" * 65, "a b", 7):
        with pytest.raises(fr.FieldRecordError):
            fr.FieldRecord.from_dict({"id": evil})


def test_resolve_photo_traversal_and_missing(plan) -> None:
    rec = fr.add_record(plan, {}, _png())
    assert fr.resolve_photo(plan, rec).is_file()
    fr.resolve_photo(plan, rec).unlink()
    with pytest.raises(fr.FieldRecordError, match="missing"):
        fr.resolve_photo(plan, rec)
    with pytest.raises(fr.FieldRecordError, match="no photo"):
        fr.resolve_photo(plan, fr.FieldRecord(id="z"))
    forged = fr.FieldRecord(id="z", photo="photos/../../x.png")
    with pytest.raises(fr.FieldRecordError):
        fr.resolve_photo(plan, forged)


def test_corrupt_file_is_tolerated_then_set_aside_on_write(plan) -> None:
    side = plan.with_name("garden.fieldnotes.json")
    side.write_text("{ not json", "utf-8")
    res = fr.load_records(plan)
    assert res.records == [] and res.damaged and "corrupt" in res.warnings[0]
    rec = fr.add_record(plan, {"plant_name": "ok"})
    assert [r.id for r in fr.load_records(plan).records] == [rec.id]
    assert list(plan.parent.glob("garden.fieldnotes.json.corrupt-*"))


def test_binary_and_empty_and_wrong_shape_files(plan) -> None:
    side = plan.with_name("garden.fieldnotes.json")
    for content in (b"", b"\xff\xfe\x00bad", b"[1,2]", b'{"records": 5}', b'{"version": true}'):
        side.write_bytes(content)
        res = fr.load_records(plan)
        assert res.records == []


def test_bad_entries_skipped_good_kept(plan) -> None:
    side = plan.with_name("garden.fieldnotes.json")
    side.write_text(
        json.dumps({"version": 1, "records": [
            {"id": "a", "plant_name": "Pear"},
            {"id": "a", "plant_name": "dup"},
            {"id": "../b"},
            "junk",
            {"id": "c", "lat": 500, "lon": 1},
        ]}),
        "utf-8",
    )
    res = fr.load_records(plan)
    assert [r.id for r in res.records] == ["a"] and len(res.warnings) == 4 and not res.damaged


def test_newer_version_is_read_but_never_overwritten(plan) -> None:
    side = plan.with_name("garden.fieldnotes.json")
    side.write_text(json.dumps({"version": 99, "records": [{"id": "a"}]}), "utf-8")
    res = fr.load_records(plan)
    assert [r.id for r in res.records] == ["a"] and res.newer_version
    with pytest.raises(fr.FieldRecordError, match="newer"):
        fr.add_record(plan, {})
    assert json.loads(side.read_text("utf-8"))["version"] == 99


def test_record_failure_removes_the_photo(plan, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise fr.FieldRecordError("disk full")

    monkeypatch.setattr(fr, "save_records", boom)
    with pytest.raises(fr.FieldRecordError, match="disk full"):
        fr.add_record(plan, {}, _png())
    root = plan.with_name("garden.fieldnotes") / "photos"
    assert not list(root.glob("*"))


def test_update_record_links_placed_object(plan) -> None:
    rec = fr.add_record(plan, {"plant_name": "Plum"})
    out = fr.update_record(plan, rec.id, placed_object_id="abc-123")
    assert out.placed_object_id == "abc-123"
    assert fr.get_record(plan, rec.id).placed_object_id == "abc-123"
    with pytest.raises(fr.FieldRecordError):
        fr.update_record(plan, rec.id, photo="photos/x.png")
    with pytest.raises(fr.FieldRecordError, match="no field record"):
        fr.update_record(plan, "nope", notes="x")
    with pytest.raises(fr.FieldRecordError):
        fr.update_record(plan, rec.id, bearing_deg=999)
    assert fr.get_record(plan, rec.id).bearing_deg is None


def test_concurrent_adds_lose_nothing(plan) -> None:
    errors: list[Exception] = []

    def work() -> None:
        try:
            for _ in range(5):
                fr.add_record(plan, {"plant_name": "p"})
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(fr.load_records(plan).records) == 20


def test_record_cap(plan, monkeypatch) -> None:
    monkeypatch.setattr(fr, "MAX_RECORDS", 2)
    fr.add_record(plan, {})
    fr.add_record(plan, {})
    with pytest.raises(fr.FieldRecordError, match="at most"):
        fr.add_record(plan, {})


class TestDataPreservation:
    def test_unreadable_file_is_not_treated_as_empty_or_overwritten(self, plan, monkeypatch) -> None:
        side = plan.with_name("garden.fieldnotes.json")
        good = json.dumps({"version": 1, "records": [{"id": "a", "plant_name": "Pear"}]})
        side.write_text(good, "utf-8")
        real = type(side).read_bytes

        def deny(self):
            if self.name.endswith(".fieldnotes.json"):
                raise PermissionError(13, "Permission denied")
            return real(self)

        monkeypatch.setattr(type(side), "read_bytes", deny)
        res = fr.load_records(plan)
        assert res.unreadable and not res.damaged and res.records == []
        with pytest.raises(fr.FieldRecordError, match="could not be read"):
            fr.add_record(plan, {"plant_name": "x"})
        with pytest.raises(fr.FieldRecordError, match="could not be read"):
            fr.save_records(plan, [])
        monkeypatch.undo()
        assert side.read_text("utf-8") == good  # untouched, no .corrupt file
        assert not list(plan.parent.glob("*.corrupt-*"))

    def test_rejected_entries_unknown_keys_and_overflow_survive_a_write(self, plan, monkeypatch) -> None:
        monkeypatch.setattr(fr, "MAX_RECORDS", 2)
        side = plan.with_name("garden.fieldnotes.json")
        bad = {"id": "../evil", "keep": "me"}
        side.write_text(
            json.dumps({
                "version": 1,
                "remote": {"device": "phone-1"},
                "records": [
                    {"id": "a", "plant_name": "Pear", "future_field": [1, 2]},
                    bad,
                    {"id": "a"},
                    {"id": "b"},
                    {"id": "c", "lat": 1},
                ],
            }),
            "utf-8",
        )
        fr.update_record(plan, "a", notes="changed")
        data = json.loads(side.read_text("utf-8"))
        assert data["remote"] == {"device": "phone-1"}
        by_id = [r.get("id") for r in data["records"]]
        assert by_id[:2] == ["a", "b"] and "../evil" in by_id and by_id.count("a") == 2 and "c" in by_id
        a = data["records"][0]
        assert a["future_field"] == [1, 2] and a["notes"] == "changed"
        assert bad in data["records"]
        assert len(data["records"]) == 5

    def test_aside_name_is_unique_within_a_second(self, plan) -> None:
        side = plan.with_name("garden.fieldnotes.json")
        for _ in range(3):
            side.write_text("{ broken", "utf-8")
            fr.add_record(plan, {})
        assert len(list(plan.parent.glob("garden.fieldnotes.json.corrupt-*"))) == 3

    def test_agent_data_cannot_smuggle_unknown_keys(self, plan) -> None:
        rec = fr.add_record(plan, {"plant_name": "x", "evil": "y"})
        assert rec.extra == {}
        assert "evil" not in plan.with_name("garden.fieldnotes.json").read_text("utf-8")
