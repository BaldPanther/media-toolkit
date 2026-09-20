"""Цвета подсветки строк под системную тему.

Общие для всех вкладок: таблицы дорожек, EDL и медиатеки красят строки одними
и теми же тегами. Вынесено из `app.py` отдельным модулем, чтобы `metaui.py` мог
их взять, не импортируя главное окно (иначе получилось бы кольцо импортов).
"""
from __future__ import annotations

import tkinter as tk


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
