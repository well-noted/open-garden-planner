"""The "Style" group of the export dialogs (Package 4, WP7).

A combo with "Same as canvas (...)" (the default) and the three render styles, a
fidelity note for the vector formats, and - for SVG and PDF - the opt-in checkbox
that embeds the Line & Wash paper grain as a raster under the vectors.

The last choice is remembered in ``export/render_style_override`` (``follow`` by
default); the grain checkbox in ``export/paper_texture_in_vector`` (off).
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QCheckBox, QComboBox, QGroupBox, QLabel, QVBoxLayout, QWidget

from open_garden_planner.core.render_style import (
    RENDER_STYLES,
    RenderStyle,
    normalise_render_style,
)
from open_garden_planner.ui.render_style_controller import render_style_label

#: The output kinds the group knows.
FORMAT_PNG = "png"
FORMAT_SVG = "svg"
FORMAT_PDF = "pdf"
FORMAT_PRINT = "print"
VECTOR_FORMATS = (FORMAT_SVG, FORMAT_PDF)

FOLLOW = "follow"


def fidelity_note(style: RenderStyle, output_format: str) -> str:
    """What the output keeps of ``style`` - empty when it is faithful.

    CAD is fully vector in SVG and PDF; Line & Wash is an approximation there
    (design 2.5); raster outputs and print keep everything."""
    if output_format not in VECTOR_FORMATS:
        return ""
    if style is RenderStyle.CAD:
        return QCoreApplication.translate(
            "ExportStyleGroup", "Architectural CAD is fully vector in SVG and PDF.")
    if style is RenderStyle.LINE_AND_WASH:
        return QCoreApplication.translate(
            "ExportStyleGroup", "SVG/PDF: washes without paper texture or glazing")
    return ""


class ExportStyleGroup(QGroupBox):
    """The style choice of one export dialog."""

    def __init__(self, canvas_style: RenderStyle | str, output_format: str,
                 parent: QWidget | None = None, *,
                 settings_getter: Any = None) -> None:
        super().__init__(QCoreApplication.translate("ExportStyleGroup", "Style"), parent)
        self._format = output_format
        self._canvas_style = normalise_render_style(canvas_style)
        self._settings_getter = settings_getter or _app_settings
        settings = self._settings_getter()

        layout = QVBoxLayout(self)
        self._combo = QComboBox(self)
        self._combo.addItem("", FOLLOW)
        for style in RENDER_STYLES:
            self._combo.addItem(render_style_label(style), style.value)
        self._combo.setAccessibleName(QCoreApplication.translate("ExportStyleGroup", "Export style"))
        self._combo.setToolTip(QCoreApplication.translate(
            "ExportStyleGroup", "Draw this output in the canvas style or in another style"))
        layout.addWidget(self._combo)

        self._note = QLabel(self)
        self._note.setWordWrap(True)
        self._note.setProperty("secondary", True)
        self._note.setAccessibleName(QCoreApplication.translate("ExportStyleGroup", "Style fidelity note"))
        layout.addWidget(self._note)

        self._grain = QCheckBox(
            QCoreApplication.translate("ExportStyleGroup", "Include paper texture (raster)"), self)
        self._grain.setToolTip(QCoreApplication.translate(
            "ExportStyleGroup", "Embed the Line & Wash paper grain as one raster image under the vector drawing"))
        self._grain.setChecked(bool(getattr(settings, "export_paper_texture_in_vector", False)))
        layout.addWidget(self._grain)

        self._refresh_follow_text()
        stored = settings.export_render_style_override
        index = self._combo.findData(stored)
        self._combo.setCurrentIndex(index if index >= 0 else 0)
        self._combo.currentIndexChanged.connect(self._refresh)
        self._refresh()

    # ── reads ────────────────────────────────────────────────────────────────

    @property
    def combo(self) -> QComboBox:
        return self._combo

    @property
    def note_label(self) -> QLabel:
        return self._note

    @property
    def grain_checkbox(self) -> QCheckBox:
        return self._grain

    def override(self) -> RenderStyle | None:
        """The style chosen for this output; ``None`` follows the canvas."""
        data = self._combo.currentData()
        if data == FOLLOW:
            return None
        return normalise_render_style(data, self._canvas_style)

    def effective_style(self) -> RenderStyle:
        """The style the output is drawn in."""
        return self.override() or self._canvas_style

    def paper_texture(self) -> bool:
        """Embed the paper grain in the vector output (only meaningful for SVG/PDF Line & Wash)."""
        return (self._format in VECTOR_FORMATS
                and self.effective_style() is RenderStyle.LINE_AND_WASH
                and self._grain.isChecked())

    def commit(self) -> None:
        """Remember the choice (called when the dialog is accepted)."""
        settings = self._settings_getter()
        settings.export_render_style_override = str(self._combo.currentData())
        if self._format in VECTOR_FORMATS:
            settings.export_paper_texture_in_vector = self._grain.isChecked()

    # ── internals ────────────────────────────────────────────────────────────

    def _refresh_follow_text(self) -> None:
        self._combo.setItemText(0, QCoreApplication.translate(
            "ExportStyleGroup", "Same as canvas ({style})").format(
                style=render_style_label(self._canvas_style)))

    def _refresh(self) -> None:
        style = self.effective_style()
        text = fidelity_note(style, self._format)
        self._note.setText(text)
        self._note.setVisible(bool(text))
        grain_relevant = (self._format in VECTOR_FORMATS
                          and style is RenderStyle.LINE_AND_WASH)
        self._grain.setVisible(self._format in VECTOR_FORMATS)
        self._grain.setEnabled(grain_relevant)


def _app_settings() -> Any:
    from open_garden_planner.app.settings import get_settings

    return get_settings()
