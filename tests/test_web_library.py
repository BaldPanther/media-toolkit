from pathlib import Path

import pytest

import artwork
import library
import metaconf
import metadata
import paths
from web import server
from web.state import AppState


def _info(name_language=metaconf.NAME_EN):
    info = metadata.MediaInfo(kind=library.TV, tmdb_id=10283, title="Арчер", title_en="Archer",
                              original_title="Archer", original_language="en", year=2009,
                              name_language=name_language)
    for n, (ru, en) in enumerate([("Охота на крота", "Mole Hunt"),
                                  ("Тренировочный день", "Training Day")], start=1):
        info.episodes[(1, n)] = metadata.EpisodeInfo(1, n, title=ru, title_en=en)
    info.art = [
        metadata.ArtCandidate(metadata.ART_POSTER, "https://x/p-en.jpg", "https://x/t-en.jpg", "en", 1000, 1500),
        metadata.ArtCandidate(metadata.ART_POSTER, "https://x/p-ru.jpg", "https://x/t-ru.jpg", "ru", 1000, 1500),
        metadata.ArtCandidate(metadata.ART_SEASON, "https://x/s1.jpg", "https://x/s1t.jpg", "en", 1000, 1500, season=1),
    ]
    return info


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    tv = tmp_path / "tv"
    raw = tv / "Archer.S01.1080p.BluRay-GRP"
    raw.mkdir(parents=True)
    for n in (1, 2):
        (raw / f"Archer.S01E0{n}.1080p.BluRay-GRP.mkv").write_bytes(b"x")
    (raw / "RARBG.txt").write_text("junk")
    metaconf.save_settings(metaconf.Settings(tmdb_key="k", tv_roots=[str(tv)],
                                             art_languages=["ru", "en", ""]))
    hits = [metadata.SearchHit(library.TV, 10283, "Archer", 2009)]
    monkeypatch.setattr(metadata, "search", lambda kind, q, s, year=None: list(hits))
    monkeypatch.setattr(metadata, "fetch", lambda *a, **kw: _info())

    def fake_download(tasks, stop=None, progress=None):
        for t in tasks:
            t.dest.write_bytes(b"img")
        return [artwork.ArtResult(t, True) for t in tasks]

    monkeypatch.setattr(artwork, "download", fake_download)
    st = AppState("desktop")
    app = server.create_app(st)
    app.testing = True
    return st, app.test_client(), raw, hits


def _tab(c):
    return c.get("/api/state").get_json()["tabs"]["library"]


def test_path_change_guesses_title(env):
    st, c, raw, _ = env
    c.post("/api/path", json={"path": str(raw)})
    t = _tab(c)
    assert (t["query"], t["year"]) == ("Archer", "")


def test_find_single_hit_loads_info_and_plan(env):
    st, c, raw, _ = env
    c.post("/api/path", json={"path": str(raw)})
    res = c.post("/api/library/find", json={}).get_json()
    assert res["job"]["title"] == "Поиск в TMDb"
    assert st.jobs.wait()
    assert st.jobs.last.status == "done", st.jobs.last.error
    t = _tab(c)
    assert t["hit"]["tmdb_id"] == 10283
    assert t["info"]["folder"] == "Archer (2009)"
    # Лучший постер — русский (он первый в приоритете языков).
    poster = next(a for a in t["art"] if a["kind"] == metadata.ART_POSTER)
    assert poster["candidate"]["lang"] == "ru"
    nexts = [r["next"] for r in t["rows"] if r["i"] is not None]
    assert "Archer (2009)/Season 01/Archer - S01E01 - Mole Hunt.mkv" in nexts
    junk = next(r for r in t["rows"] if r["junk"])
    assert junk["sel"] is True
    assert any(r.get("write") and r["next"].endswith("tvshow.nfo") for r in t["rows"])


def test_art_language_choice_and_manual_pick(env):
    st, c, raw, _ = env
    c.post("/api/path", json={"path": str(raw)})
    c.post("/api/library/find", json={})
    st.jobs.wait()
    c.post("/api/library/art_language", json={"choice": "en"})
    poster = next(a for a in _tab(c)["art"] if a["kind"] == metadata.ART_POSTER)
    assert poster["candidate"]["lang"] == "en"
    opts = c.post("/api/library/art/options", json={"kind": metadata.ART_POSTER}).get_json()
    ru = next(i for i in opts["items"] if i["lang"] == "ru")
    c.post("/api/library/art/choose", json={"kind": metadata.ART_POSTER, "index": ru["i"]})
    poster = next(a for a in _tab(c)["art"] if a["kind"] == metadata.ART_POSTER)
    assert poster["candidate"]["lang"] == "ru"


def test_name_language_override_is_saved(env):
    st, c, raw, _ = env
    c.post("/api/path", json={"path": str(raw)})
    c.post("/api/library/find", json={})
    st.jobs.wait()
    c.post("/api/library/name_language", json={"choice": "local"})
    assert metaconf.load_settings().name_overrides == {"tv:10283": metaconf.NAME_LOCAL}
    t = _tab(c)
    assert t["name_choice"] == "local"
    assert t["info"]["folder"] == "Арчер (2009)"


def test_apply_renames_writes_nfo_and_switches_path(env, monkeypatch):
    st, c, raw, _ = env
    import core
    monkeypatch.setattr(core, "scan_folder", lambda folder, recursive, **kw: [])
    c.post("/api/path", json={"path": str(raw)})
    c.post("/api/library/find", json={})
    st.jobs.wait()
    junk = next(r for r in _tab(c)["rows"] if r["junk"])
    c.post("/api/library/junk", json={"index": junk["i"]})      # мусор не трогаем
    ask = c.post("/api/library/apply", json={}).get_json()
    assert ask["ask"]["id"] == "apply"
    c.post("/api/library/apply", json={"answers": {"apply": True}})
    assert st.jobs.wait()
    assert st.jobs.last.status == "done", st.jobs.last.error
    root = raw.parent / "Archer (2009)"
    assert (root / "Season 01" / "Archer - S01E01 - Mole Hunt.mkv").exists()
    assert (root / "Season 01" / "Archer - S01E02 - Training Day.nfo").exists()
    assert (root / "tvshow.nfo").exists()
    assert (root / "poster.jpg").exists()
    assert st.path == str(root)
    assert _tab(c)["hit"]["tmdb_id"] == 10283               # тайтл не сброшен сменой пути
    assert "переименовано 2" in st.jobs.last.summary


def test_policy_ask_asks_second_question(env, monkeypatch):
    st, c, raw, _ = env
    import core
    monkeypatch.setattr(core, "scan_folder", lambda folder, recursive, **kw: [])
    c.post("/api/path", json={"path": str(raw)})
    c.post("/api/library/find", json={})
    st.jobs.wait()
    c.post("/api/library/options", json={"policy": metaconf.POLICY_ASK})
    res = c.post("/api/library/apply", json={"answers": {"apply": True}}).get_json()
    assert res["ask"]["id"] == "policy"
    assert [b["value"] for b in res["ask"]["buttons"]] == [None, "missing_only", "overwrite"]


def test_episode_override_rebuilds_plan(env, tmp_path):
    st, c, raw, _ = env
    (raw / "Archer.S01E02.1080p.BluRay-GRP.mkv").rename(raw / "Archer - bonus.mkv")
    c.post("/api/path", json={"path": str(raw)})
    c.post("/api/library/find", json={})
    st.jobs.wait()
    row = next(r for r in _tab(c)["rows"] if r["video"] and "bonus" in r["now"])
    assert "нет номера" in row["status"]
    c.post("/api/library/episode", json={"index": row["i"], "season": "1", "episode": "2"})
    nexts = [r["next"] for r in _tab(c)["rows"]]
    assert "Archer (2009)/Season 01/Archer - S01E02 - Training Day.mkv" in nexts


def test_pending_take_and_ignore(env):
    st, c, raw, _ = env
    c.post("/api/library/pending", json={})
    assert st.jobs.wait()
    t = _tab(c)
    assert [p["name"] for p in t["pending"]] == [raw.name]
    c.post("/api/library/pending/take", json={"path": str(raw)})
    assert st.path == str(raw)
    assert _tab(c)["kind_choice"] == "tv"
    c.post("/api/library/pending/ignore", json={"path": str(raw)})
    assert _tab(c)["pending"] == []
    assert library.is_ignored(raw)


def test_system_junk_find_then_delete(env, monkeypatch):
    st, c, raw, _ = env
    ds = raw / ".DS_Store"
    ds.write_bytes(b"x")
    monkeypatch.setattr(library, "send_to_trash_available", lambda: False)
    c.post("/api/library/system-junk", json={})
    assert st.jobs.wait()
    assert _tab(c)["system_junk"] == 1
    ask = c.post("/api/library/system-junk/delete", json={}).get_json()
    assert ask["ask"]["id"] == "delete"
    assert ".DS_Store — 1" in ask["ask"]["text"]
    c.post("/api/library/system-junk/delete", json={"answers": {"delete": True}})
    assert st.jobs.wait()
    assert not ds.exists()


def test_find_without_key(env):
    st, c, raw, _ = env
    metaconf.save_settings(metaconf.Settings())
    st.tabs["library"].on_settings_changed()
    c.post("/api/path", json={"path": str(raw)})
    res = c.post("/api/library/find", json={})
    assert res.status_code == 400
    assert res.get_json()["error"]["title"] == "Нет ключа TMDb"
