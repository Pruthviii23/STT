"""System tray icon (pystray). Callbacks run on the tray thread; the app must marshal."""
from __future__ import annotations

import logging
from typing import Callable

import pystray

from .icon import make_icon

log = logging.getLogger(__name__)


class Tray:
    def __init__(self, on_toggle: Callable[[bool], None], on_open_notes: Callable[[], None],
                 on_open_folder: Callable[[], None], on_settings: Callable[[], None],
                 on_quit: Callable[[], None]):
        self._on_toggle = on_toggle
        self._on_open_notes = on_open_notes
        self._on_open_folder = on_open_folder
        self._on_settings = on_settings
        self._on_quit = on_quit
        self._enabled = True
        self._status = "Murmur"
        self._icons = {True: make_icon(64, True), False: make_icon(64, False)}
        self._icon: pystray.Icon | None = None

    def _safe(self, fn):
        def run(icon=None, item=None):
            try:
                fn()
            except Exception:
                log.exception("tray callback failed")
        return run

    def _toggle(self, icon=None, item=None):
        self._enabled = not self._enabled
        self._apply()
        try:
            self._on_toggle(self._enabled)
        except Exception:
            log.exception("on_toggle failed")

    def _menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Enabled", self._toggle, checked=lambda item: self._enabled, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open this month's notes", self._safe(self._on_open_notes)),
            pystray.MenuItem("Open notes folder", self._safe(self._on_open_folder)),
            pystray.MenuItem("Settings", self._safe(self._on_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._safe(self._on_quit)),
        )

    def _apply(self) -> None:
        ic = self._icon
        if ic is None:
            return
        try:
            ic.icon = self._icons[self._enabled]
            ic.title = self._title()
            ic.update_menu()
        except Exception:
            log.debug("tray update failed", exc_info=True)

    def _title(self) -> str:
        t = self._status if self._enabled else f"{self._status} (disabled)"
        return t[:127]

    def start(self) -> None:
        if self._icon is not None:
            return
        self._icon = pystray.Icon("murmur", self._icons[self._enabled], self._title(), self._menu())
        self._icon.run_detached()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._apply()

    def set_status(self, text: str) -> None:
        self._status = text
        self._apply()

    def stop(self) -> None:
        ic, self._icon = self._icon, None
        if ic is not None:
            try:
                ic.stop()
            except Exception:
                log.debug("tray stop failed", exc_info=True)
