"""Поиск раздач: все названия тайтла из TMDb → источники → склейка → рекомендация.

- Названия — все, под которыми трекеры могут знать тайтл: русское, английское,
  оригинальное, альтернативные (дорама «Самый богатый бедняк» на трекерах —
  «Без труда не вытащишь рыбку из пруда»).
- Источники — список в `search_sources.json` (каталог настроек): `jacred`
  (jac.red или свой JacRed) и `torznab` (Jackett, Prowlarr). Не код: репозиторий
  публичный, а добавить источник — дописать строку, без пересборки. Плюс Knaben
  (The Pirate Bay, 1337x, Nyaa) — английские раздачи, под галкой в поиске.
- Одна и та же раздача с разных трекеров склеивается по infohash: трекеры
  перечисляются, сиды берутся наибольшие (рой у неё общий), трекеры из всех
  magnet-ссылок — в одну. Те же файлы под другим хешем (двойник: тот же размер и то же
  содержание — сезоны, серии, качество, озвучка) —
  в ту же карточку: ссылки на сайты списком, «Скачать» одна (`group`).
- Живы ли ссылки — у самих трекеров (`swarm.py`): мёртвая (трекер хеша не знает,
  сидов нет) уходит вниз, а в карточке с двойниками главной становится живая.
- Год: у сериала годится и год начала, и год выбранного сезона (±1) — трекеры
  пишут «(2026) Сезон 2» у сериала 2024-го.
- Рекомендация: русские дорожки → все серии → качество → больше серий (у неполных) →
  любимая озвучка → источник (Remux → BDRip → WEB-DL → WEBRip) → HEVC/AV1 → сиды.
  Мало сидов — не повод убирать из списка: «для коллекции подожду неделю».
- Сезоны — как у трекеров (`numbering.py`): у аниме, которое TMDb держит одним
  сезоном со сквозной нумерацией, это деление из episode groups.

Только stdlib.
"""
from __future__ import annotations

import base64
import html
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import core
import library
import metadata
import net
import nfo
import numbering
import paths
import swarm
import tmdb

_SOURCES_NAME = "search_sources.json"
_PREFS_NAME = "search_settings.json"
DEFAULT_SOURCES = [{"type": "jacred", "url": "https://jac.red"}]
KNABEN = {"type": "knaben", "url": "https://api.knaben.org/v1"}
QUALITIES = (2160, 1080, 720, 480)
QUALITY_NAMES = {2160: "4K", 1080: "1080p", 720: "720p", 480: "SD"}
HEVC, AVC, AV1 = "HEVC", "AVC", "AV1"
CODECS = (HEVC, AVC, AV1)
# Битрейт при том же качестве относительно AVC: HEVC — примерно 65% (на практике
# экономия 30–40%, «вдвое» — рекламная цифра), AV1 — примерно половина. Для сравнения
# размеров раздач с разными кодеками и для рекомендации: при равном источнике
# экономный кодек — первым (старые приставки AV1 не тянут, новые — умеют).
CODEC_FACTOR = {HEVC: 0.65, AV1: 0.5}

# Источник раздачи — по качеству он важнее кодека: пережатие не добавляет деталей.
# Порядок проверки (не качества!) важен: «BDRemux» — Remux, «WEB-DLRip» — пережатый
# WEB-DL, то есть WEBRip. Ранг от лучшего к худшему — `SOURCE_RANK`.
REMUX, BDRIP, WEBDL, WEBRIP, HDTV, DVDRIP, CAMRIP = (
    "Remux", "BDRip", "WEB-DL", "WEBRip", "HDTV", "DVDRip", "CAMRip")
_SOURCES = [
    (REMUX, re.compile(r"remux", re.I)),
    (BDRIP, re.compile(r"\b(?:bd|br)-?rip|\bblu-?ray\b", re.I)),
    (WEBRIP, re.compile(r"\bweb-?(?:dl-?)?rip|\bhdrip\b", re.I)),
    (WEBDL, re.compile(r"\bweb-?dl\b", re.I)),
    (HDTV, re.compile(r"\bhdtv(?:rip)?\b|\bsat-?rip\b|\btvrip\b|\biptv", re.I)),
    (DVDRIP, re.compile(r"\bdvd(?:rip|5|9)?\b", re.I)),
    (CAMRIP, re.compile(r"\bcam(?:rip)?\b|\btelesync\b|\bts\b", re.I)),
]
SOURCE_RANK = {name: i for i, name in enumerate((REMUX, BDRIP, WEBDL, WEBRIP, HDTV, DVDRIP, CAMRIP))}
_UNKNOWN_SOURCE_RANK = SOURCE_RANK[WEBRIP]     # не указан — не лучше пережатого
# Теги <source> tinyMediaManager в .nfo.
_TMM_SOURCES = {"BLURAY": BDRIP, "UHD_BLURAY": BDRIP, "WEB_DL": WEBDL, "WEBDL": WEBDL,
                "WEBRIP": WEBRIP, "HDTV": HDTV, "TV": HDTV, "DVD": DVDRIP}


def source_of(text: str) -> str:
    """Источник по названию раздачи или имени файла: Remux, BDRip, WEB-DL… «» — не указан."""
    return next((name for name, rx in _SOURCES if rx.search(text or "")), "")


def avc_equivalent(size: float, codec: str) -> float:
    """Размер, пересчитанный на AVC: так сравниваются раздачи с разными кодеками.
    Кодек не указан — считаем AVC: русские трекеры не пишут его, когда он обычный."""
    return size / CODEC_FACTOR.get(codec, 1.0)


def codec_of(text: str) -> str:
    """Кодек видео по названию раздачи или по ответу mkvmerge: HEVC, AVC, AV1; «» — не указан."""
    c = _CODEC.search(text or "")
    if not c:
        return ""
    return HEVC if c.group(1) else AVC if c.group(2) else AV1
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
    english: bool = False               # искать и показывать английские раздачи (Knaben)


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
class Special:
    """Спецвыпуск сериала (сезон 0 TMDb): OVA, фильм, мини-серия, «за кадром»."""
    number: int
    name: str
    year: int | None
    runtime: int = 0                    # минут; 0 — TMDb не знает


@dataclass
class Title:
    kind: str
    tmdb_id: int
    title: str                          # русское (язык описаний)
    year: int | None
    names: list[str] = field(default_factory=list)   # все названия для поиска
    romaji: list[str] = field(default_factory=list)  # из них — запись японского латиницей
    original: str = ""
    original_language: str = ""         # язык оригинала, код TMDb: «ja», «fr»
    seasons: list[Season] = field(default_factory=list)
    status: str = ""                    # у сериала: «Returning Series», «Ended»
    last_aired: tuple[int, int] = (0, 0)     # (сезон, серия) последней вышедшей
    next_air: tuple[int, int, str] = (0, 0, "")  # следующая: (сезон, серия, дата)
    # Сезоны выше, last_aired и next_air — в нумерации трекеров; это — её связь с TMDb.
    numbering: numbering.Numbering = field(default_factory=numbering.Numbering)
    runtime: int = 0                    # минут: фильм или серия — для битрейта раздач
    specials: list[Special] = field(default_factory=list)

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
              original_language=str(d.get("original_language") or ""),
              year=_year(d.get("release_date") or d.get("first_air_date")),
              status=d.get("status") or "")
    names = [name, original]
    for tr in ((d.get("translations") or {}).get("translations") or []):
        if tr.get("iso_639_1") in ("en", "ru"):
            data = tr.get("data") or {}
            names.append(data.get("title") or data.get("name") or "")
    # Альтернативные — российские и запись оригинала латиницей («Kusuriya no
    # Hitorigoto» — так аниме пишут почти все трекеры, а Nyaa — только так).
    # Американские бывают рабочими названиями («Durango 60623»), по которым
    # трекеры тайтл не знают. Ромадзи — вперёд: запросов по названиям немного.
    alt = d.get("alternative_titles") or {}
    alts = alt.get("titles") or alt.get("results") or []
    t.romaji = [str(a.get("title") or "").strip() for a in alts
                if str(a.get("type") or "").casefold() == "romaji" and a.get("title")]
    names += t.romaji
    names += [a.get("title") or "" for a in alts if a.get("iso_3166_1") == "RU"]
    seen = set()
    for n in names:
        key = n.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            t.names.append(n.strip())
    if kind != library.TV:
        t.runtime = int(d.get("runtime") or 0)
        return t
    t.runtime = int(next(iter(d.get("episode_run_time") or []), 0)
                    or (d.get("last_episode_to_air") or {}).get("runtime") or 0)
    t.specials = _specials(d.get("season/0") or {}, tmdb_id, api_key)
    nb = t.numbering = numbering.load(d, tmdb_id, api_key)
    t.seasons = [Season(s.season, s.count, s.year) for s in nb.spans]
    last, nxt = d.get("last_episode_to_air") or {}, d.get("next_episode_to_air") or {}
    if last.get("season_number"):
        t.last_aired = nb.from_tmdb(int(last["season_number"]), int(last.get("episode_number") or 0))
    if nxt.get("season_number"):
        t.next_air = (*nb.from_tmdb(int(nxt["season_number"]), int(nxt.get("episode_number") or 0)),
                      str(nxt.get("air_date") or ""))
    return t


def _specials(season0: dict, tmdb_id: int, api_key: str) -> list[Special]:
    """Спецвыпуски из сезона 0 (он приходит вместе с карточкой). Русских названий у TMDb
    почти нет — вместо них заглушки «Эпизод 1»: тогда английское (ещё один запрос)."""
    episodes = season0.get("episodes") or []
    if not episodes:
        return []
    english = {}
    if any(not metadata.real_title(str(e.get("name") or "")) for e in episodes):
        try:
            english = {e.get("episode_number"): str(e.get("name") or "")
                       for e in tmdb.season(tmdb_id, 0, api_key).get("episodes") or []}
        except net.NetError:
            pass
    return [Special(int(e.get("episode_number") or 0),
                    metadata.real_title(str(e.get("name") or ""),
                                        english.get(e.get("episode_number"), "")),
                    _year(e.get("air_date")), int(e.get("runtime") or 0))
            for e in episodes]


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
    seeds_at: str = ""                  # когда источник видел столько сидов (у склеенной — свой)
    quality: int = 0
    hdr: bool = False
    dv: bool = False
    dv_only: bool = False
    codec: str = ""                     # HEVC / AVC — из названия; "" — не указан
    source: str = ""                    # Remux / BDRip / WEB-DL / WEBRip… — из названия
    voices: list[str] = field(default_factory=list)
    seasons: list[int] = field(default_factory=list)
    year: int | None = None
    types: list[str] = field(default_factory=list)
    trackers: list[list[str]] = field(default_factory=list)   # [[трекер, страница], …]
    episodes: list[int] = field(default_factory=list)          # [первая, последняя, всего]
    pack: bool = False                  # «S01» без серий — весь сезон (английская запись)
    stale: bool = False                 # magnet от прежней версии раздачи (см. group)
    dead: bool = False                  # трекер хеша не знает, сидов нет (см. check_alive)
    alive: bool = False                 # жива наверняка: сиды сейчас или rutracker её знает
    seeds_live: bool = False            # сиды — у трекеров прямо сейчас, а не из памяти источника
    rutracker_peers: int | None = None  # участников у rutracker сейчас (30 — «30+»); None — не знаем
    site_seeds: dict = field(default_factory=dict)   # трекер → [сиды, когда, сейчас ли]: у каждого свои
    alts: list[dict] = field(default_factory=list)   # те же файлы по другим хешам: [{hash, magnet, dead}]
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
# Кодек видео: HEVC (x265), AVC (x264), AV1 — см. CODEC_FACTOR.
_CODEC = re.compile(r"\b(hevc|h\.?265|x\.?265)\b|\b(avc|h\.?264|x\.?264)\b|\b(av1)\b", re.I)
_QUALITY = re.compile(r"\b(2160|1080|720|480)[pi]\b|\b(4k|uhd)\b", re.I)
_SEASON_RANGE = re.compile(r"(?:сезон[ыа]?|seasons?)\s*[:.]?\s*(\d{1,2})\s*[-–]\s*(\d{1,2})"
                           r"|\[?s(\d{1,2})\s*[-–]\s*s?(\d{1,2})\]?", re.I)
# Английская запись: «S01», «S01E07», «S01E01-E08», «Season 1».
_SCENE = re.compile(r"\bs(\d{1,2})(?:[ .]?e(\d{1,3})(?:-?e?(\d{1,3}))?)?\b", re.I)
_SEASON_WORD = re.compile(r"\bseason[ .]?(\d{1,2})\b", re.I)
# Аниме: «Записки аптекаря [ТВ-2]», «(TV-3, часть 1)», «Kusuriya no Hitorigoto 2nd Season».
# Только вплотную или через дефис: «TV 24 эп.» — число серий, а не сезон.
_TV_PART = re.compile(r"\b(?:тв|tv)-?(\d{1,2})\b|\b(\d{1,2})(?:st|nd|rd|th)[\s.]+season\b", re.I)
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_CYRILLIC = re.compile(r"[а-яё]", re.I)
_UKRAINIAN = re.compile(r"[іїєґ]", re.I)
_LATIN = re.compile(r"[a-z]", re.I)
_RUS_TAG = re.compile(r"\brus\b", re.I)


_TOPIC = re.compile(r"[?&](?:t|id)=(\d+)|/torrent/(\d+)")


def page_key(url: str) -> str:
    """Страница раздачи на трекере → «трекер:номер темы». У rutor в адресе ещё и
    название («/torrent/959050/…-01x01-22-iz-24»), оно меняется с каждой новой
    серией, а rutracker отдаётся то с .org, то с .net — тема при этом та же."""
    host = (urllib.parse.urlparse(url).hostname or "").removeprefix("www.")
    m = _TOPIC.search(url)
    if not host or not m:
        return url
    return f"{host.split('.')[0]}:{m.group(1) or m.group(2)}"


def infohash(magnet: str) -> str:
    """Хеш из magnet — всегда 40 hex-знаков, как у qBittorrent (бывает и base32)."""
    m = _BTIH.search(magnet or "")
    if not m:
        return ""
    h = m.group(1)
    return base64.b32decode(h.upper()).hex() if len(h) == 32 else h.lower()


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
    out["codec"] = codec_of(text)
    out["source"] = source_of(text)
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
    elif m := _TV_PART.search(text or ""):
        out["seasons"] = [int(m.group(1) or m.group(2))]
    return out


def russian(rel: Release) -> bool:
    """Есть ли русские дорожки — насколько видно по озвучкам и названию раздачи.
    Кириллица с украинскими буквами («Монолог травниці … Ukr/Jap» с toloka) — не
    русская: такие раздачи приходят по ромадзи и английскому названию."""
    if rel.voices or _RUS_TAG.search(rel.title):
        return True
    return bool(_CYRILLIC.search(rel.title)) and not _UKRAINIAN.search(rel.title)


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
        dv=parsed["dv"], dv_only=parsed["dv_only"], codec=parsed["codec"],
        source=parsed["source"], voices=[str(v) for v in item.get("voices") or []],
        seasons=[int(s) for s in item.get("seasons") or [] if str(s).isdigit()]
        or parsed.get("seasons", []),
        # Год источник знает не всегда («relased»: 0 у выпусков телепередачи) — тогда из
        # названия: «… (Паутина любви) / 2025 / РУ», «(24.05.2026)».
        year=(int(item["relased"]) if str(item.get("relased") or "").isdigit()
              else int(m.group(1)) if (m := _YEAR.search(title)) else None),
        types=[str(t) for t in item.get("types") or []],
        trackers=[[str(item.get("tracker") or "?"), str(item.get("url") or "")]],
        episodes=parsed.get("episodes", []),
    )
    return rel


JACRED_QUERIES = 4


def _jacred_key(name: str) -> str:
    """Название так, как его сравнивает jac.red: латиница, кириллица и цифры, «ё» — «е»,
    остальное он выбрасывает (от «Shōgun» остаётся «shgun»)."""
    return re.sub(r"[^0-9a-zа-я]", "", name.casefold().replace("ё", "е"))


def _foreign_script(name: str) -> bool:
    """Есть буквы не латиницей и не кириллицей (японские, корейские). jac.red их
    выбросит: от «Re:ゼロから始める異世界生活» останется «re», а такой запрос перебирает
    всю его базу и падает (504). Диакритика — не в счёт: «ō» — это «o» со знаком."""
    return bool(re.search(r"[^\W\d_a-zа-яё]", _no_accents(name), re.I))


def _no_accents(name: str) -> str:
    """Латиница без диакритики: «Pokémon» → «Pokemon». Кириллицу не трогает: «й» — не «и»."""
    out = []
    for c in name:
        plain = "".join(x for x in unicodedata.normalize("NFKD", c) if not unicodedata.combining(x))
        out.append(plain if plain.isascii() else c)
    return "".join(out)


def _pairs(names: list[str]) -> list[dict]:
    """Точный поиск — по два названия за запрос: `search` и `altname`, найдёт по любому."""
    return [{"search": a, "altname": b, "exact": "true"}
            for a, b in zip(names[::2], names[1::2] + [""])]


def _jacred_queries(title: Title) -> list[dict]:
    """Запросы к jac.red по названиям тайтла.

    По очереди: русское, английское, ромадзи, российские альтернативные — качаем с русской
    озвучкой, а русские трекеры пишут тайтл так; оригинал на другом языке (французский,
    испанский) — последним: раздачи обычно пишут «Русское / Оригинал», но русское
    название TMDb бывает не тем, что у трекеров («Бюро» — а раздачи «Бюро легенд / Le
    Bureau des légendes»). Японскими и корейскими буквами jac.red не ищет вовсе
    (`_foreign_script`). Название с диакритикой — ещё и без неё: «Pokémon» jac.red
    превращает в «pokmon» и находит не то, что по «Pokemon».

    Точный поиск берёт два названия за запрос (`_pairs`), так что разных названий
    помещается вдвое больше: у аниме их бывает десяток, и каждое находит свою часть раздач
    (kinozal пишет «Re: Жизнь в альтернативном мире с нуля», rutracker — «Re:Zero — жизнь
    с нуля в другом мире»). Ромадзи — неточным поиском: трекеры дописывают к нему сезон
    («… Isekai Seikatsu 4», «… 2nd Season»), и точный такие раздачи не находит. jac.red
    отвечает по одному запросу за раз (два разом — 429), каждый — около секунды: запросов
    не больше `JACRED_QUERIES`.
    """
    native = title.original.strip() if title.original_language not in ("", "ru", "en") else ""
    if _jacred_key(native) == _jacred_key(title.title):
        native = ""         # русского названия у TMDb нет — оригинал и есть главное
    ordered = [n for n in [title.title, title.original, *title.names]
               if (n or "").strip() and n.strip() != native] + [native]
    names, seen = [], set()
    for n in ordered:
        for v in dict.fromkeys((n.strip(), _no_accents(n.strip()))):
            key = _jacred_key(v)
            if key and key not in seen and not _foreign_script(v):
                seen.add(key)
                names.append(v)
    romaji = {_jacred_key(n) for n in title.romaji}
    wide = [n for n in names if _jacred_key(n) in romaji][:1]
    queries = _pairs([n for n in names if n not in wide])
    # Ромадзи — вторым: первый запрос (русское и английское) важнее всего.
    queries[1:1] = [{"search": n} for n in wide]
    return queries[:JACRED_QUERIES]


def _jacred(source: dict, title: Title, log=None) -> list[Release]:
    """Раздачи из jac.red. Запрос, на который он не ответил (504 бывает и на отдельные
    названия), — в журнал, остальные всё равно в деле; не ответил ни на один — ошибка
    источника."""
    base = str(source["url"]).rstrip("/")
    found: dict[tuple, Release] = {}
    failed = []
    queries = _jacred_queries(title)
    for q in queries:
        params = {k: v for k, v in q.items() if v}
        if source.get("apikey"):
            params["apikey"] = source["apikey"]
        try:
            data = net.get_json(f"{base}/api/v1.0/torrents?{urllib.parse.urlencode(params)}",
                                timeout=30) or []
        except net.NetError as e:
            failed.append((q, e))
            continue
        for item in data if isinstance(data, list) else []:
            rel = _from_jacred(item)
            if rel is not None and not _other_title(item, title):
                found.setdefault((rel.trackers[0][0], rel.trackers[0][1], rel.hash), rel)
    if failed and len(failed) == len(queries):
        raise failed[0][1]
    for q, e in failed:
        if log:
            log(f"⚠ Поиск: {base}: не ответил по «{q['search']}»: {e}")
    return list(found.values())


def _plain(text: str) -> str:
    """Для сравнения названий: без регистра, диакритики («Shōgun» — «Shogun») и знаков."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold().replace("ё", "е")
    return " " + " ".join(re.findall(r"\w+", text)) + " "


def _other_title(item: dict, title: Title) -> bool:
    """jac.red ищет по русскому названию, а под ним бывают и чужие тайтлы: «Настоящий
    детектив» — ещё и русская телепередача (выпуски без года и сезона — фильтры её не
    отсеют), «Бумажный дом» — и турецкий сериал. Раздача зарубежного тайтла называет и
    оригинал — в названии или в `originalname` jac.red; не называет ни одного его
    латинского названия — чужая. Тайтлы с оригиналом не латиницей (русские, аниме) так
    не проверяются: их раздачи латинского названия могут и не писать."""
    if not _LATIN.search(title.original) or _CYRILLIC.search(title.original):
        return False
    names = {_plain(n) for n in [title.original] + title.names
             if _LATIN.search(n) and not _CYRILLIC.search(n)}
    text = _plain(f"{item.get('title') or ''} {item.get('originalname') or ''}")
    return not any(n in text for n in names if n.strip())


_TZ = "{http://torznab.com/schemas/2015/feed}"


def _torznab(source: dict, title: Title, log=None) -> list[Release]:
    base = str(source["url"]).rstrip("/")
    out = []
    for name in title.names[:4]:
        params = {"t": "search", "q": name}
        if source.get("apikey"):
            params["apikey"] = source["apikey"]
        try:
            raw = net.get_bytes(f"{base}?{urllib.parse.urlencode(params)}", timeout=40)
            root = ET.fromstring(raw)
        except (net.NetError, ET.ParseError) as e:
            if log:
                log(f"⚠ Поиск: {base}: не ответил по «{name}»: {e}")
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
                codec=parsed["codec"], source=parsed["source"], seasons=parsed.get("seasons", []), year=int(year.group(1)) if year else None,
                trackers=[[it.findtext("jackettindexer") or source.get("name") or "torznab",
                           it.findtext("comments") or ""]],
                episodes=parsed.get("episodes", []), pack=parsed.get("pack", False)))
    return out


_KNABEN_CATEGORY = {library.TV: 2_000_000, library.MOVIE: 3_000_000}


def _knaben(source: dict, title: Title, log=None) -> list[Release]:
    """Knaben — поиск по The Pirate Bay, 1337x, Nyaa и др.: в основном английские
    раздачи. Русские названия не спрашиваем: русские трекеры — у jac.red."""
    url = str(source.get("url") or KNABEN["url"])
    cat = _KNABEN_CATEGORY[title.kind]
    out = []
    # Латиница — вперёд: английское и ромадзи находят больше, чем иероглифы.
    names = sorted((n for n in title.names if not _CYRILLIC.search(n)),
                   key=lambda n: not _LATIN.search(n))[:2]
    failed = []
    for name in names:
        try:
            data = net.post_json(url, {"search_type": "100%", "search_field": "title",
                                       "query": name, "order_by": "seeders",
                                       "order_direction": "desc", "size": 300,
                                       "hide_unsafe": True, "hide_xxx": True}, timeout=30)
        except net.NetError as e:
            failed.append((name, e))
            continue
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
                codec=parsed["codec"], source=parsed["source"], seasons=parsed.get("seasons", []), year=int(year.group(1)) if year else None,
                trackers=[[str(it.get("tracker") or "Knaben"), str(it.get("details") or "")]],
                episodes=parsed.get("episodes", []), pack=parsed.get("pack", False)))
    if failed and len(failed) == len(names):
        raise failed[0][1]
    for name, e in failed:
        if log:
            log(f"⚠ Поиск: Knaben: не ответил по «{name}»: {e}")
    return out


TWIN_MIN_SIZE = 100 * 1024 ** 2


def _same_size(a: Release, b: Release) -> bool:
    """Трекеры пишут размер по-разному: одна и та же раздача — 32.01 ГБ на rutracker и
    31.67 на bitru. До 3% — тот же размер; прежняя версия обновлённой — заметно другой."""
    return not a.size or not b.size or abs(a.size - b.size) <= max(a.size, b.size) * 0.03


def group(items: list[Release], live=None) -> list[Release]:
    """Одна раздача — одна карточка: трекеры списком, сиды — наибольшие.

    Один хеш — один и тот же набор файлов, а значит, и размер. Разные размеры при
    одном хеше — трекер обновил раздачу (название и размер новые), а источник знает
    magnet прежней версии: так jac.red держит раздачи kinozal. Такую запись (более
    свежую) не склеиваем — она отдельной строкой, с пометкой: скачается старая версия.

    Потом — двойники (`_merge_twins`); `live(cards)` перед этим отмечает живые и
    мёртвые ссылки (`check_alive`), чтобы главной в карточке стала живая.
    """
    by_hash: dict[str, Release] = {}
    stale: list[Release] = []
    for r in items:
        if not r.site_seeds and r.trackers:
            r.site_seeds = {r.trackers[0][0]: [r.seeds, r.seeds_at, False]}
        g = by_hash.get(r.hash)
        if g is None:
            by_hash[r.hash] = r
            continue
        if not _same_size(g, r):
            old, new = (g, r) if g.updated <= r.updated else (r, g)
            new.stale = True
            by_hash[r.hash] = old
            stale.append(new)
            continue
        if r.seeds > g.seeds:
            # Дата — от той же записи, что и число: «57» с nnm-club из февраля не станет
            # августовским оттого, что bitru обходили в августе.
            g.seeds, g.peers, g.seeds_at = r.seeds, max(g.peers, r.peers), r.seeds_at
        for site, value in r.site_seeds.items():
            if value[0] > g.site_seeds.get(site, [-1])[0]:
                g.site_seeds[site] = list(value)
        # Хеш тот же — толпа одна: трекеры всех сайтов в одну ссылку, больше участников.
        g.magnet = swarm.with_trackers(g.magnet, swarm.trackers_of(r.magnet))
        _absorb(g, r)
    cards = list(by_hash.values()) + stale
    if live is not None:
        live(cards)
    return _merge_twins(cards)


def _absorb(g: Release, r: Release) -> None:
    """Описание раздачи из второй записи о ней — в первую."""
    # Трекер — один раз, даже если раздача лежит у него в двух темах.
    for tr in r.trackers:
        if all(tr[0] != have[0] for have in g.trackers):
            g.trackers.append(tr)
    g.updated = max(g.updated, r.updated)
    g.quality = g.quality or r.quality
    if r.quality > g.quality and r.quality in QUALITIES:
        g.quality = r.quality
    # «Только Dolby Vision» — пока ни один трекер не назвал рядом HDR10.
    if r.dv:
        g.dv_only = r.dv_only and (g.dv_only or not g.dv)
    g.hdr, g.dv = g.hdr or r.hdr, g.dv or r.dv
    g.codec = g.codec or r.codec
    g.source = g.source or r.source
    g.voices += [v for v in r.voices if v not in g.voices]
    g.seasons = sorted(set(g.seasons) | set(r.seasons))
    g.types += [t for t in r.types if t not in g.types]
    g.year = g.year or r.year
    g.episodes = g.episodes or r.episodes
    g.pack = g.pack or r.pack
    # Название — самое подробное: у rutracker в нём и сезон, и озвучки.
    if len(r.title) > len(g.title):
        g.title = r.title


def _same_content(a: Release, b: Release) -> bool:
    """Размер у jac.red — из надписи на странице трекера («3.90 GB»), до сотых гигабайта:
    так он совпадает и у разных раздач (1-й сезон HDRip и одна серия 4-го — обе по 3.90).
    Поэтому двойник — ещё и то же содержание: у сериала — те же сезоны (и серии, если
    они названы), у раздачи без сезона — фильм того же года. Серии без сезона в
    названии («Настоящий детектив (Паутина любви)» — русский сериал, по 0.50 ГБ каждая)
    не склеиваются: что внутри, не понять. Качество — то же, озвучка — хоть одна общая,
    если они известны у обеих."""
    if a.quality and b.quality and a.quality != b.quality:
        return False
    va, vb = {v.casefold() for v in a.voices}, {v.casefold() for v in b.voices}
    if va and vb and not va & vb:
        return False
    if a.seasons or b.seasons:
        if set(a.seasons) != set(b.seasons):
            return False
        return not (a.episodes or b.episodes) or a.episodes[:2] == b.episodes[:2]
    return (bool(a.year) and a.year == b.year
            and not set(a.types + b.types) & (TV_TYPES - MOVIE_TYPES))


def _merge_twins(cards: list[Release]) -> list[Release]:
    """Двойники — те же файлы под другим хешем: тот же размер и то же содержание
    (`_same_content`). Так бывает, когда раздачу перезалили, а у источника остался
    прежний magnet, или трекер завёл свой торрент на те же файлы.

    Это разные толпы — сложить их сиды BitTorrent не умеет, — но раздача для вас одна:
    одна карточка, ссылки на все сайты, одна кнопка. Главная — живая и с сидами
    побольше, остальные хеши — в `alts`: «Скачать» пускает их наперегонки
    (autopilot). Запись со старым magnet (`stale`) даёт карточке только ссылку на сайт."""
    buckets: dict[int, list[Release]] = {}
    order: list[Release] = []
    for c in cards:
        if c.size < TWIN_MIN_SIZE:
            order.append(c)
            continue
        bucket = buckets.setdefault(c.size, [])
        if not bucket:
            order.append(c)
        bucket.append(c)
    out = []
    for c in order:
        members = buckets.get(c.size) if c.size >= TWIN_MIN_SIZE else None
        if not members or len(members) == 1:
            out.append(c)
            continue
        # Один размер — ещё не одна раздача: делим по содержанию.
        sets: list[list[Release]] = []
        for m in members:
            twins = next((t for t in sets if all(_same_content(m, x) for x in t)), None)
            if twins is None:
                sets.append([m])
            else:
                twins.append(m)
        for twins in sets:
            twins.sort(key=lambda m: m.updated, reverse=True)
            twins.sort(key=lambda m: (m.stale, m.dead, not m.alive, -m.seeds))
            head = twins[0]
            known = {head.hash} | {a["hash"] for a in head.alts}
            for m in twins[1:]:
                _absorb(head, m)
                if m.stale:
                    continue
                for alt in [{"hash": m.hash, "magnet": m.magnet, "dead": m.dead}] + m.alts:
                    if alt["hash"] not in known:
                        known.add(alt["hash"])
                        head.alts.append(alt)
            out.append(head)
    return out


# Закрытые трекеры: открытые их раздачи почти не видят, зато многие из них — те же
# торренты, что на rutracker (kinozal и nnm-club перезаливают его раздачи).
_CLOSED = {"rutracker", "kinozal", "nnmclub"}


def check_alive(cards: list[Release], seasons=()) -> None:
    """Живы ли ссылки — у самих трекеров (`swarm`), а не по памяти источника.

    Сиды считаются по каждому сайту карточки (`site_seeds`), потом берётся наибольшее:
    - rutor — у его трекера opentor, nnm-club — у его трекера: их сегодняшнее число
      заменяет число источника, и большее, и меньшее;
    - rutracker числа сидов не говорит, но участников (до 30) — да, а сидов не бывает
      больше, чем участников: его число в источнике, большее их, — устаревшее;
    - kinozal и прочие — как в источнике;
    - открытые трекеры видят сидов всех сайтов сразу: их число, если оно больше.

    Мёртвая — когда каждый сайт карточки проверен и сказал «нет»: rutracker хеша не
    знает или участников ноль, nnm-club не знает или сидов ноль, у opentor ноль, — и
    открытые трекеры сидов не видят. Сайт, который не проверить (kinozal), мёртвой
    карточку не делает: там могут и раздавать.

    `seasons` — выбранные сезоны: их раздачи спрашиваются первыми (участников rutracker
    программа узнаёт не у всех, см. `swarm.RUTRACKER_COUNT`)."""
    wanted = set(seasons)
    order = sorted(cards, key=lambda c: (bool(wanted and c.seasons and not wanted & set(c.seasons)),
                                         -c.seeds))
    everything = [c.hash for c in order]
    rutor = [c.hash for c in order if any(tr[0] == "rutor" for tr in c.trackers)]
    closed = [c.hash for c in order
              if {tr[0] for tr in c.trackers} & _CLOSED or "t-ru.org" in c.magnet]
    # Все вопросы разом. Раздачи rutor — первыми: opentor спрашивается об одной пачке.
    with ThreadPoolExecutor(max_workers=3) as pool:
        scraping = pool.submit(swarm.scrape, rutor + everything)
        asking_nnm = pool.submit(swarm.nnm, everything)
        known = swarm.rutracker(closed)
        answers, on_nnm = scraping.result(), asking_nnm.result()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for c in cards:
        got, rt = answers.get(c.hash, {}), known.get(c.hash)
        n = max(got.values(), default=None)
        nn = on_nnm.get(c.hash) if c.hash in on_nnm else -1      # -1 — nnm-club не ответил
        sites = ({k: list(v) for k, v in c.site_seeds.items()}
                 or {(c.trackers[0][0] if c.trackers else ""): [c.seeds, c.seeds_at, False]})
        if swarm.RUTOR in got and any(tr[0] == "rutor" for tr in c.trackers):
            sites["rutor"] = [got[swarm.RUTOR], now, True]
        if nn is not None and nn >= 0:
            sites["nnmclub"] = [nn, now, True]
        c.rutracker_peers = peers = rt.peers if rt is not None and rt.known else None
        if "rutracker" in sites and peers is not None                 and peers < min(sites["rutracker"][0], swarm.RUTRACKER_PEERS):
            sites["rutracker"] = [peers, now, False]
        gone = {"rutracker": rt is not None and (not rt.known or peers == 0),
                "nnmclub": nn is None or nn == 0,
                "rutor": got.get(swarm.RUTOR) == 0}
        for site in sites:
            if gone.get(site):
                sites[site] = [0, now, True]          # трекер раздачи не знает — сидов там нет
        c.site_seeds = sites
        c.seeds, c.seeds_at, c.seeds_live = max(sites.values(), key=lambda v: (v[0], v[2]))
        if n is not None and n > c.seeds:
            c.seeds, c.seeds_at, c.seeds_live = n, now, True
        c.alive = bool(n) or bool(nn and nn > 0) or (rt is not None and rt.known and peers != 0)
        mine = {tr[0] for tr in c.trackers}
        c.dead = not c.alive and bool(mine) and all(gone.get(site, False) for site in mine)


def hashes(rel: Release) -> set[str]:
    """Все хеши карточки: главный и двойники."""
    return {rel.hash} | {a["hash"] for a in rel.alts}


def alternatives(rel: Release) -> list[dict]:
    """Двойники для гонки (autopilot): живые; если мертва вся карточка — все."""
    alive = [a for a in rel.alts if not a.get("dead")]
    return [{"hash": a["hash"], "magnet": a["magnet"]}
            for a in (rel.alts if rel.dead else alive)]


def find(title: Title, sources=None, log=None, seasons=()) -> list[Release]:
    """Все раздачи со всех источников, склеенные. Источник, который не ответил, — в журнал.

    Без явного списка — источники из файла и, если стоит галка «и английские»,
    Knaben. Источники опрашиваются разом — общее ожидание по самому медленному.
    `seasons` — выбранные сезоны: живость их раздач проверяется первой.
    """
    log = log or (lambda text: None)
    if sources is None:
        sources = load_sources() + ([KNABEN] if load_prefs().english else [])
    readers = {"jacred": _jacred, "torznab": _torznab, "knaben": _knaben}

    def read(source: dict) -> list[Release]:
        try:
            return readers[source["type"]](source, title, log)
        except (net.NetError, OSError) as e:
            log(f"⚠ Поиск: {source.get('url')}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:
        parts = list(pool.map(read, [s for s in sources if s.get("type") in readers]))
    items = [r for part in parts for r in part]
    for r in items:
        r.seeds_at = r.seeds_at or r.updated
    # Живость — только у подходящих тайтлу (тип, год): остальные jac.red нашёл по
    # созвучию, их не покажут.
    out = group(items, live=lambda cards: check_alive([c for c in cards if fits(c, title)], seasons))
    for r in out:
        count_in_season(r, title)
    return out


def count_in_season(rel: Release, title: Title) -> None:
    """Серии раздачи — в счёт одного сезона, чтобы сравнивать с медиатекой.

    Сборник «1-3 сезоны: 1-49 из 72» у сериала 24 + 24 + 12 — это первая серия
    третьего сезона: [1, 1, 12]. Сквозной номер в раздаче одного сезона аниме
    («3 сезон: 49-53») — тоже в счёт этого сезона: [1, 5, 12]. «Из 10» в сборнике,
    где последний сезон — 10 серий, уже про него и не трогается.
    """
    if title.kind != library.TV or len(rel.episodes) != 3 or not rel.seasons:
        return
    first, last, total = rel.episodes
    seasons = sorted(rel.seasons)
    tail = title.season(seasons[-1])
    if tail is None or not tail.episodes or (total and total <= tail.episodes):
        return
    nb = title.numbering if title.numbering.spans else numbering.regular(
        [(s.number, s.episodes, s.year) for s in title.seasons])
    if len(seasons) > 1:
        pos = nb.from_absolute(last, start=seasons[0])
        if pos is None:
            return
        start = nb.from_absolute(first, start=seasons[0])
        first = start[1] if start and start[0] == seasons[-1] else 1
        # Последнего сезона в сборнике ещё нет: «0 из 12».
        rel.episodes = [first, pos[1] if pos[0] == seasons[-1] else 0, tail.episodes]
        return
    if not nb.grouped:
        return
    before = sum(s.episodes for s in title.seasons if 0 < s.number < seasons[0])
    if before < first and last - before <= tail.episodes:
        rel.episodes = [first - before, last - before, tail.episodes]
        return
    if last <= tail.episodes:
        return              # «[03x01-04 из 24]» — серии внутри сезона, «из 24» — весь ТВ-3
    # Нумерация как в TMDb — один сезон подряд: «1 сезон: 1-52 из 60», «S01E50».
    # Номера больше самого сезона — сквозные: раздача уходит в те сезоны трекеров,
    # где эти серии.
    start, end = nb.from_absolute(first), nb.from_absolute(last)
    if start is None or end is None:
        return
    rel.seasons = list(range(start[0], end[0] + 1))
    rel.episodes = [start[1] if start[0] == end[0] else 1, end[1], title.season(end[0]).episodes]


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
            inner = {s.year for s in title.seasons if s.year and (not inside or s.number in inside)}
            # Сезон в названии не указан, а сезонов несколько — какой это, подскажет
            # только год: «[1-24] 2023» у аниме — первый сезон, а не третий (2026).
            years = inner if not rel.seasons and seasons and len(title.seasons) > 1 and inner \
                else years | inner
        if all(abs(rel.year - y) > 1 for y in years):
            return False
    if title.kind == library.TV and seasons and rel.seasons and not set(rel.seasons) & set(seasons):
        return False
    return True


DUB = "Дубляж"
# Дубляж любой студии — как его пишут трекеры: «ДБ (DEEP)» у kinozal, «| D, L |» у rutor,
# «Дублированный», «Dub». jac.red обычно и сам кладёт «Дубляж» в озвучки.
_DUB = re.compile(r"дубляж|дублир|\bдб\b|\bdub\b|\|\s*(?:[a-z]\s*,\s*)*d(?:\s*,\s*[a-z])*\s*\|", re.I)


RUS_VOICE = "Любая русская"
# Русский звук, а не только субтитры — для редких фильмов, где дубляжа может и не быть:
# озвучки из jac.red, сокращения трекеров (ДБ, ПМ, ЛМ, АП, MVO, «RUS(int)»), коды rutor
# «| D, P2, L1, A |». «Sub», «СТ» и «субтитры» — не озвучка.
_RUS_VOICE = re.compile(
    r"дубляж|дублир|многоголос|двухголос|одноголос|закадров|любительск|профессиональн|авторск"
    r"|\b(?:дб|пм|пд|мво|дво|лм|лд|лп|ло|ао|ап|mvo|dvo|avo|vo|dub)\b|rus\s*\(int\)", re.I)
_RUTOR_VOICES = re.compile(r"\|\s*(?:[DPLA]\d?\s*,\s*)*[DPLA]\d?\s*\|")


def has_voice(rel: Release, voice: str) -> bool:
    have = " ".join(rel.voices + [rel.title]).casefold().replace("ё", "е")
    if voice == DUB:
        return bool(_DUB.search(have))
    if voice == RUS_VOICE:
        return bool(rel.voices or _RUS_VOICE.search(have) or _RUTOR_VOICES.search(rel.title))
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
        if first <= 1 and len(rel.seasons) == 1:
            # Сезон аниме выходит частями: трекер пишет «1-12 из 24», а TMDb пока
            # знает 12 — все вышедшие серии сезона в раздаче есть.
            s = title.season(rel.seasons[0])
            if s is not None and s.episodes and last >= s.episodes:
                return True
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
        # После любимой озвучки — источник (Remux → BDRip → WEB-DL → WEBRip → HDTV),
        # и только при равном источнике HEVC/AV1: они мельче, но качества не добавляют.
        # Устаревший magnet и мёртвая ссылка — в самый низ: скачалась бы не та версия,
        # что в названии, или не скачалось бы ничего.
        # Среди неполных — где больше серий (у сборника — в его последнем сезоне):
        # пять серий третьего сезона лучше сборника на 48 ГБ с одной.
        count = r.episodes[1] - r.episodes[0] + 1 if r.episodes and not full else 0
        r.score = (1 if r.stale or r.dead else 0, 0 if russian(r) else 1, 0 if full else 1,
                   -quality_of(r), -count, _picture_rank(r, prefs), _voice_rank(r, prefs.voices),
                   SOURCE_RANK.get(r.source, _UNKNOWN_SOURCE_RANK),
                   0 if r.codec in CODEC_FACTOR else 1, 0 if r.seeds >= 1 else 1, -r.seeds)
        r.notes = []
        if r.stale:
            r.notes.append("старый magnet: скачается прежняя версия — качайте со страницы")
        elif r.dead:
            r.notes.append("мёртвая ссылка: трекер её больше не знает, сидов нет — "
                           "качайте со страницы")
        if not russian(r):
            r.notes.append("украинская" if _UKRAINIAN.search(r.title) else "английская")
        if r.seeds < LOW_SEEDS:
            r.notes.append("мало сидов" if r.seeds else "нет сидов")
        if r.dv_only:
            r.notes.append("только Dolby Vision")
        if title.kind == library.TV and seasons and r.seasons and not set(r.seasons) <= set(seasons):
            r.notes.append("сборник: сезоны " + _ranges(r.seasons))
        if title.kind == library.TV and r.episodes and not full:
            r.notes.append("не все серии: " + (f"сезон {max(r.seasons)} — " if len(r.seasons) > 1 else "")
                           + _episodes_text(r))
        r.recommended = False
        r.why = _why(r, prefs)
    releases.sort(key=lambda r: r.score)
    best = next((r for r in releases if r.seeds >= 1 and not r.stale and not r.dead), None)
    if best is not None:
        best.recommended = True
    return releases


def _ranges(nums) -> str:
    nums = sorted(set(nums))
    return f"{nums[0]}–{nums[-1]}" if len(nums) > 1 else str(nums[0])


def _why(r: Release, prefs: Prefs) -> str:
    parts = [f"{r.quality}p" if r.quality > 480 else ("SD" if r.quality else "качество ?")]
    if r.source:
        parts[0] += f" {r.source}"
    if r.codec in (HEVC, AV1):
        parts[0] += f" {r.codec}"
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
        text = f"{r.episodes[1]}/{r.episodes[2]}" if r.episodes[2] else _episodes_text(r)
        # У сборника серии посчитаны в его последнем сезоне (`count_in_season`).
        parts.append(f"сезон {max(r.seasons)}: {text}" if len(r.seasons) > 1 else text)
    parts.append(f"{r.seeds} сид.")
    return " · ".join(parts)


def in_library(title: Title, roots) -> tuple[str, dict[int, int]]:
    """Тайтл в медиатеке: папка и сколько серий каждого сезона уже на диске.

    Папка ищется по номеру TMDb в .nfo тайтла — имя папки могло быть любым.
    У фильма словарь пустой: важен сам факт папки.
    """
    folder = library_dir(title.kind, title.tmdb_id, roots)
    if not folder or title.kind != library.TV:
        return folder, {}
    have: dict[int, int] = {}
    for season, episode in library_episodes(Path(folder), title.numbering):
        have[season] = have.get(season, 0) + 1
    return folder, have


def tmdb_range(title: Title, season: int) -> str:
    """Сезон трекеров в нумерации TMDb: «серии 49–60» или «S01E49–E60»."""
    span = title.numbering.span(season)
    if span is None or not title.numbering.grouped:
        return ""
    last = span.first + span.count - 1
    if len(title.numbering.tmdb_seasons()) == 1:
        return f"серии {span.first}–{last}"
    return f"S{span.tmdb_season:02d}E{span.first:02d}–E{last:02d}"


def numbering_note(title: Title, folder: str = "") -> str:
    """Нумерация трекеров и TMDb разная — сказать прямо, чтобы не искать «сезон 3»
    в TMDb, где он часть первого, и знать, под какими номерами лягут серии."""
    nb = title.numbering
    if title.kind != library.TV or not nb.grouped:
        return ""
    tmdb_count = len(nb.tmdb_seasons())
    total = sum(s.count for s in nb.spans)
    where = (f"в TMDb один сезон из {total} серий подряд" if tmdb_count == 1
             else f"в TMDb сезонов {tmdb_count}")
    parts = ", ".join(f"{s.season} — {tmdb_range(title, s.season)}" for s in nb.spans)
    group = f" «{nb.source}»" if nb.source else ""
    text = (f"Нумерация разная: {where}, а по сезонам, как у раздач, серии делит группа "
            f"TMDb{group}: {parts}. Программа сверяет сама.")
    grouped_disk = numbering.library_grouped(nb, disk_episodes(folder) if folder else ())
    last = nb.spans[-1]
    tmdb_season, first = nb.to_tmdb(last.season, 1)
    text += (" В медиатеку — по сезонам трекеров (S02E01…)." if grouped_disk else
             f" В медиатеке — как в TMDb: сезон {last.season} ложится как "
             f"S{tmdb_season:02d}E{first:02d} и дальше.")
    return text


# --------------------------------------------------------------------------- #
# Что вышло: статус сериала, ход сезонов, русская озвучка, спецвыпуски, версии
# --------------------------------------------------------------------------- #

AIRED, RUNNING, ANNOUNCE = "aired", "running", "announce"


def season_state(title: Title, number: int) -> tuple[str, int]:
    """Сезон вышел целиком (AIRED), идёт (RUNNING) или ещё не начался (ANNOUNCE) —
    и сколько его серий уже вышло. Дат выхода TMDb не знает вовсе — «вышел», как раньше."""
    s = title.season(number)
    if s is None or not s.episodes:
        return ANNOUNCE, 0
    if title.last_aired == (0, 0) and not title.next_air[0]:
        return AIRED, s.episodes
    if title.season_complete(number):
        return AIRED, s.episodes
    aired = title.aired(number)
    return (RUNNING, aired) if aired else (ANNOUNCE, 0)


def short_date(date: str, today=None) -> str:
    """«2026-11-14» → «14.11» в этом году, «14.11.2027» — в другом."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date or "")
    if not m:
        return ""
    year = (today or datetime.now().date()).year
    return f"{m.group(3)}.{m.group(2)}" + (f".{m.group(1)}" if int(m.group(1)) != year else "")


def announce_date(title: Title, number: int, today=None) -> str:
    """Когда начнётся сезон — если TMDb уже знает дату его первой серии."""
    season, episode, date = title.next_air
    return short_date(date, today) if season == number and episode == 1 else ""


def status_text(title: Title, today=None) -> str:
    """Строка под названием сериала: завершён ли, когда следующая серия, анонсы."""
    if title.kind != library.TV:
        return ""
    if title.status == "Ended":
        return "Завершён — новых сезонов не будет"
    if title.status == "Canceled":
        return "Закрыт — новых сезонов не будет"
    season, episode, date = title.next_air
    when = short_date(date, today)
    if season and episode == 1:
        return f"Сезон {season} выходит {when}" if when else f"Сезон {season} анонсирован"
    if season:
        return (f"Идёт: следующая серия {when} (сезон {season}, серия {episode})" if when
                else f"Идёт: дальше сезон {season}, серия {episode}")
    later = next((s for s in title.seasons if season_state(title, s.number)[0] == ANNOUNCE), None)
    if later is not None:
        return f"Продлён: сезон {later.number}" + (f" ({later.year})" if later.year else "")             + " — дата пока неизвестна"
    if title.status in ("In Production", "Planned", "Pilot"):
        return "Готовится — дата выхода пока неизвестна"
    if title.status == "Returning Series":
        return "Не завершён — дат новых серий пока нет"
    return ""


def _reaches(rel: Release, title: Title, number: int) -> int:
    """До какой серии сезона дошла раздача; 0 — не понять. В сборнике сезоны до
    последнего — целиком (серии там посчитаны в последнем, `count_in_season`)."""
    seasons = sorted(rel.seasons)
    s = title.season(number)
    if number not in seasons or s is None:
        return 0
    if number != seasons[-1]:
        return s.episodes
    if rel.episodes:
        return rel.episodes[1]
    return (title.aired(number) or s.episodes) if complete(rel, title, [number]) else 0


def voice_lag(title: Title, releases, seasons) -> str:
    """Выбран идущий сезон (или вышедший, а русские раздачи не догнали) — сколько серий
    вышло в оригинале и докуда дошли русские раздачи, по студиям: ждать или качать."""
    if title.kind != library.TV or len(seasons) != 1:
        return ""
    number = seasons[0]
    state, aired = season_state(title, number)
    if state == ANNOUNCE:
        return ""
    total = title.season(number).episodes
    best: dict[str, int] = {}
    seen = False
    for r in releases:
        if r.dead or not russian(r) or number not in r.seasons or not fits(r, title, [number]):
            continue
        seen = True
        reach = _reaches(r, title, number)
        for v in r.voices or [""]:
            best[v] = max(best.get(v, 0), reach)
    top = max(best.values(), default=0)
    head = (f"Сезон {number} идёт: в оригинале вышло {aired} из {total}" if state == RUNNING
            else f"Сезон {number} вышел целиком ({total} сер.)")
    if not seen:
        return f"{head}, русских раздач с ним пока нет" if state == RUNNING else ""
    if not top or (state == AIRED and top >= total):
        return ""
    studios = sorted(((n, v) for v, n in best.items() if v and n), key=lambda x: -x[0])[:3]
    who = f" ({', '.join(f'{v} {n}' for n, v in studios)})" if studios else ""
    if top >= aired:
        return f"{head}, русские раздачи успевают: {top}{who}"
    return f"{head}, в русских раздачах — до {top}{who}"


SHORT_SPECIAL = 10      # минут: до стольких — мини-серии, гоняться за ними незачем


def specials_note(title: Title, have: int = 0) -> tuple[str, str]:
    """Спецвыпуски сериала → (строка, подсказка со списком). Строка — сколько их и какой
    длины: у аниме это часто мини-серии по 2–3 минуты (OVA-фильмы TMDb держит отдельными
    фильмами), у сериалов — «за кадром» и превью. Список с названиями — в подсказке."""
    if title.kind != library.TV or not title.specials:
        return "", ""
    text = f"Спецвыпуски в TMDb: {len(title.specials)}"
    times = sorted(s.runtime for s in title.specials if s.runtime)
    if times:
        span = f"{times[0]} мин" if times[0] == times[-1] else f"{times[0]}–{times[-1]} мин"
        text += f" ({'короткие, ' if times[-1] <= SHORT_SPECIAL else ''}{span})"
    if have:
        text += f", у вас: {have}"
    hint = "\n".join(f"{s.number}. {s.name or '—'}" + (f" ({s.year})" if s.year else "")
                     + (f", {s.runtime} мин" if s.runtime else "") for s in title.specials[:40])
    if len(title.specials) > 40:
        hint += f"\n… и ещё {len(title.specials) - 40}"
    return text, hint


# Версии фильма (или сериала: аниме «без цензуры») — по названию раздачи: TMDb знает их
# плохо (у «Царства небесного» режиссёрская там — строчка среди фестивалей).
VERSIONS = [
    ("режиссёрская", re.compile(r"режисс[её]рск|director[’'`]?s?[\s.]*cut", re.I)),
    ("театральная", re.compile(r"театральн|theatrical", re.I)),
    ("расширенная", re.compile(r"расширенн|удлин[её]нн|\bextended\b", re.I)),
    ("без цензуры", re.compile(r"без\s+цензур|uncensored|\bunrated\b|\buncut\b", re.I)),
    ("Final Cut", re.compile(r"final[\s.]*cut", re.I)),
    ("Redux", re.compile(r"\bredux\b", re.I)),
    ("IMAX", re.compile(r"\bimax\b", re.I)),
    ("Ultimate", re.compile(r"ultimate[\s.]*(?:cut|edition)", re.I)),
]


def versions_of(text: str) -> list[str]:
    """Версии, названные в раздаче: «режиссёрская», «театральная»… Ничего — обычная."""
    return [name for name, rx in VERSIONS if rx.search(text or "")]


@dataclass
class Owned:
    """Версия тайтла в медиатеке — с ней сравниваются раздачи."""
    size: int = 0                       # фильм — файл, сериал — все серии
    episodes: int = 1
    quality: int = 0                    # 2160 / 1080 / 720 / 480; 0 — не прочитать
    bitrate: float = 0.0                # Мбит/с, вместе со звуком
    codec: str = ""                     # HEVC / AVC / прочее, как у раздач
    source: str = ""                    # WEB-DL / BDRip… — по исходному имени в .nfo

    @property
    def per_episode(self) -> int:
        return self.size // max(self.episodes, 1)


def quality_of_width(width: int) -> int:
    """По ширине кадра, а не высоте: у широкоэкранных фильмов 1920×800 — это 1080p."""
    if not width:
        return 0
    return 2160 if width >= 3200 else 1080 if width >= 1800 else 720 if width >= 1200 else 480


def library_version(kind: str, folder: str, extras: str = "Extras", probe: bool = True) -> Owned | None:
    """Что лежит в медиатеке: фильм — самый большой видеофайл, сериал — все серии
    (спецвыпуски и Extras не в счёт). `probe` — читать качество и длительность
    (mkvmerge, у сериала — одна серия); без него — только размеры, это мгновенно."""
    if not folder:
        return None
    root = Path(folder)
    scan = library.scan_folder(root)
    videos = [v for v in scan.videos if extras not in v.relative_to(root).parts[:-1]]
    if kind == library.TV:
        videos = [v for v in videos if scan.season_of(v) != 0]
    if not videos:
        return None
    if kind == library.TV:
        owned = Owned(sum(scan.size_of(v) for v in videos),
                      sum(max(len(scan.episodes_of(v)[1]), 1) for v in videos))
    else:
        owned = Owned(max(scan.size_of(v) for v in videos))
    if probe:
        sample = max(videos, key=scan.size_of)
        width, duration, codec = core.video_info(sample)
        owned.quality = quality_of_width(width)
        owned.codec = codec_of(codec) or codec.split("/")[0]
        # Источник — по имени файла в раздаче, которое сохранено в .nfo (его пишут и
        # эта программа, и tinyMediaManager); у tmm есть ещё тег <source>.
        hint = next((p for p in (sample.with_suffix(".nfo"), root / "movie.nfo") if p.is_file()), None)
        if hint is not None:
            name, tag = nfo.read_source_hint(hint)
            owned.source = source_of(name) or _TMM_SOURCES.get(tag.upper(), "")
        if duration:
            owned.bitrate = round(scan.size_of(sample) * 8 / duration / 1e6, 1)
    return owned


def episode_count(rel: Release, title: Title) -> int:
    """Сколько серий в раздаче — чтобы сравнивать размер «на серию». 0 — неизвестно.
    У сборника серии посчитаны в последнем сезоне (`count_in_season`), прежние — целиком."""
    if title.kind != library.TV:
        return 1
    seasons = sorted(rel.seasons)
    counts = [title.season(s).episodes if title.season(s) else 0 for s in seasons]
    if rel.episodes:
        first, last, _ = rel.episodes
        return sum(counts[:-1]) + max(last - first + 1, 0)
    return sum(counts) if counts and all(counts) else 0


def bitrate(size: int, minutes: int) -> float:
    """Примерный битрейт раздачи, Мбит/с: размер на длительность из TMDb."""
    return round(size * 8 / (minutes * 60) / 1e6, 1) if size and minutes else 0.0


def library_dir(kind: str, tmdb_id: int, roots) -> str:
    """Папка тайтла в медиатеке — по номеру TMDb в его .nfo; нет — пустая строка.
    И на уровень глубже — в папках-подборках без своего .nfo («Super Collection»)."""
    name = "tvshow.nfo" if kind == library.TV else "movie.nfo"
    for root in roots:
        for d in _subdirs(Path(root)):
            if (d / name).is_file():
                if nfo.read_tmdb_id(d / name) == tmdb_id:
                    return str(d)
                continue
            for sub in _subdirs(d):
                if nfo.read_tmdb_id(sub / name) == tmdb_id:
                    return str(sub)
    return ""


def _subdirs(folder: Path) -> list[Path]:
    try:
        return [d for d in folder.iterdir() if d.is_dir()]
    except OSError:
        return []


def disk_episodes(folder) -> set[tuple[int, int]]:
    """Серии тайтла на диске — (сезон, серия), как названы файлы."""
    scan = library.scan_folder(folder)
    found = set()
    for v in scan.videos:
        season, numbers = scan.episodes_of(v)
        if season is not None:
            found.update((season, n) for n in numbers)
    return found


def library_episodes(folder: Path, nb: numbering.Numbering) -> set[tuple[int, int]]:
    """Серии тайтла на диске — (сезон, серия) в нумерации трекеров. Сквозные номера
    медиатеки (S01E50) пересчитываются в сезоны трекеров (3, 2)."""
    found = disk_episodes(folder)
    grouped_disk = numbering.library_grouped(nb, found)
    return {numbering.from_library(nb, s, n, grouped_disk) for s, n in found}


def tracker_search_links(names) -> list[list[str]]:
    """Ничего не нашлось — поиск на трекерах руками, с подставленным названием."""
    name = next((n for n in names if n), "")
    q = urllib.parse.quote(name)
    return [["rutor", f"http://rutor.info/search/0/0/000/0/{q}"],
            ["rutracker", f"https://rutracker.org/forum/tracker.php?nm={q}"]]
