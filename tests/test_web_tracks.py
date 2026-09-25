from pathlib import Path

import pytest

import core
import paths
from web import server
from web.state import AppState


def _track(i, kind, lang, name, default=False):
    return core.Track(id=i, uid=100 + i, number=i + 1, type=kind, codec="x", language=lang,
                      name=name, default=default, forced=False)


def _file(name, audio_default="Original"):
    tracks = [_track(0, "video", "und", ""),
              _track(1, "audio", "rus", "LostFilm", audio_default == "LostFilm"),
              _track(2, "audio", "eng", "Original", audio_default == "Original"),
              _track(3, "subtitles", "rus", "Полные")]
    return core.MkvFile(Path("/lib/Show") / name, tracks, duration=1500.0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    st = AppState("desktop")
    app = server.create_app(st)
    app.testing = True
    st.path = "/lib/Show"
    return st, app.test_client()


def _tab(client):
    return client.get("/api/state").get_json()["tabs"]["tracks"]


def test_scan_result_fills_options_and_rows(env):
    st, c = env
    st.apply_scan([_file("S01E01.mkv"), _file("S01E02.mkv", "LostFilm")])
    t = _tab(c)
    assert [o["label"] for o in t["audio_options"]] == ["LostFilm [rus] — 2/2", "Original [eng] — 2/2"]
    assert t["audio_choice"] == ""
    assert [r["kind"] for r in t["rows"]] == ["nochange", "nochange"]


def test_choice_changes_preview(env):
    st, c = env
    st.apply_scan([_file("S01E01.mkv"), _file("S01E02.mkv", "LostFilm")])
    lostfilm = _tab(c)["audio_options"][0]["id"]
    c.post("/api/tracks/choice", json={"audio": lostfilm, "sub": "off"})
    t = _tab(c)
    assert t["audio_choice"] == lostfilm
    assert t["sub_choice"] == "off"
    assert [r["anew"] for r in t["rows"]] == ["LostFilm", "LostFilm"]
    assert [r["kind"] for r in t["rows"]] == ["change", "nochange"]
    assert t["n_change"] == 1


def test_apply_asks_then_runs_and_rescans(env, monkeypatch):
    st, c = env
    files = [_file("S01E01.mkv"), _file("S01E02.mkv", "LostFilm")]
    st.apply_scan(files)
    monkeypatch.setattr(core, "find_tools", lambda: ("mkvmerge", "mkvpropedit"))
    applied = []

    def fake_apply(propedit, plan):
        applied.append(plan.file.path.name)
        return core.ApplyResult(plan=plan, ok=True)

    monkeypatch.setattr(core, "apply_plan", fake_apply)
    rescanned = [_file("S01E01.mkv", "LostFilm"), _file("S01E02.mkv", "LostFilm")]
    monkeypatch.setattr(core, "scan_folder", lambda folder, recursive, **kw: rescanned)

    lostfilm = _tab(c)["audio_options"][0]["id"]
    c.post("/api/tracks/choice", json={"audio": lostfilm, "sub": ""})
    ask = c.post("/api/tracks/apply", json={}).get_json()
    assert ask["ask"]["id"] == "apply"
    assert "Будет изменено файлов: 1" in ask["ask"]["text"]
    assert applied == []                                  # без ответа ничего не тронуто
    run = c.post("/api/tracks/apply", json={"answers": {"apply": True}}).get_json()
    assert run["job"]["title"] == "Применение дорожек"
    assert st.jobs.wait()
    assert applied == ["S01E01.mkv"]
    t = _tab(c)
    assert t["audio_choice"] == lostfilm                  # выбор пережил пересканирование
    assert [r["kind"] for r in t["rows"]] == ["nochange", "nochange"]
    assert st.jobs.last.summary == "Готово: успешно 1/1."


def test_apply_without_choice_is_a_message(env):
    st, c = env
    st.apply_scan([_file("S01E01.mkv")])
    res = c.post("/api/tracks/apply", json={}).get_json()
    assert res["message"]["title"] == "Ничего не выбрано"


def test_apply_without_scan(env):
    _, c = env
    assert c.post("/api/tracks/apply", json={}).get_json()["message"]["title"] == "Нет данных"


def test_subs_download_asks_and_reports(env, monkeypatch):
    import subs
    st, c = env
    st.apply_scan([_file("S01E01.mkv"), _file("S01E02.mkv")])
    monkeypatch.setattr(subs, "subliminal_available", lambda: "")
    monkeypatch.setattr(subs, "load_settings", lambda: subs.Settings("u", "p", "k", False))
    monkeypatch.setattr(subs, "has_external_ru", lambda p: p.name == "S01E02.mkv")
    got = {}

    def fake_download(targets, settings, progress=None, stop=None):
        got["targets"] = [p.name for p in targets]
        return [subs.SubResult(p, "downloaded", provider="opensubtitlescom") for p in targets]

    monkeypatch.setattr(subs, "download", fake_download)
    # Во всех файлах есть встроенные русские — качать нечего.
    assert c.post("/api/tracks/subs", json={}).get_json()["message"]["title"] == "Нечего качать"
    # А без галки «только где нет» — берутся все.
    ask = c.post("/api/tracks/subs", json={"only_missing": False}).get_json()
    assert "для 2 серий" in ask["ask"]["text"]
    c.post("/api/tracks/subs", json={"only_missing": False, "answers": {"go": True}})
    assert st.jobs.wait()
    assert got["targets"] == ["S01E01.mkv", "S01E02.mkv"]
    assert _tab(c)["subs_status"] == "Субтитры: скачано 2, не найдено 0, ошибок 0."


def test_apply_only_selected_files(env, monkeypatch):
    st, c = env
    files = [_file("S01E01.mkv"), _file("S01E02.mkv"), _file("S01E03.mkv")]
    st.apply_scan(files)
    monkeypatch.setattr(core, "find_tools", lambda: ("mkvmerge", "mkvpropedit"))
    applied = []
    monkeypatch.setattr(core, "apply_plan", lambda pe, plan: (applied.append(plan.file.path.name),
                                                             core.ApplyResult(plan=plan, ok=True))[1])
    monkeypatch.setattr(core, "scan_folder", lambda folder, recursive, **kw: files)
    lostfilm = _tab(c)["audio_options"][0]["id"]
    c.post("/api/tracks/choice", json={"audio": lostfilm, "sub": ""})
    body = {"scope": "sel", "selected": [str(files[1].path)]}
    ask = c.post("/api/tracks/apply", json=body).get_json()
    assert "Будет изменено файлов: 1 — по выделенным файлам." in ask["ask"]["text"]
    c.post("/api/tracks/apply", json={**body, "answers": {"apply": True}})
    assert st.jobs.wait()
    assert applied == ["S01E02.mkv"]
    empty = c.post("/api/tracks/apply", json={"scope": "sel", "selected": []}).get_json()
    assert empty["message"]["title"] == "Нет выделения"


def test_subs_only_selected_files(env, monkeypatch):
    import subs
    st, c = env
    st.apply_scan([_file("S01E01.mkv"), _file("S01E02.mkv")])
    monkeypatch.setattr(subs, "subliminal_available", lambda: "")
    monkeypatch.setattr(subs, "load_settings", lambda: subs.Settings("u", "p", "k", False))
    ask = c.post("/api/tracks/subs", json={"only_missing": False, "scope": "sel",
                                           "selected": ["/lib/Show/S01E02.mkv"]}).get_json()
    assert "для 1 серий — по выделенным файлам?" in ask["ask"]["text"]
