@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

title JupyterLab Portable - Cai dat day du

echo.
echo ================================================================
echo          JUPYTER LAB PORTABLE - CAI DAT TU DONG
echo ================================================================
echo.

:: Thiet lap duong dan
set "BASE_DIR=%~dp0"
set "PYTHON_DIR=%BASE_DIR%python"
set "WORKSPACE=%BASE_DIR%workspace"
set "CONFIG_DIR=%BASE_DIR%jupyter_config"
set "TOOLS_DIR=%BASE_DIR%tools"

:: Phien ban
set "PYTHON_VERSION=3.11.9"
set "JAVA_VERSION=17.0.13_11"
set "TESSERACT_VERSION=5.5.0.20241111"
set "POPPLER_VERSION=24.08.0-0"

:: URLs
set "PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/python-%PYTHON_VERSION%-embed-amd64.zip"
set "JAVA_URL=https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.13+11/OpenJDK17U-jre_x64_windows_hotspot_%JAVA_VERSION%.zip"
set "POPPLER_URL=https://github.com/oschwartz10612/poppler-windows/releases/download/v%POPPLER_VERSION%/Release-%POPPLER_VERSION%.zip"
set "GHOSTSCRIPT_URL=https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10040/gs10040w64.exe"
set "TESSERACT_URL=https://github.com/UB-Mannheim/tesseract/releases/download/v%TESSERACT_VERSION%/tesseract-ocr-w64-setup-%TESSERACT_VERSION%.exe"
set "CLOUDFLARED_URL=https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

:: ================================================================
:: [1/11] TAO THU MUC
:: ================================================================
echo [1/11] Tao cau truc thu muc...
if not exist "%PYTHON_DIR%" mkdir "%PYTHON_DIR%"
if not exist "%WORKSPACE%" mkdir "%WORKSPACE%"
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"
if not exist "%CONFIG_DIR%\data" mkdir "%CONFIG_DIR%\data"
if not exist "%CONFIG_DIR%\runtime" mkdir "%CONFIG_DIR%\runtime"
if not exist "%TOOLS_DIR%" mkdir "%TOOLS_DIR%"
echo     [OK] Da tao thu muc

:: ================================================================
:: [2/11] TAI PYTHON
:: ================================================================
echo.
echo [2/11] Tai Python Embedded %PYTHON_VERSION%...
if not exist "%PYTHON_DIR%\python.exe" (
    echo     Dang tai tu python.org...
    curl -L -o "%BASE_DIR%python_embed.zip" "%PYTHON_URL%" --progress-bar
    if errorlevel 1 (
        echo     [LOI] Khong the tai Python. Kiem tra ket noi mang.
        pause
        exit /b 1
    )
    echo     Dang giai nen...
    powershell -Command "Expand-Archive -Path '%BASE_DIR%python_embed.zip' -DestinationPath '%PYTHON_DIR%' -Force"
    del "%BASE_DIR%python_embed.zip"
    echo     [OK] Da tai Python %PYTHON_VERSION%
) else (
    echo     [OK] Python da ton tai
)

:: ================================================================
:: [3/11] CAU HINH PYTHON
:: ================================================================
echo.
echo [3/11] Cau hinh Python...

set "PTH_FILE="
for %%f in ("%PYTHON_DIR%\python*.pth" "%PYTHON_DIR%\python*._pth") do (
    if exist "%%f" set "PTH_FILE=%%f"
)

if defined PTH_FILE (
    for %%f in ("%PYTHON_DIR%\python*.zip") do set "PYTHON_ZIP=%%~nxf"
    echo !PYTHON_ZIP!> "%PTH_FILE%"
    echo .>> "%PTH_FILE%"
    echo Lib>> "%PTH_FILE%"
    echo Lib\site-packages>> "%PTH_FILE%"
    echo import site>> "%PTH_FILE%"
    echo     [OK] Da cau hinh Python path
) else (
    echo     [CANH BAO] Khong tim thay file .pth
)

if not exist "%PYTHON_DIR%\Lib\site-packages" mkdir "%PYTHON_DIR%\Lib\site-packages"

:: ================================================================
:: [4/11] CAI DAT PIP
:: ================================================================
echo.
echo [4/11] Cai dat pip...
if not exist "%PYTHON_DIR%\Scripts\pip.exe" (
    echo     Dang tai get-pip.py...
    curl -L -o "%PYTHON_DIR%\get-pip.py" https://bootstrap.pypa.io/get-pip.py --progress-bar
    echo     Dang cai dat pip...
    "%PYTHON_DIR%\python.exe" "%PYTHON_DIR%\get-pip.py" --no-warn-script-location
    del "%PYTHON_DIR%\get-pip.py"
    echo     [OK] Da cai dat pip
) else (
    echo     [OK] pip da ton tai
)

set "PATH=%PYTHON_DIR%;%PYTHON_DIR%\Scripts;%PATH%"

:: ================================================================
:: [5/11] CAI DAT THU VIEN PYTHON
:: ================================================================
echo.
echo [5/11] Cai dat thu vien Python...
echo.
echo     - JupyterLab + Extensions
echo     - Xu ly Excel/CSV (pandas, openpyxl, xlrd...)
echo     - Xu ly Word/PDF (python-docx, pdfplumber...)
echo     - OCR (easyocr, pytesseract)
echo     - Ke toan/Tai chinh (numpy-financial, matplotlib...)
echo.

"%PYTHON_DIR%\python.exe" -m pip install --upgrade pip --no-warn-script-location -q

if exist "%BASE_DIR%requirements.txt" (
    "%PYTHON_DIR%\python.exe" -m pip install -r "%BASE_DIR%requirements.txt" --no-warn-script-location
) else (
    echo     [LOI] Khong tim thay requirements.txt
    "%PYTHON_DIR%\python.exe" -m pip install jupyterlab pandas openpyxl python-docx pdfplumber easyocr --no-warn-script-location
)

echo.
echo     [OK] Da cai dat thu vien Python

:: ================================================================
:: [6/11] TAO CAU HINH JUPYTER
:: ================================================================
echo.
echo [6/11] Tao cau hinh JupyterLab...

:: Tao file config bang Python
"%PYTHON_DIR%\python.exe" -c "
config = '''# JUPYTER LAB PORTABLE - CAU HINH
import os
c.ServerApp.root_dir = os.environ.get('JUPYTER_WORKSPACE', os.getcwd())
c.ServerApp.port = 8888
c.ServerApp.port_retries = 50
c.ServerApp.ip = '0.0.0.0'
c.ServerApp.password_required = True
c.ServerApp.token = ''
c.PasswordIdentityProvider.hashed_password = ''
c.ServerApp.allow_origin = '*'
c.ServerApp.allow_remote_access = True
c.ServerApp.trust_xheaders = True
c.ServerApp.allow_credentials = True
c.ServerApp.tornado_settings = {
    'headers': {
        'Content-Security-Policy': \"frame-ancestors 'self' * https://*.trycloudflare.com\",
        'X-Frame-Options': 'ALLOWALL',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-Requested-With',
    },
    'websocket_ping_interval': 30,
    'websocket_ping_timeout': 60,
}
c.ServerApp.websocket_compression = False
c.ServerApp.websocket_max_message_size = 500 * 1024 * 1024
c.ServerApp.open_browser = False
c.ServerApp.shutdown_no_activity_timeout = 0
c.MappingKernelManager.cull_idle_timeout = 0
c.MappingKernelManager.cull_connected = False
c.ServerApp.max_body_size = 1024 * 1024 * 1024
c.ServerApp.max_buffer_size = 1024 * 1024 * 1024
c.Application.log_level = 'INFO'
c.LabApp.collaborative = False
c.LabApp.default_url = '/lab'
'''
import os
config_dir = os.environ.get('CONFIG_DIR', r'%CONFIG_DIR%')
with open(os.path.join(config_dir, 'jupyter_lab_config.py'), 'w', encoding='utf-8') as f:
    f.write(config)
"

echo     [OK] Da tao jupyter_lab_config.py

:: ================================================================
:: [7/11] TAI POPPLER (cho pdf2image)
:: ================================================================
echo.
echo [7/11] Tai Poppler (cho pdf2image)...
if not exist "%TOOLS_DIR%\poppler\Library\bin\pdftoppm.exe" (
    echo     Dang tai Poppler...
    curl -L -o "%TOOLS_DIR%\poppler.zip" "%POPPLER_URL%" --progress-bar
    if errorlevel 1 (
        echo     [CANH BAO] Khong the tai Poppler
    ) else (
        echo     Dang giai nen...
        powershell -Command "Expand-Archive -Path '%TOOLS_DIR%\poppler.zip' -DestinationPath '%TOOLS_DIR%' -Force"
        :: Rename thu muc
        for /d %%d in ("%TOOLS_DIR%\poppler-*") do (
            if not exist "%TOOLS_DIR%\poppler" ren "%%d" "poppler"
        )
        del "%TOOLS_DIR%\poppler.zip" 2>nul
        echo     [OK] Da cai Poppler
    )
) else (
    echo     [OK] Poppler da ton tai
)

:: ================================================================
:: [8/11] TAI JAVA JRE (cho tabula-py)
:: ================================================================
echo.
echo [8/11] Tai Java JRE 17 (cho tabula-py)...
if not exist "%TOOLS_DIR%\java\bin\java.exe" (
    echo     Dang tai OpenJDK Temurin JRE 17...
    curl -L -o "%TOOLS_DIR%\java.zip" "%JAVA_URL%" --progress-bar
    if errorlevel 1 (
        echo     [CANH BAO] Khong the tai Java JRE
    ) else (
        echo     Dang giai nen...
        powershell -Command "Expand-Archive -Path '%TOOLS_DIR%\java.zip' -DestinationPath '%TOOLS_DIR%' -Force"
        :: Rename thu muc
        for /d %%d in ("%TOOLS_DIR%\jdk-*-jre") do (
            if not exist "%TOOLS_DIR%\java" ren "%%d" "java"
        )
        del "%TOOLS_DIR%\java.zip" 2>nul
        echo     [OK] Da cai Java JRE
    )
) else (
    echo     [OK] Java JRE da ton tai
)

:: ================================================================
:: [9/11] TAI TESSERACT-OCR (cho pytesseract)
:: ================================================================
echo.
echo [9/11] Tai Tesseract-OCR (cho pytesseract)...
if not exist "%TOOLS_DIR%\tesseract\tesseract.exe" (
    echo     Dang tai Tesseract-OCR...
    curl -L -o "%TOOLS_DIR%\tesseract-installer.exe" "%TESSERACT_URL%" --progress-bar
    if errorlevel 1 (
        echo     [CANH BAO] Khong the tai Tesseract
    ) else (
        echo     Dang cai dat Tesseract (silent mode)...
        "%TOOLS_DIR%\tesseract-installer.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR="%TOOLS_DIR%\tesseract" /COMPONENTS="main,langs\eng,langs\vie" /TASKS=""
        timeout /t 10 /nobreak >nul
        if exist "%TOOLS_DIR%\tesseract\tesseract.exe" (
            echo     [OK] Da cai Tesseract-OCR
            del "%TOOLS_DIR%\tesseract-installer.exe" 2>nul
        ) else (
            echo     [CANH BAO] Cai dat Tesseract chua hoan tat
            echo     Vui long chay: %TOOLS_DIR%\tesseract-installer.exe
        )
    )
) else (
    echo     [OK] Tesseract da ton tai
)

:: ================================================================
:: [10/11] TAI GHOSTSCRIPT (cho camelot-py)
:: ================================================================
echo.
echo [10/11] Tai Ghostscript (cho camelot-py)...
if not exist "%TOOLS_DIR%\gs\bin\gswin64c.exe" (
    echo     Dang tai Ghostscript...
    curl -L -o "%TOOLS_DIR%\gs-installer.exe" "%GHOSTSCRIPT_URL%" --progress-bar
    if errorlevel 1 (
        echo     [CANH BAO] Khong the tai Ghostscript
    ) else (
        echo     Dang cai dat Ghostscript (silent mode)...
        "%TOOLS_DIR%\gs-installer.exe" /S /D=%TOOLS_DIR%\gs
        timeout /t 15 /nobreak >nul
        if exist "%TOOLS_DIR%\gs\bin\gswin64c.exe" (
            echo     [OK] Da cai Ghostscript
            del "%TOOLS_DIR%\gs-installer.exe" 2>nul
        ) else (
            echo     [CANH BAO] Cai dat Ghostscript chua hoan tat
        )
    )
) else (
    echo     [OK] Ghostscript da ton tai
)

:: ================================================================
:: [11/11] TAI CLOUDFLARED
:: ================================================================
echo.
echo [11/11] Tai Cloudflared...
if not exist "%TOOLS_DIR%\cloudflared.exe" (
    echo     Dang tai cloudflared...
    curl -L -o "%TOOLS_DIR%\cloudflared.exe" "%CLOUDFLARED_URL%" --progress-bar
    echo     [OK] Da tai cloudflared
) else (
    echo     [OK] cloudflared da ton tai
)

:: ================================================================
:: KIEM TRA KET QUA
:: ================================================================
echo.
echo ================================================================
echo                      KIEM TRA CAI DAT
echo ================================================================
echo.

set "ALL_OK=1"

if exist "%PYTHON_DIR%\python.exe" (
    echo   [OK] Python %PYTHON_VERSION%
) else (
    echo   [X]  Python - THIEU
    set "ALL_OK=0"
)

if exist "%TOOLS_DIR%\poppler\Library\bin\pdftoppm.exe" (
    echo   [OK] Poppler - pdf2image
) else (
    echo   [X]  Poppler - THIEU
    set "ALL_OK=0"
)

if exist "%TOOLS_DIR%\java\bin\java.exe" (
    echo   [OK] Java JRE - tabula-py
) else (
    echo   [X]  Java JRE - THIEU
    set "ALL_OK=0"
)

if exist "%TOOLS_DIR%\tesseract\tesseract.exe" (
    echo   [OK] Tesseract-OCR - pytesseract
) else (
    echo   [X]  Tesseract - THIEU (EasyOCR van hoat dong)
    set "ALL_OK=0"
)

if exist "%TOOLS_DIR%\gs\bin\gswin64c.exe" (
    echo   [OK] Ghostscript - camelot-py
) else (
    echo   [X]  Ghostscript - THIEU
    set "ALL_OK=0"
)

if exist "%TOOLS_DIR%\cloudflared.exe" (
    echo   [OK] Cloudflared - tunnel
) else (
    echo   [X]  Cloudflared - THIEU
    set "ALL_OK=0"
)

echo.
echo ================================================================
if "%ALL_OK%"=="1" (
    echo              CAI DAT HOAN TAT 100%%!
) else (
    echo           CAI DAT HOAN TAT (mot so tool thieu)
)
echo ================================================================
echo.
echo   KHOI DONG:
echo   - start_jupyter.bat : JupyterLab + Cloudflare Tunnel
echo   - start_local.bat   : JupyterLab local only
echo.
echo   Lan dau se yeu cau tao mat khau
echo   Truy cap: http://localhost:8888
echo.
echo ================================================================

pause
exit /b 0
