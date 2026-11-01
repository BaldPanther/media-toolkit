"""Автоматика: новые раздачи qBittorrent в категориях «tv»/«movies» раскладываются сами.

Раз в полминуты программа смотрит раздачи этих категорий (остальные не трогает
никогда) и с каждой новой раздачей с видео:

1. ставит галки «последовательная загрузка» и «сначала первые и последние
   части» — один раз; снимете вручную — обратно не поставит;
2. опознаёт тайтл в TMDb по имени раздачи. Не уверена — спрашивает на вкладке
   «Торренты» → «Автоматика», вариантами с кнопками;
3. раскладывает через qBittorrent, как вкладка «Медиатека» (`arrange.apply`):
   загрузка продолжается под новыми именами, сразу пишутся .nfo и картинки.
   Серии, которые в медиатеке уже есть (по номеру серии), — «не загружать»:
   поверх файла медиатеки раздача не пишет никогда;
4. просит Kodi просканировать папку; докачалась — ещё раз.

Раздача, добавленная программой (поиск, слежение), встаёт в qBittorrent на паузу,
как только получен список файлов, и запускается после раскладки — до неё ничего
не скачано, лишнего на диске нет. Разложить не вышло — остаётся на паузе, и
программа предупреждает: в журнале, на вкладке и в Telegram. Больше автоматика
в Telegram не пишет: туда — слежение (`watchlist.py`).

Торренты программа не удаляет. Одно исключение — обновляемая раздача: на трекере
добавили серии, у раздачи новый хеш. Новая версия получает имена уже скачанных
файлов, qBittorrent их перепроверяет и докачивает только новое, а старая версия
убирается из qBittorrent — файлы остаются на месте (`Autopilot._update`).

Что уже сделано — в `autopilot_state.json`, чтобы перезапуск не начинал всё заново.
"""
from __future__ import annotations

import collections
import re
import shutil
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import arrange
import kodi
import library
import metaconf
import metadata
import nfo
import numbering
import paths
import qbit
import search

_STATE_NAME = "autopilot_state.json"
INTERVAL = 30            # секунд между проверками
# Сколько помнить раздачу, которой в qBittorrent уже нет: в списке её не видно
# сразу, а сутки — на случай, если qBittorrent при запуске отдал список не целиком.
KEEP_GONE = 86400
# Сколько ждать список файлов у раздачи, добавленной программой: дольше — у ссылки
# почти нет живых сидов (бывает у устаревшего magnet), берём двойника.
STUCK = 300

NEW, ASK, DONE, ERROR, SKIP = "new", "ask", "done", "error", "skip"
STATUS_LABELS = {NEW: "в очереди", ASK: "ждёт выбора", DONE: "разложено",
                 ERROR: "ошибка", SKIP: "не видео — пропущено"}
S_OWNED = "уже в медиатеке"    # строка плана: серия уже есть — файл раздачи не загружать


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
    seasons: list = field(default_factory=list)  # сезоны раздачи, как их пишет трекер
    hold: bool = False             # добавлена программой на паузе — запустить после раскладки
    warned: bool = False           # о паузе из-за ошибки уже предупредили
    twins: list = field(default_factory=list)    # та же раздача по другим ссылкам (из поиска)
    waiting_since: float = 0.0     # когда добавлена программой — ждём список файлов
    progress: float = 0.0
    seq_set: bool = False          # галки последовательной загрузки уже ставили
    done_notified: bool = False    # докачку уже отметили (журнал, скан Kodi)
    seen: float = 0.0              # когда последний раз была в qBittorrent
    added_on: int = 0              # когда добавлена в qBittorrent (его added_on)
    pages: list = field(default_factory=list)    # страницы раздачи на трекерах (из поиска)
    replaces: str = ""             # хеш старой версии, которую эта обновляет
    update_step: str = ""          # шаг обновления: "" → «rename» → «check»
    update_since: float = 0.0      # когда запущена перепроверка


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


def free_space(kind: str) -> int:
    """Свободно на диске медиатеки этого вида, байт; 0 — не узнать."""
    meta = metaconf.load_settings()
    for root in meta.tv_roots if kind == library.TV else meta.movies_roots:
        try:
            return shutil.disk_usage(root).free
        except OSError:
            continue
    return 0


def gb(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} ГБ"


def update_map(new: qbit.Torrent, old: qbit.Torrent) -> list[tuple[str, str]]:
    """Файлы новой версии обновляемой раздачи → пути тех же файлов у старой.

    Тот же файл — тот же размер и расширение; при одинаковых размерах решает номер
    серии. Старая версия уже разложена, так что её пути — имена в медиатеке.
    """
    by_key: dict[tuple, list[str]] = {}
    for f in old.files:
        by_key.setdefault((f.size, Path(f.rel).suffix.lower()), []).append(f.rel)
    out, used = [], set()
    for f in new.files:
        cands = [r for r in by_key.get((f.size, Path(f.rel).suffix.lower()), []) if r not in used]
        if len(cands) > 1:
            episode = library.parse_episodes(Path(f.rel))
            cands = [r for r in cands if library.parse_episodes(Path(r)) == episode]
        if len(cands) == 1 and f.size:
            used.add(cands[0])
            out.append((f.rel, cands[0]))
    return out


# --------------------------------------------------------------------------- #
# Автоматика
# --------------------------------------------------------------------------- #

class Autopilot:
    def __init__(self, log=None, on_change=None, notify=None):
        self.log = log or (lambda text: None)
        self.on_change = on_change or (lambda: None)
        # Telegram — только чтобы предупредить: добавленная программой раздача
        # не разложилась и стоит на паузе. Остальное автоматика туда не пишет.
        self.notify = notify or (lambda text: False)
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
        """Раздачи, что есть в qBittorrent сейчас (на последней проверке), свежие сверху.
        Удалённые из qBittorrent не показываются — история остаётся в журнале."""
        with self._lock:
            present = [i for i in self.items.values() if i.seen >= self.last_check]
            return sorted(present, key=lambda i: i.added_on or i.seen, reverse=True)[:limit]

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
               wanted=(), pages=(), replaces: str = "", seasons=(), hold: bool = False,
               twins=()) -> None:
        """Раздача добавлена из поиска: тайтл уже известен, опознавать не надо.
        `wanted` — какие сезоны нужны из сборника; файлам остальных — «не загружать».
        `pages` — её страницы на трекерах: по ним узнаётся следующая версия.
        `seasons` — сезоны раздачи по трекеру, `hold` — она ждёт раскладки на паузе.
        `twins` — та же раздача по другим ссылкам ([{hash, magnet, title, pages, seasons}])."""
        now = time.time()
        with self._lock:
            self.items[torrent_hash] = Item(torrent_hash, name, kind, tmdb_id=tmdb_id,
                                            wanted=sorted(set(wanted)),
                                            seasons=sorted(set(seasons)), hold=hold,
                                            pages=[p for p in pages if p], replaces=replaces,
                                            twins=list(twins), waiting_since=now, seen=now)
        self.save()
        self._wake.set()

    def present(self) -> set[str]:
        """Хеши раздач сериалов и фильмов, что сейчас в qBittorrent (на последней проверке)."""
        with self._lock:
            return {h for h, i in self.items.items() if i.seen >= self.last_check - 1}

    def replaced_by(self, pages, new_hash: str) -> Item | None:
        """Раздача с той же страницы трекера, что ещё в qBittorrent, — старая версия."""
        pages = {search.page_key(p) for p in pages if p}
        if not pages:
            return None
        with self._lock:
            return next((i for i in self.items.values()
                         if i.hash != new_hash and {search.page_key(p) for p in i.pages} & pages
                         and not i.replaces
                         and i.status != SKIP and i.seen >= self.last_check - 1), None)

    def add(self, magnet: str, torrent_hash: str, kind: str, tmdb_id: int, name: str,
            wanted=(), pages=(), seasons=(), twins=()) -> str:
        """Раздачу — в qBittorrent с категорией сериалов или фильмов, дальше её ведёт
        автоматика. Новая версия уже скачиваемой раздачи — обновление. → что сказать.

        qBittorrent получает список файлов и встаёт на паузу: пока ничего не скачано,
        автоматика отмечает уже имеющиеся серии «не загружать», переименовывает
        остальные и запускает раздачу — лишнего на диске не появляется."""
        qs = qbit.load_settings()
        if not qs.configured():
            raise qbit.QbitError("qBittorrent не подключён")
        category = qs.tv_category if kind == library.TV else qs.movies_category
        old = self.replaced_by(pages, torrent_hash) if qs.auto else None
        qbit.connect(qs).add(magnet, category=category, stop_on_metadata=qs.auto)
        self.expect(torrent_hash, kind, tmdb_id, name, wanted, pages,
                    replaces=old.hash if old is not None else "", seasons=seasons,
                    hold=qs.auto and old is None, twins=twins)
        if old is None:
            return "Скачиваю"
        self.log(f"Автоматика: «{name}» — новая версия «{old.name}», обновляю")
        return "Новая версия вашей раздачи — докачаю только новые серии"

    def retry(self, torrent_hash: str) -> None:
        with self._lock:
            item = self.items.get(torrent_hash)
            if item is not None and item.status in (ERROR, SKIP, DONE, ASK):
                item.status, item.error, item.warned = NEW, "", False
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
        infos = client.torrents()
        by_hash = {str(i.get("hash", "")): i for i in infos}
        for item_info in infos:
            kind = qs.kind_of(str(item_info.get("category", "")))
            if kind is None:
                continue
            h = str(item_info.get("hash", ""))
            added = int(item_info.get("added_on") or 0)
            with self._lock:
                item = self.items.get(h)
                if item is not None and item.added_on and added and added != item.added_on:
                    # Удалили из qBittorrent и добавили снова — файлы у неё опять с
                    # именами из раздачи, раскладывать заново. Тайтл и нужные сезоны
                    # от прошлого раза годятся: раздача та же.
                    item = Item(h, item.name, kind, tmdb_id=item.tmdb_id, wanted=item.wanted,
                                seasons=item.seasons, pages=item.pages)
                item = item or Item(h, str(item_info.get("name", "")), kind)
                self.items[h] = item
            item.seen = now
            item.added_on = added or item.added_on
            item.progress = float(item_info.get("progress") or 0)
            if item.status != SKIP:
                self._check(item, item_info, client, pmap, by_hash, now, answers, meta, roots)
            self._settle(item, client)
        with self._lock:
            for h in [h for h, i in self.items.items() if now - i.seen > KEEP_GONE]:
                del self.items[h]
        self.last_check = now
        self.save()
        self.on_change()

    def _check(self, item: Item, info: dict, client, pmap, by_hash, now, answers, meta,
               roots) -> None:
        torrent = qbit.torrent_of(info, client, pmap)
        if torrent is None:
            # Нет метаданных — ждём. Папка вне медиатеки — ждать нечего, а добавленная
            # программой раздача так и стояла бы на паузе молча.
            if (item.hold and item.status == NEW
                    and pmap.to_local(str(info.get("save_path") or "")) is None):
                self._fail(item, "папка раздачи вне медиатеки программы — проверьте категорию "
                                 "в qBittorrent")
            elif (item.hold and item.status == NEW and item.waiting_since
                  and now - item.waiting_since > STUCK):
                self._stuck(item, client)
            return
        if not is_video_torrent(torrent):
            item.status = SKIP
            return
        if not item.seq_set:
            self._sequential(client, info)
            item.seq_set = True
        if item.replaces:
            self._update(item, info, torrent, client, pmap, by_hash, now)
            return
        for h_answer, index in answers:
            if h_answer == item.hash and item.status == ASK:
                self._apply_answer(item, index, torrent, client, meta, roots)
        if item.status == NEW:
            self._process(item, torrent, client, meta, roots)
        if item.status == DONE and item.progress >= 1 and not item.done_notified:
            item.done_notified = True
            self.log(f"Автоматика: скачано — {item.title}")
            self._kodi(item.root, roots)

    def _stuck(self, item: Item, client) -> None:
        """Раздача из программы STUCK секунд без списка файлов: у ссылки почти нет живых
        сидов — так бывает с устаревшим magnet (раздачу перезалили, а источник знает
        прежний). Есть двойник — та же раздача по другой ссылке: берём его, а застрявшую
        убираем из qBittorrent (файлов у неё нет, терять нечего). Нет — предупреждаем раз."""
        if not item.twins:
            if not item.warned:
                item.warned = True
                text = (f"«{item.name}» уже {STUCK // 60} мин без списка файлов: у этой ссылки "
                        f"почти нет живых сидов. Возьмите другую раздачу или скачайте .torrent "
                        f"со страницы трекера")
                item.error = text
                self.log(f"⚠ Автоматика: {text}")
                self.notify(f"⏳ {text}.")
            return
        twin = item.twins[0]
        qs = qbit.load_settings()
        category = qs.tv_category if item.kind == library.TV else qs.movies_category
        try:
            client.add(twin["magnet"], category=category, stop_on_metadata=True)
        except qbit.QbitError as e:
            self.log(f"⚠ Автоматика: «{item.name}» — двойник не добавился: {e}")
            item.twins = item.twins[1:]
            return
        client.remove_keep_files(item.hash)
        with self._lock:
            self.items.pop(item.hash, None)
        self.expect(twin["hash"], item.kind, item.tmdb_id, twin.get("title") or item.name,
                    item.wanted, twin.get("pages") or (), seasons=twin.get("seasons") or item.seasons,
                    hold=True, twins=item.twins[1:])
        self.log(f"Автоматика: «{item.name}» — {STUCK // 60} мин без списка файлов, беру ту же "
                 f"раздачу по другой ссылке: «{twin.get('title') or twin['hash']}»")

    def _settle(self, item: Item, client) -> None:
        """Добавленная программой раздача стоит на паузе, пока её не разложат.
        Разложена (или это не видео) — запустить. Не вышло — оставить на паузе,
        чтобы не качать мимо медиатеки и не дублировать, и предупредить один раз."""
        if not item.hold or item.replaces or item.status == NEW:
            return
        if item.status in (DONE, SKIP):
            client.start(item.hash)
            item.hold = item.warned = False
            return
        if item.warned:
            return
        item.warned = True
        why = item.error if item.status == ERROR else "не уверена, что это за тайтл"
        self.log(f"⚠ Автоматика: «{item.name}» на паузе — {why}")
        self.notify(f"⏸ Раздача на паузе: {item.name}\n\n{why}.\n\nРазберитесь на вкладке "
                    f"«Торренты» → «Автоматика» и нажмите «Заново» — или запустите её в "
                    f"qBittorrent как есть.")

    def _update(self, item: Item, info: dict, torrent, client, pmap, by_hash, now) -> None:
        """Обновление раздачи — по шагам, раз в проверку: qBittorrent делает всё не
        сразу. Список файлов есть → переименовать их в уже скачанные → перепроверить →
        старую версию убрать (файлы остаются) и запустить новую. Дальше — обычная
        раскладка: старые серии уже на местах, новые переименуются."""
        old_info = by_hash.get(item.replaces)
        if item.update_step == "":
            old = qbit.torrent_of(old_info, client, pmap) if old_info else None
            # Папка тайтла, где лежит старая версия, — «Show (2026)».
            tops = {f.rel.split("/")[0] for f in old.files if "/" in f.rel} if old else set()
            top = next(iter(tops)) if len(tops) == 1 and all("/" in f.rel for f in old.files) else ""
            same_place = bool(top) and old.save_path == torrent.save_path
            pairs = update_map(torrent, old) if same_place else []
            if not pairs:
                self.log(f"Автоматика: «{item.name}» — со старой версией общих файлов нет, "
                         f"качаю целиком")
                item.replaces = ""
                client.start(item.hash)
                return
            # Новые серии — тоже в папку тайтла: у раздачи одна общая папка, и
            # раскладка дальше смотрит только её.
            matched = {new_rel for new_rel, _ in pairs}
            moves = pairs + [(f.rel, f"{top}/{f.rel}") for f in torrent.files if f.rel not in matched]
            for new_rel, target in moves:
                if new_rel != target:
                    client.rename_file(item.hash, new_rel, target)
            self.log(f"Автоматика: «{item.name}» — уже скачано файлов: {len(pairs)}, сверяю")
            item.update_step = "rename"
        elif item.update_step == "rename":
            client.recheck(item.hash)
            item.update_step, item.update_since = "check", now
        elif item.update_step == "check":
            state = str(info.get("state", ""))
            if state.startswith("checking") or state in ("queuedForChecking", "moving"):
                return
            if item.progress <= 0 and now - item.update_since < 120:
                return                      # перепроверка ещё не началась
            if old_info is not None:
                client.remove_keep_files(item.replaces)
            client.start(item.hash)
            self.log(f"Автоматика: «{item.name}» — обновлено: уже было {item.progress:.0%}, "
                     f"старая версия убрана из qBittorrent (файлы на месте), докачиваю новое")
            item.replaces, item.update_step = "", ""

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
        if folder == torrent.save_path:
            # Файлы раздачи лежат прямо в папке категории — «папка раздачи» тогда вся
            # медиатека, а раскладывать её целиком нельзя.
            self._fail(item, "у раздачи нет своей папки — разложите на вкладке «Медиатека»")
            return
        try:
            extra, overrides = files.extra(), {}
            if item.kind == library.TV:
                full = library.scan_folder(folder, extra)
                nb = numbering.for_title(tmdb_id, meta.tmdb_key)
                tracker = self._tracker_episodes(item, full, nb) if nb.grouped else {}
                skipped = self._skip_unwanted(item, torrent, client, tracker)
                extra = {p: s for p, s in extra.items() if p not in skipped}
                scan = library.scan_folder(folder, extra)
                if nb.grouped:
                    info, overrides = self._grouped_info(tmdb_id, meta, nb, scan, tracker)
                else:
                    info = metadata.fetch(item.kind, tmdb_id, meta, seasons=scan.seasons(),
                                          season_sizes=scan.season_sizes())
            else:
                info = metadata.fetch(item.kind, tmdb_id, meta)
            arrange.resolve_name_language(info, meta)
            chosen = arrange.choose_art(
                info, arrange.art_languages(meta.art_languages, info.original_language))
            plan = library.build_plan(folder, item.kind, info, meta, overrides=overrides,
                                      extra=extra)
            if any(r.status == library.S_COLLECTION for r in plan.rows):
                self._fail(item, "в раздаче несколько фильмов — разложите вручную")
                return
            owned = self._skip_owned(item, plan, files, client) if item.kind == library.TV else 0
            if item.kind == library.MOVIE:
                self._retire_old_movie(item, plan, files, meta, roots)
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
        # Видео, которое план оставил как есть (нет номера серии, два файла на одно
        # имя), — не «разложено»: раздача качалась бы под своими именами мимо медиатеки.
        stuck = collections.Counter(r.status for r in plan.rows if r.what == "video"
                                    and r.action == library.A_SKIP and r.status != S_OWNED)
        if stuck:
            why = ", ".join(f"{status}: {n}" for status, n in stuck.items())
            self._fail(item, f"не разложено видео — {why}; разложите на вкладке «Медиатека»")
            return
        item.status, item.error = DONE, ""
        self.log(f"Автоматика: разложено — {item.title} ({plan.root}), "
                 f"через qBittorrent {out.via_qbit}, .nfo {out.nfo}, картинок {out.art_done}"
                 + (f", уже есть в медиатеке — не загружать: {owned}" if owned else ""))
        self._kodi(item.root, roots)

    def _tracker_episodes(self, item: Item, scan, nb) -> dict:
        """Серии аниме со сквозной нумерацией в TMDb → {видео: (сезон, серия)} в
        нумерации трекеров: «[ТВ-3] - 02», «S03E02» и сквозное «50» — одна (3, 2).
        Спецвыпуски и видео без номера — как есть, их разберёт план."""
        seasons = item.seasons or search.parse_title(item.name).get("seasons", [])
        single = seasons[0] if len(seasons) == 1 else None
        out = {}
        for v in scan.videos:
            season, numbers = scan.episodes_of(v)
            if not numbers or season == 0:
                continue
            explicit = library.parse_episodes(v)[0] is not None or v.parent in scan.season_dirs
            out[v] = nb.place(numbers[0], season if explicit else single)
        return out

    def _grouped_info(self, tmdb_id: int, meta, nb, scan, tracker: dict):
        """Карточка и номера серий в медиатеке для аниме со сквозной нумерацией.

        В медиатеке серия ложится так, как там уже принято у тайтла: сквозным
        номером TMDb (S01E50) или по сезонам трекеров (S03E02). → (info, overrides)."""
        lib = search.library_dir(library.TV, tmdb_id, meta.tv_roots)
        grouped_disk = bool(lib) and numbering.library_grouped(
            nb, library.scan_folder(Path(lib)).seasons())
        videos = set(scan.videos)
        overrides = {v: numbering.to_library(nb, s, e, grouped_disk)
                     for v, (s, e) in tracker.items() if v in videos}
        seasons = {nb.to_tmdb(s, e)[0] for s, e in tracker.values()}
        if any(scan.season_of(v) == 0 for v in scan.videos):
            seasons.add(0)
        info = metadata.fetch(library.TV, tmdb_id, meta, seasons=sorted(seasons))
        if grouped_disk:
            # Описания серий — из TMDb, номера в .nfo — как в имени файла.
            for season, episode in overrides.values():
                ep = info.episodes.get(nb.to_tmdb(season, episode))
                if ep is not None:
                    info.episodes[(season, episode)] = replace(ep, season=season, episode=episode)
        return info, overrides

    def _skip_unwanted(self, item: Item, torrent, client, tracker=None) -> set:
        """Сборник, а нужны не все сезоны: файлам лишних — «не загружать». Спецвыпуски
        не трогаем: их в поиске не выбрать, а качается по умолчанию всё, чего нет.
        → их пути, чтобы не раскладывать то, чего не будет на диске."""
        if not item.wanted or item.kind != library.TV:
            return set()
        tracker = tracker or {}
        skip, ids = set(), []
        for f in torrent.files:
            path = torrent.local(f.rel)
            if not library.is_video(path):
                continue
            if path in tracker:
                season = tracker[path][0]
            else:
                season = library.parse_episodes(path)[0]
                if season is None:
                    season = next((s for s in (library.season_of_dir(p.name) for p in path.parents)
                                   if s is not None), None)
            if season and season not in item.wanted:
                skip.add(path)
                ids.append(f.index)
        if ids:
            client.set_file_priority(torrent.hash, ids, 0)
            self.log(f"Автоматика: «{item.name}» — не загружать файлов: {len(ids)} "
                     f"(нужны сезоны {', '.join(map(str, item.wanted))})")
        return skip

    def _skip_owned(self, item: Item, plan, files, client) -> int:
        """Серии, что уже есть в медиатеке, — «не загружать» и не переименовывать.

        Сверка по номеру серии (S01E05), а не по размеру или названию: в медиатеке
        может лежать другая раздача. Поверх файла медиатеки раздача не пишет никогда.
        Свои файлы раздачи (раскладка заново) — не в счёт. → сколько серий."""
        present = set()
        if plan.root.is_dir():
            lib = library.scan_folder(plan.root)
            for v in lib.videos:
                if files.owns(v):
                    continue
                season, numbers = lib.episodes_of(v)
                if season is not None:
                    present.update((season, n) for n in numbers)
        ids, owned, video = [], 0, None
        for row in plan.rows:
            if row.what == "video":
                video = None
                if row.action != library.A_RENAME or row.dst is None or not files.owns(row.src):
                    continue
                season, numbers = library.parse_episodes(row.dst)
                if season is None or not numbers or not all((season, n) in present for n in numbers):
                    continue
                video = row
                owned += 1
            elif row.what != "sidecar" or video is None or not files.owns(row.src):
                if row.what != "sidecar":
                    video = None
                continue
            row.action, row.status, row.note = library.A_SKIP, S_OWNED, "не загружать"
            index = files.index_of(row.src)
            if index is not None:
                ids.append(index)
        if ids:
            client.set_file_priority(item.hash, ids, 0)
        return owned

    def _retire_old_movie(self, item: Item, plan, files, meta, roots) -> None:
        """Новая версия фильма (4K вместо Full HD): прежний видеофайл — в Extras фильма.
        Kodi его фильмом не покажет, удалите сами, когда убедитесь в новой: программа
        файлы не удаляет. Если его ещё раздаёт старая раздача — переносит qBittorrent,
        чтобы она не сломалась.

        Внешние субтитры: есть свои в новой раздаче — они и встают под имя фильма (по
        времени совпадают с новым видео), прежние едут в Extras вместе со старым видео;
        своих нет — прежние остаются, фильм тот же. .edl остаётся всегда: если чуть
        разойдётся, его проще подогнать, чем делать заново."""
        row = next((r for r in plan.rows if r.what == "video" and r.action == library.A_RENAME
                    and r.dst is not None and files.owns(r.src)), None)
        if row is None or not row.dst.exists() or files.owns(row.dst):
            return
        new_subs = any(r.what == "sidecar" and r.action == library.A_RENAME and files.owns(r.src)
                       and r.src.suffix.lower() in library.SUB_EXTS for r in plan.rows)
        old = [row.dst] + [p for p in library.find_sidecars(row.dst)
                           if new_subs and p.suffix.lower() in library.SUB_EXTS]
        extras = plan.root / meta.extras_folder
        owners = qbit.torrents_in(plan.root, qbit.load_settings(), roots)
        moved = []
        for path in old:
            target, n = extras / path.name, 2
            while target.exists():
                target, n = extras / f"{path.stem} ({n}){path.suffix}", n + 1
            if owners is not None and owners.owns(path):
                owners.rename(path, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                path.rename(target)
            moved.append(target.name)
        if owners is not None:
            owners.wait()
        # movie.nfo остаётся прежним (описание то же), а исходное имя — уже нового файла.
        if (plan.root / "movie.nfo").is_file():
            nfo.set_original_filename(plan.root / "movie.nfo", row.src.name)
        self.log(f"Автоматика: «{item.name}» — новая версия фильма, прежнее отложено в "
                 f"{meta.extras_folder}: {', '.join(moved)}")

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
