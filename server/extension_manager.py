#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JupyterLab Extension Manager
Manage extensions from admin dashboard
"""

import subprocess
import re
import pwd
import os
import json


VENV_PATH = os.environ.get('JUPYTER_VENV', '/opt/jupyterlab/venv')
PIP = f'{VENV_PATH}/bin/pip'
JUPYTER = f'{VENV_PATH}/bin/jupyter'

# Curated JupyterLab extensions catalog
POPULAR_EXTENSIONS = [
    # --- Developer Tools ---
    {'package': 'jupyterlab-git', 'name': 'Git Integration', 'desc': 'Git extension for JupyterLab (clone, commit, push, diff, branches)', 'cat': 'dev'},
    {'package': 'jupyterlab-lsp', 'name': 'Language Server', 'desc': 'Code intelligence (autocomplete, diagnostics, go-to-definition)', 'cat': 'dev'},
    {'package': 'jupyterlab_code_formatter', 'name': 'Code Formatter', 'desc': 'Auto-format code with black, autopep8, isort', 'cat': 'dev'},
    {'package': 'jupyterlab-variableinspector', 'name': 'Variable Inspector', 'desc': 'Inspect variables in running kernel', 'cat': 'dev'},
    {'package': 'jupyterlab_vim', 'name': 'Vim Keybindings', 'desc': 'Vim keybindings for JupyterLab editor', 'cat': 'dev'},
    {'package': 'jupyterlab-quickopen', 'name': 'Quick Open', 'desc': 'Quick file opener (Ctrl+P style)', 'cat': 'dev'},
    {'package': 'jupyterlab-execute-time', 'name': 'Execute Time', 'desc': 'Show cell execution time in each cell', 'cat': 'dev'},
    {'package': 'jupyterlab_templates', 'name': 'Notebook Templates', 'desc': 'Create and use notebook templates', 'cat': 'dev'},
    {'package': 'jupyterlab_autoversion', 'name': 'Auto Version', 'desc': 'Automatic notebook versioning on save', 'cat': 'dev'},
    {'package': 'jupyterlab-github', 'name': 'GitHub Browser', 'desc': 'Browse GitHub repos directly in JupyterLab', 'cat': 'dev'},
    {'package': 'jupyterlab-recents', 'name': 'Recent Files', 'desc': 'Quick access to recently opened files', 'cat': 'dev'},
    {'package': 'jupyterlab-cell-flash', 'name': 'Cell Flash', 'desc': 'Flash executed cells to show which ran', 'cat': 'dev'},
    {'package': 'jupyterlab-skip-traceback', 'name': 'Skip Traceback', 'desc': 'Collapse long Python tracebacks', 'cat': 'dev'},
    {'package': 'jupyterlab-code-snippets', 'name': 'Code Snippets', 'desc': 'Save and reuse code snippets', 'cat': 'dev'},
    # --- UI & Themes ---
    {'package': 'jupyterlab-night', 'name': 'Night Theme', 'desc': 'Dark theme for JupyterLab', 'cat': 'ui'},
    {'package': 'jupyterlab-unfold', 'name': 'Unfold', 'desc': 'File browser with nested folder tree view', 'cat': 'ui'},
    {'package': 'jupyterlab-favorites', 'name': 'Favorites', 'desc': 'Add favorite files/folders for quick access', 'cat': 'ui'},
    {'package': 'jupyterlab-system-monitor', 'name': 'System Monitor', 'desc': 'CPU/Memory usage monitor in top bar', 'cat': 'ui'},
    {'package': 'jupyterlab-topbar-text', 'name': 'Top Bar Text', 'desc': 'Add custom text to the top bar', 'cat': 'ui'},
    {'package': 'jupyterlab_miami_nights', 'name': 'Miami Nights Theme', 'desc': 'Synthwave/retro neon theme', 'cat': 'ui'},
    {'package': 'jupyterlab-horizon-theme', 'name': 'Horizon Theme', 'desc': 'Warm dark theme inspired by VS Code Horizon', 'cat': 'ui'},
    {'package': 'jupyterlab_theme_solarized_dark', 'name': 'Solarized Dark', 'desc': 'Solarized dark color theme', 'cat': 'ui'},
    # --- Data & Visualization ---
    {'package': 'jupyterlab-spreadsheet-editor', 'name': 'Spreadsheet Editor', 'desc': 'Edit CSV/TSV files with spreadsheet UI', 'cat': 'data'},
    {'package': 'jupyterlab-drawio', 'name': 'DrawIO Diagrams', 'desc': 'Draw.io diagram editor integration', 'cat': 'data'},
    {'package': 'jupyterlab_widgets', 'name': 'Widgets Manager', 'desc': 'IPython widgets support for JupyterLab', 'cat': 'data'},
    {'package': 'jupyterlab-plotly', 'name': 'Plotly', 'desc': 'Interactive Plotly charts in JupyterLab', 'cat': 'data'},
    {'package': 'jupyterlab-dash', 'name': 'Dash', 'desc': 'View Plotly Dash apps inside JupyterLab', 'cat': 'data'},
    {'package': 'jupyterlab-geojson', 'name': 'GeoJSON Viewer', 'desc': 'Render GeoJSON files as interactive maps', 'cat': 'data'},
    {'package': 'jupyterlab-fasta', 'name': 'FASTA Viewer', 'desc': 'Render FASTA/FASTQ bioinformatics files', 'cat': 'data'},
    {'package': 'jupyterlab-tabular-data-editor', 'name': 'Tabular Data Editor', 'desc': 'Visual editor for tabular data files', 'cat': 'data'},
    # --- AI & Productivity ---
    {'package': 'jupyter-ai', 'name': 'Jupyter AI', 'desc': 'Generative AI assistant for notebooks (ChatGPT, Claude)', 'cat': 'ai'},
    {'package': 'jupyterlab-commenting', 'name': 'Commenting', 'desc': 'Add comments and annotations to cells', 'cat': 'ai'},
    {'package': 'jupyterlab-spellchecker', 'name': 'Spell Checker', 'desc': 'Spell checking for markdown and text cells', 'cat': 'ai'},
    {'package': 'jupyterlab-latex', 'name': 'LaTeX Editor', 'desc': 'Live editing and preview of LaTeX documents', 'cat': 'ai'},
    {'package': 'jupyterlab-citation-manager', 'name': 'Citation Manager', 'desc': 'Manage citations and references in notebooks', 'cat': 'ai'},
    # --- Language Support ---
    {'package': 'jupyterlab-language-pack-vi-VN', 'name': 'Vietnamese Language', 'desc': 'Vietnamese language pack for JupyterLab', 'cat': 'lang'},
    {'package': 'jupyterlab-language-pack-zh-CN', 'name': 'Chinese (Simplified)', 'desc': 'Simplified Chinese language pack', 'cat': 'lang'},
    {'package': 'jupyterlab-language-pack-ja-JP', 'name': 'Japanese Language', 'desc': 'Japanese language pack for JupyterLab', 'cat': 'lang'},
    {'package': 'jupyterlab-language-pack-ko-KR', 'name': 'Korean Language', 'desc': 'Korean language pack for JupyterLab', 'cat': 'lang'},
    {'package': 'jupyterlab-language-pack-fr-FR', 'name': 'French Language', 'desc': 'French language pack for JupyterLab', 'cat': 'lang'},
    # --- Kernel & Runtime ---
    {'package': 'ipykernel', 'name': 'IPython Kernel', 'desc': 'IPython kernel for Jupyter', 'cat': 'kernel'},
    {'package': 'bash_kernel', 'name': 'Bash Kernel', 'desc': 'Bash kernel for Jupyter notebooks', 'cat': 'kernel'},
    {'package': 'xeus-python', 'name': 'Xeus Python', 'desc': 'Alternative Python kernel with debugging support', 'cat': 'kernel'},
    {'package': 'xeus-sql', 'name': 'Xeus SQL', 'desc': 'SQL kernel for Jupyter notebooks', 'cat': 'kernel'},
    {'package': 'ijavascript', 'name': 'JavaScript Kernel', 'desc': 'JavaScript (Node.js) kernel for Jupyter', 'cat': 'kernel'},
    # --- File Management ---
    {'package': 'jupyter-archive', 'name': 'Archive', 'desc': 'Download folders as zip/tar archives', 'cat': 'file'},
    {'package': 'jupyterlab-s3-browser', 'name': 'S3 Browser', 'desc': 'Browse and manage S3 buckets in JupyterLab', 'cat': 'file'},
    {'package': 'jupyterlab-google-drive', 'name': 'Google Drive', 'desc': 'Google Drive integration for JupyterLab', 'cat': 'file'},
]


def _strip_ansi(text):
    """Remove ANSI escape codes from text"""
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def list_extensions():
    """List installed JupyterLab extensions"""
    result = subprocess.run(
        [JUPYTER, 'labextension', 'list'],
        capture_output=True, text=True, timeout=30
    )
    output = _strip_ansi(result.stdout + result.stderr)
    extensions = []
    for line in output.splitlines():
        line = line.strip()
        # Match lines like: @jupyterlab/some-ext v4.0.0 enabled OK
        m = re.match(r'^(\S+)\s+v?([\d.]+\S*)\s+(enabled|disabled)', line)
        if m:
            extensions.append({
                'name': m.group(1),
                'version': m.group(2),
                'status': m.group(3),
            })
    return extensions


def get_installed_packages():
    """Get set of installed pip package names (lowercase)"""
    result = subprocess.run(
        [PIP, 'list', '--format=json'],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return set()
    try:
        pkgs = json.loads(result.stdout)
        return {p['name'].lower() for p in pkgs}
    except Exception:
        return set()


def get_popular_extensions():
    """Return popular extensions with install status"""
    installed = get_installed_packages()
    result = []
    for ext in POPULAR_EXTENSIONS:
        result.append({
            **ext,
            'installed': ext['package'].lower() in installed,
        })
    return result


def search_catalog(query):
    """Search the curated extension catalog"""
    query = query.strip().lower()
    if not query:
        return POPULAR_EXTENSIONS
    results = []
    for ext in POPULAR_EXTENSIONS:
        searchable = f"{ext['package']} {ext['name']} {ext['desc']} {ext.get('cat','')}".lower()
        if query in searchable:
            results.append(ext)
    return results


def install_extension(package_name):
    """Install a JupyterLab extension via pip"""
    package_name = package_name.strip()
    if not package_name or not re.match(r'^[a-zA-Z0-9_\-\.>=<\[\]]+$', package_name):
        return False, "Invalid package name"
    result = subprocess.run(
        [PIP, 'install', package_name],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode == 0:
        return True, result.stdout.strip().split('\n')[-1]
    return False, result.stderr.strip().split('\n')[-1] if result.stderr else "Install failed"


def uninstall_extension(package_name):
    """Uninstall a JupyterLab extension via pip"""
    package_name = package_name.strip()
    if not package_name or not re.match(r'^[a-zA-Z0-9_\-\.]+$', package_name):
        return False, "Invalid package name"
    result = subprocess.run(
        [PIP, 'uninstall', '-y', package_name],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode == 0:
        return True, f"Uninstalled {package_name}"
    return False, result.stderr.strip().split('\n')[-1] if result.stderr else "Uninstall failed"


def restart_all_jupyterlab():
    """Restart all running JupyterLab instances"""
    restarted = []
    base_port = int(os.environ.get('JUPYTER_BASE_PORT', 9800))
    for p in pwd.getpwall():
        if p.pw_uid >= 1000 and '/home/' in p.pw_dir:
            username = p.pw_name
            port = base_port + (p.pw_uid - 1000)
            # Check if running
            check = subprocess.run(
                ['/opt/jupyterhub/lab_manager.sh', 'status', username],
                capture_output=True, text=True
            )
            if 'running' in check.stdout:
                subprocess.run(['/opt/jupyterhub/lab_manager.sh', 'stop', username])
                subprocess.run(['/opt/jupyterhub/lab_manager.sh', 'start', username, str(port)])
                restarted.append(username)
    return restarted
