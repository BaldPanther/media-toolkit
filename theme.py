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

# Размеры сняты пипеткой с системной кнопки «Choose» в диалоге выбора файла
# macOS: 78×24 точки, скругление 6, белый текст, неактивная заливка #2f2f2f.
ACCENT_TEXT = "#ffffff"
ACCENT_FALLBACK = "#3478f6"      # для систем без системного акцента
ACCENT_OFF_DARK = "#2f2f2f"
ACCENT_OFF_LIGHT = "#e4e4e4"
ACCENT_OFF_TEXT_DARK = "#7a7a7a"
ACCENT_OFF_TEXT_LIGHT = "#9b9b9b"


def accent_rgb(widget) -> tuple[int, int, int]:
    """Акцентный цвет системы как (r, g, b) 0..255.

    Берём именно системный `systemControlAccentColor`, а не свой хардкод, по
    двум причинам. Во-первых, он подстраивается под цвет, выбранный в
    системных настройках. Во-вторых — и это важнее — macOS прогоняет цвета Tk
    через профиль дисплея: заданный вручную `#3478f6` рисуется как `#4676ee` и
    рядом с системной кнопкой заметно отличается. Значение системного цвета
    проходит ровно то же преобразование, что и у native-кнопки, и совпадает с
    ней пиксель в пиксель. Проверено скриншотами.
    """
    try:
        r, g, b = widget.winfo_rgb("systemControlAccentColor")
        return r // 256, g // 256, b // 256
    except tk.TclError:
        return tuple(int(ACCENT_FALLBACK[i:i + 2], 16) for i in (1, 3, 5))


def _darken(rgb, factor: float = 0.82) -> tuple[int, int, int]:
    return tuple(max(0, int(c * factor)) for c in rgb)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def _rgb_to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb)


MUTED_TEXT = "#8a8a8a"
HAND = "pointinghand" if sys.platform == "darwin" else "hand2"
_HAND = HAND
_RADIUS = 6          # как у системной кнопки
_BTN_H = 24          # её высота в точках
_PAD_X = 14          # поля по бокам: «Choose» выходит ровно 78 точек шириной


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
    Поэтому берём Label и добавляем ему клик и состояния руками.
    """

    def __init__(self, parent, text: str = "", command=None,
                 state: str = "normal", **kwargs):
        back = widget_bg(parent)
        dark = is_dark_theme(parent)
        accent = accent_rgb(parent)
        self._fills = {
            "normal": accent,
            "pressed": _darken(accent),
            "off": _hex_to_rgb(ACCENT_OFF_DARK if dark else ACCENT_OFF_LIGHT),
        }
        self._off_text = ACCENT_OFF_TEXT_DARK if dark else ACCENT_OFF_TEXT_LIGHT
        super().__init__(parent, text=text, bg=back, fg=ACCENT_TEXT,
                         cursor=_HAND, borderwidth=0, highlightthickness=0,
                         **kwargs)
        self._command = command
        self._enabled = True
        self._skins = self._make_skins(text)
        if self._skins:
            self.configure(compound="center", image=self._skins["normal"])
        else:                            # без Pillow — заливка без скругления
            self.configure(padx=_PAD_X, pady=4, bg=_rgb_to_hex(accent))
        # Наведение не подсвечиваем: системные кнопки macOS так себя не ведут,
        # у них меняется только нажатое состояние.
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        if state != "normal":
            self._set_enabled(False)

    def _make_skins(self, text: str) -> dict:
        """По картинке-подложке на каждое состояние: скругление рисуем сами."""
        if ImageTk is None:
            return {}
        metrics = tkfont.Font(font=self.cget("font"))
        width = metrics.measure(text) + 2 * _PAD_X
        height = max(_BTN_H, metrics.metrics("linespace") + 6)
        skins = {}
        for name, fill in self._fills.items():
            image = _rounded(width, height, fill)
            if image is None:
                return {}
            skins[name] = ImageTk.PhotoImage(image)
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

    def _paint(self, name: str) -> None:
        if self._skins:
            super().configure(image=self._skins[name])
        else:
            super().configure(bg=_rgb_to_hex(self._fills[name]))

    def _set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._paint("normal" if enabled else "off")
        super().configure(fg=ACCENT_TEXT if enabled else self._off_text,
                          cursor=_HAND if enabled else "")

    def _on_press(self, _event):
        if self._enabled:
            self._paint("pressed")

    def _on_release(self, event):
        if not self._enabled:
            return
        self._paint("normal")
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
