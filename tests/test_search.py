import pytest

import library
import metadata
import paths
import qbit
import search


def _item(tracker, title, h="A" * 40, sid=10, quality=1080, seasons=(1,), year=2024,
          voices=(), videotype="sdr", types=("serial",), size=0):
    # Размер по умолчанию — свой у каждого хеша: одинаковый до байта — это двойники.
    size = size or 10 ** 9 + sum(map(ord, h)) * 1000
    return {"tracker": tracker, "url": f"https://{tracker}/t/1", "title": title, "size": size,
            "sid": sid, "pir": 1, "magnet": f"magnet:?xt=urn:btih:{h}", "relased": year,
            "videotype": videotype, "quality": quality, "voices": list(voices),
            "seasons": list(seasons), "types": list(types), "updateTime": "2026-10-29T00:00:00Z",
            "originalname": "Dark Matter"}


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


def _rezero():
    names = ["Re:ZERO – Жизнь с нуля в альтернативном мире", "Re:ゼロから始める異世界生活",
             "Re:ZERO -Starting Life in Another World-", "Re:Zero kara Hajimeru Isekai Seikatsu",
             "Re: Жизнь в другом мире с нуля", "Re: Жизнь в альтернативном мире с нуля",
             "Re:Zero — жизнь с нуля в другом мире", "Re:Zero. Жизнь с нуля в альтернативном мире",
             "Жизнь в альтернативном мире с нуля", "Жизнь в другом мире с нуля"]
    return search.Title(kind=library.TV, tmdb_id=65942, title=names[0], year=2016,
                        original=names[1], names=names, romaji=[names[3]])


def test_jacred_queries_pair_names_and_skip_foreign_script():
    queries = search._jacred_queries(_rezero())
    # Японское jac.red превратил бы в «re» (504); «Re:Zero. Жизнь…» для него — то же, что
    # первое; ромадзи — неточным поиском, вторым.
    assert queries == [
        {"search": "Re:ZERO – Жизнь с нуля в альтернативном мире",
         "altname": "Re:ZERO -Starting Life in Another World-", "exact": "true"},
        {"search": "Re:Zero kara Hajimeru Isekai Seikatsu"},
        {"search": "Re: Жизнь в другом мире с нуля",
         "altname": "Re: Жизнь в альтернативном мире с нуля", "exact": "true"},
        {"search": "Re:Zero — жизнь с нуля в другом мире",
         "altname": "Жизнь в альтернативном мире с нуля", "exact": "true"},
    ]                                                   # запросов — не больше JACRED_QUERIES
    assert not search._foreign_script("Shōgun") and search._foreign_script("오징어 게임")
    assert search._no_accents("Pokémon") == "Pokemon"
    assert search._no_accents("Настоящий детектив") == "Настоящий детектив"   # «й» — не «и»


def test_asked_names_are_only_those_sent():
    t = _rezero()
    asked = search.asked_names(t)
    # «Ищу по» — то, что ушло в запросы; японское и сверх лимита — только в подсказке.
    assert asked[:3] == ["Re:ZERO – Жизнь с нуля в альтернативном мире",
                         "Re:ZERO -Starting Life in Another World-", "Re:Zero kara Hajimeru Isekai Seikatsu"]
    assert "Re:ゼロから始める異世界生活" not in asked and len(asked) == 7
    # Knaben — два латинских: «Re» в японском оригинале латиницей его не делает.
    assert search._knaben_names(t) == ["Re:ZERO -Starting Life in Another World-",
                                       "Re:Zero kara Hajimeru Isekai Seikatsu"]


def _bureau():
    return search.Title(kind=library.TV, tmdb_id=62480, title="Бюро", year=2015,
                        original="Le Bureau des légendes", original_language="fr",
                        names=["Бюро", "Le Bureau des légendes", "The Bureau"])


def test_jacred_original_in_other_language_goes_last():
    # Русское и английское — первыми, французский оригинал — после них (и без диакритики:
    # jac.red её выбрасывает, «lgendes» и «legendes» — разные названия).
    assert search._jacred_queries(_bureau()) == [
        {"search": "Бюро", "altname": "The Bureau", "exact": "true"},
        {"search": "Le Bureau des légendes", "altname": "Le Bureau des legendes", "exact": "true"}]


def test_find_checks_rutracker_while_jacred_answers(monkeypatch):
    # После каждого запроса — только новые раздачи; rutracker о них спрошен ещё до конца
    # поиска (check_alive потом берёт ответ из кэша).
    answers = iter([[_item("rutracker", "Re:Zero [ТВ-1]", h="1" * 40, year=2016, types=("anime",))],
                    [_item("rutracker", "Re:Zero [ТВ-1]", h="1" * 40, year=2016, types=("anime",)),
                     _item("rutor", "Re:Zero [ТВ-2]", h="2" * 40, year=2020, types=("anime",))]])
    monkeypatch.setattr(search.net, "get_json", lambda url, timeout=None, **kw: next(answers, []))
    got = []
    search._jacred({"url": "https://jac.red"}, _rezero(), on_found=lambda rels: got.append(
        [r.hash[0] for r in rels]))
    # Японский оригинал с «Re» — не латинское название: раздачи без ромадзи — не чужие.
    assert got == [["1"], ["2"]]
    answers = iter([[_item("rutracker", "Re:Zero [ТВ-1]", h="1" * 40, year=2016, types=("anime",))]])
    warmed = []
    monkeypatch.setattr(search.swarm, "rutracker", lambda hs: warmed.append(list(hs)) or {})
    search.find(_rezero(), sources=[{"type": "jacred", "url": "https://jac.red"}])
    assert warmed[0] == ["1" * 40] and len(warmed) == 2          # заранее и в check_alive


def test_jacred_survives_failed_query(monkeypatch):
    def fake_get_json(url, timeout=None, **kw):
        if "altname" in url and "Starting" in url:
            raise search.net.NetError("HTTP 504")
        return [_item("rutor", "Re:Zero kara Hajimeru Isekai Seikatsu 4 [ТВ-4] (2026)",
                      year=2026, types=("anime",))]

    monkeypatch.setattr(search.net, "get_json", fake_get_json)
    logged = []
    rels = search.find(_rezero(), sources=[{"type": "jacred", "url": "https://jac.red"}],
                       log=logged.append)
    assert len(rels) == 1
    assert len(logged) == 1 and "504" in logged[0] and "Re:ZERO – Жизнь" in logged[0]


def _api(title, h, tracker="rutracker", seeders=12):
    """Раздача, как её отдаёт api.jacred.su."""
    return {"tracker": tracker, "source_url": f"https://{tracker}/t/9", "title": title,
            "size": 4 * 10 ** 9, "created_at": "2026-11-01T10:00:00Z", "seeders": seeders,
            "peers": 3, "magnet": f"magnet:?xt=urn:btih:{h}", "year": 2024, "video_type": "sdr",
            "quality": 1080, "voices": ["LostFilm"], "seasons": [1]}


def test_jacred_su_searches_its_api_with_key_in_header(monkeypatch):
    calls = []

    def fake(url, headers=None, timeout=None):
        calls.append((url, headers))
        return ({"results": [_api("Тёмная материя / Dark Matter (2024) WEB-DL 1080p", "1" * 40)]},
                {"X-RateLimit-Remaining": str(100 - len(calls))})

    monkeypatch.setattr(search.net, "get_json_headers", fake)
    logged = []
    rels = search._jacred({"type": "jacred", "url": "https://jacred.su", "apikey": "SECRET"},
                          _title(), log=logged.append)
    url, headers = calls[0]
    assert url.startswith("https://api.jacred.su/api/search?") and "query=" in url
    assert headers == {"Authorization": "Bearer SECRET"} and "SECRET" not in url
    # По одному названию за запрос, не больше трёх: поисков 100 в день.
    assert len(calls) == 2                       # у «Тёмной материи» два названия
    assert len(rels) == 1                        # одна раздача из обоих ответов
    r = rels[0]
    assert (r.seeds, r.peers, r.quality, r.voices) == (12, 3, 1080, ["LostFilm"])
    assert r.trackers == [["rutracker", "https://rutracker/t/9"]]
    assert logged == ["jacred.su: поисков на сегодня осталось 98"]


def test_jacred_su_refusal_stops_at_first_query(monkeypatch):
    calls = []

    def fake(url, headers=None, timeout=None):
        calls.append(url)
        raise search.net.NetError("HTTP 429", code=429)

    monkeypatch.setattr(search.net, "get_json_headers", fake)
    with pytest.raises(search.net.NetError, match="кончились"):
        search._jacred({"type": "jacred", "url": "https://api.jacred.su", "apikey": "K"}, _rezero())
    assert len(calls) == 1
    with pytest.raises(search.net.NetError, match="нужен личный ключ"):
        search._jacred({"type": "jacred", "url": "https://jacred.su"}, _rezero())
    assert len(calls) == 1


def test_jacred_su_queries_one_name_each_romaji_second():
    assert search._jacred_api_queries(_rezero()) == [
        "Re:ZERO – Жизнь с нуля в альтернативном мире", "Re:Zero kara Hajimeru Isekai Seikatsu",
        "Re:ZERO -Starting Life in Another World-"]


def test_jacred_key_goes_to_sources_file_first(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    assert search.jacred_key() == ""
    search.set_jacred_key(" KEY ")
    assert search.load_sources() == [{"type": "jacred", "url": "https://jacred.su", "apikey": "KEY"},
                                     {"type": "jacred", "url": "https://jac.red"}]
    assert search.jacred_key() == "KEY"
    search.set_jacred_key("")
    assert search.load_sources() == [{"type": "jacred", "url": "https://jac.red"}]


def test_year_from_title_when_source_has_none():
    item = {"magnet": "magnet:?xt=urn:btih:" + "a" * 40, "relased": 0, "types": ["docuserial"],
            "title": "Настояший детектив (Паутина любви) / 2025 / РУ / WEBRip"}
    rel = search._from_jacred(item)
    assert rel.year == 2025
    t = search.Title(kind=library.TV, tmdb_id=1, title="Детектив", year=2014,
                     seasons=[search.Season(1, 8, 2014), search.Season(2, 8, 2015)])
    assert not search.fits(rel, t, [1])                  # без года прошла бы под любым сезоном


def test_other_title_with_same_russian_name_dropped():
    t = search.Title(kind=library.TV, tmdb_id=46648, title="Настоящий детектив", year=2014,
                     original="True Detective", names=["Настоящий детектив", "True Detective"])
    show = {"title": "Настояший детектив (Паутина любви) / 2025 / РУ / WEBRip",
            "originalname": "Настояший детектив"}
    real = {"title": "Настоящий детектив [04x01 из 06] (2024) WEB-DL", "originalname": "True Detective"}
    eng = {"title": "Настояший детектив / True Detective / Сезон: 4", "originalname": "Настояший детектив"}
    assert search._other_title(show, t)
    assert not search._other_title(real, t) and not search._other_title(eng, t)
    shogun = search.Title(kind=library.TV, tmdb_id=1, title="Сёгун", year=2024, original="Shōgun",
                          names=["Сёгун", "Shōgun"])
    assert not search._other_title({"title": "Сёгун / Shogun [S01] (2024)"}, shogun)
    # Оригинал не латиницей (русский сериал, аниме) — не проверяется.
    noon = search.Title(kind=library.TV, tmdb_id=2, title="Полдень", year=2026, original="Полдень",
                        names=["Полдень", "Noon"])
    assert not search._other_title({"title": "Полдень (2026) WEB-DL 1080p"}, noon)


def test_in_library_by_tmdb_id(tmp_path):
    show = tmp_path / "tv" / "Тёмная материя (2024)"
    (show / "Season 01").mkdir(parents=True)
    (show / "tvshow.nfo").write_text('<tvshow><uniqueid type="tmdb">1</uniqueid></tvshow>',
                                     encoding="utf-8")
    for n in (1, 2, 3):
        (show / "Season 01" / f"Dark Matter - S01E0{n}.mkv").write_bytes(b"x")
    found, have = search.in_library(_title(), [str(tmp_path / "tv")])
    assert found == str(show) and have == {1: 3}


def test_library_dir_inside_collection_folder(tmp_path):
    film = tmp_path / "movies" / "Super Collection" / "City Hunter (2019) - Nicky Larson"
    film.mkdir(parents=True)
    (film / "movie.nfo").write_text('<movie><uniqueid type="tmdb">469274</uniqueid></movie>',
                                    encoding="utf-8")
    other = tmp_path / "movies" / "Dune (2021)"
    (other / "Extras").mkdir(parents=True)
    (other / "movie.nfo").write_text('<movie><uniqueid type="tmdb">438631</uniqueid></movie>',
                                     encoding="utf-8")
    roots = [str(tmp_path / "movies")]
    assert search.library_dir(library.MOVIE, 469274, roots) == str(film)
    assert search.library_dir(library.MOVIE, 438631, roots) == str(other)
    assert search.library_dir(library.MOVIE, 1, roots) == ""


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
    monkeypatch.setattr(search, "find", lambda title, sources=None, log=None, seasons=(): _releases([
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
    # В magnet дописаны трекеры: у ссылок kinozal и nnm-club их нет вовсе.
    assert [(m.split("&")[0], cat) for m, cat in added] == [("magnet:?xt=urn:btih:" + "2" * 40, "tv")]
    assert "tr=http%3A%2F%2Fbt4.t-ru.org" in added[0][0]
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


def test_search_any_year_in_brackets_goes_first(monkeypatch):
    import tmdb
    calls = []

    def call(path, key, **p):
        calls.append((path, p.get("query"), p.get("year") or p.get("first_air_date_year")))
        if path == "/search/multi":
            return {"results": [{"media_type": "tv", "id": 36046, "name": "Городской охотник",
                                 "first_air_date": "1987-04-06"},
                                {"media_type": "movie", "id": 45401, "title": "Леди охотник",
                                 "release_date": "1993-01-01"}]}
        if path == "/search/movie":
            return {"results": [{"id": 469274, "title": "Плейбой под прикрытием",
                                 "release_date": "2019-02-06"}]}
        return {"results": []}

    monkeypatch.setattr(tmdb, "_call", call)
    hits = metadata.search_any("City Hunter (2019)",
                               type("S", (), {"tmdb_key": "k", "meta_language": "ru-RU"})())
    assert hits[0].tmdb_id == 469274 and len(hits) == 3
    assert ("/search/multi", "City Hunter", None) in calls and ("/search/movie", "City Hunter", 2019) in calls


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
    assert res["ok"] and len(added) == 1 and added[0][1] == "movies"
    assert added[0][0].startswith(magnet + "&tr=")
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
    assert _search(c)["query"] == "The Apothecary Diaries (2023)"   # год — подсказка поиску
    st.tabs["torrents"].query = ""                         # поле стёрли — «Сканировать» вернёт
    c.post("/api/scan", json={"path": str(season)})       # сканирование может и не начаться
    assert _search(c)["query"] == "The Apothecary Diaries (2023)"


def test_folder_nfo_opens_its_title(web, tmp_path, monkeypatch):
    st, c, _ = web
    film = tmp_path / "movies" / "City Hunter (2019) - Nicky Larson et le parfum de Cupidon"
    film.mkdir(parents=True)
    (film / "movie.nfo").write_text('<movie><uniqueid type="tmdb">469274</uniqueid></movie>',
                                    encoding="utf-8")
    asked = []
    monkeypatch.setattr(metadata, "hit_by_id", lambda kind, tid, s: asked.append((kind, tid)) or
                        metadata.SearchHit(kind, tid, "Плейбой под прикрытием", 2019))
    monkeypatch.setattr(search, "title_info", lambda kind, tmdb_id, key: search.Title(
        kind=kind, tmdb_id=tmdb_id, title="Плейбой под прикрытием", year=2019))
    c.post("/api/path", json={"path": str(film)})
    assert _search(c)["query"] == "City Hunter (2019)"
    c.post("/api/torrents/search", json={"query": "City Hunter (2019)"})
    assert st.jobs.wait() and st.jobs.last.status == "done", st.jobs.last.error
    s = _search(c)
    assert asked == [(library.MOVIE, 469274)] and s["title"]["label"] == "Плейбой под прикрытием (2019)"
    assert len(s["hits"]) == 2                           # и найденное по названию — в «другой…»
    # Поменяли запрос — обычный поиск, .nfo ни при чём.
    c.post("/api/torrents/search", json={"query": "City Hunter"})
    assert st.jobs.wait() and asked == [(library.MOVIE, 469274)]


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


SIZE = 34_370_475_786
OLD, NEW = "05bb" * 10, "24e4" * 10


def test_twins_merged_into_one_card_alive_first():
    rt = search.Release(OLD, "Сезон 4 rutracker", f"magnet:?xt=urn:btih:{OLD}", size=SIZE, seasons=[4],
                        seeds=57, updated="2026-02-04", trackers=[["rutracker", "u1"]])
    kz = search.Release(NEW, "4 сезон kinozal", f"magnet:?xt=urn:btih:{NEW}", size=SIZE, seasons=[4],
                        seeds=3, updated="2026-07-22", trackers=[["kinozal", "u2"]],
                        voices=["Кубик в Кубе"])
    other = search.Release("cccc" * 10, "другая", "m", size=SIZE - 786)
    small = [search.Release(h * 40, "сэмпл", "m", size=1000) for h in "de"]

    def live(cards):
        for c in cards:
            c.dead, c.alive = c.hash == OLD, c.hash == NEW

    out = search.group([rt, kz, other] + small, live=live)
    assert len(out) == 4                                 # сэмплы по 1000 байт — не двойники
    card = out[0]
    assert card.hash == NEW and card.seeds == 3          # главная — живая, а не сидовая
    assert [t[0] for t in card.trackers] == ["kinozal", "rutracker"]
    assert card.alts == [{"hash": OLD, "magnet": f"magnet:?xt=urn:btih:{OLD}", "dead": True}]
    assert search.alternatives(card) == []               # мёртвую наперегонки не пускаем
    assert search.hashes(card) == {OLD, NEW} and card.voices == ["Кубик в Кубе"]


def test_twins_without_liveness_by_seeds():
    a = search.Release(OLD, "a", "ma", size=SIZE, seasons=[4], seeds=5, trackers=[["rutracker", "u1"]])
    b = search.Release(NEW, "b", "mb", size=SIZE, seasons=[4], seeds=9, trackers=[["kinozal", "u2"]])
    card = search.group([a, b])[0]
    assert card.hash == NEW and search.alternatives(card) == [{"hash": OLD, "magnet": "ma"}]


def test_same_rounded_size_other_content_not_twins():
    # jac.red: «3.90 GB» → 4187593113 — у 1-го сезона в HDRip и у одной серии 4-го.
    size = 4_187_593_113
    s1 = search.Release("4f7b" * 10, "сезон 1, серии 1-8 из 8", "m1", size=size, quality=1080,
                        seasons=[1], episodes=[1, 8, 8], trackers=[["nnmclub", "u1"]])
    s4 = search.Release("6432" * 10, "[04x01 из 06] от NewComers", "m2", size=size, quality=1080,
                        seasons=[4], episodes=[1, 1, 6], voices=["NewComers"],
                        trackers=[["rutor", "u2"]])
    lf = search.Release("7777" * 10, "[04x01 из 06] LostFilm", "m3", size=size, quality=1080,
                        seasons=[4], episodes=[1, 1, 6], voices=["LostFilm"],
                        trackers=[["lostfilm", "u3"]])
    kz = search.Release("8888" * 10, "4 сезон: 1 серия из 6 (NewComers)", "m4", size=size,
                        seasons=[4], episodes=[1, 1, 6], voices=["NewComers", "Original"],
                        trackers=[["kinozal", "u4"]])
    out = search.group([s1, s4, lf, kz])
    assert len(out) == 3
    card = next(r for r in out if r.hash in (s4.hash, kz.hash))
    assert search.hashes(card) == {s4.hash, kz.hash} and card.seasons == [4]
    first = next(r for r in out if r.hash == s1.hash)
    assert first.alts == [] and first.seasons == [1] and len(first.trackers) == 1


def test_twins_without_season_only_movies_of_same_year():
    size = 536_870_912                                   # «0.50 GB»
    ep = [search.Release(h * 40, f"Настоящий детектив ({name}) / 2025 / WEBRip", "m", size=size,
                         year=2025, types=["serial"], trackers=[["kinozal", h]])
          for h, name in (("1", "Паутина любви"), ("2", "Голос из преисподней"))]
    assert len(search.group(ep)) == 2                    # серии без сезона — не двойники
    film = [search.Release(h * 40, "Фильм", "m", size=8 * 2**30, year=y, types=["movie"],
                           trackers=[[t, h]]) for h, y, t in (("3", 2024, "rutracker"),
                                                              ("4", 2024, "kinozal"),
                                                              ("5", 2021, "rutor"))]
    out = search.group(film)
    assert len(out) == 2 and search.hashes(out[0]) == {"3" * 40, "4" * 40}


def test_dead_card_races_all_links():
    a = search.Release(OLD, "a", "ma", size=SIZE, seasons=[4], dead=True)
    b = search.Release(NEW, "b", "mb", size=SIZE, seasons=[4], dead=True)
    card = search.group([a, b])[0]
    assert card.dead and len(search.alternatives(card)) == 1


def test_stale_twin_gives_only_its_page():
    # kinozal: новое название и размер, но magnet прежней версии (хеш как у старой).
    old = search.Release(OLD, "Сезон 4 [1-5 из 6]", "m1", size=20 * 2**30, updated="2026-02-01",
                         trackers=[["rutracker", "u1"]])
    stale = search.Release(OLD, "Сезон 4 [1-6 из 6]", "m2", size=SIZE, seasons=[4], updated="2026-03-01",
                           trackers=[["kinozal", "u2"]])
    new = search.Release(NEW, "Сезон 4 [1-6 из 6]", "m3", size=SIZE, seasons=[4], updated="2026-02-20",
                         trackers=[["rutracker", "u3"]])
    out = search.group([old, stale, new])
    card = next(r for r in out if r.hash == NEW)
    assert not card.stale and card.alts == [] and ["kinozal", "u2"] in card.trackers
    assert len(out) == 2                                 # старая версия — своей строкой


def test_same_hash_trackers_joined_in_magnet():
    a = search.Release(OLD, "a", f"magnet:?xt=urn:btih:{OLD}&tr=udp%3A%2F%2Fopentor.net%3A6969",
                       trackers=[["rutor", "u1"]])
    b = search.Release(OLD, "b",
                       f"magnet:?xt=urn:btih:{OLD}&tr=http%3A%2F%2Fbt4.t-ru.org%2Fann%3Fmagnet",
                       trackers=[["rutracker", "u2"]])
    card = search.group([a, b])[0]
    assert search.swarm.trackers_of(card.magnet) == ["udp://opentor.net:6969",
                                                     "http://bt4.t-ru.org/ann?magnet"]


def test_check_alive(monkeypatch):
    dead = search.Release("1" * 40, "rt", "m", seeds=845, trackers=[["rutracker", "u"]])
    rutor = search.Release("2" * 40, "rutor", "m", seeds=103, trackers=[["rutor", "u"]])
    known = search.Release("3" * 40, "kz", "m", seeds=3, trackers=[["kinozal", "u"]])
    popular = search.Release("4" * 40, "korsars", "m", seeds=152, seeds_at="2026-08-18",
                             trackers=[["korsars", "u"], ["rutracker", "u2"]])
    quiet = search.Release("5" * 40, "rutor", "m", seeds=40, trackers=[["rutor", "u"]])
    own = search.Release("6" * 40, "свой торрент kinozal", "m", seeds=22, trackers=[["kinozal", "u"]])
    asked, scraped = [], []
    ot, op = search.swarm.RUTOR, "tracker.opentrackr.org"

    def scrape(hs):
        scraped.extend(hs)
        return {"1" * 40: {op: 0}, "2" * 40: {ot: 0, op: 1}, "3" * 40: {op: 0},
                "4" * 40: {op: 50}, "5" * 40: {op: 0}, "6" * 40: {op: 0}}

    T = search.swarm.Tracked
    monkeypatch.setattr(search.swarm, "scrape", scrape)
    monkeypatch.setattr(search.swarm, "rutracker",
                        lambda hs: asked.extend(hs) or {"1" * 40: T(False), "3" * 40: T(True),
                                                        "4" * 40: T(True, 30), "6" * 40: T(False)})
    search.check_alive([dead, rutor, known, popular, quiet, own])
    assert scraped[:2] == ["2" * 40, "5" * 40]           # раздачи rutor — первыми (для opentor)
    assert asked == ["1" * 40, "4" * 40, "6" * 40, "3" * 40]   # закрытые, сидовые — первыми
    assert not own.dead and not own.alive                # rutracker не обязан знать чужой торрент
    assert dead.dead and not dead.alive and (dead.seeds, dead.seeds_live) == (0, True)   # не 845
    assert rutor.seeds == 1 and rutor.seeds_live and not rutor.dead   # opentor — его трекер
    assert quiet.seeds == 40 and not quiet.seeds_live    # opentor промолчал — число источника
    assert known.alive and known.seeds == 3 and not known.seeds_live
    assert known.rutracker_peers is None                 # участников у него не спрашивали
    assert popular.seeds == 152 and not popular.seeds_live and popular.alive
    assert popular.rutracker_peers == 30


def test_rutracker_peers_cap_stale_seeds(monkeypatch):
    def rt(h, **sites):
        found = [search.Release(h * 40, h, "m", seeds=n, seeds_at="2026-02-04",
                                trackers=[[site, f"u{site}"]]) for site, n in sites.items()]
        return search.group(found)[0]

    old = rt("1", rutracker=41)
    empty = rt("2", rutracker=16)
    mixed = rt("3", rutracker=41, kinozal=20)            # число карточки — от rutracker
    kz = rt("5", rutracker=8, kinozal=35)                # число карточки — от kinozal
    big = rt("4", rutracker=125)
    T = search.swarm.Tracked
    monkeypatch.setattr(search.swarm, "rutracker", lambda hs: {
        "1" * 40: T(True, 13), "2" * 40: T(True, 0), "3" * 40: T(True, 0), "4" * 40: T(True, 30),
        "5" * 40: T(True, 2)})
    search.check_alive([old, empty, mixed, kz, big])
    assert old.seeds == 13 and old.seeds_at != "2026-02-04" and old.alive   # 41 — устарело
    assert empty.dead and not empty.alive                # у rutracker ни одного участника
    assert mixed.seeds == 20 and mixed.seeds_at == "2026-02-04" and not mixed.dead
    assert kz.seeds == 35                                # у kinozal свои участники
    assert big.seeds == 125 and big.rutracker_peers == 30    # 30 — «30 и больше»


def test_nnm_seeds_now_and_dead_only_when_every_site_checked(monkeypatch):
    def card(h, **sites):
        return search.group([search.Release(h * 40, h, "m", seeds=n, seeds_at="2026-02-04",
                                            trackers=[[site, f"u{site}"]]) for site, n in sites.items()])[0]

    old = card("1", nnmclub=20)                          # у nnm-club сейчас 12
    gone = card("2", nnmclub=20, rutracker=24)           # nnm-club не знает, rutracker — тоже
    kz = card("3", nnmclub=20, kinozal=5)                # kinozal не проверить — не мёртвая
    stale_rt = card("4", rutracker=24, nnmclub=3)        # 05bb: rutracker забыл, у nnm-club 2 сида
    T = search.swarm.Tracked
    monkeypatch.setattr(search.swarm, "nnm", lambda hs: {"1" * 40: 12, "2" * 40: None,
                                                         "3" * 40: 0, "4" * 40: 2})
    monkeypatch.setattr(search.swarm, "rutracker", lambda hs: {"2" * 40: T(False), "4" * 40: T(False)})
    search.check_alive([old, gone, kz, stale_rt])
    assert (old.seeds, old.seeds_live, old.alive) == (12, True, True)
    assert gone.dead
    assert not kz.dead and kz.seeds == 5 and not kz.seeds_live
    assert stale_rt.alive and not stale_rt.dead and stale_rt.seeds == 2


def test_selected_seasons_checked_first(monkeypatch):
    asked = []
    monkeypatch.setattr(search.swarm, "rutracker", lambda hs: asked.extend(hs) or {})
    rels = [search.Release(h * 40, h, "m", seeds=n, seasons=[s], trackers=[["rutracker", h]])
            for h, n, s in (("1", 100, 1), ("2", 5, 3), ("3", 50, 2))]
    search.check_alive(rels, seasons=[3])
    assert asked == ["2" * 40, "1" * 40, "3" * 40]


def test_dead_link_ranked_last_and_not_recommended():
    t = _title()
    dead = search.Release("1" * 40, "Тёмная материя [S01] 1080p", "m", seeds=845, quality=1080,
                          seasons=[1], dead=True)
    alive = search.Release("2" * 40, "Тёмная материя [S01] 1080p", "m", seeds=4, quality=1080,
                           seasons=[1])
    ranked = search.rank([dead, alive], t, search.Prefs(), [1])
    assert ranked[0] is alive and alive.recommended and not dead.recommended
    assert any("мёртвая ссылка" in n for n in dead.notes)


def test_seeds_date_from_same_record():
    rt = search.Release("05bb" * 10, "rutracker", "m", seeds=24, updated="2026-02-04",
                        seeds_at="2026-02-04", trackers=[["rutracker", "u1"]])
    nnm = search.Release("05bb" * 10, "nnmclub", "m", seeds=57, updated="2026-02-04",
                         seeds_at="2026-02-04", trackers=[["nnmclub", "u2"]])
    bit = search.Release("05bb" * 10, "bitru", "m", seeds=4, updated="2026-08-11",
                         seeds_at="2026-08-11", trackers=[["bitru", "u3"]])
    g = search.group([rt, nnm, bit])[0]
    assert (g.seeds, g.seeds_at, g.updated) == (57, "2026-02-04", "2026-08-11")


# ------------------------------------------------- что вышло: статус, сезоны --

def _airing(status="Returning Series", last=(2, 7), nxt=(2, 8, "2026-11-14")):
    return search.Title(kind=library.TV, tmdb_id=1, title="Сериал", year=2024, status=status,
                        seasons=[search.Season(1, 10, 2024), search.Season(2, 19, 2026),
                                 search.Season(3, 0, None)],
                        last_aired=last, next_air=nxt)


def test_season_state_and_status_line():
    import datetime
    today = datetime.date(2026, 11, 7)
    t = _airing()
    assert [search.season_state(t, n) for n in (1, 2, 3)] == [
        ("aired", 10), ("running", 7), ("announce", 0)]
    assert search.status_text(t, today) == "Идёт: следующая серия 14.11 (сезон 2, серия 8)"
    t = _airing(last=(2, 19), nxt=(3, 1, "2027-01-10"))
    assert search.status_text(t, today) == "Сезон 3 выходит 10.01.2027"
    assert search.announce_date(t, 3, today) == "10.01.2027"
    assert search.status_text(_airing(last=(2, 19), nxt=(0, 0, ""))) == \
        "Продлён: сезон 3 — дата пока неизвестна"
    ended = _airing("Ended", (2, 19), (0, 0, ""))
    # TMDb «Ended», а сезон 3 анонсирован (TVmaze завёл) — продлён, не завершён.
    assert search.status_text(ended) == "Продлён: сезон 3 — дата пока неизвестна"
    ended.seasons = ended.seasons[:2]
    assert search.status_text(ended) == "Завершён"
    # TMDb «Ended», а TVmaze — «Running» и нового сезона нет (как у «Re:Zero»).
    ended.numbering.status = "Running"
    assert search.status_text(ended) == "Продолжение пока не объявлено"
    # TMDb не знает, что вышло, — сезоны считаются вышедшими, как раньше.
    unknown = _airing(last=(0, 0), nxt=(0, 0, ""))
    assert search.season_state(unknown, 2) == ("aired", 19)


def test_voice_lag_by_studio():
    t = _airing()
    rels = [search.Release("a" * 40, "Сериал [02x01-05 из 19]", "m", seasons=[2],
                           episodes=[1, 5, 19], voices=["AniLibria"]),
            search.Release("b" * 40, "Сериал [02x01-04 из 19]", "m", seasons=[2],
                           episodes=[1, 4, 19], voices=["Dream Cast"]),
            search.Release("c" * 40, "Сериал [02x01-07 из 19]", "m", seasons=[2],
                           episodes=[1, 7, 19], voices=["Мёртвая"], dead=True),
            search.Release("d" * 40, "Show S02E07 1080p", "m", seasons=[2], episodes=[7, 7, 19])]
    assert search.voice_lag(t, rels, [2]) == (
        "Сезон 2 идёт: в оригинале вышло 7 из 19, в русских раздачах — до 5 (AniLibria 5, Dream Cast 4)")
    assert search.voice_lag(t, rels, [3]) == ""                  # анонс — нечего сравнивать
    assert search.voice_lag(t, [], [2]) == "Сезон 2 идёт: в оригинале вышло 7 из 19, русских раздач с ним пока нет"
    # Сезон вышел целиком, а русские раздачи отстают — тоже сказать; догнали — молчать.
    full = [search.Release("e" * 40, "Сериал [01x01-08 из 10]", "m", seasons=[1],
                           episodes=[1, 8, 10], voices=["LostFilm"])]
    assert search.voice_lag(t, full, [1]) == \
        "Сезон 1 вышел целиком (10 сер.), в русских раздачах — до 8 (LostFilm 8)"
    full[0].episodes = [1, 10, 10]
    assert search.voice_lag(t, full, [1]) == ""


def test_specials_note_count_and_length():
    t = _airing()
    t.specials = [search.Special(1, "Break Time 1-1", 2016, 2), search.Special(2, "Break Time 1-4", 2016, 3),
                  search.Special(3, "Memory Snow: Manner Movie", 2018, 3)]
    text, hint = search.specials_note(t, have=1)
    assert text == "Спецвыпуски в TMDb: 3 (короткие, 2–3 мин), у вас: 1"
    assert hint.splitlines()[2] == "3. Memory Snow: Manner Movie (2018), 3 мин"
    t.specials.append(search.Special(4, "Making of", 2019, 60))
    assert search.specials_note(t)[0] == "Спецвыпуски в TMDb: 4 (2–60 мин)"
    assert search.specials_note(_title()) == ("", "")


def test_versions_from_release_titles():
    assert search.versions_of("Царство небесное (Режиссёрская версия) / Kingdom of Heaven [Director's Cut]") \
        == ["режиссёрская"]
    assert search.versions_of("Бегущий по лезвию / Blade Runner (Final Cut + Theatrical)") == \
        ["театральная", "Final Cut"]
    assert search.versions_of("Апокалипсис сегодня Redux") == ["Redux"]
    assert search.versions_of("Дюна: Часть вторая 2024 WEB-DL 2160p") == []


def test_version_filter_in_search(web, monkeypatch):
    st, c, _ = web
    monkeypatch.setattr(search, "find", lambda title, sources=None, log=None, seasons=(): _releases([
        _item("rutor", "Тёмная материя [S01] (Режиссёрская версия)", h="1" * 40, seasons=[1]),
        _item("rutor", "Тёмная материя [S01]", h="2" * 40, seasons=[1])]))
    c.post("/api/torrents/search", json={"query": "Тёмная материя", "kind": "tv"})
    assert st.jobs.wait() and st.jobs.last.status == "done", st.jobs.last.error
    c.post("/api/torrents/search/options", json={"quality": 1080})
    s = _search(c)
    assert s["versions"] == [["режиссёрская", 1]] and len(s["releases"]) == 2
    c.post("/api/torrents/search/options", json={"version": "режиссёрская"})
    s = _search(c)
    assert [r["hash"] for r in s["releases"]] == ["1" * 40] and s["releases"][0]["versions"] == ["режиссёрская"]


def test_voice_filter_keeps_releases_with_unknown_voices(web, monkeypatch):
    # rutracker студий в названии не пишет — такую раздачу фильтр не прячет, а ставит
    # ниже и без ⭐; раздача с другой известной озвучкой — мимо.
    st, c, _ = web
    monkeypatch.setattr(search, "find", lambda title, sources=None, log=None, seasons=(): _releases([
        _item("rutracker", "Тёмная материя [S01] [RUS(int)]", h="1" * 40, sid=90),
        _item("rutor", "Тёмная материя [S01] | Crunchyroll", h="2" * 40, sid=5, voices=["Crunchyroll"]),
        _item("rutor", "Тёмная материя [S01] | AniDUB", h="3" * 40, sid=50, voices=["AniDUB"])]))
    c.post("/api/torrents/search", json={"query": "Тёмная материя", "kind": "tv"})
    assert st.jobs.wait() and st.jobs.last.status == "done", st.jobs.last.error
    c.post("/api/torrents/search/options", json={"quality": 1080, "voice": "Crunchyroll"})
    s = _search(c)
    assert [(r["hash"][0], r["recommended"], r["voice_unknown"]) for r in s["releases"]] == [
        ("2", True, False), ("1", False, True)]
    assert dict(s["qualities"])[1080] == 2
