# CLAUDE.md — media-toolkit

GUI подготовки медиатеки Kodi: метаданные и картинки с TMDb/Fanart.tv, раскладка папок и
имён файлов, аудио/субтитры по умолчанию в MKV (`mkvpropedit`), русские субтитры
(subliminal/OpenSubtitles), `.edl` для пропуска заставок. Python + Tkinter.
Подробное описание — [README.md](README.md). Раньше жил в приватном репо `toolbox`
(ещё раньше — `mkv-default-tracks`); история перенесена.

**Репозиторий публичный** (лицензия MIT) — всё, что коммитится, видят все.

## Личные заметки — `notes/`, не в этот репозиторий

`notes/` — отдельный **приватный** репозиторий `BaldPanther/media-toolkit-notes`,
склонированный внутрь этой папки и исключённый `.gitignore`. Там TODO, планы,
EDL-заметки, разборы конкретных сериалов — всё личное и рабочее.

- В публичный репозиторий — только то, что нужно пользователю или разработчику:
  README, инструкции, код, тесты, этот файл.
- Заметки коммитятся в `notes/` отдельно (`git -C notes commit …`, `git -C notes push`).
- Нет `notes/` (свежий клон) — `git clone https://github.com/BaldPanther/media-toolkit-notes.git notes`.

## Где запускается

Одна кодовая база — три способа запуска, логика и интерфейс общие:

- **Windows / macOS из исходников** — `python app.py`; ярлыки `make_shortcut.ps1` / `make_app.sh`.
- **Docker** — тот же Tkinter-интерфейс в браузере через noVNC, для ZimaOS и любого
  сервера: обработка идёт рядом с медиатекой, без копирования по Wi-Fi. Один образ, два
  compose-файла: `docker-compose.yaml` (обычный Docker, относительные пути) и
  `docker-compose.zimaos.yaml` (то же + метаданные `x-casaos` и пути ZimaOS
  `/DATA/AppData/…`, `/media`).
- **Сборки** (план) — GitHub Actions по тегу: Windows и macOS через PyInstaller,
  Docker-образ в GHCR (`ghcr.io/baldpanther/media-toolkit`, amd64 + arm64).

Код держать кроссплатформенным: Windows-специфику закрывать `os.name == "nt"` /
`sys.platform == "win32"`, внешние бинарники искать через `shutil.which` (+ запасные
папки brew — см. `edl._TOOL_DIRS`), а не по зашитым путям. Linux (Docker) идёт по
тем же веткам, что и macOS.

## Docker-образ

База — `jlesage/baseimage-gui` (Debian 13, Python 3.13, Tk 8.6): X-сервер, openbox, noVNC
на порту 5800. Приложение запускает `docker/startapp.sh` от пользователя `USER_ID:GROUP_ID`
(по умолчанию 1000). `HOME=/config`, `XDG_*` указывают внутрь `/config` — поэтому
`paths.py` сам кладёт настройки в volume, код под Docker не менялся.

- Версию базы пинить точно (`debian-13-v4.14.0`), обновлять осознанно.
- `docker/main-window-selection.xml` — на весь экран разворачивается только главное окно
  (`Class=Tk`); без него openbox растягивает и все `Toplevel` (настройки, сетка постеров).
- Локали `en_US`/`ru_RU` генерируются в образе: база объявляет `LANG=en_US.UTF-8`, но
  локаль не ставит, и **mkvmerge падает на старте** (`_S_create_c_locale name not valid`).
- `webbrowser.open` в контейнере ничего не откроет — браузера там нет.
- Тесты внутри образа (там другие версии ffmpeg/MKVToolNix, чем на маке):
  ```bash
  docker build -t media-toolkit:dev .
  docker run --rm --entrypoint sh -v "$PWD/tests:/app/tests:ro" media-toolkit:dev -c \
    '/opt/venv/bin/pip install -q pytest && cd /app && /opt/venv/bin/python -m pytest tests -q'
  ```
- Скриншот экрана контейнера (проверить UI без браузера):
  `docker exec -u 1000 <имя> /opt/venv/bin/python -c "from PIL import ImageGrab; ImageGrab.grab(xdisplay=':0').save('/tmp/s.png')"`.

## Настройки пользователя

Лежат не в папке программы, а в каталоге ОС — см. `paths.py`
(`~/Library/Application Support/media-toolkit/`, `%APPDATA%\media-toolkit\`,
`$XDG_CONFIG_HOME/media-toolkit/`). Обновление программы их не трогает; в Docker
этот каталог выносится в volume `/config`.

- Читать и писать файлы настроек **только** через `paths.read_json` / `paths.write_json`:
  запись атомарная (оборванная не портит файл), права 0600, битый файл откладывается
  в `<имя>.bad-<время>`, а не затирается дефолтами при следующем сохранении.
- Каждое поле читается отдельно с дефолтом (`d.get(key, default)`) — новые поля
  добавлять так же, тогда старые файлы настроек читаются новой версией.
- **Поля не переименовывать** без переноса старого значения — иначе при обновлении
  оно молча потеряется (там ключи API и пароль OpenSubtitles).

## Стек

**Python 3 + обычный Tkinter/ttk**, стандартная светлая тема, **системный шрифт**
(не переопределять), без bold (заголовки секций — `ttk.LabelFrame`), списки/таблицы —
`ttk.Treeview`. Внешние CLI (ffmpeg, mkvpropedit, fpcalc) — через `subprocess`.

⚠ **Tkinter на macOS:** системный Python 3.9 идёт с Tcl/Tk **8.5** — ttk в нём выглядит
плохо. Нужен Python с Tk 8.6+ (`brew install python-tk@3.14`).

Иконки: `assets/app-icon.png` (для `Tk.iconphoto`), `assets/app-icon.ico` (Windows,
кадры 16–256), крупный исходник `assets/app-icon-source.png` (из него `make_app.sh`
собирает `.icns`). Для главного окна — `root.iconbitmap(str(ico_path))`, **не**
`iconbitmap(default=…)`; ссылку на `PhotoImage` хранить всё время жизни окна.

## Тесты

```bash
python -m pytest tests/ -q
```
Не трогают реальные MKV и не ходят в сеть (кроме `test_trim.py`, который сам собирает
маленький MKV, если есть ffmpeg и mkvmerge).

## Git
- Commit format: `<type>: <description>` (`fix`/`add`/`update`/`refactor`/`docs`).
- **Коммитить по ходу работы**, не копить: закончил осмысленный кусок — закоммитил.
  Дерево после каждого коммита рабочее, тесты зелёные.
- Работаем прямо в `main`.
