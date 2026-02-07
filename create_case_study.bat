@echo off
chcp 65001 >nul

set "BASE_DIR=%~dp0"
set "PYTHON_DIR=%BASE_DIR%python"
set "PATH=%PYTHON_DIR%;%PYTHON_DIR%\Scripts;%PATH%"

echo.
echo ============================================
echo    TAO THU MUC VA FILE MAU CASE STUDY
echo ============================================
echo.

if not exist "%PYTHON_DIR%\python.exe" (
    echo [LOI] Chua cai dat Python. Vui long chay install.bat truoc.
    goto :end
)

"%PYTHON_DIR%\python.exe" "%PYTHON_DIR%\create_sample_files.py"

:end
echo.
echo Nhan phim bat ky de dong cua so nay...
pause >nul
