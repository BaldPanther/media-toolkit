"""Генерация Kodi-метаданных: movie.nfo, tvshow.nfo и .nfo для каждой серии.

Состав тегов повторяет файлы, которые в этой медиатеке уже написал
tinyMediaManager, — чтобы после перехода на свой скрапер карточки в Kodi
выглядели ровно так же. Часть тегов tmm (`<streamdetails>`, `<original_filename>`,
`<dateadded>`) описывает сам медиафайл, а не тайтл; Kodi добывает это сам при
сканировании, поэтому мы их не пишем.

Отметки просмотра — `<watched>`, `<playcount>`, `<userrating>` — при перезаписи
переносятся из старого файла (`read_watch_state`). Без этого повторный прогон
сбрасывал бы историю просмотра.

Модуль чистый: на входе модели из `metadata.py`, на выходе строка XML.
"""
from __future__ import annotations

import datetime as _dt
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import metadata

GENERATOR = "media-toolkit"


def _text(parent: ET.Element, tag: str, value=None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    el.text = "" if value is None else str(value)
    return el


def _serialize(root: ET.Element) -> str:
    """Дерево → строка с декларацией и отступами.

    Экранирование `&`, `<`, кавычек делает ElementTree — руками ничего не клеим.
    """
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    today = _dt.date.today().isoformat()
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f"<!--created on {today} by {GENERATOR}-->\n" + body + "\n")


# --------------------------------------------------------------------------- #
# Отметки просмотра из существующего файла
# --------------------------------------------------------------------------- #

_WATCH_TAGS = ("watched", "playcount", "userrating", "lastplayed", "dateadded")


def read_watch_state(path) -> dict:
    """Забирает из существующего .nfo то, что принадлежит пользователю, а не TMDb."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        text = p.read_text("utf-8", errors="replace")
    except OSError:
        return {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # У многосерийного файла в .nfo несколько корней подряд — для XML это
        # ошибка, но именно так Kodi хранит такие серии. Оборачиваем и берём
        # отметки первого блока. Декларацию перед этим снимаем: внутри
        # элемента она недопустима.
        body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
        try:
            root = ET.fromstring(f"<nfo>{body}</nfo>")
        except ET.ParseError:
            return {}
        first = next(iter(root), None)
        if first is None:
            return {}
        root = first
    state = {}
    for tag in _WATCH_TAGS:
        el = root.find(tag)
        if el is not None and (el.text or "").strip():
            state[tag] = el.text.strip()
    return state


def _watch(parent: ET.Element, preserve: dict | None) -> None:
    preserve = preserve or {}
    _text(parent, "watched", preserve.get("watched", "false"))
    _text(parent, "playcount", preserve.get("playcount", "0"))
    for tag in ("lastplayed", "dateadded"):
        if preserve.get(tag):
            _text(parent, tag, preserve[tag])


# --------------------------------------------------------------------------- #
# Общие блоки
# --------------------------------------------------------------------------- #

def _ratings(parent: ET.Element, info, preserve: dict | None) -> None:
    """<ratings>: IMDb первым и по умолчанию, если он есть — как у tinyMediaManager."""
    block = ET.SubElement(parent, "ratings")
    entries = []
    if info.rating_imdb:
        entries.append(("imdb", info.rating_imdb))
    if info.rating_tmdb:
        entries.append(("themoviedb", info.rating_tmdb))
    for i, (name, rating) in enumerate(entries):
        el = ET.SubElement(block, "rating",
                           {"default": "true" if i == 0 else "false",
                            "max": "10", "name": name})
        _text(el, "value", round(rating.value, 1))
        _text(el, "votes", rating.votes or 0)
    _text(parent, "userrating", (preserve or {}).get("userrating", "0"))


def _unique_ids(parent: ET.Element, info, default_type: str) -> None:
    ids = [("tmdb", str(info.tmdb_id or "")), ("imdb", info.imdb_id),
           ("tvdb", info.tvdb_id)]
    present = [(t, v) for t, v in ids if v]
    if not any(t == default_type for t, _ in present) and present:
        default_type = present[0][0]
    for id_type, value in present:
        _text(parent, "uniqueid", value, type=id_type,
              default="true" if id_type == default_type else "false")


def _art_thumbs(parent: ET.Element, art: dict | None, season_art: dict | None = None) -> None:
    """<thumb aspect="…"> и <fanart> — теми же URL, что скачаны на диск.

    Kodi берёт картинки с диска по именам файлов; эти теги нужны, чтобы при
    обновлении из .nfo он знал исходники, и чтобы файл совпадал по составу
    с тем, что писал tinyMediaManager.
    """
    art = art or {}
    if art.get(metadata.ART_POSTER):
        _text(parent, "thumb", art[metadata.ART_POSTER], aspect="poster")
    if art.get(metadata.ART_LOGO):
        _text(parent, "thumb", art[metadata.ART_LOGO], aspect="clearlogo")
    for season, url in sorted((season_art or {}).items()):
        _text(parent, "thumb", url, aspect="poster", season=season, type="season")
    if art.get(metadata.ART_FANART):
        block = ET.SubElement(parent, "fanart")
        _text(block, "thumb", art[metadata.ART_FANART])


def _people(parent: ET.Element, info) -> None:
    for genre in info.genres:
        _text(parent, "genre", genre)
    for studio in info.studios:
        _text(parent, "studio", studio)
    for person in info.writers:
        _text(parent, "credits", person.name)
    for person in info.directors:
        _text(parent, "director", person.name)
    for person in info.actors:
        actor = ET.SubElement(parent, "actor")
        _text(actor, "name", person.name)
        _text(actor, "role", person.role)
        _text(actor, "order", person.order)
        if person.thumb:
            _text(actor, "thumb", person.thumb)


# --------------------------------------------------------------------------- #
# Фильм
# --------------------------------------------------------------------------- #

def make_movie_nfo(info, art: dict | None = None, preserve: dict | None = None) -> str:
    root = ET.Element("movie")
    _text(root, "title", info.title or info.title_en)
    _text(root, "originaltitle", info.original_title or info.title_en)
    _text(root, "sorttitle")
    _text(root, "year", info.year or "")
    _ratings(root, info, preserve)
    if info.set_name:
        block = ET.SubElement(root, "set")
        _text(block, "name", info.set_name)
        _text(block, "overview", info.set_overview)
    _text(root, "plot", info.plot)
    _text(root, "outline", info.outline)
    _text(root, "tagline", info.tagline)
    _text(root, "runtime", info.runtime or "")
    _art_thumbs(root, art)
    _text(root, "mpaa", info.mpaa)
    _text(root, "certification", info.mpaa)
    if info.imdb_id:
        _text(root, "id", info.imdb_id)
    if info.tmdb_id:
        _text(root, "tmdbid", info.tmdb_id)
    _unique_ids(root, info, "imdb")
    for country in info.countries:
        _text(root, "country", country)
    _text(root, "premiered", info.premiered)
    _watch(root, preserve)
    _people(root, info)
    return _serialize(root)


# --------------------------------------------------------------------------- #
# Сериал
# --------------------------------------------------------------------------- #

def make_tvshow_nfo(info, art: dict | None = None, season_art: dict | None = None,
                    preserve: dict | None = None) -> str:
    root = ET.Element("tvshow")
    title = info.title or info.title_en
    _text(root, "title", title)
    _text(root, "originaltitle", info.original_title or info.title_en)
    _text(root, "showtitle", title)
    _text(root, "sorttitle")
    _text(root, "year", info.year or "")
    _ratings(root, info, preserve)
    _text(root, "outline")
    _text(root, "plot", info.plot)
    _text(root, "tagline", info.tagline)
    _text(root, "runtime", info.runtime or "")
    _art_thumbs(root, art, season_art)
    _text(root, "mpaa", info.mpaa)
    _text(root, "certification", info.mpaa)
    if info.tvdb_id:
        _text(root, "id", info.tvdb_id)
    if info.imdb_id:
        _text(root, "imdbid", info.imdb_id)
    if info.tmdb_id:
        _text(root, "tmdbid", info.tmdb_id)
    _unique_ids(root, info, "tvdb")
    for country in info.countries:
        _text(root, "country", country)
    _text(root, "premiered", info.premiered)
    _text(root, "status", info.status)
    _watch(root, preserve)
    _people(root, info)
    return _serialize(root)


# --------------------------------------------------------------------------- #
# Серия
# --------------------------------------------------------------------------- #

def make_episodes_nfo(info, episodes, preserve: dict | None = None) -> str:
    """`.nfo` для файла, в котором лежит несколько серий подряд.

    Kodi ждёт в таком файле несколько корневых `<episodedetails>` друг за другом
    (строго говоря, невалидный XML, но это его формат, и так же писал
    tinyMediaManager). Декларация при этом одна, в начале файла.
    """
    blocks = [make_episode_nfo(info, e, preserve if i == 0 else None)
              for i, e in enumerate(episodes)]
    head, first = blocks[0].split("-->\n", 1)
    rest = [b.split("-->\n", 1)[1] for b in blocks[1:]]
    return head + "-->\n" + first + "".join(rest)


def make_episode_nfo(info, episode, preserve: dict | None = None) -> str:
    root = ET.Element("episodedetails")
    _text(root, "title", episode.title or episode.title_en)
    _text(root, "originaltitle", episode.title_en)
    _text(root, "showtitle", info.title or info.title_en)
    _text(root, "season", episode.season)
    _text(root, "episode", episode.episode)
    if episode.tmdb_id:
        _text(root, "uniqueid", episode.tmdb_id, type="tmdb", default="true")
    block = ET.SubElement(root, "ratings")
    if episode.rating:
        el = ET.SubElement(block, "rating",
                           {"default": "true", "max": "10", "name": "themoviedb"})
        _text(el, "value", round(episode.rating.value, 1))
        _text(el, "votes", episode.rating.votes or 0)
    _text(root, "userrating", (preserve or {}).get("userrating", "0"))
    _text(root, "plot", episode.plot)
    _text(root, "runtime", episode.runtime or info.runtime or "")
    _text(root, "mpaa", info.mpaa)
    _text(root, "premiered", episode.aired)
    _text(root, "aired", episode.aired)
    _watch(root, preserve)
    for studio in info.studios:
        _text(root, "studio", studio)
    # Съёмочная группа и гости — у каждой серии свои; общий состав сериала
    # лежит в tvshow.nfo и здесь не дублируется.
    for person in episode.writers:
        _text(root, "credits", person.name)
    for person in episode.directors:
        _text(root, "director", person.name)
    for person in episode.guests:
        actor = ET.SubElement(root, "actor")
        _text(actor, "name", person.name)
        _text(actor, "role", person.role)
        _text(actor, "order", person.order)
        if person.thumb:
            _text(actor, "thumb", person.thumb)
    return _serialize(root)


# --------------------------------------------------------------------------- #
# Запись
# --------------------------------------------------------------------------- #

def write(path, content: str) -> None:
    """Записывает готовый .nfo.

    Отметки просмотра переносит не эта функция, а `read_watch_state` на входе
    в `make_*_nfo` — вызывающий обязан прочитать старый файл до записи нового.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
