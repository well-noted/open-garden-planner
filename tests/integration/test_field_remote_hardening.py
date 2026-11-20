"""Field Remote hardening from the security review (docs 8.10, ADR-081)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from PyQt6.QtWidgets import QApplication, QMessageBox

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.field_remote_host import ERROR, OFF, RUNNING
from open_garden_planner.app.settings import get_settings
from open_garden_planner.field_remote import server as remote_server
from open_garden_planner.field_remote import tls
from open_garden_planner.ui.dialogs.field_remote_dialog import (
    CLIPBOARD_EXCLUDE_FORMAT,
    FieldRemoteDialog,
)
from open_garden_planner.ui.dialogs.preferences_dialog import PreferencesDialog

from .test_field_remote_lifecycle import _free_port, _server_threads


@pytest.fixture(autouse=True)
def _no_welcome_dialog(_reset_app_settings: Any) -> None:
    get_settings().show_welcome_on_startup = False


@pytest.fixture
def app(qtbot: Any, tmp_path: Path):
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    win.field_remote_host._data_dir = tmp_path / "tls"
    yield win
    win.field_remote_host.stop()
    assert not _server_threads()


def test_uvicorn_does_not_trust_forwarded_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Forwarded-For is attacker input here: the lockout must key on the socket address."""
    seen: list[uvicorn.Config] = []

    class Recording(uvicorn.Config):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            seen.append(self)

    monkeypatch.setattr(uvicorn, "Config", Recording)
    material = tls.generate_self_signed(tmp_path, ["127.0.0.1"])
    srv = remote_server.FieldRemoteServer(remote_server.ServerState(token="t" * 43), material, port=_free_port())
    srv.start()
    try:
        assert seen and seen[0].proxy_headers is False
    finally:
        srv.stop()


def test_a_server_that_never_reports_ready_raises_and_leaves_nothing_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = tls.generate_self_signed(tmp_path, ["127.0.0.1"])
    monkeypatch.setattr(remote_server, "_READY_TIMEOUT_S", 0.0)
    srv = remote_server.FieldRemoteServer(remote_server.ServerState(token="t" * 43), material, port=_free_port())
    with pytest.raises(RuntimeError, match="ready"):
        srv.start()
    assert not srv.is_running
    assert not [t for t in threading.enumerate() if t.name == remote_server.SERVER_THREAD_NAME and t.is_alive()]


def test_host_reports_error_not_running_when_start_never_becomes_ready(
    app: GardenPlannerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_settings().field_remote_port = _free_port()
    monkeypatch.setattr(remote_server, "_READY_TIMEOUT_S", 0.0)
    app.field_remote_host.start()
    assert app.field_remote_host.status().state == ERROR
    assert not app.field_remote_host.is_running and app.field_remote_host.pairing_url() is None


def test_field_remote_refuses_the_agent_api_port(app: GardenPlannerApp) -> None:
    settings = get_settings()
    settings.field_remote_port = settings.agent_api_port
    app.field_remote_host.start()
    assert app.field_remote_host.status().state == ERROR
    assert "Agent API" in app.field_remote_host.status().message


def test_regenerate_certificate_works_while_stopped(app: GardenPlannerApp) -> None:
    host = app.field_remote_host
    assert host.certificate_fingerprint() == ""
    host.regenerate_certificate()
    first = host.certificate_fingerprint()
    assert first and host.status().state == OFF and not host.is_running
    host.regenerate_certificate()
    assert host.certificate_fingerprint() not in ("", first)


def test_regenerate_certificate_while_running_restarts_on_the_new_one(app: GardenPlannerApp) -> None:
    get_settings().field_remote_port = _free_port()
    host = app.field_remote_host
    host.start()
    before = host.certificate_fingerprint()
    host.regenerate_certificate()
    assert host.status().state == RUNNING and host.certificate_fingerprint() != before
    assert host.status().fingerprint_hex == host.certificate_fingerprint()


def test_preferences_show_the_fingerprint_and_regenerate_from_the_ui(app: GardenPlannerApp, qtbot: Any) -> None:
    dialog = PreferencesDialog(app)
    qtbot.addWidget(dialog)
    assert dialog._field_remote_fp_label.text() == "Not created yet"
    dialog._field_remote_check.setChecked(True)
    dialog._on_regenerate_field_remote_certificate()
    shown = dialog._field_remote_fp_label.text()
    assert shown.count(":") == 31  # AA:BB:... for 32 bytes
    assert shown.replace(":", "").lower() == app.field_remote_host.certificate_fingerprint()


@pytest.mark.parametrize("bind", ["0.0.0.0", "8.8.8.8", "not-an-address"])
def test_preferences_refuse_an_unusable_address_and_save_nothing(
    app: GardenPlannerApp, qtbot: Any, monkeypatch: pytest.MonkeyPatch, bind: str
) -> None:
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **_k: warned.append(a[2]))
    dialog = PreferencesDialog(app)
    qtbot.addWidget(dialog)
    dialog._field_remote_check.setChecked(True)
    dialog._field_remote_bind_combo.setCurrentText(bind)
    dialog._save_and_accept()
    assert warned and "cannot be used" in warned[0]
    assert dialog.result() == 0
    assert get_settings().field_remote_enabled is False


def test_preferences_refuse_a_port_shared_with_the_agent_api(
    app: GardenPlannerApp, qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **_k: warned.append(a[2]))
    dialog = PreferencesDialog(app)
    qtbot.addWidget(dialog)
    dialog._agent_api_check.setChecked(True)
    dialog._field_remote_check.setChecked(True)
    dialog._field_remote_port_spin.setValue(dialog._agent_api_port_spin.value())
    dialog._save_and_accept()
    assert warned and "cannot share port" in warned[0] and get_settings().field_remote_enabled is False


def test_preferences_save_a_valid_field_remote_configuration(app: GardenPlannerApp, qtbot: Any) -> None:
    dialog = PreferencesDialog(app)
    qtbot.addWidget(dialog)
    dialog._field_remote_check.setChecked(True)
    dialog._field_remote_bind_combo.setCurrentText("127.0.0.1")
    dialog._field_remote_port_spin.setValue(_free_port())
    dialog._save_and_accept()
    assert get_settings().field_remote_enabled is True and dialog.result() == 1


def test_copy_link_hides_from_clipboard_history_and_is_cleared_on_close(qtbot: Any) -> None:
    url = "https://127.0.0.1:8766/#token=secret&fp=00"
    dialog = FieldRemoteDialog(url, "00" * 32)
    qtbot.addWidget(dialog)
    dialog._on_copy()
    mime = QApplication.clipboard().mimeData()
    assert mime.text() == url and mime.hasFormat(CLIPBOARD_EXCLUDE_FORMAT)
    dialog.done(0)
    assert QApplication.clipboard().text() == ""


def test_hiding_the_link_clears_it_from_the_clipboard_but_not_someone_elses_copy(qtbot: Any) -> None:
    dialog = FieldRemoteDialog("https://127.0.0.1:8766/#token=secret&fp=00", "00" * 32)
    qtbot.addWidget(dialog)
    dialog._on_copy()
    dialog.hide_link()
    assert QApplication.clipboard().text() == ""
    QApplication.clipboard().setText("something the user copied later")
    dialog._copied = "https://127.0.0.1:8766/#token=secret&fp=00"
    dialog.done(0)
    assert QApplication.clipboard().text() == "something the user copied later"
