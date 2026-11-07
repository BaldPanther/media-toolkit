"""Раздачи-сборники со своей раскладкой — как у VCB-Studio.

Пример — «Re:Zero» S01–03 + OVA от VCB-Studio:

    [S01 Director's Cut SPs 2016]/… 01 … 13      режиссёрская версия первого сезона
    [S02 SPs 2020-2021]/… 14 … 38                 второй сезон, нумерация — дальше
    [S03 SPs 2024-2025]/… 39 … 54                 третий
    … Memory Snow [OVA-1 SPs 2018]/… Memory Snow  OVA, без номера
    [S02 SPs 2020-2021]/SPs/… Break Time 03.01    сценки по 2–5 минут

- Сезон — только в пометке папки («[S02 …]», `library.season_of_tag`).
- Номера сквозные, но свои: первый сезон здесь — 13 серий режиссёрской версии, а не
  25 ТВ-серий, поэтому второй начинается с «14», а не с «26». Номер в папке сезона,
  больший числа серий сезона, — сквозной; сдвиг подбирается такой, чтобы все номера
  папки легли в сезон (`layout`).
- Режиссёрская версия склеивает ТВ-серии: «02» — серии 2 и 3. В медиатеку такой файл
  ложится как «S01E02-E03» с двумя описаниями в .nfo — так Kodi понимает файл из
  нескольких серий. Какие серии склеены — по их длительности в TMDb (`merge`).
- Мини-спецвыпуски (сценки, заставки без титров) — по размеру: меньше четверти
  обычной серии раздачи (`minis`). Качать ли их — галка «и мини-спецвыпуски» в поиске.
- Спецвыпуски без номера (OVA) и с номерами, которые раздача придумала сама
  («Break Time 03.01»), со спецвыпусками TMDb не совпадают. Совпало название — его
  номер, иначе — в Specials следующими номерами после последнего спецвыпуска TMDb,
  с названием из имени файла (`specials`).
- Спецвыпуск встаёт между сериями по дате выхода (`place`): в его .nfo — перед какой
  серией он вышел; Kodi показывает его и внутри сезона, а не только в Specials.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from pathlib import Path

import library
import metadata
import net
import tmdb

# Режиссёрская версия — в имени папки или файла.
CUT = re.compile(r"(?i)director'?s?[\s._-]*cut|режисс[её]рск")
# Мини-спецвыпуск — меньше этой доли обычной серии раздачи (по размеру файла).
MINI_SHARE = 0.25
# Склеенная серия режиссёрской версии — не дальше этой доли от средней длины.
MERGE_SLACK = 0.35

_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_WORDS = re.compile(r"[^\W_]+")


# --------------------------------------------------------------------------- #
# Серии сезонов
# --------------------------------------------------------------------------- #

def layout(scan, nb, runtimes=None) -> dict[Path, tuple[int, list[int]]]:
    """Серии в папках сезонов сборника, где номер в имени — не номер серии в сезоне.
    → {видео: (сезон, [серии])} в нумерации трекеров (`numbering`). Остальные видео
    (номер и так верный, «S02E01» в имени, спецвыпуски) сюда не попадают.

    `nb` — нумерация тайтла (`numbering.Numbering`), `runtimes(tmdb_season)` →
    {серия: минут} — для режиссёрской версии; без него она не склеивается."""
    folders: dict[Path, list[tuple[Path, int]]] = {}
    for v in scan.videos:
        season = scan.season_dirs.get(v.parent)
        if not season:
            continue
        explicit, numbers = library.parse_episodes(v)
        if explicit is not None:
            continue                    # «S02E05» в имени — как есть
        if not numbers and v in scan.guessed:
            numbers = [scan.guessed[v]]
        if len(numbers) == 1:
            folders.setdefault(v.parent, []).append((v, numbers[0]))
    out: dict[Path, tuple[int, list[int]]] = {}
    prior = 0                           # серий (файлов) в предыдущих сезонах раздачи
    for folder in sorted(folders, key=lambda d: (scan.season_dirs[d], str(d))):
        files = folders[folder]
        season = scan.season_dirs[folder]
        span = nb.span(season)
        nums = [n for _, n in files]
        here, prior = prior, prior + len(files)
        if span is None or not span.count:
            continue
        offset = 0
        if max(nums) > span.count:
            # Сквозные: от начала сериала (как у трекеров) или свои у раздачи — от
            # первого её сезона; последнее средство — папка и есть весь сезон.
            low, high = min(nums), max(nums)
            options = [nb.before(season), here]
            if len(files) == span.count:
                options.append(low - 1)
            offset = next((o for o in options if low - o >= 1 and high - o <= span.count), None)
            if offset is None:
                continue
        relative = {v: n - offset for v, n in files}
        groups = None
        if (len(files) < span.count and runtimes is not None and _is_cut(folder, files)
                and sorted(relative.values()) == list(range(1, len(files) + 1))):
            minutes = runtimes(span.tmdb_season) or {}
            groups = merge([minutes.get(span.first + i) for i in range(span.count)], len(files))
        if groups:
            for v, k in relative.items():
                out[v] = (season, groups[k - 1])
        elif offset:
            for v, k in relative.items():
                out[v] = (season, [k])
    return out


def _is_cut(folder: Path, files) -> bool:
    return bool(CUT.search(folder.name)) or any(CUT.search(v.name) for v, _ in files)


def merge(minutes, parts: int) -> list[list[int]] | None:
    """Серии сезона (их длительности по порядку) → `parts` склеенных файлов: какие серии
    в каком. Делит подряд идущие серии на куски равной длины: «Re:Zero» — первая серия
    50 минут, остальные по 25, 25 серий в 13 файлов → [1], [2, 3], … [24, 25]. Длина
    неизвестна или куски не выходят ровными — None: не гадать."""
    count = len(minutes)
    if parts < 1 or parts >= count or any(not m for m in minutes):
        return None
    target = sum(minutes) / parts
    prefix = [0]
    for m in minutes:
        prefix.append(prefix[-1] + m)
    inf = float("inf")
    # best[k][i] — лучшая раскладка первых i серий в k файлов (сумма квадратов отклонений).
    best = [[inf] * (count + 1) for _ in range(parts + 1)]
    cut = [[0] * (count + 1) for _ in range(parts + 1)]
    best[0][0] = 0.0
    for k in range(1, parts + 1):
        for i in range(k, count - (parts - k) + 1):
            for j in range(k - 1, i):
                if best[k - 1][j] == inf:
                    continue
                cost = best[k - 1][j] + (prefix[i] - prefix[j] - target) ** 2
                if cost < best[k][i]:
                    best[k][i], cut[k][i] = cost, j
    if best[parts][count] == inf:
        return None
    groups, i = [], count
    for k in range(parts, 0, -1):
        j = cut[k][i]
        groups.append(list(range(j + 1, i + 1)))
        i = j
    groups.reverse()
    if any(abs(sum(minutes[e - 1] for e in g) - target) > target * MERGE_SLACK for g in groups):
        return None
    return groups


def tmdb_runtimes(tmdb_id: int, api_key: str):
    """→ runtimes(tmdb_season) для `layout`: длительности серий сезона TMDb в минутах."""
    cache: dict[int, dict[int, int]] = {}

    def runtimes(season: int) -> dict[int, int]:
        if season not in cache:
            try:
                data = tmdb.season(tmdb_id, season, api_key)
            except net.NetError:
                data = {}
            cache[season] = {int(e["episode_number"]): int(e.get("runtime") or 0)
                             for e in data.get("episodes") or [] if e.get("episode_number")}
        return cache[season]

    return runtimes


# --------------------------------------------------------------------------- #
# Спецвыпуски
# --------------------------------------------------------------------------- #

def _is_episode(scan, video: Path) -> bool:
    season, numbers = scan.episodes_of(video)
    return bool(season) and bool(numbers)


def minis(scan) -> set[Path]:
    """Мини-спецвыпуски: не серии (спецвыпуски, видео без номера) меньше четверти
    обычной серии раздачи по размеру. Серий в раздаче нет — сравнивать не с чем."""
    sizes = [scan.size_of(v) for v in scan.videos if _is_episode(scan, v)]
    if not sizes:
        return set()
    limit = statistics.median(sizes) * MINI_SHARE
    return {v for v in scan.videos if not _is_episode(scan, v) and scan.size_of(v) < limit}


def specials(scan, info, taken=()) -> tuple[dict[Path, tuple[int, list[int]]], dict[int, str]]:
    """Спецвыпуски раздачи, у которых нет номера спецвыпуска TMDb: без номера (OVA) или
    с номером, угаданным по соседним файлам («Break Time 03.01» → «1») — такие номера
    раздача придумала сама. Явный «S00E05» в имени — как есть.

    Название совпало со спецвыпуском TMDb («Memory Snow Manner Movie» — «Memory Snow:
    Manner Movie») — его номер. Иначе — следующий после последнего спецвыпуска TMDb и
    номеров `taken` (уже в медиатеке), по году в пути, затем по имени; в `info.episodes`
    добавляется серия с названием из имени файла — для имени и .nfo.
    → ({видео: (0, [номер])}, {номер: «2018-12-31»} — когда вышли добавленные: даты у
    них нет, только год из пути, — конец года; для `place`)."""
    loose = [v for v in scan.videos
             if scan.season_of(v) == 0 and library.parse_episodes(v)[0] is None
             and (not library.parse_episodes(v)[1] or v in scan.guessed)]
    if not loose:
        return {}, {}
    prefix = _show_prefix(scan)
    known = {n: ep for (s, n), ep in info.episodes.items() if s == 0}
    used = set(known) | set(taken)
    explicit = {library.parse_episodes(v)[1][0] for v in scan.videos
                if library.parse_episodes(v)[0] == 0 and library.parse_episodes(v)[1]}
    out: dict[Path, tuple[int, list[int]]] = {}
    rest = []
    for v in loose:
        n = _match(_title(v, prefix), prefix, known, explicit | {e[0] for _, e in out.values()})
        if n is None:
            rest.append(v)
        else:
            out[v] = (0, [n])
    nxt = max(used | explicit, default=0)
    dates: dict[int, str] = {}
    for v in sorted(rest, key=lambda v: (_year_of(v, scan.root), str(v))):
        nxt += 1
        title = _title(v, prefix) or v.stem
        info.episodes[(0, nxt)] = _episode(nxt, title)
        out[v] = (0, [nxt])
        year = _year_of(v, scan.root)
        if year != 9999:
            dates[nxt] = f"{year}-12-31"
    return out, dates


def place(plan, info, dates=None) -> None:
    """Спецвыпуски плана — между сериями по дате выхода: в `info.episodes` им ставится,
    перед какой серией этого плана они вышли (`display_season`/`display_episode`), а
    вышли после всех — после её сезона (`display_after`). В .nfo это теги Kodi
    `displayseason`/`displayepisode`/`displayafterseason`: спецвыпуск виден и внутри
    сезона, и «следующая серия» после первого сезона «Re:Zero» — Memory Snow.
    Дата — из TMDb или из `dates` ({номер: дата} от `specials`); нет даты или в плане
    нет серий с датами — не трогаем."""
    dates = dates or {}
    regular, specials_here = [], []
    for row in plan.rows:
        if row.what != "video" or row.dst is None or row.action == library.A_SKIP:
            continue
        season, numbers = library.parse_episodes(row.dst)
        if season == 0:
            specials_here += numbers
        elif season:
            regular += [(ep.aired, season, n) for n in numbers
                        for ep in [info.episodes.get((season, n))] if ep and ep.aired]
    if not regular:
        return
    regular.sort()
    for n in specials_here:
        ep = info.episodes.get((0, n))
        when = dates.get(n) or (ep.aired if ep else "")
        if ep is None or not when:
            continue
        later = next((r for r in regular if r[0] > when), None)
        if later is not None:
            ep.display_season, ep.display_episode = later[1], later[2]
        else:
            ep.display_after = regular[-1][1]


def _episode(number: int, title: str) -> metadata.EpisodeInfo:
    return metadata.EpisodeInfo(0, number, title=title, title_en=title)


def _clean(stem: str) -> str:
    text = _BRACKETS.sub(" ", stem)
    text = re.sub(r"[._]+", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -–—")


def _show_prefix(scan) -> list[str]:
    """Общее начало имён серий раздачи — название тайтла, как его пишет раздача:
    «Re Zero kara Hajimeru Isekai Seikatsu». По нему из имени спецвыпуска остаётся
    только его собственное название."""
    stems = [_clean(v.stem).split() for v in scan.videos if _is_episode(scan, v)]
    if len(stems) < 2:
        return []
    common = []
    for words in zip(*stems):
        if len(set(words)) != 1:
            break
        common.append(words[0])
    return common


def _title(video: Path, prefix: list[str]) -> str:
    words = _clean(video.stem).split()
    if prefix and words[:len(prefix)] == prefix:
        words = words[len(prefix):]
    return " ".join(words).strip(" -–—")


def _tokens(text: str) -> set[str]:
    return {w.casefold() for w in _WORDS.findall(text or "")}


def _match(title: str, prefix: list[str], known: dict, busy: set) -> int | None:
    """Номер спецвыпуска TMDb, все слова названия которого есть в названии файла (не
    считая названия тайтла). Подходят несколько одинаково — None."""
    have = _tokens(title)
    show = _tokens(" ".join(prefix))
    scored: Counter = Counter()
    for n, ep in known.items():
        if n in busy:
            continue
        for name in (ep.title_en, ep.title):
            words = _tokens(name) - show
            if len(words) >= 2 and words <= have:
                scored[n] = max(scored[n], len(words))
    if not scored:
        return None
    (n, top), *others = scored.most_common()
    return None if others and others[0][1] == top else n


def _year_of(video: Path, root: Path) -> int:
    try:
        rel = str(video.relative_to(root))
    except ValueError:
        rel = str(video)
    m = _YEAR.search(rel)
    return int(m.group(1)) if m else 9999
