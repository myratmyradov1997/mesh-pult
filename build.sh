#!/usr/bin/env bash
# build.sh — собрать MeshPult (menubar-версия и версия для чёлки/notch) локально.
# Требования: Xcode Command Line Tools (swiftc): xcode-select --install
set -euo pipefail
cd "$(dirname "$0")"

# ВАЖНО: -target задаёт min deployment (LC_BUILD_VERSION.minos). Без него
# свежие Swift-инструменты пишут minos выше текущей macOS → LaunchServices
# отказывается запускать app из Finder с «Данная версия программы не может
# быть использована с этой версией macOS» (прямой запуск бинарника работает).
swiftc -target arm64-apple-macos14.0 main_notch.swift -o MeshPultNotch -framework AppKit
swiftc -target arm64-apple-macos14.0 main_menubar.swift -o MeshPultMenubar -framework AppKit

echo "Собрано: ./MeshPultNotch (панель у выреза экрана), ./MeshPultMenubar (иконка в меню-баре)"
