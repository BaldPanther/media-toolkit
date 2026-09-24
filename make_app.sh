#!/usr/bin/env bash
# Собирает «Media Toolkit.app» — маковский аналог make_shortcut.ps1.
#
#   bash make_app.sh
#
# Собирает в ~/Applications — туда можно писать без sudo, и Spotlight,
# Launchpad и Dock эту папку видят наравне с /Applications. Изменить цель:
#   DEST=/Applications bash make_app.sh     # потребует прав администратора
#
# Внутри .app — тонкая обёртка вокруг python app.py из этой папки, а не копия
# кода: правки в проекте подхватываются без пересборки. Путь к app.py и к
# интерпретатору зашивается при сборке: перенёс проект — пересобери.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${DEST:-$HOME/Applications}"
TITLE="Media Toolkit"

ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }
die()  { printf '  ✗ %s\n' "$*" >&2; exit 1; }

# --- интерпретатор ---------------------------------------------------------
# Нужен питон с tkinter 8.6+. У системного (из Xcode) стоит Tk 8.5 — в нём
# ttk-виджеты выглядят плохо, поэтому такой кандидат отбраковывается.
find_python() {
  local cands=(/opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3)
  command -v python3.14 >/dev/null 2>&1 && cands+=("$(command -v python3.14)")
  local p
  for p in "${cands[@]}"; do
    [ -x "$p" ] || continue
    "$p" -c 'import tkinter,sys; sys.exit(0 if tkinter.TkVersion >= 8.6 else 1)' 2>/dev/null \
      && { echo "$p"; return 0; }
  done
  die "не найден python с tkinter 8.6+. Поставь: brew install python-tk@3.14"
}

# --- иконка ----------------------------------------------------------------
# Берём самый крупный исходник: мельче 512 пикселей апскейл заметен на Retina.
pick_icon_src() {
  local f
  for f in app-icon-master.png app-icon-source.png app-icon.png; do
    [ -f "$HERE/assets/$f" ] && { echo "$HERE/assets/$f"; return 0; }
  done
  return 1
}

build_icns() {
  local src="$1" out="$2" work s
  work="$(mktemp -d)"
  mkdir -p "$work/icon.iconset"
  for s in 16 32 128 256 512; do
    sips -z "$s" "$s" "$src" --out "$work/icon.iconset/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$src" --out "$work/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$work/icon.iconset" -o "$out"
  rm -rf "$work"
}

# --- сборка ----------------------------------------------------------------
PY="$(find_python)"
[ -f "$HERE/app.py" ] || die "нет app.py рядом со скриптом"
mkdir -p "$DEST" || die "не могу создать $DEST (для /Applications нужен sudo)"
printf '\nИнтерпретатор: %s (Tk %s)\nКуда:          %s\n\n' "$PY" \
  "$("$PY" -c 'import tkinter; print(tkinter.TkVersion)')" "$DEST"

BUNDLE="$DEST/$TITLE.app"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

# Исполняемый файл: тонкая обёртка вокруг python.
# stdout/stderr уходят в лог — без окна терминала иначе не видно, почему упало.
cat > "$BUNDLE/Contents/MacOS/launcher" <<LAUNCHER
#!/bin/bash
LOG="\$HOME/Library/Logs/$TITLE.log"
cd "$HERE" || exit 1
exec "$PY" "$HERE/app.py" "\$@" >>"\$LOG" 2>&1
LAUNCHER
chmod +x "$BUNDLE/Contents/MacOS/launcher"

# NSHighResolutionCapable обязателен: без него macOS рисует окно в 1x
# и растягивает — шрифт получается мыльным.
cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>$TITLE</string>
    <key>CFBundleDisplayName</key>       <string>$TITLE</string>
    <key>CFBundleIdentifier</key>        <string>local.media-toolkit</string>
    <key>CFBundleExecutable</key>        <string>launcher</string>
    <key>CFBundleIconFile</key>          <string>app</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key>           <string>1</string>
    <key>LSMinimumSystemVersion</key>    <string>13.0</string>
    <key>NSHighResolutionCapable</key>   <true/>
</dict>
</plist>
PLIST

if icon="$(pick_icon_src)"; then
  build_icns "$icon" "$BUNDLE/Contents/Resources/app.icns"
else
  warn "иконки нет, будет системная заглушка"
fi

touch "$BUNDLE"   # чтобы Finder перечитал иконку
ok "$TITLE.app"

cat <<NEXT

Готово: $BUNDLE
Для быстрого доступа перетащи его в Dock.
Если приложение не открылось, смотри ~/Library/Logs/$TITLE.log
NEXT
