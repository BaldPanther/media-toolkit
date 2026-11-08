"""TVmaze — деление сериала на сезоны, как у трекеров и TheTVDB, и его спецвыпуски.

TMDb держит многие аниме одним длинным сезоном («Re:Zero» — 85 серий подряд), а
трекеры делят их на сезоны. Обычно TMDb знает деление в своей группе серий
«Seasons» (`numbering.grouped`), но её правят позже, чем выходит новый сезон, а у
части тайтлов её нет вовсе («Blue Lock» — 38 серий одним сезоном). TVmaze делит так
же, как TheTVDB, сразу заводит анонсированные сезоны и знает даты всех серий.

Спецвыпуски (`specials`) — для сборников (`packs.py`): из каких ТВ-серий склеена каждая
серия режиссёрской версии и когда вышла OVA, которой нет среди фильмов TMDb.

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


def _lookup(tvdb_id=None, imdb_id=None) -> int | None:
    """Номер сериала в TVmaze по номеру TheTVDB или IMDb, которые отдаёт TMDb."""
    for key, value in (("thetvdb", tvdb_id), ("imdb", imdb_id)):
        if not value:
            continue
        found = net.get_json(f"{BASE}/lookup/shows?{urllib.parse.urlencode({key: value})}",
                             timeout=TIMEOUT)
        if isinstance(found, dict) and found.get("id"):
            return int(found["id"])
    return None


def show(tvdb_id=None, imdb_id=None) -> dict:
    """Сериал вместе с сезонами и сериями (`_embedded`). Не нашёлся — {}."""
    maze_id = _lookup(tvdb_id, imdb_id)
    if maze_id is None:
        return {}
    embed = urllib.parse.urlencode([("embed[]", "seasons"), ("embed[]", "episodes")])
    return net.get_json(f"{BASE}/shows/{maze_id}?{embed}", timeout=TIMEOUT) or {}


def specials(tvdb_id=None, imdb_id=None) -> list[dict]:
    """Спецвыпуски сериала — у TVmaze без номеров, с названием, датой, длительностью и
    сезоном, к которому они относятся: фильмы и OVA («significant_special»: «Re:Zero -
    Starting Life in Another World - Memory Snow»), режиссёрская версия и прочее
    («insignificant_special»: «Director's Cut - 2 - Reunion With The Witch / Starting
    Life from Zero in Another World»). Не нашёлся — []."""
    maze_id = _lookup(tvdb_id, imdb_id)
    if maze_id is None:
        return []
    episodes = net.get_json(f"{BASE}/shows/{maze_id}/episodes?specials=1", timeout=TIMEOUT)
    return [e for e in episodes or [] if isinstance(e, dict)
            and e.get("type", "regular") != "regular"]
