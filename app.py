"""GUI для пакетной смены дорожек по умолчанию (аудио/субтитры) в MKV-файлах.

Запуск:
    python app.py [папка]

Выбор дорожки — по названию и языку, поэтому одна и та же озвучка ставится по
умолчанию во всех сериях, даже если её номер в файлах различается; при этом
одноимённые дорожки разных языков (напр. «Surround» rus и eng) различаются.
Изменения вносит mkvpropedit (из MKVToolNix) прямо в заголовок — мгновенно,
без перезаписи файла.
"""
from __future__ import annotations

import base64
import json
import math
import queue
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import chapters
import core
import edl
import metaui
import paths
import theme
import trim
from theme import is_dark_theme, row_colors  # noqa: F401 — is_dark_theme держим в API модуля

NOTOUCH = "— не трогать —"
SUBOFF = "— выключить субтитры —"
EDL_TRACK_AUTO = "Оригинал (авто)"
# Как называть kind из edl.detect_season в строке прогресса.
EDL_KIND_RU = {"intro": "интро", "outro": "титры"}

# Локальные настройки приложения (последний путь и т.п.) — в пользовательском
# каталоге настроек; см. paths.py.
_SETTINGS_NAME = "settings.json"


def load_app_settings() -> dict:
    try:
        return json.loads(paths.settings_path(_SETTINGS_NAME).read_text("utf-8"))
    except Exception:  # noqa: BLE001 — нет файла/битый JSON: просто пустые настройки
        return {}


def save_app_settings(data: dict) -> None:
    try:
        path = paths.settings_path(_SETTINGS_NAME)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except Exception:  # noqa: BLE001 — не критично, просто не сохранили
        pass


class App:
    def __init__(self, root: tk.Tk, start_folder: str = ""):
        self.root = root
        self.files: list[core.MkvFile] = []
        self.audio_options: list[core.Option] = []
        self.sub_options: list[core.Option] = []
        self.total_files = 0
        self.busy = False
        self.cancel_event = threading.Event()
        self.edl_eps: list[edl.EpisodeEdl] = []
        self.edl_row_ep: dict[str, edl.EpisodeEdl] = {}
        self.edl_sort: tuple[str | None, bool] = (None, False)  # колонка, обратный ли порядок
        # Что сейчас есть у серий — этим гасятся кнопки вкладки; считается при
        # перерисовке таблицы, чтобы не ходить на диск на каждый клик.
        self._edl_tools_ok = True
        self._edl_have_files = 0      # серий, рядом с которыми уже лежит .edl
        self._edl_have_online = 0     # серий с загруженными онлайн-таймингами
        self._edl_have_chapters = 0   # серий, в которых уже есть главы
        # Кнопки вкладок, которые тоже надо гасить на время длинных операций.
        # Вкладка «Медиатека» дописывает сюда свои при построении.
        self.extra_busy_buttons: list = []

        root.title("Медиатека Kodi — метаданные, дорожки, пропуск заставок")
        self._set_window_icon()
        root.minsize(900, 700)

        self._build_top(start_folder)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        # Порядок вкладок = порядок работы: сначала раскладка и метаданные
        # (она переименовывает файлы), потом дорожки и заставки.
        self.tab_meta = ttk.Frame(self.nb)
        self.tab_tracks = ttk.Frame(self.nb)
        self.tab_edl = ttk.Frame(self.nb)
        self.nb.add(self.tab_meta, text="Медиатека")
        self.nb.add(self.tab_tracks, text="Дорожки и субтитры")
        self.nb.add(self.tab_edl, text="Пропуск заставок (EDL)")

        self._build_selection(self.tab_tracks)
        self._build_subs(self.tab_tracks)
        self._build_table(self.tab_tracks)
        self._build_tracks_apply(self.tab_tracks)
        self._build_edl(self.tab_edl)
        self._build_common_bottom()
        self.meta = metaui.MetaTab(self.tab_meta, self)
        self._load_edl_settings()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._place_window()
        self._enable_entry_clipboard()
        self._check_tools()
        # Путь (последний/аргумент) только подставляется в поле — сканирование вручную
        # кнопкой «Сканировать»: если папка уже обрабатывалась, повторный скан не нужен.

    def _place_window(self):
        """Размер по содержимому, а не зашитый.

        Зашитые 1080×760 были меньше, чем окну нужно: таблица предпросмотра
        сжималась до трёх строк, а лог выдавливался за нижний край совсем.
        Берём то, что виджеты запросили, и подрезаем по экрану.

        Позицию задаём явно, не только размер: при запуске из Dock на macOS окно
        без координат уезжает в левый нижний угол. По вертикали ставим чуть выше
        центра — иначе окно выглядит утопленным под строкой меню.
        """
        root = self.root
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = min(max(root.winfo_reqwidth(), 1080), sw - 80)
        h = min(max(root.winfo_reqheight(), 760), sh - 120)
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(40, (sh - h) // 3)}")

    def _on_close(self):
        self._save_edl_settings()
        self.root.destroy()

    def _set_window_icon(self):
        assets = Path(__file__).resolve().parent / "assets"
        self.icon_image = None
        try:
            self.icon_image = tk.PhotoImage(file=str(assets / "app-icon.png"))
            self.root.iconphoto(True, self.icon_image)
        except tk.TclError:
            pass

        if sys.platform == "win32":
            try:
                self.root.iconbitmap(str(assets / "app-icon.ico"))
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ UI --
    def _build_top(self, start_folder):
        f = ttk.Frame(self.root, padding=(10, 10, 10, 4))
        f.pack(fill="x")

        ttk.Label(f, text="Папка:").pack(side="left")
        self.path_var = tk.StringVar(value=start_folder)
        ttk.Entry(f, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(f, text="Обзор…", command=self.browse).pack(side="left")
        # Фильм может лежать одним файлом прямо в корне библиотеки — папку
        # вокруг него создаёт вкладка «Медиатека», но указать его надо явно.
        ttk.Button(f, text="Файл…", command=self.browse_file).pack(side="left", padx=(4, 0))

        self.recursive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Включая вложенные папки", variable=self.recursive_var).pack(side="left", padx=10)
        self.scan_btn = theme.accent_button(f, text="Сканировать", command=self.scan)
        self.scan_btn.pack(side="left")

        self.summary_var = tk.StringVar(value="Папка не выбрана.")
        ttk.Label(self.root, textvariable=self.summary_var, padding=(10, 0)).pack(fill="x")

    def _build_selection(self, parent):
        f = ttk.LabelFrame(parent, text="Что поставить по умолчанию", padding=10)
        f.pack(fill="x", padx=10, pady=6)
        f.columnconfigure(1, weight=1)
        f.columnconfigure(3, weight=1)

        ttk.Label(f, text="Аудио:").grid(row=0, column=0, sticky="w")
        self.audio_combo = ttk.Combobox(f, state="readonly", values=[NOTOUCH])
        self.audio_combo.current(0)
        self.audio_combo.grid(row=0, column=1, sticky="ew", padx=(6, 16))
        self.audio_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_preview())

        ttk.Label(f, text="Субтитры:").grid(row=0, column=2, sticky="w")
        self.sub_combo = ttk.Combobox(f, state="readonly", values=[NOTOUCH, SUBOFF])
        self.sub_combo.current(0)
        self.sub_combo.grid(row=0, column=3, sticky="ew", padx=6)
        self.sub_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_preview())

    def _build_subs(self, parent):
        f = ttk.LabelFrame(parent, text="Русские субтитры (качаются файлом .ru.srt рядом с серией)", padding=10)
        f.pack(fill="x", padx=10, pady=(0, 6))

        self.subs_only_missing = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="только где русских ещё нет", variable=self.subs_only_missing).pack(side="left")
        self.subs_btn = ttk.Button(f, text="Скачать русские субтитры", command=self.download_subs)
        self.subs_btn.pack(side="left", padx=8)
        ttk.Button(f, text="OpenSubtitles…", command=self.subs_settings_dialog).pack(side="left")
        self.subs_status = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.subs_status).pack(side="left", padx=10)

    def _build_table(self, parent):
        f = ttk.Frame(parent, padding=(10, 0))
        f.pack(fill="both", expand=True)

        cols = ("file", "acur", "anew", "scur", "snew", "status")
        heads = {
            "file": "Файл", "acur": "Аудио сейчас", "anew": "→ Новая аудио",
            "scur": "Субтитры сейчас", "snew": "→ Новые субтитры", "status": "Статус",
        }
        widths = {"file": 280, "acur": 150, "anew": 150, "scur": 130, "snew": 150, "status": 130}
        self.tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="none")
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor="w")
        vsb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        c = row_colors(self.tree)
        self.tree.tag_configure("warn", **c["warn"])
        self.tree.tag_configure("change", **c["change"])
        self.tree.tag_configure("nochange", **c["nochange"])
        self.tree.tag_configure("error", **c["error"])

    def _build_tracks_apply(self, parent):
        f = ttk.Frame(parent, padding=(10, 6))
        f.pack(fill="x")
        self.apply_btn = ttk.Button(f, text="Применить (дорожки/субтитры)", command=self.apply)
        self.apply_btn.pack(side="left")
        self.preview_status = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.preview_status).pack(side="left", padx=10)

    def _build_common_bottom(self):
        f = ttk.Frame(self.root, padding=(10, 6))
        ttk.Label(f, text="Прогресс:").pack(side="left")
        self.progress = ttk.Progressbar(f, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        # Что именно сейчас идёт. Ширина постоянная, иначе полоса прыгала бы
        # на каждом обновлении текста.
        self.progress_text = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.progress_text, width=58).pack(side="left", padx=(0, 10))
        # Рабочие потоки проверяют cancel_event между файлами и мягко прерываются.
        self.cancel_btn = ttk.Button(f, text="Отмена", state="disabled",
                                     command=self.cancel_event.set)
        self.cancel_btn.pack(side="left")

        # Полоса прокрутки — ttk, как у таблиц. ScrolledText ставит классическую
        # tk.Scrollbar, а та в тёмной теме macOS рисуется белым столбом.
        log_f = ttk.Frame(self.root)
        self.log = tk.Text(log_f, height=7, state="disabled", wrap="word")
        log_sb = ttk.Scrollbar(log_f, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="left", fill="y")
        # Прижаты к низу и упакованы раньше вкладок. Когда окну не хватает
        # высоты, pack урезает упакованное последним — раньше это были прогресс,
        # «Отмена» и лог, и на экране ноутбука они не показывались вовсе.
        log_f.pack(side="bottom", fill="x", padx=10, pady=(0, 10), before=self.nb)
        f.pack(side="bottom", fill="x", before=self.nb)

    # -------------------------------------------------------- вкладка EDL --
    def _build_edl(self, parent):
        # Настройки, ручной ввод, главы с онлайном — под-вкладками. Стопкой они
        # были выше экрана ноутбука, и таблица с прогрессом и логом уезжали за
        # нижний край. Таблица и кнопки остаются видны при любой из них.
        self.edl_sub = ttk.Notebook(parent)
        self.edl_sub.pack(fill="x", padx=10, pady=(8, 4))
        opt = ttk.Frame(self.edl_sub, padding=10)
        manual = ttk.Frame(self.edl_sub, padding=10)
        more = ttk.Frame(self.edl_sub, padding=(10, 6))
        self.edl_sub.add(opt, text="Настройки пропуска")
        self.edl_sub.add(manual, text="Задать вручную")
        self.edl_sub.add(more, text="Главы и онлайн")

        self.edl_keep_first = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt, text="Показывать заставку в первой серии сезона (E01) — пропускать только титры",
            variable=self.edl_keep_first, command=self.refresh_edl_preview,
        ).grid(row=0, column=0, columnspan=6, sticky="w")

        # Дорожка для детекта: по умолчанию оригинал (не дубляж) — чистая музыка без
        # закадрового названия серии, которое сбивает определение границ.
        ttk.Label(opt, text="Дорожка для детекта:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.edl_track_var = tk.StringVar(value=EDL_TRACK_AUTO)
        self.edl_track_combo = ttk.Combobox(opt, state="readonly", width=16,
                                             textvariable=self.edl_track_var, values=[EDL_TRACK_AUTO])
        self.edl_track_combo.grid(row=1, column=1, columnspan=2, sticky="w", padx=(6, 0), pady=(6, 0))
        self.edl_track_combo.bind("<<ComboboxSelected>>", lambda e: self._update_track_info())
        self.edl_track_info = tk.StringVar(value="")
        ttk.Label(opt, textvariable=self.edl_track_info,
                  **row_colors(opt)["accent"]).grid(
            row=1, column=3, columnspan=3, sticky="w", padx=(8, 0), pady=(6, 0))

        # Окно поиска — сколько звука слушать с каждого края серии. Время детекта
        # ему прямо пропорционально: чтобы добраться до звука в MKV, ffmpeg читает
        # весь этот кусок вместе с видео. Но заставка должна уложиться в окно
        # ЦЕЛИКОМ: то, что за край не влезло, обрезается по краю окна.
        self.edl_window = {"intro": tk.StringVar(value=f"{edl.INTRO_WINDOW:.0f}"),
                           "outro": tk.StringVar(value=f"{edl.OUTRO_WINDOW:.0f}")}

        def window_spin(col, label, key):
            ttk.Label(opt, text=label).grid(row=2, column=col, sticky="e", padx=(12, 2), pady=(6, 0))
            ttk.Spinbox(opt, from_=30, to=900, increment=30, width=5,
                        textvariable=self.edl_window[key]).grid(
                row=2, column=col + 1, sticky="w", pady=(6, 0))

        ttk.Label(opt, text="Окно поиска (сек):").grid(row=2, column=0, sticky="w", pady=(6, 0))
        window_spin(1, "с начала:", "intro")
        window_spin(3, "с конца:", "outro")
        ttk.Label(opt, text=f"по умолчанию {edl.INTRO_WINDOW:.0f}; заставка должна "
                            "уложиться в окно целиком",
                  **row_colors(opt)["nochange"]).grid(
            row=2, column=5, sticky="w", padx=(10, 0), pady=(6, 0))

        # Отступы (padding) поверх автодетекта, на весь сезон. + позже / − раньше.
        self.edl_pad = {k: tk.StringVar(value="0") for k in
                        ("intro_start", "intro_end", "outro_start", "outro_end")}

        def spin(row, col, label, key):
            ttk.Label(opt, text=label).grid(row=row, column=col, sticky="e", padx=(12, 2), pady=(6, 0))
            sb = ttk.Spinbox(opt, from_=-120, to=120, increment=1, width=5,
                             textvariable=self.edl_pad[key], command=self.refresh_edl_preview)
            sb.grid(row=row, column=col + 1, sticky="w", pady=(6, 0))
            sb.bind("<KeyRelease>", lambda e: self.refresh_edl_preview())
            return sb

        ttk.Label(opt, text="Отступы (сек): + сдвинуть позже, − раньше").grid(
            row=3, column=0, sticky="w", pady=(6, 0))
        spin(3, 1, "начало интро:", "intro_start")
        spin(3, 3, "конец интро:", "intro_end")
        spin(4, 1, "начало титров:", "outro_start")
        self.edl_outro_end_spin = spin(4, 3, "конец титров:", "outro_end")

        # Обычно после титров ничего нет и пропуск честнее вести до самого конца.
        # Но если там сцена после титров, её бы тоже проглотило — тогда галку
        # снимают, и конец берётся по найденной границе плюс отступ «конец титров».
        # Это политика на случай «конец неизвестен»: заданный вручную конец титров
        # галка не трогает, иначе вписанное время молча отменялось бы.
        self.edl_outro_to_end = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt, text="Титры — до конца файла, когда конец не задан "
                      "(снимите, если после титров есть сцена)",
            variable=self.edl_outro_to_end, command=self._on_outro_to_end,
        ).grid(row=5, column=0, columnspan=6, sticky="w", pady=(6, 0))

        # Звук длиннее картинки — Kodi на пропуске титров встаёт на полпути и
        # показывает чёрный экран, пока звук не кончится (почему — в trim.py).
        # Чинится только файл, поэтому по умолчанию хвост режется перед записью.
        self.edl_trim_tail = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt, text=f"Обрезать хвост после конца видео (звук дольше картинки больше "
                      f"{trim.TAIL_MIN_S:.0f} с) — без перекодирования, при записи .edl",
            variable=self.edl_trim_tail,
        ).grid(row=6, column=0, columnspan=6, sticky="w", pady=(6, 0))

        # --- Задать вручную (когда детект промахнулся или его нет) ---
        # В некоторых сериалах интро/титры одинаковы по времени во всех сериях, но
        # детект их не берёт (нет чёткой музыкальной темы) — задаём одним значением.
        # Переключатель scope позволяет применить не ко всем, а к выделенным строкам
        # (Shift/Ctrl в таблице) — напр. когда один сезон-папка склеен из двух cours
        # с разными таймингами.
        self.edl_manual = {k: tk.StringVar(value="")
                           for k in ("intro_start", "intro_end", "intro_dur",
                                     "outro_start", "outro_end", "outro_last",
                                     "outro_from_start", "recap_end")}
        self.edl_scope = tk.StringVar(value="all")
        ttk.Label(manual, text="Время: MM:SS, H:MM:SS или секунды. Применяется по переключателю "
                               "«Применять к» у кнопок ниже.",
                  **row_colors(manual)["nochange"]).grid(
            row=0, column=0, columnspan=7, sticky="w", pady=(0, 4))

        ttk.Label(manual, text="Интро:").grid(row=1, column=0, sticky="e")
        ttk.Label(manual, text="начало").grid(row=1, column=1, sticky="e", padx=(8, 2))
        ttk.Entry(manual, textvariable=self.edl_manual["intro_start"], width=8).grid(row=1, column=2)
        ttk.Label(manual, text="конец").grid(row=1, column=3, sticky="e", padx=(8, 2))
        ttk.Entry(manual, textvariable=self.edl_manual["intro_end"], width=8).grid(row=1, column=4)
        ttk.Button(manual, text="Задать", command=self.apply_intro_all).grid(row=1, column=5, padx=(10, 0))

        # Интро «длительность N сек»: начало у каждой серии своё (из автодетекта или
        # ручной правки), конец пересчитывается как начало + N — лечит случай, когда
        # детект верно нашёл начало, но криво определил конец.
        ttk.Label(manual, text="…или длительность").grid(row=2, column=1, sticky="e", padx=(8, 2), pady=(6, 0))
        ttk.Entry(manual, textvariable=self.edl_manual["intro_dur"], width=8).grid(row=2, column=2, pady=(6, 0))
        ttk.Label(manual, text="сек — продолжительность интро (конец = начало серии + N)").grid(
            row=2, column=3, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(manual, text="Задать", command=self.apply_intro_dur_all).grid(row=2, column=5, padx=(10, 0), pady=(6, 0))

        # Конец титров необязателен: пусто — до конца файла, как было всегда. Но если
        # после титров идёт сцена (Marvel и прочие), её задают явно, и тогда указанное
        # время главнее галки «до конца файла» — см. edl.final_outro.
        ttk.Label(manual, text="Титры:").grid(row=3, column=0, sticky="e", pady=(6, 0))
        ttk.Label(manual, text="начало").grid(row=3, column=1, sticky="e", padx=(8, 2), pady=(6, 0))
        ttk.Entry(manual, textvariable=self.edl_manual["outro_start"], width=8).grid(row=3, column=2, pady=(6, 0))
        ttk.Label(manual, text="конец").grid(row=3, column=3, sticky="e", padx=(8, 2), pady=(6, 0))
        ttk.Entry(manual, textvariable=self.edl_manual["outro_end"], width=8).grid(row=3, column=4, pady=(6, 0))
        ttk.Button(manual, text="Задать", command=self.apply_outro_all).grid(row=3, column=5, padx=(10, 0), pady=(6, 0))
        ttk.Label(manual, text="пусто — до конца файла; укажите, если после титров есть сцена",
                  **row_colors(manual)["nochange"]).grid(
            row=3, column=6, sticky="w", padx=(10, 0), pady=(6, 0))

        # Титры «последние N сек от конца» — устойчиво к разной длине серий (титры обычно
        # фиксированной длительности), для каждой серии начало = длительность − N.
        ttk.Label(manual, text="…или последние").grid(row=4, column=1, sticky="e", padx=(8, 2), pady=(6, 0))
        ttk.Entry(manual, textvariable=self.edl_manual["outro_last"], width=8).grid(row=4, column=2, pady=(6, 0))
        ttk.Label(manual, text="сек — продолжительность титров (от конца файла)").grid(
            row=4, column=3, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(manual, text="Задать", command=self.apply_outro_last_all).grid(row=4, column=5, padx=(10, 0), pady=(6, 0))

        # В плеере видно, КОГДА титры начались, а поле выше просит их ПРОДОЛЖИТЕЛЬНОСТЬ.
        # Вычитать одно время из другого в уме — лишний повод ошибиться, считаем сами.
        ttk.Label(manual, text="…знаете только начало —").grid(row=5, column=1, sticky="e", padx=(8, 2), pady=(6, 0))
        ttk.Entry(manual, textvariable=self.edl_manual["outro_from_start"], width=8).grid(row=5, column=2, pady=(6, 0))
        self.edl_outro_calc = tk.StringVar(
            value="время начала титров (MM:SS) — посчитаем продолжительность по выделенной серии")
        ttk.Label(manual, textvariable=self.edl_outro_calc,
                  **row_colors(manual)["nochange"]).grid(
            row=5, column=3, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(manual, text="Посчитать", command=self.calc_outro_last).grid(
            row=5, column=5, padx=(10, 0), pady=(6, 0))

        ttk.Label(manual, text="Recap:").grid(row=6, column=0, sticky="e", pady=(6, 0))
        ttk.Label(manual, text="до").grid(row=6, column=1, sticky="e", padx=(8, 2), pady=(6, 0))
        ttk.Entry(manual, textvariable=self.edl_manual["recap_end"], width=8).grid(row=6, column=2, pady=(6, 0))
        ttk.Label(manual, text="(с начала файла)").grid(row=6, column=3, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(manual, text="Задать", command=self.apply_recap_all).grid(row=6, column=5, padx=(10, 0), pady=(6, 0))

        # Прицельное удаление по сегменту (уважает scope): напр. снять только титры
        # у выделенного последнего сезона, где их нет, сохранив интро.
        ttk.Label(manual, text="Убрать:").grid(row=7, column=0, sticky="e", pady=(8, 0))
        clr = ttk.Frame(manual)
        clr.grid(row=7, column=1, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Button(clr, text="интро", command=lambda: self.clear_segment_scope("intro")).pack(side="left", padx=(0, 4))
        ttk.Button(clr, text="титры", command=lambda: self.clear_segment_scope("outro")).pack(side="left", padx=4)
        ttk.Button(clr, text="recap", command=lambda: self.clear_segment_scope("recap")).pack(side="left", padx=4)
        ttk.Button(clr, text="всё", command=self.clear_all_segments).pack(side="left", padx=4)

        btns = ttk.Frame(parent, padding=(10, 4))
        btns.pack(fill="x")
        # Главное действие вкладки — акцентная, как «Сканировать» и «Применить».
        self.edl_detect_btn = theme.accent_button(btns, text="Определить автоматически",
                                                  command=self.detect_edl)
        self.edl_detect_btn.pack(side="left")
        self.edl_write_btn = ttk.Button(btns, text="Записать .edl", command=self.write_edl_files)
        self.edl_write_btn.pack(side="left", padx=8)
        self.edl_delete_btn = ttk.Button(btns, text="Удалить .edl", command=self.delete_edl_files)
        self.edl_delete_btn.pack(side="left")
        # Переключатель правит всю вкладку — детект, запись, ручной ввод, — поэтому
        # стоит у главных кнопок, а не внутри одной из под-вкладок.
        ttk.Label(btns, text="Применять к:").pack(side="left", padx=(20, 0))
        ttk.Radiobutton(btns, text="ко всем сериям", value="all",
                        variable=self.edl_scope).pack(side="left", padx=(6, 0))
        self.edl_scope_sel_rb = ttk.Radiobutton(btns, text="к выделенным (0)", value="sel",
                                                 variable=self.edl_scope)
        self.edl_scope_sel_rb.pack(side="left", padx=(6, 0))
        ttk.Label(btns, text="  (двойной клик по строке — правка вручную)").pack(side="left", padx=10)

        # Автодетект держится на внешних ffmpeg и fpcalc. Чего-то нет — говорим
        # об этом сразу, а не ошибкой после нажатия кнопки и долгого скана.
        missing = edl.missing_tools()
        self._edl_tools_ok = not missing
        if missing:
            ttk.Label(parent, text="⚠ " + edl.install_hint(missing),
                      padding=(10, 0)).pack(fill="x")

        # Главы Matroska из той же разметки: `.edl` понимает только Kodi, а главы —
        # любой плеер. Пишутся в заголовок mkvpropedit-ом, без перекодирования.
        ch_f = ttk.LabelFrame(more, text="Главы Matroska (чтобы разметка работала и в других плеерах)",
                              padding=8)
        ch_f.pack(fill="x")
        self.chapters_with_edl = tk.BooleanVar(value=False)
        ttk.Checkbutton(ch_f, text="писать вместе с .edl",
                        variable=self.chapters_with_edl).pack(side="left")
        self.chapters_write_btn = ttk.Button(ch_f, text="Записать главы",
                                             command=self.write_chapters)
        self.chapters_write_btn.pack(side="left", padx=(12, 0))
        self.chapters_clear_btn = ttk.Button(ch_f, text="Убрать главы",
                                             command=self.clear_chapters)
        self.chapters_clear_btn.pack(side="left", padx=(8, 0))
        # mkvpropedit заменяет главы целиком, дописать свои к чужим нельзя. Поэтому
        # файлы с посторонней разметкой пропускаются, пока это не разрешено явно.
        self.chapters_force = tk.BooleanVar(value=False)
        ttk.Checkbutton(ch_f, text="перезаписывать чужие главы (их деление на сцены пропадёт)",
                        variable=self.chapters_force).pack(side="left", padx=(12, 0))

        # Онлайн-тайминги: подтягиваем готовые интро/титры из баз, показываем рядом с
        # локальными (отдельные колонки) и переносим в активные по кнопке — с учётом scope.
        online_f = ttk.LabelFrame(more, text="Онлайн-тайминги (AniSkip / TheIntroDB)", padding=8)
        online_f.pack(fill="x", pady=(8, 0))
        self.online_load_btn = ttk.Button(online_f, text="Загрузить онлайн…", command=self.online_dialog)
        self.online_load_btn.pack(side="left")
        self.online_take_on_btn = ttk.Button(online_f, text="Взять онлайн (по scope)", command=self.take_online)
        self.online_take_on_btn.pack(side="left", padx=(8, 0))
        self.online_take_loc_btn = ttk.Button(online_f, text="Вернуть локальные (по scope)", command=self.take_local)
        self.online_take_loc_btn.pack(side="left", padx=(8, 0))
        self.online_status = tk.StringVar(value="")
        ttk.Label(online_f, textvariable=self.online_status,
                  **row_colors(online_f)["accent"]).pack(side="left", padx=10)

        f = ttk.Frame(parent, padding=(10, 0))
        f.pack(fill="both", expand=True)
        cols = ("file", "se", "recap", "intro", "intro_on", "outro", "outro_on", "edl", "ch",
                "tail", "note")
        heads = {"file": "Файл", "se": "S/E", "recap": "Recap",
                 "intro": "Интро (актив.)", "intro_on": "Интро (онлайн)",
                 "outro": "Титры (актив.)", "outro_on": "Титры (онлайн)",
                 "edl": ".edl", "ch": "главы", "tail": "хвост", "note": "Заметка"}
        widths = {"file": 320, "se": 60, "recap": 74, "intro": 120, "intro_on": 120,
                  "outro": 120, "outro_on": 120, "edl": 44, "ch": 52, "tail": 56, "note": 130}
        # Свободную ширину отдаём имени файла и заметке: у остальных колонок
        # содержимое фиксированной длины и растягивать их незачем, а имя серии
        # без этого обрезалось на середине.
        stretchy = {"file", "note"}
        self._edl_heads = heads
        self.edl_tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="extended")
        for c in cols:
            self.edl_tree.heading(c, text=heads[c], command=lambda c=c: self._edl_sort_by(c))
            self.edl_tree.column(c, width=widths[c], anchor="w", stretch=c in stretchy)
        vsb = ttk.Scrollbar(f, orient="vertical", command=self.edl_tree.yview)
        self.edl_tree.configure(yscrollcommand=vsb.set)
        self.edl_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        c = row_colors(self.edl_tree)
        self.edl_tree.tag_configure("first", **c["accent"])
        self.edl_tree.tag_configure("has", **c["change"])
        self.edl_tree.bind("<Double-1>", self._edl_edit_row)
        self.edl_tree.bind("<<TreeviewSelect>>", self._update_scope_count)

        self.edl_status = tk.StringVar(value="Просканируйте папку, затем «Определить автоматически».")
        # Упакована раньше таблицы: при нехватке высоты ужимается таблица, а не статус.
        ttk.Label(parent, textvariable=self.edl_status, padding=(10, 4)).pack(
            side="bottom", fill="x", before=f)
        self._sync_edl_buttons()

    # Ключи сортировки таблицы EDL — по одному на колонку. Сортируем сам список
    # серий, а не строки дерева: порядок тогда переживает перерисовку таблицы,
    # которая идёт после каждой правки.
    _EDL_SORT_KEYS = {
        "file": lambda e: e.path.name.casefold(),
        "se": lambda e: (e.season is None, e.season or 0, e.episode or 0),
        "recap": lambda e: (e.recap is None, e.recap.end if e.recap else 0.0),
        "intro": lambda e: (e.intro is None, e.intro.start if e.intro else 0.0),
        "intro_on": lambda e: (e.online_intro is None,
                               e.online_intro.start if e.online_intro else 0.0),
        "outro": lambda e: (e.outro is None, e.outro.start if e.outro else 0.0),
        "outro_on": lambda e: (e.online_outro is None,
                               e.online_outro.start if e.online_outro else 0.0),
        "edl": lambda e: not edl.has_external_edl(e.path),
        "ch": lambda e: -e.chapters,
        "tail": lambda e: -(e.tail or 0.0) if trim.needs_trim(e.tail) else 0.0,
        "note": lambda e: "; ".join(x for x in (e.note, e.online_note) if x).casefold(),
    }

    def _edl_sort_by(self, col: str):
        """Клик по заголовку: сортировка по колонке, повторный клик — наоборот.

        Пустые значения при прямом порядке уходят вниз, при обратном поднимаются
        наверх — так серии без найденного интро собираются в кучу одним кликом.
        """
        key = self._EDL_SORT_KEYS.get(col)
        if key is None or not self.edl_eps:
            return
        prev_col, prev_rev = self.edl_sort
        self.edl_sort = (col, not prev_rev if col == prev_col else False)
        self.edl_eps.sort(key=key, reverse=self.edl_sort[1])
        self.refresh_edl_preview()

    def _mark_edl_sort(self):
        """Стрелка в заголовке: какая колонка сортирует и в какую сторону."""
        col, reverse = self.edl_sort
        for c, text in self._edl_heads.items():
            mark = (" ▼" if reverse else " ▲") if c == col else ""
            self.edl_tree.heading(c, text=text + mark)

    def _sync_edl_buttons(self):
        """Гасит кнопки вкладки EDL, когда работать нечем.

        Иначе они отвечают модалкой «Сначала просканируйте» — то же самое, но
        лишним кликом позже. Во время работы состоянием кнопок ведает set_busy.
        """
        if self.busy or not hasattr(self, "chapters_clear_btn"):
            return
        have = bool(self.edl_eps)
        for btn, ok in ((self.edl_detect_btn, have and self._edl_tools_ok),
                        (self.edl_write_btn, have),
                        (self.edl_delete_btn, self._edl_have_files > 0),
                        (self.online_load_btn, have),
                        (self.online_take_on_btn, self._edl_have_online > 0),
                        (self.online_take_loc_btn, have),
                        (self.chapters_write_btn, have),
                        (self.chapters_clear_btn, self._edl_have_chapters > 0)):
            btn.configure(state="normal" if ok else "disabled")

    def _enable_entry_clipboard(self):
        """Ctrl+C/V/X/A в полях ввода + меню по правой кнопке мыши.

        В tkinter на Windows при не-латинской раскладке стандартные сочетания не
        работают: приходит кириллический keysym, и привязки вроде <Control-v> молчат.
        Ловим по keycode (он от раскладки не зависит) и генерируем виртуальные
        события правки; плюс контекстное меню «Вставить/Копировать/…» на ПКМ.
        """
        def on_ctrl(event):
            kc = event.keycode
            if kc == 86:        # V
                event.widget.event_generate("<<Paste>>"); return "break"
            if kc == 67:        # C
                event.widget.event_generate("<<Copy>>"); return "break"
            if kc == 88:        # X
                event.widget.event_generate("<<Cut>>"); return "break"
            if kc == 65:        # A — выделить всё
                try:
                    event.widget.select_range(0, "end"); event.widget.icursor("end")
                except tk.TclError:
                    pass
                return "break"
            return None

        menu = tk.Menu(self.root, tearoff=0)
        target = {"w": None}

        def gen(action):
            if target["w"] is not None:
                target["w"].event_generate(action)

        def select_all():
            w = target["w"]
            if w is not None:
                try:
                    w.select_range(0, "end"); w.icursor("end")
                except tk.TclError:
                    pass

        menu.add_command(label="Вырезать", command=lambda: gen("<<Cut>>"))
        menu.add_command(label="Копировать", command=lambda: gen("<<Copy>>"))
        menu.add_command(label="Вставить", command=lambda: gen("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=select_all)

        def popup(event):
            target["w"] = event.widget
            menu.tk_popup(event.x_root, event.y_root)

        # Привязка по классу действует и на поля, созданные позже (диалог OpenSubtitles).
        for cls in ("TEntry", "Entry"):
            self.root.bind_class(cls, "<Control-KeyPress>", on_ctrl)
            self.root.bind_class(cls, "<Button-3>", popup)

    # -------------------------------------------------------------- helpers --
    def _check_tools(self):
        try:
            core.find_tools()
        except FileNotFoundError as e:
            messagebox.showwarning("MKVToolNix не найден", str(e))

    def log_line(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_busy(self, busy: bool, what: str = ""):
        """Работа пошла или кончилась. what — что делается, для подписи у полосы."""
        self.busy = busy
        if busy:
            self.cancel_event.clear()
        else:
            self._lock_tabs(False)
        self.progress_text.set(f"{what or 'Идёт работа'}…" if busy else "")
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        state = "disabled" if busy else "normal"
        for b in (self.scan_btn, self.apply_btn, self.subs_btn,
                  getattr(self, "edl_detect_btn", None),
                  getattr(self, "edl_write_btn", None),
                  getattr(self, "edl_delete_btn", None),
                  getattr(self, "online_load_btn", None),
                  getattr(self, "online_take_on_btn", None),
                  getattr(self, "online_take_loc_btn", None),
                  getattr(self, "chapters_write_btn", None),
                  getattr(self, "chapters_clear_btn", None),
                  *self.extra_busy_buttons):
            if b is not None:
                b.configure(state=state)
        if busy:
            self._lock_tabs(True)
        # Работа кончилась — часть кнопок EDL всё равно гасится: включать
        # «Удалить .edl», когда удалять нечего, незачем.
        self._sync_edl_buttons()
        if not busy and hasattr(self, "edl_outro_end_spin"):
            self._sync_outro_end_spin()

    # Что гасить на время работы. Во ttk Spinbox и Combobox — те же Entry.
    _LOCKABLE = (ttk.Button, ttk.Checkbutton, ttk.Radiobutton, ttk.Entry)

    def _lock_tabs(self, locked: bool):
        """Гасит на время работы всё, что можно нажать или вписать на вкладках
        «Дорожки» и «EDL», и возвращает как было. Главные кнопки гасит сам
        set_busy; здесь — остальное: «Задать», галки, поля, отступы. Правка
        таймингов посреди записи легла бы в .edl вперемешку со старыми.

        Состояние меняется флагом ttk, а не опцией state: так у Combobox
        сохраняется его «только выбор из списка». Гасится только то, что было
        доступно, и возвращается только оно.
        """
        if not locked:
            for w in getattr(self, "_locked_widgets", []):
                try:
                    w.state(["!disabled"])
                except tk.TclError:
                    pass                          # виджет успели уничтожить
            self._locked_widgets = []
            return
        found, stack = [], [self.tab_tracks, self.tab_edl]
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            if isinstance(w, self._LOCKABLE) and not w.instate(["disabled"]):
                w.state(["disabled"])
                found.append(w)
        # Дописываем, а не заменяем: если работа начата поверх работы, первый
        # список иначе потерялся бы, и его виджеты остались бы погашенными.
        self._locked_widgets = getattr(self, "_locked_widgets", []) + found

    def audio_choice(self):
        i = self.audio_combo.current()
        if i <= 0:
            return None
        return self.audio_options[i - 1].key

    def sub_choice(self):
        i = self.sub_combo.current()
        if i == 0:
            return None
        if i == 1:
            return core.SUB_DISABLE
        return self.sub_options[i - 2].key

    # ---------------------------------------------------------------- scan --
    def browse(self):
        d = filedialog.askdirectory(initialdir=self.path_var.get() or None)
        if d:
            self.path_var.set(d)

    def browse_file(self):
        """Выбор одиночного видеофайла — для фильма, лежащего без своей папки."""
        current = Path(self.path_var.get() or ".")
        start = current if current.is_dir() else current.parent
        f = filedialog.askopenfilename(
            initialdir=str(start) if start.exists() else None,
            filetypes=[("Видео", "*.mkv *.mp4 *.avi *.m4v *.ts *.mov"),
                       ("Все файлы", "*.*")])
        if f:
            self.path_var.set(f)

    def scan(self):
        if self.busy:
            return
        folder = self.path_var.get().strip()
        # Путь может указывать и на одиночный файл фильма — его выбирают
        # кнопкой «Файл…», и дорожки в нём настраиваются так же, как в сериале.
        if not folder or not Path(folder).exists():
            messagebox.showerror("Ошибка", "Укажите существующую папку или файл.")
            return
        save_app_settings({**load_app_settings(), "last_path": folder})
        # Нажатие «Сканировать» — тоже подтверждение выбора папки, так что
        # вкладка «Медиатека» подставляет по ней название, если его ещё нет.
        if getattr(self, "meta", None) is not None:
            self.meta.fill_from_path()
        try:
            core.find_tools()
        except FileNotFoundError as e:
            messagebox.showerror("MKVToolNix не найден", str(e))
            return

        self.set_busy(True, "Сканирование")
        self.summary_var.set("Сканирование…")
        self.log_line(f"Сканирование: {folder} (рекурсивно: {self.recursive_var.get()})")
        recursive = self.recursive_var.get()
        self.progress.configure(value=0, maximum=1)

        def progress(i, total, path):
            self.root.after(0, lambda: self._scan_progress(i, total, path))

        def work():
            try:
                files = core.scan_folder(folder, recursive, progress=progress,
                                         stop=self.cancel_event.is_set)
            except Exception as e:  # noqa: BLE001
                self.root.after(0, lambda: self._scan_error(e))
                return
            if self.cancel_event.is_set():
                # Частичный список не показываем — остаётся прежнее состояние.
                self.root.after(0, self._scan_cancelled)
                return
            self.root.after(0, lambda: self._scan_done(files))

        threading.Thread(target=work, daemon=True).start()

    def _scan_progress(self, i, total, path):
        # i — число уже готовых файлов (1..total), path — последний завершённый.
        self.progress.configure(maximum=max(total, 1), value=i)
        self.summary_var.set(f"Сканирование {i}/{total}: {Path(path).name}")

    def _scan_error(self, e):
        self.set_busy(False)
        self.summary_var.set("Ошибка сканирования.")
        messagebox.showerror("Ошибка", str(e))

    def _scan_cancelled(self):
        self.set_busy(False)
        self.progress.configure(value=0)
        self.summary_var.set("Сканирование отменено.")
        self.log_line("Сканирование отменено.")

    def _scan_done(self, files):
        self.files = files
        ok = [f for f in files if not f.error]
        self.total_files = len(ok)
        self.audio_options, _ = core.aggregate_audio(ok)
        self.sub_options, _ = core.aggregate_subtitles(ok)
        groups = core.group_by_audio_signature(ok)

        self.audio_combo.configure(values=[NOTOUCH] + [self._opt_text(o) for o in self.audio_options])
        self.audio_combo.current(0)
        self.sub_combo.configure(values=[NOTOUCH, SUBOFF] + [self._opt_text(o) for o in self.sub_options])
        self.sub_combo.current(0)

        errs = len(files) - len(ok)
        msg = f"Найдено MKV: {len(files)} (ошибок: {errs}). Раскладок аудио: {len(groups)}."
        if len(groups) > 1:
            msg += "  ⚠ Раскладки различаются между файлами — выбирайте дорожку по названию."
        target = Path(self.path_var.get().strip() or ".")
        if not files and target.is_file():
            # Указан одиночный файл не того формата: mkvpropedit работает
            # только с MKV, но вкладка «Медиатека» такой файл всё равно разложит.
            msg = (f"«{target.name}» — не MKV, дорожки в нём менять нечем. "
                   "Разложить его по папкам можно на вкладке «Медиатека».")
        self.summary_var.set(msg)
        self.progress.configure(value=0)
        self.log_line(msg)
        self.set_busy(False)
        self.refresh_preview()
        self._rebuild_edl_eps()
        self.refresh_edl_preview()

    def _opt_text(self, o: core.Option) -> str:
        return f"{o.label} — {o.count}/{self.total_files}"

    # ------------------------------------------------------------- preview --
    def refresh_preview(self):
        self.tree.delete(*self.tree.get_children())
        if not self.files:
            self.preview_status.set("")
            return
        ac = self.audio_choice()
        sc = self.sub_choice()
        plans = core.plan_changes(self.files, ac, sc)
        n_change = n_warn = 0
        for plan in plans:
            f = plan.file
            if f.error:
                self.tree.insert("", "end", values=(f.path.name, "", "", "", "", f"ошибка: {f.error}"), tags=("error",))
                n_warn += 1
                continue

            acur = self._cur_default(f.audio)
            scur = self._cur_default(f.subtitles)

            if ac is None:
                anew = "(не трогаем)"
            elif plan.audio_target is not None:
                anew = plan.audio_target.label()
            else:
                anew = "⚠ нет дорожки"

            if sc is None:
                snew = "(не трогаем)"
            elif plan.sub_disabled:
                snew = "выключены"
            elif plan.sub_target is not None:
                snew = plan.sub_target.label()
            else:
                snew = "⚠ нет дорожки"

            if plan.warnings:
                tag = "warn"; status = "; ".join(plan.warnings); n_warn += 1
            elif plan.has_changes:
                tag = "change"; status = "будет изменён"; n_change += 1
            else:
                tag = "nochange"; status = "без изменений"

            self.tree.insert("", "end", values=(f.path.name, acur, anew, scur, snew, status), tags=(tag,))

        # Счётчик у кнопки «Применить», а не в лог: превью пересчитывается при каждом
        # переключении комбобокса, и лог быстро замусоривался бы.
        self.preview_status.set(f"К изменению: {n_change}, предупреждений: {n_warn}.")

    def _cur_default(self, tracks) -> str:
        d = [t for t in tracks if t.default]
        return d[0].label() if d else "—"

    # --------------------------------------------------------------- apply --
    def apply(self):
        if self.busy or not self.files:
            if not self.files:
                messagebox.showinfo("Нет данных", "Сначала просканируйте папку.")
            return
        ac = self.audio_choice()
        sc = self.sub_choice()
        if ac is None and sc is None:
            messagebox.showinfo("Ничего не выбрано", "Выберите аудио и/или субтитры.")
            return

        try:
            _, propedit = core.find_tools()
        except FileNotFoundError as e:
            messagebox.showerror("MKVToolNix не найден", str(e))
            return

        plans = core.plan_changes(self.files, ac, sc)
        todo = [p for p in plans if not p.file.error and core.build_command(propedit, p)]
        if not todo:
            messagebox.showinfo("Нечего применять", "Во всех файлах уже стоят нужные дорожки.")
            return

        warns = sum(1 for p in plans if p.warnings)
        text = f"Будет изменено файлов: {len(todo)}."
        if warns:
            text += f"\nС предупреждениями (будут пропущены/частично): {warns}."
        text += "\n\nИзменения обратимы — само видео не трогается. Продолжить?"
        if not messagebox.askyesno("Подтверждение", text):
            return

        self.set_busy(True, "Применение дорожек")
        self.progress.configure(value=0, maximum=len(todo))
        self.log_line(f"ПРИМЕНЕНИЕ: {len(todo)} файл(ов)")

        def work():
            ok = 0
            for i, p in enumerate(todo, 1):
                if self.cancel_event.is_set():
                    self.root.after(0, lambda: self.log_line("Применение отменено."))
                    break
                res = core.apply_plan(propedit, p)
                ok += 1 if res.ok else 0
                self.root.after(0, lambda r=res, d=i: self._apply_step(r, d))
            # И после отмены пересканируем: часть файлов уже изменена.
            self.root.after(0, lambda: self._apply_done(ok, len(todo)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_step(self, res: core.ApplyResult, done: int):
        self.progress.configure(value=done)
        name = res.plan.file.path.name
        if res.ok:
            self.log_line(f"  ✓ {name}")
        else:
            self.log_line(f"  ✗ {name}: {res.output}")

    def _apply_done(self, ok: int, total: int):
        self.log_line(f"Готово: успешно {ok}/{total}. Обновляю состояние…")
        # пересканировать, чтобы показать новые дефолты
        folder = self.path_var.get().strip()
        recursive = self.recursive_var.get()
        ac_i, sc_i = self.audio_combo.current(), self.sub_combo.current()

        def work():
            files = core.scan_folder(folder, recursive)
            self.root.after(0, lambda: self._after_apply_rescan(files, ac_i, sc_i))

        threading.Thread(target=work, daemon=True).start()

    def _after_apply_rescan(self, files, ac_i, sc_i):
        self.files = files
        ok = [f for f in files if not f.error]
        self.total_files = len(ok)
        self.audio_options, _ = core.aggregate_audio(ok)
        self.sub_options, _ = core.aggregate_subtitles(ok)
        self.audio_combo.configure(values=[NOTOUCH] + [self._opt_text(o) for o in self.audio_options])
        self.sub_combo.configure(values=[NOTOUCH, SUBOFF] + [self._opt_text(o) for o in self.sub_options])
        self.audio_combo.current(min(ac_i, len(self.audio_options)))
        self.sub_combo.current(min(sc_i, len(self.sub_options) + 1))
        self.progress.configure(value=0)
        self.set_busy(False)
        self.refresh_preview()
        self._rebuild_edl_eps()
        self.refresh_edl_preview()


    # ------------------------------------------------------ субтитры (.ru.srt) --
    def download_subs(self):
        if self.busy:
            return
        import subs as subsmod  # ленивый импорт: subliminal тяжёлый, без него остальное работает

        err = subsmod.subliminal_available()
        if err:
            messagebox.showerror(
                "subliminal не установлен",
                "Для скачивания субтитров нужен пакет subliminal:\n\n    pip install subliminal\n\n"
                f"({err})",
            )
            return
        if not self.files:
            messagebox.showinfo("Нет данных", "Сначала просканируйте папку.")
            return

        settings = subsmod.load_settings()
        if not subsmod.providers_for(settings):
            messagebox.showwarning(
                "Не задан источник",
                "Укажите данные OpenSubtitles (кнопка «OpenSubtitles…») или включите запасные провайдеры.",
            )
            return
        if not settings.has_opensubtitles():
            if not messagebox.askyesno(
                "Без OpenSubtitles",
                "Данные OpenSubtitles не заданы. Бесплатные источники для русского почти всегда пусты.\n"
                "Продолжить только на запасных провайдерах?",
            ):
                return

        ok_files = [f for f in self.files if not f.error]
        if self.subs_only_missing.get():
            targets = [f.path for f in subsmod.missing_russian(ok_files)]
        else:
            targets = [f.path for f in ok_files]
        if not targets:
            messagebox.showinfo("Нечего качать", "У всех серий русские субтитры уже есть.")
            return

        if not messagebox.askyesno(
            "Скачивание субтитров",
            f"Найти и скачать русские субтитры для {len(targets)} серий?\n"
            "Файлы лягут рядом как .ru.srt — видео не трогается.",
        ):
            return

        self.set_busy(True, "Скачивание субтитров")
        self.subs_status.set("")
        self.progress.configure(value=0, maximum=len(targets))
        self.log_line(f"СУБТИТРЫ: ищу русские для {len(targets)} серий…")

        def progress(i, total, path):
            self.root.after(0, lambda: self._subs_progress(i, total, path))

        def work():
            try:
                results = subsmod.download(targets, settings, progress=progress,
                                           stop=self.cancel_event.is_set)
            except Exception as e:  # noqa: BLE001
                self.root.after(0, lambda: self._subs_error(e))
                return
            self.root.after(0, lambda: self._subs_done(results))

        threading.Thread(target=work, daemon=True).start()

    def _subs_progress(self, i, total, path):
        self.progress.configure(maximum=max(total, 1), value=i)
        self.subs_status.set(f"{i + 1}/{total}: {Path(path).name}")

    def _subs_error(self, e):
        self.set_busy(False)
        self.subs_status.set("ошибка")
        self.progress.configure(value=0)
        messagebox.showerror("Ошибка", str(e))

    def _subs_done(self, results):
        dl = sum(1 for r in results if r.status == "downloaded")
        nf = sum(1 for r in results if r.status == "notfound")
        errs = [r for r in results if r.status == "error"]
        for r in results:
            if r.status == "downloaded":
                self.log_line(f"  ✓ {r.path.name}  ({r.provider})")
            elif r.status == "notfound":
                self.log_line(f"  – {r.path.name}: нет русских")
            else:
                self.log_line(f"  ✗ {r.path.name}: {r.detail}")
        msg = f"Субтитры: скачано {dl}, не найдено {nf}, ошибок {len(errs)}."
        if self.cancel_event.is_set():
            msg = "Отменено. " + msg
        self.log_line(msg)
        self.subs_status.set(msg)
        self.progress.configure(value=0)
        self.set_busy(False)

    def subs_settings_dialog(self):
        import subs as subsmod
        s = subsmod.load_settings()

        win = tk.Toplevel(self.root)
        win.title("OpenSubtitles")
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(
            frm,
            text="Бесплатный аккаунт OpenSubtitles.com нужен для русских субтитров.\n"
                 "API-ключ: войти на opensubtitles.com → профиль → Consumers → New consumer.\n"
                 "Поля можно оставить пустыми — тогда поиск идёт только по запасным\n"
                 "провайдерам (для русского там обычно пусто).",
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        u = tk.StringVar(value=s.username)
        p = tk.StringVar(value=s.password)
        k = tk.StringVar(value=s.apikey)
        fb = tk.BooleanVar(value=s.use_fallback)
        rows = (("Логин:", u, False), ("Пароль:", p, True), ("API-ключ:", k, False))
        for i, (lab, var, secret) in enumerate(rows, start=1):
            ttk.Label(frm, text=lab).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(frm, textvariable=var, width=44, show="*" if secret else "").grid(
                row=i, column=1, sticky="ew", pady=2
            )
        ttk.Checkbutton(
            frm, text="Использовать запасные провайдеры (podnapisi/tvsubtitles/gestdown)",
            variable=fb,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 8))

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=2, sticky="e")

        def save():
            subsmod.save_settings(subsmod.Settings(u.get().strip(), p.get(), k.get().strip(), fb.get()))
            win.destroy()

        ttk.Button(btns, text="Сохранить", command=save).pack(side="right")
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="right", padx=6)
        win.grab_set()

    # ------------------------------------------------------- EDL (пропуск) --
    @staticmethod
    def _fmt_time(s) -> str:
        if s is None:
            return "—"
        s = max(0, int(round(s)))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

    @staticmethod
    def _parse_time(txt: str):
        txt = (txt or "").strip()
        if not txt:
            return None
        try:
            if ":" in txt:
                s = 0.0
                for part in txt.split(":"):
                    s = s * 60 + float(part)
                return s
            return float(txt)
        except ValueError:
            return None

    def _pad_val(self, key: str) -> float:
        return self._parse_time(self.edl_pad[key].get()) or 0.0

    def _window_val(self, key: str) -> float:
        """Окно поиска в секундах. Мусор или совсем мало — берём умолчание.

        Нижняя граница не придирка: сегмент короче MIN_LEN_S детект отбрасывает,
        так что окно в пару секунд гарантированно не нашло бы ничего.
        """
        default = edl.INTRO_WINDOW if key == "intro" else edl.OUTRO_WINDOW
        value = self._parse_time(self.edl_window[key].get())
        return value if value and value >= 30 else default

    def edl_padding(self) -> edl.Padding:
        return edl.Padding(
            self._pad_val("intro_start"), self._pad_val("intro_end"),
            self._pad_val("outro_start"), self._pad_val("outro_end"),
        )

    def _sync_outro_end_spin(self):
        """Гасит отступ конца титров, пока он ни на что не влияет.

        Влиять ему не на что, только если титры у всех серий тянутся до конца
        файла. Там, где конец задан руками, отступ ложится поверх него — и тогда
        поле должно быть доступным, иначе забытое в нём значение применялось бы
        втихую, а поправить его было бы нечем.

        Состояние виджета и перерисовка таблицы разведены намеренно: перерисовка
        сама вызывает этот метод, и объединение их замкнуло бы рекурсию.
        """
        if self.busy:
            return            # на время работы поле погашено; set_busy вернёт сам
        fixed = any(ep.outro_fixed_end for ep in getattr(self, "edl_eps", []))
        active = not self.edl_outro_to_end.get() or fixed
        self.edl_outro_end_spin.configure(state="normal" if active else "disabled")

    def _on_outro_to_end(self):
        self._sync_outro_end_spin()
        self.refresh_edl_preview()

    # Настройки вкладки EDL переживают перезапуск: отступы и дорожка детекта
    # подбираются под конкретную медиатеку, набирать их заново каждый раз незачем.
    # Момент записи — выход из программы и удачная запись .edl.
    def _save_edl_settings(self):
        save_app_settings({**load_app_settings(), "edl": {
            "keep_first": self.edl_keep_first.get(),
            "outro_to_end": self.edl_outro_to_end.get(),
            "trim_tail": self.edl_trim_tail.get(),
            "track": self.edl_track_var.get(),
            "pad": {k: v.get() for k, v in self.edl_pad.items()},
            "window": {k: v.get() for k, v in self.edl_window.items()},
        }})

    def _load_edl_settings(self):
        data = load_app_settings().get("edl")
        if not isinstance(data, dict):
            return
        self.edl_keep_first.set(bool(data.get("keep_first", True)))
        self.edl_outro_to_end.set(bool(data.get("outro_to_end", True)))
        self.edl_trim_tail.set(bool(data.get("trim_tail", True)))
        track = data.get("track")
        if isinstance(track, str) and track:
            # Языка может не оказаться в новой папке — скан вернёт «Оригинал (авто)».
            self.edl_track_var.set(track)
            self.edl_track_combo.configure(values=[EDL_TRACK_AUTO, track])
        for key, value in (data.get("pad") or {}).items():
            if key in self.edl_pad:
                self.edl_pad[key].set(str(value))
        for key, value in (data.get("window") or {}).items():
            if key in self.edl_window:
                self.edl_window[key].set(str(value))
        self._on_outro_to_end()

    def _rebuild_edl_eps(self):
        """Пересобирает список серий из self.files, сохраняя уже найденные тайминги."""
        prev = {str(e.path): e for e in self.edl_eps}
        eps = []
        langs: list[str] = []
        for f in self.files:
            if f.error:
                continue
            file_langs = [t.language or "" for t in f.audio]
            for l in file_langs:
                if l and l not in langs:
                    langs.append(l)
            e = edl.EpisodeEdl.from_path(f.path, duration=f.duration, audio_langs=file_langs)
            e.chapters = f.chapters
            e.tail = trim.tail_seconds(f.tracks)
            old = prev.get(str(f.path))
            if old:
                e.intro, e.outro, e.recap, e.note = old.intro, old.outro, old.recap, old.note
                e.outro_fixed_end = old.outro_fixed_end
                e.local_intro, e.local_outro, e.local_recap = \
                    old.local_intro, old.local_outro, old.local_recap
                e.online_intro, e.online_outro, e.online_recap = \
                    old.online_intro, old.online_outro, old.online_recap
                e.online_note = old.online_note
            elif edl.has_external_edl(f.path):
                # Свежий скан: подхватываем тайминги из уже записанного .edl —
                # можно править вручную без повторного детекта. В файле лежат
                # финальные значения (padding уже применён при записи), поэтому
                # ненулевые отступы лягут поверх них ещё раз.
                e.recap, e.intro, e.outro = edl.read_edl(f.path)
                if e.recap or e.intro or e.outro:
                    e.note = "из .edl"
                # Титры, не доходящие до конца файла, кто-то задал руками — иначе
                # их дотянули бы до самого конца. Не пометив это, следующая запись
                # растянула бы их обратно и съела сцену после титров.
                if e.outro and e.duration and e.outro.end < e.duration - edl.END_EPS:
                    e.outro_fixed_end = True
            eps.append(e)
        self.edl_eps = eps
        # Список пересобран в порядке скана — прежняя сортировка к нему не относится.
        self.edl_sort = (None, False)

        # Обновляем список языков для выбора дорожки детекта.
        if hasattr(self, "edl_track_combo"):
            values = [EDL_TRACK_AUTO] + sorted(langs)
            self.edl_track_combo.configure(values=values)
            if self.edl_track_var.get() not in values:
                self.edl_track_var.set(EDL_TRACK_AUTO)
        self._update_track_info()

    def _update_track_info(self):
        """Показывает, какую именно дорожку выберет текущая настройка (язык + название)."""
        if not hasattr(self, "edl_track_info"):
            return
        prefer = None if self.edl_track_var.get() == EDL_TRACK_AUTO else self.edl_track_var.get()
        info = ""
        for f in self.files:
            if getattr(f, "error", "") or not f.audio:
                continue
            idx = edl.pick_audio_index([t.language or "" for t in f.audio], prefer)
            if 0 <= idx < len(f.audio):
                t = f.audio[idx]
                name = t.name.strip()
                info = f"→ {t.language or 'und'}" + (f" «{name}»" if name else "")
            break
        self.edl_track_info.set(info)

    def _edl_have_eps(self) -> bool:
        if not self.edl_eps:
            messagebox.showinfo("Нет данных", "Сначала просканируйте папку.")
            return False
        return True

    def _update_scope_count(self, *_):
        """Обновляет счётчик у радиокнопки «к выделенным (N)» при смене выделения."""
        if hasattr(self, "edl_scope_sel_rb"):
            self.edl_scope_sel_rb.configure(text=f"к выделенным ({len(self.edl_tree.selection())})")

    def _edl_scope_eps(self):
        """Серии, к которым применять значения: все или выделенные (по scope).

        None — если применять не к чему (нет данных или пустое выделение): вызывающий
        просто выходит.
        """
        if not self.edl_eps:
            messagebox.showinfo("Нет данных", "Сначала просканируйте папку.")
            return None
        if self.edl_scope.get() == "sel":
            eps = [self.edl_row_ep[i] for i in self.edl_tree.selection() if i in self.edl_row_ep]
            if not eps:
                messagebox.showinfo("Нет выделения",
                                    "Выделите серии в таблице (Shift/Ctrl) или переключите "
                                    "«Применять к: ко всем сериям».")
                return None
            return eps
        return list(self.edl_eps)

    def _scope_word(self, n: int) -> str:
        return f"выделенным ({n})" if self.edl_scope.get() == "sel" else f"всем ({n})"

    def _scope_phrase(self) -> str:
        """«по чему» работает действие — для текста в диалогах подтверждения."""
        return ("выделенным сериям" if self.edl_scope.get() == "sel"
                else "всем просканированным сериям")

    def apply_intro_all(self):
        eps = self._edl_scope_eps()
        if eps is None:
            return
        s = self._parse_time(self.edl_manual["intro_start"].get())
        e_ = self._parse_time(self.edl_manual["intro_end"].get())
        if s is None or e_ is None or e_ <= s:
            messagebox.showinfo("Интро", "Укажите начало и конец интро (например 0:00 и 0:13).")
            return
        for ep in eps:
            ep.intro = edl.Segment(s, e_)
        self.refresh_edl_preview()
        self.log_line(f"Интро задано {self._scope_word(len(eps))}: {self._fmt_time(s)}–{self._fmt_time(e_)}.")

    def apply_intro_dur_all(self):
        """Конец интро = его начало + N сек. Начало у каждой серии остаётся своё
        (из автодетекта или ручной правки) — серии без начала пропускаются."""
        eps = self._edl_scope_eps()
        if eps is None:
            return
        n = self._parse_time(self.edl_manual["intro_dur"].get())
        if not n or n <= 0:
            messagebox.showinfo("Интро", "Укажите продолжительность интро в секундах (например 30) — "
                                          "конец станет «начало + N» у серий с известным началом.")
            return
        done = skipped = 0
        for ep in eps:
            if ep.intro:
                ep.intro = edl.Segment(ep.intro.start, ep.intro.start + n)
                done += 1
            else:
                skipped += 1
        self.refresh_edl_preview()
        msg = f"Длительность интро {self._fmt_time(n)} применена: {done} серий."
        if skipped:
            msg += f" Пропущено без начала интро: {skipped} (задайте начало двойным кликом)."
        self.log_line(msg)

    def apply_outro_all(self):
        eps = self._edl_scope_eps()
        if eps is None:
            return
        s = self._parse_time(self.edl_manual["outro_start"].get())
        if s is None:
            messagebox.showinfo("Титры", "Укажите начало титров (например 20:30). "
                                         "Конец можно не указывать — тогда до конца файла.")
            return
        fixed = self._parse_time(self.edl_manual["outro_end"].get())
        if fixed is not None and fixed <= s:
            messagebox.showinfo("Титры", "Конец титров должен быть позже начала.")
            return
        for ep in eps:
            end = fixed if fixed is not None else (ep.duration or (s + 60))
            ep.outro = edl.Segment(s, end) if end > s else None
            ep.outro_fixed_end = ep.outro is not None and fixed is not None
        self.refresh_edl_preview()
        tail = (f"по {self._fmt_time(fixed)}" if fixed is not None else "до конца файла")
        self.log_line(f"Титры заданы {self._scope_word(len(eps))}: "
                      f"с {self._fmt_time(s)} {tail}.")

    def apply_outro_last_all(self):
        eps = self._edl_scope_eps()
        if eps is None:
            return
        n = self._parse_time(self.edl_manual["outro_last"].get())
        if not n or n <= 0:
            messagebox.showinfo("Титры", "Укажите длительность титров в секундах (например 60) — "
                                          "вырежутся последними N сек каждой серии.")
            return
        done = 0
        for ep in eps:
            if ep.duration:
                ep.outro = edl.Segment(max(0.0, ep.duration - n), ep.duration)
                ep.outro_fixed_end = False
                done += 1
        self.refresh_edl_preview()
        self.log_line(f"Титры заданы как последние {self._fmt_time(n)} "
                      f"({done} серий с известной длительностью).")

    def apply_recap_all(self):
        eps = self._edl_scope_eps()
        if eps is None:
            return
        x = self._parse_time(self.edl_manual["recap_end"].get())
        if not x or x <= 0:
            messagebox.showinfo("Recap", "Укажите конец recap (например 0:13) — начало с начала файла.")
            return
        for ep in eps:
            ep.recap = edl.Segment(0.0, x)
        self.refresh_edl_preview()
        self.log_line(f"Recap задан {self._scope_word(len(eps))}: 0:00–{self._fmt_time(x)}.")

    def _calc_reference_ep(self):
        """Серия, по которой считаем: выделенная, иначе первая с известной длительностью."""
        if not self.edl_eps:
            messagebox.showinfo("Нет данных", "Сначала просканируйте папку.")
            return None
        sel = [self.edl_row_ep[i] for i in self.edl_tree.selection() if i in self.edl_row_ep]
        for ep in (sel or self.edl_eps):
            if ep.duration:
                return ep
        messagebox.showinfo("Нет длительности",
                            "У этих серий неизвестна длительность — считать не от чего.")
        return None

    def calc_outro_last(self):
        """Продолжительность титров = длительность серии − время их начала.

        Начало у каждой серии своё, а продолжительность обычно общая — её и просит
        поле «последние N сек». Смотреть начало в плеере по каждой серии незачем:
        достаточно одной, разницу посчитаем сами.
        """
        start = self._parse_time(self.edl_manual["outro_from_start"].get())
        if start is None or start < 0:
            messagebox.showinfo("Титры", "Укажите, на какой минуте пошли титры — "
                                         "MM:SS или секунды (например 56:10).")
            return
        ref = self._calc_reference_ep()
        if ref is None:
            return
        if start >= ref.duration:
            messagebox.showinfo(
                "Титры", f"Титры не могут начинаться позже конца серии: у "
                         f"«{ref.path.name}» длительность {self._fmt_time(ref.duration)}.")
            return
        length = ref.duration - start
        self.edl_manual["outro_last"].set(f"{length:.0f}")
        self.edl_outro_calc.set(f"{self._fmt_time(ref.duration)} − {self._fmt_time(start)} = "
                                f"{self._fmt_time(length)} → {length:.0f} сек")
        self.log_line(f"Продолжительность титров по «{ref.path.name}»: "
                      f"{self._fmt_time(ref.duration)} − {self._fmt_time(start)} = "
                      f"{length:.0f} сек. Подставлено в «последние» — нажмите «Задать».")

    def clear_segment_scope(self, kind: str):
        """Убирает один сегмент (intro/outro/recap) у серий по scope."""
        eps = self._edl_scope_eps()
        if eps is None:
            return
        label = {"intro": "интро", "outro": "титры", "recap": "recap"}[kind]
        for ep in eps:
            setattr(ep, kind, None)
            if kind == "outro":
                ep.outro_fixed_end = False
        self.refresh_edl_preview()
        self.log_line(f"Убрано «{label}» у {len(eps)} серий.")

    def clear_all_segments(self):
        eps = self._edl_scope_eps()
        if eps is None:
            return
        if not messagebox.askyesno("Убрать всё",
                                   f"Сбросить интро, титры и recap у {len(eps)} серий?"):
            return
        for ep in eps:
            ep.intro = ep.outro = ep.recap = None
            ep.outro_fixed_end = False
        self.refresh_edl_preview()
        self.log_line(f"Сброшены интро/титры/recap у {len(eps)} серий.")

    # ---------------------------------------------------- онлайн-тайминги --
    def take_online(self):
        """Переносит онлайн-тайминги в активные (по scope). Пофайлово по сегментам:
        перезаписываем только те, что онлайн реально нашёл — локальные не теряются."""
        eps = self._edl_scope_eps()
        if eps is None:
            return
        n = 0
        for ep in eps:
            got = False
            if ep.online_intro:
                ep.intro = ep.online_intro; got = True
            if ep.online_outro:
                ep.outro = ep.online_outro
                ep.outro_fixed_end = False
                got = True
            if ep.online_recap:
                ep.recap = ep.online_recap; got = True
            n += 1 if got else 0
        self.refresh_edl_preview()
        self.log_line(f"Взяты онлайн-тайминги: применены к {n} из {len(eps)} серий "
                      "(где онлайн-данные есть). Для полного удаления сегмента — «Убрать».")

    def take_local(self):
        """Возвращает активным значениям снимок локального детекта (по scope, по сегментам)."""
        eps = self._edl_scope_eps()
        if eps is None:
            return
        n = 0
        for ep in eps:
            got = False
            if ep.local_intro:
                ep.intro = ep.local_intro; got = True
            if ep.local_outro:
                ep.outro = ep.local_outro
                ep.outro_fixed_end = False
                got = True
            if ep.local_recap:
                ep.recap = ep.local_recap; got = True
            n += 1 if got else 0
        self.refresh_edl_preview()
        self.log_line(f"Возвращены локальные тайминги у {n} из {len(eps)} серий "
                      "(у которых был локальный детект).")

    def online_dialog(self):
        if self.busy:
            return
        if not self.edl_eps:
            messagebox.showinfo("Нет данных", "Сначала просканируйте папку.")
            return
        import online as onlinemod  # ленивый импорт: сеть нужна только здесь

        eps_se = [e for e in self.edl_eps if e.episode is not None]
        if not eps_se:
            messagebox.showinfo("Нет номеров серий",
                                "У файлов не распознаны S/E (SxxEyy) — онлайн-сопоставление невозможно.")
            return
        emin = min(e.episode for e in eps_se)
        emax = max(e.episode for e in eps_se)
        folder = self.path_var.get().strip()

        win = tk.Toplevel(self.root)
        win.title("Онлайн-тайминги")
        win.transient(self.root)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, justify="left", text=(
            "Правила сопоставления серий с онлайн-базой. Для аниме — AniSkip (ID = MyAnimeList),\n"
            "для сериалов/фильмов — TheIntroDB (ID = TMDb-число или IMDb вида tt…).\n"
            "«Серии от…до» — по номеру E из имени файла; «онлайн E1» — какой серии базы\n"
            "соответствует первая серия диапазона (для AniSkip; лечит склейку двух cours).\n"
            f"Найденные серии: E{emin:02d}–E{emax:02d}."
        )).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 8))

        for c, h in enumerate(("Источник", "ID", "Серии от", "до", "онлайн E1")):
            ttk.Label(frm, text=h).grid(row=1, column=c, sticky="w", padx=3)

        rows: list[dict] = []
        rows_frame = ttk.Frame(frm)
        rows_frame.grid(row=2, column=0, columnspan=6, sticky="ew")

        def add_row(source="AniSkip", idv="", efrom=emin, eto=emax, first=1):
            r = len(rows)
            src = tk.StringVar(value=source)
            idvar = tk.StringVar(value=str(idv))
            fromv, tov, firstv = (tk.StringVar(value=str(efrom)),
                                  tk.StringVar(value=str(eto)), tk.StringVar(value=str(first)))
            widgets = [
                ttk.Combobox(rows_frame, textvariable=src, state="readonly", width=11,
                             values=["AniSkip", "TheIntroDB"]),
                ttk.Entry(rows_frame, textvariable=idvar, width=16),
                ttk.Entry(rows_frame, textvariable=fromv, width=6),
                ttk.Entry(rows_frame, textvariable=tov, width=6),
                ttk.Entry(rows_frame, textvariable=firstv, width=6),
            ]
            for c, w in enumerate(widgets):
                w.grid(row=r, column=c, padx=3, pady=2)
            rows.append({"source": src, "id": idvar, "from": fromv, "to": tov,
                         "first": firstv, "widgets": widgets})

        def clear_rows():
            for row in rows:
                for w in row["widgets"]:
                    w.destroy()
            rows.clear()

        saved = load_app_settings().get("online_rules", {}).get(folder)
        if saved:
            for rule in saved:
                add_row(rule.get("source", "AniSkip"), rule.get("id", ""),
                        rule.get("from", emin), rule.get("to", emax), rule.get("first", 1))
        else:
            add_row()

        btnbar = ttk.Frame(frm)
        btnbar.grid(row=3, column=0, columnspan=6, sticky="w", pady=(8, 0))
        ttk.Button(btnbar, text="+ правило", command=lambda: add_row()).pack(side="left")

        def auto_suggest():
            # Подбираем ID сразу для обоих источников по названию/метаданным:
            #  TheIntroDB — ID СЕРИАЛА из tvshow.nfo (tmdb→imdb), иначе TVmaze по названию;
            #  AniSkip — MAL из tvshow.nfo, иначе поиск Jikan по названию, с авто-разбивкой
            #  на cours (в MAL один тайтл = один cour, поэтому 48 серий = два MAL ID).
            title = onlinemod.clean_show_title(Path(folder).name)
            try:
                ids = onlinemod.read_nfo_ids(eps_se[0].path)
            except Exception:  # noqa: BLE001 — .nfo необязателен
                ids = {}

            idb_id, idb_src = (ids.get("tmdb") or ids.get("imdb")), "tvshow.nfo"
            if not idb_id:
                try:
                    imdb = onlinemod.search_tvmaze_imdb(title)
                except onlinemod.OnlineError:
                    imdb = None
                if imdb:
                    idb_id, idb_src = imdb, "TVmaze (по названию)"

            mal_src, mal_cands = "tvshow.nfo", []
            aniskip_rules = []  # (mal_id, efrom, eto, first)
            if ids.get("mal"):
                aniskip_rules.append((ids["mal"], emin, emax, 1))
            else:
                try:
                    mal_cands = onlinemod.search_mal(title, 5)
                except onlinemod.OnlineError as ex:
                    self.log_line(f"MAL-поиск не удался: {ex}")
                if mal_cands:
                    mal_src = "Jikan (по названию)"
                    seasons = sorted([c for c in mal_cands if c.episodes],
                                     key=lambda c: (c.year or 9999))
                    if seasons and emax > seasons[0].episodes:
                        start = emin
                        for c in seasons:
                            if start > emax:
                                break
                            end = min(emax, start + c.episodes - 1)
                            aniskip_rules.append((str(c.mal_id), start, end, 1))
                            start = end + 1
                        if start <= emax:  # хвост не покрыт — добьём лучшим кандидатом
                            aniskip_rules.append((str(mal_cands[0].mal_id), start, emax, 1))
                    else:
                        aniskip_rules.append((str(mal_cands[0].mal_id), emin, emax, 1))

            if not idb_id and not aniskip_rules:
                messagebox.showinfo("Не найдено",
                                    "Не удалось подобрать ID ни из tvshow.nfo, ни по названию.\n"
                                    "Введите ID вручную: TMDb со страницы themoviedb.org/tv/<id>, "
                                    "MAL — с myanimelist.net.")
                return

            clear_rows()
            if idb_id:
                add_row("TheIntroDB", idb_id, emin, emax, 1)
            for mid, a, b, first in aniskip_rules:
                add_row("AniSkip", mid, a, b, first)
            if not rows:
                add_row()

            summary = []
            if idb_id:
                summary.append(f"TheIntroDB {idb_id} [{idb_src}]")
            if len(aniskip_rules) > 1:
                summary.append("AniSkip " + ", ".join(
                    f"E{a:02d}–E{b:02d}→MAL {mid}" for mid, a, b, _ in aniskip_rules) + f" [{mal_src}]")
            elif aniskip_rules:
                summary.append(f"AniSkip MAL {aniskip_rules[0][0]} [{mal_src}]")
            self.online_status.set("Подобрано (проверьте): " + "; ".join(summary))
            if mal_cands:
                messagebox.showinfo(
                    "Кандидаты MAL",
                    "Проверьте авто-разбивку по сезонам/cours:\n"
                    + "\n".join("• " + c.label() for c in mal_cands[:5])
                    + "\n\nЕсли раскладка серий неверна — поправьте «Серии от…до» и ID вручную.")

        ttk.Button(btnbar, text="Авто-подобрать", command=auto_suggest).pack(side="left", padx=8)

        def collect_rules():
            out = []
            for r in rows:
                idv = r["id"].get().strip()
                if not idv:
                    continue
                try:
                    ef, et = int(float(r["from"].get())), int(float(r["to"].get()))
                    fr = int(float(r["first"].get()))
                except ValueError:
                    continue
                out.append({"source": r["source"].get(), "id": idv, "from": ef, "to": et, "first": fr})
            return out

        def load():
            rules = collect_rules()
            if not rules:
                messagebox.showinfo("Нет правил", "Заполните хотя бы одно правило с ID.")
                return
            data = load_app_settings()
            data.setdefault("online_rules", {})[folder] = rules
            save_app_settings(data)
            win.destroy()
            self._online_fetch(rules)

        actionbar = ttk.Frame(frm)
        actionbar.grid(row=4, column=0, columnspan=6, sticky="e", pady=(12, 0))
        ttk.Button(actionbar, text="Загрузить тайминги", command=load).pack(side="right")
        ttk.Button(actionbar, text="Отмена", command=win.destroy).pack(side="right", padx=6)

        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")
        win.grab_set()

    def _online_fetch(self, rules):
        import online as onlinemod

        def match(ep):
            if ep.episode is None:
                return None
            for rule in rules:
                if rule["from"] <= ep.episode <= rule["to"]:
                    return rule
            return None

        targets = [(e, match(e)) for e in self.edl_eps]
        targets = [(e, r) for e, r in targets if r is not None]
        if not targets:
            messagebox.showinfo("Нечего загружать",
                                "Ни одна серия не попала под правила (проверьте диапазоны E).")
            return

        self.set_busy(True, "Загрузка онлайн-таймингов")
        self.online_status.set("")
        self.progress.configure(value=0, maximum=len(targets))
        self.log_line(f"ОНЛАЙН: запрашиваю тайминги для {len(targets)} серий…")
        total = len(targets)

        def work():
            import time
            ok = miss = err = 0
            for i, (ep, rule) in enumerate(targets, 1):
                if self.cancel_event.is_set():
                    break
                try:
                    res = self._fetch_one(onlinemod, ep, rule)
                    ep.online_intro, ep.online_outro, ep.online_recap = res.intro, res.outro, res.recap
                    ep.online_note = res.source + (f": {res.note}" if res.note else "")
                    ok += 1 if res.any else 0
                    miss += 0 if res.any else 1
                except onlinemod.OnlineError as ex:
                    ep.online_note = f"ошибка: {ex}"
                    err += 1
                self.root.after(0, lambda i=i, ep=ep: self._online_progress(i, total, ep))
                time.sleep(0.4)  # мягкий rate-limit (Jikan/AniSkip/TheIntroDB)
            self.root.after(0, lambda: self._online_done(ok, miss, err))

        threading.Thread(target=work, daemon=True).start()

    def _fetch_one(self, onlinemod, ep, rule):
        src, idv = rule["source"], rule["id"]
        if src == "AniSkip":
            if not idv.isdigit():
                raise onlinemod.OnlineError(f"MAL ID должен быть числом, а не «{idv}»")
            online_ep = ep.episode - rule["from"] + rule["first"]
            return onlinemod.fetch_aniskip(int(idv), online_ep, ep.duration)
        # TheIntroDB: season/episode как в имени файла; ID = tmdb (число) или imdb (tt…).
        if idv.lower().startswith("tt"):
            return onlinemod.fetch_theintrodb(imdb_id=idv, season=ep.season,
                                              episode=ep.episode, duration_s=ep.duration)
        return onlinemod.fetch_theintrodb(tmdb_id=idv, season=ep.season,
                                          episode=ep.episode, duration_s=ep.duration)

    def _online_progress(self, i, total, ep):
        self.progress.configure(value=i)
        self.online_status.set(f"{i}/{total}: {ep.path.name}")

    def _online_done(self, ok, miss, err):
        self.progress.configure(value=0)
        self.set_busy(False)
        msg = f"Онлайн: с таймингами {ok}, пусто {miss}, ошибок {err}."
        if self.cancel_event.is_set():
            msg = "Отменено. " + msg
        self.online_status.set(msg)
        self.log_line(msg + " Сравните колонки и при нужде «Взять онлайн».")
        self.refresh_edl_preview()

    def refresh_edl_preview(self):
        if not hasattr(self, "edl_tree"):
            return
        # Доступность отступа конца титров зависит от того, задан ли где-то конец
        # вручную, а это меняется на каждой правке — пересчитываем здесь.
        self._sync_outro_end_spin()
        # Строки пересоздаются целиком, и выделение вместе с ними пропадало —
        # а по нему теперь работают и детект, и запись. Запоминаем выделенные
        # серии по пути и возвращаем выделение на их новые строки.
        selected = {str(self.edl_row_ep[i].path) for i in self.edl_tree.selection()
                    if i in self.edl_row_ep}
        reselect: list[str] = []
        self.edl_tree.delete(*self.edl_tree.get_children())
        self.edl_row_ep = {}
        keep = self.edl_keep_first.get()
        to_end = self.edl_outro_to_end.get()
        pad = self.edl_padding()
        n_intro = n_outro = n_tail = 0
        self._edl_have_files = self._edl_have_online = self._edl_have_chapters = 0
        for e in self.edl_eps:
            if e.online_intro or e.online_outro or e.online_recap:
                self._edl_have_online += 1
            self._edl_have_chapters += bool(e.chapters)
            se = (f"S{e.season:02d}E{e.episode:02d}"
                  if e.season is not None and e.episode is not None else "—")
            intro_eff = edl.apply_padding(edl.effective_intro(e, keep),
                                          pad.intro_start, pad.intro_end, e.duration)
            outro_eff = edl.final_outro(e, pad, to_end)

            recap_txt = f"0:00–{self._fmt_time(e.recap.end)}" if e.recap else "—"

            tags = []
            if keep and edl.is_first_of_season(e) and e.intro is not None:
                intro_txt = "показ (1-я серия)"
                tags.append("first")
            elif intro_eff:
                intro_txt = f"{self._fmt_time(intro_eff.start)}–{self._fmt_time(intro_eff.end)}"
                n_intro += 1
            else:
                intro_txt = "—"

            if outro_eff:
                outro_txt = f"{self._fmt_time(outro_eff.start)}–{self._fmt_time(outro_eff.end)}"
                n_outro += 1
            else:
                outro_txt = "—"

            intro_on_txt = (f"{self._fmt_time(e.online_intro.start)}–{self._fmt_time(e.online_intro.end)}"
                            if e.online_intro else "—")
            outro_on_txt = (f"{self._fmt_time(e.online_outro.start)}–{self._fmt_time(e.online_outro.end)}"
                            if e.online_outro else "—")
            note_txt = "; ".join(x for x in (e.note, e.online_note) if x)

            has = "есть" if edl.has_external_edl(e.path) else ""
            self._edl_have_files += bool(has)
            # Пусто — хвоста нет или его не видно (у файла нет статистики дорожек).
            tail_txt = f"{e.tail:.0f} с" if trim.needs_trim(e.tail) else ""
            n_tail += bool(tail_txt)
            row_tags = tuple(tags) + (("has",) if has else ())
            iid = self.edl_tree.insert("", "end",
                                       values=(e.path.name, se, recap_txt, intro_txt, intro_on_txt,
                                               outro_txt, outro_on_txt, has,
                                               e.chapters or "—", tail_txt, note_txt),
                                       tags=row_tags)
            self.edl_row_ep[iid] = e
            if str(e.path) in selected:
                reselect.append(iid)
        if reselect:
            self.edl_tree.selection_set(reselect)
        self._update_scope_count()
        self.edl_status.set(
            f"Серий: {len(self.edl_eps)}. К пропуску интро: {n_intro}, титры: {n_outro}."
            + (f" Звук дольше видео: {n_tail}." if n_tail else "")
        )
        self._mark_edl_sort()
        self._sync_edl_buttons()

    def detect_edl(self):
        if self.busy:
            return
        scope = self._edl_scope_eps()
        if scope is None:
            return
        missing = edl.missing_tools()
        if missing:
            messagebox.showerror("Нет инструментов для детекта", edl.install_hint(missing))
            return
        # Детект ищет то, что повторяется между сериями, поэтому по выделению он
        # сравнивает серии ТОЛЬКО внутри выделенного. Так и нужно, когда сезон-папка
        # склеена из двух cours с разными заставками: эталон должен быть из своего
        # cours. Но одной серии для сравнения не хватит.
        if len(scope) < 2:
            messagebox.showinfo(
                "Мало серий",
                "Детект сравнивает серии между собой — нужно хотя бы две.\n"
                "Выделите больше строк или переключите «Применять к: ко всем сериям».")
            return
        fpcalc = edl.find_fpcalc()
        ffmpeg = edl.find_ffmpeg()

        # Свежий прогон — свежие заметки: detect_season дописывает через «; »,
        # без сброса текст копился бы между запусками.
        for e in scope:
            e.note = ""

        self._edl_detect_scope = scope
        seasons = edl.group_by_season([e.path for e in scope])
        by_path = {str(e.path): e for e in scope}
        total = len(scope)
        prefer = None if self.edl_track_var.get() == EDL_TRACK_AUTO else self.edl_track_var.get()

        self.set_busy(True, "Определение заставок")
        self._edl_prog = 0
        self._edl_prog_total = total * 2                     # intro + outro
        self.progress.configure(value=0, maximum=self._edl_prog_total)
        win_in, win_out = self._window_val("intro"), self._window_val("outro")
        self.log_line(f"АВТОДЕТЕКТ по {self._scope_word(total)}: сезонов {len(seasons)}, "
                      f"дорожка: {self.edl_track_var.get()}, "
                      f"окно {win_in:.0f}/{win_out:.0f} с…")

        def progress(kind, i, n, path):
            self.root.after(0, self._edl_detect_progress, kind, path)

        # Отпечатки переживают прогон: повторный детект и подбор порогов идут
        # из кэша, не вычитывая гигабайты с диска или сетевой шары заново.
        cache = paths.cache_dir() / "fingerprints"
        self._edl_detect_stats = stats = {}

        def work():
            try:
                edl.prune_cache(cache)
                for season, eps_paths in sorted(seasons.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
                    if self.cancel_event.is_set():
                        break
                    eps = [by_path[str(p)] for p in eps_paths]
                    edl.detect_season(eps, fpcalc, ffmpeg, prefer_lang=prefer, progress=progress,
                                      stop=self.cancel_event.is_set,
                                      intro_window=win_in, outro_window=win_out,
                                      cache_dir=cache, stats=stats)
            except Exception as ex:  # noqa: BLE001
                self.root.after(0, lambda: self._edl_detect_error(ex))
                return
            self.root.after(0, self._edl_detect_done)

        threading.Thread(target=work, daemon=True).start()

    def _edl_detect_progress(self, kind, path):
        self._edl_prog += 1
        self.progress.configure(value=self._edl_prog)
        self.edl_status.set(f"Детект [{EDL_KIND_RU.get(kind, kind)}] "
                            f"{self._edl_prog}/{self._edl_prog_total}: {Path(path).name}")

    def _edl_detect_error(self, ex):
        self.set_busy(False)
        self.progress.configure(value=0)
        messagebox.showerror("Ошибка детекта", str(ex))

    def _edl_detect_done(self):
        self.progress.configure(value=0)
        # Считаем по тому, что гоняли: при детекте по выделению остальные серии
        # не трогались, и мешать их в итог нечестно.
        scope = getattr(self, "_edl_detect_scope", None) or self.edl_eps
        fi = sum(1 for e in scope if e.intro)
        fo = sum(1 for e in scope if e.outro)
        n = len(scope)
        st = getattr(self, "_edl_detect_stats", None) or {}
        cached = (f" Отпечатков из кэша: {st.get('cached', 0)} из {st['total']}."
                  if st.get("total") else "")
        if self.cancel_event.is_set():
            self.log_line(f"Детект отменён. Найдено до отмены: интро {fi}/{n}, титры {fo}/{n}.{cached}")
        else:
            self.log_line(f"Детект готов: интро {fi}/{n}, титры {fo}/{n}."
                          f"{cached} Проверьте таблицу и при нужде поправьте.")
        # Снимок локального детекта — чтобы «Вернуть локальные» восстанавливал именно его,
        # даже если активные значения потом заменили онлайновыми.
        for e in scope:
            e.local_intro, e.local_outro, e.local_recap = e.intro, e.outro, e.recap
        self.set_busy(False)
        self.refresh_edl_preview()

    def write_edl_files(self):
        if self.busy:
            return
        scope = self._edl_scope_eps()
        if scope is None:
            return
        keep = self.edl_keep_first.get()
        to_end = self.edl_outro_to_end.get()
        pad = self.edl_padding()
        to_write = [e for e in scope
                    if edl.apply_padding(edl.effective_intro(e, keep), pad.intro_start, pad.intro_end, e.duration)
                    or edl.final_outro(e, pad, to_end)
                    or e.recap]
        if not to_write:
            messagebox.showinfo("Нечего записывать",
                                "Нет ни интро, ни титров. Сначала «Определить автоматически» или задайте вручную.")
            return
        also_chapters = self.chapters_with_edl.get()
        prepared = self._chapters_plan(scope) if also_chapters else None
        if also_chapters and prepared is None:
            return                       # нет MKVToolNix — про это уже сказали
        # Хвост режется до записи: конец титров в .edl и главах должен лечь на
        # новый конец файла, а не на старый, за концом видео.
        to_trim = [e for e in scope if trim.needs_trim(e.tail)] if self.edl_trim_tail.get() else []
        tools = None
        if to_trim:
            tools, missing = trim.find_tools()
            if tools is None:
                messagebox.showerror(
                    "Нечем обрезать хвост",
                    f"Не найдены: {', '.join(missing)}.\n"
                    + ("Установите ffmpeg и MKVToolNix и добавьте их в PATH."
                       if sys.platform == "win32" else "brew install ffmpeg mkvtoolnix")
                    + "\n\nИли снимите галку «Обрезать хвост после конца видео».")
                return
        extra = ""
        if prepared:
            _, plan, skipped = prepared
            extra = f"\nЗаодно главы в MKV: {len(plan)} файлов."
            if skipped:
                extra += f" Пропущено с чужими главами: {len(skipped)}."
        if to_trim:
            longest = max(e.tail for e in to_trim)
            extra += (f"\n\nСначала обрезать хвост у {len(to_trim)} файлов: звук идёт дальше "
                      f"картинки (до {longest:.0f} с). Без перекодирования, но каждый файл "
                      "переписывается целиком — несколько минут на серию. Оригинал заменяется "
                      "только после сверки с ним.")
        if not messagebox.askyesno(
            "Запись .edl",
            f"Записать {len(to_write)} файлов .edl — по {self._scope_phrase()}?\n"
            "Существующие .edl будут перезаписаны."
            + ("" if to_trim else " Видео не трогается.") + extra,
        ):
            return
        if to_trim:
            # Главы тогда планируются после обрезки: у файлов будет другая длительность.
            self._run_trim(tools, to_trim,
                           then=lambda: self._write_edl_now(scope, pad, keep, to_end, also_chapters))
        else:
            self._write_edl_now(scope, pad, keep, to_end, also_chapters, prepared)

    def _write_edl_now(self, scope, pad, keep, to_end, also_chapters, prepared=None):
        """Пишет .edl по scope и, если просили, запускает запись глав."""
        if also_chapters and prepared is None:
            prepared = self._chapters_plan(scope)
        written = 0
        for e in scope:
            p = edl.build_and_write(e, pad, keep, to_end)
            if p:
                written += 1
                self.log_line(f"  ✓ {p.name}")
        self.log_line(f"Записано .edl: {written}.")
        self._save_edl_settings()
        self.refresh_edl_preview()
        if prepared and prepared[1]:
            self._run_chapters(prepared[0], prepared[1])

    def _run_trim(self, tools, eps, then):
        """Обрезает хвосты в фоне, затем зовёт then() — запись .edl и глав.

        Серия занимает минуты: ffmpeg переписывает весь файл, mkvpropedit ещё раз
        читает его ради статистики. Поэтому прогресс идёт долями внутри файла, а
        после каждого файла он пересканируется — длительность и хвост в таблице
        сразу новые. Отмена оставляет текущий файл как был и .edl не пишет.
        """
        self.set_busy(True, f"Обрезка хвоста 1/{len(eps)}")
        self.progress.configure(value=0, maximum=len(eps) * 100)
        results: "queue.Queue" = queue.Queue()
        tally = {"ok": 0, "failed": 0}
        started = time.monotonic()

        def show(n, frac):
            """Подпись у полосы: какая серия, какой этап, сколько осталось."""
            done = (n + frac) / len(eps)
            stage = "перепаковка" if frac < trim.REMUX_SHARE else "проверка"
            text = f"Обрезка хвоста {n + 1}/{len(eps)} · {stage} · {frac * 100:.0f}%"
            elapsed = time.monotonic() - started
            # Первые секунды оценка скачет — показываем её, когда есть на что опереться.
            if done > 0.02 and elapsed > 20:
                left = elapsed * (1 - done) / done
                text += " · осталось " + (f"~{math.ceil(left / 60)} мин" if left >= 60 else "<1 мин")
            self.progress_text.set(text)
            self.progress.configure(value=done * len(eps) * 100)

        def work():
            for n, ep in enumerate(eps):
                if self.cancel_event.is_set():
                    break
                results.put(("start", n, ep))
                res = trim.trim_file(ep.path, tools,
                                     progress=lambda frac, n=n: results.put(("progress", n, frac)),
                                     cancel=self.cancel_event.is_set)
                fresh = core.scan_file(tools.mkvmerge, ep.path) if res.ok and not res.skipped else None
                results.put(("done", n, ep, res, fresh))
            results.put(None)

        def poll():
            finished = False
            while True:
                try:
                    item = results.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    finished = True
                    continue
                kind, n = item[0], item[1]
                if kind == "start":
                    ep = item[2]
                    self.edl_status.set(f"Обрезка хвоста {n + 1}/{len(eps)}: {ep.path.name}")
                    self.log_line(f"  … {ep.path.name}: хвост {ep.tail:.0f} с, обрезаю")
                    show(n, 0.0)
                elif kind == "progress":
                    show(n, item[2])
                else:
                    _, _, ep, res, fresh = item
                    if res.ok and not res.skipped:
                        tally["ok"] += 1
                        self._take_rescan(ep, fresh)
                        self.log_line(f"  ✓ {ep.path.name}: {res.message}, "
                                      f"{self._fmt_time(res.old_duration)} → "
                                      f"{self._fmt_time(res.new_duration)}")
                    elif res.ok:
                        self.log_line(f"  — {ep.path.name}: {res.message}")
                    elif not res.cancelled:
                        tally["failed"] += 1
                        self.log_line(f"  ✗ {ep.path.name}: {res.message} — оригинал не тронут")
                    show(n, 1.0)
                    self.refresh_edl_preview()
            if not finished:
                self.root.after(150, poll)
                return
            cancelled = self.cancel_event.is_set()
            self.progress.configure(value=0)
            self.set_busy(False)
            self.progress_text.set("Отменено" if cancelled else
                                   f"Готово: хвост обрезан у {tally['ok']} из {len(eps)}")
            self.log_line(f"Хвост обрезан: {tally['ok']}"
                          + (f", не вышло: {tally['failed']}" if tally["failed"] else "") + ".")
            if cancelled:
                self.log_line("Отменено — .edl не записаны.")
                self.refresh_edl_preview()
                return
            then()

        threading.Thread(target=work, daemon=True).start()
        poll()

    def _take_rescan(self, ep, fresh):
        """Обрезанный файл пересканирован: подменяет его в списке и в серии."""
        if fresh is None or fresh.error:
            return
        self.files = [fresh if str(f.path) == str(fresh.path) else f for f in self.files]
        ep.duration = fresh.duration
        ep.chapters = fresh.chapters
        ep.tail = trim.tail_seconds(fresh.tracks)

    # ------------------------------------------------------- главы Matroska --
    def _chapters_plan(self, scope):
        """Что и куда писать: (mkvpropedit, план, пропущенные) или None при отказе.

        Пропущенные — файлы с чужими главами: mkvpropedit заменяет набор целиком,
        и деление фильма на сцены пропало бы безвозвратно. Берём их в работу,
        только когда это разрешено галкой.
        """
        propedit = core.find_tool("mkvpropedit")
        extract = core.find_tool("mkvextract")
        if not (propedit and extract):
            messagebox.showerror(
                "Нет MKVToolNix",
                "Для глав нужны mkvpropedit и mkvextract из MKVToolNix.\n"
                + ("choco install mkvtoolnix" if sys.platform == "win32"
                   else "brew install mkvtoolnix"))
            return None
        keep, to_end, pad = (self.edl_keep_first.get(), self.edl_outro_to_end.get(),
                             self.edl_padding())
        force = self.chapters_force.get()
        plan, skipped = [], []
        for e in scope:
            points = chapters.build_points(e, pad, keep, to_end)
            if not points:
                continue
            # Глав нет — проверять нечего; иначе смотрим, наша ли это разметка.
            state = chapters.state(e.path, extract) if e.chapters else "none"
            if state == "foreign" and not force:
                skipped.append(e)
            else:
                plan.append((e, points))
        return propedit, plan, skipped

    def _run_chapters(self, propedit, plan):
        """Пишет главы в фоне: mkvpropedit лезет в сам файл, а тот бывает на шаре.

        Результаты едут через очередь, а разбирает их главный поток по таймеру:
        tkinter из рабочего потока трогать нельзя даже через after.
        """
        self.set_busy(True, "Запись глав")
        self.progress.configure(value=0, maximum=len(plan))
        results: "queue.Queue" = queue.Queue()
        tally = {"ok": 0, "seen": 0}

        def work():
            for ep, points in plan:
                if self.cancel_event.is_set():
                    break
                results.put((ep, points, chapters.write_chapters(ep.path, points, propedit)))
            results.put(None)

        def poll():
            finished = False
            while True:
                try:
                    item = results.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    finished = True
                    continue
                ep, points, ok = item
                tally["seen"] += 1
                if ok:
                    ep.chapters = len(points)
                    tally["ok"] += 1
                    self.log_line(f"  ✓ {ep.path.name}: глав {len(points)}")
                else:
                    self.log_line(f"  ✗ {ep.path.name}: записать не удалось")
                self.progress.configure(value=tally["seen"])
            if finished:
                self.progress.configure(value=0)
                self.set_busy(False)
                self.log_line(f"Главы записаны: {tally['ok']}.")
                self.refresh_edl_preview()
                return
            self.root.after(120, poll)

        threading.Thread(target=work, daemon=True).start()
        poll()

    def write_chapters(self):
        if self.busy:
            return
        scope = self._edl_scope_eps()
        if scope is None:
            return
        prepared = self._chapters_plan(scope)
        if prepared is None:
            return
        propedit, plan, skipped = prepared
        if not plan:
            messagebox.showinfo(
                "Нечего размечать",
                "Нет ни интро, ни титров, ни recap — главы строить не из чего."
                if not skipped else
                f"У всех серий ({len(skipped)}) свои главы, они не тронуты.\n"
                "Чтобы заменить их, включите «перезаписывать чужие главы».")
            return
        tail = (f"\nПропущено с чужими главами: {len(skipped)}." if skipped else "")
        if not messagebox.askyesno(
            "Запись глав",
            f"Записать главы в {len(plan)} файлов — по {self._scope_phrase()}?\n"
            "Правится только заголовок MKV, видео не перекодируется." + tail,
        ):
            return
        self._run_chapters(propedit, plan)

    def clear_chapters(self):
        """Убирает ТОЛЬКО свою разметку: чужие главы не наши, чтобы их удалять."""
        if self.busy:
            return
        scope = self._edl_scope_eps()
        if scope is None:
            return
        propedit = core.find_tool("mkvpropedit")
        extract = core.find_tool("mkvextract")
        if not (propedit and extract):
            messagebox.showerror("Нет MKVToolNix", "Нужны mkvpropedit и mkvextract.")
            return
        ours = [e for e in scope if e.chapters and chapters.state(e.path, extract) == "ours"]
        if not ours:
            messagebox.showinfo("Нет наших глав",
                                "Среди этих серий нет файлов с нашей разметкой. "
                                "Чужие главы кнопка не трогает.")
            return
        if not messagebox.askyesno("Удаление глав",
                                   f"Убрать главы из {len(ours)} файлов?"):
            return
        gone = 0
        for e in ours:
            if chapters.clear_chapters(e.path, propedit):
                e.chapters = 0
                gone += 1
        self.log_line(f"Главы убраны: {gone}.")
        self.refresh_edl_preview()

    def delete_edl_files(self):
        if self.busy:
            return
        scope = self._edl_scope_eps()
        if scope is None:
            return
        existing = [e for e in scope if edl.has_external_edl(e.path)]
        if not existing:
            messagebox.showinfo("Нет .edl", f"Рядом с этими сериями ({self._scope_phrase()}) "
                                            "нет .edl файлов.")
            return
        if not messagebox.askyesno("Удаление .edl",
                                   f"Удалить {len(existing)} файлов .edl — по {self._scope_phrase()}?"):
            return
        n = sum(1 for e in existing if edl.delete_edl(e.path))
        self.log_line(f"Удалено .edl: {n}.")
        self.refresh_edl_preview()

    def _edl_edit_row(self, event):
        if self.busy:
            return            # правка посреди записи легла бы в .edl вперемешку
        iid = self.edl_tree.identify_row(event.y)
        if not iid or iid not in self.edl_row_ep:
            return
        self._edl_edit_dialog(self.edl_row_ep[iid])

    # Границы, которые можно проверить кадром: подпись → (поле диалога, отступ сезона).
    # У recap отступа нет, поэтому и сдвигать сезон по нему нечем.
    _EDL_BOUNDS = {
        "конец интро": ("ie", "intro_end"),
        "начало интро": ("is", "intro_start"),
        "начало титров": ("os", "outro_start"),
        # Пусто, пока титры идут до конца файла. Для сцены после титров это как раз
        # та граница, которую глазами и ищут: где титры кончились, а сцена началась.
        "конец титров": ("oe", "outro_end"),
        "конец recap": ("rc", None),
    }
    _FRAME_COLS = 4

    def _build_frames_block(self, parent, win, e: "edl.EpisodeEdl", varmap):
        """Полоса кадров вокруг границы — увидеть глазами, куда она попала.

        По числу «конец интро 0:29» не понять, идёт там ещё заставка, затемнение
        или уже серия. Кадры отвечают сразу, а найденную поправку переносим на
        весь сезон отступом: детект ошибается у всех серий одинаково, и лечить
        это по одной серии бессмысленно.

        Полоса строится вокруг ДЕЙСТВУЮЩЕЙ границы (значение серии плюс отступ
        сезона) — именно её и пропустит Kodi. Поэтому «применить к этой серии»
        вычитает отступ обратно, а «сдвинуть сезон» правит сам отступ.
        """
        box = ttk.LabelFrame(parent, text="Проверка кадром", padding=8)
        ffmpeg = edl.find_ffmpeg()
        if not ffmpeg:
            ttk.Label(box, text="⚠ " + edl.install_hint(["ffmpeg"])).pack(anchor="w")
            return box

        top = ttk.Frame(box)
        top.pack(fill="x")
        ttk.Label(top, text="Граница:").pack(side="left")
        bound_var = tk.StringVar(value="конец интро")
        bound_combo = ttk.Combobox(top, state="readonly", width=15, textvariable=bound_var,
                                   values=list(self._EDL_BOUNDS))
        bound_combo.pack(side="left", padx=(6, 12))
        ttk.Label(top, text="шаг:").pack(side="left")
        step_var = tk.StringVar(value=f"{edl.FRAME_STEP:.0f}")
        ttk.Combobox(top, state="readonly", width=3, textvariable=step_var,
                     values=["1", "2", "5", "10"]).pack(side="left", padx=(4, 12))
        show_btn = ttk.Button(top, text="Показать кадры")
        show_btn.pack(side="left")
        status = tk.StringVar(value="")
        ttk.Label(top, textvariable=status, **row_colors(top)["nochange"]).pack(side="left", padx=10)

        strip = ttk.Frame(box)
        strip.pack(fill="x", pady=(8, 0))
        # Пустая картинка нужного размера держит сетку: без неё width/height у Label
        # считаются в символах, и окно раздувается до тысяч точек.
        blank = tk.PhotoImage(width=edl.FRAME_WIDTH, height=edl.FRAME_HEIGHT, master=parent)
        cells, stamps = [], []
        for i in range(edl.FRAME_COUNT):
            cell = ttk.Frame(strip)
            cell.grid(row=i // self._FRAME_COLS, column=i % self._FRAME_COLS, padx=2, pady=2)
            # tk.Label, а не ttk: только у него есть рамка выделения (highlight*).
            img = tk.Label(cell, image=blank, borderwidth=0, highlightthickness=2,
                           highlightbackground=theme.widget_bg(cell), cursor=theme.HAND)
            img.pack()
            stamp = ttk.Label(cell, text="—", anchor="center")
            stamp.pack(fill="x")
            cells.append(img)
            stamps.append(stamp)

        pick_var = tk.StringVar(value="Нажмите «Показать кадры».")
        ttk.Label(box, textvariable=pick_var).pack(anchor="w", pady=(6, 0))
        act = ttk.Frame(box)
        act.pack(anchor="w", pady=(4, 0))
        apply_one = ttk.Button(act, text="Применить к этой серии", state="disabled")
        apply_one.pack(side="left")
        # В подписи всегда стоит величина сдвига: без неё непонятно, ставится
        # ли всем одно время или каждая серия двигается на эту разницу.
        apply_all = ttk.Button(act, text="Сдвинуть все серии", state="disabled")
        apply_all.pack(side="left", padx=8)
        player = edl.find_player()
        open_btn = ttk.Button(act, text=f"Открыть в {player[0]}" if player else "Плеер не найден",
                              state="normal" if player else "disabled")
        open_btn.pack(side="left")

        accent = theme._rgb_to_hex(theme.accent_rgb(box))
        plain = theme.widget_bg(strip)
        # Ссылки на картинки держим сами: Tk их не удерживает, и кадры пропадут.
        state = {"imgs": [None] * edl.FRAME_COUNT, "times": [], "pick": None,
                 "center": None, "run": 0, "blank": blank}
        win.bind("<Destroy>", lambda ev: state.update(run=state["run"] + 1), add="+")

        def pad_of(bound: str) -> float:
            key = self._EDL_BOUNDS[bound][1]
            return self._pad_val(key) if key else 0.0

        def current_effective(bound: str):
            """Действующее значение границы: из поля диалога плюс отступ сезона."""
            raw = self._parse_time(varmap[self._EDL_BOUNDS[bound][0]].get())
            return None if raw is None else raw + pad_of(bound)

        def choose(i):
            if i >= len(state["times"]) or state["imgs"][i] is None:
                return
            state["pick"] = state["times"][i]
            for k, c in enumerate(cells):
                c.configure(highlightbackground=accent if k == i else plain)
            delta = state["pick"] - state["center"]
            pick_var.set(f"Сейчас {self._fmt_time(state['center'])} → выбрано "
                         f"{self._fmt_time(state['pick'])} (сдвиг {delta:+.0f} с)")
            apply_one.configure(state="normal")
            apply_all.configure(
                text=f"Сдвинуть все серии на {delta:+.0f} с",
                state="disabled" if self._EDL_BOUNDS[bound_var.get()][1] is None else "normal")

        for i, c in enumerate(cells):
            c.bind("<Button-1>", lambda ev, i=i: choose(i))

        def show():
            bound = bound_var.get()
            center = current_effective(bound)
            if center is None:
                messagebox.showinfo("Нет границы",
                                    f"У этой серии не задана граница «{bound}» — "
                                    "показывать нечего вокруг пустого значения.")
                return
            state.update(center=center, pick=None, run=state["run"] + 1)
            run = state["run"]
            state["times"] = edl.frame_times(center, e.duration, step=float(step_var.get()))
            for c, s in zip(cells, stamps):
                c.configure(image=blank, highlightbackground=plain)
                s.configure(text="…")
            state["imgs"] = [None] * edl.FRAME_COUNT
            apply_one.configure(state="disabled")
            apply_all.configure(state="disabled")
            pick_var.set(f"Действующая граница: {self._fmt_time(center)}. "
                         "Кликните кадр, на котором граница должна быть.")
            status.set(f"читаю кадры 0/{len(state['times'])}…")
            show_btn.configure(state="disabled")

            # Готовые кадры кладём в очередь, а рисует их главный поток по таймеру:
            # трогать Tk из рабочего потока нельзя, даже через after.
            ready_q: "queue.Queue" = queue.Queue()

            def work():
                edl.grab_frames(ffmpeg, e.path, state["times"],
                                on_frame=lambda i, png: ready_q.put((i, png)),
                                stop=lambda: run != state["run"])
                ready_q.put(None)

            def poll():
                if run != state["run"] or not win.winfo_exists():
                    return
                finished = False
                while True:
                    try:
                        item = ready_q.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        finished = True
                        continue
                    i, png = item
                    stamp = self._fmt_time(state["times"][i])
                    if png:
                        img = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"), master=win)
                        state["imgs"][i] = img          # держим ссылку: иначе кадр пропадёт
                        cells[i].configure(image=img)
                        stamps[i].configure(text=stamp)
                    else:
                        stamps[i].configure(text=f"{stamp} — нет кадра")
                got = sum(1 for x in state["imgs"] if x is not None)
                if finished:
                    status.set("")
                    show_btn.configure(state="normal")
                    return
                status.set(f"читаю кадры {got}/{len(state['times'])}…")
                win.after(120, poll)

            threading.Thread(target=work, daemon=True).start()
            poll()

        show_btn.configure(command=show)

        def to_episode():
            bound = bound_var.get()
            field = self._EDL_BOUNDS[bound][0]
            # В поле диалога живёт «сырое» значение, отступ сезона ляжет на него сверху.
            varmap[field].set(self._fmt_time(state["pick"] - pad_of(bound)))
            pick_var.set(f"Граница «{bound}» этой серии — {self._fmt_time(state['pick'])}. "
                         "Не забудьте «Сохранить».")

        def to_season():
            bound = bound_var.get()
            key = self._EDL_BOUNDS[bound][1]
            delta = state["pick"] - state["center"]
            new = self._pad_val(key) + delta
            self.edl_pad[key].set(f"{new:.0f}")
            self.refresh_edl_preview()
            self.log_line(f"Отступ «{bound}» сдвинут на {delta:+.0f} с (теперь {new:+.0f} с) — "
                          f"по кадрам серии «{e.path.name}».")
            pick_var.set(f"Отступ «{bound}» теперь {new:+.0f} с — применён ко всем сериям.")

        def in_player():
            """Открывает серию на выбранном кадре, а без выбора — на самой границе."""
            bound = bound_var.get()
            at = state["pick"] if state["pick"] is not None else current_effective(bound)
            if at is None:
                messagebox.showinfo("Нет границы",
                                    f"У этой серии не задана граница «{bound}» — "
                                    "открывать не на чем.")
                return
            name = edl.open_in_player(e.path, at)
            if name:
                pick_var.set(f"Открыто в {name} на {self._fmt_time(at)}.")

        def on_bound_change(_event=None):
            """Границу сменили — старые кадры к ней не относятся, гасим их."""
            state.update(pick=None, center=None, times=[])
            state["imgs"] = [None] * edl.FRAME_COUNT
            for c, s in zip(cells, stamps):
                c.configure(image=blank, highlightbackground=plain)
                s.configure(text="—")
            apply_one.configure(state="disabled")
            apply_all.configure(text="Сдвинуть все серии", state="disabled")
            pick_var.set("Нажмите «Показать кадры».")

        bound_combo.bind("<<ComboboxSelected>>", on_bound_change)
        apply_one.configure(command=to_episode)
        apply_all.configure(command=to_season)
        open_btn.configure(command=in_player)
        return box

    def _edl_edit_dialog(self, e: "edl.EpisodeEdl"):
        win = tk.Toplevel(self.root)
        win.title(f"Правка: {e.path.name}")
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(
            frm,
            text="Время: MM:SS, H:MM:SS или секунды. Пусто — сегмента нет.\n"
                 "Recap идёт с начала файла. Конец титров пуст — они идут до конца\n"
                 "файла; задайте его, если после титров есть сцена.\n"
                 "Конец интро можно не заполнять, если в блоке «Задать всем» указана\n"
                 "длительность интро — тогда конец = начало + длительность.\n"
                 "Отступы сезона применяются к интро/титрам поверх этих значений.",
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        rows = [
            ("Recap до (с начала, пусто = нет):", "rc", e.recap.end if e.recap else None),
            ("Интро начало:", "is", e.intro.start if e.intro else None),
            ("Интро конец:", "ie", e.intro.end if e.intro else None),
            ("Титры начало:", "os", e.outro.start if e.outro else None),
            ("Титры конец (пусто = до конца файла):", "oe",
             e.outro.end if (e.outro and e.outro_fixed_end) else None),
        ]
        varmap = {}
        # Продолжительность сегмента рядом с полями: начало не всегда нулевое, и
        # считать «сколько же это выходит» в уме при каждой правке — лишняя работа.
        lengths = {}
        for i, (lab, key, val) in enumerate(rows, start=1):
            ttk.Label(frm, text=lab).grid(row=i, column=0, sticky="e", pady=2, padx=(0, 6))
            v = tk.StringVar(value=self._fmt_time(val) if val is not None else "")
            varmap[key] = v
            ttk.Entry(frm, textvariable=v, width=12).grid(row=i, column=1, sticky="w", pady=2)
            if key in ("rc", "ie", "oe"):      # строки, на которых сегмент замыкается
                lengths[key] = tk.StringVar(value="")
                ttk.Label(frm, textvariable=lengths[key],
                          **row_colors(frm)["nochange"]).grid(
                    row=i, column=2, sticky="w", pady=2, padx=(10, 0))

        def show_lengths(*_):
            """Пересчитывает длительности на каждый ввод в любом из полей."""
            def put(var, length, note=""):
                var.set(f"длительность {self._fmt_time(length)} = {length:.0f} сек{note}"
                        if length is not None and length > 0 else "")

            rc = self._parse_time(varmap["rc"].get())
            put(lengths["rc"], rc)             # recap идёт от нуля: конец и есть длина
            start = self._parse_time(varmap["is"].get())
            end = self._parse_time(varmap["ie"].get())
            put(lengths["ie"], (end - start) if (start is not None and end is not None) else None)
            outro = self._parse_time(varmap["os"].get())
            outro_end = self._parse_time(varmap["oe"].get())
            end = outro_end if outro_end is not None else e.duration
            put(lengths["oe"], (end - outro) if (outro is not None and end) else None,
                "" if outro_end is not None else " (до конца файла)")

        for var in varmap.values():
            var.trace_add("write", show_lengths)
        show_lengths()

        # Явное удаление сегмента: обнуляет поля, а save() пустое поле трактует как «нет».
        # Нужно, когда детект нашёл лишнее (напр. титров нет — мультсериал идёт до конца):
        # ввод 0 давал бы сегмент «весь эпизод», а эти кнопки убирают его начисто.
        clear_frm = ttk.Frame(frm)
        clear_frm.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(clear_frm, text="Убрать:").pack(side="left")
        ttk.Button(clear_frm, text="интро", width=7,
                   command=lambda: (varmap["is"].set(""), varmap["ie"].set(""))).pack(side="left", padx=2)
        ttk.Button(clear_frm, text="титры", width=7,
                   command=lambda: (varmap["os"].set(""), varmap["oe"].set(""))).pack(side="left", padx=2)
        ttk.Button(clear_frm, text="recap", width=7,
                   command=lambda: varmap["rc"].set("")).pack(side="left", padx=2)

        self._build_frames_block(frm, win, e, varmap).grid(
            row=len(rows) + 2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=len(rows) + 3, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def save():
            rc = self._parse_time(varmap["rc"].get())
            is_, ie = self._parse_time(varmap["is"].get()), self._parse_time(varmap["ie"].get())
            os_ = self._parse_time(varmap["os"].get())
            e.recap = edl.Segment(0.0, rc) if (rc is not None and rc > 0) else None
            if is_ is not None and ie is None:
                # Конец не задан — берём «начало + длительность интро» из блока «Задать всем».
                dur = self._parse_time(self.edl_manual["intro_dur"].get())
                if dur and dur > 0:
                    ie = is_ + dur
            e.intro = edl.Segment(is_, ie) if (is_ is not None and ie is not None and ie > is_) else None
            oe = self._parse_time(varmap["oe"].get())
            if os_ is not None and oe is not None and oe > os_:
                # Конец указан руками — он главнее галки «титры до конца файла».
                e.outro, e.outro_fixed_end = edl.Segment(os_, oe), True
            elif os_ is not None:
                end = e.duration or (e.outro.end if e.outro else os_ + 60)
                e.outro = edl.Segment(os_, end) if end > os_ else None
                e.outro_fixed_end = False
            else:
                e.outro = None
                e.outro_fixed_end = False
            win.destroy()
            self.refresh_edl_preview()

        ttk.Button(btns, text="Сохранить", command=save).pack(side="right")
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="right", padx=6)

        # Центрируем окно на экране (иначе Toplevel появляется в левом верхнем углу).
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")
        win.grab_set()


def main():
    # Приоритет: последний путь (если он есть и папка доступна) → аргумент запуска
    # (ярлык подставляет дефолтный путь) → пусто.
    last = load_app_settings().get("last_path", "")
    if last and Path(last).is_dir():
        start = last
    elif len(sys.argv) > 1:
        start = sys.argv[1]
    else:
        start = ""
    root = tk.Tk()
    App(root, start)
    root.mainloop()


if __name__ == "__main__":
    main()
