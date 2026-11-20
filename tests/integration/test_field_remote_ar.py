"""Field Remote AR plan lock from the running app (docs 8.10, WP-H).

Real uvicorn + TLS on loopback and the real canvas: the phone locks A and B, posts synthetic
VIO, and the locked position reaches the desktop puck through ``source.on_position`` with
``track_source`` ``vio``. A loss then drops the lock and the puck is no longer fed from VIO.
Synthetic poses only: nothing here says how a real phone's WebXR tracking behaves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.geo_frame import make_frame
from tests.integration.test_field_remote_position import Phone, _free_port

T0 = 1_700_000_000.0
A_CM = (1000.0, 500.0)
B_CM = (3000.0, 500.0)  # 20 m east of A at 1 cm per unit


@pytest.fixture(autouse=True)
def _no_welcome_dialog(_reset_app_settings: Any) -> None:
    get_settings().show_welcome_on_startup = False


@pytest.fixture
def phone(qtbot: Any, tmp_path: Path):
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    host = win.field_remote_host
    host._data_dir = tmp_path / "tls"
    port = _free_port()
    get_settings().field_remote_port = port
    win._project_manager.set_georeference(make_frame(origin_lat=48.0, origin_lon=11.0, scale_meters_per_unit=0.01).georeference)
    win._project_manager.mark_clean()
    host.start()
    assert host.is_running
    client = Phone(qtbot, port, get_settings().field_remote_token)
    yield win, client
    client.close()
    host.stop()


def _vio(t: float, x: float, z: float, tracking: str = "tracking") -> dict[str, Any]:
    return {"t": t, "x": x, "y": 1.4, "z": z, "qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0, "tracking": tracking}


def _lock(plan: tuple[float, float], hit: tuple[float, float], vio_t: float) -> dict[str, Any]:
    return {"plan": {"x_cm": plan[0], "y_cm": plan[1]}, "hit": {"x": hit[0], "y": 0.0, "z": hit[1]}, "vio_t": vio_t}


def test_a_plan_lock_drives_the_desktop_puck_and_a_loss_drops_it(phone: Any, qtbot: Any) -> None:
    win, client = phone
    scene = win.canvas_scene
    pm = win._project_manager
    undo_before = len(win.canvas_view.command_manager._undo_stack)

    def call(method: str, path: str, **kw: Any) -> Any:
        qtbot.wait(250)  # the write limit is 5 per second; the phone sends about one batch a second
        return client.call(method, path, **kw)

    def post_vio(samples: list[dict[str, Any]]) -> dict[str, Any]:
        res = call("POST", "/api/position", json={"vio_batch": samples})
        assert res.status_code == 200, res.text
        return res.json()["telemetry"]

    post_vio([_vio(T0, 0.0, 0.0)])
    assert call("POST", "/api/ar/lock/a", json=_lock(A_CM, (0.0, 0.0), T0)).status_code == 200
    post_vio([_vio(T0 + 0.5 * k, float(k), 0.0) for k in range(1, 21)])
    # a lock naming a pose the server has not received yet is refused (the phone flushes first)
    early = call("POST", "/api/ar/lock/b", json=_lock(B_CM, (20.0, 0.0), T0 + 10.5))
    assert early.status_code == 409 and early.json()["error"] == "ar_lock_refused"
    res = call("POST", "/api/ar/lock/b", json=_lock(B_CM, (20.0, 0.0), T0 + 10.0))
    assert res.status_code == 200 and res.json()["state"] == "locked" and res.json()["scale"]["ok"] is True
    assert res.json()["fused"]["lock_t"] == T0 + 10.0  # from the pose stream, not a phone clock

    tele = post_vio([_vio(T0 + 10.4, 21.0, -1.0)])
    assert tele["track_source"] == "vio" and tele["fix_mode"] == "vio_fused" and tele["quality"] == "unknown"
    assert tele["x_cm"] == pytest.approx(A_CM[0] + 2100.0, abs=1.0) and tele["y_cm"] == pytest.approx(A_CM[1] + 100.0, abs=1.0)

    # the desktop puck: queued from the server thread through source.on_position
    qtbot.waitUntil(lambda: scene.position_puck is not None, timeout=5000)
    puck = scene.position_puck
    assert (puck.pos().x(), puck.pos().y()) == pytest.approx((tele["x_cm"], tele["y_cm"]), abs=0.5)
    assert puck.fix_mode == "vio_fused" and puck.badge_text == "VIO"  # no spread: the sigma is a placeholder
    assert puck.halo_radius_cm == 0.0

    # walking on moves the same puck
    moved = post_vio([_vio(T0 + 10.8, 21.5, -1.0)])
    qtbot.waitUntil(lambda: puck.pos().x() == pytest.approx(moved["x_cm"], abs=0.5), timeout=5000)

    # a relocalising jump on recovery from limited drops the lock; the puck is not fed from VIO again
    post_vio([_vio(T0 + 11.0, 21.5, -1.0, "limited")])
    lost = post_vio([_vio(T0 + 11.5, 51.5, -1.0)])
    assert lost["fused"]["state"] == "unlocked" and lost["fused"]["unlock_reason"] == "vio_pose_jump"
    assert lost["x_cm"] is None and lost["track_source"] == "gnss"
    qtbot.wait(200)
    assert puck.pos().x() == pytest.approx(moved["x_cm"], abs=0.5)  # not moved by the unlocked frame

    # an overlay only: nothing saved, no undo step
    assert len(win.canvas_view.command_manager._undo_stack) == undo_before
    assert pm.is_dirty is False
