#!/usr/bin/env bash
# install.sh — установка «Пульта учителя (МЭШ)» для macOS.
# Сам решает: вариант с чёлкой (MacBook с вырезом) или иконка в меню-баре.
# Требуется: macOS 12+, Xcode Command Line Tools (swiftc), python3.
set -euo pipefail
cd "$(dirname "$0")"

msg() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

msg "1/5 Python-окружение для моста к API МЭШ"
VENV="$HOME/.meshpult/venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet requests browser_cookie3 pycryptodome

msg "2/5 Конфиг (пути к python/мосту)"
mkdir -p "$HOME/.config/mesh-remote"
cat > "$HOME/.config/mesh-remote/config.json" <<EOF
{
  "python":  "$VENV/bin/python",
  "bridge":  "$PWD/mesh_remote.py",
  "notchWidth": 220,
  "panelWidth": 400
}
EOF

msg "3/5 Сборка (swiftc, нужен Xcode Command Line Tools)"
if ! command -v swiftc >/dev/null 2>&1; then
  echo "Не найден swiftc. Установите Command Line Tools и повторите:"
  echo "  xcode-select --install"
  exit 1
fi
bash build.sh

msg "4/5 Какой вариант ставить? (авто: чёлка → панель у выреза, иначе меню-бар)"
VARIANT="auto"
if [ "${1:-}" = "--menubar" ]; then VARIANT="menubar"; fi
if [ "${1:-}" = "--notch" ];  then VARIANT="notch";  fi
if [ "$VARIANT" = "auto" ]; then
  if ./MeshPultNotch --check-notch | grep -q YES; then VARIANT="notch"; else VARIANT="menubar"; fi
fi
echo "Выбран вариант: $VARIANT"

APP="$HOME/Applications/MeshPult.app"
mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/Info.plist"
if [ "$VARIANT" = "notch" ]; then
  cp MeshPultNotch "$APP/Contents/MacOS/MeshPult"
else
  cp MeshPultMenubar "$APP/Contents/MacOS/MeshPult"
fi
codesign --force --sign - "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true

msg "5/5 Автозагрузка (LaunchAgent)"
LABEL="ru.meshpult.$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$APP/Contents/MacOS/MeshPult</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || open "$APP"

msg "Готово: $APP ($VARIANT)"
echo "Первый вход: откройте https://school.mos.ru и войдите в «Кабинет учителя»."
echo "Пульт возьмёт токен из cookies браузера (Chrome или Helium) сам,"
echo "далее обновляет его при каждом открытии school.mos.ru в Chrome."
