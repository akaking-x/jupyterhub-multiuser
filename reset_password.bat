@echo off
chcp 65001 >nul

set "BASE_DIR=%~dp0"
set "CONFIG_DIR=%BASE_DIR%jupyter_config"
set "PASSWORD_FILE=%CONFIG_DIR%\jupyter_server_config.json"

echo.
echo ============================================
echo         RESET MAT KHAU JUPYTER LAB
echo ============================================
echo.

if exist "%PASSWORD_FILE%" (
    del "%PASSWORD_FILE%"
    echo [OK] Da xoa mat khau cu.
) else (
    echo [INFO] Chua co mat khau nao duoc thiet lap.
)

echo.
echo Chay start_jupyter.bat de tao mat khau moi.
echo.
echo Nhan phim bat ky de dong cua so nay...
pause >nul
