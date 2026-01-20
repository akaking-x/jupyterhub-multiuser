@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

title JupyterLab Portable + Cloudflare Tunnel

set "BASE_DIR=%~dp0"
set "PYTHON_DIR=%BASE_DIR%python"
set "WORKSPACE=%BASE_DIR%workspace"
set "CONFIG_DIR=%BASE_DIR%jupyter_config"
set "TOOLS_DIR=%BASE_DIR%tools"
set "CLOUDFLARED=%TOOLS_DIR%\cloudflared.exe"
set "TUNNEL_LOG=%TOOLS_DIR%\tunnel.log"

if not exist "%PYTHON_DIR%\python.exe" (
    echo [LOI] Chua cai dat. Vui long chay install.bat truoc.
    pause
    exit /b 1
)

if not exist "%WORKSPACE%" mkdir "%WORKSPACE%"

set "PATH=%PYTHON_DIR%;%PYTHON_DIR%\Scripts;%PATH%"

:: Tools
if exist "%TOOLS_DIR%\poppler\Library\bin" set "PATH=%TOOLS_DIR%\poppler\Library\bin;%PATH%"
if exist "%TOOLS_DIR%\tesseract" (
    set "PATH=%TOOLS_DIR%\tesseract;%PATH%"
    set "TESSDATA_PREFIX=%TOOLS_DIR%\tesseract\tessdata"
)
if exist "%TOOLS_DIR%\gs\bin" set "PATH=%TOOLS_DIR%\gs\bin;%PATH%"
if exist "%TOOLS_DIR%\java\bin" (
    set "JAVA_HOME=%TOOLS_DIR%\java"
    set "PATH=%TOOLS_DIR%\java\bin;%PATH%"
)

set "JUPYTER_CONFIG_DIR=%CONFIG_DIR%"
set "JUPYTER_DATA_DIR=%CONFIG_DIR%\data"
set "JUPYTER_RUNTIME_DIR=%CONFIG_DIR%\runtime"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

:: Dang ky kernel
"%PYTHON_DIR%\python.exe" -m ipykernel install --prefix="%PYTHON_DIR%" --name python3 --display-name "Python 3" >nul 2>&1

for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
    for /f "tokens=1" %%b in ("%%a") do set "LOCAL_IP=%%b"
)

cls
echo.
echo ============================================
echo   JUPYTER LAB PORTABLE + CLOUDFLARE TUNNEL
echo ============================================
echo.
echo   Workspace: %WORKSPACE%
echo.

:: Kiem tra cloudflared
if not exist "%CLOUDFLARED%" (
    echo Dang tai cloudflared...
    curl -L -o "%CLOUDFLARED%" https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe --progress-bar
)

:: Xoa log cu
if exist "%TUNNEL_LOG%" del "%TUNNEL_LOG%"

echo [1/2] Khoi dong Cloudflare Tunnel...
start /b "" "%CLOUDFLARED%" tunnel --url http://127.0.0.1:8888 --no-autoupdate 2>"%TUNNEL_LOG%"

:: Doi tunnel khoi dong va lay URL
echo       Dang cho tunnel san sang...
set "TUNNEL_URL="
set "RETRY=0"
:wait_tunnel
if !RETRY! geq 15 goto tunnel_timeout
timeout /t 1 /nobreak >nul
set /a RETRY+=1
for /f "tokens=*" %%u in ('findstr /i "trycloudflare.com" "%TUNNEL_LOG%" 2^>nul') do (
    for %%w in (%%u) do (
        echo %%w | findstr /i "https://.*trycloudflare.com" >nul && set "TUNNEL_URL=%%w"
    )
)
if "!TUNNEL_URL!"=="" goto wait_tunnel
goto tunnel_ready

:tunnel_timeout
echo       [CANH BAO] Khong lay duoc URL tunnel
echo       Kiem tra file: %TUNNEL_LOG%
goto start_jupyter

:tunnel_ready
echo       URL: !TUNNEL_URL!

:start_jupyter
echo.
echo [2/2] Khoi dong JupyterLab...
echo.
echo ============================================
echo   LOCAL:  http://localhost:8888/lab
echo           http://%LOCAL_IP%:8888/lab
if defined TUNNEL_URL (
echo.
echo   TUNNEL: !TUNNEL_URL!/lab
)
echo.
echo   Ctrl+C de dung tat ca
echo ============================================
echo.

cd /d "%WORKSPACE%"

:: Chay JupyterLab - config doc tu JUPYTER_CONFIG_DIR
"%PYTHON_DIR%\python.exe" -m jupyterlab --no-browser

echo.
echo Dang tat tunnel...
taskkill /f /im cloudflared.exe >nul 2>&1
if exist "%TUNNEL_LOG%" del "%TUNNEL_LOG%"
echo Da dung.
pause
