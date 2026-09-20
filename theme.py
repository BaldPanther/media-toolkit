"""Цвета подсветки строк под системную тему.

Общие для всех вкладок: таблицы дорожек, EDL и медиатеки красят строки одними
и теми же тегами. Вынесено из `app.py` отдельным модулем, чтобы `metaui.py` мог
их взять, не импортируя главное окно (иначе получилось бы кольцо импортов).
"""
from __future__ import annotations

import sys
import tkinter as tk

# Цвет «основного действия» — им подсвечиваются кнопки, которые логично нажать
# следующими. Светлый, чтобы поверх читался тёмный текст в обеих системных темах.
ACCENT = "#6fa8f0"
ACCENT_HOVER = "#8bbcf5"
ACCENT_PRESSED = "#5b96e0"
ACCENT_TEXT = "#10243a"
ACCENT_OFF = "#4a4a4a"
ACCENT_OFF_TEXT = "#8a8a8a"

_HAND = "pointinghand" if sys.platform == "darwin" else "hand2"


class AccentButton(tk.Label):
    """Кнопка основного действия — с настоящей заливкой, а не серая системная.

    Почему Label, а не кнопка: в теме aqua ttk рисует кнопку силами системы и
    цвета из `ttk.Style` игнорирует целиком, а у `tk.Button` на macOS
    `background` не применяется вовсе — красится только рамка через
    `highlightbackground`, внутри остаётся белое. Проверено скриншотами.
    Поэтому берём Label и добавляем ему клик, наведение и состояние руками.
    """

    def __init__(self, parent, text: str = "", command=None,
                 state: str = "normal", **kwargs):
        super().__init__(parent, text=text, padx=12, pady=5,
                         bg=ACCENT, fg=ACCENT_TEXT, cursor=_HAND, **kwargs)
        self._command = command
        self._enabled = True
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        if state != "normal":
            self._set_enabled(False)

    # tkinter зовёт configure(state=…) из общего кода (например, set_busy),
    # поэтому состояние перехватываем здесь, а не отдельным методом.
    def configure(self, cnf=None, **kwargs):
        if "state" in kwargs:
            self._set_enabled(str(kwargs.pop("state")) == "normal")
        if cnf or kwargs:
            return super().configure(cnf, **kwargs)
        return super().configure()

    config = configure

    def _set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        super().configure(bg=ACCENT if enabled else ACCENT_OFF,
                          fg=ACCENT_TEXT if enabled else ACCENT_OFF_TEXT,
                          cursor=_HAND if enabled else "")

    def _on_enter(self, _event):
        if self._enabled:
            super().configure(bg=ACCENT_HOVER)

    def _on_leave(self, _event):
        if self._enabled:
            super().configure(bg=ACCENT)

    def _on_press(self, _event):
        if self._enabled:
            super().configure(bg=ACCENT_PRESSED)

    def _on_release(self, event):
        if not self._enabled:
            return
        super().configure(bg=ACCENT_HOVER)
        # Клик засчитываем, только если отпустили над кнопкой — как у обычной.
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        if inside and self._command is not None:
            self._command()


def accent_button(parent, **kwargs) -> AccentButton:
    return AccentButton(parent, **kwargs)


def is_dark_theme(widget) -> bool:
    """Тёмная ли системная тема.

    На macOS ttk (тема aqua) следует системной автоматически, и в тёмной теме текст
    в таблицах становится белым. Светлая заливка строки, заданная без явного
    foreground, делает её нечитаемой — белое по светло-зелёному.
    На Windows системного цвета нет, TclError → считаем тему светлой.
    """
    try:
        r, g, b = widget.winfo_rgb("systemTextBackgroundColor")
    except tk.TclError:
        return False
    return (0.299 * r + 0.587 * g + 0.114 * b) / 65535 < 0.5


def row_colors(widget) -> dict[str, dict[str, str]]:
    """Цвета подсветки строк под текущую тему. Светлая — как было на Windows."""
    if is_dark_theme(widget):
        return {
            "warn":     {"background": "#4a3020", "foreground": "#ffdcc4"},
            "change":   {"background": "#1e3a24", "foreground": "#d7f0d7"},
            "nochange": {"foreground": "#9a9a9a"},
            "error":    {"background": "#5c2020", "foreground": "#ffd9d9"},
            "accent":   {"foreground": "#6fa8ff"},
        }
    return {
        "warn":     {"background": "#ffd9d9"},
        "change":   {"background": "#dff5df"},
        "nochange": {"foreground": "#888888"},
        "error":    {"background": "#ffbcbc"},
        "accent":   {"foreground": "#0a58ca"},
    }
