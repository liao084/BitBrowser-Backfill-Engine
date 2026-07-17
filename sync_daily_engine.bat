@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

rem 将 _release\daily_engine.exe 同步到三个分类下包含 .env 的客户目录。
set "ROOT=%~dp0"
set "SOURCE=%ROOT%_release\daily_engine.exe"

if not exist "%SOURCE%" (
    echo [ERROR] 未找到发布文件：%SOURCE%
    echo 请先创建 _release 文件夹，并将最新版 daily_engine.exe 放入其中。
    pause
    exit /b 1
)

set /a UPDATED=0
set /a FAILED=0

echo.
echo 开始同步：%SOURCE%
echo.

rem 已有 daily_engine.exe 时覆盖，不存在时新增。
call :SYNC_GROUP "DY_JD"
call :SYNC_GROUP "JD"
call :SYNC_GROUP "SYCM"

echo.
echo 同步完成：成功 !UPDATED! 个，失败 !FAILED! 个。
if not "!FAILED!"=="0" (
    echo 失败通常表示目标 EXE 仍在运行；关闭对应任务后再次执行本脚本。
)
pause
exit /b !FAILED!

:SYNC_GROUP
set "GROUP_DIR=%ROOT%%~1"
if not exist "%GROUP_DIR%\" (
    echo [WARNING] 未找到分类目录：%GROUP_DIR%
    exit /b 0
)

set "FOUND_ENV=0"
for /f "delims=" %%F in ('dir /b /s /a:-d "%GROUP_DIR%\.env" 2^>nul') do (
    set "FOUND_ENV=1"
    copy /y "%SOURCE%" "%%~dpFdaily_engine.exe" >nul
    if errorlevel 1 (
        echo [FAILED] %%~dpFdaily_engine.exe
        set /a FAILED+=1
    ) else (
        echo [OK]     %%~dpFdaily_engine.exe
        set /a UPDATED+=1
    )
)
if "!FOUND_ENV!"=="0" echo [WARNING] 分类目录内未找到 .env：%GROUP_DIR%
exit /b 0
