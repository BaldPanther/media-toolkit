"""Раскладка медиатеки: разбор папки, целевые имена, план переименования и его применение.

Правила раскладки взяты не из документации, а с реальной медиатеки, которую
разложил tinyMediaManager — чтобы повторный прогон по уже готовым сериалам не
переименовал ни одного файла:

    <movies>/Deadpool & Wolverine (2024)/Deadpool & Wolverine (2024).mkv
                                        /movie.nfo, poster.jpg, fanart.jpg, clearlogo.png
    <tv>/Archer (2009)/tvshow.nfo, poster.jpg, fanart.jpg, clearlogo.png
                      /season01-poster.jpg, season-specials-poster.jpg
                      /Season 01/Archer - S01E01 - Mole Hunt.mkv + .nfo + .edl
                      /Specials/Archer - S00E03 - Heart of Archness (1).mkv

На каком языке названия в именах, решает `metadata.MediaInfo.folder_title`:
здесь берётся уже готовая строка, и про настройку языка этот модуль не знает.

Файлы никуда не «переносятся» в смысле копирования: и сериал, и фильм уже лежат
в корне библиотеки, поэтому всё — переименование в пределах тома, мгновенное.
Разные тома план помечает ошибкой и не трогает.
"""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import edl

MOVIE = "movie"
TV = "tv"

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv", ".mpg", ".mpeg"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}
# Спутники видео — переименовываются вместе с ним. Иначе уже сгенерированные
# .edl и скачанные .ru.srt отвяжутся от серий.
SIDECAR_EXTS = {".nfo", ".edl"} | SUB_EXTS
SIDECAR_SUFFIXES = ("-thumb.jpg", "-fanart.jpg", "-thumb.png")

SPECIALS_DIR = "Specials"

# Артефакты медиатеки: не мусор, но и не спутники — переезжают в корень тайтла.
_ART_EXACT = {
    "poster.jpg", "poster.png", "fanart.jpg", "fanart.png", "banner.jpg",
    "clearart.png", "clearlogo.png", "discart.png", "disc.png", "landscape.jpg",
    "logo.png", "thumb.jpg", "keyart.jpg", "characterart.png", "folder.jpg",
    "movie.nfo", "tvshow.nfo", "theme.mp3",
}
_ART_RE = re.compile(r"(?i)^season(\d+|-specials|-all)-(poster|banner|landscape|thumb)\.(jpe?g|png)$")
_ART_DIRS = {".actors", "extrafanart", "extrathumbs"}

# Метка «не предлагать эту папку». Кладётся внутрь папки тайтла; на ручной
# прогон не влияет — только на автоматический обход библиотеки.
IGNORE_SUFFIX = ".ignore"
IGNORE_FILE = "не обрабатывать.ignore"

_SEASON_DIR_RE = re.compile(r"(?i)^season[\s._-]*(\d{1,3})$")
_SPECIALS_DIR_RE = re.compile(r"(?i)^(specials?|season[\s._-]*0+)$")

# Токен, после которого в имени раздачи начинается техническая часть.
_RELEASE_TOKEN = re.compile(
    r"(?i)^(s\d{1,2}(e\d{1,3})?|season|сезон|\d{3,4}p|4k|uhd|hd|web[-.]?dl|webrip|web|"
    r"bluray|blu-ray|bdrip|brrip|bdremux|hdtv|dvdrip|dvd|remux|hdr\d*|sdr|dovi|dv|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|xvid|divx|aac\d*|ac3|eac3|dts(-hd)?|ddp?\d?|"
    r"atmos|truehd|flac|opus|10bit|8bit|repack|proper|extended|uncut|imax|complete|"
    r"multi|dual|rus|eng|jpn|jap|ukr|sub|subs|hybrid|limited|unrated)$")
_YEAR_TOKEN = re.compile(r"^[\[(]?((?:19|20)\d{2})[\])]?$")

# Действия строки плана.
A_RENAME = "rename"       # видео/спутник/артефакт переезжает на целевой путь
A_KEEP = "keep"           # уже на месте
A_JUNK = "junk"           # посторонний файл: в Extras или в Корзину
A_SKIP = "skip"           # ничего сделать нельзя (нет номера серии, конфликт)

# Статусы для таблицы.
S_OK = "уже верно"
S_RENAME = "переименование"
S_MOVE = "перемещение"
S_JUNK = "мусор"
S_NOEP = "нет номера серии"
S_CONFLICT = "конфликт имён"
S_CROSSDEV = "другой том"
S_COLLECTION = "папка-сборник"


# --------------------------------------------------------------------------- #
# Имена
# --------------------------------------------------------------------------- #

# Запрещённые в имени символы, которые просто вырезаются.
_BAD_CHARS = re.compile(r'[*?"<>|\x00-\x1f]')
# Разделители, которых в имени файла быть не может: двоеточие запрещает Windows
# (и Finder рисует его слэшем), слэш не проходит нигде. Забираем вместе с
# пробелами вокруг — по ним и решается, каким дефис будет.
_SEPARATOR = re.compile(r"(\s*)[:/\\](\s*)")
# Висящий дефис от разделителя в начале или в конце названия.
_DANGLING_DASH = re.compile(r"^-\s*|\s*-$")


def _dash(m: re.Match) -> str:
    """Разделитель → дефис. Пробел хотя бы с одной стороны считается за оба:
    половинчатое «Eddie Murphy- Delirious» не нужно никому."""
    return " - " if (m.group(1) or m.group(2)) else "-"


def sanitize_name(name: str) -> str:
    """Название → безопасное имя файла/папки.

    Двоеточие и слэш становятся дефисом. Был разделитель отбит пробелом хотя бы
    с одной стороны — дефис отбивается с обеих; не отбит вовсе — дефис тоже
    остаётся без пробелов:

        «Eddie Murphy: Delirious»  → «Eddie Murphy - Delirious» (подзаголовок)
        «Re:Zero», «9:30»          → «Re-Zero», «9-30» (часть одного выражения)
        «Arrival/Departure»        → «Arrival-Departure» (пара слов)

    Пробел на месте разделителя не годится: «The Lord of the Rings The
    Fellowship of the Ring» и «9 30» читаются заметно хуже.

    Точки и пробелы в конце обрезаются — Windows такие имена не создаёт.
    """
    s = _SEPARATOR.sub(_dash, name or "")
    s = _BAD_CHARS.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = _DANGLING_DASH.sub("", s).strip()
    return s.rstrip(". ").strip() or "Untitled"


def season_dir_name(season: int) -> str:
    """0 → «Specials», иначе «Season NN»."""
    return SPECIALS_DIR if season == 0 else f"Season {season:02d}"


def season_poster_name(season: int) -> str:
    return "season-specials-poster.jpg" if season == 0 else f"season{season:02d}-poster.jpg"


def title_with_year(title: str, year: int | None) -> str:
    base = sanitize_name(title)
    return f"{base} ({year})" if year else base


def episode_file_name(show: str, season: int, episodes, title: str, ext: str) -> str:
    """«Archer - S01E01 - Mole Hunt.mkv». Без названия серии — «Archer - S01E01.mkv».

    `episodes` — номер или список номеров. Несколько серий в одном файле
    записываются диапазоном «S14E09-E11», как это делает tinyMediaManager.
    """
    nums = [episodes] if isinstance(episodes, int) else sorted(episodes)
    tag = f"S{season:02d}E{nums[0]:02d}"
    if len(nums) > 1:
        tag += f"-E{nums[-1]:02d}"
    stem = f"{sanitize_name(show)} - {tag}"
    if title:
        stem += f" - {sanitize_name(title)}"
    return stem + ext


def movie_file_name(title: str, year: int | None, ext: str) -> str:
    return title_with_year(title, year) + ext


def guess_title_year(name: str) -> tuple[str, int | None]:
    """Имя папки/файла раздачи → (название для поиска, год).

    «Altered.Carbon.S01.2160p.NF.WEBRip.x265…» → («Altered Carbon», None)
    «Deadpool.and.Wolverine.2024.2160p.WEB-DL» → («Deadpool and Wolverine», 2024)
    Разбор обрывается на первом техническом токене: всё, что после него, — параметры
    рипа, а не название.
    """
    # Точки-разделители заменяем на пробелы, а точку сокращения — нет: в имени
    # раздачи точка идёт вплотную к следующему слову («Altered.Carbon»), а в
    # сокращении за ней стоит пробел («Dr. STONE»).
    raw = re.sub(r"[._]+(?!\s)", " ", name or "").strip()
    year = None
    words: list[str] = []
    for token in raw.split(" "):
        if not token:
            continue
        m = _YEAR_TOKEN.match(token)
        if m:
            year = int(m.group(1))
            break
        if _RELEASE_TOKEN.match(token.strip("[]()")):
            break
        words.append(token)
    title = " ".join(words).strip(" -–—[]()")
    if not title:                       # имя целиком техническое — отдаём как есть
        title = raw.strip(" -–—[]()")
    return re.sub(r"\s{2,}", " ", title), year


# --------------------------------------------------------------------------- #
# Разбор папки
# --------------------------------------------------------------------------- #

# Где в имени заканчивается первый номер серии. Сами значения сезона и серии
# берём у edl.parse_season_episode, чтобы разбор не разъехался между модулями;
# отсюда нужна только позиция, с которой искать продолжение.
_SE_TAG = re.compile(r"[Ss]\d{1,2}[\s._-]*[Ee]\d{1,3}|(?<!\d)\d{1,2}[xX]\d{1,3}(?!\d)")

# Продолжение номера серии после первого: «-E11», «E10», «.E11» или «-02».
# Голый «-NN» принимается только вплотную к цифрам, иначе «S01E01 - 2 Girls»
# было бы прочитано как вторая серия в файле.
_MORE_EP = re.compile(r"(?:[\s._-]*[Ee][\s._]*(\d{1,3})|-(\d{1,3})(?=$|[\s._-]))")

# Номер серии без «S01E01» — типовое именование аниме:
# «Koukaku Kidoutai - 01 [WEB-DL AMZN 1080p AVC EAC3]», «Show E05», «Show #7».
# Перед числом обязателен явный маркер, иначе «Loaded Weapon 1 (1993)» или
# «1080p» тоже стали бы номерами серий.
_BARE_EP = re.compile(r"""
    (?:^|[\s._])                 # начало имени или разделитель
    (?: -[\s._]*                 # « - 01 »
      | [Ee][Pp]?[\s._]*         # « E01 », « EP 01 »
      | \#[\s._]* )              # « #01 »
    (\d{1,3})                    # сам номер
    (?!\d)                       # не кусок числа побольше (1080p)
    (?!\w)                       # и не начало слова
""", re.X)


def parse_episodes(path) -> tuple[int | None, list[int]]:
    """(сезон, список серий) из имени файла. Один файл может нести несколько.

    «Archer - S14E09-E11 - Into the Cold.mkv» → (14, [9, 10, 11]): диапазон
    раскрывается целиком, потому что в Kodi такому файлу нужен .nfo на каждую
    серию, иначе две из трёх просто пропадут из медиатеки.
    """
    stem = Path(path).stem
    season, first = edl.parse_season_episode(path)
    if season is None or first is None:
        # «S01E01» нет — пробуем голый номер. Сезон при этом неизвестен,
        # его достаёт вызывающий: из папки «Season NN» или по раскладке тайтла.
        bare = _BARE_EP.search(stem)
        return (None, [int(bare.group(1))]) if bare else (season, [])
    match = _SE_TAG.search(stem)
    if match is None:
        return season, [first]
    nums = [first]
    pos = match.end()
    while True:
        more = _MORE_EP.match(stem, pos)
        if not more:
            break
        nums.append(int(more.group(1) or more.group(2)))
        pos = more.end()
    if len(nums) > 1 and nums[-1] > nums[0]:
        return season, list(range(nums[0], nums[-1] + 1))
    return season, nums


# «Show - S02E01 - Название» → «Название». Только строгий формат медиатеки:
# хвост вроде «.2160p.NF.WEBRip» из имени раздачи названием не считается.
_EXISTING_TITLE = re.compile(r" - [Ss]\d{1,2}[Ee]\d{1,3}(?:-[Ee]\d{1,3})? - (.+)$")


def existing_episode_title(path) -> str:
    """Название серии, уже записанное в имени файла, — или пустая строка.

    Нужно, когда у TMDb нет такой серии. Самый частый случай — аниме: TMDb
    держит «Solo Leveling» одним сезоном из 25 серий, а на диске (нумерация
    TVDb, как у tinyMediaManager) их два по 12–13. Переименовать в таком случае
    в «Solo Leveling - S02E01.mkv» значит потерять верное название, поэтому имя
    сохраняем как есть.
    """
    m = _EXISTING_TITLE.search(Path(path).stem)
    return m.group(1).strip() if m else ""


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS and not path.name.startswith(".")


def is_art_file(path: Path) -> bool:
    n = path.name.lower()
    # Метка игнорирования мусором не считается: иначе прогон увёз бы её в
    # Extras, и папка снова начала бы предлагаться.
    return (n in _ART_EXACT or bool(_ART_RE.match(path.name))
            or n.endswith(IGNORE_SUFFIX))


def is_ignored(folder) -> bool:
    """Есть ли в папке метка «не предлагать» — любой файл с расширением .ignore."""
    try:
        return any(p.is_file() and p.name.lower().endswith(IGNORE_SUFFIX)
                   for p in Path(folder).iterdir())
    except OSError:
        return False


def write_ignore_marker(folder) -> Path:
    """Создаёт метку игнорирования. Удалить — обычным удалением файла."""
    path = Path(folder) / IGNORE_FILE
    path.write_text(
        "Пока этот файл лежит здесь, «Что не обработано» пропускает эту папку.\n"
        "Удалите его, чтобы вернуть её в список.\n"
        "Ручной прогон работает в любом случае — метка на него не влияет.\n",
        encoding="utf-8")
    return path


def season_of_dir(name: str) -> int | None:
    """«Season 02» → 2, «Specials»/«Season 00» → 0, иначе None."""
    if _SPECIALS_DIR_RE.match(name):
        return 0
    m = _SEASON_DIR_RE.match(name)
    return int(m.group(1)) if m else None


def find_sidecars(video: Path) -> list[Path]:
    """Файлы-спутники рядом с видео: те же имя+точка и известное расширение.

    Ловит и `Show - S01E01.ru.srt`, и `Show - S01E01-thumb.jpg` — у первого между
    именем и расширением вклинивается языковой код, у второго дефис-суффикс.
    """
    stem = video.stem
    out = []
    for p in sorted(video.parent.iterdir()):
        if p == video or not p.is_file():
            continue
        name = p.name
        if not name.startswith(stem):
            continue
        rest = name[len(stem):]
        if rest.lower().endswith(SIDECAR_SUFFIXES):
            out.append(p)
        elif rest.startswith(".") and p.suffix.lower() in SIDECAR_EXTS:
            out.append(p)
    return out


@dataclass
class Scan:
    root: Path
    videos: list[Path] = field(default_factory=list)
    arts: list[Path] = field(default_factory=list)
    junk_files: list[Path] = field(default_factory=list)
    junk_dirs: list[Path] = field(default_factory=list)
    season_dirs: dict[Path, int] = field(default_factory=dict)

    def season_of(self, video: Path) -> int | None:
        season, numbers = parse_episodes(video)
        if season is not None:
            return season
        by_dir = self.season_dirs.get(video.parent)
        if by_dir is not None:
            return by_dir
        # Номер серии есть, а сезона нет и папок сезонов в тайтле тоже нет —
        # значит, это плоская раскладка одного сезона (обычное дело у аниме).
        return 1 if numbers and not self.season_dirs else None

    def seasons(self) -> list[int]:
        found = {s for s in (self.season_of(v) for v in self.videos) if s is not None}
        found.update(self.season_dirs.values())
        return sorted(found)

    def season_sizes(self) -> dict[int, int]:
        """Сколько серий в каждом сезоне на диске.

        Считаем серии, а не файлы: многосерийный «S14E09-E11» — это три штуки.
        По этим числам выбирается подходящая разбивка в episode groups TMDb.
        """
        sizes: dict[int, int] = {}
        for v in self.videos:
            season = self.season_of(v)
            if season is None:
                continue
            sizes[season] = sizes.get(season, 0) + max(len(parse_episodes(v)[1]), 1)
        return sizes


def scan_folder(folder) -> Scan:
    """Обходит папку тайтла: видео, спутники, артефакты медиатеки, мусор.

    Спутники в результат не попадают отдельной строкой — они привязаны к своему
    видео и переезжают вместе с ним (`find_sidecars`).
    """
    root = Path(folder)
    scan = Scan(root=root)
    if root.is_file():
        scan.videos = [root] if is_video(root) else []
        scan.root = root.parent
        return scan

    sidecars: set[Path] = set()
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if is_video(path):
            scan.videos.append(path)
            sidecars.update(find_sidecars(path))
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            s = season_of_dir(path.name)
            if s is not None:
                scan.season_dirs[path] = s
            continue
        if path in sidecars or path in scan.videos:
            continue
        if is_art_file(path):
            scan.arts.append(path)
        elif any(part in _ART_DIRS for part in path.relative_to(root).parts[:-1]):
            scan.arts.append(path)
        elif path.name.startswith("."):
            continue                     # .DS_Store и прочая служебка — не наше дело
        else:
            scan.junk_files.append(path)
    for path in sorted(root.rglob("*")):
        if path.is_dir() and path.name not in _ART_DIRS and path not in scan.season_dirs:
            has_video = any(is_video(p) for p in path.rglob("*") if p.is_file())
            if not has_video:
                scan.junk_dirs.append(path)
    return scan


def guess_kind(path, settings) -> str:
    """Фильм или сериал. Корень библиотеки из настроек — сильнейший признак."""
    p = Path(path).resolve()
    for root in settings.tv_roots:
        if _is_within(p, Path(root)):
            return TV
    for root in settings.movies_roots:
        if _is_within(p, Path(root)):
            return MOVIE
    scan = scan_folder(p)
    if scan.season_dirs:
        return TV
    for v in scan.videos:
        if edl.parse_season_episode(v)[0] is not None:
            return TV
    return TV if len(scan.videos) > 2 else MOVIE


# --------------------------------------------------------------------------- #
# Что в библиотеке ещё не обработано
# --------------------------------------------------------------------------- #

@dataclass
class Pending:
    """Папка (или одиночный файл), которую стоит прогнать через скрапер."""
    path: Path
    kind: str
    reasons: list[str] = field(default_factory=list)

    @property
    def why(self) -> str:
        return ", ".join(self.reasons)


def _has_file(folder: Path, *names: str) -> bool:
    return any((folder / n).is_file() for n in names)


def _poster_missing(folder: Path) -> bool:
    return not _has_file(folder, "poster.jpg", "poster.png")


def _videos_in(folder: Path) -> list[Path]:
    try:
        return [p for p in folder.iterdir() if p.is_file() and is_video(p)]
    except OSError:
        return []


def _subdirs(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir()
                      if p.is_dir() and p.name not in _ART_DIRS
                      and not p.name.startswith("."))
    except OSError:
        return []


def _check_movie(folder: Path) -> Pending | None:
    reasons = []
    if not _has_file(folder, "movie.nfo"):
        reasons.append("нет movie.nfo")
    if _poster_missing(folder):
        reasons.append("нет постера")
    return Pending(folder, MOVIE, reasons) if reasons else None


def _check_show(folder: Path, extras_name: str) -> Pending | None:
    """Сериал: есть ли tvshow.nfo, постер и .nfo у каждой серии.

    Обход всего на два уровня (сериал → сезоны → файлы) и через `iterdir`:
    библиотека лежит на сетевом диске, и рекурсивный обход целиком заметно
    медленнее.
    """
    reasons = []
    if not _has_file(folder, "tvshow.nfo"):
        reasons.append("нет tvshow.nfo")
    if _poster_missing(folder):
        reasons.append("нет постера")

    loose = _videos_in(folder)
    if loose:
        reasons.append(f"серии не разложены по сезонам: {len(loose)}")

    no_nfo = 0
    raw_dirs = []
    for sub in _subdirs(folder):
        if sub.name == extras_name:
            continue
        videos = _videos_in(sub)
        if season_of_dir(sub.name) is None:
            if videos:
                raw_dirs.append(sub.name)
            continue
        no_nfo += sum(1 for v in videos if not v.with_suffix(".nfo").is_file())
    if raw_dirs:
        reasons.append("новый сезон: " + ", ".join(raw_dirs[:3]))
    if no_nfo:
        reasons.append(f"серий без .nfo: {no_nfo}")
    return Pending(folder, TV, reasons) if reasons else None


def find_unprocessed(settings, stop=None, progress=None) -> list[Pending]:
    """Обходит корни библиотеки и собирает то, что скрапер ещё не разложил.

    Никакой базы: состояние целиком выводится из самих файлов. Отдельный список
    обработанного пришлось бы синхронизировать вручную после каждого
    переименования или удаления в Finder, и он бы врал.
    """
    found: list[Pending] = []
    roots = ([(MOVIE, r) for r in settings.movies_roots]
             + [(TV, r) for r in settings.tv_roots])
    for kind, raw_root in roots:
        root = Path(raw_root)
        if not root.is_dir():
            continue
        entries = _subdirs(root)
        loose = _videos_in(root) if kind == MOVIE else []
        total = len(entries) + len(loose)
        for i, entry in enumerate(entries, 1):
            if stop and stop():
                return found
            if progress:
                progress(i, total, entry.name)
            if is_ignored(entry):
                continue
            if kind == MOVIE:
                # Папка-сборник: фильмов внутри несколько, проверяем каждый.
                if not _videos_in(entry) and any(_videos_in(d) for d in _subdirs(entry)):
                    for inner in _subdirs(entry):
                        if is_ignored(inner):
                            continue
                        item = _check_movie(inner)
                        if item:
                            found.append(item)
                    continue
                item = _check_movie(entry)
            else:
                item = _check_show(entry, settings.extras_folder)
            if item:
                found.append(item)
        # Фильм, скачанный одним файлом прямо в корень, папки ещё не имеет.
        for video in loose:
            found.append(Pending(video, MOVIE, ["файл без папки фильма"]))
    return found


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


# --------------------------------------------------------------------------- #
# План
# --------------------------------------------------------------------------- #

@dataclass
class Row:
    src: Path | None
    dst: Path | None
    action: str
    status: str
    what: str = "video"      # video | sidecar | art | junk
    note: str = ""
    selected: bool = True    # для строк мусора — галка в таблице

    @property
    def changes(self) -> bool:
        return self.action in (A_RENAME, A_JUNK)


@dataclass
class Plan:
    kind: str
    root: Path               # папка тайтла после применения плана
    rows: list[Row] = field(default_factory=list)
    unmatched: list[Path] = field(default_factory=list)   # видео без номера серии

    def changed(self) -> list[Row]:
        return [r for r in self.rows if r.changes]

    def conflicts(self) -> list[Row]:
        return [r for r in self.rows if r.status in (S_CONFLICT, S_CROSSDEV)]


def norm(path) -> str:
    """Путь в единой юникод-форме для сравнения.

    macOS и SMB хранят имена в NFD («o» + комбинирующее умляут-двоеточие), а
    TMDb отдаёт NFC (готовая «ö»). Без нормализации «Auflösung.mkv» на диске и
    «Auflösung.mkv» из API считаются разными, и каждый прогон предлагал бы
    переименовать файл сам в себя. Регистр при этом важен и не трогается.
    """
    return unicodedata.normalize("NFC", str(path))


def _row_for(src: Path, dst: Path, what: str) -> Row:
    if norm(src) == norm(dst):
        return Row(src, dst, A_KEEP, S_OK, what)
    status = S_RENAME if src.parent == dst.parent else S_MOVE
    return Row(src, dst, A_RENAME, status, what)


def _add_with_sidecars(rows: list[Row], video: Path, dst: Path) -> None:
    rows.append(_row_for(video, dst, "video"))
    for side in find_sidecars(video):
        rest = side.name[len(video.stem):]
        rows.append(_row_for(side, dst.with_name(dst.stem + rest), "sidecar"))


def _junk_rows(scan: Scan, target_root: Path, settings) -> list[Row]:
    """Строки для посторонних файлов.

    То, что уже лежит в `Extras/` с прошлого прогона, мусором заново не считается:
    иначе включённая галка «удалять» снесла бы отложенное туда в прошлый раз.
    Такие файлы просто переезжают вместе с папкой тайтла.
    """
    extras_name = settings.extras_folder
    extras = target_root / extras_name
    rows = []
    for p in scan.junk_files:
        rel = p.relative_to(scan.root)
        if rel.parts and rel.parts[0] == extras_name:
            rows.append(_row_for(p, target_root / rel, "art"))
        elif any(d in p.parents for d in scan.junk_dirs):
            continue        # переедет вместе со своей папкой, отдельной строкой не дублируем
        else:
            rows.append(Row(p, extras / p.name, A_JUNK, S_JUNK, "junk"))
    for d in scan.junk_dirs:
        rel = d.relative_to(scan.root)
        if rel.parts and rel.parts[0] == extras_name:
            continue                    # содержимое уже обработано выше
        if any(other in d.parents for other in scan.junk_dirs):
            continue                    # вложенная в другую мусорную папку — поедет с ней
        rows.append(Row(d, extras / d.name, A_JUNK, S_JUNK, "junk",
                        note="папка без видео"))
    return rows


def _looks_like_show_folder(d: Path) -> bool:
    """Похожа ли папка на корень сериала: есть tvshow.nfo или папки сезонов."""
    try:
        if (d / "tvshow.nfo").is_file():
            return True
        return any(season_of_dir(c.name) is not None for c in d.iterdir() if c.is_dir())
    except OSError:
        return False


def _show_root_for(folder: Path, target_name: str) -> Path:
    """Куда ляжет сериал, если указали `folder`.

    Указать можно и папку сериала целиком, и отдельную папку одного сезона —
    и лежать она может как в корне библиотеки (свежая раздача), так и уже внутри
    готового сериала (дозагруженный второй сезон). Различаем по родителю: если он
    сам похож на папку сериала, значит указан сезон внутри неё.
    """
    base = folder.parent if _looks_like_show_folder(folder.parent) else folder
    return base if base.name == target_name else base.parent / target_name


def build_tv_plan(folder, info, settings, overrides=None) -> Plan:
    """План для сериала. `overrides` — ручные (season, episode) по путям видео."""
    folder = Path(folder)
    scan = scan_folder(folder)
    overrides = overrides or {}
    show = info.folder_title
    target_name = title_with_year(show, info.year)
    show_root = _show_root_for(folder, target_name)

    plan = Plan(kind=TV, root=show_root)
    for video in scan.videos:
        if video in overrides:
            season, numbers = overrides[video][0], [overrides[video][1]]
        else:
            season, numbers = parse_episodes(video)
            if season is None:
                season = scan.season_of(video)
        if season is None or not numbers:
            plan.rows.append(Row(video, None, A_SKIP, S_NOEP, "video",
                                 note="двойной клик — задать вручную"))
            plan.unmatched.append(video)
            continue
        # Название берём у первой серии файла: так подписан и многосерийный
        # файл в уже разложенной медиатеке («S14E09-E11 - Into the Cold»).
        ep = info.episodes.get((season, numbers[0]))
        if ep:
            title, note = info.episode_name(ep), ""
        else:
            # У TMDb такой серии нет — сохраняем название, уже стоящее в имени,
            # вместо того чтобы его потерять.
            title = existing_episode_title(video)
            note = ("серии нет в TMDb — название оставлено прежним" if title
                    else "серии нет в TMDb — имя без названия")
        dst = show_root / season_dir_name(season) / episode_file_name(
            show, season, numbers, title, video.suffix.lower())
        _add_with_sidecars(plan.rows, video, dst)
        if note:
            plan.rows[-1].note = note

    for art in scan.arts:
        plan.rows.append(_row_for(art, show_root / art.name, "art"))
    plan.rows += _junk_rows(scan, show_root, settings)
    _mark_conflicts(plan)
    return plan


def build_movie_plan(path, info, settings, overrides=None) -> Plan:
    """План для фильма. Путь может быть как папкой, так и одиночным файлом."""
    path = Path(path)
    scan = scan_folder(path)
    # И для одиночного файла, и для папки раздачи целевая папка фильма создаётся
    # рядом: `…/movies/Deadpool.2024.WEB-DL` и `…/movies/Deadpool.2024.mkv` дают
    # один и тот же `…/movies/Deadpool & Wolverine (2024)`.
    target_name = title_with_year(info.folder_title, info.year)
    movie_root = path.parent / target_name

    plan = Plan(kind=MOVIE, root=movie_root)

    # Папка-сборник («Scary Movie Collection» с четырьмя фильмами внутри) — не
    # один фильм. Молча признать три из четырёх «лишним видео» и увезти их в
    # Extras было бы катастрофой, поэтому просто отказываемся строить план.
    if len({v.parent for v in scan.videos}) > 1:
        for video in scan.videos:
            plan.rows.append(Row(video, None, A_SKIP, S_COLLECTION, "video",
                                 note="укажите папку одного фильма"))
        return plan

    videos = sorted(scan.videos, key=lambda p: p.stat().st_size if p.exists() else 0,
                    reverse=True)
    for i, video in enumerate(videos):
        if i == 0:
            dst = movie_root / movie_file_name(info.folder_title, info.year,
                                               video.suffix.lower())
            _add_with_sidecars(plan.rows, video, dst)
        else:
            # Второй видеофайл в папке фильма — трейлер, сэмпл или «бонус».
            # Сам фильм угадан по размеру, остальное уезжает в Extras.
            plan.rows.append(Row(video, movie_root / settings.extras_folder / video.name,
                                 A_JUNK, S_JUNK, "junk", note="лишнее видео в папке фильма"))
    for art in scan.arts:
        plan.rows.append(_row_for(art, movie_root / art.name, "art"))
    plan.rows += _junk_rows(scan, movie_root, settings)
    _mark_conflicts(plan)
    return plan


def build_plan(path, kind: str, info, settings, overrides=None) -> Plan:
    if kind == MOVIE:
        return build_movie_plan(path, info, settings, overrides)
    return build_tv_plan(path, info, settings, overrides)


def _mark_conflicts(plan: Plan) -> None:
    """Две строки в один путь или переезд на другой том — помечаем и не применяем."""
    seen: dict[str, Row] = {}
    for row in plan.rows:
        if row.dst is None or not row.changes:
            continue
        other = seen.get(norm(row.dst))
        if other is not None:
            for r in (row, other):
                r.action, r.status = A_SKIP, S_CONFLICT
                r.note = r.note or "два файла претендуют на одно имя"
            continue
        seen[norm(row.dst)] = row
        if row.src is not None and _cross_device(row.src, row.dst):
            row.action, row.status = A_SKIP, S_CROSSDEV
            row.note = "источник и цель на разных томах"


def _existing_device(path: Path) -> int | None:
    p = path
    for _ in range(6):
        try:
            return p.stat().st_dev
        except OSError:
            if p.parent == p:
                return None
            p = p.parent
    return None


def _cross_device(src: Path, dst: Path) -> bool:
    a, b = _existing_device(src), _existing_device(dst)
    return a is not None and b is not None and a != b


# --------------------------------------------------------------------------- #
# Применение
# --------------------------------------------------------------------------- #

def send_to_trash_available() -> bool:
    try:
        import send2trash  # noqa: F401
    except ImportError:
        return False
    return True


def _same_name_ci(a: Path, b: Path) -> bool:
    """Отличаются только регистром: на APFS и SMB это один и тот же файл."""
    return a.parent == b.parent and a.name != b.name and a.name.lower() == b.name.lower()


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if _same_name_ci(src, dst):
        # Регистронезависимая ФС не даст переименовать напрямую — через временное имя.
        tmp = src.with_name(f".rename-{os.getpid()}-{src.name}")
        src.rename(tmp)
        tmp.rename(dst)
        return
    if dst.exists():
        raise FileExistsError(f"уже существует: {dst}")
    src.rename(dst)


# Как именно избавились от мусора — у тома может не быть Корзины.
DISPOSE_TRASH = "trash"
DISPOSE_DELETE = "delete"


def _delete(path: Path) -> None:
    """Удаление без Корзины. Мусором бывает и папка целиком («Sample»)."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


# Как именно избавились от мусора — у тома может не быть Корзины.
DISPOSE_TRASH = "trash"
DISPOSE_DELETE = "delete"


@dataclass
class ApplyResult:
    row: Row
    ok: bool
    error: str = ""
    disposal: str = ""       # DISPOSE_* — только у строк мусора


def apply_plan(plan: Plan, delete_junk: bool = False,
               stop=None, progress=None) -> list[ApplyResult]:
    """Применяет план. Порядок: сначала файлы по местам, потом уборка пустых папок.

    Каждый файл едет со своего текущего места сразу на конечное — так строки не
    зависят друг от друга и прерывание на середине оставляет корректное состояние.
    Циклы имён (E01→E02, E02→E01) разводятся временными именами.
    """
    results: list[ApplyResult] = []
    todo = [r for r in plan.rows if r.changes and (r.what != "junk" or r.selected)]
    sources = {r.src for r in todo if r.src is not None}
    staged: list[tuple[Path, Row]] = []
    trash = _trash_func() if delete_junk else None

    total = len(todo)
    for i, row in enumerate(todo, 1):
        if stop and stop():
            break
        if progress:
            progress(i, total, row.src)
        try:
            if row.action == A_JUNK and delete_junk:
                if trash is None:
                    raise RuntimeError("нет пакета send2trash — удаление недоступно")
                try:
                    trash(str(row.src))
                    results.append(ApplyResult(row, True, disposal=DISPOSE_TRASH))
                except OSError:
                    # У сетевого тома Корзины нет вовсе («Корзина в этом томе
                    # отсутствует»), и Finder там тоже удаляет сразу. Галка —
                    # это уже принятое решение удалить, так что удаляем.
                    _delete(row.src)
                    row.note = "на этом томе нет Корзины — удалено насовсем"
                    results.append(ApplyResult(row, True, disposal=DISPOSE_DELETE))
                continue
            if row.dst in sources:
                # Цель занята файлом, который сам ещё поедет: паркуем во временное имя.
                tmp = row.src.with_name(f".stage-{os.getpid()}-{row.src.name}")
                row.src.rename(tmp)
                staged.append((tmp, row))
                continue
            _move(row.src, row.dst)
            results.append(ApplyResult(row, True))
        except (OSError, RuntimeError) as e:
            results.append(ApplyResult(row, False, str(e)))

    for tmp, row in staged:
        try:
            _move(tmp, row.dst)
            results.append(ApplyResult(row, True))
        except OSError as e:
            results.append(ApplyResult(row, False, str(e)))

    _cleanup_empty_dirs(plan)
    return results


def _trash_func():
    try:
        from send2trash import send2trash
    except ImportError:
        return None
    return send2trash


def _cleanup_empty_dirs(plan: Plan) -> None:
    """Убирает опустевшие папки раздачи — но никогда сам корень тайтла."""
    roots = {r.src.parent for r in plan.rows if r.src is not None}
    for d in sorted(roots, key=lambda p: len(p.parts), reverse=True):
        try:
            if d == plan.root or not d.is_dir():
                continue
            if _is_within(plan.root, d):
                continue                 # это родитель целевой папки, не трогаем
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Служебные файлы систем
# --------------------------------------------------------------------------- #

# Кэши превью и настройки папок, которые Windows и macOS рассыпают по всей
# медиатеке. Содержимого не несут и создаются заново сами, поэтому чистятся
# оптом, а не разбираются по одному. Отключение их создания в системе помогает
# не всегда: Thumbs.db возвращается после любого визита проводника.
SYSTEM_JUNK_NAMES = {
    "thumbs.db", "thumbs.db:encryptable", "ehthumbs.db", "ehthumbs_vista.db",
    "desktop.ini",              # Windows: вид папки и её иконка
    ".ds_store",                # macOS: положение окна Finder
}
# AppleDouble: macOS кладёт «._имя» рядом с файлом, когда том не умеет хранить
# расширенные атрибуты. На SMB-шаре таких набегает больше всего.
APPLEDOUBLE_PREFIX = "._"


def is_system_junk(path: Path) -> bool:
    name = path.name
    if name.lower() in SYSTEM_JUNK_NAMES:
        return True
    return name.startswith(APPLEDOUBLE_PREFIX) and len(name) > len(APPLEDOUBLE_PREFIX)


def find_system_junk(settings, stop=None, progress=None) -> list[Path]:
    """Служебные файлы систем во всех корнях библиотеки.

    Обход идёт по папкам тайтлов, а не одним `rglob` по корню: так виден
    прогресс и обход можно прервать — медиатека лежит на сетевом диске, и
    рекурсия по ней не мгновенная.
    """
    targets: list[tuple[Path, bool]] = []
    for raw in list(settings.movies_roots) + list(settings.tv_roots):
        root = Path(raw)
        if not root.is_dir():
            continue
        targets.append((root, False))       # файлы самого корня, без рекурсии
        try:
            targets += [(d, True) for d in sorted(root.iterdir())
                        if d.is_dir() and not d.name.startswith(".")]
        except OSError:
            continue
    found: list[Path] = []
    total = len(targets)
    for i, (folder, deep) in enumerate(targets, 1):
        if stop and stop():
            break
        if progress:
            progress(i, total, folder.name)
        try:
            items = folder.rglob("*") if deep else folder.iterdir()
            found += [p for p in items if is_system_junk(p) and p.is_file()]
        except OSError:
            continue
    return sorted(found)


@dataclass
class DeleteResult:
    path: Path
    ok: bool
    trashed: bool = False
    error: str = ""


def delete_files(paths, to_trash: bool = True, stop=None, progress=None) -> list[DeleteResult]:
    """Удаляет файлы: по возможности в Корзину, иначе насовсем.

    У сетевого тома Корзины нет, и Finder на нём тоже удаляет сразу — так что
    откат на обычное удаление не «опаснее ручного», а ровно то же самое.
    Применяется только к `find_system_junk`: там содержимого нет по определению.
    """
    trash = _trash_func() if to_trash else None
    results: list[DeleteResult] = []
    total = len(paths)
    for i, path in enumerate(paths, 1):
        if stop and stop():
            break
        if progress:
            progress(i, total, path.name)
        if trash is not None:
            try:
                trash(str(path))
                results.append(DeleteResult(path, True, trashed=True))
                continue
            except OSError:
                pass                     # у тома нет Корзины — удаляем обычным способом
        try:
            _delete(path)
            results.append(DeleteResult(path, True))
        except OSError as e:
            results.append(DeleteResult(path, False, error=str(e)))
    return results


def prune_empty_dirs(paths, keep) -> list[Path]:
    """Убирает папки, опустевшие после удаления файлов.

    Ровно тот случай, ради которого нужно: в папке от старого имени тайтла
    остался один `Thumbs.db`, и пока он там лежал, папка не удалялась.
    Вверх поднимаемся, пока пусто; корни библиотеки не трогаем никогда.
    """
    protected = set()
    for k in keep:
        try:
            protected.add(Path(k).resolve())
        except OSError:
            pass
    removed: list[Path] = []
    for folder in sorted({Path(p).parent for p in paths},
                         key=lambda p: len(p.parts), reverse=True):
        d = folder
        while True:
            try:
                if not d.is_dir() or d.resolve() in protected or any(d.iterdir()):
                    break
                d.rmdir()
            except OSError:
                break
            removed.append(d)
            d = d.parent
    return removed
