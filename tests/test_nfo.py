"""Тесты генерации .nfo: состав тегов и сохранение отметок просмотра."""
import xml.etree.ElementTree as ET

import metadata
import nfo
from metadata import EpisodeInfo, MediaInfo, Person, Rating


def parse(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def movie():
    return MediaInfo(
        kind=metadata.MOVIE, tmdb_id=533535, imdb_id="tt6263850",
        title="Дэдпул и Росомаха", title_en="Deadpool & Wolverine",
        original_title="Deadpool & Wolverine", year=2024,
        plot="Уэйд Уилсон…", outline="Уэйд Уилсон…", tagline="Come together.",
        runtime=128, premiered="2024-07-22", mpaa="US:R",
        genres=["Action", "Comedy"], studios=["Marvel Studios"],
        countries=["US"],
        directors=[Person("Shawn Levy", "Director")],
        writers=[Person("Rhett Reese", "Writer")],
        actors=[Person("Ryan Reynolds", "Wade Wilson", 0, "https://img/rr.jpg")],
        rating_tmdb=Rating(7.6, 8784), rating_imdb=Rating(7.5, 573539),
        set_name="Deadpool Collection", set_overview="Серия фильмов…",
    )


def show():
    return MediaInfo(
        kind=metadata.TV, tmdb_id=127532, imdb_id="tt21209876", tvdb_id="389597",
        title="Solo Leveling: Поднятие уровня в одиночку",
        title_en="Solo Leveling", original_title="俺だけレベルアップな件",
        year=2024, plot="10 лет назад…", tagline="Раньше он был самым слабым.",
        runtime=24, premiered="2024-01-07", mpaa="US:TV-14", status="Ended",
        genres=["Animation", "Action"], studios=["Tokyo MX"],
        rating_tmdb=Rating(8.7, 1797),
    )


def episode():
    return EpisodeInfo(season=1, episode=1, title="Я привык к этому",
                       title_en="I'm Used to It", plot="Около десяти лет назад…",
                       aired="2024-01-07", runtime=23, rating=Rating(7.6, 52),
                       tmdb_id=3026285)


# ------------------------------------------------------------------ фильм --

def test_movie_nfo_core_tags():
    root = parse(nfo.make_movie_nfo(movie()))
    assert root.tag == "movie"
    assert root.findtext("title") == "Дэдпул и Росомаха"
    assert root.findtext("originaltitle") == "Deadpool & Wolverine"
    assert root.findtext("year") == "2024"
    assert root.findtext("plot").startswith("Уэйд")
    assert root.findtext("tagline") == "Come together."
    assert root.findtext("runtime") == "128"
    assert root.findtext("mpaa") == "US:R"
    assert root.findtext("premiered") == "2024-07-22"
    assert [g.text for g in root.findall("genre")] == ["Action", "Comedy"]
    assert [s.text for s in root.findall("studio")] == ["Marvel Studios"]
    assert root.findtext("director") == "Shawn Levy"
    assert root.findtext("credits") == "Rhett Reese"


def test_movie_nfo_imdb_rating_is_default():
    # Ради этого и берётся ключ OMDb: Kodi показывает рейтинг с default="true".
    ratings = parse(nfo.make_movie_nfo(movie())).find("ratings")
    first = ratings.findall("rating")[0]
    assert first.get("name") == "imdb" and first.get("default") == "true"
    assert first.findtext("value") == "7.5" and first.findtext("votes") == "573539"
    second = ratings.findall("rating")[1]
    assert second.get("name") == "themoviedb" and second.get("default") == "false"


def test_movie_nfo_tmdb_is_default_without_imdb_rating():
    info = movie()
    info.rating_imdb = Rating()
    ratings = parse(nfo.make_movie_nfo(info)).find("ratings")
    only = ratings.findall("rating")
    assert len(only) == 1
    assert only[0].get("name") == "themoviedb" and only[0].get("default") == "true"


def test_movie_nfo_unique_ids_and_set():
    root = parse(nfo.make_movie_nfo(movie()))
    ids = {el.get("type"): (el.text, el.get("default")) for el in root.findall("uniqueid")}
    assert ids["imdb"] == ("tt6263850", "true")
    assert ids["tmdb"] == ("533535", "false")
    assert root.findtext("id") == "tt6263850"
    assert root.find("set").findtext("name") == "Deadpool Collection"


def test_movie_nfo_actor_with_thumb():
    actor = parse(nfo.make_movie_nfo(movie())).find("actor")
    assert actor.findtext("name") == "Ryan Reynolds"
    assert actor.findtext("role") == "Wade Wilson"
    assert actor.findtext("thumb") == "https://img/rr.jpg"


def test_movie_nfo_art_thumbs():
    art = {metadata.ART_POSTER: "https://img/p.jpg",
           metadata.ART_LOGO: "https://img/l.png",
           metadata.ART_FANART: "https://img/f.jpg"}
    root = parse(nfo.make_movie_nfo(movie(), art=art))
    aspects = {el.get("aspect"): el.text for el in root.findall("thumb")}
    assert aspects["poster"] == "https://img/p.jpg"
    assert aspects["clearlogo"] == "https://img/l.png"
    assert root.find("fanart").findtext("thumb") == "https://img/f.jpg"


def test_movie_nfo_escapes_ampersand():
    xml = nfo.make_movie_nfo(movie())
    assert "&amp;" in xml and "Deadpool & Wolverine" not in xml
    assert parse(xml).findtext("originaltitle") == "Deadpool & Wolverine"


# ---------------------------------------------------------------- сериал --

def test_tvshow_nfo_core_tags():
    root = parse(nfo.make_tvshow_nfo(show()))
    assert root.tag == "tvshow"
    assert root.findtext("title").startswith("Solo Leveling:")
    assert root.findtext("showtitle") == root.findtext("title")
    assert root.findtext("originaltitle") == "俺だけレベルアップな件"
    assert root.findtext("status") == "Ended"
    assert root.findtext("id") == "389597"
    assert root.findtext("imdbid") == "tt21209876"


def test_tvshow_nfo_tvdb_is_default_uniqueid():
    # Так это лежит в текущей медиатеке — tvdb помечен основным.
    root = parse(nfo.make_tvshow_nfo(show()))
    ids = {el.get("type"): el.get("default") for el in root.findall("uniqueid")}
    assert ids == {"tmdb": "false", "imdb": "false", "tvdb": "true"}


def test_tvshow_nfo_falls_back_to_tmdb_when_no_tvdb():
    info = show()
    info.tvdb_id = ""
    ids = {el.get("type"): el.get("default")
           for el in parse(nfo.make_tvshow_nfo(info)).findall("uniqueid")}
    assert ids["tmdb"] == "true"


def test_tvshow_nfo_season_posters():
    root = parse(nfo.make_tvshow_nfo(show(), season_art={1: "https://img/s1.jpg"}))
    season = [el for el in root.findall("thumb") if el.get("type") == "season"]
    assert season[0].get("season") == "1" and season[0].text == "https://img/s1.jpg"


# ------------------------------------------------------------------ серия --

def test_episode_nfo():
    root = parse(nfo.make_episode_nfo(show(), episode()))
    assert root.tag == "episodedetails"
    assert root.findtext("title") == "Я привык к этому"
    assert root.findtext("originaltitle") == "I'm Used to It"
    assert root.findtext("showtitle").startswith("Solo Leveling:")
    assert root.findtext("season") == "1" and root.findtext("episode") == "1"
    assert root.findtext("aired") == "2024-01-07"
    assert root.find("uniqueid").text == "3026285"


# -------------------------------------------------- отметки просмотра --

def test_read_watch_state(tmp_path):
    p = tmp_path / "movie.nfo"
    p.write_text('<movie><watched>true</watched><playcount>3</playcount>'
                 '<userrating>8</userrating></movie>', encoding="utf-8")
    assert nfo.read_watch_state(p) == {"watched": "true", "playcount": "3",
                                       "userrating": "8"}


def test_read_watch_state_missing_file(tmp_path):
    assert nfo.read_watch_state(tmp_path / "nope.nfo") == {}


def test_read_watch_state_broken_xml(tmp_path):
    p = tmp_path / "movie.nfo"
    p.write_text("не xml вовсе", encoding="utf-8")
    assert nfo.read_watch_state(p) == {}


def test_watch_state_survives_rewrite(tmp_path):
    # Регрессия: перезапись .nfo не должна сбрасывать «просмотрено».
    old = tmp_path / "movie.nfo"
    old.write_text('<movie><watched>true</watched><playcount>1</playcount>'
                   '<userrating>9</userrating></movie>', encoding="utf-8")
    root = parse(nfo.make_movie_nfo(movie(), preserve=nfo.read_watch_state(old)))
    assert root.findtext("watched") == "true"
    assert root.findtext("playcount") == "1"
    assert root.findtext("userrating") == "9"


def test_watch_state_defaults_when_no_old_file():
    root = parse(nfo.make_movie_nfo(movie()))
    assert root.findtext("watched") == "false"
    assert root.findtext("playcount") == "0"
    assert root.findtext("userrating") == "0"


def test_declaration_and_generator_comment():
    xml = nfo.make_movie_nfo(movie())
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
    assert "by media-toolkit" in xml.splitlines()[1]


def test_episode_nfo_has_own_crew_and_guests():
    # tinyMediaManager пишет режиссёра, сценаристов и приглашённых актёров
    # у каждой серии — данные приходят внутри ответа по сезону.
    ep = episode()
    ep.directors = [Person("Shunsuke Nakashige", "Director")]
    ep.writers = [Person("Noboru Kimura", "Writer")]
    ep.guests = [Person("Kaito Ishikawa", "Hunter", 0, "https://img/g.jpg")]
    root = parse(nfo.make_episode_nfo(show(), ep))
    assert root.findtext("director") == "Shunsuke Nakashige"
    assert root.findtext("credits") == "Noboru Kimura"
    assert root.find("actor").findtext("name") == "Kaito Ishikawa"


def test_tvshow_nfo_country():
    info = show()
    info.countries = ["JP"]
    assert parse(nfo.make_tvshow_nfo(info)).findtext("country") == "JP"
