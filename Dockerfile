# Media Toolkit в Docker: тот же Tkinter-интерфейс, что на Windows и macOS,
# показанный в браузере через noVNC. База jlesage/baseimage-gui даёт X-сервер,
# оконный менеджер и веб-доступ на порту 5800; настройки живут в /config.
FROM jlesage/baseimage-gui:debian-13-v4.14.0

# Внешние инструменты — из репозитория Debian: mkvmerge/mkvpropedit (MKVToolNix),
# ffmpeg/ffprobe, fpcalc (Chromaprint). Шрифты — с кириллицей: в базе их нет.
RUN add-pkg \
        python3 python3-tk python3-venv \
        mkvtoolnix ffmpeg libchromaprint-tools \
        fonts-dejavu-core

# База объявляет LANG=en_US.UTF-8, но саму локаль не ставит — и mkvmerge падает
# на старте («locale::facet::_S_create_c_locale name not valid»). Генерируем её
# и русскую: пользователь может выставить LANG=ru_RU.UTF-8.
RUN add-pkg locales \
    && sed-patch 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
    && sed-patch 's/# ru_RU.UTF-8 UTF-8/ru_RU.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen

# Питоновские зависимости — в venv поверх системного python3; tkinter берётся
# из его стандартной библиотеки (пакет python3-tk).
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Иконка вкладки браузера и веб-приложения.
COPY assets/app-icon-source.png /tmp/app-icon.png
RUN install_app_icon.sh /tmp/app-icon.png && rm /tmp/app-icon.png

COPY docker/main-window-selection.xml /etc/openbox/main-window-selection.xml
COPY docker/startapp.sh /startapp.sh

COPY *.py /app/
COPY assets/ /app/assets/

# Код лежит в /app и принадлежит root, а приложение работает от пользователя
# app — писать туда __pycache__ ему незачем.
ENV PYTHONDONTWRITEBYTECODE=1

RUN set-cont-env APP_NAME "Media Toolkit"
