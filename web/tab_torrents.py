"""Вкладка «Торренты»: поиск раздач, слежение и автоматика qBittorrent.

Логика — в `search.py`, `watchlist.py`, `autopilot.py` и `telegram.py`. Фоновые
потоки (автоматику, слежение и бота) запускает `web/server.serve` через
`start_background`: в тестах их нет.
"""
from __future__ import annotations

import collections
import time

from flask import Blueprint, current_app, jsonify

import autopilot
import kodi
import library
import metaconf
import metadata
import qbit
import search
import telegram
import watchlist
from web import replies
from web.replies import Info, UserError

bp = Blueprint("torrents", __name__)


class TorrentsTab:
    def __init__(self, state):
        self.st = state
        self.bot = telegram.Service(log=state.log.add)
        self.autopilot = autopilot.Autopilot(log=state.log.add, bot=self.bot,
                                             on_change=state.touch)
        self.watcher = watchlist.Watcher(log=state.log.add, bot=self.bot,
                                         autopilot=self.autopilot, on_change=state.touch)
        # Поиск: выбранный тайтл, его сезоны, найденные раздачи и фильтры.
        self.query = ""
        self.kind = library.TV
        self.hits: list[metadata.SearchHit] = []
        self.hit: metadata.SearchHit | None = None
        self.title: search.Title | None = None
        self.library_dir = ""
        self.have: dict[int, int] = {}
        self.seasons: list[int] = []
        self.releases: list[search.Release] = []
        self.any_year = False
        self.voice = ""
        self.search_status = ""

    def start_background(self) -> None:
        self.bot.start()
        self.autopilot.start()
        self.watcher.start()

    # ------------------------------------------------------------ поиск --
    def default_seasons(self) -> list[int]:
        """Первый сезон, которого в медиатеке нет целиком; все есть — последний."""
        if self.title is None or self.title.kind != library.TV or not self.title.seasons:
            return []
        for s in self.title.seasons:
            if s.episodes and self.have.get(s.number, 0) < s.episodes:
                return [s.number]
        return [self.title.seasons[-1].number]

    def fitting(self, prefs: search.Prefs) -> list[search.Release]:
        """Подходят тайтлу и экрану: тип, год, сезоны, HDR/DV. Качество и озвучка — потом."""
        if self.title is None:
            return []
        return [r for r in self.releases if search.fits(r, self.title, self.seasons, self.any_year)
                and search.picture_ok(r, prefs)]

    def shown(self, prefs: search.Prefs | None = None) -> list[search.Release]:
        prefs = prefs or search.load_prefs()
        fit = [r for r in self.fitting(prefs) if search.quality_of(r) == prefs.quality
               and (not self.voice or search.has_voice(r, self.voice))]
        return search.rank(fit, self.title, prefs, self.seasons) if fit else []

    def search_dict(self) -> dict:
        title = self.title
        prefs = search.load_prefs()
        fit = self.fitting(prefs)
        shown = self.shown(prefs)
        # Озвучки — из раздач выбранного качества, число раздач на кнопках
        # качества — с выбранной озвучкой: пустых кнопок не бывает.
        voices = [v for v, _ in collections.Counter(
            v for r in fit if search.quality_of(r) == prefs.quality for v in r.voices).most_common(14)]
        if self.voice and self.voice not in voices:
            voices.append(self.voice)
        per_quality = collections.Counter(
            search.quality_of(r) for r in fit if not self.voice or search.has_voice(r, self.voice))
        return {
            "query": self.query,
            "kind": self.kind,
            "hits": [{"i": i, "label": h.label(), "poster": h.poster_url, "overview": h.overview,
                      "kind": "фильм" if h.kind == library.MOVIE else "сериал"}
                     for i, h in enumerate(self.hits)],
            "title": ({"label": library.title_with_year(title.title, title.year),
                       "kind": title.kind, "names": title.names, "status": title.status,
                       "poster": self.hit.poster_url if self.hit else "",
                       "library": self.library_dir,
                       "watching": self.watcher.find(title.kind, title.tmdb_id) is not None}
                      if title else None),
            "seasons": [{"number": s.number, "episodes": s.episodes, "year": s.year,
                         "have": self.have.get(s.number, 0),
                         "selected": s.number in self.seasons}
                        for s in (title.seasons if title else [])],
            "releases": [r.to_dict() for r in shown[:200]],
            "total": len(self.releases),
            "fit": len(shown),
            "qualities": [[q, per_quality.get(q, 0)] for q in search.QUALITIES],
            "voices": voices,
            "voice": self.voice,
            "any_year": self.any_year,
            "prefs": {"quality": prefs.quality, "hdr": prefs.hdr, "dv": prefs.dv,
                      "voices": prefs.voices},
            "status": (f"Найдено раздач: {len(self.releases)}, под фильтры: {len(shown)}"
                       if title else self.search_status),
            "links": search.tracker_search_links(title.names) if title and not shown else [],
        }

    def to_dict(self) -> dict:
        qs, tg, kd = qbit.load_settings(), telegram.load_settings(), kodi.load_settings()
        ap = self.autopilot
        return {
            "qbit": qs.configured(),
            "auto": qs.configured() and qs.auto,
            "categories": [qs.tv_category, qs.movies_category],
            "telegram": tg.configured(),
            "telegram_bound": tg.configured() and bool(tg.chat_id),
            "kodi": kd.configured(),
            "last_check": ap.last_check,
            "last_error": ap.last_error,
            "items": [{
                "hash": i.hash, "name": i.name,
                "kind": "сериал" if i.kind == library.TV else "фильм",
                "status": i.status, "status_label": autopilot.STATUS_LABELS.get(i.status, i.status),
                "title": i.title, "root": i.root, "error": i.error,
                "candidates": [label for _, label in i.candidates],
                "progress": i.progress,
            } for i in ap.recent()],
            "search": self.search_dict(),
            "watch": [{
                "id": w.id, "kind": w.kind, "title": w.title, "poster": w.poster,
                "mode": w.mode, "status": w.status, "error": w.error,
                "last_check": w.last_check,
                "modes": [[m, label] for m, label in watchlist.MODE_LABELS.items()
                          if (m == watchlist.MOVIE) == (w.kind == library.MOVIE)],
                "offer": [{"hash": o["hash"], "why": o["why"], "title": o["title"],
                           "page": o.get("page", "")} for o in w.offer],
            } for w in sorted(self.watcher.items.values(), key=lambda w: w.title)],
        }


def tab() -> TorrentsTab:
    return current_app.config["STATE"].tabs["torrents"]


def setup(state) -> None:
    state.add_tab("torrents", TorrentsTab(state))


def _select(t: TorrentsTab, hit: metadata.SearchHit, job) -> None:
    """Тайтл выбран: названия и сезоны из TMDb, сверка с медиатекой, поиск раздач."""
    meta = metaconf.load_settings()
    t.hit = hit
    job.progress(0, 0, "названия и сезоны…")
    title = search.title_info(hit.kind, hit.tmdb_id, meta.tmdb_key)
    roots = meta.tv_roots if hit.kind == library.TV else meta.movies_roots
    t.library_dir, t.have = search.in_library(title, roots)
    t.title, t.releases, t.voice, t.any_year = title, [], "", False
    t.seasons = t.default_seasons()
    job.progress(0, 0, "поиск раздач…")
    t.releases = search.find(title, log=t.st.log.add)
    t.search_status = (f"Найдено раздач: {len(t.releases)}"
                       + (f" — в медиатеке: {t.library_dir}" if t.library_dir else ""))
    t.st.log.add(f"Поиск раздач: {library.title_with_year(title.title, title.year)} — "
                 f"{t.search_status}")
    job.summary = t.search_status


@bp.post("/api/torrents/search")
def search_title():
    """Название → карточки TMDb; одна — сразу к раздачам."""
    t = tab()
    t.st.jobs.ensure_idle()
    data = replies.body()
    t.query = str(data.get("query", t.query)).strip()
    if data.get("kind") in (library.TV, library.MOVIE):
        t.kind = data["kind"]
    if not t.query:
        raise UserError("Нет названия", "Впишите, что искать.")
    meta = metaconf.load_settings()
    if not meta.has_tmdb():
        raise UserError("Нет ключа TMDb", "Впишите его в «Настройках» → «Ключи API».")

    def work(job):
        job.progress(0, 0, "поиск в TMDb…")
        t.hits, t.hit, t.title, t.releases = metadata.search(t.kind, t.query, meta), None, None, []
        t.search_status = ""
        if len(t.hits) == 1:
            _select(t, t.hits[0], job)
        elif not t.hits:
            job.summary = t.search_status = "TMDb ничего не нашёл — поправьте название."
        else:
            job.summary = f"Найдено в TMDb: {len(t.hits)} — выберите нужный"
        return None

    job = t.st.jobs.start("Поиск раздач", work)
    return jsonify({"job": job.to_dict()})


@bp.post("/api/torrents/search/select")
def search_select():
    t = tab()
    t.st.jobs.ensure_idle()
    try:
        hit = t.hits[int(replies.body().get("index"))]
    except (TypeError, ValueError, IndexError):
        raise UserError("Нет такого варианта", "Выполните поиск заново.")
    job = t.st.jobs.start("Поиск раздач", lambda job: _select(t, hit, job))
    return jsonify({"job": job.to_dict()})


@bp.post("/api/torrents/search/options")
def search_options():
    """Фильтры на лету: сезоны, год, озвучка, качество, HDR, Dolby Vision."""
    t = tab()
    data = replies.body()
    if isinstance(data.get("seasons"), list) and t.title is not None:
        known = {s.number for s in t.title.seasons}
        t.seasons = sorted({int(n) for n in data["seasons"] if str(n).isdigit() and int(n) in known})
    if "any_year" in data:
        t.any_year = bool(data["any_year"])
    if "voice" in data:
        t.voice = str(data["voice"] or "")
    prefs = search.load_prefs()
    if data.get("quality") in search.QUALITIES:
        prefs.quality = int(data["quality"])
    for key in ("hdr", "dv"):
        if key in data:
            setattr(prefs, key, bool(data[key]))
    if isinstance(data.get("voices"), list):
        prefs.voices = [str(v).strip() for v in data["voices"] if str(v).strip()]
    search.save_prefs(prefs)
    t.st.touch()
    return jsonify({"ok": True})


@bp.post("/api/torrents/search/download")
def search_download():
    """Раздачу — в qBittorrent с категорией сериалов/фильмов; дальше её ведёт автоматика."""
    t = tab()
    h = str(replies.body().get("hash", ""))
    rel = next((r for r in t.releases if r.hash == h), None)
    if rel is None or t.title is None:
        raise UserError("Нет такой раздачи", "Выполните поиск заново.")
    qs = qbit.load_settings()
    if not qs.configured():
        raise UserError("qBittorrent не подключён",
                        "Впишите его адрес в «Настройках» → «qBittorrent».")
    kind = t.title.kind
    category = qs.tv_category if kind == library.TV else qs.movies_category
    try:
        qbit.connect(qs).add(rel.magnet, category=category)
    except qbit.QbitError as e:
        raise UserError("qBittorrent не взял раздачу",
                        f"{e}\n\nВозможно, она уже есть в qBittorrent.")
    wanted = (t.seasons if kind == library.TV and rel.seasons
              and not set(rel.seasons) <= set(t.seasons) else [])
    label = library.title_with_year(t.title.title, t.title.year)
    t.autopilot.expect(rel.hash, kind, t.title.tmdb_id, rel.title, wanted)
    t.st.log.add(f"Поиск: в qBittorrent добавлено — {label}: {rel.title}")
    if not qs.auto:
        raise Info("Добавлено в qBittorrent",
                   "Автоматика выключена — когда начнёт качаться, разложите раздачу на вкладке "
                   "«Медиатека» или включите «Раскладывать новые раздачи сами».")
    return jsonify({"ok": True})


# ------------------------------------------------------------ слежение --
def _watch_id() -> str:
    wid = str(replies.body().get("id", ""))
    if wid not in tab().watcher.items:
        raise UserError("Нет такого тайтла в слежении", "Обновите страницу.")
    return wid


@bp.post("/api/torrents/watch/add")
def watch_add():
    """Следить за тайтлом, выбранным в поиске."""
    t = tab()
    if t.title is None:
        raise UserError("Нет тайтла", "Сначала найдите его в поиске.")
    have_seasons = max((n for n, c in t.have.items() if c), default=0)
    w = t.watcher.add(t.title.kind, t.title.tmdb_id,
                      library.title_with_year(t.title.title, t.title.year),
                      t.hit.poster_url if t.hit else "", have_seasons)
    t.st.log.add(f"Слежение: добавлено — {w.title}")
    t.st.touch()
    return jsonify({"ok": True})


@bp.post("/api/torrents/watch/remove")
def watch_remove():
    t = tab()
    t.watcher.remove(_watch_id())
    t.st.touch()
    return jsonify({"ok": True})


@bp.post("/api/torrents/watch/mode")
def watch_mode():
    t = tab()
    t.watcher.set_mode(_watch_id(), str(replies.body().get("mode", "")))
    t.st.touch()
    return jsonify({"ok": True})


@bp.post("/api/torrents/watch/check")
def watch_check():
    """Проверить сейчас один тайтл или все (без id)."""
    t = tab()
    wid = str(replies.body().get("id", ""))
    if wid and wid not in t.watcher.items:
        raise UserError("Нет такого тайтла в слежении", "Обновите страницу.")
    t.watcher.check_now(wid)
    return jsonify({"ok": True})


@bp.post("/api/torrents/watch/download")
def watch_download():
    t = tab()
    text = t.watcher.download(_watch_id(), str(replies.body().get("hash", "")))
    if text != "Скачиваю.":
        raise UserError("Не получилось", text)
    t.st.touch()
    return jsonify({"ok": True})


def _hash() -> str:
    h = str(replies.body().get("hash", ""))
    if h not in tab().autopilot.items:
        raise UserError("Нет такой раздачи", "Обновите страницу.")
    return h


@bp.post("/api/torrents/answer")
def answer():
    """Выбор тайтла для раздачи, в которой автоматика не уверена; -1 — не раскладывать."""
    try:
        index = int(replies.body().get("index"))
    except (TypeError, ValueError):
        raise UserError("Нет такого варианта", "Обновите страницу.")
    tab().autopilot.answer(_hash(), index)
    return jsonify({"ok": True})


@bp.post("/api/torrents/retry")
def retry():
    tab().autopilot.retry(_hash())
    return jsonify({"ok": True})


@bp.post("/api/torrents/check")
def check_now():
    t = tab()
    t.autopilot.wake()
    t.st.log.add("Автоматика: проверка раздач — сейчас.")
    return jsonify({"ok": True, "time": time.time()})
