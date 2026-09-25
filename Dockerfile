# Media Toolkit в Docker: веб-интерфейс в браузере (порт 5800), настройки — в /config,
# медиатека — в /media. Программа работает от владельца медиатеки USER_ID:GROUP_ID.
FROM python:3.13-slim-trixie

# Внешние инструменты — из репозитория Debian: mkvmerge/mkvpropedit (MKVToolNix),
# ffmpeg/ffprobe, fpcalc (Chromaprint). Без рекомендуемых пакетов: у ffmpeg они
# тянут за собой сотни мегабайт, которые программе не нужны.
RUN apt-get update \
    && apt-get install -y --no-install-recommends mkvtoolnix ffmpeg libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY *.py /app/
COPY web/ /app/web/
COPY assets/ /app/assets/
COPY docker/entrypoint.sh /entrypoint.sh

# Настройки и кэш — там же, где их держал прежний образ (база jlesage): /config/xdg/…
# Поэтому после обновления программа находит ключи и корни, введённые раньше.
ENV HOME=/config \
    XDG_CONFIG_HOME=/config/xdg/config \
    XDG_CACHE_HOME=/config/xdg/cache \
    XDG_DATA_HOME=/config/xdg/data \
    LANG=C.UTF-8 \
    MEDIA_ROOT=/media \
    MT_SERVER=1 \
    PORT=5800 \
    USER_ID=1000 \
    GROUP_ID=1000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Версия — из тега релиза (см. .github/workflows/release.yml); видна в /api/info.
ARG MT_VERSION=""
ENV MT_VERSION=${MT_VERSION}

WORKDIR /app
EXPOSE 5800
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5800/api/info', timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "/app/webapp.py", "--server"]
