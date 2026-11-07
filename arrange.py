"""Разложить тайтл целиком: язык имени, картинки, план, .nfo — без страницы.

Вкладка «Медиатека» делает то же по шагам, с выбором и подтверждениями;
автоматика (`autopilot.py`) — сама, по раздаче qBittorrent. Общая часть живёт
здесь, чтобы обе раскладывали одинаково.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import artwork
import library
import metaconf
import metadata
import nfo
import qbit

# Виды картинок тайтла; постеры сезонов выбираются отдельно, по сезонам.
ART_KINDS = (metadata.ART_POSTER, metadata.ART_FANART, metadata.ART_LOGO)


def resolve_name_language(info, settings) -> None:
    """Режим имени тайтла (личное исключение или общая настройка) → в `info`."""
    info.name_language = metadata.resolve_name_language(
        metaconf.name_language_for(settings, info.kind, info.tmdb_id),
        info.original_language, settings.meta_language)


def art_languages(langs, original_language: str) -> list[str]:
    """Приоритет языков картинок: псевдоязык «оригинал» → код языка тайтла."""
    out: list[str] = []
    for lang in langs:
        if lang == metaconf.ART_ORIGINAL:
            if not original_language:
                continue
            lang = original_language
        if lang not in out:
            out.append(lang)
    return out


def choose_art(info, langs) -> dict:
    """Лучшая картинка каждого вида и постер каждого сезона по приоритету языков."""
    chosen = {}
    for kind in ART_KINDS:
        best = metadata.best_art(info.art, kind, langs)
        if best:
            chosen[(kind, None)] = best
    for season in metadata.seasons_with_art(info.art):
        best = metadata.best_art(info.art, metadata.ART_SEASON, langs, season=season)
        if best:
            chosen[(metadata.ART_SEASON, season)] = best
    return chosen


def title_watch_state(plan, info) -> dict:
    """Отметки просмотра тайтла — до того, как план тронет файлы.

    У фильма они могут лежать только в старом `Фильм (1994).nfo` от
    tinyMediaManager, а он в этом же прогоне уедет в мусор. Читаем, пока он
    на месте: всё остальное в .nfo придёт заново из TMDb, а просмотр — нет.
    """
    name = "movie.nfo" if info.kind == library.MOVIE else "tvshow.nfo"
    found = [r.src for r in plan.rows if r.src is not None and r.src.name.lower() == name]
    if info.kind == library.MOVIE:
        found += [r.src for r in plan.rows
                  if r.src is not None and r.what == "junk" and r.src.suffix.lower() == ".nfo"]
    for path in found:
        state = nfo.read_watch_state(path)
        if state:
            return state
    return {}


def write_episode_nfo(plan, info, policy: str) -> int:
    if info.kind != library.TV:
        return 0
    written = 0
    for row in plan.rows:
        if row.what != "video" or row.dst is None or row.action == library.A_SKIP:
            continue
        season, numbers = library.parse_episodes(row.dst)
        # В одном файле может лежать несколько серий — тогда и блоков в
        # .nfo должно быть столько же, иначе Kodi потеряет остальные.
        eps = [info.episodes[(season, n)] for n in numbers if (season, n) in info.episodes]
        if not eps:
            continue
        path = row.dst.with_suffix(".nfo")
        if path.exists() and policy == metaconf.POLICY_MISSING:
            continue
        preserve = nfo.read_watch_state(path)
        if row.action == library.A_RENAME and row.src is not None:
            # Имя в раздаче («…WEB-DL.1080p…») — потом по нему видно источник.
            preserve.setdefault("original_filename", row.src.name)
        text = (nfo.make_episode_nfo(info, eps[0], preserve=preserve) if len(eps) == 1
                else nfo.make_episodes_nfo(info, eps, preserve=preserve))
        nfo.write(path, text)
        written += 1
        if len(eps) > 1:
            # Kodi 22 читает у файла из нескольких серий свой .nfo на каждую серию —
            # «<имя файла>-S01E02.nfo» (общий, со всеми сериями, — для Kodi 21 и старше).
            for ep in eps:
                own = row.dst.with_name(f"{row.dst.stem}-S{ep.season:02d}E{ep.episode:02d}.nfo")
                if own.exists() and policy == metaconf.POLICY_MISSING:
                    continue
                nfo.write(own, nfo.make_episode_nfo(info, ep, preserve=nfo.read_watch_state(own)))
                written += 1
    return written


def write_title_nfo(plan, info, policy: str, art_urls: dict, season_urls: dict,
                    preserve: dict) -> int:
    name = "movie.nfo" if info.kind == library.MOVIE else "tvshow.nfo"
    path = plan.root / name
    if path.exists() and policy == metaconf.POLICY_MISSING:
        return 0
    if info.kind == library.MOVIE:
        text = nfo.make_movie_nfo(info, art=art_urls, preserve=preserve)
    else:
        text = nfo.make_tvshow_nfo(info, art=art_urls, season_art=season_urls,
                                   preserve=preserve)
    nfo.write(path, text)
    return 1


@dataclass
class Outcome:
    results: list = field(default_factory=list)      # library.ApplyResult
    nfo: int = 0
    art_done: int = 0
    art_skipped: int = 0
    errors: list[str] = field(default_factory=list)  # для журнала: картинки, папки
    via_qbit: int = 0                                # переименовано через qBittorrent

    @property
    def failed(self) -> list:
        return [r for r in self.results if not r.ok]


def apply(plan, info, chosen: dict, policy: str, delete_junk: bool = False,
          torrents=None, stop=None, progress=None, status=None) -> Outcome:
    """Файлы по местам, затем .nfo серий, картинки и .nfo тайтла.

    `.nfo` тайтла пишется после картинок: в него идут URL именно тех, что легли
    на диск. `torrents` — раздачи qBittorrent в папке тайтла (`qbit.TorrentFiles`).
    `progress(i, total, item)` — ход по файлам, `status(text)` — смена этапа.
    """
    stop = stop or (lambda: False)
    status = status or (lambda text: None)
    out = Outcome()
    preserve = title_watch_state(plan, info)
    if info.kind == library.MOVIE:
        video = next((r for r in plan.rows if r.what == "video" and r.action == library.A_RENAME
                      and r.src is not None), None)
        if video is not None:
            preserve.setdefault("original_filename", video.src.name)
    if torrents is not None:
        status("переименование через qBittorrent…")
    out.results = library.apply_plan(plan, delete_junk=delete_junk, stop=stop,
                                     progress=progress, files=torrents)
    if torrents is not None:
        out.via_qbit = sum(1 for r in out.results if r.ok and torrents.owns(r.row.dst))
        # Папки под .nfo и картинки — с правами родителя: созданные от root,
        # они не пустили бы qBittorrent положить туда ещё не скачанные серии.
        dirs = {plan.root} | {r.dst.parent for r in plan.rows if r.what == "video"
                              and r.dst is not None and r.action != library.A_SKIP}
        for d in sorted(dirs):
            try:
                qbit.make_dirs(d)
            except OSError as e:
                out.errors.append(f"{d}: {e}")
    if not stop():
        status("запись .nfo…")
        out.nfo = write_episode_nfo(plan, info, policy)
    if not stop():
        tasks = artwork.plan_art(plan.root, chosen, policy)
        art_results = artwork.download(tasks, stop=stop, progress=progress)
        out.art_done = sum(1 for r in art_results if r.ok and not r.skipped)
        out.art_skipped = sum(1 for r in art_results if r.skipped)
        out.errors += [f"{r.task.dest.name}: {r.error}" for r in art_results if not r.ok]
        art_urls, season_urls = artwork.chosen_urls(tasks)
        out.nfo += write_title_nfo(plan, info, policy, art_urls, season_urls, preserve)
    return out
