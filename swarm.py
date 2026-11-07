"""Живы ли раздачи: сиды прямо сейчас — у самих трекеров, а не из памяти источника.

jac.red помнит сиды на день, когда обходил тему, — у старых тем это месяцы назад,
а magnet за это время мог устареть (тему перезалили, хеш у раздачи новый). Быстрые
вопросы, без логинов:

- Открытые трекеры (opentor — трекер rutor, opentrackr, stealth): «сколько сидов»
  (scrape, BEP 15) — одним UDP-пакетом на десятки раздач, доли секунды. Раздачи
  rutracker и kinozal они видят плохо: ноль у них — ещё не «мёртвая».
- Трекер rutracker (пускает без входа — им пользуются magnet-ссылки rutracker):
  числа сидов не говорит (scrape — 403), но про устаревший хеш отвечает «Torrent
  not registered». «Ушёл с раздачи» (event=stopped) — только это, в список участников
  нас не вносит. «Подключился» (started) — ещё и список участников, до 30 человек:
  у раздачи поменьше это и есть их число (сиды и качающие вместе). Сразу за ним —
  «ушёл»: в списке мы на долю секунды, как любой клиент.
- Трекер nnm-club (без входа — «ffff…» вместо личного ключа, как в его magnet):
  scrape отвечает точным числом сидов, до сотни раздач одним запросом. Хешей, которых
  он не знает, в ответе нет. Трекер kinozal без личного ключа не пускает.

И трекеры для magnet: у kinozal и nnm-club на jac.red в ссылке нет ни одного, и
qBittorrent ищет людей только через DHT — список файлов идёт минутами. Одна раздача
(один хеш) на разных трекерах — одна толпа: чем больше трекеров в ссылке, тем больше
её участников qBittorrent найдёт.

Только stdlib.
"""
from __future__ import annotations

import random
import re
import socket
import struct
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import net

# Дописываются в каждую magnet-ссылку, что программа отдаёт qBittorrent. rutracker —
# анонимный вход его magnet-ссылок: kinozal и nnm-club часто держат те же торренты.
NNM = "http://bt02.nnm-club.cc:2710/ffffffffffffffffffffffffffffffff"
EXTRA_TRACKERS = (
    "http://bt4.t-ru.org/ann?magnet",
    "http://bt2.t-ru.org/ann?magnet",
    f"{NNM}/announce",
    "udp://opentor.net:6969",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "http://retracker.local/announce",
)
RUTOR = "opentor.net"
_BATCH = 70              # хешей в одном пакете scrape — чтобы ответ влез в UDP
# (трекер, порт, сколько хешей спрашивать; 0 — все). opentor после пары пакетов
# замолкает на время — ему одну пачку: первые хеши, их порядок задаёт тот, кто зовёт.
SCRAPE = ((RUTOR, 6969, _BATCH), ("tracker.opentrackr.org", 1337, 0), ("open.stealth.si", 80, 0))
RUTRACKER = "http://bt4.t-ru.org/ann?magnet"
RUTRACKER_MAX = 120      # хешей за один поиск: самые сидовые — остальные вряд ли нужны
RUTRACKER_COUNT = 40     # у скольких из них (первых) узнавать участников — два запроса
RUTRACKER_PEERS = 30     # больше участников трекер не присылает: 30 — это «30 и больше»
NNM_BATCH = 70           # хешей в одном scrape nnm-club: 200 он уже не принимает
# Трекеры отвечают за 0,1–0,4 с; кто молчит дольше — не ответит и вовсе.
TIMEOUT = 2
UDP_TIMEOUT = 1
CACHE = 600              # секунд: фильтры поиска и слежение спрашивают те же хеши
_PEER = "-MT0100-" + "".join(random.choice("0123456789") for _ in range(12))

_cache: dict[tuple[str, str], tuple[float, object]] = {}
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# magnet
# --------------------------------------------------------------------------- #

def trackers_of(magnet: str) -> list[str]:
    query = urllib.parse.urlparse(magnet or "").query
    return urllib.parse.parse_qs(query).get("tr", [])


def with_trackers(magnet: str, extra=EXTRA_TRACKERS) -> str:
    """magnet + трекеры, которых в нём ещё нет. Хеш и имя — как были."""
    if not (magnet or "").startswith("magnet:"):
        return magnet
    have = set(trackers_of(magnet))
    add = [t for t in dict.fromkeys(extra) if t not in have]
    return magnet + "".join("&tr=" + urllib.parse.quote(t, safe="") for t in add)


# --------------------------------------------------------------------------- #
# Открытые трекеры: scrape
# --------------------------------------------------------------------------- #

def _connect_request(tid: int) -> bytes:
    return struct.pack(">QII", 0x41727101980, 0, tid)


def _connect_reply(data: bytes, tid: int) -> int:
    action, got, cid = struct.unpack(">IIQ", data[:16])
    if action != 0 or got != tid:
        raise ValueError("не тот ответ")
    return cid


def _scrape_request(cid: int, tid: int, hashes: list[str]) -> bytes:
    return struct.pack(">QII", cid, 2, tid) + b"".join(bytes.fromhex(h) for h in hashes)


def _scrape_reply(data: bytes, tid: int, hashes: list[str]) -> dict[str, int]:
    """Ответ scrape → хеш → сиды. Тройки (сиды, скачали, качают) — в порядке запроса."""
    action, got = struct.unpack(">II", data[:8])
    if action != 2 or got != tid:
        raise ValueError("не тот ответ")
    out = {}
    for i, h in enumerate(hashes):
        part = data[8 + 12 * i:20 + 12 * i]
        if len(part) < 12:
            break
        out[h] = struct.unpack(">III", part)[0]
    return out


def _udp_scrape(host: str, port: int, hashes: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(UDP_TIMEOUT)
            tid = random.getrandbits(32)
            s.sendto(_connect_request(tid), addr)
            cid = _connect_reply(s.recvfrom(2048)[0], tid)
            for i in range(0, len(hashes), _BATCH):
                part = hashes[i:i + _BATCH]
                tid = random.getrandbits(32)
                s.sendto(_scrape_request(cid, tid, part), addr)
                out.update(_scrape_reply(s.recvfrom(8 + 12 * len(part) + 64)[0], tid, part))
    except (OSError, ValueError, struct.error):
        pass                                # что успели — то и есть
    return out


def scrape(hashes) -> dict[str, dict[str, int]]:
    """Сиды на открытых трекерах сейчас: хеш → {трекер: сиды}. Складывать ответы
    нельзя — трекеры видят одних и тех же людей. Не ответил ни один — хеша нет."""
    hashes = [h for h in dict.fromkeys(hashes) if len(h) == 40]
    out, todo = _cached("scrape", hashes)
    if todo:
        fresh: dict[str, dict[str, int]] = {}

        def ask(tracker):
            host, port, limit = tracker
            return host, _udp_scrape(host, port, todo[:limit] if limit else todo)

        with ThreadPoolExecutor(max_workers=len(SCRAPE)) as pool:
            for host, part in pool.map(ask, SCRAPE):
                for h, n in part.items():
                    fresh.setdefault(h, {})[host] = n
        _remember("scrape", fresh)
        out.update(fresh)
    return out


# --------------------------------------------------------------------------- #
# rutracker: знает ли он хеш, сколько участников
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Tracked:
    """Что сказал трекер rutracker о хеше."""
    known: bool                         # False — «Torrent not registered»: тему перезалили
    peers: int | None = None            # участников сейчас (до RUTRACKER_PEERS); None — не спрашивали


_PEERS = re.compile(rb"5:peers(\d+):")


def _registered_reply(body: bytes) -> bool | None:
    if b"not registered" in body.lower():
        return False
    if b"failure reason" in body or not body.startswith(b"d"):
        return None                         # другая ошибка — не знаем
    return True


def _peer_count(body: bytes) -> int | None:
    """Участников в ответе: компактный список — по 6 байт на каждого."""
    m = _PEERS.search(body)
    return int(m.group(1)) // 6 if m else None


def _announce(h: str, event: str, numwant: int) -> bytes:
    url = (f"{RUTRACKER}&info_hash={urllib.parse.quote(bytes.fromhex(h))}&peer_id={_PEER}"
           f"&port=6881&uploaded=0&downloaded=0&left=1&numwant={numwant}&compact=1&event={event}")
    return net.get_bytes(url, timeout=TIMEOUT)


_leaving = ThreadPoolExecutor(max_workers=6, thread_name_prefix="rutracker-stopped")


def _leave(h: str) -> None:
    try:
        _announce(h, "stopped", 0)
    except (net.NetError, OSError):
        pass


def _ask_rutracker(h: str, count: bool) -> Tracked | None:
    try:
        if not count:
            known = _registered_reply(_announce(h, "stopped", 0))
            return None if known is None else Tracked(known)
        body = _announce(h, "started", 50)
        known = _registered_reply(body)
        if known:
            # Сразу «ушёл»: из списка участников — вон. Ответ не нужен — не ждём его,
            # иначе каждая раздача стоила бы два ожидания подряд.
            _leaving.submit(_leave, h)
        return None if known is None else Tracked(known, _peer_count(body) if known else None)
    except (net.NetError, OSError, ValueError):
        return None


def rutracker(hashes) -> dict[str, Tracked]:
    """Трекер rutracker о раздачах: знает ли хеш и — у первых RUTRACKER_COUNT — сколько
    у неё участников сейчас. Спрашиваются первые RUTRACKER_MAX: порядок задаёт тот,
    кто зовёт. Не ответил — хеша в ответе нет."""
    hashes = [h for h in dict.fromkeys(hashes) if len(h) == 40][:RUTRACKER_MAX]
    counted = set(hashes[:RUTRACKER_COUNT])
    out, todo = _cached("rutracker", hashes)
    # Из кэша без числа участников не годится тому, у кого его теперь хотят знать.
    todo += [h for h in list(out) if h in counted and out[h].known and out[h].peers is None]
    for h in todo:
        out.pop(h, None)
    if todo:
        with ThreadPoolExecutor(max_workers=12) as pool:
            answers = pool.map(lambda h: _ask_rutracker(h, h in counted), todo)
            fresh = {h: r for h, r in zip(todo, answers) if r is not None}
        _remember("rutracker", fresh)
        out.update(fresh)
    return out


# --------------------------------------------------------------------------- #
# nnm-club: сиды по scrape
# --------------------------------------------------------------------------- #

def _bdecode(data: bytes, i: int = 0):
    """Bencode → (значение, где кончилось). Хватает для ответа трекера."""
    c = data[i:i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c in (b"l", b"d"):
        items, i = [], i + 1
        while data[i:i + 1] != b"e":
            value, i = _bdecode(data, i)
            items.append(value)
        return (dict(zip(items[::2], items[1::2])) if c == b"d" else items), i + 1
    colon = data.index(b":", i)
    end = colon + 1 + int(data[i:colon])
    return data[colon + 1:end], end


def _nnm_reply(body: bytes, hashes: list[str]) -> dict[str, int | None]:
    """Ответ scrape → хеш → сиды; None — трекер хеша не знает (в ответе его нет)."""
    files = _bdecode(body)[0].get(b"files", {})
    out: dict[str, int | None] = {}
    for h in hashes:
        stats = files.get(bytes.fromhex(h))
        out[h] = int(stats.get(b"complete", 0)) if isinstance(stats, dict) else None
    return out


def _ask_nnm(hashes: list[str]) -> dict[str, int | None]:
    url = f"{NNM}/scrape?" + "&".join(f"info_hash={urllib.parse.quote(bytes.fromhex(h))}"
                                      for h in hashes)
    try:
        return _nnm_reply(net.get_bytes(url, timeout=TIMEOUT), hashes)
    except (net.NetError, OSError, ValueError, IndexError, AttributeError):
        return {}


def nnm(hashes) -> dict[str, int | None]:
    """Сиды на трекере nnm-club сейчас: хеш → число; None — такой раздачи он не знает.
    Не ответил — хеша в ответе нет."""
    hashes = [h for h in dict.fromkeys(hashes) if len(h) == 40]
    out, todo = _cached("nnm", hashes)
    if todo:
        parts = [todo[i:i + NNM_BATCH] for i in range(0, len(todo), NNM_BATCH)]
        fresh: dict[str, int | None] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for part in pool.map(_ask_nnm, parts):
                fresh.update(part)
        _remember("nnm", fresh)
        out.update(fresh)
    return out


# --------------------------------------------------------------------------- #
# Кэш
# --------------------------------------------------------------------------- #

def _cached(kind: str, hashes: list[str]) -> tuple[dict, list[str]]:
    now = time.time()
    out, todo = {}, []
    with _lock:
        for h in hashes:
            hit = _cache.get((kind, h))
            if hit is not None and now - hit[0] < CACHE:
                out[h] = hit[1]
            else:
                todo.append(h)
    return out, todo


def _remember(kind: str, values: dict) -> None:
    now = time.time()
    with _lock:
        for h, v in values.items():
            _cache[(kind, h)] = (now, v)
        if len(_cache) > 20000:
            _cache.clear()
