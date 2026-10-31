@echo off
rem Сборка одного файла MFTI_Kick.exe (Python на других компьютерах не нужен).
rem Запускать из папки игры.

python -m pip install --upgrade pyinstaller pygame psutil
if errorlevel 1 goto fail

python -m PyInstaller --onefile --windowed --name MFTI_Kick main.py
if errorlevel 1 goto fail

echo.
echo Готово: dist\MFTI_Kick.exe
echo Рядом с ним игра создаст settings.json (ник) и папку cache (звуки).
pause
exit /b 0

:fail
echo.
echo Сборка не удалась, смотрите сообщения выше.
pause
exit /b 1
