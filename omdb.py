"""Клиент OMDb — единственное, что от него нужно: рейтинг IMDb с числом голосов.

В TMDb API рейтинга IMDb нет, только собственный. А в .nfo, которые писал
tinyMediaManager, `<rating name="imdb" default="true">` стоит первым — Kodi
показывает именно его. Один запрос по imdb-ID закрывает разницу.

Ключ бесплатный: omdbapi.com/apikey.aspx, лимит 1000 запросов в сутки —
для «скачал сериал → прогнал» с большим запасом.
"""
from __future__ import annotations

import net
from net import build_url

_get_json = net.get_json

BASE = "https://www.omdbapi.com/"


def rating(imdb_id: str, api_key: str) -> tuple[float | None, int | None]:
    """imdb-ID → (рейтинг, число голосов). Нет данных или ключа → (None, None).

    Ошибку наружу не бросаем: рейтинг — необязательное украшение, из-за него не
    должен падать весь прогон. Причина молча превращается в «рейтинга нет».
    """
    if not (imdb_id or "").strip() or not (api_key or "").strip():
        return None, None
    try:
        data = _get_json(build_url(BASE, "", i=imdb_id, apikey=api_key)) or {}
    except net.NetError:
        return None, None
    if str(data.get("Response", "")).lower() != "true":
        return None, None
    return _num(data.get("imdbRating")), _votes(data.get("imdbVotes"))


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):   # "N/A" у свежих тайтлов без оценок
        return None


def _votes(value) -> int | None:
    try:
        return int(str(value).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None
