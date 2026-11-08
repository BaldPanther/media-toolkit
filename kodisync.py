"""Kodi и медиатека — без столкновений с самим Kodi.

Kodi выполняет «просканировать папку» (`VideoLibrary.Scan`) встроенной командой
`UpdateLibrary`, а она — выключатель: если Kodi уже сканирует, он это сканирование
останавливает, а новое не начинает (Kodi 22, `LibraryBuiltins.cpp`). Так скан из
программы обрывал собственное обновление Kodi при запуске, и сериал добавлялся
наполовину. Поэтому всё, что программа просит у Kodi, идёт через очередь (`KodiSync`):

- скан или очистка папки тайтла ставятся в очередь (`request`) и уходят по одной, только
  когда Kodi ничего не сканирует и не чистит (`Library.IsScanningVideo`). Очистку Kodi
  во время скана просто не начинает, а скан — обрывает, так что ждём обоих;
- Kodi выключен — очередь ждёт (она на диске и переживает перезапуск программы).
  Включился — сначала `GRACE` секунд на его собственное «Обновлять медиатеку при запуске»;
- раз в `CHECK` секунд медиатека на диске сверяется с прошлым разом (`check`) — то, что
  делал аддон Library Watchdog: пропали видео тайтла (удалили в Kodi или с компьютера,
  раскладка переименовала) — очистка его папки в Kodi; появились новые и за проверку не
  изменились — скан. Kodi, удаляя фильм, стирает только видеофайл — папка с .nfo,
  картинками и .edl остаётся. С галкой «убирать и папку» (`kodi.Settings.remove_folders`)
  такая папка — была с видео, видео не осталось — убирается целиком: в корзину
  накопителя (`.trash` у ZimaOS/CasaOS), иначе в Корзину системы или насовсем. Папки, где
  лежит раздача qBittorrent, не трогаются: её видео может быть ещё не создано.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

import kodi
import library
import paths

_QUEUE_NAME = "kodi_queue.json"
_SNAPSHOT_NAME = "library_snapshot.json"
CHECK = 300          # секунд между сверками медиатеки
GRACE = 120          # секунд после включения Kodi — на его собственное обновление
SCAN, CLEAN = "scan", "clean"
# Папки внутри тайтла, где видео — не сам фильм: удалённый фильм их не держит.
EXTRAS = {"extras", "featurettes", "behind the scenes", "deleted scenes", "trailers",
          "sample", "samples", "other", "interviews", "scenes", "shorts"}


def _skip_dir(name: str) -> bool:
    """Служебные папки накопителя и систем: корзины, превью Synology."""
    return name.startswith((".", "@", "#", "$"))


def snapshot(tv_roots, movies_roots, extras_name: str = "Extras") -> dict[str, dict[str, int]]:
    """Видео медиатеки по тайтлам: {папка тайтла: {путь видео в ней: размер}}.

    Тайтл сериала — папка прямо в корне сериалов; фильма — папка с видео (папки
    дополнительных материалов — Extras и т. п. — относятся к фильму над ними). Видео
    прямо в корне — не тайтл. Корня нет (накопитель не подключён) — его тайтлов нет в
    ответе, и `check` их не трогает."""
    extras = EXTRAS | {extras_name.casefold()}
    out: dict[str, dict[str, int]] = {}
    for roots, tv in ((tv_roots, True), (movies_roots, False)):
        for root in map(Path, roots):
            if not root.is_dir():
                continue
            for folder, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if not _skip_dir(d)]
                here = Path(folder)
                for name in files:
                    video = here / name
                    if not library.is_video(video):
                        continue
                    title = _title_of(video, root, tv, extras)
                    if title is None:
                        continue
                    try:
                        size = video.stat().st_size
                    except OSError:
                        continue
                    out.setdefault(str(title), {})[video.relative_to(title).as_posix()] = size
    return out


def _title_of(video: Path, root: Path, tv: bool, extras) -> Path | None:
    parts = video.relative_to(root).parts
    if len(parts) < 2:
        return None
    if tv:
        return root / parts[0]
    folder = video.parent
    while folder != root and folder.name.casefold() in extras:
        folder = folder.parent
    return None if folder == root else folder


def main_videos(videos: dict[str, int], extras_name: str = "Extras") -> list[str]:
    """Видео тайтла без дополнительных материалов."""
    extras = EXTRAS | {extras_name.casefold()}
    return [rel for rel in videos
            if not any(p.casefold() in extras for p in rel.split("/")[:-1])]


def remove_folder(folder: Path) -> str:
    """Папка тайтла целиком — в корзину накопителя (`.trash` выше по пути, на том же
    диске: переезд мгновенный, а корзину ZimaOS чистит сама), иначе в Корзину системы
    (Windows, macOS), иначе насовсем. → куда делась (для журнала)."""
    trash = _drive_trash(folder)
    if trash is not None:
        dest = trash / folder.name
        if dest.exists():
            dest = trash / f"{folder.name} {time.strftime('%Y-%m-%d %H-%M-%S')}"
        shutil.move(str(folder), str(dest))
        return f"в корзину накопителя ({trash})"
    if sys.platform in ("win32", "darwin"):
        try:
            from send2trash import send2trash
            send2trash(str(folder))
            return "в Корзину"
        except (ImportError, OSError):
            pass
    shutil.rmtree(folder)
    return "насовсем"


def _drive_trash(folder: Path) -> Path | None:
    try:
        dev = folder.stat().st_dev
    except OSError:
        return None
    for parent in folder.parents:
        candidate = parent / ".trash"
        try:
            if candidate.is_dir() and candidate.stat().st_dev == dev:
                return candidate
        except OSError:
            continue
    return None


class KodiSync:
    def __init__(self, log=None):
        self.log = log or (lambda text: None)
        # Очередь трогают поток автоматики и вкладка «Медиатека».
        self._lock = threading.RLock()
        self.queue: list[list[str]] = []        # [[SCAN|CLEAN, папка тайтла], …]
        self.last_check = 0.0
        self._offline = False                   # Kodi не отвечал — сказали и ждём
        self._online_since: float | None = None
        self._fresh: dict[str, int] = {}        # новое видео → размер при первой встрече
        d = paths.read_json(paths.settings_path(_QUEUE_NAME))
        if isinstance(d, dict):
            self.queue = [[str(op), str(p)] for op, p in d.get("queue", [])
                          if op in (SCAN, CLEAN)]

    # ------------------------------------------------------------ очередь --
    def request(self, folder, op: str = SCAN) -> None:
        """Поставить папку тайтла в очередь на скан или очистку в Kodi."""
        item = [op, str(folder)]
        with self._lock:
            if item not in self.queue:
                self.queue.append(item)
                self._save()

    def _save(self) -> None:
        try:
            paths.write_json(paths.settings_path(_QUEUE_NAME), {"queue": self.queue})
        except OSError as e:
            self.log(f"⚠ Kodi: не сохранилась очередь: {e}")

    def _next(self) -> list[str]:
        """Очистка — раньше скана той же папки: сначала убрать старые имена, потом
        добавить новые."""
        return next((i for i in self.queue if i[0] == CLEAN), self.queue[0])

    def flush(self, roots, now: float | None = None) -> None:
        """Отправить в Kodi одно дело из очереди, если он включён и свободен."""
        with self._lock:
            self._flush(roots, time.time() if now is None else now)

    def _flush(self, roots, now: float) -> None:
        s = kodi.load_settings()
        if not s.configured():
            if self.queue:
                self.queue = []
                self._save()
            return
        if not self.queue:
            return
        client = kodi.Client(s.url, s.username, s.password)
        try:
            busy = client.busy()
            if self._offline:
                self._offline = False
                self._online_since = now
                self.log(f"Kodi снова на связи — очередь ({len(self.queue)}) отправлю, когда он "
                         f"закончит своё обновление")
            if busy or (self._online_since is not None and now - self._online_since < GRACE):
                return
            pmap = kodi.detect_map(client.sources(), roots)
            op, folder = item = self._next()
            target = (pmap.to_kodi(folder) if pmap else None) or ""
            if op == CLEAN and not target:
                # Папка вне медиатеки Kodi (раздача до раскладки) — чистить нечего, а пустая
                # папка значила бы очистку всей медиатеки.
                self.queue.remove(item)
                self._save()
                return
            if op == CLEAN:
                client.clean(target)
            else:
                client.scan(target)
        except kodi.KodiOffline:
            if not self._offline:
                self.log(f"Kodi не отвечает (приставка выключена?) — в очереди {len(self.queue)}, "
                         f"отправлю, когда появится")
            self._offline = True
            self._online_since = None
            return
        except kodi.KodiError as e:
            self.log(f"⚠ {e}")
            return
        self.queue.remove(item)
        self._save()
        what = "очистка" if op == CLEAN else "сканирование"
        self.log(f"Kodi: {what} — {target or 'вся медиатека'}")

    # ------------------------------------------------------------ медиатека --
    def check(self, tv_roots, movies_roots, extras_name: str = "Extras", owned=None,
              now: float | None = None) -> None:
        """Сверка медиатеки с прошлым разом — не чаще раза в `CHECK` секунд.

        `owned(папка)` → True, если там лежит раздача qBittorrent (её не убирать);
        None — проверить нечем, папки не убираются вовсе."""
        now = time.time() if now is None else now
        if now - self.last_check < CHECK:
            return
        with self._lock:
            self.last_check = now
            self._check(tv_roots, movies_roots, extras_name, owned)

    def _check(self, tv_roots, movies_roots, extras_name: str, owned) -> None:
        s = kodi.load_settings()
        if not s.configured():
            return
        path = paths.settings_path(_SNAPSHOT_NAME)
        stored = paths.read_json(path)
        new = snapshot(tv_roots, movies_roots, extras_name)
        if not isinstance(stored, dict) or not isinstance(stored.get("titles"), dict):
            self._write(path, {"titles": new, "suspect": []})
            return
        old = stored["titles"]
        # Тайтлы без видео с прошлой сверки — на диске: подтверждение переживает перезапуск.
        suspect = {str(t) for t in stored.get("suspect") or []}
        live = [Path(r) for r in list(tv_roots) + list(movies_roots) if Path(r).is_dir()]

        def reachable(title: str) -> bool:
            return any(Path(title).is_relative_to(r) for r in live)

        old_files = {f"{t}/{rel}": size for t, v in old.items() for rel, size in v.items()}
        new_files = {f"{t}/{rel}": size for t, v in new.items() for rel, size in v.items()}
        # Пропали видео — в Kodi остались записи на них.
        for title, videos in old.items():
            if not reachable(title):
                new.setdefault(title, videos)      # накопитель не подключён — как было
                continue
            if any(f"{title}/{rel}" not in new_files for rel in videos):
                self.request(title, CLEAN)
        # Новые видео — в Kodi, когда за сверку не изменились (докопировались).
        for f, size in new_files.items():
            if f in old_files and f not in self._fresh:
                continue
            if f not in self._fresh:
                self._fresh[f] = size
            elif self._fresh.pop(f) == size:
                self.request(_title_from(f, new), SCAN)
            else:
                self._fresh[f] = size
        self._fresh = {f: v for f, v in self._fresh.items() if f in new_files}
        suspect = (self._remove_empty(old, new, suspect, extras_name, owned)
                   if s.remove_folders else set())
        data = {"titles": new, "suspect": sorted(suspect)}
        if data != stored:
            self._write(path, data)

    def _remove_empty(self, old, new, suspect: set, extras_name: str, owned) -> set:
        """Тайтлы, у которых были видео, а теперь нет (кроме доп. материалов), — папку
        целиком: со второй сверки подряд (первая только отмечает, `suspect`) и если там
        нет раздачи. → отмеченные для следующей сверки."""
        def empty(title: str) -> bool:
            return Path(title).is_dir() and not main_videos(new.get(title, {}), extras_name)

        confirmed = {t for t in suspect if empty(t)}
        later = confirmed | {t for t, v in old.items() if main_videos(v, extras_name) and empty(t)}
        if owned is None:
            return later
        for title in sorted(confirmed):
            folder = Path(title)
            if owned(folder):
                continue
            try:
                where = remove_folder(folder)
            except OSError as e:
                self.log(f"⚠ Медиатека: «{folder.name}» без видео, но папка не убралась: {e}")
                continue
            later.discard(title)
            new.pop(title, None)
            self.log(f"Медиатека: видео «{folder.name}» удалено — папка убрана {where}")
            self.request(title, CLEAN)
        return later

    def _write(self, path: Path, data) -> None:
        try:
            paths.write_json(path, data)
        except OSError as e:
            self.log(f"⚠ Медиатека: не сохранился снимок: {e}")


def _title_from(file_key: str, titles: dict) -> str:
    """Папка тайтла по ключу «папка/путь видео» — самая длинная подходящая."""
    return max((t for t in titles if file_key.startswith(t + "/")), key=len)
