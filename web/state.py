"""Состояние программы на сервере: рабочая папка, журнал, фоновая операция.

В окне на Tk всё это жило в полях окна и пропадало вместе с ним. Здесь то же
самое хранит сервер, а страница в браузере только показывает его — поэтому её
можно закрыть и открыть снова, хоть с другого компьютера, и увидеть то же, что
было: просканированные серии, найденные тайминги, ход операции.

Пользователь один, поэтому и состояние одно на всех, без сессий. Изменилось
что-то — растёт `version`; страница видит это в ответе `/api/status` и
перечитывает то, что показывает.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import paths
from web.jobs import JobManager, Log

# Локальные настройки приложения (последний путь, настройки EDL, онлайн-правила) —
# тот же файл, что у прежнего окна на Tk, чтобы они переехали сами.
_SETTINGS_NAME = "settings.json"

# Режимы запуска: своя машина (сервер слушает только её, страница открывается сама)
# и сервер (Docker, NAS — слушает сеть, браузер открывают снаружи).
DESKTOP = "desktop"
SERVER = "server"


def load_app_settings() -> dict:
    d = paths.read_json(paths.settings_path(_SETTINGS_NAME))
    return d if isinstance(d, dict) else {}  # нет файла/битый JSON: просто пустые настройки


def save_app_settings(data: dict) -> None:
    try:
        paths.write_json(paths.settings_path(_SETTINGS_NAME), data)
    except Exception:  # noqa: BLE001 — не критично, просто не сохранили
        pass


def update_app_settings(**changes) -> None:
    save_app_settings({**load_app_settings(), **changes})


class AppState:
    def __init__(self, mode: str = DESKTOP, start_path: str = ""):
        self.mode = mode
        self.log = Log()
        self.lock = threading.RLock()
        self.version = 0
        self.jobs = JobManager(self.log, on_change=self.touch)

        self.path = start_path
        self.recursive = True
        self.summary = "Папка не выбрана."
        # Результат последнего сканирования (core.MkvFile) — общий для вкладок
        # «Дорожки» и «EDL», как и в прежнем окне.
        self.files: list = []
        # Вкладки: у каждой своё состояние и свои реакции на смену папки и скан.
        self.tabs: dict[str, object] = {}
        # Когда страница последний раз спрашивала статус. На своей машине сервер
        # по нему понимает, что вкладку закрыли, и выключается сам.
        self.last_seen = time.monotonic()

    def heartbeat(self) -> None:
        self.last_seen = time.monotonic()

    def touch(self) -> None:
        with self.lock:
            self.version += 1

    def add_tab(self, name: str, tab) -> None:
        self.tabs[name] = tab

    def set_path(self, path: str, recursive: bool | None = None) -> None:
        """Сменить рабочую папку (или файл). Вкладки узнают об этом сами."""
        path = path.strip()
        changed = path != self.path
        self.path = path
        if recursive is not None:
            self.recursive = bool(recursive)
        if changed:
            # Прежняя сводка сканирования к новой папке не относится.
            self.summary = ("Нажмите «Сканировать», чтобы увидеть дорожки и серии." if path
                            else "Папка не выбрана.")
            for tab in self.tabs.values():
                hook = getattr(tab, "on_path_changed", None)
                if hook is not None:
                    hook(path)
        self.touch()

    def target(self) -> Path | None:
        """Текущая папка или файл, если они существуют."""
        if not self.path:
            return None
        p = Path(self.path)
        return p if p.exists() else None

    def apply_scan(self, files, rescan: bool = False) -> None:
        """Новый результат сканирования — вкладкам. rescan: после «Применить»,
        выбор в вкладках сохраняется."""
        with self.lock:
            self.files = files
            for tab in self.tabs.values():
                hook = getattr(tab, "on_scan", None)
                if hook is not None:
                    hook(files, rescan)
        self.touch()

    def replace_file(self, fresh) -> None:
        """Один файл пересканирован (после обрезки хвоста) — подменить его в списке."""
        with self.lock:
            self.files = [fresh if str(f.path) == str(fresh.path) else f for f in self.files]

    def to_dict(self) -> dict:
        return {"path": self.path, "recursive": self.recursive, "summary": self.summary,
                "scanned": len(self.files)}
