# Author: joelsnl and Anthropic Claude
"""View → Look: the same palettes as the browser pages, remembered as ``ui_look``."""

from __future__ import annotations

from PySide6.QtCore import Slot
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QApplication

from core.settings import save_settings
from gui import theme


class LookActionsMixin:
    def _build_look_menu(self, menu) -> None:
        look_m = menu.addMenu("Look")
        group = QActionGroup(self)
        group.setExclusive(True)
        current = theme.normalize_look(self.session.settings.get("ui_look") or "auto")
        self._look_actions = {}
        for look_id, label in theme.LOOKS:
            if look_id == "random":
                look_m.addSeparator()
            act = QAction(label, self, checkable=True)
            act.setChecked(look_id == current)
            act.triggered.connect(lambda _checked=False, lid=look_id: self._set_look(lid))
            group.addAction(act)
            look_m.addAction(act)
            self._look_actions[look_id] = act
        app = QApplication.instance()
        try:
            app.styleHints().colorSchemeChanged.connect(self._on_system_scheme_changed)
        except AttributeError:  # Qt < 6.5: Auto stays on the colours picked at start
            pass

    @Slot(str)
    def _set_look(self, look_id: str) -> None:
        look_id = theme.normalize_look(look_id)
        self.session.settings["ui_look"] = look_id
        save_settings(self.session.settings)
        self._apply_look()

    def _apply_look(self) -> None:
        theme.apply_look(QApplication.instance(), self.session.settings.get("ui_look") or "auto")
        # Custom-painted widgets read the palette when they paint.
        if getattr(self.reader, "book", None):
            self.reader._render()
        if hasattr(self, "server_screen"):
            self.server_screen.repaint_qr()
        self.update()

    @Slot()
    def _on_system_scheme_changed(self, *_args) -> None:
        if theme.normalize_look(self.session.settings.get("ui_look") or "auto") == "auto":
            self._apply_look()
