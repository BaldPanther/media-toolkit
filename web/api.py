"""Общая часть API: статус операции, журнал, настройки, выбор папки."""
from __future__ import annotations

import os
import string
import sys
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

import core
import edl
import library
import metaconf
import subs
from web import replies
from web.replies import UserError
from web.state import update_app_settings

bp = Blueprint("common", __name__)

# Размеры постера у TMDb. Крупнее «original» не бывает, мельче w500 постер
# заметно мылит на большом экране.
POSTER_SIZE_LABELS = {
    "w342": "342 px (экономно)",
    "w500": "500 px",
    "w780": "780 px",
    "original": "оригинал (до 2000 px)",
}

# Страницы выдачи ключей — на них ведут ссылки «Получить…» в настройках.
KEY_URLS = {
    "tmdb": "https://www.themoviedb.org/settings/api",
    "fanart": "https://fanart.tv/get-an-api-key/",
    "omdb": "https://www.omdbapi.com/apikey.aspx",
    "opensubtitles": "https://www.opensubtitles.com/consumers",
}

# Языки картинок, которые предлагаются в настройках. Список открытый: у TMDb
# двухбуквенные коды ISO 639-1, вписать можно любой.
ART_LANGUAGE_LABELS = {
    "ru": "русский", "en": "английский", metaconf.ART_ORIGINAL: "язык оригинала",
    "": "без текста", "uk": "украинский", "ja": "японский", "ko": "корейский",
    "zh": "китайский", "de": "немецкий", "fr": "французский", "es": "испанский",
    "it": "итальянский",
}


def state():
    return current_app.config["STATE"]


def app_version() -> str:
    """Версия: из окружения (Docker) или из файла VERSION (сборки); из исходников — пусто."""
    env = os.environ.get("MT_VERSION", "").strip()
    if env:
        return env
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text("ascii").strip()
    except OSError:
        return ""


# ------------------------------------------------------------- статус --
@bp.get("/api/info")
def info():
    """Что умеет эта установка: режим, найденные инструменты, адрес медиатеки."""
    st = state()
    missing = edl.missing_tools()
    try:
        core.find_tools()
        mkv_error = ""
    except FileNotFoundError as e:
        mkv_error = str(e)
    player = edl.find_player() if st.mode == "desktop" else None
    return jsonify({
        "app": "media-toolkit",
        "version": app_version(),
        "mode": st.mode,
        "platform": sys.platform,
        "auth": bool(current_app.config.get("PASSWORD")),
        "mkvtoolnix_error": mkv_error,
        "edl_missing": missing,
        "edl_hint": edl.install_hint(missing) if missing else "",
        "send2trash": library.send_to_trash_available(),
        "player": player[0] if player else "",
    })


@bp.get("/api/status")
def status():
    """Ход операции и новые строки журнала. Страница спрашивает раз в секунду."""
    st = state()
    st.heartbeat()
    try:
        since = int(request.args.get("log", "0"))
    except ValueError:
        since = 0
    return jsonify({
        **st.jobs.status(),
        "version": st.version,
        "log": st.log.since(since),
    })


@bp.post("/api/cancel")
def cancel():
    return jsonify({"ok": state().jobs.cancel()})


@bp.get("/api/state")
def full_state():
    """Всё, что показывает страница: рабочая папка и содержимое вкладок."""
    st = state()
    with st.lock:
        return jsonify({
            "version": st.version,
            "workspace": st.to_dict(),
            "tabs": {name: tab.to_dict() for name, tab in st.tabs.items()},
        })


@bp.post("/api/path")
def set_path():
    data = replies.body()
    st = state()
    st.jobs.ensure_idle()
    st.set_path(str(data.get("path", "")), data.get("recursive"))
    return full_state()


# ------------------------------------------------------------ настройки --
def _settings_dict() -> dict:
    meta = metaconf.load_settings()
    sub = subs.load_settings()
    return {
        "meta": {
            "tmdb_key": meta.tmdb_key, "fanart_key": meta.fanart_key,
            "omdb_key": meta.omdb_key, "meta_language": meta.meta_language,
            "name_language": meta.name_language, "art_languages": meta.art_languages,
            "poster_size": meta.poster_size, "movies_roots": meta.movies_roots,
            "tv_roots": meta.tv_roots,
        },
        "subs": {"username": sub.username, "password": sub.password,
                 "apikey": sub.apikey, "use_fallback": sub.use_fallback},
        "choices": {
            "languages": metaconf.LANGUAGES,
            "name_modes": {m: metaconf.NAME_LABELS[m] for m in metaconf.NAME_MODES},
            "poster_sizes": POSTER_SIZE_LABELS,
            "art_languages": ART_LANGUAGE_LABELS,
        },
        "key_urls": KEY_URLS,
    }


def _str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        item = str(item).strip() if item is not None else ""
        if item not in out:
            out.append(item)
    return out


@bp.get("/api/settings")
def get_settings():
    return jsonify(_settings_dict())


@bp.post("/api/settings")
def save_settings():
    """Сохраняет то, что пришло: только известные поля, остальные — как были.

    Настройки скрапера и OpenSubtitles — разные файлы, и прислать можно любой
    из разделов: вкладка «Дорожки» правит только OpenSubtitles.
    """
    data = replies.body()
    st = state()
    meta_in = data.get("meta")
    if isinstance(meta_in, dict):
        s = metaconf.load_settings()
        for key in ("tmdb_key", "fanart_key", "omdb_key"):
            if key in meta_in:
                setattr(s, key, str(meta_in[key] or "").strip())
        if meta_in.get("meta_language") in metaconf.LANGUAGES:
            s.meta_language = meta_in["meta_language"]
        if meta_in.get("name_language") in metaconf.NAME_MODES:
            s.name_language = meta_in["name_language"]
        if "art_languages" in meta_in:
            s.art_languages = _str_list(meta_in["art_languages"]) or ["ru", "en", ""]
        if meta_in.get("poster_size") in POSTER_SIZE_LABELS:
            s.poster_size = meta_in["poster_size"]
        for key in ("movies_roots", "tv_roots"):
            if key in meta_in:
                setattr(s, key, [p for p in _str_list(meta_in[key]) if p])
        metaconf.save_settings(s)
    subs_in = data.get("subs")
    if isinstance(subs_in, dict):
        old = subs.load_settings()
        subs.save_settings(subs.Settings(
            username=str(subs_in.get("username", old.username) or "").strip(),
            password=str(subs_in.get("password", old.password) or ""),
            apikey=str(subs_in.get("apikey", old.apikey) or "").strip(),
            use_fallback=bool(subs_in.get("use_fallback", old.use_fallback)),
        ))
    st.log.add("Настройки сохранены.")
    for tab in st.tabs.values():
        hook = getattr(tab, "on_settings_changed", None)
        if hook is not None:
            hook()
    st.touch()
    return jsonify(_settings_dict())


# --------------------------------------------------------- выбор папки --
def _browse_roots(st) -> list[dict]:
    """С чего начинается выбор папки: медиатека, диски, домашняя папка."""
    roots: list[dict] = []

    def add(name: str, path) -> None:
        path = str(path)
        if Path(path).is_dir() and all(r["path"] != path for r in roots):
            roots.append({"name": name, "path": path})

    media = os.environ.get("MEDIA_ROOT", "").strip()
    if media:
        add(media, media)
    meta = metaconf.load_settings()
    for p in meta.movies_roots:
        add(f"Фильмы: {Path(p).name or p}", p)
    for p in meta.tv_roots:
        add(f"Сериалы: {Path(p).name or p}", p)
    if media:
        return roots                   # в контейнере за пределами /media смотреть нечего
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            add(f"Диск {letter}:", f"{letter}:\\")
    elif sys.platform == "darwin":
        volumes = Path("/Volumes")
        try:
            # «Macintosh HD» там — ссылка на сам корень, её пропускаем.
            for v in sorted(volumes.iterdir(), key=lambda p: p.name.casefold()):
                if not v.is_symlink():
                    add(v.name, v)
        except OSError:
            pass
        add("Домашняя папка", Path.home())
    else:
        add("Домашняя папка", Path.home())
        add("/", "/")
    return roots


def _parent(path: Path) -> str:
    parent = path.parent
    return "" if parent == path else str(parent)


@bp.get("/api/fs")
def browse():
    """Содержимое папки для окна выбора: подпапки и (по просьбе) видеофайлы.

    Системного диалога у браузера нет — он не видит диски сервера, — поэтому
    папку выбирают в своём окне, которое ходит сюда.
    """
    st = state()
    raw = request.args.get("path", "").strip()
    want_files = request.args.get("files") == "1"
    roots = _browse_roots(st)
    if not raw:
        return jsonify({"path": "", "parent": "", "entries": [], "roots": roots})
    path = Path(raw).expanduser()
    if path.is_file():
        path = path.parent
    if not path.is_dir():
        raise UserError("Нет такой папки", str(path))
    entries = []
    try:
        for child in path.iterdir():
            if child.name.startswith((".", "$")) or child.name.startswith(library.APPLEDOUBLE_PREFIX):
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if is_dir:
                entries.append({"name": child.name, "path": str(child), "dir": True})
            elif want_files and library.is_video(child):
                entries.append({"name": child.name, "path": str(child), "dir": False})
    except PermissionError:
        raise UserError("Нет доступа", f"Нет прав читать папку {path}")
    except OSError as e:
        raise UserError("Папка недоступна", f"{path}: {e}")
    entries.sort(key=lambda e: (not e["dir"], e["name"].casefold()))
    return jsonify({"path": str(path), "parent": _parent(path), "entries": entries,
                    "roots": roots})


def remember_path(path: str) -> None:
    update_app_settings(last_path=path)


# --------------------------------------------------------- сканирование --
def scan_summary(files, target: Path) -> str:
    ok = [f for f in files if not f.error]
    groups = core.group_by_audio_signature(ok)
    errs = len(files) - len(ok)
    msg = f"Найдено MKV: {len(files)} (ошибок: {errs}). Раскладок аудио: {len(groups)}."
    if len(groups) > 1:
        msg += "  ⚠ Раскладки различаются между файлами — выбирайте дорожку по названию."
    if not files and target.is_file():
        # Указан одиночный файл не того формата: mkvpropedit работает
        # только с MKV, но вкладка «Медиатека» такой файл всё равно разложит.
        msg = (f"«{target.name}» — не MKV, дорожки в нём менять нечем. "
               "Разложить его по папкам можно на вкладке «Медиатека».")
    return msg


def start_scan(st, folder: str, recursive: bool, rescan: bool = False, title="Сканирование"):
    """Сканирует папку в фоне; результат — во вкладки. Возвращает операцию."""
    def work(job):
        job.progress(0, 0, "список файлов…")
        try:
            return core.scan_folder(
                folder, recursive, stop=job.cancelled,
                progress=lambda i, total, p: job.progress(i, total, f"{i}/{total}: {Path(p).name}"))
        except Exception:
            st.summary = "Ошибка сканирования."
            raise

    def done(job, files):
        if job.cancelled():
            # Частичный список не показываем — остаётся прежнее состояние.
            st.summary = "Сканирование отменено."
            st.log.add("Сканирование отменено.")
            job.summary = st.summary
            return
        msg = scan_summary(files, Path(folder))
        st.summary = msg
        st.log.add(msg)
        job.summary = msg
        st.apply_scan(files, rescan)

    return st.jobs.start(title, work, on_done=done)


@bp.post("/api/scan")
def scan():
    st = state()
    st.jobs.ensure_idle()
    data = replies.body()
    folder = str(data.get("path") or st.path).strip()
    # Путь может указывать и на одиночный файл фильма — его выбирают
    # кнопкой «Файл…», и дорожки в нём настраиваются так же, как в сериале.
    if not folder or not Path(folder).exists():
        raise UserError("Нет такой папки", "Укажите существующую папку или файл.")
    recursive = bool(data.get("recursive", st.recursive))
    st.set_path(folder, recursive)
    remember_path(folder)
    # Нажатие «Сканировать» — тоже подтверждение выбора папки: вкладка
    # «Медиатека» подставляет по ней название, если его ещё нет.
    for tab in st.tabs.values():
        hook = getattr(tab, "on_scan_requested", None)
        if hook is not None:
            hook(folder)
    try:
        core.find_tools()
    except FileNotFoundError as e:
        raise UserError("MKVToolNix не найден", str(e))
    st.summary = "Сканирование…"
    st.log.add(f"Сканирование: {folder} (рекурсивно: {'да' if recursive else 'нет'})")
    job = start_scan(st, folder, recursive)
    return jsonify({"job": job.to_dict()})
