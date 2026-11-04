@echo off
chcp 65001 >nul
cd /d "%~dp0"
if defined FLEET_PYTHON goto run
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -X utf8 -m fleet.setup_admin %*
  if errorlevel 1 goto end
  py -3 -X utf8 -m fleet.app %*
  goto end
)
where python >nul 2>nul
if not errorlevel 1 (
  python -X utf8 -m fleet.setup_admin %*
  if errorlevel 1 goto end
  python -X utf8 -m fleet.app %*
  goto end
)
echo 请安装 Python 3.12 或更新版本。
pause
goto end
:run
"%FLEET_PYTHON%" -X utf8 -m fleet.setup_admin %*
if errorlevel 1 goto end
"%FLEET_PYTHON%" -X utf8 -m fleet.app %*
:end
