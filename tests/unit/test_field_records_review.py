"""Review fixes for field records v2 (ADR-080 addendum): ownership, caps, lat/lon, kind rules,
status transitions, groups, idempotency, storage errors and photo/file budgets."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from open_garden_planner.services import field_records as fr

KEY = "abcdef0123456789"
PHONE = {"source": "field_remote"}
V1 = {
    "version": 1,
    "records": [
        {"id": "old1", "plant_name": "Pear", "timestamp": "2026-01-01T00:00:00Z"},
        {"id": "old2", "plant_name": "Plum", "timestamp": "2026-01-02T00:00:00Z", "placed_object_id": "obj-9"},
    ],
}


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def plan(tmp_path: Path) -> Path:
    return tmp_path / "garden.ogp"


def _write(plan: Path, document: Any) -> None:
    fr.sidecar_paths(plan)[0].write_text(json.dumps(document), encoding="utf-8")


def _point(lat: float = 48.0) -> dict[str, Any]:
    return {"type": "point", "lat": lat, "lon": 11.0, "x_cm": 1.0, "y_cm": 2.0}


def _request(plan: Path, group: str, n: int, **extra: Any) -> fr.FieldRecord:
    return fr.add_record(
        plan,
        {"kind": "placement_request", "what": "apple", "group_id": group, "geometry": _point(48.0 + n / 1e5), **extra},
    )


def test_the_phone_cannot_touch_records_it_did_not_create(plan: Path) -> None:
    _write(plan, V1)  # migrated v1 records are pending but are not the phone's
    agent = fr.add_record(plan, {"notes": "agent", "source": "agent"}, _png())
    desktop = fr.add_record(plan, {"notes": "desktop", "source": "desktop"})
    unsourced = fr.add_record(plan, {"notes": "none"})
    for rid in ("old1", agent.id, desktop.id, unsourced.id):
        with pytest.raises(fr.RecordNotFoundError):
            fr.edit_pending_record(plan, rid, {"notes": "hacked"})
        with pytest.raises(fr.RecordNotFoundError):
            fr.delete_pending_record(plan, rid)
    assert fr.resolve_photo(plan, fr.get_record(plan, agent.id)).exists()
    assert {r.notes for r in fr.load_records(plan).records} >= {"agent", "desktop", "none"}


def test_legacy_pending_records_do_not_count_against_the_phone_cap(plan: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr, "MAX_PENDING", 2)
    _write(plan, {"version": 1, "records": [{"id": f"old{n}", "notes": "x"} for n in range(5)]})
    fr.add_record(plan, {"notes": "a", **PHONE})
    fr.add_record(plan, {"notes": "agent", "source": "agent"})
    fr.add_record(plan, {"notes": "b", **PHONE})
    with pytest.raises(fr.RecordCapError):
        fr.add_record(plan, {"notes": "c", **PHONE})


def test_lat_lon_always_follow_the_point_geometry(plan: Path) -> None:
    rec = fr.add_record(plan, {"kind": "note", "geometry": _point(48.0), **PHONE})
    moved = fr.edit_pending_record(plan, rec.id, {"geometry": _point(48.5)})
    assert (moved.lat, moved.lon) == (48.5, 11.0)
    assert fr.get_record(plan, rec.id).lat == 48.5
    with pytest.raises(fr.FieldRecordError, match="disagree"):
        fr.add_record(plan, {"kind": "note", "geometry": _point(48.0), "lat": 10.0, "lon": 11.0})
    assert fr.add_record(plan, {"kind": "note", "geometry": _point(48.0), "lat": 48.0, "lon": 11.0}).lat == 48.0


def test_patch_enforces_the_kind_rules_on_the_merged_record(plan: Path) -> None:
    line = {"type": "polyline", "points": [[48.0, 11.0], [48.0001, 11.0]]}
    irrigation = fr.add_record(plan, {"kind": "irrigation_path", "geometry": line, **PHONE})
    with pytest.raises(fr.FieldRecordError, match="polyline"):
        fr.edit_pending_record(plan, irrigation.id, {"geometry": _point()})
    ref = fr.add_record(plan, {"kind": "reference_point", "geometry": _point(), "name": "corner", **PHONE})
    with pytest.raises(fr.FieldRecordError, match="polyline"):
        fr.edit_pending_record(plan, ref.id, {"geometry": line})
    note = fr.add_record(plan, {"kind": "note", **PHONE})
    with pytest.raises(fr.FieldRecordError, match="only an irrigation_path"):
        fr.edit_pending_record(plan, note.id, {"geometry": line})
    assert fr.get_record(plan, irrigation.id).geometry["type"] == "polyline"  # type: ignore[index]


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "note", "group_id": "g1"},  # only placement requests have groups
        {"kind": "placement_request", "what": "a", "geometry": _point(), "group_id": "has space"},
        {"kind": "placement_request", "what": "a", "geometry": _point(), "group_id": "x" * 65},
        {"kind": "reference_point", "geometry": _point()},
        {"kind": "placement_request", "geometry": _point(), "group_id": "g1"},
    ],
)
def test_kind_and_group_rules_are_refused_on_create(plan: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(fr.FieldRecordError):
        fr.add_record(plan, bad)
    assert fr.load_records(plan).records == []


def test_a_foreign_group_id_cannot_sweep_other_sources(plan: Path) -> None:
    mine = _request(plan, "shared", 0, source="agent")
    phone = _request(plan, "shared", 1, **PHONE)
    placed, superseded = fr.approve_candidate(plan, mine.id, "obj-1")
    assert placed.status == "placed" and superseded == []
    assert fr.get_record(plan, phone.id).status == "pending"
    assert fr.reopen_group(plan, "shared", source="field_remote") == []  # the phone's pending one is untouched
    assert fr.get_record(plan, mine.id).status == "placed"


def test_approve_candidate_places_and_supersedes_in_one_write(plan: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b, c = (_request(plan, "g1", n) for n in range(3))
    writes: list[int] = []
    real = fr.save_records

    def counting(*args: Any, **kwargs: Any) -> None:
        writes.append(1)
        real(*args, **kwargs)

    monkeypatch.setattr(fr, "save_records", counting)
    placed, superseded = fr.approve_candidate(plan, a.id, "obj-1")
    assert len(writes) == 1 and placed.placed_object_id == "obj-1" and {r.id for r in superseded} == {b.id, c.id}
    now = {r.id: r.status for r in fr.load_records(plan).records}
    assert now == {a.id: "placed", b.id: "superseded", c.id: "superseded"}
    with pytest.raises(fr.FieldRecordError):
        fr.approve_candidate(plan, b.id, "obj-2")  # a superseded candidate cannot be placed directly
    with pytest.raises(fr.FieldRecordError):
        fr.approve_candidate(plan, a.id, "")


def test_status_transitions_and_the_placed_link_stay_consistent(plan: Path) -> None:
    rec = fr.add_record(plan, {"notes": "x"})
    with pytest.raises(fr.FieldRecordError, match="placed_object_id"):
        fr.update_record(plan, rec.id, status="placed")  # placed without an object
    with pytest.raises(fr.FieldRecordError, match="only a placed"):
        fr.update_record(plan, rec.id, status="approved", placed_object_id="obj-1")
    assert fr.update_record(plan, rec.id, placed_object_id="obj-1").status == "placed"  # a link places it
    with pytest.raises(fr.FieldRecordError, match="cannot become"):
        fr.update_record(plan, rec.id, status="rejected")
    back = fr.update_record(plan, rec.id, status="pending")  # reopening drops the dangling link
    assert back.placed_object_id is None and fr.get_record(plan, rec.id).status == "pending"
    fr.update_record(plan, rec.id, status="rejected")
    with pytest.raises(fr.FieldRecordError, match="cannot become"):
        fr.update_record(plan, rec.id, status="placed", placed_object_id="obj-2")


def test_a_reused_key_with_other_content_is_a_conflict(plan: Path) -> None:
    fr.create_record(plan, {"notes": "a", "idempotency_key": KEY})
    with pytest.raises(fr.RecordConflictError):
        fr.create_record(plan, {"notes": "different", "idempotency_key": KEY})
    with pytest.raises(fr.RecordConflictError):  # a retry that adds a photo is not a silent no-op
        fr.create_record(plan, {"notes": "a", "idempotency_key": KEY}, _png())
    assert len(fr.load_records(plan).records) == 1


def test_a_retry_after_withdrawal_does_not_recreate_the_record(plan: Path) -> None:
    body = {"notes": "a", "idempotency_key": KEY, **PHONE}
    rec, _ = fr.create_record(plan, body)
    fr.delete_pending_record(plan, rec.id)
    with pytest.raises(fr.RecordGoneError):
        fr.create_record(plan, body)
    assert fr.load_records(plan).records == []
    _, created = fr.create_record(plan, {**body, "idempotency_key": "another-key-77"})
    assert created
    assert json.loads(fr.sidecar_paths(plan)[0].read_text(encoding="utf-8"))["withdrawn_keys"] == [KEY]


def test_storage_errors_name_no_path(plan: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(plan, V1)
    original = Path.read_bytes

    def boom(self: Path) -> bytes:
        if self.name.endswith(".fieldnotes.json"):
            raise PermissionError(13, f"denied {self}")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", boom)
    for call in (
        lambda: fr.create_record(plan, {"notes": "n"}),
        lambda: fr.update_record(plan, "old1", notes="n"),
        lambda: fr.delete_pending_record(plan, "old1"),
    ):
        with pytest.raises(fr.RecordStorageError) as raised:
            call()
        assert str(plan.parent) not in str(raised.value) and "denied" not in str(raised.value)


def test_a_photo_that_unpacks_to_a_huge_image_is_refused(plan: Path) -> None:
    buf = io.BytesIO()
    Image.new("1", (12000, 9000)).save(buf, "PNG")  # ~108 megapixels, a few KB on disk
    assert len(buf.getvalue()) < 1_000_000
    with pytest.raises(fr.FieldRecordError, match="megapixels"):
        fr.add_record(plan, {"notes": "bomb"}, buf.getvalue())
    assert fr.load_records(plan).records == []


def test_photo_and_file_budgets(plan: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr, "MAX_PHOTO_TOTAL_BYTES", len(_png()) * 2 + 1)
    fr.add_record(plan, {"notes": "1"}, _png())
    fr.add_record(plan, {"notes": "2"}, _png())
    with pytest.raises(fr.RecordCapError, match="photo storage"):
        fr.add_record(plan, {"notes": "3"}, _png())
    monkeypatch.setattr(fr, "MAX_SIDECAR_BYTES", fr.sidecar_paths(plan)[0].stat().st_size + 50)
    with pytest.raises(fr.RecordCapError, match="full"):
        fr.add_record(plan, {"notes": "x" * 500})
    assert len(fr.load_records(plan).records) == 2
