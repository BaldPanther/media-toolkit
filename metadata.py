"""Сведение TMDb + Fanart.tv + OMDb в одну модель для .nfo и картинок.

Здесь живёт всё, что знает, «откуда какое поле берётся»; клиенты API
(`tmdb.py`, `fanart.py`, `omdb.py`) остаются тонкими, а `nfo.py`, `artwork.py`
и UI работают уже с готовыми `MediaInfo`/`ArtCandidate` и про источники не знают.

Два языка. Локализованный ответ даёт `<title>`, `<plot>`, `<tagline>` — то, что
человек читает в Kodi. Английский даёт `title_en` для имён папок и файлов и,
отдельным решением, жанры со студиями: в существующей медиатеке они английские
даже там, где описание русское (сверено с `Solo Leveling (2024)/tvshow.nfo`).

Сеть дёргается только в `search()` и `fetch()`; остальное — чистые функции.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

import fanart
import net
import omdb
import tmdb

MOVIE = "movie"
TV = "tv"

# Виды арта, которые скачиваем. Превью серий и banner/discart намеренно не нужны.
ART_POSTER = "poster"
ART_FANART = "fanart"
ART_LOGO = "clearlogo"
ART_SEASON = "seasonposter"

_TMDB = "TMDb"
_FANART = "Fanart.tv"

# При равном языке предпочитаем TMDb: именно оттуда пришли картинки, которые уже
# лежат в медиатеке, и менять их вид без спроса незачем. Fanart.tv нужен как
# источник дополнительных вариантов в сетке выбора и как запасной, когда у TMDb
# логотипа нет вовсе.
_SOURCE_ORDER = (_TMDB, _FANART)

# Виды арта, где язык — это суть картинки: на постере и логотипе написано
# название. У фона надписей обычно нет, и выбирать его по языку бессмысленно —
# там решает разрешение, а надпись поверх Kodi рисует свою.
_LANG_MATTERS = (ART_POSTER, ART_SEASON, ART_LOGO)

# Ниже этой ширины картинка считается непригодной и уходит в конец списка, даже
# если язык подходящий. Иначе единственный русский логотип на 425 пикселей
# обходит английский на 2241 и в медиатеке оказывается мыло.
_MIN_WIDTH = {ART_POSTER: 500, ART_SEASON: 500, ART_LOGO: 500, ART_FANART: 1280}


# --------------------------------------------------------------------------- #
# Модели
# --------------------------------------------------------------------------- #

@dataclass
class Rating:
    value: float | None = None
    votes: int | None = None

    def __bool__(self) -> bool:
        return self.value is not None


@dataclass
class Person:
    name: str
    role: str = ""
    order: int = 0
    thumb: str = ""


@dataclass
class ArtCandidate:
    kind: str                 # poster | fanart | clearlogo | seasonposter
    url: str                  # полноразмерная картинка
    thumb_url: str            # уменьшенная — для сетки выбора
    lang: str = ""            # "ru" / "en" / "" — вариант без текста
    width: int | None = None
    height: int | None = None
    vote: float = 0.0
    source: str = _TMDB
    season: int | None = None

    @property
    def is_png(self) -> bool:
        return self.url.lower().endswith(".png")

    def label(self) -> str:
        """Подпись под миниатюрой: язык, размер, источник."""
        lang = self.lang or "без текста"
        size = f"{self.width}×{self.height}" if self.width and self.height else "?"
        return f"{lang} · {size} · {self.source}"


@dataclass
class SearchHit:
    kind: str
    tmdb_id: int
    title: str
    year: int | None
    overview: str = ""
    poster_url: str = ""

    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass
class EpisodeInfo:
    season: int
    episode: int
    title: str = ""           # локализованное — в .nfo
    title_en: str = ""        # английское — в имя файла
    original_title: str = ""
    plot: str = ""
    aired: str = ""
    runtime: int | None = None
    rating: Rating = field(default_factory=Rating)
    tmdb_id: int | None = None
    # Съёмочная группа и приглашённые актёры конкретной серии — приходят внутри
    # ответа по сезону, отдельных запросов не требуют.
    directors: list[Person] = field(default_factory=list)
    writers: list[Person] = field(default_factory=list)
    guests: list[Person] = field(default_factory=list)


@dataclass
class MediaInfo:
    kind: str
    tmdb_id: int
    imdb_id: str = ""
    tvdb_id: str = ""

    title: str = ""           # локализованное
    title_en: str = ""        # английское — основа имени папки и файлов
    original_title: str = ""
    year: int | None = None

    plot: str = ""
    outline: str = ""
    tagline: str = ""
    runtime: int | None = None
    premiered: str = ""
    mpaa: str = ""
    status: str = ""

    genres: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)

    directors: list[Person] = field(default_factory=list)
    writers: list[Person] = field(default_factory=list)
    actors: list[Person] = field(default_factory=list)

    rating_tmdb: Rating = field(default_factory=Rating)
    rating_imdb: Rating = field(default_factory=Rating)

    set_name: str = ""
    set_overview: str = ""

    episodes: dict[tuple[int, int], EpisodeInfo] = field(default_factory=dict)
    art: list[ArtCandidate] = field(default_factory=list)

    @property
    def folder_title(self) -> str:
        """Название для папки: английское, с откатом на оригинальное."""
        return self.title_en or self.title or self.original_title


# --------------------------------------------------------------------------- #
# Выбор арта
# --------------------------------------------------------------------------- #

def art_of(candidates, kind: str, season: int | None = None) -> list[ArtCandidate]:
    """Кандидаты нужного вида (для постера сезона — нужного сезона)."""
    out = [c for c in candidates if c.kind == kind]
    if kind == ART_SEASON:
        out = [c for c in out if c.season == season]
    return out


def _too_small(c: ArtCandidate) -> int:
    """1 — картинка заведомо мелкая для своего вида. Неизвестный размер (так
    отдаёт Fanart.tv) мелким не считаем: там разрешение стабильно приличное."""
    limit = _MIN_WIDTH.get(c.kind)
    return int(limit is not None and c.width is not None and c.width < limit)


def rank_art(candidates, lang_priority) -> list[ArtCandidate]:
    """Сортировка кандидатов. Правило зависит от вида арта.

    Постеры и логотипы — сначала язык (на них написано название, и выбор между
    русским и английским осмыслен), потом источник, оценка, разрешение.

    Фон (fanart) — сначала «без текста», потом разрешение: надписей там обычно
    нет, язык ни о чём не говорит, а Kodi поверх фона рисует собственный
    логотип, так что чужая надпись только мешает.

    Слишком мелкие для своего вида картинки уходят в конец в любом случае.
    Оценки TMDb (0–10) и лайки Fanart.tv (штуки) между собой не сравниваются:
    источник стоит в ключе выше, и разные шкалы не встречаются.
    """
    prio = list(lang_priority or [])

    def key(c: ArtCandidate):
        src_rank = _SOURCE_ORDER.index(c.source) if c.source in _SOURCE_ORDER else 9
        if c.kind in _LANG_MATTERS:
            lang_rank = prio.index(c.lang) if c.lang in prio else len(prio)
            return (_too_small(c), lang_rank, src_rank, -c.vote, -(c.width or 0))
        return (_too_small(c), 0 if not c.lang else 1, -(c.width or 0), src_rank, -c.vote)

    return sorted(candidates, key=key)


def best_art(candidates, kind: str, lang_priority,
             season: int | None = None) -> ArtCandidate | None:
    """Лучший кандидат вида `kind` — то, что подставляется без ручного выбора."""
    ranked = rank_art(art_of(candidates, kind, season), lang_priority)
    return ranked[0] if ranked else None


def seasons_with_art(candidates) -> list[int]:
    return sorted({c.season for c in candidates
                   if c.kind == ART_SEASON and c.season is not None})


# --------------------------------------------------------------------------- #
# Разбор ответов в кандидатов
# --------------------------------------------------------------------------- #

def _tmdb_art(items, kind: str, size: str, thumb_size: str,
              season: int | None = None) -> list[ArtCandidate]:
    out = []
    for it in items or []:
        path = it.get("file_path")
        if not path:
            continue
        if path.lower().endswith(".svg"):
            continue                    # Kodi векторные логотипы не показывает
        out.append(ArtCandidate(
            kind=kind,
            url=tmdb.image_url(path, size),
            thumb_url=tmdb.image_url(path, thumb_size),
            lang=it.get("iso_639_1") or "",
            width=it.get("width"),
            height=it.get("height"),
            vote=float(it.get("vote_average") or 0.0),
            source=_TMDB,
            season=season,
        ))
    return out


def _fanart_preview(url: str) -> str:
    """Уменьшенная копия картинки Fanart.tv: тот же путь через /preview/."""
    return url.replace("/fanart/", "/preview/", 1) if "/fanart/" in url else url


def _fanart_art(block, kind: str, season: int | None = None) -> list[ArtCandidate]:
    out = []
    for it in block or []:
        url = it.get("url")
        if not url:
            continue
        try:
            item_season = int(it["season"]) if it.get("season") not in (None, "", "all") else None
        except (TypeError, ValueError):
            item_season = None
        if kind == ART_SEASON:
            if season is not None and item_season != season:
                continue
            item_season = season
        out.append(ArtCandidate(
            kind=kind,
            url=url,
            thumb_url=_fanart_preview(url),
            lang="" if (it.get("lang") or "") in ("", "00") else it["lang"],
            vote=float(it.get("likes") or 0),
            source=_FANART,
            season=item_season,
        ))
    return out


def _collect_tmdb_art(kind: str, tmdb_id: int, key: str, seasons, settings) -> list[ArtCandidate]:
    data = tmdb.images(kind, tmdb_id, key)
    art = []
    art += _tmdb_art(data.get("posters"), ART_POSTER, settings.poster_size, "w342")
    art += _tmdb_art(data.get("backdrops"), ART_FANART, settings.fanart_size, "w300")
    art += _tmdb_art(data.get("logos"), ART_LOGO, settings.logo_size, "w300")
    if kind == TV:
        for n in seasons:
            season_data = tmdb.season_images(tmdb_id, n, key)
            art += _tmdb_art(season_data.get("posters"), ART_SEASON,
                             settings.poster_size, "w342", season=n)
    return art


def _collect_fanart_art(kind: str, ids: dict, seasons, settings) -> list[ArtCandidate]:
    """Арт из Fanart.tv. Отсутствие ключа или записи — не ошибка, просто меньше вариантов."""
    if not settings.has_fanart():
        return []
    try:
        if kind == MOVIE:
            data = fanart.movie_art(ids.get("imdb") or ids.get("tmdb"), settings.fanart_key)
            keys = fanart.MOVIE_KEYS
        else:
            if not ids.get("tvdb"):
                return []           # TV-эндпоинт Fanart.tv адресуется только TVDb-ID
            data = fanart.tv_art(ids["tvdb"], settings.fanart_key)
            keys = fanart.TV_KEYS
    except net.NetError:
        return []
    art = []
    for art_kind, block_names in keys.items():
        for name in block_names:
            block = data.get(name)
            if not block:
                continue
            if art_kind == ART_SEASON:
                for n in seasons:
                    art += _fanart_art(block, ART_SEASON, season=n)
            else:
                art += _fanart_art(block, art_kind)
    return art


# --------------------------------------------------------------------------- #
# Разбор карточки тайтла
# --------------------------------------------------------------------------- #

def _year(date_str: str | None) -> int | None:
    try:
        return int(str(date_str or "")[:4])
    except ValueError:
        return None


def _people(cast, limit: int = 25) -> list[Person]:
    out = []
    for i, c in enumerate(cast or []):
        if i >= limit:
            break
        roles = c.get("roles")           # aggregate_credits у сериалов
        role = (roles[0].get("character", "") if roles else c.get("character", "")) or ""
        out.append(Person(name=c.get("name", ""), role=role,
                          order=c.get("order", i),
                          thumb=tmdb.image_url(c.get("profile_path"), "original") or ""))
    return out


def _crew(crew, jobs=(), department="") -> list[Person]:
    seen, out = set(), []
    for c in crew or []:
        if (jobs and c.get("job") in jobs) or (department and c.get("department") == department):
            name = c.get("name", "")
            if name and name not in seen:
                seen.add(name)
                out.append(Person(name=name, role=c.get("job", "")))
    return out


# --------------------------------------------------------------------------- #
# Альтернативная нумерация сезонов (episode groups)
# --------------------------------------------------------------------------- #

def _group_season_number(subgroup: dict, index: int) -> int:
    """Номер сезона у подгруппы: из названия «Season 2», иначе по порядку."""
    m = re.search(r"(\d{1,2})", subgroup.get("name") or "")
    if m:
        return int(m.group(1))
    return int(subgroup.get("order", index)) + 1


def _score_group(detail: dict, season_sizes: dict) -> int:
    """Насколько разбивка совпала с диском: +1 за каждый сезон точного размера.

    Совпадение по числу серий — единственная защита от кривого сопоставления.
    Промахнёмся — в .nfo уедут описания чужих серий, поэтому ниже берутся
    только те сезоны, что совпали точно.
    """
    score = 0
    for i, sub in enumerate(detail.get("groups", [])):
        number = _group_season_number(sub, i)
        if season_sizes.get(number) == len(sub.get("episodes", [])):
            score += 1
    return score


def renumbering_map(detail: dict, season_sizes: dict) -> dict:
    """Подгруппы → {(сезон на диске, серия): (сезон TMDb, серия TMDb)}.

    Берём только сезоны, у которых число серий совпало с диском.
    """
    mapping = {}
    for i, sub in enumerate(detail.get("groups", [])):
        number = _group_season_number(sub, i)
        episodes = sub.get("episodes", [])
        if season_sizes.get(number) != len(episodes):
            continue
        for position, ep in enumerate(episodes, start=1):
            src, dst = ep.get("season_number"), ep.get("episode_number")
            if src is not None and dst is not None:
                mapping[(number, position)] = (src, dst)
    return mapping


def find_renumbering(tmdb_id: int, api_key: str, season_sizes: dict,
                     language: str = "en-US", limit: int = 4) -> dict:
    """Ищет в episode groups разбивку, совпадающую с раскладкой на диске.

    Возвращает таблицу перенумерации или пустой словарь, если подходящей нет.
    Запрашивается только при промахе по обычным сезонам, так что лишних
    обращений к API в обычной жизни не возникает.
    """
    try:
        groups = tmdb.episode_groups(tmdb_id, api_key)
    except net.NetError:
        return {}
    best, best_score = {}, 0
    for meta in groups[:limit]:
        if not meta.get("id"):
            continue
        try:
            detail = tmdb.episode_group(meta["id"], api_key, language=language)
        except net.NetError:
            continue
        score = _score_group(detail, season_sizes)
        if score > best_score:
            best, best_score = renumbering_map(detail, season_sizes), score
    return best


def _movie_mpaa(release_dates) -> str:
    for block in (release_dates or {}).get("results", []):
        if block.get("iso_3166_1") != "US":
            continue
        for rel in block.get("release_dates", []):
            cert = (rel.get("certification") or "").strip()
            if cert:
                return f"US:{cert}"
    return ""


def _tv_mpaa(content_ratings) -> str:
    for block in (content_ratings or {}).get("results", []):
        if block.get("iso_3166_1") == "US" and (block.get("rating") or "").strip():
            return f"US:{block['rating'].strip()}"
    return ""


# --------------------------------------------------------------------------- #
# Публичное API
# --------------------------------------------------------------------------- #

def search(kind: str, query: str, settings, year: int | None = None) -> list[SearchHit]:
    """Поиск тайтла по названию. Возвращает то, что показывается в диалоге выбора."""
    hits = []
    for r in tmdb.search(kind, query, settings.tmdb_key, year=year):
        title = r.get("title") or r.get("name") or ""
        date = r.get("release_date") or r.get("first_air_date")
        hits.append(SearchHit(
            kind=kind, tmdb_id=r.get("id"), title=title, year=_year(date),
            overview=r.get("overview") or "",
            poster_url=tmdb.image_url(r.get("poster_path"), "w185") or "",
        ))
    return hits


def search_by_imdb(imdb_id: str, settings) -> list[SearchHit]:
    """Поиск по вставленному IMDb-ID — запасной путь, когда название не находится."""
    data = tmdb.find_by_imdb(imdb_id, settings.tmdb_key)
    hits = []
    for kind, key in ((MOVIE, "movie_results"), (TV, "tv_results")):
        for r in data.get(key, []) or []:
            date = r.get("release_date") or r.get("first_air_date")
            hits.append(SearchHit(
                kind=kind, tmdb_id=r.get("id"),
                title=r.get("title") or r.get("name") or "", year=_year(date),
                overview=r.get("overview") or "",
                poster_url=tmdb.image_url(r.get("poster_path"), "w185") or "",
            ))
    return hits


def _season_episodes(tmdb_id: int, number: int, key: str, lang: str,
                     dual: bool) -> dict[int, EpisodeInfo]:
    """Серии одного сезона TMDb, ключ — номер серии в нумерации TMDb."""
    loc_season = tmdb.season(tmdb_id, number, key, language=lang)
    eng_season = tmdb.season(tmdb_id, number, key, language="en-US") if dual else loc_season
    eng_by_num = {e.get("episode_number"): e for e in eng_season.get("episodes", [])}
    out: dict[int, EpisodeInfo] = {}
    for e in loc_season.get("episodes", []):
        num = e.get("episode_number")
        if num is None:
            continue
        en = eng_by_num.get(num, {})
        out[num] = EpisodeInfo(
            season=number, episode=num,
            title=e.get("name") or en.get("name") or "",
            title_en=en.get("name") or e.get("name") or "",
            plot=e.get("overview") or en.get("overview") or "",
            aired=e.get("air_date") or "",
            runtime=e.get("runtime") or None,
            rating=Rating(e.get("vote_average") or None, e.get("vote_count") or None),
            tmdb_id=e.get("id"),
            directors=_crew(e.get("crew"), jobs=("Director",)),
            writers=_crew(e.get("crew"), department="Writing"),
            guests=_people(e.get("guest_stars"), limit=10),
        )
    return out


def fetch(kind: str, tmdb_id: int, settings, seasons=(), season_sizes=None,
          progress=None) -> MediaInfo:
    """Полная карточка тайтла: метаданные на двух языках, рейтинги, арт.

    `seasons` — номера сезонов, которые реально есть на диске: серии и постеры
    качаем только для них, а не для всего сериала. `season_sizes` — сколько в
    каждом из них серий; по этим числам подбирается альтернативная разбивка,
    если обычные сезоны TMDb с диском не сошлись.
    """
    key = settings.tmdb_key
    lang = settings.meta_language
    dual = lang != "en-US"

    def step(text):
        if progress:
            progress(text)

    step("Карточка тайтла…")
    if kind == MOVIE:
        loc = tmdb.movie(tmdb_id, key, language=lang)
        eng = tmdb.movie(tmdb_id, key, language="en-US") if dual else loc
    else:
        loc = tmdb.tv(tmdb_id, key, language=lang)
        eng = tmdb.tv(tmdb_id, key, language="en-US") if dual else loc

    ext = loc.get("external_ids") or {}
    ids = {
        "tmdb": str(tmdb_id),
        "imdb": ext.get("imdb_id") or "",
        "tvdb": str(ext.get("tvdb_id") or ""),
    }

    info = MediaInfo(
        kind=kind, tmdb_id=tmdb_id, imdb_id=ids["imdb"], tvdb_id=ids["tvdb"],
        # Жанры и студии берём из английского ответа — так в текущей медиатеке.
        genres=[g.get("name", "") for g in eng.get("genres", [])],
        countries=[c.get("iso_3166_1", "") for c in eng.get("production_countries", [])],
        rating_tmdb=Rating(loc.get("vote_average") or None, loc.get("vote_count") or None),
        plot=loc.get("overview") or eng.get("overview") or "",
        tagline=loc.get("tagline") or eng.get("tagline") or "",
    )

    if kind == MOVIE:
        info.title = loc.get("title") or ""
        info.title_en = eng.get("title") or ""
        info.original_title = loc.get("original_title") or ""
        info.premiered = loc.get("release_date") or ""
        info.runtime = loc.get("runtime") or None
        info.mpaa = _movie_mpaa(loc.get("release_dates"))
        info.outline = info.plot
        info.studios = [c.get("name", "") for c in eng.get("production_companies", [])]
        credits = loc.get("credits") or {}
        info.actors = _people(credits.get("cast"))
        info.directors = _crew(credits.get("crew"), jobs=("Director",))
        info.writers = _crew(credits.get("crew"), department="Writing")
        coll = loc.get("belongs_to_collection") or {}
        if coll.get("id"):
            step("Коллекция…")
            eng_coll = tmdb.collection(coll["id"], key, language="en-US")
            loc_coll = tmdb.collection(coll["id"], key, language=lang) if dual else eng_coll
            info.set_name = eng_coll.get("name") or coll.get("name") or ""
            info.set_overview = loc_coll.get("overview") or ""
    else:
        info.title = loc.get("name") or ""
        info.title_en = eng.get("name") or ""
        info.original_title = loc.get("original_name") or ""
        info.premiered = loc.get("first_air_date") or ""
        runtimes = loc.get("episode_run_time") or []
        info.runtime = runtimes[0] if runtimes else None
        info.mpaa = _tv_mpaa(loc.get("content_ratings"))
        info.status = loc.get("status") or ""
        info.studios = [n.get("name", "") for n in eng.get("networks", [])]
        info.countries = list(eng.get("origin_country") or [])
        info.actors = _people((loc.get("aggregate_credits") or {}).get("cast"))
        info.directors = _crew((loc.get("aggregate_credits") or {}).get("crew"),
                               jobs=("Director",))

    info.year = _year(info.premiered)

    if info.imdb_id and settings.has_omdb():
        step("Рейтинг IMDb…")
        value, votes = omdb.rating(info.imdb_id, settings.omdb_key)
        info.rating_imdb = Rating(value, votes)

    season_numbers = sorted({int(s) for s in seasons})
    # Запоминаем, был ли у сериала свой runtime: если нет, возьмём его из серий
    # ниже. У многих сериалов TMDb отдаёт episode_run_time пустым, а Kodi ждёт
    # число — в файлах tinyMediaManager оно проставлено.
    need_runtime = kind == TV and not info.runtime
    if kind == TV and season_numbers:
        cache: dict[int, dict[int, EpisodeInfo]] = {}
        for n in season_numbers:
            step(f"Сезон {n}…")
            cache[n] = _season_episodes(tmdb_id, n, key, lang, dual)
            for num, ep in cache[n].items():
                info.episodes[(n, num)] = ep

        # Сезоны, которых у TMDb в обычной структуре нет. Самый частый случай —
        # аниме: «Solo Leveling» там один сезон из 25 серий, а на диске два.
        # Официальная разбивка лежит в episode groups, ищем её там.
        missing = [n for n in season_numbers if not cache.get(n)]
        if missing and season_sizes:
            step("Альтернативная нумерация…")
            mapping = find_renumbering(tmdb_id, key, season_sizes, language=lang)
            for (disk_season, disk_ep), (src_season, src_ep) in sorted(mapping.items()):
                if disk_season not in missing:
                    continue
                if src_season not in cache:
                    step(f"Сезон {src_season} (по группе)…")
                    cache[src_season] = _season_episodes(tmdb_id, src_season, key, lang, dual)
                found = cache[src_season].get(src_ep)
                if found is None:
                    continue
                # Номера в .nfo должны совпасть с именем файла на диске, иначе
                # Kodi не свяжет их: так же это записано в файлах от tmm.
                info.episodes[(disk_season, disk_ep)] = replace(
                    found, season=disk_season, episode=disk_ep)

    if need_runtime:
        lengths = sorted(e.runtime for e in info.episodes.values() if e.runtime)
        if lengths:
            info.runtime = lengths[len(lengths) // 2]     # медиана, а не первая серия:
                                                          # пилот и финал часто длиннее
    step("Картинки…")
    info.art = _collect_tmdb_art(kind, tmdb_id, key, season_numbers, settings)
    info.art += _collect_fanart_art(kind, ids, season_numbers, settings)
    return info
