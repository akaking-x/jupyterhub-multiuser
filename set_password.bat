@echo off
chcp 65001 >nul

set "BASE_DIR=%~dp0"
set "PYTHON_DIR=%BASE_DIR%python"
set "CONFIG_DIR=%BASE_DIR%jupyter_config"
set "PATH=%PYTHON_DIR%;%PYTHON_DIR%\Scripts;%PATH%"

echo.
echo ============================================
echo         DOI MAT KHAU JUPYTER LAB
echo ============================================
echo.

set "PASSWORD_FILE=%CONFIG_DIR%\jupyter_server_config.json"

"%PYTHON_DIR%\python.exe" "%PYTHON_DIR%\set_password.py" "%PASSWORD_FILE%"

echo.
echo Nhan phim bat ky de dong cua so nay...
pause >nul
