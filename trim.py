"""Обрезка хвоста — того, что в MKV идёт после конца видео.

Зачем. Бывают релизы, где звук длиннее картинки: видео титров релизёр укоротил,
а дубляжи оставил целиком. В Altered Carbon S01 все дорожки на ~37 с длиннее
видео, а одна (Profix) — ещё на 50 с тишины. Длительность файла считается по
самой длинной дорожке, туда же ложится и конец титров в `.edl`.

Kodi на пропуске встаёт на последний ключевой кадр до цели, и если цель дальше
него больше чем на 20 с (`CheckPlayerInit`, «too far to decode before finishing
seek»), бросает точный переход и играет прямо с этого кадра: остаток титров,
потом чёрный экран, пока не кончится звук. Следующая серия включается только
через полминуты. Значением в `.edl` это не лечится: цель должна быть и за концом
звука, и не дальше 20 с от кадра — одновременно так не бывает. Лечится файл.
(mpv и IINA от этого не страдают: прыжок за конец файла сразу даёт следующий.)

Как. ffmpeg перекладывает пакеты в новый контейнер без перекодирования и
останавливает каждую дорожку на конце видео — картинка и звук остаются бит в бит.
Чего ffmpeg не переносит или переносит по-своему, возвращает mkvpropedit:
вложения (обложку ffmpeg превратил бы в дорожку V_MJPEG), главы, флаги,
DefaultDuration; он же пересчитывает статистику дорожек — ffmpeg оставляет
старую. Затем новый файл сверяется с оригиналом, и только после этого занимает
его место. Не сошлось — оригинал не тронут, временный файл удалён.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import core
import edl

# Хвост короче — не трогаем. У обычных файлов звук расходится с видео на доли
# секунды (последний аудиокадр, округления), от силы на секунду; перепаковка ради
# этого ничего бы не дала. Ошибиться в другую сторону тоже не страшно: лишняя
# обрезка ничего не портит, только занимает несколько минут.
TAIL_MIN_S = 2.0

# Временный файл лежит рядом с оригиналом — замена тогда простое переименование
# в пределах одного диска. Расширение не видео, чтобы медиатека не подхватила
# недописанный файл.
TMP_SUFFIX = ".trim-tmp"

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Доля прогресса на перепаковку; остальное — пересчёт статистики (он тоже читает
# файл целиком, но только читает).
_FFMPEG_SHARE = 0.7


# --------------------------------------------------------------------------- #
# Сколько хвоста
# --------------------------------------------------------------------------- #

def tail_seconds(tracks) -> float | None:
    """На сколько звук длиннее видео. None — не понять.

    Считается только звук. Субтитры за концом видео бывают и в нормальных
    релизах (последняя надпись висит на пару секунд дольше картинки), Kodi они не
    мешают — файл кончается, когда иссякнут звук и картинка, — а ffmpeg длину
    надписи всё равно не укорачивает.

    Концы берутся из статистики дорожек (core.Track.duration). Её пишут mkvmerge
    и ffmpeg, но не всякий муксер — без неё хвост не виден, и файл не трогается.
    Это оценка для таблицы: резать trim_file будет по концу видео, измеренному по
    пакетам.
    """
    video = [t.duration for t in tracks if t.type == "video" and t.duration]
    audio = [t.duration for t in tracks if t.type == "audio" and t.duration]
    if not video or not audio:
        return None
    return max(0.0, max(audio) - max(video))


def needs_trim(tail: float | None) -> bool:
    return tail is not None and tail > TAIL_MIN_S


# --------------------------------------------------------------------------- #
# Инструменты
# --------------------------------------------------------------------------- #

@dataclass
class Tools:
    ffmpeg: str
    ffprobe: str
    mkvmerge: str
    mkvpropedit: str
    mkvextract: str


def find_tools() -> tuple[Tools | None, list[str]]:
    """(инструменты, []) или (None, [чего не хватает])."""
    found = {"ffmpeg": edl.find_ffmpeg(), "ffprobe": edl.find_ffprobe(),
             "mkvmerge": core.find_tool("mkvmerge"),
             "mkvpropedit": core.find_tool("mkvpropedit"),
             "mkvextract": core.find_tool("mkvextract")}
    missing = [name for name, path in found.items() if not path]
    return (None if missing else Tools(**found)), missing


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, creationflags=_CREATE_NO_WINDOW)


def _text(b: bytes) -> str:
    return b.decode("utf-8", "replace") if b else ""


def identify(mkvmerge: str, path) -> dict | None:
    cp = _run([mkvmerge, "-J", str(path)])
    if cp.returncode not in (0, 1):          # 1 — предупреждения, JSON валиден
        return None
    try:
        return json.loads(_text(cp.stdout))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# Точный конец видео и что перекладывать
# --------------------------------------------------------------------------- #

def probe_video_end(ffprobe: str, path, near: float) -> float | None:
    """Конец видео по последним пакетам: метка плюс длительность пакета.

    near — примерно где искать (конец видео по статистике, а без неё —
    длительность файла). Читается только хвост от near назад; если видео там не
    нашлось — хвост длиннее окна, и окно расширяется.
    """
    for back in (30.0, 120.0, 600.0):
        cp = _run([ffprobe, "-v", "error", "-select_streams", "V:0",
                   "-read_intervals", f"{max(0.0, near - back):.3f}%",
                   "-show_entries", "packet=pts_time,duration_time",
                   "-of", "csv=p=0", str(path)])
        ends = []
        for line in _text(cp.stdout).splitlines():
            parts = [p for p in line.strip().split(",") if p]
            try:
                pts = float(parts[0])
                dur = float(parts[1]) if len(parts) > 1 and parts[1] != "N/A" else 0.0
            except (ValueError, IndexError):
                continue
            ends.append(pts + dur)
        if ends:
            return max(ends)
    return None


def stream_indexes(ffprobe: str, path) -> list[int]:
    """Номера потоков ffmpeg, которые надо переложить: все дорожки, без вложений.

    Картинки-вложения ffmpeg показывает видеопотоком с пометкой attached_pic;
    переложенная, такая стала бы настоящей видеодорожкой. Вложения возвращает
    mkvpropedit, поэтому ни картинки, ни шрифты ffmpeg не достаются.
    """
    cp = _run([ffprobe, "-v", "error", "-show_entries",
               "stream=index,codec_type:stream_disposition=attached_pic",
               "-of", "json", str(path)])
    try:
        streams = json.loads(_text(cp.stdout) or "{}").get("streams", [])
    except json.JSONDecodeError:
        return []
    return [s["index"] for s in streams
            if s.get("codec_type") in ("video", "audio", "subtitle")
            and not (s.get("disposition") or {}).get("attached_pic")]


def ffmpeg_args(ffmpeg: str, src, dst, cut: float, indexes) -> list[str]:
    """Перепаковка без перекодирования, каждая дорожка — до cut секунд.

    -default_mode passthrough: иначе старый ffmpeg сам назначил бы дорожкой по
    умолчанию первые субтитры там, где такой не было, и Kodi начал бы их
    показывать. Главы ffmpeg не переносит (-map_chapters -1): свои он пишет
    упрощённо, без языка и флагов, — их кладёт mkvpropedit из оригинала.
    """
    args = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-nostats",
            "-progress", "pipe:1", "-i", str(src)]
    for i in indexes:
        args += ["-map", f"0:{i}"]
    args += ["-map_chapters", "-1", "-c", "copy", "-default_mode", "passthrough",
             "-t", f"{cut:.3f}", "-f", "matroska", "-y", str(dst)]
    return args


# --------------------------------------------------------------------------- #
# Главы
# --------------------------------------------------------------------------- #

def _parse_ts(text: str) -> float:
    h, m, s = text.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _fmt_ts(seconds: float) -> str:
    ns = int(round(seconds * 1e9))
    h, ns = divmod(ns, 3600 * 10**9)
    m, ns = divmod(ns, 60 * 10**9)
    s, ns = divmod(ns, 10**9)
    return f"{h:02d}:{m:02d}:{s:02d}.{ns:09d}"


def chapters_ordered(xml_text: str) -> bool:
    """Упорядоченные главы: файл собирается из кусков, возможно, других файлов."""
    root = ET.fromstring(xml_text)
    return any((e.text or "").strip() == "1" for e in root.iter("EditionFlagOrdered"))


def clamp_chapters(xml_text: str, cut: float) -> tuple[str, int]:
    """Главы, подрезанные по cut: (XML, сколько глав осталось).

    Глава, начавшаяся после конца видео, пропадает вместе с хвостом; та, что его
    пересекает, кончается на cut. Остальное — языки, флаги, UID — как было.
    """
    root = ET.fromstring(xml_text)

    def walk(parent) -> int:
        kept = 0
        for atom in list(parent.findall("ChapterAtom")):
            start = atom.find("ChapterTimeStart")
            if start is not None and _parse_ts(start.text or "0:0:0") >= cut:
                parent.remove(atom)
                continue
            end = atom.find("ChapterTimeEnd")
            if end is not None and _parse_ts(end.text or "0:0:0") > cut:
                end.text = _fmt_ts(cut)
            kept += 1 + walk(atom)
        return kept

    count = sum(walk(edition) for edition in root.findall("EditionEntry"))
    # Без объявления XML mkvpropedit не узнаёт формат глав, а ElementTree его
    # не пишет — ставим сами.
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode"), count


def _chapter_count(info: dict) -> int:
    return sum((c or {}).get("num_entries", 0) for c in info.get("chapters", []) or [])


# --------------------------------------------------------------------------- #
# Что вернуть после ffmpeg и как сверить
# --------------------------------------------------------------------------- #

# Флаги дорожки: ключ mkvmerge -J → свойство mkvpropedit, и значение по
# умолчанию по спецификации Matroska (отсутствие флага значит именно его).
_FLAGS = {
    "default_track": ("flag-default", True),
    "forced_track": ("flag-forced", False),
    "enabled_track": ("flag-enabled", True),
    "flag_hearing_impaired": ("flag-hearing-impaired", False),
    "flag_visual_impaired": ("flag-visual-impaired", False),
    "flag_text_descriptions": ("flag-text-descriptions", False),
    "flag_original": ("flag-original", False),
    "flag_commentary": ("flag-commentary", False),
}

# Свойства, которые обязаны совпасть. Цвет и HDR сверяются, только если они были
# в оригинале: ffmpeg добавляет их из самого видеопотока, и это не расхождение.
_SAME = ("codec_id", "language", "language_ietf", "track_name", "pixel_dimensions",
         "display_dimensions", "display_unit", "audio_channels",
         "audio_sampling_frequency", "audio_bits_per_sample", "default_duration",
         "stereo_mode", "content_encoding_algorithms")
_SAME_IF_PRESENT = ("color_matrix_coefficients", "color_range", "color_primaries",
                    "color_transfer_characteristics", "max_content_light",
                    "max_frame_light", "max_luminance", "min_luminance",
                    "chromaticity_coordinates", "white_color_coordinates")


def restore_args(src: dict, new: dict) -> list[str]:
    """Аргументы mkvpropedit, возвращающие свойства дорожек как в оригинале."""
    args: list[str] = []
    for n, (a, b) in enumerate(zip(src.get("tracks", []), new.get("tracks", [])), start=1):
        pa, pb = a.get("properties", {}) or {}, b.get("properties", {}) or {}
        sets: list[str] = []
        for key, (prop, default) in _FLAGS.items():
            want = bool(pa.get(key, default))
            if want != bool(pb.get(key, default)):
                sets += ["--set", f"{prop}={int(want)}"]
        # bit-depth: ffmpeg пишет 32 бита сжатому звуку (AC-3, AAC), у которого
        # глубины нет вовсе, — вычищаем, раз в оригинале её не было.
        for key, prop in (("track_name", "name"), ("language", "language"),
                          ("language_ietf", "language-ietf"),
                          ("default_duration", "default-duration"),
                          ("audio_bits_per_sample", "bit-depth")):
            if pa.get(key) == pb.get(key):
                continue
            if pa.get(key) in (None, ""):
                sets += ["--delete", prop]
            else:
                sets += ["--set", f"{prop}={pa[key]}"]
        if pa.get("display_dimensions") and pa.get("display_dimensions") != pb.get("display_dimensions"):
            w, h = str(pa["display_dimensions"]).split("x")
            sets += ["--set", f"display-width={w}", "--set", f"display-height={h}"]
        if "display_unit" in pa and pa.get("display_unit") != pb.get("display_unit"):
            sets += ["--set", f"display-unit={pa['display_unit']}"]
        if sets:
            args += ["--edit", f"track:{n}", *sets]
    title = (src.get("container", {}).get("properties", {}) or {}).get("title")
    if title != (new.get("container", {}).get("properties", {}) or {}).get("title"):
        args += ["--edit", "info"] + (["--set", f"title={title}"] if title else ["--delete", "title"])
    return args


def _norm_private(codec_id: str, hexdata: str | None) -> str | None:
    """Заголовок кодека без несущественного.

    В записи hvcC ffmpeg сбрасывает флаг array_completeness у массивов VPS/SPS/PPS
    («наборы параметров могут встретиться и в потоке»). Сами наборы те же,
    декодеру разницы нет — флаг при сравнении гасим.
    """
    if not hexdata or codec_id != "V_MPEGH/ISO/HEVC":
        return hexdata
    data = bytearray(bytes.fromhex(hexdata))
    if len(data) < 23:
        return hexdata
    i = 23
    for _ in range(data[22]):
        if i + 3 > len(data):
            return hexdata
        data[i] &= 0x7F
        count = int.from_bytes(data[i + 1:i + 3], "big")
        i += 3
        for _ in range(count):
            i += 2 + int.from_bytes(data[i:i + 2], "big")
    return data.hex()


def compare(src: dict, new: dict, cut: float, chapters_left: int) -> list[str]:
    """Чем новый файл расходится с оригиналом. Пусто — можно заменять."""
    problems: list[str] = []
    st, nt = src.get("tracks", []), new.get("tracks", [])
    if len(st) != len(nt):
        return [f"дорожек было {len(st)}, стало {len(nt)}"]
    for a, b in zip(st, nt):
        pa, pb = a.get("properties", {}) or {}, b.get("properties", {}) or {}
        label = f"дорожка {a.get('id')} ({a.get('type')})"
        if a.get("type") != b.get("type"):
            problems.append(f"{label}: тип {b.get('type')}")
            continue
        keys = list(_SAME) + [k for k in _SAME_IF_PRESENT if k in pa]
        for key in keys:
            if pa.get(key) != pb.get(key):
                problems.append(f"{label}: {key} {pa.get(key)!r} → {pb.get(key)!r}")
        for key, (_, default) in _FLAGS.items():
            if bool(pa.get(key, default)) != bool(pb.get(key, default)):
                problems.append(f"{label}: {key} {pa.get(key, default)} → {pb.get(key, default)}")
        cid = pa.get("codec_id", "")
        if _norm_private(cid, pa.get("codec_private_data")) != \
                _norm_private(cid, pb.get("codec_private_data")):
            problems.append(f"{label}: заголовок кодека другой")
        ta, tb = pa.get("minimum_timestamp"), pb.get("minimum_timestamp")
        if ta is not None and tb is not None and abs(ta - tb) > 1_000_000:
            problems.append(f"{label}: сдвинулось начало ({ta / 1e9:.3f} → {tb / 1e9:.3f} с)")
        if a.get("type") == "video" and pa.get("tag_number_of_frames") and \
                pa.get("tag_number_of_frames") != pb.get("tag_number_of_frames"):
            problems.append(f"{label}: кадров было {pa.get('tag_number_of_frames')}, "
                            f"стало {pb.get('tag_number_of_frames')}")
        end = core.tag_seconds(pb.get("tag_duration"))
        if a.get("type") in ("video", "audio") and end is not None and end > cut + 0.5:
            problems.append(f"{label}: кончается на {end:.3f} с, а видео на {cut:.3f}")

    def atts(info):
        return [(x.get("file_name"), x.get("content_type"), x.get("size"),
                 x.get("description") or "") for x in info.get("attachments", []) or []]
    if atts(src) != atts(new):
        problems.append("вложения не совпали")
    if _chapter_count(new) != chapters_left:
        problems.append(f"глав {_chapter_count(new)} вместо {chapters_left}")
    title = lambda info: (info.get("container", {}).get("properties", {}) or {}).get("title")
    if title(src) != title(new):
        problems.append("название файла не совпало")
    return problems


# --------------------------------------------------------------------------- #
# Обрезка файла
# --------------------------------------------------------------------------- #

@dataclass
class TrimResult:
    ok: bool
    message: str                      # для лога
    skipped: bool = False             # хвоста не оказалось — файл не трогали
    cancelled: bool = False
    cut: float | None = None          # где кончается видео, секунды
    old_duration: float | None = None
    new_duration: float | None = None


def tmp_path(path) -> Path:
    return Path(path).with_suffix(TMP_SUFFIX)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _progress_ffmpeg(proc, cut: float, progress, cancel) -> bool:
    """Читает -progress ffmpeg до конца. False — прервано отменой."""
    for raw in proc.stdout:
        if cancel and cancel():
            proc.terminate()
            proc.wait()
            return False
        line = raw.decode("ascii", "replace").strip()
        if progress and line.startswith("out_time_us=") and cut > 0:
            try:
                done = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            progress(_FFMPEG_SHARE * min(1.0, max(0.0, done / cut)))
    proc.wait()
    return True


_MKV_PROGRESS = re.compile(r"(?:Progress:|#GUI#progress)\s*(\d+)%")


def _run_stats(mkvpropedit: str, path: Path, progress, cancel) -> tuple[bool, str]:
    """Пересчёт статистики дорожек. (успех, текст ошибки); отмена — (False, '')."""
    proc = subprocess.Popen([mkvpropedit, str(path), "--add-track-statistics-tags"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=_CREATE_NO_WINDOW)
    out: list[str] = []
    buf = b""
    while True:
        chunk = proc.stdout.read1(256)   # что уже есть, не дожидаясь 256 байт
        if not chunk:
            break
        if cancel and cancel():
            proc.terminate()
            proc.wait()
            return False, ""
        buf += chunk
        # mkvpropedit обновляет процент через \r, а не перевод строки
        *lines, buf = re.split(rb"[\r\n]", buf)
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            m = _MKV_PROGRESS.search(line)
            if m and progress:
                progress(_FFMPEG_SHARE + (1 - _FFMPEG_SHARE) * int(m.group(1)) / 100)
            elif line.strip():
                out.append(line)
    proc.wait()
    return proc.returncode in (0, 1), "\n".join(out[-5:])


def trim_file(path, tools: Tools, progress=None, cancel=None) -> TrimResult:
    """Обрезает всё, что идёт после конца видео, и ставит результат на место файла.

    progress(доля 0..1) и cancel() -> bool вызываются из того же потока, что и
    сама обрезка. Оригинал заменяется только после сверки; при любой неудаче или
    отмене он остаётся как был, временный файл удаляется.
    """
    path = Path(path)
    src = identify(tools.mkvmerge, path)
    if src is None:
        return TrimResult(False, "mkvmerge не прочитал файл")
    old = (src.get("container", {}).get("properties", {}) or {}).get("duration")
    old = old / 1e9 if isinstance(old, (int, float)) else None
    def ends(kind):
        return [core.tag_seconds((t.get("properties") or {}).get("tag_duration"))
                for t in src.get("tracks", []) if t.get("type") == kind]
    if not ends("video") or not old:
        return TrimResult(True, "нет видео — резать не по чему", skipped=True)
    near = max([x for x in ends("video") if x] or [old])
    video_end = probe_video_end(tools.ffprobe, path, near)
    if video_end is None:
        return TrimResult(False, "не удалось найти конец видео")
    cut = math.ceil(video_end * 1000) / 1000
    # Конец звука — по статистике; без неё мерилом служит длительность файла.
    audio_end = max([x for x in ends("audio") if x] or [old])
    if audio_end - cut <= TAIL_MIN_S:
        return TrimResult(True, "хвоста нет", skipped=True, cut=cut, old_duration=old)

    indexes = stream_indexes(tools.ffprobe, path)
    if len(indexes) != len(src.get("tracks", [])):
        return TrimResult(False, f"ffmpeg видит {len(indexes)} дорожек из "
                                 f"{len(src.get('tracks', []))} — такой файл не трогаю")

    tmp = tmp_path(path)
    with tempfile.TemporaryDirectory(prefix="mkv-trim-") as work:
        work = Path(work)
        # Главы и вложения — из оригинала, до перепаковки: упорядоченные главы
        # собирают фильм из кусков, и резать такой файл по концу видео нельзя.
        chapters_xml, chapters_left = None, 0
        if _chapter_count(src):
            xml_file = work / "chapters.xml"
            _run([tools.mkvextract, str(path), "chapters", str(xml_file)])
            if not xml_file.is_file() or not xml_file.stat().st_size:
                return TrimResult(False, "не удалось прочитать главы")
            xml_text = xml_file.read_text(encoding="utf-8")
            if chapters_ordered(xml_text):
                return TrimResult(False, "упорядоченные главы — такой файл не трогаю")
            clamped, chapters_left = clamp_chapters(xml_text, cut)
            chapters_xml = work / "chapters-cut.xml"
            chapters_xml.write_text(clamped, encoding="utf-8")
        attachments = src.get("attachments", []) or []
        att_args: list[str] = []
        if attachments:
            specs = [f"{a['id']}:{work / ('att-%d' % a['id'])}" for a in attachments]
            _run([tools.mkvextract, str(path), "attachments", *specs])
            for a in attachments:
                file = work / ("att-%d" % a["id"])
                if not file.is_file():
                    return TrimResult(False, f"не удалось извлечь вложение {a.get('file_name')}")
                att_args += ["--attachment-name", a.get("file_name") or file.name,
                             "--attachment-mime-type", a.get("content_type") or "application/octet-stream"]
                if a.get("description"):
                    att_args += ["--attachment-description", a["description"]]
                uid = (a.get("properties") or {}).get("uid")
                if uid:
                    att_args += ["--attachment-uid", str(uid)]
                att_args += ["--add-attachment", str(file)]

        def fail(message: str) -> TrimResult:
            _remove(tmp)
            return TrimResult(False, message, cut=cut, old_duration=old)

        err_file = work / "ffmpeg.err"
        with open(err_file, "wb") as err:
            proc = subprocess.Popen(ffmpeg_args(tools.ffmpeg, path, tmp, cut, indexes),
                                    stdout=subprocess.PIPE, stderr=err,
                                    creationflags=_CREATE_NO_WINDOW)
            finished = _progress_ffmpeg(proc, cut, progress, cancel)
        if not finished:
            _remove(tmp)
            return TrimResult(False, "отменено", cancelled=True, cut=cut, old_duration=old)
        if proc.returncode != 0:
            tail = err_file.read_text(encoding="utf-8", errors="replace").strip()[-300:]
            return fail(f"ffmpeg: {tail or 'код ' + str(proc.returncode)}")

        new = identify(tools.mkvmerge, tmp)
        if new is None:
            return fail("mkvmerge не прочитал результат")
        edit = restore_args(src, new) + att_args
        if chapters_xml is not None:
            edit += ["--chapters", str(chapters_xml)]
        if edit:
            cp = _run([tools.mkvpropedit, str(tmp), *edit])
            if cp.returncode not in (0, 1):
                return fail("mkvpropedit: " + _text(cp.stdout).strip()[-300:])

        ok, err_text = _run_stats(tools.mkvpropedit, tmp, progress, cancel)
        if cancel and cancel():
            _remove(tmp)
            return TrimResult(False, "отменено", cancelled=True, cut=cut, old_duration=old)
        if not ok:
            return fail("статистика дорожек: " + err_text)

        new = identify(tools.mkvmerge, tmp)
        if new is None:
            return fail("mkvmerge не прочитал результат")
        problems = compare(src, new, cut, chapters_left)
        if problems:
            return fail("не сошлось с оригиналом: " + "; ".join(problems[:4]))

    try:
        shutil.copymode(path, tmp)       # права как у оригинала (на шаре может не выйти)
    except OSError:
        pass
    try:
        os.replace(tmp, path)
    except OSError as ex:
        _remove(tmp)
        return TrimResult(False, f"не удалось заменить файл: {ex}", cut=cut, old_duration=old)
    new_dur = (new.get("container", {}).get("properties", {}) or {}).get("duration")
    new_dur = new_dur / 1e9 if isinstance(new_dur, (int, float)) else cut
    if progress:
        progress(1.0)
    return TrimResult(True, f"хвост {audio_end - cut:.0f} с отрезан", cut=cut,
                      old_duration=old, new_duration=new_dur)
