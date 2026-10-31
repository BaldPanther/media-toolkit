"""Автоматика: новые раздачи qBittorrent в категориях «tv»/«movies» раскладываются сами.

Раз в полминуты программа смотрит раздачи этих категорий (остальные не трогает
никогда) и с каждой новой раздачей с видео:

1. ставит галки «последовательная загрузка» и «сначала первые и последние
   части» — один раз; снимете вручную — обратно не поставит;
2. опознаёт тайтл в TMDb по имени раздачи. Не уверена — спрашивает на вкладке
   «Торренты» → «Автоматика», вариантами с кнопками;
3. раскладывает через qBittorrent, как вкладка «Медиатека» (`arrange.apply`):
   загрузка продолжается под новыми именами, сразу пишутся .nfo и картинки;
4. просит Kodi просканировать папку; докачалась — ещё раз.

В Telegram автоматика не пишет: туда — только слежение (`watchlist.py`).

Торренты программа не удаляет и не останавливает. Что уже сделано — в
`autopilot_state.json`, чтобы перезапуск не начинал всё заново.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field

import arrange
import kodi
import library
import metaconf
import metadata
import nfo
import paths
import qbit

_STATE_NAME = "autopilot_state.json"
INTERVAL = 30            # секунд между проверками
KEEP_DAYS = 30           # сколько помнить раздачи, которых в qBittorrent уже нет

NEW, ASK, DONE, ERROR, SKIP = "new", "ask", "done", "error", "skip"
STATUS_LABELS = {NEW: "в очереди", ASK: "ждёт выбора", DONE: "разложено",
                 ERROR: "ошибка", SKIP: "не видео — пропущено"}


@dataclass
class Item:
    """Раздача, которую видела автоматика."""
    hash: str
    name: str
    kind: str
    status: str = NEW
    title: str = ""                # «Dark Matter (2024)» — опознанный тайтл
    root: str = ""                 # папка тайтла после раскладки
    error: str = ""
    candidates: list = field(default_factory=list)   # [[tmdb_id, «Название (год)»], …]
    tmdb_id: int = 0
    wanted: list = field(default_factory=list)   # сезоны, нужные из сборника (из поиска)
    progress: float = 0.0
    seq_set: bool = False          # галки последовательной загрузки уже ставили
    done_notified: bool = False    # докачку уже отметили (журнал, скан Kodi)
    seen: float = 0.0              # когда последний раз была в qBittorrent


# --------------------------------------------------------------------------- #
# Опознание
# --------------------------------------------------------------------------- #

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    return re.sub(r"[\W_]+", " ", text).strip()


# Год где угодно в имени: «Polden.S01.2026.WEB-DL» — после сезона, где разбор
# названия уже остановился. «1080p»/«2160p» годом не считаются.
_ANY_YEAR = re.compile(r"(?<![\d])((?:19|20)\d{2})(?![\dpPiI])")


def queries(name: str) -> list[tuple[str, int | None]]:
    """Имя раздачи → варианты (название, год) для поиска.

    Русские трекеры пишут несколько названий через « / »: «Тёмная материя / Dark
    Matter (2024) WEB-DL» — ищем по каждому. Год, найденный в любом куске, общий.
    """
    parts = [p for p in re.split(r"\s+/\s+|\s*\|\s*", name or "") if p.strip()] or [name]
    guessed = [library.guess_title_year(p) for p in parts]
    year = next((y for _, y in guessed if y), None)
    if year is None:
        m = _ANY_YEAR.search(name or "")
        year = int(m.group(1)) if m else None
    out: list[tuple[str, int | None]] = []
    for title, y in guessed:
        if title and (title, y or year) not in out:
            out.append((title, y or year))
    return out


def identify(name: str, kind: str, settings):
    """→ (SearchHit или None, кандидаты). None с кандидатами — надо спросить."""
    candidates: list = []
    for title, year in queries(name):
        hits = []
        # С годом сезона («(2026) Сезон 2» у сериала 2024-го) поиск по году
        # первого эфира ничего не даёт — тогда ищем без года.
        for y in ([year, None] if year else [None]):
            hits = metadata.search(kind, title, settings, year=y)
            if hits:
                break
        if not hits:
            continue
        if len(hits) == 1:
            return hits[0], hits
        if year:
            near = [h for h in hits if h.year and abs(h.year - year) <= 1]
            if len(near) == 1:
                return near[0], hits
        exact = [h for h in hits if _norm(h.title) == _norm(title)]
        if len(exact) == 1:
            return exact[0], hits
        candidates = candidates or hits
    return None, candidates[:5]


def known_tmdb_id(torrent: qbit.Torrent, kind: str) -> int | None:
    """Раздача уже разложена (этой программой или tinyMediaManager) — номер TMDb
    берётся из .nfo тайтла, искать по имени незачем."""
    root = torrent.content_path
    if not root.is_dir():
        root = root.parent
    name = "tvshow.nfo" if kind == library.TV else "movie.nfo"
    return nfo.read_tmdb_id(root / name)


def is_video_torrent(torrent: qbit.Torrent) -> bool:
    """Самый большой файл — видео. Так игры, музыка и программы отсеиваются сами."""
    biggest = max(torrent.files, key=lambda f: f.size, default=None)
    return biggest is not None and library.is_video(torrent.local(biggest.rel))


# --------------------------------------------------------------------------- #
# Автоматика
# --------------------------------------------------------------------------- #

class Autopilot:
    def __init__(self, log=None, on_change=None):
        self.log = log or (lambda text: None)
        self.on_change = on_change or (lambda: None)
        self.items: dict[str, Item] = {}
        self.last_check = 0.0
        self.last_error = ""
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._answers: list[tuple[str, int]] = []     # (hash, номер кандидата | -1)
        self._thread: threading.Thread | None = None
        self._kodi_off = False         # о выключенном Kodi уже сказали
        self.load()

    # ------------------------------------------------------------ состояние --
    def load(self) -> None:
        d = paths.read_json(paths.settings_path(_STATE_NAME))
        items = d.get("items", {}) if isinstance(d, dict) else {}
        for h, raw in items.items():
            try:
                self.items[h] = Item(**{k: v for k, v in raw.items()
                                        if k in Item.__dataclass_fields__})
            except TypeError:
                continue

    def save(self) -> None:
        with self._lock:
            data = {"items": {h: asdict(i) for h, i in self.items.items()}}
        try:
            paths.write_json(paths.settings_path(_STATE_NAME), data)
        except OSError as e:
            self.log(f"⚠ Автоматика: не сохранилось состояние: {e}")

    def recent(self, limit: int = 50) -> list[Item]:
        with self._lock:
            return sorted(self.items.values(), key=lambda i: i.seen, reverse=True)[:limit]

    # ------------------------------------------------------------ поток --
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="autopilot", daemon=True)
            self._thread.start()

    def wake(self) -> None:
        """Проверить сейчас, не дожидаясь очередных полминуты."""
        self._wake.set()

    def _run(self) -> None:
        while True:
            try:
                self.tick()
                self.last_error = ""
            except qbit.QbitError as e:
                if str(e) != self.last_error:
                    self.log(f"⚠ Автоматика: {e}")
                self.last_error = str(e)
            except Exception as e:  # noqa: BLE001 — фоновый поток не должен умирать
                self.last_error = str(e)
                self.log(f"⚠ Автоматика: {e}")
            self._wake.wait(INTERVAL)
            self._wake.clear()

    def answer(self, torrent_hash: str, index: int) -> None:
        """Выбор кандидата (со страницы или из Telegram); -1 — не раскладывать.
        Сама раскладка — в потоке автоматики, чтобы не пересечься с проверкой."""
        with self._lock:
            self._answers.append((torrent_hash, index))
        self._wake.set()

    def expect(self, torrent_hash: str, kind: str, tmdb_id: int, name: str,
               wanted=()) -> None:
        """Раздача добавлена из поиска: тайтл уже известен, опознавать не надо.
        `wanted` — какие сезоны нужны из сборника; файлам остальных — «не загружать»."""
        with self._lock:
            self.items[torrent_hash] = Item(torrent_hash, name, kind, tmdb_id=tmdb_id,
                                            wanted=sorted(set(wanted)), seen=time.time())
        self.save()
        self._wake.set()

    def retry(self, torrent_hash: str) -> None:
        with self._lock:
            item = self.items.get(torrent_hash)
            if item is not None and item.status in (ERROR, SKIP, DONE, ASK):
                item.status, item.error = NEW, ""
        self._wake.set()

    # ------------------------------------------------------------ проверка --
    def tick(self) -> None:
        qs = qbit.load_settings()
        if not qs.configured() or not qs.auto:
            return
        meta = metaconf.load_settings()
        if not meta.has_tmdb():
            raise qbit.QbitError("нет ключа TMDb — опознавать раздачи нечем")
        roots = meta.tv_roots + meta.movies_roots
        client = qbit.connect(qs)
        pmap = qbit.resolve_map(client, qs, roots)
        now = time.time()
        with self._lock:
            answers, self._answers = self._answers, []
        for item_info in client.torrents():
            kind = qs.kind_of(str(item_info.get("category", "")))
            if kind is None:
                continue
            h = str(item_info.get("hash", ""))
            with self._lock:
                item = self.items.get(h) or Item(h, str(item_info.get("name", "")), kind)
                self.items[h] = item
            item.seen = now
            item.progress = float(item_info.get("progress") or 0)
            if item.status == SKIP:
                continue
            torrent = qbit.torrent_of(item_info, client, pmap)
            if torrent is None:
                continue                    # нет метаданных или папка вне медиатеки
            if not is_video_torrent(torrent):
                item.status = SKIP
                continue
            if not item.seq_set:
                self._sequential(client, item_info)
                item.seq_set = True
            for h_answer, index in answers:
                if h_answer == h and item.status == ASK:
                    self._apply_answer(item, index, torrent, client, meta, roots)
            if item.status == NEW:
                self._process(item, torrent, client, meta, roots)
            if (item.status == DONE and item.progress >= 1 and not item.done_notified):
                item.done_notified = True
                self.log(f"Автоматика: скачано — {item.title}")
                self._kodi(item.root, roots)
        with self._lock:
            for h in [h for h, i in self.items.items() if now - i.seen > KEEP_DAYS * 86400]:
                del self.items[h]
        self.last_check = now
        self.save()
        self.on_change()

    def _sequential(self, client: qbit.Client, info: dict) -> None:
        if not info.get("seq_dl"):
            client.toggle_sequential(str(info["hash"]))
        if not info.get("f_l_piece_prio"):
            client.toggle_first_last(str(info["hash"]))

    def _process(self, item: Item, torrent, client, meta, roots) -> None:
        known = item.tmdb_id or known_tmdb_id(torrent, item.kind)
        if known:
            self._arrange(item, known, torrent, client, meta, roots)
            return
        hit, candidates = identify(item.name, item.kind, meta)
        if hit is None and not candidates:
            self._fail(item, "не нашлось в TMDb — разложите на вкладке «Медиатека»")
            return
        if hit is None:
            item.status = ASK
            item.candidates = [[c.tmdb_id, c.label()] for c in candidates]
            self.log(f"Автоматика: не уверена, что это — «{item.name}». Ждёт выбора.")
            return
        self._arrange(item, hit.tmdb_id, torrent, client, meta, roots)

    def _apply_answer(self, item: Item, index: int, torrent, client, meta, roots) -> None:
        if index < 0 or index >= len(item.candidates):
            item.status, item.error = SKIP, "не раскладывать — выбрано вручную"
            return
        tmdb_id, _ = item.candidates[index]
        self._arrange(item, int(tmdb_id), torrent, client, meta, roots)

    def _arrange(self, item: Item, tmdb_id: int, torrent, client, meta, roots) -> None:
        files = qbit.TorrentFiles(client, [torrent])
        folder = torrent.content_path
        try:
            skipped = self._skip_unwanted(item, torrent, client)
            extra = {p: s for p, s in files.extra().items() if p not in skipped}
            scan = library.scan_folder(folder, extra) if item.kind == library.TV else None
            info = metadata.fetch(item.kind, tmdb_id, meta,
                                  seasons=scan.seasons() if scan else [],
                                  season_sizes=scan.season_sizes() if scan else {})
            arrange.resolve_name_language(info, meta)
            chosen = arrange.choose_art(
                info, arrange.art_languages(meta.art_languages, info.original_language))
            plan = library.build_plan(folder, item.kind, info, meta, extra=extra)
            if any(r.status == library.S_COLLECTION for r in plan.rows):
                self._fail(item, "в раздаче несколько фильмов — разложите вручную")
                return
            policy = (meta.existing_policy if meta.existing_policy != metaconf.POLICY_ASK
                      else metaconf.POLICY_MISSING)
            out = arrange.apply(plan, info, chosen, policy, torrents=files)
        except Exception as e:  # noqa: BLE001 — сеть, TMDb, qBittorrent: всё в отчёт
            self._fail(item, str(e))
            return
        item.tmdb_id = tmdb_id
        item.title = library.title_with_year(info.folder_title, info.year)
        item.root = str(plan.root)
        item.candidates = []
        for e in [f"{r.row.src}: {r.error}" for r in out.failed] + out.errors:
            self.log(f"✗ Автоматика: {e}")
        if out.failed:
            self._fail(item, f"не переименовано файлов: {len(out.failed)} — см. журнал")
            return
        item.status, item.error = DONE, ""
        self.log(f"Автоматика: разложено — {item.title} ({plan.root}), "
                 f"через qBittorrent {out.via_qbit}, .nfo {out.nfo}, картинок {out.art_done}")
        self._kodi(item.root, roots)

    def _skip_unwanted(self, item: Item, torrent, client) -> set:
        """Сборник, а нужны не все сезоны: файлам лишних — «не загружать».
        → их пути, чтобы не раскладывать то, чего не будет на диске."""
        if not item.wanted or item.kind != library.TV:
            return set()
        skip, ids = set(), []
        for f in torrent.files:
            path = torrent.local(f.rel)
            if not library.is_video(path):
                continue
            season = library.parse_episodes(path)[0]
            if season is None:
                season = next((s for s in (library.season_of_dir(p.name) for p in path.parents)
                               if s is not None), None)
            if season is not None and season not in item.wanted:
                skip.add(path)
                ids.append(f.index)
        if ids:
            client.set_file_priority(torrent.hash, ids, 0)
            self.log(f"Автоматика: «{item.name}» — не загружать файлов: {len(ids)} "
                     f"(нужны сезоны {', '.join(map(str, item.wanted))})")
        return skip

    def _fail(self, item: Item, error: str) -> None:
        item.status, item.error = ERROR, error
        self.log(f"⚠ Автоматика: «{item.name}» — {error}")

    # ------------------------------------------------------------ наружу --

    def _kodi(self, root: str, roots) -> None:
        if not root:
            return
        try:
            target = kodi.scan(root, roots)
        except kodi.KodiOffline:
            # Приставка выключена — дело обычное: сказать раз и молчать, пока не ответит.
            if not self._kodi_off:
                self.log("Kodi не отвечает (приставка выключена?) — сканирование пропускаю, "
                         "пока не появится")
            self._kodi_off = True
            return
        except kodi.KodiError as e:
            self.log(f"⚠ {e}")
            return
        self._kodi_off = False
        if target:
            self.log(f"Kodi: сканирование — {target}")
