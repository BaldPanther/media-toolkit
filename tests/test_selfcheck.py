import selfcheck


def _boom():
    raise ImportError("No module named 'numpy'")


def test_all_ok_returns_zero_and_writes_report(tmp_path):
    report = tmp_path / "report.txt"
    code = selfcheck.run(report, [("tkinter", lambda: "Tk 8.6", True)])

    assert code == 0
    assert report.read_text("utf-8") == "ok    tkinter: Tk 8.6\n"


def test_required_failure_returns_one(tmp_path):
    report = tmp_path / "report.txt"
    code = selfcheck.run(report, [("tkinter", lambda: "Tk 8.6", True), ("numpy", _boom, True)])

    assert code == 1
    assert "FAIL  numpy: ImportError: No module named 'numpy'" in report.read_text("utf-8")


def test_optional_failure_is_reported_but_passes(tmp_path):
    # mkvmerge/ffmpeg в сборку не входят — их отсутствие не валит проверку.
    report = tmp_path / "report.txt"
    code = selfcheck.run(report, [("mkvmerge", _boom, False)])

    assert code == 0
    assert report.read_text("utf-8").startswith("нет   mkvmerge:")


def test_default_checks_cover_bundled_dependencies():
    names = {name for name, _, _ in selfcheck.default_checks()}
    assert {"tkinter", "модули программы", "numpy", "Pillow", "send2trash",
            "subliminal", "иконки", "fpcalc"} <= names
