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
    found = packs.specials(scan, info, taken={3})
    got = {p.name: v for p, v in found.items()}
    assert got["Show Memory Snow Manner Movie.mkv"] == (0, [1])
    # Не совпало — после спецвыпусков TMDb и уже лежащих в медиатеке, по году.
    assert got["Show Memory Snow.mkv"] == (0, [4])
    assert got["Show Frozen Bond.mkv"] == (0, [5])
    assert info.episodes[(0, 4)].title_en == "Memory Snow"


def test_specials_take_tmdb_movie(tmp_path):
    scan = _scan(tmp_path, {
        "[S01]/Show 01.mkv": 1000, "[S01]/Show 02.mkv": 1000,
        "Show Memory Snow [OVA-1 2018]/Show Memory Snow.mkv": 900,
        "Show Hyouketsu no Kizuna [OVA-2 2019]/Show Hyouketsu no Kizuna.mkv": 900})
    info = metadata.MediaInfo(kind=library.TV, tmdb_id=1, title="Show")
    films = {"Hyouketsu no Kizuna": metadata.EpisodeInfo(
        0, 0, title="Замороженные узы", title_en="The Frozen Bond", aired="2019-11-08")}
    found = packs.specials(scan, info, movie=lambda title, year: films.get(title))
    got = {p.name: v for p, v in found.items()}
    assert got["Show Hyouketsu no Kizuna.mkv"] == (0, [2])
    ep = info.episodes[(0, 2)]
    assert (ep.season, ep.episode, ep.title, ep.aired) == (0, 2, "Замороженные узы", "2019-11-08")
    assert info.episodes[(0, 1)].title_en == "Memory Snow"     # фильма нет — из имени файла


def test_tmdb_movies_only_of_this_title(monkeypatch):
    import tmdb
    info = metadata.MediaInfo(kind=library.TV, tmdb_id=65942, title="Re:ZERO – Жизнь с нуля",
                              title_en="Re:ZERO -Starting Life in Another World-",
                              original_title="Re:ゼロから始める異世界生活")
    found = {
        ("Memory Snow", 2018): [
            {"id": 1, "title": "Happy Family: Snowball The Memory Gobbler", "original_title": "x"},
            {"id": 532321, "title": "Re:ZERO -Starting Life in Another World- Memory Snow",
             "original_title": "Re:ゼロから始める異世界生活 Memory Snow",
             "release_date": "2018-10-06", "vote_average": 7.35, "vote_count": 330,
             "backdrop_path": "/b.jpg", "overview": "en"}],
        ("Hyouketsu no Kizuna", 2019): [],
        ("Hyouketsu no Kizuna", None): [
            {"id": 566451, "title": "Re:ZERO -Starting Life in Another World- The Frozen Bond",
             "original_title": "Re:ゼロから始める異世界生活 氷結の絆", "release_date": "2019-11-08"}],
    }
    monkeypatch.setattr(tmdb, "search", lambda kind, q, key, year=None: found.get((q, year), []))
    monkeypatch.setattr(tmdb, "movie", lambda i, key, language="en-US": {
        532321: {"title": "Re: Жизнь в другом мире с нуля: Снежные воспоминания",
                 "overview": "Холода.", "runtime": 64},
        566451: {"title": "Re:ZERO -Starting Life in Another World- The Frozen Bond"}}[i])
    movie = packs.tmdb_movies(info, "k", "ru-RU")
    ep = movie("Memory Snow", 2018)
    assert (ep.title, ep.title_en, ep.aired, ep.runtime, ep.plot) == (
        "Снежные воспоминания", "Memory Snow", "2018-10-06", 64, "Холода.")
    assert ep.rating.value == 7.35 and ep.thumb.endswith("/b.jpg")
    # Перевода нет — английское; год не совпал — без года.
    assert movie("Hyouketsu no Kizuna", 2019).title == "The Frozen Bond"
    assert movie("Unknown", 2020) is None


def test_join_episodes_one_card():
    a = metadata.EpisodeInfo(1, 6, title="Звук цепей", title_en="The Sound of Chains",
                             plot="Первая.", aired="2016-05-09", runtime=25, tmdb_id=11,
                             rating=metadata.Rating(9.1, 80))
    b = metadata.EpisodeInfo(1, 7, title="Эпизод 7", title_en="Natsuki Subaru's Restart",
                             plot="Вторая.", aired="2016-05-16", runtime=24, tmdb_id=12,
                             rating=metadata.Rating(9.2, 77))
    ep = metadata.join_episodes([a, b])
    assert (ep.season, ep.episode, ep.aired, ep.tmdb_id) == (1, 6, "2016-05-09", 11)
    assert ep.title == "Звук цепей / Natsuki Subaru's Restart"       # заглушка TMDb — английское
    assert ep.title_en == "The Sound of Chains / Natsuki Subaru's Restart"
    assert ep.plot == "Первая.\n\nВторая." and ep.runtime == 49
    assert ep.rating.value == 9.15 and ep.rating.votes == 157


def test_episode_sidecars_of_multi_episode_file(tmp_path):
    video = tmp_path / "Show - S01E02-E03 - Title.mkv"
    names = ["Show - S01E02-E03 - Title.nfo", "Show - S01E02-E03 - Title-S01E02.nfo",
             "Show - S01E02-E03 - Title-S01E03-thumb.jpg", "Show - S01E02-E03 - Title-extra.mkv"]
    for n in [video.name] + names:
        (tmp_path / n).write_bytes(b"x")
    got = {p.name for p in library.find_sidecars(video)}
    assert got == set(names[:3])


def test_old_multi_episode_file_becomes_one_card(tmp_path):
    """Файл, разложенный прежним видом «S01E02-E03» (две карточки Kodi на одно видео):
    повторная раскладка — «S01E02+E03», один блок в .nfo, отметки просмотра и имя из
    раздачи сохраняются, .nfo по сериям убраны — даже при «только недостающие»."""
    import arrange
    import metaconf
    import nfo
    season = tmp_path / "Show (2016)" / "Season 01"
    season.mkdir(parents=True)
    old = "Show - S01E02-E03 - Ep 2"
    (season / f"{old}.mkv").write_bytes(b"x")
    (season / f"{old}.nfo").write_text(
        "<episodedetails><title>Э2</title><playcount>1</playcount>"
        "<original_filename>[VCB] Show 02.mkv</original_filename></episodedetails>\n"
        "<episodedetails><title>Э3</title></episodedetails>\n", encoding="utf-8")
    for n in (2, 3):
        (season / f"{old}-S01E0{n}.nfo").write_text("<episodedetails/>", encoding="utf-8")
    info = metadata.MediaInfo(kind=library.TV, tmdb_id=1, title="Show", title_en="Show",
                              year=2016)
    for n in (2, 3):
        info.episodes[(1, n)] = metadata.EpisodeInfo(1, n, title=f"Э{n}", title_en=f"Ep {n}")
    settings = metaconf.Settings(tmdb_key="k")
    plan = library.build_tv_plan(tmp_path / "Show (2016)", info, settings)
    library.apply_plan(plan)
    assert sorted(p.name for p in season.iterdir()) == [
        "Show - S01E02+E03 - Ep 2-S01E02.nfo", "Show - S01E02+E03 - Ep 2-S01E03.nfo",
        "Show - S01E02+E03 - Ep 2.mkv", "Show - S01E02+E03 - Ep 2.nfo"]
    arrange.write_episode_nfo(plan, info, metaconf.POLICY_MISSING)
    assert sorted(p.name for p in season.iterdir()) == [
        "Show - S01E02+E03 - Ep 2.mkv", "Show - S01E02+E03 - Ep 2.nfo"]
    path = season / "Show - S01E02+E03 - Ep 2.nfo"
    text = path.read_text(encoding="utf-8")
    assert nfo.episode_blocks(path) == 1 and "<title>Э2 / Э3</title>" in text
    assert "<playcount>1</playcount>" in text and "[VCB] Show 02.mkv" in text
