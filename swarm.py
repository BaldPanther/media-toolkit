"""Живы ли раздачи: сиды прямо сейчас — у самих трекеров, а не из памяти источника.

jac.red помнит сиды на день, когда обходил тему, — у старых тем это месяцы назад,
а magnet за это время мог устареть (тему перезалили, хеш у раздачи новый). Два
быстрых вопроса, без логинов:

- Открытые трекеры (opentor — трекер rutor, opentrackr, stealth): «сколько сидов»
  (scrape, BEP 15) — одним UDP-пакетом на десятки раздач, доли секунды. Раздачи
  rutracker и kinozal они видят плохо: ноль у них — ещё не «мёртвая».
- Трекер rutracker (пускает без входа — им пользуются magnet-ссылки rutracker):
  числа не говорит, но про устаревший хеш отвечает «Torrent not registered».
  Запрос — «ушёл с раздачи» (event=stopped): в список участников нас не вносит.

И трекеры для magnet: у kinozal и nnm-club на jac.red в ссылке нет ни одного, и
qBittorrent ищет людей только через DHT — список файлов идёт минутами. Одна раздача
(один хеш) на разных трекерах — одна толпа: чем больше трекеров в ссылке, тем больше
её участников qBittorrent найдёт.

Только stdlib.
"""
from __future__ import annotations

import random
import socket
import struct
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import net

# Дописываются в каждую magnet-ссылку, что программа отдаёт qBittorrent. rutracker —
# анонимный вход его magnet-ссылок: kinozal и nnm-club часто держат те же торренты.
EXTRA_TRACKERS = (
    "http://bt4.t-ru.org/ann?magnet",
    "http://bt2.t-ru.org/ann?magnet",
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
TIMEOUT = 4
UDP_TIMEOUT = 2          # открытые трекеры отвечают за доли секунды; молчат — пропуск
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
# rutracker: знает ли он хеш
# --------------------------------------------------------------------------- #

def _registered_reply(body: bytes) -> bool | None:
    if b"not registered" in body.lower():
        return False
    if b"failure reason" in body or not body.startswith(b"d"):
        return None                         # другая ошибка — не знаем
    return True


def _ask_rutracker(h: str) -> bool | None:
    url = (f"{RUTRACKER}&info_hash={urllib.parse.quote(bytes.fromhex(h))}&peer_id={_PEER}"
           f"&port=6881&uploaded=0&downloaded=0&left=0&numwant=0&compact=1&event=stopped")
    try:
        return _registered_reply(net.get_bytes(url, timeout=TIMEOUT))
    except (net.NetError, OSError, ValueError):
        return None


def registered(hashes) -> dict[str, bool]:
    """Знает ли раздачу трекер rutracker: False — «Torrent not registered» (тему
    перезалили, хеш устарел), True — знает. Не ответил — хеша в ответе нет.
    Спрашиваются первые RUTRACKER_MAX — порядок задаёт тот, кто зовёт."""
    hashes = [h for h in dict.fromkeys(hashes) if len(h) == 40][:RUTRACKER_MAX]
    out, todo = _cached("rutracker", hashes)
    if todo:
        with ThreadPoolExecutor(max_workers=12) as pool:
            fresh = {h: r for h, r in zip(todo, pool.map(_ask_rutracker, todo)) if r is not None}
        _remember("rutracker", fresh)
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
