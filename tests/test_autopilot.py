import pytest

import artwork
import autopilot
import kodi
import library
import metaconf
import metadata
import paths
import qbit
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
