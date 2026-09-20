"""Клиент Fanart.tv — дополнительные варианты логотипов и постеров.

Зачем при наличии TMDb: у Fanart.tv заметно больше clearlogo (прозрачный PNG с
названием), и они аккуратнее — именно такие лежат в медиатеке после
tinyMediaManager. Постеры и фоны оттуда тоже попадают в общий список кандидатов,
так что в сетке выбора видно оба источника сразу.

⚠ Ключевой нюанс адресации: фильмы ищутся по TMDb- или IMDb-ID, а **сериалы —
только по TVDb-ID**. Его берём из `tmdb.external_ids("tv", id)["tvdb_id"]`.

Ключ бесплатный: fanart.tv → профиль → Add project → project API key.
"""
from __future__ import annotations

import net
from net import build_url

_get_json = net.get_json

BASE = "https://webservice.fanart.tv/v3"

# Ключи ответа, из которых берём арт. Внутри каждого — список
# {'id','url','lang','likes'}, у постеров сезонов дополнительно 'season'.
MOVIE_KEYS = {
    "poster": ("movieposter",),
    "fanart": ("moviebackground",),
    "clearlogo": ("hdmovielogo", "movielogo"),
}
TV_KEYS = {
    "poster": ("tvposter",),
    "fanart": ("showbackground",),
    "clearlogo": ("hdtvlogo", "clearlogo"),
    "seasonposter": ("seasonposter",),
}


def _call(path: str, api_key: str):
    if not (api_key or "").strip():
        raise net.NetError("Не задан ключ Fanart.tv — «Настройки скрапера…»")
    return _get_json(build_url(BASE, path, api_key=api_key))


def movie_art(movie_id, api_key: str) -> dict:
    """Арт фильма по TMDb-ID (число) или IMDb-ID (tt…). Нет записи → {}."""
    return _call(f"/movies/{movie_id}", api_key) or {}


def tv_art(tvdb_id, api_key: str) -> dict:
    """Арт сериала по **TVDb**-ID. Нет записи → {}."""
    if not tvdb_id:
        return {}
    return _call(f"/tv/{tvdb_id}", api_key) or {}
