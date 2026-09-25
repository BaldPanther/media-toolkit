"""Сервер: приложение Flask, вход по паролю, запуск.

Два режима (см. `main`):

- **своя машина** — сервер слушает только её (127.0.0.1), страница открывается
  в браузере по умолчанию сама, а когда её долго никто не открывал — сервер
  выключается: отдельного окна, которое можно закрыть, у него нет;
- **сервер** (`--server`, Docker, NAS) — слушает сеть, работает постоянно.

Пароль необязателен: задан `WEB_PASSWORD` — страница спросит его один раз и
запомнит вход на месяц. Дома за вход в ZimaOS он обычно не нужен.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session

import paths
from web import api, replies
from web.jobs import Busy
from web.replies import Reply
from web.state import DESKTOP, SERVER, AppState, load_app_settings

STATIC = Path(__file__).resolve().parent / "static"
TEMPLATES = Path(__file__).resolve().parent / "templates"
DEFAULT_PORT = 5800
# Без вопросов страницы столько секунд — на своей машине сервер выключается.
# Фоновые вкладки браузер будит редко (раз в минуту), поэтому запас большой.
IDLE_EXIT_S = 15 * 60
_OPEN_PATHS = {"/login", "/api/login", "/api/info"}


def _secret_key() -> bytes:
    """Ключ подписи куки входа — постоянный, чтобы вход переживал перезапуск."""
    path = paths.config_dir() / "web-secret.key"
    try:
        return bytes.fromhex(path.read_text("ascii").strip())
    except (OSError, ValueError):
        pass
    key = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key.hex())
    except OSError:
        pass                              # не сохранили — вход просто не переживёт перезапуск
    return key


def create_app(state: AppState, password: str = "") -> Flask:
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static",
                template_folder=str(TEMPLATES))
    app.config["STATE"] = state
    app.config["PASSWORD"] = password
    # Без кэша: после обновления программы браузер должен взять новые файлы страницы.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["JSON_SORT_KEYS"] = False
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    app.secret_key = _secret_key() if password else secrets.token_bytes(32)
    app.permanent_session_lifetime = timedelta(days=30)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    app.register_blueprint(api.bp)
    from web import tabs
    tabs.register(app, state)

    @app.before_request
    def guard():
        if not password or session.get("auth"):
            return None
        if request.path in _OPEN_PATHS or request.path.startswith("/static/"):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": {"title": "Нужен вход",
                                      "text": "Обновите страницу и введите пароль."},
                            "login": True}), 401
        return redirect("/login")

    # Метка версии в адресах стилей и скриптов: после обновления программы
    # браузер возьмёт новые файлы, а не старые из своего кэша.
    asset_version = api.app_version() or str(int(time.time()))

    @app.get("/")
    def index():
        return render_template("index.html", v=asset_version)

    @app.get("/login")
    def login_page():
        if not password or session.get("auth"):
            return redirect("/")
        return render_template("login.html", v=asset_version)

    @app.post("/api/login")
    def login():
        given = str(replies.body().get("password", ""))
        if not password or hmac.compare_digest(given.encode(), password.encode()):
            session.permanent = True
            session["auth"] = True
            return jsonify({"ok": True})
        time.sleep(0.5)                    # подбор перебором — медленнее
        return jsonify({"error": {"title": "Неверный пароль", "text": ""}}), 403

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.post("/api/shutdown")
    def shutdown():
        """Выключить программу — только на своей машине, на сервере некому включить."""
        if state.mode != DESKTOP:
            raise replies.UserError("Нельзя выключить",
                                    "Программа работает на сервере — её выключают там.")
        state.jobs.ensure_idle()
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return jsonify({"ok": True})

    @app.errorhandler(Reply)
    def on_reply(e: Reply):
        return jsonify(e.payload()), e.status

    @app.errorhandler(Busy)
    def on_busy(e: Busy):
        return jsonify({"error": {"title": "Идёт операция", "text": str(e)}}), 409

    return app


# ------------------------------------------------------------------ запуск --
def _is_ours(host: str, port: int) -> bool:
    """На порту уже работает эта же программа (запустили второй раз)?"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/info", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == "media-toolkit"
    except Exception:  # noqa: BLE001 — не ответил или ответил не то: значит, не наша
        return False


def _port_free(host: str, port: int) -> bool:
    """Можно ли занять порт. SO_REUSEADDR — как у самого waitress: иначе порт
    считался бы занятым ещё минуту после выхода программы (закрытые соединения
    в TIME_WAIT), и перезапуск уезжал бы на соседний."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _idle_watchdog(state: AppState, limit: float) -> None:
    """Выключает сервер своей машины, когда страницу давно никто не открывал."""
    while True:
        time.sleep(30)
        if not state.jobs.busy() and time.monotonic() - state.last_seen > limit:
            print("Страница давно закрыта — выключаюсь.", flush=True)
            os._exit(0)


def serve(state: AppState, host: str, port: int, password: str = "",
          open_browser: bool = False, idle_exit: float = 0) -> None:
    from waitress import serve as waitress_serve

    app = create_app(state, password)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    if idle_exit:
        threading.Thread(target=_idle_watchdog, args=(state, idle_exit), daemon=True).start()
    if open_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    print(f"Media Toolkit: {url}", flush=True)
    waitress_serve(app, host=host, port=port, threads=8, ident="media-toolkit")


def _log_to_file() -> None:
    """Сборка без консоли (Windows, .app): вывод — в файл, иначе ошибки не увидеть."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        path = paths.config_dir() / "media-toolkit.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            path.replace(path.with_suffix(".log.old"))
        log = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 — живёт до выхода
    except OSError:
        return
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


def main(argv=None) -> int:
    _log_to_file()
    parser = argparse.ArgumentParser(prog="media-toolkit")
    parser.add_argument("folder", nargs="?", default="",
                        help="папка, которую открыть, если прошлой нет")
    parser.add_argument("--server", action="store_true",
                        help="режим сервера (Docker, NAS): слушать сеть, браузер не открывать")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--selfcheck", metavar="ОТЧЁТ",
                        help="проверить зависимости и выйти (нужно сборкам) — selfcheck.py")
    args = parser.parse_args(argv)
    if args.selfcheck:
        import selfcheck
        return selfcheck.run(args.selfcheck)

    server_mode = args.server or os.environ.get("MT_SERVER") == "1"
    mode = SERVER if server_mode else DESKTOP
    host = args.host or os.environ.get("HOST") or ("0.0.0.0" if server_mode else "127.0.0.1")
    port = args.port or int(os.environ.get("PORT") or DEFAULT_PORT)
    password = os.environ.get("WEB_PASSWORD", "")

    if not server_mode:
        # Второй запуск: программа уже работает — просто открыть её страницу.
        if _is_ours(host, port):
            if not args.no_browser:
                webbrowser.open(f"http://{host}:{port}/")
            print(f"Media Toolkit уже работает: http://{host}:{port}/", flush=True)
            return 0
        if not _port_free(host, port):
            port = next((p for p in range(port + 1, port + 50) if _port_free(host, p)), 0)
            if not port:
                print("Нет свободного порта", file=sys.stderr)
                return 1

    # Приоритет: последний путь (если папка доступна) → аргумент запуска → пусто.
    last = load_app_settings().get("last_path", "")
    start = last if last and Path(last).exists() else args.folder

    state = AppState(mode, start)
    if server_mode:
        # В Docker программа — процесс 1: без своего обработчика SIGTERM ядро его
        # игнорирует, и docker stop (и автообновление) ждёт таймаута, а потом убивает.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    serve(state, host, port, password,
          open_browser=not server_mode and not args.no_browser,
          idle_exit=0 if server_mode else IDLE_EXIT_S)
    return 0
