"""Тесты чистой логики core.py: идентичность дорожек, агрегация, план, команда."""
from pathlib import Path

import core
from core import MkvFile, Track


def mk_track(type="audio", name="", language="", default=False, uid=None,
             number=None, id=0, forced=False):
    return Track(id=id, uid=uid, number=number, type=type, codec="AC-3",
                 language=language, name=name, default=default, forced=forced)


def mk_file(name, tracks, error=""):
    return MkvFile(Path(name), tracks, error=error)


# --------------------------------------------------------- идентичность --

def test_audio_identity_casefold_and_language():
    a = mk_track(name="Кубик в Кубе", language="rus")
    b = mk_track(name="кубик в кубе ", language="rus")
    c = mk_track(name="Кубик в Кубе", language="eng")
    assert core.audio_identity(a) == core.audio_identity(b)
    assert core.audio_identity(a) != core.audio_identity(c)   # язык различает одноимённые


def test_audio_identity_no_name_falls_back_to_language():
    a = mk_track(name="", language="eng")
    b = mk_track(name="", language="ENG")
    assert core.audio_identity(a) == core.audio_identity(b)
    assert core.audio_identity(a) == ("lang", "eng")


def test_sub_identity_and_labels():
    s = mk_track(type="subtitles", name="Full", language="rus")
    assert core.sub_identity(s) == ("sub", "rus", "full")
    assert core.sub_label(s) == "rus — Full"
    assert core.sub_label(mk_track(type="subtitles", language="eng")) == "eng"
    assert core.audio_label(mk_track(language="")) == "без названия [und]"


# ------------------------------------------------------------ агрегация --

def make_library():
    f1 = mk_file("e1.mkv", [
        mk_track(name="Dub", language="rus", default=True, uid=11),
        mk_track(name="Original", language="eng", uid=12),
        mk_track(type="subtitles", language="rus", uid=13),
    ])
    f2 = mk_file("e2.mkv", [
        mk_track(name="Original", language="eng", default=True, uid=22),
        mk_track(name="Dub", language="rus", uid=21),
    ])
    f3 = mk_file("broken.mkv", [], error="boom")
    return [f1, f2, f3]


def test_aggregate_audio_counts_and_order():
    opts, total = core.aggregate_audio(make_library())
    assert total == 2                                    # файл с ошибкой не считается
    assert [(o.label, o.count) for o in opts] == [
        ("Dub [rus]", 2), ("Original [eng]", 2),         # при равном счёте — по алфавиту
    ]


def test_aggregate_subtitles():
    opts, total = core.aggregate_subtitles(make_library())
    assert total == 2
    assert [(o.label, o.count) for o in opts] == [("rus", 1)]


def test_group_by_audio_signature():
    files = make_library()
    groups = core.group_by_audio_signature(files)
    assert len(groups) == 2                              # порядок дорожек различается


# ------------------------------------------------------------------ план --

def test_plan_changes_audio_found_and_missing():
    files = make_library()
    audio_key = core.audio_identity(mk_track(name="Original", language="eng"))
    plans = core.plan_changes(files, audio_key, None)

    p1, p2, p3 = plans
    assert p1.audio_target.name == "Original"
    assert p1.has_changes                                # дефолт переезжает с Dub
    assert p2.audio_target.name == "Original"
    assert not p2.has_changes                            # уже стоит нужная
    assert p3.warnings and "ошибка" in p3.warnings[0]


def test_plan_changes_missing_track_warns():
    files = make_library()
    audio_key = core.audio_identity(mk_track(name="Другая студия", language="rus"))
    plans = core.plan_changes(files, audio_key, None)
    assert plans[0].warnings == ["нет выбранной аудиодорожки"]
    assert not plans[0].has_changes


def test_plan_changes_sub_disable():
    files = make_library()
    plans = core.plan_changes(files, None, core.SUB_DISABLE)
    p1 = plans[0]
    assert p1.sub_disabled
    assert [c.new_default for c in p1.sub_changes] == [False]
    assert not plans[1].sub_changes                      # у f2 субтитров нет


# --------------------------------------------------- селектор и команда --

def test_selector_prefers_uid_then_number_then_id():
    assert core._selector(mk_track(uid=777, number=3, id=1)) == "track:=777"
    assert core._selector(mk_track(uid=None, number=3, id=1)) == "track:@3"
    assert core._selector(mk_track(uid=None, number=None, id=1)) == "track:2"


def test_build_command_only_changed_tracks():
    files = make_library()
    audio_key = core.audio_identity(mk_track(name="Original", language="eng"))
    plans = core.plan_changes(files, audio_key, None)

    cmd = core.build_command("mkvpropedit.exe", plans[0])
    assert cmd == [
        "mkvpropedit.exe", str(Path("e1.mkv")),
        "--edit", "track:=11", "--set", "flag-default=0",
        "--edit", "track:=12", "--set", "flag-default=1",
    ]
    assert core.build_command("mkvpropedit.exe", plans[1]) is None   # менять нечего


# ------------------------------------------------------------ scan_folder --

def test_scan_folder_keeps_order_and_reports_progress(tmp_path, monkeypatch):
    for name in ("b.mkv", "a.mkv", "c.mkv"):
        (tmp_path / name).touch()
    monkeypatch.setattr(core, "find_tools", lambda: ("mkvmerge", "mkvpropedit"))
    monkeypatch.setattr(core, "scan_file", lambda tool, p: MkvFile(p))

    calls = []
    files = core.scan_folder(tmp_path, recursive=False,
                             progress=lambda i, total, path: calls.append((i, total)))
    # порядок результата — сортированный список файлов, несмотря на пул потоков
    assert [f.path.name for f in files] == ["a.mkv", "b.mkv", "c.mkv"]
    assert sorted(calls) == [(1, 3), (2, 3), (3, 3)]


def test_scan_folder_stop_returns_partial(tmp_path, monkeypatch):
    for i in range(5):
        (tmp_path / f"e{i}.mkv").touch()
    monkeypatch.setattr(core, "find_tools", lambda: ("mkvmerge", "mkvpropedit"))
    monkeypatch.setattr(core, "scan_file", lambda tool, p: MkvFile(p))

    files = core.scan_folder(tmp_path, recursive=False, stop=lambda: True)
    assert len(files) < 5                                # прервались, всё не дочитали


def test_apply_plan_dry_run_and_skip():
    files = make_library()
    audio_key = core.audio_identity(mk_track(name="Original", language="eng"))
    plans = core.plan_changes(files, audio_key, None)

    res = core.apply_plan("mkvpropedit.exe", plans[0], dry_run=True)
    assert res.ok and not res.skipped

    res2 = core.apply_plan("mkvpropedit.exe", plans[1], dry_run=True)
    assert res2.ok and res2.skipped


def test_list_mkv_accepts_single_file(tmp_path):
    # Фильм может лежать в корне библиотеки одним файлом; кнопка «Файл…»
    # указывает прямо на него, и «Сканировать» обязано это принять.
    video = tmp_path / "It Chapter Two (2019).mkv"
    video.write_bytes(b"")
    assert core.list_mkv(video, recursive=False) == [video]
    assert core.list_mkv(video, recursive=True) == [video]


def test_list_mkv_single_file_of_other_format(tmp_path):
    other = tmp_path / "movie.mp4"
    other.write_bytes(b"")
    assert core.list_mkv(other, recursive=False) == []


def test_list_mkv_folder_unchanged(tmp_path):
    (tmp_path / "a.mkv").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.mkv").write_bytes(b"")
    assert [p.name for p in core.list_mkv(tmp_path, recursive=False)] == ["a.mkv"]
    assert [p.name for p in core.list_mkv(tmp_path, recursive=True)] == ["a.mkv", "b.mkv"]
