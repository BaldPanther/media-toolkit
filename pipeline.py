"""Запись .edl серия за серией: обрезка хвоста → .edl → главы.

Раньше шаги шли пачками: хвост у всех серий, потом .edl у всех, потом главы.
Оборвалась сеть посреди обрезки — и у уже обрезанных серий оставались старые
.edl с концом титров за новым концом файла (Altered Carbon E04–E08, 2026-09-23).
Теперь серия проходит все шаги, прежде чем взяться за следующую: где бы работа
ни прервалась, готовые серии готовы целиком, остальные не тронуты.

Всё здесь выполняется в рабочем потоке фоновой операции (web/jobs.py).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

import chapters
import core
import edl
import trim


@dataclass
class EpisodeRun:
    """Что вышло с одной серией."""
    trim: trim.TrimResult | None = None   # None — хвост не резали
    fresh: core.MkvFile | None = None     # скан после обрезки: длительность и хвост новые
    edl: Path | None = None               # записанный .edl; None — нечего было писать
    edl_error: str = ""
    chapters: int | None = None           # сколько глав записано; None — главы не писали
    chapters_failed: bool = False
    cancelled: bool = False               # отменили — дальше серии не идём


def run_episode(ep: edl.EpisodeEdl, pad: edl.Padding, keep: bool, to_end: bool, *,
                tools: trim.Tools | None = None, propedit: str | None = None,
                progress=None, cancel=None, waiting=None, trimmed=None) -> EpisodeRun:
    """Проводит серию через все шаги.

    tools — резать хвост (иначе не режем), propedit — писать главы. Сама ep не
    меняется: новую длительность в список кладёт главный поток (по run.fresh).
    waiting(on, шаг) — ждём сеть или дождались; шаг — "trim", "edl", "chapters".
    trimmed(run) — обрезка кончилась (run.trim, run.fresh уже есть): сказать об
    этом сразу, а не после .edl и глав.

    Отмена бросает только обрезку — файл тогда как был, и .edl ему не нужен.
    Обрезанной серии .edl и главы дописываются и после отмены: это секунды, а
    без них обрезанный файл остался бы со старым .edl.
    """
    run = EpisodeRun()
    work = ep
    if tools is not None:
        run.trim = trim.trim_file_retrying(
            ep.path, tools, progress, cancel,
            waiting=waiting and (lambda on: waiting(on, "trim")))
        if run.trim.ok and not run.trim.skipped:
            run.fresh = core.scan_file(tools.mkvmerge, ep.path)
            work = copy.copy(ep)
            # Скан мог не пройти, если сеть пропала сразу после замены; длину
            # нового файла trim_file и сам измерил перед заменой.
            work.duration = (run.fresh.duration if not run.fresh.error and run.fresh.duration
                             else run.trim.new_duration)
        if trimmed:
            trimmed(run)
        if run.trim.cancelled:
            run.cancelled = True
            return run

    def write_edl():
        try:
            return True, edl.build_and_write(work, pad, keep, to_end)
        except OSError as ex:
            return False, ex

    value, gave_up = trim.retry_on_drop(ep.path, write_edl, cancel,
                                        waiting and (lambda on: waiting(on, "edl")))
    if isinstance(value, OSError):
        run.edl_error = value.strerror or str(value)
    else:
        run.edl = value
    if gave_up:
        run.cancelled = True
        return run

    if propedit is not None:
        points = chapters.build_points(work, pad, keep, to_end)
        if points:
            def write_chapters():
                ok = chapters.write_chapters(ep.path, points, propedit)
                return ok, ok

            ok, gave_up = trim.retry_on_drop(ep.path, write_chapters, cancel,
                                             waiting and (lambda on: waiting(on, "chapters")))
            run.chapters, run.chapters_failed = (len(points), False) if ok else (None, True)
            run.cancelled = gave_up
    return run
