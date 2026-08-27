@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

rem 本脚本放在 backfill 目录中。
rem 将 _release\backfill_engine.exe 同步到与 _release 同级且包含 .env 的客户目录。
set "ROOT=%~dp0"
set "SOURCE=%ROOT%_release\backfill_engine.exe"

if not exist "%SOURCE%" (
    echo [ERROR] 未找到发布文件：%SOURCE%
    echo 请先创建 _release 文件夹，并将最新版 backfill_engine.exe 放入其中。
    pause
    exit /b 1
)

set /a UPDATED=0
set /a FAILED=0
set "FOUND_ENV=0"

echo.
echo 开始同步：%SOURCE%
echo.

rem Backfill 客户目录直接与 _release 同级，因此只扫描当前目录的一级子目录。
for /d %%D in ("%ROOT%*") do (
    if exist "%%~fD\.env" (
        set "FOUND_ENV=1"
        copy /y "%SOURCE%" "%%~fD\backfill_engine.exe" >nul
        if errorlevel 1 (
            echo [FAILED] %%~fD\backfill_engine.exe
            set /a FAILED+=1
        ) else (
            echo [OK]     %%~fD\backfill_engine.exe
            set /a UPDATED+=1
        )
    )
)

if "!FOUND_ENV!"=="0" echo [WARNING] 当前目录的一级客户目录中未找到 .env。

echo.
echo 同步完成：成功 !UPDATED! 个，失败 !FAILED! 个。
if not "!FAILED!"=="0" (
    echo 失败通常表示目标 EXE 仍在运行；关闭对应任务后再次执行本脚本。
)
pause
exit /b !FAILED!
