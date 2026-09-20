"""Скачивание картинок медиатеки и кэш миниатюр для сетки выбора.

Имена файлов — те же, что уже лежат в медиатеке после tinyMediaManager, иначе
Kodi не подхватит: `poster.jpg`, `fanart.jpg`, `clearlogo.png`,
`seasonNN-poster.jpg`, `season-specials-poster.jpg`.

Расширение задаётся видом арта, а не источником: постеры и фоны на обоих
источниках — JPEG, логотипы — PNG с прозрачностью. Kodi ориентируется на имя
файла, поэтому смешивать нельзя.
"""
from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import library
import metadata
import net
from metaconf import POLICY_ASK, POLICY_MISSING, POLICY_OVERWRITE

# Вид арта → расширение файла на диске.
_EXT = {
    metadata.ART_POSTER: ".jpg",
    metadata.ART_FANART: ".jpg",
    metadata.ART_LOGO: ".png",
    metadata.ART_SEASON: ".jpg",
}

# Порядок задач в логе и прогрессе: как человек смотрит на карточку тайтла,
# а не по алфавиту.
_ORDER = {metadata.ART_POSTER: 0, metadata.ART_FANART: 1,
          metadata.ART_LOGO: 2, metadata.ART_SEASON: 3}

DO_DOWNLOAD = "download"
DO_REPLACE = "replace"
DO_SKIP = "skip"
DO_ASK = "ask"


def target_name(kind: str, season: int | None = None) -> str:
    """Имя файла картинки в папке тайтла."""
    if kind == metadata.ART_SEASON:
        if season is None:
            raise ValueError("для постера сезона нужен номер сезона")
        return library.season_poster_name(season)
    return f"{kind}{_EXT[kind]}"


@dataclass
class ArtTask:
    kind: str
    candidate: metadata.ArtCandidate
    dest: Path
    action: str
    season: int | None = None

    @property
    def exists(self) -> bool:
        return self.action in (DO_REPLACE, DO_SKIP, DO_ASK)


@dataclass
class ArtResult:
    task: ArtTask
    ok: bool
    error: str = ""
    skipped: bool = False


def plan_art(root, chosen: dict, policy: str = POLICY_MISSING) -> list[ArtTask]:
    """Выбранные кандидаты → список задач на скачивание.

    `chosen` — {(вид, сезон): ArtCandidate}; сезон у постера тайтла None.
    Политика решает судьбу уже лежащего файла: пропустить, перезаписать или
    спросить (тогда решение проставит UI, заменив DO_ASK).
    """
    root = Path(root)
    tasks = []
    def order(item):
        (kind, season), _ = item
        return _ORDER.get(kind, 9), -1 if season is None else season

    for (kind, season), candidate in sorted(chosen.items(), key=order):
        if candidate is None:
            continue
        dest = root / target_name(kind, season)
        if not dest.exists():
            action = DO_DOWNLOAD
        elif policy == POLICY_OVERWRITE:
            action = DO_REPLACE
        elif policy == POLICY_ASK:
            action = DO_ASK
        else:
            action = DO_SKIP
        tasks.append(ArtTask(kind=kind, candidate=candidate, dest=dest,
                             action=action, season=season))
    return tasks


def download(tasks, stop=None, progress=None) -> list[ArtResult]:
    """Качает то, что помечено к скачиванию. DO_SKIP и DO_ASK пропускаются.

    Незакрытый DO_ASK — это не «скачать молча», а «UI не спросил»: такую задачу
    считаем пропущенной, чтобы случайно не затереть выбранную вручную картинку.
    """
    results = []
    todo = list(tasks)
    total = len(todo)
    for i, task in enumerate(todo, 1):
        if stop and stop():
            break
        if progress:
            progress(i, total, task.dest.name)
        if task.action in (DO_SKIP, DO_ASK):
            results.append(ArtResult(task, True, skipped=True))
            continue
        try:
            net.download_file(task.candidate.url, task.dest)
            results.append(ArtResult(task, True))
        except net.NetError as e:
            results.append(ArtResult(task, False, str(e)))
    return results


def chosen_urls(tasks) -> tuple[dict, dict]:
    """Задачи → (арт тайтла, постеры сезонов) для записи ссылок в .nfo."""
    art, seasons = {}, {}
    for task in tasks:
        if task.kind == metadata.ART_SEASON:
            if task.season is not None:
                seasons[task.season] = task.candidate.url
        else:
            art[task.kind] = task.candidate.url
    return art, seasons


# --------------------------------------------------------------------------- #
# Кэш миниатюр для сетки выбора
# --------------------------------------------------------------------------- #

def cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "media-toolkit" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "media-toolkit"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "media-toolkit"


def thumbnail_bytes(url: str) -> bytes:
    """Миниатюра по URL с диск-кэшем: сетка выбора открывается повторно мгновенно."""
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    path = cache_dir() / f"{key}{Path(url).suffix[:5] or '.img'}"
    if path.is_file():
        try:
            return path.read_bytes()
        except OSError:
            pass
    data = net.get_bytes(url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        pass          # кэш необязателен, без него просто медленнее
    return data
