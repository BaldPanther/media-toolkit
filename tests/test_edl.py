"""Тесты каркаса edl.py: парсинг S/E, padding, формат/запись .edl, выбор дорожки."""
from pathlib import Path

import pytest

import edl
from edl import EpisodeEdl, Padding, Segment


# ------------------------------------------------------------- parse S/E --

@pytest.mark.parametrize("name, expected", [
    ("Show.S01E02.1080p.mkv", (1, 2)),
    ("show s1e1.mkv", (1, 1)),
    ("Show.S01.E03.mkv", (1, 3)),
    ("Show.S02_E11.mkv", (2, 11)),
    ("Show.1x07.mkv", (1, 7)),
    ("Show 10X103.mkv", (10, 103)),
    ("Movie.2019.mkv", (None, None)),
    ("Show.E05.mkv", (None, None)),          # эпизод без сезона не ловим
    ("Show.1080x720.mkv", (None, None)),     # разрешение — не NxNN
])
def test_parse_season_episode(name, expected):
    assert edl.parse_season_episode(Path("folder") / name) == expected


def test_group_by_season_sorts_by_episode():
    paths = [
        Path("Show.S01E03.mkv"),
        Path("Show.S02E01.mkv"),
        Path("Show.S01E01.mkv"),
        Path("Show.S01E02.mkv"),
        Path("random.mkv"),
    ]
    groups = edl.group_by_season(paths)
    assert set(groups) == {1, 2, None}
    assert [p.name for p in groups[1]] == ["Show.S01E01.mkv", "Show.S01E02.mkv", "Show.S01E03.mkv"]
    assert [p.name for p in groups[None]] == ["random.mkv"]


# --------------------------------------------------------------- padding --

def test_apply_padding_none_passthrough():
    assert edl.apply_padding(None, 5, 5) is None


def test_apply_padding_shifts_bounds():
    seg = edl.apply_padding(Segment(10.0, 40.0), 2.0, -3.0)
    assert (seg.start, seg.end) == (12.0, 37.0)


def test_apply_padding_clamps_to_zero_and_duration():
    seg = edl.apply_padding(Segment(2.0, 40.0), -10.0, 30.0, duration=50.0)
    assert (seg.start, seg.end) == (0.0, 50.0)


def test_apply_padding_collapsed_returns_none():
    assert edl.apply_padding(Segment(10.0, 15.0), 4.0, -4.0) is None


def test_segment_length_non_negative():
    assert Segment(10.0, 5.0).length == 0.0
    assert Segment(5.0, 10.0).length == 5.0


# ------------------------------------------------- первая серия сезона --

def test_effective_intro_keep_first():
    e01 = EpisodeEdl.from_path("Show.S01E01.mkv")
    e01.intro = Segment(5.0, 35.0)
    e02 = EpisodeEdl.from_path("Show.S01E02.mkv")
    e02.intro = Segment(5.0, 35.0)

    assert edl.is_first_of_season(e01)
    assert not edl.is_first_of_season(e02)
    assert edl.effective_intro(e01, keep_first_intro=True) is None
    assert edl.effective_intro(e01, keep_first_intro=False) is e01.intro
    assert edl.effective_intro(e02, keep_first_intro=True) is e02.intro


# ------------------------------------------------------------ format .edl --

def test_format_edl_empty():
    assert edl.format_edl(None, None, None) == ""


def test_format_edl_segments_and_action():
    text = edl.format_edl(Segment(5.0, 35.5), Segment(1200.0, 1260.0), Segment(0.0, 12.0))
    lines = text.splitlines()
    # порядок: recap, intro, outro; данные — start<TAB>end<TAB>3
    data = [l for l in lines if not l.startswith("##")]
    assert data == ["0.000\t12.000\t3", "5.000\t35.500\t3", "1200.000\t1260.000\t3"]
    comments = [l for l in lines if l.startswith("##")]
    assert len(comments) == 3
    assert text.endswith("\n")


def test_write_and_delete_edl(tmp_path):
    video = tmp_path / "Show.S01E02.mkv"
    video.touch()
    p = edl.write_edl(video, Segment(5.0, 35.0), None)
    assert p == tmp_path / "Show.S01E02.edl"
    assert edl.has_external_edl(video)
    assert "5.000\t35.000\t3" in p.read_text(encoding="utf-8")

    assert edl.delete_edl(video) is True
    assert not edl.has_external_edl(video)
    assert edl.delete_edl(video) is False          # повторное удаление — ничего


def test_write_edl_nothing_to_write(tmp_path):
    video = tmp_path / "e.mkv"
    assert edl.write_edl(video, None, None) is None
    assert not edl.has_external_edl(video)


def test_build_and_write_applies_padding_and_snaps_outro(tmp_path):
    ep = EpisodeEdl.from_path(tmp_path / "Show.S01E02.mkv", duration=1500.0)
    ep.intro = Segment(10.0, 40.0)
    ep.outro = Segment(1400.0, 1450.0)
    pad = Padding(intro_start=-2.0, intro_end=3.0, outro_start=5.0)
    p = edl.build_and_write(ep, pad, keep_first_intro=True)
    text = p.read_text(encoding="utf-8")
    assert "8.000\t43.000\t3" in text            # интро со сдвигом границ
    assert "1405.000\t1500.000\t3" in text       # титры дотянуты до конца файла


def test_build_and_write_keep_first_skips_intro(tmp_path):
    ep = EpisodeEdl.from_path(tmp_path / "Show.S01E01.mkv", duration=1500.0)
    ep.intro = Segment(10.0, 40.0)
    p = edl.build_and_write(ep, Padding(), keep_first_intro=True)
    assert p is None                             # интро скрыто, больше писать нечего


# -------------------------------------------------------------- read_edl --

def test_read_edl_roundtrip(tmp_path):
    video = tmp_path / "Show.S01E02.mkv"
    edl.write_edl(video, Segment(5.0, 35.5), Segment(1200.0, 1500.0), Segment(0.0, 12.0))
    recap, intro, outro = edl.read_edl(video)
    assert (recap.start, recap.end) == (0.0, 12.0)
    assert (intro.start, intro.end) == (5.0, 35.5)
    assert (outro.start, outro.end) == (1200.0, 1500.0)


def test_read_edl_missing_file(tmp_path):
    assert edl.read_edl(tmp_path / "nope.mkv") == (None, None, None)


def test_read_edl_foreign_without_markers(tmp_path):
    video = tmp_path / "e.mkv"
    edl.edl_path(video).write_text("0.0\t30.0\t3\n1200.0\t1290.0\t3\n", encoding="utf-8")
    recap, intro, outro = edl.read_edl(video)
    assert recap is None
    assert (intro.start, intro.end) == (0.0, 30.0)      # стартует с нуля → интро
    assert (outro.start, outro.end) == (1200.0, 1290.0)  # самый поздний → титры


def test_read_edl_skips_garbage_lines(tmp_path):
    video = tmp_path / "e.mkv"
    edl.edl_path(video).write_text(
        "## Intro\nabc\tdef\t3\n10.0\t40.0\t3\n50.0\t20.0\t3\n", encoding="utf-8")
    recap, intro, outro = edl.read_edl(video)
    # битые строки (не числа, end <= start) пропускаются, маркер доживает
    # до первой валидной строки
    assert recap is None and outro is None
    assert (intro.start, intro.end) == (10.0, 40.0)


# ------------------------------------------------------ выбор аудиодорожки --

@pytest.mark.parametrize("langs, prefer, expected", [
    (["rus", "eng"], None, 1),          # авто: первая не-дубляж
    (["rus", "jpn", "eng"], None, 1),
    (["eng", "rus"], "rus", 1),         # явный выбор языка
    (["rus", "eng"], "fre", 1),         # запрошенного нет — авто-логика
    (["rus", "und"], None, 1),          # запасной: любая не-дубляж
    (["rus", "ru"], None, 0),           # всё дубляж — первая
    ([], None, 0),
    (["", ""], None, 0),
])
def test_pick_audio_index(langs, prefer, expected):
    assert edl.pick_audio_index(langs, prefer) == expected


def test_build_and_write_outro_not_to_end(tmp_path):
    """Снятая галка «до конца файла» оставляет найденную границу титров."""
    video = tmp_path / "Show.S01E02.mkv"
    video.write_text("")
    ep = EpisodeEdl(video, season=1, episode=2, duration=1400.0,
                    outro=Segment(1200.0, 1340.0))

    edl.build_and_write(ep, Padding(), keep_first_intro=False, outro_to_end=False)
    assert "1200.000\t1340.000" in video.with_suffix(".edl").read_text("utf-8")

    # с галкой (по умолчанию) — тянем до длительности, сцена после титров не важна
    edl.build_and_write(ep, Padding(), keep_first_intro=False)
    assert "1200.000\t1400.000" in video.with_suffix(".edl").read_text("utf-8")


def test_final_outro_applies_outro_end_padding():
    ep = EpisodeEdl(Path("Show.S01E02.mkv"), duration=1400.0,
                    outro=Segment(1200.0, 1340.0))
    seg = edl.final_outro(ep, Padding(outro_start=-5.0, outro_end=10.0), outro_to_end=False)
    assert (seg.start, seg.end) == (1195.0, 1350.0)
    assert edl.final_outro(EpisodeEdl(Path("x.mkv")), Padding()) is None


# ------------------------------------------- поиск внешних инструментов --

def test_find_tool_falls_back_to_brew_dirs(monkeypatch, tmp_path):
    """PATH урезан (запуск из Dock/Finder) — инструмент ищется в папках brew."""
    fake = tmp_path / "fpcalc"
    fake.write_text("")
    monkeypatch.setattr(edl.shutil, "which", lambda *_: None)
    monkeypatch.setattr(edl, "_TOOL_DIRS", [str(tmp_path)])
    assert edl._find_tool("fpcalc") == str(fake)
    assert edl._find_tool("ffmpeg") is None


def test_install_hint_lists_only_missing(monkeypatch):
    monkeypatch.setattr(edl.os, "name", "posix")
    assert edl.install_hint([]) == ""
    assert "brew install chromaprint" in edl.install_hint(["fpcalc"])
    assert "brew install chromaprint ffmpeg" in edl.install_hint(["fpcalc", "ffmpeg"])


# ------------------------------------------------- кэш отпечатков --

def _fake_np_fp():
    np = pytest.importorskip("numpy")
    return np.array([1, 2, 3, 4294967295], dtype=np.uint32)


def test_cache_roundtrip(tmp_path):
    fp = _fake_np_fp()
    edl._cache_store(tmp_path, "abc", fp, 240.0)
    got = edl._cache_load(tmp_path, "abc")
    assert got is not None
    assert list(got[0]) == list(fp) and got[1] == 240.0


def test_cache_miss_and_garbage(tmp_path):
    assert edl._cache_load(tmp_path, "нет-такого") is None
    (tmp_path / "bad.json").write_text("не json", encoding="utf-8")
    assert edl._cache_load(tmp_path, "bad") is None


def test_cache_key_follows_file_and_window(tmp_path):
    video = tmp_path / "Show.S01E01.mkv"
    video.write_text("a")
    base = edl._cache_key(video, 240.0, False, 0)
    assert base is not None
    assert base == edl._cache_key(video, 240.0, False, 0)          # тот же файл — тот же ключ
    assert base != edl._cache_key(video, 120.0, False, 0)          # другое окно
    assert base != edl._cache_key(video, 240.0, True, 0)           # другой край файла
    assert base != edl._cache_key(video, 240.0, False, 1)          # другая дорожка
    video.write_text("другое содержимое")                          # файл изменился
    assert base != edl._cache_key(video, 240.0, False, 0)
    assert edl._cache_key(tmp_path / "нет.mkv", 240.0, False, 0) is None


def test_prune_cache_by_age(tmp_path):
    import os, time
    fresh, stale = tmp_path / "fresh.json", tmp_path / "stale.json"
    for f in (fresh, stale):
        f.write_text("{}", encoding="utf-8")
    old = time.time() - 100 * 86400
    os.utime(stale, (old, old))
    assert edl.prune_cache(tmp_path, ttl_days=90) == 1
    assert fresh.exists() and not stale.exists()
    assert edl.prune_cache(tmp_path / "нет-каталога") == 0


# ----------------------------------------- кадры для проверки границ --

def test_frame_times_centred_window():
    """Окно смещено вперёд: после границы важнее, чем до неё."""
    t = edl.frame_times(29.0, 3533.0, count=8, step=2.0)
    assert t == [25.0, 27.0, 29.0, 31.0, 33.0, 35.0, 37.0, 39.0]
    assert 29.0 in t


def test_frame_times_shifts_window_at_edges():
    """У краёв окно съезжает целиком — иначе кадры дублировались бы."""
    start = edl.frame_times(0.0, 3533.0, count=8, step=2.0)
    assert start == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0]
    assert len(set(start)) == 8

    end = edl.frame_times(3530.0, 3533.0, count=8, step=2.0)
    assert len(set(end)) == 8
    assert max(end) < 3533.0

    # Отступ назад — треть кадров, поэтому у короткой полосы он меньше.
    assert edl.frame_times(29.0, None, count=4, step=2.0) == [27.0, 29.0, 31.0, 33.0]


def test_grab_frames_collects_and_reports(monkeypatch):
    monkeypatch.setattr(edl, "grab_frame", lambda ff, p, at, w: None if at == 27.0 else b"png")
    seen = []
    out = edl.grab_frames("ffmpeg", "x.mkv", [25.0, 27.0, 29.0],
                          on_frame=lambda i, png: seen.append((i, png)))
    assert out == [b"png", None, b"png"]
    assert sorted(i for i, _ in seen) == [0, 1, 2]


def test_grab_frames_stops_on_request(monkeypatch):
    monkeypatch.setattr(edl, "grab_frame", lambda *a: b"png")
    out = edl.grab_frames("ffmpeg", "x.mkv", [1.0, 2.0], stop=lambda: True)
    assert out == [None, None]


# ---------------------------------------------- открытие в плеере --

def test_find_player_prefers_bundle_over_path(monkeypatch, tmp_path):
    """CLI внутри .app бандла в PATH не виден, но запустить его можно."""
    bundle = tmp_path / "iina-cli"
    bundle.write_text("")
    monkeypatch.setattr(edl, "_PLAYERS", (
        ("IINA", (str(bundle),), lambda t: [f"--mpv-start={t:.3f}"]),
        ("mpv", ("mpv",), lambda t: [f"--start={t:.3f}"]),
    ))
    monkeypatch.setattr(edl.shutil, "which", lambda *_: "/usr/bin/mpv")
    name, path, args = edl.find_player()
    assert name == "IINA" and path == str(bundle)
    assert args(35.0) == ["--mpv-start=35.000"]


def test_find_player_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(edl, "_PLAYERS", (
        ("IINA", ("/нет/такого/iina-cli",), lambda t: []),
        ("mpv", ("mpv",), lambda t: [f"--start={t:.3f}"]),
    ))
    monkeypatch.setattr(edl.shutil, "which", lambda n: "/usr/bin/mpv" if n == "mpv" else None)
    assert edl.find_player()[0] == "mpv"


def test_open_in_player_without_any(monkeypatch):
    monkeypatch.setattr(edl, "find_player", lambda: None)
    assert edl.open_in_player("x.mkv", 10.0) is None


def test_open_in_player_builds_command(monkeypatch):
    seen = {}
    monkeypatch.setattr(edl, "find_player",
                        lambda: ("mpv", "/usr/bin/mpv", lambda t: [f"--start={t:.3f}"]))
    monkeypatch.setattr(edl.subprocess, "Popen",
                        lambda args, **kw: seen.setdefault("args", args))
    assert edl.open_in_player("/tmp/Show.mkv", -5.0) == "mpv"
    assert seen["args"] == ["/usr/bin/mpv", "--start=0.000", "/tmp/Show.mkv"]  # отрицательное → 0
