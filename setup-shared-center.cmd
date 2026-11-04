@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo Bench Web Plus - 共享中心首次配置
set "_BENCH_PYTHON="
if defined FLEET_PYTHON (
  call :probe "%FLEET_PYTHON%"
  goto checked
)
where py >nul 2>nul
if not errorlevel 1 call :probe py -3
if defined _BENCH_PYTHON goto checked
where python >nul 2>nul
if not errorlevel 1 call :probe python
:checked
if not defined _BENCH_PYTHON goto missing_python
echo 已找到可用的 Python 3.12 或更新版本，开始配置。
%_BENCH_PYTHON% -X utf8 -m fleet.setup_shared
if errorlevel 1 goto failed
echo 共享中心配置完成。
goto end
:missing_python
echo.
echo [未开始配置] 没有找到可运行的 Python 3.12 或更新版本。
echo Windows 应用商店的 python 占位入口不能运行本工具。
echo 请安装 Python，勾选 Add Python to PATH，再关闭本窗口并重新运行。
echo 已安装但未加入 PATH 时，可设置 FLEET_PYTHON 为 python.exe 的完整路径。
goto end
:failed
echo.
echo [配置未完成] 请保留本窗口，查看上方错误信息；不要按成功部署处理。
:end
pause
exit /b
:probe
set "_BENCH_PROBE_FILE=%CD%\.fleet-python-check-%RANDOM%-%RANDOM%.tmp"
%* -c "import sys; print('BENCH_PYTHON_OK' if sys.version_info.__ge__((3,12)) else 'BENCH_PYTHON_OLD')" >"%_BENCH_PROBE_FILE%" 2>nul
set "_BENCH_PROBE_RESULT="
set /p _BENCH_PROBE_RESULT=<"%_BENCH_PROBE_FILE%"
del /q "%_BENCH_PROBE_FILE%" >nul 2>nul
if "%_BENCH_PROBE_RESULT%"=="BENCH_PYTHON_OK" set "_BENCH_PYTHON=%*"
exit /b
