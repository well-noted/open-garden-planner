"""Desktop review panel for the field-records sidecar (WP-R, design 1.7).

The phone only writes *pending* records; the plan changes when the user approves one here
(or the agent places it). The panel lists records with kind/status filters, previews where a
record sits on the plan with its accuracy halo, shows the photo and notes, and offers
Approve / Reject / Reopen. A ``placement_request`` group (candidates sharing a ``group_id``)
opens a side-by-side comparison: approving one candidate places it and supersedes the others,
*Reject all* rejects every candidate and *Reopen* returns the group to pending.

The panel never edits the scene: every action goes through the ``approver`` (a
:class:`app.field_record_approver.FieldRecordApprover`), which uses the plan's own commands.
"""

from __future__ import annotations

from typing import Any, Protocol

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from open_garden_planner.core.i18n import format_decimal
from open_garden_planner.services import field_record_placement as frp
from open_garden_planner.services import field_records as fr
from open_garden_planner.ui.theme import set_text_role

_ID_ROLE = Qt.ItemDataRole.UserRole
_GROUP_ROLE = Qt.ItemDataRole.UserRole + 1

_KINDS = ("plant", "irrigation_path", "reference_point", "note", "measurement", "placement_request")
_STATUSES = ("pending", "approved", "rejected", "placed", "superseded")


class Approver(Protocol):
    """What the panel needs from the app (see ``FieldRecordApprover``)."""

    def context(self) -> tuple[str | None, Any]: ...
    def canvas_size(self) -> tuple[float, float]: ...
    def approve(self, record_id: str, *, allow_duplicate: bool = False) -> Any: ...
    def reject(self, record_id: str) -> Any: ...
    def reject_group(self, group_id: str) -> Any: ...
    def reopen(self, record_id: str) -> Any: ...
    def object_exists(self, item_id: str | None) -> bool: ...


class _MapPreview(QWidget):
    """The plan canvas outline with a record's point (and accuracy halo) or polyline."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(200, 150)
        self._canvas = (1.0, 1.0)
        self._points: list[tuple[float, float]] = []
        self._polyline = False
        self._halo_cm = 0.0

    def set_data(
        self, canvas: tuple[float, float], points: list[tuple[float, float]], polyline: bool, halo_cm: float
    ) -> None:
        self._canvas, self._points, self._polyline, self._halo_cm = canvas, points, polyline, halo_cm
        self.update()

    @property
    def has_position(self) -> bool:
        return bool(self._points)

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = max(self._canvas[0], 1.0), max(self._canvas[1], 1.0)
        scale = min((self.width() - 8) / w, (self.height() - 8) / h)
        ox, oy = (self.width() - w * scale) / 2.0, (self.height() - h * scale) / 2.0
        painter.fillRect(self.rect(), self.palette().window())
        painter.setPen(QPen(self.palette().mid().color(), 1))
        painter.setBrush(QBrush(self.palette().base()))
        painter.drawRect(QRectF(ox, oy, w * scale, h * scale))

        def to_px(x: float, y: float) -> QPointF:  # scene is Y-up, the widget is Y-down
            return QPointF(ox + x * scale, oy + (h - y) * scale)

        accent = QColor(200, 60, 40)
        if not self._points:
            painter.setPen(self.palette().text().color())
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.tr("No position on the plan"))
            return
        painter.setPen(QPen(accent, 2))
        if self._polyline and len(self._points) > 1:
            for a, b in zip(self._points, self._points[1:], strict=False):
                painter.drawLine(to_px(*a), to_px(*b))
        for x, y in self._points[:1] if not self._polyline else self._points[::max(1, len(self._points) - 1)]:
            centre = to_px(x, y)
            if self._halo_cm > 0:
                halo = QColor(accent)
                halo.setAlpha(50)
                painter.setBrush(QBrush(halo))
                painter.drawEllipse(centre, self._halo_cm * scale, self._halo_cm * scale)
            painter.setBrush(QBrush(accent))
            painter.drawEllipse(centre, 4.0, 4.0)


class _RecordCard(QFrame):
    """One record's photo, preview and details (used alone and, side by side, per candidate)."""

    def __init__(self, panel: FieldRecordsPanel, record: fr.FieldRecord, *, candidate: bool) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.record_id = record.id
        layout = QVBoxLayout(self)
        path, geo = panel._approver.context()
        canvas = panel._approver.canvas_size()

        preview = _MapPreview()
        geometry = frp.preview_geometry(record, geo)
        points, is_line = geometry if geometry else ([], False)
        scale = getattr(geo, "scale_meters_per_unit", 0.01) or 0.01
        halo = (record.accuracy_m or 0.0) / scale
        preview.set_data(canvas, points, is_line, halo)
        layout.addWidget(preview)
        self.preview = preview

        self.photo_label = QLabel()
        self.photo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = self._load_photo(path, record)
        if pixmap is not None:
            self.photo_label.setPixmap(pixmap)
        else:
            self.photo_label.setText(self.tr("No photo"))
        layout.addWidget(self.photo_label)

        details = QLabel(panel.describe(record))
        details.setWordWrap(True)
        details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(details)
        self.details = details

        buttons = QHBoxLayout()
        self.approve_button = QPushButton(self.tr("Approve this one") if candidate else self.tr("Approve"))
        self.approve_button.setEnabled(record.status in frp.APPROVABLE_STATUSES)
        self.approve_button.clicked.connect(lambda: panel.approve_record(record.id))
        buttons.addWidget(self.approve_button)
        if not candidate:
            self.reject_button = QPushButton(self.tr("Reject"))
            self.reject_button.setEnabled(record.status in ("pending", "approved"))
            self.reject_button.clicked.connect(lambda: panel.reject_record(record.id))
            buttons.addWidget(self.reject_button)
            self.reopen_button = QPushButton(self.tr("Reopen"))
            self.reopen_button.setEnabled(panel.can_reopen(record))
            self.reopen_button.clicked.connect(lambda: panel.reopen_record(record.id))
            buttons.addWidget(self.reopen_button)
        layout.addLayout(buttons)

    @staticmethod
    def _load_photo(plan_path: str | None, record: fr.FieldRecord) -> QPixmap | None:
        if not record.photo:
            return None
        try:
            pixmap = QPixmap(str(fr.resolve_photo(plan_path, record)))
        except (fr.FieldRecordError, OSError):
            return None
        if pixmap.isNull():
            return None
        return pixmap.scaledToWidth(220, Qt.TransformationMode.SmoothTransformation)


class FieldRecordsPanel(QWidget):
    """List, filter, preview and decide on the plan's field records."""

    #: Emitted after any approve / reject / reopen (the plan or the sidecar changed).
    records_changed = pyqtSignal()

    def __init__(self, approver: Approver, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._approver = approver
        self._records: dict[str, fr.FieldRecord] = {}
        self._selected_id: str | None = None

        root = QVBoxLayout(self)
        header = QLabel(self.tr("Field records"))
        set_text_role(header, "h2")
        root.addWidget(header)

        filters = QHBoxLayout()
        filters.addWidget(QLabel(self.tr("Kind:")))
        self.kind_filter = QComboBox()
        self.kind_filter.addItem(self.tr("All kinds"), "")
        for kind in _KINDS:
            self.kind_filter.addItem(self.kind_name(kind), kind)
        filters.addWidget(self.kind_filter)
        filters.addWidget(QLabel(self.tr("Status:")))
        self.status_filter = QComboBox()
        self.status_filter.addItem(self.tr("All statuses"), "")
        for status in _STATUSES:
            self.status_filter.addItem(self.status_name(status), status)
        self.status_filter.setCurrentIndex(self.status_filter.findData("pending"))
        filters.addWidget(self.status_filter)
        filters.addStretch(1)
        self.refresh_button = QPushButton(self.tr("Refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        filters.addWidget(self.refresh_button)
        root.addLayout(filters)
        self.kind_filter.currentIndexChanged.connect(self.refresh)
        self.status_filter.currentIndexChanged.connect(self.refresh)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([self.tr("Record"), self.tr("Kind"), self.tr("Status")])
        self.tree.setMinimumWidth(260)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        splitter.addWidget(self.tree)
        self._detail_scroll = QScrollArea()
        self._detail_scroll.setWidgetResizable(True)
        splitter.addWidget(self._detail_scroll)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        self.message = QLabel()
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        self.refresh()

    # -- text -------------------------------------------------------------------------------

    def kind_name(self, kind: str) -> str:
        names = {
            "plant": self.tr("Plant"),
            "irrigation_path": self.tr("Irrigation line"),
            "reference_point": self.tr("Reference point"),
            "note": self.tr("Note"),
            "measurement": self.tr("Measurement"),
            "placement_request": self.tr("Placement request"),
        }
        return names.get(kind, kind)

    def status_name(self, status: str) -> str:
        names = {
            "pending": self.tr("Pending"),
            "approved": self.tr("Approved"),
            "rejected": self.tr("Rejected"),
            "placed": self.tr("Placed"),
            "superseded": self.tr("Superseded"),
        }
        return names.get(status, status)

    def describe(self, rec: fr.FieldRecord) -> str:
        lines = [f"{self.kind_name(rec.kind)} - {self.status_name(rec.status)}"]
        for label, value in (
            (self.tr("Name"), rec.name or rec.plant_name),
            (self.tr("Wanted"), rec.what),
            (self.tr("Requested by"), rec.requester),
            (self.tr("Notes"), rec.notes),
        ):
            if value:
                lines.append(f"{label}: {value}")
        if rec.accuracy_m is not None:
            lines.append(self.tr("Accuracy: {m} m").format(m=format_decimal(rec.accuracy_m, 1)))
        if rec.bearing_deg is not None:
            lines.append(self.tr("Bearing: {deg}°").format(deg=format_decimal(rec.bearing_deg, 0)))
        if rec.fix_mode:
            lines.append(self.tr("Fix: {mode}").format(mode=rec.fix_mode))
        if rec.precision_hint:
            lines.append(self.tr("Precision: {hint}").format(hint=rec.precision_hint))
        if rec.status == "placed" and rec.placed_object_id and not self._approver.object_exists(rec.placed_object_id):
            lines.append(self.tr("The placed object is no longer on the plan."))
        return "\n".join(lines)

    # -- list ---------------------------------------------------------------------------------

    def _load(self) -> list[fr.FieldRecord]:
        path, _geo = self._approver.context()
        if path is None:
            self.message.setText(self.tr("Save the plan first: field records are stored beside the saved file."))
            return []
        loaded = fr.load_records(path)
        if loaded.warnings:
            self.message.setText(loaded.warnings[0])
        return sorted(loaded.records, key=lambda r: r.timestamp[:19], reverse=True)

    def _matches(self, rec: fr.FieldRecord) -> bool:
        kind = self.kind_filter.currentData()
        status = self.status_filter.currentData()
        return (not kind or rec.kind == kind) and (not status or rec.status == status)

    def refresh(self, *_args: object) -> None:
        records = self._load()
        self._records = {r.id: r for r in records}
        self.tree.blockSignals(True)
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for rec in records:
            if not self._matches(rec):
                continue
            row = self._row(rec)
            if rec.group_id:
                parent = groups.get(rec.group_id)
                if parent is None:
                    parent = QTreeWidgetItem([rec.what or self.tr("Placement request"), self.kind_name(rec.kind), ""])
                    parent.setData(0, _GROUP_ROLE, rec.group_id)
                    self.tree.addTopLevelItem(parent)
                    groups[rec.group_id] = parent
                parent.addChild(row)
                parent.setText(2, self.tr("{n} candidate(s)").format(n=parent.childCount()))
            else:
                self.tree.addTopLevelItem(row)
        self.tree.expandAll()
        self.tree.blockSignals(False)
        self._restore_selection()

    def _row(self, rec: fr.FieldRecord) -> QTreeWidgetItem:
        label = rec.name or rec.plant_name or rec.what or rec.id[:8]
        item = QTreeWidgetItem([label, self.kind_name(rec.kind), self.status_name(rec.status)])
        item.setData(0, _ID_ROLE, rec.id)
        return item

    def visible_record_ids(self) -> list[str]:
        out: list[str] = []
        it = self.tree
        for i in range(it.topLevelItemCount()):
            top = it.topLevelItem(i)
            if top.data(0, _ID_ROLE):
                out.append(top.data(0, _ID_ROLE))
            out.extend(top.child(j).data(0, _ID_ROLE) for j in range(top.childCount()))
        return out

    def _item_for(self, record_id: str) -> QTreeWidgetItem | None:
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.data(0, _ID_ROLE) == record_id:
                return top
            for j in range(top.childCount()):
                if top.child(j).data(0, _ID_ROLE) == record_id:
                    return top.child(j)
        return None

    def _restore_selection(self) -> None:
        item = self._item_for(self._selected_id) if self._selected_id else None
        if item is not None:
            self.tree.setCurrentItem(item)
        self._show_detail()

    def select_record(self, record_id: str) -> None:
        if record_id not in self._records:
            self.refresh()  # a record written since the last refresh (phone, agent)
        self._selected_id = record_id
        item = self._item_for(record_id)
        if item is not None:
            self.tree.setCurrentItem(item)
        self._show_detail()

    def _on_current_changed(self, current: QTreeWidgetItem | None, _prev: object) -> None:
        if current is not None and current.data(0, _ID_ROLE):
            self._selected_id = current.data(0, _ID_ROLE)
        elif current is not None and current.childCount():
            self._selected_id = current.child(0).data(0, _ID_ROLE)
        self._show_detail()

    # -- detail -------------------------------------------------------------------------------

    def _show_detail(self) -> None:
        rec = self._records.get(self._selected_id or "")
        holder = QWidget()
        layout = QVBoxLayout(holder)
        self.cards: list[_RecordCard] = []
        self.group_buttons: dict[str, QPushButton] = {}
        if rec is None:
            layout.addWidget(QLabel(self.tr("Select a record to review it.")))
        elif rec.group_id:
            self._group_view(layout, rec)
        else:
            card = _RecordCard(self, rec, candidate=False)
            self.cards.append(card)
            layout.addWidget(card)
        layout.addStretch(1)
        self._detail_scroll.setWidget(holder)

    def _group_view(self, layout: QVBoxLayout, rec: fr.FieldRecord) -> None:
        siblings = [r for r in self._records.values() if r.group_id == rec.group_id]
        siblings.sort(key=lambda r: r.timestamp[:19])
        title = QLabel(self.tr("Placement request: {what}").format(what=rec.what or "-"))
        set_text_role(title, "h2")
        layout.addWidget(title)
        row = QHBoxLayout()
        for sibling in siblings:
            card = _RecordCard(self, sibling, candidate=True)
            self.cards.append(card)
            row.addWidget(card)
        layout.addLayout(row)
        bar = QHBoxLayout()
        reject_all = QPushButton(self.tr("Reject all"))
        reject_all.setEnabled(any(s.status in ("pending", "approved") for s in siblings))
        reject_all.clicked.connect(lambda: self.reject_group(rec.group_id or ""))
        reopen = QPushButton(self.tr("Reopen group"))
        reopen.setEnabled(any(self.can_reopen(s) for s in siblings))
        reopen.clicked.connect(lambda: self.reopen_record(rec.id))
        bar.addWidget(reject_all)
        bar.addWidget(reopen)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.reject_all_button, self.reopen_group_button = reject_all, reopen

    def can_reopen(self, rec: fr.FieldRecord) -> bool:
        if rec.status in ("rejected", "superseded"):
            return True
        if rec.status == "approved":  # an applied reference point (no object to lose)
            return rec.kind == "reference_point"
        if rec.status == "placed":
            return not self._approver.object_exists(rec.placed_object_id) and rec.kind != "reference_point"
        return False

    # -- actions ------------------------------------------------------------------------------

    def _run(self, action: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            result = action(*args, **kwargs)
        except (ValueError, OSError) as error:  # PlacementError and FieldRecordError are ValueErrors
            self.message.setText(str(error))
            return None
        self.message.setText("")
        self.refresh()
        self.records_changed.emit()
        return result

    def approve_record(self, record_id: str) -> Any:
        self._selected_id = record_id
        result = self._run(self._approver.approve, record_id)
        if result is not None and getattr(result, "warnings", None):
            self.message.setText(" ".join(result.warnings))
        elif result is not None and getattr(result, "cancelled", False):
            self.message.setText(self.tr("The reference point was not applied."))
        return result

    def reject_record(self, record_id: str) -> Any:
        return self._run(self._approver.reject, record_id)

    def reject_group(self, group_id: str) -> Any:
        return self._run(self._approver.reject_group, group_id)

    def reopen_record(self, record_id: str) -> Any:
        return self._run(self._approver.reopen, record_id)


class FieldRecordsDialog(QDialog):
    """Non-modal window hosting the panel (opened from the Plan menu)."""

    def __init__(self, approver: Approver, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Field records"))
        self.setModal(False)
        self.resize(980, 640)
        layout = QVBoxLayout(self)
        self.panel = FieldRecordsPanel(approver, self)
        layout.addWidget(self.panel)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.panel.refresh()
