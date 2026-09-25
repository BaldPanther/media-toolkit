"""Подключение вкладок: у каждой — своё состояние и свои адреса API."""
from __future__ import annotations

# Порядок = порядок вкладок на странице и порядок работы: сначала раскладка и
# метаданные (она переименовывает файлы), потом дорожки и заставки.
_MODULES: list[str] = ["tab_tracks"]


def register(app, state) -> None:
    import importlib

    for name in _MODULES:
        module = importlib.import_module(f"web.{name}")
        module.setup(state)
        app.register_blueprint(module.bp)
