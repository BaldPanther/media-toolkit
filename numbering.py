"""Сезоны, как их пишут трекеры, ↔ серии TMDb.

У большинства сериалов это одно и то же. Исключение — аниме со сквозной
нумерацией: в TMDb «Монолог фармацевта» — один сезон из 60 серий, а трекеры
пишут «ТВ-3», «3 сезон: 1-5 из 12». Официальное деление на сезоны TMDb держит
в episode groups («Seasons»: Season 3 → S01E49–60). Оно и становится сезонами
для поиска и слежения.

В медиатеку серия ложится так, как там уже принято у этого тайтла: сквозным
номером TMDb (S01E50) или по сезонам трекеров (S03E02 — так разложен, например,
«Solo Leveling» вкладкой «Медиатека»). Тайтла в медиатеке ещё нет — по сезонам
трекеров.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import net
import tmdb

# Подгруппы-сезоны: «Season 3», «Сезон 3». «Cour 2», «Part 1», арки — не сезоны трекеров.
_SEASON_NAME = re.compile(r"(?i)\b(?:season|сезон)\b\D*(\d{1,2})|(\d{1,2})\D*\b(?:season|сезон)\b")
_SPECIALS_NAME = re.compile(r"(?i)special|спец")


@dataclass
class Span:
    """Сезон `season` (как у трекеров) — серии TMDb `first`…`first + count - 1`
    сезона `tmdb_season`."""
    season: int
    tmdb_season: int
    first: int
    count: int
    year: int | None = None

    def holds(self, tmdb_season: int, episode: int) -> bool:
        return self.tmdb_season == tmdb_season and self.first <= episode < self.first + self.count


@dataclass
class Numbering:
    spans: list[Span] = field(default_factory=list)
    grouped: bool = False      # сезоны из episode group, а не обычные сезоны TMDb
    source: str = ""           # её название в TMDb («Seasons»)

    def span(self, season: int) -> Span | None:
        return next((s for s in self.spans if s.season == season), None)

    def tmdb_seasons(self) -> set[int]:
        return {s.tmdb_season for s in self.spans}

    def to_tmdb(self, season: int, episode: int) -> tuple[int, int]:
        s = self.span(season)
        if s is None:
            return season, episode
        return s.tmdb_season, s.first + episode - 1

    def from_tmdb(self, tmdb_season: int, episode: int) -> tuple[int, int]:
        s = next((s for s in self.spans if s.holds(tmdb_season, episode)), None)
        if s is None:
            return tmdb_season, episode
        return s.season, episode - s.first + 1

    def from_absolute(self, n: int, start: int = 1) -> tuple[int, int] | None:
        """Сквозной номер серии («49» в раздаче «1-3 сезоны: 1-49 из 72» или файл
        «Show - 49.mkv») → (сезон, серия в нём). Отсчёт — с начала сезона `start`."""
        for s in sorted(self.spans, key=lambda s: s.season):
            if s.season < start or s.season == 0:
                continue
            if n <= s.count:
                return s.season, n
            n -= s.count
        return None

    def before(self, season: int) -> int:
        """Сколько серий во всех сезонах до этого."""
        return sum(s.count for s in self.spans if 0 < s.season < season)

    def place(self, n: int, season: int | None = None) -> tuple[int, int]:
        """Номер серии из файла раздачи → (сезон, серия) в нумерации трекеров.

        `season` — сезон, к которому файл относит раздача (тег S03E02, папка
        «Season 3», раздача одного сезона); None — неизвестен, номер сквозной.
        Номер больше самого сезона и больше всех серий до него — тоже сквозной:
        у VCB-Studio в папке второго сезона лежат «25»…«48». Серия за концом
        известных TMDb («S03E13» при 12 в сезоне) остаётся в своём сезоне.
        """
        span = self.span(season) if season else None
        if span is not None and (n <= span.count or n <= self.before(season)):
            return season, n
        pos = self.from_absolute(n)
        if pos is not None:
            return pos
        last = max((s for s in self.spans if s.season), key=lambda s: s.season, default=None)
        if last is None:
            return season or 1, n
        return last.season, n - self.before(last.season)


def regular(seasons) -> Numbering:
    """Обычный сериал: сезоны трекеров = сезоны TMDb. `seasons` — [(номер, серий, год)]."""
    return Numbering([Span(n, n, 1, count, year) for n, count, year in seasons])


def _year(date) -> int | None:
    m = re.match(r"(\d{4})", str(date or ""))
    return int(m.group(1)) if m else None


def _spans_of(detail: dict) -> list[Span] | None:
    """Подгруппы episode group → сезоны. None — группа не про сезоны или с дырами:
    тогда лучше обычные сезоны TMDb, чем кривое сопоставление."""
    spans = []
    for sub in detail.get("groups") or []:
        name = str(sub.get("name") or "")
        eps = sorted(((int(e.get("season_number") or 0), int(e.get("episode_number") or 0),
                       e.get("air_date")) for e in sub.get("episodes") or []))
        if not eps or _SPECIALS_NAME.search(name) or all(e[0] == 0 for e in eps):
            continue
        m = _SEASON_NAME.search(name)
        if not m:
            return None
        tmdb_season = eps[0][0]
        numbers = [e[1] for e in eps]
        if any(e[0] != tmdb_season for e in eps) or numbers != list(range(numbers[0], numbers[-1] + 1)):
            return None
        spans.append(Span(int(m.group(1) or m.group(2)), tmdb_season, numbers[0], len(numbers),
                          _year(eps[0][2])))
    spans.sort(key=lambda s: s.season)
    if len({s.season for s in spans}) != len(spans):
        return None
    # Без дыр: внутри сезона TMDb сезоны трекеров идут подряд с первой серии.
    by_tmdb: dict[int, list[Span]] = {}
    for s in spans:
        by_tmdb.setdefault(s.tmdb_season, []).append(s)
    for group in by_tmdb.values():
        expect = 1
        for s in sorted(group, key=lambda s: s.first):
            if s.first != expect:
                return None
            expect = s.first + s.count
    return spans


def grouped(tmdb_id: int, api_key: str, seasons, limit: int = 4) -> Numbering | None:
    """Деление на сезоны из episode groups — если у трекеров сезонов больше, чем в
    TMDb. `seasons` — обычные сезоны TMDb [(номер, серий, год)]."""
    counts = {n: count for n, count, _ in seasons}
    try:
        groups = tmdb.episode_groups(tmdb_id, api_key)
    except net.NetError:
        return None
    best: list[Span] | None = None
    source = ""
    for meta in groups[:limit]:
        if not meta.get("id") or int(meta.get("group_count") or 0) <= len(counts):
            continue
        try:
            detail = tmdb.episode_group(meta["id"], api_key)
        except net.NetError:
            continue
        spans = _spans_of(detail)
        if not spans or len(spans) <= len(counts) or not {s.tmdb_season for s in spans} <= set(counts):
            continue
        if best is None or (len(spans), sum(s.count for s in spans)) > (len(best), sum(s.count for s in best)):
            best, source = spans, str(meta.get("name") or detail.get("name") or "")
    if best is None:
        return None
    # Новые серии TMDb добавляет в свой сезон раньше, чем кто-то правит группу:
    # всё после последнего сезона трекеров — его продолжение (вторая часть сезона).
    for tmdb_season, total in counts.items():
        last = max((s for s in best if s.tmdb_season == tmdb_season), key=lambda s: s.first,
                   default=None)
        if last is not None and last.first + last.count - 1 < total:
            last.count = total - last.first + 1
    return Numbering(best, grouped=True, source=source)


def load(details: dict, tmdb_id: int, api_key: str) -> Numbering:
    """Нумерация сериала по его карточке TMDb (`tmdb.titles`/`tmdb.tv`)."""
    seasons = [(int(s["season_number"]), int(s.get("episode_count") or 0), _year(s.get("air_date")))
               for s in details.get("seasons") or []
               if isinstance(s.get("season_number"), int) and s["season_number"] > 0]
    return grouped(tmdb_id, api_key, seasons) or regular(seasons)


def for_title(tmdb_id: int, api_key: str) -> Numbering:
    """Нумерация сериала по номеру TMDb — там, где карточки под рукой нет (раскладка)."""
    return load(tmdb.tv(tmdb_id, api_key), tmdb_id, api_key)


def library_grouped(numbering: Numbering, disk_episodes) -> bool:
    """Раскладывать ли по сезонам трекеров (S02E01), а не сквозными номерами TMDb (S01E26).

    Как уже лежит тайтл: сезон, которого нет в TMDb («Season 02» у «Solo Leveling» при
    одном сезоне TMDb), — по сезонам трекеров; серия, у которой номера в двух нумерациях
    разные (S01E26 при 25 сериях в первом сезоне трекеров), — сквозными. Тайтла в
    медиатеке нет или лежит только то, что в обеих нумерациях одинаково (первый сезон), —
    по сезонам трекеров: так делят раздачи. `disk_episodes` — (сезон, серия) с диска."""
    if not numbering.grouped:
        return False
    tmdb_seasons = numbering.tmdb_seasons()
    episodes = [(s, e) for s, e in disk_episodes if s]
    if any(s not in tmdb_seasons for s, _ in episodes):
        return True
    return all(numbering.from_tmdb(s, e) == (s, e) for s, e in episodes)


def to_library(numbering: Numbering, season: int, episode: int, grouped_disk: bool) -> tuple[int, int]:
    """Серия в нумерации трекеров → номер в медиатеке этого тайтла."""
    if season == 0 or not numbering.grouped or grouped_disk:
        return season, episode
    return numbering.to_tmdb(season, episode)


def from_library(numbering: Numbering, season: int, episode: int, grouped_disk: bool) -> tuple[int, int]:
    if season == 0 or not numbering.grouped or grouped_disk:
        return season, episode
    return numbering.from_tmdb(season, episode)
