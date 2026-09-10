@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

rem 将 _release\backfill_engine.exe 同步到 backfill 根目录下包含 .env 的客户目录。
set "ROOT=%~dp0"
set "SOURCE=%ROOT%_release\backfill_engine.exe"

if not exist "%SOURCE%" (
    echo [ERROR] 未找到发布文件：%SOURCE%
    echo 请确认最新版 backfill_engine.exe 位于 _release 文件夹中。
    pause
    exit /b 1
)

set /a UPDATED=0
set /a FAILED=0
set "FOUND_ENV=0"

echo.
echo 开始同步：%SOURCE%
echo.

rem Backfill 客户目录直接位于当前目录下，不经过 Daily 的分类目录层。
rem 已有 backfill_engine.exe 时覆盖，不存在时新增。
for /d %%D in ("%ROOT%*") do (
    if /i not "%%~nxD"=="_release" if exist "%%~fD\.env" (
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

if "!FOUND_ENV!"=="0" echo [WARNING] backfill 目录下未找到包含 .env 的客户目录：%ROOT%

echo.
echo 同步完成：成功 !UPDATED! 个，失败 !FAILED! 个。
if not "!FAILED!"=="0" (
    echo 失败通常表示目标 EXE 仍在运行；关闭对应任务后再次执行本脚本。
)
pause
exit /b !FAILED!
