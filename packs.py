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
  ложится как «S01E02+E03» — в Kodi одна серия с обоими названиями и описаниями
  (`library.episode_file_name`). Какие серии склеены — по их длительности в TMDb (`merge`).
- Мини-спецвыпуски (сценки, заставки без титров) — по размеру: меньше четверти
  обычной серии раздачи (`minis`). Качать ли их — галка «и мини-спецвыпуски» в поиске.
- Спецвыпуски без номера (OVA) и с номерами, которые раздача придумала сама
  («Break Time 03.01»), со спецвыпусками TMDb не совпадают. Совпало название — его
  номер, иначе — в Specials следующими номерами после последнего спецвыпуска TMDb
  (`specials`). Описание — фильма TMDb, если OVA вышла фильмом («Memory Snow»), иначе
  только название из имени файла.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import replace
from pathlib import Path

import library
import metadata
import net
import tmdb
import tvmaze

# Режиссёрская версия — в имени папки или файла.
CUT = re.compile(r"(?i)director'?s?[\s._-]*cut|режисс[её]рск")
# Мини-спецвыпуск — меньше этой доли обычной серии раздачи (по размеру файла).
MINI_SHARE = 0.25
# Склеенная серия режиссёрской версии — не дальше этой доли от средней длины.
MERGE_SLACK = 0.35

# После «Director's Cut» в названии спецвыпуска TVmaze: « - 2 - Название / Название».
_CUT_NUMBER = re.compile(r"\D*?(\d+)\s*[-–—:.]?\s*(.*)$")
_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_WORDS = re.compile(r"[^\W_]+")


# --------------------------------------------------------------------------- #
# Серии сезонов
# --------------------------------------------------------------------------- #

def layout(scan, nb, runtimes=None, maze=None) -> dict[Path, tuple[int, list[int]]]:
    """Серии в папках сезонов сборника, где номер в имени — не номер серии в сезоне.
    → {видео: (сезон, [серии])} в нумерации трекеров (`numbering`). Остальные видео
    (номер и так верный, «S02E01» в имени, спецвыпуски) сюда не попадают.

    `nb` — нумерация тайтла (`numbering.Numbering`). Режиссёрская версия склеивается
    по TVmaze (`maze()` → его спецвыпуски, `cut_parts`), а не знает он её — по
    длительностям TMDb (`runtimes(tmdb_season)` → {серия: минут}, `merge`); нет ни того,
    ни другого — не склеивается."""
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
        if (len(files) < span.count and _is_cut(folder, files)
                and sorted(relative.values()) == list(range(1, len(files) + 1))):
            groups = _split(cut_parts(maze(), season) if maze else [], span.count, len(files))
            if groups is None and runtimes is not None:
                minutes = runtimes(span.tmdb_season) or {}
                groups = merge([minutes.get(span.first + i) for i in range(span.count)],
                               len(files))
        if groups:
            for v, k in relative.items():
                out[v] = (season, groups[k - 1])
        elif offset:
            for v, k in relative.items():
                out[v] = (season, [k])
    return out


def _is_cut(folder: Path, files) -> bool:
    return bool(CUT.search(folder.name)) or any(CUT.search(v.name) for v, _ in files)


def cut_parts(specials, season: int) -> list[int]:
    """Режиссёрская версия сезона по спецвыпускам TVmaze (`tvmaze.specials`): сколько
    ТВ-серий в каждой её серии, по порядку. «Director's Cut - 2 - Reunion With The
    Witch / Starting Life from Zero in Another World» — две. Номера серий режиссёрской
    версии должны идти подряд с первой — иначе [] (не гадать)."""
    cuts = []
    for e in specials or []:
        name = str(e.get("name") or "")
        m = CUT.search(name)
        if not m or e.get("season") != season:
            continue
        num = _CUT_NUMBER.match(name[m.end():])
        if num:
            cuts.append((int(num.group(1)), num.group(2)))
    cuts.sort()
    if [n for n, _ in cuts] != list(range(1, len(cuts) + 1)):
        return []
    return [max(1, sum(1 for t in title.split(" / ") if t.strip())) for _, title in cuts]


def _split(parts, count: int, files: int) -> list[list[int]] | None:
    """[1, 2, 2, …] → [[1], [2, 3], [4, 5], …] — если частей столько же, сколько файлов, и
    вместе они — весь сезон."""
    if not parts or len(parts) != files or sum(parts) != count:
        return None
    groups, start = [], 1
    for p in parts:
        groups.append(list(range(start, start + p)))
        start += p
    return groups


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


def specials(scan, info, taken=(), movie=None, maze=None) -> dict[Path, tuple[int, list[int]]]:
    """Спецвыпуски раздачи, у которых нет номера спецвыпуска TMDb: без номера (OVA) или
    с номером, угаданным по соседним файлам («Break Time 03.01» → «1») — такие номера
    раздача придумала сама. Явный «S00E05» в имени — как есть.

    Название совпало со спецвыпуском TMDb («Memory Snow Manner Movie» — «Memory Snow:
    Manner Movie») — его номер. Иначе — следующий после последнего спецвыпуска TMDb и
    номеров `taken` (уже в медиатеке), по году в пути, затем по имени; в `info.episodes`
    добавляется серия — для имени и .nfo: из фильма TMDb (`movie(название, год)` →
    EpisodeInfo, см. `tmdb_movies`), нет такого — дата, длительность и английское
    название из спецвыпусков TVmaze (`maze()`, см. `tvmaze_specials`), нет и там — с
    названием из имени файла. → {видео: (0, [номер])}."""
    loose = [v for v in scan.videos
             if scan.season_of(v) == 0 and library.parse_episodes(v)[0] is None
             and (not library.parse_episodes(v)[1] or v in scan.guessed)]
    if not loose:
        return {}
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
    for v in sorted(rest, key=lambda v: (_year_of(v, scan.root), str(v))):
        nxt += 1
        title = _title(v, prefix) or v.stem
        year = _year_of(v, scan.root)
        year = None if year == 9999 else year
        found = movie(title, year) if movie and title else None
        if found is None and maze and title:
            found = _maze_special(maze(), title, year, _show_names(info))
        info.episodes[(0, nxt)] = (replace(found, season=0, episode=nxt) if found
                                   else _episode(nxt, title))
        out[v] = (0, [nxt])
    return out


def tvmaze_specials(tmdb_id: int, api_key: str):
    """→ maze() для `layout` и `specials`: спецвыпуски сериала в TVmaze
    (`tvmaze.specials`) по его номерам TheTVDB/IMDb из TMDb. Спрашиваются один раз за
    раскладку и только когда понадобились; не ответил — []."""
    cache: list[list[dict]] = []

    def maze() -> list[dict]:
        if not cache:
            try:
                ids = tmdb.external_ids("tv", tmdb_id, api_key)
                cache.append(tvmaze.specials(ids.get("tvdb_id"), ids.get("imdb_id")))
            except net.NetError:
                cache.append([])
        return cache[0]

    return maze


def _maze_special(specials, title: str, year: int | None, shows) -> metadata.EpisodeInfo | None:
    """Спецвыпуск TVmaze, чьё название (без названия тайтла) совпало с названием OVA из
    имени файла словами — одно содержит все слова другого; год из пути, если есть, — тот
    же (±1). Сначала фильмы и OVA («significant_special»), режиссёрская версия — никогда."""
    want = _tokens(title)
    order = sorted(specials or [], key=lambda e: e.get("type") != "significant_special")
    for e in order:
        name = str(e.get("name") or "")
        if not want or CUT.search(name):
            continue
        own = _part(name, shows)
        have = _tokens(own)
        if not have or not (want <= have or have <= want):
            continue
        aired = str(e.get("airdate") or "")
        if year and aired[:4].isdigit() and abs(int(aired[:4]) - year) > 1:
            continue
        return metadata.EpisodeInfo(0, 0, title=own, title_en=own, aired=aired,
                                    runtime=e.get("runtime") or None)
    return None


def tmdb_movies(info, api_key: str, language: str):
    """→ movie(название OVA, год) для `specials`: OVA, вышедшая фильмом, — фильм TMDb
    этого тайтла («Re:ZERO … Memory Snow», 2018) как EpisodeInfo: название без имени
    тайтла, описание, дата, длительность, оценка, задник — картинкой серии. Фильм
    считается этим тайтлом, если его название начинается с названия тайтла (у аниме —
    японского: «Re:ゼロから始める異世界生活 氷結の絆»). Нет такого — None."""
    shows = _show_names(info)

    def ours(hit: dict) -> bool:
        return any(str(hit.get(k) or "").casefold().startswith(s)
                   for k in ("original_title", "title") for s in shows)

    def movie(title: str, year: int | None) -> metadata.EpisodeInfo | None:
        hit = None
        for y in ([year, None] if year else [None]):
            try:
                hit = next((h for h in tmdb.search("movie", title, api_key, year=y) if ours(h)),
                           None)
            except net.NetError:
                return None
            if hit:
                break
        if hit is None:
            return None
        try:
            local = tmdb.movie(int(hit["id"]), api_key, language=language)
        except net.NetError:
            local = {}
        en = _part(str(hit.get("title") or ""), shows) or title
        rating = metadata.Rating(float(hit["vote_average"]), int(hit.get("vote_count") or 0)) \
            if hit.get("vote_count") else metadata.Rating()
        return metadata.EpisodeInfo(
            0, 0, title=_part(str(local.get("title") or ""), shows) or en, title_en=en,
            original_title=str(hit.get("original_title") or ""),
            plot=str(local.get("overview") or hit.get("overview") or ""),
            aired=str(hit.get("release_date") or ""), runtime=local.get("runtime") or None,
            rating=rating, thumb=tmdb.image_url(hit.get("backdrop_path")) or "")

    return movie


def _show_names(info) -> list[str]:
    return [n.casefold() for n in (info.original_title, info.title_en, info.title) if n]


def _part(title: str, shows) -> str:
    """Название фильма или спецвыпуска без названия тайтла: «Re:ZERO -Starting Life in
    Another World- Memory Snow» → «Memory Snow», «Re: Жизнь в другом мире с нуля:
    Снежные воспоминания» → «Снежные воспоминания», у TVmaze «Re:Zero - Starting Life in
    Another World - The Frozen Bond» → «The Frozen Bond»."""
    low = title.casefold()
    for show in shows:
        if low.startswith(show) and title[len(show):].strip(" -–—:"):
            return title[len(show):].strip(" -–—:")
    if ": " in title:
        return title.rsplit(": ", 1)[-1].strip()
    # Название тайтла записано иначе («Re:Zero - Starting …» вместо «Re:ZERO -Starting …»):
    # снять в начале слова, которые все есть в названии тайтла.
    known = set().union(*(_tokens(s) for s in shows)) if shows else set()
    for m in _WORDS.finditer(title):
        if m.group(0).casefold() not in known:
            rest = title[m.start():].strip(" -–—:")
            return rest if m.start() else title
    return title


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
