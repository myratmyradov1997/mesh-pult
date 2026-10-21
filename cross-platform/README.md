# Пульт МЭШ — кроссплатформенная версия (Windows / Linux / macOS)

Один файл `mesh_pult_gui.py`. Открывает окно-пульт на любом компьютере,
не требует Xcode/сборки на месте.

## Как это работает без командной строки

1. **Скачайте готовый файл** из релиза GitHub (`MeshPult-Windows.exe`
   или `MeshPult-Linux.tar.gz`).
2. **Откройте** program.exe (Windows) / `./MeshPult` (Linux).
3. Программа сама найдёт токен в Chrome/Edge/Helium/Firefox — **нужно
   только один раз войти в https://school.mos.ru в браузере**.
4. Программа сама запросит один раз `Profile-Id` — это число, которое
   показывает school.mos.ru по любому запросу API. 1 раз, 30 секунд.
5. Всё остальное — расписание, списки классов, выставление оценок, «Н»,
   «без д/з» — работает само.

## Сборка из исходников

```bash
pip install -r cross-platform/requirements.txt
pip install pyinstaller
pyinstaller --onefile --windowed \
    --name MeshPult \
    --add-data ../mesh_automation_v3.py:. \
    --add-data ../mesh_remote.py:. \
    cross-platform/mesh_pult_gui.py
# результат: dist/MeshPult(.exe)
```

## Приятный бонус

На macOS есть нативная версия (см. `build.sh`) — чёлка-пульт и иконка в
меню-баре. Кроссплатформенная нужна, чтобы не дублировать эту логику
на Windows и Linux, где красивой панели-чёлки нет.
