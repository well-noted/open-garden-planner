"""Field Remote read API (WP-C) through the real ASGI stack with starlette's TestClient: auth,
the writes toggle, ETag/304, caps, bad input and the unsaved plan. Each test names its rule."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from open_garden_planner.field_remote import events, projection, routes, server
from open_garden_planner.field_remote.routes import EventHub, read_routes, records_routes
from tests.unit.test_field_remote_projection import LAYER_A, _background, make_snapshot

TOKEN = "t" * 43
PORT = 8766
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _Tick:
    now = 1000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


class FakeSource:
    """What the host provides; counts calls so tests can see what a route asked of the desktop."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snap = snapshot
        self.name: str | None = "garden"
        self.positions: list[dict[str, Any]] = []
        self.fail: BaseException | None = None
        self.snapshot_calls = 0

    def snapshot(self) -> dict[str, Any]:
        self.snapshot_calls += 1
        if self.fail is not None:
            raise self.fail
        return self.snap

    def plan_name(self) -> str | None:
        return self.name

    def on_position(self, telemetry: dict[str, Any]) -> None:
        self.positions.append(telemetry)


class Env:
    def __init__(self, tmp_path: Path, *, records: bool = False, saved: bool = True, snapshot: dict[str, Any] | None = None) -> None:
        self.plan: str | None = str(tmp_path / "garden.ogp") if saved else None
        self.source = FakeSource(snapshot or make_snapshot())
        self.hub = EventHub()
        self.state = server.ServerState(token=TOKEN, records_enabled=records)
        extra = [*records_routes(lambda: self.plan), *read_routes(self.source, lambda: self.plan, self.hub)]
        app = server.build_app(self.state, "127.0.0.1", PORT, extra_routes=extra,
                               guard_kwargs={"failure_delay_s": 0.0, "clock": _Tick()})
        self.client = TestClient(app, base_url=f"https://127.0.0.1:{PORT}")

    def get(self, path: str, **kw: Any) -> Any:
        return self.client.get(path, headers={**AUTH, **kw.pop("headers", {})}, **kw)

    def post(self, path: str, body: Any, **kw: Any) -> Any:
        return self.client.post(path, json=body, headers=AUTH, **kw)


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


READ_PATHS = ["/api/state", "/api/scene", "/api/basemap", "/api/species", "/api/stream"]


# -- auth and toggle --------------------------------------------------------------------------------


@pytest.mark.parametrize("path", READ_PATHS)
def test_reads_need_the_token(env: Env, path: str) -> None:
    assert env.client.get(path).status_code == 401
    assert env.client.get(path, headers={"Authorization": "Bearer " + "x" * 43}).status_code == 401


def test_position_needs_the_token(env: Env) -> None:
    assert env.client.post("/api/position", json={}).status_code == 401


def test_reads_work_with_records_off_and_position_is_allowed_with_writes_off(env: Env) -> None:
    assert env.state.records_enabled is False
    assert env.get("/api/state").status_code == 200
    res = env.post("/api/position", {"t": 1.0, "lat": 48.0, "lon": 11.0, "accuracy_m": 5})
    assert res.status_code == 200  # changes nothing on disk, so the toggle does not apply
    assert env.post("/api/records", {"kind": "note", "idempotency_key": "0123456789abcdef"}).status_code == 403


def test_position_changes_nothing_on_disk(env: Env, tmp_path: Path) -> None:
    env.post("/api/position", {"t": 1.0, "lat": 48.0, "lon": 11.0, "accuracy_m": 5})
    assert list(tmp_path.iterdir()) == []


# -- state --------------------------------------------------------------------------------------------


def test_state_shape_and_etag_304(env: Env) -> None:
    res = env.get("/api/state")
    body = res.json()
    assert body["plan_name"] == "garden" and body["saved"] is True and body["records_enabled"] is False
    assert body["georef"]["georeferenced"] is True and len(body["layers"]) == 2
    assert "server_time" in body and set(body["capabilities"]) >= {"scene", "basemap", "position", "stream"}
    etag = res.headers["etag"]
    again = env.get("/api/state", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
    env.state.records_enabled = True
    assert env.get("/api/state", headers={"If-None-Match": etag}).status_code == 200


def test_state_on_an_unsaved_plan_still_answers(tmp_path: Path) -> None:
    env = Env(tmp_path, saved=False)
    body = env.get("/api/state").json()
    assert body["saved"] is False  # the phone shows "save the plan first" for records only
    assert env.get("/api/scene").status_code == 200
    assert env.get("/api/field-records").status_code == 409  # the record routes keep their refusal


def test_state_when_the_desktop_is_busy_is_a_503(env: Env) -> None:
    env.source.fail = TimeoutError()
    res = env.get("/api/state")
    assert res.status_code == 503 and res.json()["error"] == "plan_unavailable"
    env.source.fail = RuntimeError("bridge closed")
    assert env.get("/api/scene").status_code == 503


# -- scene ------------------------------------------------------------------------------------------


def test_scene_returns_geometry_and_honours_bbox_and_layers(env: Env) -> None:
    body = env.get("/api/scene").json()
    assert {o["id"] for o in body["objects"]} == {"r1", "c1", "p1"} and body["y_up"] is True
    small = env.get("/api/scene", params={"bbox": "0,0,40,40", "layers": "Beds"}).json()
    assert {o["id"] for o in small["objects"]} == {"c1", "p1"}


def test_scene_etag_304(env: Env) -> None:
    etag = env.get("/api/scene").headers["etag"]
    assert env.get("/api/scene", headers={"If-None-Match": etag}).status_code == 304
    env.source.snap = make_snapshot(objects=[])
    assert env.get("/api/scene", headers={"If-None-Match": etag}).status_code == 200


@pytest.mark.parametrize("bbox", ["1,2,3", "a,b,c,d", "9,0,1,1", "nan,0,1,1", "1,2,3,inf"])
def test_scene_bad_bbox_is_a_400(env: Env, bbox: str) -> None:
    res = env.get("/api/scene", params={"bbox": bbox})
    assert res.status_code == 400 and res.json()["error"] == "bad_bbox"
    assert env.source.snapshot_calls == 0  # refused before the desktop was asked


def test_scene_caps_at_5000_objects_and_says_so(tmp_path: Path) -> None:
    objs = [{"type": "circle", "item_id": f"c{i}", "center_x": i, "center_y": 0, "radius": 1, "layer_id": LAYER_A}
            for i in range(5100)]
    env = Env(tmp_path, snapshot=make_snapshot(objects=objs))
    body = env.get("/api/scene").json()
    assert body["count"] == 5000 and body["matched"] == 5100 and body["truncated"] is True


# -- basemap ------------------------------------------------------------------------------------------


def test_basemap_is_a_jpeg_with_its_rectangle_and_etag(tmp_path: Path) -> None:
    env = Env(tmp_path, snapshot=make_snapshot(objects=[_background(3000, 1500, x=10, y=20, factor=1.0)]))
    res = env.get("/api/basemap")
    assert res.status_code == 200 and res.headers["content-type"] == "image/jpeg"
    assert res.content[:2] == b"\xff\xd8"
    assert res.headers["x-basemap-rect"] == "10.0,20.0,3000.0,1500.0"
    again = env.get("/api/basemap", headers={"If-None-Match": res.headers["etag"]})
    assert again.status_code == 304
    meta = env.get("/api/basemap", params={"meta": "1"}).json()
    assert meta["rect"]["width_cm"] == 3000.0 and meta["width_px"] == 2048 and meta["source_px"] == [3000, 1500]


def test_basemap_is_resized_once_per_background(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real = projection.project_basemap
    monkeypatch.setattr(projection, "project_basemap", lambda snap: calls.append(1) or real(snap))
    env = Env(tmp_path, snapshot=make_snapshot(objects=[_background(64, 64)]))
    for _ in range(3):
        assert env.get("/api/basemap").status_code == 200
    assert len(calls) == 1


def test_no_basemap_is_a_404(env: Env) -> None:
    res = env.get("/api/basemap")
    assert res.status_code == 404 and res.json()["error"] == "no_basemap"


# -- species ---------------------------------------------------------------------------------------


def test_species_are_capped_at_25_and_searchable(env: Env) -> None:
    body = env.get("/api/species").json()
    assert body["count"] == len(body["species"]) == 25 == body["limit"]
    assert env.get("/api/species", params={"q": "zzzzqqqq"}).json()["species"] == []
    assert env.get("/api/species", params={"q": "x" * 5000}).status_code == 200  # query is bounded


# -- position ---------------------------------------------------------------------------------------


def _pos(**kw: Any) -> dict[str, Any]:
    return {"t": 100.0, "lat": 48.0, "lon": 11.0, "accuracy_m": 4.0, **kw}


def test_single_position_returns_telemetry_and_reaches_the_desktop(env: Env) -> None:
    res = env.post("/api/position", _pos(heading_deg=90.0, speed_mps=1.2, alt=500.0))
    body = res.json()
    assert res.status_code == 200 and body["accepted"] == 1
    tele = body["telemetry"]
    assert tele["x_cm"] == pytest.approx(0.0, abs=0.1) and tele["y_cm"] == pytest.approx(0.0, abs=0.1)
    # the filter reports a heading only once it is moving (a lone fix has no velocity yet)
    assert tele["fix_mode"] == "standard" and tele["heading_canvas_deg"] is None
    assert env.source.positions == [tele]


def test_batch_forms_use_the_last_fix_and_cap_at_50(env: Env) -> None:
    batch = [_pos(t=float(i), lon=11.0 + i * 1e-5) for i in range(50)]
    res = env.post("/api/position", {"positions": batch})
    assert res.status_code == 200 and res.json()["accepted"] == 50 and res.json()["telemetry"]["samples_used"] == 50
    assert env.post("/api/position", batch).json()["accepted"] == 50  # a bare array is a batch too
    too_many = env.post("/api/position", {"positions": batch + [_pos()]})
    assert too_many.status_code == 422 and too_many.json()["error"] == "invalid_position"
    assert env.post("/api/position", {"positions": []}).status_code == 422


def test_position_accepts_reported_fix_quality_hdop_sigma_and_source(env: Env) -> None:
    res = env.post("/api/position", _pos(fix_quality="rtk_float", hdop=0.8, sigma_m=0.3, source="nmea_ble"))
    tele = res.json()["telemetry"]
    assert tele["fix_mode"] == "rtk_float" and tele["fix_mode_reported"] is True
    assert tele["sigma_m"] >= 0.3  # the filter floors an optimistic reported sigma (core/positioning R_MIN_M)
    assert tele["fix_source"] == "nmea_ble"


@pytest.mark.parametrize(
    "bad",
    [
        {"lat": 91}, {"lat": -91}, {"lon": 181}, {"accuracy_m": -1}, {"t": -1}, {"heading_deg": 361},
        {"fix_quality": "magic"}, {"source": "satellite"}, {"sigma_m": 0}, {"lat": "48"}, {"lat": True},
        {"extra": 1}, {"speed_mps": -1}, {"lat": float("inf")},
    ],
)
def test_bad_position_fields_are_422(env: Env, bad: dict[str, Any]) -> None:
    import json

    body = json.dumps(_pos(**bad), allow_nan=True)
    res = env.client.post("/api/position", content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert res.status_code == 422 and res.json()["error"] == "invalid_position"
    assert env.source.positions == []


def test_position_wrong_media_type_and_garbage(env: Env) -> None:
    assert env.client.post("/api/position", content="lat=1", headers=AUTH).status_code == 415
    res = env.client.post("/api/position", content=b"{not json", headers={**AUTH, "Content-Type": "application/json"})
    assert res.status_code == 422


def test_oversized_position_body_is_refused_by_the_guard(env: Env) -> None:
    big = {"positions": [_pos(vio={"pad": "x" * 7000}) for _ in range(40)]}
    res = env.post("/api/position", big)
    assert res.status_code == 413


def test_position_without_a_georef_has_no_plan_coordinates(tmp_path: Path) -> None:
    env = Env(tmp_path, snapshot=make_snapshot(georef=None))
    tele = env.post("/api/position", _pos()).json()["telemetry"]
    assert tele["x_cm"] is None and tele["georeferenced"] is False


def test_position_still_answers_when_the_desktop_is_busy(env: Env) -> None:
    env.source.fail = TimeoutError()
    res = env.post("/api/position", _pos())
    assert res.status_code == 200 and res.json()["telemetry"]["x_cm"] is None


def test_position_on_an_unsaved_plan_is_allowed(tmp_path: Path) -> None:
    assert Env(tmp_path, saved=False).post("/api/position", _pos()).status_code == 200


# -- stream -----------------------------------------------------------------------------------------


def _read_stream_lines(env: Env, params: dict[str, Any], *, stop_after: int) -> list[dict[str, Any]]:
    import json

    out: list[dict[str, Any]] = []
    with env.client.stream("GET", "/api/stream", params=params, headers=AUTH) as res:
        assert res.status_code == 200 and res.headers["content-type"].startswith("application/x-ndjson")
        for line in res.iter_lines():
            if line:
                out.append(json.loads(line))
            if len(out) >= stop_after:
                break
    return out


def test_stream_says_hello_then_replays_events_since(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "STREAM_POLL_S", 0.01)
    env.hub.publish("plan_changed")
    env.hub.publish("records_changed", {"n": 2})
    lines = _read_stream_lines(env, {"since": 0}, stop_after=3)
    assert lines[0]["type"] == "hello" and lines[0]["records_enabled"] is False
    assert [(e["type"], e["seq"]) for e in lines[1:]] == [("plan_changed", 1), ("records_changed", 2)]
    assert lines[2]["data"] == {"n": 2}


def test_stream_without_since_starts_at_now_and_a_new_event_arrives(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "STREAM_POLL_S", 0.01)
    env.hub.publish("plan_changed")  # old: not replayed
    timer = threading.Timer(0.2, lambda: env.hub.publish("settings_changed", {"records_enabled": True}))
    timer.start()
    try:
        lines = _read_stream_lines(env, {}, stop_after=2)
    finally:
        timer.cancel()
    assert [e["type"] for e in lines] == ["hello", "settings_changed"]


def test_stream_is_bounded_in_time_and_in_concurrency(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "STREAM_POLL_S", 0.01)
    monkeypatch.setattr(routes, "STREAM_LIFETIME_S", 0.1)
    lines = _read_stream_lines(env, {}, stop_after=10)
    assert lines[-1]["type"] == "end" and lines[-1]["reconnect"] is True  # then the phone reconnects
    assert env.hub.open_stream(routes.MAX_STREAMS) and env.hub.open_stream(routes.MAX_STREAMS)
    busy = env.get("/api/stream")
    assert busy.status_code == 429 and busy.json()["error"] == "stream_busy"
    env.hub.close_stream()
    env.hub.close_stream()


def test_stream_slot_is_released_after_the_stream_ends(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "STREAM_POLL_S", 0.01)
    monkeypatch.setattr(routes, "STREAM_LIFETIME_S", 0.05)
    for _ in range(4):  # more than MAX_STREAMS in a row
        assert _read_stream_lines(env, {}, stop_after=10)[-1]["type"] == "end"


def test_stream_rejects_a_bad_since(env: Env) -> None:
    assert env.get("/api/stream", params={"since": "abc"}).status_code == 400


def test_event_hub_is_bounded_and_rejects_unknown_kinds() -> None:
    hub = EventHub()
    for _ in range(events.MAX_EVENTS + 50):
        hub.publish("plan_changed")
    assert len(hub.since(0)) == events.MAX_EVENTS and hub.last_seq == events.MAX_EVENTS + 50
    with pytest.raises(ValueError):
        hub.publish("shell")


# -- WP-D routes are not duplicated -------------------------------------------------------------------


def test_field_record_reads_come_from_the_records_routes_only() -> None:
    paths = [r.path for r in read_routes(FakeSource(make_snapshot()), lambda: None)]
    assert "/api/field-records" not in paths and not any("photo" in p for p in paths)
    assert "/api/field-records" in [r.path for r in records_routes(lambda: None)]
