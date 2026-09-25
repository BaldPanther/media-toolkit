"""Вкладка «Медиатека»: опознание тайтла, картинки, план раскладки, применение.

Логика — в `library`, `metadata`, `artwork`, `nfo`; здесь состояние выбора,
предпросмотр плана с записями .nfo и картинок, подтверждения и запуск в фоне.
"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, jsonify

import artwork
import core
import edl
import library
import metaconf
import metadata
import nfo
from web import replies
from web.replies import Info, UserError

bp = Blueprint("library", __name__)

KIND_CHOICES = {"auto": None, "movie": library.MOVIE, "tv": library.TV}

ART_ROWS = (
    (metadata.ART_POSTER, "Постер"),
    (metadata.ART_FANART, "Фанарт"),
    (metadata.ART_LOGO, "Логотип"),
)
ART_TITLES = {metadata.ART_POSTER: "Постер", metadata.ART_FANART: "Фанарт",
              metadata.ART_LOGO: "Логотип", metadata.ART_SEASON: "Постер сезона"}

# Выбор языка сразу для всех видов арта. «как в настройках» — приоритет из
# meta_settings.json, остальное поднимает выбранный язык на первое место.
ART_LANG_CHOICES = {
    "settings": ("как в настройках", None),
    "ru": ("русский", "ru"),
    "en": ("английский", "en"),
    "orig": ("язык оригинала", metaconf.ART_ORIGINAL),
    "none": ("без текста", ""),
}

# Язык имени для одного тайтла. «как в настройках» — исключения нет,
# остальное кладётся в `name_overrides` и переживает перезапуск. «Русское» —
# это язык описаний (`meta_language`); подписано так, потому что он русский.
NAME_CHOICES = {
    "settings": ("как в настройках", None),
    "en": ("английское", metaconf.NAME_EN),
    "local": ("русское", metaconf.NAME_LOCAL),
    "original": ("оригинальное", metaconf.NAME_ORIGINAL),
    "auto": ("авто по языку оригинала", metaconf.NAME_AUTO),
}

_ROW_KIND = {
    library.S_OK: "nochange",
    library.S_JUNK: "warn",
    library.S_NOEP: "warn",
    library.S_CONFLICT: "error",
    library.S_CROSSDEV: "error",
    library.S_COLLECTION: "error",
}


def _rel(path: Path | None, base: Path) -> str:
    if path is None:
        return "—"
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _candidate(c: metadata.ArtCandidate | None) -> dict | None:
    if c is None:
        return None
    return {"thumb": c.thumb_url, "url": c.url, "label": c.label(), "lang": c.lang,
            "png": c.is_png}


class LibraryTab:
    def __init__(self, state):
        self.st = state
        self.settings = metaconf.load_settings()
        self.kind_choice = "auto"
        self.query = ""
        self.year = ""
        self._auto_query = ""          # что подставили сами — это можно перезаписать
        self._last_path = state.path
        self.hits: list[metadata.SearchHit] = []
        self.hit: metadata.SearchHit | None = None
        self.info: metadata.MediaInfo | None = None
        self.plan: library.Plan | None = None
        self.chosen: dict[tuple[str, int | None], metadata.ArtCandidate] = {}
        self.overrides: dict[Path, tuple[int, int]] = {}
        self.art_lang = "settings"
        self.season: int | None = None
        self.plan_status = ""          # базовый текст; записи дописывает _write_preview
        self.status = ""
        self.pending: list[library.Pending] = []
        self.system_junk: list[Path] = []
        self.fill_from_path()

    # ----------------------------------------------------------- события --
    def on_path_changed(self, path: str) -> None:
        """Путь наверху сменился — подставить название и год из его имени."""
        if path == self._last_path:
            return
        self._last_path = path
        # Папка другая — всё, что нашлось для прежней, к ней отношения не имеет.
        self.reset_title()
        self.fill_from_path()

    def on_scan_requested(self, path: str) -> None:
        self.fill_from_path()

    def on_settings_changed(self) -> None:
        self.settings = metaconf.load_settings()
        # Тайтл уже загружен — показать новые язык имени и приоритет картинок сразу.
        if self.info is not None:
            self.auto_choose_art()
            self.refresh_name_language()

    # ----------------------------------------------------------- помощники --
    def folder(self) -> Path | None:
        return self.st.target()

    def need_folder(self) -> Path:
        folder = self.folder()
        if folder is None:
            raise UserError("Нет папки", "Сначала укажите папку в поле наверху.")
        return folder

    def need_key(self) -> None:
        if not self.settings.has_tmdb():
            raise UserError("Нет ключа TMDb",
                            "Метаданные берутся с themoviedb.org — нужен бесплатный ключ.\n"
                            "Впишите его в «Настройках» → «Ключи API».")

    def kind(self) -> str:
        chosen = KIND_CHOICES.get(self.kind_choice)
        if chosen:
            return chosen
        folder = self.folder()
        return library.guess_kind(folder, self.settings) if folder else library.TV

    def reset_title(self) -> None:
        """Забыть прежний тайтл: картинки, таблицу, выбор и язык имени."""
        self.hits = []
        self.hit = None
        self.info = None
        self.plan = None
        self.chosen.clear()
        self.overrides.clear()
        self.season = None
        self.plan_status = ""
        self.status = ""

    def fill_from_path(self) -> None:
        """Подставить название из текущего пути, если поле не занято вручную."""
        target = self.folder()
        if target is None:
            return
        # Если в поле лежит наш же прежний автоподбор — заменяем. Если название
        # правили руками, оставляем как есть.
        if self.query.strip() not in ("", self._auto_query):
            return
        self.apply_guess(target)

    def apply_guess(self, path: Path) -> None:
        # У одиночного файла фильма расширение в название попасть не должно.
        name = path.stem if path.is_file() else path.name
        title, year = library.guess_title_year(name)
        self.query = title
        self.year = str(year) if year else ""
        self._auto_query = title

    # ------------------------------------------------------ язык имени --
    def name_choice(self) -> str:
        if self.info is None:
            return "settings"
        saved = self.settings.name_overrides.get(
            metaconf.title_key(self.info.kind, self.info.tmdb_id))
        return next((k for k, (_, v) in NAME_CHOICES.items() if v == saved), "settings")

    def resolve_name_language(self) -> None:
        info = self.info
        if info is not None:
            info.name_language = metadata.resolve_name_language(
                metaconf.name_language_for(self.settings, info.kind, info.tmdb_id),
                info.original_language, self.settings.meta_language)

    def refresh_name_language(self) -> None:
        """Пересчитать язык имени текущего тайтла по настройкам и обновить план.

        Зовётся и после правки настроек: иначе смена общего режима ничего бы не
        делала до следующей загрузки метаданных.
        """
        if self.info is not None:
            self.resolve_name_language()
            self.build_plan(quiet=True)

    # --------------------------------------------------------------- арт --
    def art_languages(self) -> list[str]:
        """Приоритет языков картинок с учётом выбора на вкладке.

        Выбранный язык поднимается наверх, остальные из настроек остаются
        запасными — иначе у тайтла без русского постера не нашлось бы ничего.
        """
        code = ART_LANG_CHOICES.get(self.art_lang, ("", None))[1]
        base = list(self.settings.art_languages)
        if code is not None:
            base = [code] + [lang for lang in base if lang != code]
        return self._resolve_art_original(base)

    def _resolve_art_original(self, langs: list[str]) -> list[str]:
        """Псевдоязык «оригинал» → код языка оригинала тайтла."""
        original = self.info.original_language if self.info else ""
        out: list[str] = []
        for lang in langs:
            if lang == metaconf.ART_ORIGINAL:
                if not original:
                    continue
                lang = original
            if lang not in out:
                out.append(lang)
        return out

    def seasons(self) -> list[int]:
        return metadata.seasons_with_art(self.info.art) if self.info else []

    def auto_choose_art(self) -> None:
        self.chosen.clear()
        if self.info is None:
            return
        langs = self.art_languages()
        for kind, _ in ART_ROWS:
            best = metadata.best_art(self.info.art, kind, langs)
            if best:
                self.chosen[(kind, None)] = best
        seasons = self.seasons()
        for season in seasons:
            best = metadata.best_art(self.info.art, metadata.ART_SEASON, langs, season=season)
            if best:
                self.chosen[(metadata.ART_SEASON, season)] = best
        if self.season not in seasons:
            self.season = seasons[0] if seasons else None

    def art_pool(self, kind: str, season: int | None):
        if self.info is None:
            return []
        return metadata.rank_art(metadata.art_of(self.info.art, kind, season),
                                 self.art_languages())

    # --------------------------------------------------------------- план --
    def build_plan(self, quiet: bool = False) -> None:
        folder = self.folder()
        if folder is None or self.info is None:
            return
        self.plan = library.build_plan(folder, self.info.kind, self.info,
                                       self.settings, self.overrides)
        changed = len(self.plan.changed())
        conflicts = len(self.plan.conflicts())
        msg = f"В плане изменений: {changed}"
        if self.plan.unmatched:
            msg += f", без номера серии: {len(self.plan.unmatched)}"
        if conflicts:
            msg += f", конфликтов: {conflicts}"
        if any(r.status == library.S_COLLECTION for r in self.plan.rows):
            msg = ("Похоже на папку-сборник: внутри несколько фильмов в своих папках. "
                   "Укажите папку одного фильма.")
            self.st.log.add("⚠ " + msg)
        elif not quiet:
            self.st.log.add(msg + f". Папка тайтла: {self.plan.root}")
        self.plan_status = msg

    def is_collection(self) -> bool:
        return self.plan is not None and any(
            r.status == library.S_COLLECTION for r in self.plan.rows)

    def _will_exist(self, path: Path) -> bool:
        """Будет ли файл лежать по этому пути после применения плана."""
        target = library.norm(path)
        for row in self.plan.rows:
            if (row.dst is not None and row.action != library.A_SKIP
                    and library.norm(row.dst) == target):
                return True
        return path.exists()

    def _write_row(self, dest: Path, base: Path, policy: str, exists: bool | None = None):
        if exists is None:
            exists = self._will_exist(dest)
        if not exists:
            return ("", _rel(dest, base), "запишется", "change")
        if policy == metaconf.POLICY_MISSING:
            return ("есть", _rel(dest, base), "уже есть — пропуск", "nochange")
        if policy == metaconf.POLICY_ASK:
            return ("есть", _rel(dest, base), "спросит при применении", "warn")
        return ("есть", _rel(dest, base), "перезапишется", "change")

    def _episodes_row(self, episodes: list, policy: str):
        """Одна строка на все `.nfo` серий: их бывает и полторы сотни."""
        have = sum(1 for r in episodes if self._will_exist(r.dst.with_suffix(".nfo")))
        missing = len(episodes) - have
        label = f".nfo серий: {len(episodes)}"
        now = f"есть {have}" if have else ""
        if not have:
            return (now, label, f"запишется {missing}", "change")
        if policy == metaconf.POLICY_MISSING:
            if not missing:
                return (now, label, "уже есть — пропуск", "nochange")
            return (now, label, f"допишется {missing}", "change")
        if policy == metaconf.POLICY_ASK:
            return (now, label, "спросит при применении", "warn")
        return (now, label, f"перезапишется {have}"
                + (f", запишется {missing}" if missing else ""), "change")

    def _write_preview(self):
        """Что случится с .nfo и картинками: строки (сейчас, станет, статус, вид)."""
        if self.plan is None or self.info is None:
            return []
        policy = self.settings.existing_policy
        base = self.plan.root.parent
        rows = []
        name = "movie.nfo" if self.info.kind == library.MOVIE else "tvshow.nfo"
        rows.append(self._write_row(self.plan.root / name, base, policy))
        if self.info.kind == library.TV:
            episodes = [r for r in self.plan.rows
                        if r.what == "video" and r.dst is not None
                        and r.action != library.A_SKIP]
            if episodes:
                rows.append(self._episodes_row(episodes, policy))
        for task in artwork.plan_art(self.plan.root, self.chosen, policy,
                                     exists=self._will_exist):
            rows.append(self._write_row(task.dest, base, policy,
                                        exists=task.action != artwork.DO_DOWNLOAD))
        return rows

    def delete_junk(self) -> bool:
        return (self.settings.junk_action == metaconf.JUNK_DELETE
                and library.send_to_trash_available())

    def _dst_text(self, row: library.Row) -> str:
        """Что показать в «Станет». У мусора при галке «удалять» — «удалить»."""
        if row.what == "junk" and row.selected and self.delete_junk():
            return "удалить"
        return _rel(row.dst, self.plan.root.parent)

    def plan_rows(self) -> tuple[list[dict], str]:
        if self.plan is None:
            return [], ""
        base = self.plan.root.parent
        rows = []
        for i, row in enumerate(self.plan.rows):
            rows.append({
                "i": i,
                "junk": row.what == "junk",
                "sel": row.selected if row.what == "junk" else None,
                "video": row.what == "video" and row.src is not None,
                "now": _rel(row.src, base),
                "next": self._dst_text(row),
                "status": row.status + (f" · {row.note}" if row.note else ""),
                "kind": _ROW_KIND.get(row.status, "change"),
            })
        # Записи .nfo и картинок плана не касаются, но в предпросмотре им место:
        # иначе смена политики на «перезаписывать всё» ничем себя не выдаёт.
        writes = self._write_preview()
        for now, nxt, status, kind in writes:
            rows.append({"i": None, "junk": False, "sel": None, "video": False,
                         "now": now, "next": nxt, "status": status, "kind": kind,
                         "write": True})
        status = self.plan_status
        if status and not self.is_collection():
            pending = sum(1 for *_, kind in writes if kind != "nochange")
            status += f", записей: {pending}" if pending else ""
        return rows, status

    # ------------------------------------------------------- для страницы --
    def to_dict(self) -> dict:
        info = self.info
        rows, plan_status = self.plan_rows()
        art = []
        for kind, label in ART_ROWS + ((metadata.ART_SEASON, "Сезон"),):
            season = self.season if kind == metadata.ART_SEASON else None
            available = bool(info is not None and metadata.art_of(info.art, kind, season))
            art.append({"kind": kind, "label": label, "season": season,
                        "candidate": _candidate(self.chosen.get((kind, season))),
                        "available": available})
        return {
            "kind_choice": self.kind_choice,
            "query": self.query,
            "year": self.year,
            "hits": [{"i": i, "label": h.label(),
                      "kind": "фильм" if h.kind == library.MOVIE else "сериал",
                      "tmdb_id": h.tmdb_id, "overview": h.overview,
                      "poster": h.poster_url} for i, h in enumerate(self.hits)],
            "hit": ({"label": self.hit.label(), "tmdb_id": self.hit.tmdb_id,
                     "poster": self.hit.poster_url} if self.hit else None),
            "info": ({"folder": library.title_with_year(info.folder_title, info.year),
                      "original_language": info.original_language or "?",
                      "episodes": len(info.episodes)} if info else None),
            "name_choice": self.name_choice(),
            "name_choices": [[k, label] for k, (label, _) in NAME_CHOICES.items()],
            "art_lang": self.art_lang,
            "art_lang_choices": [[k, label] for k, (label, _) in ART_LANG_CHOICES.items()],
            "seasons": self.seasons(),
            "season": self.season,
            "art": art,
            "rows": rows,
            "plan_status": plan_status,
            "can_apply": self.plan is not None and not self.is_collection(),
            "delete_junk": self.delete_junk(),
            "send2trash": library.send_to_trash_available(),
            "policy": self.settings.existing_policy,
            "policies": [[p, metaconf.POLICY_LABELS[p]] for p in metaconf.POLICIES],
            "pending": [{"path": str(p.path), "name": p.path.name,
                         "kind": "фильм" if p.kind == library.MOVIE else "сериал",
                         "why": p.why, "is_dir": p.path.is_dir()} for p in self.pending],
            "system_junk": len(self.system_junk),
            "status": self.status,
        }


def tab() -> LibraryTab:
    return current_app.config["STATE"].tabs["library"]


def setup(state) -> None:
    state.add_tab("library", LibraryTab(state))


def _idle() -> LibraryTab:
    t = tab()
    t.st.jobs.ensure_idle()
    return t


def _ok():
    tab().st.touch()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- поиск --
@bp.post("/api/library/form")
def form():
    """Поля «Тип», «Название», «Год» — сервер помнит их между открытиями страницы."""
    t = _idle()
    data = replies.body()
    if data.get("kind") in KIND_CHOICES:
        t.kind_choice = data["kind"]
    if "query" in data:
        t.query = str(data["query"])
    if "year" in data:
        t.year = str(data["year"]).strip()
    return _ok()


@bp.post("/api/library/guess")
def guess():
    t = _idle()
    t.apply_guess(t.need_folder())
    return _ok()


def _start_fetch(t: LibraryTab, hit: metadata.SearchHit, job=None):
    """Метаданные выбранного тайтла; в своей операции или внутри текущей."""
    folder = t.need_folder()
    t.hit = hit
    t.st.log.add(f"Выбран тайтл: {hit.label()} (TMDb {hit.tmdb_id})")

    def fetch(job):
        scan = library.scan_folder(folder) if hit.kind == library.TV else None
        seasons = scan.seasons() if scan else []
        # Размеры сезонов нужны, чтобы подобрать верную разбивку в episode
        # groups, когда обычные сезоны TMDb с диском не сошлись (аниме).
        sizes = scan.season_sizes() if scan else {}
        job.progress(0, 0, "загрузка метаданных…")
        return metadata.fetch(hit.kind, hit.tmdb_id, t.settings, seasons=seasons,
                              season_sizes=sizes,
                              progress=lambda text: job.progress(text=text))

    def ready(job, info):
        t.info = info
        t.st.log.add(f"Метаданные готовы: {info.folder_title} ({info.year}), "
                     f"серий {len(info.episodes)}, вариантов арта {len(info.art)}")
        t.resolve_name_language()
        t.auto_choose_art()
        t.build_plan()

    if job is not None:
        ready(job, fetch(job))
        return None
    return t.st.jobs.start("Загрузка метаданных", fetch, on_done=ready)


@bp.post("/api/library/find")
def find():
    t = _idle()
    data = replies.body()
    if data.get("kind") in KIND_CHOICES:
        t.kind_choice = data["kind"]
    if "query" in data:
        t.query = str(data["query"])
    if "year" in data:
        t.year = str(data["year"]).strip()
    t.need_key()
    folder = t.need_folder()
    if not t.query.strip():
        t.apply_guess(folder)
    query = t.query.strip()
    if not query:
        raise UserError("Нет названия", "Впишите название для поиска.")
    year = int(t.year) if t.year.isdigit() else None
    kind = t.kind()
    t.st.log.add(f"Поиск в TMDb: {query}" + (f" ({year})" if year else ""))

    def work(job):
        job.progress(0, 0, "поиск…")
        if query.lower().startswith("tt") and query[2:].isdigit():
            hits = metadata.search_by_imdb(query, t.settings)
        else:
            hits = metadata.search(kind, query, t.settings, year=year)
        t.hits = hits
        if not hits:
            t.st.log.add("Ничего не найдено — поправьте название или вставьте IMDb-ID (tt…).")
            job.summary = "TMDb ничего не нашёл"
            return None
        if len(hits) == 1:
            _start_fetch(t, hits[0], job=job)
            job.summary = f"Выбрано: {hits[0].label()}"
        else:
            job.summary = f"Найдено вариантов: {len(hits)} — выберите нужный"
        return None

    job = t.st.jobs.start("Поиск в TMDb", work)
    return jsonify({"job": job.to_dict()})


@bp.post("/api/library/select")
def select():
    t = _idle()
    try:
        hit = t.hits[int(replies.body().get("index"))]
    except (TypeError, ValueError, IndexError):
        raise UserError("Нет такого варианта", "Выполните поиск заново.")
    job = _start_fetch(t, hit)
    return jsonify({"job": job.to_dict()})


# ------------------------------------------------------ язык и картинки --
@bp.post("/api/library/name_language")
def name_language():
    t = _idle()
    info = t.info
    if info is None:
        raise Info("Нет тайтла", "Сначала найдите тайтл.")
    choice = str(replies.body().get("choice", "settings"))
    if choice not in NAME_CHOICES:
        raise UserError("Неизвестный вариант", choice)
    label, mode = NAME_CHOICES[choice]
    key = metaconf.title_key(info.kind, info.tmdb_id)
    if mode is None:
        t.settings.name_overrides.pop(key, None)
    else:
        t.settings.name_overrides[key] = mode
    metaconf.save_settings(t.settings)
    t.refresh_name_language()
    t.st.log.add(f"Язык имени ({label}): "
                 f"{library.title_with_year(info.folder_title, info.year)}")
    return _ok()


@bp.post("/api/library/art_language")
def art_language():
    t = _idle()
    choice = str(replies.body().get("choice", "settings"))
    if choice in ART_LANG_CHOICES:
        t.art_lang = choice
        t.auto_choose_art()
    return _ok()


@bp.post("/api/library/season")
def season():
    t = _idle()
    try:
        value = int(replies.body().get("season"))
    except (TypeError, ValueError):
        value = None
    if value in t.seasons():
        t.season = value
    return _ok()


def _art_key(t: LibraryTab, data: dict):
    kind = str(data.get("kind", ""))
    if kind not in ART_TITLES:
        raise UserError("Неизвестный вид картинки", kind)
    return kind, (t.season if kind == metadata.ART_SEASON else None)


@bp.post("/api/library/art/options")
def art_options():
    """Варианты для сетки выбора — в порядке приоритета языков."""
    t = tab()
    kind, season = _art_key(t, replies.body())
    pool = t.art_pool(kind, season)
    if not pool:
        raise Info("Нет вариантов", "Для этого вида картинок ничего не нашлось.")
    chosen = t.chosen.get((kind, season))
    title = ART_TITLES[kind] + (f", сезон {season}" if season is not None else "")
    return jsonify({"title": title, "kind": kind, "items": [
        {**_candidate(c), "i": i, "chosen": c is chosen} for i, c in enumerate(pool)]})


@bp.post("/api/library/art/choose")
def art_choose():
    t = _idle()
    data = replies.body()
    kind, season = _art_key(t, data)
    pool = t.art_pool(kind, season)
    try:
        candidate = pool[int(data.get("index"))]
    except (TypeError, ValueError, IndexError):
        raise UserError("Нет такого варианта", "Откройте выбор заново.")
    t.chosen[(kind, season)] = candidate
    t.st.log.add(f"Выбрана картинка: {ART_TITLES[kind]} — {candidate.label()}")
    return _ok()


# --------------------------------------------------------------- план --
@bp.post("/api/library/plan")
def rebuild_plan():
    t = _idle()
    t.need_folder()
    if t.info is None:
        raise Info("Нет тайтла", "Сначала найдите тайтл.")
    t.build_plan()
    return _ok()


@bp.post("/api/library/options")
def options():
    """Галка «удалять мусор» и судьба существующих .nfo и картинок."""
    t = _idle()
    data = replies.body()
    if "delete_junk" in data:
        want = bool(data["delete_junk"]) and library.send_to_trash_available()
        t.settings.junk_action = metaconf.JUNK_DELETE if want else metaconf.JUNK_EXTRAS
    if data.get("policy") in metaconf.POLICIES:
        t.settings.existing_policy = data["policy"]
    metaconf.save_settings(t.settings)
    return _ok()


@bp.post("/api/library/junk")
def junk_toggle():
    t = _idle()
    try:
        row = t.plan.rows[int(replies.body().get("index"))]
    except (AttributeError, TypeError, ValueError, IndexError):
        raise UserError("Нет такой строки", "Пересчитайте план.")
    if row.what == "junk":
        row.selected = not row.selected
    return _ok()


@bp.post("/api/library/episode")
def episode():
    """Сезон и серия вручную — когда из имени файла они не прочитались."""
    t = _idle()
    data = replies.body()
    try:
        row = t.plan.rows[int(data.get("index"))]
    except (AttributeError, TypeError, ValueError, IndexError):
        raise UserError("Нет такой строки", "Пересчитайте план.")
    if row.what != "video" or row.src is None:
        raise UserError("Это не серия", "Номер задаётся только видеофайлу.")
    s, e = str(data.get("season", "")).strip(), str(data.get("episode", "")).strip()
    if s.isdigit() and e.isdigit():
        t.overrides[row.src] = (int(s), int(e))
    else:
        t.overrides.pop(row.src, None)
    t.build_plan()
    return _ok()


@bp.post("/api/library/episode/current")
def episode_current():
    """Что сейчас стоит у серии: ручной номер или прочитанный из имени."""
    t = tab()
    try:
        row = t.plan.rows[int(replies.body().get("index"))]
    except (AttributeError, TypeError, ValueError, IndexError):
        raise UserError("Нет такой строки", "Пересчитайте план.")
    season, ep = t.overrides.get(row.src, edl.parse_season_episode(row.src))
    return jsonify({"name": row.src.name,
                    "season": "" if season is None else season,
                    "episode": "" if ep is None else ep})


# ---------------------------------------------------------- применение --
def _title_watch_state(t: LibraryTab) -> dict:
    """Отметки просмотра тайтла — до того, как план тронет файлы.

    У фильма они могут лежать только в старом `Фильм (1994).nfo` от
    tinyMediaManager, а он в этом же прогоне уедет в мусор. Читаем, пока он
    на месте: всё остальное в .nfo придёт заново из TMDb, а просмотр — нет.
    """
    name = "movie.nfo" if t.info.kind == library.MOVIE else "tvshow.nfo"
    found = [r.src for r in t.plan.rows if r.src is not None and r.src.name.lower() == name]
    if t.info.kind == library.MOVIE:
        found += [r.src for r in t.plan.rows
                  if r.src is not None and r.what == "junk" and r.src.suffix.lower() == ".nfo"]
    for path in found:
        state = nfo.read_watch_state(path)
        if state:
            return state
    return {}


def _write_episode_nfo(t: LibraryTab, policy: str) -> int:
    if t.info.kind != library.TV:
        return 0
    written = 0
    for row in t.plan.rows:
        if row.what != "video" or row.dst is None or row.action == library.A_SKIP:
            continue
        season, numbers = library.parse_episodes(row.dst)
        # В одном файле может лежать несколько серий — тогда и блоков в
        # .nfo должно быть столько же, иначе Kodi потеряет остальные.
        eps = [t.info.episodes[(season, n)] for n in numbers if (season, n) in t.info.episodes]
        if not eps:
            continue
        path = row.dst.with_suffix(".nfo")
        if path.exists() and policy == metaconf.POLICY_MISSING:
            continue
        preserve = nfo.read_watch_state(path)
        text = (nfo.make_episode_nfo(t.info, eps[0], preserve=preserve) if len(eps) == 1
                else nfo.make_episodes_nfo(t.info, eps, preserve=preserve))
        nfo.write(path, text)
        written += 1
    return written


def _write_title_nfo(t: LibraryTab, policy: str, art_urls: dict, season_urls: dict,
                     preserve: dict) -> int:
    name = "movie.nfo" if t.info.kind == library.MOVIE else "tvshow.nfo"
    path = t.plan.root / name
    if path.exists() and policy == metaconf.POLICY_MISSING:
        return 0
    if t.info.kind == library.MOVIE:
        text = nfo.make_movie_nfo(t.info, art=art_urls, preserve=preserve)
    else:
        text = nfo.make_tvshow_nfo(t.info, art=art_urls, season_art=season_urls,
                                   preserve=preserve)
    nfo.write(path, text)
    return 1


@bp.post("/api/library/apply")
def apply():
    t = _idle()
    st = t.st
    if t.plan is None or t.info is None:
        raise Info("Нет плана", "Сначала найдите тайтл — план построится сам.")
    t.need_folder()
    if t.is_collection():
        raise UserError("Папка-сборник", t.plan_status)
    changed = t.plan.changed()
    junk = [r for r in changed if r.what == "junk" and r.selected]
    delete = t.delete_junk()
    action = ("удалены — в Корзину, а если у тома её нет, то насовсем"
              if delete else "перенесены в Extras")
    text = (f"Файлов будет переименовано: {len(changed) - len(junk)}\n"
            f"Посторонних файлов ({action}): {len(junk)}\n"
            f"Папка тайтла: {t.plan.root}\n\n"
            "Плюс будут записаны .nfo и скачаны картинки.")
    if t.plan.conflicts():
        text = f"⚠ Конфликтных строк: {len(t.plan.conflicts())} — они будут пропущены.\n\n" + text
    replies.confirm("apply", "Применить план?", text, yes="Применить")
    policy = t.settings.existing_policy
    if policy == metaconf.POLICY_ASK:
        policy = replies.choose("policy", "Существующие файлы",
                                "Перезаписать уже существующие .nfo и картинки?",
                                [("Отмена", None, "secondary"),
                                 ("Оставить как есть", metaconf.POLICY_MISSING, "secondary"),
                                 ("Перезаписать", metaconf.POLICY_OVERWRITE, "primary")])
    plan, recursive = t.plan, st.recursive

    def work(job):
        stop = job.cancelled
        progress = lambda i, total, item: job.progress(i, total, f"{i}/{total}: {Path(str(item)).name}")
        preserve = _title_watch_state(t)
        results = library.apply_plan(plan, delete_junk=delete, stop=stop, progress=progress)
        failed = [r for r in results if not r.ok]
        for r in failed:
            st.log.add(f"✗ {r.row.src}: {r.error}")
        # Удалённое насовсем называем поимённо: восстановить его уже неоткуда.
        for r in results:
            if r.disposal == library.DISPOSE_DELETE:
                st.log.add(f"Удалено насовсем: {r.row.src}")
        written = art_done = art_skipped = 0
        if not stop():
            job.progress(0, 0, "запись .nfo…")
            written = _write_episode_nfo(t, policy)
        if not stop():
            tasks = artwork.plan_art(plan.root, t.chosen, policy)
            art_results = artwork.download(tasks, stop=stop, progress=progress)
            art_done = sum(1 for r in art_results if r.ok and not r.skipped)
            art_skipped = sum(1 for r in art_results if r.skipped)
            for r in art_results:
                if not r.ok:
                    st.log.add(f"✗ {r.task.dest.name}: {r.error}")
            art_urls, season_urls = artwork.chosen_urls(tasks)
            # Пишется после картинок: в .nfo идут URL именно тех, что легли на диск.
            written += _write_title_nfo(t, policy, art_urls, season_urls, preserve)
        disposed = [r for r in results if r.disposal]
        parts = [f"переименовано {len(results) - len(failed) - len(disposed)}"]
        trashed = sum(1 for r in results if r.disposal == library.DISPOSE_TRASH)
        deleted = sum(1 for r in results if r.disposal == library.DISPOSE_DELETE)
        if trashed:
            parts.append(f"в Корзину {trashed}")
        if deleted:
            parts.append(f"удалено насовсем {deleted}")
        if failed:
            parts.append(f"ошибок {len(failed)}")
        parts.append(f".nfo {written}")
        parts.append(f"картинок {art_done}")
        if art_skipped:
            parts.append(f"пропущено картинок {art_skipped}")
        if stop():
            parts.append("ОТМЕНЕНО")
        summary = "Готово: " + ", ".join(parts)
        st.log.add(summary)
        job.summary = t.status = summary
        # Путь в общем поле теперь другой — иначе вкладки «Дорожки» и «EDL»
        # остались бы со старым именем папки. Меняем его сами, без сброса
        # тайтла: итог прогона должен остаться на экране.
        if plan.root.exists():
            t._last_path = str(plan.root)
            st.set_path(str(plan.root))
            try:
                job.progress(0, 0, "сканирование папки…")
                files = core.scan_folder(plan.root, recursive)
            except FileNotFoundError:
                return None                   # нет MKVToolNix — вкладкам «Дорожки»/EDL нечем
            st.summary = f"Найдено MKV: {len(files)}."
            return files
        return None

    def done(job, files):
        t.build_plan(quiet=True)
        if files is not None:
            st.apply_scan(files)

    job = st.jobs.start("Применение плана", work, on_done=done)
    return jsonify({"job": job.to_dict()})


# ------------------------------------------------ что в библиотеке не готово --
def _need_roots(t: LibraryTab, what: str) -> None:
    if not (t.settings.movies_roots or t.settings.tv_roots):
        raise Info("Не заданы корни",
                   "Укажите папки с фильмами и сериалами в «Настройках» → «Корни "
                   f"библиотеки» — по ним {what}.")


@bp.post("/api/library/pending")
def pending():
    """Пробегает по корням библиотеки и собирает необработанное."""
    t = _idle()
    _need_roots(t, "и составляется список")

    def work(job):
        job.progress(0, 0, "обход библиотеки…")
        items = library.find_unprocessed(
            t.settings, stop=job.cancelled,
            progress=lambda i, total, name: job.progress(i, total, f"{i}/{total}: {Path(str(name)).name}"))
        t.pending = items
        job.summary = f"Не обработано: {len(items)}"
        t.st.log.add(f"Обход библиотеки: не обработано {len(items)}")

    job = t.st.jobs.start("Что не обработано", work)
    return jsonify({"job": job.to_dict()})


def _pending_item(t: LibraryTab) -> library.Pending:
    path = str(replies.body().get("path", ""))
    item = next((p for p in t.pending if str(p.path) == path), None)
    if item is None:
        raise UserError("Нет в списке", "Обновите список «Что не обработано».")
    return item


@bp.post("/api/library/pending/take")
def pending_take():
    t = _idle()
    item = _pending_item(t)
    t.st.set_path(str(item.path))
    t.kind_choice = "movie" if item.kind == library.MOVIE else "tv"
    t.apply_guess(item.path)
    t.st.log.add(f"Выбрано из списка: {item.path}")
    return _ok()


@bp.post("/api/library/pending/ignore")
def pending_ignore():
    t = _idle()
    item = _pending_item(t)
    if not item.path.is_dir():
        raise Info("Нечего пометить",
                   "Это отдельный файл, а не папка — метку положить некуда.\n"
                   "Обработайте его: папка вокруг него создастся сама.")
    marker = library.write_ignore_marker(item.path)
    t.pending = [p for p in t.pending if p is not item]
    t.st.log.add(f"Папка исключена из списка: {item.path} (метка {marker.name})")
    return _ok()


# ------------------------------------------------- служебные файлы систем --
@bp.post("/api/library/system-junk")
def system_junk():
    """Thumbs.db, .DS_Store, desktop.ini, «._…» по корням — сначала найти.

    Два шага: поиск (на большой медиатеке — минуты), потом
    вопрос со сводкой (`/system-junk/delete`). Каждый поиск начинается заново.
    """
    t = _idle()
    st = t.st
    _need_roots(t, "и идёт уборка")
    t.system_junk = []

    def find(job):
        job.progress(0, 0, "поиск служебных файлов…")
        items = library.find_system_junk(
            t.settings, stop=job.cancelled,
            progress=lambda i, total, name: job.progress(i, total, f"{i}/{total}: {Path(str(name)).name}"))
        if job.cancelled():
            return
        t.system_junk = items
        if items:
            job.summary = f"Служебных файлов найдено: {len(items)}"
        else:
            st.log.add("Служебных файлов не найдено.")
            job.summary = "Служебных файлов нет"

    job = st.jobs.start("Поиск служебных файлов", find)
    return jsonify({"job": job.to_dict()})


@bp.post("/api/library/system-junk/delete")
def system_junk_delete():
    t = _idle()
    st = t.st
    items = t.system_junk
    if not items:
        raise Info("Чисто", "Служебных файлов в библиотеке нет.")
    # Группируем по виду, а не показываем список из тысячи путей: важно
    # понять, что именно уедет, а не прочитать каждый путь.
    groups: dict[str, int] = {}
    size = 0
    for path in items:
        key = ("«._…» (macOS)" if path.name.startswith(library.APPLEDOUBLE_PREFIX)
               else path.name)
        groups[key] = groups.get(key, 0) + 1
        try:
            size += path.stat().st_size
        except OSError:
            pass
    st.log.add(f"Служебных файлов найдено: {len(items)} ({size / 1048576:.1f} МБ)")
    lines = [f"  {name} — {count}" for name, count in sorted(groups.items(), key=lambda kv: -kv[1])]
    text = (f"Найдено файлов: {len(items)} ({size / 1048576:.1f} МБ)\n\n"
            + "\n".join(lines[:8]) + ("\n  …" if len(lines) > 8 else "")
            + "\n\nЭто кэши превью и настройки папок: содержимого в них нет, "
              "системы создают их заново сами.\n\n"
              "Уйдут в Корзину, а на сетевом томе — сразу и безвозвратно: Корзины у него нет. "
              "Папки, которые после этого останутся пустыми, тоже уберутся.")
    replies.confirm("delete", "Убрать служебные файлы?", text, yes="Убрать", danger=True)
    t.system_junk = []
    roots = list(t.settings.movies_roots) + list(t.settings.tv_roots)

    def delete(job):
        results = library.delete_files(
            items, stop=job.cancelled,
            progress=lambda i, total, name: job.progress(i, total, f"{i}/{total}: {Path(str(name)).name}"))
        pruned = library.prune_empty_dirs([r.path for r in results if r.ok], roots)
        failed = [r for r in results if not r.ok]
        trashed = sum(1 for r in results if r.ok and r.trashed)
        deleted = sum(1 for r in results if r.ok and not r.trashed)
        for r in failed:
            st.log.add(f"✗ {r.path}: {r.error}")
        for d in pruned:
            st.log.add(f"Убрана опустевшая папка: {d}")
        parts = []
        if trashed:
            parts.append(f"в Корзину {trashed}")
        if deleted:
            parts.append(f"удалено насовсем {deleted}")
        if pruned:
            parts.append(f"пустых папок {len(pruned)}")
        if failed:
            parts.append(f"не удалось {len(failed)}")
        summary = "Служебные файлы: " + (", ".join(parts) if parts else "ничего не тронуто")
        st.log.add(summary)
        job.summary = t.status = summary

    job = st.jobs.start("Уборка служебных файлов", delete)
    return jsonify({"job": job.to_dict()})
