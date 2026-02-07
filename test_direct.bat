@echo off
chcp 65001 >nul

set "BASE_DIR=%~dp0"
set "PYTHON_DIR=%BASE_DIR%python"
set "WORKSPACE=%BASE_DIR%workspace"

cd /d "%WORKSPACE%"

"%PYTHON_DIR%\python.exe" -m jupyterlab --no-browser --debug --ServerApp.root_dir="%WORKSPACE%" --IdentityProvider.token="" --PasswordIdentityProvider.hashed_password="argon2:$argon2id$v=19$m=10240,t=10,p=8$j7AYY3sdpyZO3exZzvNskw$b7iGdBOZnwWwkbdagCvFMZ7IEpUnyIuOVZi2gIv+6Ms"

pause
