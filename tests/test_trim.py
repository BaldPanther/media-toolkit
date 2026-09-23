"""Тесты trim.py: оценка хвоста, главы, сверка — и обрезка настоящего MKV.

Интеграционные тесты собирают маленький файл ffmpeg-ом и mkvmerge-ом и режут его
по-настоящему; без этих инструментов они пропускаются.
"""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import core
import trim


def _t(type, duration):
    return SimpleNamespace(type=type, duration=duration)


# ----------------------------------------------------------------- хвост --

def test_tail_is_longest_audio_past_video():
    """Altered Carbon S01E03: дубляжи на 37.6 с длиннее видео, Profix — на 88.6."""
    tracks = [_t("video", 2978.768), _t("audio", 3016.352), _t("audio", 3067.349)]
    assert trim.tail_seconds(tracks) == pytest.approx(88.581)


def test_tail_ignores_subtitles_past_video():
    """Apothecary Diaries: последняя надпись висит на 3 с дольше картинки — не хвост."""
    tracks = [_t("video", 1372.08), _t("audio", 1372.09), _t("subtitles", 1375.06)]
    assert trim.tail_seconds(tracks) == pytest.approx(0.01)


def test_tail_unknown_without_statistics():
    assert trim.tail_seconds([_t("video", None), _t("audio", 60.0)]) is None
    assert trim.tail_seconds([_t("audio", 60.0)]) is None
    assert trim.tail_seconds([_t("video", 59.0), _t("audio", None)]) is None


def test_tail_never_negative():
    assert trim.tail_seconds([_t("video", 60.1), _t("audio", 60.0)]) == 0.0


def test_needs_trim_threshold():
    assert not trim.needs_trim(None)
    assert not trim.needs_trim(0.1)          # обычный файл: последний аудиокадр
    assert not trim.needs_trim(trim.TAIL_MIN_S)
    assert trim.needs_trim(2.4)
    assert trim.needs_trim(88.6)


def test_tag_seconds():
    assert core.tag_seconds("00:49:38.768000000") == pytest.approx(2978.768)
    assert core.tag_seconds("00:00:33,042000000") == pytest.approx(33.042)
    assert core.tag_seconds(None) is None
    assert core.tag_seconds("мусор") is None


def test_scan_file_reads_track_duration(monkeypatch):
    data = {"tracks": [{"id": 0, "type": "video", "codec": "HEVC",
                        "properties": {"tag_duration": "00:49:38.768000000"}},
                       {"id": 1, "type": "audio", "codec": "AAC",
                        "properties": {"tag_duration": "00:51:07.349000000"}},
                       {"id": 2, "type": "audio", "codec": "AC-3", "properties": {}}],
            "container": {"properties": {"duration": 3067349000000}}}
    monkeypatch.setattr(core, "_run", lambda args: SimpleNamespace(
        returncode=0, stdout=json.dumps(data).encode(), stderr=b""))
    f = core.scan_file("mkvmerge", Path("x.mkv"))
    assert f.tracks[0].duration == pytest.approx(2978.768)
    assert f.tracks[2].duration is None
    assert trim.tail_seconds(f.tracks) == pytest.approx(88.581)


# ----------------------------------------------------------------- главы --

CHAPTERS = """<?xml version="1.0"?>
<Chapters>
  <EditionEntry>
    <EditionFlagOrdered>0</EditionFlagOrdered>
    <ChapterAtom><ChapterUID>1</ChapterUID>
      <ChapterTimeStart>00:00:00.000000000</ChapterTimeStart>
      <ChapterTimeEnd>00:00:29.000000000</ChapterTimeEnd>
      <ChapterDisplay><ChapterString>Intro</ChapterString><ChapterLanguage>eng</ChapterLanguage></ChapterDisplay>
    </ChapterAtom>
    <ChapterAtom><ChapterUID>2</ChapterUID>
      <ChapterTimeStart>00:48:00.000000000</ChapterTimeStart>
      <ChapterTimeEnd>00:51:07.349000000</ChapterTimeEnd>
      <ChapterDisplay><ChapterString>Credits</ChapterString><ChapterLanguage>eng</ChapterLanguage></ChapterDisplay>
    </ChapterAtom>
    <ChapterAtom><ChapterUID>3</ChapterUID>
      <ChapterTimeStart>00:50:30.000000000</ChapterTimeStart>
      <ChapterDisplay><ChapterString>Tail</ChapterString></ChapterDisplay>
    </ChapterAtom>
  </EditionEntry>
</Chapters>"""


def test_clamp_chapters_cuts_crossing_and_drops_after():
    xml, count = trim.clamp_chapters(CHAPTERS, 2978.784)
    assert count == 2
    assert "Tail" not in xml                                  # началась после конца видео
    assert "00:49:38.784000000" in xml                        # «Credits» кончается на cut
    assert "00:00:29.000000000" in xml and "<ChapterLanguage>eng" in xml


def test_chapters_ordered():
    assert not trim.chapters_ordered(CHAPTERS)
    assert trim.chapters_ordered(CHAPTERS.replace(
        "<EditionFlagOrdered>0", "<EditionFlagOrdered>1"))


def test_fmt_ts_roundtrip():
    assert trim._fmt_ts(2978.784) == "00:49:38.784000000"
    assert trim._parse_ts(trim._fmt_ts(3725.5)) == pytest.approx(3725.5)


# ------------------------------------------------------ заголовок кодека --

def _hvcc(sps: bytes, complete: bool) -> str:
    """Запись hvcC: 22 байта заголовка, число массивов, массивы VPS/SPS/PPS."""
    arrays = b""
    for nal_type, nal in ((32, b"\x40\x01vps"), (33, sps), (34, b"\x44\x01pps")):
        arrays += bytes([(0x80 if complete else 0) | nal_type]) + (1).to_bytes(2, "big")
        arrays += len(nal).to_bytes(2, "big") + nal
    return (bytes(22) + bytes([3]) + arrays).hex()


def test_hevc_completeness_flag_ignored():
    cid = "V_MPEGH/ISO/HEVC"
    a, b = _hvcc(b"\x42\x01sps", True), _hvcc(b"\x42\x01sps", False)
    assert a != b
    assert trim._norm_private(cid, a) == trim._norm_private(cid, b)


def test_hevc_other_parameters_differ():
    cid = "V_MPEGH/ISO/HEVC"
    assert trim._norm_private(cid, _hvcc(b"\x42\x01sps", True)) != \
        trim._norm_private(cid, _hvcc(b"\x42\x01SPS", True))


def test_private_of_other_codecs_compared_as_is():
    assert trim._norm_private("A_AAC", "1190") == "1190"


# --------------------------------------------------------------- сверка --

def _info(tracks, duration=10.0, attachments=(), chapters=0, title=None):
    return {"tracks": [{"id": i, "type": t, "properties": dict(p)}
                       for i, (t, p) in enumerate(tracks)],
            "attachments": list(attachments),
            "chapters": [{"num_entries": chapters}] if chapters else [],
            "container": {"properties": {"duration": int(duration * 1e9),
                                         **({"title": title} if title else {})}}}


VIDEO = {"codec_id": "V_MPEG4/ISO/AVC", "tag_number_of_frames": "72",
         "tag_duration": "00:00:03.000000000", "minimum_timestamp": 0}
AUDIO = {"codec_id": "A_AC3", "language": "rus", "track_name": "Дубляж",
         "default_track": True, "tag_duration": "00:00:03.010000000", "minimum_timestamp": 0}


def test_compare_accepts_identical():
    src = _info([("video", VIDEO), ("audio", AUDIO)], duration=8.0)
    new = _info([("video", VIDEO), ("audio", AUDIO)], duration=3.01)
    assert trim.compare(src, new, 3.0, 0) == []


def test_compare_catches_lost_frames_flags_and_track_count():
    src = _info([("video", VIDEO), ("audio", AUDIO)], duration=8.0)
    new = _info([("video", {**VIDEO, "tag_number_of_frames": "71"}),
                 ("audio", {**AUDIO, "default_track": False})], duration=3.01)
    problems = " | ".join(trim.compare(src, new, 3.0, 0))
    assert "кадров" in problems and "default_track" in problems
    assert trim.compare(src, _info([("video", VIDEO)], 3.0), 3.0, 0) == \
        ["дорожек было 2, стало 1"]


def test_compare_catches_shift_tail_attachments_chapters():
    src = _info([("video", VIDEO), ("audio", AUDIO)], duration=8.0,
                attachments=[{"file_name": "cover", "content_type": "image/jpeg", "size": 5}],
                chapters=3)
    new = _info([("video", {**VIDEO, "minimum_timestamp": 83_000_000}),
                 ("audio", {**AUDIO, "tag_duration": "00:00:08.000000000"})], duration=8.0)
    problems = " | ".join(trim.compare(src, new, 3.0, 3))
    for word in ("сдвинулось начало", "кончается на", "вложения", "глав 0 вместо 3"):
        assert word in problems


def test_restore_args_brings_back_flags_name_and_duration():
    src = _info([("video", VIDEO),
                 ("audio", {**AUDIO, "forced_track": True, "default_duration": 32000000}),
                 ("subtitles", {"codec_id": "S_TEXT/UTF8", "default_track": False})],
                title="Серия")
    new = _info([("video", VIDEO),
                 ("audio", {**AUDIO, "track_name": None}),
                 ("subtitles", {"codec_id": "S_TEXT/UTF8", "default_track": True,
                                "track_name": "лишнее"})])
    args = trim.restore_args(src, new)
    assert args[:2] == ["--edit", "track:2"]
    assert "flag-forced=1" in args and "name=Дубляж" in args
    assert "default-duration=32000000" in args
    i = args.index("track:3")
    assert args[i + 1:i + 5] == ["--set", "flag-default=0", "--delete", "name"]
    assert args[-4:] == ["--edit", "info", "--set", "title=Серия"]


def test_restore_args_nothing_to_do():
    src = _info([("video", VIDEO), ("audio", AUDIO)])
    assert trim.restore_args(src, src) == []


def test_ffmpeg_args_maps_tracks_only_and_keeps_default_flags():
    args = trim.ffmpeg_args("ffmpeg", "in.mkv", "out.trim-tmp", 2978.784, [0, 1, 11])
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["0:0", "0:1", "0:11"]
    assert args[args.index("-default_mode") + 1] == "passthrough"
    assert args[args.index("-map_chapters") + 1] == "-1"
    assert args[args.index("-t") + 1] == "2978.784"
    assert args[args.index("-c") + 1] == "copy"


# ------------------------------------------------- обрезка настоящего MKV --

TOOLS, MISSING = trim.find_tools()
needs_tools = pytest.mark.skipif(TOOLS is None, reason=f"нет инструментов: {MISSING}")


def _sh(*args):
    cp = subprocess.run([str(a) for a in args], capture_output=True)
    assert cp.returncode in (0, 1), cp.stderr.decode(errors="replace")[-500:]
    return cp


def _make_mkv(tmp: Path, audio_len: float) -> Path:
    """3 с видео с B-кадрами, два звука (audio_len и 5 с), субтитры, обложка,
    «шрифт», главы — всё, что ffmpeg переносит не так, как хотелось бы."""
    ff = TOOLS.ffmpeg
    _sh(ff, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=3",
        "-c:v", "libx264", "-bf", "2", "-g", "24", "-pix_fmt", "yuv420p", tmp / "v.mp4")
    _sh(ff, "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_len}",
        "-c:a", "ac3", "-b:a", "96k", tmp / "a1.ac3")
    _sh(ff, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=660:duration=5",
        "-c:a", "aac", "-b:a", "64k", tmp / "a2.m4a")
    (tmp / "s.srt").write_text("1\n00:00:00,500 --> 00:00:02,000\nпривет\n\n"
                               "2\n00:00:04,000 --> 00:00:05,000\nв хвосте\n", encoding="utf-8")
    _sh(ff, "-v", "error", "-f", "lavfi", "-i", "color=red:size=64x64", "-frames:v", "1",
        tmp / "cover.jpg")
    (tmp / "font.ttf").write_bytes(b"\x00\x01\x00\x00fake font")
    # Явный конец у «Credits» — за концом видео, как его записал mkvpropedit в
    # Altered Carbon: после обрезки он должен встать на конец видео.
    atoms = "".join(
        f"<ChapterAtom><ChapterTimeStart>{s}</ChapterTimeStart>{e}"
        f"<ChapterDisplay><ChapterString>{n}</ChapterString></ChapterDisplay></ChapterAtom>"
        for s, e, n in (("00:00:00.000", "", "Intro"), ("00:00:01.000", "", "Episode"),
                        ("00:00:02.000", "<ChapterTimeEnd>00:00:08.000</ChapterTimeEnd>",
                         "Credits")))
    (tmp / "ch.xml").write_text('<?xml version="1.0"?><Chapters><EditionEntry>'
                                f"{atoms}</EditionEntry></Chapters>", encoding="utf-8")
    out = tmp / "Show - S01E01.mkv"
    _sh(TOOLS.mkvmerge, "-q", "-o", out, "--title", "Серия",
        tmp / "v.mp4",
        "--language", "0:rus", "--track-name", "0:Дубляж", "--default-track-flag", "0:1",
        tmp / "a1.ac3",
        "--language", "0:eng", "--track-name", "0:Original", "--default-track-flag", "0:0",
        tmp / "a2.m4a",
        "--language", "0:rus", "--track-name", "0:Forced", "--default-track-flag", "0:0",
        "--forced-display-flag", "0:1", tmp / "s.srt",
        "--attachment-name", "cover", "--attachment-mime-type", "image/jpeg",
        "--attach-file", tmp / "cover.jpg",
        "--attachment-mime-type", "font/ttf", "--attach-file", tmp / "font.ttf",
        "--chapters", tmp / "ch.xml")
    return out


@needs_tools
def test_trim_real_file(tmp_path):
    path = _make_mkv(tmp_path, audio_len=8)
    before = trim.identify(TOOLS.mkvmerge, path)
    assert before["container"]["properties"]["duration"] / 1e9 > 7.9

    seen = []
    result = trim.trim_file(path, TOOLS, progress=seen.append)
    assert result.ok and not result.skipped, result.message
    assert result.new_duration == pytest.approx(3.0, abs=0.1)
    assert seen and seen[-1] == 1.0
    assert not trim.tmp_path(path).exists()

    after = trim.identify(TOOLS.mkvmerge, path)
    assert [t["type"] for t in after["tracks"]] == ["video", "audio", "audio", "subtitles"]
    names = [t["properties"].get("track_name") for t in after["tracks"]]
    assert names == [None, "Дубляж", "Original", "Forced"]
    assert [t["properties"].get("default_track") for t in after["tracks"]][1:] == \
        [True, False, False]
    assert after["tracks"][3]["properties"].get("forced_track") is True
    assert [(a["file_name"], a["content_type"]) for a in after["attachments"]] == \
        [("cover", "image/jpeg"), ("font.ttf", "font/ttf")]
    assert after["container"]["properties"].get("title") == "Серия"
    ends = [core.tag_seconds(t["properties"].get("tag_duration")) for t in after["tracks"][:3]]
    assert max(ends) - min(ends) < 0.1                    # все дорожки кончаются вместе

    ch = subprocess.run([TOOLS.mkvextract, str(path), "chapters", "-"],
                        capture_output=True).stdout.decode()
    assert ch.count("<ChapterAtom>") == 3
    assert f"<ChapterTimeEnd>{trim._fmt_ts(result.cut)}" in ch

    # повторный проход — хвоста уже нет, файл не трогается
    again = trim.trim_file(path, TOOLS)
    assert again.ok and again.skipped


@needs_tools
def test_trim_skips_file_without_tail(tmp_path):
    path = _make_mkv(tmp_path, audio_len=3)
    # второй звук 5 с — хвост есть; делаем файл честным: только видео и первый звук
    clean = tmp_path / "clean.mkv"
    _sh(TOOLS.mkvmerge, "-q", "-o", clean, "-a", "1", "-S", "-M", "--no-chapters", path)
    stat = clean.stat()
    result = trim.trim_file(clean, TOOLS)
    assert result.ok and result.skipped
    assert clean.stat().st_mtime == stat.st_mtime


@needs_tools
def test_trim_cancel_leaves_original(tmp_path):
    path = _make_mkv(tmp_path, audio_len=8)
    data = path.read_bytes()
    result = trim.trim_file(path, TOOLS, cancel=lambda: True)
    assert result.cancelled and not result.ok
    assert path.read_bytes() == data
    assert not trim.tmp_path(path).exists()


@needs_tools
def test_trim_mismatch_leaves_original(tmp_path, monkeypatch):
    path = _make_mkv(tmp_path, audio_len=8)
    data = path.read_bytes()
    monkeypatch.setattr(trim, "compare", lambda *a: ["вложения не совпали"])
    result = trim.trim_file(path, TOOLS)
    assert not result.ok and "вложения не совпали" in result.message
    assert path.read_bytes() == data
    assert not trim.tmp_path(path).exists()
