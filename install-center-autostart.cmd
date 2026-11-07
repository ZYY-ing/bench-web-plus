@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo 一次性安装开机中心：请以 Windows 管理员身份运行。
echo 会复制到受保护的 ProgramData 目录，不会从可编辑工作区运行 SYSTEM 任务。
echo 请先正常停止当前中心；不会强制结束进程，不修改服务器或 vLLM。
if defined FLEET_PYTHON (
  "%FLEET_PYTHON%" -X utf8 -m fleet.service install-boot
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    python -X utf8 -m fleet.service install-boot
  ) else (
    py -3 -X utf8 -m fleet.service install-boot
  )
)
if errorlevel 1 echo 安装未完成，请查看上方原因；现有服务未按成功处理。
pause
