import library
import numbering
import search
import tmdb


def _group_detail():
    """Как у «Монолога фармацевта» в TMDb: один сезон, деление — в группе «Seasons»."""
    def eps(first, last, season=1, year="2023"):
        return [{"season_number": season, "episode_number": n, "air_date": f"{year}-10-01"}
                for n in range(first, last + 1)]
    return {"groups": [
        {"name": "Season 1", "order": 1, "episodes": eps(1, 24)},
        {"name": "Season 2", "order": 2, "episodes": eps(25, 48, year="2025")},
        {"name": "Specials", "order": 0, "episodes": eps(1, 50, season=0)},
        {"name": "Season 3", "order": 3, "episodes": eps(49, 60, year="2026")},
    ]}


def _cours_detail():
    return {"groups": [{"name": f"Cour {i}", "order": i,
                        "episodes": [{"season_number": 1, "episode_number": n}
                                     for n in range(12 * i - 11, 12 * i + 1)]}
                       for i in range(1, 5)]}


def _patch_groups(monkeypatch, total=60):
    monkeypatch.setattr(tmdb, "episode_groups", lambda tid, key: [
        {"id": "seasons", "group_count": 4}, {"id": "cours", "group_count": 4}])
    monkeypatch.setattr(tmdb, "episode_group", lambda gid, key, language="en-US":
                        _group_detail() if gid == "seasons" else _cours_detail())
    return {"seasons": [{"season_number": 0, "episode_count": 54},
                        {"season_number": 1, "episode_count": total, "air_date": "2023-10-22"}]}


def test_seasons_come_from_episode_group(monkeypatch):
    nb = numbering.load(_patch_groups(monkeypatch), 220542, "k")
    assert nb.grouped
    assert [(s.season, s.first, s.count, s.year) for s in nb.spans] == [
        (1, 1, 24, 2023), (2, 25, 24, 2025), (3, 49, 12, 2026)]
    assert nb.to_tmdb(3, 2) == (1, 50) and nb.from_tmdb(1, 50) == (3, 2)
    assert nb.from_absolute(49) == (3, 1) and nb.from_absolute(61) is None


def test_last_season_grows_with_tmdb(monkeypatch):
    # TMDb добавил вторую часть третьего сезона, а группу ещё никто не поправил.
    nb = numbering.load(_patch_groups(monkeypatch, total=72), 220542, "k")
    assert nb.span(3).count == 24 and nb.from_tmdb(1, 70) == (3, 22)


def test_regular_show_keeps_tmdb_seasons(monkeypatch):
    monkeypatch.setattr(tmdb, "episode_groups", lambda tid, key: [])
    nb = numbering.load({"seasons": [{"season_number": 1, "episode_count": 10},
                                     {"season_number": 2, "episode_count": 8}]}, 1, "k")
    assert not nb.grouped and nb.to_tmdb(2, 3) == (2, 3)


def test_place_episode_numbers_from_files(monkeypatch):
    nb = numbering.load(_patch_groups(monkeypatch), 220542, "k")
    assert nb.place(2, 3) == (3, 2)          # «S03E02», раздача «ТВ-3»
    assert nb.place(49, 3) == (3, 1)         # сквозной номер в раздаче третьего сезона
    assert nb.place(25, 1) == (2, 1)         # VCB: «25» в раздаче «S01-02»
    assert nb.place(50) == (3, 2)            # сезон неизвестен — номер сквозной
    assert nb.place(13, 3) == (3, 13)        # за концом известного TMDb — свой сезон
    assert nb.place(61) == (3, 13)


def test_library_layout_decides_numbers():
    nb = numbering.Numbering([numbering.Span(1, 1, 1, 24), numbering.Span(2, 1, 25, 24)], grouped=True)
    assert not numbering.library_grouped(nb, [1])           # S01E25 — сквозные номера
    assert numbering.library_grouped(nb, [1, 2])            # Season 02 при одном сезоне TMDb
    assert numbering.to_library(nb, 2, 3, grouped_disk=False) == (1, 27)
    assert numbering.to_library(nb, 2, 3, grouped_disk=True) == (2, 3)


def _title(monkeypatch):
    nb = numbering.load(_patch_groups(monkeypatch), 220542, "k")
    return search.Title(kind=library.TV, tmdb_id=220542, title="Монолог фармацевта", year=2023,
                        seasons=[search.Season(s.season, s.count, s.year) for s in nb.spans],
                        numbering=nb)


def test_pack_episodes_counted_in_its_last_season(monkeypatch):
    title = _title(monkeypatch)
    pack = search.Release("a" * 40, "Монолог фармацевта (1-3 сезоны: 1-49 серия из 72)", "m",
                          seasons=[1, 2, 3], episodes=[1, 49, 72])
    search.count_in_season(pack, title)
    assert pack.episodes == [1, 1, 12]
    full = search.Release("b" * 40, "S01-02", "m", seasons=[1, 2], episodes=[1, 48, 48])
    search.count_in_season(full, title)
    assert full.episodes == [1, 24, 24] and search.complete(full, title, [2])
    one = search.Release("c" * 40, "3 сезон: 49-53", "m", seasons=[3], episodes=[49, 53, 60])
    search.count_in_season(one, title)
    assert one.episodes == [1, 5, 12]


def test_release_numbered_like_tmdb_goes_to_tracker_seasons(monkeypatch):
    title = _title(monkeypatch)
    whole = search.Release("a" * 40, "1 сезон: 1-52 из 60", "m", seasons=[1], episodes=[1, 52, 60])
    search.count_in_season(whole, title)
    assert whole.seasons == [1, 2, 3] and whole.episodes == [1, 4, 12]
    scene = search.Release("b" * 40, "Kusuriya no Hitorigoto S01E50", "m", seasons=[1],
                           episodes=[50, 50, 0])
    search.count_in_season(scene, title)
    assert scene.seasons == [3] and scene.episodes == [2, 2, 12]
    assert search.fits(scene, title, [3])


def test_release_without_season_checked_by_season_year(monkeypatch):
    title = _title(monkeypatch)
    tv1 = search.Release("d" * 40, "Монолог фармацевта / 2023 / [WEBRip 1080p][1-24]", "m", year=2023)
    tv3 = search.Release("e" * 40, "Монолог фармацевта [03x01-04 из 24] (2026)", "m", year=2026,
                         seasons=[3], episodes=[1, 4, 24])
    assert not search.fits(tv1, title, [3]) and search.fits(tv1, title, [1])
    assert search.fits(tv3, title, [3])


def test_season_released_in_parts_is_complete_by_tmdb(monkeypatch):
    title = _title(monkeypatch)      # в TMDb у сезона 3 пока 12 серий, у трекера «из 24»
    part = search.Release("f" * 40, "[03x01-12 из 24]", "m", seasons=[3], episodes=[1, 12, 24])
    assert search.complete(part, title, [3])
    part.episodes = [1, 11, 24]
    assert not search.complete(part, title, [3])


def test_in_library_counts_absolute_episodes_by_tracker_seasons(tmp_path, monkeypatch):
    title = _title(monkeypatch)
    show = tmp_path / "tv" / "The Apothecary Diaries (2023)"
    (show / "Season 01").mkdir(parents=True)
    (show / "tvshow.nfo").write_text('<tvshow><uniqueid type="tmdb">220542</uniqueid></tvshow>',
                                     encoding="utf-8")
    for n in range(1, 51):
        (show / "Season 01" / f"The Apothecary Diaries - S01E{n:02d} - x.mkv").write_bytes(b"")
    folder, have = search.in_library(title, [str(tmp_path / "tv")])
    assert folder == str(show) and have == {1: 24, 2: 24, 3: 2}


def test_parse_title_anime_seasons():
    assert search.parse_title("Записки аптекаря [ТВ-2] [2025, TV, 24 из 24]")["seasons"] == [2]
    assert search.parse_title("Kusuriya no Hitorigoto 2nd Season - 05")["seasons"] == [2]
    assert "seasons" not in search.parse_title("Show [2023, TV 24 эп.]")


def test_sibling_numbers(tmp_path):
    d = tmp_path / "[VCB-Studio] Show [S01 SPs 2023-2024]"
    vids = [d / f"[VCB-Studio] Show {n:02d} [BDRip 1080p x265 FLAC].mkv" for n in (1, 2, 25)]
    vids += [d / "SPs" / f"[VCB-Studio] Maomao {n:02d} [BDRip 1080p x265 FLAC].mkv" for n in (1, 2)]
    vids += [d / "Show Opening.mkv"]
    got = library.sibling_numbers(vids)
    assert [got.get(v) for v in vids] == [1, 2, 25, 1, 2, None]
    assert library.season_of_dir("SPs") == 0
