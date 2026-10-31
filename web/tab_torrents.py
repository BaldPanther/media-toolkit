"""Вкладка «Торренты»: автоматика qBittorrent — что разложено, что ждёт выбора.

Логика — в `autopilot.py` и `telegram.py`. Фоновые потоки (автоматику и бота)
запускает `web/server.serve` через `start_background`: в тестах их нет.
"""
from __future__ import annotations

import time

from flask import Blueprint, current_app, jsonify

import autopilot
import kodi
import library
import qbit
import telegram
from web import replies
from web.replies import UserError

bp = Blueprint("torrents", __name__)


class TorrentsTab:
    def __init__(self, state):
        self.st = state
        self.bot = telegram.Service(log=state.log.add)
        self.autopilot = autopilot.Autopilot(log=state.log.add, bot=self.bot,
                                             on_change=state.touch)

    def start_background(self) -> None:
        self.bot.start()
        self.autopilot.start()

    def to_dict(self) -> dict:
        qs, tg, kd = qbit.load_settings(), telegram.load_settings(), kodi.load_settings()
        ap = self.autopilot
        return {
            "qbit": qs.configured(),
            "auto": qs.configured() and qs.auto,
            "categories": [qs.tv_category, qs.movies_category],
            "telegram": tg.configured(),
            "telegram_bound": tg.configured() and bool(tg.chat_id),
            "kodi": kd.configured(),
            "last_check": ap.last_check,
            "last_error": ap.last_error,
            "items": [{
                "hash": i.hash, "name": i.name,
                "kind": "сериал" if i.kind == library.TV else "фильм",
                "status": i.status, "status_label": autopilot.STATUS_LABELS.get(i.status, i.status),
                "title": i.title, "root": i.root, "error": i.error,
                "candidates": [label for _, label in i.candidates],
                "progress": i.progress,
            } for i in ap.recent()],
        }


def tab() -> TorrentsTab:
    return current_app.config["STATE"].tabs["torrents"]


def setup(state) -> None:
    state.add_tab("torrents", TorrentsTab(state))


def _hash() -> str:
    h = str(replies.body().get("hash", ""))
    if h not in tab().autopilot.items:
        raise UserError("Нет такой раздачи", "Обновите страницу.")
    return h


@bp.post("/api/torrents/answer")
def answer():
    """Выбор тайтла для раздачи, в которой автоматика не уверена; -1 — не раскладывать."""
    try:
        index = int(replies.body().get("index"))
    except (TypeError, ValueError):
        raise UserError("Нет такого варианта", "Обновите страницу.")
    tab().autopilot.answer(_hash(), index)
    return jsonify({"ok": True})


@bp.post("/api/torrents/retry")
def retry():
    tab().autopilot.retry(_hash())
    return jsonify({"ok": True})


@bp.post("/api/torrents/check")
def check_now():
    t = tab()
    t.autopilot.wake()
    t.st.log.add("Автоматика: проверка раздач — сейчас.")
    return jsonify({"ok": True, "time": time.time()})
