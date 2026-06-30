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

import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import core

NOTOUCH = "— не трогать —"
SUBOFF = "— выключить субтитры —"


class App:
    def __init__(self, root: tk.Tk, start_folder: str = ""):
        self.root = root
        self.files: list[core.MkvFile] = []
        self.audio_options: list[core.Option] = []
        self.sub_options: list[core.Option] = []
        self.total_files = 0
        self.busy = False

        root.title("MKV — дорожки по умолчанию")
        root.geometry("1080x720")
        root.minsize(900, 600)

        self._build_top(start_folder)
        self._build_selection()
        self._build_subs()
        self._build_table()
        self._build_bottom()

        self._enable_entry_clipboard()
        self._check_tools()
        if start_folder and Path(start_folder).is_dir():
            # Папка предзаполнена (см. _build_top). Авто-скан запускаем только если в
            # самой папке есть .mkv напрямую; для корня библиотеки из подпапок-сериалов
            # просто оставляем путь как стартовую точку для «Обзор»/«Сканировать».
            try:
                direct = core.list_mkv(start_folder, recursive=False)
            except Exception:
                direct = []
            if direct:
                self.root.after(200, self.scan)

    # ------------------------------------------------------------------ UI --
    def _build_top(self, start_folder):
        f = ttk.Frame(self.root, padding=(10, 10, 10, 4))
        f.pack(fill="x")

        ttk.Label(f, text="Папка:").pack(side="left")
        self.path_var = tk.StringVar(value=start_folder)
        ttk.Entry(f, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(f, text="Обзор…", command=self.browse).pack(side="left")

        self.recursive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Включая вложенные папки", variable=self.recursive_var).pack(side="left", padx=10)
        self.scan_btn = ttk.Button(f, text="Сканировать", command=self.scan)
        self.scan_btn.pack(side="left")

        self.summary_var = tk.StringVar(value="Папка не выбрана.")
        ttk.Label(self.root, textvariable=self.summary_var, padding=(10, 0)).pack(fill="x")

    def _build_selection(self):
        f = ttk.LabelFrame(self.root, text="Что поставить по умолчанию", padding=10)
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

    def _build_subs(self):
        f = ttk.LabelFrame(self.root, text="Русские субтитры (качаются файлом .ru.srt рядом с серией)", padding=10)
        f.pack(fill="x", padx=10, pady=(0, 6))

        self.subs_only_missing = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="только где русских ещё нет", variable=self.subs_only_missing).pack(side="left")
        self.subs_btn = ttk.Button(f, text="Скачать русские субтитры", command=self.download_subs)
        self.subs_btn.pack(side="left", padx=8)
        ttk.Button(f, text="OpenSubtitles…", command=self.subs_settings_dialog).pack(side="left")
        self.subs_status = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.subs_status).pack(side="left", padx=10)

    def _build_table(self):
        f = ttk.Frame(self.root, padding=(10, 0))
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

        self.tree.tag_configure("warn", background="#ffd9d9")
        self.tree.tag_configure("change", background="#dff5df")
        self.tree.tag_configure("nochange", foreground="#888888")
        self.tree.tag_configure("error", background="#ffbcbc")

    def _build_bottom(self):
        f = ttk.Frame(self.root, padding=(10, 6))
        f.pack(fill="x")
        self.apply_btn = ttk.Button(f, text="Применить", command=self.apply)
        self.apply_btn.pack(side="left")
        self.progress = ttk.Progressbar(f, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)

        self.log = ScrolledText(self.root, height=8, state="disabled", wrap="word")
        self.log.pack(fill="x", padx=10, pady=(0, 10))

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
        state = "disabled" if busy else "normal"
        for b in (self.scan_btn, self.apply_btn, self.subs_btn):
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

    def scan(self):
        if self.busy:
            return
        folder = self.path_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showerror("Ошибка", "Укажите существующую папку.")
            return
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
                files = core.scan_folder(folder, recursive, progress=progress)
            except Exception as e:  # noqa: BLE001
                self.root.after(0, lambda: self._scan_error(e))
                return
            self.root.after(0, lambda: self._scan_done(files))

        threading.Thread(target=work, daemon=True).start()

    def _scan_progress(self, i, total, path):
        self.progress.configure(maximum=max(total, 1), value=i)
        self.summary_var.set(f"Сканирование {i + 1}/{total}: {Path(path).name}")

    def _scan_error(self, e):
        self.set_busy(False)
        self.summary_var.set("Ошибка сканирования.")
        messagebox.showerror("Ошибка", str(e))

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

    def _opt_text(self, o: core.Option) -> str:
        return f"{o.label} — {o.count}/{self.total_files}"

    # ------------------------------------------------------------- preview --
    def refresh_preview(self):
        self.tree.delete(*self.tree.get_children())
        if not self.files:
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

        self.log_line(f"Превью: к изменению {n_change}, предупреждений {n_warn}.")

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
                res = core.apply_plan(propedit, p)
                ok += 1 if res.ok else 0
                self.root.after(0, lambda r=res, d=i: self._apply_step(r, d))
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
                results = subsmod.download(targets, settings, progress=progress)
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


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else ""
    root = tk.Tk()
    App(root, start)
    root.mainloop()


if __name__ == "__main__":
    main()
