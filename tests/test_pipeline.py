"""Тесты pipeline.py: серия проходит обрезку, .edl и главы, прежде чем взяться за
следующую, — и переживает обрыв сети на любом шаге.

Сами обрезка и главы подменены: их проверяют test_trim.py и test_chapters.py.
Здесь — порядок шагов и то, с какой длительностью пишется .edl.
"""
from types import SimpleNamespace

import pytest

import chapters
import core
import edl
import pipeline
import trim

# Altered Carbon S01E04: до обрезки 2977.237 с, после — 2886.816.
OLD, NEW = 2977.237, 2886.816
TOOLS = SimpleNamespace(mkvmerge="mkvmerge")
PAD = edl.Padding()


@pytest.fixture
def ep(tmp_path):
    path = tmp_path / "Altered Carbon - S01E04 - Force of Evil.mkv"
    path.write_bytes(b"mkv")
    e = edl.EpisodeEdl.from_path(path, duration=OLD)
    e.intro = edl.Segment(0.0, 29.0)
    e.outro = edl.Segment(2790.237, OLD)
    return e


@pytest.fixture
def steps(monkeypatch):
    """Подменяет обрезку, скан и главы; пишет, в каком порядке шли шаги."""
    log = SimpleNamespace(order=[], points=None,
                          trim=trim.TrimResult(True, "хвост 90 с отрезан", cut=NEW,
                                               old_duration=OLD, new_duration=NEW),
                          fresh=None)

    def fake_trim(path, tools, progress=None, cancel=None, waiting=None):
        log.order.append("trim")
        return log.trim

    def fake_scan(mkvmerge, path):
        return log.fresh or core.MkvFile(path, duration=NEW, chapters=3)

    real_write = edl.build_and_write

    def spy_write(e, pad, keep, to_end=True):
        log.order.append("edl")
        return real_write(e, pad, keep, to_end)

    def fake_chapters(path, points, propedit):
        log.order.append("chapters")
        log.points = points
        return True

    monkeypatch.setattr(trim, "trim_file_retrying", fake_trim)
    monkeypatch.setattr(core, "scan_file", fake_scan)
    monkeypatch.setattr(edl, "build_and_write", spy_write)
    monkeypatch.setattr(chapters, "write_chapters", fake_chapters)
    monkeypatch.setattr(trim, "NET_POLL_S", 0)
    return log


def _outro_line(ep):
    return edl.edl_path(ep.path).read_text("utf-8").strip().splitlines()[-1]


def test_trimmed_episode_gets_edl_and_chapters_right_away(ep, steps):
    run = pipeline.run_episode(ep, PAD, False, True, tools=TOOLS, propedit="mkvpropedit")
    assert steps.order == ["trim", "edl", "chapters"]
    assert _outro_line(ep) == f"2790.237\t{NEW:.3f}\t3"      # конец титров — новый конец файла
    assert run.edl == edl.edl_path(ep.path) and not run.edl_error
    assert run.chapters == len(steps.points) and not run.chapters_failed
    assert "Stinger" not in [name for _, name in steps.points]
    assert run.fresh.duration == NEW
    assert ep.duration == OLD                     # саму серию обновляет главный поток


def test_new_duration_from_trim_when_rescan_failed(ep, steps):
    """Сеть пропала сразу после замены файла — скан не прошёл."""
    steps.fresh = core.MkvFile(ep.path, error="mkvmerge код 2")
    pipeline.run_episode(ep, PAD, False, True, tools=TOOLS)
    assert _outro_line(ep) == f"2790.237\t{NEW:.3f}\t3"


def test_without_tail_only_edl(ep, steps):
    run = pipeline.run_episode(ep, PAD, False, True)
    assert steps.order == ["edl"]
    assert _outro_line(ep) == f"2790.237\t{OLD:.3f}\t3"
    assert run.trim is None and run.chapters is None


def test_trim_cancelled_writes_nothing(ep, steps):
    steps.trim = trim.TrimResult(False, "отменено", cancelled=True)
    run = pipeline.run_episode(ep, PAD, False, True, tools=TOOLS, propedit="mkvpropedit")
    assert run.cancelled
    assert steps.order == ["trim"]
    assert not edl.edl_path(ep.path).exists()


def test_trim_failed_edl_by_untouched_file(ep, steps):
    """Обрезка не сошлась — файл прежний, и .edl пишется по прежней длине."""
    steps.trim = trim.TrimResult(False, "не сошлось с оригиналом: вложения не совпали")
    run = pipeline.run_episode(ep, PAD, False, True, tools=TOOLS)
    assert steps.order == ["trim", "edl"]
    assert _outro_line(ep) == f"2790.237\t{OLD:.3f}\t3"
    assert not run.cancelled and run.fresh is None


def _drop_once(monkeypatch, ep):
    """Шара пропадает на первой записи .edl и возвращается через две проверки."""
    real_write = edl.build_and_write
    state = {"writes": 0, "polls": 0}

    def flaky_write(e, pad, keep, to_end=True):
        state["writes"] += 1
        if state["writes"] == 1:
            raise FileNotFoundError(2, "No such file or directory")
        return real_write(e, pad, keep, to_end)

    def reachable(path):
        state["polls"] += 1
        return state["polls"] > 2

    monkeypatch.setattr(edl, "build_and_write", flaky_write)
    monkeypatch.setattr(trim, "reachable", reachable)
    return state


def test_edl_waits_for_network_after_trim(ep, steps, monkeypatch):
    """Ровно случай E04–E08: файл обрезан, а .edl записать не вышло."""
    state = _drop_once(monkeypatch, ep)
    events = []
    run = pipeline.run_episode(ep, PAD, False, True, tools=TOOLS,
                               waiting=lambda on, step: events.append((on, step)))
    assert state["writes"] == 2
    assert events == [(True, "edl"), (False, "edl")]
    assert _outro_line(ep) == f"2790.237\t{NEW:.3f}\t3"
    assert not run.edl_error and not run.cancelled


def test_edl_error_on_reachable_file_is_final(ep, steps, monkeypatch):
    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(edl, "build_and_write", denied)
    events = []
    run = pipeline.run_episode(ep, PAD, False, True,
                               waiting=lambda on, step: events.append((on, step)))
    assert run.edl is None and run.edl_error == "Permission denied"
    assert events == [] and not run.cancelled


def test_cancel_while_waiting_to_write_edl(ep, steps, monkeypatch):
    _drop_once(monkeypatch, ep)
    monkeypatch.setattr(trim, "reachable", lambda path: False)   # сеть так и не вернулась
    run = pipeline.run_episode(ep, PAD, False, True, tools=TOOLS, propedit="mkvpropedit",
                               cancel=lambda: True)
    assert run.cancelled and run.edl is None and run.edl_error
    assert "chapters" not in steps.order


def test_chapters_failure_reported(ep, steps, monkeypatch):
    monkeypatch.setattr(chapters, "write_chapters", lambda *a: False)
    run = pipeline.run_episode(ep, PAD, False, True, propedit="mkvpropedit")
    assert run.chapters is None and run.chapters_failed
    assert run.edl is not None


def test_trim_reported_before_edl(ep, steps):
    """Итог обрезки уходит в главный поток сразу, до .edl и глав."""
    pipeline.run_episode(ep, PAD, False, True, tools=TOOLS, propedit="mkvpropedit",
                         trimmed=lambda run: steps.order.append(f"reported {run.fresh.duration}"))
    assert steps.order == ["trim", f"reported {NEW}", "edl", "chapters"]
