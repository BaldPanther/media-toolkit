import pytest

import metaconf
import paths
import subs
from web import server
from web.state import AppState


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Настройки — во временной папке, настоящие не трогаем."""
    d = tmp_path / "cfg"
    monkeypatch.setattr(paths, "config_dir", lambda: d)
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    monkeypatch.delenv("MEDIA_ROOT", raising=False)
    return d


@pytest.fixture
def state(cfg):
    return AppState("desktop")


@pytest.fixture
def client(state):
    app = server.create_app(state)
    app.testing = True
    return app.test_client()


def test_index_and_info(client):
    assert client.get("/").status_code == 200
    info = client.get("/api/info").get_json()
    assert info["app"] == "media-toolkit"
    assert info["mode"] == "desktop"
    assert info["auth"] is False


def test_status_returns_new_log_lines_only(client, state):
    state.log.add("первая")
    state.log.add("вторая")
    data = client.get("/api/status?log=0").get_json()
    assert [x["text"] for x in data["log"]] == ["первая", "вторая"]
    assert data["busy"] is False
    last = data["log"][-1]["seq"]
    assert client.get(f"/api/status?log={last}").get_json()["log"] == []


def test_settings_roundtrip_keeps_unsent_fields(client, cfg):
    s = metaconf.Settings(tmdb_key="old", omdb_key="omdb", movies_roots=["/m"])
    metaconf.save_settings(s)
    res = client.post("/api/settings", json={"meta": {
        "tmdb_key": "  new-key ", "art_languages": ["en", "orig", "", "en"],
        "tv_roots": ["/tv", "", "/tv"], "name_language": "нет такого"}})
    assert res.status_code == 200
    saved = metaconf.load_settings()
    assert saved.tmdb_key == "new-key"
    assert saved.omdb_key == "omdb"                 # не присылали — осталось
    assert saved.movies_roots == ["/m"]
    assert saved.tv_roots == ["/tv"]                 # пустые и повторы выкинуты
    assert saved.art_languages == ["en", "orig", ""]
    assert saved.name_language == metaconf.NAME_AUTO  # неизвестный режим не принят
    got = client.get("/api/settings").get_json()
    assert got["meta"]["tmdb_key"] == "new-key"
    assert "name_modes" in got["choices"]


def test_settings_subs_section(client, cfg):
    subs.save_settings(subs.Settings("user", "secret", "key", True))
    client.post("/api/settings", json={"subs": {"username": " u2 ", "use_fallback": False}})
    s = subs.load_settings()
    assert (s.username, s.password, s.apikey, s.use_fallback) == ("u2", "secret", "key", False)
    # Файл настроек скрапера при этом не создан и не тронут.
    assert not (cfg / "meta_settings.json").exists()


def test_fs_lists_dirs_and_videos(client, tmp_path):
    root = tmp_path / "lib"
    (root / "Show (2020)").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "movie.mkv").write_bytes(b"")
    (root / "notes.txt").write_text("x")
    dirs = client.get(f"/api/fs?path={root}").get_json()
    assert [e["name"] for e in dirs["entries"]] == ["Show (2020)"]
    assert dirs["parent"] == str(tmp_path)
    files = client.get(f"/api/fs?path={root}&files=1").get_json()
    assert [e["name"] for e in files["entries"]] == ["Show (2020)", "movie.mkv"]
    missing = client.get(f"/api/fs?path={root / 'нет'}")
    assert missing.status_code == 400
    assert missing.get_json()["error"]["title"] == "Нет такой папки"


def test_fs_roots_in_container_are_only_media(client, tmp_path, monkeypatch):
    media = tmp_path / "media"
    (media / "tv").mkdir(parents=True)
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    roots = client.get("/api/fs").get_json()["roots"]
    assert [r["path"] for r in roots] == [str(media)]
    # Выше медиатеки не подняться: у неё самой нет «родителя», соседи недоступны.
    assert client.get(f"/api/fs?path={media}").get_json()["parent"] == ""
    assert client.get(f"/api/fs?path={media / 'tv'}").get_json()["parent"] == str(media)
    outside = client.get(f"/api/fs?path={tmp_path}")
    assert outside.status_code == 400
    assert outside.get_json()["error"]["title"] == "Вне медиатеки"


def test_path_change_and_busy_refusal(client, state, tmp_path):
    data = client.post("/api/path", json={"path": str(tmp_path), "recursive": False}).get_json()
    assert data["workspace"]["path"] == str(tmp_path)
    assert data["workspace"]["recursive"] is False
    import threading
    gate = threading.Event()
    state.jobs.start("Долгая", lambda job: gate.wait(5))
    res = client.post("/api/path", json={"path": "/"})
    assert res.status_code == 409
    assert "Долгая" in res.get_json()["error"]["text"]
    gate.set()
    state.jobs.wait()


def test_password_guards_api(state, cfg):
    app = server.create_app(state, password="пароль")
    app.testing = True
    c = app.test_client()
    assert c.get("/api/state").status_code == 401
    assert c.get("/").status_code == 302
    assert c.get("/api/info").status_code == 200      # открыт: странице входа нужен
    assert c.post("/api/login", json={"password": "не тот"}).status_code == 403
    assert c.post("/api/login", json={"password": "пароль"}).status_code == 200
    assert c.get("/api/state").status_code == 200
    # Ключ подписи входа сохранён — вход переживёт перезапуск.
    assert (cfg / "web-secret.key").exists()


def test_ask_reply_roundtrip(state):
    """Вопрос → повтор запроса с ответом → проход дальше."""
    from web import replies
    app = server.create_app(state)

    @app.post("/api/test-ask")
    def ask():
        replies.confirm("go", "Продолжить?", "Точно?", yes="Да")
        return {"ok": True}

    c = app.test_client()
    first = c.post("/api/test-ask", json={}).get_json()
    assert first["ask"]["id"] == "go"
    assert [b["value"] for b in first["ask"]["buttons"]] == [None, True]
    second = c.post("/api/test-ask", json={"answers": {"go": True}}).get_json()
    assert second == {"ok": True}
