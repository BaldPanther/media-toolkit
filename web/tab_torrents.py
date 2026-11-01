"""Вкладка «Торренты»: поиск раздач, слежение и автоматика qBittorrent.

Логика — в `search.py`, `watchlist.py`, `autopilot.py` и `telegram.py`. Фоновые
потоки (автоматику, слежение и бота) запускает `web/server.serve` через
`start_background`: в тестах их нет.
"""
from __future__ import annotations

import collections
import re
import time
import urllib.parse
from pathlib import Path

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
        self.autopilot = autopilot.Autopilot(log=state.log.add, on_change=state.touch,
                                             notify=lambda text: self.bot.notify(telegram.esc(text)))
        self.watcher = watchlist.Watcher(log=state.log.add, bot=self.bot,
                                         autopilot=self.autopilot, on_change=state.touch)
        # Поиск: выбранный тайтл, его сезоны, найденные раздачи и фильтры.
        self.query = ""
        self.hits: list[metadata.SearchHit] = []
        self.hit: metadata.SearchHit | None = None
        self.title: search.Title | None = None
        self.library_dir = ""
        self.numbering_note = ""       # нумерация трекеров и TMDb разная — как связаны
        self.owned_version: search.Owned | None = None   # что лежит в медиатеке
        self.have: dict[int, int] = {}
        self.seasons: list[int] = []
        self.releases: list[search.Release] = []
        self.any_year = False
        self.with_english = False      # Knaben уже спрошен для этого тайтла
        self.in_qbit: set[str] = set() # хеши раздач в qBittorrent на момент поиска
        self.voice = ""
        self.codec = ""                # фильтр кодека: HEVC / AVC / "" — все
        self.search_status = ""

    def start_background(self) -> None:
        self.bot.start()
        self.autopilot.start()
        self.watcher.start()

    def on_scan_requested(self, path: str) -> None:
        """Выбрали папку тайтла и нажали «Сканировать» — его название в поле поиска:
        дальше логично поискать продолжение. Искать — по кнопке, самому."""
        p = Path(path)
        if p.is_file():
            p = p.with_suffix("")
        elif library.season_of_dir(p.name) is not None:
            p = p.parent                # папка сезона — название у папки сериала
        title, _ = library.guess_title_year(p.name)
        if title:
            self.query = title

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
        """Подходят тайтлу и экрану: тип, год, сезоны, HDR/DV, английские по галке.
        Качество и озвучка — потом."""
        if self.title is None:
            return []
        return [r for r in self.releases if search.fits(r, self.title, self.seasons, self.any_year)
                and search.picture_ok(r, prefs) and (prefs.english or search.russian(r))]

    def shown(self, prefs: search.Prefs | None = None) -> list[search.Release]:
        prefs = prefs or search.load_prefs()
        fit = [r for r in self.fitting(prefs) if search.quality_of(r) == prefs.quality
               and (not self.voice or search.has_voice(r, self.voice))
               and (not self.codec or r.codec == self.codec)]
        return search.rank(fit, self.title, prefs, self.seasons) if fit else []

    def refresh_in_qbit(self) -> None:
        qs = qbit.load_settings()
        if not qs.configured():
            self.in_qbit = set()
            return
        try:
            self.in_qbit = {str(i.get("hash", "")) for i in qbit.connect(qs).torrents()}
        except qbit.QbitError:
            pass

    def owned(self, rel: search.Release) -> bool:
        """Всё, что в раздаче (или выбранные сезоны), уже лежит в медиатеке целиком."""
        if self.title is None:
            return False
        if self.title.kind == library.MOVIE:
            return bool(self.library_dir)
        numbers = rel.seasons or self.seasons
        seasons = [self.title.season(n) for n in numbers]
        return bool(numbers) and all(s is not None and s.episodes
                                     and self.have.get(s.number, 0) >= s.episodes for s in seasons)

    def release_dict(self, rel: search.Release, in_qbit: set[str], pinned: set[str]) -> dict:
        d = rel.to_dict()
        d["in_qbit"] = rel.hash in in_qbit
        d["update"] = (not d["in_qbit"]
                       and self.autopilot.replaced_by(pages_of(rel), rel.hash) is not None)
        d["in_library"] = self.owned(rel)
        # Неполная раздача сериала — за ней можно следить («Следить»).
        d["partial"] = (self.title is not None and self.title.kind == library.TV
                        and bool(rel.episodes) and not search.complete(rel, self.title, self.seasons))
        d["pinned"] = bool({search.page_key(p) for p in pages_of(rel)} & pinned)
        # Сравнение с медиатекой: у сериала — на серию, у фильма — файл целиком.
        n = search.episode_count(rel, self.title) if self.title else 0
        per = rel.size // n if n else 0
        d["bitrate"] = search.bitrate(per, self.title.runtime) if self.title else 0.0
        mine = self.owned_version.per_episode if self.owned_version else 0
        d["vs_library"] = round(per / mine, 1) if per and mine else 0.0
        return d

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
        # Число раздач на кнопках кодека — в выбранном качестве и с выбранной озвучкой.
        per_codec = collections.Counter(
            r.codec for r in fit if search.quality_of(r) == prefs.quality
            and (not self.voice or search.has_voice(r, self.voice)))
        ap = self.autopilot
        in_qbit = self.in_qbit | {h for h, i in ap.items.items() if i.seen >= ap.last_check - 1}
        watching = self.watcher.find(title.kind, title.tmdb_id) if title else None
        pinned = set(watching.pin) if watching else set()
        return {
            "query": self.query,
            "hits": [{"i": i, "label": h.label(), "poster": h.poster_url, "overview": h.overview,
                      "kind": "фильм" if h.kind == library.MOVIE else "сериал"}
                     for i, h in enumerate(self.hits)],
            "title": ({"label": library.title_with_year(title.title, title.year),
                       "kind": title.kind, "names": title.names, "status": title.status,
                       "poster": self.hit.poster_url if self.hit else "",
                       "library": self.library_dir,
                       "numbering": self.numbering_note,
                       "owned": owned_text(self.owned_version, title.kind),
                       "watching": watching is not None}
                      if title else None),
            "seasons": [{"number": s.number, "episodes": s.episodes, "year": s.year,
                         "have": self.have.get(s.number, 0),
                         "tmdb": search.tmdb_range(title, s.number),
                         "selected": s.number in self.seasons}
                        for s in (title.seasons if title else [])],
            "releases": [self.release_dict(r, in_qbit, pinned) for r in shown[:200]],
            "free": autopilot.free_space(title.kind) if title else 0,
            "total": len(self.releases),
            "fit": len(shown),
            "qualities": [[q, per_quality.get(q, 0)] for q in search.QUALITIES],
            "voices": voices,
            "voice": self.voice,
            "codecs": [[c, per_codec.get(c, 0)] for c in search.CODECS],
            "codec": self.codec,
            "any_year": self.any_year,
            "prefs": {"quality": prefs.quality, "hdr": prefs.hdr, "dv": prefs.dv,
                      "english": prefs.english, "voices": prefs.voices},
            "status": (f"Найдено раздач: {len(self.releases)}, под фильтры: {len(shown)}"
                       if title else self.search_status),
            "links": search.tracker_search_links(title.names) if title and not shown else [],
        }

    def to_dict(self) -> dict:
        qs, tg, kd = qbit.load_settings(), telegram.load_settings(), kodi.load_settings()
        ap = self.autopilot
        prefs = search.load_prefs()
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
                "status": i.status,
                "status_label": (UPDATE_LABELS[i.update_step] if i.replaces
                                 else autopilot.STATUS_LABELS.get(i.status, i.status)
                                 + (" — на паузе" if i.hold and i.status in (autopilot.ERROR,
                                                                             autopilot.ASK) else "")),
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
                "quality": w.quality, "voice": w.voice, "bigger": w.bigger,
                "qualities": [[q, label] for q, label in watchlist.QUALITY_LABELS.items()],
                "voices": sorted(set(w.voices) | set(prefs.voices) | ({w.voice} - {""})),
                "pin_title": w.pin_title,
                "offer": [{"hash": o["hash"], "why": o["why"], "title": o["title"],
                           "page": o.get("page", ""), "update": self.watcher.is_update(o)}
                          for o in w.offer],
            } for w in sorted(self.watcher.items.values(), key=lambda w: w.title)],
        }


def owned_text(owned: search.Owned | None, kind: str) -> str:
    """«1080p · 7.0 ГБ · ≈ 9.6 Мбит/с»; у сериала — ещё число серий и размер на серию."""
    if owned is None:
        return ""
    parts = [f"{owned.episodes} сер."] if kind == library.TV else []
    if owned.quality:
        parts.append(search.QUALITY_NAMES.get(owned.quality, f"{owned.quality}p")
                     + (f" {owned.codec}" if owned.codec else ""))
    parts.append(autopilot.gb(owned.size) + (f" (≈ {autopilot.gb(owned.per_episode)} на серию)"
                                             if kind == library.TV and owned.episodes > 1 else ""))
    if owned.bitrate:
        parts.append(f"≈ {owned.bitrate:g} Мбит/с")
    return " · ".join(parts)


UPDATE_LABELS = {"": "обновление: жду список файлов", "rename": "обновление: сверяю файлы",
                 "check": "обновление: проверяю скачанное"}


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
    t.numbering_note = search.numbering_note(title, t.library_dir)
    t.owned_version = search.library_version(hit.kind, t.library_dir, meta.extras_folder)
    t.title, t.releases, t.voice, t.codec, t.any_year = title, [], "", "", False
    t.seasons = t.default_seasons()
    job.progress(0, 0, "поиск раздач…")
    t.with_english = search.load_prefs().english
    t.releases = search.find(title, log=t.st.log.add)
    t.refresh_in_qbit()
    t.search_status = (f"Найдено раздач: {len(t.releases)}"
                       + (f" — в медиатеке: {t.library_dir}" if t.library_dir else ""))
    t.st.log.add(f"Поиск раздач: {library.title_with_year(title.title, title.year)} — "
                 f"{t.search_status}")
    job.summary = t.search_status


@bp.post("/api/torrents/search")
def search_title():
    """Название → карточки TMDb, фильмы и сериалы вместе; одна — сразу к раздачам.
    Категория qBittorrent и папка — по выбранной карточке."""
    t = tab()
    t.st.jobs.ensure_idle()
    data = replies.body()
    t.query = str(data.get("query", t.query)).strip()
    if not t.query:
        raise UserError("Нет названия", "Впишите, что искать.")
    meta = metaconf.load_settings()
    if not meta.has_tmdb():
        raise UserError("Нет ключа TMDb", "Впишите его в «Настройках» → «Ключи API».")

    def work(job):
        job.progress(0, 0, "поиск в TMDb…")
        t.hits, t.hit, t.title, t.releases = metadata.search_any(t.query, meta), None, None, []
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
    """Фильтры на лету: сезоны, год, озвучка, кодек, качество, HDR, Dolby Vision, английские."""
    t = tab()
    data = replies.body()
    if isinstance(data.get("seasons"), list) and t.title is not None:
        known = {s.number for s in t.title.seasons}
        t.seasons = sorted({int(n) for n in data["seasons"] if str(n).isdigit() and int(n) in known})
    if "any_year" in data:
        t.any_year = bool(data["any_year"])
    if "voice" in data:
        t.voice = str(data["voice"] or "")
    if "codec" in data:
        t.codec = data["codec"] if data["codec"] in search.CODECS else ""
    prefs = search.load_prefs()
    if data.get("quality") in search.QUALITIES:
        prefs.quality = int(data["quality"])
    for key in ("hdr", "dv", "english"):
        if key in data:
            setattr(prefs, key, bool(data[key]))
    if isinstance(data.get("voices"), list):
        prefs.voices = [str(v).strip() for v in data["voices"] if str(v).strip()]
    search.save_prefs(prefs)
    t.st.touch()
    if prefs.english and t.title is not None and not t.with_english:
        # Галку поставили после поиска — Knaben дозапрашиваем, jac.red не трогаем.
        t.st.jobs.ensure_idle()

        def work(job):
            job.progress(0, 0, "английские раздачи…")
            title = t.title
            more = search.find(title, sources=[search.KNABEN], log=t.st.log.add)
            if t.title is title:
                t.releases = search.group(t.releases + more)
                t.with_english = True
            job.summary = f"Английских раздач: {len(more)}"

        return jsonify({"job": t.st.jobs.start("Поиск раздач", work).to_dict()})
    return jsonify({"ok": True})


@bp.post("/api/torrents/search/download")
def search_download():
    """Раздачу — в qBittorrent с категорией сериалов/фильмов; дальше её ведёт автоматика."""
    t = tab()
    h = str(replies.body().get("hash", ""))
    rel = next((r for r in t.releases if r.hash == h), None)
    if rel is None or t.title is None:
        raise UserError("Нет такой раздачи", "Выполните поиск заново.")
    kind = t.title.kind
    wanted = (t.seasons if kind == library.TV and rel.seasons
              and not set(rel.seasons) <= set(t.seasons) else [])
    # Сборник, из которого нужны не все сезоны, займёт примерно свою долю.
    need = rel.size * len(wanted) // len(rel.seasons) if wanted else rel.size
    free = autopilot.free_space(kind)
    if free and need > free and t.autopilot.replaced_by(pages_of(rel), rel.hash) is None:
        replies.confirm("space", "Места может не хватить",
                        f"Раздача — {autopilot.gb(need)}, свободно на диске — "
                        f"{autopilot.gb(free)}. Всё равно скачать?", yes="Скачать")
    said = _add(t, rel.magnet, rel.hash, kind, t.title.tmdb_id, rel.title, wanted, pages_of(rel),
                rel.seasons)
    label = library.title_with_year(t.title.title, t.title.year)
    t.st.log.add(f"Поиск: в qBittorrent добавлено — {label}: {rel.title}")
    return jsonify({"ok": True, "text": said})


def pages_of(rel: search.Release) -> list[str]:
    return [tr[1] for tr in rel.trackers if tr[1]]


def _add(t: TorrentsTab, magnet: str, h: str, kind: str, tmdb_id: int, name: str,
         wanted=(), pages=(), seasons=()) -> str:
    """В qBittorrent через автоматику. → что сказать; автоматика выключена — Info."""
    qs = qbit.load_settings()
    if not qs.configured():
        raise UserError("qBittorrent не подключён",
                        "Впишите его адрес в «Настройках» → «qBittorrent».")
    try:
        said = t.autopilot.add(magnet, h, kind, tmdb_id, name, wanted, pages, seasons)
    except qbit.QbitError as e:
        raise UserError("qBittorrent не взял раздачу",
                        f"{e}\n\nВозможно, она уже есть в qBittorrent.") from e
    t.in_qbit.add(h)
    if not qs.auto:
        raise Info("Добавлено в qBittorrent",
                   "Автоматика выключена — когда начнёт качаться, разложите раздачу на вкладке "
                   "«Медиатека» или включите «Раскладывать новые раздачи сами».")
    return said


_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")


@bp.post("/api/torrents/add-link")
def add_link():
    """Вставленная magnet-ссылка, её хеш или ссылка на .torrent — в qBittorrent.
    Страница раздачи из текущего поиска — берётся её magnet и уже известный тайтл."""
    t = tab()
    data = replies.body()
    url = str(data.get("url", "")).strip()
    kind = data["kind"] if data.get("kind") in (library.TV, library.MOVIE) else library.TV
    if _HEX40.match(url):
        url = f"magnet:?xt=urn:btih:{url.lower()}"
    rel = next((r for r in t.releases if url in pages_of(r)), None)
    if rel is not None and t.title is not None:
        said = _add(t, rel.magnet, rel.hash, t.title.kind, t.title.tmdb_id, rel.title, (),
                    pages_of(rel), rel.seasons)
        t.st.log.add(f"Ссылка: в qBittorrent добавлено — {rel.title}")
        return jsonify({"ok": True, "text": said})
    h = search.infohash(url)
    if not h and not url.startswith(("http://", "https://")):
        raise UserError("Это не ссылка на раздачу",
                        "Вставьте magnet-ссылку (начинается с magnet:), её хеш из 40 знаков "
                        "или ссылку на файл .torrent.")
    if not h:
        # Ссылка на .torrent: хеша не знаем — раздачу подхватит автоматика по категории.
        qs = qbit.load_settings()
        if not qs.configured():
            raise UserError("qBittorrent не подключён",
                            "Впишите его адрес в «Настройках» → «qBittorrent».")
        try:
            qbit.connect(qs).add(url, category=qs.tv_category if kind == library.TV
                                 else qs.movies_category)
        except qbit.QbitError as e:
            raise UserError("qBittorrent не взял ссылку", str(e)) from e
        t.st.log.add(f"Ссылка: в qBittorrent отправлено — {url}")
        return jsonify({"ok": True, "text": "Отправлено в qBittorrent"})
    name = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("dn", [""])[0] or h
    said = _add(t, url, h, kind, 0, name)
    t.st.log.add(f"Ссылка: в qBittorrent добавлено — {name}")
    return jsonify({"ok": True, "text": said})


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
    ok, text = t.watcher.download(_watch_id(), str(replies.body().get("hash", "")))
    if not ok:
        raise UserError("Не получилось", text)
    t.st.touch()
    return jsonify({"ok": True, "text": text})


@bp.post("/api/torrents/watch/filter")
def watch_filter():
    """Условия слежения: качество (0 / 1080 / 2160) и озвучка («» — любая)."""
    t = tab()
    data = replies.body()
    quality = data.get("quality")
    t.watcher.set_filter(_watch_id(), quality=int(quality) if str(quality).isdigit() else None,
                         voice=str(data["voice"]) if "voice" in data else None,
                         bigger=bool(data["bigger"]) if "bigger" in data else None)
    t.st.touch()
    return jsonify({"ok": True})


@bp.post("/api/torrents/watch/pin")
def watch_pin():
    """Следить за раздачей из выдачи поиска: сообщать, когда в ней прибавятся серии."""
    t = tab()
    rel = next((r for r in t.releases if r.hash == str(replies.body().get("hash", ""))), None)
    if rel is None or t.title is None:
        raise UserError("Нет такой раздачи", "Выполните поиск заново.")
    have_seasons = max((n for n, c in t.have.items() if c), default=0)
    w = t.watcher.pin_release(t.title.kind, t.title.tmdb_id,
                              library.title_with_year(t.title.title, t.title.year),
                              t.hit.poster_url if t.hit else "", have_seasons, pages_of(rel),
                              rel.title)
    t.st.log.add(f"Слежение: {w.title} — слежу за раздачей «{rel.title}»")
    t.st.touch()
    return jsonify({"ok": True})


@bp.post("/api/torrents/watch/unpin")
def watch_unpin():
    t = tab()
    t.watcher.unpin(_watch_id())
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
