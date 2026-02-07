@echo off
chcp 65001 >nul

set "BASE_DIR=%~dp0"
set "PYTHON_DIR=%BASE_DIR%python"
set "PATH=%PYTHON_DIR%;%PYTHON_DIR%\Scripts;%PATH%"

echo.
echo ============================================
echo    XUAT HUONG DAN RA FILE PDF
echo ============================================
echo.

if not exist "%PYTHON_DIR%\python.exe" (
    echo [LOI] Chua cai dat Python. Vui long chay install.bat truoc.
    goto :end
)

echo Dang xu ly...
echo.

"%PYTHON_DIR%\python.exe" "%PYTHON_DIR%\export_pdf.py" "%BASE_DIR%"

echo.
echo ============================================

:end
echo.
echo Nhan phim bat ky de dong cua so nay...
pause >nul
