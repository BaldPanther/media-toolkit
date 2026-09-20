"""Настройки скрапера медиатеки: ключи API, языки, корни библиотек, политики.

Хранятся рядом с программой в `meta_settings.json` (в .gitignore — там личные
ключи). Формат и повадки те же, что у настроек OpenSubtitles в `subs.py`:
битый или отсутствующий файл молча даёт значения по умолчанию.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

_SETTINGS = Path(__file__).resolve().parent / "meta_settings.json"

# Что делать с уже существующими .nfo и картинками при повторном прогоне.
POLICY_ASK = "ask"
POLICY_MISSING = "missing_only"
POLICY_OVERWRITE = "overwrite"
POLICIES = (POLICY_MISSING, POLICY_OVERWRITE, POLICY_ASK)
POLICY_LABELS = {
    POLICY_MISSING: "докачивать недостающее",
    POLICY_OVERWRITE: "перезаписывать всё",
    POLICY_ASK: "спрашивать каждый раз",
}

# Что делать с посторонними файлами из раздачи.
JUNK_EXTRAS = "extras"
JUNK_DELETE = "delete"
JUNK_LABELS = {JUNK_EXTRAS: "складывать в Extras", JUNK_DELETE: "удалять в Корзину"}

# Языки описаний. Имена файлов и папок от этой настройки не зависят —
# они всегда английские.
LANGUAGES = {"ru-RU": "русский", "en-US": "английский"}


@dataclass
class Settings:
    tmdb_key: str = ""
    fanart_key: str = ""
    omdb_key: str = ""

    # Язык <title>/<plot> в .nfo. Имена на диске всегда английские.
    meta_language: str = "ru-RU"
    # Приоритет языка картинок; "" — вариант без текста (null у TMDb).
    art_languages: list[str] = field(default_factory=lambda: ["ru", "en", ""])

    poster_size: str = "original"
    fanart_size: str = "original"
    logo_size: str = "original"

    movies_roots: list[str] = field(default_factory=list)
    tv_roots: list[str] = field(default_factory=list)

    existing_policy: str = POLICY_MISSING
    junk_action: str = JUNK_EXTRAS
    extras_folder: str = "Extras"

    def has_tmdb(self) -> bool:
        return bool(self.tmdb_key.strip())

    def has_fanart(self) -> bool:
        return bool(self.fanart_key.strip())

    def has_omdb(self) -> bool:
        return bool(self.omdb_key.strip())


def _as_str_list(value, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    return [str(v) for v in value]


def load_settings() -> Settings:
    if _SETTINGS.is_file():
        try:
            d = json.loads(_SETTINGS.read_text("utf-8"))
            base = Settings()
            return Settings(
                tmdb_key=str(d.get("tmdb_key", "")),
                fanart_key=str(d.get("fanart_key", "")),
                omdb_key=str(d.get("omdb_key", "")),
                meta_language=str(d.get("meta_language", base.meta_language)),
                art_languages=_as_str_list(d.get("art_languages"), base.art_languages),
                poster_size=str(d.get("poster_size", base.poster_size)),
                fanart_size=str(d.get("fanart_size", base.fanart_size)),
                logo_size=str(d.get("logo_size", base.logo_size)),
                movies_roots=_as_str_list(d.get("movies_roots"), []),
                tv_roots=_as_str_list(d.get("tv_roots"), []),
                existing_policy=str(d.get("existing_policy", base.existing_policy)),
                junk_action=str(d.get("junk_action", base.junk_action)),
                extras_folder=str(d.get("extras_folder", base.extras_folder)),
            )
        except Exception:  # noqa: BLE001 — нет файла/битый JSON: значения по умолчанию
            pass
    return Settings()


def save_settings(s: Settings) -> None:
    _SETTINGS.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), "utf-8")
