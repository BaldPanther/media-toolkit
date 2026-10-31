import pytest

import library
import metaconf
import paths
import qbit
import search
import watchlist


class FakeBot:
    def __init__(self):
        self.sent, self.handlers = [], {}

    def on(self, prefix, handler):
        self.handlers[prefix] = handler

    def notify(self, text, buttons=None):
        self.sent.append((text, buttons))
        return len(self.sent)


class FakeAutopilot:
    def __init__(self):
        self.expected = []

    def expect(self, h, kind, tmdb_id, name, wanted=()):
        self.expected.append((h, kind, tmdb_id, list(wanted)))


def _title(last=(2, 6), nxt=(2, 7, "2026-11-07"), seasons=2, kind=library.TV):
    t = search.Title(kind=kind, tmdb_id=5, title="Тёмная материя", year=2024,
                     names=["Тёмная материя"],
                     seasons=[search.Season(1, 9, 2024), search.Season(2, 10, 2026),
                              search.Season(3, 0, None)][:seasons])
    t.last_aired, t.next_air = last, nxt
    return t


def _rel(h, episodes, seasons=(2,), quality=1080, seeds=20, year=2026):
    return search.Release(hash=h * 40, title=f"Тёмная материя {h}", magnet=f"magnet:?xt=urn:btih:{h * 40}",
                          seeds=seeds, quality=quality, seasons=list(seasons), year=year,
                          types=["serial"], episodes=list(episodes),
                          trackers=[["rutor", f"http://rutor.info/t/{h}"]])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "_LEGACY_DIR", tmp_path / "legacy")
    metaconf.save_settings(metaconf.Settings(tmdb_key="k", tv_roots=[str(tmp_path / "tv")]))
    state = {"title": _title(), "releases": [], "have": {1: 9}}
    monkeypatch.setattr(search, "title_info", lambda kind, tmdb_id, key: state["title"])
    monkeypatch.setattr(search, "find", lambda title, sources=None, log=None: list(state["releases"]))
    monkeypatch.setattr(search, "in_library", lambda title, roots: ("/tv/x", dict(state["have"])))
    bot, ap = FakeBot(), FakeAutopilot()
    w = watchlist.Watcher(bot=bot, autopilot=ap)
    return state, w, bot, ap


def test_title_aired_and_complete():
    t = _title()
    assert t.aired(1) == 9 and t.aired(2) == 6 and not t.season_complete(2)
    t.last_aired, t.next_air = (2, 10), (0, 0, "")
    assert t.season_complete(2)


def test_season_mode_waits_then_offers_once(env):
    state, w, bot, _ = env
    item = w.add(library.TV, 5, "Тёмная материя (2024)")
    w.tick(now=10 ** 9)
    assert "жду сезон 2: вышло 6 из 10, следующая серия 2026-11-07" == item.status
    assert bot.sent == []
    # Сезон вышел, но полной раздачи нет — ещё ждём.
    state["title"] = _title(last=(2, 10), nxt=(0, 0, ""))
    state["releases"] = [_rel("a", [1, 8, 10])]
    w.check_now()
    w.tick(now=10 ** 9 + 1)
    assert "полной раздачи пока нет" in item.status and bot.sent == []
    # Появилась полная — одно сообщение с кнопками.
    state["releases"].append(_rel("b", [1, 10, 10]))
    w.check_now()
    w.tick(now=10 ** 9 + 2)
    assert len(bot.sent) == 1 and "сезон 2 вышел целиком" in bot.sent[0][0]
    assert bot.sent[0][1][0][0][1] == f"wl:{item.id}:{'b' * 12}"
    w.check_now()
    w.tick(now=10 ** 9 + 3)
    assert len(bot.sent) == 1             # о том же событии — один раз


def test_season_mode_all_in_library(env):
    state, w, bot, _ = env
    state["have"] = {1: 9, 2: 10}
    item = w.add(library.TV, 5, "x")
    w.tick(now=10 ** 9)
    assert "все сезоны уже в медиатеке" in item.status


def test_episode_mode_offers_new_episodes(env):
    state, w, bot, _ = env
    state["have"] = {1: 9, 2: 5}
    state["releases"] = [_rel("c", [1, 6, 10]), _rel("d", [1, 5, 10])]
    item = w.add(library.TV, 5, "x")
    w.set_mode(item.id, watchlist.EPISODE)
    w.tick(now=10 ** 9)
    assert "в раздаче уже 6 сер., у вас 5" in item.status
    assert [o["hash"] for o in item.offer] == ["c" * 40]
    assert "у вас 5" in bot.sent[0][0]


def test_new_seasons_mode_announces(env):
    state, w, bot, _ = env
    item = w.add(library.TV, 5, "x", have_seasons=2)
    w.set_mode(item.id, watchlist.NEW_SEASONS)
    state["have"] = {1: 9, 2: 10}
    w.tick(now=10 ** 9)
    assert bot.sent == [] and item.seasons_known == 2
    state["title"] = _title(seasons=3)
    w.check_now()
    w.tick(now=10 ** 9 + 1)
    assert "анонсирован сезон 3" in bot.sent[0][0]


def test_movie_waits_for_good_quality(env):
    state, w, bot, _ = env
    state["title"] = _title(kind=library.MOVIE, seasons=0)
    state["releases"] = [_rel("e", [], seasons=(), quality=480, year=2024)]
    item = w.add(library.MOVIE, 5, "Фильм (2024)")
    assert item.mode == watchlist.MOVIE
    state["releases"][0].types = ["movie"]
    w.tick(now=10 ** 9)
    assert item.status == "раздач в хорошем качестве пока нет"
    hd = _rel("f", [], seasons=(), quality=2160, year=2024)
    hd.types = ["movie"]
    state["releases"].append(hd)
    w.check_now()
    w.tick(now=10 ** 9 + 1)
    assert "появились раздачи" in bot.sent[0][0]


def test_download_button_adds_and_hands_to_autopilot(env, monkeypatch):
    state, w, bot, ap = env
    added = []

    class FakeQ:
        def add(self, url, category=""):
            added.append((url, category))

    qbit.save_settings(qbit.Settings(url="http://q"))
    monkeypatch.setattr(qbit, "connect", lambda s: FakeQ())
    state["title"] = _title(last=(2, 10), nxt=(0, 0, ""))
    state["releases"] = [_rel("g", [1, 10, 10], seasons=(1, 2))]
    item = w.add(library.TV, 5, "x")
    w.tick(now=10 ** 9)
    assert bot.handlers["wl"](f"{item.id}:{'g' * 12}") == "Скачиваю."
    assert added == [("magnet:?xt=urn:btih:" + "g" * 40, "tv")]
    assert ap.expected == [("g" * 40, library.TV, 5, [2])]    # из сборника — только сезон 2
    assert "устарело" in bot.handlers["wl"](f"{item.id}:zzz")


def test_list_survives_restart(env):
    state, w, bot, _ = env
    item = w.add(library.TV, 5, "x")
    assert watchlist.Watcher().items[item.id].tmdb_id == 5
    w.remove(item.id)
    assert watchlist.Watcher().items == {}


def test_offer_skips_hdr_without_checkbox_and_prefers_4k(env):
    state, w, bot, _ = env
    state["title"] = _title(last=(2, 10), nxt=(0, 0, ""))
    hdr = _rel("h", [1, 10, 10], quality=2160, seeds=500)
    hdr.hdr = True
    state["releases"] = [_rel("a", [1, 10, 10]), _rel("k", [1, 10, 10], quality=2160), hdr]
    item = w.add(library.TV, 5, "Тёмная материя (2024)")
    w.tick(now=10 ** 9)
    # Кнопка качества поиска (по умолчанию 4K) слежению не мешает: 1080p тоже в списке.
    assert [o["hash"][0] for o in item.offer] == ["k", "a"]
    search.save_prefs(search.Prefs(hdr=True))
    w.check_now()
    w.tick(now=10 ** 9 + 1)
    assert [o["hash"][0] for o in item.offer] == ["h", "k", "a"]
