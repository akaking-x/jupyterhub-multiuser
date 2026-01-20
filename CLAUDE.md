# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **JupyterLab Portable** project for Windows USB deployment. It provides batch scripts to create a self-contained, portable JupyterLab environment that can run from a USB drive without requiring installation on the host system.

**Primary language:** Vietnamese (all user-facing text, documentation, and comments)

## Architecture

The project is designed to create the following directory structure on USB:

```
USB:\JupyterLab\
├── python\                    # Python embedded (auto-downloaded)
├── workspace\                 # User notebooks directory
├── jupyter_config\            # JupyterLab configuration
│   ├── jupyter_lab_config.py
│   ├── jupyter_server_config.json
│   ├── data\
│   └── runtime\
├── tools\                     # cloudflared.exe for tunneling
├── install.bat                # One-time setup script
├── start_jupyter.bat          # Main launcher
├── cloudflare_tunnel.bat      # Remote access via Cloudflare
└── requirements.txt           # Python dependencies
```

## Key Components

- **Python Embedded**: Uses Python 3.11.9 embedded distribution (not system Python)
- **Cloudflare Tunnel**: Enables remote access via temporary `*.trycloudflare.com` URLs
- **Password Authentication**: Uses Jupyter's hashed password system (stored in `jupyter_server_config.json`)

## Target Use Case

Accounting and document processing workflows:
- Excel/CSV processing (pandas, openpyxl, xlrd)
- Word/PDF handling (python-docx, pdfplumber, PyMuPDF)
- OCR for invoice extraction (easyocr, pytesseract)
- Financial calculations (numpy-financial)

## Development Notes

### Batch Script Conventions
- Use `chcp 65001` for UTF-8 console output
- Use `setlocal EnableDelayedExpansion` for variable expansion in loops
- Set `BASE_DIR=%~dp0` to get the script's directory path
- Configure PATH to include `%PYTHON_DIR%;%PYTHON_DIR%\Scripts`

### Environment Variables for Jupyter
```batch
set "JUPYTER_CONFIG_DIR=%CONFIG_DIR%"
set "JUPYTER_DATA_DIR=%CONFIG_DIR%\data"
set "JUPYTER_RUNTIME_DIR=%CONFIG_DIR%\runtime"
set "JUPYTER_WORKSPACE=%WORKSPACE%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
```

### Python Embedded Setup
The `.pth` file must be modified to enable pip:
```
python311.zip
.
Lib
Lib\site-packages
import site
```

### Jupyter Configuration
Key settings in `jupyter_lab_config.py`:
- `c.ServerApp.ip = '0.0.0.0'` - Required for Cloudflare Tunnel
- `c.ServerApp.allow_origin = '*'` - Required for remote access
- `c.ServerApp.trust_xheaders = True` - For proxy headers
- `c.ServerApp.websocket_compression = False` - Prevents tunnel issues
