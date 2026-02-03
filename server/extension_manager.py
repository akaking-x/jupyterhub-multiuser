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

VENV_PATH = os.environ.get('JUPYTER_VENV', '/opt/jupyterlab/venv')
PIP = f'{VENV_PATH}/bin/pip'
JUPYTER = f'{VENV_PATH}/bin/jupyter'


def list_extensions():
    """List installed JupyterLab extensions"""
    result = subprocess.run(
        [JUPYTER, 'labextension', 'list'],
        capture_output=True, text=True, timeout=30
    )
    extensions = []
    for line in (result.stdout + result.stderr).splitlines():
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
