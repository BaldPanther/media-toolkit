"""Слежение: раз в день — что вышло и появилось ли на трекерах.

У каждого сериала в списке свой режим:

- «весь сезон» (по умолчанию) — ждём, пока сезон выйдет целиком (по TMDb) и на
  трекерах появится раздача со всеми сериями; тогда — оповещение с лучшими
  вариантами и кнопками «Скачать». Сезоны, которые уже есть в медиатеке, не
  предлагаются;
- «каждая серия» — для сериалов, которые смотрят по мере выхода: в раздаче
  появилась серия, которой в медиатеке ещё нет;
- «новые сезоны» — как «весь сезон», но только для сезонов новее тех, что были
  на момент добавления; заодно сообщает об анонсе нового сезона.

Фильм ждёт раздачу не хуже 1080p (пока он в кино, на трекерах экранки).

Об одном и том же событии бот пишет один раз (`notified`). Качает программа
только по кнопке — сама ничего не добавляет.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

import library
import metaconf
import paths
import qbit
import search
import telegram

_NAME = "watchlist.json"
CHECK_EVERY = 24 * 3600        # как часто проверять каждый тайтл
TICK = 3600                    # как часто поток смотрит, кого пора проверить
OFFER = 3                      # сколько вариантов предлагать

SEASON, EPISODE, NEW_SEASONS, MOVIE = "season", "episode", "new_seasons", "movie"
MODE_LABELS = {SEASON: "весь сезон", EPISODE: "каждая серия", NEW_SEASONS: "новые сезоны",
               MOVIE: "появится раздача"}


@dataclass
class Watch:
    id: str
    kind: str
    tmdb_id: int
    title: str                          # «Тёмная материя (2024)»
    mode: str = SEASON
    poster: str = ""
    since_season: int = 0               # «новые сезоны»: новее этого
    seasons_known: int = 0              # сколько сезонов было в TMDb в прошлый раз
    last_check: float = 0.0
    status: str = ""                    # что сейчас: «ждёт сезон 2: вышло 6 из 10»
    error: str = ""
    offer: list = field(default_factory=list)       # [{hash, title, magnet, why, seasons}]
    offer_season: int = 0
    notified: list = field(default_factory=list)    # ключи событий, о которых уже писали


class Watcher:
    def __init__(self, log=None, bot: telegram.Service | None = None, autopilot=None,
                 on_change=None):
        self.log = log or (lambda text: None)
        self.bot = bot
        self.autopilot = autopilot
        self.on_change = on_change or (lambda: None)
        self.items: dict[str, Watch] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.load()
        if bot is not None:
            bot.on("wl", self._on_button)

    # ------------------------------------------------------------ список --
    def load(self) -> None:
        d = paths.read_json(paths.settings_path(_NAME))
        for raw in d.get("items", []) if isinstance(d, dict) else []:
            try:
                w = Watch(**{k: v for k, v in raw.items() if k in Watch.__dataclass_fields__})
            except TypeError:
                continue
            self.items[w.id] = w

    def save(self) -> None:
        with self._lock:
            data = {"items": [asdict(w) for w in self.items.values()]}
        try:
            paths.write_json(paths.settings_path(_NAME), data)
        except OSError as e:
            self.log(f"⚠ Слежение: не сохранился список: {e}")

    def find(self, kind: str, tmdb_id: int) -> Watch | None:
        return next((w for w in self.items.values()
                     if w.kind == kind and w.tmdb_id == tmdb_id), None)

    def add(self, kind: str, tmdb_id: int, title: str, poster: str = "",
            have_seasons: int = 0) -> Watch:
        with self._lock:
            w = self.find(kind, tmdb_id)
            if w is None:
                mode = SEASON if kind == library.TV else MOVIE
                w = Watch(uuid.uuid4().hex[:8], kind, tmdb_id, title, mode, poster,
                          since_season=have_seasons)
                self.items[w.id] = w
        self.save()
        self._wake.set()
        return w

    def remove(self, watch_id: str) -> None:
        with self._lock:
            self.items.pop(watch_id, None)
        self.save()

    def set_mode(self, watch_id: str, mode: str) -> None:
        with self._lock:
            w = self.items.get(watch_id)
            if w is not None and mode in MODE_LABELS and (mode == MOVIE) == (w.kind == library.MOVIE):
                w.mode, w.last_check = mode, 0.0
        self.save()
        self._wake.set()

    def check_now(self, watch_id: str = "") -> None:
        with self._lock:
            for w in self.items.values():
                if not watch_id or w.id == watch_id:
                    w.last_check = 0.0
        self._wake.set()

    # ------------------------------------------------------------ поток --
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="watchlist", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — фоновый поток не должен умирать
                self.log(f"⚠ Слежение: {e}")
            self._wake.wait(TICK)
            self._wake.clear()

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        meta = metaconf.load_settings()
        if not meta.has_tmdb():
            return
        with self._lock:
            due = [w for w in self.items.values() if now - w.last_check >= CHECK_EVERY]
        for w in due:
            try:
                self.check(w, meta)
                w.error = ""
            except Exception as e:  # noqa: BLE001 — сеть, TMDb, трекеры: в отчёт и дальше
                w.error = str(e)
                self.log(f"⚠ Слежение, {w.title}: {e}")
            w.last_check = now
        if due:
            self.save()
            self.on_change()

    # ------------------------------------------------------------ проверка --
    def check(self, w: Watch, meta) -> None:
        title = search.title_info(w.kind, w.tmdb_id, meta.tmdb_key)
        w.title = library.title_with_year(title.title, title.year)
        roots = meta.tv_roots if w.kind == library.TV else meta.movies_roots
        _, have = search.in_library(title, roots)
        if w.kind == library.MOVIE:
            self._check_movie(w, title)
            return
        if w.mode == NEW_SEASONS and w.seasons_known and len(title.seasons) > w.seasons_known:
            new = title.seasons[-1]
            self._event(w, f"announce:{new.number}",
                        f"📣 <b>{telegram.esc(w.title)}</b>: анонсирован сезон {new.number}")
        w.seasons_known = len(title.seasons)
        if w.mode == EPISODE:
            self._check_episode(w, title, have)
        else:
            self._check_season(w, title, have)

    def _target_season(self, w: Watch, title, have) -> int:
        """Первый сезон, которого нет в медиатеке целиком (для «новых» — новее известных)."""
        for s in title.seasons:
            if w.mode == NEW_SEASONS and s.number <= w.since_season:
                continue
            if s.episodes and have.get(s.number, 0) < s.episodes:
                return s.number
        return 0

    def _check_season(self, w: Watch, title, have) -> None:
        number = self._target_season(w, title, have)
        if not number:
            w.status, w.offer = "все сезоны уже в медиатеке — жду новых", []
            return
        s = title.season(number)
        if not title.season_complete(number):
            aired = title.aired(number)
            when = (f", следующая серия {title.next_air[2]}"
                    if title.next_air[0] == number and title.next_air[2] else "")
            w.status = (f"жду сезон {number}: вышло {aired} из {s.episodes}{when}" if aired
                        else f"жду сезон {number}: ещё не начался")
            w.offer = []
            return
        found = self._offer(w, title, [number], full=True)
        if not found:
            w.status = f"сезон {number} вышел целиком — полной раздачи пока нет"
            return
        w.status = f"сезон {number}: есть полные раздачи — можно качать"
        self._event(w, f"season:{number}",
                    f"🎬 <b>{telegram.esc(w.title)}</b>: сезон {number} вышел целиком, "
                    f"раздачи со всеми сериями есть", self._buttons(w))

    def _check_episode(self, w: Watch, title, have) -> None:
        number, episode = title.last_aired
        if not number:
            w.status, w.offer = "ещё не начался", []
            return
        if have.get(number, 0) >= title.aired(number):
            w.status, w.offer = f"сезон {number}: все вышедшие серии ({episode}) уже есть", []
            return
        found = self._offer(w, title, [number], full=False, more_than=have.get(number, 0))
        if not found:
            w.status = f"вышла серия {episode} сезона {number} — в раздачах её пока нет"
            return
        top = w.offer[0]
        w.status = f"сезон {number}: в раздаче уже {top['episodes']} сер., у вас {have.get(number, 0)}"
        self._event(w, f"episode:{number}:{top['episodes']}",
                    f"🆕 <b>{telegram.esc(w.title)}</b>: сезон {number}, в раздаче уже "
                    f"{top['episodes']} сер. (у вас {have.get(number, 0)})", self._buttons(w))

    def _check_movie(self, w: Watch, title) -> None:
        if not self._offer(w, title, [], full=False, min_quality=1080):
            w.status = "раздач в хорошем качестве пока нет"
            return
        w.status = "есть раздачи — можно качать"
        self._event(w, f"movie:{w.offer[0]['hash']}",
                    f"🎬 <b>{telegram.esc(w.title)}</b>: появились раздачи", self._buttons(w))

    def _offer(self, w: Watch, title, seasons, full: bool, more_than: int = 0,
               min_quality: int = 0) -> bool:
        """Лучшие раздачи для сезонов → `w.offer`. → нашлось ли что-нибудь.
        Кнопка качества поиска сюда не относится: лучшее из 4K → 1080p → 720p.
        Только раздачи с русскими дорожками: иначе о сезоне сообщили бы по английской
        раздаче, а о русской (событие то же) — уже нет."""
        prefs = search.load_prefs()
        fit = [r for r in search.find(title, log=self.log)
               if search.fits(r, title, seasons) and r.seeds >= 1 and search.picture_ok(r, prefs)
               and search.russian(r)
               and (not full or search.complete(r, title, seasons))
               and (not more_than or (r.episodes and r.episodes[1] > more_than))
               and r.quality >= min_quality]
        ranked = search.rank(fit, title, prefs, seasons)
        w.offer = [{"hash": r.hash, "title": r.title, "magnet": r.magnet, "why": r.why,
                    "seasons": r.seasons, "episodes": r.episodes[1] if r.episodes else 0,
                    "page": r.trackers[0][1] if r.trackers else ""}
                   for r in ranked[:OFFER]]
        w.offer_season = seasons[0] if seasons else 0
        return bool(w.offer)

    def _buttons(self, w: Watch) -> list:
        rows = [[(("⭐ " if i == 0 else f"{i + 1}. ") + o["why"], f"wl:{w.id}:{o['hash'][:12]}")]
                for i, o in enumerate(w.offer)]
        if w.offer and w.offer[0].get("page"):
            rows.append([("Страница лучшей раздачи", w.offer[0]["page"])])
        return rows

    def _event(self, w: Watch, key: str, text: str, buttons=None) -> None:
        """Сообщить о событии один раз. Не дошло (бот не настроен или чат не
        привязан) — не помечаем: скажем при следующей проверке."""
        if key in w.notified or self.bot is None:
            return
        if self.bot.notify(text, buttons):
            w.notified.append(key)
            self.log(f"Слежение: {w.title} — сообщено в Telegram ({key})")

    # ------------------------------------------------------------ качать --
    def download(self, watch_id: str, hash_prefix: str) -> str:
        """Скачать предложенную раздачу: в qBittorrent, дальше её ведёт автоматика."""
        with self._lock:
            w = self.items.get(watch_id)
            offer = next((o for o in (w.offer if w else []) if o["hash"].startswith(hash_prefix)), None)
        if w is None or offer is None:
            return "Предложение устарело — проверьте заново."
        qs = qbit.load_settings()
        if not qs.configured():
            return "qBittorrent не подключён."
        category = qs.tv_category if w.kind == library.TV else qs.movies_category
        try:
            qbit.connect(qs).add(offer["magnet"], category=category)
        except qbit.QbitError as e:
            return f"qBittorrent не взял раздачу: {e}"
        wanted = ([w.offer_season] if w.offer_season and offer["seasons"]
                  and set(offer["seasons"]) - {w.offer_season} else [])
        if self.autopilot is not None:
            self.autopilot.expect(offer["hash"], w.kind, w.tmdb_id, offer["title"], wanted)
        self.log(f"Слежение: в qBittorrent добавлено — {w.title}: {offer['title']}")
        return "Скачиваю."

    def _on_button(self, args: str) -> str:
        watch_id, _, prefix = args.partition(":")
        return self.download(watch_id, prefix)
