"""The render style's toolbar: a label and a combo (Package 4, WP7).

The combo is created by, and stays in step with, the ``RenderStyleController``
that also drives the View menu and the 3D pane's button.
"""

from __future__ import annotations

from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtWidgets import QComboBox, QLabel, QToolBar, QWidget

from open_garden_planner.ui.render_style_controller import (
    RenderStyleController,
    render_style_title,
)


class RenderStyleToolbar(QToolBar):
    """"Style: [Illustrated v]" - switches the plan's drawing style."""

    def __init__(self, controller: RenderStyleController,
                 parent: QWidget | None = None) -> None:
        super().__init__(render_style_title(), parent)
        self.setObjectName("RenderStyleToolbar")  # required for QMainWindow.saveState
        self.setMovable(False)
        self.setOrientation(Qt.Orientation.Horizontal)
        self.setAccessibleName(render_style_title())
        label = QLabel(QCoreApplication.translate("RenderStyleToolbar", "Style:"), self)
        label.setContentsMargins(6, 0, 4, 0)
        self.addWidget(label)
        self._combo = controller.create_combo(self)
        label.setBuddy(self._combo)
        self.addWidget(self._combo)

    @property
    def combo(self) -> QComboBox:
        return self._combo
