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

import requests

VENV_PATH = os.environ.get('JUPYTER_VENV', '/opt/jupyterlab/venv')
PIP = f'{VENV_PATH}/bin/pip'
JUPYTER = f'{VENV_PATH}/bin/jupyter'

# Curated popular JupyterLab extensions
POPULAR_EXTENSIONS = [
    {'package': 'jupyterlab-git', 'name': 'Git Integration', 'desc': 'Git extension for JupyterLab (clone, commit, push, diff, branches)'},
    {'package': 'jupyterlab-lsp', 'name': 'Language Server', 'desc': 'Code intelligence (autocomplete, diagnostics, go-to-definition)'},
    {'package': 'jupyterlab_code_formatter', 'name': 'Code Formatter', 'desc': 'Auto-format code with black, autopep8, isort'},
    {'package': 'jupyterlab-drawio', 'name': 'DrawIO Diagrams', 'desc': 'Draw.io diagram editor integration'},
    {'package': 'jupyterlab-execute-time', 'name': 'Execute Time', 'desc': 'Show cell execution time'},
    {'package': 'jupyterlab-spreadsheet-editor', 'name': 'Spreadsheet Editor', 'desc': 'Edit CSV/TSV files with spreadsheet UI'},
    {'package': 'jupyterlab_templates', 'name': 'Notebook Templates', 'desc': 'Notebook templates support'},
    {'package': 'jupyterlab-variableinspector', 'name': 'Variable Inspector', 'desc': 'Inspect variables in running kernel'},
    {'package': 'jupyterlab-system-monitor', 'name': 'System Monitor', 'desc': 'CPU/Memory usage monitor in top bar'},
    {'package': 'jupyterlab_vim', 'name': 'Vim Keybindings', 'desc': 'Vim keybindings for JupyterLab editor'},
    {'package': 'jupyterlab-night', 'name': 'Night Theme', 'desc': 'Dark theme for JupyterLab'},
    {'package': 'jupyterlab-unfold', 'name': 'Unfold', 'desc': 'File browser with nested folder tree view'},
    {'package': 'jupyterlab_widgets', 'name': 'Widgets Manager', 'desc': 'IPython widgets support for JupyterLab'},
    {'package': 'jupyter-ai', 'name': 'Jupyter AI', 'desc': 'Generative AI assistant for notebooks'},
    {'package': 'jupyterlab-github', 'name': 'GitHub Browser', 'desc': 'Browse GitHub repos directly in JupyterLab'},
    {'package': 'jupyterlab-favorites', 'name': 'Favorites', 'desc': 'Add favorite files/folders for quick access'},
    {'package': 'jupyterlab_autoversion', 'name': 'Auto Version', 'desc': 'Automatic notebook versioning'},
    {'package': 'jupyterlab-quickopen', 'name': 'Quick Open', 'desc': 'Quick file opener (Ctrl+P style)'},
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


def search_pypi(query, limit=20):
    """Search PyPI for JupyterLab extensions"""
    query = query.strip()
    if not query:
        return []
    try:
        # Use PyPI simple JSON search via warehouse API
        search_term = f"jupyterlab {query}" if 'jupyter' not in query.lower() else query
        resp = requests.get(
            'https://pypi.org/search/',
            params={'q': search_term, 'o': '-zscore'},
            headers={'Accept': 'text/html'},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        # Parse search results from HTML
        results = []
        # Match package snippets: <a class="package-snippet" href="/project/NAME/">
        #   <h3 class="package-snippet__title">...<span>NAME</span>...<span>VERSION</span></h3>
        #   <p class="package-snippet__description">DESC</p>
        import re as _re
        snippets = _re.findall(
            r'<a class="package-snippet"[^>]*href="/project/([^/]+)/"[^>]*>.*?'
            r'<span class="package-snippet__name">([^<]*)</span>\s*'
            r'<span class="package-snippet__version">([^<]*)</span>.*?'
            r'<p class="package-snippet__description">([^<]*)</p>',
            resp.text, _re.DOTALL
        )
        installed = get_installed_packages()
        for slug, name, version, desc in snippets[:limit]:
            results.append({
                'package': name.strip(),
                'version': version.strip(),
                'desc': desc.strip(),
                'installed': name.strip().lower() in installed,
            })
        return results
    except Exception:
        return []


def get_pypi_info(package_name):
    """Get package info from PyPI JSON API"""
    try:
        resp = requests.get(
            f'https://pypi.org/pypi/{package_name}/json',
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        info = data.get('info', {})
        return {
            'package': info.get('name', package_name),
            'version': info.get('version', ''),
            'desc': info.get('summary', ''),
            'author': info.get('author', ''),
            'home_page': info.get('home_page') or info.get('project_url', ''),
        }
    except Exception:
        return None


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
