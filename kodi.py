"""Kodi: обновить медиатеку сразу после раскладки.

Kodi принимает команды по сети (JSON-RPC; в Kodi: «Настройки» → «Службы» →
«Управление» → «Разрешить удалённое управление по HTTP»). `VideoLibrary.Scan` с
папкой сканирует только её — быстро и в тот момент, когда файлы уже на местах.

Kodi видит медиатеку по своему пути — обычно `smb://nas/share/media/tv/`, — а
программа по своему. Соответствие находится само: по общему хвосту путей
источников Kodi (`Files.GetSources`) и корней библиотеки. Не нашлось —
сканируется вся медиатека.

Только stdlib.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

import paths

_SETTINGS_NAME = "kodi_settings.json"
TIMEOUT = 10


class KodiError(RuntimeError):
    """Kodi недоступен или отказал."""


@dataclass
class Settings:
    url: str = ""              # http://192.168.1.20:8080
    username: str = "kodi"
    password: str = ""

    def configured(self) -> bool:
        return bool(self.url.strip())


def load_settings() -> Settings:
    d = paths.read_json(paths.settings_path(_SETTINGS_NAME))
    if isinstance(d, dict):
        try:
            base = Settings()
            return Settings(url=str(d.get("url", "")),
                            username=str(d.get("username", base.username)),
                            password=str(d.get("password", "")))
        except Exception:  # noqa: BLE001 — поле неожиданного типа: значения по умолчанию
            pass
    return Settings()


def save_settings(s: Settings) -> None:
    paths.write_json(paths.settings_path(_SETTINGS_NAME), asdict(s))


class Client:
    def __init__(self, url: str, username: str = "", password: str = "", timeout: int = TIMEOUT):
        base = url.strip().rstrip("/")
        if base and "://" not in base:
            base = "http://" + base
        self.base = base
        self.username = username
        self.password = password
        self.timeout = timeout

    def rpc(self, method: str, **params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        headers = {"Content-Type": "application/json"}
        if self.username or self.password:
            token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        req = urllib.request.Request(f"{self.base}/jsonrpc", data=body.encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise KodiError("Kodi: неверный логин или пароль.") from e
            raise KodiError(f"Kodi ответил HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise KodiError(f"Kodi недоступен ({self.base}): {getattr(e, 'reason', e)}") from e
        except ValueError as e:
            raise KodiError("Kodi: битый ответ") from e
        if "error" in data:
            raise KodiError(f"Kodi: {data['error'].get('message', 'ошибка')}")
        return data.get("result")

    def version(self) -> str:
        v = (self.rpc("Application.GetProperties", properties=["version"]) or {}).get("version", {})
        return f"{v.get('major', '?')}.{v.get('minor', '?')}"

    def sources(self) -> list[str]:
        result = self.rpc("Files.GetSources", media="video") or {}
        return [str(s.get("file", "")) for s in result.get("sources", []) if s.get("file")]

    def scan(self, directory: str = "") -> None:
        if directory:
            self.rpc("VideoLibrary.Scan", directory=directory)
        else:
            self.rpc("VideoLibrary.Scan")


def _segments(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").split("/") if p]


@dataclass(frozen=True)
class PathMap:
    """Префикс пути у программы ↔ тот же префикс у Kodi (`smb://nas/share/`)."""
    local: str
    remote: str

    def to_kodi(self, local_path) -> str | None:
        parts, prefix = Path(local_path).parts, Path(self.local).parts
        if parts[:len(prefix)] != prefix:
            return None
        rest = "/".join(parts[len(prefix):])
        return self.remote.rstrip("/") + "/" + (rest + "/" if rest else "")


def detect_map(sources, local_roots) -> PathMap | None:
    """По общему хвосту: `smb://nas/share/media/tv/` и `/media/disk1/share/media/tv`
    дают `/media/disk1/share` ↔ `smb://nas/share/`."""
    best, best_len = None, 0
    for source in sources:
        src = _segments(source.split("://", 1)[-1])
        scheme = source.split("://", 1)[0] + "://" if "://" in source else "/"
        for local in local_roots:
            loc = Path(local)
            lp = list(loc.parts[1:])
            n = 0
            while n < len(src) and n < len(lp) and src[-1 - n].casefold() == lp[-1 - n].casefold():
                n += 1
            if n > best_len:
                best_len = n
                remote_head = src[:len(src) - n]
                remote = scheme + "/".join(remote_head) + ("/" if remote_head else "")
                best = PathMap(str(loc.parents[n - 1]), remote)
    return best


def scan(path, library_roots, settings: Settings | None = None) -> str:
    """Просканировать в Kodi папку тайтла. → что именно отправлено (для журнала)."""
    s = settings or load_settings()
    if not s.configured():
        return ""
    client = Client(s.url, s.username, s.password)
    pmap = detect_map(client.sources(), library_roots)
    target = pmap.to_kodi(path) if pmap else None
    client.scan(target or "")
    return target or "вся медиатека"


def check(settings: Settings, library_roots) -> str:
    client = Client(settings.url, settings.username, settings.password)
    version = client.version()
    pmap = detect_map(client.sources(), library_roots)
    where = (f"пути «{pmap.local}» ↔ «{pmap.remote}»" if pmap
             else "источники Kodi не совпали с корнями — будет сканироваться вся медиатека")
    return f"Kodi {version}, {where}."
