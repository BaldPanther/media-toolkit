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
JUNK_LABELS = {JUNK_EXTRAS: "складывать в Extras", JUNK_DELETE: "удалять"}

# Языки описаний. На имена файлов и папок влияют только через режим «на языке
# описаний» ниже — в остальных режимах эта настройка их не касается.
LANGUAGES = {"ru-RU": "русский", "en-US": "английский"}

# Язык имён папок и файлов. «Оригинал» — как тайтл называется на родине
# (`original_title` у TMDb), «на языке описаний» — то же, что идёт в <title>
# внутри .nfo, то есть у нас русское.
NAME_AUTO = "auto"
NAME_EN = "en"
NAME_LOCAL = "local"
NAME_ORIGINAL = "original"
NAME_MODES = (NAME_AUTO, NAME_EN, NAME_LOCAL, NAME_ORIGINAL)
NAME_LABELS = {
    NAME_AUTO: "оригинал, если он на языке описаний",
    NAME_EN: "всегда английские",
    NAME_LOCAL: "всегда на языке описаний",
    NAME_ORIGINAL: "всегда оригинальные",
}

# Псевдоязык в приоритете картинок: на его месте подставляется язык оригинала
# тайтла. Настоящие коды у TMDb двухбуквенные, так что столкнуться не с чем.
ART_ORIGINAL = "orig"


@dataclass
class Settings:
    tmdb_key: str = ""
    fanart_key: str = ""
    omdb_key: str = ""

    # Язык <title>/<plot> в .nfo.
    meta_language: str = "ru-RU"
    # Приоритет языка картинок; "" — вариант без текста (null у TMDb),
    # ART_ORIGINAL — язык оригинала тайтла.
    art_languages: list[str] = field(default_factory=lambda: ["ru", "en", ""])

    # Язык имён папок и файлов — один из NAME_MODES.
    name_language: str = NAME_AUTO
    # Исключения из него по тайтлам: {"movie:10986": NAME_LOCAL}. Список личных
    # решений, а не состояние библиотеки: «Il bisbetico domato» ни на английском,
    # ни в оригинале ничего не говорит, и такой тайтл хочется видеть по-русски.
    name_overrides: dict[str, str] = field(default_factory=dict)

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


def _as_str_dict(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def title_key(kind: str, tmdb_id) -> str:
    """Ключ тайтла в списке исключений.

    Фильмы и сериалы у TMDb нумеруются порознь, поэтому одного номера мало:
    10986 — это и «Il bisbetico domato», и какой-то сериал.
    """
    return f"{kind}:{tmdb_id}"


def name_language_for(settings, kind: str, tmdb_id) -> str:
    """Режим имени конкретного тайтла: личное исключение или общая настройка."""
    return settings.name_overrides.get(title_key(kind, tmdb_id), settings.name_language)


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
                name_language=str(d.get("name_language", base.name_language)),
                name_overrides=_as_str_dict(d.get("name_overrides")),
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
