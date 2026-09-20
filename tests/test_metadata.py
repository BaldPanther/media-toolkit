"""Тесты разбора ответов API в модели и выбора картинок — без сети.

Ответы TMDb/Fanart.tv подставляются вместо `_get_json` у соответствующего клиента,
как это уже сделано в тестах online.py.
"""
import artwork
import fanart
import metaconf
import metadata
import omdb
import tmdb
from metaconf import POLICY_MISSING, POLICY_OVERWRITE


def settings(**kw):
    s = metaconf.Settings(tmdb_key="K")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def cand(kind, lang="", vote=0.0, width=0, source="TMDb", season=None):
    return metadata.ArtCandidate(kind=kind, url=f"u-{lang}-{vote}-{width}-{source}",
                                 thumb_url="t", lang=lang, width=width, height=width,
                                 vote=vote, source=source, season=season)


# ----------------------------------------------------- ранжирование арта --

def test_rank_prefers_language_over_votes():
    # Язык важнее оценки: русский постер с оценкой 1 обходит английский с 9.
    ru = cand("poster", "ru", vote=1.0, width=500)
    en = cand("poster", "en", vote=9.0, width=2000)
    assert metadata.rank_art([en, ru], ["ru", "en", ""])[0] is ru
    assert metadata.rank_art([en, ru], ["en", "ru", ""])[0] is en


def test_rank_prefers_tmdb_over_fanart_within_language():
    t = cand("poster", "ru", vote=1.0, source="TMDb")
    f = cand("poster", "ru", vote=99.0, source="Fanart.tv")
    assert metadata.rank_art([f, t], ["ru"])[0] is t


def test_rank_falls_back_to_votes_then_resolution():
    a = cand("poster", "ru", vote=5.0, width=1000)
    b = cand("poster", "ru", vote=5.0, width=2000)
    c = cand("poster", "ru", vote=7.0, width=500)
    assert [x.width for x in metadata.rank_art([a, b, c], ["ru"])] == [500, 2000, 1000]


def test_best_art_filters_by_kind_and_season():
    pool = [cand("poster", "ru"), cand("clearlogo", "en"),
            cand("seasonposter", "ru", season=1), cand("seasonposter", "en", season=2)]
    assert metadata.best_art(pool, "clearlogo", ["ru", "en", ""]).lang == "en"
    assert metadata.best_art(pool, "seasonposter", ["ru", "en", ""], season=2).lang == "en"
    assert metadata.best_art(pool, "seasonposter", ["ru"], season=3) is None


def test_best_art_none_when_nothing_matches():
    assert metadata.best_art([], "poster", ["ru"]) is None


def test_unknown_language_sorts_last():
    known = cand("poster", "ru")
    other = cand("poster", "de")
    assert metadata.rank_art([other, known], ["ru", "en", ""])[0] is known


# --------------------------------------------- разбор картинок из TMDb --

def test_tmdb_images_become_candidates(monkeypatch):
    monkeypatch.setattr(tmdb, "_get_json", lambda url, **kw: {
        "posters": [{"file_path": "/p.jpg", "iso_639_1": "ru",
                     "vote_average": 5.3, "width": 1000, "height": 1500}],
        "backdrops": [{"file_path": "/b.jpg", "iso_639_1": None,
                       "vote_average": 5.0, "width": 3840, "height": 2160}],
        "logos": [{"file_path": "/l.png", "iso_639_1": "en",
                   "vote_average": 1.0, "width": 800, "height": 200}],
    })
    art = metadata._collect_tmdb_art("movie", 1, "K", [], settings())
    by_kind = {c.kind: c for c in art}
    assert by_kind["poster"].lang == "ru"
    assert by_kind["poster"].url.endswith("/original/p.jpg")
    assert by_kind["poster"].thumb_url.endswith("/w342/p.jpg")
    assert by_kind["fanart"].lang == ""          # null → «без текста»
    assert by_kind["clearlogo"].width == 800


def test_svg_logos_are_dropped(monkeypatch):
    # Kodi не умеет SVG, а TMDb их отдаёт вперемешку с PNG.
    monkeypatch.setattr(tmdb, "_get_json", lambda url, **kw: {
        "logos": [{"file_path": "/l.svg", "iso_639_1": "en", "width": 800},
                  {"file_path": "/l.png", "iso_639_1": "en", "width": 800}],
    })
    art = metadata._collect_tmdb_art("movie", 1, "K", [], settings())
    assert [c.url.rsplit("/", 1)[-1] for c in art] == ["l.png"]


def test_season_posters_requested_only_for_existing_seasons(monkeypatch):
    seen = []

    def fake(url, **kw):
        seen.append(url)
        return {"posters": [{"file_path": "/s.jpg", "iso_639_1": "ru", "width": 1000}]}

    monkeypatch.setattr(tmdb, "_get_json", fake)
    art = metadata._collect_tmdb_art("tv", 7, "K", [1, 2], settings())
    seasons = sorted(c.season for c in art if c.kind == "seasonposter")
    assert seasons == [1, 2]
    assert sum("/season/" in u for u in seen) == 2


# ------------------------------------------------- разбор Fanart.tv --

def test_fanart_candidates_and_preview_url(monkeypatch):
    monkeypatch.setattr(fanart, "_get_json", lambda url, **kw: {
        "hdtvlogo": [{"url": "https://assets.fanart.tv/fanart/tv/1/hdtvlogo/x.png",
                      "lang": "ru", "likes": "7"}],
        "seasonposter": [{"url": "https://assets.fanart.tv/fanart/tv/1/seasonposter/s.jpg",
                          "lang": "en", "likes": "2", "season": "1"}],
    })
    art = metadata._collect_fanart_art("tv", {"tvdb": "389597"}, [1],
                                       settings(fanart_key="F"))
    logo = [c for c in art if c.kind == "clearlogo"][0]
    assert logo.source == "Fanart.tv" and logo.vote == 7.0
    assert "/preview/" in logo.thumb_url
    season = [c for c in art if c.kind == "seasonposter"][0]
    assert season.season == 1


def test_fanart_skipped_without_key():
    assert metadata._collect_fanart_art("tv", {"tvdb": "1"}, [], settings()) == []


def test_fanart_tv_skipped_without_tvdb_id():
    # TV-эндпоинт Fanart.tv адресуется только TVDb-ID; без него запроса нет.
    assert metadata._collect_fanart_art("tv", {"tmdb": "1"}, [],
                                        settings(fanart_key="F")) == []


# ------------------------------------------------------------- OMDb --

def test_omdb_rating_parses_votes(monkeypatch):
    monkeypatch.setattr(omdb, "_get_json", lambda url, **kw: {
        "Response": "True", "imdbRating": "7.5", "imdbVotes": "573,539"})
    assert omdb.rating("tt1", "K") == (7.5, 573539)


def test_omdb_na_rating_is_none(monkeypatch):
    monkeypatch.setattr(omdb, "_get_json", lambda url, **kw: {
        "Response": "True", "imdbRating": "N/A", "imdbVotes": "N/A"})
    assert omdb.rating("tt1", "K") == (None, None)


def test_omdb_without_key_does_not_call(monkeypatch):
    monkeypatch.setattr(omdb, "_get_json", lambda url, **kw: 1 / 0)
    assert omdb.rating("tt1", "") == (None, None)


# ------------------------------------------------------------ поиск --

def test_search_maps_movie_and_tv_fields(monkeypatch):
    monkeypatch.setattr(tmdb, "_get_json", lambda url, **kw: {"results": [
        {"id": 68421, "name": "Altered Carbon", "first_air_date": "2018-02-02",
         "overview": "…", "poster_path": "/p.jpg"}]})
    hit = metadata.search("tv", "Altered Carbon", settings())[0]
    assert hit.tmdb_id == 68421 and hit.year == 2018
    assert hit.label() == "Altered Carbon (2018)"
    assert hit.poster_url.endswith("/w185/p.jpg")


# ------------------------------------------------- имена файлов арта --

def test_target_names_match_library():
    assert artwork.target_name("poster") == "poster.jpg"
    assert artwork.target_name("fanart") == "fanart.jpg"
    assert artwork.target_name("clearlogo") == "clearlogo.png"
    assert artwork.target_name("seasonposter", 2) == "season02-poster.jpg"
    assert artwork.target_name("seasonposter", 0) == "season-specials-poster.jpg"


def test_plan_art_skips_existing_by_default(tmp_path):
    (tmp_path / "poster.jpg").write_bytes(b"old")
    chosen = {("poster", None): cand("poster", "ru"),
              ("fanart", None): cand("fanart", "")}
    tasks = {t.kind: t for t in artwork.plan_art(tmp_path, chosen, POLICY_MISSING)}
    assert tasks["poster"].action == artwork.DO_SKIP
    assert tasks["fanart"].action == artwork.DO_DOWNLOAD


def test_plan_art_overwrite_policy(tmp_path):
    (tmp_path / "poster.jpg").write_bytes(b"old")
    chosen = {("poster", None): cand("poster", "ru")}
    tasks = artwork.plan_art(tmp_path, chosen, POLICY_OVERWRITE)
    assert tasks[0].action == artwork.DO_REPLACE


def test_download_skips_and_reports(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(artwork.net, "download_file",
                        lambda url, dest, **kw: calls.append(dest) or 10)
    chosen = {("poster", None): cand("poster", "ru"),
              ("fanart", None): cand("fanart", "")}
    (tmp_path / "poster.jpg").write_bytes(b"old")
    tasks = artwork.plan_art(tmp_path, chosen, POLICY_MISSING)
    # Порядок задач осмысленный: постер, фанарт, логотип, постеры сезонов.
    assert [t.kind for t in tasks] == ["poster", "fanart"]
    results = artwork.download(tasks)
    assert [r.skipped for r in results] == [True, False]
    assert calls == [tmp_path / "fanart.jpg"]


def test_download_reports_network_error(tmp_path, monkeypatch):
    def boom(url, dest, **kw):
        raise artwork.net.NetError("сеть упала")

    monkeypatch.setattr(artwork.net, "download_file", boom)
    tasks = artwork.plan_art(tmp_path, {("poster", None): cand("poster", "ru")})
    result = artwork.download(tasks)[0]
    assert not result.ok and "сеть упала" in result.error


def test_chosen_urls_split_title_and_season_art():
    chosen = {("poster", None): cand("poster", "ru"),
              ("seasonposter", 1): cand("seasonposter", "ru", season=1)}
    tasks = artwork.plan_art("/tmp/x", chosen)
    art, seasons = artwork.chosen_urls(tasks)
    assert set(art) == {"poster"} and set(seasons) == {1}


# --------------------------------- правила выбора по видам арта --

def test_fanart_prefers_resolution_over_language():
    # Регрессия с живых данных Altered Carbon: русский фон 1280×720 обходил
    # безтекстовый 4K. На фоне надписей нет, язык там ни о чём не говорит.
    ru_small = cand("fanart", "ru", vote=0.0, width=1280)
    plain_4k = cand("fanart", "", vote=4.8, width=3840)
    assert metadata.rank_art([ru_small, plain_4k], ["ru", "en", ""])[0] is plain_4k


def test_fanart_prefers_textless_over_titled():
    plain = cand("fanart", "", width=1920)
    titled = cand("fanart", "en", width=1920)
    assert metadata.rank_art([titled, plain], ["en", "ru", ""])[0] is plain


def test_tiny_logo_loses_to_big_one_despite_language():
    # Регрессия: единственный русский логотип 425×46 обходил английский 2241×1233.
    ru_tiny = cand("clearlogo", "ru", width=425)
    en_big = cand("clearlogo", "en", width=2241)
    assert metadata.rank_art([ru_tiny, en_big], ["ru", "en", ""])[0] is en_big


def test_language_still_wins_for_posters_of_sane_size():
    ru = cand("poster", "ru", vote=0.0, width=1000)
    en = cand("poster", "en", vote=9.0, width=2000)
    assert metadata.rank_art([ru, en], ["ru", "en", ""])[0] is ru


def test_unknown_width_is_not_treated_as_small():
    # Fanart.tv размеров не отдаёт, но картинки там крупные.
    unknown = cand("clearlogo", "ru", source="Fanart.tv")
    unknown.width = unknown.height = None
    tiny = cand("clearlogo", "ru", width=100)
    assert metadata.rank_art([tiny, unknown], ["ru"])[0] is unknown


def test_tv_runtime_falls_back_to_episode_median(monkeypatch):
    # У Altered Carbon episode_run_time пустой, а Kodi ждёт число.
    def fake(url, **kw):
        if "/season/" in url:
            return {"episodes": [
                {"episode_number": 1, "name": "A", "runtime": 70},
                {"episode_number": 2, "name": "B", "runtime": 55},
                {"episode_number": 3, "name": "C", "runtime": 57},
            ]}
        if "/tv/" in url:
            return {"id": 1, "name": "Show", "episode_run_time": [],
                    "external_ids": {}, "first_air_date": "2018-02-02"}
        return {}

    monkeypatch.setattr(tmdb, "_get_json", fake)
    info = metadata.fetch("tv", 1, settings(meta_language="en-US"), seasons=[1])
    assert info.runtime == 57


# ------------------------------- альтернативная нумерация (episode groups) --

def _group(name, episodes):
    return {"name": name, "episodes": episodes}


def _ep(season, number):
    return {"season_number": season, "episode_number": number,
            "name": f"S{season}E{number}"}


SOLO_GROUP = {"groups": [
    _group("Season 1", [_ep(1, n) for n in range(1, 13)]),      # 12 серий
    _group("Season 2", [_ep(1, n) for n in range(13, 26)]),     # 13 серий
]}


def test_renumbering_map_matches_disk_layout():
    # Реальный случай Solo Leveling: на диске два сезона 12 и 13,
    # в TMDb один из 25. S02E01 должен указать на S01E13.
    mapping = metadata.renumbering_map(SOLO_GROUP, {1: 12, 2: 13})
    assert mapping[(2, 1)] == (1, 13)
    assert mapping[(2, 13)] == (1, 25)
    assert mapping[(1, 1)] == (1, 1)


def test_renumbering_skips_season_with_wrong_size():
    # Если число серий не сошлось, сезон не сопоставляем: неверная привязка
    # записала бы в .nfo описания чужих серий.
    mapping = metadata.renumbering_map(SOLO_GROUP, {1: 12, 2: 99})
    assert (2, 1) not in mapping
    assert (1, 1) in mapping


def test_group_season_number_from_name_then_order():
    assert metadata._group_season_number({"name": "Season 2"}, 0) == 2
    assert metadata._group_season_number({"name": "Part Two", "order": 1}, 5) == 2
    assert metadata._group_season_number({"name": "Прочее"}, 3) == 4


def test_find_renumbering_picks_best_matching_group(monkeypatch):
    # Две группы: «Air Date» делит 13+13, «Seasons» — 12+13. Диск говорит 12+13.
    air_date = {"groups": [
        _group("Season 1", [_ep(1, n) for n in range(1, 14)]),
        _group("Season 2", [_ep(1, n) for n in range(13, 26)]),
    ]}
    monkeypatch.setattr(tmdb, "episode_groups", lambda tid, key: [
        {"id": "air", "name": "Air Date"}, {"id": "seasons", "name": "Seasons"}])
    monkeypatch.setattr(tmdb, "episode_group",
                        lambda gid, key, language="en-US":
                        air_date if gid == "air" else SOLO_GROUP)
    mapping = metadata.find_renumbering(127532, "K", {1: 12, 2: 13})
    assert mapping[(2, 1)] == (1, 13)
    assert mapping[(1, 12)] == (1, 12)      # взята группа «Seasons», не «Air Date»


def test_find_renumbering_empty_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(tmdb, "episode_groups", lambda tid, key: [{"id": "g"}])
    monkeypatch.setattr(tmdb, "episode_group",
                        lambda gid, key, language="en-US": SOLO_GROUP)
    assert metadata.find_renumbering(1, "K", {1: 7, 2: 7}) == {}


def test_find_renumbering_survives_network_error(monkeypatch):
    def boom(*a, **kw):
        raise metadata.net.NetError("нет сети")

    monkeypatch.setattr(tmdb, "episode_groups", boom)
    assert metadata.find_renumbering(1, "K", {1: 12}) == {}


def test_fetch_uses_group_and_keeps_disk_numbering(monkeypatch):
    # Сквозной сценарий: сезона 2 в TMDb нет, он берётся из группы, но в модели
    # остаётся под дисковыми номерами — иначе Kodi не свяжет .nfo с файлом.
    def fake(url, **kw):
        if "/season/2" in url:
            return {"episodes": []}
        if "/season/1" in url:
            return {"episodes": [{"episode_number": n, "name": f"Episode {n}",
                                  "overview": f"описание {n}", "id": 1000 + n}
                                 for n in range(1, 26)]}
        if "/tv/" in url:
            return {"id": 127532, "name": "Solo Leveling", "external_ids": {},
                    "first_air_date": "2024-01-07", "episode_run_time": []}
        return {}

    monkeypatch.setattr(tmdb, "_get_json", fake)
    monkeypatch.setattr(tmdb, "episode_groups", lambda tid, key: [{"id": "g"}])
    monkeypatch.setattr(tmdb, "episode_group",
                        lambda gid, key, language="en-US": SOLO_GROUP)

    info = metadata.fetch("tv", 127532, settings(meta_language="en-US"),
                          seasons=[1, 2], season_sizes={1: 12, 2: 13})
    ep = info.episodes[(2, 1)]
    assert (ep.season, ep.episode) == (2, 1)        # нумерация диска
    assert ep.tmdb_id == 1013                        # данные 13-й серии TMDb
    assert ep.plot == "описание 13"


def test_fetch_without_sizes_does_not_query_groups(monkeypatch):
    # Без размеров сезонов подбирать разбивку не по чему — лишних запросов нет.
    def boom(*a, **kw):
        raise AssertionError("episode_groups не должен вызываться")

    monkeypatch.setattr(tmdb, "episode_groups", boom)
    monkeypatch.setattr(tmdb, "_get_json", lambda url, **kw: (
        {"episodes": []} if "/season/" in url
        else {"id": 1, "name": "X", "external_ids": {}, "episode_run_time": []}))
    metadata.fetch("tv", 1, settings(meta_language="en-US"), seasons=[2])
