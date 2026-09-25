"""Вкладка «Дорожки и субтитры»: дорожки по умолчанию в MKV и русские .ru.srt.

Логика — в `core` (сканирование, план, mkvpropedit) и `subs` (subliminal);
здесь то, что в прежнем окне делал `app.py`: выбор дорожек, предпросмотр,
подтверждение и запуск в фоне.
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, current_app, jsonify

import core
from web import replies
from web.api import start_scan
from web.replies import Info, UserError

bp = Blueprint("tracks", __name__)

SUB_OFF = "off"                 # «— выключить субтитры —»


def _option_id(key: tuple) -> str:
    return json.dumps(list(key), ensure_ascii=False)


class TracksTab:
    def __init__(self, state):
        self.st = state
        self.audio_options: list[core.Option] = []
        self.sub_options: list[core.Option] = []
        self.total = 0
        self.audio_key: tuple | None = None          # None — «не трогать»
        self.sub_key: tuple | None = None            # None — «не трогать», SUB_DISABLE — выключить
        self.subs_only_missing = True
        self.subs_status = ""

    # ---------------------------------------------------------- события --
    def on_scan(self, files, rescan: bool) -> None:
        ok = [f for f in files if not f.error]
        self.total = len(ok)
        self.audio_options, _ = core.aggregate_audio(ok)
        self.sub_options, _ = core.aggregate_subtitles(ok)
        if rescan:
            # После «Применить» выбор остаётся, если такая дорожка ещё есть.
            if self.audio_key not in {o.key for o in self.audio_options}:
                self.audio_key = None
            if self.sub_key not in {o.key for o in self.sub_options} | {core.SUB_DISABLE}:
                self.sub_key = None
        else:
            self.audio_key = self.sub_key = None
        self.subs_status = ""

    # ------------------------------------------------------------- план --
    def plans(self):
        return core.plan_changes(self.st.files, self.audio_key, self.sub_key)

    @staticmethod
    def _cur_default(tracks) -> str:
        d = [t for t in tracks if t.default]
        return d[0].label() if d else "—"

    def _rows(self):
        rows, n_change, n_warn = [], 0, 0
        base = Path(self.st.path) if self.st.path else None
        for plan in self.plans():
            f = plan.file
            name = f.path.name
            if base is not None and base.is_dir():
                try:
                    name = str(f.path.relative_to(base))
                except ValueError:
                    pass
            if f.error:
                rows.append({"file": name, "path": str(f.path), "acur": "", "anew": "",
                             "scur": "", "snew": "", "status": f"ошибка: {f.error}",
                             "kind": "error"})
                n_warn += 1
                continue
            if self.audio_key is None:
                anew = "(не трогаем)"
            elif plan.audio_target is not None:
                anew = plan.audio_target.label()
            else:
                anew = "⚠ нет дорожки"
            if self.sub_key is None:
                snew = "(не трогаем)"
            elif plan.sub_disabled:
                snew = "выключены"
            elif plan.sub_target is not None:
                snew = plan.sub_target.label()
            else:
                snew = "⚠ нет дорожки"
            if plan.warnings:
                kind, status = "warn", "; ".join(plan.warnings)
                n_warn += 1
            elif plan.has_changes:
                kind, status = "change", "будет изменён"
                n_change += 1
            else:
                kind, status = "nochange", "без изменений"
            rows.append({"file": name, "path": str(f.path),
                         "acur": self._cur_default(f.audio), "anew": anew,
                         "scur": self._cur_default(f.subtitles), "snew": snew,
                         "status": status, "kind": kind})
        return rows, n_change, n_warn

    def _opt_text(self, o: core.Option) -> str:
        return f"{o.label} — {o.count}/{self.total}"

    def to_dict(self) -> dict:
        rows, n_change, n_warn = self._rows() if self.st.files else ([], 0, 0)
        if self.sub_key == core.SUB_DISABLE:
            sub_choice = SUB_OFF
        else:
            sub_choice = _option_id(self.sub_key) if self.sub_key else ""
        return {
            "audio_options": [{"id": _option_id(o.key), "label": self._opt_text(o)}
                              for o in self.audio_options],
            "sub_options": [{"id": _option_id(o.key), "label": self._opt_text(o)}
                            for o in self.sub_options],
            "audio_choice": _option_id(self.audio_key) if self.audio_key else "",
            "sub_choice": sub_choice,
            "rows": rows,
            "n_change": n_change,
            "n_warn": n_warn,
            "subs_only_missing": self.subs_only_missing,
            "subs_status": self.subs_status,
        }

    def choose(self, audio: str, sub: str) -> None:
        by_id = {_option_id(o.key): o.key for o in self.audio_options}
        self.audio_key = by_id.get(audio) if audio else None
        if sub == SUB_OFF:
            self.sub_key = core.SUB_DISABLE
        else:
            by_id = {_option_id(o.key): o.key for o in self.sub_options}
            self.sub_key = by_id.get(sub) if sub else None


def tab() -> TracksTab:
    return current_app.config["STATE"].tabs["tracks"]


def setup(state) -> None:
    state.add_tab("tracks", TracksTab(state))


# --------------------------------------------------------------- API --
@bp.post("/api/tracks/choice")
def choice():
    t = tab()
    t.st.jobs.ensure_idle()
    data = replies.body()
    t.choose(str(data.get("audio") or ""), str(data.get("sub") or ""))
    if "subs_only_missing" in data:
        t.subs_only_missing = bool(data["subs_only_missing"])
    t.st.touch()
    return jsonify({"ok": True})


@bp.post("/api/tracks/apply")
def apply():
    t = tab()
    st = t.st
    st.jobs.ensure_idle()
    if not st.files:
        raise Info("Нет данных", "Сначала просканируйте папку.")
    if t.audio_key is None and t.sub_key is None:
        raise Info("Ничего не выбрано", "Выберите аудио и/или субтитры.")
    try:
        _, propedit = core.find_tools()
    except FileNotFoundError as e:
        raise UserError("MKVToolNix не найден", str(e))
    plans = t.plans()
    todo = [p for p in plans if not p.file.error and core.build_command(propedit, p)]
    if not todo:
        raise Info("Нечего применять", "Во всех файлах уже стоят нужные дорожки.")
    warns = sum(1 for p in plans if p.warnings)
    text = f"Будет изменено файлов: {len(todo)}."
    if warns:
        text += f"\nС предупреждениями (будут пропущены или частично): {warns}."
    text += "\n\nИзменения обратимы — само видео не трогается."
    replies.confirm("apply", "Применить дорожки?", text, yes="Применить")

    folder, recursive = st.path, st.recursive
    st.log.add(f"ПРИМЕНЕНИЕ: {len(todo)} файл(ов)")

    def work(job):
        ok = 0
        job.progress(0, len(todo))
        for i, p in enumerate(todo, 1):
            if job.cancelled():
                st.log.add("Применение отменено.")
                break
            res = core.apply_plan(propedit, p)
            ok += 1 if res.ok else 0
            name = res.plan.file.path.name
            st.log.add(f"  ✓ {name}" if res.ok else f"  ✗ {name}: {res.output}")
            job.progress(i, len(todo), f"{i}/{len(todo)}: {name}")
        # И после отмены пересканируем: часть файлов уже изменена.
        msg = f"Готово: успешно {ok}/{len(todo)}."
        st.log.add(msg + " Обновляю состояние…")
        job.summary = msg
        job.progress(0, 0, "обновляю состояние…")
        return core.scan_folder(folder, recursive)

    def done(job, files):
        st.apply_scan(files, rescan=True)

    job = st.jobs.start("Применение дорожек", work, on_done=done)
    return jsonify({"job": job.to_dict()})


@bp.post("/api/tracks/rescan")
def rescan():
    st = tab().st
    st.jobs.ensure_idle()
    if not st.path:
        raise Info("Нет папки", "Сначала выберите папку.")
    return jsonify({"job": start_scan(st, st.path, st.recursive, rescan=True).to_dict()})


@bp.post("/api/tracks/subs")
def download_subs():
    import subs as subsmod  # ленивый импорт: subliminal тяжёлый, без него остальное работает

    t = tab()
    st = t.st
    st.jobs.ensure_idle()
    err = subsmod.subliminal_available()
    if err:
        raise UserError("subliminal не установлен",
                        f"Для скачивания субтитров нужен пакет subliminal:\n\n    pip install subliminal\n\n({err})")
    if not st.files:
        raise Info("Нет данных", "Сначала просканируйте папку.")
    data = replies.body()
    if "only_missing" in data:
        t.subs_only_missing = bool(data["only_missing"])
    settings = subsmod.load_settings()
    if not subsmod.providers_for(settings):
        raise UserError("Не задан источник",
                        "Укажите данные OpenSubtitles (кнопка «OpenSubtitles…») "
                        "или включите запасные провайдеры.")
    if not settings.has_opensubtitles():
        replies.confirm("no_os", "Без OpenSubtitles",
                        "Данные OpenSubtitles не заданы. Бесплатные источники для русского "
                        "почти всегда пусты.\nПродолжить только на запасных провайдерах?",
                        yes="Продолжить")
    ok_files = [f for f in st.files if not f.error]
    if t.subs_only_missing:
        targets = [f.path for f in subsmod.missing_russian(ok_files)]
    else:
        targets = [f.path for f in ok_files]
    if not targets:
        raise Info("Нечего качать", "У всех серий русские субтитры уже есть.")
    replies.confirm("go", "Скачивание субтитров",
                    f"Найти и скачать русские субтитры для {len(targets)} серий?\n"
                    "Файлы лягут рядом как .ru.srt — видео не трогается.", yes="Скачать")

    t.subs_status = ""
    st.log.add(f"СУБТИТРЫ: ищу русские для {len(targets)} серий…")

    def work(job):
        job.progress(0, len(targets))
        return subsmod.download(
            targets, settings, stop=job.cancelled,
            progress=lambda i, total, p: job.progress(i, total, f"{i + 1}/{total}: {Path(p).name}"))

    def done(job, results):
        dl = sum(1 for r in results if r.status == "downloaded")
        nf = sum(1 for r in results if r.status == "notfound")
        errs = sum(1 for r in results if r.status == "error")
        for r in results:
            if r.status == "downloaded":
                st.log.add(f"  ✓ {r.path.name}  ({r.provider})")
            elif r.status == "notfound":
                st.log.add(f"  – {r.path.name}: нет русских")
            else:
                st.log.add(f"  ✗ {r.path.name}: {r.detail}")
        msg = f"Субтитры: скачано {dl}, не найдено {nf}, ошибок {errs}."
        if job.cancelled():
            msg = "Отменено. " + msg
        st.log.add(msg)
        t.subs_status = msg
        job.summary = msg

    job = st.jobs.start("Скачивание субтитров", work, on_done=done)
    return jsonify({"job": job.to_dict()})
