from pathlib import Path

import pytest

import library
import metadata
import numbering
import packs


@pytest.mark.parametrize("name, season", [
    ("[VCB-Studio] Show [S02 SPs 2020-2021]", 2),
    ("[VCB-Studio] Show [S01 Director's Cut SPs 2016]", 1),
    ("[VCB-Studio] Show Memory Snow [OVA-1 SPs 2018]", 0),
    ("[VCB-Studio] Show [BDRip 1080p x265 FLAC]", None),
    ("Show.S02.1080p.WEB-DL", 2),
    ("Show 2nd Season", 2),
    ("Сезон 3", 3),
    ("[ТВ-2] Show", 2),
    ("Show S01-S03", None),
    ("Show S01 - S03", None),
    ("Show Season 1-3", None),
    ("Show s01-03", None),
    ("Show.S02E01", None),
    ("Extras", None),
])
def test_season_of_tag(name, season):
    assert library.season_of_tag(name) == season


def test_merge_by_runtime_like_director_cut():
    # Первая ТВ-серия — 50 минут, остальные по 25: 25 серий в 13 файлов.
    groups = packs.merge([50] + [25] * 24, 13)
    assert groups[0] == [1] and groups[1] == [2, 3] and groups[-1] == [24, 25]
    assert [e for g in groups for e in g] == list(range(1, 26))


def test_merge_refuses_to_guess():
    assert packs.merge([25] * 24 + [0], 13) is None      # длина серии неизвестна
    assert packs.merge([25] * 10 + [200], 5) is None     # куски ровными не выходят
    assert packs.merge([25] * 4, 4) is None              # файлов не меньше серий


def _scan(tmp_path, files: dict[str, int]):
    root = tmp_path / "pack"
    return library.scan_folder(root, {root / rel: size for rel, size in files.items()})


def test_layout_shifts_own_numbering_after_short_first_season(tmp_path):
    files = {f"[S01]/Show {n:02d}.mkv": 100 for n in range(1, 14)}
    files.update({f"[S02]/Show {n:02d}.mkv": 100 for n in range(14, 39)})
    nb = numbering.Numbering([numbering.Span(1, 1, 1, 25), numbering.Span(2, 1, 26, 25)])
    got = packs.layout(_scan(tmp_path, files), nb)
    by_name = {p.name: v for p, v in got.items()}
    assert by_name["Show 14.mkv"] == (2, [1]) and by_name["Show 38.mkv"] == (2, [25])
    assert "Show 01.mkv" not in by_name          # первый сезон и так верный; не режиссёрская


def test_layout_keeps_tracker_absolute_numbering(tmp_path):
    # VCB без режиссёрской: во втором сезоне «26»…«50» — сдвиг на серии первого.
    files = {f"[S02]/Show {n:02d}.mkv": 100 for n in range(26, 51)}
    nb = numbering.Numbering([numbering.Span(1, 1, 1, 25), numbering.Span(2, 1, 26, 25)])
    got = packs.layout(_scan(tmp_path, files), nb)
    assert sorted(got.values())[0] == (2, [1]) and sorted(got.values())[-1] == (2, [25])


def test_layout_leaves_partial_season_alone(tmp_path):
    # Идущий сезон: «05»…«08» в папке сезона — это и есть номера серий.
    files = {f"[S02]/Show {n:02d}.mkv": 100 for n in range(5, 9)}
    nb = numbering.Numbering([numbering.Span(1, 1, 1, 12), numbering.Span(2, 1, 13, 12)])
    assert packs.layout(_scan(tmp_path, files), nb) == {}


def test_minis_by_size(tmp_path):
    scan = _scan(tmp_path, {
        "[S01]/Show 01.mkv": 1000, "[S01]/Show 02.mkv": 1100,
        "[S01]/SPs/Show NCOP1.mkv": 90, "[OVA]/Show OVA.mkv": 900})
    assert {p.name for p in packs.minis(scan)} == {"Show NCOP1.mkv"}


def test_specials_match_title_or_go_after_tmdb(tmp_path):
    scan = _scan(tmp_path, {
        "[S01]/Show 01.mkv": 1000, "[S01]/Show 02.mkv": 1000,
        "Show Frozen Bond [OVA-2 2019]/Show Frozen Bond.mkv": 900,
        "Show Memory Snow [OVA-1 2018]/Show Memory Snow.mkv": 900,
        "Show Memory Snow [OVA-1 2018]/SPs/Show Memory Snow Manner Movie.mkv": 40})
    info = metadata.MediaInfo(kind=library.TV, tmdb_id=1, title="Show")
    info.episodes[(0, 1)] = metadata.EpisodeInfo(0, 1, title_en="Memory Snow: Manner Movie")
    info.episodes[(0, 2)] = metadata.EpisodeInfo(0, 2, title_en="Recap")
    got = {p.name: v for p, v in packs.specials(scan, info, taken={3}).items()}
    assert got["Show Memory Snow Manner Movie.mkv"] == (0, [1])
    # Не совпало — после спецвыпусков TMDb и уже лежащих в медиатеке, по году.
    assert got["Show Memory Snow.mkv"] == (0, [4])
    assert got["Show Frozen Bond.mkv"] == (0, [5])
    assert info.episodes[(0, 4)].title_en == "Memory Snow"
