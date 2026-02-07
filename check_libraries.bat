@echo off
chcp 65001 >nul

set "BASE_DIR=%~dp0"
set "PYTHON_DIR=%BASE_DIR%python"
set "PATH=%PYTHON_DIR%;%PYTHON_DIR%\Scripts;%PATH%"

echo.
echo ============================================
echo       KIEM TRA THU VIEN DA CAI
echo ============================================

"%PYTHON_DIR%\python.exe" "%PYTHON_DIR%\check_libs.py"

echo Nhan phim bat ky de dong cua so nay...
pause >nul
