"""Media Toolkit — подготовка медиатеки Kodi: метаданные и картинки, дорожки по умолчанию,
русские субтитры, пропуск заставок.

Запуск:
    python app.py [папка]               — на своей машине: сервер и страница в браузере
    python app.py --server              — сервер для Docker/NAS: слушает сеть
    python app.py --selfcheck отчёт.txt — проверка зависимостей (нужна сборкам)

Интерфейс — веб-страница (`web/`), логика — модули рядом (`core`, `library`, `edl`…).
"""
import sys

from web.server import main

if __name__ == "__main__":
    sys.exit(main())
