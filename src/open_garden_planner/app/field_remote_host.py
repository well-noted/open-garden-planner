"""FieldRemoteHost: owns the Field Remote server's lifecycle inside the GUI process.

Beside ``agent_host.py``, but a separate listener (own port, own token, own thread) with a
different threat model: it can be bound to a LAN or Tailscale address. Off by default, bound to
127.0.0.1 unless the user chooses another address in Preferences, and the phone may not save
records until a second, separate toggle is on. The phone never edits the plan.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    from open_garden_planner.field_remote.server import FieldRemoteServer, ServerState

logger = logging.getLogger(__name__)

OFF = "off"
RUNNING = "running"
ERROR = "error"


@dataclass(frozen=True)
class FieldRemoteStatus:
    """What the UI shows. Never carries the token or the key."""

    state: str
    bind: str
    port: int
    fingerprint_hex: str
    records_enabled: bool
    message: str = ""

    @property
    def base_url(self) -> str:
        shown = f"[{self.bind}]" if ":" in self.bind else self.bind
        return f"https://{shown}:{self.port}/"


class FieldRemoteHost(QObject):
    """Start/stop/status for the phone listener; ``status_changed`` fires on every change."""

    status_changed = pyqtSignal(object)  # FieldRemoteStatus

    def __init__(self, parent: QObject | None = None, *, data_dir: Path | None = None) -> None:
        super().__init__(parent)
        self._data_dir = data_dir
        self._server: FieldRemoteServer | None = None
        self._state: ServerState | None = None
        self._fingerprint = ""
        self._status = self._make_status(OFF)

    # -- reads ---------------------------------------------------------------------------
    def status(self) -> FieldRemoteStatus:
        return self._status

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_running

    def pairing_url(self) -> str | None:
        """The URL for the phone, with the token in the fragment. None unless running.

        Not stored in ``status`` so it is not passed around signal handlers or logs."""
        if not self.is_running or self._state is None or self._server is None:
            return None
        from open_garden_planner.field_remote.auth import pairing_url

        return pairing_url(self._server.bind, self._server.port, self._state.token, self._fingerprint)

    # -- lifecycle -----------------------------------------------------------------------
    def maybe_start(self) -> None:
        """Start only if the user turned Field Remote on (it is off by default)."""
        from open_garden_planner.app.settings import get_settings

        if get_settings().field_remote_enabled:
            self.start()

    def start(self, *, force_new_certificate: bool = False) -> None:
        from open_garden_planner.app.settings import get_settings
        from open_garden_planner.field_remote import tls
        from open_garden_planner.field_remote.server import (
            FieldRemoteServer,
            PortInUseError,
            ServerState,
            check_bind_address,
        )

        if self.is_running:
            return
        settings = get_settings()
        port = settings.field_remote_port
        records = settings.field_remote_records
        try:
            bind = check_bind_address(settings.field_remote_bind)
        except ValueError as exc:
            self._fail(settings.field_remote_bind, port, records, str(exc))
            return
        try:
            material = tls.load_or_create(self._tls_dir(), [bind], force=force_new_certificate)
            state = ServerState(token=settings.field_remote_token, records_enabled=records)
            server = FieldRemoteServer(state, material, bind=bind, port=port)
            server.start()
        except PortInUseError:
            self._fail(bind, port, records, self.tr("Port {port} is already in use").format(port=port))
            return
        except Exception:
            logger.exception("Field Remote failed to start")  # never logs the key or token
            self._fail(bind, port, records, self.tr("Field Remote failed to start (see log)"))
            return
        self._server, self._state, self._fingerprint = server, state, material.fingerprint_hex
        self._publish(RUNNING)

    def stop(self) -> None:
        server = self._server
        self._server = None
        self._state = None
        if server is not None:
            server.stop()
        self._publish(OFF)

    def restart(self) -> None:
        self.stop()
        self.maybe_start()

    # -- live changes (no restart needed) -------------------------------------------------
    def set_records_enabled(self, enabled: bool) -> None:
        """Apply the permission toggle to the running server at once."""
        if self._state is not None:
            self._state.records_enabled = bool(enabled)
            self._publish(RUNNING)

    def rotate_token(self) -> None:
        """New token: the old one stops working on the next request; phones must pair again."""
        from open_garden_planner.app.settings import get_settings

        token = get_settings().regenerate_field_remote_token()
        if self._state is not None:
            self._state.token = token
            self._publish(RUNNING)

    def regenerate_certificate(self) -> None:
        """New certificate (new fingerprint); restarts the listener if it was running."""
        was_running = self.is_running
        self.stop()
        if was_running:
            self.start(force_new_certificate=True)

    # -- internals -----------------------------------------------------------------------
    def _tls_dir(self) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        import os
        import sys

        from open_garden_planner.models.storage_paths import get_app_data_dir

        # The private key belongs to this machine: LOCALAPPDATA does not roam, APPDATA can.
        local = os.environ.get("LOCALAPPDATA")
        if sys.platform == "win32" and local:
            return Path(local) / "OpenGardenPlanner" / "field_remote"
        return get_app_data_dir() / "field_remote"

    def _make_status(self, state: str, message: str = "") -> FieldRemoteStatus:
        from open_garden_planner.app.settings import get_settings

        settings = get_settings()
        server = self._server
        return FieldRemoteStatus(
            state=state,
            bind=server.bind if server is not None else settings.field_remote_bind,
            port=server.port if server is not None else settings.field_remote_port,
            fingerprint_hex=self._fingerprint if state == RUNNING else "",
            records_enabled=self._state.records_enabled if self._state is not None else False,
            message=message,
        )

    def _publish(self, state: str, message: str = "") -> None:
        self._status = self._make_status(state, message)
        self.status_changed.emit(self._status)

    def _fail(self, bind: str, port: int, records: bool, message: str) -> None:
        self._server = None
        self._state = None
        self._status = FieldRemoteStatus(ERROR, bind, port, "", records, message)
        self.status_changed.emit(self._status)
