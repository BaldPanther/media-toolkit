"""Вкладка «Пропуск заставок (EDL)»: детект, ручная правка, запись .edl и глав.

Логика — в `edl` (детект, .edl, кадры), `pipeline` (серия за серией: хвост →
.edl → главы), `chapters`, `trim`, `online`. Здесь список серий с таймингами,
настройки пропуска, «Применять к», подтверждения и запуск в фоне.
"""
from __future__ import annotations

import io
import math
import secrets
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, send_file

import chapters
import core
import edl
import paths
import pipeline
import trim
from web import replies
from web.replies import Info, UserError
from web.state import load_app_settings, update_app_settings

bp = Blueprint("edl", __name__)

EDL_TRACK_AUTO = "Оригинал (авто)"
# Как называть kind из edl.detect_season в строке прогресса.
EDL_KIND_RU = {"intro": "интро", "outro": "титры"}
PAD_KEYS = ("intro_start", "intro_end", "outro_start", "outro_end")

# Границы, которые можно проверить кадром: → отступ сезона, который её двигает.
# У recap отступа нет, поэтому и сдвигать сезон по нему нечем.
BOUND_PADS = {"is": "intro_start", "ie": "intro_end", "os": "outro_start",
              "oe": "outro_end", "rc": None}

# Шаг серии, на котором пропала сеть: (что делали, что добавить в журнал).
_NET_STEPS = {"trim": ("обрезка хвоста", "Оригинал цел. "),
              "edl": ("запись .edl", ""), "chapters": ("запись глав", "")}


def fmt_time(s) -> str:
    if s is None:
        return "—"
    s = max(0, int(round(s)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def parse_time(txt):
    txt = str(txt or "").strip().replace(",", ".")
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


def _seg_txt(seg) -> str:
    return f"{fmt_time(seg.start)}–{fmt_time(seg.end)}" if seg else "—"


# Ключи сортировки таблицы — по одному на колонку. Сортируется сам список
# серий, поэтому порядок переживает перерисовку после каждой правки.
_SORT_KEYS = {
    "file": lambda e: e.path.name.casefold(),
    "se": lambda e: (e.season is None, e.season or 0, e.episode or 0),
    "recap": lambda e: (e.recap is None, e.recap.end if e.recap else 0.0),
    "intro": lambda e: (e.intro is None, e.intro.start if e.intro else 0.0),
    "intro_on": lambda e: (e.online_intro is None, e.online_intro.start if e.online_intro else 0.0),
    "outro": lambda e: (e.outro is None, e.outro.start if e.outro else 0.0),
    "outro_on": lambda e: (e.online_outro is None, e.online_outro.start if e.online_outro else 0.0),
    "edl": lambda e: not edl.has_external_edl(e.path),
    "ch": lambda e: -e.chapters,
    "tail": lambda e: -(e.tail or 0.0) if trim.needs_trim(e.tail) else 0.0,
    "note": lambda e: "; ".join(x for x in (e.note, e.online_note) if x).casefold(),
}


class EdlTab:
    def __init__(self, state):
        self.st = state
        self.eps: list[edl.EpisodeEdl] = []
        self.sort: tuple[str | None, bool] = (None, False)
        self.langs: list[str] = []
        # Настройки пропуска — переживают перезапуск (settings.json, раздел "edl").
        self.keep_first = True
        self.outro_to_end = True
        self.trim_tail = True
        self.track = EDL_TRACK_AUTO
        self.pad = {k: "0" for k in PAD_KEYS}
        self.window = {"intro": f"{edl.INTRO_WINDOW:.0f}", "outro": f"{edl.OUTRO_WINDOW:.0f}"}
        self.chapters_with_edl = False
        self.chapters_force = False
        self.status = "Просканируйте папку, затем «Определить автоматически»."
        self.online_status = ""
        self.frames: dict[str, dict] = {}
        self.load_settings()

    # -------------------------------------------------------- настройки --
    def load_settings(self) -> None:
        data = load_app_settings().get("edl")
        if not isinstance(data, dict):
            return
        self.keep_first = bool(data.get("keep_first", True))
        self.outro_to_end = bool(data.get("outro_to_end", True))
        self.trim_tail = bool(data.get("trim_tail", True))
        track = data.get("track")
        if isinstance(track, str) and track:
            # Языка может не оказаться в новой папке — скан вернёт «Оригинал (авто)».
            self.track = track
        for key, value in (data.get("pad") or {}).items():
            if key in self.pad:
                self.pad[key] = str(value)
        for key, value in (data.get("window") or {}).items():
            if key in self.window:
                self.window[key] = str(value)

    def save_settings(self) -> None:
        update_app_settings(edl={
            "keep_first": self.keep_first, "outro_to_end": self.outro_to_end,
            "trim_tail": self.trim_tail, "track": self.track,
            "pad": dict(self.pad), "window": dict(self.window),
        })

    def pad_val(self, key: str) -> float:
        return parse_time(self.pad[key]) or 0.0

    def window_val(self, key: str) -> float:
        """Окно поиска в секундах. Мусор или совсем мало — берём умолчание.

        Нижняя граница не придирка: сегмент короче MIN_LEN_S детект отбрасывает,
        так что окно в пару секунд гарантированно не нашло бы ничего.
        """
        default = edl.INTRO_WINDOW if key == "intro" else edl.OUTRO_WINDOW
        value = parse_time(self.window[key])
        return value if value and value >= 30 else default

    def padding(self) -> edl.Padding:
        return edl.Padding(*(self.pad_val(k) for k in PAD_KEYS))

    def prefer_lang(self) -> str | None:
        return None if self.track == EDL_TRACK_AUTO else self.track

    # ----------------------------------------------------------- события --
    def on_scan(self, files, rescan: bool) -> None:
        """Пересобирает список серий из скана, сохраняя уже найденные тайминги."""
        prev = {str(e.path): e for e in self.eps}
        eps: list[edl.EpisodeEdl] = []
        langs: list[str] = []
        for f in files:
            if f.error:
                continue
            file_langs = [t.language or "" for t in f.audio]
            for lang in file_langs:
                if lang and lang not in langs:
                    langs.append(lang)
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
                # финальные значения (отступы уже применены при записи), поэтому
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
        self.eps = eps
        # Список пересобран в порядке скана — прежняя сортировка к нему не относится.
        self.sort = (None, False)
        self.langs = sorted(langs)
        if self.track != EDL_TRACK_AUTO and self.track not in self.langs:
            self.track = EDL_TRACK_AUTO
        self.update_status()

    def on_path_changed(self, path: str) -> None:
        self.online_status = ""

    # ----------------------------------------------------------- помощники --
    def by_path(self, path) -> edl.EpisodeEdl:
        ep = next((e for e in self.eps if str(e.path) == str(path)), None)
        if ep is None:
            raise UserError("Нет такой серии", "Просканируйте папку заново.")
        return ep

    def scope(self, data: dict) -> list[edl.EpisodeEdl]:
        """Серии, к которым применять действие: все или выделенные («Применять к»)."""
        if not self.eps:
            raise Info("Нет данных", "Сначала просканируйте папку.")
        if data.get("scope") == "sel":
            wanted = {str(p) for p in data.get("selected") or []}
            eps = [e for e in self.eps if str(e.path) in wanted]
            if not eps:
                raise Info("Нет выделения",
                           "Выделите серии в таблице или переключите «Применять к: ко всем сериям».")
            return eps
        return list(self.eps)

    @staticmethod
    def scope_word(data: dict, n: int) -> str:
        return f"выделенным ({n})" if data.get("scope") == "sel" else f"всем ({n})"

    @staticmethod
    def scope_phrase(data: dict) -> str:
        return ("выделенным сериям" if data.get("scope") == "sel"
                else "всем просканированным сериям")

    def track_info(self) -> str:
        """Какую именно дорожку выберет текущая настройка (язык + название)."""
        for f in self.st.files:
            if getattr(f, "error", "") or not f.audio:
                continue
            idx = edl.pick_audio_index([t.language or "" for t in f.audio], self.prefer_lang())
            if 0 <= idx < len(f.audio):
                t = f.audio[idx]
                name = t.name.strip()
                return f"→ {t.language or 'und'}" + (f" «{name}»" if name else "")
            break
        return ""

    def outro_end_active(self) -> bool:
        """Отступ конца титров на что-то влияет: галка снята или где-то конец задан."""
        return not self.outro_to_end or any(ep.outro_fixed_end for ep in self.eps)

    def update_status(self) -> None:
        keep, to_end, pad = self.keep_first, self.outro_to_end, self.padding()
        n_intro = n_outro = n_tail = 0
        for e in self.eps:
            if not (keep and edl.is_first_of_season(e) and e.intro is not None):
                if edl.apply_padding(edl.effective_intro(e, keep), pad.intro_start,
                                     pad.intro_end, e.duration):
                    n_intro += 1
            n_outro += bool(edl.final_outro(e, pad, to_end))
            n_tail += trim.needs_trim(e.tail)
        if self.eps:
            self.status = (f"Серий: {len(self.eps)}. К пропуску интро: {n_intro}, титры: {n_outro}."
                           + (f" Звук дольше видео: {n_tail}." if n_tail else ""))

    def rows(self) -> list[dict]:
        keep, to_end, pad = self.keep_first, self.outro_to_end, self.padding()
        out = []
        for e in self.eps:
            se = (f"S{e.season:02d}E{e.episode:02d}"
                  if e.season is not None and e.episode is not None else "—")
            intro_eff = edl.apply_padding(edl.effective_intro(e, keep),
                                          pad.intro_start, pad.intro_end, e.duration)
            outro_eff = edl.final_outro(e, pad, to_end)
            first = keep and edl.is_first_of_season(e) and e.intro is not None
            out.append({
                "path": str(e.path), "file": e.path.name, "se": se,
                "recap": f"0:00–{fmt_time(e.recap.end)}" if e.recap else "—",
                "intro": "показ (1-я серия)" if first else _seg_txt(intro_eff),
                "first": first,
                "intro_on": _seg_txt(e.online_intro),
                "outro": _seg_txt(outro_eff),
                "outro_on": _seg_txt(e.online_outro),
                "edl": edl.has_external_edl(e.path),
                "ch": e.chapters,
                # Пусто — хвоста нет или его не видно (у файла нет статистики дорожек).
                "tail": f"{e.tail:.0f} с" if trim.needs_trim(e.tail) else "",
                "note": "; ".join(x for x in (e.note, e.online_note) if x),
            })
        return out

    def to_dict(self) -> dict:
        rows = self.rows()
        missing = edl.missing_tools()
        self.update_status()
        return {
            "rows": rows,
            "sort": list(self.sort),
            "status": self.status,
            "have_eps": bool(self.eps),
            "have_files": sum(1 for r in rows if r["edl"]),
            "have_online": sum(1 for e in self.eps
                               if e.online_intro or e.online_outro or e.online_recap),
            "have_chapters": sum(1 for e in self.eps if e.chapters),
            "tools_missing": missing,
            "tools_hint": edl.install_hint(missing) if missing else "",
            "keep_first": self.keep_first,
            "outro_to_end": self.outro_to_end,
            "trim_tail": self.trim_tail,
            "trim_min": trim.TAIL_MIN_S,
            "track": self.track,
            "tracks": [EDL_TRACK_AUTO] + self.langs,
            "track_info": self.track_info(),
            "pad": dict(self.pad),
            "window": dict(self.window),
            "window_default": edl.INTRO_WINDOW,
            "outro_end_active": self.outro_end_active(),
            "chapters_with_edl": self.chapters_with_edl,
            "chapters_force": self.chapters_force,
            "online_status": self.online_status,
            "frame_step": edl.FRAME_STEP,
        }


def tab() -> EdlTab:
    return current_app.config["STATE"].tabs["edl"]


def setup(state) -> None:
    state.add_tab("edl", EdlTab(state))


def _idle() -> EdlTab:
    t = tab()
    t.st.jobs.ensure_idle()
    return t


def _ok():
    tab().st.touch()
    return jsonify({"ok": True})


# ------------------------------------------------------------ настройки --
@bp.post("/api/edl/settings")
def settings():
    t = _idle()
    data = replies.body()
    for key in ("keep_first", "outro_to_end", "trim_tail", "chapters_with_edl", "chapters_force"):
        if key in data:
            setattr(t, key, bool(data[key]))
    if data.get("track") in [EDL_TRACK_AUTO] + t.langs:
        t.track = data["track"]
    for key, value in (data.get("pad") or {}).items():
        if key in t.pad:
            t.pad[key] = str(value).strip() or "0"
    for key, value in (data.get("window") or {}).items():
        if key in t.window:
            t.window[key] = str(value).strip()
    t.save_settings()
    return _ok()


@bp.post("/api/edl/sort")
def sort():
    """Клик по заголовку: сортировка по колонке, повторный клик — наоборот.

    Пустые значения при прямом порядке уходят вниз, при обратном поднимаются
    наверх — так серии без найденного интро собираются в кучу одним кликом.
    """
    t = _idle()
    col = str(replies.body().get("col", ""))
    key = _SORT_KEYS.get(col)
    if key is None or not t.eps:
        return _ok()
    prev_col, prev_rev = t.sort
    t.sort = (col, not prev_rev if col == prev_col else False)
    t.eps.sort(key=key, reverse=t.sort[1])
    return _ok()


# ------------------------------------------------------- задать вручную --
@bp.post("/api/edl/manual")
def manual():
    """Значения «Задать вручную» — ко всем или к выделенным сериям."""
    t = _idle()
    data = replies.body()
    op = str(data.get("op", ""))
    v = data.get("values") or {}
    log = t.st.log.add

    if op == "calc_outro_last":
        return _calc_outro_last(t, data, v)

    eps = t.scope(data)
    word = t.scope_word(data, len(eps))
    if op == "intro":
        s, e_ = parse_time(v.get("intro_start")), parse_time(v.get("intro_end"))
        if s is None or e_ is None or e_ <= s:
            raise Info("Интро", "Укажите начало и конец интро (например 0:00 и 0:13).")
        for ep in eps:
            ep.intro = edl.Segment(s, e_)
        log(f"Интро задано {word}: {fmt_time(s)}–{fmt_time(e_)}.")
    elif op == "intro_dur":
        # Конец интро = его начало + N сек. Начало у каждой серии остаётся своё
        # (из детекта или ручной правки) — серии без начала пропускаются.
        n = parse_time(v.get("intro_dur"))
        if not n or n <= 0:
            raise Info("Интро", "Укажите продолжительность интро в секундах (например 30) — "
                                "конец станет «начало + N» у серий с известным началом.")
        done = skipped = 0
        for ep in eps:
            if ep.intro:
                ep.intro = edl.Segment(ep.intro.start, ep.intro.start + n)
                done += 1
            else:
                skipped += 1
        msg = f"Длительность интро {fmt_time(n)} применена: {done} серий."
        if skipped:
            msg += f" Пропущено без начала интро: {skipped} (задайте начало двойным кликом)."
        log(msg)
    elif op == "outro":
        s = parse_time(v.get("outro_start"))
        if s is None:
            raise Info("Титры", "Укажите начало титров (например 20:30). "
                                "Конец можно не указывать — тогда до конца файла.")
        fixed = parse_time(v.get("outro_end"))
        if fixed is not None and fixed <= s:
            raise Info("Титры", "Конец титров должен быть позже начала.")
        for ep in eps:
            end = fixed if fixed is not None else (ep.duration or (s + 60))
            ep.outro = edl.Segment(s, end) if end > s else None
            ep.outro_fixed_end = ep.outro is not None and fixed is not None
        tail = f"по {fmt_time(fixed)}" if fixed is not None else "до конца файла"
        log(f"Титры заданы {word}: с {fmt_time(s)} {tail}.")
    elif op == "outro_last":
        n = parse_time(v.get("outro_last"))
        if not n or n <= 0:
            raise Info("Титры", "Укажите длительность титров в секундах (например 60) — "
                                "вырежутся последними N сек каждой серии.")
        done = 0
        for ep in eps:
            if ep.duration:
                ep.outro = edl.Segment(max(0.0, ep.duration - n), ep.duration)
                ep.outro_fixed_end = False
                done += 1
        log(f"Титры заданы как последние {fmt_time(n)} ({done} серий с известной длительностью).")
    elif op == "recap":
        x = parse_time(v.get("recap_end"))
        if not x or x <= 0:
            raise Info("Recap", "Укажите конец recap (например 0:13) — начало с начала файла.")
        for ep in eps:
            ep.recap = edl.Segment(0.0, x)
        log(f"Recap задан {word}: 0:00–{fmt_time(x)}.")
    elif op in ("clear_intro", "clear_outro", "clear_recap"):
        kind = op[len("clear_"):]
        label = {"intro": "интро", "outro": "титры", "recap": "recap"}[kind]
        for ep in eps:
            setattr(ep, kind, None)
            if kind == "outro":
                ep.outro_fixed_end = False
        log(f"Убрано «{label}» у {len(eps)} серий.")
    elif op == "clear_all":
        replies.confirm("clear", "Убрать всё?",
                        f"Сбросить интро, титры и recap у {len(eps)} серий?", yes="Сбросить",
                        danger=True)
        for ep in eps:
            ep.intro = ep.outro = ep.recap = None
            ep.outro_fixed_end = False
        log(f"Сброшены интро/титры/recap у {len(eps)} серий.")
    elif op == "take_online":
        # Переносим только то, что онлайн реально нашёл — локальные не теряются.
        n = 0
        for ep in eps:
            got = False
            if ep.online_intro:
                ep.intro = ep.online_intro
                got = True
            if ep.online_outro:
                ep.outro = ep.online_outro
                ep.outro_fixed_end = False
                got = True
            if ep.online_recap:
                ep.recap = ep.online_recap
                got = True
            n += got
        log(f"Взяты онлайн-тайминги: применены к {n} из {len(eps)} серий "
            "(где онлайн-данные есть). Для полного удаления сегмента — «Убрать».")
    elif op == "take_local":
        n = 0
        for ep in eps:
            got = False
            if ep.local_intro:
                ep.intro = ep.local_intro
                got = True
            if ep.local_outro:
                ep.outro = ep.local_outro
                ep.outro_fixed_end = False
                got = True
            if ep.local_recap:
                ep.recap = ep.local_recap
                got = True
            n += got
        log(f"Возвращены локальные тайминги у {n} из {len(eps)} серий "
            "(у которых был локальный детект).")
    else:
        raise UserError("Неизвестное действие", op)
    return _ok()


def _calc_outro_last(t: EdlTab, data: dict, v: dict):
    """Продолжительность титров = длительность серии − время их начала.

    В плеере видно, КОГДА титры начались, а поле «последние N сек» просит их
    ПРОДОЛЖИТЕЛЬНОСТЬ. Достаточно одной серии — разницу посчитаем сами.
    """
    start = parse_time(v.get("outro_from_start"))
    if start is None or start < 0:
        raise Info("Титры", "Укажите, на какой минуте пошли титры — MM:SS или секунды "
                            "(например 56:10).")
    if not t.eps:
        raise Info("Нет данных", "Сначала просканируйте папку.")
    # Серия, по которой считаем: выделенная, иначе первая с известной длительностью.
    wanted = {str(p) for p in data.get("selected") or []}
    pool = [e for e in t.eps if str(e.path) in wanted] or t.eps
    ref = next((e for e in pool if e.duration), None)
    if ref is None:
        raise Info("Нет длительности", "У этих серий неизвестна длительность — считать не от чего.")
    if start >= ref.duration:
        raise Info("Титры", f"Титры не могут начинаться позже конца серии: у «{ref.path.name}» "
                            f"длительность {fmt_time(ref.duration)}.")
    length = ref.duration - start
    text = (f"{fmt_time(ref.duration)} − {fmt_time(start)} = {fmt_time(length)} → "
            f"{length:.0f} сек")
    t.st.log.add(f"Продолжительность титров по «{ref.path.name}»: {fmt_time(ref.duration)} − "
                 f"{fmt_time(start)} = {length:.0f} сек. Подставлено в «последние» — "
                 "нажмите «Задать».")
    return jsonify({"outro_last": f"{length:.0f}", "calc": text})


# ------------------------------------------------------- правка серии --
@bp.post("/api/edl/episode/get")
def episode_get():
    t = tab()
    ep = t.by_path(replies.body().get("path"))
    return jsonify({
        "path": str(ep.path), "name": ep.path.name, "duration": ep.duration,
        "rc": fmt_time(ep.recap.end) if ep.recap else "",
        "is": fmt_time(ep.intro.start) if ep.intro else "",
        "ie": fmt_time(ep.intro.end) if ep.intro else "",
        "os": fmt_time(ep.outro.start) if ep.outro else "",
        "oe": fmt_time(ep.outro.end) if (ep.outro and ep.outro_fixed_end) else "",
    })


@bp.post("/api/edl/episode")
def episode_save():
    t = _idle()
    data = replies.body()
    e = t.by_path(data.get("path"))
    rc = parse_time(data.get("rc"))
    is_, ie = parse_time(data.get("is")), parse_time(data.get("ie"))
    os_, oe = parse_time(data.get("os")), parse_time(data.get("oe"))
    e.recap = edl.Segment(0.0, rc) if (rc is not None and rc > 0) else None
    if is_ is not None and ie is None:
        # Конец не задан — берём «начало + длительность интро» из «Задать вручную».
        dur = parse_time(data.get("intro_dur"))
        if dur and dur > 0:
            ie = is_ + dur
    e.intro = edl.Segment(is_, ie) if (is_ is not None and ie is not None and ie > is_) else None
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
    return _ok()


@bp.post("/api/edl/shift")
def shift():
    """«Сдвинуть все серии»: поправка, найденная по кадрам, — в отступ сезона."""
    t = _idle()
    data = replies.body()
    key = BOUND_PADS.get(str(data.get("bound")))
    if key is None:
        raise UserError("Нечего сдвигать", "У recap нет отступа сезона.")
    try:
        delta = float(data.get("delta"))
    except (TypeError, ValueError):
        raise UserError("Нет сдвига", "Выберите кадр.")
    new = t.pad_val(key) + delta
    t.pad[key] = f"{new:.0f}"
    t.save_settings()
    name = Path(str(data.get("path", ""))).name
    label = {"intro_start": "начало интро", "intro_end": "конец интро",
             "outro_start": "начало титров", "outro_end": "конец титров"}[key]
    t.st.log.add(f"Отступ «{label}» сдвинут на {delta:+.0f} с (теперь {new:+.0f} с) — "
                 f"по кадрам серии «{name}».")
    t.st.touch()
    return jsonify({"ok": True, "pad": t.pad[key]})


# ----------------------------------------------------------- кадры --
@bp.post("/api/edl/frames")
def frames_start():
    """Полоса кадров вокруг границы — увидеть глазами, куда она попала.

    Кадры читаются в своём потоке, не операцией: это секунды, и правке серии
    они мешать не должны. Страница спрашивает готовые по номеру полосы.
    """
    t = tab()
    data = replies.body()
    ep = t.by_path(data.get("path"))
    ffmpeg = edl.find_ffmpeg()
    if not ffmpeg:
        raise UserError("Нет ffmpeg", edl.install_hint(["ffmpeg"]))
    try:
        center = float(data.get("center"))
        step = float(data.get("step") or edl.FRAME_STEP)
    except (TypeError, ValueError):
        raise UserError("Нет границы", "Укажите время границы.")
    times = edl.frame_times(center, ep.duration, step=step)
    token = secrets.token_hex(8)
    session = {"times": times, "pngs": {}, "done": False, "stop": False, "at": time.time()}
    # Держим только последние полосы: кадры в памяти, а смотрят их один раз.
    for old in sorted(t.frames, key=lambda k: t.frames[k]["at"])[:-3]:
        t.frames[old]["stop"] = True
        t.frames.pop(old, None)
    t.frames[token] = session

    def work():
        edl.grab_frames(ffmpeg, ep.path, times,
                        on_frame=lambda i, png: session["pngs"].__setitem__(i, png),
                        stop=lambda: session["stop"])
        session["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"token": token, "times": times, "center": center})


@bp.get("/api/edl/frames/<token>")
def frames_status(token):
    session = tab().frames.get(token)
    if session is None:
        raise UserError("Кадры устарели", "Нажмите «Показать кадры» ещё раз.")
    return jsonify({"done": session["done"],
                    "ready": {i: bool(png) for i, png in session["pngs"].items()}})


@bp.get("/api/edl/frames/<token>/<int:i>.png")
def frame_png(token, i):
    session = tab().frames.get(token)
    png = session["pngs"].get(i) if session else None
    if not png:
        return ("", 404)
    return send_file(io.BytesIO(png), mimetype="image/png", max_age=3600)


@bp.post("/api/edl/player")
def player():
    """Открыть серию в плеере на секунде — только на своей машине."""
    t = tab()
    if t.st.mode != "desktop":
        raise UserError("Плеер недоступен", "Программа работает на сервере — плеера рядом нет.")
    data = replies.body()
    ep = t.by_path(data.get("path"))
    try:
        at = float(data.get("at"))
    except (TypeError, ValueError):
        raise UserError("Нет времени", "Выберите кадр или задайте границу.")
    name = edl.open_in_player(ep.path, at)
    if not name:
        raise UserError("Плеер не найден", "Нужен IINA, mpv или VLC.")
    return jsonify({"player": name, "at": fmt_time(at)})


# ------------------------------------------------------------ детект --
@bp.post("/api/edl/detect")
def detect():
    t = _idle()
    st = t.st
    data = replies.body()
    scope = t.scope(data)
    missing = edl.missing_tools()
    if missing:
        raise UserError("Нет инструментов для детекта", edl.install_hint(missing))
    # Детект ищет то, что повторяется между сериями, поэтому по выделению он
    # сравнивает серии ТОЛЬКО внутри выделенного. Одной серии для этого мало.
    if len(scope) < 2:
        raise Info("Мало серий", "Детект сравнивает серии между собой — нужно хотя бы две.\n"
                                 "Выделите больше строк или переключите «Применять к: ко всем сериям».")
    fpcalc, ffmpeg = edl.find_fpcalc(), edl.find_ffmpeg()
    # Свежий прогон — свежие заметки: detect_season дописывает через «; ».
    for e in scope:
        e.note = ""
    seasons = edl.group_by_season([e.path for e in scope])
    by_path = {str(e.path): e for e in scope}
    total = len(scope) * 2                                    # intro + outro
    prefer = t.prefer_lang()
    win_in, win_out = t.window_val("intro"), t.window_val("outro")
    st.log.add(f"АВТОДЕТЕКТ по {t.scope_word(data, len(scope))}: сезонов {len(seasons)}, "
               f"дорожка: {t.track}, окно {win_in:.0f}/{win_out:.0f} с…")
    # Отпечатки переживают прогон: повторный детект идёт из кэша, не вычитывая
    # гигабайты с диска или сетевой шары заново.
    cache = paths.cache_dir() / "fingerprints"
    stats: dict = {}

    def work(job):
        done = [0]

        def progress(kind, i, n, path):
            done[0] += 1
            job.progress(done[0], total,
                         f"{EDL_KIND_RU.get(kind, kind)} {done[0]}/{total}: {Path(path).name}")

        job.progress(0, total, "чтение звука…")
        edl.prune_cache(cache)
        for season, eps_paths in sorted(seasons.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
            if job.cancelled():
                break
            eps = [by_path[str(p)] for p in eps_paths]
            edl.detect_season(eps, fpcalc, ffmpeg, prefer_lang=prefer, progress=progress,
                              stop=job.cancelled, intro_window=win_in, outro_window=win_out,
                              cache_dir=cache, stats=stats)

    def done(job, _):
        fi = sum(1 for e in scope if e.intro)
        fo = sum(1 for e in scope if e.outro)
        n = len(scope)
        cached = (f" Отпечатков из кэша: {stats.get('cached', 0)} из {stats['total']}."
                  if stats.get("total") else "")
        if job.cancelled():
            msg = f"Детект отменён. Найдено до отмены: интро {fi}/{n}, титры {fo}/{n}.{cached}"
        else:
            msg = f"Детект готов: интро {fi}/{n}, титры {fo}/{n}.{cached}"
        st.log.add(msg + ("" if job.cancelled() else " Проверьте таблицу и при нужде поправьте."))
        job.summary = msg
        # Снимок локального детекта — чтобы «Вернуть локальные» восстанавливал именно
        # его, даже если активные значения потом заменили онлайновыми.
        for e in scope:
            e.local_intro, e.local_outro, e.local_recap = e.intro, e.outro, e.recap

    job = st.jobs.start("Определение заставок", work, on_done=done)
    return jsonify({"job": job.to_dict()})


# ------------------------------------------------------ главы Matroska --
def _chapters_plan(t: EdlTab, scope):
    """Что и куда писать: (mkvpropedit, план, пропущенные).

    Пропущенные — файлы с чужими главами: mkvpropedit заменяет набор целиком,
    и деление фильма на сцены пропало бы безвозвратно. Берём их в работу,
    только когда это разрешено галкой.
    """
    propedit = core.find_tool("mkvpropedit")
    extract = core.find_tool("mkvextract")
    if not (propedit and extract):
        raise UserError("Нет MKVToolNix", "Для глав нужны mkvpropedit и mkvextract из MKVToolNix.\n"
                        + ("choco install mkvtoolnix" if sys.platform == "win32"
                           else "brew install mkvtoolnix"))
    keep, to_end, pad = t.keep_first, t.outro_to_end, t.padding()
    plan, skipped = [], []
    for e in scope:
        points = chapters.build_points(e, pad, keep, to_end)
        if not points:
            continue
        # Глав нет — проверять нечего; иначе смотрим, наша ли это разметка.
        state = chapters.state(e.path, extract) if e.chapters else "none"
        if state == "foreign" and not t.chapters_force:
            skipped.append(e)
        else:
            plan.append((e, points))
    return propedit, plan, skipped


@bp.post("/api/edl/chapters")
def write_chapters():
    t = _idle()
    st = t.st
    data = replies.body()
    scope = t.scope(data)
    propedit, plan, skipped = _chapters_plan(t, scope)
    if not plan:
        raise Info("Нечего размечать",
                   "Нет ни интро, ни титров, ни recap — главы строить не из чего."
                   if not skipped else
                   f"У всех серий ({len(skipped)}) свои главы, они не тронуты.\n"
                   "Чтобы заменить их, включите «перезаписывать чужие главы».")
    tail = f"\nПропущено с чужими главами: {len(skipped)}." if skipped else ""
    replies.confirm("go", "Запись глав",
                    f"Записать главы в {len(plan)} файлов — по {t.scope_phrase(data)}?\n"
                    "Правится только заголовок MKV, видео не перекодируется." + tail,
                    yes="Записать")

    def work(job):
        ok = 0
        for n, (ep, points) in enumerate(plan, 1):
            if job.cancelled():
                break
            job.progress(n - 1, len(plan), f"{n}/{len(plan)}: {ep.path.name}")
            if chapters.write_chapters(ep.path, points, propedit):
                ep.chapters = len(points)
                ok += 1
                st.log.add(f"  ✓ {ep.path.name}: глав {len(points)}")
            else:
                st.log.add(f"  ✗ {ep.path.name}: записать не удалось")
            st.touch()
        st.log.add(f"Главы записаны: {ok}.")
        job.summary = f"Готово: главы записаны у {ok} из {len(plan)}"

    job = st.jobs.start("Запись глав", work)
    return jsonify({"job": job.to_dict()})


@bp.post("/api/edl/chapters/clear")
def clear_chapters():
    """Убирает ТОЛЬКО свою разметку: чужие главы не наши, чтобы их удалять."""
    t = _idle()
    st = t.st
    data = replies.body()
    scope = t.scope(data)
    propedit = core.find_tool("mkvpropedit")
    extract = core.find_tool("mkvextract")
    if not (propedit and extract):
        raise UserError("Нет MKVToolNix", "Нужны mkvpropedit и mkvextract.")
    ours = [e for e in scope if e.chapters and chapters.state(e.path, extract) == "ours"]
    if not ours:
        raise Info("Нет наших глав", "Среди этих серий нет файлов с нашей разметкой. "
                                     "Чужие главы кнопка не трогает.")
    replies.confirm("go", "Удаление глав", f"Убрать главы из {len(ours)} файлов?",
                    yes="Убрать", danger=True)

    def work(job):
        gone = 0
        for n, e in enumerate(ours, 1):
            if job.cancelled():
                break
            job.progress(n - 1, len(ours), f"{n}/{len(ours)}: {e.path.name}")
            if chapters.clear_chapters(e.path, propedit):
                e.chapters = 0
                gone += 1
        st.log.add(f"Главы убраны: {gone}.")
        job.summary = f"Главы убраны: {gone}"

    job = st.jobs.start("Удаление глав", work)
    return jsonify({"job": job.to_dict()})


@bp.post("/api/edl/delete")
def delete_edl():
    t = _idle()
    data = replies.body()
    scope = t.scope(data)
    existing = [e for e in scope if edl.has_external_edl(e.path)]
    if not existing:
        raise Info("Нет .edl", f"Рядом с этими сериями ({t.scope_phrase(data)}) нет .edl файлов.")
    replies.confirm("go", "Удаление .edl",
                    f"Удалить {len(existing)} файлов .edl — по {t.scope_phrase(data)}?",
                    yes="Удалить", danger=True)
    n = sum(1 for e in existing if edl.delete_edl(e.path))
    t.st.log.add(f"Удалено .edl: {n}.")
    return _ok()


# ----------------------------------------------------------- запись .edl --
@bp.post("/api/edl/write")
def write_edl():
    t = _idle()
    st = t.st
    data = replies.body()
    scope = t.scope(data)
    keep, to_end, pad = t.keep_first, t.outro_to_end, t.padding()
    to_write = [e for e in scope
                if edl.apply_padding(edl.effective_intro(e, keep), pad.intro_start, pad.intro_end,
                                     e.duration)
                or edl.final_outro(e, pad, to_end) or e.recap]
    if not to_write:
        raise Info("Нечего записывать", "Нет ни интро, ни титров. Сначала «Определить "
                                        "автоматически» или задайте вручную.")
    prepared = _chapters_plan(t, scope) if t.chapters_with_edl else None
    # Хвост режется до записи .edl этой серии: конец титров в .edl и главах
    # должен лечь на новый конец файла, а не на старый, за концом видео.
    to_trim = [e for e in scope if trim.needs_trim(e.tail)] if t.trim_tail else []
    tools = None
    if to_trim:
        tools, missing = trim.find_tools()
        if tools is None:
            raise UserError("Нечем обрезать хвост",
                            f"Не найдены: {', '.join(missing)}.\n"
                            + ("Установите ffmpeg и MKVToolNix и добавьте их в PATH."
                               if sys.platform == "win32" else "brew install ffmpeg mkvtoolnix")
                            + "\n\nИли снимите галку «Обрезать хвост после конца видео».")
    extra = ""
    if prepared:
        _, plan, skipped = prepared
        extra = f"\nЗаодно главы в MKV: {len(plan)} файлов."
        if skipped:
            extra += f" Пропущено с чужими главами: {len(skipped)}."
    if to_trim:
        longest = max(e.tail for e in to_trim)
        extra += (f"\n\nУ {len(to_trim)} файлов звук идёт дальше картинки (до {longest:.0f} с) — "
                  "перед записью их .edl хвост обрезается. Без перекодирования, но каждый файл "
                  "переписывается целиком — несколько минут на серию. Оригинал заменяется только "
                  "после сверки с ним.")
    replies.confirm("go", "Запись .edl",
                    f"Записать {len(to_write)} файлов .edl — по {t.scope_phrase(data)}?\n"
                    "Существующие .edl будут перезаписаны."
                    + ("" if to_trim else " Видео не трогается.") + extra,
                    yes="Записать")
    job = _run_edl(t, scope, pad, keep, to_end, tools, to_trim, prepared)
    return jsonify({"job": job.to_dict()})


def _run_edl(t: EdlTab, scope, pad, keep, to_end, tools, to_trim, prepared):
    """Пишет .edl в фоне — серия за серией (pipeline.run_episode).

    Серия с хвостом сначала обрезается, и сразу за этим пишутся её .edl и
    главы; только потом берёмся за следующую. Обрезка занимает минуты, поэтому
    прогресс идёт долями внутри файла, а обрезанная серия пересканируется —
    длительность и хвост в таблице сразу новые. Оборвалась сеть — ждём её и
    повторяем шаг. Отмена не начинает следующую серию, а обрезку посреди серии
    бросает, оставляя файл как был.
    """
    st = t.st
    log = st.log.add
    trim_set = {str(e.path) for e in to_trim}
    propedit, planned = ((prepared[0], {str(e.path) for e, _ in prepared[1]})
                         if prepared else (None, set()))
    # Обрезка — минуты, .edl и главы — секунды: вес в полосе прогресса по этому.
    weights = [100 if str(e.path) in trim_set else 1 for e in scope]
    total = sum(weights)
    tally = {"edl": 0, "edl_failed": 0, "trimmed": 0, "trim_failed": 0,
             "chapters": 0, "chapters_failed": 0, "seen": 0}
    # Время без сети в оценку «осталось» не входит: иначе после получаса
    # ожидания она бы раздулась до конца пачки.
    clock = {"started": time.monotonic(), "offline_since": None}

    def work(job):
        def show(n, frac, offline=""):
            done = (sum(weights[:n]) + frac * weights[n]) / total
            text = f"Серия {n + 1}/{len(scope)}"
            if offline:
                text += f" · {offline} · нет доступа к файлу — жду сеть"
            elif str(scope[n].path) in trim_set and frac < 1:
                stage = "перепаковка" if frac < trim.REMUX_SHARE else "проверка"
                text += f" · обрезка хвоста · {stage} · {frac * 100:.0f}%"
            else:
                text += " · запись .edl" + (" и глав" if str(scope[n].path) in planned else "")
            elapsed = time.monotonic() - clock["started"]
            # Первые секунды оценка скачет — показываем её, когда есть на что опереться.
            if not offline and done > 0.02 and elapsed > 20:
                left = elapsed * (1 - done) / done
                text += " · осталось " + (f"~{math.ceil(left / 60)} мин" if left >= 60 else "<1 мин")
            job.progress(done * total, total, text)

        def waiting(on, step, n, ep):
            what, note = _NET_STEPS[step]
            if on:
                clock["offline_since"] = time.monotonic()
                show(n, 0.0, offline=what)
                log(f"  ⚠ {ep.path.name}: пропал доступ к файлу ({what}) — похоже, отвалилась "
                    f"сеть. {note}Жду связь и повторю. Не ждать — «Отмена».")
            else:
                if clock["offline_since"] is not None:
                    clock["started"] += time.monotonic() - clock["offline_since"]
                    clock["offline_since"] = None
                log(f"  … {ep.path.name}: связь вернулась, {what} заново")
                show(n, 0.0)

        def trimmed(run, ep):
            """Обрезка серии кончилась — в журнал и в таблицу, не дожидаясь .edl."""
            name, res = ep.path.name, run.trim
            if res.ok and not res.skipped:
                tally["trimmed"] += 1
                fresh = run.fresh
                if fresh is not None and not fresh.error:
                    st.replace_file(fresh)
                    ep.duration = fresh.duration
                    ep.chapters = fresh.chapters
                    ep.tail = trim.tail_seconds(fresh.tracks)
                log(f"  ✓ {name}: {res.message}, {fmt_time(res.old_duration)} → "
                    f"{fmt_time(res.new_duration)}")
            elif res.ok or res.cancelled:
                log(f"  — {name}: {res.message}")
            else:
                tally["trim_failed"] += 1
                log(f"  ✗ {name}: {res.message} — оригинал не тронут")
            st.touch()

        for n, ep in enumerate(scope):
            if job.cancelled():
                break
            if str(ep.path) in trim_set:
                log(f"  … {ep.path.name}: хвост {ep.tail:.0f} с, обрезаю")
            show(n, 0.0)
            run = pipeline.run_episode(
                ep, pad, keep, to_end,
                tools=tools if str(ep.path) in trim_set else None,
                propedit=propedit if str(ep.path) in planned else None,
                progress=lambda frac, n=n: show(n, frac),
                cancel=job.cancelled,
                waiting=lambda on, step, n=n, ep=ep: waiting(on, step, n, ep),
                trimmed=lambda run, ep=ep: trimmed(run, ep))
            tally["seen"] += 1
            name = ep.path.name
            if run.edl:
                tally["edl"] += 1
                log(f"  ✓ {run.edl.name}")
            elif run.edl_error:
                tally["edl_failed"] += 1
                log(f"  ✗ {edl.edl_path(ep.path).name}: {run.edl_error}")
            if run.chapters is not None:
                tally["chapters"] += 1
                ep.chapters = run.chapters
                log(f"  ✓ {name}: глав {run.chapters}")
            elif run.chapters_failed:
                tally["chapters_failed"] += 1
                log(f"  ✗ {name}: главы записать не удалось")
            show(n, 1.0)
            st.touch()
            if run.cancelled:
                break

        cancelled = job.cancelled()
        parts = [f".edl записано {tally['edl']}"]
        if trim_set:
            parts.append(f"хвост обрезан у {tally['trimmed']} из {len(trim_set)}")
        if planned:
            parts.append(f"главы у {tally['chapters']}")
        failed = [f"{what} {tally[key]}" for key, what in
                  (("edl_failed", ".edl"), ("trim_failed", "обрезка"),
                   ("chapters_failed", "главы")) if tally[key]]
        text = ", ".join(parts) + (f"; не вышло: {', '.join(failed)}" if failed else "")
        if cancelled and tally["seen"] < len(scope):
            text += f"; не начаты: {len(scope) - tally['seen']}"
        text = ("Отменено: " if cancelled else "Готово: ") + text
        log(text + ".")
        if tally["edl_failed"]:
            log("Не записанные .edl — проверьте доступ к папке и запишите ещё раз.")
        job.summary = text
        t.save_settings()

    return st.jobs.start("Запись .edl", work)


# -------------------------------------------------- онлайн-тайминги --
def _episode_range(t: EdlTab):
    eps_se = [e for e in t.eps if e.episode is not None]
    if not eps_se:
        raise Info("Нет номеров серий",
                   "У файлов не распознаны S/E (SxxEyy) — онлайн-сопоставление невозможно.")
    return eps_se, min(e.episode for e in eps_se), max(e.episode for e in eps_se)


@bp.post("/api/edl/online/rules")
def online_rules():
    """Сохранённые правила для папки (или одно пустое) и найденный диапазон серий."""
    t = _idle()
    if not t.eps:
        raise Info("Нет данных", "Сначала просканируйте папку.")
    _, emin, emax = _episode_range(t)
    saved = load_app_settings().get("online_rules", {}).get(t.st.path)
    rules = [{"source": r.get("source", "AniSkip"), "id": str(r.get("id", "")),
              "from": r.get("from", emin), "to": r.get("to", emax), "first": r.get("first", 1)}
             for r in saved] if saved else [
        {"source": "AniSkip", "id": "", "from": emin, "to": emax, "first": 1}]
    return jsonify({"rules": rules, "emin": emin, "emax": emax})


@bp.post("/api/edl/online/suggest")
def online_suggest():
    """Подбор ID сразу для обоих источников по названию и метаданным.

    TheIntroDB — ID сериала из tvshow.nfo (tmdb→imdb), иначе TVmaze по названию;
    AniSkip — MAL из tvshow.nfo, иначе поиск Jikan по названию с авто-разбивкой
    на cours (в MAL один тайтл = один cour, поэтому 48 серий = два MAL ID).
    """
    import online as onlinemod

    t = _idle()
    eps_se, emin, emax = _episode_range(t)
    title = onlinemod.clean_show_title(Path(t.st.path).name)
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
            t.st.log.add(f"MAL-поиск не удался: {ex}")
        if mal_cands:
            mal_src = "Jikan (по названию)"
            seasons = sorted([c for c in mal_cands if c.episodes], key=lambda c: (c.year or 9999))
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
        raise Info("Не найдено",
                   "Не удалось подобрать ID ни из tvshow.nfo, ни по названию.\n"
                   "Введите ID вручную: TMDb со страницы themoviedb.org/tv/<id>, "
                   "MAL — с myanimelist.net.")
    rules = []
    if idb_id:
        rules.append({"source": "TheIntroDB", "id": str(idb_id), "from": emin, "to": emax, "first": 1})
    for mid, a, b, first in aniskip_rules:
        rules.append({"source": "AniSkip", "id": str(mid), "from": a, "to": b, "first": first})
    summary = []
    if idb_id:
        summary.append(f"TheIntroDB {idb_id} [{idb_src}]")
    if len(aniskip_rules) > 1:
        summary.append("AniSkip " + ", ".join(
            f"E{a:02d}–E{b:02d}→MAL {mid}" for mid, a, b, _ in aniskip_rules) + f" [{mal_src}]")
    elif aniskip_rules:
        summary.append(f"AniSkip MAL {aniskip_rules[0][0]} [{mal_src}]")
    t.online_status = "Подобрано (проверьте): " + "; ".join(summary)
    t.st.touch()
    return jsonify({"rules": rules, "summary": t.online_status,
                    "candidates": [c.label() for c in mal_cands[:5]]})


def _rules_from(data) -> list[dict]:
    out = []
    for r in data.get("rules") or []:
        idv = str(r.get("id", "")).strip()
        if not idv:
            continue
        try:
            ef, et, fr = int(float(r.get("from"))), int(float(r.get("to"))), int(float(r.get("first")))
        except (TypeError, ValueError):
            continue
        src = r.get("source") if r.get("source") in ("AniSkip", "TheIntroDB") else "AniSkip"
        out.append({"source": src, "id": idv, "from": ef, "to": et, "first": fr})
    return out


def _fetch_one(onlinemod, ep, rule):
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


@bp.post("/api/edl/online/load")
def online_load():
    import online as onlinemod

    t = _idle()
    st = t.st
    rules = _rules_from(replies.body())
    if not rules:
        raise Info("Нет правил", "Заполните хотя бы одно правило с ID.")
    data = load_app_settings()
    data.setdefault("online_rules", {})[st.path] = rules
    update_app_settings(online_rules=data["online_rules"])

    def match(ep):
        if ep.episode is None:
            return None
        return next((r for r in rules if r["from"] <= ep.episode <= r["to"]), None)

    targets = [(e, match(e)) for e in t.eps]
    targets = [(e, r) for e, r in targets if r is not None]
    if not targets:
        raise Info("Нечего загружать", "Ни одна серия не попала под правила (проверьте диапазоны E).")
    t.online_status = ""
    st.log.add(f"ОНЛАЙН: запрашиваю тайминги для {len(targets)} серий…")

    def work(job):
        ok = miss = err = 0
        for i, (ep, rule) in enumerate(targets, 1):
            if job.cancelled():
                break
            job.progress(i - 1, len(targets), f"{i}/{len(targets)}: {ep.path.name}")
            try:
                res = _fetch_one(onlinemod, ep, rule)
                ep.online_intro, ep.online_outro, ep.online_recap = res.intro, res.outro, res.recap
                ep.online_note = res.source + (f": {res.note}" if res.note else "")
                ok += 1 if res.any else 0
                miss += 0 if res.any else 1
            except onlinemod.OnlineError as ex:
                ep.online_note = f"ошибка: {ex}"
                err += 1
            st.touch()
            time.sleep(0.4)  # мягкий rate-limit (Jikan/AniSkip/TheIntroDB)
        msg = f"Онлайн: с таймингами {ok}, пусто {miss}, ошибок {err}."
        if job.cancelled():
            msg = "Отменено. " + msg
        t.online_status = msg
        job.summary = msg
        st.log.add(msg + " Сравните колонки и при нужде «Взять онлайн».")

    job = st.jobs.start("Загрузка онлайн-таймингов", work)
    return jsonify({"job": job.to_dict()})
