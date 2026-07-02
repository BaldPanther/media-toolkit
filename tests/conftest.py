# Модули проекта лежат в папке выше (не пакет) — добавляем её в sys.path,
# чтобы тесты импортировали core/edl откуда бы pytest ни запускался.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
