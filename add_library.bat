@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

set "BASE_DIR=%~dp0"
set "PYTHON_DIR=%BASE_DIR%python"
set "PATH=%PYTHON_DIR%;%PYTHON_DIR%\Scripts;%PATH%"

echo.
echo ============================================
echo       CAI THEM THU VIEN PYTHON
echo ============================================
echo.
echo Cach su dung:
echo   - Nhap ten thu vien: flask
echo   - Nang cap thu vien: flask -U
echo   - Cai phien ban cu the: flask==2.0.0
echo   - Nhap 'list' de xem thu vien da cai
echo   - Nhap 'exit' de thoat
echo.
echo ============================================
echo.

:input_loop
set "LIB_NAME="
set /p "LIB_NAME=Thu vien: "

if /i "%LIB_NAME%"=="exit" goto :end
if /i "%LIB_NAME%"=="list" goto :list
if "%LIB_NAME%"=="" goto :input_loop

echo.

:: Kiem tra xem co flag -U khong
echo %LIB_NAME% | findstr /i "\-U \-\-upgrade" >nul
if not errorlevel 1 (
    echo Dang nang cap %LIB_NAME%...
    "%PYTHON_DIR%\python.exe" -m pip install --upgrade %LIB_NAME% --no-warn-script-location
) else (
    echo Dang cai dat %LIB_NAME%...
    "%PYTHON_DIR%\python.exe" -m pip install %LIB_NAME% --no-warn-script-location
)

echo.
echo --------------------------------------------
goto :input_loop

:list
echo.
echo Danh sach thu vien da cai:
echo.
"%PYTHON_DIR%\python.exe" -m pip list
echo.
echo --------------------------------------------
goto :input_loop

:end
echo.
echo Da thoat.
echo Nhan phim bat ky de dong cua so nay...
pause >nul
