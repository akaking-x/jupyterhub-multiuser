@echo off
chcp 65001 >nul

set "BASE_DIR=%~dp0"
set "PYTHON_DIR=%BASE_DIR%python"
set "WORKSPACE=%BASE_DIR%workspace"

cd /d "%WORKSPACE%"

echo Testing with NO authentication...
echo.

"%PYTHON_DIR%\python.exe" -m jupyterlab --no-browser --ServerApp.root_dir="%WORKSPACE%" --ServerApp.token="" --ServerApp.password="" --IdentityProvider.token="" --ServerApp.disable_check_xsrf=True --ServerApp.allow_origin="*"

pause
