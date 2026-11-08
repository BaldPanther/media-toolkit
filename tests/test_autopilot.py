from pathlib import Path

import pytest

import artwork
import autopilot
import kodi
import library
import metaconf
import metadata
import numbering
import packs
import paths
import qbit
import search
import telegram
from test_qbit import FakeClient


def _info(tmdb_id=1, title="Show", year=2026):
    info = metadata.MediaInfo(kind=library.TV, tmdb_id=tmdb_id, title=title, title_en=title,
                              original_title=title, original_language="en", year=year)
    for n in (1, 2):
        info.episodes[(1, n)] = metadata.EpisodeInfo(1, n, title=f"Э{n}", title_en=f"Ep {n}")
    return info


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    tv = tmp_path / "tv"
    tv.mkdir()
    metaconf.save_settings(metaconf.Settings(tmdb_key="k", tv_roots=[str(tv)]))
    qbit.save_settings(qbit.Settings(url="http://q", auto=True))
    fake = FakeClient(tv)
    monkeypatch.setattr(qbit, "connect", lambda s: fake)
    hits = [metadata.SearchHit(library.TV, 1, "Show", 2026)]
    monkeypatch.setattr(metadata, "search", lambda kind, q, s, year=None: list(hits))
    monkeypatch.setattr(metadata, "fetch", lambda kind, tmdb_id, s, **kw: _info(tmdb_id))
    monkeypatch.setattr(artwork, "download", lambda tasks, stop=None, progress=None: [])
    monkeypatch.setattr(numbering, "for_title", lambda tmdb_id, key: numbering.Numbering())
    monkeypatch.setattr(packs, "tmdb_movies", lambda info, key, language: lambda title, year: None)
    monkeypatch.setattr(packs, "tvmaze_specials", lambda tmdb_id, key: lambda: [])
    return tv, fake, hits


def _add(tv, fake, h="h1", root="Show.S01.1080p", on_disk=(1,), episodes=(1, 2)):
    fake.add(h, {f"{root}/Show.S01E0{n}.1080p.mkv": 100 for n in episodes})
    for n in on_disk:
        p = tv / root / f"Show.S01E0{n}.1080p.mkv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")


# -------------------------------------------------------------- опознание --

def test_queries_split_titles_and_share_year():
    q = autopilot.queries("Тёмная материя / Dark Matter (2024) WEB-DL 1080p")
    assert ("Тёмная материя", 2024) in q and ("Dark Matter", 2024) in q
    assert autopilot.queries("Dark.Matter.S02.1080p.ATVP.WEB-DL") == [("Dark Matter", None)]


def test_year_found_anywhere_in_name():
    assert autopilot.queries("Polden.S01.2026.WEB-DL.1080p.ExKinoRay") == [("Polden", 2026)]
    assert autopilot.queries("Show.S01.2160p.WEB-DL") == [("Show", None)]


def test_already_arranged_torrent_uses_tmdb_id_from_nfo(env, monkeypatch):
    tv, fake, hits = env
    hits.clear()                        # поиск по имени ничего не дал бы
    root = "Show (2026)"
    _add(tv, fake, root=root)
    (tv / root / "tvshow.nfo").write_text('<tvshow><uniqueid type="tmdb" default="true">77'
                                          '</uniqueid></tvshow>', encoding="utf-8")
    ap = autopilot.Autopilot()
    ap.tick()
    assert ap.items["h1"].status == autopilot.DONE and ap.items["h1"].tmdb_id == 77


def test_identify_by_year_and_exact_title(monkeypatch):
    hits = [metadata.SearchHit(library.TV, 1, "Dark Matter", 2015),
            metadata.SearchHit(library.TV, 2, "Dark Matter", 2024)]
    monkeypatch.setattr(metadata, "search", lambda kind, q, s, year=None: list(hits))
    hit, _ = autopilot.identify("Dark.Matter.2024.S01", library.TV, None)
    assert hit.tmdb_id == 2
    # Без года две одинаковые — надо спрашивать.
    hit, candidates = autopilot.identify("Dark.Matter.S01", library.TV, None)
    assert hit is None and [c.tmdb_id for c in candidates] == [1, 2]


def test_identify_retries_without_season_year(monkeypatch):
    calls = []

    def search(kind, q, s, year=None):
        calls.append(year)
        return [] if year else [metadata.SearchHit(library.TV, 7, "Show", 2024)]

    monkeypatch.setattr(metadata, "search", search)
    hit, _ = autopilot.identify("Show (2026) Сезон 2", library.TV, None)
    assert hit.tmdb_id == 7 and calls == [2026, None]


# ---------------------------------------------------------------- проверка --

def test_tick_arranges_new_torrent_through_qbit(env):
    tv, fake, _ = env
    _add(tv, fake)
    ap = autopilot.Autopilot()
    ap.tick()
    item = ap.items["h1"]
    assert item.status == autopilot.DONE, item.error
    assert item.title == "Show (2026)"
    assert sorted(new for _, new in fake.renamed) == [
        "Show (2026)/Season 01/Show - S01E01 - Ep 1.mkv",
        "Show (2026)/Season 01/Show - S01E02 - Ep 2.mkv"]
    assert ("seq", "h1") in fake.toggled and ("first_last", "h1") in fake.toggled
    # .nfo ещё не скачанной серии уже на месте.
    assert (tv / "Show (2026)" / "Season 01" / "Show - S01E02 - Ep 2.nfo").exists()
    # Повторная проверка ничего не переименовывает и галки не трогает.
    fake.renamed.clear()
    fake.toggled.clear()
    ap.tick()
    assert fake.renamed == [] and fake.toggled == []
    # Состояние переживает перезапуск.
    assert autopilot.Autopilot().items["h1"].status == autopilot.DONE


def test_search_download_skips_unwanted_seasons_of_pack(env, monkeypatch):
    tv, fake, hits = env
    hits.clear()                        # опознание не нужно: тайтл известен из поиска
    info = _info()
    info.episodes[(2, 1)] = metadata.EpisodeInfo(2, 1, title="Э21", title_en="Ep 2x1")
    monkeypatch.setattr(metadata, "fetch", lambda kind, tmdb_id, s, **kw: info)
    fake.add("pk", {"Show.S01-02/Show.S01E01.mkv": 100, "Show.S01-02/Show.S02E01.mkv": 100})
    ap = autopilot.Autopilot()
    ap.expect("pk", library.TV, 1, "Show S01-02", wanted=[2])
    ap.tick()
    assert ap.items["pk"].status == autopilot.DONE
    assert fake.priorities == [("pk", [0], 0)]          # S01E01 — не загружать
    assert [new for _, new in fake.renamed] == ["Show (2026)/Season 02/Show - S02E01 - Ep 2x1.mkv"]


def test_tick_ignores_other_categories_and_disabled_auto(env):
    tv, fake, _ = env
    fake.category = "games"
    _add(tv, fake)
    ap = autopilot.Autopilot()
    ap.tick()
    assert ap.items == {} and fake.renamed == []
    fake.category = "tv"
    qbit.save_settings(qbit.Settings(url="http://q", auto=False))
    ap.tick()
    assert ap.items == {}


def test_tick_skips_non_video(env):
    tv, fake, _ = env
    fake.add("g", {"Game/setup.iso": 10_000, "Game/readme.txt": 1})
    ap = autopilot.Autopilot()
    ap.tick()
    assert ap.items["g"].status == autopilot.SKIP and fake.toggled == []


def test_removed_torrent_leaves_list_and_readded_is_arranged_again(env):
    tv, fake, _ = env
    _add(tv, fake, on_disk=())
    ap = autopilot.Autopilot()
    ap.tick()
    assert [i.hash for i in ap.recent()] == ["h1"]
    names = list(fake.names["h1"])
    # Удалили в qBittorrent — из списка пропала сразу, а помнится ещё сутки.
    del fake.names["h1"], fake.sizes["h1"]
    ap.tick()
    assert ap.recent() == [] and "h1" in ap.items
    # Добавили снова — имена опять из раздачи, раскладывается заново.
    fake.add("h1", {f"Show.S01.1080p/Show.S01E0{n}.1080p.mkv": 100 for n in (1, 2)})
    assert fake.names["h1"] != names
    fake.renamed.clear()
    ap.tick()
    assert ap.items["h1"].status == autopilot.DONE and len(fake.renamed) == 2
    # Удалили насовсем — через сутки забыта.
    del fake.names["h1"], fake.sizes["h1"]
    ap.items["h1"].seen -= autopilot.KEEP_GONE + 1
    ap.tick()
    assert "h1" not in ap.items


def test_videos_without_episode_number_are_error_not_done(env):
    tv, fake, _ = env
    root = "[Group] Show [BDRip 1080p]"
    fake.add("v", {f"{root}/Show Opening.mkv": 100, f"{root}/Show Ending.mkv": 100})
    ap = autopilot.Autopilot()
    ap.tick()
    item = ap.items["v"]
    assert item.status == autopilot.ERROR and "нет номера серии: 2" in item.error
    assert fake.renamed == []


def test_episode_number_without_marker_taken_from_siblings(env):
    tv, fake, _ = env
    root = "[VCB-Studio] Show [BDRip 1080p x265 FLAC]"
    fake.add("v", {f"{root}/[VCB-Studio] Show 0{n} [BDRip 1080p x265 FLAC].mkv": 100 for n in (1, 2)})
    ap = autopilot.Autopilot()
    ap.tick()
    assert ap.items["v"].status == autopilot.DONE, ap.items["v"].error
    assert sorted(new for _, new in fake.renamed) == [
        "Show (2026)/Season 01/Show - S01E01 - Ep 1.mkv",
        "Show (2026)/Season 01/Show - S01E02 - Ep 2.mkv"]


def test_episodes_already_in_library_are_not_downloaded(env):
    tv, fake, _ = env
    # В медиатеке уже есть первая серия — из другой раздачи, с другим названием.
    have = tv / "Show (2026)" / "Season 01" / "Show - S01E01 - Пилот.mkv"
    have.parent.mkdir(parents=True)
    have.write_bytes(b"old")
    fake.add("h1", {"Other.S01/Other.S01E01.mkv": 100, "Other.S01/Other.S01E02.mkv": 100,
                    "Other.S01/Other.S01E01.srt": 1})
    ap = autopilot.Autopilot()
    ap.expect("h1", library.TV, 1, "Other S01", hold=True)
    ap.tick()
    item = ap.items["h1"]
    assert item.status == autopilot.DONE, item.error
    assert fake.priorities == [("h1", [0, 2], 0)]          # серия 1 и её субтитры
    assert [new for _, new in fake.renamed] == ["Show (2026)/Season 01/Show - S01E02 - Ep 2.mkv"]
    assert have.read_bytes() == b"old"
    assert ("start", "h1") in fake.events and not item.hold


fetched = []           # аргументы metadata.fetch в тестах аниме


def _anime(env, monkeypatch, layout):
    """TMDb: один сезон из 6 серий, у трекеров — три сезона по две. В медиатеке —
    первые 4 серии: сквозными номерами (S01E01–04) или по сезонам (S01, S02);
    `layout` None — тайтла в медиатеке нет."""
    tv, fake, hits = env
    hits.clear()
    fetched.clear()
    nb = numbering.Numbering([numbering.Span(n, 1, 2 * n - 1, 2) for n in (1, 2, 3)], grouped=True)
    monkeypatch.setattr(numbering, "for_title", lambda tmdb_id, key: nb)
    info = _info()
    for n in range(1, 7):
        info.episodes[(1, n)] = metadata.EpisodeInfo(1, n, title=f"Э{n}", title_en=f"Ep {n}")
    monkeypatch.setattr(metadata, "fetch",
                        lambda kind, tmdb_id, s, **kw: fetched.append(kw) or info)
    if layout is None:
        return tv, fake
    show = tv / "Show (2026)"
    show.mkdir()
    (show / "tvshow.nfo").write_text('<tvshow><uniqueid type="tmdb">1</uniqueid></tvshow>',
                                     encoding="utf-8")
    for season, episode in layout:
        p = show / f"Season {season:02d}" / f"Show - S{season:02d}E{episode:02d} - old.mkv"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"old")
    return tv, fake


def test_anime_pack_downloads_only_new_season_with_absolute_numbers(env, monkeypatch):
    tv, fake = _anime(env, monkeypatch, [(1, n) for n in range(1, 5)])
    fake.add("pk", {f"Show.S01-03/Show.S0{s}E0{e}.mkv": 100 for s in (1, 2, 3) for e in (1, 2)})
    ap = autopilot.Autopilot()
    ap.expect("pk", library.TV, 1, "Show S01-03", seasons=[1, 2, 3])
    ap.tick()
    assert ap.items["pk"].status == autopilot.DONE, ap.items["pk"].error
    assert fake.priorities == [("pk", [0, 1, 2, 3], 0)]     # сезоны 1–2 уже есть
    assert sorted(new for _, new in fake.renamed) == [
        "Show (2026)/Season 01/Show - S01E05 - Ep 5.mkv",
        "Show (2026)/Season 01/Show - S01E06 - Ep 6.mkv"]
    assert fetched[-1]["art_seasons"] is None               # сквозные номера — сезоны TMDb


def test_anime_single_season_release_keeps_library_layout(env, monkeypatch):
    # В медиатеке тайтл уже по сезонам трекеров (как «Solo Leveling»), раздача — «ТВ-3»
    # без сезона в именах файлов.
    tv, fake = _anime(env, monkeypatch, [(1, 1), (1, 2), (2, 1), (2, 2)])
    fake.add("t3", {"[ТВ-3] Show/Show - 01.mkv": 100, "[ТВ-3] Show/Show - 02.mkv": 100})
    ap = autopilot.Autopilot()
    ap.expect("t3", library.TV, 1, "[ТВ-3] Show")      # сезон — из имени раздачи
    ap.tick()
    assert ap.items["t3"].status == autopilot.DONE, ap.items["t3"].error
    assert sorted(new for _, new in fake.renamed) == [
        "Show (2026)/Season 03/Show - S03E01 - Ep 5.mkv",
        "Show (2026)/Season 03/Show - S03E02 - Ep 6.mkv"]


def test_new_anime_title_goes_by_tracker_seasons(env, monkeypatch):
    # Тайтла в медиатеке нет — сезоны как у раздачи (S02E01), а не сквозные номера TMDb.
    tv, fake = _anime(env, monkeypatch, None)
    fake.add("t2", {"[ТВ-2] Show/Show - 01.mkv": 100, "[ТВ-2] Show/Show - 02.mkv": 100})
    ap = autopilot.Autopilot()
    ap.expect("t2", library.TV, 1, "[ТВ-2] Show")
    ap.tick()
    assert ap.items["t2"].status == autopilot.DONE, ap.items["t2"].error
    assert sorted(new for _, new in fake.renamed) == [
        "Show (2026)/Season 02/Show - S02E01 - Ep 3.mkv",
        "Show (2026)/Season 02/Show - S02E02 - Ep 4.mkv"]
    assert fetched[-1]["art_seasons"] == [2]                # постер Season 02, а не сезона TMDb


# -------------------------------------------------------- сборник VCB-Studio --

def _vcb(env, monkeypatch):
    """Как «Re:Zero» S01–03 + OVA от VCB-Studio: первый сезон — режиссёрская версия (13
    серий из 25 ТВ, первая ТВ-серия — 50 минут), дальше нумерация своя сквозная: второй
    сезон — «14»…«38», третий — «39»…«54». OVA без номера, мини-спецвыпуски в SPs.
    TMDb: один сезон из 66 серий, у трекеров — 25/25/16."""
    tv, fake, hits = env
    hits.clear()
    nb = numbering.Numbering([numbering.Span(1, 1, 1, 25), numbering.Span(2, 1, 26, 25),
                              numbering.Span(3, 1, 51, 16)], grouped=True)
    monkeypatch.setattr(numbering, "for_title", lambda tmdb_id, key: nb)
    monkeypatch.setattr(packs, "tmdb_runtimes", lambda tmdb_id, key: lambda season: {
        n: 50 if n == 1 else 25 for n in range(1, 67)})
    info = _info()
    for n in range(1, 67):
        aired = (f"2016-05-{n:02d}" if n <= 25 else f"2020-07-{n - 25:02d}" if n <= 50
                 else f"2024-10-{n - 50:02d}")
        info.episodes[(1, n)] = metadata.EpisodeInfo(1, n, title=f"Э{n}", title_en=f"Ep {n}",
                                                     aired=aired)
    for n, name in enumerate(["Recap", "Break Time from Zero - Party - 1",
                              "Memory Snow: Manner Movie"], start=1):
        info.episodes[(0, n)] = metadata.EpisodeInfo(0, n, title=name, title_en=name)
    monkeypatch.setattr(metadata, "fetch", lambda kind, tmdb_id, s, **kw: info)
    top = "[VCB-Studio] Show [BDRip 1080p x265 FLAC]"
    files = {}
    for folder, numbers, size in (("[S01 Director's Cut SPs 2016]", range(1, 14), 1500),
                                  ("[S02 SPs 2020-2021]", range(14, 39), 800),
                                  ("[S03 SPs 2024-2025]", range(39, 55), 1000)):
        for n in numbers:
            files[f"{top}/[VCB-Studio] Show {folder}/[VCB-Studio] Show {n:02d} [BDRip].mkv"] = size
    for n in (1, 2, 3):
        files[f"{top}/[VCB-Studio] Show [S01 Director's Cut SPs 2016]/SPs/"
              f"[VCB-Studio] Show Break Time 01.{n:02d} [BDRip].mkv"] = 30
        files[f"{top}/[VCB-Studio] Show [S02 SPs 2020-2021]/SPs/"
              f"[VCB-Studio] Re Petit 02.{n:02d} [BDRip].mkv"] = 30
    ova1 = f"{top}/[VCB-Studio] Show Memory Snow [OVA-1 SPs 2018]"
    files[f"{ova1}/[VCB-Studio] Show Memory Snow [BDRip].mkv"] = 1900
    files[f"{ova1}/SPs/[VCB-Studio] Show Memory Snow Manner Movie [BDRip].mkv"] = 45
    files[f"{top}/[VCB-Studio] Show Hyouketsu no Kizuna [OVA-2 SPs 2019]/"
          f"[VCB-Studio] Show Hyouketsu no Kizuna [BDRip].mkv"] = 3500
    fake.add("vcb", files)
    return tv, fake


def _renamed(fake) -> dict[str, str]:
    return {old.rsplit("/", 1)[1]: new for old, new in fake.renamed}


def test_vcb_pack_whole_director_cut_merged_and_seasons_shifted(env, monkeypatch):
    tv, fake = _vcb(env, monkeypatch)
    ap = autopilot.Autopilot()
    ap.expect("vcb", library.TV, 1, "Re:Zero [S01-03 + OVA + Specials]", seasons=[1, 2, 3])
    ap.tick()
    item = ap.items["vcb"]
    assert item.status == autopilot.DONE, item.error
    got = _renamed(fake)
    assert got["[VCB-Studio] Show 01 [BDRip].mkv"] == "Show (2026)/Season 01/Show - S01E01 - Ep 1.mkv"
    assert got["[VCB-Studio] Show 02 [BDRip].mkv"] == "Show (2026)/Season 01/Show - S01E02+E03 - Ep 2.mkv"
    assert got["[VCB-Studio] Show 13 [BDRip].mkv"] == "Show (2026)/Season 01/Show - S01E24+E25 - Ep 24.mkv"
    assert got["[VCB-Studio] Show 14 [BDRip].mkv"] == "Show (2026)/Season 02/Show - S02E01 - Ep 26.mkv"
    assert got["[VCB-Studio] Show 38 [BDRip].mkv"] == "Show (2026)/Season 02/Show - S02E25 - Ep 50.mkv"
    assert got["[VCB-Studio] Show 39 [BDRip].mkv"] == "Show (2026)/Season 03/Show - S03E01 - Ep 51.mkv"
    assert got["[VCB-Studio] Show 54 [BDRip].mkv"] == "Show (2026)/Season 03/Show - S03E16 - Ep 66.mkv"
    # OVA: в TMDb их нет — в Specials после последнего спецвыпуска, по году.
    assert got["[VCB-Studio] Show Memory Snow [BDRip].mkv"] ==         "Show (2026)/Specials/Show - S00E04 - Memory Snow.mkv"
    assert got["[VCB-Studio] Show Hyouketsu no Kizuna [BDRip].mkv"] ==         "Show (2026)/Specials/Show - S00E05 - Hyouketsu no Kizuna.mkv"
    # Мини-спецвыпуски — «не загружать» и не раскладываются.
    names = fake.names["vcb"]
    minis = [i for i, n in enumerate(names) if "Break Time" in n or "Re Petit" in n
             or "Manner" in n]
    assert len(minis) == 7 and fake.priorities == [("vcb", minis, 0)]
    assert not any("Break Time" in old or "Manner" in old for old, _ in fake.renamed)
    # Склеенная серия: одно видео — одна серия в Kodi, с обоими названиями.
    season1 = tv / "Show (2026)/Season 01"
    text = (season1 / "Show - S01E02+E03 - Ep 2.nfo").read_text(encoding="utf-8")
    assert text.count("<episodedetails>") == 1
    assert "<episode>2</episode>" in text and "<title>Э2 / Э3</title>" in text
    assert not list(season1.glob("*-S01E0*.nfo"))
    # OVA — только в Specials: внутрь сезонов не ставятся.
    ova = (tv / "Show (2026)/Specials/Show - S00E04 - Memory Snow.nfo").read_text(encoding="utf-8")
    assert "displayseason" not in ova and "displayafterseason" not in ova


def test_vcb_pack_ova_from_tmdb_movie(env, monkeypatch):
    tv, fake = _vcb(env, monkeypatch)
    asked = []

    def movie(title, year):
        asked.append((title, year))
        if title != "Hyouketsu no Kizuna":
            return None
        return metadata.EpisodeInfo(0, 0, title="Замороженные узы", title_en="The Frozen Bond",
                                    plot="Лес Элиор.", aired="2019-11-08", runtime=76,
                                    thumb="https://image.tmdb.org/t/p/original/b.jpg")

    monkeypatch.setattr(packs, "tmdb_movies", lambda info, key, language: movie)
    ap = autopilot.Autopilot()
    ap.expect("vcb", library.TV, 1, "Re:Zero [S01-03 + OVA + Specials]", seasons=[1, 2, 3])
    ap.tick()
    assert ap.items["vcb"].status == autopilot.DONE, ap.items["vcb"].error
    assert ("Hyouketsu no Kizuna", 2019) in asked and ("Memory Snow", 2018) in asked
    got = _renamed(fake)
    assert got["[VCB-Studio] Show Hyouketsu no Kizuna [BDRip].mkv"] == \
        "Show (2026)/Specials/Show - S00E05 - The Frozen Bond.mkv"
    text = (tv / "Show (2026)/Specials/Show - S00E05 - The Frozen Bond.nfo").read_text(encoding="utf-8")
    for part in ("<title>Замороженные узы</title>", "<episode>5</episode>",
                 "<aired>2019-11-08</aired>", "<runtime>76</runtime>",
                 "<thumb>https://image.tmdb.org/t/p/original/b.jpg</thumb>"):
        assert part in text


def test_vcb_pack_first_season_only(env, monkeypatch):
    tv, fake = _vcb(env, monkeypatch)
    ap = autopilot.Autopilot()
    ap.expect("vcb", library.TV, 1, "Re:Zero [S01-03]", wanted=[1], seasons=[1, 2, 3])
    ap.tick()
    assert ap.items["vcb"].status == autopilot.DONE, ap.items["vcb"].error
    names = fake.names["vcb"]
    off = {i for _, ids, _ in fake.priorities for i in ids}
    later = {i for i, n in enumerate(names) if "[S02 SPs" in n and "/SPs/" not in n
             or "[S03 SPs" in n}
    assert later <= off and len(later) == 41
    # Первый сезон — режиссёрская версия целиком, а не она плюс полсезона второго.
    renamed = [new for _, new in fake.renamed]
    assert sum("/Season 01/" in n for n in renamed) == 13
    assert not any("/Season 02/" in n or "/Season 03/" in n for n in renamed)


def test_vcb_pack_minis_when_asked(env, monkeypatch):
    tv, fake = _vcb(env, monkeypatch)
    prefs = search.load_prefs()
    prefs.minis = True
    search.save_prefs(prefs)
    ap = autopilot.Autopilot()
    ap.expect("vcb", library.TV, 1, "Re:Zero [S01-03 + OVA + Specials]", seasons=[1, 2, 3])
    ap.tick()
    assert ap.items["vcb"].status == autopilot.DONE, ap.items["vcb"].error
    assert not getattr(fake, "priorities", [])
    got = _renamed(fake)
    # Название совпало со спецвыпуском TMDb — его номер.
    assert got["[VCB-Studio] Show Memory Snow Manner Movie [BDRip].mkv"] ==         "Show (2026)/Specials/Show - S00E03 - Memory Snow - Manner Movie.mkv"
    # Свои номера раздачи («01.02») — не номера TMDb: следом за его спецвыпусками.
    specials = sorted(new for old, new in fake.renamed if "/Specials/" in new)
    assert len(specials) == 9 and len(set(specials)) == 9
    assert got["[VCB-Studio] Show Break Time 01.01 [BDRip].mkv"].startswith(
        "Show (2026)/Specials/Show - S00E")


def test_held_torrent_that_failed_stays_paused_and_warns_once(env):
    tv, fake, hits = env
    said = []
    fake.add("h1", {"Show.S01/Show Opening.mkv": 100, "Show.S01/Show Ending.mkv": 100})
    ap = autopilot.Autopilot(notify=lambda text: said.append(text) or True)
    ap.expect("h1", library.TV, 1, "Show S01", hold=True)
    ap.tick()
    ap.tick()
    item = ap.items["h1"]
    assert item.status == autopilot.ERROR and item.hold
    assert ("start", "h1") not in fake.events
    assert len(said) == 1 and "на паузе" in said[0] and "нет номера серии" in said[0]


def test_ambiguous_title_asks_and_answer_arranges(env, monkeypatch):
    tv, fake, hits = env
    hits[:] = [metadata.SearchHit(library.TV, 1, "Show", 2026),
               metadata.SearchHit(library.TV, 2, "Show", 1999)]
    _add(tv, fake)
    ap = autopilot.Autopilot()
    ap.tick()
    item = ap.items["h1"]
    assert item.status == autopilot.ASK and fake.renamed == []
    assert [label for _, label in item.candidates] == ["Show (2026)", "Show (1999)"]
    # Ответ на вкладке: второй вариант.
    ap.answer("h1", 1)
    ap.tick()
    assert item.status == autopilot.DONE and item.tmdb_id == 2


def test_answer_skip_marks_skipped(env):
    tv, fake, hits = env
    hits[:] = [metadata.SearchHit(library.TV, 1, "Show", 2026),
               metadata.SearchHit(library.TV, 2, "Show", 1999)]
    _add(tv, fake)
    ap = autopilot.Autopilot()
    ap.tick()
    ap.answer("h1", -1)
    ap.tick()
    assert ap.items["h1"].status == autopilot.SKIP and fake.renamed == []


def test_not_found_is_error_and_retry_requeues(env, monkeypatch):
    tv, fake, hits = env
    hits.clear()
    _add(tv, fake)
    ap = autopilot.Autopilot()
    ap.tick()
    assert ap.items["h1"].status == autopilot.ERROR
    hits.append(metadata.SearchHit(library.TV, 1, "Show", 2026))
    ap.retry("h1")
    ap.tick()
    assert ap.items["h1"].status == autopilot.DONE


def test_finished_download_is_reported_once_and_kodi_scans(env, monkeypatch):
    tv, fake, _ = env
    _add(tv, fake)
    scanned = []
    monkeypatch.setattr(kodi, "scan", lambda path, roots, settings=None: scanned.append(path) or "x")
    logged = []
    ap = autopilot.Autopilot(log=logged.append)
    ap.tick()
    fake.progress = 1.0
    ap.tick()
    ap.tick()
    assert sum("скачано" in t for t in logged) == 1
    assert len(scanned) == 2            # после раскладки и после докачки


def test_kodi_offline_is_said_once(env, monkeypatch):
    tv, fake, _ = env
    _add(tv, fake)
    state = {"off": True}

    def scan(path, roots, settings=None):
        if state["off"]:
            raise kodi.KodiOffline("Kodi недоступен")
        return "smb://nas/tv/"

    monkeypatch.setattr(kodi, "scan", scan)
    logged = []
    ap = autopilot.Autopilot(log=logged.append)
    ap.tick()                           # после раскладки
    fake.progress = 1.0
    ap.tick()                           # после докачки
    assert sum("Kodi не отвечает" in t for t in logged) == 1
    assert not any(t.startswith("⚠") for t in logged)
    state["off"] = False
    ap.retry("h1")
    ap.tick()
    assert any("Kodi: сканирование" in t for t in logged)


def test_missing_tmdb_key_is_reported(env):
    metaconf.save_settings(metaconf.Settings(tmdb_key=""))
    with pytest.raises(qbit.QbitError, match="TMDb"):
        autopilot.Autopilot().tick()


# ----------------------------------------------------------------- Telegram --

def test_telegram_binds_first_start_and_ignores_strangers(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    calls = []
    monkeypatch.setattr(telegram.Bot, "call", lambda self, method, http_timeout=None, **p:
                        calls.append((method, p)) or {"message_id": 1})
    telegram.save_settings(telegram.Settings(token="t"))
    svc = telegram.Service()
    pressed = []
    svc.on("wl", lambda args: pressed.append(args) or "ок")
    svc.handle({"message": {"chat": {"id": 42}, "text": "/start"}})
    assert telegram.load_settings().chat_id == 42
    # Чужой /start и чужая кнопка — молча мимо.
    svc.handle({"message": {"chat": {"id": 7}, "text": "/start"}})
    svc.handle({"callback_query": {"id": "c1", "data": "wl:x:1", "message": {"chat": {"id": 7}}}})
    assert telegram.load_settings().chat_id == 42 and pressed == []
    svc.handle({"callback_query": {"id": "c2", "data": "wl:x:1", "message": {"chat": {"id": 42}}}})
    assert pressed == ["x:1"]
    assert ("answerCallbackQuery", {"callback_query_id": "c2", "text": "ок"}) in calls
    assert svc.notify("hi") == 1
    assert calls[-1][0] == "sendMessage" and calls[-1][1]["chat_id"] == 42


def test_telegram_keyboard_shapes():
    kb = telegram.keyboard([[("Да", "wl:1:0"), ("Страница", "https://rutor.info/t/1")]])
    assert kb == {"inline_keyboard": [[{"text": "Да", "callback_data": "wl:1:0"},
                                       {"text": "Страница", "url": "https://rutor.info/t/1"}]]}


# --------------------------------------------------------------------- Kodi --

def test_kodi_path_map_from_smb_source():
    m = kodi.detect_map(["smb://192.168.1.3/nas/media/tv/", "smb://192.168.1.3/nas/media/movies/"],
                        ["/media/disk1/nas/media/tv"])
    assert m.remote == "smb://192.168.1.3/"
    assert m.to_kodi("/media/disk1/nas/media/tv/Show (2026)") == \
        "smb://192.168.1.3/nas/media/tv/Show (2026)/"
    assert kodi.detect_map(["smb://x/share/music/"], ["/media/tv"]) is None


# ---------------------------------------------------- обновляемая раздача --

def test_update_map_matches_by_size_then_episode():
    old = qbit.Torrent("o", "o", "tv", 1, Path("/tv"), Path("/tv/Show (2026)"), [
        qbit.TorrentFile("Show (2026)/Season 01/Show - S01E01 - A.mkv", 100, 1),
        qbit.TorrentFile("Show (2026)/Season 01/Show - S01E02 - B.mkv", 100, 1),
        qbit.TorrentFile("Show (2026)/Season 01/Show - S01E01 - A.rus.srt", 7, 1)])
    new = qbit.Torrent("n", "n", "tv", 0, Path("/tv"), Path("/tv/Show.S01"), [
        qbit.TorrentFile("Show.S01/Show.S01E02.mkv", 100, 0),
        qbit.TorrentFile("Show.S01/Show.S01E01.mkv", 100, 0),
        qbit.TorrentFile("Show.S01/Show.S01E03.mkv", 120, 0),
        qbit.TorrentFile("Show.S01/Subs/Show.S01E01.rus.srt", 7, 0)])
    assert dict(autopilot.update_map(new, old)) == {
        "Show.S01/Show.S01E02.mkv": "Show (2026)/Season 01/Show - S01E02 - B.mkv",
        "Show.S01/Show.S01E01.mkv": "Show (2026)/Season 01/Show - S01E01 - A.mkv",
        "Show.S01/Subs/Show.S01E01.rus.srt": "Show (2026)/Season 01/Show - S01E01 - A.rus.srt"}


def test_updated_release_reuses_downloaded_files(env):
    tv, fake, _ = env
    page = "https://rutracker.org/forum/viewtopic.php?t=1"
    _add(tv, fake, on_disk=(1, 2))
    ap = autopilot.Autopilot()
    ap.expect("h1", library.TV, 1, "Show.S01.1080p", pages=[page])
    ap.tick()
    assert ap.items["h1"].status == autopilot.DONE
    # На трекере добавили третью серию — новый хеш, та же страница.
    old = ap.replaced_by([page], "h2")
    assert old is not None and old.hash == "h1"
    fake.add("h2", {f"Show.S01.1080p/Show.S01E0{n}.1080p.mkv": 100 for n in (1, 2, 3)})
    ap.expect("h2", library.TV, 1, "Show.S01.1080p v2", pages=[page], replaces="h1")
    fake.renamed.clear()
    ap.tick()                                   # список файлов есть — имена уже скачанных
    assert sorted(fake.renamed) == [
        ("Show.S01.1080p/Show.S01E01.1080p.mkv", "Show (2026)/Season 01/Show - S01E01 - Ep 1.mkv"),
        ("Show.S01.1080p/Show.S01E02.1080p.mkv", "Show (2026)/Season 01/Show - S01E02 - Ep 2.mkv"),
        # новая серия — пока в папку тайтла, на место её поставит раскладка
        ("Show.S01.1080p/Show.S01E03.1080p.mkv", "Show (2026)/Show.S01.1080p/Show.S01E03.1080p.mkv")]
    ap.tick()                                   # перепроверка
    assert ("recheck", "h2") in fake.events
    ap.tick()
    assert ("remove", "h1") not in fake.events  # ещё проверяет
    fake.states["h2"] = "stoppedDL"
    ap.tick()
    assert fake.events[-2:] == [("remove", "h1"), ("start", "h2")]
    assert ap.items["h2"].replaces == ""
    fake.renamed.clear()
    ap.tick()                                   # обычная раскладка: новая серия на место
    assert ap.items["h2"].status == autopilot.DONE, ap.items["h2"].error
    assert fake.renamed == [("Show (2026)/Show.S01.1080p/Show.S01E03.1080p.mkv",
                             "Show (2026)/Season 01/Show - S01E03.mkv")]


def test_update_without_common_files_downloads_whole(env):
    tv, fake, _ = env
    _add(tv, fake)
    ap = autopilot.Autopilot()
    ap.tick()
    fake.add("h2", {"Other.S01/Other.S01E01.mkv": 555})
    ap.expect("h2", library.TV, 1, "Other", replaces="h1")
    ap.tick()
    assert ap.items["h2"].replaces == "" and ("start", "h2") in fake.events
    assert ("remove", "h1") not in fake.events


def test_new_version_found_by_topic_even_if_rutor_slug_changed(env):
    tv, fake, _ = env
    _add(tv, fake)
    ap = autopilot.Autopilot()
    ap.expect("h1", library.TV, 1, "Show [01x01-22 из 24]",
              pages=["http://rutor.info/torrent/959050/show-01x01-22-iz-24"])
    ap.tick()
    old = ap.replaced_by(["http://rutor.info/torrent/959050/show-01x01-23-iz-24"], "h2")
    assert old is not None and old.hash == "h1"


def test_new_movie_version_moves_old_file_to_extras(env, monkeypatch):
    tv, fake, hits = env
    movies = tv.parent / "movies"
    movies.mkdir()
    metaconf.save_settings(metaconf.Settings(tmdb_key="k", tv_roots=[str(tv)],
                                             movies_roots=[str(movies)]))
    qbit.save_settings(qbit.Settings(url="http://q", auto=True))
    fake.save = fake.remote_save_path = movies
    fake.remote_save = str(movies)
    fake.category = "movies"
    info = metadata.MediaInfo(kind=library.MOVIE, tmdb_id=9, title="Film", title_en="Film",
                              original_title="Film", original_language="en", year=2021)
    monkeypatch.setattr(metadata, "fetch", lambda kind, tmdb_id, s, **kw: info)
    old_dir = movies / "Film (2021)"
    old_dir.mkdir()
    (old_dir / "Film (2021).mkv").write_bytes(b"1080p")
    (old_dir / "Film (2021).edl").write_text("1 2 3", encoding="utf-8")
    (old_dir / "Film (2021).ru.srt").write_text("мои", encoding="utf-8")
    fake.add("uhd", {"Film.2021.2160p/Film.2021.2160p.mkv": 100,
                     "Film.2021.2160p/Film.2021.2160p.ru.srt": 1})
    ap = autopilot.Autopilot()
    ap.expect("uhd", library.MOVIE, 9, "Film 2160p", hold=True)
    ap.tick()
    assert ap.items["uhd"].status == autopilot.DONE, ap.items["uhd"].error
    assert (old_dir / "Extras" / "Film (2021).mkv").read_bytes() == b"1080p"
    # .edl — при фильме; в раздаче свои субтитры — они встают под имя фильма, а прежние
    # (к старой версии) — в Extras.
    assert (old_dir / "Film (2021).edl").exists()
    assert (old_dir / "Extras" / "Film (2021).ru.srt").read_text(encoding="utf-8") == "мои"
    assert sorted(new for _, new in fake.renamed) == ["Film (2021)/Film (2021).mkv",
                                                      "Film (2021)/Film (2021).ru.srt"]


def test_new_movie_version_without_subs_keeps_old_subs(env, monkeypatch):
    tv, fake, hits = env
    movies = tv.parent / "movies"
    movies.mkdir()
    metaconf.save_settings(metaconf.Settings(tmdb_key="k", tv_roots=[str(tv)],
                                             movies_roots=[str(movies)]))
    fake.save, fake.remote_save, fake.category = movies, str(movies), "movies"
    info = metadata.MediaInfo(kind=library.MOVIE, tmdb_id=9, title="Film", title_en="Film",
                              original_title="Film", original_language="en", year=2021)
    monkeypatch.setattr(metadata, "fetch", lambda kind, tmdb_id, s, **kw: info)
    old_dir = movies / "Film (2021)"
    old_dir.mkdir()
    (old_dir / "Film (2021).mkv").write_bytes(b"1080p")
    (old_dir / "Film (2021).ru.srt").write_text("мои", encoding="utf-8")
    fake.add("uhd", {"Film.2021.2160p/Film.2021.2160p.mkv": 100})
    ap = autopilot.Autopilot()
    ap.expect("uhd", library.MOVIE, 9, "Film 2160p")
    ap.tick()
    assert ap.items["uhd"].status == autopilot.DONE, ap.items["uhd"].error
    assert (old_dir / "Film (2021).ru.srt").exists()           # своих нет — прежние остаются


def test_original_filename_kept_in_episode_nfo(env):
    tv, fake, _ = env
    _add(tv, fake, on_disk=())
    autopilot.Autopilot().tick()
    text = (tv / "Show (2026)" / "Season 01" / "Show - S01E01 - Ep 1.nfo").read_text(encoding="utf-8")
    assert "<original_filename>Show.S01E01.1080p.mkv</original_filename>" in text


A, B, C = "a" * 40, "b" * 40, "c" * 40
EPISODE = {"Show.S01.1080p/Show.S01E01.1080p.mkv": 100}


def _metadata(fake, h, files=EPISODE):
    """Список файлов пришёл — как у настоящего qBittorrent после метаданных."""
    fake.names[h], fake.sizes[h] = list(files), dict(files)


def test_add_races_twins_with_trackers_appended(env):
    tv, fake, _ = env
    ap = autopilot.Autopilot()
    said = ap.add(f"magnet:?xt=urn:btih:{A}", A, library.TV, 1, "Show S01",
                  alts=[{"hash": B, "magnet": f"magnet:?xt=urn:btih:{B}"}])
    assert said == "Скачиваю"
    assert [m[0].split("btih:")[1][:40] for m in fake.magnets] == [A, B]
    assert all("tr=http%3A%2F%2Fbt4.t-ru.org" in m[0] and m[2] for m in fake.magnets)
    assert list(ap.items) == [A] and ap.items[A].rivals == [B]
    ap.tick()
    assert A in fake.names and B in fake.names and list(ap.items) == [A]   # ждут оба


def test_race_won_by_twin(env):
    tv, fake, _ = env
    ap = autopilot.Autopilot()
    ap.add(f"magnet:?xt=urn:btih:{A}", A, library.TV, 1, "Show S01",
           alts=[{"hash": B, "magnet": f"magnet:?xt=urn:btih:{B}"},
                 {"hash": C, "magnet": f"magnet:?xt=urn:btih:{C}"}])
    _metadata(fake, B)
    ap.tick()
    assert ("remove", A) in fake.events and ("remove", C) in fake.events
    assert list(ap.items) == [B]
    item = ap.items[B]
    assert item.tmdb_id == 1 and item.rivals == [] and item.status == autopilot.DONE, item.error
    assert ("start", B) in fake.events


def test_race_won_by_main_link(env):
    tv, fake, _ = env
    ap = autopilot.Autopilot()
    ap.add(f"magnet:?xt=urn:btih:{A}", A, library.TV, 1, "Show S01",
           alts=[{"hash": B, "magnet": f"magnet:?xt=urn:btih:{B}"}])
    _metadata(fake, A)
    _metadata(fake, B)
    fake.seeds = {A: 5, B: 2}                   # обе успели — где больше сидов
    ap.tick()
    assert ("remove", B) in fake.events and ("remove", A) not in fake.events
    assert list(ap.items) == [A] and ap.items[A].rivals == []


def test_twin_already_in_qbittorrent_is_not_a_rival(env):
    tv, fake, _ = env
    fake.add(B, EPISODE)                        # своя раздача пользователя
    ap = autopilot.Autopilot()
    ap.add(f"magnet:?xt=urn:btih:{A}", A, library.TV, 1, "Show S01",
           alts=[{"hash": B, "magnet": f"magnet:?xt=urn:btih:{B}"}])
    assert len(fake.magnets) == 1 and ap.items[A].rivals == []


def test_stuck_race_warns_once(env):
    tv, fake, _ = env
    said = []
    ap = autopilot.Autopilot(notify=lambda text: said.append(text) or True)
    ap.add(f"magnet:?xt=urn:btih:{A}", A, library.TV, 1, "Show S01",
           alts=[{"hash": B, "magnet": f"magnet:?xt=urn:btih:{B}"}])
    ap.items[A].waiting_since -= autopilot.STUCK + 1
    ap.tick()
    ap.tick()
    assert len(said) == 1 and "ни по одной из 2 ссылок" in said[0]
    assert A in fake.names and B in fake.names           # ждём дальше, ничего не убираем
    _metadata(fake, B)                                   # сид появился
    ap.tick()
    assert list(ap.items) == [B] and not ap.items[B].stuck and not ap.items[B].error


def test_fast_checks_while_waiting_for_file_list(env):
    tv, fake, _ = env
    ap = autopilot.Autopilot()
    assert not ap.waiting()
    ap.add(f"magnet:?xt=urn:btih:{A}", A, library.TV, 1, "Show S01")
    assert ap.waiting()                                  # ждём список файлов — раз в 5 с
    ap.items[A].waiting_since -= autopilot.STUCK + 1
    ap.tick()
    assert not ap.waiting()                              # застряла — обратно раз в 30 с
    _metadata(fake, A)
    ap.tick()
    assert ap.items[A].status == autopilot.DONE and not ap.waiting()


def test_has_metadata_without_flag():
    assert not autopilot._has_metadata({"state": "metaDL", "size": 0})
    assert not autopilot._has_metadata({"state": "queuedDL", "total_size": -1})
    assert autopilot._has_metadata({"state": "stoppedDL", "total_size": 10})
    assert not autopilot._has_metadata({"has_metadata": False, "total_size": 10})


def test_stuck_magnet_without_twin_warns_once(env):
    tv, fake, _ = env
    said = []
    fake.add("magnet:?xt=urn:btih:old")
    ap = autopilot.Autopilot(notify=lambda text: said.append(text) or True)
    ap.expect("old", library.TV, 1, "Show S01", hold=True)
    ap.items["old"].waiting_since -= autopilot.STUCK + 1
    ap.tick()
    ap.tick()
    assert len(said) == 1 and "без списка файлов" in said[0]
    assert "old" in fake.names                           # без двойника — не убираем
