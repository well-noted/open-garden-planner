"""Field Remote record routes through the real ASGI stack (guard + router) with starlette's
TestClient (ADR-080 addendum). The phone writes only the sidecar; each test names the rule it pins."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from starlette.testclient import TestClient

from open_garden_planner.field_remote import limits, server
from open_garden_planner.field_remote.routes import records_routes
from open_garden_planner.services import field_records as fr

TOKEN = "t" * 43
PORT = 8766
AUTH = {"Authorization": f"Bearer {TOKEN}"}
KEY = "0123456789abcdef"


def _png(size: int = 4) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), "blue").save(buf, "PNG")
    return buf.getvalue()


class _Tick:
    """A clock that moves 1 s per reading, so the write rate limit never trips in a test."""

    now = 1000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


class Env:
    def __init__(self, tmp_path: Path, enabled: bool = True, saved: bool = True) -> None:
        self.plan: str | None = str(tmp_path / "garden.ogp") if saved else None
        self.state = server.ServerState(token=TOKEN, records_enabled=enabled)
        app = server.build_app(
            self.state, "127.0.0.1", PORT, extra_routes=records_routes(lambda: self.plan),
            guard_kwargs={"failure_delay_s": 0.0, "clock": _Tick()},
        )
        self.client = TestClient(app, base_url=f"https://127.0.0.1:{PORT}")

    def post(self, body: dict[str, Any], **kw: Any) -> Any:
        return self.client.post("/api/records", json=body, headers=AUTH, **kw)


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _note(key: str = KEY, **extra: Any) -> dict[str, Any]:
    return {"kind": "note", "idempotency_key": key, "notes": "dead branch", **extra}


def _point(lat: float = 48.0) -> dict[str, Any]:
    return {"type": "point", "lat": lat, "lon": 11.0, "x_cm": 10, "y_cm": 20}


def test_create_stores_a_pending_field_remote_record(env: Env) -> None:
    res = env.post(_note(geometry=_point(), accuracy_m=3.5, fix_mode="gps"))
    assert res.status_code == 201
    rec = res.json()["record"]
    assert res.json()["created"] is True
    assert rec["status"] == "pending" and rec["source"] == "field_remote" and rec["kind"] == "note"
    assert rec["lat"] == 48.0 and rec["geometry"]["x_cm"] == 10.0
    stored = fr.get_record(env.plan, rec["id"])
    assert stored.status == "pending" and stored.source == "field_remote"


def test_replay_returns_the_original_and_adds_nothing(env: Env) -> None:
    first = env.post(_note()).json()["record"]
    again = env.post(_note(notes="changed")).json()
    assert again["created"] is False and again["record"]["id"] == first["id"] and again["record"]["notes"] == "dead branch"
    assert env.client.post("/api/records", json=_note(), headers=AUTH).status_code == 200
    assert len(fr.load_records(env.plan).records) == 1


def test_no_token_or_wrong_token_is_401_and_writes_nothing(env: Env) -> None:
    assert env.client.post("/api/records", json=_note()).status_code == 401
    assert env.client.post("/api/records", json=_note(), headers={"Authorization": "Bearer nope"}).status_code == 401
    assert env.client.get("/api/field-records").status_code == 401
    assert fr.load_records(env.plan).records == []


def test_toggle_off_refuses_every_write_but_not_reads(tmp_path: Path) -> None:
    env = Env(tmp_path, enabled=False)
    for res in (
        env.post(_note()),
        env.client.patch("/api/records/abc", json={"notes": "x"}, headers=AUTH),
        env.client.delete("/api/records/abc", headers=AUTH),
    ):
        assert res.status_code == 403 and res.json()["error"] == "records_disabled"
    assert env.client.get("/api/field-records", headers=AUTH).status_code == 200
    env.state.records_enabled = True  # the toggle applies live
    assert env.post(_note()).status_code == 201


def test_unsaved_plan_is_409_plan_not_saved(tmp_path: Path) -> None:
    env = Env(tmp_path, saved=False)
    for res in (
        env.post(_note()),
        env.client.patch("/api/records/abc", json={"notes": "x"}, headers=AUTH),
        env.client.delete("/api/records/abc", headers=AUTH),
        env.client.get("/api/field-records", headers=AUTH),
    ):
        assert res.status_code == 409 and res.json()["error"] == "plan_not_saved"
    assert not list(tmp_path.iterdir())  # nothing was created anywhere


def test_list_filters_and_reports_limits(env: Env) -> None:
    env.post(_note("key-aaaaaaaa"))
    env.post({"kind": "plant", "idempotency_key": "key-bbbbbbbb", "plant_name": "Apple"})
    rec_id = env.post(_note("key-cccccccc")).json()["record"]["id"]
    fr.update_record(env.plan, rec_id, status="rejected")
    body = env.client.get("/api/field-records?kind=note&status=pending", headers=AUTH).json()
    assert body["total"] == 3 and body["pending"] == 2 and body["returned"] == 1
    assert body["limits"]["max_pending"] == fr.MAX_PENDING
    assert env.client.get("/api/field-records?limit=0", headers=AUTH).status_code == 400
    assert env.client.get("/api/field-records?status=bogus", headers=AUTH).status_code == 400


def test_patch_edits_a_pending_record_only(env: Env) -> None:
    rec_id = env.post(_note(geometry=_point())).json()["record"]["id"]
    res = env.client.patch(f"/api/records/{rec_id}", json={"notes": "moved", "geometry": _point(48.5)}, headers=AUTH)
    assert res.status_code == 200 and res.json()["record"]["notes"] == "moved"
    assert fr.get_record(env.plan, rec_id).geometry["lat"] == 48.5  # type: ignore[index]
    fr.update_record(env.plan, rec_id, status="placed")  # decided on the computer
    res = env.client.patch(f"/api/records/{rec_id}", json={"notes": "late"}, headers=AUTH)
    assert res.status_code == 409 and res.json()["error"] == "not_pending"
    assert fr.get_record(env.plan, rec_id).notes == "moved"
    assert env.client.patch("/api/records/nope", json={"notes": "x"}, headers=AUTH).status_code == 404


@pytest.mark.parametrize("body", [{}, {"status": "placed"}, {"source": "agent"}, {"id": "x"}, {"photo": "p"}, {"notes": None}])
def test_patch_refuses_forbidden_or_empty_changes(env: Env, body: dict[str, Any]) -> None:
    rec_id = env.post(_note()).json()["record"]["id"]
    res = env.client.patch(f"/api/records/{rec_id}", json=body, headers=AUTH)
    assert res.status_code == 422 and res.json()["error"] == "invalid_record"
    assert fr.get_record(env.plan, rec_id).status == "pending"


def test_delete_withdraws_a_pending_record_and_its_photo(env: Env) -> None:
    res = env.client.post(
        "/api/records", headers=AUTH, data={"meta": json.dumps(_note())}, files={"photo": ("p.png", _png(), "image/png")}
    )
    assert res.status_code == 201
    rec = fr.get_record(env.plan, res.json()["record"]["id"])
    photo = fr.resolve_photo(env.plan, rec)
    assert env.client.delete(f"/api/records/{rec.id}", headers=AUTH).json() == {"deleted": rec.id}
    assert not photo.exists() and fr.load_records(env.plan).records == []
    assert env.client.delete(f"/api/records/{rec.id}", headers=AUTH).status_code == 404


def test_delete_refuses_a_decided_record(env: Env) -> None:
    rec_id = env.post(_note()).json()["record"]["id"]
    fr.update_record(env.plan, rec_id, status="approved")
    res = env.client.delete(f"/api/records/{rec_id}", headers=AUTH)
    assert res.status_code == 409 and res.json()["error"] == "not_pending"
    assert len(fr.load_records(env.plan).records) == 1


def test_multipart_photo_is_stored_under_a_store_issued_name_and_served(env: Env) -> None:
    res = env.client.post(
        "/api/records", headers=AUTH, data={"meta": json.dumps(_note(plant_name="Pear", kind="plant"))},
        files={"photo": ("../../evil.exe", _png(), "application/octet-stream")},
    )
    assert res.status_code == 201
    rec = res.json()["record"]
    assert rec["photo"] is None and rec["photo_url"] == f"/api/field-records/{rec['id']}/photo"
    assert fr.get_record(env.plan, rec["id"]).photo == f"photos/{rec['id']}.png"  # sniffed type, store name
    served = env.client.get(rec["photo_url"], headers=AUTH)
    assert served.status_code == 200 and served.headers["content-type"] == "image/png" and served.content == _png()
    assert env.client.get("/api/field-records/missing/photo", headers=AUTH).status_code == 404


def test_a_non_image_upload_is_refused_and_leaves_nothing(env: Env) -> None:
    res = env.client.post(
        "/api/records", headers=AUTH, data={"meta": json.dumps(_note())}, files={"photo": ("a.png", b"MZ not an image", "image/png")}
    )
    assert res.status_code == 422 and res.json()["error"] == "invalid_record"
    assert fr.load_records(env.plan).records == []
    photos = fr.sidecar_paths(env.plan)[1] / "photos"
    assert not photos.exists() or not list(photos.iterdir())


def test_oversize_photo_is_413(env: Env) -> None:
    big = b"\x89PNG" + b"0" * (limits.MAX_PHOTO_BYTES + 10)
    res = env.client.post("/api/records", headers=AUTH, data={"meta": json.dumps(_note())}, files={"photo": ("a.png", big, "image/png")})
    assert res.status_code == 413
    assert fr.load_records(env.plan).records == []


def test_json_body_over_its_cap_is_413_and_patch_keeps_the_small_cap(env: Env) -> None:
    big = json.dumps(_note(notes="n")) + " " * (128 * 1024)  # under the upload cap, over the JSON cap
    res = env.client.post("/api/records", content=big, headers={**AUTH, "content-type": "application/json"})
    assert res.status_code == 413
    res = env.client.patch(
        "/api/records/abc", content="x" * (limits.MAX_BODY_BYTES + 1), headers={**AUTH, "content-type": "application/json"}
    )
    assert res.status_code == 413  # PATCH is not an upload route: the 256 KB guard cap applies
    assert fr.load_records(env.plan).records == []


def test_json_part_over_its_cap_is_413(env: Env) -> None:
    meta = json.dumps(_note(notes="n")) + " " * (128 * 1024)
    res = env.client.post("/api/records", headers=AUTH, data={"meta": meta}, files={"photo": ("a.png", _png(), "image/png")})
    assert res.status_code == 413


@pytest.mark.parametrize(
    "body",
    [
        {"idempotency_key": KEY},  # no kind
        {"kind": "note"},  # no key
        {"kind": "tree", "idempotency_key": KEY},
        {"kind": "note", "idempotency_key": "short"},
        {"kind": "note", "idempotency_key": KEY, "status": "placed"},
        {"kind": "note", "idempotency_key": KEY, "source": "agent"},
        {"kind": "note", "idempotency_key": KEY, "id": "chosen"},
        {"kind": "note", "idempotency_key": KEY, "photo": "photos/x.png"},
        {"kind": "note", "idempotency_key": KEY, "placed_object_id": "o"},
        {"kind": "note", "idempotency_key": KEY, "lat": "48"},
        {"kind": "note", "idempotency_key": KEY, "accuracy_m": True},
        {"kind": "note", "idempotency_key": KEY, "geometry": {"type": "point", "lat": 95, "lon": 0}},
        {"kind": "note", "idempotency_key": KEY, "geometry": {"type": "polyline", "points": [[1, 1]]}},
        {"kind": "note", "idempotency_key": KEY, "geometry": {"type": "polyline", "points": [[1, 1]] * (fr.MAX_POLYLINE_POINTS + 1)}},
        {"kind": "note", "idempotency_key": KEY, "notes": "n" * (fr.MAX_TEXT + 1)},
        {"kind": "irrigation_path", "idempotency_key": KEY},  # needs a polyline
        {"kind": "irrigation_path", "idempotency_key": KEY, "geometry": _point()},
        {"kind": "reference_point", "idempotency_key": KEY, "geometry": _point()},  # needs a name
        {"kind": "placement_request", "idempotency_key": KEY, "geometry": _point(), "what": "apple"},  # needs group_id
        {"kind": "note", "idempotency_key": KEY, "fix_mode": "telepathy"},
    ],
)
def test_strict_validation_refuses_bad_bodies_without_echoing_input(env: Env, body: dict[str, Any]) -> None:
    res = env.post(body)
    assert res.status_code == 422 and res.json()["error"] == "invalid_record"
    assert "n" * 100 not in res.text and TOKEN not in res.text
    assert fr.load_records(env.plan).records == []


def test_malformed_json_and_wrong_media_type(env: Env) -> None:
    res = env.client.post("/api/records", content=b"{not json", headers={**AUTH, "content-type": "application/json"})
    assert res.status_code == 422
    assert env.client.post("/api/records", content=b"x", headers={**AUTH, "content-type": "text/plain"}).status_code == 415
    res = env.client.post("/api/records", headers=AUTH, data={"other": "x"}, files={"photo": ("a.png", _png(), "image/png")})
    assert res.status_code == 400


def test_each_kind_can_be_written_with_its_geometry(env: Env) -> None:
    line = {"type": "polyline", "points": [[48.0, 11.0, 0, 0], [48.0001, 11.0001, 5, 5]]}
    bodies = [
        {"kind": "irrigation_path", "idempotency_key": "key-0000001", "geometry": line, "diameter_mm": 16, "closes_on": False},
        {"kind": "reference_point", "idempotency_key": "key-0000002", "geometry": _point(), "name": "SW corner"},
        {"kind": "measurement", "idempotency_key": "key-0000003", "distances": [1.5, {"to": "A", "m": 2.0}], "rms": 0.2},
        {"kind": "plant", "idempotency_key": "key-0000004", "plant_name": "Plum", "geometry": _point()},
        {"kind": "placement_request", "idempotency_key": "key-0000005", "what": "apple", "group_id": "g-1", "geometry": _point(),
         "precision_hint": "gps", "requester": "Sam"},
        {"kind": "placement_request", "idempotency_key": "key-0000006", "what": "apple", "group_id": "g-1", "geometry": _point(48.1)},
    ]
    for body in bodies:
        assert env.post(body).status_code == 201, body
    recs = fr.load_records(env.plan).records
    assert {r.kind for r in recs} == {"irrigation_path", "reference_point", "measurement", "plant", "placement_request"}
    assert {r.group_id for r in recs if r.kind == "placement_request"} == {"g-1"}


def test_pending_cap_is_409_record_limit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr, "MAX_PENDING", 2)
    assert env.post(_note("key-aaaaaaaa")).status_code == 201
    assert env.post(_note("key-bbbbbbbb")).status_code == 201
    res = env.post(_note("key-cccccccc"))
    assert res.status_code == 409 and res.json()["error"] == "record_limit"
    assert env.post(_note("key-aaaaaaaa")).status_code == 200  # a replay is still answered at the cap


def test_a_damaged_sidecar_blocks_writes_with_no_data_loss(env: Env) -> None:
    path = fr.sidecar_paths(env.plan)[0]
    path.write_text(json.dumps({"version": 9, "records": [{"id": "future"}]}), encoding="utf-8")
    res = env.post(_note())
    assert res.status_code == 422 and "newer" in res.json()["detail"]
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 9


def test_error_bodies_never_name_paths(env: Env, tmp_path: Path) -> None:
    for res in (env.post({"kind": "x"}), env.client.delete("/api/records/zzz", headers=AUTH)):
        assert str(tmp_path) not in res.text and ".fieldnotes" not in res.text


def test_routes_never_reach_the_qt_thread_or_the_plan() -> None:
    import ast
    import inspect

    from open_garden_planner.field_remote import routes

    names: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(routes))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom | ast.Import):
            names.update(a.name for a in node.names)
            names.add(getattr(node, "module", None) or "")
    assert not {n for n in names if n.startswith(("PyQt6", "open_garden_planner.app", "open_garden_planner.ui"))}
    assert not names & {"run_on_main", "QGraphicsScene", "command_manager", "agent_host"}
