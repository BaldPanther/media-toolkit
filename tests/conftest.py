# Модули проекта лежат в папке выше (не пакет) — добавляем её в sys.path,
# чтобы тесты импортировали core/edl откуда бы pytest ни запускался.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import swarm  # noqa: E402


@pytest.fixture(autouse=True)
def _no_swarm(monkeypatch):
    """Живость раздач (swarm) в тестах — без сети: трекеры будто не ответили."""
    monkeypatch.setattr(swarm, "scrape", lambda hashes: {})
    monkeypatch.setattr(swarm, "rutracker", lambda hashes: {})
    monkeypatch.setattr(swarm, "nnm", lambda hashes: {})
