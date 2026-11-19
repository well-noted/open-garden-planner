"""Georeference calibration dialog (ADR-079).

Shows and edits the plan's georeference: the GPS position of the canvas origin,
the scale and the rotation (which IS the plan's north angle, one source of truth),
plus a table of reference points (a canvas point and the GPS reading taken there).
"Fit from reference points" solves origin, scale and rotation and reports the
residual of every point, so a mistyped corner shows up at once.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from open_garden_planner.core.georef import (
    DEFAULT_SCALE_M_PER_UNIT,
    FitResult,
    GeoReference,
    GeorefError,
    ReferencePoint,
    fit_from_reference_points,
)

_COL_NAME, _COL_LAT, _COL_LON, _COL_X, _COL_Y, _COL_RES = range(6)


def _spin(lo: float, hi: float, decimals: int, suffix: str = "") -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(lo, hi)
    spin.setDecimals(decimals)
    spin.setSuffix(suffix)
    spin.setAlignment(Qt.AlignmentFlag.AlignRight)
    spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
    return spin


class GeoreferenceDialog(QDialog):
    """Edit the plan's georeference and reference points."""

    def __init__(
        self,
        parent: QWidget | None = None,
        georef: GeoReference | None = None,
        north_angle_deg: float = 0.0,
        location: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Georeference"))
        self.setModal(True)
        self.setMinimumWidth(640)
        self._parcel = georef.parcel_boundary if georef else None
        self._source = georef.source if georef else "manual"
        self._fit: FitResult | None = None
        self._fit_rms: float | None = None
        self._original = georef
        self._ready = False
        self._touched = False
        self._setup_ui()
        lat = lon = 0.0
        if georef is not None:
            lat, lon = georef.origin_lat, georef.origin_lon
            self._scale.setValue(georef.scale_meters_per_unit)
            self._fit_rms = georef.fit_rms_m
            for p in georef.reference_points:
                self._add_row(p.name, p.lat, p.lon, p.x, p.y)
            if georef.fit_rms_m is not None:
                self._status.setText(
                    self.tr("Last fit: RMS {rms:.3f} m").format(rms=georef.fit_rms_m)
                )
        elif location and location.get("latitude") is not None:
            try:
                lat, lon = float(location["latitude"]), float(location["longitude"])
            except (TypeError, ValueError, KeyError):
                lat = lon = 0.0
        self._lat.setValue(lat)
        self._lon.setValue(lon)
        self._rotation.setValue(north_angle_deg)
        self._enabled.setChecked(georef is not None)
        self._on_enabled(georef is not None)
        for spin in (self._lat, self._lon, self._scale, self._rotation):
            spin.valueChanged.connect(self._on_hand_edit)
        self._enabled.toggled.connect(self._mark_touched)
        self._ready = True

    # -- UI ----------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        self._enabled = QCheckBox(self.tr("This plan is georeferenced"))
        self._enabled.toggled.connect(self._on_enabled)
        layout.addWidget(self._enabled)

        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)

        origin = QGroupBox(self.tr("Canvas origin and orientation"))
        form = QFormLayout(origin)
        self._lat = _spin(-89.0, 89.0, 7, "°")
        self._lon = _spin(-180.0, 180.0, 7, "°")
        self._scale = _spin(0.000001, 1000.0, 6, " m")
        self._scale.setValue(DEFAULT_SCALE_M_PER_UNIT)
        self._rotation = _spin(0.0, 359.999, 3, "°")
        form.addRow(self.tr("Latitude of the origin (0, 0):"), self._lat)
        form.addRow(self.tr("Longitude of the origin (0, 0):"), self._lon)
        form.addRow(self.tr("Metres per canvas unit:"), self._scale)
        form.addRow(self.tr("True north, clockwise from plan-up:"), self._rotation)
        note = QLabel(
            self.tr(
                "The rotation is the plan's north orientation: changing it here "
                "changes it everywhere (sun, shade, reports)."
            )
        )
        note.setWordWrap(True)
        form.addRow("", note)
        body.addWidget(origin)

        points = QGroupBox(self.tr("Reference points"))
        pv = QVBoxLayout(points)
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            [
                self.tr("Name"),
                self.tr("Latitude"),
                self.tr("Longitude"),
                self.tr("X (cm)"),
                self.tr("Y (cm)"),
                self.tr("Residual (m)"),
            ]
        )
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setMinimumHeight(140)
        pv.addWidget(self._table)
        buttons = QHBoxLayout()
        add = QPushButton(self.tr("Add point"))
        add.clicked.connect(lambda: self._add_row("", 0.0, 0.0, 0.0, 0.0))
        remove = QPushButton(self.tr("Remove selected"))
        remove.clicked.connect(self._remove_selected)
        self._fit_button = QPushButton(self.tr("Fit from reference points"))
        self._fit_button.clicked.connect(self._on_fit)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        buttons.addWidget(self._fit_button)
        pv.addLayout(buttons)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        pv.addWidget(self._status)
        hint = QLabel(
            self.tr(
                "One point sets only the position; two or more also set scale and "
                "rotation. X and Y are canvas coordinates in cm (Y points up)."
            )
        )
        hint.setWordWrap(True)
        pv.addWidget(hint)
        body.addWidget(points)
        layout.addWidget(self._body)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _on_enabled(self, on: bool) -> None:
        self._body.setEnabled(on)

    def _mark_touched(self, *_args: object) -> None:
        if self._ready:
            self._touched = True

    def _on_hand_edit(self, *_args: object) -> None:
        """A typed origin/scale/rotation is a manual georeference, not the old fit."""
        if not self._ready:
            return
        self._touched = True
        self._source = "manual"
        self._fit = None
        self._fit_rms = None

    def _add_row(self, name: str, lat: float, lon: float, x: float, y: float) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        name_edit = QLineEdit(name)
        name_edit.textChanged.connect(self._mark_touched)
        self._table.setCellWidget(row, _COL_NAME, name_edit)
        self._mark_touched()
        for col, (lo, hi, dec, value) in {
            _COL_LAT: (-90.0, 90.0, 7, lat),
            _COL_LON: (-180.0, 180.0, 7, lon),
            _COL_X: (-1e9, 1e9, 1, x),
            _COL_Y: (-1e9, 1e9, 1, y),
        }.items():
            spin = _spin(lo, hi, dec)
            spin.setValue(value)
            spin.valueChanged.connect(self._mark_touched)
            self._table.setCellWidget(row, col, spin)
        item = QTableWidgetItem("")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self._table.setItem(row, _COL_RES, item)

    def _remove_selected(self) -> None:
        for index in sorted({i.row() for i in self._table.selectedIndexes()}, reverse=True):
            self._table.removeRow(index)

    # -- data --------------------------------------------------------------

    def _points(self) -> list[ReferencePoint]:
        points = []
        for row in range(self._table.rowCount()):
            name = self._table.cellWidget(row, _COL_NAME)
            values = [self._spin_at(row, c) for c in (_COL_LAT, _COL_LON, _COL_X, _COL_Y)]
            points.append(
                ReferencePoint(
                    name.text().strip() if isinstance(name, QLineEdit) else "", *values
                )
            )
        return points

    def _spin_at(self, row: int, col: int) -> float:
        spin = self._table.cellWidget(row, col)
        return float(spin.value()) if isinstance(spin, QDoubleSpinBox) else 0.0

    def _on_fit(self) -> None:
        points = self._points()
        try:
            fit = fit_from_reference_points(
                points,
                scale_meters_per_unit=self._scale.value(),
                rotation_deg=self._rotation.value(),
                source="fit",
            )
        except GeorefError as error:
            self._status.setText(self.tr("Cannot fit: {reason}").format(reason=str(error)))
            return
        geo = fit.georef
        self._lat.setValue(geo.origin_lat)
        self._lon.setValue(geo.origin_lon)
        self._scale.setValue(geo.scale_meters_per_unit)
        self._rotation.setValue(geo.rotation_deg)
        # After the setValue calls: each of them reads as a hand edit.
        self._fit = fit
        self._fit_rms = fit.rms_m
        self._source = "fit"
        self._touched = True
        for row, residual in enumerate(fit.residuals_m):
            cell = self._table.item(row, _COL_RES)
            if cell is not None:
                cell.setText(f"{residual:.3f}")
        self._status.setText(
            self.tr("Fit of {n} point(s): RMS {rms:.3f} m").format(n=len(points), rms=fit.rms_m)
        )

    @property
    def is_georeferenced(self) -> bool:
        """False when the user unticked the box: accepting clears the georef."""
        return self._enabled.isChecked()

    @property
    def north_angle_deg(self) -> float:
        return float(self._rotation.value())

    @property
    def georef(self) -> GeoReference | None:
        """The edited georeference (rotation = the north angle shown), or None."""
        if not self._enabled.isChecked():
            return None
        if self._original is not None and not self._touched:
            # Nothing was edited: hand back the stored value, not the spin boxes'
            # rounded copy of it (7 decimals of origin, 3 of rotation).
            return self._original.with_rotation(self._rotation.value())
        return GeoReference(
            origin_lat=self._lat.value(),
            origin_lon=self._lon.value(),
            scale_meters_per_unit=self._scale.value(),
            rotation_deg=self._rotation.value(),
            parcel_boundary=self._parcel,
            reference_points=tuple(self._points()),
            fit_rms_m=self._fit_rms,
            source=self._source,
        )
