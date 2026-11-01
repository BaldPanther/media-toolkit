import struct

import pytest

import swarm
from swarm import registered as real_registered
from swarm import scrape as real_scrape

H1, H2 = "a" * 40, "b" * 40


@pytest.fixture(autouse=True)
def _fresh_cache():
    swarm._cache.clear()


def test_with_trackers_adds_missing_only():
    m = f"magnet:?xt=urn:btih:{H1}&dn=Show&tr=udp%3A%2F%2Fopentor.net%3A6969"
    out = swarm.with_trackers(m)
    trs = swarm.trackers_of(out)
    assert out.startswith(m) and trs.count("udp://opentor.net:6969") == 1
    assert "http://bt4.t-ru.org/ann?magnet" in trs and len(trs) == len(swarm.EXTRA_TRACKERS)
    assert swarm.with_trackers("https://site/file.torrent") == "https://site/file.torrent"


def test_scrape_reply_parsed_in_request_order():
    data = struct.pack(">II", 2, 7) + struct.pack(">III", 5, 100, 2) + struct.pack(">III", 0, 3, 9)
    assert swarm._scrape_reply(data, 7, [H1, H2]) == {H1: 5, H2: 0}
    with pytest.raises(ValueError):
        swarm._scrape_reply(data, 8, [H1, H2])                  # чужой ответ
    with pytest.raises(ValueError):
        swarm._connect_reply(struct.pack(">IIQ", 3, 1, 0), 1)   # ошибка трекера


def test_scrape_answers_per_tracker_and_caches(monkeypatch):
    calls = []

    def fake(host, port, hashes):
        calls.append((host, len(hashes)))
        table = {H1: 2, H2: 0} if host == swarm.RUTOR else {H1: 7, H2: 1}
        return {h: n for h, n in table.items() if h in hashes}

    monkeypatch.setattr(swarm, "_udp_scrape", fake)
    monkeypatch.setattr(swarm, "SCRAPE", ((swarm.RUTOR, 1, 1), ("other", 2, 0)))
    got = real_scrape([H1, H2, H1, "short"])
    assert got == {H1: {swarm.RUTOR: 2, "other": 7}, H2: {"other": 1}}
    assert sorted(calls) == [(swarm.RUTOR, 1), ("other", 2)]          # opentor — одна пачка
    assert real_scrape([H1]) == {H1: got[H1]} and len(calls) == 2      # из кэша


def test_registered_reply():
    assert swarm._registered_reply(
        b"d8:intervali3016e14:failure reason22:Torrent not registerede") is False
    assert swarm._registered_reply(b"d8:intervali464e12:min intervali464e5:peers0:e") is True
    assert swarm._registered_reply(b"d14:failure reason6:bannede") is None
    assert swarm._registered_reply(b"<html>blocked</html>") is None


def test_registered_skips_unknown_answers(monkeypatch):
    monkeypatch.setattr(swarm, "_ask_rutracker", lambda h: {H1: False, H2: None}[h])
    assert real_registered([H1, H2]) == {H1: False}
