"""Подключение вкладок: у каждой — своё состояние и свои адреса API."""
from __future__ import annotations

from web import tab_edl, tab_library, tab_tracks

# Порядок = порядок вкладок на странице и порядок работы: сначала раскладка и
# метаданные (она переименовывает файлы), потом дорожки и заставки. Импорт
# явный, а не по имени: иначе сборщик (PyInstaller) вкладки бы не увидел.
_MODULES = (tab_library, tab_tracks, tab_edl)


def register(app, state) -> None:
    for module in _MODULES:
        module.setup(state)
        app.register_blueprint(module.bp)
