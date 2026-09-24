"""Самопроверка программы: `app.py --selfcheck <отчёт.txt>` (или собранный exe/.app).

Нужна прежде всего сборкам (PyInstaller в GitHub Actions). У необязательных
зависимостей часть модулей подгружается по имени во время работы — провайдеры
subliminal, бэкенд кэша dogpile, конвертеры babelfish, — и сборщик может их
молча потерять: программа запустится, а сломается только на кнопке. Поэтому
каждая зависимость здесь дёргается по-настоящему, как её дёргает программа.

Отчёт пишется в файл, а не в консоль: у windowed-сборки на Windows stdout нет.
Код возврата 0 — всё обязательное на месте, 1 — чего-то нет.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _tk():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    version = tk.TkVersion
    root.destroy()
    return f"Tk {version}"


def _modules():
    import artwork, chapters, core, edl, fanart, library, metaconf, metadata  # noqa: F401, E401
    import metaui, net, nfo, omdb, online, paths, pipeline, subs, theme, tmdb, trim  # noqa: F401, E401
    return "ok"


def _numpy():
    import numpy
    return numpy.__version__


def _pillow():
    import PIL
    from PIL import Image, ImageTk  # noqa: F401 — ImageTk: миниатюры в сетке постеров
    return PIL.__version__


def _send2trash():
    import send2trash  # noqa: F401
    return "ok"


def _subliminal():
    import subs
    from babelfish import Language
    from subliminal import Video
    from subliminal.extensions import provider_manager

    subs._ensure_region()                                 # бэкенд кэша по имени
    video = Video.fromname("Show.Name.S01E02.720p.WEB.mkv")  # guessit
    Language(subs.LANG_RU)
    missing = {"opensubtitlescom", *subs.FALLBACK_PROVIDERS} - set(provider_manager.names())
    if missing:
        raise RuntimeError(f"нет провайдеров: {', '.join(sorted(missing))}")
    return f"{type(video).__name__}, провайдеры на месте"


def _assets():
    need = ["app-icon.png"] + (["app-icon.ico"] if sys.platform == "win32" else [])
    missing = [n for n in need if not (_HERE / "assets" / n).is_file()]
    if missing:
        raise RuntimeError(f"нет {', '.join(missing)} в {_HERE / 'assets'}")
    return "ok"


def _fpcalc():
    import edl
    found = edl.find_fpcalc()
    if not found:
        raise RuntimeError("не найден")
    return found


def _mkvmerge():
    import core
    found = core.find_tool("mkvmerge")
    if not found:
        raise RuntimeError("не найден")
    return found


def _ffmpeg():
    import edl
    found = edl.find_ffmpeg()
    if not found:
        raise RuntimeError("не найден")
    return found


def default_checks() -> list[tuple[str, object, bool]]:
    """(название, проверка, обязательна ли). Внешние CLI в сборку не входят —
    их ставит пользователь, поэтому они только в отчёте. fpcalc на Windows
    кладётся в сборку, на macOS — из brew, как ffmpeg и MKVToolNix."""
    return [
        ("tkinter", _tk, True),
        ("модули программы", _modules, True),
        ("numpy", _numpy, True),
        ("Pillow", _pillow, True),
        ("send2trash", _send2trash, True),
        ("subliminal", _subliminal, True),
        ("иконки", _assets, True),
        ("fpcalc", _fpcalc, sys.platform == "win32"),
        ("mkvmerge", _mkvmerge, False),
        ("ffmpeg", _ffmpeg, False),
    ]


def run(report, checks=None) -> int:
    """Прогоняет проверки, пишет отчёт в файл `report`; 0 — всё обязательное есть."""
    lines, failed = [], False
    for name, check, required in (default_checks() if checks is None else checks):
        try:
            lines.append(f"ok    {name}: {check()}")
        except Exception as e:  # noqa: BLE001 — любая ошибка = проверка не прошла
            failed = failed or required
            mark = "FAIL" if required else "нет "
            lines.append(f"{mark}  {name}: {type(e).__name__}: {e}")
    Path(report).write_text("\n".join(lines) + "\n", "utf-8")
    return 1 if failed else 0
