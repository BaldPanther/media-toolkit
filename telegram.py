"""Telegram-бот: оповещения и вопросы с кнопками.

Бот спрашивает Telegram о новых сообщениях сам (long polling, `getUpdates`), поэтому
работает с NAS без белого IP и вебхука. Чат привязывается первым `/start`: дальше
бот слушает только его, а чужие сообщения и кнопки молча игнорирует — иначе кто
угодно, нашедший бота, мог бы качать через него.

Кнопки несут короткую строку `префикс:аргументы` (Telegram ограничивает её 64
байтами). Обработчик префикса регистрирует тот, кто задаёт вопрос (`on`).

Только stdlib.
"""
from __future__ import annotations

import html
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

import paths

_SETTINGS_NAME = "telegram_settings.json"
API = "https://api.telegram.org"
POLL_TIMEOUT = 50          # секунд держит запрос getUpdates, пока нет новостей


class TelegramError(RuntimeError):
    """Telegram недоступен или отказал (неверный токен, бот заблокирован)."""


@dataclass
class Settings:
    token: str = ""
    chat_id: int = 0           # 0 — чат ещё не привязан: ждём первого /start

    def configured(self) -> bool:
        return bool(self.token.strip())


def load_settings() -> Settings:
    d = paths.read_json(paths.settings_path(_SETTINGS_NAME))
    if isinstance(d, dict):
        try:
            return Settings(token=str(d.get("token", "")), chat_id=int(d.get("chat_id") or 0))
        except Exception:  # noqa: BLE001 — поле неожиданного типа: значения по умолчанию
            pass
    return Settings()


def save_settings(s: Settings) -> None:
    paths.write_json(paths.settings_path(_SETTINGS_NAME), asdict(s))


def esc(text) -> str:
    """Текст для сообщения с разметкой HTML."""
    return html.escape(str(text), quote=False)


def keyboard(rows) -> dict:
    """[[("Текст", "данные" | "https://…"), …], …] → inline_keyboard Telegram."""
    out = []
    for row in rows:
        buttons = []
        for label, value in row:
            key = "url" if str(value).startswith(("http://", "https://")) else "callback_data"
            buttons.append({"text": label, key: value})
        out.append(buttons)
    return {"inline_keyboard": out}


class Bot:
    def __init__(self, token: str, timeout: int = 15):
        self.token = token.strip()
        self.timeout = timeout

    def call(self, method: str, http_timeout: int | None = None, **params):
        req = urllib.request.Request(
            f"{API}/bot{self.token}/{method}", data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout or self.timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                desc = json.loads(e.read().decode("utf-8", "replace")).get("description", "")
            except (ValueError, OSError):
                desc = ""
            if e.code == 401:
                raise TelegramError("Telegram: неверный токен бота.") from e
            raise TelegramError(f"Telegram: {desc or f'HTTP {e.code}'}") from e
        except (urllib.error.URLError, OSError) as e:
            raise TelegramError(f"Telegram недоступен: {getattr(e, 'reason', e)}") from e
        except ValueError as e:
            raise TelegramError("Telegram: битый ответ") from e
        if not data.get("ok"):
            raise TelegramError(f"Telegram: {data.get('description', 'отказ')}")
        return data.get("result")

    def me(self) -> dict:
        return self.call("getMe")

    def send(self, chat_id: int, text: str, buttons=None) -> int:
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = keyboard(buttons)
        return int((self.call("sendMessage", **params) or {}).get("message_id", 0))

    def edit(self, chat_id: int, message_id: int, text: str, buttons=None) -> None:
        params = {"chat_id": chat_id, "message_id": message_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = keyboard(buttons)
        self.call("editMessageText", **params)

    def answer(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def updates(self, offset: int, timeout: int = POLL_TIMEOUT) -> list:
        return self.call("getUpdates", http_timeout=timeout + 10, offset=offset,
                         timeout=timeout, allowed_updates=["message", "callback_query"]) or []


class Service:
    """Бот в фоне: привязка чата, оповещения, кнопки."""

    def __init__(self, log=None):
        self.log = log or (lambda text: None)
        self._handlers: dict[str, object] = {}
        self._thread: threading.Thread | None = None
        self._offset = 0

    def on(self, prefix: str, handler) -> None:
        """`handler(args: str) -> str` — ответ на кнопку `prefix:args` (всплывающий текст)."""
        self._handlers[prefix] = handler

    # ------------------------------------------------------------ отправка --
    def ready(self) -> bool:
        s = load_settings()
        return s.configured() and bool(s.chat_id)

    def notify(self, text: str, buttons=None) -> int:
        """Сообщение в привязанный чат. Бот не настроен — молча ничего; ошибка — в журнал."""
        s = load_settings()
        if not s.configured() or not s.chat_id:
            return 0
        try:
            return Bot(s.token).send(s.chat_id, text, buttons)
        except TelegramError as e:
            self.log(f"⚠ {e}")
            return 0

    def edit(self, message_id: int, text: str, buttons=None) -> None:
        s = load_settings()
        if not message_id or not s.configured() or not s.chat_id:
            return
        try:
            Bot(s.token).edit(s.chat_id, message_id, text, buttons)
        except TelegramError as e:
            self.log(f"⚠ {e}")

    # ------------------------------------------------------------- приём --
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            s = load_settings()
            if not s.configured():
                time.sleep(30)
                continue
            try:
                for update in Bot(s.token).updates(self._offset):
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    self.handle(update)
            except TelegramError as e:
                self.log(f"⚠ {e}")
                time.sleep(60)
            except Exception as e:  # noqa: BLE001 — фоновый поток не должен умирать
                self.log(f"⚠ Telegram: {e}")
                time.sleep(60)

    def handle(self, update: dict) -> None:
        s = load_settings()
        bot = Bot(s.token)
        if "callback_query" in update:
            self._callback(bot, s, update["callback_query"])
            return
        msg = update.get("message") or {}
        chat = int((msg.get("chat") or {}).get("id") or 0)
        text = str(msg.get("text") or "").strip()
        if not chat:
            return
        if not s.chat_id and text.startswith("/start"):
            s.chat_id = chat
            save_settings(s)
            self.log("Telegram: бот привязан к чату.")
            bot.send(chat, "Готово: оповещения медиатеки будут приходить сюда.")
        elif chat == s.chat_id and text.startswith("/start"):
            bot.send(chat, "Я на связи: оповещения медиатеки приходят сюда.")

    def _callback(self, bot: Bot, s: Settings, query: dict) -> None:
        chat = int(((query.get("message") or {}).get("chat") or {}).get("id") or 0)
        if not s.chat_id or chat != s.chat_id:
            return
        prefix, _, args = str(query.get("data") or "").partition(":")
        handler = self._handlers.get(prefix)
        reply = ""
        try:
            reply = handler(args) if handler else "Кнопка устарела."
        except Exception as e:  # noqa: BLE001 — ответ на кнопку всё равно нужен
            reply = f"Ошибка: {e}"
            self.log(f"⚠ Telegram, кнопка {prefix}: {e}")
        try:
            bot.answer(str(query.get("id", "")), str(reply or "")[:190])
        except TelegramError:
            pass


def check(token: str) -> str:
    """Проверка из настроек: имя бота и привязан ли чат."""
    me = Bot(token).me()
    s = load_settings()
    bound = ("чат привязан" if s.chat_id and s.token.strip() == token.strip()
             else "напишите боту /start, чтобы привязать чат")
    return f"Бот @{me.get('username', '?')}: {bound}."
