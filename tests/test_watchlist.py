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
        self.expected, self.added = [], []

    def add(self, magnet, h, kind, tmdb_id, name, wanted=(), pages=(), seasons=()):
        self.added.append((magnet, list(pages)))
        self.seasons = list(seasons)
        self.expect(h, kind, tmdb_id, name, wanted)
        return "Скачиваю"

    def replaced_by(self, pages, h):
        return None

    def present(self):
        return getattr(self, "hashes", set())

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


def test_movie_waits_for_good_quality(env, monkeypatch):
    state, w, bot, _ = env
    monkeypatch.setattr(search, "in_library", lambda title, roots: ("", {}))   # ещё не скачан
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
    qbit.save_settings(qbit.Settings(url="http://q"))
    state["title"] = _title(last=(2, 10), nxt=(0, 0, ""))
    state["releases"] = [_rel("g", [1, 10, 10], seasons=(1, 2))]
    item = w.add(library.TV, 5, "x")
    w.tick(now=10 ** 9)
    assert bot.handlers["wl"](f"{item.id}:{'g' * 12}") == "Скачиваю."
    assert ap.added == [("magnet:?xt=urn:btih:" + "g" * 40, ["http://rutor.info/t/g"])]
    assert ap.expected == [("g" * 40, library.TV, 5, [2])]    # из сборника — только сезон 2
    assert ap.seasons == [1, 2]                                 # сезоны раздачи — для раскладки
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


def test_offer_ignores_english_releases(env):
    state, w, bot, _ = env
    state["title"] = _title(last=(2, 10), nxt=(0, 0, ""))
    eng = _rel("x", [1, 10, 10])
    eng.title = "Dark.Matter.2024.S02.1080p"        # без кириллицы и озвучек
    state["releases"] = [eng]
    item = w.add(library.TV, 5, "Тёмная материя (2024)")
    w.tick(now=10 ** 9)
    assert item.offer == [] and bot.sent == []


def test_pinned_release_only_and_reports_new_episodes(env):
    state, w, bot, _ = env
    state["title"] = _title(last=(2, 6))
    state["have"] = {1: 9, 2: 3}
    mine = _rel("m", [1, 4, 10], seeds=0)          # своя — даже без сидов
    other = _rel("o", [1, 6, 10], seeds=50)
    state["releases"] = [mine, other]
    item = w.pin_release(library.TV, 5, "Тёмная материя (2024)", "", 1,
                         ["http://rutor.info/t/m"], mine.title)
    assert item.mode == watchlist.EPISODE and item.pin == ["http://rutor.info/t/m"]
    w.tick(now=10 ** 9)
    assert [o["hash"] for o in item.offer] == ["m" * 40]
    assert item.status == "сезон 2: в вашей раздаче уже 4 сер., у вас 3"
    assert "в вашей раздаче уже 4" in bot.sent[0][0]
    w.unpin(item.id)
    w.tick(now=10 ** 9 + 1)
    assert item.offer[0]["hash"] == "o" * 40      # снова лучшие


def test_conditions_quality_and_voice(env):
    state, w, bot, _ = env
    state["title"] = _title(last=(2, 10), nxt=(0, 0, ""))
    hd = _rel("h", [1, 10, 10], quality=1080, seeds=100)
    uhd = _rel("u", [1, 10, 10], quality=2160, seeds=5)
    uhd.voices = ["LostFilm"]
    state["releases"] = [hd, uhd]
    item = w.add(library.TV, 5, "x")
    w.set_filter(item.id, quality=2160)
    w.tick(now=10 ** 9)
    assert [o["hash"] for o in item.offer] == ["u" * 40]
    assert item.voices == ["LostFilm"]
    w.set_filter(item.id, quality=0, voice="Кубик в Кубе")
    w.tick(now=10 ** 9 + 1)
    assert item.offer == [] and "(Кубик в Кубе)" in item.status


def test_movie_in_library_waits_only_for_conditions(env):
    state, w, bot, _ = env
    state["title"] = _title(kind=library.MOVIE, seasons=0)
    state["releases"] = [_rel("h", [], seasons=(), quality=1080, year=2024),
                         _rel("u", [], seasons=(), quality=2160, year=2024)]
    for r in state["releases"]:
        r.types = ["movie"]
    item = w.add(library.MOVIE, 5, "Фильм")
    w.tick(now=10 ** 9)
    assert item.offer == [] and "уже в медиатеке" in item.status   # in_library даёт папку
    w.set_filter(item.id, quality=2160)
    w.tick(now=10 ** 9 + 1)
    assert [o["hash"] for o in item.offer] == ["u" * 40] and "(4K)" in item.status


def test_condition_bigger_than_library(env, monkeypatch):
    state, w, bot, _ = env
    state["title"] = _title(last=(2, 10), nxt=(0, 0, ""))
    monkeypatch.setattr(search, "library_version",
                        lambda kind, folder, extras="Extras", probe=True: search.Owned(size=1000, episodes=1))
    small = _rel("s", [1, 10, 10], seeds=100)
    small.size = 12_000                  # 1200 на серию — как у вас
    big = _rel("b", [1, 10, 10], seeds=5)
    big.size = 20_000                    # 2000 на серию — вдвое крупнее
    state["releases"] = [small, big]
    item = w.add(library.TV, 5, "x")
    w.set_filter(item.id, bigger=True)
    w.tick(now=10 ** 9)
    assert [o["hash"] for o in item.offer] == ["b" * 40]


def test_release_already_in_qbittorrent_not_offered_again(env):
    state, w, bot, ap = env
    state["title"] = _title(last=(2, 6))
    state["have"] = {1: 9, 2: 4}
    # На трекере название уже «1-5», а magnet у источника прежний — тот, что качается.
    mine = _rel("m", [1, 5, 10])
    state["releases"] = [mine]
    ap.hashes = {mine.hash}
    item = w.pin_release(library.TV, 5, "Тёмная материя (2024)", "", 1,
                         ["http://rutor.info/t/m"], mine.title)
    w.tick(now=10 ** 9)
    assert item.offer == [] and bot.sent == []
    assert "уже в qBittorrent" in item.status
    # Пришла настоящая новая версия — другой хеш: сообщение и «Обновить».
    state["releases"] = [_rel("n", [1, 5, 10])]
    state["releases"][0].trackers = [["rutor", "http://rutor.info/t/m"]]
    w.check_now()
    w.tick(now=10 ** 9 + 1)
    assert [o["hash"] for o in item.offer] == ["n" * 40] and len(bot.sent) == 1


def test_watch_dub_condition_any_studio(env):
    state, w, bot, _ = env
    state["title"] = _title(last=(2, 10), nxt=(0, 0, ""))
    amateur = _rel("a", [1, 10, 10], seeds=100)
    amateur.title = "Тёмная материя / ЛМ (AniLibria)"
    dub = _rel("d", [1, 10, 10], seeds=3)
    dub.title = "Тёмная материя / ДБ (DEEP)"
    state["releases"] = [amateur, dub]
    item = w.add(library.TV, 5, "x")
    w.set_filter(item.id, voice="Дубляж")
    w.tick(now=10 ** 9)
    assert [o["hash"] for o in item.offer] == ["d" * 40]


def test_any_russian_then_dub_two_messages(env, monkeypatch):
    state, w, bot, _ = env
    monkeypatch.setattr(search, "in_library", lambda title, roots: ("", {}))
    state["title"] = _title(kind=library.MOVIE, seasons=0)
    subs = _rel("s", [], seasons=(), quality=1080, year=2024, seeds=50)
    subs.title, subs.types = "Фильм / JAP + Sub Rus", ["movie"]
    state["releases"] = [subs]
    item = w.add(library.MOVIE, 5, "Фильм")
    w.set_filter(item.id, voice="Любая русская")
    w.tick(now=10 ** 9)
    assert item.offer == [] and bot.sent == []              # одни субтитры — не озвучка
    amateur = _rel("a", [], seasons=(), quality=1080, year=2024, seeds=5)
    amateur.title, amateur.types = "Фильм / ЛМ (Dream Cast)", ["movie"]
    state["releases"].append(amateur)
    w.check_now()
    w.tick(now=10 ** 9 + 1)
    assert len(bot.sent) == 1 and "дубляжа пока нет" in bot.sent[0][0]
    other = _rel("o", [], seasons=(), quality=1080, year=2024, seeds=9)
    other.title, other.types = "Фильм / ЛО (Кто-то)", ["movie"]
    state["releases"].append(other)
    w.check_now()
    w.tick(now=10 ** 9 + 2)
    assert len(bot.sent) == 1                                # ещё любительская — молчим
    dub = _rel("d", [], seasons=(), quality=1080, year=2024, seeds=1)
    dub.title, dub.types = "Фильм / ДБ (Мосфильм-Мастер)", ["movie"]
    state["releases"].append(dub)
    w.check_now()
    w.tick(now=10 ** 9 + 3)
    assert len(bot.sent) == 2 and "с дубляжом" in bot.sent[1][0]
    assert item.offer[0]["hash"] == "d" * 40                 # дубляж — первым
