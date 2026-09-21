from pathlib import Path

import paths


def _fake_home(monkeypatch, home: Path) -> None:
    """Подменяет домашний каталог: config_dir() строится от него на mac и Linux."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def test_config_dir_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    _fake_home(monkeypatch, tmp_path)
    expected = tmp_path / "Library" / "Application Support" / "media-toolkit"
    assert paths.config_dir() == expected


def test_config_dir_windows_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert paths.config_dir() == tmp_path / "Roaming" / "media-toolkit"


def test_config_dir_windows_without_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    _fake_home(monkeypatch, tmp_path)
    assert paths.config_dir() == tmp_path / "AppData" / "Roaming" / "media-toolkit"


def test_config_dir_linux_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert paths.config_dir() == tmp_path / "cfg" / "media-toolkit"


def test_config_dir_linux_without_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _fake_home(monkeypatch, tmp_path)
    assert paths.config_dir() == tmp_path / ".config" / "media-toolkit"


def _redirect(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """Уводит config_dir() и папку программы во временные каталоги."""
    cfg, legacy = tmp_path / "cfg", tmp_path / "app"
    legacy.mkdir()
    monkeypatch.setattr(paths, "config_dir", lambda: cfg)
    monkeypatch.setattr(paths, "_LEGACY_DIR", legacy)
    return cfg, legacy


def test_settings_path_creates_config_dir_when_nothing_to_migrate(monkeypatch, tmp_path):
    cfg, _ = _redirect(monkeypatch, tmp_path)
    assert paths.settings_path("settings.json") == cfg / "settings.json"
    assert cfg.is_dir()


def test_settings_path_migrates_legacy_file(monkeypatch, tmp_path):
    cfg, legacy = _redirect(monkeypatch, tmp_path)
    (legacy / "meta_settings.json").write_text('{"tmdb_key": "K"}', "utf-8")

    path = paths.settings_path("meta_settings.json")

    assert path == cfg / "meta_settings.json"
    assert path.read_text("utf-8") == '{"tmdb_key": "K"}'
    # Переносим, а не копируем: у папки программы не должно остаться дубля с ключами.
    assert not (legacy / "meta_settings.json").exists()


def test_settings_path_keeps_existing_and_ignores_legacy(monkeypatch, tmp_path):
    cfg, legacy = _redirect(monkeypatch, tmp_path)
    cfg.mkdir()
    (cfg / "subs_settings.json").write_text('{"apikey": "new"}', "utf-8")
    (legacy / "subs_settings.json").write_text('{"apikey": "old"}', "utf-8")

    path = paths.settings_path("subs_settings.json")

    assert path.read_text("utf-8") == '{"apikey": "new"}'
    # Старый файл не тронут — перезаписать настройки миграция не может.
    assert (legacy / "subs_settings.json").read_text("utf-8") == '{"apikey": "old"}'


def test_settings_path_falls_back_to_legacy_when_config_dir_unavailable(monkeypatch, tmp_path):
    _, legacy = _redirect(monkeypatch, tmp_path)
    # Каталог настроек недоступен (нет прав) — работаем рядом с программой, как раньше.
    blocked = tmp_path / "blocked"
    blocked.write_text("", "utf-8")  # файл вместо папки: mkdir обязан упасть
    monkeypatch.setattr(paths, "config_dir", lambda: blocked / "media-toolkit")
    (legacy / "settings.json").write_text("{}", "utf-8")

    assert paths.settings_path("settings.json") == legacy / "settings.json"
