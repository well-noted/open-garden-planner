"""The render style's UI state (Package 4, WP7): controller sync, export group."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtWidgets import QWidget

from open_garden_planner.core.render_style import RENDER_STYLES, RenderStyle
from open_garden_planner.ui.render_style_controller import (
    RenderStyleController,
    callouts_enabled_for,
    render_style_label,
    vector_paper_texture,
)
from open_garden_planner.ui.widgets.export_style_group import (
    FORMAT_PNG,
    FORMAT_SVG,
    ExportStyleGroup,
    fidelity_note,
)


class _Settings:
    def __init__(self, style: str = "illustrated", callouts: bool = False) -> None:
        self.render_style = style
        self.cad_callouts = callouts
        self.export_render_style_override = "follow"
        self.export_paper_texture_in_vector = False


class _Scene:
    def __init__(self) -> None:
        self.render_style = RenderStyle.ILLUSTRATED
        self.calls: list[RenderStyle] = []

    def set_render_style(self, style: RenderStyle) -> None:
        self.render_style = style
        self.calls.append(style)


class _Workspace:
    def __init__(self) -> None:
        self.styles: list[str] = []

    def set_render_style(self, style: str) -> None:
        self.styles.append(style)


def _controller(qtbot: Any, settings: _Settings | None = None):
    settings = settings or _Settings()
    scene = _Scene()
    controller = RenderStyleController(scene, settings_getter=lambda: settings)
    return controller, scene, settings


def test_labels_are_the_agreed_words(qtbot: Any) -> None:
    assert [render_style_label(s) for s in RENDER_STYLES] == [
        "Illustrated", "Architectural CAD", "Line & Wash"]


def test_action_text_does_not_turn_the_ampersand_into_a_mnemonic(qtbot: Any) -> None:
    controller, _, _ = _controller(qtbot)
    assert controller.action(RenderStyle.LINE_AND_WASH).text().replace("&&", "&") == "Line & Wash"
    assert controller.action(RenderStyle.LINE_AND_WASH).text() == "Line && Wash"


def test_startup_restores_the_stored_style_on_the_canvas(qtbot: Any) -> None:
    controller, scene, _ = _controller(qtbot, _Settings("line_and_wash"))
    assert scene.render_style is RenderStyle.LINE_AND_WASH
    assert controller.action(RenderStyle.LINE_AND_WASH).isChecked()
    assert not controller.action(RenderStyle.ILLUSTRATED).isChecked()


def test_unknown_stored_style_reads_as_illustrated(qtbot: Any) -> None:
    controller, scene, _ = _controller(qtbot, _Settings("nonsense"))
    assert controller.style is RenderStyle.ILLUSTRATED
    assert scene.render_style is RenderStyle.ILLUSTRATED


def test_menu_action_switches_scene_workspace_setting_and_combo(qtbot: Any) -> None:
    controller, scene, settings = _controller(qtbot)
    workspace = _Workspace()
    controller.attach_workspace(workspace)
    parent = QWidget()
    qtbot.addWidget(parent)
    combo = controller.create_combo(parent)
    seen: list[str] = []
    controller.style_changed.connect(seen.append)
    controller.action(RenderStyle.CAD).trigger()
    assert scene.render_style is RenderStyle.CAD
    assert workspace.styles[-1] == "cad"
    assert settings.render_style == "cad"
    assert combo.currentData() == "cad"
    assert seen == ["cad"]


def test_combo_choice_checks_the_menu_action(qtbot: Any) -> None:
    controller, scene, settings = _controller(qtbot)
    parent = QWidget()
    qtbot.addWidget(parent)
    combo = controller.create_combo(parent)
    combo.setCurrentIndex(2)
    combo.activated.emit(2)
    assert scene.render_style is RenderStyle.LINE_AND_WASH
    assert controller.action(RenderStyle.LINE_AND_WASH).isChecked()
    assert settings.render_style == "line_and_wash"


def test_reselecting_the_current_style_emits_nothing(qtbot: Any) -> None:
    controller, _, _ = _controller(qtbot)
    seen: list[str] = []
    controller.style_changed.connect(seen.append)
    controller.set_style(RenderStyle.ILLUSTRATED)
    assert seen == []


def test_callouts_toggle_is_enabled_only_in_cad(qtbot: Any) -> None:
    controller, _, settings = _controller(qtbot)
    assert not controller.callouts_action.isEnabled()
    controller.set_style(RenderStyle.CAD)
    assert controller.callouts_action.isEnabled()
    controller.callouts_action.setChecked(True)
    assert settings.cad_callouts is True
    controller.set_style(RenderStyle.LINE_AND_WASH)
    assert not controller.callouts_action.isEnabled()
    assert callouts_enabled_for("cad") and not callouts_enabled_for("illustrated")


@pytest.mark.parametrize("style", ["illustrated", "cad", "line_and_wash"])
def test_fidelity_note_only_for_vector_formats(qtbot: Any, style: str) -> None:
    assert fidelity_note(RenderStyle(style), FORMAT_PNG) == ""
    note = fidelity_note(RenderStyle(style), FORMAT_SVG)
    assert bool(note) == (style != "illustrated")


def test_export_group_defaults_to_follow_and_commits_choice(qtbot: Any) -> None:
    settings = _Settings()
    group = ExportStyleGroup(RenderStyle.CAD, FORMAT_SVG, settings_getter=lambda: settings)
    qtbot.addWidget(group)
    assert group.override() is None
    assert group.effective_style() is RenderStyle.CAD
    assert "Architectural CAD" in group.combo.itemText(0)
    assert not group.note_label.isHidden() or group.note_label.text()
    group.combo.setCurrentIndex(group.combo.findData("line_and_wash"))
    assert group.override() is RenderStyle.LINE_AND_WASH
    assert group.note_label.text() == "SVG/PDF: washes without paper texture or glazing"
    assert not group.paper_texture()          # opt-in
    group.grain_checkbox.setChecked(True)
    assert group.paper_texture()
    group.commit()
    assert settings.export_render_style_override == "line_and_wash"
    assert settings.export_paper_texture_in_vector is True


def test_export_group_remembers_the_last_choice(qtbot: Any) -> None:
    settings = _Settings()
    settings.export_render_style_override = "cad"
    group = ExportStyleGroup(RenderStyle.ILLUSTRATED, FORMAT_PNG,
                             settings_getter=lambda: settings)
    qtbot.addWidget(group)
    assert group.override() is RenderStyle.CAD
    assert not group.paper_texture()          # a raster format has no texture option


def test_vector_paper_texture_context_restores_the_default(qtbot: Any) -> None:
    from open_garden_planner.ui.canvas.style.line_wash import PAPER_TEXTURE_IN_VECTOR

    assert PAPER_TEXTURE_IN_VECTOR.get() is False
    with vector_paper_texture(True):
        assert PAPER_TEXTURE_IN_VECTOR.get() is True
    assert PAPER_TEXTURE_IN_VECTOR.get() is False
