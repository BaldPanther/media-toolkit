"""Цвета подсветки строк под системную тему.

Общие для всех вкладок: таблицы дорожек, EDL и медиатеки красят строки одними
и теми же тегами. Вынесено из `app.py` отдельным модулем, чтобы `metaui.py` мог
их взять, не импортируя главное окно (иначе получилось бы кольцо импортов).
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

try:                                     # скруглённый фон рисуется Pillow
    from PIL import Image, ImageDraw, ImageTk
except ImportError:                      # noqa: BLE001 — без него кнопка просто прямоугольная
    Image = ImageDraw = ImageTk = None

# Цвет «основного действия» — им подсвечиваются кнопки, которые логично нажать
# следующими. Светлый, чтобы поверх читался тёмный текст в обеих системных темах.
ACCENT = "#6fa8f0"
ACCENT_HOVER = "#8bbcf5"
ACCENT_PRESSED = "#5b96e0"
ACCENT_TEXT = "#10243a"
ACCENT_OFF = "#4a4a4a"
ACCENT_OFF_TEXT = "#8a8a8a"

MUTED_TEXT = "#8a8a8a"
HAND = "pointinghand" if sys.platform == "darwin" else "hand2"
_HAND = HAND
_RADIUS = 7          # примерно как у системных кнопок macOS
_PAD_X, _PAD_Y = 14, 6


def widget_bg(widget) -> str:
    """Фон, на котором лежит виджет. Для ttk-контейнеров — из темы.

    Нужен, чтобы скруглённые углы и пустые ячейки сливались с подложкой:
    у ttk-фреймов обычного `cget("bg")` нет.
    """
    try:
        return widget.cget("background")
    except tk.TclError:
        pass
    style = ttk.Style(widget)
    for name in (widget.winfo_class(), "TFrame"):
        color = style.lookup(name, "background")
        if color:
            return color
    return widget.winfo_toplevel().cget("background")


def _rounded(width: int, height: int, color: str, radius: int = _RADIUS):
    """Прямоугольник со скруглёнными углами и прозрачным фоном."""
    if Image is None:
        return None
    scale = 4            # рисуем крупнее и уменьшаем — так края не рваные
    img = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=radius * scale, fill=color)
    return img.resize((width, height), Image.LANCZOS)


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
        back = widget_bg(parent)
        super().__init__(parent, text=text, bg=back, fg=ACCENT_TEXT,
                         cursor=_HAND, borderwidth=0, highlightthickness=0,
                         **kwargs)
        self._command = command
        self._enabled = True
        self._skins = self._make_skins(text, back)
        if self._skins:
            self.configure(compound="center", image=self._skins[ACCENT])
        else:                            # без Pillow — просто заливка без скругления
            self.configure(padx=_PAD_X, pady=_PAD_Y, bg=ACCENT)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        if state != "normal":
            self._set_enabled(False)

    def _make_skins(self, text: str, back: str) -> dict:
        """По картинке-подложке на каждое состояние: скругление рисуем сами."""
        if ImageTk is None:
            return {}
        metrics = tkfont.Font(font=self.cget("font"))
        width = metrics.measure(text) + 2 * _PAD_X
        height = metrics.metrics("linespace") + 2 * _PAD_Y
        skins = {}
        for color in (ACCENT, ACCENT_HOVER, ACCENT_PRESSED, ACCENT_OFF):
            image = _rounded(width, height, color)
            if image is None:
                return {}
            skins[color] = ImageTk.PhotoImage(image)
        del back                         # фон уже учтён прозрачностью углов
        return skins

    # tkinter зовёт configure(state=…) из общего кода (например, set_busy),
    # поэтому состояние перехватываем здесь, а не отдельным методом.
    def configure(self, cnf=None, **kwargs):
        if "state" in kwargs:
            self._set_enabled(str(kwargs.pop("state")) == "normal")
        if cnf or kwargs:
            return super().configure(cnf, **kwargs)
        return super().configure()

    config = configure

    def _paint(self, color: str) -> None:
        if self._skins:
            super().configure(image=self._skins[color])
        else:
            super().configure(bg=color)

    def _set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._paint(ACCENT if enabled else ACCENT_OFF)
        super().configure(fg=ACCENT_TEXT if enabled else ACCENT_OFF_TEXT,
                          cursor=_HAND if enabled else "")

    def _on_enter(self, _event):
        if self._enabled:
            self._paint(ACCENT_HOVER)

    def _on_leave(self, _event):
        if self._enabled:
            self._paint(ACCENT)

    def _on_press(self, _event):
        if self._enabled:
            self._paint(ACCENT_PRESSED)

    def _on_release(self, event):
        if not self._enabled:
            return
        self._paint(ACCENT_HOVER)
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
