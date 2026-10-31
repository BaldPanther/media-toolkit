"""Поиск раздач: все названия тайтла из TMDb → источники → склейка → рекомендация.

- Названия — все, под которыми трекеры могут знать тайтл: русское, английское,
  оригинальное, альтернативные (дорама «Самый богатый бедняк» на трекерах —
  «Без труда не вытащишь рыбку из пруда»).
- Источники — список в `search_sources.json` (каталог настроек): `jacred`
  (jac.red или свой JacRed) и `torznab` (Jackett, Prowlarr). Не код: репозиторий
  публичный, а добавить источник — дописать строку, без пересборки. Плюс Knaben
  (The Pirate Bay, 1337x, Nyaa) — английские раздачи, под галкой в поиске.
- Одна и та же раздача с разных трекеров склеивается по infohash: трекеры
  перечисляются, сиды берутся наибольшие (рой у неё общий).
- Год: у сериала годится и год начала, и год выбранного сезона (±1) — трекеры
  пишут «(2026) Сезон 2» у сериала 2024-го.
- Рекомендация: русские дорожки → все серии → качество → любимая озвучка → сиды.
  Мало сидов — не повод убирать из списка: «для коллекции подожду неделю».

Только stdlib.
"""
from __future__ import annotations

import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import library
import net
import nfo
import paths
import tmdb

_SOURCES_NAME = "search_sources.json"
_PREFS_NAME = "search_settings.json"
DEFAULT_SOURCES = [{"type": "jacred", "url": "https://jac.red"}]
KNABEN = {"type": "knaben", "url": "https://api.knaben.org/v1"}
QUALITIES = (2160, 1080, 720, 480)
LOW_SEEDS = 5

TV_TYPES = {"serial", "multserial", "docuserial", "anime", "tvshow"}
MOVIE_TYPES = {"movie", "multfilm", "documovie", "anime"}


# --------------------------------------------------------------------------- #
# Настройки поиска и источники
# --------------------------------------------------------------------------- #

@dataclass
class Prefs:
    quality: int = 2160                 # поиск показывает только его; слежение спускается ниже
    hdr: bool = False                   # экран показывает HDR; нет — HDR-раздачи скрыты
    dv: bool = False                    # экран показывает Dolby Vision; нет — DV-раздачи скрыты
    voices: list[str] = field(default_factory=lambda: ["Кубик в Кубе", "TVShows"])
    english: bool = True                # показывать английские раздачи (Knaben)


def load_prefs() -> Prefs:
    d = paths.read_json(paths.settings_path(_PREFS_NAME))
    if isinstance(d, dict):
        try:
            base = Prefs()
            q = int(d.get("quality", base.quality))
            return Prefs(quality=q if q in QUALITIES else base.quality,
                         hdr=bool(d.get("hdr", base.hdr)), dv=bool(d.get("dv", base.dv)),
                         voices=[str(v) for v in d.get("voices", base.voices) if str(v).strip()],
                         english=bool(d.get("english", base.english)))
        except Exception:  # noqa: BLE001 — поле неожиданного типа: значения по умолчанию
            pass
    return Prefs()


def save_prefs(p: Prefs) -> None:
    paths.write_json(paths.settings_path(_PREFS_NAME), asdict(p))


def load_sources() -> list[dict]:
    """Источники из файла; нет файла — он создаётся с jac.red, чтобы было что править."""
    path = paths.settings_path(_SOURCES_NAME)
    d = paths.read_json(path)
    if isinstance(d, list) and all(isinstance(s, dict) for s in d):
        return [s for s in d if s.get("type") in ("jacred", "torznab") and s.get("url")]
    try:
        paths.write_json(path, DEFAULT_SOURCES)
    except OSError:
        pass
    return list(DEFAULT_SOURCES)


# --------------------------------------------------------------------------- #
# Тайтл
# --------------------------------------------------------------------------- #

@dataclass
class Season:
    number: int
    episodes: int
    year: int | None


@dataclass
class Title:
    kind: str
    tmdb_id: int
    title: str                          # русское (язык описаний)
    year: int | None
    names: list[str] = field(default_factory=list)   # все названия для поиска
    original: str = ""
    seasons: list[Season] = field(default_factory=list)
    status: str = ""                    # у сериала: «Returning Series», «Ended»
    last_aired: tuple[int, int] = (0, 0)     # (сезон, серия) последней вышедшей
    next_air: tuple[int, int, str] = (0, 0, "")  # следующая: (сезон, серия, дата)

    def season(self, number: int) -> Season | None:
        return next((s for s in self.seasons if s.number == number), None)

    def aired(self, number: int) -> int:
        """Сколько серий сезона уже вышло (по последней вышедшей серии)."""
        season, episode = self.last_aired
        s = self.season(number)
        if s is None or season < number:
            return 0
        return s.episodes if season > number else min(episode, s.episodes)

    def season_complete(self, number: int) -> bool:
        """Сезон вышел целиком: все серии позади и следующей в нём не ждут."""
        s = self.season(number)
        if s is None or not s.episodes:
            return False
        if self.next_air[0] == number:
            return False
        return self.aired(number) >= s.episodes


def _year(date: str | None) -> int | None:
    m = re.match(r"(\d{4})", date or "")
    return int(m.group(1)) if m else None


def title_info(kind: str, tmdb_id: int, api_key: str) -> Title:
    d = tmdb.titles(kind, tmdb_id, api_key)
    name = d.get("title") or d.get("name") or ""
    original = d.get("original_title") or d.get("original_name") or ""
    t = Title(kind=kind, tmdb_id=tmdb_id, title=name, original=original,
              year=_year(d.get("release_date") or d.get("first_air_date")),
              status=d.get("status") or "")
    names = [name, original]
    for tr in ((d.get("translations") or {}).get("translations") or []):
        if tr.get("iso_639_1") in ("en", "ru"):
            data = tr.get("data") or {}
            names.append(data.get("title") or data.get("name") or "")
    # Альтернативные — только российские: американские бывают рабочими
    # названиями («Durango 60623»), по которым трекеры тайтл не знают.
    alt = d.get("alternative_titles") or {}
    for a in alt.get("titles") or alt.get("results") or []:
        if a.get("iso_3166_1") == "RU":
            names.append(a.get("title") or "")
    seen = set()
    for n in names:
        key = n.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            t.names.append(n.strip())
    last, nxt = d.get("last_episode_to_air") or {}, d.get("next_episode_to_air") or {}
    t.last_aired = (int(last.get("season_number") or 0), int(last.get("episode_number") or 0))
    t.next_air = (int(nxt.get("season_number") or 0), int(nxt.get("episode_number") or 0),
                  str(nxt.get("air_date") or ""))
    for s in d.get("seasons") or []:
        number = s.get("season_number")
        if isinstance(number, int) and number > 0:
            t.seasons.append(Season(number, int(s.get("episode_count") or 0),
                                    _year(s.get("air_date"))))
    return t


# --------------------------------------------------------------------------- #
# Раздачи
# --------------------------------------------------------------------------- #

@dataclass
class Release:
    hash: str
    title: str
    magnet: str
    size: int = 0
    seeds: int = 0
    peers: int = 0
    updated: str = ""
    quality: int = 0
    hdr: bool = False
    dv: bool = False
    dv_only: bool = False
    voices: list[str] = field(default_factory=list)
    seasons: list[int] = field(default_factory=list)
    year: int | None = None
    types: list[str] = field(default_factory=list)
    trackers: list[list[str]] = field(default_factory=list)   # [[трекер, страница], …]
    episodes: list[int] = field(default_factory=list)          # [первая, последняя, всего]
    pack: bool = False                  # «S01» без серий — весь сезон (английская запись)
    # Заполняет rank():
    score: tuple = ()
    recommended: bool = False
    why: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("score")
        return d


_BTIH = re.compile(r"urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")
_EPISODES = re.compile(
    r"(?:(\d{1,4})\s*[-–]\s*)?(\d{1,4})\s*(?:сери[яий]\s*)?из\s*(\d{1,4})", re.I)
_DV = re.compile(r"dolby\s*vision|\bdovi\b|\bdv\b", re.I)
_HDR = re.compile(r"\bhdr(?:10\+?)?\b", re.I)
_QUALITY = re.compile(r"\b(2160|1080|720|480)[pi]\b|\b(4k|uhd)\b", re.I)
_SEASON_RANGE = re.compile(r"(?:сезон[ыа]?|seasons?)\s*[:.]?\s*(\d{1,2})\s*[-–]\s*(\d{1,2})"
                           r"|\[?s(\d{1,2})\s*[-–]\s*s?(\d{1,2})\]?", re.I)
# Английская запись: «S01», «S01E07», «S01E01-E08», «Season 1».
_SCENE = re.compile(r"\bs(\d{1,2})(?:[ .]?e(\d{1,3})(?:-?e?(\d{1,3}))?)?\b", re.I)
_SEASON_WORD = re.compile(r"\bseason[ .]?(\d{1,2})\b", re.I)
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_CYRILLIC = re.compile(r"[а-яё]", re.I)
_RUS_TAG = re.compile(r"\brus\b", re.I)


def infohash(magnet: str) -> str:
    m = _BTIH.search(magnet or "")
    return m.group(1).lower() if m else ""


def parse_title(text: str) -> dict:
    """Что можно вытащить из названия раздачи, когда источник не разобрал сам."""
    out: dict = {}
    m = _EPISODES.search(text or "")
    if m:
        last, total = int(m.group(2)), int(m.group(3))
        first = int(m.group(1)) if m.group(1) else last
        if 0 < last <= total < 2000:
            out["episodes"] = [first, last, total]
    q = _QUALITY.search(text or "")
    if q:
        out["quality"] = int(q.group(1)) if q.group(1) else 2160
    dv = bool(_DV.search(text or ""))
    hdr = bool(_HDR.search(text or ""))
    out["dv"] = dv
    out["hdr"] = hdr or dv
    out["dv_only"] = dv and not hdr
    r = _SEASON_RANGE.search(text or "")
    if r:
        a, b = (int(r.group(1)), int(r.group(2))) if r.group(1) else (int(r.group(3)), int(r.group(4)))
        if 0 < a <= b < 100:
            out["seasons"] = list(range(a, b + 1))
    elif m := _SCENE.search(text or ""):
        out["seasons"] = [int(m.group(1))]
        if m.group(2):
            first = int(m.group(2))
            last = int(m.group(3) or first)
            if "episodes" not in out and 0 < first <= last:
                out["episodes"] = [first, last, 0]      # всего — из TMDb, см. complete()
        else:
            out["pack"] = True
    elif m := _SEASON_WORD.search(text or ""):
        out["seasons"] = [int(m.group(1))]
        out["pack"] = True
    return out


def russian(rel: Release) -> bool:
    """Есть ли русские дорожки — насколько видно по озвучкам и названию раздачи."""
    return bool(rel.voices or _CYRILLIC.search(rel.title) or _RUS_TAG.search(rel.title))


def _from_jacred(item: dict) -> Release | None:
    magnet = str(item.get("magnet") or "")
    h = infohash(magnet)
    if not h:
        return None
    title = html.unescape(str(item.get("title") or ""))     # jac.red отдаёт «&amp;»
    parsed = parse_title(title)
    quality = int(item.get("quality") or 0) or parsed.get("quality", 0)
    rel = Release(
        hash=h, title=title, magnet=magnet, size=int(item.get("size") or 0),
        seeds=int(item.get("sid") or 0), peers=int(item.get("pir") or 0),
        updated=str(item.get("updateTime") or item.get("createTime") or ""),
        quality=quality,
        hdr=str(item.get("videotype") or "").lower() == "hdr" or parsed["hdr"],
        dv=parsed["dv"], dv_only=parsed["dv_only"],
        voices=[str(v) for v in item.get("voices") or []],
        seasons=[int(s) for s in item.get("seasons") or [] if str(s).isdigit()]
        or parsed.get("seasons", []),
        year=int(item["relased"]) if str(item.get("relased") or "").isdigit() else None,
        types=[str(t) for t in item.get("types") or []],
        trackers=[[str(item.get("tracker") or "?"), str(item.get("url") or "")]],
        episodes=parsed.get("episodes", []),
    )
    return rel


def _jacred(source: dict, title: Title) -> list[Release]:
    base = str(source["url"]).rstrip("/")
    found: dict[tuple, Release] = {}
    original = title.original if title.original.casefold() != title.title.casefold() else ""
    queries = [{"search": title.title, "altname": original}] if title.title else []
    # Каждый запрос — сотни раздач и секунды ожидания: остальных названий хватит трёх.
    queries += [{"search": n} for n in title.names if n not in (title.title, original)][:3]
    for q in queries:
        params = {k: v for k, v in q.items() if v}
        params["exact"] = "true"
        if source.get("apikey"):
            params["apikey"] = source["apikey"]
        data = net.get_json(f"{base}/api/v1.0/torrents?{urllib.parse.urlencode(params)}",
                            timeout=30) or []
        for item in data if isinstance(data, list) else []:
            rel = _from_jacred(item)
            if rel is not None:
                found.setdefault((rel.trackers[0][0], rel.trackers[0][1], rel.hash), rel)
    return list(found.values())


_TZ = "{http://torznab.com/schemas/2015/feed}"


def _torznab(source: dict, title: Title) -> list[Release]:
    base = str(source["url"]).rstrip("/")
    out = []
    for name in title.names[:4]:
        params = {"t": "search", "q": name}
        if source.get("apikey"):
            params["apikey"] = source["apikey"]
        try:
            raw = net.get_bytes(f"{base}?{urllib.parse.urlencode(params)}", timeout=40)
            root = ET.fromstring(raw)
        except (net.NetError, ET.ParseError):
            continue
        for it in root.iter("item"):
            attrs = {a.get("name"): a.get("value") for a in it.iter(f"{_TZ}attr")}
            magnet = attrs.get("magneturl") or ""
            h = (attrs.get("infohash") or infohash(magnet)).lower()
            if not h:
                continue
            text = it.findtext("title") or ""
            parsed = parse_title(text)
            year = _YEAR.search(text)
            out.append(Release(
                hash=h, title=text, magnet=magnet or f"magnet:?xt=urn:btih:{h}",
                size=int(it.findtext("size") or attrs.get("size") or 0),
                seeds=int(attrs.get("seeders") or 0), peers=int(attrs.get("peers") or 0),
                updated=it.findtext("pubDate") or "", quality=parsed.get("quality", 0),
                hdr=parsed["hdr"], dv=parsed["dv"], dv_only=parsed["dv_only"],
                seasons=parsed.get("seasons", []), year=int(year.group(1)) if year else None,
                trackers=[[it.findtext("jackettindexer") or source.get("name") or "torznab",
                           it.findtext("comments") or ""]],
                episodes=parsed.get("episodes", []), pack=parsed.get("pack", False)))
    return out


_KNABEN_CATEGORY = {library.TV: 2_000_000, library.MOVIE: 3_000_000}


def _knaben(source: dict, title: Title) -> list[Release]:
    """Knaben — поиск по The Pirate Bay, 1337x, Nyaa и др.: в основном английские
    раздачи. Русские названия не спрашиваем: русские трекеры — у jac.red."""
    url = str(source.get("url") or KNABEN["url"])
    cat = _KNABEN_CATEGORY[title.kind]
    out = []
    for name in [n for n in title.names if not _CYRILLIC.search(n)][:2]:
        data = net.post_json(url, {"search_type": "100%", "search_field": "title", "query": name,
                                   "order_by": "seeders", "order_direction": "desc", "size": 300,
                                   "hide_unsafe": True, "hide_xxx": True}, timeout=30)
        for it in data.get("hits", []) if isinstance(data, dict) else []:
            # Категории: 2xxxxxx — сериалы, 3xxxxxx — фильмы, остальное (музыка…) мимо.
            cats = [int(c) for c in it.get("categoryId") or [] if str(c).isdigit()]
            if cats and not any(cat <= c < cat + 1_000_000 for c in cats):
                continue
            magnet = str(it.get("magnetUrl") or "")
            h = str(it.get("hash") or "").lower() or infohash(magnet)
            if not h:
                continue
            text = str(it.get("title") or "")
            parsed = parse_title(text)
            year = _YEAR.search(text)
            out.append(Release(
                hash=h, title=text, magnet=magnet or f"magnet:?xt=urn:btih:{h}",
                size=int(it.get("bytes") or 0), seeds=int(it.get("seeders") or 0),
                peers=int(it.get("peers") or 0), updated=str(it.get("date") or ""),
                quality=parsed.get("quality", 0),
                hdr=parsed["hdr"], dv=parsed["dv"], dv_only=parsed["dv_only"],
                seasons=parsed.get("seasons", []), year=int(year.group(1)) if year else None,
                trackers=[[str(it.get("tracker") or "Knaben"), str(it.get("details") or "")]],
                episodes=parsed.get("episodes", []), pack=parsed.get("pack", False)))
    return out


def group(items: list[Release]) -> list[Release]:
    """Одна раздача на разных трекерах — одна строка: трекеры списком, сиды — наибольшие."""
    by_hash: dict[str, Release] = {}
    for r in items:
        g = by_hash.get(r.hash)
        if g is None:
            by_hash[r.hash] = r
            continue
        # Трекер — один раз, даже если раздача лежит у него в двух темах.
        for tr in r.trackers:
            if all(tr[0] != have[0] for have in g.trackers):
                g.trackers.append(tr)
        if r.seeds > g.seeds:
            g.seeds, g.peers = r.seeds, max(g.peers, r.peers)
        g.updated = max(g.updated, r.updated)
        g.quality = g.quality or r.quality
        if r.quality > g.quality and r.quality in QUALITIES:
            g.quality = r.quality
        # «Только Dolby Vision» — пока ни один трекер не назвал рядом HDR10.
        if r.dv:
            g.dv_only = r.dv_only and (g.dv_only or not g.dv)
        g.hdr, g.dv = g.hdr or r.hdr, g.dv or r.dv
        g.voices += [v for v in r.voices if v not in g.voices]
        g.seasons = sorted(set(g.seasons) | set(r.seasons))
        g.types += [t for t in r.types if t not in g.types]
        g.year = g.year or r.year
        g.episodes = g.episodes or r.episodes
        g.pack = g.pack or r.pack
        # Название — самое подробное: у rutracker в нём и сезон, и озвучки.
        if len(r.title) > len(g.title):
            g.title = r.title
    return list(by_hash.values())


def find(title: Title, sources=None, log=None) -> list[Release]:
    """Все раздачи со всех источников, склеенные. Источник, который не ответил, — в журнал.

    Без явного списка — источники из файла и Knaben. Английские раздачи ищутся
    всегда, а показываются по галке: переключить её можно без нового поиска.
    Источники опрашиваются разом — общее ожидание по самому медленному.
    """
    log = log or (lambda text: None)
    if sources is None:
        sources = load_sources() + [KNABEN]
    readers = {"jacred": _jacred, "torznab": _torznab, "knaben": _knaben}

    def read(source: dict) -> list[Release]:
        try:
            return readers[source["type"]](source, title)
        except (net.NetError, OSError) as e:
            log(f"⚠ Поиск: {source.get('url')}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:
        parts = list(pool.map(read, [s for s in sources if s.get("type") in readers]))
    return group([r for part in parts for r in part])


# --------------------------------------------------------------------------- #
# Фильтр и рекомендация
# --------------------------------------------------------------------------- #

def fits(rel: Release, title: Title, seasons=(), any_year: bool = False) -> bool:
    """Подходит ли раздача тайтлу: тип, год, выбранные сезоны."""
    wanted_types = TV_TYPES if title.kind == library.TV else MOVIE_TYPES
    if rel.types and not set(rel.types) & wanted_types:
        return False
    if not any_year and rel.year and title.year:
        years = {title.year}
        if title.kind == library.TV:
            # Год выбранных сезонов и сезонов внутри раздачи: сборник «S01-02»
            # трекеры подписывают годом последнего.
            inside = set(seasons) | set(rel.seasons)
            years |= {s.year for s in title.seasons if s.year and (not inside or s.number in inside)}
        if all(abs(rel.year - y) > 1 for y in years):
            return False
    if title.kind == library.TV and seasons and rel.seasons and not set(rel.seasons) & set(seasons):
        return False
    return True


def has_voice(rel: Release, voice: str) -> bool:
    have = " ".join(rel.voices + [rel.title]).casefold().replace("ё", "е")
    return voice.casefold().replace("ё", "е") in have


def _voice_rank(rel: Release, voices: list[str]) -> int:
    """Номер любимой озвучки в раздаче (меньше — лучше); нет ни одной — их число."""
    return next((i for i, v in enumerate(voices) if has_voice(rel, v)), len(voices))


def quality_of(rel: Release) -> int:
    """Кнопка качества, под которой раздача: в SD — и всё без пометки качества."""
    return rel.quality if rel.quality in QUALITIES[:-1] else QUALITIES[-1]


def picture_ok(rel: Release, prefs: Prefs) -> bool:
    """HDR и Dolby Vision — только с галками. Гибрид HDR10 + DV годится под любую
    из двух; «только Dolby Vision» (без слоя HDR) — только под DV."""
    if not rel.hdr:
        return True
    if rel.dv and prefs.dv:
        return True
    return prefs.hdr and not rel.dv_only


def _picture_rank(rel: Release, prefs: Prefs) -> int:
    if rel.dv and prefs.dv:
        return 0
    return 1 if rel.hdr and prefs.hdr else 2


def complete(rel: Release, title: Title, seasons=()) -> bool:
    """Все серии выбранных сезонов: «1-9 из 9», сезон уже весь вышел в сборнике или
    английский «S01» без серий у вышедшего целиком сезона."""
    if title.kind != library.TV:
        return True
    if rel.episodes and not rel.episodes[2] and len(rel.seasons) == 1:
        # «S01E01-E08»: сколько всего серий, знает только TMDb — дописываем.
        s = title.season(rel.seasons[0])
        if s is not None and s.episodes:
            rel.episodes = [rel.episodes[0], rel.episodes[1], s.episodes]
    if rel.episodes:
        first, last, total = rel.episodes
        return bool(total) and last >= total and first <= 1
    if rel.pack and rel.seasons:
        return (set(seasons or rel.seasons) <= set(rel.seasons)
                and all(title.season_complete(n) for n in rel.seasons))
    return bool(rel.seasons) and len(rel.seasons) > 1 and bool(seasons) and set(seasons) <= set(rel.seasons)


def _episodes_text(rel: Release) -> str:
    """«5 из 9» — с первой по пятую; «серия 9 из 9», «серии 6–9», «серия 7» — с середины."""
    first, last, total = rel.episodes
    if first <= 1 and total:
        return f"{last} из {total}"
    text = f"серия {last}" if first >= last else f"серии {first}–{last}"
    return text + (f" из {total}" if total else "")


def rank(releases: list[Release], title: Title, prefs: Prefs, seasons=()) -> list[Release]:
    """Лучшие сверху: с русскими дорожками, все серии, качество повыше, картинка
    под галки, любимая озвучка, сиды. Фильтры (`picture_ok`, качество) — до вызова."""
    for r in releases:
        full = complete(r, title, seasons)
        r.score = (0 if russian(r) else 1, 0 if full else 1, -quality_of(r),
                   _picture_rank(r, prefs), _voice_rank(r, prefs.voices),
                   0 if r.seeds >= 1 else 1, -r.seeds)
        r.notes = []
        if not russian(r):
            r.notes.append("английская")
        if r.seeds < LOW_SEEDS:
            r.notes.append("мало сидов" if r.seeds else "нет сидов")
        if r.dv_only:
            r.notes.append("только Dolby Vision")
        if title.kind == library.TV and seasons and r.seasons and not set(r.seasons) <= set(seasons):
            r.notes.append("сборник: сезоны " + _ranges(r.seasons))
        if title.kind == library.TV and r.episodes and not full:
            r.notes.append("не все серии: " + _episodes_text(r))
        r.recommended = False
        r.why = _why(r, prefs)
    releases.sort(key=lambda r: r.score)
    best = next((r for r in releases if r.seeds >= 1), None)
    if best is not None:
        best.recommended = True
    return releases


def _ranges(nums) -> str:
    nums = sorted(set(nums))
    return f"{nums[0]}–{nums[-1]}" if len(nums) > 1 else str(nums[0])


def _why(r: Release, prefs: Prefs) -> str:
    parts = [f"{r.quality}p" if r.quality > 480 else ("SD" if r.quality else "качество ?")]
    if r.dv:
        parts.append("Dolby Vision")
    elif r.hdr:
        parts.append("HDR")
    fav = [v for v in prefs.voices if has_voice(r, v)]
    if fav:
        parts.append(fav[0])
    elif r.voices:
        parts.append(r.voices[0] + (f" +{len(r.voices) - 1}" if len(r.voices) > 1 else ""))
    elif not russian(r):
        parts.append("англ.")
    if r.episodes:
        parts.append(f"{r.episodes[1]}/{r.episodes[2]}" if r.episodes[2] else _episodes_text(r))
    parts.append(f"{r.seeds} сид.")
    return " · ".join(parts)


def in_library(title: Title, roots) -> tuple[str, dict[int, int]]:
    """Тайтл в медиатеке: папка и сколько серий каждого сезона уже на диске.

    Папка ищется по номеру TMDb в .nfo тайтла — имя папки могло быть любым.
    У фильма словарь пустой: важен сам факт папки.
    """
    name = "tvshow.nfo" if title.kind == library.TV else "movie.nfo"
    for root in roots:
        try:
            dirs = [d for d in Path(root).iterdir() if d.is_dir()]
        except OSError:
            continue
        for d in dirs:
            if nfo.read_tmdb_id(d / name) == title.tmdb_id:
                if title.kind != library.TV:
                    return str(d), {}
                return str(d), library.scan_folder(d).season_sizes()
    return "", {}


def tracker_search_links(names) -> list[list[str]]:
    """Ничего не нашлось — поиск на трекерах руками, с подставленным названием."""
    name = next((n for n in names if n), "")
    q = urllib.parse.quote(name)
    return [["rutor", f"http://rutor.info/search/0/0/000/0/{q}"],
            ["rutracker", f"https://rutracker.org/forum/tracker.php?nm={q}"]]
