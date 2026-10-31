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
    monkeypatch.setattr(metadata, "search", lambda kind, q, s, year=None:
                        [metadata.SearchHit(library.TV, 1, "Dark Matter", 2024)])
    monkeypatch.setattr(search, "title_info", lambda kind, tmdb_id, key: _title())
    monkeypatch.setattr(search, "find", lambda title, sources=None, log=None: _releases([
        _item("rutor", "Тёмная материя (1 сезон: 1-9 серии из 9)", h="1" * 40, seasons=[1]),
        _item("rutor", "Тёмная материя [S01-02]", h="2" * 40, seasons=[1, 2], year=2026),
        _item("rutor", "Тёмная материя (1 сезон: 1-9 серии из 9) HDR10", h="3" * 40, seasons=[1],
              videotype="hdr")]))
    added = []

    class FakeQ:
        def add(self, url, category=""):
            added.append((url, category))

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
