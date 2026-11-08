from pathlib import Path

import pytest

import kodi
import kodisync
import paths

T0 = 10_000.0
STEP = kodisync.CHECK + 1


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    kodi.save_settings(kodi.Settings(url="http://tv:8080", remove_folders=True))
    drive = tmp_path / "disk"
    tv, movies = drive / "media" / "tv", drive / "media" / "movies"
    tv.mkdir(parents=True)
    movies.mkdir(parents=True)
    return drive, tv, movies


def put(path: Path, data: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def run(sync, tv, movies, n, owned=lambda folder: False):
    sync.check([str(tv)], [str(movies)], "Extras", owned, now=T0 + n * STEP)


def test_snapshot_titles(lib):
    _, tv, movies = lib
    put(tv / "Show (2016)" / "Season 01" / "Show - S01E01.mkv")
    put(movies / "Film (1999)" / "Film (1999).mkv")
    put(movies / "Film (1999)" / "Extras" / "Film (1999) - old.mkv")
    put(movies / "Collection" / "Part 2 (2001)" / "Part 2 (2001).mkv")
    put(movies / "loose.mkv")                              # прямо в корне — не тайтл
    put(tv / ".trash" / "Old" / "x.mkv")                   # служебная папка
    snap = kodisync.snapshot([str(tv)], [str(movies)])
    assert set(snap) == {str(tv / "Show (2016)"), str(movies / "Film (1999)"),
                         str(movies / "Collection" / "Part 2 (2001)")}
    film = snap[str(movies / "Film (1999)")]
    assert kodisync.main_videos(film) == ["Film (1999).mkv"]


def test_removed_video_cleans_and_new_video_scans_when_settled(lib):
    _, tv, movies = lib
    ep1 = put(tv / "Show (2016)" / "Season 01" / "Show - S01E01.mkv")
    sync = kodisync.KodiSync()
    run(sync, tv, movies, 0)                               # первый снимок — без действий
    assert sync.queue == []
    ep1.rename(ep1.with_name("Show - S01E01 - Pilot.mkv"))  # раскладка переименовала
    run(sync, tv, movies, 1)
    assert sync.queue == [["clean", str(tv / "Show (2016)")]]
    run(sync, tv, movies, 2)                               # новое не менялось — скан
    assert ["scan", str(tv / "Show (2016)")] in sync.queue
    assert sync._next() == ["clean", str(tv / "Show (2016)")]   # очистка — первой


def test_growing_file_is_not_scanned_yet(lib):
    _, tv, movies = lib
    sync = kodisync.KodiSync()
    run(sync, tv, movies, 0)
    f = put(movies / "Film (1999)" / "Film.mkv", b"1")
    run(sync, tv, movies, 1)
    f.write_bytes(b"12")                                   # ещё копируется
    run(sync, tv, movies, 2)
    assert sync.queue == []
    run(sync, tv, movies, 3)
    assert sync.queue == [["scan", str(movies / "Film (1999)")]]


def test_movie_deleted_in_kodi_folder_goes_to_drive_trash(lib):
    drive, tv, movies = lib
    trash = drive / ".trash"
    trash.mkdir()
    film = movies / "Film (1999)"
    video = put(film / "Film (1999).mkv")
    put(film / "movie.nfo", b"<movie/>")
    put(film / "Extras" / "old.mkv")
    logged = []
    sync = kodisync.KodiSync(logged.append)
    run(sync, tv, movies, 0)
    video.unlink()                                         # Kodi стёр только видео
    run(sync, tv, movies, 1)
    assert film.is_dir()                                   # первая сверка — только заметили
    run(sync, tv, movies, 2)
    assert not film.exists() and (trash / "Film (1999)" / "movie.nfo").is_file()
    assert ["clean", str(film)] in sync.queue
    assert any("убрана в корзину накопителя" in t for t in logged)


def test_folder_kept_when_torrent_there_or_option_off(lib):
    _, tv, movies = lib
    film = movies / "Film (1999)"
    video = put(film / "Film (1999).mkv")
    put(film / "movie.nfo", b"<movie/>")
    sync = kodisync.KodiSync()
    run(sync, tv, movies, 0)
    video.unlink()
    # Новая версия ещё качается: видео нет, а папка — раздачи.
    for n in (1, 2, 3):
        run(sync, tv, movies, n, owned=lambda folder: folder == film)
    assert film.is_dir()
    # qBittorrent не ответил — тоже не трогать.
    run(sync, tv, movies, 4, owned=None)
    assert film.is_dir()
    kodi.save_settings(kodi.Settings(url="http://tv:8080", remove_folders=False))
    for n in (5, 6):
        run(sync, tv, movies, n)
    assert film.is_dir()


def test_unplugged_drive_changes_nothing(lib):
    _, tv, movies = lib
    put(tv / "Show (2016)" / "Season 01" / "Show - S01E01.mkv")
    sync = kodisync.KodiSync()
    run(sync, tv, movies, 0)
    tv.rename(tv.with_name("tv-off"))                      # накопитель не подключён
    run(sync, tv, movies, 1)
    run(sync, tv, movies, 2)
    assert sync.queue == []


def test_remove_folder_without_drive_trash(tmp_path, monkeypatch):
    monkeypatch.setattr(kodisync.sys, "platform", "linux")
    folder = put(tmp_path / "Film (1999)" / "movie.nfo").parent
    assert kodisync.remove_folder(folder) == "насовсем" and not folder.exists()


class FakeKodi:
    sent: list = []

    def __init__(self, *a, **kw):
        pass

    def busy(self):
        return False

    def sources(self):
        return ["smb://nas/media/tv/", "smb://nas/media/movies/"]

    def scan(self, directory=""):
        FakeKodi.sent.append(("scan", directory))

    def clean(self, directory=""):
        FakeKodi.sent.append(("clean", directory))


def test_flush_one_by_one_and_never_whole_library_clean(lib, monkeypatch):
    drive, tv, movies = lib
    FakeKodi.sent = []
    monkeypatch.setattr(kodi, "Client", FakeKodi)
    sync = kodisync.KodiSync()
    away = drive.parent / "downloads" / "Show.S01"          # раздача до раскладки
    sync.request(away)
    sync.request(away, kodisync.CLEAN)
    sync.request(tv / "Show (2016)", kodisync.SCAN)
    roots = [str(tv), str(movies)]
    sync.flush(roots)                                      # вне медиатеки — очистка отброшена
    assert FakeKodi.sent == []
    sync.flush(roots)                                      # скан вне медиатеки — вся медиатека
    sync.flush(roots)
    assert FakeKodi.sent == [("scan", ""), ("scan", "smb://nas/media/tv/Show (2016)/")]
    assert sync.queue == []


def test_library_tab_apply_asks_kodi(lib, monkeypatch):
    from types import SimpleNamespace
    from web import tab_library
    _, tv, _ = lib
    sync = kodisync.KodiSync()
    monkeypatch.setattr(sync, "flush", lambda roots, now=None: None)
    st = SimpleNamespace(tabs={"torrents": SimpleNamespace(autopilot=SimpleNamespace(kodi=sync))})
    old, new = Path("/downloads/Show.S01"), tv / "Show (2016)"
    tab_library._kodi_after(st, old, new)
    assert sync.queue == [["clean", str(old)], ["clean", str(new)], ["scan", str(new)]]
