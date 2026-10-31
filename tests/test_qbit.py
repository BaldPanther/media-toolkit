import os
import unicodedata
from pathlib import Path

import pytest

import library
import metaconf
import metadata
import qbit


class FakeClient:
    """qBittorrent в памяти: раздачи, файлы и переименование — как у настоящего,
    только переезд на диске сразу, а не асинхронно."""

    def __init__(self, save: Path, remote_save: str = "", category: str = "tv"):
        self.save = save
        self.remote_save = remote_save or str(save)
        self.category = category
        self.names: dict[str, list[str]] = {}
        self.sizes: dict[str, dict[str, int]] = {}
        self.renamed: list[tuple[str, str]] = []
        self.lag = 0              # сколько опросов `files` отдавать старые имена
        self.toggled: list[tuple[str, str]] = []
        self.progress = 0.25

    def add(self, h: str, files: dict[str, int]) -> None:
        self.names[h] = list(files)
        self.sizes[h] = dict(files)

    def torrents(self):
        out = []
        for h, names in self.names.items():
            root = names[0].split("/")[0]
            content = f"{self.remote_save}/{root}" if "/" in names[0] else f"{self.remote_save}/{names[0]}"
            out.append({"hash": h, "name": root, "category": self.category,
                        "progress": self.progress, "seq_dl": False, "f_l_piece_prio": False,
                        "save_path": self.remote_save, "content_path": content})
        return out

    def files(self, h):
        if self.lag:
            self.lag -= 1
            return [{"name": "старое"}]
        return [{"name": n, "size": self.sizes[h].get(n, 1), "progress": 0.5, "index": i}
                for i, n in enumerate(self.names[h])]

    def rename_file(self, h, old, new):
        names = self.names[h]
        names[names.index(old)] = new
        self.sizes[h][new] = self.sizes[h].pop(old)
        self.renamed.append((old, new))
        src, dst = self.save / old, self.save / new
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)

    def set_file_priority(self, h, ids, priority):
        self.priorities = getattr(self, "priorities", []) + [(h, sorted(ids), priority)]

    def toggle_sequential(self, h):
        self.toggled.append(("seq", h))

    def toggle_first_last(self, h):
        self.toggled.append(("first_last", h))

    def categories(self):
        return {"tv": {"name": "tv", "savePath": self.remote_save}}

    def default_save_path(self):
        return self.remote_save


def _info():
    info = metadata.MediaInfo(kind=library.TV, tmdb_id=1, title="Шоу", title_en="Show",
                              original_title="Show", original_language="en", year=2026,
                              name_language=metaconf.NAME_EN)
    for n in (1, 2, 3):
        info.episodes[(1, n)] = metadata.EpisodeInfo(1, n, title=f"Э{n}", title_en=f"Ep {n}")
    return info


# ------------------------------------------------------------------ пути --

def test_detect_map_by_common_tail():
    m = qbit.detect_map(["/data/media/tv", "/data/torrents/games"],
                        ["/media/disk1/nas/media/tv"])
    assert m.remote == "/data" and Path(m.local) == Path("/media/disk1/nas")
    assert m.to_local("/data/media/tv/Show/E01.mkv") == Path("/media/disk1/nas/media/tv/Show/E01.mkv")
    assert m.to_local("/ssd-1/x.mkv") is None


def test_detect_map_same_paths_and_no_match():
    assert qbit.detect_map(["/srv/tv"], ["/srv/tv"]) == qbit.PathMap()
    assert qbit.PathMap().to_local("/srv/tv/a.mkv") == Path("/srv/tv/a.mkv")
    assert qbit.detect_map(["/data/downloads"], ["/media/tv"]) is None


@pytest.mark.skipif(os.name != "nt", reason="пути Windows")
def test_detect_map_windows_share():
    m = qbit.detect_map(["/data/media/tv"], ["Z:\\media\\tv"])
    assert m == qbit.PathMap("/data", "Z:\\")
    assert m.to_local("/data/media/tv/a.mkv") == Path("Z:/media/tv/a.mkv")


def test_resolve_map_prefers_manual_settings():
    c = FakeClient(Path("/x"), remote_save="/data/media/tv")
    s = qbit.Settings(url="http://q", remote_root="/dl", local_root="/mnt/dl")
    assert qbit.resolve_map(c, s, []) == qbit.PathMap("/dl", "/mnt/dl")
    with pytest.raises(qbit.QbitError):
        qbit.resolve_map(c, qbit.Settings(url="http://q"), ["/elsewhere/movies"])


# --------------------------------------------------------------- раздачи --

def test_find_torrents_inside_and_around_folder(tmp_path):
    c = FakeClient(tmp_path)
    c.add("a", {"Show.S01/Show.S01E01.mkv": 10})
    c.add("b", {"Other.S01/Other.S01E01.mkv": 10})
    found = qbit.find_torrents(tmp_path / "Show.S01", c, qbit.PathMap())
    assert [t.hash for t in found] == ["a"]
    # Указана папка внутри раздачи — раздача та же.
    assert [t.hash for t in qbit.find_torrents(tmp_path / "Show.S01" / "sub", c, qbit.PathMap())] == ["a"]
    # Указан корень — обе.
    assert len(qbit.find_torrents(tmp_path, c, qbit.PathMap())) == 2


# ------------------------------------------------------------ план с extra --

def _torrent(tmp_path, files, on_disk=()):
    c = FakeClient(tmp_path)
    c.add("h", files)
    for rel in on_disk:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return c, qbit.TorrentFiles(c, qbit.find_torrents(tmp_path / "Show.S01", c, qbit.PathMap()))


def test_scan_sees_files_not_yet_on_disk(tmp_path):
    c, tf = _torrent(tmp_path, {"Show.S01/Show.S01E01.mkv": 100, "Show.S01/Show.S01E02.mkv": 300},
                     on_disk=["Show.S01/Show.S01E01.mkv"])
    scan = library.scan_folder(tmp_path / "Show.S01", tf.extra())
    assert [v.name for v in scan.videos] == ["Show.S01E01.mkv", "Show.S01E02.mkv"]
    assert scan.size_of(tmp_path / "Show.S01" / "Show.S01E02.mkv") == 300
    assert scan.junk_files == []


def test_scan_hides_incomplete_qb_suffix(tmp_path):
    c, tf = _torrent(tmp_path, {"Show.S01/Show.S01E01.mkv": 100},
                     on_disk=["Show.S01/Show.S01E01.mkv.!qB"])
    scan = library.scan_folder(tmp_path / "Show.S01", tf.extra())
    assert [v.name for v in scan.videos] == ["Show.S01E01.mkv"]
    assert scan.junk_files == []


def test_scan_merges_unicode_forms(tmp_path):
    name = "Auflösung.S01E01.mkv"
    nfd = unicodedata.normalize("NFD", name)
    d = tmp_path / "Show.S01"
    d.mkdir()
    (d / nfd).write_bytes(b"x")
    extra = {d / unicodedata.normalize("NFC", name): 5}
    scan = library.scan_folder(d, extra)
    assert len(scan.videos) == 1


def test_plan_renames_missing_episodes(tmp_path):
    c, tf = _torrent(tmp_path, {"Show.S01/Show.S01E01.mkv": 1, "Show.S01/Show.S01E02.mkv": 1},
                     on_disk=["Show.S01/Show.S01E01.mkv"])
    s = metaconf.Settings(tv_roots=[str(tmp_path)])
    plan = library.build_tv_plan(tmp_path / "Show.S01", _info(), s, extra=tf.extra())
    dsts = sorted(r.dst.relative_to(tmp_path).as_posix() for r in plan.rows if r.changes)
    assert dsts == ["Show (2026)/Season 01/Show - S01E01 - Ep 1.mkv",
                    "Show (2026)/Season 01/Show - S01E02 - Ep 2.mkv"]


# ------------------------------------------------------------- применение --

def test_apply_renames_torrent_files_through_qbit(tmp_path):
    c, tf = _torrent(tmp_path, {"Show.S01/Show.S01E01.mkv": 1, "Show.S01/Show.S01E02.mkv": 1,
                                "Show.S01/RARBG.txt": 1},
                     on_disk=["Show.S01/Show.S01E01.mkv", "Show.S01/RARBG.txt"])
    # Свой .edl от прошлого прогона — не из раздачи, едет обычным переименованием.
    (tmp_path / "Show.S01" / "Show.S01E01.edl").write_text("1 2 3")
    s = metaconf.Settings(tv_roots=[str(tmp_path)])
    plan = library.build_tv_plan(tmp_path / "Show.S01", _info(), s, extra=tf.extra())
    results = library.apply_plan(plan, delete_junk=True, files=tf)
    assert all(r.ok for r in results), [r.error for r in results]
    assert sorted(new for _, new in c.renamed) == [
        "Show (2026)/Extras/RARBG.txt",
        "Show (2026)/Season 01/Show - S01E01 - Ep 1.mkv",
        "Show (2026)/Season 01/Show - S01E02 - Ep 2.mkv"]
    season = tmp_path / "Show (2026)" / "Season 01"
    assert (season / "Show - S01E01 - Ep 1.mkv").exists()
    assert (season / "Show - S01E01 - Ep 1.edl").exists()
    # Мусор раздачи не удалён, хотя галка стоит: qBittorrent скачал бы его заново.
    assert (tmp_path / "Show (2026)" / "Extras" / "RARBG.txt").exists()
    assert not (tmp_path / "Show.S01").exists()
    assert tf.owns(season / "Show - S01E02 - Ep 2.mkv")


def test_apply_reports_renames_qbit_did_not_finish(tmp_path, monkeypatch):
    c, tf = _torrent(tmp_path, {"Show.S01/Show.S01E01.mkv": 1})
    c.lag = 10 ** 6
    monkeypatch.setattr(qbit.time, "sleep", lambda _: None)
    s = metaconf.Settings(tv_roots=[str(tmp_path)])
    plan = library.build_tv_plan(tmp_path / "Show.S01", _info(), s, extra=tf.extra())
    real = tf.wait
    monkeypatch.setattr(tf, "wait", lambda timeout=60, stop=None: real(timeout=0, stop=stop))
    results = library.apply_plan(plan, files=tf)
    assert [r.ok for r in results] == [False]
    assert "не успел" in results[0].error


def test_apply_refuses_to_overwrite_existing_file(tmp_path):
    c, tf = _torrent(tmp_path, {"Show.S01/Show.S01E01.mkv": 1}, on_disk=["Show.S01/Show.S01E01.mkv"])
    s = metaconf.Settings(tv_roots=[str(tmp_path)])
    plan = library.build_tv_plan(tmp_path / "Show.S01", _info(), s, extra=tf.extra())
    taken = tmp_path / "Show (2026)" / "Season 01" / "Show - S01E01 - Ep 1.mkv"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"old")
    results = library.apply_plan(plan, files=tf)
    assert not results[0].ok and c.renamed == []
    assert taken.read_bytes() == b"old"


def test_rename_outside_save_path_is_refused(tmp_path):
    c, tf = _torrent(tmp_path, {"Show.S01/Show.S01E01.mkv": 1})
    with pytest.raises(qbit.QbitError):
        tf.rename(tmp_path / "Show.S01" / "Show.S01E01.mkv", tmp_path.parent / "elsewhere.mkv")


# ------------------------------------------------------------------ папки --

def test_make_dirs_copies_parent_mode(tmp_path):
    parent = tmp_path / "tv"
    parent.mkdir()
    os.chmod(parent, 0o777)
    qbit.make_dirs(parent / "Show (2026)" / "Season 01")
    made = parent / "Show (2026)" / "Season 01"
    assert made.is_dir()
    if os.name != "nt":
        assert made.stat().st_mode & 0o777 == 0o777


# ------------------------------------------------------------------- вход --

@pytest.mark.parametrize("reply,ok", [(b"Ok.", True), (b"", True), (b"Fails.", False)])
def test_login_accepts_old_and_new_replies(monkeypatch, reply, ok):
    # До 5.x успех — «Ok.», в 5.x — пустой ответ 204; отказ — «Fails.».
    c = qbit.Client("nas:8181", "user", "pass")
    assert c.base == "http://nas:8181"
    monkeypatch.setattr(c, "_send", lambda path, data=None, params=None: reply)
    if ok:
        c.login()
    else:
        with pytest.raises(qbit.QbitError, match="неверный логин"):
            c.login()


@pytest.mark.parametrize("reply,ok", [
    (b"Ok.", True), (b"", True), (b"Fails.", False),
    (b'{"added_torrent_ids":["b2e2"],"failure_count":0,"pending_count":0,"success_count":1}', True),
    (b'{"added_torrent_ids":[],"failure_count":1,"pending_count":0,"success_count":0}', False),
])
def test_add_accepts_old_and_new_replies(monkeypatch, reply, ok):
    # До 5.x — «Ok.» / «Fails.», в 5.x — JSON со счётчиками.
    c = qbit.Client("nas:8181")
    monkeypatch.setattr(c, "_send", lambda path, data=None, params=None: reply)
    if ok:
        c.add("magnet:?xt=urn:btih:" + "a" * 40, category="tv")
    else:
        with pytest.raises(qbit.QbitError, match="не добавил"):
            c.add("magnet:?xt=urn:btih:" + "a" * 40, category="tv")


def test_login_401_is_wrong_password(monkeypatch):
    import urllib.error
    c = qbit.Client("http://nas:8181", "user", "pass")

    def refuse(path, data=None, params=None):
        raise urllib.error.HTTPError(c.base, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(c, "_send", refuse)
    with pytest.raises(qbit.QbitError, match="неверный логин"):
        c.login()


# --------------------------------------------------------------- настройки --

def test_settings_roundtrip(tmp_path, monkeypatch):
    import paths
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    assert not qbit.load_settings().configured()
    qbit.save_settings(qbit.Settings(url="http://nas:8181", username="u", password=" p "))
    s = qbit.load_settings()
    assert s.configured() and s.password == " p "
