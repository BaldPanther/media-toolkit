import struct

import pytest

import swarm
from swarm import nnm as real_nnm
from swarm import rutracker as real_rutracker
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


def test_peer_count_from_compact_list():
    body = b"d8:intervali3266e12:min intervali3266e5:peers30:" + bytes(30) + b"e"
    assert swarm._peer_count(body) == 5
    assert swarm._peer_count(b"d8:intervali60ee") is None


def test_rutracker_counts_first_and_caches(monkeypatch):
    calls = []

    def ask(h, count):
        calls.append((h[0], count))
        return {"a": swarm.Tracked(True, 5 if count else None), "b": swarm.Tracked(False),
                "c": None}[h[0]]

    monkeypatch.setattr(swarm, "_ask_rutracker", ask)
    monkeypatch.setattr(swarm, "RUTRACKER_COUNT", 1)
    H3 = "c" * 40
    assert real_rutracker([H1, H2, H3]) == {H1: swarm.Tracked(True, 5), H2: swarm.Tracked(False)}
    assert sorted(calls) == [("a", True), ("b", False), ("c", False)]
    calls.clear()
    real_rutracker([H1, H2])                             # из кэша, кроме неответившего
    assert calls == []
    # Знал только «знает ли» — а теперь нужно и число участников: спросить заново.
    monkeypatch.setattr(swarm, "RUTRACKER_COUNT", 2)
    swarm._remember("rutracker", {H2: swarm.Tracked(True)})
    assert real_rutracker([H1, H2])[H2] == swarm.Tracked(False)
    assert calls == [("b", True)]


def test_announce_started_then_stopped(monkeypatch):
    sent = []

    def get(url, timeout=None):
        sent.append(url.split("event=")[1])
        return b"d8:intervali60e5:peers12:" + bytes(12) + b"e" if "started" in url else b"d8:intervali60ee"

    monkeypatch.setattr(swarm.net, "get_bytes", get)
    left = []
    monkeypatch.setattr(swarm._leaving, "submit", lambda fn, *a: left.append(a) or fn(*a))
    assert swarm._ask_rutracker(H1, True) == swarm.Tracked(True, 2)
    assert sent == ["started", "stopped"] and left == [(H1,)]   # сразу «ушёл», не дожидаясь
    sent.clear()
    assert swarm._ask_rutracker(H1, False) == swarm.Tracked(True)
    assert sent == ["stopped"]                           # только «знает ли» — не подключаясь


def test_nnm_reply_unknown_hash_is_none():
    body = (b"d5:filesd20:" + bytes.fromhex(H1) + b"d8:completei12e10:downloadedi99e"
            b"10:incompletei2eeee")
    assert swarm._nnm_reply(body, [H1, H2]) == {H1: 12, H2: None}
    assert swarm._bdecode(b"l4:spami-3ee") == ([b"spam", -3], 12)


def test_nnm_batches_and_skips_failed(monkeypatch):
    asked = []

    def ask(part):
        asked.append(len(part))
        return {} if part[0] == "c" * 40 else {h: 1 for h in part}

    monkeypatch.setattr(swarm, "_ask_nnm", ask)
    monkeypatch.setattr(swarm, "NNM_BATCH", 2)
    got = real_nnm([H1, H2, "c" * 40])
    assert sorted(asked) == [1, 2] and got == {H1: 1, H2: 1}     # пачка «c» не ответила
