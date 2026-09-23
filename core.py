"""Логика чтения и смены дорожек по умолчанию в MKV-файлах.

Опирается на MKVToolNix:
  - mkvmerge -J  — идентификация дорожек (JSON);
  - mkvpropedit  — смена флага flag-default прямо в заголовке (мгновенно, без ремукса).

Модуль не зависит от GUI и пригоден для запуска из командной строки:
    python core.py "<папка>" [--recursive]
выводит отчёт по дорожкам (только чтение, ничего не меняет).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Поиск инструментов MKVToolNix
# --------------------------------------------------------------------------- #

# Запасные папки на случай, если инструментов нет в PATH.
_MKV_DIRS = (
    [r"C:\Program Files\MKVToolNix", r"C:\Program Files (x86)\MKVToolNix"]
    if os.name == "nt"
    else ["/opt/homebrew/bin", "/usr/local/bin"]  # brew: Apple Silicon и Intel
)

# Скрывает всплывающие окна консоли при запуске из GUI на Windows.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_tool(name: str) -> str | None:
    exe = name + ".exe" if os.name == "nt" and not name.lower().endswith(".exe") else name
    found = shutil.which(name) or shutil.which(exe)
    if found:
        return found
    for d in _MKV_DIRS:
        cand = os.path.join(d, exe)
        if os.path.isfile(cand):
            return cand
    return None


def find_tools() -> tuple[str, str]:
    """Возвращает (mkvmerge, mkvpropedit) или бросает FileNotFoundError."""
    merge = find_tool("mkvmerge")
    propedit = find_tool("mkvpropedit")
    missing = [n for n, v in (("mkvmerge", merge), ("mkvpropedit", propedit)) if not v]
    if missing:
        raise FileNotFoundError(
            "Не найдены инструменты MKVToolNix: " + ", ".join(missing) + ".\n"
            "Установите MKVToolNix ("
            + ("choco install mkvtoolnix" if os.name == "nt" else "brew install mkvtoolnix")
            + ") или добавьте его папку в PATH."
        )
    return merge, propedit


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, creationflags=_CREATE_NO_WINDOW)


def _decode(b: bytes) -> str:
    return b.decode("utf-8", "replace") if b else ""


# --------------------------------------------------------------------------- #
# Модель данных
# --------------------------------------------------------------------------- #

@dataclass
class Track:
    id: int                 # порядковый id дорожки в mkvmerge (0-based)
    uid: int | None         # properties.uid — для точного селектора track:=uid
    number: int | None      # properties.number — Matroska TrackNumber
    type: str               # "audio" | "subtitles" | "video"
    codec: str
    language: str
    name: str               # track_name, "" если нет
    default: bool
    forced: bool
    # Длительность из статистики дорожки (тег DURATION от mkvmerge/ffmpeg), если
    # она есть. По ней видно, что звук длиннее видео — см. trim.py.
    duration: float | None = None

    def label(self) -> str:
        return self.name.strip() if self.name.strip() else f"[{self.language or 'und'}]"


@dataclass
class MkvFile:
    path: Path
    tracks: list[Track] = field(default_factory=list)
    error: str = ""
    duration: float | None = None   # длительность в секундах (из mkvmerge -J), если известна
    chapters: int = 0               # сколько глав в файле (оттуда же, бесплатно при скане)

    @property
    def audio(self) -> list[Track]:
        return [t for t in self.tracks if t.type == "audio"]

    @property
    def subtitles(self) -> list[Track]:
        return [t for t in self.tracks if t.type == "subtitles"]


# --------------------------------------------------------------------------- #
# Идентичность дорожек (совпадение «по названию», а не по номеру)
# --------------------------------------------------------------------------- #

def audio_identity(t: Track) -> tuple:
    """Ключ совпадения аудио: название + язык.

    Язык входит в ключ, чтобы различать одноимённые дорожки разных языков
    (например, две дорожки «Surround» — русская и английская): иначе они
    схлопывались бы в один пункт и выбрать можно было бы только первую из них.
    Язык — стабильное свойство контента, поэтому совпадение «одна озвучка во
    всех сериях» сохраняется так же, как при сравнении по одному названию.
    """
    lang = (t.language or "und").casefold()
    name = t.name.strip().casefold()
    if name:
        return ("name", name, lang)
    return ("lang", lang)


def audio_label(t: Track) -> str:
    lang = t.language or "und"
    if t.name.strip():
        return f"{t.name.strip()} [{lang}]"
    return f"без названия [{lang}]"


def sub_identity(t: Track) -> tuple:
    """Ключ совпадения субтитров: язык + название (у субтитров имя часто пустое)."""
    return ("sub", (t.language or "und").casefold(), t.name.strip().casefold())


def sub_label(t: Track) -> str:
    base = t.language or "und"
    if t.name.strip():
        return f"{base} — {t.name.strip()}"
    return base


def audio_signature(f: MkvFile) -> tuple:
    return tuple((t.name.strip(), t.language) for t in f.audio)


# --------------------------------------------------------------------------- #
# Сканирование
# --------------------------------------------------------------------------- #

def tag_seconds(value) -> float | None:
    """Тег DURATION ('00:49:38.768000000') → секунды. Нет тега или мусор → None."""
    if not isinstance(value, str):
        return None
    try:
        h, m, s = value.strip().replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return None


def scan_file(mkvmerge: str, path: Path) -> MkvFile:
    cp = _run([mkvmerge, "-J", str(path)])
    if cp.returncode not in (0, 1):  # 1 = предупреждения, тоже валидный JSON
        return MkvFile(path, error=_decode(cp.stderr) or f"mkvmerge код {cp.returncode}")
    try:
        data = json.loads(_decode(cp.stdout))
    except json.JSONDecodeError as e:
        return MkvFile(path, error=f"ошибка JSON: {e}")

    tracks: list[Track] = []
    for t in data.get("tracks", []):
        props = t.get("properties", {}) or {}
        tracks.append(Track(
            id=t.get("id", -1),
            uid=props.get("uid"),
            number=props.get("number"),
            type=t.get("type", ""),
            codec=t.get("codec", ""),
            language=props.get("language", "") or "",
            name=props.get("track_name", "") or "",
            default=bool(props.get("default_track", False)),
            forced=bool(props.get("forced_track", False)),
            duration=tag_seconds(props.get("tag_duration")),
        ))

    # Длительность контейнера (mkvmerge отдаёт наносекунды) — нужна для конца титров.
    cprops = (data.get("container", {}) or {}).get("properties", {}) or {}
    dur_ns = cprops.get("duration")
    duration = dur_ns / 1e9 if isinstance(dur_ns, (int, float)) else None
    # Число глав mkvmerge отдаёт тем же вызовом — берём заодно, отдельный запуск
    # mkvextract нужен только там, где важны их названия (см. chapters.py).
    chapters = sum((c or {}).get("num_entries", 0) for c in data.get("chapters", []) or [])
    return MkvFile(path, tracks, duration=duration, chapters=chapters)


def list_mkv(folder, recursive: bool) -> list[Path]:
    """Список MKV по пути. Путь может быть и одним файлом — вернётся он сам.

    Одиночный файл — это фильм, лежащий в корне библиотеки без своей папки.
    Дорожки и субтитры в нём настраиваются ровно так же, как в сериале, так
    что запрещать такой выбор незачем.
    """
    path = Path(folder)
    if path.is_file():
        return [path] if path.suffix.lower() == ".mkv" else []
    globber = path.rglob if recursive else path.glob
    return sorted(p for p in globber("*.mkv") if p.is_file())


def scan_folder(folder, recursive: bool, progress=None, stop=None) -> list[MkvFile]:
    """Читает дорожки всех MKV папки; порядок результата — как в list_mkv.

    mkvmerge -J — короткие внешние процессы, поэтому файлы обрабатываются пулом
    потоков (GIL не мешает: потоки ждут subprocess). progress(i, total, path)
    вызывается по завершении каждого файла, i — число уже готовых (1..total).
    stop() -> True прерывает работу: возвращается прочитанное к этому моменту.
    """
    mkvmerge, _ = find_tools()
    paths = list_mkv(folder, recursive)
    total = len(paths)
    results: dict[int, MkvFile] = {}
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as ex:
        futures = {ex.submit(scan_file, mkvmerge, p): i for i, p in enumerate(paths)}
        done = 0
        for fut in as_completed(futures):
            if stop and stop():
                for f in futures:
                    f.cancel()
                break
            i = futures[fut]
            results[i] = fut.result()
            done += 1
            if progress:
                progress(done, total, paths[i])
    return [results[i] for i in sorted(results)]


# --------------------------------------------------------------------------- #
# Агрегация: какие дорожки есть и в скольких файлах
# --------------------------------------------------------------------------- #

@dataclass
class Option:
    key: tuple
    label: str
    count: int          # в скольких файлах присутствует
    languages: set = field(default_factory=set)


def _aggregate(files, tracks_of, identity, labeller) -> tuple[list[Option], int]:
    total = sum(1 for f in files if not f.error)
    opts: dict[tuple, Option] = {}
    for f in files:
        if f.error:
            continue
        seen = set()
        for t in tracks_of(f):
            k = identity(t)
            if k in seen:          # один файл считаем один раз на идентичность
                continue
            seen.add(k)
            o = opts.get(k)
            if o is None:
                o = opts[k] = Option(k, labeller(t), 0)
            o.count += 1
            if t.language:
                o.languages.add(t.language)
    # сортировка: сперва самые распространённые, затем по алфавиту
    return sorted(opts.values(), key=lambda o: (-o.count, o.label.casefold())), total


def aggregate_audio(files) -> tuple[list[Option], int]:
    return _aggregate(files, lambda f: f.audio, audio_identity, audio_label)


def aggregate_subtitles(files) -> tuple[list[Option], int]:
    return _aggregate(files, lambda f: f.subtitles, sub_identity, sub_label)


def group_by_audio_signature(files) -> dict[tuple, list[MkvFile]]:
    groups: dict[tuple, list[MkvFile]] = {}
    for f in files:
        if f.error:
            continue
        groups.setdefault(audio_signature(f), []).append(f)
    return groups


# --------------------------------------------------------------------------- #
# Планирование изменений
# --------------------------------------------------------------------------- #

# Сентинел для субтитров: «выключить дефолтные во всех файлах».
SUB_DISABLE = ("__disable__",)


@dataclass
class TrackChange:
    track: Track
    new_default: bool

    @property
    def changed(self) -> bool:
        return self.track.default != self.new_default


@dataclass
class FilePlan:
    file: MkvFile
    audio_changes: list[TrackChange] = field(default_factory=list)
    sub_changes: list[TrackChange] = field(default_factory=list)
    audio_target: Track | None = None
    sub_target: Track | None = None      # None = «выключены» или «не трогаем»
    sub_disabled: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return any(c.changed for c in self.audio_changes + self.sub_changes)


def plan_changes(files, audio_choice, sub_choice) -> list[FilePlan]:
    """audio_choice: ключ из aggregate_audio или None (не трогать аудио).
    sub_choice: ключ из aggregate_subtitles, SUB_DISABLE, или None (не трогать).
    """
    plans: list[FilePlan] = []
    for f in files:
        plan = FilePlan(file=f)
        if f.error:
            plan.warnings.append(f"ошибка чтения: {f.error}")
            plans.append(plan)
            continue

        # --- аудио ---
        if audio_choice is not None:
            target = next((t for t in f.audio if audio_identity(t) == audio_choice), None)
            if target is None:
                plan.warnings.append("нет выбранной аудиодорожки")
            else:
                plan.audio_target = target
                plan.audio_changes = [TrackChange(t, t is target) for t in f.audio]

        # --- субтитры ---
        if sub_choice == SUB_DISABLE:
            plan.sub_disabled = True
            plan.sub_changes = [TrackChange(t, False) for t in f.subtitles]
        elif sub_choice is not None:
            target = next((t for t in f.subtitles if sub_identity(t) == sub_choice), None)
            if target is None:
                plan.warnings.append("нет выбранных субтитров")
            else:
                plan.sub_target = target
                plan.sub_changes = [TrackChange(t, t is target) for t in f.subtitles]

        plans.append(plan)
    return plans


# --------------------------------------------------------------------------- #
# Применение через mkvpropedit
# --------------------------------------------------------------------------- #

def _selector(t: Track) -> str:
    if t.uid:
        return f"track:={t.uid}"
    if t.number:
        return f"track:@{t.number}"
    return f"track:{t.id + 1}"  # последний фолбэк: 1-based порядок


def build_command(mkvpropedit: str, plan: FilePlan) -> list[str] | None:
    """Одна команда mkvpropedit на все изменённые дорожки файла. None — если менять нечего."""
    edits: list[str] = []
    for c in plan.audio_changes + plan.sub_changes:
        if not c.changed:
            continue
        edits += ["--edit", _selector(c.track), "--set", f"flag-default={1 if c.new_default else 0}"]
    if not edits:
        return None
    return [mkvpropedit, str(plan.file.path)] + edits


@dataclass
class ApplyResult:
    plan: FilePlan
    command: list[str] | None = None
    skipped: bool = False
    ok: bool = False
    output: str = ""


def apply_plan(mkvpropedit: str, plan: FilePlan, dry_run: bool = False) -> ApplyResult:
    cmd = build_command(mkvpropedit, plan)
    res = ApplyResult(plan=plan, command=cmd)
    if cmd is None:
        res.skipped = True
        res.ok = True
        res.output = "без изменений"
        return res
    if dry_run:
        res.ok = True
        res.output = "(пробный прогон)"
        return res
    cp = _run(cmd)
    res.ok = cp.returncode < 2  # 0 = ок, 1 = предупреждения (изменения применены), 2 = ошибка
    res.output = (_decode(cp.stdout) + _decode(cp.stderr)).strip()
    return res


# --------------------------------------------------------------------------- #
# CLI-отчёт (только чтение)
# --------------------------------------------------------------------------- #

def _report(folder: str, recursive: bool) -> None:
    files = scan_folder(folder, recursive)
    ok = [f for f in files if not f.error]
    errs = [f for f in files if f.error]
    print(f"Найдено MKV: {len(files)} (успешно прочитано {len(ok)}, ошибок {len(errs)})")

    groups = group_by_audio_signature(ok)
    print(f"Раскладок аудио: {len(groups)}")
    if len(groups) > 1:
        print("  ВНИМАНИЕ: раскладки различаются между файлами!")

    durs = [f.duration for f in ok if f.duration]
    if durs:
        def _hms(s): return f"{int(s // 60)}:{int(s % 60):02d}"
        print(f"Длительность: от {_hms(min(durs))} до {_hms(max(durs))} "
              f"(известна у {len(durs)}/{len(ok)})")

    audio, total = aggregate_audio(ok)
    print(f"\nАудиодорожки (по названию и языку, всего файлов {total}):")
    for o in audio:
        print(f"  [{o.count}/{total}] {o.label}")

    subs, _ = aggregate_subtitles(ok)
    print(f"\nСубтитры:")
    for o in subs:
        print(f"  [{o.count}/{total}] {o.label}")

    for f in errs:
        print(f"\nОШИБКА {f.path.name}: {f.error}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Отчёт по дорожкам MKV (только чтение).")
    ap.add_argument("folder", help="папка с MKV-файлами")
    ap.add_argument("-r", "--recursive", action="store_true", help="включая вложенные папки")
    args = ap.parse_args(argv)
    try:
        find_tools()
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    _report(args.folder, args.recursive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
