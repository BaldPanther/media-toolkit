import pytest

import library
import metadata
import paths
import qbit
import search


def _item(tracker, title, h="A" * 40, sid=10, quality=1080, seasons=(1,), year=2024,
          voices=(), videotype="sdr", types=("serial",), size=10 ** 9):
    return {"tracker": tracker, "url": f"https://{tracker}/t/1", "title": title, "size": size,
            "sid": sid, "pir": 1, "magnet": f"magnet:?xt=urn:btih:{h}", "relased": year,
            "videotype": videotype, "quality": quality, "voices": list(voices),
            "seasons": list(seasons), "types": list(types), "updateTime": "2026-10-29T00:00:00Z"}


def _title():
    return search.Title(kind=library.TV, tmdb_id=1, title="Тёмная материя", year=2024,
                        original="Dark Matter", names=["Тёмная материя", "Dark Matter"],
                        seasons=[search.Season(1, 9, 2024), search.Season(2, 10, 2026)])


def _releases(items):
    return search.group([r for r in (search._from_jacred(i) for i in items) if r])


# ----------------------------------------------------------------- разбор --

def test_parse_title_episodes_quality_hdr_dv():
    p = search.parse_title("Темная материя (1 сезон: 1-9 серии из 9) / 2024 / 4K, HEVC, HDR, Dolby Vision")
    assert p["episodes"] == [1, 9, 9] and p["hdr"] and p["dv"] and not p["dv_only"]
    p = search.parse_title("Тёмная материя [01x01-08 из 09] (2024) WEB-DL 2160p | Dolby Vision")
    assert p["episodes"] == [1, 8, 9] and p["quality"] == 2160 and p["dv_only"]
    p = search.parse_title("Сериал (2 сезон: 1 серия из 10) WEB-DL (1080p)")
    assert p["episodes"] == [1, 1, 10] and p["quality"] == 1080
    assert search.parse_title("Show [S01-03] 1080p")["seasons"] == [1, 2, 3]


def test_group_merges_same_hash_across_trackers():
    rels = _releases([
        _item("rutor", "Тёмная материя [S01] 1080p", sid=5, voices=["LostFilm"]),
        _item("rutracker", "Темная материя / Dark Matter / Сезон: 1 / Серии: 1-9 из 9 | LostFilm, TVShows",
              sid=40, voices=["LostFilm", "TVShows"]),
        _item("kinozal", "Другая от DoMiNo &amp; селезень", h="B" * 40, sid=3),
    ])
    assert len(rels) == 2
    assert next(r for r in rels if r.hash == "b" * 40).title == "Другая от DoMiNo & селезень"
    a = next(r for r in rels if r.hash == "a" * 40)
    assert a.seeds == 40 and [t[0] for t in a.trackers] == ["rutor", "rutracker"]
    assert a.voices == ["LostFilm", "TVShows"] and a.episodes == [1, 9, 9]
    assert "Серии" in a.title          # самое подробное название


# ------------------------------------------------------------ фильтр, ранг --

def test_fits_uses_season_year_and_type():
    t = _title()
    s2 = _releases([_item("kinozal", "Тёмная материя (2 сезон) 2026", seasons=[2], year=2026)])[0]
    old = _releases([_item("rutor", "Тёмная материя (2015)", h="C" * 40, year=2015)])[0]
    movie = _releases([_item("rutor", "Фильм", h="D" * 40, types=["movie"])])[0]
    assert search.fits(s2, t, [2])                # год второго сезона, а не сериала
    assert not search.fits(s2, t, [1])            # не тот сезон
    assert not search.fits(old, t, [1])           # другой сериал с тем же названием
    assert search.fits(old, t, [1], any_year=True)
    assert not search.fits(movie, t, [1])


def test_picture_filter_follows_hdr_and_dv_checkboxes():
    sdr, hdr, hybrid, dv_only = _releases([
        _item("a", "SDR", h="1" * 40),
        _item("b", "HDR10+", h="2" * 40, videotype="hdr"),
        _item("c", "HDR10, Dolby Vision Profile 8", h="3" * 40, videotype="hdr"),
        _item("d", "Dolby Vision", h="4" * 40),
    ])
    shown = lambda **kw: [r.title for r in (sdr, hdr, hybrid, dv_only)   # noqa: E731
                          if search.picture_ok(r, search.Prefs(**kw))]
    assert shown() == ["SDR"]                                   # обе галки сняты — только SDR
    assert shown(hdr=True) == ["SDR", "HDR10+", "HDR10, Dolby Vision Profile 8"]
    assert shown(dv=True) == ["SDR", "HDR10, Dolby Vision Profile 8", "Dolby Vision"]
    assert len(shown(hdr=True, dv=True)) == 4


def test_quality_of_puts_unknown_into_sd():
    rels = _releases([_item("a", "x", h="1" * 40, quality=2160), _item("b", "y", h="2" * 40, quality=0)])
    assert [search.quality_of(r) for r in rels] == [2160, 480]


def test_rank_prefers_complete_then_quality_voice_and_keeps_low_seeds():
    t = _title()
    rels = _releases([
        _item("a", "4K мало сидов (1 сезон: 1-9 серии из 9)", h="1" * 40, quality=2160, sid=1),
        _item("b", "4K TVShows (1 сезон: 1-9 серии из 9)", h="2" * 40, quality=2160, sid=0,
              voices=["TVShows"]),
        _item("c", "4K неполный (1 сезон: 1-8 серии из 9)", h="3" * 40, quality=2160, sid=300),
        _item("e", "1080 (1 сезон: 1-9 серии из 9)", h="5" * 40, quality=1080, sid=900),
    ])
    ranked = search.rank(rels, t, search.Prefs(), [1])
    assert [r.hash[0] for r in ranked] == ["2", "1", "5", "3"]
    assert "нет сидов" in ranked[0].notes and not ranked[0].recommended   # без сидов — не советую
    assert ranked[1].recommended and "мало сидов" in ranked[1].notes
    assert "не все серии: 8 из 9" in ranked[-1].notes


def test_rank_puts_dv_then_hdr_first_when_allowed():
    t = _title()
    rels = _releases([
        _item("a", "SDR (1 сезон: 1-9 серии из 9)", h="1" * 40, sid=500),
        _item("b", "HDR10 (1 сезон: 1-9 серии из 9)", h="2" * 40, sid=50, videotype="hdr"),
        _item("c", "Dolby Vision (1 сезон: 1-9 серии из 9)", h="3" * 40, sid=5),
    ])
    ranked = search.rank(rels, t, search.Prefs(hdr=True, dv=True), [1])
    assert [r.hash[0] for r in ranked] == ["3", "2", "1"]
    assert "только Dolby Vision" in ranked[0].notes


def test_pack_with_extra_seasons_is_noted():
    t = _title()
    pack = _releases([_item("a", "Тёмная материя [S01-02]", seasons=[1, 2])])
    ranked = search.rank(pack, t, search.Prefs(), [2])
    assert "сборник: сезоны 1–2" in ranked[0].notes


def test_find_uses_sources_and_reports_failures(monkeypatch):
    calls = []

    def fake_get_json(url, timeout=None, **kw):
        calls.append(url)
        if "broken" in url:
            raise search.net.NetError("HTTP 500")
        return [_item("rutor", "Тёмная материя [S01] 1080p")]

    monkeypatch.setattr(search.net, "get_json", fake_get_json)
    logged = []
    rels = search.find(_title(), sources=[{"type": "jacred", "url": "https://jac.red"},
                                          {"type": "jacred", "url": "https://broken"}],
                       log=logged.append)
    assert len(rels) == 1 and "exact=true" in calls[0] and "altname=Dark+Matter" in calls[0]
    assert logged and "broken" in logged[0]


def test_in_library_by_tmdb_id(tmp_path):
    show = tmp_path / "tv" / "Тёмная материя (2024)"
    (show / "Season 01").mkdir(parents=True)
    (show / "tvshow.nfo").write_text('<tvshow><uniqueid type="tmdb">1</uniqueid></tvshow>',
                                     encoding="utf-8")
    for n in (1, 2, 3):
        (show / "Season 01" / f"Dark Matter - S01E0{n}.mkv").write_bytes(b"x")
    found, have = search.in_library(_title(), [str(tmp_path / "tv")])
    assert found == str(show) and have == {1: 3}


def test_sources_file_created_with_jacred(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    assert search.load_sources() == search.DEFAULT_SOURCES
    assert (tmp_path / "cfg" / "search_sources.json").exists()


# ------------------------------------------------------------------- веб --

@pytest.fixture
def web(tmp_path, monkeypatch):
    import metaconf
    from web import server
    from web.state import AppState
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    metaconf.save_settings(metaconf.Settings(tmdb_key="k", tv_roots=[str(tmp_path / "tv")]))
    qbit.save_settings(qbit.Settings(url="http://q", auto=True))
    monkeypatch.setattr(metadata, "search_any", lambda q, s:
                        [metadata.SearchHit(library.TV, 1, "Dark Matter", 2024)])
    monkeypatch.setattr(search, "title_info", lambda kind, tmdb_id, key: _title())
    monkeypatch.setattr(search, "find", lambda title, sources=None, log=None: _releases([
        _item("rutor", "Тёмная материя (1 сезон: 1-9 серии из 9)", h="1" * 40, seasons=[1]),
        _item("rutor", "Тёмная материя [S01-02]", h="2" * 40, seasons=[1, 2], year=2026),
        _item("rutor", "Тёмная материя (1 сезон: 1-9 серии из 9) HDR10", h="3" * 40, seasons=[1],
              videotype="hdr")]))
    added = []

    class FakeQ:
        def add(self, url, category="", stop_on_metadata=False):
            added.append((url, category))

        def torrents(self):
            return []

    monkeypatch.setattr(qbit, "connect", lambda s: FakeQ())
    st = AppState("desktop")
    app = server.create_app(st)
    app.testing = True
    return st, app.test_client(), added


def _search(c):
    return c.get("/api/state").get_json()["tabs"]["torrents"]["search"]


def test_search_flow_select_filter_download(web):
    st, c, added = web
    res = c.post("/api/torrents/search", json={"query": "Тёмная материя", "kind": "tv"}).get_json()
    assert res["job"]["title"] == "Поиск раздач"
    assert st.jobs.wait() and st.jobs.last.status == "done", st.jobs.last.error
    s = _search(c)
    assert s["title"]["label"] == "Тёмная материя (2024)"
    assert [x["number"] for x in s["seasons"] if x["selected"]] == [1]   # в медиатеке пусто
    # Показывается только выбранное качество (4K), HDR — только с галкой.
    assert s["releases"] == [] and dict(s["qualities"]) == {2160: 0, 1080: 2, 720: 0, 480: 0}
    c.post("/api/torrents/search/options", json={"quality": 1080})
    s = _search(c)
    assert len(s["releases"]) == 2 and s["releases"][0]["recommended"]
    assert s["status"] == "Найдено раздач: 3, под фильтры: 2"
    c.post("/api/torrents/search/options", json={"hdr": True})
    assert len(_search(c)["releases"]) == 3
    # Второй сезон: остаётся только сборник, с пометкой.
    c.post("/api/torrents/search/options", json={"seasons": [2]})
    s = _search(c)
    assert [r["hash"] for r in s["releases"]] == ["2" * 40]
    assert "сборник: сезоны 1–2" in s["releases"][0]["notes"]
    assert s["prefs"]["quality"] == 1080 and s["prefs"]["hdr"] is True
    # Скачать: в qBittorrent с категорией сериалов, автоматика ждёт раздачу с тайтлом.
    assert c.post("/api/torrents/search/download", json={"hash": "2" * 40}).get_json()["ok"]
    assert added == [("magnet:?xt=urn:btih:" + "2" * 40, "tv")]
    item = st.tabs["torrents"].autopilot.items["2" * 40]
    assert item.tmdb_id == 1 and item.wanted == [2]


def test_watch_from_search(web):
    st, c, _ = web
    c.post("/api/torrents/search", json={"query": "Тёмная материя", "kind": "tv"})
    assert st.jobs.wait()
    assert c.post("/api/torrents/watch/add", json={}).get_json()["ok"]
    tab = c.get("/api/state").get_json()["tabs"]["torrents"]
    assert tab["search"]["title"]["watching"] is True
    w = tab["watch"][0]
    assert w["title"] == "Тёмная материя (2024)" and w["mode"] == "season"
    assert [m[0] for m in w["modes"]] == ["season", "episode", "new_seasons"]
    c.post("/api/torrents/watch/mode", json={"id": w["id"], "mode": "episode"})
    assert c.get("/api/state").get_json()["tabs"]["torrents"]["watch"][0]["mode"] == "episode"
    c.post("/api/torrents/watch/remove", json={"id": w["id"]})
    assert c.get("/api/state").get_json()["tabs"]["torrents"]["watch"] == []


def test_search_any_mixes_movies_and_series(monkeypatch):
    import tmdb
    monkeypatch.setattr(tmdb, "_call", lambda path, key, **p: {"results": [
        {"media_type": "movie", "id": 1, "title": "Дюна", "release_date": "2021-09-15"},
        {"media_type": "person", "id": 2, "name": "Дени Вильнёв"},
        {"media_type": "tv", "id": 3, "name": "Дюна: Пророчество", "first_air_date": "2024-11-17"},
    ]} if path == "/search/multi" else None)
    hits = metadata.search_any("Дюна", type("S", (), {"tmdb_key": "k", "meta_language": "ru-RU"})())
    assert [(h.kind, h.tmdb_id, h.year) for h in hits] == [(library.MOVIE, 1, 2021), (library.TV, 3, 2024)]


# ---------------------------------------------------------------- Knaben --

def test_parse_title_scene_seasons_and_episodes():
    p = search.parse_title("Dark.Matter.2024.S01E07.1080p.WEB.H264-SuccessfulCrab")
    assert p["seasons"] == [1] and p["episodes"] == [7, 7, 0] and not p.get("pack")
    assert search.parse_title("Show S02E01-E08 2160p")["episodes"] == [1, 8, 0]
    p = search.parse_title("Dark.Matter.2024.S01.2160p.ATVP.WEB-DL.DDP5.1.HEVC")
    assert p["seasons"] == [1] and p["pack"] and "episodes" not in p
    assert search.parse_title("Dark Matter Season 2 Complete 1080p")["seasons"] == [2]


def _knaben_hit(h, title, seeders=10, cat=(2000000, 2001000), tracker="The Pirate Bay"):
    return {"hash": h * 40, "title": title, "seeders": seeders, "peers": 1, "bytes": 5 * 2 ** 30,
            "categoryId": list(cat), "tracker": tracker, "details": f"https://tpb/{h}",
            "magnetUrl": f"magnet:?xt=urn:btih:{h * 40}", "date": "2026-10-01T00:00:00+00:00"}


def test_knaben_source_reads_english_releases(monkeypatch):
    asked = []

    def fake_post(url, body, timeout=None):
        asked.append(body["query"])
        return {"hits": [
            _knaben_hit("e", "Dark.Matter.2024.S01.2160p.ATVP.WEB-DL.DDP5.1.HEVC", 170),
            _knaben_hit("f", "Dark.Matter.2024.S01E07.1080p.WEB.H264", 60),
            _knaben_hit("g", "Dark Matter OST FLAC", 5, cat=(1002000, 1000000)),
        ]}

    monkeypatch.setattr(search.net, "post_json", fake_post)
    t = _title()
    rels = search._knaben(search.KNABEN, t)
    assert asked == ["Dark Matter"]                        # русские названия — у jac.red
    assert [r.hash[0] for r in rels] == ["e", "f"]         # музыка мимо
    pack, single = rels
    assert pack.quality == 2160 and pack.pack and pack.trackers == [["The Pirate Bay", "https://tpb/e"]]
    assert not search.russian(pack)
    # Сезон вышел целиком — «S01» считается полным; одна серия — нет, всего — из TMDb.
    t.last_aired = (2, 6)
    assert search.complete(pack, t, [1])
    assert not search.complete(single, t, [1]) and single.episodes == [7, 7, 9]


def test_russian_releases_rank_above_english():
    t = _title()
    t.last_aired = (2, 6)
    rels = _releases([_item("rutor", "Тёмная материя (1 сезон: 1-5 серии из 9)", h="1" * 40, sid=3)])
    eng = search.Release(hash="2" * 40, title="Dark.Matter.2024.S01.1080p", magnet="m", seeds=500,
                         quality=1080, seasons=[1], pack=True, year=2024)
    ranked = search.rank(rels + [eng], t, search.Prefs(), [1])
    assert [r.hash[0] for r in ranked] == ["1", "2"] and ranked[0].recommended
    assert "английская" in ranked[1].notes and "англ." in ranked[1].why


def test_episodes_text_for_partial_releases():
    mk = lambda eps: search.Release(hash="a", title="x", magnet="m", episodes=eps)   # noqa: E731
    assert search._episodes_text(mk([1, 5, 9])) == "5 из 9"
    assert search._episodes_text(mk([9, 9, 9])) == "серия 9 из 9"
    assert search._episodes_text(mk([6, 9, 9])) == "серии 6–9 из 9"
    assert search._episodes_text(mk([7, 7, 0])) == "серия 7"


def test_paste_magnet_goes_to_qbit_with_chosen_category(web):
    st, c, added = web
    magnet = "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=Some.Movie.2024.2160p"
    res = c.post("/api/torrents/add-link", json={"url": magnet, "kind": "movie"}).get_json()
    assert res["ok"] and added == [(magnet, "movies")]
    item = st.tabs["torrents"].autopilot.items["c" * 40]
    assert item.kind == library.MOVIE and item.name == "Some.Movie.2024.2160p"
    bad = c.post("/api/torrents/add-link", json={"url": "Тёмная материя"})
    assert bad.status_code >= 400 or not bad.get_json().get("ok")


def test_release_marks_and_free_space_warning(web, monkeypatch):
    import autopilot
    st, c, added = web
    c.post("/api/torrents/search", json={"query": "Тёмная материя"})
    assert st.jobs.wait()
    c.post("/api/torrents/search/options", json={"quality": 1080})
    monkeypatch.setattr(autopilot, "free_space", lambda kind: 10 ** 12)
    s = _search(c)
    assert s["free"] == 10 ** 12 and not any(r["in_qbit"] or r["update"] for r in s["releases"])
    # Места мало — сначала вопрос, с «да» — качает.
    monkeypatch.setattr(autopilot, "free_space", lambda kind: 1000)
    h = s["releases"][0]["hash"]
    ask = c.post("/api/torrents/search/download", json={"hash": h}).get_json()
    assert ask["ask"]["id"] == "space" and added == []
    ok = c.post("/api/torrents/search/download", json={"hash": h, "answers": {"space": True}}).get_json()
    assert ok["ok"] and len(added) == 1
    assert next(r for r in _search(c)["releases"] if r["hash"] == h)["in_qbit"]
    # Тот же сезон уже в медиатеке — пометка.
    tab = st.tabs["torrents"]
    tab.have = {1: 9}
    assert all(r["in_library"] for r in _search(c)["releases"] if r["seasons"] == [1])


def test_title_folder_fills_search_field_without_scan(web, tmp_path):
    st, c, _ = web
    season = tmp_path / "tv" / "The Apothecary Diaries (2023)" / "Season 01"
    season.mkdir(parents=True)
    c.post("/api/path", json={"path": str(season)})       # только выбрали папку
    assert _search(c)["query"] == "The Apothecary Diaries"
    st.tabs["torrents"].query = ""                         # поле стёрли — «Сканировать» вернёт
    c.post("/api/scan", json={"path": str(season)})       # сканирование может и не начаться
    assert _search(c)["query"] == "The Apothecary Diaries"


def test_title_names_include_romaji(monkeypatch):
    import tmdb
    monkeypatch.setattr(tmdb, "titles", lambda kind, tid, key: {
        "name": "Монолог фармацевта", "original_name": "薬屋のひとりごと",
        "first_air_date": "2023-10-22",
        "translations": {"translations": [{"iso_639_1": "en", "data": {"name": "The Apothecary Diaries"}}]},
        "alternative_titles": {"results": [
            {"iso_3166_1": "JP", "title": "Kusuriya no Hitorigoto", "type": "Romaji"},
            {"iso_3166_1": "JP", "title": "Kusuriya no Hitorigoto 2nd Season", "type": ""},
            {"iso_3166_1": "JP", "title": "Ore dake S2", "type": "season 2 romaji"},
            {"iso_3166_1": "RU", "title": "Записки аптекаря", "type": ""}]},
        "seasons": [{"season_number": 1, "episode_count": 60}]})
    monkeypatch.setattr(tmdb, "episode_groups", lambda tid, key: [])
    t = search.title_info(library.TV, 220542, "k")
    assert t.names == ["Монолог фармацевта", "薬屋のひとりごと", "The Apothecary Diaries",
                       "Kusuriya no Hitorigoto", "Записки аптекаря"]


def test_ukrainian_release_is_not_russian():
    ukr = search.Release("u" * 40, "Монолог травниці (Сезон 3, серії 04 з 12) / Kusuriya no Hitorigoto "
                         "(Season 3) (2026) WEBDLRip 1080p H.265 Ukr/Jap | sub Ukr", "m")
    rus = search.Release("r" * 40, "Монолог фармацевта / Kusuriya no Hitorigoto [03x01-04 из 24]", "m")
    both = search.Release("b" * 40, "Монолог травниці / Kusuriya no Hitorigoto | Rus, Ukr", "m")
    assert not search.russian(ukr) and search.russian(rus) and search.russian(both)
    title = _title()
    search.rank([ukr], title, search.Prefs())
    assert "украинская" in ukr.notes


def test_page_key_same_topic():
    a = "http://rutor.info/torrent/959050/monolog-farmacevta_kusuriya-no-hitorigoto-01x01-22-iz-24"
    b = "http://rutor.info/torrent/959050/monolog-farmacevta_kusuriya-no-hitorigoto-01x01-23-iz-24"
    assert search.page_key(a) == search.page_key(b) == "rutor:959050"
    assert (search.page_key("https://rutracker.org/forum/viewtopic.php?t=6426359")
            == search.page_key("https://rutracker.net/forum/viewtopic.php?t=6426359"))
    assert search.page_key("https://kinozal.guru/details.php?id=2155818") == "kinozal:2155818"
    assert search.page_key("https://torrent.by/111210/Monolog") == "https://torrent.by/111210/Monolog"


def test_pin_release_and_watch_conditions(web):
    st, c, _ = web
    c.post("/api/torrents/search", json={"query": "Тёмная материя"})
    assert st.jobs.wait()
    c.post("/api/torrents/search/options", json={"quality": 1080})
    rel = _search(c)["releases"][0]
    assert c.post("/api/torrents/watch/pin", json={"hash": rel["hash"]}).get_json()["ok"]
    state = c.get("/api/state").get_json()["tabs"]["torrents"]
    w = state["watch"][0]
    assert w["mode"] == "episode" and w["pin_title"] == rel["title"]
    assert next(r for r in state["search"]["releases"] if r["hash"] == rel["hash"])["pinned"]
    c.post("/api/torrents/watch/filter", json={"id": w["id"], "quality": 2160, "voice": "LostFilm"})
    c.post("/api/torrents/watch/unpin", json={"id": w["id"]})
    w = c.get("/api/state").get_json()["tabs"]["torrents"]["watch"][0]
    assert (w["quality"], w["voice"], w["pin_title"]) == (2160, "LostFilm", "")


def test_library_version_and_comparison(tmp_path, monkeypatch):
    import core
    monkeypatch.setattr(core, "video_info", lambda path: (1920, 1000.0, "HEVC/H.265/MPEG-H"))
    movie = tmp_path / "Film (2021)"
    (movie / "Extras").mkdir(parents=True)
    (movie / "Film (2021).mkv").write_bytes(b"x" * 1_000_000)
    (movie / "Extras" / "Film (2021).mkv").write_bytes(b"x" * 5_000_000)   # отложенная — не в счёт
    owned = search.library_version(library.MOVIE, str(movie))
    assert (owned.size, owned.quality, owned.bitrate, owned.codec) == (1_000_000, 1080, 0.0, "HEVC")
    show = tmp_path / "Show"
    (show / "Season 01").mkdir(parents=True)
    (show / "Specials").mkdir()
    for n in (1, 2):
        (show / "Season 01" / f"Show - S01E0{n} - x.mkv").write_bytes(b"x" * 300)
    (show / "Specials" / "Show - S00E01 - x.mkv").write_bytes(b"x" * 999)
    owned = search.library_version(library.TV, str(show), probe=False)
    assert (owned.size, owned.episodes, owned.per_episode) == (600, 2, 300)
    title = _title()
    pack = search.Release("p" * 40, "S01-02", "m", seasons=[1, 2], episodes=[1, 4, 10], size=1300)
    assert search.episode_count(pack, title) == 13          # 9 серий первого + 4 второго
    assert search.bitrate(3_000_000_000, 50) == 8.0


def test_codec_parsed_and_hevc_preferred():
    assert search.parse_title("Show [BDRip 1080p x265 FLAC]")["codec"] == "HEVC"
    assert search.parse_title("Show WEBRip-HEVC 1080p")["codec"] == "HEVC"
    assert search.parse_title("Show / H.264 / WEB-DL 1080p")["codec"] == "AVC"
    assert search.parse_title("Show WEB-DL 1080p")["codec"] == ""
    title = _title()
    avc = search.Release("a" * 40, "S01 1080p x264", "m", quality=1080, seeds=50, codec="AVC",
                         seasons=[1], episodes=[1, 9, 9], voices=["LostFilm"])
    hevc = search.Release("h" * 40, "S01 1080p x265", "m", quality=1080, seeds=10, codec="HEVC",
                          seasons=[1], episodes=[1, 9, 9], voices=["LostFilm"])
    ranked = search.rank([avc, hevc], title, search.Prefs(), [1])
    assert ranked[0] is hevc and hevc.why.startswith("1080p HEVC")


def test_codec_filter_in_search(web):
    st, c, _ = web
    c.post("/api/torrents/search", json={"query": "Тёмная материя"})
    assert st.jobs.wait()
    c.post("/api/torrents/search/options", json={"quality": 1080, "codec": "HEVC"})
    s = _search(c)
    assert s["codec"] == "HEVC" and s["releases"] == []
    c.post("/api/torrents/search/options", json={"codec": ""})
    assert len(_search(c)["releases"]) == 2


def test_source_parsed_and_ranked_before_codec():
    assert search.source_of("Полдень (1 сезон) / 2026 / РУ / HEVC / WEBRip (1080p)") == "WEBRip"
    assert search.source_of("Polden.S01.2026.WEB-DL.1080p.ExKinoRay") == "WEB-DL"
    assert search.source_of("Show WEBDLRip 1080p") == "WEBRip"
    assert search.source_of("Film 2021 BDRemux 1080p") == "Remux"
    assert search.source_of("[BDRip 1080p x265 FLAC]") == "BDRip"
    assert search.source_of("Film 2021 1080p") == ""
    title = _title()
    rip = search.Release("r" * 40, "S01 1080p WEBRip HEVC", "m", quality=1080, seeds=50,
                         codec="HEVC", source="WEBRip", seasons=[1], episodes=[1, 9, 9])
    web = search.Release("w" * 40, "S01 1080p WEB-DL", "m", quality=1080, seeds=10,
                         source="WEB-DL", seasons=[1], episodes=[1, 9, 9])
    assert search.rank([rip, web], title, search.Prefs(), [1])[0] is web
    assert web.why.startswith("1080p WEB-DL") and rip.why.startswith("1080p WEBRip HEVC")


def test_owned_source_from_nfo_and_codec_aware_comparison(tmp_path, monkeypatch):
    import core
    monkeypatch.setattr(core, "video_info", lambda path: (1920, 2400.0, "AVC/H.264/MPEG-4p10"))
    show = tmp_path / "Полдень (2026)"
    (show / "Season 01").mkdir(parents=True)
    ep = show / "Season 01" / "Полдень - S01E01.mkv"
    ep.write_bytes(b"x" * 1400)
    ep.with_suffix(".nfo").write_text(
        "<episodedetails><original_filename>Polden.S01E01.2026.WEB-DL.1080p.mkv"
        "</original_filename></episodedetails>", encoding="utf-8")
    owned = search.library_version(library.TV, str(show))
    assert (owned.codec, owned.source) == ("AVC", "WEB-DL")
    # HEVC в 0.6 размера — с поправкой на кодек примерно как ваша.
    assert round(search.avc_equivalent(860, "HEVC") / search.avc_equivalent(1400, "AVC"), 1) == 0.9
    tmm = tmp_path / "Film (2021)"
    tmm.mkdir()
    (tmm / "Film (2021).mkv").write_bytes(b"x")
    (tmm / "movie.nfo").write_text("<movie><source>BLURAY</source></movie>", encoding="utf-8")
    assert search.library_version(library.MOVIE, str(tmm)).source == "BDRip"


def test_av1_recognized_and_preferred_like_hevc():
    assert search.parse_title("Полдень (2026) WEBRip [AV1/1080p]")["codec"] == "AV1"
    assert search.codec_of("HEVC/H.265/MPEG-H") == "HEVC" and search.codec_of("AVC/H.264/MPEG-4p10") == "AVC"
    assert search.avc_equivalent(500, "AV1") == 1000
    title = _title()
    av1 = search.Release("a" * 40, "S01 1080p WEBRip AV1", "m", quality=1080, seeds=5, codec="AV1",
                         source="WEBRip", seasons=[1], episodes=[1, 9, 9])
    avc = search.Release("c" * 40, "S01 1080p WEBRip", "m", quality=1080, seeds=50,
                         source="WEBRip", seasons=[1], episodes=[1, 9, 9])
    assert search.rank([avc, av1], title, search.Prefs(), [1])[0] is av1


def test_same_hash_different_size_is_stale_magnet_not_merged():
    # torrent.by: 4 серии, 1.64 ГБ; kinozal обновил раздачу до 5 серий (2.09 ГБ), а
    # jac.red держит его magnet от прежней версии — тот же хеш.
    tby = search.Release("6ad3" * 10, "[03x01-04 из 24]", "m", size=1_640_000_000, seeds=6,
                         updated="2026-10-25", episodes=[1, 4, 24], seasons=[3],
                         trackers=[["torrentby", "https://torrent.by/219936/x"]])
    kz = search.Release("6ad3" * 10, "(3 сезон: 1-5 серии из 24)", "m", size=2_090_000_000, seeds=6,
                        updated="2026-10-31", episodes=[1, 5, 24], seasons=[3],
                        trackers=[["kinozal", "https://kinozal.guru/details.php?id=2155642"]])
    out = search.group([tby, kz])
    assert len(out) == 2 and not out[0].stale and out[1].stale
    assert (out[0].size, out[0].title) == (1_640_000_000, "[03x01-04 из 24]")
    ranked = search.rank(out, _title(), search.Prefs(), [3])
    assert ranked[0] is tby and tby.recommended and not kz.recommended
    assert any("старый magnet" in n for n in kz.notes)


def test_more_episodes_first_among_incomplete():
    title = _title()
    pack = search.Release("p" * 40, "1-2 сезоны WEB-DL", "m", quality=1080, seeds=2, source="WEB-DL",
                          seasons=[1, 2], episodes=[1, 1, 10])          # из второго — одна серия
    five = search.Release("f" * 40, "[02x01-05 из 10] WEBRip", "m", quality=1080, seeds=2,
                          source="WEBRip", seasons=[2], episodes=[1, 5, 10])
    assert search.rank([pack, five], title, search.Prefs(), [2])[0] is five


def test_dub_matches_any_studio():
    def rel(title, voices=()):
        return search.Release("d" * 40, title, "m", voices=list(voices))
    assert search.has_voice(rel("Фильм / ДБ (DEEP), СТ / WEB-DL (1080p)"), "Дубляж")
    assert search.has_voice(rel("Film (2026) WEB-DL 1080p | D, L | DEEP"), "Дубляж")
    assert search.has_voice(rel("Film [2026, WEB-DL] Dub"), "Дубляж")
    assert search.has_voice(rel("Фильм", ["Дубляж", "AniLibria"]), "Дубляж")
    assert not search.has_voice(rel("Фильм / ЛМ (AniLibria) / WEB-DL"), "Дубляж")
    assert not search.has_voice(rel("Film (2026) WEB-DL | L, P2 |"), "Дубляж")


def test_any_russian_voice_not_subtitles():
    def rel(title, voices=()):
        return search.Release("v" * 40, title, "m", voices=list(voices))
    rus = "Любая русская"
    assert search.has_voice(rel("Фильм / 2026 / ЛМ (Dream Cast) / WEB-DL"), rus)
    assert search.has_voice(rel("Film (2026) WEB-DL 1080p | L1 |"), rus)
    assert search.has_voice(rel("Фильм [2026, BDRip] MVO + Sub"), rus)
    assert search.has_voice(rel("Фильм [RUS(int), JAP+Sub]"), rus)
    assert search.has_voice(rel("Фильм", ["AniLibria"]), rus)
    assert not search.has_voice(rel("Фильм / Film [2026, WEB-DL] JAP + Sub Rus"), rus)
    assert not search.has_voice(rel("Фильм / 2026 / СТ / WEB-DL (1080p)"), rus)


def test_same_hash_rounded_sizes_still_merged():
    # Одна раздача: rutracker — точный размер, bitru — округлённый (≈1% меньше).
    a = search.Release("05bb" * 10, "Сезон 4 rutracker", "m", size=34_370_475_786, updated="2026-02-04",
                       trackers=[["rutracker", "https://rutracker.org/forum/viewtopic.php?t=6472229"]])
    b = search.Release("05bb" * 10, "4 сезон bitru", "m", size=34_005_403_566, updated="2026-08-11",
                       trackers=[["bitru", "https://bitru.org/details.php?id=581862"]])
    out = search.group([a, b])
    assert len(out) == 1 and not out[0].stale and len(out[0].trackers) == 2


def test_twins_same_size_other_hash():
    a = search.Release("05bb" * 10, "rutracker", "m", size=34_370_475_786, updated="2026-02-04")
    b = search.Release("24e4" * 10, "kinozal", "m", size=34_370_475_786, updated="2026-07-22")
    c = search.Release("cccc" * 10, "другая", "m", size=34_370_475_000)
    small = search.Release("dddd" * 10, "сэмпл", "m", size=1000)
    assert [t.hash for t in search.twins(a, [a, b, c, small])] == [b.hash]
    assert search.twins(small, [small, search.Release("e" * 40, "x", "m", size=1000)]) == []


def test_seeds_date_from_same_record():
    rt = search.Release("05bb" * 10, "rutracker", "m", seeds=24, updated="2026-02-04",
                        seeds_at="2026-02-04", trackers=[["rutracker", "u1"]])
    nnm = search.Release("05bb" * 10, "nnmclub", "m", seeds=57, updated="2026-02-04",
                         seeds_at="2026-02-04", trackers=[["nnmclub", "u2"]])
    bit = search.Release("05bb" * 10, "bitru", "m", seeds=4, updated="2026-08-11",
                         seeds_at="2026-08-11", trackers=[["bitru", "u3"]])
    g = search.group([rt, nnm, bit])[0]
    assert (g.seeds, g.seeds_at, g.updated) == (57, "2026-02-04", "2026-08-11")
