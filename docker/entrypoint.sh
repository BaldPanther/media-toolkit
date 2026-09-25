#!/bin/sh
# Запуск в контейнере от владельца медиатеки (USER_ID:GROUP_ID, по умолчанию 1000:1000):
# от его имени программа переименовывает файлы и пишет .nfo, .edl, картинки.
# USER_ID=0 — работать от root (ZimaOS: Samba пишет файлы медиатеки от root).
set -e

mkdir -p /config
if [ "$(id -u)" = "0" ] && [ "${USER_ID:-0}" != "0" ]; then
    # Настройки должны принадлежать тому, кто их пишет. Папка маленькая
    # (ключи, кэш отпечатков), поэтому забираем её целиком.
    chown -R "${USER_ID}:${GROUP_ID:-$USER_ID}" /config
    exec setpriv --reuid="${USER_ID}" --regid="${GROUP_ID:-$USER_ID}" --clear-groups "$@"
fi
exec "$@"
