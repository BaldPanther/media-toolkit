from pathlib import Path

import pytest

import core
import edl
import paths
import pipeline
from web import server
from web.state import AppState, load_app_settings


def _file(folder: Path, name: str, duration=1400.0, tail=None):
    path = folder / name
    path.write_bytes(b"x")
    tracks = [core.Track(0, 1, 1, "video", "h264", "und", "", True, False, duration=duration),
              core.Track(1, 2, 2, "audio", "aac", "jpn", "Original", True, False,
                         duration=duration + tail if tail else None)]
    return core.MkvFile(path, tracks, duration=duration)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    show = tmp_path / "Show (2020)" / "Season 01"
    show.mkdir(parents=True)
    files = [_file(show, f"Show - S01E0{n} - Ep.mkv") for n in (1, 2, 3)]
    st = AppState("desktop")
    app = server.create_app(st)
    app.testing = True
    st.path = str(show.parent)
    return st, app.test_client(), show, files


def _tab(c):
    return c.get("/api/state").get_json()["tabs"]["edl"]


def _ep(st, n):
    return st.tabs["edl"].eps[n]


def test_scan_reads_existing_edl(env):
    st, c, show, files = env
    edl.write_edl(files[1].path, edl.Segment(90, 180), edl.Segment(1300, 1350))
    st.apply_scan(files)
    t = _tab(c)
    assert [r["se"] for r in t["rows"]] == ["S01E01", "S01E02", "S01E03"]
    row = t["rows"][1]
    assert row["intro"] == "1:30–3:00" and row["edl"] is True and row["note"] == "из .edl"
    # Титры не доходят до конца файла — значит, конец задан руками.
    assert _ep(st, 1).outro_fixed_end is True
    assert t["outro_end_active"] is True


def test_manual_ops_follow_scope(env):
    st, c, show, files = env
    st.apply_scan(files)
    sel = [str(files[2].path)]
    c.post("/api/edl/manual", json={"op": "intro", "values": {"intro_start": "1:30", "intro_end": "3:00"},
                                    "scope": "sel", "selected": sel})
    assert [e.intro for e in st.tabs["edl"].eps] == [None, None, edl.Segment(90, 180)]
    c.post("/api/edl/manual", json={"op": "outro_last", "values": {"outro_last": "60"}, "scope": "all"})
    assert all(e.outro == edl.Segment(1340, 1400) for e in st.tabs["edl"].eps)
    c.post("/api/edl/manual", json={"op": "intro_dur", "values": {"intro_dur": "30"}, "scope": "all"})
    assert _ep(st, 2).intro == edl.Segment(90, 120)
    res = c.post("/api/edl/manual", json={"op": "recap", "values": {}, "scope": "sel", "selected": []})
    assert res.get_json()["message"]["title"] == "Нет выделения"
    ask = c.post("/api/edl/manual", json={"op": "clear_all", "scope": "all"}).get_json()
    assert ask["ask"]["id"] == "clear"
    c.post("/api/edl/manual", json={"op": "clear_all", "scope": "all", "answers": {"clear": True}})
    assert all(e.intro is None and e.outro is None for e in st.tabs["edl"].eps)


def test_calc_outro_last_uses_selected_episode(env):
    st, c, show, files = env
    files[1] = _file(show, "Show - S01E02 - Ep.mkv", duration=1500.0)
    st.apply_scan(files)
    res = c.post("/api/edl/manual", json={"op": "calc_outro_last",
                                          "values": {"outro_from_start": "24:00"},
                                          "selected": [str(files[1].path)]}).get_json()
    assert res["outro_last"] == "60"
    assert "25:00 − 24:00" in res["calc"]


def test_episode_edit_semantics(env):
    st, c, show, files = env
    st.apply_scan(files)
    path = str(files[0].path)
    got = c.post("/api/edl/episode/get", json={"path": path}).get_json()
    assert got["is"] == "" and got["duration"] == 1400.0
    # Конец интро пуст — берётся «начало + длительность» из «Задать вручную».
    c.post("/api/edl/episode", json={"path": path, "is": "1:00", "ie": "", "os": "22:00",
                                     "oe": "23:00", "rc": "0:10", "intro_dur": "45"})
    ep = _ep(st, 0)
    assert ep.intro == edl.Segment(60, 105)
    assert ep.outro == edl.Segment(1320, 1380) and ep.outro_fixed_end is True
    assert ep.recap == edl.Segment(0, 10)
    c.post("/api/edl/episode", json={"path": path, "is": "1:00", "ie": "1:30", "os": "22:00", "oe": ""})
    assert ep.outro == edl.Segment(1320, 1400) and ep.outro_fixed_end is False


def test_settings_persist(env):
    st, c, show, files = env
    c.post("/api/edl/settings", json={"keep_first": False, "pad": {"intro_end": "-2"},
                                      "window": {"outro": "300"}})
    saved = load_app_settings()["edl"]
    assert saved["keep_first"] is False
    assert saved["pad"]["intro_end"] == "-2"
    assert saved["window"]["outro"] == "300"
    fresh = AppState("desktop")
    server.create_app(fresh)
    assert fresh.tabs["edl"].pad["intro_end"] == "-2"


def test_sort_toggles_direction(env):
    st, c, show, files = env
    st.apply_scan(files)
    c.post("/api/edl/sort", json={"col": "se"})
    assert _tab(c)["sort"] == ["se", False]
    c.post("/api/edl/sort", json={"col": "se"})
    t = _tab(c)
    assert t["sort"] == ["se", True]
    assert [r["se"] for r in t["rows"]] == ["S01E03", "S01E02", "S01E01"]


def test_detect_runs_by_season_and_snapshots(env, monkeypatch):
    st, c, show, files = env
    st.apply_scan(files)
    monkeypatch.setattr(edl, "missing_tools", lambda: [])
    monkeypatch.setattr(edl, "find_fpcalc", lambda: "fpcalc")
    monkeypatch.setattr(edl, "find_ffmpeg", lambda: "ffmpeg")
    seen = {}

    def fake_detect(eps, fpcalc, ffmpeg, prefer_lang=None, progress=None, stop=None, **kw):
        seen["n"] = len(eps)
        for e in eps:
            e.intro = edl.Segment(60, 150)
            progress("intro", 1, len(eps), e.path)

    monkeypatch.setattr(edl, "detect_season", fake_detect)
    res = c.post("/api/edl/detect", json={"scope": "sel", "selected": [str(files[0].path)]}).get_json()
    assert res["message"]["title"] == "Мало серий"
    c.post("/api/edl/detect", json={"scope": "all"})
    assert st.jobs.wait()
    assert st.jobs.last.status == "done", st.jobs.last.error
    assert seen["n"] == 3
    assert "интро 3/3" in st.jobs.last.summary
    assert _ep(st, 0).local_intro == edl.Segment(60, 150)


def test_write_runs_pipeline_per_episode(env, monkeypatch):
    st, c, show, files = env
    st.apply_scan(files)
    c.post("/api/edl/manual", json={"op": "outro_last", "values": {"outro_last": "60"}, "scope": "all"})
    calls = []

    def fake_run(ep, pad, keep, to_end, **kw):
        calls.append((ep.path.name, kw["tools"], kw["propedit"]))
        return pipeline.EpisodeRun(edl=edl.edl_path(ep.path))

    monkeypatch.setattr(pipeline, "run_episode", fake_run)
    ask = c.post("/api/edl/write", json={"scope": "all"}).get_json()
    assert "Записать 3 файлов .edl" in ask["ask"]["text"]
    c.post("/api/edl/write", json={"scope": "all", "answers": {"go": True}})
    assert st.jobs.wait()
    assert st.jobs.last.status == "done", st.jobs.last.error
    assert [x[0] for x in calls] == [f.path.name for f in files]
    assert all(x[1] is None and x[2] is None for x in calls)     # хвоста нет, главы не просили
    assert st.jobs.last.summary == "Готово: .edl записано 3"


def test_write_asks_about_tail_trim(env, monkeypatch):
    st, c, show, files = env
    files[0] = _file(show, "Show - S01E01 - Ep.mkv", tail=6.0)
    st.apply_scan(files)
    assert _tab(c)["rows"][0]["tail"] == "6 с"
    import trim
    monkeypatch.setattr(trim, "find_tools", lambda: (object(), []))
    c.post("/api/edl/manual", json={"op": "outro_last", "values": {"outro_last": "60"}, "scope": "all"})
    ask = c.post("/api/edl/write", json={"scope": "all"}).get_json()
    assert "звук идёт дальше картинки (до 6 с)" in ask["ask"]["text"]


def test_delete_edl(env):
    st, c, show, files = env
    edl.write_edl(files[0].path, edl.Segment(1, 20), None)
    st.apply_scan(files)
    ask = c.post("/api/edl/delete", json={"scope": "all"}).get_json()
    assert "Удалить 1 файлов .edl" in ask["ask"]["text"]
    c.post("/api/edl/delete", json={"scope": "all", "answers": {"go": True}})
    assert not edl.has_external_edl(files[0].path)


def test_frames_and_shift(env, monkeypatch):
    st, c, show, files = env
    st.apply_scan(files)
    monkeypatch.setattr(edl, "find_ffmpeg", lambda: "ffmpeg")

    def fake_grab(ffmpeg, path, times, on_frame=None, stop=None, **kw):
        for i in range(len(times)):
            on_frame(i, b"\x89PNG-fake" if i != 2 else None)

    monkeypatch.setattr(edl, "grab_frames", fake_grab)
    res = c.post("/api/edl/frames", json={"path": str(files[0].path), "center": 100, "step": 1}).get_json()
    assert len(res["times"]) == edl.FRAME_COUNT
    import time
    for _ in range(50):
        status = c.get(f"/api/edl/frames/{res['token']}").get_json()
        if status["done"]:
            break
        time.sleep(0.02)
    assert status["ready"]["2"] is False and status["ready"]["0"] is True
    png = c.get(f"/api/edl/frames/{res['token']}/0.png")
    assert png.status_code == 200 and png.data.startswith(b"\x89PNG")
    shift = c.post("/api/edl/shift", json={"bound": "ie", "delta": 3, "path": str(files[0].path)}).get_json()
    assert shift["pad"] == "3"
    assert st.tabs["edl"].pad["intro_end"] == "3"
