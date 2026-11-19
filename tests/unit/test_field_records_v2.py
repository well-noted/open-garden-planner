"""Schema v2 of the field-record sidecar (design 1.9, ADR-082): kind/status/geometry, migration
from v1, idempotency, groups, caps, and the P3 hardening that must survive the change."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from open_garden_planner.services import field_records as fr

KEY = "abcdef0123456789"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def plan(tmp_path: Path) -> Path:
    return tmp_path / "garden.ogp"


def _write(plan: Path, document: Any) -> None:
    fr.sidecar_paths(plan)[0].write_text(json.dumps(document), encoding="utf-8")


def _file(plan: Path) -> dict[str, Any]:
    return json.loads(fr.sidecar_paths(plan)[0].read_text(encoding="utf-8"))


def _point(lat: float = 48.0, lon: float = 11.0) -> dict[str, Any]:
    return {"type": "point", "lat": lat, "lon": lon, "x_cm": 100.0, "y_cm": 200.0}


def _candidate(plan: Path, group: str, n: int) -> fr.FieldRecord:
    return fr.add_record(
        plan,
        {"kind": "placement_request", "what": "apple", "group_id": group, "geometry": _point(48.0 + n / 1e5),
         "idempotency_key": f"cand-{group}-{n:04d}"},
    )


# -- v1 compatibility -------------------------------------------------------------------------
V1 = {
    "version": 1,
    "top_level_unknown": {"keep": True},
    "records": [
        {"id": "old1", "plant_name": "Pear", "lat": 48.1, "lon": 11.5, "timestamp": "2026-01-01T00:00:00Z",
         "future_field": [1, 2]},
        {"id": "old2", "plant_name": "Plum", "timestamp": "2026-01-02T00:00:00Z", "placed_object_id": "obj-9"},
        {"id": "../evil", "keep": "me"},
    ],
}


def test_v1_file_loads_unchanged_and_is_migrated_in_memory(plan: Path) -> None:
    _write(plan, V1)
    loaded = fr.load_records(plan)
    by_id = {r.id: r for r in loaded.records}
    assert set(by_id) == {"old1", "old2"} and not loaded.damaged
    assert by_id["old1"].kind == "plant" and by_id["old1"].status == "pending"
    assert by_id["old2"].status == "placed"  # it already had a plan object
    assert by_id["old1"].plant_name == "Pear" and by_id["old1"].lat == 48.1
    assert by_id["old1"].extra == {"future_field": [1, 2]}
    assert loaded.rejected == [V1["records"][2]]  # kept exactly as written, not migrated


def test_v1_file_round_trips_through_a_write_losing_nothing(plan: Path) -> None:
    _write(plan, V1)
    fr.update_record(plan, "old1", notes="checked")
    data = _file(plan)
    assert data["version"] == fr.FIELD_RECORDS_VERSION and data["top_level_unknown"] == {"keep": True}
    by_id = {r["id"]: r for r in data["records"]}
    assert by_id["old1"]["future_field"] == [1, 2] and by_id["old1"]["notes"] == "checked"
    assert by_id["old2"]["placed_object_id"] == "obj-9" and by_id["old2"]["status"] == "placed"
    assert by_id["../evil"] == {"id": "../evil", "keep": "me"}
    again = fr.load_records(plan)  # and the written file loads cleanly as v2
    assert [r.id for r in again.records] == ["old1", "old2"] and not again.warnings[:0]


def test_newer_version_is_still_never_overwritten(plan: Path) -> None:
    _write(plan, {"version": 3, "records": [{"id": "x", "kind": "plant"}]})
    assert fr.load_records(plan).newer_version
    with pytest.raises(fr.FieldRecordError, match="newer"):
        fr.add_record(plan, {"notes": "n"})
    with pytest.raises(fr.FieldRecordError, match="newer"):
        fr.update_record(plan, "x", notes="n")
    assert _file(plan)["version"] == 3


def test_unreadable_file_is_never_written_over(plan: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(plan, V1)
    original = Path.read_bytes

    def boom(self: Path) -> bytes:
        if self.name.endswith(".fieldnotes.json"):
            raise PermissionError(13, "denied")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", boom)
    for call in (
        lambda: fr.create_record(plan, {"notes": "n"}),
        lambda: fr.update_record(plan, "old1", notes="n"),
        lambda: fr.delete_pending_record(plan, "old1"),
        lambda: fr.supersede_siblings(plan, "old1"),
        lambda: fr.reopen_group(plan, "g"),
    ):
        with pytest.raises(fr.FieldRecordError):
            call()
    monkeypatch.undo()
    assert _file(plan)["records"][0]["id"] == "old1"


def test_corrupt_file_is_set_aside_not_overwritten(plan: Path) -> None:
    fr.sidecar_paths(plan)[0].write_text("{not json", encoding="utf-8")
    assert fr.load_records(plan).damaged
    fr.add_record(plan, {"notes": "fresh"})
    assert list(plan.parent.glob("*.corrupt-*"))
    assert len(_file(plan)["records"]) == 1


# -- validation -------------------------------------------------------------------------------
def test_defaults_for_a_new_record(plan: Path) -> None:
    rec = fr.add_record(plan, {"notes": "n"})
    assert (rec.kind, rec.status, rec.geometry) == ("plant", "pending", None)


def test_point_geometry_fills_lat_lon(plan: Path) -> None:
    rec = fr.add_record(plan, {"kind": "note", "geometry": _point()})
    assert (rec.lat, rec.lon) == (48.0, 11.0) and rec.geometry == _point()


def test_polyline_geometry_and_irrigation_fields(plan: Path) -> None:
    line = {"type": "polyline", "points": [[48.0, 11.0, 0, 0], [48.0001, 11.0001]]}
    rec = fr.add_record(
        plan, {"kind": "irrigation_path", "geometry": line, "diameter_mm": 16, "closes_on": True, "flow_lph": 2}
    )
    assert rec.geometry == {"type": "polyline", "points": [[48.0, 11.0, 0.0, 0.0], [48.0001, 11.0001, None, None]]}
    assert fr.load_records(plan).records[0].closes_on is True


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "tree"},
        {"status": "done"},
        {"idempotency_key": "short"},
        {"geometry": {"type": "circle"}},
        {"geometry": {"type": "point", "lat": 99, "lon": 0}},
        {"geometry": {"type": "polyline", "points": [[1, 1]]}},
        {"geometry": {"type": "polyline", "points": [[1, 1]] * (fr.MAX_POLYLINE_POINTS + 1)}},
        {"geometry": {"type": "point", "lat": 1, "lon": 1, "x_cm": 5}},
        {"source": "somebody"},
        {"precision_hint": "vibes"},
        {"closes_on": "yes"},
        {"distances": [1.0] * (fr.MAX_DISTANCES + 1)},
        {"diameter_mm": float("nan")},
        {"fix_mode": "telepathy"},
    ],
)
def test_invalid_v2_fields_are_refused(plan: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(fr.FieldRecordError):
        fr.add_record(plan, bad)
    assert fr.load_records(plan).records == []


def test_polyline_at_the_vertex_cap_is_accepted(plan: Path) -> None:
    pts = [[48.0 + i / 1e6, 11.0] for i in range(fr.MAX_POLYLINE_POINTS)]
    rec = fr.add_record(plan, {"kind": "irrigation_path", "geometry": {"type": "polyline", "points": pts}})
    assert len(rec.geometry["points"]) == fr.MAX_POLYLINE_POINTS  # type: ignore[index]


def test_new_fix_modes_are_accepted(plan: Path) -> None:
    for mode in ("rtk_fixed", "vio_fused", "anchor_calibrated", "standard"):
        assert fr.add_record(plan, {"fix_mode": mode}).fix_mode == mode


# -- idempotency ------------------------------------------------------------------------------
def test_a_retry_with_the_same_key_returns_the_original(plan: Path) -> None:
    first, created = fr.create_record(plan, {"notes": "a", "idempotency_key": KEY}, _png())
    again, created_again = fr.create_record(plan, {"notes": "different", "idempotency_key": KEY})
    assert created and not created_again and again.id == first.id and again.notes == "a"
    assert len(fr.load_records(plan).records) == 1
    assert len(list((fr.sidecar_paths(plan)[1] / "photos").iterdir())) == 1
    found = fr.find_by_idempotency_key(plan, KEY)
    assert found is not None and found.id == first.id
    assert fr.find_by_idempotency_key(plan, "no-such-key-1") is None


def test_the_key_cannot_be_changed_later(plan: Path) -> None:
    rec = fr.add_record(plan, {"idempotency_key": KEY})
    with pytest.raises(fr.FieldRecordError):
        fr.update_record(plan, rec.id, idempotency_key="another-key-99")


def test_racing_retries_of_one_key_create_one_record(plan: Path) -> None:
    results: list[bool] = []

    def worker() -> None:
        results.append(fr.create_record(plan, {"notes": "x", "idempotency_key": KEY})[1])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and len(fr.load_records(plan).records) == 1


# -- status, groups ---------------------------------------------------------------------------
def test_status_changes_go_through_update_record(plan: Path) -> None:
    rec = fr.add_record(plan, {"notes": "n"})
    done = fr.update_record(plan, rec.id, status="rejected")
    assert done.status == "rejected" and fr.get_record(plan, rec.id).status == "rejected"
    with pytest.raises(fr.FieldRecordError):
        fr.update_record(plan, rec.id, status="bogus")


def test_approving_one_candidate_supersedes_the_open_siblings_only(plan: Path) -> None:
    a, b, c = (_candidate(plan, "g1", n) for n in range(3))
    other = _candidate(plan, "g2", 0)
    fr.update_record(plan, c.id, status="rejected")
    fr.update_record(plan, a.id, status="placed", placed_object_id="obj-1")
    changed = fr.supersede_siblings(plan, a.id)
    assert [r.id for r in changed] == [b.id]
    status = {r.id: r.status for r in fr.load_records(plan).records}
    assert status == {a.id: "placed", b.id: "superseded", c.id: "rejected", other.id: "pending"}


def test_reopen_returns_the_group_to_pending_and_clears_the_link(plan: Path) -> None:
    a, b, c = (_candidate(plan, "g1", n) for n in range(3))
    fr.update_record(plan, c.id, status="rejected")
    fr.update_record(plan, a.id, status="placed", placed_object_id="obj-1")
    fr.supersede_siblings(plan, a.id)
    reopened = fr.reopen_group(plan, "g1")
    assert {r.id for r in reopened} == {a.id, b.id}
    now = {r.id: r for r in fr.load_records(plan).records}
    assert now[a.id].status == now[b.id].status == "pending" and now[a.id].placed_object_id is None
    assert now[c.id].status == "rejected"
    fr.reopen_group(plan, "g1", include_rejected=True)
    assert fr.get_record(plan, c.id).status == "pending"


def test_a_record_without_a_group_has_no_siblings(plan: Path) -> None:
    solo = fr.add_record(plan, {"notes": "x"})
    fr.add_record(plan, {"notes": "y"})
    assert fr.supersede_siblings(plan, solo.id) == []
    with pytest.raises(fr.RecordNotFoundError):
        fr.supersede_siblings(plan, "missing")


# -- phone edit / withdraw --------------------------------------------------------------------
def test_phone_edit_only_while_pending_and_only_allowed_fields(plan: Path) -> None:
    rec = fr.add_record(plan, {"notes": "a"})
    assert fr.edit_pending_record(plan, rec.id, {"notes": "b"}).notes == "b"
    for forbidden in ({"status": "placed"}, {"placed_object_id": "x"}, {"source": "agent"}, {"photo": "photos/x.png"}):
        with pytest.raises(fr.FieldRecordError):
            fr.edit_pending_record(plan, rec.id, forbidden)
    fr.update_record(plan, rec.id, status="placed")
    with pytest.raises(fr.RecordNotPendingError):
        fr.edit_pending_record(plan, rec.id, {"notes": "c"})
    with pytest.raises(fr.RecordNotFoundError):
        fr.edit_pending_record(plan, "missing", {"notes": "c"})


def test_withdraw_removes_the_record_and_its_photo_only_while_pending(plan: Path) -> None:
    rec = fr.add_record(plan, {"notes": "a"}, _png())
    photo = fr.resolve_photo(plan, rec)
    fr.delete_pending_record(plan, rec.id)
    assert not photo.exists() and fr.load_records(plan).records == []
    kept = fr.add_record(plan, {"notes": "b", "status": "approved"}, _png())
    with pytest.raises(fr.RecordNotPendingError):
        fr.delete_pending_record(plan, kept.id)
    assert fr.resolve_photo(plan, kept).exists()


def test_withdraw_keeps_rejected_and_unknown_data(plan: Path) -> None:
    _write(plan, V1)
    fr.delete_pending_record(plan, "old1")
    data = _file(plan)
    assert [r["id"] for r in data["records"]] == ["old2", "../evil"] and data["top_level_unknown"] == {"keep": True}


# -- caps -------------------------------------------------------------------------------------
def test_pending_cap_stops_a_runaway_phone_but_not_decided_records(plan: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr, "MAX_PENDING", 3)
    made = [fr.add_record(plan, {"notes": str(n)}) for n in range(3)]
    with pytest.raises(fr.RecordCapError, match="review"):
        fr.add_record(plan, {"notes": "4"})
    fr.update_record(plan, made[0].id, status="rejected")
    fr.add_record(plan, {"notes": "4"})  # a decision frees a slot
    with pytest.raises(fr.RecordCapError):  # reopening past the cap is refused too
        fr.update_record(plan, made[0].id, status="pending")


def test_total_record_cap_and_a_failed_add_leaves_no_photo(plan: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr, "MAX_RECORDS", 2)
    fr.add_record(plan, {"notes": "1"})
    fr.add_record(plan, {"notes": "2"})
    with pytest.raises(fr.RecordCapError):
        fr.add_record(plan, {"notes": "3"}, _png())
    photos = fr.sidecar_paths(plan)[1] / "photos"
    assert not photos.exists() or not list(photos.iterdir())


def test_photo_rules_are_unchanged(plan: Path) -> None:
    with pytest.raises(fr.FieldRecordError):
        fr.add_record(plan, {"notes": "x"}, b"not an image")
    assert fr.add_record(plan, {"photo": "../../etc/passwd"}).photo is None  # a client-named photo is ignored
    assert len(fr.load_records(plan).records) == 1


def test_eight_threads_by_fifty_records_lose_none(plan: Path) -> None:
    errors: list[BaseException] = []

    def worker(t: int) -> None:
        try:
            for n in range(50):
                fr.create_record(plan, {"notes": f"{t}-{n}", "kind": "note", "status": "approved"})
        except BaseException as error:  # noqa: BLE001 - collected for the assertion
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    notes = {r.notes for r in fr.load_records(plan).records}
    assert len(notes) == 400
