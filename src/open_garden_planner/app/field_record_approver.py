"""The desktop approver: turns a reviewed field record into the plan's own undoable commands.

One code path for the "Field records" panel and the agent's ``place_field_record`` (design
1.7). The Qt-free planner (:mod:`services.field_record_placement`) decides *what* goes
where; this class only runs it through the commands the rest of the app uses and then
records the decision in the sidecar:

* an :class:`ObjectSpec` goes through ``AgentHost._do_agent_create_object`` (species match,
  active-layer lock refusal, canvas clamp, one ``CreateItemCommand``);
* a :class:`PolylineSpec` is one :class:`MacroCommand`: add the "Irrigation" layer when it
  is absent, then create the line on it. An existing *locked* Irrigation layer is refused,
  never unlocked;
* a :class:`ReferencePointSpec` is handed to the georeference dialog, whose fit is one undo.

The sidecar write (``status=placed``, ``placed_object_id``, siblings ``superseded``) is a
file write, not a scene change: it is not part of the undo step. Undoing a placement leaves
``placed_object_id`` dangling (ADR-080); *Reopen* returns the record and its group siblings
to ``pending``. Call every method on the Qt main thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QCoreApplication

from open_garden_planner.services import field_record_placement as frp
from open_garden_planner.services import field_records as fr

if TYPE_CHECKING:
    from open_garden_planner.app.application import GardenPlannerApp
    from open_garden_planner.core.georef import GeoReference, ReferencePoint

PlacementError = frp.PlacementError


@dataclass
class ApprovalResult:
    """What one approval did."""

    record_id: str
    kind: str
    #: UUID of the created object (None for a reference point).
    item_id: str | None = None
    undo_description: str | None = None
    #: Ids of siblings that became ``superseded`` (placement requests).
    superseded: list[str] = field(default_factory=list)
    #: True when a reference point was handed to the dialog and the user accepted the fit.
    georef_updated: bool = False
    #: True when the user closed the reference-point dialog without applying it.
    cancelled: bool = False
    #: RMS of the georeference fit after a reference point was applied, metres.
    fit_rms_m: float | None = None
    warnings: list[str] = field(default_factory=list)


class FieldRecordApprover:
    """Approve, reject and reopen field records against the open plan."""

    def __init__(self, app: GardenPlannerApp) -> None:
        self._app = app

    # -- context ---------------------------------------------------------------------------

    def context(self) -> tuple[str | None, GeoReference | None]:
        """``(saved plan path or None, georeference with the plan's north)``."""
        return self._app._agent_host._agent_field_context()

    def canvas_size(self) -> tuple[float, float]:
        """Plan canvas width and height in scene cm (for the review preview)."""
        rect = self._app.canvas_scene.canvas_rect
        return float(rect.width()), float(rect.height())

    def _require_plan_path(self) -> str:
        plan_path, _geo = self.context()
        if plan_path is None:
            raise PlacementError("The plan has not been saved yet; field records live beside the saved file.")
        return plan_path

    # -- approve ---------------------------------------------------------------------------

    def plan(self, record_id: str, *, allow_duplicate: bool = False, **overrides: Any) -> frp.Placement:
        """Dry run: what approving ``record_id`` would do (raises :class:`PlacementError`)."""
        plan_path = self._require_plan_path()
        _path, geo = self.context()
        try:
            record = fr.get_record(plan_path, record_id)
        except fr.FieldRecordError as error:
            raise PlacementError(str(error)) from error
        return frp.plan_record(record, geo, plan_path=plan_path, allow_duplicate=allow_duplicate, **overrides)

    def approve(
        self, record_id: str, *, allow_duplicate: bool = False, interactive: bool = True
    ) -> ApprovalResult:
        """Place the record on the plan (one undo step) and mark it ``placed``.

        ``interactive=False`` (the agent) never opens a dialog: a reference point is fitted
        straight into the georeference as one undo step.

        Everything that can refuse runs before the first command, so a refusal leaves the plan
        and the undo stack untouched.
        """
        plan_path = self._require_plan_path()
        placement = self.plan(record_id, allow_duplicate=allow_duplicate)
        record = fr.get_record(plan_path, record_id)
        spec = placement.spec
        result = ApprovalResult(record_id, record.kind, warnings=list(placement.warnings))
        if isinstance(spec, frp.ReferencePointSpec):
            return self._approve_reference_point(plan_path, record, spec, result, interactive)
        if isinstance(spec, frp.PolylineSpec):
            created = self._place_polyline(spec)
        else:
            created = self._place_object(spec)
        result.item_id = created["item_id"]
        result.undo_description = created["undo_description"]
        result.warnings.extend(created.get("warnings", []))
        self._mark_placed(plan_path, record, result)
        return result

    def _mark_placed(self, plan_path: str, record: fr.FieldRecord, result: ApprovalResult) -> None:
        try:
            fr.update_record(plan_path, record.id, placed_object_id=result.item_id, status="placed")
            result.superseded = [r.id for r in fr.supersede_siblings(plan_path, record.id)]
        except (fr.FieldRecordError, OSError) as error:
            # The object is already on the undo stack: a warning, never a failure.
            result.warnings.append(f"The object was placed but the record could not be updated: {error}")

    def _place_object(self, spec: frp.ObjectSpec) -> dict[str, Any]:
        host = self._app._agent_host
        try:
            created = host._do_agent_create_object(
                spec.object_type,
                spec.x,
                spec.y,
                None,
                None,
                spec.radius,
                spec.name,
                spec.species,
                text=spec.text,
                extra_metadata=spec.metadata,
            )
        except PlacementError:
            raise
        except ValueError as error:  # the create path's refusals (locked layer, bad shape)
            raise PlacementError(str(error)) from error
        warnings: list[str] = []
        if abs(created["x"] - spec.x) > 1.0 or abs(created["y"] - spec.y) > 1.0:
            warnings.append("The position was at the canvas edge and was moved inside the canvas.")
        return {"item_id": created["item_id"], "undo_description": created["undo_description"], "warnings": warnings}

    def _irrigation_layer(self, name: str) -> Any:
        scene = self._app.canvas_scene
        for layer in scene.layers:
            if layer.name.casefold() == name.casefold():
                if layer.locked:
                    raise PlacementError(
                        f"The layer {layer.name!r} is locked; unlock it before placing irrigation on it "
                        "(it is never unlocked automatically)."
                    )
                return layer
        return None

    def _place_polyline(self, spec: frp.PolylineSpec) -> dict[str, Any]:
        from open_garden_planner.agent_api import creates
        from open_garden_planner.core.commands import (
            AddLayerCommand,
            CreateItemCommand,
            MacroCommand,
        )
        from open_garden_planner.models.layer import Layer

        app = self._app
        layer = self._irrigation_layer(spec.layer_name)  # refuses a locked layer first
        canvas = app.canvas_scene.canvas_rect
        item_spec = creates.build_create_dict(
            object_type=spec.object_type,
            x=None,
            y=None,
            canvas_width_cm=canvas.width(),
            canvas_height_cm=canvas.height(),
            width=None,
            height=None,
            radius=None,
            name=spec.name,
            points=[[px, py] for px, py in spec.points],
            text=None,
            box_dx=None,
            box_dy=None,
        )
        commands: list[Any] = []
        if layer is None:
            layer = Layer(name=spec.layer_name)
            commands.append(AddLayerCommand(app.canvas_scene, layer))
        item_spec["layer_id"] = str(layer.id)
        item = app._project_manager._deserialize_item_core(item_spec)
        if item is None:
            raise PlacementError("Could not build the irrigation line from those points.")
        if spec.metadata:
            item.metadata.update(spec.metadata)
        app._agent_host._clamp_agent_items_to_canvas([item])
        commands.append(CreateItemCommand(app.canvas_scene, item, "irrigation line"))
        command = MacroCommand(
            commands,
            QCoreApplication.translate("FieldRecordApprover", "Place irrigation line"),
        )
        app.canvas_view.command_manager.execute(command)
        return {"item_id": str(item.item_id), "undo_description": command.description}

    # -- reference points ----------------------------------------------------------------------

    def _approve_reference_point(
        self,
        plan_path: str,
        record: fr.FieldRecord,
        spec: frp.ReferencePointSpec,
        result: ApprovalResult,
        interactive: bool,
    ) -> ApprovalResult:
        from open_garden_planner.core.georef import ReferencePoint

        point = ReferencePoint(spec.name, spec.lat, spec.lon, spec.x, spec.y)
        pm = self._app._project_manager
        before = pm.georef
        if interactive:
            self._app._on_set_georeference(extra_reference_points=[point])
        else:
            self._fit_headless(point)
        after = pm.georef
        kept = after is not None and any(
            p.name == point.name and p.x == point.x and p.y == point.y for p in after.reference_points
        )
        result.georef_updated = bool(kept and after != before)
        result.cancelled = not result.georef_updated
        if after is not None:
            result.fit_rms_m = after.fit_rms_m
        if result.georef_updated:
            try:
                fr.update_record(plan_path, record.id, status="placed")
            except (fr.FieldRecordError, OSError) as error:
                result.warnings.append(f"The point was added but the record could not be updated: {error}")
        return result

    def _fit_headless(self, point: ReferencePoint) -> None:
        """Agent path: the plan's reference points plus this one, fitted in ONE undo step."""
        from dataclasses import replace

        from open_garden_planner.core.commands import SetGeoreferenceCommand
        from open_garden_planner.core.georef import (
            DEFAULT_SCALE_M_PER_UNIT,
            GeorefError,
            fit_from_reference_points,
        )

        app = self._app
        pm = app._project_manager
        existing = pm.georef
        points = [*(existing.reference_points if existing else ()), point]
        if len(points) < 2:
            if existing is None:
                raise PlacementError(
                    "The plan is not georeferenced and one reference point cannot fit it; "
                    "approve a second one or set the georeference first."
                )
            # One point cannot be fitted: keep it with the georeference for the next fit
            # (what the dialog does too), as one undo step.
            kept = replace(existing, reference_points=tuple(points))
            app.canvas_view.command_manager.execute(SetGeoreferenceCommand(pm, kept, None))
            return
        try:
            fit = fit_from_reference_points(
                points,
                scale_meters_per_unit=existing.scale_meters_per_unit if existing else DEFAULT_SCALE_M_PER_UNIT,
                rotation_deg=pm.north_angle_deg,
                source="fit",
            )
        except GeorefError as error:
            raise PlacementError(f"The georeference fit was refused: {error}") from error
        geo = replace(fit.georef, parcel_boundary=existing.parcel_boundary if existing else None)
        app.canvas_view.command_manager.execute(SetGeoreferenceCommand(pm, geo, geo.rotation_deg))

    # -- reject / reopen ---------------------------------------------------------------------

    def reject(self, record_id: str) -> fr.FieldRecord:
        """Set ``status=rejected`` (kept, hideable). A placed record is refused."""
        plan_path = self._require_plan_path()
        try:
            record = fr.get_record(plan_path, record_id)
            if record.status == "placed":
                raise PlacementError(f"Record {record_id} is already placed; remove its object first.")
            return fr.update_record(plan_path, record_id, status="rejected")
        except fr.FieldRecordError as error:
            raise PlacementError(str(error)) from error

    def reject_group(self, group_id: str) -> list[fr.FieldRecord]:
        """Reject every still-open candidate of a placement-request group."""
        plan_path = self._require_plan_path()
        out = []
        for rec in fr.load_records(plan_path).records:
            if rec.group_id == group_id and rec.status in ("pending", "approved", "superseded"):
                out.append(fr.update_record(plan_path, rec.id, status="rejected"))
        return out

    def reopen(self, record_id: str) -> list[fr.FieldRecord]:
        """Back to ``pending``: the record, or its whole group (superseded, rejected, placed)."""
        plan_path = self._require_plan_path()
        try:
            record = fr.get_record(plan_path, record_id)
            if record.group_id:
                return fr.reopen_group(plan_path, record.group_id, include_rejected=True)
            if record.status == "pending":
                return []
            return [fr.update_record(plan_path, record_id, status="pending", placed_object_id=None)]
        except fr.FieldRecordError as error:
            raise PlacementError(str(error)) from error

    def object_exists(self, item_id: str | None) -> bool:
        """Whether a placed record's object is still on the plan (undo/delete leaves it dangling)."""
        if not item_id:
            return False
        try:
            self._app._agent_host._resolve_agent_read_item(item_id)
        except Exception:
            return False
        return True
