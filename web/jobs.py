"""Фоновые операции: одна за раз, с прогрессом, журналом и отменой.

Скан, детект заставок, обрезка хвоста идут минутами, а запрос браузера так
долго не живёт. Поэтому операция запускается в своём потоке, а страница раз в
секунду спрашивает, как дела (`JobManager.status`). Вкладку можно закрыть и
открыть снова — операция от этого не зависит, её состояние хранит сервер.

Одновременно идёт не больше одной операции, как и в прежнем окне на Tk: почти
все они правят одни и те же файлы, и две сразу легли бы друг на друга.
"""
from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field

RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


class Busy(Exception):
    """Операция уже идёт — новую начинать нельзя."""

    def __init__(self, title: str):
        super().__init__(f"Сейчас идёт «{title}». Дождитесь конца или нажмите «Отмена».")
        self.title = title


class Log:
    """Журнал для панели внизу страницы — общий для всех операций.

    У каждой строки свой номер: страница помнит последний полученный и
    спрашивает только новые. Хранятся последние `limit` строк — журнал нужен,
    чтобы видеть, что происходит, а не как архив.
    """

    def __init__(self, limit: int = 2000):
        self._lines: deque[tuple[int, float, str]] = deque(maxlen=limit)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        with self._lock:
            for line in str(text).splitlines() or [""]:
                self._seq += 1
                self._lines.append((self._seq, time.time(), line))

    @property
    def last_seq(self) -> int:
        return self._seq

    def since(self, seq: int, limit: int = 500) -> list[dict]:
        """Строки новее `seq`; не больше `limit` последних."""
        with self._lock:
            lines = [x for x in self._lines if x[0] > seq]
        return [{"seq": s, "time": t, "text": text} for s, t, text in lines[-limit:]]


@dataclass
class Job:
    id: int
    title: str
    started: float = field(default_factory=time.time)
    value: float = 0.0
    maximum: float = 0.0           # 0 — сколько всего, неизвестно: полоса «бегущая»
    text: str = ""                 # что именно сейчас делается
    status: str = RUNNING
    error: str = ""
    summary: str = ""              # итог одной строкой — показывается у полосы
    finished: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def progress(self, value: float | None = None, maximum: float | None = None,
                 text: str | None = None) -> None:
        if maximum is not None:
            self.maximum = float(maximum)
        if value is not None:
            self.value = float(value)
        if text is not None:
            self.text = text

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "status": self.status,
            "value": self.value, "maximum": self.maximum, "text": self.text,
            "error": self.error, "summary": self.summary,
            "started": self.started, "finished": self.finished,
            "cancelling": self.status == RUNNING and self.cancelled(),
        }


class JobManager:
    """Запускает операции в потоке и хранит текущую и последнюю из них."""

    def __init__(self, log: Log, on_change=None):
        self.log = log
        self._on_change = on_change or (lambda: None)
        self._lock = threading.Lock()
        self._next_id = 1
        self.current: Job | None = None       # идёт сейчас
        self.last: Job | None = None          # закончилась последней
        self._thread: threading.Thread | None = None

    def busy(self) -> bool:
        return self.current is not None

    def ensure_idle(self) -> None:
        job = self.current
        if job is not None:
            raise Busy(job.title)

    def start(self, title: str, work, on_done=None) -> Job:
        """Запускает `work(job)` в потоке; `on_done(job, result)` — там же, после.

        Исключение из `work` или `on_done` заканчивает операцию со статусом
        «ошибка», его текст попадает в журнал и в `job.error`. Отмена — это
        просьба: `work` сам проверяет `job.cancelled()` и выходит, когда может.
        """
        with self._lock:
            if self.current is not None:
                raise Busy(self.current.title)
            job = Job(self._next_id, title)
            self._next_id += 1
            self.current = job

        def run():
            try:
                result = work(job)
                if on_done is not None:
                    on_done(job, result)
                job.status = CANCELLED if job.cancelled() else DONE
            except Exception as e:  # noqa: BLE001 — любую поломку показываем, не молчим
                job.status = FAILED
                job.error = str(e) or type(e).__name__
                self.log.add(f"✗ {title}: {job.error}")
                traceback.print_exc()
            finally:
                job.finished = time.time()
                with self._lock:
                    self.current = None
                    self.last = job
                self._on_change()

        self._thread = threading.Thread(target=run, name=f"job-{job.id}", daemon=True)
        self._thread.start()
        self._on_change()
        return job

    def cancel(self) -> bool:
        job = self.current
        if job is None:
            return False
        job.cancel_event.set()
        return True

    def wait(self, timeout: float = 10.0) -> bool:
        """Дождаться конца текущей операции (для тестов). True — закончилась."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.current is None

    def status(self) -> dict:
        job = self.current
        return {
            "busy": job is not None,
            "job": job.to_dict() if job else None,
            "last": self.last.to_dict() if self.last else None,
        }
