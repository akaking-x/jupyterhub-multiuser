@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

title Cloudflare Tunnel (Khoi dong rieng)

set "BASE_DIR=%~dp0"
set "TOOLS_DIR=%BASE_DIR%tools"
set "CLOUDFLARED=%TOOLS_DIR%\cloudflared.exe"

echo.
echo ============================================
echo    CLOUDFLARE TUNNEL (KHOI DONG RIENG)
echo ============================================
echo.
echo   Luu y: File nay chi khoi dong tunnel.
echo   JupyterLab phai dang chay o cong 8888.
echo.
echo   De khoi dong ca 2: dung start_jupyter.bat
echo.
echo ============================================
echo.

if not exist "%CLOUDFLARED%" (
    echo [!] Chua co cloudflared. Dang tai...
    curl -L -o "%CLOUDFLARED%" https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe --progress-bar
    echo [OK] Da tai cloudflared
    echo.
)

:: Kiem tra JupyterLab
curl -s -o nul http://localhost:8888 2>nul
if errorlevel 1 (
    echo [CANH BAO] JupyterLab chua chay!
    echo Vui long chay start_local.bat truoc.
    echo.
    pause
    exit /b 1
)

echo [OK] JupyterLab dang chay
echo.
echo Khoi dong Cloudflare Tunnel...
echo.
echo ============================================
echo   URL cong khai se hien thi ben duoi
echo   (dang https://xxx-xxx.trycloudflare.com)
echo.
echo   Nhan Ctrl+C de dung tunnel
echo ============================================
echo.

"%CLOUDFLARED%" tunnel --url http://localhost:8888 --no-autoupdate

echo.
echo Tunnel da dung.
pause
