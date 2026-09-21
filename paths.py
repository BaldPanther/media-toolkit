"""Где лежат настройки и кэш: в пользовательских каталогах ОС, а не рядом с программой.

Раньше `settings.json`, `meta_settings.json` и `subs_settings.json` лежали в папке
с кодом, то есть внутри репозитория. В git они не попадали (`.gitignore`), но
уезжали вместе с папкой при копировании и терялись при переносе или переклоне —
а там личные ключи TMDb/Fanart.tv/OMDb и пароль OpenSubtitles. Теперь они живут
там, где ОС держит настройки пользователя:

    macOS    ~/Library/Application Support/media-toolkit/
    Windows  %APPDATA%\\media-toolkit\\
    Linux    ~/.config/media-toolkit/   (или $XDG_CONFIG_HOME)

Старый файл рядом с программой переезжает сам при первом обращении к нему.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "media-toolkit"

# Прежнее место хранения — папка с кодом. Отсюда переносим.
_LEGACY_DIR = Path(__file__).resolve().parent


def config_dir() -> Path:
    """Каталог настроек текущей ОС. Существовать на этот момент не обязан."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP_NAME


def cache_dir() -> Path:
    """Каталог кэша текущей ОС — для того, что можно удалить без потерь.

    Отдельно от настроек намеренно: настройки терять нельзя, а кэш отпечатков
    весит и восстанавливается сам. Пути — те, где ОС держит кэши приложений:

        macOS    ~/Library/Caches/media-toolkit/
        Windows  %LOCALAPPDATA%\\media-toolkit\\
        Linux    ~/.cache/media-toolkit/   (или $XDG_CACHE_HOME)
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / APP_NAME


def settings_path(name: str) -> Path:
    """Путь к файлу настроек; старый файл рядом с программой переносится сюда.

    Если каталог недоступен (нет прав, не создаётся), возвращается прежний путь:
    работать по-старому лучше, чем потерять настройки.
    """
    target = config_dir() / name
    if target.exists():
        return target

    legacy = _LEGACY_DIR / name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if legacy.is_file():
            shutil.move(str(legacy), str(target))
    except Exception:  # noqa: BLE001 — каталог недоступен: остаёмся рядом с программой
        return legacy
    return target
