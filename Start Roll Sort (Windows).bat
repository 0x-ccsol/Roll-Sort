@echo off
rem Double-click to start Roll Sort on Windows.
title Roll Sort
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 app\launch.py %*
  goto done
)
where python >nul 2>nul
if %errorlevel%==0 (
  python app\launch.py %*
  goto done
)

echo.
echo   Roll Sort needs Python 3, which isn't installed yet.
echo   Opening the download page: install it (tick "Add python.exe to PATH"),
echo   then double-click this file again.
echo.
start "" "https://www.python.org/downloads/windows/"

:done
echo.
pause
