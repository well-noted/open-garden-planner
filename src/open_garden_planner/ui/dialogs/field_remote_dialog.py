"""Pairing dialog for Field Remote: the link for the phone and the certificate fingerprint.

The link carries the access token in its fragment, so it is shown on demand and cleared after
two minutes (design 1.5). A QR image is not drawn yet: the design leaves the encoder choice
(a vendored MIT encoder or the ``qrcode`` package) open, and no QR library is installed.
"""

from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

#: How long the link stays visible. It is a credential.
SHOW_SECONDS = 120


class FieldRemoteDialog(QDialog):
    """Shows the pairing link and the certificate fingerprint to compare on the phone."""

    def __init__(self, pairing_url: str, fingerprint_hex: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Local import: the ui layer may not import field_remote eagerly (check_import_boundaries).
        from open_garden_planner.field_remote.auth import format_fingerprint

        self.setWindowTitle(self.tr("Pair a phone"))
        self._url = pairing_url
        layout = QVBoxLayout(self)

        intro = QLabel(
            self.tr(
                "Open this link in Chrome on your Android phone. Your phone's browser will warn "
                "that the certificate is not trusted; that is expected for a certificate this "
                "computer made itself. Compare the fingerprint below with the one the browser "
                "shows (under the warning's certificate details), then continue."
            )
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._url_edit = QLineEdit(pairing_url)
        self._url_edit.setReadOnly(True)
        layout.addWidget(self._url_edit)

        fingerprint_label = QLabel(self.tr("Certificate fingerprint (SHA-256):"))
        layout.addWidget(fingerprint_label)
        self._fingerprint_edit = QLineEdit(format_fingerprint(fingerprint_hex))
        self._fingerprint_edit.setReadOnly(True)
        layout.addWidget(self._fingerprint_edit)

        self._note = QLabel(
            self.tr("The link contains the access token. It is hidden after two minutes; do not share it.")
        )
        self._note.setWordWrap(True)
        layout.addWidget(self._note)

        buttons = QHBoxLayout()
        self._copy_btn = QPushButton(self.tr("Copy link"))
        self._copy_btn.clicked.connect(self._on_copy)
        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(self._copy_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide_link)
        self._timer.start(SHOW_SECONDS * 1000)

    def _on_copy(self) -> None:
        if self._url:
            QApplication.clipboard().setText(self._url)

    def hide_link(self) -> None:
        """Remove the token from the screen and the dialog's memory."""
        self._url = ""
        self._url_edit.clear()
        self._url_edit.setPlaceholderText(self.tr("Hidden. Close this window and open it again."))
        self._copy_btn.setEnabled(False)
