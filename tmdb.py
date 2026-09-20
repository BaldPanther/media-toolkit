"""Клиент TMDb (api.themoviedb.org/3) — основной источник метаданных и картинок.

Тонкий слой: собирает URL, отдаёт разобранный JSON как есть. Превращение ответов
в модели приложения — в `metadata.py`, чтобы этот модуль оставался проверяемым
подстановкой `_get_json`.

Ключ бесплатный: themoviedb.org → профиль → Settings → API → запросить v3 auth key.

Два языка запрашиваются намеренно порознь. Локализованный ответ идёт в `<title>`
и `<plot>` внутри .nfo, английский — в имена папок и файлов (у пользователя имена
на диске всегда английские). Брать для имени `original_title` нельзя: у аниме это
японский, а в медиатеке лежит «Solo Leveling (2024)».
"""
from __future__ import annotations

import net
from net import build_url

_get_json = net.get_json

BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"

# Размеры из /configuration. Зашиты, чтобы не тратить запрос на каждый прогон;
# менялись они за всю историю API один раз.
POSTER_SIZES = ("w92", "w154", "w185", "w342", "w500", "w780", "original")
BACKDROP_SIZES = ("w300", "w780", "w1280", "original")
LOGO_SIZES = ("w45", "w92", "w154", "w185", "w300", "w500", "original")
STILL_SIZES = ("w92", "w185", "w300", "original")
PROFILE_SIZES = ("w45", "w185", "h632", "original")

# Языки картинок: русский, английский и null — «без текста», логотипы и постеры
# без надписей. null в API передаётся строкой "null".
ART_LANGS = "ru,en,null"

KINDS = ("movie", "tv")


def image_url(path: str | None, size: str = "original") -> str | None:
    """Относительный путь из ответа API → полный URL картинки."""
    if not path:
        return None
    return f"{IMAGE_BASE}/{size}{path}"


def _call(path: str, api_key: str, **params):
    if not (api_key or "").strip():
        raise net.NetError("Не задан ключ TMDb — «Настройки скрапера…»")
    return _get_json(build_url(BASE, path, api_key=api_key, **params))


def check_key(api_key: str) -> bool:
    """Проверка ключа самым дешёвым запросом. Неверный ключ → TMDb отдаёт 401."""
    return bool(_call("/configuration", api_key))


def search(kind: str, query: str, api_key: str, year: int | None = None,
           language: str = "en-US") -> list[dict]:
    """Поиск фильма или сериала по названию. → список сырых результатов TMDb."""
    if kind not in KINDS:
        raise ValueError(f"kind должен быть movie или tv, получено {kind!r}")
    params = {"query": query, "language": language, "include_adult": "false"}
    if year:
        params["year" if kind == "movie" else "first_air_date_year"] = year
    data = _call(f"/search/{kind}", api_key, **params)
    return (data or {}).get("results", []) or []


def find_by_imdb(imdb_id: str, api_key: str) -> dict:
    """IMDb ID (tt…) → {'movie_results': [...], 'tv_results': [...]}.

    Нужен, когда поиск по названию не справился и пользователь вставил tt-ID.
    """
    return _call(f"/find/{imdb_id}", api_key, external_source="imdb_id") or {}


def movie(tmdb_id: int, api_key: str, language: str = "en-US") -> dict:
    """Карточка фильма вместе с составом, внешними ID и возрастными рейтингами."""
    return _call(f"/movie/{tmdb_id}", api_key, language=language,
                 append_to_response="credits,external_ids,release_dates") or {}


def tv(tmdb_id: int, api_key: str, language: str = "en-US") -> dict:
    """Карточка сериала. Состав берём aggregate_credits — это актёры всего сериала,
    а не одного эпизода, как в обычном credits."""
    return _call(f"/tv/{tmdb_id}", api_key, language=language,
                 append_to_response="aggregate_credits,external_ids,content_ratings") or {}


def season(tv_id: int, number: int, api_key: str, language: str = "en-US") -> dict:
    """Сезон целиком со списком серий. Сезон 0 — спецвыпуски (папка Specials)."""
    return _call(f"/tv/{tv_id}/season/{number}", api_key, language=language) or {}


def external_ids(kind: str, tmdb_id: int, api_key: str) -> dict:
    """{'imdb_id':…, 'tvdb_id':…}. TVDb-ID нужен для запроса арта в Fanart.tv —
    у него TV-эндпоинт адресуется именно им, а не идентификатором TMDb."""
    return _call(f"/{kind}/{tmdb_id}/external_ids", api_key) or {}


def images(kind: str, tmdb_id: int, api_key: str, languages: str = ART_LANGS) -> dict:
    """Все постеры/фоны/логотипы тайтла: {'posters':[…],'backdrops':[…],'logos':[…]}."""
    return _call(f"/{kind}/{tmdb_id}/images", api_key,
                 include_image_language=languages) or {}


def season_images(tv_id: int, number: int, api_key: str,
                  languages: str = ART_LANGS) -> dict:
    """Постеры конкретного сезона: {'posters': […]}."""
    return _call(f"/tv/{tv_id}/season/{number}/images", api_key,
                 include_image_language=languages) or {}


def episode_groups(tv_id: int, api_key: str) -> list[dict]:
    """Альтернативные разбивки сериала на сезоны.

    Нужны для аниме: в основной структуре TMDb «Solo Leveling» — один сезон из
    25 серий, а в группе «Seasons» лежит официальное деление 12 + 13, как на
    диске и у TVDb. Отдаёт только описания групп, без самих серий.
    """
    data = _call(f"/tv/{tv_id}/episode_groups", api_key) or {}
    return data.get("results", []) or []


def episode_group(group_id: str, api_key: str, language: str = "en-US") -> dict:
    """Содержимое одной группы: подгруппы-«сезоны» со списками серий.

    У каждой серии внутри — её каноничные `season_number`/`episode_number`,
    то есть группа работает как таблица перенумерации.
    """
    return _call(f"/tv/episode_group/{group_id}", api_key, language=language) or {}


def collection(collection_id: int, api_key: str, language: str = "en-US") -> dict:
    """Киноколлекция (<set> в .nfo): название и описание серии фильмов."""
    return _call(f"/collection/{collection_id}", api_key, language=language) or {}
