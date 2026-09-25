import threading

import pytest

from web import jobs


def _manager():
    changes = []
    log = jobs.Log()
    return jobs.JobManager(log, on_change=lambda: changes.append(1)), log, changes


def test_job_runs_and_finishes_done():
    mgr, log, changes = _manager()
    seen = {}

    def work(job):
        job.progress(1, 2, "первый")
        return 42

    def done(job, result):
        seen["result"] = result

    job = mgr.start("Проверка", work, on_done=done)
    assert mgr.wait()
    assert seen["result"] == 42
    assert job.status == jobs.DONE
    assert mgr.status()["busy"] is False
    assert mgr.status()["last"]["id"] == job.id
    assert len(changes) >= 2          # старт и конец


def test_second_job_while_busy_is_refused():
    mgr, _, _ = _manager()
    gate = threading.Event()
    mgr.start("Долгая", lambda job: gate.wait(5))
    with pytest.raises(jobs.Busy) as e:
        mgr.start("Вторая", lambda job: None)
    assert "Долгая" in str(e.value)
    with pytest.raises(jobs.Busy):
        mgr.ensure_idle()
    gate.set()
    assert mgr.wait()
    mgr.ensure_idle()


def test_cancel_marks_job_cancelled():
    mgr, _, _ = _manager()
    started = threading.Event()

    def work(job):
        started.set()
        while not job.cancelled():
            job.cancel_event.wait(0.01)

    job = mgr.start("Отменяемая", work)
    started.wait(5)
    assert mgr.status()["job"]["cancelling"] is False
    assert mgr.cancel() is True
    assert mgr.wait()
    assert job.status == jobs.CANCELLED
    assert mgr.cancel() is False       # отменять уже нечего


def test_exception_fails_job_and_goes_to_log():
    mgr, log, _ = _manager()

    def work(job):
        raise OSError("нет доступа")

    job = mgr.start("Скан", work)
    assert mgr.wait()
    assert job.status == jobs.FAILED
    assert job.error == "нет доступа"
    assert any("нет доступа" in line["text"] for line in log.since(0))


def test_log_since_returns_only_new_lines():
    log = jobs.Log(limit=3)
    for i in range(5):
        log.add(f"строка {i}")
    lines = log.since(0)
    assert [x["text"] for x in lines] == ["строка 2", "строка 3", "строка 4"]
    assert [x["text"] for x in log.since(lines[1]["seq"])] == ["строка 4"]
    log.add("а\nб")
    assert [x["text"] for x in log.since(log.last_seq - 2)] == ["а", "б"]
