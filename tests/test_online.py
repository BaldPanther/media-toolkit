"""Тесты online.py: разбор ответов AniSkip/TheIntroDB и подбора ID — без сети.

Сетевой слой (_get_json) подменяется, проверяется только парсинг в Segment.
"""
import online
from edl import Segment


# ------------------------------------------------------------- AniSkip --

def test_aniskip_op_ed_recap(monkeypatch):
    monkeypatch.setattr(online, "_get_json", lambda url: {
        "found": True,
        "results": [
            {"skipType": "op", "interval": {"startTime": 0, "endTime": 94.783}},
            {"skipType": "ed", "interval": {"startTime": 1344.9, "endTime": 1434.9}},
            {"skipType": "recap", "interval": {"startTime": 95.0, "endTime": 120.0}},
        ],
    })
    r = online.fetch_aniskip(54492, 1, 1440)
    assert r.intro == Segment(0.0, 94.783)
    assert r.outro == Segment(1344.9, 1434.9)
    assert r.recap == Segment(95.0, 120.0)
    assert r.source == "AniSkip" and r.note == ""


def test_aniskip_mixed_types_map_to_intro_outro(monkeypatch):
    monkeypatch.setattr(online, "_get_json", lambda url: {
        "found": True,
        "results": [
            {"skipType": "mixed-op", "interval": {"startTime": 10, "endTime": 30}},
            {"skipType": "mixed-ed", "interval": {"startTime": 1300, "endTime": 1400}},
        ],
    })
    r = online.fetch_aniskip(1, 1, 1440)
    assert r.intro == Segment(10.0, 30.0)
    assert r.outro == Segment(1300.0, 1400.0)


def test_aniskip_not_found_sets_note(monkeypatch):
    monkeypatch.setattr(online, "_get_json", lambda url: {"found": False, "results": []})
    r = online.fetch_aniskip(1, 1, 1440)
    assert not r.any and r.note == "нет в AniSkip"


def test_aniskip_drops_degenerate_interval(monkeypatch):
    monkeypatch.setattr(online, "_get_json", lambda url: {
        "found": True,
        "results": [{"skipType": "op", "interval": {"startTime": 50, "endTime": 50}}],
    })
    r = online.fetch_aniskip(1, 1, 1440)
    assert r.intro is None and not r.any


# ---------------------------------------------------------- TheIntroDB --

def test_theintrodb_ms_to_seconds(monkeypatch):
    monkeypatch.setattr(online, "_get_json", lambda url: {
        "intro": [{"start_ms": 229032, "end_ms": 245775}],
        "credits": [{"start_ms": 1200000, "end_ms": 1300000}],
    })
    r = online.fetch_theintrodb(tmdb_id=1396, season=1, episode=1, duration_s=1400)
    assert r.intro == Segment(229.032, 245.775)
    assert r.outro == Segment(1200.0, 1300.0)


def test_theintrodb_null_credits_end_uses_duration(monkeypatch):
    monkeypatch.setattr(online, "_get_json", lambda url: {
        "credits": [{"start_ms": 1300000, "end_ms": None}],
    })
    r = online.fetch_theintrodb(tmdb_id=1, season=1, episode=1, duration_s=1450)
    assert r.outro == Segment(1300.0, 1450.0)


def test_theintrodb_none_response_sets_note(monkeypatch):
    monkeypatch.setattr(online, "_get_json", lambda url: None)
    r = online.fetch_theintrodb(tmdb_id=1, season=1, episode=1)
    assert not r.any and r.note == "нет в TheIntroDB"


def test_theintrodb_requires_id():
    try:
        online.fetch_theintrodb(season=1, episode=1)
    except online.OnlineError:
        return
    raise AssertionError("ожидалась OnlineError без tmdb_id/imdb_id")


# ------------------------------------------------------------- .nfo IDs --

def test_read_nfo_ids_uses_show_nfo_not_episode(tmp_path):
    # Регрессия: у эпизодного .nfo tmdb/imdb — id СЕРИИ (неверный для матчинга),
    # правильные show-level id лежат в tvshow.nfo. Берём именно show-level.
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"")
    (tmp_path / "Show.S01E01.nfo").write_text(
        '<episodedetails><uniqueid type="tmdb">4237712</uniqueid>'
        '<uniqueid type="imdb">tt26743791</uniqueid></episodedetails>', encoding="utf-8")
    (tmp_path / "tvshow.nfo").write_text(
        '<?xml version="1.0"?>\n<tvshow>'
        '<uniqueid type="tmdb">220542</uniqueid>'
        '<uniqueid type="imdb">tt26743760</uniqueid></tvshow>', encoding="utf-8")
    ids = online.read_nfo_ids(video)
    assert ids["tmdb"] == "220542" and ids["imdb"] == "tt26743760"


def test_read_nfo_ids_finds_tvshow_one_level_up(tmp_path):
    # Реальный layout: видео в «Season 01», tvshow.nfo — в папке сериала выше.
    season = tmp_path / "Season 01"
    season.mkdir()
    video = season / "Show.S01E01.mkv"
    video.write_bytes(b"")
    (tmp_path / "tvshow.nfo").write_text(
        '<tvshow><uniqueid type="tmdb">220542</uniqueid></tvshow>', encoding="utf-8")
    assert online.read_nfo_ids(video)["tmdb"] == "220542"


def test_search_tvmaze_imdb(monkeypatch):
    monkeypatch.setattr(online, "_get_json", lambda url: {
        "name": "The Apothecary Diaries",
        "externals": {"tvrage": None, "thetvdb": 431162, "imdb": "tt26743760"},
    })
    assert online.search_tvmaze_imdb("apothecary") == "tt26743760"


def test_search_tvmaze_imdb_none_when_no_match(monkeypatch):
    monkeypatch.setattr(online, "_get_json", lambda url: None)
    assert online.search_tvmaze_imdb("nope") is None


def test_read_nfo_ids_falls_back_to_tvshow_and_regex(tmp_path):
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"")
    # Битый XML (мусор перед корнем) — должен сработать регексп-фолбэк.
    (tmp_path / "tvshow.nfo").write_text(
        'junk\n<tvshow><uniqueid type="myanimelist">54492</uniqueid></tvshow>', encoding="utf-8")
    ids = online.read_nfo_ids(video)
    assert ids["mal"] == "54492"


def test_clean_show_title_strips_year_and_season():
    assert online.clean_show_title("The Apothecary Diaries (2023)") == "The Apothecary Diaries"
    assert online.clean_show_title("Some.Show.Season 2") == "Some Show"
