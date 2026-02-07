@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

title JupyterLab Portable (Local)

set "BASE_DIR=%~dp0"
set "PYTHON_DIR=%BASE_DIR%python"
set "WORKSPACE=%BASE_DIR%workspace"
set "CONFIG_DIR=%BASE_DIR%jupyter_config"
set "TOOLS_DIR=%BASE_DIR%tools"

if not exist "%PYTHON_DIR%\python.exe" (
    echo [LOI] Chua cai dat. Vui long chay install.bat truoc.
    echo.
    pause
    exit /b 1
)

set "PATH=%PYTHON_DIR%;%PYTHON_DIR%\Scripts;%PATH%"

:: Poppler (cho pdf2image)
if exist "%TOOLS_DIR%\poppler\Library\bin" (
    set "PATH=%TOOLS_DIR%\poppler\Library\bin;%PATH%"
)

:: Tesseract-OCR (cho pytesseract)
if exist "%TOOLS_DIR%\tesseract" (
    set "PATH=%TOOLS_DIR%\tesseract;%PATH%"
    set "TESSDATA_PREFIX=%TOOLS_DIR%\tesseract\tessdata"
)

:: Ghostscript (cho camelot-py)
if exist "%TOOLS_DIR%\gs\bin\gswin64c.exe" (
    set "PATH=%TOOLS_DIR%\gs\bin;%PATH%"
    set "GS_LIB=%TOOLS_DIR%\gs\lib;%TOOLS_DIR%\gs\fonts"
)

:: Java (cho tabula-py)
if exist "%TOOLS_DIR%\java\bin" (
    set "JAVA_HOME=%TOOLS_DIR%\java"
    set "PATH=%TOOLS_DIR%\java\bin;%PATH%"
)

set "JUPYTER_CONFIG_DIR=%CONFIG_DIR%"
set "JUPYTER_DATA_DIR=%CONFIG_DIR%\data"
set "JUPYTER_RUNTIME_DIR=%CONFIG_DIR%\runtime"
set "JUPYTER_WORKSPACE=%WORKSPACE%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

:: Dang ky lai kernel
"%PYTHON_DIR%\python.exe" -m ipykernel install --prefix="%PYTHON_DIR%" --name python3 --display-name "Python 3 (ipykernel)" >nul 2>&1

set "PASSWORD_FILE=%CONFIG_DIR%\jupyter_server_config.json"

if not exist "%PASSWORD_FILE%" (
    echo.
    echo ============================================
    echo    THIET LAP MAT KHAU LAN DAU
    echo ============================================
    echo.
    "%PYTHON_DIR%\python.exe" "%PYTHON_DIR%\set_password.py" "%PASSWORD_FILE%"
    if errorlevel 1 (
        pause
        exit /b 1
    )
    echo.
)

for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
    for /f "tokens=1" %%b in ("%%a") do set "LOCAL_IP=%%b"
)

cls
echo.
echo ============================================
echo      JUPYTER LAB PORTABLE (LOCAL)
echo ============================================
echo.
echo   Thu muc lam viec: %WORKSPACE%
echo.
echo   DUONG DAN TRUY CAP:
echo   - Local:   http://localhost:8888
echo   - Network: http://%LOCAL_IP%:8888
echo.
echo   Nhan Ctrl+C de dung server
echo ============================================
echo.

cd /d "%WORKSPACE%"

:: Password hash (thay doi bang cach chay set_password.bat)
set "PASS_HASH=argon2:$argon2id$v=19$m=10240,t=10,p=8$1v5uoIAwdBaDb6EceUZ+1g$BOVyNSopmDsQch9u8y417DXOfKntP6aKGs0T4+XRpWo"

:: Chay JupyterLab
"%PYTHON_DIR%\python.exe" -m jupyterlab ^
    --no-browser ^
    --ServerApp.root_dir="%WORKSPACE%" ^
    --ServerApp.ip=127.0.0.1 ^
    --ServerApp.port=8888 ^
    --IdentityProvider.token="" ^
    --PasswordIdentityProvider.hashed_password="%PASS_HASH%"

echo.
echo JupyterLab da dung.
pause
