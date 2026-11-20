"""The render style's UI state: one controller behind every surface (Package 4, WP7).

The View menu's "Render Style" radio group, the toolbar combo and the 3D pane's
style button all show ONE value, owned here. Choosing a style:

* switches the running 2D canvas (``CanvasScene.set_render_style``: a view
  preference - it moves the paint revision only, marks nothing dirty and pushes
  no undo entry);
* switches the 3D view (``Workspace.set_render_style`` -> ``LookController`` ->
  the Quick3D host; Line & Wash draws the NPR effect, CAD stays plain 3D);
* stores the choice (``appearance/render_style``) so the next start restores it.

The "Leader callouts" toggle (``appearance/cad_callouts``) is enabled only while
the style is CAD. The surfaces are synchronised from here, never from each
other, so a change from any of them reaches all of them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from PyQt6.QtCore import QCoreApplication, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import QComboBox

from open_garden_planner.core.render_style import (
    RENDER_STYLES,
    RenderStyle,
    normalise_render_style,
)


def render_style_label(style: RenderStyle | str) -> str:
    """The translated name of ``style`` (literals, so the translation tools see them)."""
    resolved = normalise_render_style(style)
    if resolved is RenderStyle.CAD:
        return QCoreApplication.translate("RenderStyle", "Architectural CAD")
    if resolved is RenderStyle.LINE_AND_WASH:
        return QCoreApplication.translate("RenderStyle", "Line & Wash")
    return QCoreApplication.translate("RenderStyle", "Illustrated")


def render_style_tooltip(style: RenderStyle | str) -> str:
    """One sentence on what ``style`` looks like."""
    resolved = normalise_render_style(style)
    if resolved is RenderStyle.CAD:
        return QCoreApplication.translate(
            "RenderStyle", "Black ink on white paper, hatched buildings and exact line weights")
    if resolved is RenderStyle.LINE_AND_WASH:
        return QCoreApplication.translate(
            "RenderStyle", "Hand-drawn ink lines with soft watercolour washes on paper")
    return QCoreApplication.translate("RenderStyle", "The full-colour textured plan")


def leader_callouts_label() -> str:
    return QCoreApplication.translate("RenderStyle", "Leader callouts")


def render_style_title() -> str:
    return QCoreApplication.translate("RenderStyle", "Render Style")


def menu_text(text: str) -> str:
    """``text`` for a QAction: an ampersand in a name is not a mnemonic."""
    return text.replace("&", "&&")


def callouts_enabled_for(style: RenderStyle | str) -> bool:
    """The Leader callouts toggle works only in CAD."""
    return normalise_render_style(style) is RenderStyle.CAD


@contextmanager
def vector_paper_texture(enabled: bool) -> Iterator[None]:
    """Embed the Line & Wash paper grain in the SVG/PDF drawn inside this block
    (the opt-in checkbox of the export dialogs; off, the vector outputs omit it)."""
    from open_garden_planner.ui.canvas.style.line_wash import PAPER_TEXTURE_IN_VECTOR

    token = PAPER_TEXTURE_IN_VECTOR.set(bool(enabled))
    try:
        yield
    finally:
        PAPER_TEXTURE_IN_VECTOR.reset(token)


class RenderStyleController(QObject):
    """Owns the render style shown by the app and keeps every surface in step."""

    #: The style changed (a ``RenderStyle`` id), whoever asked.
    style_changed = pyqtSignal(str)
    #: The Leader callouts toggle changed.
    callouts_changed = pyqtSignal(bool)

    def __init__(self, scene: Any, parent: QObject | None = None, *,
                 settings_getter: Callable[[], Any] | None = None) -> None:
        super().__init__(parent)
        self._scene = scene
        self._settings_getter = settings_getter or _app_settings
        self._workspace: Any = None
        self._combos: list[QComboBox] = []
        self._syncing = False
        settings = self._settings_getter()
        self._style = normalise_render_style(settings.render_style)
        self._callouts = bool(settings.cad_callouts)

        self._group = QActionGroup(self)
        self._group.setExclusive(True)
        self._actions: dict[RenderStyle, QAction] = {}
        for style in RENDER_STYLES:
            action = QAction(menu_text(render_style_label(style)), self)
            action.setCheckable(True)
            action.setData(style.value)
            action.setToolTip(render_style_tooltip(style))
            action.setStatusTip(render_style_tooltip(style))
            action.setChecked(style is self._style)
            action.triggered.connect(lambda _checked=False, s=style: self.set_style(s))
            self._group.addAction(action)
            self._actions[style] = action

        self._callouts_action = QAction(menu_text(leader_callouts_label()), self)
        self._callouts_action.setCheckable(True)
        self._callouts_action.setChecked(self._callouts)
        self._callouts_action.setToolTip(QCoreApplication.translate(
            "RenderStyle", "Label plants with leader lines (Architectural CAD only)"))
        self._callouts_action.setStatusTip(self._callouts_action.toolTip())
        self._callouts_action.toggled.connect(self.set_callouts)
        self._sync_callouts_enabled()
        # restore the stored preference on the running canvas (no persist, no signal storm)
        self._scene.set_render_style(self._style)

    # ── reads ────────────────────────────────────────────────────────────────

    @property
    def style(self) -> RenderStyle:
        return self._style

    @property
    def callouts(self) -> bool:
        return self._callouts

    def action(self, style: RenderStyle) -> QAction:
        """The checkable action of ``style`` (the same action in every menu)."""
        return self._actions[style]

    def actions(self) -> list[QAction]:
        """The three style actions, in ``RENDER_STYLES`` order."""
        return [self._actions[style] for style in RENDER_STYLES]

    @property
    def callouts_action(self) -> QAction:
        return self._callouts_action

    # ── surfaces ─────────────────────────────────────────────────────────────

    def create_combo(self, parent: Any = None) -> QComboBox:
        """A style combo (the toolbar's) kept in step with the others."""
        combo = QComboBox(parent)
        for style in RENDER_STYLES:
            combo.addItem(render_style_label(style), style.value)
            combo.setItemData(combo.count() - 1, render_style_tooltip(style),
                              Qt.ItemDataRole.ToolTipRole)
        combo.setCurrentIndex(RENDER_STYLES.index(self._style))
        combo.setToolTip(QCoreApplication.translate(
            "RenderStyle", "Choose how the plan is drawn"))
        combo.setAccessibleName(render_style_title())
        combo.setAccessibleDescription(QCoreApplication.translate(
            "RenderStyle", "Switches the plan between Illustrated, Architectural CAD and Line & Wash"))
        combo.activated.connect(self._on_combo)
        self._combos.append(combo)
        return combo

    def attach_workspace(self, workspace: Any) -> None:
        """The 3D workspace: it follows the style (its look is built when 3D first opens)."""
        self._workspace = workspace
        workspace.set_render_style(self._style.value)

    def retranslate(self) -> None:
        """Re-read every text (the language changed)."""
        for style, action in self._actions.items():
            action.setText(menu_text(render_style_label(style)))
        self._callouts_action.setText(menu_text(leader_callouts_label()))
        for combo in self._combos:
            for index, style in enumerate(RENDER_STYLES):
                combo.setItemText(index, render_style_label(style))

    # ── writes ───────────────────────────────────────────────────────────────

    def set_style(self, style: RenderStyle | str) -> None:
        """Show the plan (2D and 3D) in ``style`` and remember it. No undo entry, never dirty."""
        new = normalise_render_style(style, self._style)
        self._scene.set_render_style(new)       # idempotent
        self._settings_getter().render_style = new.value
        if self._workspace is not None:
            self._workspace.set_render_style(new.value)
        changed = new is not self._style
        self._style = new
        self._sync_surfaces()
        if changed:
            self.style_changed.emit(new.value)

    def set_callouts(self, enabled: bool) -> None:
        """The Leader callouts preference (it acts only in CAD)."""
        enabled = bool(enabled)
        self._settings_getter().cad_callouts = enabled
        if enabled == self._callouts:
            return
        self._callouts = enabled
        if self._callouts_action.isChecked() != enabled:
            self._callouts_action.setChecked(enabled)
        self.callouts_changed.emit(enabled)

    # ── internals ────────────────────────────────────────────────────────────

    def _on_combo(self, index: int) -> None:
        if self._syncing or not 0 <= index < len(RENDER_STYLES):
            return
        self.set_style(RENDER_STYLES[index])

    def _sync_surfaces(self) -> None:
        self._syncing = True
        try:
            for style, action in self._actions.items():
                action.setChecked(style is self._style)
            for combo in self._combos:
                combo.setCurrentIndex(RENDER_STYLES.index(self._style))
        finally:
            self._syncing = False
        self._sync_callouts_enabled()

    def _sync_callouts_enabled(self) -> None:
        self._callouts_action.setEnabled(callouts_enabled_for(self._style))


def _app_settings() -> Any:
    from open_garden_planner.app.settings import get_settings

    return get_settings()

