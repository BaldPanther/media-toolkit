"""Тесты раскладки медиатеки: имена, план переименования, применение.

Работают на дереве файлов нулевого размера во временной папке — ни сети,
ни настоящих MKV. Главный смысл — гарантировать, что повторный прогон по уже
разложенной библиотеке не меняет ни одного имени.
"""
from pathlib import Path

import library
import metaconf
from metadata import EpisodeInfo, MediaInfo


def make_settings(**kw):
    s = metaconf.Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def show_info(title="Archer", year=2009, episodes=()):
    info = MediaInfo(kind=library.TV, tmdb_id=1, title_en=title, title=title, year=year)
    for season, number, name in episodes:
        info.episodes[(season, number)] = EpisodeInfo(
            season=season, episode=number, title_en=name, title=name)
    return info


def movie_info(title="Deadpool & Wolverine", year=2024):
    return MediaInfo(kind=library.MOVIE, tmdb_id=1, title_en=title, title=title, year=year)


def touch(path, content=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def dst_of(plan, name_part):
    for row in plan.rows:
        if row.src is not None and name_part in row.src.name:
            return row.dst
    raise AssertionError(f"нет строки с {name_part!r}")


# ------------------------------------------------------------------ имена --

def test_sanitize_name_subtitle_colon_becomes_dash():
    # Двоеточие почти всегда отделяет подзаголовок, и пробел на его месте
    # читается плохо: «The Lord of the Rings The Fellowship of the Ring».
    assert library.sanitize_name("Mission: Difficult") == "Mission - Difficult"
    assert library.sanitize_name("Solo Leveling: Ragnarok") == "Solo Leveling - Ragnarok"
    assert library.sanitize_name("Eddie Murphy: Delirious") == "Eddie Murphy - Delirious"
    # Уже стоящий в названии дефис вторым не обрастает.
    assert library.sanitize_name("Mission: Impossible - Fallout") == \
        "Mission - Impossible - Fallout"


def test_sanitize_name_keeps_tight_colon_as_space():
    # Без пробела следом двоеточие значит не подзаголовок — дефис там неуместен.
    assert library.sanitize_name("Re:Zero") == "Re Zero"
    assert library.sanitize_name("Aliens vs Predator 9:30") == "Aliens vs Predator 9 30"


def test_sanitize_name_slash_becomes_space():
    # Слэш стоит между словами пары, дефис с пробелами там выглядел бы чужеродно.
    assert library.sanitize_name("Arrival/Departure") == "Arrival Departure"


def test_sanitize_name_never_leaves_a_dangling_dash():
    assert library.sanitize_name("Название:") == "Название"
    assert library.sanitize_name(": Название") == "Название"


def test_sanitize_name_strips_forbidden_and_trailing_dot():
    assert library.sanitize_name('A*B?C"D<E>F|G') == "ABCDEFG"
    assert library.sanitize_name("Dr. Strange.") == "Dr. Strange"
    # Амперсанд и апостроф допустимы и остаются — они есть в реальной медиатеке.
    assert library.sanitize_name("Deadpool & Wolverine") == "Deadpool & Wolverine"
    assert library.sanitize_name("I'm Used to It") == "I'm Used to It"


def test_season_names_match_tinymediamanager():
    assert library.season_dir_name(1) == "Season 01"
    assert library.season_dir_name(12) == "Season 12"
    assert library.season_dir_name(0) == "Specials"
    assert library.season_poster_name(2) == "season02-poster.jpg"
    assert library.season_poster_name(0) == "season-specials-poster.jpg"


def test_season_of_dir_accepts_both_specials_spellings():
    assert library.season_of_dir("Season 01") == 1
    assert library.season_of_dir("season.3") == 3
    assert library.season_of_dir("Specials") == 0
    assert library.season_of_dir("Season 00") == 0
    assert library.season_of_dir("Extras") is None


def test_episode_file_name():
    assert library.episode_file_name("Archer", 1, 1, "Mole Hunt", ".mkv") == \
        "Archer - S01E01 - Mole Hunt.mkv"
    assert library.episode_file_name("Archer", 0, 3, "Heart of Archness (1)", ".mkv") == \
        "Archer - S00E03 - Heart of Archness (1).mkv"
    assert library.episode_file_name("Archer", 2, 4, "", ".mkv") == "Archer - S02E04.mkv"


def test_guess_title_year_cuts_release_tokens():
    assert library.guess_title_year(
        "Altered.Carbon.S01.2160p.NF.WEBRip.x265.10bit.HDR") == ("Altered Carbon", None)
    assert library.guess_title_year(
        "Deadpool.and.Wolverine.2024.2160p.WEB-DL") == ("Deadpool and Wolverine", 2024)
    assert library.guess_title_year("Solo Leveling (2024)") == ("Solo Leveling", 2024)
    assert library.guess_title_year("Top Gear") == ("Top Gear", None)


# ------------------------------------------------------- план для сериала --

def test_tv_plan_season_torrent_in_library_root(tmp_path):
    # Свежая раздача одного сезона лежит прямо в корне библиотеки: нужно создать
    # папку сериала и убрать раздачу внутрь как «Season 01».
    tv = tmp_path / "tv"
    src = tv / "Altered.Carbon.S01.2160p.NF.WEBRip"
    touch(src / "Altered.Carbon.S01E01.2160p.mkv")
    touch(src / "Altered.Carbon.S01E02.2160p.mkv")

    info = show_info("Altered Carbon", 2018,
                     [(1, 1, "Out of the Past"), (1, 2, "Fallen Angel")])
    plan = library.build_tv_plan(src, info, make_settings())

    assert plan.root == tv / "Altered Carbon (2018)"
    assert dst_of(plan, "S01E01") == \
        tv / "Altered Carbon (2018)" / "Season 01" / "Altered Carbon - S01E01 - Out of the Past.mkv"


def test_tv_plan_already_correct_changes_nothing(tmp_path):
    tv = tmp_path / "tv"
    root = tv / "Archer (2009)"
    touch(root / "Season 01" / "Archer - S01E01 - Mole Hunt.mkv")
    touch(root / "Specials" / "Archer - S00E03 - Heart of Archness (1).mkv")
    touch(root / "poster.jpg")
    touch(root / "season-specials-poster.jpg")
    touch(root / "theme.mp3")

    info = show_info("Archer", 2009, [(1, 1, "Mole Hunt"),
                                      (0, 3, "Heart of Archness (1)")])
    plan = library.build_tv_plan(root, info, make_settings())

    assert plan.root == root
    assert plan.changed() == [], [str(r.src) for r in plan.changed()]
    assert all(r.status == library.S_OK for r in plan.rows)


def test_tv_plan_new_season_dropped_into_ready_show(tmp_path):
    # Сценарий дозагрузки: первый сезон разложен, рядом появилась сырая папка S02.
    tv = tmp_path / "tv"
    root = tv / "Archer (2009)"
    touch(root / "Season 01" / "Archer - S01E01 - Mole Hunt.mkv")
    touch(root / "tvshow.nfo")
    raw = root / "Archer.S02.1080p.WEB-DL"
    touch(raw / "Archer.S02E01.1080p.mkv")

    info = show_info("Archer", 2009, [(1, 1, "Mole Hunt"), (2, 1, "Swiss Miss")])
    plan = library.build_tv_plan(root, info, make_settings())

    assert plan.root == root
    assert dst_of(plan, "S02E01") == root / "Season 02" / "Archer - S02E01 - Swiss Miss.mkv"
    # Первый сезон не тронут.
    first = [r for r in plan.rows if r.src and "S01E01" in r.src.name]
    assert [r.status for r in first] == [library.S_OK]


def test_tv_plan_pointed_at_season_folder_inside_show(tmp_path):
    # Указана сама папка сезона внутри готового сериала — сериал не должен
    # вложиться сам в себя.
    tv = tmp_path / "tv"
    root = tv / "Archer (2009)"
    touch(root / "tvshow.nfo")
    touch(root / "Season 01" / "Archer - S01E01 - Mole Hunt.mkv")
    raw = root / "Archer.S02.1080p"
    touch(raw / "Archer.S02E01.mkv")

    info = show_info("Archer", 2009, [(2, 1, "Swiss Miss")])
    plan = library.build_tv_plan(raw, info, make_settings())

    assert plan.root == root
    assert dst_of(plan, "S02E01") == root / "Season 02" / "Archer - S02E01 - Swiss Miss.mkv"


def test_tv_plan_moves_sidecars_with_episode(tmp_path):
    tv = tmp_path / "tv"
    src = tv / "Archer.S01"
    touch(src / "Archer.S01E01.mkv")
    touch(src / "Archer.S01E01.edl")
    touch(src / "Archer.S01E01.ru.srt")
    touch(src / "Archer.S01E01-thumb.jpg")

    info = show_info("Archer", 2009, [(1, 1, "Mole Hunt")])
    plan = library.build_tv_plan(src, info, make_settings())

    base = tv / "Archer (2009)" / "Season 01" / "Archer - S01E01 - Mole Hunt"
    assert dst_of(plan, ".edl") == base.with_suffix(".edl")
    assert dst_of(plan, ".ru.srt").name == "Archer - S01E01 - Mole Hunt.ru.srt"
    assert dst_of(plan, "-thumb.jpg").name == "Archer - S01E01 - Mole Hunt-thumb.jpg"


def test_tv_plan_unparsed_episode_is_skipped_not_guessed(tmp_path):
    src = tmp_path / "tv" / "Archer.S01"
    touch(src / "random-name.mkv")
    plan = library.build_tv_plan(src, show_info(), make_settings())
    row = plan.rows[0]
    assert row.action == library.A_SKIP and row.status == library.S_NOEP
    assert plan.unmatched == [src / "random-name.mkv"]


def test_tv_plan_manual_override_sets_season_episode(tmp_path):
    src = tmp_path / "tv" / "Show"
    video = touch(src / "13.mkv")
    info = show_info("Archer", 2009, [(2, 1, "Swiss Miss")])
    plan = library.build_tv_plan(src, info, make_settings(), overrides={video: (2, 1)})
    assert dst_of(plan, "13.mkv").name == "Archer - S02E01 - Swiss Miss.mkv"


def test_tv_plan_conflict_when_two_files_map_to_one_name(tmp_path):
    src = tmp_path / "tv" / "Show.S01"
    touch(src / "Show.S01E01.720p.mkv")
    touch(src / "Show.S01E01.1080p.mkv")
    info = show_info("Show", 2020, [(1, 1, "Pilot")])
    plan = library.build_tv_plan(src, info, make_settings())
    assert len(plan.conflicts()) == 2
    assert all(r.action == library.A_SKIP for r in plan.conflicts())


def test_art_files_follow_show_folder_rename(tmp_path):
    tv = tmp_path / "tv"
    src = tv / "Archer.2009.COMPLETE"
    touch(src / "Season 01" / "Archer.S01E01.mkv")
    touch(src / "poster.jpg")
    touch(src / "theme.mp3")

    info = show_info("Archer", 2009, [(1, 1, "Mole Hunt")])
    plan = library.build_tv_plan(src, info, make_settings())

    assert dst_of(plan, "poster.jpg") == tv / "Archer (2009)" / "poster.jpg"
    # theme.mp3 — артефакт медиатеки, а не мусор: переезжает, но не удаляется.
    theme = [r for r in plan.rows if r.src and r.src.name == "theme.mp3"]
    assert theme[0].what == "art" and theme[0].action == library.A_RENAME


# --------------------------------------------------------- мусор и Extras --

def test_junk_goes_to_extras(tmp_path):
    src = tmp_path / "tv" / "Show.S01"
    touch(src / "Show.S01E01.mkv")
    touch(src / "sample.txt")
    touch(src / "screens" / "01.png")

    info = show_info("Show", 2020, [(1, 1, "Pilot")])
    plan = library.build_tv_plan(src, info, make_settings())
    junk = {r.src.name: r for r in plan.rows if r.what == "junk"}
    assert junk["sample.txt"].dst == tmp_path / "tv" / "Show (2020)" / "Extras" / "sample.txt"
    assert "screens" in junk


def test_existing_extras_is_not_junk_again(tmp_path):
    # Регрессия: то, что отложено в Extras прошлым прогоном, не должно попасть
    # под галку «удалять» — иначе второй прогон тихо снесёт отложенное.
    tv = tmp_path / "tv"
    root = tv / "Show (2020)"
    touch(root / "Season 01" / "Show - S01E01 - Pilot.mkv")
    touch(root / "Extras" / "notes.txt")

    info = show_info("Show", 2020, [(1, 1, "Pilot")])
    plan = library.build_tv_plan(root, info, make_settings())
    rows = [r for r in plan.rows if r.src and r.src.name == "notes.txt"]
    assert rows[0].what == "art" and rows[0].action == library.A_KEEP


# --------------------------------------------------------- план для фильма --

def test_movie_plan_from_loose_file(tmp_path):
    movies = tmp_path / "movies"
    video = touch(movies / "Deadpool.and.Wolverine.2024.2160p.WEB-DL.mkv")
    plan = library.build_movie_plan(video, movie_info(), make_settings())
    target = movies / "Deadpool & Wolverine (2024)"
    assert plan.root == target
    assert dst_of(plan, ".mkv") == target / "Deadpool & Wolverine (2024).mkv"


def test_movie_plan_extra_video_goes_to_extras(tmp_path):
    movies = tmp_path / "movies"
    src = movies / "Deadpool.2024"
    touch(src / "movie.mkv", b"x" * 100)
    touch(src / "sample.mkv", b"x")
    plan = library.build_movie_plan(src, movie_info(), make_settings())
    extra = [r for r in plan.rows if r.src.name == "sample.mkv"][0]
    assert extra.action == library.A_JUNK
    assert dst_of(plan, "movie.mkv").name == "Deadpool & Wolverine (2024).mkv"


def test_movie_plan_already_correct(tmp_path):
    movies = tmp_path / "movies"
    root = movies / "Deadpool & Wolverine (2024)"
    touch(root / "Deadpool & Wolverine (2024).mkv")
    touch(root / "movie.nfo")
    touch(root / "poster.jpg")
    plan = library.build_movie_plan(root, movie_info(), make_settings())
    assert plan.changed() == []


# ----------------------------------------------------------- применение --

def test_apply_renames_files_and_removes_empty_folder(tmp_path):
    tv = tmp_path / "tv"
    src = tv / "Archer.S01.WEB-DL"
    touch(src / "Archer.S01E01.mkv", b"video")
    touch(src / "Archer.S01E01.edl", b"0 10 3")

    info = show_info("Archer", 2009, [(1, 1, "Mole Hunt")])
    plan = library.build_tv_plan(src, info, make_settings())
    results = library.apply_plan(plan)

    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    target = tv / "Archer (2009)" / "Season 01"
    assert (target / "Archer - S01E01 - Mole Hunt.mkv").read_bytes() == b"video"
    assert (target / "Archer - S01E01 - Mole Hunt.edl").read_bytes() == b"0 10 3"
    assert not src.exists()


def test_apply_handles_name_cycle(tmp_path):
    # Перенумерация внутри уже разложенного сезона: E01→E02, E02→E01. Источник и
    # цель лежат в одной папке, прямое переименование затёрло бы соседний файл.
    season = tmp_path / "tv" / "Show (2020)" / "Season 01"
    first = touch(season / "Show - S01E01 - A.mkv", b"first")
    second = touch(season / "Show - S01E02 - B.mkv", b"second")
    info = show_info("Show", 2020, [(1, 1, "A"), (1, 2, "B")])
    plan = library.build_tv_plan(tmp_path / "tv" / "Show (2020)", info, make_settings(),
                                 overrides={first: (1, 2), second: (1, 1)})
    # Цель каждой строки — путь соседнего файла: это и есть цикл.
    assert {r.dst for r in plan.changed()} == {first, second}

    results = library.apply_plan(plan)
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    assert (season / "Show - S01E02 - B.mkv").read_bytes() == b"first"
    assert (season / "Show - S01E01 - A.mkv").read_bytes() == b"second"


def test_apply_case_only_rename(tmp_path):
    # На APFS/SMB прямое переименование в отличающийся только регистром путь
    # либо падает, либо молча ничего не делает — нужен проход через временное имя.
    root = tmp_path / "tv" / "Show (2020)"
    season = root / "Season 01"
    touch(season / "show - S01E01 - a.mkv", b"v")
    info = show_info("Show", 2020, [(1, 1, "A")])
    plan = library.build_tv_plan(root, info, make_settings())
    results = library.apply_plan(plan)

    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    names = [p.name for p in season.iterdir()]
    assert names == ["Show - S01E01 - A.mkv"]


def test_apply_junk_moves_to_extras_when_not_deleting(tmp_path):
    src = tmp_path / "tv" / "Show.S01"
    touch(src / "Show.S01E01.mkv")
    touch(src / "readme.txt", b"junk")
    info = show_info("Show", 2020, [(1, 1, "Pilot")])
    plan = library.build_tv_plan(src, info, make_settings())
    library.apply_plan(plan, delete_junk=False)
    assert (tmp_path / "tv" / "Show (2020)" / "Extras" / "readme.txt").read_bytes() == b"junk"


def test_apply_skips_unselected_junk(tmp_path):
    src = tmp_path / "tv" / "Show.S01"
    touch(src / "Show.S01E01.mkv")
    touch(src / "keep.txt", b"keep")
    info = show_info("Show", 2020, [(1, 1, "Pilot")])
    plan = library.build_tv_plan(src, info, make_settings())
    for row in plan.rows:
        if row.what == "junk":
            row.selected = False
    library.apply_plan(plan, delete_junk=False)
    assert (src / "keep.txt").exists()


def test_junk_inside_junk_folder_moves_with_it(tmp_path):
    # Папка «Sample» едет целиком: отдельной строки на файл внутри быть не должно,
    # иначе в Extras окажется и файл, и пустая папка рядом.
    src = tmp_path / "tv" / "Show.S01"
    touch(src / "Show.S01E01.mkv")
    touch(src / "Sample" / "sample.jpg")
    touch(src / "Sample" / "nested" / "deep.txt")

    info = show_info("Show", 2020, [(1, 1, "Pilot")])
    plan = library.build_tv_plan(src, info, make_settings())
    junk = [r for r in plan.rows if r.what == "junk"]
    assert [r.src.name for r in junk] == ["Sample"]

    library.apply_plan(plan)
    extras = tmp_path / "tv" / "Show (2020)" / "Extras"
    assert (extras / "Sample" / "sample.jpg").is_file()
    assert (extras / "Sample" / "nested" / "deep.txt").is_file()


def test_guess_title_year_keeps_abbreviation_dot():
    # «Dr. STONE» — точка сокращения, а не разделитель имени раздачи.
    assert library.guess_title_year("Dr. STONE (2019)") == ("Dr. STONE", 2019)
    assert library.guess_title_year("Оно 2.2019.UHD.Blu-Ray.Remux.2160p") == ("Оно 2", 2019)


def test_movie_plan_refuses_collection_folder(tmp_path):
    # Регрессия: в папке-сборнике три из четырёх фильмов нельзя признать
    # «лишним видео» и увезти в Extras.
    coll = tmp_path / "movies" / "Scary Movie Collection"
    touch(coll / "Scary Movie (2000)" / "Scary Movie (2000).mkv", b"x" * 10)
    touch(coll / "Scary Movie 2 (2001)" / "Scary Movie 2 (2001).mkv", b"x" * 20)

    plan = library.build_movie_plan(coll, movie_info(), make_settings())
    assert plan.changed() == []
    assert {r.status for r in plan.rows} == {library.S_COLLECTION}


# ------------------------------------------- многосерийные файлы и юникод --

def test_parse_episodes_single():
    assert library.parse_episodes("Archer - S01E01 - Mole Hunt.mkv") == (1, [1])


def test_parse_episodes_expands_range():
    # Регрессия с реальной медиатеки: в файле лежат серии 9, 10 и 11.
    # Раньше читалась только девятая, а десятая и одиннадцатая пропадали.
    assert library.parse_episodes("Archer - S14E09-E11 - Into the Cold.mkv") == \
        (14, [9, 10, 11])
    assert library.parse_episodes("Show.S01E01-02.mkv") == (1, [1, 2])
    assert library.parse_episodes("Show.S01E01E02.mkv") == (1, [1, 2])


def test_parse_episodes_does_not_eat_title_starting_with_digit():
    # «- 2 Girls» не должно превратиться во вторую серию файла.
    assert library.parse_episodes("Show - S01E01 - 2 Girls.mkv") == (1, [1])


def test_multi_episode_file_name_matches_tinymediamanager():
    assert library.episode_file_name("Archer", 14, [9, 10, 11], "Into the Cold", ".mkv") == \
        "Archer - S14E09-E11 - Into the Cold.mkv"


def test_multi_episode_plan_keeps_range(tmp_path):
    src = tmp_path / "tv" / "Archer (2009)" / "Season 14"
    touch(src / "Archer.S14E09-E11.Into.the.Cold.mkv")
    info = show_info("Archer", 2009, [(14, 9, "Into the Cold"), (14, 10, "Into the Cold"),
                                      (14, 11, "Into the Cold")])
    plan = library.build_tv_plan(tmp_path / "tv" / "Archer (2009)", info, make_settings())
    assert dst_of(plan, "S14E09").name == "Archer - S14E09-E11 - Into the Cold.mkv"


def test_nfd_and_nfc_names_are_the_same_file(tmp_path):
    # macOS хранит «ö» разложенной, TMDb отдаёт слитную. Без нормализации
    # каждый прогон предлагал бы переименовать файл сам в себя.
    season = tmp_path / "tv" / "Archer (2009)" / "Season 08"
    import unicodedata
    title_nfc = unicodedata.normalize("NFC", "Aufl\u00f6sung")  # слитная ö
    title_nfd = unicodedata.normalize("NFD", title_nfc)          # o + умляут
    assert title_nfc != title_nfd            # формы действительно разные
    touch(season / f"Archer - S08E08 - {title_nfd}.mkv")
    info = show_info("Archer", 2009, [(8, 8, title_nfc)])
    plan = library.build_tv_plan(tmp_path / "tv" / "Archer (2009)", info, make_settings())
    assert plan.changed() == [], [str(r.dst) for r in plan.changed()]


def test_missing_tmdb_episode_keeps_existing_title(tmp_path):
    # Аниме: у TMDb «Solo Leveling» — один сезон из 25 серий, а на диске два
    # (нумерация TVDb, как у tinyMediaManager). Название терять нельзя.
    root = tmp_path / "tv" / "Solo Leveling (2024)"
    touch(root / "Season 02" / "Solo Leveling - S02E01 - You Aren't E-Rank, Are You.mkv")
    info = show_info("Solo Leveling", 2024, [(1, 1, "I'm Used to It")])   # сезона 2 нет
    plan = library.build_tv_plan(root, info, make_settings())
    assert plan.changed() == [], [str(r.dst) for r in plan.changed()]
    assert "название оставлено прежним" in plan.rows[0].note


def test_missing_tmdb_episode_without_title_still_organised(tmp_path):
    # Свежая раздача: названия в имени нет, и терять нечего — раскладываем.
    src = tmp_path / "tv" / "Show.S01"
    touch(src / "Show.S01E01.2160p.NF.WEBRip.mkv")
    info = show_info("Show", 2020)          # серий в TMDb нет вовсе
    plan = library.build_tv_plan(src, info, make_settings())
    assert dst_of(plan, "S01E01").name == "Show - S01E01.mkv"


def test_existing_episode_title_ignores_release_tail():
    assert library.existing_episode_title("Show - S01E01 - Pilot.mkv") == "Pilot"
    assert library.existing_episode_title("Archer - S14E09-E11 - Into the Cold.mkv") == \
        "Into the Cold"
    assert library.existing_episode_title("Show.S01E01.2160p.NF.WEBRip.mkv") == ""


def test_guess_kind_honours_several_roots(tmp_path):
    # Корней каждого вида может быть несколько — проверяем все, а не первый.
    movies_a, movies_b = tmp_path / "m1", tmp_path / "m2"
    tv_a, tv_b = tmp_path / "t1", tmp_path / "t2"
    for d in (movies_a, movies_b, tv_a, tv_b):
        d.mkdir()
    s = make_settings(movies_roots=[str(movies_a), str(movies_b)],
                      tv_roots=[str(tv_a), str(tv_b)])

    film = movies_b / "Some Film (2020)"
    touch(film / "film.mkv")
    show = tv_b / "Some.Show.S01"
    touch(show / "Some.Show.S01E01.mkv")

    assert library.guess_kind(film, s) == library.MOVIE
    assert library.guess_kind(show, s) == library.TV


def test_guess_kind_without_roots_uses_structure(tmp_path):
    s = make_settings()
    show = tmp_path / "Some.Show.S01"
    touch(show / "Some.Show.S01E01.mkv")
    film = tmp_path / "Some Film (2020)"
    touch(film / "film.mkv")
    assert library.guess_kind(show, s) == library.TV
    assert library.guess_kind(film, s) == library.MOVIE


# ------------------------------------------- что в библиотеке не обработано --

def ready_show(root, name="Archer (2009)", seasons=(1,)):
    """Полностью разложенный сериал: nfo, постер, .nfo у каждой серии."""
    show = root / name
    touch(show / "tvshow.nfo")
    touch(show / "poster.jpg")
    for s in seasons:
        for e in (1, 2):
            base = show / f"Season {s:02d}" / f"Archer - S{s:02d}E{e:02d} - Ep.mkv"
            touch(base)
            touch(base.with_suffix(".nfo"))
    return show


def ready_movie(root, name="Deadpool & Wolverine (2024)"):
    folder = root / name
    touch(folder / f"{name}.mkv")
    touch(folder / "movie.nfo")
    touch(folder / "poster.jpg")
    return folder


def roots(tmp_path):
    movies, tv = tmp_path / "movies", tmp_path / "tv"
    movies.mkdir(parents=True)
    tv.mkdir(parents=True)
    return movies, tv, make_settings(movies_roots=[str(movies)], tv_roots=[str(tv)])


def test_unprocessed_empty_when_library_is_ready(tmp_path):
    movies, tv, s = roots(tmp_path)
    ready_show(tv)
    ready_movie(movies)
    assert library.find_unprocessed(s) == []


def test_unprocessed_finds_raw_show(tmp_path):
    movies, tv, s = roots(tmp_path)
    touch(tv / "Altered.Carbon.S01.2160p" / "Altered.Carbon.S01E01.mkv")
    found = library.find_unprocessed(s)
    assert [p.path.name for p in found] == ["Altered.Carbon.S01.2160p"]
    assert found[0].kind == library.TV
    assert "нет tvshow.nfo" in found[0].why


def test_unprocessed_finds_new_season_in_ready_show(tmp_path):
    # Главный сценарий: сериал разложен, внутрь положили сырую папку сезона.
    movies, tv, s = roots(tmp_path)
    show = ready_show(tv)
    touch(show / "Archer.S02.1080p.WEB-DL" / "Archer.S02E01.mkv")
    found = library.find_unprocessed(s)
    assert len(found) == 1
    assert found[0].path == show
    assert "новый сезон: Archer.S02.1080p.WEB-DL" in found[0].why


def test_unprocessed_finds_episodes_without_nfo(tmp_path):
    movies, tv, s = roots(tmp_path)
    show = ready_show(tv)
    touch(show / "Season 02" / "Archer - S02E01 - Ep.mkv")      # без .nfo рядом
    found = library.find_unprocessed(s)
    assert "серий без .nfo: 1" in found[0].why


def test_unprocessed_ignores_extras_folder(tmp_path):
    movies, tv, s = roots(tmp_path)
    show = ready_show(tv)
    touch(show / "Extras" / "sample.mkv")
    assert library.find_unprocessed(s) == []


def test_unprocessed_finds_loose_movie_file(tmp_path):
    movies, tv, s = roots(tmp_path)
    video = touch(movies / "Some.Film.2019.2160p.mkv")
    found = library.find_unprocessed(s)
    assert [p.path for p in found] == [video]
    assert found[0].kind == library.MOVIE


def test_unprocessed_movie_missing_poster(tmp_path):
    movies, tv, s = roots(tmp_path)
    folder = ready_movie(movies)
    (folder / "poster.jpg").unlink()
    found = library.find_unprocessed(s)
    assert found[0].why == "нет постера"


def test_unprocessed_looks_inside_collection_folder(tmp_path):
    # «Scary Movie Collection» — папка с фильмами внутри, проверяем каждый.
    movies, tv, s = roots(tmp_path)
    coll = movies / "Scary Movie Collection"
    ready_movie(coll, "Scary Movie (2000)")
    touch(coll / "Scary Movie 2 (2001)" / "Scary Movie 2 (2001).mkv")
    found = library.find_unprocessed(s)
    assert [p.path.name for p in found] == ["Scary Movie 2 (2001)"]


def test_unprocessed_flat_show_without_season_folders(tmp_path):
    movies, tv, s = roots(tmp_path)
    show = tv / "Some Show (2020)"
    touch(show / "tvshow.nfo")
    touch(show / "poster.jpg")
    touch(show / "Some Show - S01E01 - Pilot.mkv")
    found = library.find_unprocessed(s)
    assert "серии не разложены по сезонам: 1" in found[0].why


def test_unprocessed_respects_stop(tmp_path):
    movies, tv, s = roots(tmp_path)
    for i in range(5):
        touch(tv / f"Raw.Show.{i}" / f"Raw.Show.S01E0{i}.mkv")
    assert library.find_unprocessed(s, stop=lambda: True) == []


def test_unprocessed_skips_missing_root(tmp_path):
    s = make_settings(tv_roots=[str(tmp_path / "нет-такой-папки")])
    assert library.find_unprocessed(s) == []


def test_loose_movie_takes_its_subtitles_and_nothing_else(tmp_path):
    # Фильм лежит в корне одним файлом, рядом субтитры — они едут вместе с ним.
    # Остальное содержимое корня библиотеки трогать нельзя ни в коем случае.
    movies = tmp_path / "movies"
    video = touch(movies / "Some.Film.2019.2160p.mkv", b"v")
    touch(movies / "Some.Film.2019.2160p.ru.srt", b"s")
    touch(movies / "Другой Фильм (2001).mkv", b"x")          # чужой фильм рядом
    ready = movies / "Ready Movie (2010)"
    touch(ready / "Ready Movie (2010).mkv")

    plan = library.build_movie_plan(video, movie_info("It Chapter Two", 2019),
                                    make_settings())
    target = movies / "It Chapter Two (2019)"
    assert plan.root == target
    assert {r.src.name for r in plan.rows} == {"Some.Film.2019.2160p.mkv",
                                               "Some.Film.2019.2160p.ru.srt"}
    library.apply_plan(plan)
    assert (target / "It Chapter Two (2019).mkv").read_bytes() == b"v"
    assert (target / "It Chapter Two (2019).ru.srt").read_bytes() == b"s"
    assert (movies / "Другой Фильм (2001).mkv").exists()
    assert (ready / "Ready Movie (2010).mkv").exists()


# ------------------------------------------------ метка «не предлагать» --

def test_ignore_marker_hides_folder_from_list(tmp_path):
    movies, tv, s = roots(tmp_path)
    raw = tv / "Top Gear"
    touch(raw / "Top Gear - 01.mkv")
    assert len(library.find_unprocessed(s)) == 1

    library.write_ignore_marker(raw)
    assert library.find_unprocessed(s) == []


def test_ignore_marker_accepts_any_dot_ignore_file(tmp_path):
    movies, tv, s = roots(tmp_path)
    raw = tv / "Top Gear"
    touch(raw / "Top Gear - 01.mkv")
    touch(raw / "потом разберусь.ignore")
    assert library.find_unprocessed(s) == []


def test_ignore_marker_does_not_block_manual_run(tmp_path):
    # Метка влияет только на автоматический обход: вручную папка обрабатывается.
    src = tmp_path / "tv" / "Show.S01"
    touch(src / "Show.S01E01.mkv")
    library.write_ignore_marker(src)
    info = show_info("Show", 2020, [(1, 1, "Pilot")])
    plan = library.build_tv_plan(src, info, make_settings())
    assert dst_of(plan, "S01E01").name == "Show - S01E01 - Pilot.mkv"


def test_ignore_marker_is_not_junk(tmp_path):
    # Регрессия: если метку считать мусором, прогон увезёт её в Extras
    # и папка снова начнёт предлагаться.
    src = tmp_path / "tv" / "Show.S01"
    touch(src / "Show.S01E01.mkv")
    marker = library.write_ignore_marker(src)
    info = show_info("Show", 2020, [(1, 1, "Pilot")])
    plan = library.build_tv_plan(src, info, make_settings())
    row = [r for r in plan.rows if r.src == marker][0]
    assert row.what == "art"
    assert row.dst == tmp_path / "tv" / "Show (2020)" / marker.name


def test_ignore_marker_works_inside_collection(tmp_path):
    movies, tv, s = roots(tmp_path)
    coll = movies / "Scary Movie Collection"
    touch(coll / "Scary Movie 2 (2001)" / "Scary Movie 2 (2001).mkv")
    library.write_ignore_marker(coll / "Scary Movie 2 (2001)")
    assert library.find_unprocessed(s) == []


# ------------------------------- номер серии без «S01E01» (типично для аниме) --

def test_parse_episodes_bare_number():
    # «Koukaku Kidoutai - 01 [WEB-DL AMZN 1080p AVC EAC3]» — сезон неизвестен,
    # серия читается. 1080p и EAC3 номером серии стать не должны.
    assert library.parse_episodes(
        "Koukaku Kidoutai - 01 [WEB-DL AMZN 1080p AVC EAC3].mkv") == (None, [1])
    assert library.parse_episodes("Show E05.mkv") == (None, [5])
    assert library.parse_episodes("Show EP 12.mkv") == (None, [12])
    assert library.parse_episodes("Show #7.mkv") == (None, [7])


def test_parse_episodes_bare_number_false_positives():
    # Всё это номерами серий быть не должно — иначе фильмы поедут в сезоны.
    for name in ("National Lampoon's Loaded Weapon 1 (1993).mkv",
                 "The Conjuring 2 (2016).mkv",
                 "Deadpool & Wolverine (2024).mkv",
                 "Some.Film.2019.2160p.WEB-DL.mkv",
                 "Top Gear - Patagonia Special 2014 (AlexFilm) 1080i.mkv"):
        assert library.parse_episodes(name) == (None, []), name


def test_flat_anime_folder_gets_season_one(tmp_path):
    # Папок сезонов нет, номера голые — считаем это первым сезоном,
    # иначе серии вообще некуда разложить.
    root = tmp_path / "tv" / "Koukaku Kidoutai (2026)"
    for n in (1, 2):
        touch(root / f"Koukaku Kidoutai - 0{n} [WEB-DL AMZN 1080p].mkv")
    scan = library.scan_folder(root)
    assert scan.seasons() == [1]
    assert scan.season_sizes() == {1: 2}

    info = show_info("THE GHOST IN THE SHELL", 2026,
                     [(1, 1, "Prologue"), (1, 2, "Super Spartan")])
    plan = library.build_tv_plan(root, info, make_settings())
    assert dst_of(plan, "- 01").name == \
        "THE GHOST IN THE SHELL - S01E01 - Prologue.mkv"


def test_season_folder_wins_over_assumption(tmp_path):
    # Если папка сезона есть, номер берётся из неё, а не «первый по умолчанию».
    root = tmp_path / "tv" / "Show (2020)"
    touch(root / "Season 03" / "Show - 04 [1080p].mkv")
    scan = library.scan_folder(root)
    assert scan.seasons() == [3]
    info = show_info("Show", 2020, [(3, 4, "Fourth")])
    plan = library.build_tv_plan(root, info, make_settings())
    assert dst_of(plan, "- 04").name == "Show - S03E04 - Fourth.mkv"


# ----------------------------------------- язык имён папок и файлов --

def test_movie_plan_renames_to_the_chosen_language(tmp_path):
    # Личное исключение для тайтла: «Il bisbetico domato» и «The Taming of the
    # Scoundrel» одинаково ни о чём не говорят, а «Укрощение строптивого» — нет.
    movies = tmp_path / "movies"
    root = movies / "The Taming of the Scoundrel (1980)"
    touch(root / "The Taming of the Scoundrel (1980).mkv")

    info = MediaInfo(kind=library.MOVIE, tmdb_id=10986, year=1980,
                     title="Укрощение строптивого",
                     title_en="The Taming of the Scoundrel",
                     original_title="Il bisbetico domato", original_language="it")
    info.name_language = metaconf.NAME_LOCAL
    plan = library.build_movie_plan(root, info, make_settings())

    assert plan.root == movies / "Укрощение строптивого (1980)"
    assert dst_of(plan, ".mkv").name == "Укрощение строптивого (1980).mkv"


def test_tv_plan_switches_episode_titles_with_the_show(tmp_path):
    # Иначе выходит «Что было дальше - S06E01 - Episode 1».
    tv = tmp_path / "tv"
    src = tv / "Chto.bylo.dalshe.S06"
    touch(src / "S06E01.mkv")

    info = MediaInfo(kind=library.TV, tmdb_id=2, year=2020,
                     title="Что было дальше?", title_en="What Happened Next",
                     original_title="Что было дальше?", original_language="ru")
    info.episodes[(6, 1)] = EpisodeInfo(season=6, episode=1,
                                        title="Гость — Сергей Бурунов",
                                        title_en="Episode 1")
    info.name_language = metaconf.NAME_ORIGINAL
    plan = library.build_tv_plan(src, info, make_settings())

    # Вопросительный знак в имени на диске жить не может — его срезает sanitize_name.
    assert plan.root == tv / "Что было дальше (2020)"
    assert dst_of(plan, "S06E01").name == \
        "Что было дальше - S06E01 - Гость — Сергей Бурунов.mkv"


# --------------------------------------- служебные файлы систем --

def test_recognises_system_junk():
    names = ["Thumbs.db", "thumbs.db", "desktop.ini", ".DS_Store",
             "._Archer - S01E01.mkv", "ehthumbs.db"]
    for name in names:
        assert library.is_system_junk(Path(name)), name
    for name in ["Archer - S01E01.mkv", "movie.nfo", "poster.jpg", "._", "database.db"]:
        assert not library.is_system_junk(Path(name)), name


def test_find_system_junk_walks_every_root(tmp_path):
    movies, tv = tmp_path / "movies", tmp_path / "tv"
    touch(movies / "Deadpool (2024)" / "Deadpool (2024).mkv")
    touch(movies / "Deadpool (2024)" / "Thumbs.db")
    touch(movies / ".DS_Store")                       # файл самого корня — тоже наш
    touch(tv / "Archer (2009)" / "Season 01" / "._Archer - S01E01.mkv")  # в глубине
    touch(tv / "Archer (2009)" / "poster.jpg")

    found = library.find_system_junk(
        make_settings(movies_roots=[str(movies)], tv_roots=[str(tv)]))

    assert sorted(p.name for p in found) == [".DS_Store", "._Archer - S01E01.mkv",
                                             "Thumbs.db"]


def test_delete_files_falls_back_to_permanent_when_trash_refuses(tmp_path, monkeypatch):
    # У сетевого тома Корзины нет: send2trash кидает OSError, и файл должен
    # уйти обычным удалением — иначе кнопка не работает именно там, где нужна.
    def no_trash(path):
        raise OSError("Корзина в этом томе отсутствует.")

    monkeypatch.setattr(library, "_trash_func", lambda: no_trash)
    victim = touch(tmp_path / "Thumbs.db", b"x")
    results = library.delete_files([victim])

    assert [(r.ok, r.trashed) for r in results] == [(True, False)]
    assert not victim.exists()


def test_delete_files_reports_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "_trash_func", lambda: None)
    missing = tmp_path / "нет-такого.db"
    results = library.delete_files([missing], to_trash=False)
    assert results[0].ok is False and results[0].error


def _no_trash(path):
    raise OSError("Корзина в этом томе отсутствует.")


def test_junk_is_deleted_outright_when_volume_has_no_trash(tmp_path, monkeypatch):
    # Регрессия с живых данных: Thumbs.db на NAS остался лежать, а прогон
    # отчитался об ошибке. Галка «удалять мусор» — это уже принятое решение,
    # так что при отсутствии Корзины файл удаляется насовсем, как и в Finder.
    monkeypatch.setattr(library, "_trash_func", lambda: _no_trash)
    src = tmp_path / "movies" / "Deadpool.2024"
    touch(src / "movie.mkv", b"v")
    touch(src / "Thumbs.db", b"t")

    plan = library.build_movie_plan(src, movie_info(), make_settings())
    results = library.apply_plan(plan, delete_junk=True)

    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    assert not (src / "Thumbs.db").exists()
    extras = tmp_path / "movies" / "Deadpool & Wolverine (2024)" / "Extras"
    assert not extras.exists()
    junk = [r for r in results if r.row.what == "junk"][0]
    assert junk.disposal == library.DISPOSE_DELETE
    assert "нет Корзины" in junk.row.note


def test_junk_folder_is_deleted_whole_without_trash(tmp_path, monkeypatch):
    # Мусором бывает и папка целиком («Screens» из раздачи) — unlink её не возьмёт.
    monkeypatch.setattr(library, "_trash_func", lambda: _no_trash)
    src = tmp_path / "movies" / "Deadpool.2024"
    touch(src / "movie.mkv", b"v")
    touch(src / "Screens" / "01.png", b"s")

    plan = library.build_movie_plan(src, movie_info(), make_settings())
    library.apply_plan(plan, delete_junk=True)

    assert not (src / "Screens").exists()


def test_junk_goes_to_trash_when_the_volume_has_one(tmp_path, monkeypatch):
    # На обычном диске ничего не меняется: мусор уходит в Корзину, а не насовсем.
    trashed = []
    monkeypatch.setattr(library, "_trash_func", lambda: trashed.append)
    src = tmp_path / "movies" / "Deadpool.2024"
    touch(src / "movie.mkv", b"v")
    touch(src / "Thumbs.db", b"t")

    plan = library.build_movie_plan(src, movie_info(), make_settings())
    results = library.apply_plan(plan, delete_junk=True)

    assert [Path(p).name for p in trashed] == ["Thumbs.db"]
    junk = [r for r in results if r.row.what == "junk"][0]
    assert junk.disposal == library.DISPOSE_TRASH


def test_prune_empty_dirs_removes_only_what_emptied(tmp_path):
    # Ровно случай с NAS: в папке от старого имени остался один Thumbs.db,
    # и пока он лежал, папка не удалялась.
    movies = tmp_path / "movies"
    old = movies / "The Taming of the Scoundrel (1980)"
    junk = touch(old / "Thumbs.db")
    kept = touch(movies / "Deadpool (2024)" / "Thumbs.db")
    touch(movies / "Deadpool (2024)" / "Deadpool (2024).mkv")

    junk.unlink()
    kept.unlink()
    removed = library.prune_empty_dirs([junk, kept], keep=[movies])

    assert removed == [old]
    assert not old.exists()
    assert (movies / "Deadpool (2024)").is_dir()      # там осталось видео


def test_prune_empty_dirs_never_removes_a_library_root(tmp_path):
    movies = tmp_path / "movies"
    junk = touch(movies / ".DS_Store")
    junk.unlink()
    assert library.prune_empty_dirs([junk], keep=[movies]) == []
    assert movies.is_dir()
