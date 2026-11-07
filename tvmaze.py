"""TVmaze — деление сериала на сезоны, как у трекеров и TheTVDB.

TMDb держит многие аниме одним длинным сезоном («Re:Zero» — 85 серий подряд), а
трекеры делят их на сезоны. Обычно TMDb знает деление в своей группе серий
«Seasons» (`numbering.grouped`), но её правят позже, чем выходит новый сезон, а у
части тайтлов её нет вовсе («Blue Lock» — 38 серий одним сезоном). TVmaze делит так
же, как TheTVDB, сразу заводит анонсированные сезоны и знает даты всех серий.

Бесплатно, без ключа, не больше 20 запросов за 10 секунд; данные — по лицензии CC
BY-SA, источник указан в README. Описания, названия серий и картинки — по-прежнему из
TMDb: у TVmaze они только английские.

Только stdlib.
"""
from __future__ import annotations

import urllib.parse

import net

BASE = "https://api.tvmaze.com"
TIMEOUT = 10


def show(tvdb_id=None, imdb_id=None) -> dict:
    """Сериал вместе с сезонами и сериями (`_embedded`) — по номеру TheTVDB или IMDb,
    которые отдаёт TMDb. Не нашёлся — {}."""
    for key, value in (("thetvdb", tvdb_id), ("imdb", imdb_id)):
        if not value:
            continue
        found = net.get_json(f"{BASE}/lookup/shows?{urllib.parse.urlencode({key: value})}",
                             timeout=TIMEOUT)
        if isinstance(found, dict) and found.get("id"):
            embed = urllib.parse.urlencode([("embed[]", "seasons"), ("embed[]", "episodes")])
            return net.get_json(f"{BASE}/shows/{found['id']}?{embed}", timeout=TIMEOUT) or {}
    return {}
