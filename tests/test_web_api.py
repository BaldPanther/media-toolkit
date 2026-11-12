import os
import socket

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


def test_torrents_query_from_folder_at_start(cfg, tmp_path):
    # Программа открылась с прежней папкой — название уже в поиске «Торрентов», без «Сканировать».
    folder = tmp_path / "Altered Carbon (2018)"
    folder.mkdir()
    app = server.create_app(AppState("desktop", str(folder)))
    app.testing = True
    s = app.test_client().get("/api/state").get_json()["tabs"]["torrents"]["search"]
    assert s["query"] == s["folder_query"] == "Altered Carbon (2018)"


def test_settings_jacred_key(client, cfg):
    import search
    got = client.get("/api/settings").get_json()
    assert got["search"]["jacred_key"] == "" and got["key_urls"]["jacred"] == search.JACRED_KEY_URL
    client.post("/api/settings", json={"search": {"voices": [], "jacred_key": " abc "}})
    assert search.jacred_key() == "abc"
    got = client.get("/api/settings").get_json()
    assert got["search"]["jacred_key"] == "abc"
    assert all("abc" not in s["label"] for s in got["search"]["sources"])   # ключ в списке не светится


def test_settings_qbit_section_and_check(client, cfg, monkeypatch):
    import qbit
    qbit.save_settings(qbit.Settings(url="http://old", password="secret"))
    client.post("/api/settings", json={"qbit": {"url": " http://nas:8181 ", "username": "cat"}})
    s = qbit.load_settings()
    assert (s.url, s.username, s.password) == ("http://nas:8181", "cat", "secret")
    assert client.get("/api/settings").get_json()["qbit"]["url"] == "http://nas:8181"

    seen = {}

    def fake_check(settings, roots):
        seen.update(url=settings.url, roots=roots)
        return "qBittorrent v5.3.0"

    monkeypatch.setattr(qbit, "check", fake_check)
    res = client.post("/api/qbit/check", json={"qbit": {"url": "http://form"},
                                               "roots": ["/tv", ""]}).get_json()
    assert res == {"ok": True, "text": "qBittorrent v5.3.0"}
    assert seen == {"url": "http://form", "roots": ["/tv"]}     # из формы, не сохранённое

    def failing(settings, roots):
        raise qbit.QbitError("неверный логин или пароль")

    monkeypatch.setattr(qbit, "check", failing)
    res = client.post("/api/qbit/check", json={"qbit": {}}).get_json()
    assert res == {"ok": False, "text": "неверный логин или пароль"}


def test_settings_telegram_kodi_and_auto(client, cfg):
    import kodi
    import qbit
    import telegram
    telegram.save_settings(telegram.Settings(token="old", chat_id=42))
    client.post("/api/settings", json={"telegram": {"token": "old"},
                                       "qbit": {"auto": True, "tv_category": " TV "},
                                       "kodi": {"url": " http://tv:8080 "}})
    assert telegram.load_settings().chat_id == 42          # тот же бот — чат остаётся
    q = qbit.load_settings()
    assert q.auto and q.tv_category == "TV" and q.movies_category == "movies"
    assert kodi.load_settings().url == "http://tv:8080"
    got = client.get("/api/settings").get_json()
    assert got["telegram"] == {"token": "old", "bound": True}
    # Другой токен — другой бот: привязку чата начинаем заново.
    client.post("/api/settings", json={"telegram": {"token": "new"}})
    assert telegram.load_settings().chat_id == 0
    res = client.post("/api/telegram/test", json={}).get_json()
    assert res["ok"] is False and "/start" in res["text"]


def test_torrents_tab_state_and_actions(client, cfg):
    t = client.get("/api/state").get_json()["tabs"]["torrents"]
    assert t["qbit"] is False and t["auto"] is False and t["items"] == []
    res = client.post("/api/torrents/answer", json={"hash": "нет", "index": 0})
    assert res.status_code == 400 and res.get_json()["error"]["title"] == "Нет такой раздачи"
    assert client.post("/api/torrents/check", json={}).get_json()["ok"] is True


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


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM — сигнал POSIX")
def test_server_mode_exits_on_sigterm(tmp_path):
    # В Docker программа — процесс 1: без своего обработчика SIGTERM ядро его
    # игнорирует, и docker stop (и автообновление) ждёт таймаута, а потом убивает.
    import signal
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen([sys.executable, "app.py", "--server", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=root, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("сервер не поднялся")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_settings_search_voices(client, cfg):
    import search
    got = client.get("/api/settings").get_json()["search"]
    assert got["voices"] == ["Кубик в Кубе", "TVShows"]
    assert got["sources"] == [{"url": "https://jac.red", "on": True, "label": "https://jac.red"}]
    assert got["sources_file"].endswith("search_sources.json")
    client.post("/api/settings", json={"search": {"voices": [" LostFilm ", "", "TVShows"]}})
    assert search.load_prefs().voices == ["LostFilm", "TVShows"]
    # Галка снята — источник выключен, но остаётся в списке про запас.
    client.post("/api/settings", json={"search": {"voices": [], "sources_on": {"https://jac.red": False}}})
    assert search.load_sources() == []
    got = client.get("/api/settings").get_json()["search"]
    assert got["sources"] == [{"url": "https://jac.red", "on": False, "label": "https://jac.red"}]
    client.post("/api/settings", json={"search": {"voices": [], "sources_on": {"https://jac.red": True}}})
    assert search.load_sources() == [{"type": "jacred", "url": "https://jac.red"}]
