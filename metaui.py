"""Вкладка «Медиатека»: опознание тайтла, выбор картинок, план раскладки.

UI вынесен из `app.py` отдельным модулем — тот и без того на полторы тысячи
строк. Наружу класс берёт у главного окна только общие вещи (поле пути, лог,
прогресс, флаг занятости и событие отмены), поэтому вкладка не знает ничего про
дорожки и EDL, а те — про неё.

Длинные операции идут в рабочем потоке по тому же образцу, что скан MKV:
`set_busy(True)` → `threading.Thread` → результат в UI через `root.after`.
"""
from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import artwork
import edl
import library
import metaconf
import metadata
import net
import nfo
import theme

try:                                   # Pillow нужен только для миниатюр
    from PIL import Image, ImageTk
except ImportError:                      # noqa: BLE001 — без него работает всё, кроме сетки выбора
    Image = ImageTk = None

KIND_AUTO = "авто"
KIND_LABELS = {KIND_AUTO: None, "фильм": library.MOVIE, "сериал": library.TV}

ART_ROWS = (
    (metadata.ART_POSTER, "Постер"),
    (metadata.ART_FANART, "Фанарт"),
    (metadata.ART_LOGO, "Логотип"),
)

_THUMB_BOX = (90, 130)

# Страницы выдачи ключей — их открывает кнопка «Получить…» в настройках.
KEY_URLS = {
    "tmdb": "https://www.themoviedb.org/settings/api",
    "fanart": "https://fanart.tv/get-an-api-key/",
    "omdb": "https://www.omdbapi.com/apikey.aspx",
}


class MetaTab:
    def __init__(self, parent, host):
        self.host = host
        self.root = host.root
        self.settings = metaconf.load_settings()

        self.hits: list[metadata.SearchHit] = []
        self.hit: metadata.SearchHit | None = None
        self.info: metadata.MediaInfo | None = None
        self.plan: library.Plan | None = None
        self.chosen: dict[tuple[str, int | None], metadata.ArtCandidate] = {}
        self.overrides: dict[Path, tuple[int, int]] = {}
        self.row_by_iid: dict[str, library.Row] = {}
        self._images: dict[str, object] = {}     # ссылки на PhotoImage — иначе Tk их удалит

        self._build(parent)

    # ------------------------------------------------------------------ UI --
    def _build(self, parent):
        self._build_identify(parent)
        self._build_art(parent)
        self._build_table(parent)
        self._build_actions(parent)

    def _build_identify(self, parent):
        f = ttk.LabelFrame(parent, text="Что это", padding=10)
        f.pack(fill="x", padx=10, pady=(8, 4))
        f.columnconfigure(3, weight=1)

        ttk.Label(f, text="Тип:").grid(row=0, column=0, sticky="w")
        self.kind_combo = ttk.Combobox(f, state="readonly", width=8,
                                       values=list(KIND_LABELS))
        self.kind_combo.current(0)
        self.kind_combo.grid(row=0, column=1, sticky="w", padx=(6, 16))

        ttk.Label(f, text="Название:").grid(row=0, column=2, sticky="w")
        self.query_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.query_var).grid(row=0, column=3, sticky="ew", padx=6)

        ttk.Label(f, text="Год:").grid(row=0, column=4, sticky="w")
        self.year_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.year_var, width=6).grid(row=0, column=5, padx=6)

        self.find_btn = ttk.Button(f, text="Найти", command=self.find)
        self.find_btn.grid(row=0, column=6)

        self.hit_var = tk.StringVar(value="Тайтл не выбран. Укажите папку, нажмите «Найти».")
        ttk.Label(f, textvariable=self.hit_var).grid(row=1, column=0, columnspan=5,
                                                     sticky="w", pady=(8, 0))
        self.change_btn = ttk.Button(f, text="Сменить…", command=self.choose_hit,
                                     state="disabled")
        self.change_btn.grid(row=1, column=5, columnspan=2, sticky="e", pady=(8, 0))

        bottom = ttk.Frame(f)
        bottom.grid(row=2, column=0, columnspan=7, sticky="ew", pady=(8, 0))
        ttk.Button(bottom, text="Настройки скрапера…",
                   command=self.settings_dialog).pack(side="left")
        self.pending_btn = ttk.Button(bottom, text="Что не обработано…",
                                      command=self.scan_library)
        self.pending_btn.pack(side="left", padx=6)
        ttk.Button(bottom, text="Подставить из имени папки",
                   command=self.fill_from_folder).pack(side="right")

    def _build_art(self, parent):
        f = ttk.LabelFrame(parent, text="Картинки", padding=10)
        f.pack(fill="x", padx=10, pady=4)
        self.art_widgets = {}

        for col, (kind, label) in enumerate(ART_ROWS):
            self.art_widgets[kind] = self._art_cell(f, col, label, kind)

        # Постеры сезонов: один блок с переключателем сезона — у Archer их 15,
        # пятнадцатью отдельными ячейками вкладка бы не поместилась.
        cell = ttk.Frame(f)
        cell.grid(row=0, column=len(ART_ROWS), padx=10, sticky="n")
        head = ttk.Frame(cell)
        head.pack(fill="x")
        ttk.Label(head, text="Сезон").pack(side="left")
        self.season_combo = ttk.Combobox(head, state="readonly", width=6, values=[])
        self.season_combo.pack(side="left", padx=4)
        self.season_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_art_cells())
        preview = ttk.Label(cell, text="—", anchor="center", width=12)
        preview.pack(pady=4)
        info = ttk.Label(cell, text="", anchor="center")
        info.pack()
        btn = ttk.Button(cell, text="Выбрать…", state="disabled",
                         command=lambda: self.choose_art(metadata.ART_SEASON))
        btn.pack(pady=(4, 0))
        self.art_widgets[metadata.ART_SEASON] = (preview, info, btn)

    def _art_cell(self, parent, col, label, kind):
        cell = ttk.Frame(parent)
        cell.grid(row=0, column=col, padx=10, sticky="n")
        ttk.Label(cell, text=label).pack()
        preview = ttk.Label(cell, text="—", anchor="center", width=12)
        preview.pack(pady=4)
        info = ttk.Label(cell, text="", anchor="center")
        info.pack()
        btn = ttk.Button(cell, text="Выбрать…", state="disabled",
                         command=lambda k=kind: self.choose_art(k))
        btn.pack(pady=(4, 0))
        return preview, info, btn

    def _build_table(self, parent):
        f = ttk.Frame(parent, padding=(10, 0))
        f.pack(fill="both", expand=True)

        cols = ("sel", "now", "next", "status")
        heads = {"sel": "", "now": "Сейчас", "next": "Станет", "status": "Статус"}
        widths = {"sel": 28, "now": 300, "next": 360, "status": 150}
        self.tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor="center" if c == "sel" else "w",
                             stretch=(c in ("now", "next")))
        vsb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        for tag, style in theme.row_colors(self.tree).items():
            self.tree.tag_configure(tag, **style)

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)

    def _build_actions(self, parent):
        # Две строки, а не одна: в одну этот набор не влезает в окно 1080 и
        # правый край (выбор политики) обрезается.
        opts = ttk.Frame(parent, padding=(10, 4, 10, 0))
        opts.pack(fill="x")

        self.delete_junk = tk.BooleanVar(value=self.settings.junk_action == metaconf.JUNK_DELETE)
        self.junk_check = ttk.Checkbutton(opts, text="удалять мусор в Корзину",
                                          variable=self.delete_junk,
                                          command=self._on_junk_toggle)
        self.junk_check.pack(side="left")
        if not library.send_to_trash_available():
            # Без send2trash «удалить» означало бы удалить безвозвратно — не предлагаем.
            self.junk_check.configure(text="удалять мусор в Корзину (нужен send2trash)")
            self.junk_check.state(["disabled"])
            self.delete_junk.set(False)

        ttk.Label(opts, text="Существующие .nfo и картинки:").pack(side="left", padx=(16, 4))
        self.policy_combo = ttk.Combobox(opts, state="readonly", width=24,
                                         values=[metaconf.POLICY_LABELS[p]
                                                 for p in metaconf.POLICIES])
        self.policy_combo.current(metaconf.POLICIES.index(self.settings.existing_policy))
        self.policy_combo.pack(side="left")
        self.policy_combo.bind("<<ComboboxSelected>>", lambda e: self._on_policy_change())

        f = ttk.Frame(parent, padding=(10, 6))
        f.pack(fill="x")
        self.plan_btn = ttk.Button(f, text="Построить план", command=self.build_plan,
                                   state="disabled")
        self.plan_btn.pack(side="left")
        self.apply_btn = ttk.Button(f, text="Применить", command=self.apply,
                                    state="disabled")
        self.apply_btn.pack(side="left", padx=6)

        self.status_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.status_var).pack(side="left", padx=10)

        self.host.extra_busy_buttons += [self.find_btn, self.change_btn,
                                         self.plan_btn, self.apply_btn,
                                         self.pending_btn]

    # ------------------------------------------------------------- helpers --
    def folder(self) -> Path | None:
        raw = self.host.path_var.get().strip()
        if not raw:
            return None
        p = Path(raw)
        return p if p.exists() else None

    def kind(self) -> str:
        chosen = KIND_LABELS[self.kind_combo.get()]
        if chosen:
            return chosen
        folder = self.folder()
        return library.guess_kind(folder, self.settings) if folder else library.TV

    def log(self, text: str):
        self.host.log_line(text)

    def _need_folder(self) -> Path | None:
        folder = self.folder()
        if folder is None:
            messagebox.showerror("Ошибка", "Сначала укажите папку в поле наверху.")
        return folder

    def _need_key(self) -> bool:
        if self.settings.has_tmdb():
            return True
        messagebox.showerror(
            "Нет ключа TMDb",
            "Метаданные берутся с themoviedb.org — нужен бесплатный ключ.\n"
            "Впишите его в «Настройки скрапера…».")
        return False

    def _on_junk_toggle(self):
        self.settings.junk_action = (metaconf.JUNK_DELETE if self.delete_junk.get()
                                     else metaconf.JUNK_EXTRAS)
        metaconf.save_settings(self.settings)

    def _on_policy_change(self):
        self.settings.existing_policy = metaconf.POLICIES[self.policy_combo.current()]
        metaconf.save_settings(self.settings)

    def fill_from_folder(self):
        folder = self._need_folder()
        if folder is None:
            return
        # У одиночного файла фильма расширение в название попасть не должно.
        name = folder.stem if folder.is_file() else folder.name
        title, year = library.guess_title_year(name)
        self.query_var.set(title)
        self.year_var.set(str(year) if year else "")

    # ------------------------------------------- что в библиотеке не готово --
    def scan_library(self):
        """Пробегает по корням библиотеки и показывает необработанное."""
        if self.host.busy:
            return
        if not (self.settings.movies_roots or self.settings.tv_roots):
            messagebox.showinfo(
                "Не заданы корни",
                "Укажите папки с фильмами и сериалами в «Настройках скрапера…» —\n"
                "по ним и составляется список.")
            return

        self.host.set_busy(True)
        self.status_var.set("Обход библиотеки…")
        self.host.progress.configure(value=0, maximum=1)

        def progress(i, total, name):
            self.root.after(0, lambda: self._progress(i, total, name))

        def work():
            try:
                items = library.find_unprocessed(
                    self.settings, stop=self.host.cancel_event.is_set, progress=progress)
            except OSError as e:
                self.root.after(0, lambda: self._fail("Обход библиотеки", e))
                return
            self.root.after(0, lambda: self._pending_ready(items))

        threading.Thread(target=work, daemon=True).start()

    def _pending_ready(self, items):
        self.host.set_busy(False)
        self.host.progress.configure(value=0)
        self.status_var.set(f"Не обработано: {len(items)}")
        self.log(f"Обход библиотеки: не обработано {len(items)}")
        if not items:
            messagebox.showinfo("Всё готово",
                                "В библиотеке не нашлось необработанных папок.")
            return
        self._pending_dialog(items)

    def _pending_dialog(self, items):
        win = self._dialog("Что не обработано")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, justify="left", text=(
            "Список составлен по самим файлам: базы программа не ведёт.\n"
            "Выберите строку — путь подставится наверх, дальше обычный прогон.")
        ).pack(anchor="w", pady=(0, 8))

        cols = ("kind", "name", "why")
        tree = ttk.Treeview(frm, columns=cols, show="headings", height=min(16, len(items)))
        for col, title, width in (("kind", "Тип", 70), ("name", "Папка", 330),
                                  ("why", "Чего не хватает", 320)):
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor="w")
        vsb = ttk.Scrollbar(frm, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="top", fill="both", expand=True)

        by_iid = {}
        for item in items:
            iid = tree.insert("", "end", values=(
                "фильм" if item.kind == library.MOVIE else "сериал",
                item.path.name, item.why))
            by_iid[iid] = item
        children = tree.get_children()
        if children:
            tree.selection_set(children[0])

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))

        def take():
            selected = tree.selection()
            if not selected:
                return
            item = by_iid[selected[0]]
            win.destroy()
            self.host.path_var.set(str(item.path))
            self.kind_combo.set("фильм" if item.kind == library.MOVIE else "сериал")
            self.fill_from_folder()
            self.log(f"Выбрано из списка: {item.path}")

        def ignore():
            selected = tree.selection()
            if not selected:
                return
            item = by_iid[selected[0]]
            if not item.path.is_dir():
                messagebox.showinfo(
                    "Нечего пометить",
                    "Это отдельный файл, а не папка — метку положить некуда.\n"
                    "Обработайте его: папка вокруг него создастся сама.")
                return
            marker = library.write_ignore_marker(item.path)
            tree.delete(selected[0])
            self.log(f"Папка исключена из списка: {item.path} (метка {marker.name})")

        ttk.Button(btns, text="Взять в работу", command=take).pack(side="right")
        ttk.Button(btns, text="Больше не предлагать", command=ignore).pack(side="right", padx=6)
        ttk.Button(btns, text="Закрыть", command=win.destroy).pack(side="right", padx=6)
        tree.bind("<Double-1>", lambda e: take())
        self._center(win)

    # ---------------------------------------------------------------- поиск --
    def find(self):
        if self.host.busy or not self._need_key():
            return
        folder = self._need_folder()
        if folder is None:
            return
        if not self.query_var.get().strip():
            self.fill_from_folder()
        query = self.query_var.get().strip()
        if not query:
            messagebox.showerror("Ошибка", "Впишите название для поиска.")
            return
        year = self.year_var.get().strip()
        year = int(year) if year.isdigit() else None
        kind = self.kind()

        self.host.set_busy(True)
        self.status_var.set("Поиск…")
        self.log(f"Поиск в TMDb: {query}" + (f" ({year})" if year else ""))

        def work():
            try:
                if query.lower().startswith("tt") and query[2:].isdigit():
                    hits = metadata.search_by_imdb(query, self.settings)
                else:
                    hits = metadata.search(kind, query, self.settings, year=year)
            except net.NetError as e:
                self.root.after(0, lambda: self._fail("Поиск", e))
                return
            self.root.after(0, lambda: self._found(hits))

        threading.Thread(target=work, daemon=True).start()

    def _found(self, hits):
        self.host.set_busy(False)
        self.status_var.set("")
        if not hits:
            self.log("Ничего не найдено — поправьте название или вставьте IMDb-ID (tt…).")
            messagebox.showinfo("Не найдено",
                                "TMDb ничего не вернул.\nПоправьте название или "
                                "вставьте IMDb-ID вида tt6263850.")
            return
        self.hits = hits
        self.change_btn.configure(state="normal")
        if len(hits) == 1:
            self._select_hit(hits[0])
        else:
            self.choose_hit()

    def choose_hit(self):
        hits = self.hits
        if not hits:
            return
        win = self._dialog("Выбор тайтла")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Что из этого нужно:").pack(anchor="w")

        listbox = tk.Listbox(frm, width=70, height=min(12, len(hits)))
        for hit in hits:
            kind_ru = "фильм" if hit.kind == library.MOVIE else "сериал"
            listbox.insert("end", f"{hit.label()} · {kind_ru} · TMDb {hit.tmdb_id}")
        listbox.selection_set(0)
        listbox.pack(fill="both", expand=True, pady=6)

        overview = ttk.Label(frm, text=hits[0].overview[:300], wraplength=520,
                             justify="left")
        overview.pack(anchor="w", pady=(0, 8))
        listbox.bind("<<ListboxSelect>>", lambda e: overview.configure(
            text=hits[listbox.curselection()[0]].overview[:300] if listbox.curselection() else ""))

        btns = ttk.Frame(frm)
        btns.pack(fill="x")

        def take():
            sel = listbox.curselection()
            win.destroy()
            if sel:
                self._select_hit(hits[sel[0]])

        ttk.Button(btns, text="Выбрать", command=take).pack(side="right")
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="right", padx=6)
        listbox.bind("<Double-1>", lambda e: take())
        self._center(win)

    def _select_hit(self, hit: metadata.SearchHit):
        self.hit = hit
        self.hit_var.set(f"Выбрано: {hit.label()} · TMDb {hit.tmdb_id}")
        self.log(f"Выбран тайтл: {hit.label()} (TMDb {hit.tmdb_id})")
        self.fetch_info()

    # -------------------------------------------------------- метаданные --
    def fetch_info(self):
        folder = self._need_folder()
        if folder is None or self.hit is None:
            return
        scan = library.scan_folder(folder) if self.hit.kind == library.TV else None
        seasons = scan.seasons() if scan else []
        # Размеры сезонов нужны, чтобы подобрать верную разбивку в episode
        # groups, когда обычные сезоны TMDb с диском не сошлись (аниме).
        sizes = scan.season_sizes() if scan else {}
        self.host.set_busy(True)
        self.status_var.set("Загрузка метаданных…")

        def progress(text):
            self.root.after(0, lambda: self.status_var.set(text))

        def work():
            try:
                info = metadata.fetch(self.hit.kind, self.hit.tmdb_id, self.settings,
                                      seasons=seasons, season_sizes=sizes,
                                      progress=progress)
            except net.NetError as e:
                self.root.after(0, lambda: self._fail("Метаданные", e))
                return
            self.root.after(0, lambda: self._info_ready(info))

        threading.Thread(target=work, daemon=True).start()

    def _info_ready(self, info: metadata.MediaInfo):
        self.host.set_busy(False)
        self.status_var.set("")
        self.info = info
        self.log(f"Метаданные готовы: {info.folder_title} ({info.year}), "
                 f"серий {len(info.episodes)}, вариантов арта {len(info.art)}")
        self._auto_choose_art()
        self.plan_btn.configure(state="normal")
        self.build_plan()

    # --------------------------------------------------------------- арт --
    def _auto_choose_art(self):
        self.chosen.clear()
        if self.info is None:
            return
        langs = self.settings.art_languages
        for kind, _ in ART_ROWS:
            best = metadata.best_art(self.info.art, kind, langs)
            if best:
                self.chosen[(kind, None)] = best
        seasons = metadata.seasons_with_art(self.info.art)
        for season in seasons:
            best = metadata.best_art(self.info.art, metadata.ART_SEASON, langs, season=season)
            if best:
                self.chosen[(metadata.ART_SEASON, season)] = best
        self.season_combo.configure(values=[str(s) for s in seasons])
        if seasons:
            self.season_combo.current(0)
        self._refresh_art_cells()

    def _current_season(self) -> int | None:
        value = self.season_combo.get()
        return int(value) if value.isdigit() else None

    def _refresh_art_cells(self):
        for kind, _ in ART_ROWS:
            self._refresh_cell(kind, None)
        self._refresh_cell(metadata.ART_SEASON, self._current_season())

    def _refresh_cell(self, kind, season):
        preview, info_label, btn = self.art_widgets[kind]
        candidate = self.chosen.get((kind, season))
        available = self.info is not None and metadata.art_of(
            self.info.art, kind, season)
        btn.configure(state="normal" if available and ImageTk else "disabled")
        if candidate is None:
            preview.configure(image="", text="нет")
            info_label.configure(text="")
            self._images.pop(f"{kind}-{season}", None)
            return
        info_label.configure(text=candidate.label())
        image = self._thumb(candidate.thumb_url)
        if image is None:
            preview.configure(image="", text="(нет превью)")
        else:
            self._images[f"{kind}-{season}"] = image
            preview.configure(image=image, text="")

    def _thumb(self, url: str, box=_THUMB_BOX):
        """URL → PhotoImage нужного размера. Без Pillow или без сети → None."""
        if ImageTk is None or not url:
            return None
        try:
            import io
            data = artwork.thumbnail_bytes(url)
            img = Image.open(io.BytesIO(data))
            img.thumbnail(box)
            return ImageTk.PhotoImage(img)
        except Exception:  # noqa: BLE001 — превью необязательно, молча без картинки
            return None

    def choose_art(self, kind: str):
        if self.info is None:
            return
        season = self._current_season() if kind == metadata.ART_SEASON else None
        pool = metadata.art_of(self.info.art, kind, season)
        if not pool:
            messagebox.showinfo("Нет вариантов", "Для этого вида картинок ничего не нашлось.")
            return

        win = self._dialog(f"Выбор: {kind}" + (f", сезон {season}" if season is not None else ""))
        top = ttk.Frame(win, padding=(12, 12, 12, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Язык:").pack(side="left")
        lang_combo = ttk.Combobox(top, state="readonly", width=14,
                                  values=["все", "ru", "en", "без текста"])
        lang_combo.current(0)
        lang_combo.pack(side="left", padx=6)

        body = ttk.Frame(win, padding=12)
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, width=760, height=420, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        grid = ttk.Frame(canvas)
        grid.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=grid, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

        keep: list = []

        def pick(candidate):
            self.chosen[(kind, season)] = candidate
            win.destroy()
            self._refresh_art_cells()
            self.log(f"Выбрана картинка: {kind} — {candidate.label()}")

        def fill():
            for child in grid.winfo_children():
                child.destroy()
            keep.clear()
            wanted = lang_combo.get()
            items = metadata.rank_art(pool, self.settings.art_languages)
            if wanted == "ru":
                items = [c for c in items if c.lang == "ru"]
            elif wanted == "en":
                items = [c for c in items if c.lang == "en"]
            elif wanted == "без текста":
                items = [c for c in items if not c.lang]
            if not items:
                ttk.Label(grid, text="С таким языком вариантов нет.").grid(row=0, column=0)
                return
            for i, candidate in enumerate(items[:40]):
                cell = ttk.Frame(grid, padding=6)
                cell.grid(row=i // 4, column=i % 4, sticky="n")
                image = self._thumb(candidate.thumb_url, (170, 250))
                if image is not None:
                    keep.append(image)
                    btn = ttk.Button(cell, image=image, command=lambda c=candidate: pick(c))
                else:
                    btn = ttk.Button(cell, text="(нет превью)",
                                     command=lambda c=candidate: pick(c))
                btn.pack()
                ttk.Label(cell, text=candidate.label()).pack()

        lang_combo.bind("<<ComboboxSelected>>", lambda e: fill())
        fill()
        win.grid_keep = keep          # держим ссылки на изображения живыми
        self._center(win)

    # --------------------------------------------------------------- план --
    def build_plan(self):
        folder = self._need_folder()
        if folder is None or self.info is None:
            return
        self.plan = library.build_plan(folder, self.info.kind, self.info,
                                       self.settings, self.overrides)
        self._fill_table()
        changed = len(self.plan.changed())
        conflicts = len(self.plan.conflicts())
        msg = f"В плане изменений: {changed}"
        if self.plan.unmatched:
            msg += f", без номера серии: {len(self.plan.unmatched)}"
        if conflicts:
            msg += f", конфликтов: {conflicts}"
        collection = [r for r in self.plan.rows if r.status == library.S_COLLECTION]
        if collection:
            msg = ("Похоже на папку-сборник: внутри несколько фильмов в своих папках. "
                   "Укажите папку одного фильма.")
            self.status_var.set(msg)
            self.log("⚠ " + msg)
            self.apply_btn.configure(state="disabled")
            return
        self.status_var.set(msg)
        self.log(msg + f". Папка тайтла: {self.plan.root}")
        # Кнопка активна и при нуле переименований: .nfo и картинки могут быть
        # ещё не записаны, хотя имена уже верные.
        self.apply_btn.configure(state="normal")

    def _fill_table(self):
        self.tree.delete(*self.tree.get_children())
        self.row_by_iid.clear()
        if self.plan is None:
            return
        base = self.plan.root.parent
        for row in self.plan.rows:
            tag = {
                library.S_OK: "nochange",
                library.S_JUNK: "warn",
                library.S_NOEP: "warn",
                library.S_CONFLICT: "error",
                library.S_CROSSDEV: "error",
                library.S_COLLECTION: "error",
            }.get(row.status, "change")
            sel = "✓" if (row.what == "junk" and row.selected) else ""
            iid = self.tree.insert(
                "", "end", tags=(tag,),
                values=(sel, self._rel(row.src, base), self._rel(row.dst, base),
                        row.status + (f" · {row.note}" if row.note else "")))
            self.row_by_iid[iid] = row

    @staticmethod
    def _rel(path: Path | None, base: Path) -> str:
        if path is None:
            return "—"
        try:
            return str(path.relative_to(base))
        except ValueError:
            return str(path)

    def _on_click(self, event):
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        row = self.row_by_iid.get(iid)
        if row is None or row.what != "junk":
            return
        row.selected = not row.selected
        self.tree.set(iid, "sel", "✓" if row.selected else "")

    def _on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        row = self.row_by_iid.get(iid)
        if row is None or row.what != "video" or row.src is None:
            return
        self._episode_dialog(row.src)

    def _episode_dialog(self, video: Path):
        season, episode = self.overrides.get(video, edl.parse_season_episode(video))
        win = self._dialog(f"Сезон и серия: {video.name}")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Номер из имени файла определить не удалось или он неверный.\n"
                            "Задайте вручную — план пересоберётся.",
                  justify="left").grid(row=0, column=0, columnspan=2, sticky="w",
                                       pady=(0, 8))
        s_var = tk.StringVar(value=str(season) if season is not None else "")
        e_var = tk.StringVar(value=str(episode) if episode is not None else "")
        ttk.Label(frm, text="Сезон (0 — спецвыпуск):").grid(row=1, column=0, sticky="e", padx=(0, 6))
        ttk.Entry(frm, textvariable=s_var, width=6).grid(row=1, column=1, sticky="w")
        ttk.Label(frm, text="Серия:").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Entry(frm, textvariable=e_var, width=6).grid(row=2, column=1, sticky="w", pady=4)

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def save():
            if s_var.get().strip().isdigit() and e_var.get().strip().isdigit():
                self.overrides[video] = (int(s_var.get()), int(e_var.get()))
            else:
                self.overrides.pop(video, None)
            win.destroy()
            self.build_plan()

        ttk.Button(btns, text="Сохранить", command=save).pack(side="right")
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="right", padx=6)
        self._center(win)

    # ---------------------------------------------------------- применение --
    def apply(self):
        if self.host.busy or self.plan is None or self.info is None:
            return
        folder = self._need_folder()
        if folder is None:
            return
        changed = self.plan.changed()
        junk = [r for r in changed if r.what == "junk" and r.selected]
        action = "удалены в Корзину" if self.delete_junk.get() else "перенесены в Extras"
        text = (f"Файлов будет переименовано: {len(changed) - len(junk)}\n"
                f"Посторонних файлов ({action}): {len(junk)}\n"
                f"Папка тайтла: {self.plan.root}\n\n"
                "Плюс будут записаны .nfo и скачаны картинки. Продолжить?")
        if self.plan.conflicts():
            text = f"⚠ Конфликтных строк: {len(self.plan.conflicts())} — они будут пропущены.\n\n" + text
        if not messagebox.askyesno("Применить", text):
            return

        policy = self.settings.existing_policy
        if policy == metaconf.POLICY_ASK:
            policy = (metaconf.POLICY_OVERWRITE
                      if messagebox.askyesno("Существующие файлы",
                                             "Перезаписать уже существующие .nfo и картинки?")
                      else metaconf.POLICY_MISSING)

        self.host.set_busy(True)
        self.host.progress.configure(value=0, maximum=1)

        def progress(i, total, item):
            self.root.after(0, lambda: self._progress(i, total, item))

        def work():
            try:
                summary = self._apply_all(policy, progress)
            except Exception as e:  # noqa: BLE001 — любую поломку показываем, не молчим
                self.root.after(0, lambda: self._fail("Применение", e))
                return
            self.root.after(0, lambda: self._applied(summary))

        threading.Thread(target=work, daemon=True).start()

    def _apply_all(self, policy: str, progress) -> dict:
        """Переименование → .nfo → картинки. Выполняется в рабочем потоке."""
        stop = self.host.cancel_event.is_set
        results = library.apply_plan(self.plan, delete_junk=self.delete_junk.get(),
                                     stop=stop, progress=progress)
        failed = [r for r in results if not r.ok]
        for r in failed:
            self.root.after(0, lambda r=r: self.log(f"✗ {r.row.src}: {r.error}"))

        written = 0
        if not stop():
            written = self._write_nfo(policy)
        art_done = art_skipped = 0
        if not stop():
            tasks = artwork.plan_art(self.plan.root, self.chosen, policy)
            art_results = artwork.download(tasks, stop=stop, progress=progress)
            art_done = sum(1 for r in art_results if r.ok and not r.skipped)
            art_skipped = sum(1 for r in art_results if r.skipped)
            for r in art_results:
                if not r.ok:
                    self.root.after(0, lambda r=r: self.log(f"✗ {r.task.dest.name}: {r.error}"))
            art_urls, season_urls = artwork.chosen_urls(tasks)
            self._write_title_nfo(policy, art_urls, season_urls)
        return {"renamed": len(results) - len(failed), "failed": len(failed),
                "nfo": written, "art": art_done, "art_skipped": art_skipped,
                "cancelled": stop()}

    def _write_nfo(self, policy: str) -> int:
        """`.nfo` для серий. Для фильма серий нет — вернёт 0."""
        if self.info.kind != library.TV:
            return 0
        written = 0
        for row in self.plan.rows:
            if row.what != "video" or row.dst is None or row.action == library.A_SKIP:
                continue
            season, numbers = library.parse_episodes(row.dst)
            # В одном файле может лежать несколько серий — тогда и блоков в
            # .nfo должно быть столько же, иначе Kodi потеряет остальные.
            eps = [self.info.episodes[(season, n)] for n in numbers
                   if (season, n) in self.info.episodes]
            if not eps:
                continue
            path = row.dst.with_suffix(".nfo")
            if path.exists() and policy == metaconf.POLICY_MISSING:
                continue
            preserve = nfo.read_watch_state(path)
            text = (nfo.make_episode_nfo(self.info, eps[0], preserve=preserve)
                    if len(eps) == 1
                    else nfo.make_episodes_nfo(self.info, eps, preserve=preserve))
            nfo.write(path, text)
            written += 1
        return written

    def _write_title_nfo(self, policy: str, art_urls: dict, season_urls: dict) -> None:
        name = "movie.nfo" if self.info.kind == library.MOVIE else "tvshow.nfo"
        path = self.plan.root / name
        if path.exists() and policy == metaconf.POLICY_MISSING:
            return
        preserve = nfo.read_watch_state(path)
        if self.info.kind == library.MOVIE:
            text = nfo.make_movie_nfo(self.info, art=art_urls, preserve=preserve)
        else:
            text = nfo.make_tvshow_nfo(self.info, art=art_urls, season_art=season_urls,
                                       preserve=preserve)
        nfo.write(path, text)

    def _progress(self, i, total, item):
        self.host.progress.configure(maximum=max(total, 1), value=i)
        self.status_var.set(f"{i}/{total}: {Path(str(item)).name}")

    def _applied(self, summary: dict):
        self.host.set_busy(False)
        self.host.progress.configure(value=0)
        parts = [f"переименовано {summary['renamed']}"]
        if summary["failed"]:
            parts.append(f"ошибок {summary['failed']}")
        parts.append(f".nfo {summary['nfo']}")
        parts.append(f"картинок {summary['art']}")
        if summary["art_skipped"]:
            parts.append(f"пропущено картинок {summary['art_skipped']}")
        if summary["cancelled"]:
            parts.append("ОТМЕНЕНО")
        text = "Готово: " + ", ".join(parts)
        self.status_var.set(text)
        self.log(text)

        # Путь в общем поле теперь другой — иначе вкладки «Дорожки» и «EDL»
        # остались бы со старым именем папки.
        if self.plan is not None and self.plan.root.exists():
            self.host.path_var.set(str(self.plan.root))
            self.host.scan()

    def _fail(self, what: str, error):
        self.host.set_busy(False)
        self.status_var.set("")
        self.log(f"✗ {what}: {error}")
        messagebox.showerror(what, str(error))

    # ----------------------------------------------------------- настройки --
    def settings_dialog(self):
        s = self.settings
        win = self._dialog("Настройки скрапера")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, justify="left", text=(
            "Ключи бесплатные и нужны по одному разу. Кнопка «Получить…» открывает\n"
            "страницу выдачи ключа в браузере.\n"
            "Обязателен только TMDb. Без Fanart.tv меньше вариантов логотипов и\n"
            "постеров в сетке выбора, без OMDb в рейтингах остаётся только themoviedb."
        )).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        fields = [
            ("Ключ TMDb:", "tmdb_key", KEY_URLS["tmdb"]),
            ("Ключ Fanart.tv:", "fanart_key", KEY_URLS["fanart"]),
            ("Ключ OMDb:", "omdb_key", KEY_URLS["omdb"]),
        ]
        vars_ = {}
        for i, (label, attr, url) in enumerate(fields, start=1):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="e", padx=(0, 6), pady=2)
            v = tk.StringVar(value=getattr(s, attr))
            vars_[attr] = v
            ttk.Entry(frm, textvariable=v, width=40).grid(row=i, column=1, sticky="ew", pady=2)
            ttk.Button(frm, text="Получить…", width=11,
                       command=lambda u=url: webbrowser.open(u)).grid(
                row=i, column=2, sticky="w", padx=(6, 0), pady=2)

        row = len(fields) + 1
        ttk.Label(frm, text="Язык описаний:").grid(row=row, column=0, sticky="e", padx=(0, 6), pady=(10, 2))
        lang_combo = ttk.Combobox(frm, state="readonly", width=16,
                                  values=list(metaconf.LANGUAGES.values()))
        codes = list(metaconf.LANGUAGES)
        lang_combo.current(codes.index(s.meta_language) if s.meta_language in codes else 0)
        lang_combo.grid(row=row, column=1, sticky="w", pady=(10, 2))

        row += 1
        ttk.Label(frm, text="(имена папок и файлов всегда английские)").grid(
            row=row, column=1, sticky="w")

        row += 1
        ttk.Label(frm, text="Языки картинок:").grid(row=row, column=0, sticky="e", padx=(0, 6), pady=2)
        art_var = tk.StringVar(value=", ".join(x or "без текста" for x in s.art_languages))
        ttk.Entry(frm, textvariable=art_var, width=44).grid(row=row, column=1, sticky="ew", pady=2)

        row += 1
        ttk.Label(frm, justify="left", text=(
            "Корни библиотеки — по ним программа сама понимает, фильм перед ней\n"
            "или сериал, когда тип стоит «авто». Папок может быть несколько.")
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 4))

        row += 1
        movies_list = self._roots_editor(frm, win, row, "Фильмы:", s.movies_roots)
        row += 1
        tv_list = self._roots_editor(frm, win, row, "Сериалы:", s.tv_roots)

        row += 1
        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=3, sticky="e", pady=(12, 0))

        def save():
            for attr, var in vars_.items():
                setattr(s, attr, var.get().strip())
            s.meta_language = codes[lang_combo.current()]
            s.art_languages = [("" if part.strip() in ("без текста", "") else part.strip())
                               for part in art_var.get().split(",")] or ["ru", "en", ""]
            s.movies_roots = list(movies_list.get(0, "end"))
            s.tv_roots = list(tv_list.get(0, "end"))
            metaconf.save_settings(s)
            win.destroy()
            self.log("Настройки скрапера сохранены.")

        ttk.Button(btns, text="Сохранить", command=save).pack(side="right")
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="right", padx=6)
        self._center(win)

    def _roots_editor(self, parent, win, row: int, label: str, values) -> tk.Listbox:
        """Список папок с кнопками «Добавить…» и «Убрать». Возвращает Listbox."""
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="ne",
                                           padx=(0, 6), pady=2)
        box = ttk.Frame(parent)
        box.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)

        listbox = tk.Listbox(box, height=3, width=52, exportselection=False,
                             activestyle="none")
        for value in values:
            listbox.insert("end", value)
        listbox.pack(side="left", fill="both", expand=True)

        buttons = ttk.Frame(box)
        buttons.pack(side="left", padx=(6, 0))

        def add():
            existing = list(listbox.get(0, "end"))
            start = next((p for p in existing if Path(p).is_dir()), None)
            chosen = self._ask_directory(win, start)
            if chosen and chosen not in existing:
                listbox.insert("end", chosen)

        def remove():
            for index in reversed(listbox.curselection()):
                listbox.delete(index)

        ttk.Button(buttons, text="Добавить…", width=11, command=add).pack()
        ttk.Button(buttons, text="Убрать", width=11, command=remove).pack(pady=(4, 0))
        return listbox

    @staticmethod
    def _ask_directory(win: tk.Toplevel, initial: str | None = None) -> str:
        """Выбор папки поверх модального окна.

        Захват мыши снимается на время диалога: с активным grab_set системное
        окно выбора на части систем не отвечает на нажатия.
        """
        try:
            win.grab_release()
        except tk.TclError:
            pass
        try:
            return filedialog.askdirectory(parent=win, initialdir=initial or None) or ""
        finally:
            try:
                win.grab_set()
            except tk.TclError:
                pass

    # ------------------------------------------------------------- окошки --
    def _dialog(self, title: str) -> tk.Toplevel:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        return win

    @staticmethod
    def _center(win: tk.Toplevel) -> None:
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{(sw - w) // 2}+{max(40, (sh - h) // 3)}")
        win.grab_set()
