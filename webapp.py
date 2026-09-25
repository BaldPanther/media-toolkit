"""Media Toolkit в браузере: `python webapp.py [папка]` или `--server` для Docker/NAS.

Пока идёт переезд с Tk, прежнее окно запускается по-старому (`python app.py`).
"""
import sys

from web.server import main

if __name__ == "__main__":
    sys.exit(main())
