"""Скачивание внешних субтитров (`.ru.srt` рядом с серией) через subliminal.

Вынесено в отдельный модуль намеренно: subliminal — тяжёлая зависимость с большим
деревом пакетов. Основная программа (core.py / app.py) от него не зависит и работает
без него; здесь subliminal импортируется лениво, а при его отсутствии выдаётся
понятная подсказка (`subliminal_available`).

Источник русских субтитров на практике почти всегда один — OpenSubtitles
(провайдер `opensubtitlescom`), а для него нужен бесплатный аккаунт и API-ключ.
Провайдеры без регистрации (podnapisi/tvsubtitles/gestdown) для русского обычно
пусты, но оставлены как необязательный запасной источник.

Файл кладётся рядом с видео в виде `<имя>.ru.srt` — Plex/Jellyfin/Kodi/VLC
подхватывают его по языковому суффиксу автоматически. Видео не трогается.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path

import paths

# Личные настройки OpenSubtitles (логин и пароль) — в пользовательском
# каталоге настроек, а не рядом с кодом; см. paths.py.
_SETTINGS_NAME = "subs_settings.json"

LANG_RU = "rus"
# Запасные провайдеры без аккаунта — для русского почти всегда пусто, но иногда выручают.
FALLBACK_PROVIDERS = ["podnapisi", "tvsubtitles", "gestdown"]


def subliminal_available() -> str:
    """'' если subliminal импортируется; иначе текст ошибки (для подсказки в GUI)."""
    try:
        import subliminal  # noqa: F401
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)


# --------------------------------------------------------------------------- #
# Настройки OpenSubtitles (логин/пароль/ключ хранятся локально)
# --------------------------------------------------------------------------- #

@dataclass
class Settings:
    username: str = ""
    password: str = ""
    apikey: str = ""
    use_fallback: bool = True

    def has_opensubtitles(self) -> bool:
        return bool(self.username and self.password and self.apikey)


def load_settings() -> Settings:
    path = paths.settings_path(_SETTINGS_NAME)
    if path.is_file():
        try:
            d = json.loads(path.read_text("utf-8"))
            return Settings(
                username=d.get("username", ""),
                password=d.get("password", ""),
                apikey=d.get("apikey", ""),
                use_fallback=bool(d.get("use_fallback", True)),
            )
        except Exception:  # noqa: BLE001
            pass
    return Settings()


def save_settings(s: Settings) -> None:
    path = paths.settings_path(_SETTINGS_NAME)
    path.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), "utf-8")


def providers_for(s: Settings) -> list[str]:
    """Список провайдеров под текущие настройки (OpenSubtitles + запасные)."""
    provs: list[str] = []
    if s.has_opensubtitles():
        provs.append("opensubtitlescom")
    if s.use_fallback:
        provs += FALLBACK_PROVIDERS
    return provs


# --------------------------------------------------------------------------- #
# Поиск серий без русских субтитров
# --------------------------------------------------------------------------- #

_RU_CODES = {"rus", "ru"}
_EXT_SUFFIXES = (".ru.srt", ".rus.srt", ".ru.ass", ".rus.ass")


def has_external_ru(path: Path) -> bool:
    stem = str(Path(path).with_suffix(""))  # снимаем .mkv
    return any(Path(stem + suf).is_file() for suf in _EXT_SUFFIXES)


def has_embedded_ru(mkvfile) -> bool:
    """mkvfile — core.MkvFile: есть ли встроенная русская дорожка субтитров."""
    return any((t.language or "").lower() in _RU_CODES for t in mkvfile.subtitles)


def missing_russian(files) -> list:
    """core.MkvFile без русских субтитров (ни встроенных, ни внешним .ru.srt)."""
    out = []
    for f in files:
        if getattr(f, "error", ""):
            continue
        if has_embedded_ru(f) or has_external_ru(f.path):
            continue
        out.append(f)
    return out


# --------------------------------------------------------------------------- #
# Скачивание
# --------------------------------------------------------------------------- #

@dataclass
class SubResult:
    path: Path
    status: str          # "downloaded" | "notfound" | "error"
    detail: str = ""
    provider: str = ""


_region_configured = False


def _ensure_region() -> None:
    global _region_configured
    if _region_configured:
        return
    from subliminal import region
    # In-memory кэш намеренно: на Windows бэкенд dogpile `dbm` падает с
    # ModuleNotFoundError: fcntl (использует Unix-only блокировку файла), из-за чего
    # ломается получение токена авторизации OpenSubtitles. Памяти достаточно — кэш
    # (токен + результаты поиска) нужен лишь в пределах одного запуска.
    region.configure("dogpile.cache.memory", replace_existing_backend=True)
    _region_configured = True


def _login_opensubtitles(settings: Settings) -> str:
    """Логинится в OpenSubtitles и кладёт токен в кэш (его подхватит пакетная загрузка).

    Возвращает '' при успехе, иначе понятный текст ошибки. Нужен, потому что при
    неверных данных subliminal внутри пакетной загрузки глушит ошибку входа и выдаёт
    пустой результат — снаружи это неотличимо от «субтитров нет».
    """
    from subliminal.providers.opensubtitlescom import OpenSubtitlesComProvider
    prov = OpenSubtitlesComProvider(
        username=settings.username, password=settings.password, apikey=settings.apikey
    )
    try:
        prov.initialize()
        prov.login()  # token уходит в общий region-кэш; logout специально НЕ вызываем
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "too many" in msg:
            return "OpenSubtitles: слишком много попыток входа — подождите минуту и повторите."
        if "bad request" in msg or "unauthorized" in msg or "401" in msg:
            return ("OpenSubtitles: неверный логин или пароль. В поле «Логин» нужно имя "
                    "пользователя (username) с сайта, а не email.")
        return f"OpenSubtitles: вход не выполнен ({e})."
    if not getattr(prov, "token", None):
        return ("OpenSubtitles: вход не выполнен. Проверьте логин (username, не email), "
                "пароль и API-ключ.")
    return ""


def _good_match(subtitle, video) -> bool:
    """Берём субтитры, только если это та же серия (или точное совпадение по хешу),
    иначе можно скачать субтитры от чужого эпизода."""
    m = subtitle.get_matches(video)
    if "hash" in m:
        return True
    return {"series", "season", "episode"} <= m


def download(paths, settings: Settings, providers=None, progress=None, stop=None) -> list[SubResult]:
    """Качает лучший русский `.ru.srt` рядом с каждым `.mkv` из `paths`.

    progress(i, total, path) — вызывается перед обработкой каждого файла.
    stop() -> bool — если возвращает True, цикл прерывается.
    На ошибке авторизации/исчерпании лимита OpenSubtitles прерываемся сразу.
    """
    logging.getLogger("subliminal").setLevel(logging.CRITICAL)
    from babelfish import Language
    from subliminal import scan_video, download_best_subtitles, save_subtitles
    import subliminal.exceptions as ex

    _ensure_region()

    if providers is None:
        providers = providers_for(settings)
    provider_configs = {}
    if settings.has_opensubtitles():
        provider_configs["opensubtitlescom"] = {
            "username": settings.username,
            "password": settings.password,
            "apikey": settings.apikey,
        }
        # Проверяем вход заранее — иначе ошибка логина выглядит как «субтитров нет».
        if "opensubtitlescom" in providers:
            err = _login_opensubtitles(settings)
            if err:
                raise RuntimeError(err)

    langs = {Language(LANG_RU)}
    results: list[SubResult] = []
    total = len(paths)
    for i, raw in enumerate(paths):
        p = Path(raw)
        if progress:
            progress(i, total, p)
        if stop and stop():
            break
        try:
            video = scan_video(str(p))
            found = download_best_subtitles(
                [video], langs, providers=providers, provider_configs=provider_configs
            )
            subs = [s for s in found.get(video, []) if _good_match(s, video)]
            if not subs:
                results.append(SubResult(p, "notfound", "русские субтитры не найдены"))
                continue
            saved = save_subtitles(video, subs)
            prov = (saved or subs)[0].provider_name
            results.append(SubResult(p, "downloaded", f"{LANG_RU}.srt", prov))
        except ex.AuthenticationError as e:
            results.append(SubResult(p, "error", f"OpenSubtitles: ошибка авторизации ({e})"))
            break
        except ex.DownloadLimitExceeded as e:
            results.append(SubResult(p, "error", f"OpenSubtitles: исчерпан суточный лимит ({e})"))
            break
        except ex.ServiceUnavailable as e:
            results.append(SubResult(p, "error", f"сервис недоступен ({e})"))
        except Exception as e:  # noqa: BLE001
            results.append(SubResult(p, "error", f"{type(e).__name__}: {e}"))
    return results
