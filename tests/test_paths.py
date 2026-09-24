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


# --- чтение и запись файлов настроек --------------------------------------- #

def test_write_json_roundtrip_leaves_no_tmp(tmp_path):
    path = tmp_path / "settings.json"
    paths.write_json(path, {"tmdb_key": "K", "имя": "значение"})

    assert paths.read_json(path) == {"tmdb_key": "K", "имя": "значение"}
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_write_json_interrupted_keeps_old_file(monkeypatch, tmp_path):
    # Запись оборвалась на середине (контейнер остановили, ноутбук уснул):
    # старый файл с ключами обязан остаться целым, а не полупустым.
    path = tmp_path / "meta_settings.json"
    path.write_text('{"tmdb_key": "K"}', "utf-8")
    real_write = Path.write_text

    def broken_write(self, text, *args, **kwargs):
        real_write(self, text[: len(text) // 2], *args, **kwargs)
        raise OSError("disk yanked")

    monkeypatch.setattr(Path, "write_text", broken_write)
    try:
        paths.write_json(path, {"tmdb_key": "NEW", "fanart_key": "F"})
    except OSError:
        pass
    monkeypatch.setattr(Path, "write_text", real_write)

    assert path.read_text("utf-8") == '{"tmdb_key": "K"}'


def test_read_json_missing_file_is_none(tmp_path):
    assert paths.read_json(tmp_path / "nope.json") is None


def test_read_json_broken_file_is_set_aside(tmp_path):
    # Битый файл нельзя оставлять на месте: следующее сохранение перезапишет его
    # значениями по умолчанию, и ключи пропадут без следа.
    path = tmp_path / "meta_settings.json"
    path.write_text('{"tmdb_key": "K", "fanart', "utf-8")

    assert paths.read_json(path) is None

    assert not path.exists()
    (bad,) = tmp_path.glob("meta_settings.json.bad*")
    assert bad.read_text("utf-8") == '{"tmdb_key": "K", "fanart'


def test_broken_meta_settings_survive_next_save(monkeypatch, tmp_path):
    import metaconf

    cfg, _ = _redirect(monkeypatch, tmp_path)
    cfg.mkdir()
    (cfg / "meta_settings.json").write_text('{"tmdb_key": "SECRET", "omdb', "utf-8")

    s = metaconf.load_settings()          # битый файл — работаем на дефолтах
    assert s.tmdb_key == ""
    metaconf.save_settings(s)             # и сохраняем их, как сделало бы окно настроек

    (bad,) = cfg.glob("meta_settings.json.bad*")
    assert "SECRET" in bad.read_text("utf-8")


def test_write_json_file_is_private(tmp_path):
    # Там ключи API и пароль OpenSubtitles: на сервере (Docker) файл не должны
    # читать другие пользователи. Новый файл при атомарной записи рождается заново —
    # права задаём явно, а не берём по умолчанию (0644).
    import os
    import stat

    import pytest
    if os.name == "nt":
        pytest.skip("POSIX-права")
    path = tmp_path / "subs_settings.json"
    paths.write_json(path, {"password": "P"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
