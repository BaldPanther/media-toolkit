"""Генерация EDL-файлов для авто-пропуска интро/титров в Kodi.

Kodi читает `<video>.edl` рядом с видео (то же имя, расширение `.edl`). Формат —
текст «start end action», где для интро/титров используем action 3 (commercial
break): Kodi авто-пропускает сегмент один раз за сеанс, при этом можно отмотать
назад. Время — в секундах. Строки, начинающиеся с `##`, — комментарии (Kodi v19+).
См. https://kodi.wiki/view/Edit_decision_list.

Модуль делится на две части:
  - каркас (этот код): парсинг S/E, группировка по сезонам, padding, запись .edl,
    фича «первая серия сезона» (интро оставляем видимым);
  - автодетект (`detect.py`-логика ниже): аудио-фингерпринтинг через fpcalc.

Каркас не зависит ни от fpcalc, ни от numpy и пригоден для юнит-тестов.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

# action 3 = commercial break: авто-пропуск один раз, с возможностью отмотать назад.
EDL_ACTION_SKIP = 3

# Насколько конец титров должен не доходить до конца файла, чтобы считать его
# заданным руками, а не «до конца». Время в .edl пишется с тремя знаками, а
# длительность из ffprobe может отличаться на доли секунды — секунды хватает.
END_EPS = 1.0

_HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Разбор имени: сезон / эпизод
# --------------------------------------------------------------------------- #

# Ловит S01E01, s1e1, "S01.E01", "1x01". Первая группа — сезон, вторая — эпизод.
_SXXEYY = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})")
_NxNN = re.compile(r"(?<!\d)(\d{1,2})[xX](\d{1,3})(?!\d)")


def parse_season_episode(path) -> tuple[int | None, int | None]:
    """Возвращает (season, episode) из имени файла или (None, None)."""
    stem = Path(path).stem
    m = _SXXEYY.search(stem) or _NxNN.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def group_by_season(paths) -> dict[int | None, list[Path]]:
    """Группирует пути по номеру сезона (сортировка внутри — по эпизоду)."""
    groups: dict[int | None, list[Path]] = {}
    for p in paths:
        season, _ = parse_season_episode(p)
        groups.setdefault(season, []).append(Path(p))
    for season in groups:
        groups[season].sort(key=lambda p: (parse_season_episode(p)[1] or 0, p.name))
    return groups


# --------------------------------------------------------------------------- #
# Сегменты и padding
# --------------------------------------------------------------------------- #

@dataclass
class Segment:
    start: float
    end: float

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Padding:
    """Ручной сдвиг границ (в секундах) поверх автодетекта, на весь сезон.

    Отрицательный сдвиг начала = начать пропуск раньше; положительный конца =
    отпустить пропуск позже. По умолчанию нули (границы как нашёл детект).
    """
    intro_start: float = 0.0
    intro_end: float = 0.0
    outro_start: float = 0.0
    outro_end: float = 0.0


def apply_padding(seg: Segment | None, pad_start: float, pad_end: float,
                  duration: float | None = None) -> Segment | None:
    """Сдвигает границы сегмента, зажимая в [0, duration]. None → None."""
    if seg is None:
        return None
    start = max(0.0, seg.start + pad_start)
    end = seg.end + pad_end
    if duration is not None:
        end = min(end, duration)
    if end <= start:
        return None
    return Segment(start, end)


# --------------------------------------------------------------------------- #
# Модель серии и «первая серия сезона»
# --------------------------------------------------------------------------- #

@dataclass
class EpisodeEdl:
    path: Path
    season: int | None = None
    episode: int | None = None
    duration: float | None = None
    intro: Segment | None = None      # найденное/заданное интро (до фичи первой серии)
    outro: Segment | None = None
    # Конец титров задан руками, а не «до конца файла». Такой конец главнее общей
    # галки outro_to_end: иначе указанное время молча растягивалось бы до конца
    # файла и сцена после титров всё равно проглатывалась бы.
    outro_fixed_end: bool = False
    recap: Segment | None = None      # начальный recap «в предыдущих сериях» (0..X), вручную
    note: str = ""                    # диагностика детекта (для показа в таблице)
    chapters: int = 0                 # сколько глав уже в файле (из скана, см. core.MkvFile)

    audio_langs: list[str] = field(default_factory=list)  # языки аудиодорожек по порядку

    # Параллельные наборы таймингов, чтобы сравнивать источники, не теряя ни один.
    # Активные — intro/outro/recap (из них пишется .edl). local_* — снимок последнего
    # локального автодетекта (для «взять локальные»), online_* — ответ онлайн-базы.
    local_intro: Segment | None = None
    local_outro: Segment | None = None
    local_recap: Segment | None = None
    online_intro: Segment | None = None
    online_outro: Segment | None = None
    online_recap: Segment | None = None
    online_note: str = ""             # источник/диагностика онлайн-загрузки

    @classmethod
    def from_path(cls, path, duration: float | None = None,
                  audio_langs: list[str] | None = None) -> "EpisodeEdl":
        s, e = parse_season_episode(path)
        return cls(Path(path), season=s, episode=e, duration=duration,
                   audio_langs=list(audio_langs or []))


def is_first_of_season(ep: EpisodeEdl) -> bool:
    return ep.episode == 1


def effective_intro(ep: EpisodeEdl, keep_first_intro: bool) -> Segment | None:
    """Интро с учётом фичи первой серии: у E01 сезона интро оставляем видимым."""
    if keep_first_intro and is_first_of_season(ep):
        return None
    return ep.intro


# --------------------------------------------------------------------------- #
# Формирование и запись .edl
# --------------------------------------------------------------------------- #

def edl_path(video_path) -> Path:
    return Path(video_path).with_suffix(".edl")


def has_external_edl(video_path) -> bool:
    return edl_path(video_path).is_file()


def format_edl(intro: Segment | None, outro: Segment | None,
               recap: Segment | None = None) -> str:
    """Текст .edl из сегментов. Пустая строка, если все None."""
    lines: list[str] = []
    if recap is not None:
        lines.append("## Recap (в предыдущих сериях)")
        lines.append(f"{recap.start:.3f}\t{recap.end:.3f}\t{EDL_ACTION_SKIP}")
    if intro is not None:
        lines.append("## Intro")
        lines.append(f"{intro.start:.3f}\t{intro.end:.3f}\t{EDL_ACTION_SKIP}")
    if outro is not None:
        lines.append("## Outro / Credits")
        lines.append(f"{outro.start:.3f}\t{outro.end:.3f}\t{EDL_ACTION_SKIP}")
    return "\n".join(lines) + "\n" if lines else ""


def write_edl(video_path, intro: Segment | None, outro: Segment | None,
              recap: Segment | None = None) -> Path | None:
    """Пишет `<video>.edl` рядом с видео. Возвращает путь или None (нечего писать)."""
    text = format_edl(intro, outro, recap)
    if not text:
        return None
    p = edl_path(video_path)
    p.write_text(text, encoding="utf-8")
    return p


def delete_edl(video_path) -> bool:
    """Удаляет .edl, если он есть. True — если файл был удалён."""
    p = edl_path(video_path)
    if p.is_file():
        p.unlink()
        return True
    return False


def read_edl(video_path) -> tuple[Segment | None, Segment | None, Segment | None]:
    """Читает `<video>.edl` обратно в сегменты → (recap, intro, outro).

    Свои файлы разбираются точно — по комментариям-маркерам `## Recap` /
    `## Intro` / `## Outro` перед строками (их пишет format_edl). Чужой .edl без
    маркеров классифицируем эвристикой: сегмент, начинающийся с нуля, — интро,
    с наибольшим началом — титры, остальное игнорируем.
    Нечитаемый/битый файл → (None, None, None).
    """
    p = edl_path(video_path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None, None, None

    recap = intro = outro = None
    unmarked: list[Segment] = []
    marker = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("##"):
            marker = line.casefold()
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            start, end = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if end <= start:
            continue
        seg = Segment(start, end)
        if "recap" in marker:
            recap = seg
        elif "intro" in marker:
            intro = seg
        elif "outro" in marker or "credits" in marker:
            outro = seg
        else:
            unmarked.append(seg)
        marker = ""

    if unmarked:
        unmarked.sort(key=lambda s: s.start)
        if intro is None and unmarked[0].start < 1.0:
            intro = unmarked.pop(0)
        if outro is None and unmarked:
            outro = unmarked.pop()
        if intro is None and unmarked:
            intro = unmarked.pop(0)
    return recap, intro, outro


def final_outro(ep: EpisodeEdl, padding: Padding,
                outro_to_end: bool = True) -> Segment | None:
    """Титры с учётом отступов и того, тянуть ли их до конца файла.

    Обычно после титров ничего нет, и пропуск честнее вести до самого конца —
    иначе Kodi отдаст последние секунды чёрного экрана. Но там, где после титров
    идёт сцена (post-credits), так она бы тоже проглатывалась: для этого
    outro_to_end=False оставляет найденную границу, к которой применён
    padding.outro_end.

    Галка — политика на случай «конец неизвестен». Если конец задан руками
    (ep.outro_fixed_end), он главнее: растягивать до конца файла именно то время,
    которое пользователь только что вписал, значит молча его отменить.
    """
    outro = apply_padding(ep.outro, padding.outro_start, padding.outro_end, ep.duration)
    if outro is not None and outro_to_end and ep.duration and not ep.outro_fixed_end:
        outro = Segment(outro.start, ep.duration)
    return outro


def build_and_write(ep: EpisodeEdl, padding: Padding, keep_first_intro: bool,
                    outro_to_end: bool = True) -> Path | None:
    """Применяет padding + фичу первой серии и пишет .edl для одной серии."""
    intro = apply_padding(effective_intro(ep, keep_first_intro),
                          padding.intro_start, padding.intro_end, ep.duration)
    outro = final_outro(ep, padding, outro_to_end)
    return write_edl(ep.path, intro, outro, ep.recap)


# --------------------------------------------------------------------------- #
# Поиск fpcalc (Chromaprint) — для автодетекта
# --------------------------------------------------------------------------- #

# Запасные папки на случай, если инструмента нет в PATH. На macOS это основной
# случай: GUI, запущенный из Dock или Finder, получает от launchd урезанный PATH
# (/usr/bin:/bin:/usr/sbin:/sbin) — brew-каталогов там нет, и shutil.which не
# находит fpcalc, даже когда он установлен. На Windows PATH тот же, что в консоли,
# а fpcalc.exe и так лежит портативным в assets/, так что искать больше негде.
_TOOL_DIRS = [] if os.name == "nt" else ["/opt/homebrew/bin", "/usr/local/bin"]


def _find_tool(name: str) -> str | None:
    exe = name + ".exe" if os.name == "nt" else name
    found = shutil.which(name) or shutil.which(exe)
    if found:
        return found
    for d in _TOOL_DIRS:
        cand = os.path.join(d, exe)
        if os.path.isfile(cand):
            return cand
    return None


def find_fpcalc() -> str | None:
    """Ищет fpcalc: сперва портативный в assets/, затем в PATH и папках brew."""
    exe = "fpcalc.exe" if os.name == "nt" else "fpcalc"
    local = _HERE / "assets" / exe
    if local.is_file():
        return str(local)
    return _find_tool("fpcalc")


def find_ffmpeg() -> str | None:
    return _find_tool("ffmpeg")


def missing_tools() -> list[str]:
    """Каких инструментов автодетекта не хватает: ['fpcalc', 'ffmpeg'] или []."""
    return [n for n, f in (("fpcalc", find_fpcalc()), ("ffmpeg", find_ffmpeg())) if not f]


def install_hint(names) -> str:
    """Человеческая подсказка «как поставить» для списка из missing_tools()."""
    if not names:
        return ""
    listed = " и ".join(names)
    if os.name == "nt":
        if names == ["fpcalc"]:
            return ("Не найден fpcalc (Chromaprint). Положите fpcalc.exe в assets/ "
                    "или добавьте Chromaprint в PATH.")
        return f"Не найдены: {listed}. Установите их и добавьте в PATH."
    pkgs = " ".join({"fpcalc": "chromaprint", "ffmpeg": "ffmpeg"}[n] for n in names)
    return f"Не найдены: {listed}. Установите: brew install {pkgs}"


# Языки дубляжа, которые пропускаем при авто-выборе «оригинала»: на них поверх
# заставки часто проговаривают название серии, что сбивает детект. Оригинал (eng
# для западного, jpn для аниме) — чистая музыка.
_DUB_LANGS = {"rus", "ru"}


def pick_audio_index(langs, prefer_lang: str | None = None) -> int:
    """Индекс аудиодорожки (0-based среди аудио) для детекта.

    prefer_lang — конкретный код языка (напр. 'eng'); None — «оригинал (авто)»:
    первая дорожка не на языке дубляжа. Всегда возвращает валидный индекс (0 —
    запасной, если ничего не подошло).
    """
    norm = [(l or "").lower() for l in langs]
    if prefer_lang:
        pl = prefer_lang.lower()
        for i, l in enumerate(norm):
            if l == pl:
                return i
        # запрошенного языка нет — падаем в авто-логику ниже
    for i, l in enumerate(norm):          # первая не-дубляж и не «неизвестный»
        if l not in _DUB_LANGS and l not in ("", "und"):
            return i
    for i, l in enumerate(norm):          # запасной: любая не-дубляж
        if l not in _DUB_LANGS:
            return i
    return 0


# --------------------------------------------------------------------------- #
# Автодетект интро/титров (аудио-фингерпринтинг)
# --------------------------------------------------------------------------- #
#
# Идея (как в Jellyfin intro-skipper): интро и титры — это повторяющийся между
# сериями сезона аудио-сегмент. Для каждой серии берём окно у начала (интро) и у
# конца (титры), считаем Chromaprint-отпечаток и ищем самый длинный совпадающий
# кусок относительно эталонной серии — со сдвигом, чтобы поймать «плавающую» из-за
# cold open заставку. Отпечаток нечувствителен к языку визуальных титров (сравнение
# идёт по звуку), поэтому мультиязычные титры детектируются по общей музыке.

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_IPS_FALLBACK = 8.0            # отпечатков в секунду, если не удалось откалибровать

# Параметры детекта по умолчанию (подобраны под сериалы/мультсериалы ~20–45 мин).
INTRO_WINDOW = 240.0          # сек от начала, где ищем интро
OUTRO_WINDOW = 240.0          # сек от конца, где ищем титры
MAX_SHIFT_S = 150.0           # макс. относительный сдвиг сегмента между сериями
BIT_THR = 11                  # порог различия отпечатков (из 32 бит) для «похожи»
                              # (выше — точнее старт и шире покрытие; ~18+ даёт ложные)
MIN_LEN_S = 10.0              # короче — не считаем интро/титрами
SNAP_END_S = 20.0            # если титры кончаются ближе к концу файла — тянем до конца


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, creationflags=_CREATE_NO_WINDOW)


# --------------------------------------------------------------------------- #
# Кэш отпечатков
# --------------------------------------------------------------------------- #
#
# Отпечаток считается из нескольких минут звука, но чтобы добраться до звука в
# MKV, ffmpeg вынужден прочитать весь этот кусок вместе с видео: дорожки в
# контейнере переплетены. На сетевой шаре это сотни мегабайт на окно и почти всё
# время детекта — при том, что от прогона к прогону данные те же, файл не менялся.
# Поэтому посчитанное складываем на диск и при повторном детекте берём оттуда:
# повторный запуск и подбор порогов становятся мгновенными.
#
# В ключ входит всё, от чего отпечаток зависит: файл (путь, размер, время правки),
# номер дорожки и границы окна. Пороги сравнения (BIT_THR и прочие) в ключ не
# входят намеренно — на извлечение они не влияют, и крутить их можно бесплатно.

CACHE_TTL_DAYS = 90            # записи, которых столько не касались, удаляем


def _cache_key(path, window: float, from_end: bool, audio_index: int) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    raw = (f"{os.fspath(path)}|{st.st_size}|{st.st_mtime_ns}"
           f"|{audio_index}|{window:.0f}|{int(from_end)}")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_load(cache_dir, key: str):
    """(fp, dur) из кэша или None. Битую или чужую запись молча игнорируем."""
    import numpy as np
    f = Path(cache_dir) / f"{key}.json"
    try:
        data = json.loads(f.read_text("utf-8"))
        fp = np.frombuffer(base64.b64decode(data["fp"]), dtype="<u4").astype(np.uint32)
    except Exception:  # noqa: BLE001 — нет файла, битый JSON, чужой формат: считаем заново
        return None
    if fp.size == 0:
        return None
    try:
        os.utime(f, None)      # запись живая — по этой метке чистим старьё
    except OSError:
        pass
    return fp, data.get("dur")


def _cache_store(cache_dir, key: str, fp, dur) -> None:
    import numpy as np  # noqa: F401 — нужен вызывающему, здесь только для симметрии
    d = Path(cache_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
        payload = {"fp": base64.b64encode(fp.astype("<u4").tobytes()).decode("ascii"),
                   "dur": dur}
        tmp = d / f"{key}.tmp"
        tmp.write_text(json.dumps(payload), "utf-8")
        tmp.replace(d / f"{key}.json")   # подмена целиком: недописанного файла не увидим
    except Exception:  # noqa: BLE001 — кэш это ускорение, а не обязанность
        pass


def prune_cache(cache_dir, ttl_days: int = CACHE_TTL_DAYS) -> int:
    """Удаляет записи, которых давно не касались. Возвращает число удалённых."""
    cutoff = time.time() - ttl_days * 86400
    removed = 0
    try:
        entries = list(Path(cache_dir).glob("*.json"))
    except OSError:
        return 0
    for f in entries:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _extract_fp(ffmpeg: str, fpcalc: str, path, window: float, from_end: bool,
                audio_index: int = 0, cache_dir=None):
    """Отпечаток окна аудио → (np.uint32 array | None, reported_duration, из_кэша).

    cache_dir=None отключает кэш и всегда считает заново.
    """
    import numpy as np
    key = _cache_key(path, window, from_end, audio_index) if cache_dir else None
    if key:
        hit = _cache_load(cache_dir, key)
        if hit is not None:
            return hit[0], hit[1], True

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        pos = ["-sseof", f"-{window:.0f}"] if from_end else ["-ss", "0"]
        cp = _run([ffmpeg, "-v", "error", *pos, "-i", str(path), "-t", f"{window:.0f}",
                   "-map", f"0:a:{audio_index}", "-ac", "1", "-ar", "11025", "-y", tmp.name])
        if cp.returncode != 0:
            return None, None, False
        cp2 = _run([fpcalc, "-raw", "-length", "100000", tmp.name])
        out = cp2.stdout.decode("utf-8", "replace")
        fp = None
        dur = None
        for line in out.splitlines():
            if line.startswith("FINGERPRINT="):
                vals = line[len("FINGERPRINT="):].split(",")
                fp = np.array([int(x) for x in vals if x], dtype=np.uint32)
            elif line.startswith("DURATION="):
                try:
                    dur = float(line[len("DURATION="):])
                except ValueError:
                    pass
        if key and fp is not None:
            _cache_store(cache_dir, key, fp, dur)
        return fp, dur, False
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Кадры для проверки границ
# --------------------------------------------------------------------------- #
#
# По числу «конец интро: 0:29» не понять, попала граница куда надо: там может
# идти ещё логотип, а может уже затемнение или сама серия. Показываем границу
# кадрами — ряд снимков вокруг неё, и всё видно сразу.
#
# PNG, а не JPEG: Tk читает PNG сам, и Pillow для этого не нужен. Кадр берётся
# поиском по входу (-ss перед -i) — ffmpeg прыгает на ближайший опорный кадр и
# декодирует до нужного, читая доли секунды видео вместо всего файла.

FRAME_WIDTH = 190              # ширина снимка в точках
FRAME_HEIGHT = 107             # высота: 16:9 от ширины
FRAME_COUNT = 8                # сколько кадров в полосе
FRAME_STEP = 1.0               # шаг между ними по умолчанию, секунды


def grab_frame(ffmpeg: str, path, at: float, width: int = FRAME_WIDTH,
               height: int = FRAME_HEIGHT) -> bytes | None:
    """Кадр в указанной секунде как PNG. None — если вытащить не вышло.

    Кадр вписывается в коробку ровно width×height с чёрными полями: у сериалов
    бывает и 16:9, и 2.40:1, а полоса не должна прыгать от кадра к кадру.
    """
    box = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
           f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
    cp = _run([ffmpeg, "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", str(path),
               "-frames:v", "1", "-vf", box, "-c:v", "png", "-f", "image2", "-"])
    return cp.stdout if cp.returncode == 0 and cp.stdout else None


def frame_times(center: float, duration: float | None, count: int = FRAME_COUNT,
                step: float = FRAME_STEP) -> list[float]:
    """Моменты для полосы вокруг границы, в пределах файла.

    Окно смещено назад на треть: у границы важнее увидеть, что идёт ПОСЛЕ неё —
    кончилась ли заставка. Упирается в край файла — сдвигаем окно целиком, а не
    прижимаем кадры к краю: иначе несколько снимков окажутся одинаковыми.
    """
    span = (count - 1) * step
    start = max(0.0, center - (count // 3) * step)
    if duration:
        start = min(start, max(0.0, duration - span - 0.1))
    return [start + i * step for i in range(count)]


def grab_frames(ffmpeg: str, path, times, width: int = FRAME_WIDTH,
                on_frame=None, stop=None) -> list[bytes | None]:
    """Кадры в указанные моменты. on_frame(i, png) зовётся по готовности каждого."""
    out: list[bytes | None] = [None] * len(times)

    def work(i):
        if stop and stop():
            return
        out[i] = grab_frame(ffmpeg, path, times[i], width)
        if on_frame and not (stop and stop()):
            on_frame(i, out[i])

    if times:
        with ThreadPoolExecutor(max_workers=min(8, len(times))) as ex:
            list(ex.map(work, range(len(times))))
    return out


# --------------------------------------------------------------------------- #
# Открыть серию в плеере на нужной секунде
# --------------------------------------------------------------------------- #
#
# Кадры отвечают на вопрос «что там», но иногда хочется просто посмотреть кусок.
# Плееры, умеющие стартовать с заданной секунды, перечислены по предпочтению.
#
# Важный нюанс macOS: у IINA есть iina-cli, но в PATH его обычно нет — symlink
# ставится вручную через меню «IINA → Install Command Line Tool…». Сам бинарник
# при этом всегда лежит внутри бандла, поэтому shutil.which его не находит, а
# запустить можно. Отсюда и поиск по явным путям, а не только по PATH.

# Два обязательных флага для IINA, оба выяснены опытом.
#
# --no-stdin: её справка честно предупреждает — «sometimes iina-cli can detect
# whether stdin has file, but sometimes not». Запущенная из программы, она
# получает нетерминальный stdin, принимает его за поданный поток и играет его
# вместо файла: наружу это выходит ошибкой «не удаётся открыть файл или поток».
#
# --mpv-resume-playback=no: IINA (и mpv) держат сохранённую позицию просмотра в
# watch_later-файлах строкой «start=<секунды>», и при возобновлении она побеждает
# --start из командной строки. Замерено через IPC mpv: просим 5-ю секунду у файла
# с сохранёнными 45 — открывается на 45. С этим флагом открывается на 5.
_PLAYERS = (
    ("IINA", ("/Applications/IINA.app/Contents/MacOS/iina-cli",),
     lambda t: ["--no-stdin", "--mpv-resume-playback=no", f"--mpv-start={t:.3f}"]),
    ("mpv", ("mpv",), lambda t: ["--no-resume-playback", f"--start={t:.3f}"]),
    ("VLC", ("vlc", "/Applications/VLC.app/Contents/MacOS/VLC",
             r"C:\Program Files\VideoLAN\VLC\vlc.exe",
             r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"),
     lambda t: [f"--start-time={t:.0f}"]),
)


def find_player():
    """Первый доступный плеер, умеющий стартовать с секунды → (имя, путь, аргументы)."""
    for name, candidates, args in _PLAYERS:
        for cand in candidates:
            path = os.path.abspath(cand) if os.path.isfile(cand) else shutil.which(cand)
            if path:
                return name, path, args
    return None


def open_in_player(path, at: float) -> str | None:
    """Открывает файл на указанной секунде. Возвращает имя плеера или None."""
    found = find_player()
    if not found:
        return None
    name, exe, args = found
    subprocess.Popen([exe, *args(max(0.0, at)), str(path)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW)
    return name


def _longest_run(mask) -> tuple[int, int]:
    """Самый длинный непрерывный True в булевом массиве → (длина, индекс начала)."""
    import numpy as np
    if not mask.any():
        return 0, 0
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.view(np.int8), [0]))))
    starts, ends = edges[0::2], edges[1::2]
    lens = ends - starts
    k = int(lens.argmax())
    return int(lens[k]), int(starts[k])


def _best_common(a, b, max_shift: int, bit_thr: int) -> tuple[int, int]:
    """Самый длинный общий сегмент a и b по всем сдвигам.

    Возвращает (длина_в_отпечатках, старт_в_координатах_a).
    """
    import numpy as np
    na, nb = len(a), len(b)
    best_len = best_a = 0
    for off in range(-max_shift, max_shift + 1):
        a_s, b_s = (0, off) if off >= 0 else (-off, 0)
        ln = min(na - a_s, nb - b_s)
        if ln <= best_len:
            continue
        d = np.bitwise_count(a[a_s:a_s + ln] ^ b[b_s:b_s + ln])
        rl, rs = _longest_run(d <= bit_thr)
        if rl > best_len:
            best_len, best_a = rl, a_s + rs
    return best_len, best_a


def _assign(eps, fps, durs, kind, from_end, window,
            max_shift_s, bit_thr, min_len_s):
    """Проставляет ep.intro/ep.outro по КОНСЕНСУСУ нескольких сравнений.

    Каждую серию сравниваем с набором «якорей» (другие серии сезона) и берём
    медиану найденных границ. Так один атипичный эпизод не сбивает результат, а
    сегмент находится даже там, где сравнение с единственным эталоном промахивалось.
    """
    import numpy as np
    valid = [i for i, f in enumerate(fps) if f is not None and len(f) > 2]
    if len(valid) < 2:
        for e in eps:
            e.note = (e.note + "; " if e.note else "") + f"{kind}: мало отпечатков"
        return
    # На длинных сезонах ограничиваем число якорей (скорость), разбрасывая по сезону.
    anchors = valid if len(valid) <= 8 else valid[:: max(1, len(valid) // 8)]

    for i, e in enumerate(eps):
        fp = fps[i]
        if fp is None or len(fp) <= 2:
            e.note = (e.note + "; " if e.note else "") + f"{kind}: нет отпечатка"
            continue
        ips = (len(fp) / durs[i]) if durs[i] else _IPS_FALLBACK
        ms = int(max_shift_s * ips)
        starts, ends = [], []
        for j in anchors:
            if j == i:
                continue
            pfp = fps[j]
            if pfp is None or len(pfp) <= 2:
                continue
            rl, a_s = _best_common(fp, pfp, ms, bit_thr)
            if rl / ips >= min_len_s:
                starts.append(a_s)
                ends.append(a_s + rl)
        if not starts:
            e.note = (e.note + "; " if e.note else "") + f"{kind}: не найдено"
            continue
        start_t = float(np.median(starts)) / ips
        end_t = float(np.median(ends)) / ips
        if from_end:
            base = (e.duration - durs[i]) if (e.duration and durs[i]) else \
                   ((e.duration - window) if e.duration else 0.0)
            seg = Segment(base + start_t, base + end_t)
            if e.duration and 0 <= e.duration - seg.end < SNAP_END_S:
                seg.end = e.duration
        else:
            seg = Segment(start_t, end_t)
        setattr(e, kind, seg)


def detect_season(episodes, fpcalc: str, ffmpeg: str, kinds=("intro", "outro"),
                  intro_window=INTRO_WINDOW, outro_window=OUTRO_WINDOW,
                  max_shift_s=MAX_SHIFT_S, bit_thr=BIT_THR, min_len_s=MIN_LEN_S,
                  prefer_lang: str | None = None, progress=None, stop=None,
                  cache_dir=None, stats=None):
    """Проставляет intro/outro для серий ОДНОГО сезона по аудио-фингерпринтингу.

    episodes — список EpisodeEdl (желательно одного сезона, отсортированы).
    prefer_lang — язык дорожки для детекта (None = «оригинал (авто)», см.
    pick_audio_index). progress(kind, i, total, path) — колбэк по завершении
    каждого отпечатка (i — число готовых). stop() -> True прерывает работу:
    недосчитанный kind не проставляется, уже готовые остаются. Заполняет
    ep.intro/ep.outro и ep.note; возвращает тот же список.

    cache_dir — куда складывать отпечатки (None = считать всё заново). stats —
    словарь, в который дописываются ключи «cached» и «total»: сколько окон взято
    из кэша и сколько всего. Обновляется из потока, который зовёт detect_season,
    поэтому блокировка не нужна.
    """
    eps = list(episodes)
    n = len(eps)
    if n < 2:
        for e in eps:
            e.note = "нужно ≥2 серий в сезоне"
        return eps
    for kind in kinds:
        if stop and stop():
            return eps
        from_end = kind == "outro"
        window = outro_window if from_end else intro_window
        fps: list = [None] * n
        durs: list = [None] * n

        def extract(i, from_end=from_end, window=window):
            e = eps[i]
            idx = pick_audio_index(e.audio_langs, prefer_lang)
            return _extract_fp(ffmpeg, fpcalc, e.path, window, from_end, idx, cache_dir)

        # ffmpeg+fpcalc — внешние процессы, гоним пачкой; ffmpeg CPU-тяжёлый,
        # поэтому пул скромнее, чем при скане.
        cancelled = False
        with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as ex:
            futures = {ex.submit(extract, i): i for i in range(n)}
            done = 0
            for fut in as_completed(futures):
                if stop and stop():
                    cancelled = True
                    for f in futures:
                        f.cancel()
                    break
                i = futures[fut]
                fps[i], durs[i], from_cache = fut.result()
                if stats is not None:
                    stats["total"] = stats.get("total", 0) + 1
                    stats["cached"] = stats.get("cached", 0) + int(from_cache)
                done += 1
                if progress:
                    progress(kind, done, n, eps[i].path)
        if cancelled:
            # _assign по неполному набору дал бы кривые медианы — не проставляем.
            return eps
        _assign(eps, fps, durs, kind, from_end, window,
                max_shift_s, bit_thr, min_len_s)
    return eps
