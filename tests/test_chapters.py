"""Тесты chapters.py: разметка глав из таймингов, формат, чтение, «свои/чужие»."""
from pathlib import Path

import pytest

import chapters
import edl
from edl import EpisodeEdl, Padding, Segment

PAD = Padding()


def _ep(**kw) -> EpisodeEdl:
    ep = EpisodeEdl.from_path(Path("Show.S01E02.mkv"), duration=3533.0)
    for key, value in kw.items():
        setattr(ep, key, value)
    return ep


# ------------------------------------------------------------ build_points --

def test_points_intro_from_zero():
    points = chapters.build_points(_ep(intro=Segment(0, 102), outro=Segment(3300, 3533)),
                                   PAD, keep_first_intro=False)
    assert points == [(0.0, "Intro"), (102.0, "Episode"), (3300.0, "Credits")]


def test_points_with_recap():
    points = chapters.build_points(
        _ep(recap=Segment(0, 42), intro=Segment(42, 102), outro=Segment(3300, 3533)),
        PAD, keep_first_intro=False)
    assert points == [(0.0, "Recap"), (42.0, "Intro"), (102.0, "Episode"), (3300.0, "Credits")]


def test_points_cold_open_gets_own_chapter():
    """Заставка не с нуля — начало серии тоже должно попасть в разметку."""
    points = chapters.build_points(_ep(intro=Segment(120, 222), outro=Segment(3300, 3533)),
                                   PAD, keep_first_intro=False)
    assert points[0] == (0.0, "Cold Open")
    assert points[1] == (120.0, "Intro")


def test_points_nothing_marked_gives_nothing():
    """Одна глава на весь файл бесполезна — не размечаем вовсе."""
    assert chapters.build_points(_ep(), PAD) == []


def test_points_follow_padding_and_first_episode_rule():
    ep = _ep(season=1, episode=1, intro=Segment(0, 102), outro=Segment(3300, 3533))
    # у E01 интро оставляем видимым — главы «Intro» быть не должно
    names = [n for _, n in chapters.build_points(ep, PAD, keep_first_intro=True)]
    assert "Intro" not in names and "Credits" in names
    # отступ сезона сдвигает границу и в главах тоже
    shifted = chapters.build_points(ep, Padding(intro_end=6.0), keep_first_intro=False)
    assert (108.0, "Episode") in shifted


def test_points_merge_coincident_starts():
    """Совпали по времени — остаётся одна: глав нулевой длины не бывает."""
    points = chapters.build_points(_ep(recap=Segment(0, 0.2), intro=Segment(0, 102)),
                                   PAD, keep_first_intro=False)
    assert len(points) == len({start for start, _ in points})


# ------------------------------------------------------------- simple_text --

def test_simple_text_format():
    text = chapters.simple_text([(0.0, "Intro"), (102.5, "Episode")])
    assert text.splitlines() == [
        "CHAPTER01=00:00:00.000", "CHAPTER01NAME=Intro",
        "CHAPTER02=00:01:42.500", "CHAPTER02NAME=Episode",
    ]
    assert chapters.simple_text([]) == ""


def test_simple_text_hours():
    assert "CHAPTER01=01:05:04.250" in chapters.simple_text([(3904.25, "Credits")])


# ----------------------------------------------------- чтение и «свои/чужие» --

class _Result:
    def __init__(self, out: str, rc: int = 0):
        self.stdout = out.encode("utf-8")
        self.returncode = rc


def test_read_chapters_parses_simple_format(monkeypatch):
    monkeypatch.setattr(chapters, "_run", lambda args: _Result(
        "CHAPTER01=00:00:00.000\nCHAPTER01NAME=Intro\n"
        "CHAPTER02=00:01:42.000\nCHAPTER02NAME=Episode\n"))
    assert chapters.read_chapters("x.mkv", "mkvextract") == [(0.0, "Intro"), (102.0, "Episode")]


def test_read_chapters_on_error(monkeypatch):
    monkeypatch.setattr(chapters, "_run", lambda args: _Result("", rc=1))
    assert chapters.read_chapters("x.mkv", "mkvextract") == []


@pytest.mark.parametrize("out, expected", [
    ("", "none"),
    ("CHAPTER01=00:00:00.000\nCHAPTER01NAME=Intro\n", "ours"),
    ("CHAPTER01=00:00:00.000\nCHAPTER01NAME=Scene 1\n", "foreign"),
    # смешанный набор — чужой: заменять его целиком нельзя
    ("CHAPTER01=00:00:00.000\nCHAPTER01NAME=Intro\n"
     "CHAPTER02=00:10:00.000\nCHAPTER02NAME=Scene 2\n", "foreign"),
])
def test_state(monkeypatch, out, expected):
    monkeypatch.setattr(chapters, "_run", lambda args: _Result(out))
    assert chapters.state("x.mkv", "mkvextract") == expected


def test_write_chapters_refuses_empty(monkeypatch):
    called = []
    monkeypatch.setattr(chapters, "_run", lambda args: called.append(args) or _Result(""))
    assert chapters.write_chapters("x.mkv", [], "mkvpropedit") is False
    assert not called          # пустую разметку в файл не несём
