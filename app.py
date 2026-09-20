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

import json
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import core
import edl
import metaui
import theme
from theme import is_dark_theme, row_colors  # noqa: F401 — is_dark_theme держим в API модуля

NOTOUCH = "— не трогать —"
SUBOFF = "— выключить субтитры —"
EDL_TRACK_AUTO = "Оригинал (авто)"

# Локальные настройки приложения (последний путь и т.п.), рядом с программой.
_SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"


def load_app_settings() -> dict:
    try:
        return json.loads(_SETTINGS_FILE.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — нет файла/битый JSON: просто пустые настройки
        return {}


def save_app_settings(data: dict) -> None:
    try:
        _SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
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
        # Кнопки вкладок, которые тоже надо гасить на время длинных операций.
        # Вкладка «Медиатека» дописывает сюда свои при построении.
        self.extra_busy_buttons: list = []

        root.title("Медиатека Kodi — метаданные, дорожки, пропуск заставок")
        self._set_window_icon()
        # Позицию задаём явно, не только размер: при запуске из Dock на macOS окно
        # без координат уезжает в левый нижний угол. По вертикали ставим чуть выше
        # центра — иначе окно выглядит утопленным под строкой меню.
        w, h = 1080, 760
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(40, (sh - h) // 3)}")
        root.minsize(900, 640)

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

        self._enable_entry_clipboard()
        self._check_tools()
        # Путь (последний/аргумент) только подставляется в поле — сканирование вручную
        # кнопкой «Сканировать»: если папка уже обрабатывалась, повторный скан не нужен.

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
        f.pack(fill="x")
        ttk.Label(f, text="Прогресс:").pack(side="left")
        self.progress = ttk.Progressbar(f, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        # Рабочие потоки проверяют cancel_event между файлами и мягко прерываются.
        self.cancel_btn = ttk.Button(f, text="Отмена", state="disabled",
                                     command=self.cancel_event.set)
        self.cancel_btn.pack(side="left")

        self.log = ScrolledText(self.root, height=7, state="disabled", wrap="word")
        self.log.pack(fill="x", padx=10, pady=(0, 10))

    # -------------------------------------------------------- вкладка EDL --
    def _build_edl(self, parent):
        opt = ttk.LabelFrame(parent, text="Настройки пропуска", padding=10)
        opt.pack(fill="x", padx=10, pady=(8, 4))

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

        # Отступы (padding) поверх автодетекта, на весь сезон. + позже / − раньше.
        # Конец титров не регулируем — он всегда до конца файла.
        self.edl_pad = {k: tk.StringVar(value="0") for k in
                        ("intro_start", "intro_end", "outro_start")}

        def spin(row, col, label, key):
            ttk.Label(opt, text=label).grid(row=row, column=col, sticky="e", padx=(12, 2), pady=(6, 0))
            sb = ttk.Spinbox(opt, from_=-120, to=120, increment=1, width=5,
                             textvariable=self.edl_pad[key], command=self.refresh_edl_preview)
            sb.grid(row=row, column=col + 1, sticky="w", pady=(6, 0))
            sb.bind("<KeyRelease>", lambda e: self.refresh_edl_preview())

        ttk.Label(opt, text="Отступы (сек), + позже / − раньше:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        spin(2, 1, "интро нач:", "intro_start")
        spin(2, 3, "интро кон:", "intro_end")
        spin(3, 1, "титры нач:", "outro_start")

        # --- Задать вручную (когда детект промахнулся или его нет) ---
        # В некоторых сериалах интро/титры одинаковы по времени во всех сериях, но
        # детект их не берёт (нет чёткой музыкальной темы) — задаём одним значением.
        # Переключатель scope позволяет применить не ко всем, а к выделенным строкам
        # (Shift/Ctrl в таблице) — напр. когда один сезон-папка склеен из двух cours
        # с разными таймингами.
        manual = ttk.LabelFrame(parent, text="Задать вручную (время: MM:SS или секунды)", padding=10)
        manual.pack(fill="x", padx=10, pady=(0, 4))
        self.edl_manual = {k: tk.StringVar(value="")
                           for k in ("intro_start", "intro_end", "intro_dur",
                                     "outro_start", "outro_last", "recap_end")}
        self.edl_scope = tk.StringVar(value="all")

        scope_row = ttk.Frame(manual)
        scope_row.grid(row=0, column=0, columnspan=7, sticky="w", pady=(0, 4))
        ttk.Label(scope_row, text="Применять к:").pack(side="left")
        ttk.Radiobutton(scope_row, text="ко всем сериям", value="all",
                        variable=self.edl_scope).pack(side="left", padx=(6, 0))
        self.edl_scope_sel_rb = ttk.Radiobutton(scope_row, text="к выделенным (0)", value="sel",
                                                 variable=self.edl_scope)
        self.edl_scope_sel_rb.pack(side="left", padx=(6, 0))

        ttk.Label(manual, text="Интро:").grid(row=1, column=0, sticky="e")
        ttk.Label(manual, text="нач").grid(row=1, column=1, sticky="e", padx=(8, 2))
        ttk.Entry(manual, textvariable=self.edl_manual["intro_start"], width=8).grid(row=1, column=2)
        ttk.Label(manual, text="кон").grid(row=1, column=3, sticky="e", padx=(8, 2))
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

        ttk.Label(manual, text="Титры:").grid(row=3, column=0, sticky="e", pady=(6, 0))
        ttk.Label(manual, text="нач").grid(row=3, column=1, sticky="e", padx=(8, 2), pady=(6, 0))
        ttk.Entry(manual, textvariable=self.edl_manual["outro_start"], width=8).grid(row=3, column=2, pady=(6, 0))
        ttk.Label(manual, text="(до конца файла)").grid(row=3, column=3, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(manual, text="Задать", command=self.apply_outro_all).grid(row=3, column=5, padx=(10, 0), pady=(6, 0))

        # Титры «последние N сек от конца» — устойчиво к разной длине серий (титры обычно
        # фиксированной длительности), для каждой серии начало = длительность − N.
        ttk.Label(manual, text="…или последние").grid(row=4, column=1, sticky="e", padx=(8, 2), pady=(6, 0))
        ttk.Entry(manual, textvariable=self.edl_manual["outro_last"], width=8).grid(row=4, column=2, pady=(6, 0))
        ttk.Label(manual, text="сек — продолжительность титров (от конца файла)").grid(
            row=4, column=3, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(manual, text="Задать", command=self.apply_outro_last_all).grid(row=4, column=5, padx=(10, 0), pady=(6, 0))

        ttk.Label(manual, text="Recap:").grid(row=5, column=0, sticky="e", pady=(6, 0))
        ttk.Label(manual, text="до").grid(row=5, column=1, sticky="e", padx=(8, 2), pady=(6, 0))
        ttk.Entry(manual, textvariable=self.edl_manual["recap_end"], width=8).grid(row=5, column=2, pady=(6, 0))
        ttk.Label(manual, text="(с начала файла)").grid(row=5, column=3, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(manual, text="Задать", command=self.apply_recap_all).grid(row=5, column=5, padx=(10, 0), pady=(6, 0))

        # Прицельное удаление по сегменту (уважает scope): напр. снять только титры
        # у выделенного последнего сезона, где их нет, сохранив интро.
        ttk.Label(manual, text="Убрать:").grid(row=6, column=0, sticky="e", pady=(8, 0))
        clr = ttk.Frame(manual)
        clr.grid(row=6, column=1, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Button(clr, text="интро", command=lambda: self.clear_segment_scope("intro")).pack(side="left", padx=(0, 4))
        ttk.Button(clr, text="титры", command=lambda: self.clear_segment_scope("outro")).pack(side="left", padx=4)
        ttk.Button(clr, text="recap", command=lambda: self.clear_segment_scope("recap")).pack(side="left", padx=4)
        ttk.Button(clr, text="всё", command=self.clear_all_segments).pack(side="left", padx=4)

        btns = ttk.Frame(parent, padding=(10, 4))
        btns.pack(fill="x")
        self.edl_detect_btn = ttk.Button(btns, text="Определить автоматически", command=self.detect_edl)
        self.edl_detect_btn.pack(side="left")
        self.edl_write_btn = ttk.Button(btns, text="Записать .edl", command=self.write_edl_files)
        self.edl_write_btn.pack(side="left", padx=8)
        self.edl_delete_btn = ttk.Button(btns, text="Удалить .edl", command=self.delete_edl_files)
        self.edl_delete_btn.pack(side="left")
        ttk.Label(btns, text="  (двойной клик по строке — правка вручную)").pack(side="left", padx=10)

        # Онлайн-тайминги: подтягиваем готовые интро/титры из баз, показываем рядом с
        # локальными (отдельные колонки) и переносим в активные по кнопке — с учётом scope.
        online_f = ttk.LabelFrame(parent, text="Онлайн-тайминги (AniSkip / TheIntroDB)", padding=8)
        online_f.pack(fill="x", padx=10, pady=(4, 0))
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
        cols = ("file", "se", "recap", "intro", "intro_on", "outro", "outro_on", "edl", "note")
        heads = {"file": "Файл", "se": "S/E", "recap": "Recap",
                 "intro": "Интро (актив.)", "intro_on": "Интро (онлайн)",
                 "outro": "Титры (актив.)", "outro_on": "Титры (онлайн)",
                 "edl": ".edl", "note": "Заметка"}
        widths = {"file": 200, "se": 52, "recap": 74, "intro": 120, "intro_on": 120,
                  "outro": 120, "outro_on": 120, "edl": 44, "note": 130}
        self.edl_tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="extended")
        for c in cols:
            self.edl_tree.heading(c, text=heads[c])
            self.edl_tree.column(c, width=widths[c], anchor="w")
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
        ttk.Label(parent, textvariable=self.edl_status, padding=(10, 4)).pack(fill="x")

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

    def set_busy(self, busy: bool):
        self.busy = busy
        if busy:
            self.cancel_event.clear()
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        state = "disabled" if busy else "normal"
        for b in (self.scan_btn, self.apply_btn, self.subs_btn,
                  getattr(self, "edl_detect_btn", None),
                  getattr(self, "edl_write_btn", None),
                  getattr(self, "edl_delete_btn", None),
                  getattr(self, "online_load_btn", None),
                  getattr(self, "online_take_on_btn", None),
                  getattr(self, "online_take_loc_btn", None),
                  *self.extra_busy_buttons):
            if b is not None:
                b.configure(state=state)

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
        if not folder or not Path(folder).is_dir():
            messagebox.showerror("Ошибка", "Укажите существующую папку.")
            return
        save_app_settings({**load_app_settings(), "last_path": folder})
        try:
            core.find_tools()
        except FileNotFoundError as e:
            messagebox.showerror("MKVToolNix не найден", str(e))
            return

        self.set_busy(True)
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

        self.set_busy(True)
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

        self.set_busy(True)
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

    def edl_padding(self) -> edl.Padding:
        return edl.Padding(
            self._pad_val("intro_start"), self._pad_val("intro_end"),
            self._pad_val("outro_start"), 0.0,   # конец титров не регулируем (до конца файла)
        )

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
            old = prev.get(str(f.path))
            if old:
                e.intro, e.outro, e.recap, e.note = old.intro, old.outro, old.recap, old.note
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
            eps.append(e)
        self.edl_eps = eps

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
            messagebox.showinfo("Титры", "Укажите начало титров (например 20:30) — конец берётся до конца файла.")
            return
        for ep in eps:
            end = ep.duration or (s + 60)
            ep.outro = edl.Segment(s, end) if end > s else None
        self.refresh_edl_preview()
        self.log_line(f"Титры заданы {self._scope_word(len(eps))}: с {self._fmt_time(s)} до конца файла.")

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

    def clear_segment_scope(self, kind: str):
        """Убирает один сегмент (intro/outro/recap) у серий по scope."""
        eps = self._edl_scope_eps()
        if eps is None:
            return
        label = {"intro": "интро", "outro": "титры", "recap": "recap"}[kind]
        for ep in eps:
            setattr(ep, kind, None)
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
                ep.outro = ep.online_outro; got = True
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
                ep.outro = ep.local_outro; got = True
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

        self.set_busy(True)
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
        self.edl_tree.delete(*self.edl_tree.get_children())
        self.edl_row_ep = {}
        keep = self.edl_keep_first.get()
        pad = self.edl_padding()
        n_intro = n_outro = 0
        for e in self.edl_eps:
            se = (f"S{e.season:02d}E{e.episode:02d}"
                  if e.season is not None and e.episode is not None else "—")
            intro_eff = edl.apply_padding(edl.effective_intro(e, keep),
                                          pad.intro_start, pad.intro_end, e.duration)
            outro_eff = edl.apply_padding(e.outro, pad.outro_start, pad.outro_end, e.duration)
            if outro_eff and e.duration:          # титры всегда до конца файла
                outro_eff = edl.Segment(outro_eff.start, e.duration)

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
            row_tags = tuple(tags) + (("has",) if has else ())
            iid = self.edl_tree.insert("", "end",
                                       values=(e.path.name, se, recap_txt, intro_txt, intro_on_txt,
                                               outro_txt, outro_on_txt, has, note_txt),
                                       tags=row_tags)
            self.edl_row_ep[iid] = e
        self.edl_status.set(
            f"Серий: {len(self.edl_eps)}. К пропуску интро: {n_intro}, титры: {n_outro}."
        )

    def detect_edl(self):
        if self.busy:
            return
        if not self.edl_eps:
            messagebox.showinfo("Нет данных", "Сначала просканируйте папку.")
            return
        fpcalc = edl.find_fpcalc()
        ffmpeg = edl.find_ffmpeg()
        if not fpcalc:
            messagebox.showerror("Нет fpcalc", "Не найден fpcalc (Chromaprint). Ожидается в assets/fpcalc.exe.")
            return
        if not ffmpeg:
            messagebox.showerror("Нет ffmpeg", "Не найден ffmpeg в PATH.")
            return

        # Свежий прогон — свежие заметки: detect_season дописывает через «; »,
        # без сброса текст копился бы между запусками.
        for e in self.edl_eps:
            e.note = ""

        seasons = edl.group_by_season([e.path for e in self.edl_eps])
        by_path = {str(e.path): e for e in self.edl_eps}
        total = len(self.edl_eps)
        prefer = None if self.edl_track_var.get() == EDL_TRACK_AUTO else self.edl_track_var.get()

        self.set_busy(True)
        self._edl_prog = 0
        self.progress.configure(value=0, maximum=total * 2)  # intro + outro
        self.log_line(f"АВТОДЕТЕКТ: {total} серий, сезонов {len(seasons)}, дорожка: {self.edl_track_var.get()}…")

        def progress(kind, i, n, path):
            self.root.after(0, self._edl_detect_progress, kind, path)

        def work():
            try:
                for season, paths in sorted(seasons.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
                    if self.cancel_event.is_set():
                        break
                    eps = [by_path[str(p)] for p in paths]
                    edl.detect_season(eps, fpcalc, ffmpeg, prefer_lang=prefer, progress=progress,
                                      stop=self.cancel_event.is_set)
            except Exception as ex:  # noqa: BLE001
                self.root.after(0, lambda: self._edl_detect_error(ex))
                return
            self.root.after(0, self._edl_detect_done)

        threading.Thread(target=work, daemon=True).start()

    def _edl_detect_progress(self, kind, path):
        self._edl_prog += 1
        self.progress.configure(value=self._edl_prog)
        self.edl_status.set(f"Детект [{kind}] {self._edl_prog}: {Path(path).name}")

    def _edl_detect_error(self, ex):
        self.set_busy(False)
        self.progress.configure(value=0)
        messagebox.showerror("Ошибка детекта", str(ex))

    def _edl_detect_done(self):
        self.progress.configure(value=0)
        fi = sum(1 for e in self.edl_eps if e.intro)
        fo = sum(1 for e in self.edl_eps if e.outro)
        n = len(self.edl_eps)
        if self.cancel_event.is_set():
            self.log_line(f"Детект отменён. Найдено до отмены: интро {fi}/{n}, титры {fo}/{n}.")
        else:
            self.log_line(f"Детект готов: интро {fi}/{n}, титры {fo}/{n}. Проверьте таблицу и при нужде поправьте.")
        # Снимок локального детекта — чтобы «Вернуть локальные» восстанавливал именно его,
        # даже если активные значения потом заменили онлайновыми.
        for e in self.edl_eps:
            e.local_intro, e.local_outro, e.local_recap = e.intro, e.outro, e.recap
        self.set_busy(False)
        self.refresh_edl_preview()

    def write_edl_files(self):
        if self.busy or not self.edl_eps:
            if not self.edl_eps:
                messagebox.showinfo("Нет данных", "Сначала просканируйте папку.")
            return
        keep = self.edl_keep_first.get()
        pad = self.edl_padding()
        to_write = [e for e in self.edl_eps
                    if edl.apply_padding(edl.effective_intro(e, keep), pad.intro_start, pad.intro_end, e.duration)
                    or edl.apply_padding(e.outro, pad.outro_start, pad.outro_end, e.duration)
                    or e.recap]
        if not to_write:
            messagebox.showinfo("Нечего записывать",
                                "Нет ни интро, ни титров. Сначала «Определить автоматически» или задайте вручную.")
            return
        if not messagebox.askyesno(
            "Запись .edl",
            f"Записать {len(to_write)} файлов .edl рядом с сериями?\n"
            "Существующие .edl будут перезаписаны. Видео не трогается.",
        ):
            return

        written = 0
        for e in self.edl_eps:
            p = edl.build_and_write(e, pad, keep)
            if p:
                written += 1
                self.log_line(f"  ✓ {p.name}")
        self.log_line(f"Записано .edl: {written}.")
        self.refresh_edl_preview()

    def delete_edl_files(self):
        if self.busy:
            return
        existing = [e for e in self.edl_eps if edl.has_external_edl(e.path)]
        if not existing:
            messagebox.showinfo("Нет .edl", "Рядом с сериями нет .edl файлов.")
            return
        if not messagebox.askyesno("Удаление .edl", f"Удалить {len(existing)} файлов .edl рядом с сериями?"):
            return
        n = sum(1 for e in existing if edl.delete_edl(e.path))
        self.log_line(f"Удалено .edl: {n}.")
        self.refresh_edl_preview()

    def _edl_edit_row(self, event):
        iid = self.edl_tree.identify_row(event.y)
        if not iid or iid not in self.edl_row_ep:
            return
        self._edl_edit_dialog(self.edl_row_ep[iid])

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
                 "Recap идёт с начала файла; титры — до конца файла (конец не задаётся).\n"
                 "Конец интро можно не заполнять, если в блоке «Задать всем» указана\n"
                 "длительность интро — тогда конец = начало + длительность.\n"
                 "Отступы сезона применяются к интро/титрам поверх этих значений.",
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        rows = [
            ("Recap до (с начала, пусто = нет):", "rc", e.recap.end if e.recap else None),
            ("Интро начало:", "is", e.intro.start if e.intro else None),
            ("Интро конец:", "ie", e.intro.end if e.intro else None),
            ("Титры начало (до конца файла):", "os", e.outro.start if e.outro else None),
        ]
        varmap = {}
        for i, (lab, key, val) in enumerate(rows, start=1):
            ttk.Label(frm, text=lab).grid(row=i, column=0, sticky="e", pady=2, padx=(0, 6))
            v = tk.StringVar(value=self._fmt_time(val) if val is not None else "")
            varmap[key] = v
            ttk.Entry(frm, textvariable=v, width=12).grid(row=i, column=1, sticky="w", pady=2)

        # Явное удаление сегмента: обнуляет поля, а save() пустое поле трактует как «нет».
        # Нужно, когда детект нашёл лишнее (напр. титров нет — мультсериал идёт до конца):
        # ввод 0 давал бы сегмент «весь эпизод», а эти кнопки убирают его начисто.
        clear_frm = ttk.Frame(frm)
        clear_frm.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(clear_frm, text="Убрать:").pack(side="left")
        ttk.Button(clear_frm, text="интро", width=7,
                   command=lambda: (varmap["is"].set(""), varmap["ie"].set(""))).pack(side="left", padx=2)
        ttk.Button(clear_frm, text="титры", width=7,
                   command=lambda: varmap["os"].set("")).pack(side="left", padx=2)
        ttk.Button(clear_frm, text="recap", width=7,
                   command=lambda: varmap["rc"].set("")).pack(side="left", padx=2)

        btns = ttk.Frame(frm)
        btns.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="e", pady=(10, 0))

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
            if os_ is not None:
                end = e.duration or (e.outro.end if e.outro else os_ + 60)
                e.outro = edl.Segment(os_, end) if end > os_ else None
            else:
                e.outro = None
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
