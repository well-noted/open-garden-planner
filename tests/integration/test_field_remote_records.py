"""Field Remote record routes from the running app (docs 8.10, ADR-080 addendum).

Real app, real uvicorn over TLS, real sockets, real sidecar files: the phone's record writes need
the permission toggle and a saved plan, land in the sidecar from this same process, and never touch
the plan (no undo step, no scene item, no unsaved-changes flag).
"""

from __future__ import annotations

import io
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.field_remote import limits
from open_garden_planner.field_remote.server import SERVER_THREAD_NAME
from open_garden_planner.services import field_records as fr


@pytest.fixture(autouse=True)
def _no_welcome_dialog(_reset_app_settings: Any) -> None:
    get_settings().show_welcome_on_startup = False


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def running(qtbot: Any, tmp_path: Path):
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    host = win.field_remote_host
    host._data_dir = tmp_path / "tls"
    settings = get_settings()
    settings.field_remote_port = _free_port()
    host.start()
    token = settings.field_remote_token
    client = httpx.Client(
        base_url=f"https://127.0.0.1:{settings.field_remote_port}",
        verify=False,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    yield win, host, client
    client.close()
    host.stop()


def _save_as(win: GardenPlannerApp, tmp_path: Path) -> Path:
    plan = tmp_path / "garden.ogp"
    win._project_manager._current_file = plan  # what a successful Save does to the manager
    return plan


def _note(key: str = "int-key-00000001") -> dict[str, Any]:
    return {"kind": "note", "idempotency_key": key, "notes": "wet corner", "fix_mode": "gps",
            "geometry": {"type": "point", "lat": 48.0, "lon": 11.0, "x_cm": 5, "y_cm": 6}}


def test_writes_follow_the_toggle_and_the_saved_plan_and_never_touch_the_plan(
    running: Any, tmp_path: Path
) -> None:
    win, host, client = running
    stack = win.canvas_view.command_manager
    undo_before = len(stack._undo_stack)
    items_before = len(win.canvas_view.scene().items())

    # 1. permission toggle off (the default): refused, even with a valid token
    assert get_settings().field_remote_records is False
    res = client.post("/api/records", json=_note())
    assert res.status_code == 403 and res.json()["error"] == "records_disabled"

    # 2. toggle on, plan unsaved: nowhere to put records
    host.set_records_enabled(True)
    res = client.post("/api/records", json=_note())
    assert res.status_code == 409 and res.json()["error"] == "plan_not_saved"
    assert not list(tmp_path.glob("*.fieldnotes*"))

    time.sleep(1.2)  # refill the write bucket (5/s, burst 5)
    # 3. saved plan: created, then an idempotent replay returns the same record
    plan = _save_as(win, tmp_path)
    first = client.post("/api/records", json=_note())
    assert first.status_code == 201
    again = client.post("/api/records", json=_note())
    assert again.status_code == 200 and again.json()["record"]["id"] == first.json()["record"]["id"]
    assert len(fr.load_records(plan).records) == 1

    # 4. the phone's own pending record can be edited and withdrawn; a decision on the desktop locks it
    rec_id = first.json()["record"]["id"]
    assert client.patch(f"/api/records/{rec_id}", json={"notes": "dry now"}).json()["record"]["notes"] == "dry now"
    fr.update_record(plan, rec_id, status="placed", placed_object_id="obj-1")  # the desktop approves
    assert client.patch(f"/api/records/{rec_id}", json={"notes": "late"}).status_code == 409
    assert client.delete(f"/api/records/{rec_id}").status_code == 409
    listed = client.get("/api/field-records?status=placed").json()
    assert listed["returned"] == 1 and listed["records"][0]["placed_object_id"] == "obj-1"

    time.sleep(1.2)
    # 5. toggling off again stops writes at once, without a restart
    host.set_records_enabled(False)
    assert client.post("/api/records", json=_note("int-key-00000002")).status_code == 403

    # nothing above changed the plan
    assert len(stack._undo_stack) == undo_before
    assert len(win.canvas_view.scene().items()) == items_before


def test_photo_upload_is_capped_and_stored_beside_the_plan(running: Any, tmp_path: Path) -> None:
    win, host, client = running
    host.set_records_enabled(True)
    plan = _save_as(win, tmp_path)
    ok = client.post(
        "/api/records", data={"meta": json.dumps(_note())}, files={"photo": ("any-name.jpg", _png(), "image/jpeg")}
    )
    assert ok.status_code == 201
    rec = fr.get_record(plan, ok.json()["record"]["id"])
    assert rec.photo == f"photos/{rec.id}.png" and fr.resolve_photo(plan, rec).is_file()
    assert client.get(ok.json()["record"]["photo_url"]).content == _png()

    big = b"\x89PNG" + b"0" * (limits.MAX_PHOTO_BYTES + 1024)
    over = client.post(
        "/api/records", data={"meta": json.dumps(_note("int-key-00000009"))}, files={"photo": ("a.png", big, "image/png")}
    )
    assert over.status_code == 413
    assert len(fr.load_records(plan).records) == 1


def test_the_sidecar_is_written_by_this_process(running: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    win, host, client = running
    host.set_records_enabled(True)
    _save_as(win, tmp_path)
    seen: list[tuple[int, str]] = []
    original = fr.create_record

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append((os.getpid(), threading.current_thread().name))
        return original(*args, **kwargs)

    monkeypatch.setattr(fr, "create_record", spy)
    assert client.post("/api/records", json=_note()).status_code == 201
    pid, thread = seen[0]
    assert pid == os.getpid()  # one process, so the store's lock is the only lock needed (design 1.8)
    # the store call runs on a worker thread, never the Qt GUI thread
    assert thread != threading.main_thread().name
    assert any(t.name == SERVER_THREAD_NAME for t in threading.enumerate())


def test_two_writers_one_process_lose_nothing(running: Any, tmp_path: Path) -> None:
    """The phone route and an agent-style direct store call interleave under the one lock."""
    win, host, client = running
    host.set_records_enabled(True)
    plan = _save_as(win, tmp_path)
    errors: list[BaseException] = []

    def agent() -> None:
        try:
            for n in range(30):
                fr.add_record(plan, {"notes": f"agent-{n}", "source": "agent"})
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=agent)
    thread.start()
    for n in range(30):
        assert client.post("/api/records", json=_note(f"phone-key-{n:06d}")).status_code in (201, 200)
        time.sleep(0.22)  # stay under the write rate limit (5/s, burst 5)
    thread.join()
    assert not errors
    assert len(fr.load_records(plan).records) == 60
