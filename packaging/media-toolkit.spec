# PyInstaller: сборка Media Toolkit — папка с exe на Windows, .app на macOS.
#
#   pip install pyinstaller -r requirements.txt   (на macOS ещё pillow: PNG → .icns)
#   pyinstaller --noconfirm packaging/media-toolkit.spec
#
# Версия — из переменной MT_VERSION (GitHub Actions подставляет тег).
# На Windows перед сборкой положить assets/fpcalc.exe — он войдёт в сборку.
# Проверка результата: <exe> --selfcheck report.txt (см. selfcheck.py).
import os
import sys

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
NAME = "Media Toolkit"
VERSION = os.environ.get("MT_VERSION", "0.0.0")

datas = [(os.path.join(ROOT, "assets"), "assets"),
         # Страница: шаблоны и статика (стили, скрипты, Alpine.js, иконки).
         (os.path.join(ROOT, "web", "templates"), os.path.join("web", "templates")),
         (os.path.join(ROOT, "web", "static"), os.path.join("web", "static"))]
# Версия внутри сборки: переменной MT_VERSION у запущенной программы уже нет.
_version_file = os.path.join(workpath, "VERSION")
os.makedirs(workpath, exist_ok=True)
with open(_version_file, "w", encoding="ascii") as f:
    f.write(VERSION)
datas.append((_version_file, "."))
binaries = []
# Модули, которые зависимости подгружают по имени во время работы, — сами
# сборщик их не найдёт: бэкенд кэша dogpile, провайдеры subliminal, конвертеры
# babelfish, правила guessit/rebulk, данные knowit/pymediainfo.
hiddenimports = ["selfcheck", "dogpile.cache.backends.memory"]
for pkg in ("subliminal", "babelfish", "guessit", "rebulk", "knowit", "pymediainfo"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["pytest"],
)
pyz = PYZ(a.pure)

icon = os.path.join(ROOT, "assets", "app-icon.ico" if sys.platform == "win32" else "app-icon.png")
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    console=False,
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=icon,
        bundle_identifier="io.github.baldpanther.media-toolkit",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": NAME,
            # Своего окна нет — страница открывается в браузере, — поэтому и
            # значка в Dock не нужно: без окна он висел бы «не отвечает».
            "LSUIElement": True,
            "LSMinimumSystemVersion": "13.0",
        },
    )
