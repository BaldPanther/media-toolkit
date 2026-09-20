"""Общий HTTP-слой для всех онлайн-источников.

Им пользуются и тайминги заставок (`online.py` — AniSkip/TheIntroDB), и метаданные
медиатеки (`tmdb.py`, `fanart.py`, `omdb.py`). Раньше запрос жил внутри `online.py`;
вынесен сюда, чтобы не держать два разных клиента с разной обработкой 429 и 404.

Только stdlib — модуль пригоден для headless-тестов.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "media-toolkit/1.0"
TIMEOUT = 25


class NetError(Exception):
    """Сетевая ошибка, отказ сервера или битый ответ."""


def _request(url: str, headers: dict | None) -> urllib.request.Request:
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    return urllib.request.Request(url, headers=h)


def get_json(url: str, headers: dict | None = None, retries: int = 2,
             timeout: int = TIMEOUT):
    """GET → распарсенный JSON. 404 → None (нет данных). 429 → пауза и повтор.

    404 отдаём как None намеренно: для всех наших источников «нет записи» —
    штатный результат, а не ошибка, и вызывающий код проверяет на None.
    """
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(_request(url, headers), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 503) and attempt < retries:
                wait = e.headers.get("Retry-After", "1") if e.headers else "1"
                try:
                    time.sleep(min(float(wait or 1), 5))
                except ValueError:
                    time.sleep(1)
                continue
            raise NetError(f"HTTP {e.code} для {url}") from e
        except urllib.error.URLError as e:
            raise NetError(f"Сеть: {getattr(e, 'reason', e)}") from e
        except (json.JSONDecodeError, ValueError) as e:
            raise NetError(f"Битый ответ {url}: {e}") from e
    return None


def build_url(base: str, path: str = "", **params) -> str:
    """Склейка URL с query-строкой; пустые параметры отбрасываются."""
    query = {k: v for k, v in params.items() if v not in (None, "")}
    url = f"{base}{path}"
    return f"{url}?{urllib.parse.urlencode(query)}" if query else url


def download_file(url: str, dest, timeout: int = 60) -> int:
    """Скачивает URL в `dest` через временный файл рядом. → размер в байтах.

    Временный файл и `os.replace` обязательны: обрыв связи на середине не должен
    оставлять на месте существующего постера обрезанный мусор.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with urllib.request.urlopen(_request(url, {"Accept": "*/*"}), timeout=timeout) as r:
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False,
                                             prefix=".dl-", suffix=dest.suffix) as f:
                tmp = Path(f.name)
                shutil.copyfileobj(r, f)
        size = tmp.stat().st_size
        if size == 0:
            raise NetError(f"Пустой ответ: {url}")
        tmp.replace(dest)
        tmp = None
        return size
    except urllib.error.HTTPError as e:
        raise NetError(f"HTTP {e.code} для {url}") from e
    except urllib.error.URLError as e:
        raise NetError(f"Сеть: {getattr(e, 'reason', e)}") from e
    except OSError as e:
        raise NetError(f"Запись {dest}: {e}") from e
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def get_bytes(url: str, timeout: int = TIMEOUT) -> bytes:
    """GET → сырые байты (миниатюры для сетки выбора арта)."""
    try:
        with urllib.request.urlopen(_request(url, {"Accept": "*/*"}), timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise NetError(f"HTTP {e.code} для {url}") from e
    except urllib.error.URLError as e:
        raise NetError(f"Сеть: {getattr(e, 'reason', e)}") from e
