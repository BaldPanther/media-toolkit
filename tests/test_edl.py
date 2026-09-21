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
