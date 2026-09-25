"""Ответы API, которые страница показывает человеку: ошибка, сообщение, вопрос.

В окне на Tk обработчик кнопки мог по ходу дела спросить «Продолжить?» и ждать
ответа. У веба так нельзя: запрос должен вернуться сразу. Поэтому вопрос —
это ответ сервера `{"ask": …}`: страница показывает диалог и повторяет тот же
запрос, добавив выбранное в `answers`. Обработчик идёт по своему коду заново и
на этот раз проходит вопрос, забрав ответ из `answers`. Так логика и все тексты
остаются на сервере, рядом с проверками, как было в `app.py`.
"""
from __future__ import annotations

from flask import request


class Reply(Exception):
    """Досрочный ответ обработчика — не ошибка программы, а часть диалога."""

    status = 200

    def payload(self) -> dict:
        raise NotImplementedError


class UserError(Reply):
    """Сделать нельзя: нет папки, нет ключа, нет MKVToolNix."""

    status = 400

    def __init__(self, title: str, text: str = ""):
        super().__init__(text or title)
        self.title, self.text = title, text

    def payload(self) -> dict:
        return {"error": {"title": self.title, "text": self.text}}


class Info(Reply):
    """Сообщение без ошибки: «нечего применять», «всё готово»."""

    def __init__(self, title: str, text: str = ""):
        super().__init__(text or title)
        self.title, self.text = title, text

    def payload(self) -> dict:
        return {"message": {"title": self.title, "text": self.text}}


class Ask(Reply):
    """Вопрос человеку. Кнопки — (подпись, значение, вид); значение None — отмена."""

    def __init__(self, key: str, title: str, text: str, buttons):
        super().__init__(title)
        self.key, self.title, self.text, self.buttons = key, title, text, buttons

    def payload(self) -> dict:
        return {"ask": {
            "id": self.key, "title": self.title, "text": self.text,
            "buttons": [{"label": label, "value": value, "kind": kind}
                        for label, value, kind in self.buttons],
        }}


def body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def answers() -> dict:
    got = body().get("answers")
    return got if isinstance(got, dict) else {}


def choose(key: str, title: str, text: str, buttons):
    """Ответ на вопрос `key`, если он уже есть; иначе спросить (исключение Ask)."""
    got = answers()
    if key in got:
        return got[key]
    raise Ask(key, title, text, buttons)


def confirm(key: str, title: str, text: str, yes: str = "Да", no: str = "Отмена",
            danger: bool = False) -> None:
    """«Продолжить?» — проходит дальше, только если уже ответили «да»."""
    choose(key, title, text, [(no, None, "secondary"),
                              (yes, True, "danger" if danger else "primary")])
