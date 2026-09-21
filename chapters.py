"""Главы Matroska из той же разметки, что и `.edl` — для плееров помимо Kodi.

Зачем. Kodi читает `.edl` рядом с видео, а плееры на mpv (IINA и прочие) — нет:
у mpv своё понятие «EDL», это формат склейки кусков в общий таймлайн, а не список
пропусков, и сайдкар он просто игнорирует (проверено: серия проходит сквозь
помеченный интервал, не перескакивая). Зато главы понимают все, и плагины
пропуска заставок для IINA опираются именно на их НАЗВАНИЯ.

Правит `mkvpropedit` — тот же инструмент, которым меняются дорожки по умолчанию.
Пишется только заголовок: на файле в 6.3 ГБ запись заняла 0.06 с, размер не
изменился. Перекодирования нет.

⚠ Главное ограничение: mkvpropedit заменяет набор глав ЦЕЛИКОМ — дописать свои
к чужим нельзя, старые пропадают безвозвратно. Поэтому файл с посторонними
главами по умолчанию не трогаем: свою разметку узнаём по названиям (OUR_NAMES)
и перезаписываем только её.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import edl

# Названия наших глав. По ним же отличаем свою разметку от чужой, поэтому
# менять их задним числом нельзя: прежде записанные главы станут «чужими».
# Английские не по случайности — плагины пропуска ищут именно такие слова.
#
# «Stinger» — принятое название сцены после титров. Слов «credits», «outro»,
# «intro» и «recap» в нём нет намеренно: плагины пропуска разбирают главы по
# названию, и любое из них означало бы «это заставка» — сцену бы пропускало,
# ради чего её и выделяли отдельной главой.
OUR_NAMES = ("Cold Open", "Recap", "Intro", "Episode", "Credits", "Stinger")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Простой (OGM) формат глав: пара строк на главу. mkvpropedit принимает его
# наравне с XML, а читать и писать его несравнимо проще.
_CH_TIME = re.compile(r"^CHAPTER(\d+)=(\d+):(\d\d):(\d\d(?:\.\d+)?)$")
_CH_NAME = re.compile(r"^CHAPTER(\d+)NAME=(.*)$")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, creationflags=_CREATE_NO_WINDOW)


def _fmt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# --------------------------------------------------------------------------- #
# Что размечать
# --------------------------------------------------------------------------- #

def build_points(ep: "edl.EpisodeEdl", padding: "edl.Padding",
                 keep_first_intro: bool = True,
                 outro_to_end: bool = True) -> list[tuple[float, str]]:
    """Точки глав (секунда, название) из таймингов серии. Пусто — размечать нечего.

    Берутся те же значения, из которых пишется `.edl`: с отступами сезона, с
    учётом «показывать заставку в E01» и «титры до конца файла». Так главы и
    пропуск в Kodi описывают одно и то же, а не расходятся.
    """
    intro = edl.apply_padding(edl.effective_intro(ep, keep_first_intro),
                              padding.intro_start, padding.intro_end, ep.duration)
    outro = edl.final_outro(ep, padding, outro_to_end)
    recap = ep.recap
    if intro is None and outro is None and recap is None:
        return []          # размечать нечего: одна глава на весь файл бесполезна

    points: list[tuple[float, str]] = []
    if recap is not None:
        points.append((max(0.0, recap.start), "Recap"))
    if intro is not None:
        points.append((intro.start, "Intro"))
    # Тело серии начинается там, где кончилось последнее вступление.
    body = max([s.end for s in (recap, intro) if s is not None] or [0.0])
    if outro is None or body < outro.start:
        points.append((body, "Episode"))
    if outro is not None:
        points.append((outro.start, "Credits"))
        # Титры кончаются раньше файла — дальше идёт сцена, и без своей главы она
        # осталась бы внутри титров: перемотать к ней было бы нечем, а плеер
        # пропустил бы её заодно с титрами.
        if ep.duration and outro.end < ep.duration - edl.END_EPS:
            points.append((outro.end, "Stinger"))
    if not points:
        return []

    points.sort(key=lambda p: p[0])
    # Заставка не с нуля — значит, перед ней холодное открытие; без главы на нуле
    # первый кусок серии остался бы вне разметки.
    if points[0][0] > 0.5:
        points.insert(0, (0.0, "Cold Open"))

    merged: list[tuple[float, str]] = []
    for start, name in points:
        if merged and start - merged[-1][0] < 0.5:
            merged[-1] = (merged[-1][0], name)   # глав нулевой длины не бывает
        else:
            merged.append((start, name))
    return merged


def simple_text(points) -> str:
    """Текст глав в простом (OGM) формате. Пустой список → пустая строка."""
    lines: list[str] = []
    for i, (start, name) in enumerate(points, start=1):
        lines.append(f"CHAPTER{i:02d}={_fmt_time(start)}")
        lines.append(f"CHAPTER{i:02d}NAME={name}")
    return "\n".join(lines) + "\n" if lines else ""


# --------------------------------------------------------------------------- #
# Чтение и запись
# --------------------------------------------------------------------------- #

def read_chapters(path, mkvextract: str) -> list[tuple[float, str]]:
    """Главы файла как (секунда, название). Нет глав или ошибка → пустой список."""
    cp = _run([mkvextract, str(path), "chapters", "--simple", "-"])
    if cp.returncode != 0:
        return []
    times: dict[str, float] = {}
    names: dict[str, str] = {}
    for line in cp.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        m = _CH_TIME.match(line)
        if m:
            times[m.group(1)] = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + float(m.group(4))
            continue
        m = _CH_NAME.match(line)
        if m:
            names[m.group(1)] = m.group(2)
    return [(times[k], names.get(k, "")) for k in sorted(times)]


def state(path, mkvextract: str) -> str:
    """Чьи главы в файле: 'none' — глав нет, 'ours' — наша разметка, 'foreign' — чужие.

    Отличать нужно, потому что перезапись стирает чужую разметку целиком: у
    фильма там осмысленное деление на сцены, и терять его нельзя.
    """
    found = read_chapters(path, mkvextract)
    if not found:
        return "none"
    return "ours" if all(name in OUR_NAMES for _, name in found) else "foreign"


def write_chapters(path, points, mkvpropedit: str) -> bool:
    """Заменяет главы файла на указанные. False — писать было нечего или ошибка."""
    text = simple_text(points)
    if not text:
        return False
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        tmp.write(text)
        tmp.close()
        return _run([mkvpropedit, str(path), "--chapters", tmp.name]).returncode == 0
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def clear_chapters(path, mkvpropedit: str) -> bool:
    """Убирает главы из файла (пустое имя файла — так это делает mkvpropedit)."""
    return _run([mkvpropedit, str(path), "--chapters", ""]).returncode == 0
