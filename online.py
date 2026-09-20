"""Онлайн-источники готовых таймингов интро/титров/recap для .edl.

Два источника (см. EDL-заметки.md):
  - AniSkip (api.aniskip.com/v2) — аниме, ключ = MyAnimeList ID + номер серии;
    тайминги в секундах, типы op/ed/recap.
  - TheIntroDB (api.theintrodb.org/v3) — сериалы/фильмы, ключ = TMDb/IMDb ID;
    тайминги в миллисекундах, сегменты intro/credits/recap.
Плюс подбор ID: разбор соседних .nfo (tinyMediaManager/Kodi) и поиск MAL через Jikan.

Только stdlib (urllib + xml.etree) — без внешних зависимостей и без Tkinter, поэтому
модуль пригоден для headless-тестов. Форматы ответов сверены с живым API (2026-07).
"""
from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from edl import Segment
# Сетевой слой общий с клиентами метаданных (tmdb/fanart/omdb) — см. net.py.
# OnlineError оставлен именем-синонимом: его ловит app.py и проверяют тесты.
from net import NetError as OnlineError
from net import get_json as _get_json

ANISKIP_BASE = "https://api.aniskip.com/v2"
THEINTRODB_BASE = "https://api.theintrodb.org/v3"
JIKAN_BASE = "https://api.jikan.moe/v4"
TVMAZE_BASE = "https://api.tvmaze.com"


# --------------------------------------------------------------------------- #
# Результат
# --------------------------------------------------------------------------- #

@dataclass
class OnlineResult:
    intro: Segment | None = None
    outro: Segment | None = None
    recap: Segment | None = None
    source: str = ""      # "AniSkip" / "TheIntroDB"
    note: str = ""        # диагностика (нет в базе / ошибка) — для показа в таблице

    @property
    def any(self) -> bool:
        return bool(self.intro or self.outro or self.recap)


# --------------------------------------------------------------------------- #
# AniSkip
# --------------------------------------------------------------------------- #

# skipType AniSkip → наш сегмент (mixed-* — те же op/ed, но склеенные с соседним).
_ANISKIP_KIND = {"op": "intro", "mixed-op": "intro",
                 "ed": "outro", "mixed-ed": "outro",
                 "recap": "recap"}


def fetch_aniskip(mal_id, episode, episode_len_s=None) -> OnlineResult:
    """Тайминги op/ed/recap с AniSkip (в секундах). episode_len_s — длина серии
    (API требует параметр; 0 допустим). Нет данных → пустой результат с note."""
    length = int(round(episode_len_s)) if episode_len_s else 0
    q = urllib.parse.urlencode([("types", "op"), ("types", "ed"), ("types", "recap"),
                                ("episodeLength", length)])
    url = f"{ANISKIP_BASE}/skip-times/{mal_id}/{episode}?{q}"
    data = _get_json(url)
    res = OnlineResult(source="AniSkip")
    if not data or not data.get("found"):
        res.note = "нет в AniSkip"
        return res
    for item in data.get("results", []):
        kind = _ANISKIP_KIND.get(item.get("skipType", ""))
        iv = item.get("interval") or {}
        s, e = iv.get("startTime"), iv.get("endTime")
        if kind and s is not None and e is not None and float(e) > float(s):
            setattr(res, kind, Segment(float(s), float(e)))
    if not res.any:
        res.note = "нет в AniSkip"
    return res


# --------------------------------------------------------------------------- #
# TheIntroDB
# --------------------------------------------------------------------------- #

def _idb_seg(arr, duration_s=None) -> Segment | None:
    """Первый валидный сегмент из массива TheIntroDB (мс→сек). null end → до конца."""
    for it in (arr or []):
        s = it.get("start_ms")
        if s is None:
            continue
        start = s / 1000.0
        e = it.get("end_ms")
        end = (e / 1000.0) if e is not None else (duration_s if duration_s else None)
        if end is not None and end > start:
            return Segment(start, end)
    return None


def fetch_theintrodb(tmdb_id=None, imdb_id=None, season=None, episode=None,
                     duration_s=None) -> OnlineResult:
    """Тайминги intro/credits/recap с TheIntroDB (мс). Ключ — tmdb_id (приоритет)
    или imdb_id. Для фильмов season/episode можно не задавать."""
    params: list[tuple[str, object]] = []
    if tmdb_id:
        params.append(("tmdb_id", tmdb_id))
    elif imdb_id:
        params.append(("imdb_id", imdb_id))
    else:
        raise OnlineError("Нужен tmdb_id или imdb_id для TheIntroDB")
    if season is not None:
        params.append(("season", season))
    if episode is not None:
        params.append(("episode", episode))
    if duration_s:
        params.append(("duration_ms", int(round(duration_s * 1000))))
    url = f"{THEINTRODB_BASE}/media?{urllib.parse.urlencode(params)}"
    data = _get_json(url)
    res = OnlineResult(source="TheIntroDB")
    if not data:
        res.note = "нет в TheIntroDB"
        return res
    res.intro = _idb_seg(data.get("intro"))
    res.outro = _idb_seg(data.get("credits"), duration_s)
    res.recap = _idb_seg(data.get("recap"))
    if not res.any:
        res.note = "нет в TheIntroDB"
    return res


# --------------------------------------------------------------------------- #
# Подбор ID: Jikan (MAL) + разбор .nfo
# --------------------------------------------------------------------------- #

@dataclass
class MalCandidate:
    mal_id: int
    title: str
    episodes: int | None = None
    year: int | None = None

    def label(self) -> str:
        parts = [self.title or f"MAL {self.mal_id}"]
        if self.year:
            parts.append(f"({self.year})")
        if self.episodes:
            parts.append(f"— {self.episodes} эп.")
        return " ".join(parts) + f"  [MAL {self.mal_id}]"


def search_mal(title: str, limit: int = 5) -> list[MalCandidate]:
    """Поиск аниме на MyAnimeList через Jikan → кандидаты (mal_id, title, …)."""
    if not (title or "").strip():
        return []
    url = f"{JIKAN_BASE}/anime?{urllib.parse.urlencode({'q': title, 'limit': limit})}"
    data = _get_json(url)
    out: list[MalCandidate] = []
    for it in (data or {}).get("data", []):
        year = it.get("year")
        if not year:
            year = (((it.get("aired") or {}).get("prop") or {}).get("from") or {}).get("year")
        out.append(MalCandidate(
            mal_id=it.get("mal_id"),
            title=(it.get("title_english") or it.get("title") or ""),
            episodes=it.get("episodes"),
            year=year,
        ))
    return out


# tinyMediaManager/Kodi .nfo хранит внешние ID как <uniqueid type="...">value</uniqueid>.
_NFO_ID_TYPES = {"tmdb": "tmdb", "themoviedb": "tmdb",
                 "imdb": "imdb",
                 "mal": "mal", "myanimelist": "mal",
                 "anidb": "anidb", "tvdb": "tvdb"}


def _iter_uniqueids(text: str):
    """(type, value) из всех <uniqueid> в .nfo. Терпимо к битому/составному XML."""
    try:
        root = ET.fromstring(text)
        for el in root.iter("uniqueid"):
            yield el.get("type", ""), (el.text or "")
        return
    except ET.ParseError:
        pass
    for m in re.finditer(r'<uniqueid[^>]*type="([^"]+)"[^>]*>([^<]*)</uniqueid>', text, re.I):
        yield m.group(1), m.group(2)


def read_nfo_ids(video_path) -> dict:
    """ID СЕРИАЛА из `tvshow.nfo` (в папке серии и на уровень выше).

    Важно: берём именно show-level `tvshow.nfo`, а НЕ эпизодный `<video>.nfo` —
    у последнего `<uniqueid type="tmdb/imdb">` относится к самой серии, а не к
    сериалу, и для матчинга в онлайн-базах (нужен ID сериала) он неверный.
    Возвращает {'tmdb':..,'imdb':..,'tvdb':..,'mal':..,'anidb':..}."""
    ids: dict[str, str] = {}
    p = Path(video_path)
    for nfo in (p.parent / "tvshow.nfo", p.parent.parent / "tvshow.nfo"):
        if not nfo.is_file():
            continue
        try:
            text = nfo.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for uid_type, val in _iter_uniqueids(text):
            key = _NFO_ID_TYPES.get((uid_type or "").lower())
            if key and key not in ids and (val or "").strip():
                ids[key] = val.strip()
    return ids


def search_tvmaze_imdb(title: str) -> str | None:
    """Keyless-поиск сериала по названию через TVmaze → IMDb ID (tt…) или None.

    TheIntroDB принимает imdb_id и сам резолвит его в TMDb, поэтому это даёт
    «поиск по названию» для TheIntroDB без ключа TMDb API."""
    if not (title or "").strip():
        return None
    url = f"{TVMAZE_BASE}/singlesearch/shows?{urllib.parse.urlencode({'q': title})}"
    data = _get_json(url)
    imdb = ((data or {}).get("externals") or {}).get("imdb")
    return imdb or None


def clean_show_title(folder_name: str) -> str:
    """Название тайтла из имени папки для поиска: убирает год, «Season NN», скобки."""
    name = re.sub(r"\(\d{4}\)", "", folder_name)
    name = re.sub(r"(?i)\bseason\s*\d+\b", "", name)
    name = re.sub(r"(?i)\bs\d{1,2}\b", "", name)
    name = re.sub(r"[._]+", " ", name)
    return re.sub(r"\s{2,}", " ", name).strip(" -–—")
