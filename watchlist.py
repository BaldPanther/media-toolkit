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

Условия у каждого тайтла: качество («только 4K» — фильм в Full HD уже есть, жду
4K) и озвучка. Или своя раздача (`pin`): слежение смотрит только её страницу на
трекере и сообщает, когда в ней прибавились серии, — остальные не предлагает.

Об одном и том же событии бот пишет один раз (`notified`). Качает программа
только по кнопке — сама ничего не добавляет.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace

import autopilot
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
# Условие на качество: 0 — лучшее, что есть (4K → 1080p → 720p).
QUALITY_LABELS = {0: "лучшее, что есть", 1080: "не хуже 1080p", 2160: "только 4K"}
# «Заметно крупнее моей»: во столько раз больше (у сериала — на серию). 7,0 и 7,3 ГБ —
# одна и та же раздача с разных трекеров, а 15 вместо 7 — уже другое качество.
BIGGER = 1.5


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
    quality: int = 0                    # условие: QUALITY_LABELS
    voice: str = ""                     # условие: только раздачи с этой озвучкой
    bigger: bool = False                # условие: заметно крупнее того, что в медиатеке
    pin: list = field(default_factory=list)         # своя раздача: ключи страниц (search.page_key)
    pin_title: str = ""
    voices: list = field(default_factory=list)      # озвучки в раздачах тайтла — для выбора


def pages_of(rel: search.Release) -> list[str]:
    return [tr[1] for tr in rel.trackers if tr[1]]


def _per_episode(rel: search.Release, title) -> int:
    """Размер раздачи на серию (у фильма — весь); серий не узнать — 0."""
    n = search.episode_count(rel, title)
    return rel.size // n if n else 0


class Watcher:
    def __init__(self, log=None, bot: telegram.Service | None = None, autopilot=None,
                 on_change=None):
        self.log = log or (lambda text: None)
        self.bot = bot
        self.autopilot = autopilot
        self.on_change = on_change or (lambda: None)
        self.only_in_qbit = False       # последний _offer: все подходящие уже в qBittorrent
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

    def set_filter(self, watch_id: str, quality: int | None = None, voice: str | None = None,
                   bigger: bool | None = None) -> None:
        """Условия: качество (QUALITY_LABELS), озвучка («» — любая), крупнее медиатеки."""
        with self._lock:
            w = self.items.get(watch_id)
            if w is None:
                return
            if quality in QUALITY_LABELS:
                w.quality = quality
            if voice is not None:
                w.voice = voice.strip()
            if bigger is not None:
                w.bigger = bigger
            w.last_check = 0.0
        self.save()
        self._wake.set()

    def pin_release(self, kind: str, tmdb_id: int, title: str, poster: str, have_seasons: int,
                    pages, release_title: str) -> Watch:
        """Следить за своей раздачей: сериал — в слежение (если ещё нет) в режиме «каждая
        серия», и предлагается только эта раздача, когда в ней прибавятся серии."""
        w = self.add(kind, tmdb_id, title, poster, have_seasons)
        with self._lock:
            w.pin = sorted({search.page_key(p) for p in pages if p})
            w.pin_title = release_title
            if kind == library.TV:
                w.mode = EPISODE
            w.last_check = 0.0
        self.save()
        self._wake.set()
        return w

    def unpin(self, watch_id: str) -> None:
        with self._lock:
            w = self.items.get(watch_id)
            if w is not None:
                w.pin, w.pin_title, w.last_check = [], "", 0.0
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
        folder, have = search.in_library(title, roots)
        # «Заметно крупнее моей»: порог — размер того, что лежит (у сериала — на серию).
        # Кодек тоже читается: HEVC той же сути мельче, порог — в пересчёте на AVC.
        owned = search.library_version(w.kind, folder, meta.extras_folder) if w.bigger else None
        min_size = int(search.avc_equivalent(owned.per_episode, owned.codec) * BIGGER) if owned else 0
        if w.kind == library.MOVIE:
            self._check_movie(w, title, bool(folder), min_size)
            return
        if w.mode == NEW_SEASONS and w.seasons_known and len(title.seasons) > w.seasons_known:
            new = title.seasons[-1]
            self._event(w, f"announce:{new.number}",
                        f"📣 <b>{telegram.esc(w.title)}</b>: анонсирован сезон {new.number}")
        w.seasons_known = len(title.seasons)
        if w.mode == EPISODE:
            self._check_episode(w, title, have, min_size)
        else:
            self._check_season(w, title, have, min_size)

    def _target_season(self, w: Watch, title, have) -> int:
        """Первый сезон, которого нет в медиатеке целиком (для «новых» — новее известных)."""
        for s in title.seasons:
            if w.mode == NEW_SEASONS and s.number <= w.since_season:
                continue
            if s.episodes and have.get(s.number, 0) < s.episodes:
                return s.number
        return 0

    def _check_season(self, w: Watch, title, have, min_size: int = 0) -> None:
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
        found = self._offer(w, title, [number], full=True, min_size=min_size)
        if not found:
            w.status = f"сезон {number} вышел целиком — полной раздачи пока нет{self._wanted(w)}"
            return
        w.status = f"сезон {number}: есть полные раздачи — можно качать"
        # Число серий — в ключе: сезон аниме выходит частями («ТВ-3, часть 2» через
        # полгода), и о дополненном сезоне надо сказать заново.
        key, voice = self._voice_event(w)
        self._event(w, f"season:{number}:{s.episodes}{key}",
                    f"🎬 <b>{telegram.esc(w.title)}</b>: сезон {number} вышел целиком, "
                    f"раздачи со всеми сериями есть{voice}", self._buttons(w))

    def _check_episode(self, w: Watch, title, have, min_size: int = 0) -> None:
        number, episode = title.last_aired
        if not number:
            w.status, w.offer = "ещё не начался", []
            return
        if have.get(number, 0) >= title.aired(number):
            w.status, w.offer = f"сезон {number}: все вышедшие серии ({episode}) уже есть", []
            return
        found = self._offer(w, title, [number], full=False, more_than=have.get(number, 0),
                            min_size=min_size)
        where = "в вашей раздаче" if w.pin else "в раздаче"
        if not found:
            w.status = (f"сезон {number}: раздача уже в qBittorrent — сообщу, когда на трекере "
                        f"будет её новая версия" if self.only_in_qbit else
                        f"вышла серия {episode} сезона {number} — {where} её пока нет"
                        f"{self._wanted(w)}")
            return
        top = w.offer[0]
        w.status = f"сезон {number}: {where} уже {top['episodes']} сер., у вас {have.get(number, 0)}"
        key, voice = self._voice_event(w)
        self._event(w, f"episode:{number}:{top['episodes']}{key}",
                    f"🆕 <b>{telegram.esc(w.title)}</b>: сезон {number}, {where} уже "
                    f"{top['episodes']} сер. (у вас {have.get(number, 0)}){voice}", self._buttons(w))

    def _check_movie(self, w: Watch, title, owned: bool, min_size: int = 0) -> None:
        if owned and not (w.quality or w.voice or w.pin or w.bigger):
            # Уже скачан, и ждать нечего: о любой раздаче сообщать незачем.
            w.status, w.offer = ("уже в медиатеке — выберите, чего ждать: качество или "
                                 "озвучку"), []
            return
        if not self._offer(w, title, [], full=False, min_quality=1080, min_size=min_size):
            wanted = self._wanted(w, bare=True)
            w.status = f"жду: {wanted} — пока нет" if wanted else "раздач в хорошем качестве пока нет"
            return
        w.status = "есть раздачи — можно качать" + self._wanted(w)
        key, voice = self._voice_event(w)
        # «Любая русская» — два сообщения: русская озвучка появилась, потом дубляж; иначе —
        # о каждой новой лучшей раздаче.
        event = (f"movie:{'dub' if key else 'rus'}" if w.voice == search.RUS_VOICE
                 else f"movie:{w.offer[0]['hash']}")
        self._event(w, event,
                    f"🎬 <b>{telegram.esc(w.title)}</b>: появились раздачи"
                    f"{voice or telegram.esc(self._wanted(w))}", self._buttons(w))

    def _voice_event(self, w: Watch) -> tuple[str, str]:
        """«Любая русская»: (хвост ключа события, хвост сообщения). Сначала сообщаем о
        любой русской озвучке, а когда выйдет дубляж — ещё раз: ключ у него свой."""
        if w.voice != search.RUS_VOICE or not w.offer:
            return "", ""
        if w.offer[0].get("dub"):
            return ":dub", " — с дубляжом"
        return "", " — с русской озвучкой (дубляжа пока нет)"

    def _wanted(self, w: Watch, bare: bool = False) -> str:
        """Условия словами: «4K · Дубляж», «ваша раздача». bare=False — « (…)» для хвоста."""
        parts = (["ваша раздача"] if w.pin else
                 [p for p in ({1080: "от 1080p", 2160: "4K"}.get(w.quality, ""), w.voice,
                              "крупнее вашей" if w.bigger else "") if p])
        text = " · ".join(parts)
        return text if bare or not text else f" ({text})"

    def _offer(self, w: Watch, title, seasons, full: bool, more_than: int | None = None,
               min_quality: int = 0, min_size: int = 0) -> bool:
        """Лучшие раздачи для сезонов → `w.offer`. → нашлось ли что-нибудь.
        Кнопка качества поиска сюда не относится: лучшее из 4K → 1080p → 720p.
        Только раздачи с русскими дорожками: иначе о сезоне сообщили бы по английской
        раздаче, а о русской (событие то же) — уже нет. Раздача без сезона в названии
        у сериала из нескольких сезонов — неизвестно какого, не предлагается.
        `more_than` — в раздаче больше серий сезона, чем в медиатеке; `min_size` — не
        меньше этого на серию (у фильма — весь файл), в пересчёте на AVC.

        Условия тайтла: качество и озвучка. Своя раздача (`pin`) — только она, без
        прочих условий: её выбрали руками, даже если сейчас у неё нет сидов."""
        prefs = search.load_prefs()
        # Устаревший magnet (search.group) не предлагаем: скачалась бы прежняя версия;
        # мёртвую ссылку (search.check_alive) — тоже: не скачалось бы ничего.
        found = search.find(title, log=self.log, seasons=seasons)
        fit = [r for r in found
               if search.fits(r, title, seasons) and not r.stale and not r.dead
               and (title.kind != library.TV or r.seasons or len(title.seasons) == 1)
               and (not full or search.complete(r, title, seasons))
               and (more_than is None or (r.episodes and r.episodes[1] > more_than))]
        w.voices = sorted({v for r in fit if search.russian(r) for v in r.voices})
        if w.pin:
            fit = [r for r in fit if {search.page_key(p) for p in pages_of(r)} & set(w.pin)]
        else:
            fit = [r for r in fit
                   if r.seeds >= 1 and search.picture_ok(r, prefs) and search.russian(r)
                   and r.quality >= max(min_quality, w.quality)
                   and (not w.voice or search.has_voice(r, w.voice))
                   and (not min_size
                        or search.avc_equivalent(_per_episode(r, title), r.codec) >= min_size)]
        # Раздача уже в qBittorrent — качать её снова незачем. Если название при этом
        # обещает новые серии, у источника ещё прежний magnet (бывает у kinozal на
        # jac.red): «Обновить» скачало бы то же самое. Ждём настоящую новую версию.
        present = self.autopilot.present() if self.autopilot is not None else set()
        self.only_in_qbit = bool(fit) and all(search.hashes(r) & present for r in fit)
        fit = [r for r in fit if not search.hashes(r) & present]
        if w.voice == search.RUS_VOICE:
            # «Любая русская»: годится любая озвучка, но дубляж — первым.
            prefs = replace(prefs, voices=[search.DUB] + prefs.voices)
        ranked = search.rank(fit, title, prefs, seasons)
        w.offer = [{"hash": r.hash, "title": r.title, "magnet": r.magnet, "why": r.why,
                    "seasons": r.seasons, "episodes": r.episodes[1] if r.episodes else 0,
                    "page": r.trackers[0][1] if r.trackers else "", "size": r.size,
                    "pages": pages_of(r), "dub": search.has_voice(r, search.DUB),
                    # Те же файлы под другими хешами — их пустят наперегонки с главной.
                    "alts": search.alternatives(r)}
                   for r in ranked[:OFFER]]
        w.offer_season = seasons[0] if seasons else 0
        return bool(w.offer)

    def is_update(self, offer: dict) -> bool:
        """Предложена новая версия раздачи, что уже качается: «Обновить», а не «Скачать»."""
        return (self.autopilot is not None
                and self.autopilot.replaced_by(offer.get("pages") or [], offer["hash"]) is not None)

    def _buttons(self, w: Watch) -> list:
        rows = [[(("Обновить: " if self.is_update(o) else "⭐ " if i == 0 else f"{i + 1}. ")
                  + o["why"], f"wl:{w.id}:{o['hash'][:12]}")]
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
    def download(self, watch_id: str, hash_prefix: str) -> tuple[bool, str]:
        """Скачать предложенную раздачу: в qBittorrent, дальше её ведёт автоматика.
        → (добавлена ли, что сказать)."""
        with self._lock:
            w = self.items.get(watch_id)
            offer = next((o for o in (w.offer if w else []) if o["hash"].startswith(hash_prefix)), None)
        if w is None or offer is None:
            return False, "Предложение устарело — проверьте заново."
        if not qbit.load_settings().configured() or self.autopilot is None:
            return False, "qBittorrent не подключён."
        wanted = ([w.offer_season] if w.offer_season and offer["seasons"]
                  and set(offer["seasons"]) - {w.offer_season} else [])
        pages = offer.get("pages") or [offer.get("page", "")]
        free, size = autopilot.free_space(w.kind), int(offer.get("size") or 0)
        if free and size > free and self.autopilot.replaced_by(pages, offer["hash"]) is None:
            return False, (f"Не хватит места: раздача {autopilot.gb(size)}, свободно "
                    f"{autopilot.gb(free)}. Освободите место и нажмите ещё раз.")
        try:
            said = self.autopilot.add(offer["magnet"], offer["hash"], w.kind, w.tmdb_id,
                                      offer["title"], wanted, pages, offer["seasons"],
                                      offer.get("alts") or ())
        except qbit.QbitError as e:
            return False, f"qBittorrent не взял раздачу: {e}"
        self.log(f"Слежение: в qBittorrent добавлено — {w.title}: {offer['title']}")
        return True, said + "."

    def _on_button(self, args: str) -> str:
        watch_id, _, prefix = args.partition(":")
        return self.download(watch_id, prefix)[1]
