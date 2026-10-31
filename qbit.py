"""qBittorrent: раздачи через его веб-API.

Раздачу, которая ещё качается, нельзя переименовать на диске: qBittorrent потеряет
файлы и начнёт качать их заново. Его API (`torrents/renameFile`) переименовывает
файлы внутри раздачи — в том числе ещё не созданные — и дальше качает уже под
новыми именами. Поэтому сериал раскладывается сразу после добавления, а первые
серии можно смотреть, пока докачиваются остальные.

qBittorrent и программа видят медиатеку под разными путями: на NAS у qBittorrent
`/data/media/tv`, у программы в Docker — `/media/disk1/nas/media/tv`, на маке —
`/Volumes/nas/media/tv`. `PathMap` переводит одно в другое; соответствие
находится само — по папкам категорий qBittorrent и корням библиотеки.

Только stdlib — модуль пригоден для headless-тестов.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

import library
import paths

_SETTINGS_NAME = "qbit_settings.json"
TIMEOUT = 10
UA = "media-toolkit/1.0"


class QbitError(RuntimeError):
    """qBittorrent недоступен, не пускает или отказал в операции."""


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

@dataclass
class Settings:
    url: str = ""            # адрес веб-интерфейса: http://192.168.1.10:8080
    username: str = ""
    password: str = ""
    # Соответствие путей вручную. Оба пустые — определяется само.
    remote_root: str = ""    # как путь видит qBittorrent: /data
    local_root: str = ""     # он же у программы: /media/disk1/nas
    # Автоматика (`autopilot.py`): новые раздачи этих категорий раскладываются
    # сами. Остальные категории программа не трогает никогда.
    auto: bool = False
    tv_category: str = "tv"
    movies_category: str = "movies"

    def configured(self) -> bool:
        return bool(self.url.strip())

    def kind_of(self, category: str):
        """Категория раздачи → library.TV / library.MOVIE или None (не наша)."""
        cat = (category or "").strip().casefold()
        if cat and cat == self.tv_category.strip().casefold():
            return library.TV
        if cat and cat == self.movies_category.strip().casefold():
            return library.MOVIE
        return None


def load_settings() -> Settings:
    d = paths.read_json(paths.settings_path(_SETTINGS_NAME))
    if isinstance(d, dict):
        try:
            base = Settings()
            return Settings(
                url=str(d.get("url", "")),
                username=str(d.get("username", "")),
                password=str(d.get("password", "")),
                remote_root=str(d.get("remote_root", "")),
                local_root=str(d.get("local_root", "")),
                auto=bool(d.get("auto", base.auto)),
                tv_category=str(d.get("tv_category", base.tv_category)),
                movies_category=str(d.get("movies_category", base.movies_category)),
            )
        except Exception:  # noqa: BLE001 — поле неожиданного типа: значения по умолчанию
            pass
    return Settings()


def save_settings(s: Settings) -> None:
    paths.write_json(paths.settings_path(_SETTINGS_NAME), asdict(s))


# --------------------------------------------------------------------------- #
# Клиент API
# --------------------------------------------------------------------------- #

class Client:
    """Веб-API qBittorrent (v2). Вход — по cookie SID, повторный — сам на 403."""

    def __init__(self, url: str, username: str = "", password: str = "",
                 timeout: int = TIMEOUT):
        base = url.strip().rstrip("/")
        if base and "://" not in base:
            base = "http://" + base
        self.base = base
        self.username = username
        self.password = password
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _send(self, path: str, data: dict | None = None, params: dict | None = None) -> bytes:
        url = f"{self.base}/api/v2/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        # Referer — для защиты от CSRF: с ней qBittorrent сверяет Host.
        req = urllib.request.Request(url, data=body, headers={
            "User-Agent": UA, "Referer": self.base})
        with self._opener.open(req, timeout=self.timeout) as r:
            return r.read()

    def _call(self, path: str, data: dict | None = None, params: dict | None = None) -> bytes:
        for attempt in (0, 1):
            try:
                return self._send(path, data, params)
            except urllib.error.HTTPError as e:
                # 403 — не вошли или сессия истекла. Из белого списка подсетей
                # qBittorrent пускает и без входа, поэтому входим только по требованию.
                if e.code == 403 and attempt == 0:
                    self.login()
                    continue
                raise QbitError(f"qBittorrent ответил HTTP {e.code} на {path}") from e
            except urllib.error.URLError as e:
                raise QbitError(f"qBittorrent недоступен ({self.base}): "
                                f"{getattr(e, 'reason', e)}") from e
            except OSError as e:
                raise QbitError(f"qBittorrent недоступен ({self.base}): {e}") from e
        raise QbitError(f"qBittorrent не пускает к {path}")

    def _json(self, path: str, params: dict | None = None):
        raw = self._call(path, params=params)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as e:
            raise QbitError(f"qBittorrent: битый ответ на {path}") from e

    def login(self) -> None:
        """Вход. Успех — «Ok.» (до 5.x) или пустой ответ 204 (5.x); отказ —
        «Fails.» или 401."""
        if not self.username:
            raise QbitError("qBittorrent просит логин — впишите его в «Настройках» → «qBittorrent».")
        try:
            text = self._send("auth/login", {"username": self.username,
                                             "password": self.password})
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise QbitError("qBittorrent: неверный логин или пароль.") from e
            if e.code == 403:
                raise QbitError("qBittorrent временно не пускает: слишком много неудачных "
                                "входов с этого адреса.") from e
            raise QbitError(f"qBittorrent ответил HTTP {e.code} на вход") from e
        except (urllib.error.URLError, OSError) as e:
            raise QbitError(f"qBittorrent недоступен ({self.base}): "
                            f"{getattr(e, 'reason', e)}") from e
        if text.decode("utf-8", "replace").strip() not in ("Ok.", ""):
            raise QbitError("qBittorrent: неверный логин или пароль.")

    def version(self) -> str:
        return self._call("app/version").decode("utf-8", "replace").strip()

    def default_save_path(self) -> str:
        return self._call("app/defaultSavePath").decode("utf-8", "replace").strip()

    def categories(self) -> dict:
        data = self._json("torrents/categories")
        return data if isinstance(data, dict) else {}

    def torrents(self) -> list[dict]:
        data = self._json("torrents/info")
        return data if isinstance(data, list) else []

    def files(self, torrent_hash: str) -> list[dict]:
        data = self._json("torrents/files", {"hash": torrent_hash})
        return data if isinstance(data, list) else []

    def rename_file(self, torrent_hash: str, old: str, new: str) -> None:
        try:
            self._call("torrents/renameFile",
                       {"hash": torrent_hash, "oldPath": old, "newPath": new})
        except QbitError as e:
            raise QbitError(f"qBittorrent не переименовал «{old}» → «{new}»: {e}") from e

    def toggle_sequential(self, torrent_hash: str) -> None:
        """Последовательная загрузка — переключатель, а не «включить»: зовём, только
        если `seq_dl` у раздачи ещё false."""
        self._call("torrents/toggleSequentialDownload", {"hashes": torrent_hash})

    def toggle_first_last(self, torrent_hash: str) -> None:
        """«Сначала первые и последние части» — тоже переключатель (`f_l_piece_prio`)."""
        self._call("torrents/toggleFirstLastPiecePrio", {"hashes": torrent_hash})

    def add(self, url: str, category: str = "", sequential: bool = True) -> None:
        """Добавить раздачу по magnet- или http-ссылке на .torrent.

        Папка — по категории (у категорий tv/movies это корни медиатеки).
        """
        data = {"urls": url, "sequentialDownload": str(sequential).lower(),
                "firstLastPiecePrio": str(sequential).lower()}
        if category:
            data["category"] = category
        # До 5.x — «Ok.» / «Fails.», в 5.x — JSON со счётчиками.
        text = self._call("torrents/add", data).decode("utf-8", "replace").strip()
        if not text or text == "Ok.":
            return
        try:
            result = json.loads(text)
        except ValueError:
            result = None
        if isinstance(result, dict) and not result.get("failure_count"):
            return
        raise QbitError("qBittorrent не добавил раздачу" + ("" if isinstance(result, dict) else f": {text}"))

    def set_file_priority(self, torrent_hash: str, ids, priority: int) -> None:
        """Приоритет файлов раздачи по их номерам; 0 — не загружать."""
        ids = list(ids)
        if ids:
            self._call("torrents/filePrio", {"hash": torrent_hash, "id": "|".join(map(str, ids)),
                                             "priority": str(priority)})


# --------------------------------------------------------------------------- #
# Пути
# --------------------------------------------------------------------------- #

_WIN_PATH = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")


def _pure(path: str):
    """Путь в понятиях той машины, где он записан: qBittorrent может жить и на Windows."""
    return PureWindowsPath(path) if _WIN_PATH.match(path) else PurePosixPath(path)


def _key(path) -> str:
    """Путь для сравнения: SMB и macOS отдают имена в NFD, API — в NFC."""
    return unicodedata.normalize("NFC", str(path))


def _parts(path) -> tuple[str, ...]:
    return Path(_key(path)).parts


def _within(path, parent) -> bool:
    p, root = _parts(path), _parts(parent)
    return p[:len(root)] == root


@dataclass(frozen=True)
class PathMap:
    """Префикс пути у qBittorrent ↔ тот же префикс у программы.

    Оба пустые — пути совпадают: программа и qBittorrent на одной машине.
    """
    remote: str = ""
    local: str = ""

    def to_local(self, remote_path: str) -> Path | None:
        if not remote_path:
            return None
        if not self.remote:
            return Path(remote_path)
        parts, prefix = _pure(remote_path).parts, _pure(self.remote).parts
        if parts[:len(prefix)] != prefix:
            return None
        return Path(self.local).joinpath(*parts[len(prefix):])


def detect_map(remote_dirs, local_dirs) -> PathMap | None:
    """Соответствие путей по общему хвосту: `/data/media/tv` и
    `/media/disk1/nas/media/tv` дают `/data` ↔ `/media/disk1/nas`.

    Из всех пар берётся та, у которой общий хвост длиннее. Совпали целиком —
    пути одинаковые, переводить нечего.
    """
    best: PathMap | None = None
    best_len = 0
    for remote in remote_dirs:
        if not remote:
            continue
        rp = _pure(remote)
        for local in local_dirs:
            if not local:
                continue
            lp = Path(local)
            r_parts, l_parts = rp.parts[1:], lp.parts[1:]     # без корня: «/», «D:\»
            n = 0
            while (n < len(r_parts) and n < len(l_parts)
                   and _key(r_parts[-1 - n]).casefold() == _key(l_parts[-1 - n]).casefold()):
                n += 1
            if (n == len(r_parts) == len(l_parts)
                    and rp.anchor.replace("\\", "/") == lp.anchor.replace("\\", "/")):
                return PathMap()
            if n > best_len:
                best_len = n
                best = PathMap(str(rp.parents[n - 1]), str(lp.parents[n - 1]))
    return best


def resolve_map(client: Client, settings: Settings, library_roots) -> PathMap:
    if settings.remote_root.strip() or settings.local_root.strip():
        return PathMap(settings.remote_root.strip(), settings.local_root.strip())
    remote = [c.get("savePath", "") for c in client.categories().values()
              if isinstance(c, dict)]
    remote.append(client.default_save_path())
    found = detect_map(remote, list(library_roots))
    if found is None:
        raise QbitError("Не удалось сопоставить папки qBittorrent с корнями библиотеки. "
                        "Укажите корни в «Настройках» или соответствие путей в разделе "
                        "«qBittorrent».")
    return found


# --------------------------------------------------------------------------- #
# Раздачи
# --------------------------------------------------------------------------- #

@dataclass
class TorrentFile:
    rel: str          # путь внутри раздачи, как его знает qBittorrent («/» всегда)
    size: int
    progress: float
    index: int = 0    # номер файла в раздаче — для приоритетов


@dataclass
class Torrent:
    hash: str
    name: str
    category: str
    progress: float
    save_path: Path        # уже в путях программы
    content_path: Path
    files: list[TorrentFile] = field(default_factory=list)

    def local(self, rel: str) -> Path:
        return self.save_path.joinpath(*PurePosixPath(rel).parts)

    def rel_of(self, path: Path) -> str | None:
        parts, base = _parts(path), _parts(self.save_path)
        if parts[:len(base)] != base or len(parts) == len(base):
            return None
        return PurePosixPath(*parts[len(base):]).as_posix()


def torrent_of(item: dict, client: Client, pmap: PathMap) -> Torrent | None:
    """Раздача из ответа `torrents/info` — в путях программы и со списком файлов.

    None — её папка вне медиатеки программы или у magnet ещё нет метаданных.
    """
    content = pmap.to_local(str(item.get("content_path") or ""))
    save = pmap.to_local(str(item.get("save_path") or ""))
    if content is None or save is None:
        return None
    files = [TorrentFile(str(f.get("name", "")), int(f.get("size") or 0),
                         float(f.get("progress") or 0), int(f.get("index", n)))
             for n, f in enumerate(client.files(str(item.get("hash", ""))))]
    if not files:
        return None
    return Torrent(str(item.get("hash", "")), str(item.get("name", "")),
                   str(item.get("category", "")), float(item.get("progress") or 0),
                   save, content, files)


def find_torrents(folder, client: Client, pmap: PathMap) -> list[Torrent]:
    """Раздачи, чьи файлы лежат в `folder`, — или внутри которых лежит `folder`."""
    folder = Path(folder)
    found = []
    for item in client.torrents():
        content = pmap.to_local(str(item.get("content_path") or ""))
        if content is None or not (_within(content, folder) or _within(folder, content)):
            continue
        torrent = torrent_of(item, client, pmap)
        if torrent is not None:
            found.append(torrent)
    return found


class TorrentFiles:
    """Файлы раздач в папке тайтла: что из плана переименовывает qBittorrent.

    План строится по диску плюс по списку раздачи (`extra`): серий, до которых
    загрузка ещё не дошла, на диске нет, а переименовать их надо уже сейчас.
    При применении `library.apply_plan` отдаёт сюда строки с файлами раздачи.
    """

    def __init__(self, client: Client, torrents: list[Torrent]):
        self.client = client
        self.torrents = torrents
        self._owner: dict[str, tuple[Torrent, str]] = {}
        for t in torrents:
            for f in t.files:
                self._owner[_key(t.local(f.rel))] = (t, f.rel)
        self._pending: dict[str, set[str]] = {}

    def extra(self) -> dict[Path, int]:
        return {t.local(f.rel): f.size for t in self.torrents for f in t.files}

    def owns(self, path) -> bool:
        return path is not None and _key(path) in self._owner

    def describe(self) -> str:
        return ", ".join(f"«{t.name}» ({t.progress:.0%})" for t in self.torrents)

    def rename(self, src: Path, dst: Path) -> None:
        """Переименовать файл раздачи. Кладёт заявку — переезд у qBittorrent асинхронный."""
        torrent, old = self._owner[_key(src)]
        new = torrent.rel_of(dst)
        if new is None:
            raise QbitError(f"«{dst}» вне папки раздачи «{torrent.name}» — "
                            "qBittorrent переносит файлы только внутри неё.")
        self.client.rename_file(torrent.hash, old, new)
        del self._owner[_key(src)]
        self._owner[_key(dst)] = (torrent, new)
        pending = self._pending.setdefault(torrent.hash, set())
        pending.discard(old)            # временное имя при развязке циклов
        pending.add(new)

    def wait(self, timeout: float = 60, stop=None) -> list[Path]:
        """Ждёт, пока qBittorrent переложит файлы. → какие так и не появились."""
        by_hash = {t.hash: t for t in self.torrents}
        deadline = time.monotonic() + timeout
        left = {h: set(names) for h, names in self._pending.items() if names}
        while left:
            for h in list(left):
                try:
                    left[h] -= {str(f.get("name", "")) for f in self.client.files(h)}
                except QbitError:
                    continue            # разовый сбой связи — спросим на следующем круге
                if not left[h]:
                    del left[h]
            if not left or time.monotonic() >= deadline or (stop and stop()):
                break
            time.sleep(0.5)
        self._pending.clear()
        return sorted(by_hash[h].local(n) for h, names in left.items() for n in names)


def make_dirs(path) -> None:
    """Создаёт недостающие папки с правами и владельцем родителя.

    Программа в Docker работает от root, qBittorrent — от своего пользователя.
    Папка сезона, созданная программой под `.nfo` раньше, чем qBittorrent дошёл
    до первой серии, получила бы 755 root — и qBittorrent не смог бы положить в
    неё файл: раздача встала бы с ошибкой. С правами родителя (на NAS — 777 от
    Samba) такого не случится.
    """
    missing = []
    p = Path(path)
    while not p.exists() and p.parent != p:
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        parent = d.parent.stat()
        d.mkdir(exist_ok=True)
        os.chmod(d, parent.st_mode & 0o7777)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(d, parent.st_uid, parent.st_gid)


# --------------------------------------------------------------------------- #
# Для вкладок
# --------------------------------------------------------------------------- #

def connect(settings: Settings) -> Client:
    return Client(settings.url, settings.username, settings.password)


def torrents_in(folder, settings: Settings, library_roots) -> TorrentFiles | None:
    """Раздачи в папке тайтла. None — qBittorrent не настроен или раздач там нет."""
    if not settings.configured():
        return None
    client = connect(settings)
    pmap = resolve_map(client, settings, library_roots)
    found = find_torrents(folder, client, pmap)
    return TorrentFiles(client, found) if found else None


def check(settings: Settings, library_roots) -> str:
    """Проверка из настроек: версия, соответствие путей, сколько раздач."""
    client = connect(settings)
    version = client.version()
    pmap = resolve_map(client, settings, library_roots)
    count = len(client.torrents())
    where = (f"пути «{pmap.remote}» ↔ «{pmap.local}»" if pmap.remote
             else "пути совпадают")
    return f"qBittorrent {version}, раздач: {count}, {where}."
