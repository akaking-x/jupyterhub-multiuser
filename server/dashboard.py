#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JupyterHub Multi-User Dashboard
A Flask-based dashboard for managing JupyterLab instances
"""

from flask import Flask, render_template_string, request, session, redirect, Response, jsonify
import subprocess
import secrets
import string
import pam
import pwd
import os
import time
import socket
import json
import jwt
import hashlib
from datetime import datetime

from pymongo import MongoClient

from extension_manager import (
    list_extensions, install_extension, uninstall_extension, restart_all_jupyterlab,
    get_popular_extensions, search_catalog, get_installed_packages,
)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta

from s3_manager import (
    get_s3_config, has_s3_config, test_s3_connection,
    list_workspace, mkdir_workspace, delete_workspace,
    upload_to_workspace, stream_workspace_file, read_workspace_text,
    list_s3, mkdir_s3, delete_s3, upload_to_s3,
    start_transfer, get_transfer_status,
    get_shared_s3_config, list_s3_recursive,
    stream_s3_object, stream_s3_folder_as_zip, read_s3_text,
)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

# OnlyOffice Configuration
ONLYOFFICE_URL = os.environ.get('ONLYOFFICE_URL', '/onlyoffice')
ONLYOFFICE_JWT_SECRET = os.environ.get('ONLYOFFICE_JWT_SECRET', 'jupyterhub_onlyoffice_secret_2024')
# Internal URL for OnlyOffice to fetch files (use public IP for Docker container access)
ONLYOFFICE_FILE_HOST = os.environ.get('ONLYOFFICE_FILE_HOST', 'http://103.82.39.35:9998')

# Configuration from environment
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
BASE_PORT = int(os.environ.get('JUPYTER_BASE_PORT', 9800))

# MongoDB connection
MONGO_HOST = os.environ.get('MONGO_HOST', 'jupyterhub-mongodb')
MONGO_PORT = int(os.environ.get('MONGO_PORT', 27018))
MONGO_USER = os.environ.get('MONGO_USER', 'jupyterhub')
MONGO_PASS = os.environ.get('MONGO_PASS', '')
MONGO_DB = os.environ.get('MONGO_DB', 'jupyterhub')

_mongo_client = None
_mongo_db = None

def get_db():
    """Get MongoDB database connection (lazy init)"""
    global _mongo_client, _mongo_db
    if _mongo_db is None:
        if MONGO_PASS:
            uri = f"mongodb://{MONGO_USER}:{MONGO_PASS}@{MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}?authSource=admin"
        else:
            uri = f"mongodb://{MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}"
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        _mongo_db = _mongo_client[MONGO_DB]
    return _mongo_db

def generate_password(length=12):
    """Generate a random password"""
    chars = string.ascii_letters + string.digits + "!@#$%^&"
    return ''.join(secrets.choice(chars) for _ in range(length))

def get_users():
    """Get list of regular users (not system users)"""
    users = []
    for p in pwd.getpwall():
        if p.pw_uid >= 1000 and p.pw_name != ADMIN_USER and '/home/' in p.pw_dir:
            users.append({'name': p.pw_name, 'home': p.pw_dir, 'uid': p.pw_uid})
    return sorted(users, key=lambda x: x['name'])

def get_user_port(username):
    """Calculate port for user based on UID"""
    try:
        uid = pwd.getpwnam(username).pw_uid
        return BASE_PORT + (uid - 1000)
    except:
        return BASE_PORT

def set_user_password(username, password):
    """Set password for a system user"""
    proc = subprocess.run(f"echo '{username}:{password}' | chpasswd", shell=True, capture_output=True)
    return proc.returncode == 0

def regenerate_nginx():
    """Regenerate nginx config after user changes"""
    subprocess.run(['/opt/jupyterhub/gen_nginx.sh'], capture_output=True)

def create_system_user(username):
    """Create a new system user with workspace"""
    try:
        subprocess.run(['useradd', '-m', '-s', '/bin/bash', username], check=True)
        subprocess.run(['mkdir', '-p', f'/home/{username}/workspace'], check=True)
        subprocess.run(['chown', '-R', f'{username}:{username}', f'/home/{username}'], check=True)
        regenerate_nginx()
        return True
    except:
        return False

def delete_system_user(username):
    """Delete a system user"""
    stop_jupyter(username)
    subprocess.run(['pkill', '-u', username], capture_output=True)
    subprocess.run(['userdel', '-rf', username], capture_output=True)
    regenerate_nginx()
    return True

def user_exists(username):
    """Check if user exists"""
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False

def check_user_auth(username, password):
    """Authenticate user via PAM"""
    p = pam.pam()
    return p.authenticate(username, password)

def start_jupyter(username):
    """Start JupyterLab for user and wait for it to be ready"""
    port = get_user_port(username)
    subprocess.run(['/opt/jupyterhub/lab_manager.sh', 'start', username, str(port)])

    # Wait for JupyterLab to be ready (up to 15 seconds)
    for _ in range(30):
        time.sleep(0.5)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', port))
            sock.close()
            if result == 0:
                time.sleep(1)
                return port
        except:
            pass
    return port

def stop_jupyter(username):
    """Stop JupyterLab for user"""
    subprocess.run(['/opt/jupyterhub/lab_manager.sh', 'stop', username])

def is_jupyter_running(username):
    """Check if JupyterLab is running for user"""
    result = subprocess.run(['/opt/jupyterhub/lab_manager.sh', 'status', username], capture_output=True, text=True)
    return 'running' in result.stdout

# ===========================================
# HTML Templates
# ===========================================

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.navbar{background:#1e293b;padding:15px 30px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #334155}
.navbar h1{font-size:20px;color:#fff}
.navbar h1 span{color:#818cf8}
.nav-right{display:flex;align-items:center;gap:15px}
.nav-right span{color:#94a3b8}
.nav-links{display:flex;gap:10px;align-items:center}
.nav-links a{color:#94a3b8;text-decoration:none;padding:6px 12px;border-radius:6px;font-size:13px;transition:all .2s}
.nav-links a:hover,.nav-links a.active{color:#fff;background:#334155}
.btn{padding:10px 20px;border:none;border-radius:8px;cursor:pointer;font-weight:500;transition:all .2s;text-decoration:none;display:inline-block;font-size:14px}
.btn-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
.btn-success{background:#10b981;color:#fff}
.btn-danger{background:#ef4444;color:#fff}
.btn-secondary{background:#475569;color:#fff}
.btn-warning{background:#f59e0b;color:#000}
.btn:hover{transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,.3)}
.btn-sm{padding:6px 12px;font-size:13px}
.container{max-width:1000px;margin:0 auto;padding:30px}
.container-wide{max-width:1400px;margin:0 auto;padding:30px}
.card{background:#1e293b;border-radius:16px;margin-bottom:24px;border:1px solid #334155;overflow:hidden}
.card-header{padding:20px 24px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center}
.card-header h2{font-size:18px;font-weight:600}
.card-body{padding:24px}
.form-group{margin-bottom:20px}
.form-group label{display:block;margin-bottom:8px;color:#94a3b8;font-size:14px}
.form-control{width:100%;padding:12px 16px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;font-size:15px}
.form-control:focus{outline:none;border-color:#6366f1}
.form-row{display:flex;gap:15px}
.form-row .form-group{flex:1}
table{width:100%;border-collapse:collapse}
th,td{padding:14px 16px;text-align:left;border-bottom:1px solid #334155}
th{color:#94a3b8;font-weight:500;font-size:13px;text-transform:uppercase}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.alert{padding:16px 20px;border-radius:10px;margin-bottom:20px}
.alert-success{background:rgba(16,185,129,.2);border:1px solid #10b981;color:#10b981}
.alert-error{background:rgba(239,68,68,.2);border:1px solid #ef4444;color:#ef4444}
.alert-info{background:rgba(99,102,241,.2);border:1px solid #6366f1;color:#818cf8}
.password-box{background:#0f172a;padding:16px;border-radius:8px;font-family:monospace;font-size:20px;text-align:center;border:2px dashed #6366f1;margin:15px 0;color:#10b981;letter-spacing:2px}
.login-container{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.login-box{background:#1e293b;padding:40px;border-radius:20px;width:400px;max-width:100%;border:1px solid #334155}
.login-header{text-align:center;margin-bottom:30px}
.login-header .icon{font-size:48px;margin-bottom:15px}
.login-header h1{font-size:24px;margin-bottom:8px}
.login-header p{color:#94a3b8;font-size:14px}
.empty{text-align:center;padding:40px;color:#64748b}
iframe{width:100%;height:calc(100vh - 60px);border:none}
.split-pane{display:flex;gap:20px;height:calc(100vh - 160px)}
.split-pane .pane{flex:1;background:#1e293b;border-radius:12px;border:1px solid #334155;display:flex;flex-direction:column;overflow:hidden}
.pane-header{padding:12px 16px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;background:#1e293b}
.pane-header h3{font-size:14px;font-weight:600}
.breadcrumb{display:flex;gap:4px;align-items:center;font-size:13px;color:#94a3b8;flex-wrap:wrap}
.breadcrumb a{color:#818cf8;text-decoration:none}
.breadcrumb a:hover{text-decoration:underline}
.file-list{flex:1;overflow-y:auto;padding:8px}
.file-item{display:flex;align-items:center;padding:8px 12px;border-radius:6px;cursor:pointer;gap:10px;font-size:14px}
.file-item:hover{background:#334155}
.file-item.selected{background:rgba(99,102,241,.2);border:1px solid #6366f1}
.file-item input[type=checkbox]{accent-color:#6366f1}
.file-icon{width:20px;text-align:center}
.file-size{color:#64748b;font-size:12px;margin-left:auto}
.transfer-bar{display:flex;gap:10px;justify-content:center;align-items:center;padding:15px}
.transfer-bar .btn{min-width:120px}
.progress-container{padding:10px 16px;border-top:1px solid #334155;display:none}
.progress-bar{height:6px;background:#334155;border-radius:3px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,#6366f1,#10b981);transition:width .3s;width:0%}
.progress-text{font-size:12px;color:#94a3b8;margin-top:4px}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500}
.tag-green{background:rgba(16,185,129,.2);color:#10b981}
.tag-red{background:rgba(239,68,68,.2);color:#ef4444}
.tag-blue{background:rgba(99,102,241,.2);color:#818cf8}
</style>
"""

LOGIN_PAGE = CSS + """<!DOCTYPE html><html><head><title>JupyterHub</title></head><body>
<div class="login-container">
    <div class="login-box">
        <div class="login-header">
            <div class="icon">&#128218;</div>
            <h1>JupyterHub</h1>
            <p>Data Science Workspace</p>
        </div>
        {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
        <form method="post">
            <div class="form-group"><label>Username</label><input type="text" name="username" class="form-control" required autofocus></div>
            <div class="form-group"><label>Password</label><input type="password" name="password" class="form-control" required></div>
            <button type="submit" class="btn btn-primary" style="width:100%;padding:14px">Sign In</button>
        </form>
        <div style="text-align:center;margin-top:20px"><a href="/change-password" style="color:#94a3b8">Change Password</a></div>
    </div>
</div></body></html>"""

CHANGE_PW = CSS + """<!DOCTYPE html><html><head><title>Change Password</title></head><body>
<div class="login-container">
    <div class="login-box">
        <div class="login-header"><div class="icon">&#128274;</div><h1>Change Password</h1></div>
        {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
        {% if success %}<div class="alert alert-success">{{ success }}</div>{% endif %}
        <form method="post">
            <div class="form-group"><label>Username</label><input type="text" name="username" class="form-control" required></div>
            <div class="form-group"><label>Current Password</label><input type="password" name="old_password" class="form-control" required></div>
            <div class="form-group"><label>New Password</label><input type="password" name="new_password" class="form-control" required></div>
            <div class="form-group"><label>Confirm Password</label><input type="password" name="confirm_password" class="form-control" required></div>
            <button type="submit" class="btn btn-primary" style="width:100%">Change Password</button>
        </form>
        <div style="text-align:center;margin-top:20px"><a href="/" style="color:#94a3b8">Back to Login</a></div>
    </div>
</div></body></html>"""

ADMIN_DASH = CSS + """<!DOCTYPE html><html><head><title>Admin Dashboard</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span> Admin</h1>
<div class="nav-right">
    <div class="nav-links">
        <a href="/dashboard" class="active">Users</a>
        <a href="/admin/s3-config">S3 Config</a>
        <a href="/admin/extensions">Extensions</a>
        {% if has_shared %}<a href="/shared-space">Shared Space</a>{% endif %}
    </div>
    <span>admin</span><a href="/logout" class="btn btn-secondary btn-sm">Logout</a>
</div></nav>
<div class="container">
    {% if message %}<div class="alert {{ 'alert-success' if success else 'alert-error' }}">{{ message }}
    {% if new_password %}<div class="password-box">{{ new_password }}</div><small>Copy and share with user</small>{% endif %}</div>{% endif %}

    <div class="card"><div class="card-header"><h2>&#10133; Create New User</h2></div>
    <div class="card-body"><form method="post" action="/admin/create"><div class="form-row">
    <div class="form-group"><label>Username</label><input type="text" name="username" class="form-control" required pattern="[a-z0-9_]+"></div>
    <div class="form-group" style="flex:0 0 180px;display:flex;align-items:flex-end"><button class="btn btn-success" style="width:100%">Create User</button></div>
    </div></form></div></div>

    <div class="card"><div class="card-header"><h2>&#128101; Users ({{ users|length }})</h2></div>
    <div class="card-body" style="padding:0">{% if users %}<table><thead><tr><th>User</th><th>Actions</th></tr></thead><tbody>
    {% for u in users %}<tr><td><strong>{{ u.name }}</strong></td><td><div class="actions">
    <form method="post" action="/admin/reset" style="display:inline"><input type="hidden" name="username" value="{{ u.name }}"><button class="btn btn-primary btn-sm">Reset PW</button></form>
    <form method="post" action="/admin/delete" style="display:inline" onsubmit="return confirm('Delete?')"><input type="hidden" name="username" value="{{ u.name }}"><button class="btn btn-danger btn-sm">Delete</button></form>
    </div></td></tr>{% endfor %}</tbody></table>{% else %}<div class="empty">No users</div>{% endif %}</div></div>
</div></body></html>"""

USER_MENU = """<!DOCTYPE html><html><head><title>JupyterHub Desktop</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:linear-gradient(135deg,#0f172a 0%,#1e1b4b 100%);color:#e2e8f0;height:100vh;overflow:hidden;user-select:none}
.taskbar{position:fixed;bottom:0;left:0;right:0;height:48px;background:rgba(30,41,59,.95);backdrop-filter:blur(10px);border-top:1px solid #334155;display:flex;align-items:center;padding:0 8px;z-index:9999}
.start-btn{background:linear-gradient(135deg,#6366f1,#8b5cf6);border:none;color:#fff;padding:8px 16px;border-radius:6px;cursor:pointer;font-weight:600;font-size:14px;display:flex;align-items:center;gap:8px}
.start-btn:hover{filter:brightness(1.1)}
.taskbar-apps{display:flex;gap:4px;margin-left:12px;flex:1}
.taskbar-item{background:transparent;border:none;color:#94a3b8;padding:8px 12px;border-radius:6px;cursor:pointer;font-size:13px;display:flex;align-items:center;gap:6px;max-width:160px}
.taskbar-item:hover{background:#334155;color:#fff}
.taskbar-item.active{background:#475569;color:#fff;border-bottom:2px solid #6366f1}
.taskbar-right{display:flex;align-items:center;gap:12px;color:#94a3b8;font-size:13px}
.taskbar-right span{padding:4px 8px;border-radius:4px;cursor:pointer}
.taskbar-right span:hover{background:#334155}
.start-menu{position:fixed;bottom:56px;left:8px;width:320px;background:rgba(30,41,59,.98);backdrop-filter:blur(20px);border-radius:12px;border:1px solid #334155;display:none;z-index:10000;box-shadow:0 -10px 40px rgba(0,0,0,.5)}
.start-menu.show{display:block}
.start-menu-header{padding:16px;border-bottom:1px solid #334155}
.start-menu-header h3{font-size:14px;color:#94a3b8}
.menu-section{padding:8px}
.menu-section-title{font-size:11px;color:#64748b;text-transform:uppercase;padding:8px 12px;font-weight:600}
.menu-item{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:8px;cursor:pointer;color:#e2e8f0;text-decoration:none}
.menu-item:hover{background:#334155}
.menu-item .icon{font-size:20px;width:28px;text-align:center}
.menu-item .text{flex:1}
.menu-item .text span{display:block;font-size:13px;font-weight:500}
.menu-item .text small{font-size:11px;color:#64748b}
.menu-divider{height:1px;background:#334155;margin:4px 12px}
.menu-item.danger:hover{background:rgba(239,68,68,.2)}
.desktop{position:fixed;top:0;left:0;right:0;bottom:48px;padding:20px;display:flex;flex-wrap:wrap;align-content:flex-start;gap:10px}
.desktop-icon{width:80px;padding:10px;text-align:center;border-radius:8px;cursor:pointer;transition:background .15s}
.desktop-icon:hover{background:rgba(99,102,241,.2)}
.desktop-icon .icon{font-size:36px;margin-bottom:6px}
.desktop-icon .label{font-size:11px;color:#e2e8f0;word-wrap:break-word}
.window{position:absolute;background:#1e293b;border-radius:12px;border:1px solid #334155;box-shadow:0 10px 40px rgba(0,0,0,.4);display:none;flex-direction:column;min-width:300px;min-height:200px;overflow:hidden;transition:none}
.window.active{z-index:1000;box-shadow:0 15px 50px rgba(0,0,0,.5)}
.window.show{display:flex}
.window.minimized{display:none!important}
.window.snapped{border-radius:0;transition:none}
.window-header{background:#0f172a;padding:6px 10px;display:flex;align-items:center;gap:8px;cursor:move;flex-shrink:0}
.window-header .icon{font-size:14px}
.window-header .title{flex:1;font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.window-controls{display:flex;gap:8px;align-items:center}
.window-controls button{width:12px;height:12px;border:none;border-radius:50%;cursor:pointer;font-size:0;padding:0;transition:all .15s}
.window-controls .minimize{background:#f59e0b}
.window-controls .maximize{background:#22c55e}
.window-controls .close{background:#ef4444}
.window-controls button:hover{transform:scale(1.15);filter:brightness(1.1)}
.window-controls button:active{transform:scale(0.95)}
.window-body{flex:1;overflow:auto;background:#0f172a}
.window-body iframe{width:100%;height:100%;border:none}
.resize-handle{position:absolute;background:transparent;z-index:10}
.resize-n{top:0;left:10px;right:10px;height:6px;cursor:n-resize}
.resize-s{bottom:0;left:10px;right:10px;height:6px;cursor:s-resize}
.resize-e{right:0;top:10px;bottom:10px;width:6px;cursor:e-resize}
.resize-w{left:0;top:10px;bottom:10px;width:6px;cursor:w-resize}
.resize-ne{top:0;right:0;width:12px;height:12px;cursor:ne-resize}
.resize-nw{top:0;left:0;width:12px;height:12px;cursor:nw-resize}
.resize-se{bottom:0;right:0;width:12px;height:12px;cursor:se-resize}
.resize-sw{bottom:0;left:0;width:12px;height:12px;cursor:sw-resize}
.snap-preview{position:fixed;background:rgba(99,102,241,.3);border:2px solid #6366f1;border-radius:4px;z-index:9998;display:none;pointer-events:none;transition:all .15s}
.snap-divider{position:fixed;background:transparent;z-index:500;display:none}
.snap-divider::after{content:'';position:absolute;background:#475569;opacity:0;transition:opacity .15s,background .15s}
.snap-divider.vertical{width:8px;cursor:ew-resize}
.snap-divider.vertical::after{left:3px;top:0;width:2px;height:100%}
.snap-divider.horizontal{height:8px;cursor:ns-resize}
.snap-divider.horizontal::after{top:3px;left:0;height:2px;width:100%}
.snap-divider:hover::after{opacity:1;background:#6366f1}
</style>
</head><body>
<div class="desktop">
    <div class="desktop-icon" ondblclick="openWindow('jupyterlab')"><div class="icon">&#128187;</div><div class="label">JupyterLab</div></div>
    {% if has_s3 %}<div class="desktop-icon" ondblclick="openWindow('s3backup')"><div class="icon">&#9729;</div><div class="label">S3 Backup</div></div>{% endif %}
    {% if has_shared %}<div class="desktop-icon" ondblclick="openWindow('shared')"><div class="icon">&#128101;</div><div class="label">Shared Space</div></div>{% endif %}
    {% if has_s3 %}<div class="desktop-icon" ondblclick="openWindow('myshares')"><div class="icon">&#128279;</div><div class="label">My Shares</div></div>{% endif %}
    <div class="desktop-icon" ondblclick="openWindow('settings')"><div class="icon">&#9881;</div><div class="label">Settings</div></div>
</div>
<div class="snap-preview" id="snap-preview"></div>
<div class="snap-divider" id="snap-divider-v"></div>
<div class="snap-divider" id="snap-divider-h"></div>
<div id="windows-container"></div>
<div class="taskbar">
    <button class="start-btn" onclick="toggleStartMenu()"><span>&#128218;</span> Menu</button>
    <div class="taskbar-apps" id="taskbar-apps"></div>
    <div class="taskbar-right"><span>{{ username }}</span><span id="clock"></span></div>
</div>
<div class="start-menu" id="start-menu">
    <div class="start-menu-header"><h3>JupyterHub</h3></div>
    <div class="menu-section">
        <div class="menu-section-title">Applications</div>
        <a class="menu-item" href="#" onclick="openWindow('jupyterlab');hideStartMenu()"><span class="icon">&#128187;</span><div class="text"><span>JupyterLab</span><small>Data Science IDE</small></div></a>
        {% if has_s3 %}<a class="menu-item" href="#" onclick="openWindow('s3backup');hideStartMenu()"><span class="icon">&#9729;</span><div class="text"><span>S3 Backup</span><small>Backup & Restore</small></div></a>{% endif %}
        {% if has_shared %}<a class="menu-item" href="#" onclick="openWindow('shared');hideStartMenu()"><span class="icon">&#128101;</span><div class="text"><span>Shared Space</span><small>Team Storage</small></div></a>{% endif %}
        {% if has_s3 %}<a class="menu-item" href="#" onclick="openWindow('myshares');hideStartMenu()"><span class="icon">&#128279;</span><div class="text"><span>My Shares</span><small>Shared Links</small></div></a>{% endif %}
    </div>
    <div class="menu-divider"></div>
    <div class="menu-section">
        <div class="menu-section-title">Settings</div>
        <a class="menu-item" href="#" onclick="openWindow('settings');hideStartMenu()"><span class="icon">&#9881;</span><div class="text"><span>S3 Config</span><small>Storage Settings</small></div></a>
        <a class="menu-item" href="#" onclick="openWindow('password');hideStartMenu()"><span class="icon">&#128274;</span><div class="text"><span>Change Password</span><small>Security</small></div></a>
    </div>
    <div class="menu-divider"></div>
    <div class="menu-section"><a class="menu-item danger" href="/logout"><span class="icon">&#128682;</span><div class="text"><span>Logout</span><small>Sign out</small></div></a></div>
</div>
<script>
const APPS={jupyterlab:{title:'JupyterLab',icon:'&#128187;',url:'/embed/lab',w:1200,h:700},s3backup:{title:'S3 Backup',icon:'&#9729;',url:'/embed/s3-backup',w:1100,h:650},shared:{title:'Shared Space',icon:'&#128101;',url:'/embed/shared-space',w:1100,h:650},myshares:{title:'My Shares',icon:'&#128279;',url:'/embed/my-shares',w:900,h:600},settings:{title:'S3 Config',icon:'&#9881;',url:'/embed/s3-config',w:700,h:550},password:{title:'Change Password',icon:'&#128274;',url:'/embed/change-password',w:500,h:450}};
const FILE_ICONS={'image':'&#128444;','video':'&#127916;','audio':'&#127925;','text':'&#128196;','markdown':'&#128221;','html':'&#127760;','pdf':'&#128462;','office':'&#128196;','unknown':'&#128196;'};
let wins={},zIdx=100,drag=null,fileWinCounter=0;
let splitV=50,splitH=50; // vertical and horizontal split percentages
const maxH=()=>window.innerHeight-48;
const maxW=()=>window.innerWidth;

function toggleStartMenu(){document.getElementById('start-menu').classList.toggle('show');}
function hideStartMenu(){document.getElementById('start-menu').classList.remove('show');}
document.addEventListener('click',e=>{if(!e.target.closest('.start-menu')&&!e.target.closest('.start-btn'))hideStartMenu();});
function updateClock(){document.getElementById('clock').textContent=new Date().toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit'});}
setInterval(updateClock,1000);updateClock();

function createWindow(id){
    const app=APPS[id];if(!app)return;
    const el=document.createElement('div');
    el.className='window';el.id='win-'+id;el.dataset.app=id;
    const off=Object.keys(wins).length*30;
    el.style.cssText=`left:${100+off}px;top:${50+off}px;width:${app.w}px;height:${app.h}px;`;
    el.innerHTML=`<div class="window-header" onmousedown="startDrag(event,'${id}')"><span class="icon">${app.icon}</span><span class="title">${app.title}</span><div class="window-controls"><button class="close" onclick="closeWin('${id}')" title="Close"></button><button class="minimize" onclick="minimizeWin('${id}')" title="Minimize"></button><button class="maximize" onclick="toggleMax('${id}')" title="Maximize"></button></div></div><div class="window-body"><iframe src="${app.url}"></iframe></div><div class="resize-handle resize-n" onmousedown="startResize(event,'${id}','n')"></div><div class="resize-handle resize-s" onmousedown="startResize(event,'${id}','s')"></div><div class="resize-handle resize-e" onmousedown="startResize(event,'${id}','e')"></div><div class="resize-handle resize-w" onmousedown="startResize(event,'${id}','w')"></div><div class="resize-handle resize-ne" onmousedown="startResize(event,'${id}','ne')"></div><div class="resize-handle resize-nw" onmousedown="startResize(event,'${id}','nw')"></div><div class="resize-handle resize-se" onmousedown="startResize(event,'${id}','se')"></div><div class="resize-handle resize-sw" onmousedown="startResize(event,'${id}','sw')"></div>`;
    document.getElementById('windows-container').appendChild(el);
    wins[id]={el,snap:null,restore:null};
    updateTaskbar();
}
function openWindow(id){hideStartMenu();if(!wins[id])createWindow(id);const w=wins[id];w.el.classList.add('show');w.el.classList.remove('minimized');focusWin(id);updateTaskbar();}
function openFileViewer(source,path,filename){
    // Create dynamic file viewer window
    const ext=(filename.split('.').pop()||'').toLowerCase();
    const typeMap={'jpg':'image','jpeg':'image','png':'image','gif':'image','webp':'image','svg':'image','bmp':'image','ico':'image','mp4':'video','webm':'video','ogg':'video','mov':'video','avi':'video','mkv':'video','mp3':'audio','wav':'audio','flac':'audio','m4a':'audio','aac':'audio','txt':'text','log':'text','json':'text','xml':'text','yaml':'text','yml':'text','py':'text','js':'text','ts':'text','css':'text','html':'html','htm':'html','md':'markdown','markdown':'markdown','pdf':'pdf','doc':'office','docx':'office','xls':'office','xlsx':'office','ppt':'office','pptx':'office'};
    const ftype=typeMap[ext]||'unknown';
    const icon=FILE_ICONS[ftype];
    const id='file_'+fileWinCounter++;
    const url='/viewer/'+source+'?path='+encodeURIComponent(path);
    const el=document.createElement('div');
    el.className='window';el.id='win-'+id;el.dataset.app=id;el.dataset.fileviewer='1';
    const off=Object.keys(wins).length*30;
    el.style.cssText='left:'+(100+off)+'px;top:'+(50+off)+'px;width:900px;height:600px;';
    el.innerHTML='<div class="window-header" onmousedown="startDrag(event,\\''+id+'\\')"><span class="icon">'+icon+'</span><span class="title">'+filename+'</span><div class="window-controls"><button class="close" onclick="closeWin(\\''+id+'\\')"></button><button class="minimize" onclick="minimizeWin(\\''+id+'\\')"></button><button class="maximize" onclick="toggleMax(\\''+id+'\\')"></button></div></div><div class="window-body"><iframe src="'+url+'"></iframe></div><div class="resize-handle resize-n" onmousedown="startResize(event,\\''+id+'\\',\\'n\\')"></div><div class="resize-handle resize-s" onmousedown="startResize(event,\\''+id+'\\',\\'s\\')"></div><div class="resize-handle resize-e" onmousedown="startResize(event,\\''+id+'\\',\\'e\\')"></div><div class="resize-handle resize-w" onmousedown="startResize(event,\\''+id+'\\',\\'w\\')"></div><div class="resize-handle resize-ne" onmousedown="startResize(event,\\''+id+'\\',\\'ne\\')"></div><div class="resize-handle resize-nw" onmousedown="startResize(event,\\''+id+'\\',\\'nw\\')"></div><div class="resize-handle resize-se" onmousedown="startResize(event,\\''+id+'\\',\\'se\\')"></div><div class="resize-handle resize-sw" onmousedown="startResize(event,\\''+id+'\\',\\'sw\\')"></div>';
    document.getElementById('windows-container').appendChild(el);
    APPS[id]={title:filename,icon:icon,url:url,w:900,h:600,isFile:true};
    wins[id]={el,snap:null,restore:null};
    el.classList.add('show');focusWin(id);updateDividers();updateTaskbar();
}
function closeWin(id){const w=wins[id];if(!w)return;w.el.remove();delete wins[id];if(APPS[id]&&APPS[id].isFile)delete APPS[id];updateDividers();updateTaskbar();}
function minimizeWin(id){const w=wins[id];if(!w)return;w.el.classList.add('minimized');updateDividers();updateTaskbar();}
function toggleMax(id){const w=wins[id];if(!w)return;if(w.snap){unsnap(id);}else{w.restore={l:w.el.style.left,t:w.el.style.top,w:w.el.style.width,h:w.el.style.height};applySnap(id,'max');}}
function focusWin(id){Object.values(wins).forEach(w=>w.el.classList.remove('active'));const w=wins[id];if(w){w.el.classList.add('active');w.el.style.zIndex=++zIdx;}updateDividers();updateTaskbar();}
function updateTaskbar(){const c=document.getElementById('taskbar-apps');c.innerHTML='';Object.keys(wins).forEach(id=>{const w=wins[id],app=APPS[id],b=document.createElement('button');b.className='taskbar-item'+(w.el.classList.contains('active')&&!w.el.classList.contains('minimized')?' active':'');b.innerHTML='<span>'+app.icon+'</span> '+app.title;b.onclick=()=>{if(w.el.classList.contains('minimized'))openWindow(id);else if(w.el.classList.contains('active'))minimizeWin(id);else focusWin(id);};c.appendChild(b);});}

// Snap system
function getZones(){
    const H=maxH(),W=maxW(),vw=W*splitV/100,hw=H*splitH/100;
    return {max:{l:0,t:0,w:W,h:H},left:{l:0,t:0,w:vw,h:H},right:{l:vw,t:0,w:W-vw,h:H},top:{l:0,t:0,w:W,h:hw},bottom:{l:0,t:hw,w:W,h:H-hw},'top-left':{l:0,t:0,w:vw,h:hw},'top-right':{l:vw,t:0,w:W-vw,h:hw},'bottom-left':{l:0,t:hw,w:vw,h:H-hw},'bottom-right':{l:vw,t:hw,w:W-vw,h:H-hw}};
}
function applySnap(id,zone){
    const w=wins[id];if(!w)return;
    if(!w.restore)w.restore={l:w.el.style.left,t:w.el.style.top,w:w.el.style.width,h:w.el.style.height};
    w.snap=zone;w.el.classList.add('snapped');
    const z=getZones()[zone];if(!z)return;
    w.el.style.left=z.l+'px';w.el.style.top=z.t+'px';w.el.style.width=z.w+'px';w.el.style.height=z.h+'px';
    updateDividers();
}
function unsnap(id){const w=wins[id];if(!w||!w.snap)return;w.el.classList.remove('snapped');if(w.restore){w.el.style.left=w.restore.l;w.el.style.top=w.restore.t;w.el.style.width=w.restore.w;w.el.style.height=w.restore.h;}w.snap=null;updateDividers();}
function getSnapZone(x,y){
    const W=maxW(),H=maxH(),edge=40,corner=70;
    if(x<corner&&y<corner)return'top-left';
    if(x>W-corner&&y<corner)return'top-right';
    if(x<corner&&y>H-corner)return'bottom-left';
    if(x>W-corner&&y>H-corner)return'bottom-right';
    if(y<edge)return'max';
    if(y>H-edge)return'bottom';
    if(x<edge)return'left';
    if(x>W-edge)return'right';
    return null;
}
function showSnapPreview(zone){
    const p=document.getElementById('snap-preview');if(!zone){p.style.display='none';return;}
    const z=getZones()[zone];if(!z){p.style.display='none';return;}
    p.style.cssText=`display:block;left:${z.l}px;top:${z.t}px;width:${z.w}px;height:${z.h}px;`;
}
function updateSnappedWindows(){Object.keys(wins).forEach(id=>{const w=wins[id];if(w.snap)applySnap(id,w.snap);});}
function updateDividers(){
    const dv=document.getElementById('snap-divider-v'),dh=document.getElementById('snap-divider-h');
    // Only consider visible (non-minimized) windows
    const visible=Object.values(wins).filter(w=>!w.el.classList.contains('minimized'));
    // Hide dividers if there's a floating (non-snapped) visible window
    const hasFloating=visible.some(w=>!w.snap);
    if(hasFloating){dv.style.display='none';dh.style.display='none';return;}
    const snapped=visible.filter(w=>w.snap&&w.snap!=='max');
    const hasLeft=snapped.some(w=>w.snap.includes('left'));
    const hasRight=snapped.some(w=>w.snap.includes('right'));
    const hasTop=snapped.some(w=>w.snap==='top'||w.snap==='top-left'||w.snap==='top-right');
    const hasBottom=snapped.some(w=>w.snap==='bottom'||w.snap==='bottom-left'||w.snap==='bottom-right');
    if(hasLeft&&hasRight){dv.className='snap-divider vertical';dv.style.cssText=`display:block;left:${maxW()*splitV/100-4}px;top:0;height:${maxH()}px;`;}else{dv.style.display='none';}
    if(hasTop&&hasBottom){dh.className='snap-divider horizontal';dh.style.cssText=`display:block;top:${maxH()*splitH/100-4}px;left:0;width:${maxW()}px;`;}else{dh.style.display='none';}
}
// Divider drag
let divDrag=null;
document.getElementById('snap-divider-v').addEventListener('mousedown',e=>{e.preventDefault();divDrag={type:'v',startX:e.clientX,startSplit:splitV};document.addEventListener('mousemove',onDivDrag);document.addEventListener('mouseup',stopDivDrag);Object.values(wins).forEach(w=>{const f=w.el.querySelector('iframe');if(f)f.style.pointerEvents='none';});});
document.getElementById('snap-divider-h').addEventListener('mousedown',e=>{e.preventDefault();divDrag={type:'h',startY:e.clientY,startSplit:splitH};document.addEventListener('mousemove',onDivDrag);document.addEventListener('mouseup',stopDivDrag);Object.values(wins).forEach(w=>{const f=w.el.querySelector('iframe');if(f)f.style.pointerEvents='none';});});
function onDivDrag(e){if(!divDrag)return;if(divDrag.type==='v'){const dx=e.clientX-divDrag.startX;splitV=Math.max(20,Math.min(80,divDrag.startSplit+dx/maxW()*100));}else{const dy=e.clientY-divDrag.startY;splitH=Math.max(20,Math.min(80,divDrag.startSplit+dy/maxH()*100));}updateSnappedWindows();}
function stopDivDrag(){divDrag=null;document.removeEventListener('mousemove',onDivDrag);document.removeEventListener('mouseup',stopDivDrag);Object.values(wins).forEach(w=>{const f=w.el.querySelector('iframe');if(f)f.style.pointerEvents='';});}

// Window drag
function startDrag(e,id){
    if(e.target.closest('.window-controls'))return;
    const w=wins[id];if(!w)return;focusWin(id);
    // If snapped, unsnap but keep mouse position relative
    if(w.snap&&w.snap!=='max'){
        const rect=w.el.getBoundingClientRect();
        const relX=(e.clientX-rect.left)/rect.width;
        unsnap(id);
        const newRect=w.el.getBoundingClientRect();
        w.el.style.left=(e.clientX-newRect.width*relX)+'px';
        w.el.style.top=e.clientY-20+'px';
    }else if(w.snap==='max'){
        const rect=w.el.getBoundingClientRect();
        const relX=(e.clientX-rect.left)/rect.width;
        unsnap(id);
        const newRect=w.el.getBoundingClientRect();
        w.el.style.left=(e.clientX-newRect.width*relX)+'px';
        w.el.style.top='0px';
    }
    const rect=w.el.getBoundingClientRect();
    drag={type:'move',id,startX:e.clientX,startY:e.clientY,origL:rect.left,origT:rect.top};
    document.addEventListener('mousemove',onDrag);document.addEventListener('mouseup',stopDrag);
    w.el.querySelector('iframe').style.pointerEvents='none';
}
function onDrag(e){
    if(!drag)return;const w=wins[drag.id];if(!w)return;
    if(drag.type==='move'){
        const dx=e.clientX-drag.startX,dy=e.clientY-drag.startY;
        w.el.style.left=(drag.origL+dx)+'px';w.el.style.top=Math.max(0,drag.origT+dy)+'px';
        showSnapPreview(getSnapZone(e.clientX,e.clientY));
    }else if(drag.type==='resize'){
        const dx=e.clientX-drag.startX,dy=e.clientY-drag.startY,dir=drag.dir;
        let nW=drag.origW,nH=drag.origH,nL=drag.origL,nT=drag.origT;
        if(dir.includes('e'))nW=Math.max(300,drag.origW+dx);
        if(dir.includes('w')){nW=Math.max(300,drag.origW-dx);nL=drag.origL+dx;}
        if(dir.includes('s'))nH=Math.max(200,drag.origH+dy);
        if(dir.includes('n')){nH=Math.max(200,drag.origH-dy);nT=Math.max(0,drag.origT+dy);}
        w.el.style.width=nW+'px';w.el.style.height=nH+'px';w.el.style.left=nL+'px';w.el.style.top=nT+'px';
    }
}
function stopDrag(e){
    if(!drag)return;const w=wins[drag.id];
    if(w){
        w.el.querySelector('iframe').style.pointerEvents='';
        if(drag.type==='move'){
            const zone=getSnapZone(e.clientX,e.clientY);
            if(zone){w.restore={l:w.el.style.left,t:w.el.style.top,w:w.el.style.width,h:w.el.style.height};applySnap(drag.id,zone);}
        }
    }
    document.getElementById('snap-preview').style.display='none';
    drag=null;document.removeEventListener('mousemove',onDrag);document.removeEventListener('mouseup',stopDrag);
}
function startResize(e,id,dir){
    e.stopPropagation();const w=wins[id];if(!w||w.snap)return;focusWin(id);
    const rect=w.el.getBoundingClientRect();
    drag={type:'resize',id,dir,startX:e.clientX,startY:e.clientY,origW:rect.width,origH:rect.height,origL:rect.left,origT:rect.top};
    document.addEventListener('mousemove',onDrag);document.addEventListener('mouseup',stopDrag);
    w.el.querySelector('iframe').style.pointerEvents='none';
}
document.addEventListener('dblclick',e=>{const h=e.target.closest('.window-header');if(h&&!e.target.closest('.window-controls')){const win=h.closest('.window');if(win)toggleMax(win.dataset.app);}});
document.addEventListener('mousedown',e=>{const win=e.target.closest('.window');if(win&&win.dataset.app)focusWin(win.dataset.app);});
window.addEventListener('resize',updateSnappedWindows);
</script>
</body></html>"""

USER_LAB = CSS + """<!DOCTYPE html><html><head><title>JupyterLab</title>
<script>
function checkIframe() {
    var iframe = document.getElementById('labframe');
    try {
        var doc = iframe.contentDocument || iframe.contentWindow.document;
        if (doc.body.innerHTML.includes('502') || doc.body.innerHTML.includes('Bad Gateway')) {
            setTimeout(function() { iframe.src = iframe.src; }, 2000);
        }
    } catch(e) {}
}
setTimeout(checkIframe, 3000);
</script>
</head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Lab</span></h1>
<div class="nav-right"><span>{{ username }}</span><a href="/dashboard" class="btn btn-secondary btn-sm">Menu</a><a href="/logout" class="btn btn-danger btn-sm" style="margin-left:10px">Logout</a></div></nav>
<iframe id="labframe" src="/user/{{ username }}/lab"></iframe>
</body></html>"""

USER_CHANGE_PW = CSS + """<!DOCTYPE html><html><head><title>Change Password</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span></h1>
<div class="nav-right"><span>{{ username }}</span><a href="/dashboard" class="btn btn-secondary btn-sm">Menu</a><a href="/logout" class="btn btn-danger btn-sm" style="margin-left:10px">Logout</a></div></nav>
<div class="container">
    <div class="card" style="max-width:500px;margin:40px auto">
        <div class="card-header"><h2>&#128274; Change Password</h2></div>
        <div class="card-body">
            {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
            {% if success %}<div class="alert alert-success">{{ success }}</div>{% endif %}
            <form method="post">
                <div class="form-group"><label>Current Password</label><input type="password" name="old_password" class="form-control" required></div>
                <div class="form-group"><label>New Password</label><input type="password" name="new_password" class="form-control" required></div>
                <div class="form-group"><label>Confirm New Password</label><input type="password" name="confirm_password" class="form-control" required></div>
                <button type="submit" class="btn btn-primary" style="width:100%">Change Password</button>
            </form>
        </div>
    </div>
</div></body></html>"""

# ===========================================
# Admin S3 Config Template
# ===========================================

ADMIN_S3_CONFIG = CSS + """<!DOCTYPE html><html><head><title>S3 Configuration</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span> Admin</h1>
<div class="nav-right">
    <div class="nav-links">
        <a href="/dashboard">Users</a>
        <a href="/admin/s3-config" class="active">S3 Config</a>
        <a href="/admin/extensions">Extensions</a>
    </div>
    <span>admin</span><a href="/logout" class="btn btn-secondary btn-sm">Logout</a>
</div></nav>
<div class="container">
    {% if message %}<div class="alert {{ 'alert-success' if success else 'alert-error' }}">{{ message }}</div>{% endif %}

    <div class="card">
        <div class="card-header"><h2>&#9729; System S3 Configuration</h2></div>
        <div class="card-body">
            <p style="color:#94a3b8;margin-bottom:20px">Configure S3-compatible storage for all users. Users without personal S3 config will use this.</p>
            <form method="post" action="/admin/s3-config">
                <div class="form-row">
                    <div class="form-group"><label>Endpoint URL</label><input type="text" name="endpoint_url" class="form-control" value="{{ config.endpoint_url or '' }}" placeholder="https://s3.amazonaws.com"></div>
                    <div class="form-group"><label>Region</label><input type="text" name="region" class="form-control" value="{{ config.region or '' }}" placeholder="us-east-1"></div>
                </div>
                <div class="form-row">
                    <div class="form-group"><label>Access Key</label><input type="text" name="access_key" class="form-control" value="{{ config.access_key or '' }}"></div>
                    <div class="form-group"><label>Secret Key</label><input type="password" name="secret_key" class="form-control" value="{{ config.secret_key or '' }}"></div>
                </div>
                <div class="form-row">
                    <div class="form-group"><label>Bucket Name</label><input type="text" name="bucket_name" class="form-control" value="{{ config.bucket_name or '' }}"></div>
                    <div class="form-group"><label>Prefix (optional)</label><input type="text" name="prefix" class="form-control" value="{{ config.prefix or '' }}" placeholder="jupyterhub-backups"></div>
                </div>
                <div style="display:flex;gap:10px">
                    <button type="submit" class="btn btn-primary">Save Configuration</button>
                    <button type="button" class="btn btn-success" onclick="testConnection()">Test Connection</button>
                </div>
            </form>
            <div id="test-result" style="margin-top:15px"></div>
        </div>
    </div>
</div>
<script>
function testConnection() {
    var form = document.querySelector('form');
    var data = new FormData(form);
    fetch('/admin/s3-config/test', {method:'POST', body:data})
    .then(r => r.json()).then(d => {
        var el = document.getElementById('test-result');
        el.innerHTML = '<div class="alert '+(d.success?'alert-success':'alert-error')+'">'+d.message+'</div>';
    });
}
</script>
</body></html>"""

# ===========================================
# Admin Extensions Template
# ===========================================

ADMIN_EXTENSIONS = CSS + """<!DOCTYPE html><html><head><title>Extension Manager</title>
<style>
.ext-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;padding:16px}
.ext-card{background:#0f172a;border:1px solid #334155;border-radius:12px;padding:16px;transition:all .2s}
.ext-card:hover{border-color:#6366f1;transform:translateY(-2px)}
.ext-card h4{font-size:15px;margin-bottom:4px;color:#e2e8f0}
.ext-card .ext-pkg{font-size:12px;color:#818cf8;font-family:monospace;margin-bottom:8px}
.ext-card p{font-size:13px;color:#94a3b8;margin-bottom:12px;line-height:1.4}
.ext-card .ext-actions{display:flex;gap:8px;align-items:center}
.search-box{display:flex;gap:10px}
.search-box input{flex:1}
.tab-bar{display:flex;gap:0;border-bottom:2px solid #334155}
.tab-bar button{background:none;border:none;padding:12px 20px;color:#94a3b8;font-size:14px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px;font-weight:500}
.tab-bar button.active{color:#818cf8;border-bottom-color:#818cf8}
.tab-bar button:hover{color:#e2e8f0}
.tab-content{display:none}
.tab-content.active{display:block}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid #334155;border-top-color:#818cf8;border-radius:50%;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.pagination{display:flex;gap:10px;justify-content:center;align-items:center;padding:16px}
.pagination .btn{min-width:100px}
.pagination .page-info{color:#94a3b8;font-size:13px}
.section-label{padding:16px 20px 0;font-size:12px;text-transform:uppercase;color:#64748b;font-weight:600;letter-spacing:1px}
.cat-filter{background:#1e293b;border:1px solid #334155;color:#94a3b8;border-radius:20px;padding:4px 12px;font-size:12px;cursor:pointer;transition:all .2s}
.cat-filter:hover{border-color:#6366f1;color:#e2e8f0}
.cat-filter.active{background:#6366f1;border-color:#6366f1;color:#fff}
</style>
</head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span> Admin</h1>
<div class="nav-right">
    <div class="nav-links">
        <a href="/dashboard">Users</a>
        <a href="/admin/s3-config">S3 Config</a>
        <a href="/admin/extensions" class="active">Extensions</a>
    </div>
    <span>admin</span><a href="/logout" class="btn btn-secondary btn-sm">Logout</a>
</div></nav>
<div class="container" style="max-width:1200px">
    {% if message %}<div class="alert {{ 'alert-success' if success else 'alert-error' }}">{{ message }}</div>{% endif %}

    <div class="card">
        <div class="tab-bar">
            <button class="active" onclick="showTab('installed')">&#128230; Installed ({{ extensions|length }})</button>
            <button onclick="showTab('browse')">&#128269; Browse PyPI</button>
        </div>

        <!-- Tab: Installed -->
        <div class="tab-content active" id="tab-installed">
            <div style="padding:16px 20px 8px;display:flex;justify-content:space-between;align-items:center">
                <div>
                    <form method="post" action="/admin/extensions/install" style="display:flex;gap:10px">
                        <input type="text" name="package" class="form-control" style="width:300px" required placeholder="Package name (e.g. jupyterlab-git)">
                        <button class="btn btn-success btn-sm">Install</button>
                    </form>
                </div>
                <form method="post" action="/admin/extensions/restart" style="display:inline">
                    <button class="btn btn-warning btn-sm">Restart All Labs</button>
                </form>
            </div>
            <div style="padding:0 4px 4px">
            {% if extensions %}
            <table><thead><tr><th>Extension</th><th>Version</th><th>Status</th><th>Actions</th></tr></thead><tbody>
            {% for ext in extensions %}<tr>
                <td><strong>{{ ext.name }}</strong></td>
                <td>{{ ext.version }}</td>
                <td><span class="tag {{ 'tag-green' if ext.status == 'enabled' else 'tag-red' }}">{{ ext.status }}</span></td>
                <td><form method="post" action="/admin/extensions/uninstall" style="display:inline" onsubmit="return confirm('Uninstall {{ ext.name }}?')">
                    <input type="hidden" name="package" value="{{ ext.name }}">
                    <button class="btn btn-danger btn-sm">Uninstall</button>
                </form></td>
            </tr>{% endfor %}</tbody></table>
            {% else %}<div class="empty">No extensions detected</div>{% endif %}
            </div>
        </div>

        <!-- Tab: Browse PyPI -->
        <div class="tab-content" id="tab-browse">
            <!-- Search box -->
            <div style="padding:16px 20px">
                <div class="search-box">
                    <input type="text" id="search-input" class="form-control" placeholder="Search extensions (e.g. git, theme, vim, spreadsheet...)" onkeydown="if(event.key==='Enter'){doSearch();return false;}">
                    <button class="btn btn-primary" onclick="doSearch()">Search</button>
                </div>
            </div>

            <!-- Recommended section (curated) -->
            <div id="recommended-section">
                <div style="padding:16px 20px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
                    <span style="color:#64748b;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:1px;margin-right:4px">Filter:</span>
                    <button class="cat-filter active" data-cat="all" onclick="filterCat('all')">All</button>
                    <button class="cat-filter" data-cat="dev" onclick="filterCat('dev')">Developer</button>
                    <button class="cat-filter" data-cat="ui" onclick="filterCat('ui')">UI &amp; Themes</button>
                    <button class="cat-filter" data-cat="data" onclick="filterCat('data')">Data &amp; Viz</button>
                    <button class="cat-filter" data-cat="ai" onclick="filterCat('ai')">AI &amp; Productivity</button>
                    <button class="cat-filter" data-cat="lang" onclick="filterCat('lang')">Languages</button>
                    <button class="cat-filter" data-cat="kernel" onclick="filterCat('kernel')">Kernels</button>
                    <button class="cat-filter" data-cat="file" onclick="filterCat('file')">File Mgmt</button>
                </div>
                <div class="ext-grid" id="rec-grid">
                    {% for ext in popular %}
                    <div class="ext-card rec-card" data-cat="{{ ext.cat }}">
                        <h4>{{ ext.name }}</h4>
                        <div class="ext-pkg">{{ ext.package }}</div>
                        <p>{{ ext.desc }}</p>
                        <div class="ext-actions">
                            {% if ext.installed %}
                            <span class="tag tag-green">Installed</span>
                            <form method="post" action="/admin/extensions/uninstall" style="display:inline" onsubmit="return confirm('Uninstall {{ ext.package }}?')">
                                <input type="hidden" name="package" value="{{ ext.package }}">
                                <button class="btn btn-danger btn-sm">Uninstall</button>
                            </form>
                            {% else %}
                            <form method="post" action="/admin/extensions/install" style="display:inline">
                                <input type="hidden" name="package" value="{{ ext.package }}">
                                <button class="btn btn-success btn-sm">Install</button>
                            </form>
                            {% endif %}
                        </div>
                    </div>
                    {% endfor %}
                </div>
                <div class="pagination" id="rec-pagination"></div>
            </div>

            <!-- Search results (replaces recommended when searching) -->
            <div id="search-results" style="display:none"></div>
        </div>
    </div>
</div>

<script>
var recPage = 1;
var recPerPage = 12;
var currentCat = 'all';

function showTab(name) {
    document.querySelectorAll('.tab-content').forEach(function(el){ el.classList.remove('active'); });
    document.querySelectorAll('.tab-bar button').forEach(function(el){ el.classList.remove('active'); });
    document.getElementById('tab-'+name).classList.add('active');
    var btns = document.querySelectorAll('.tab-bar button');
    var map = {'installed':0,'browse':1};
    if (map[name] !== undefined) btns[map[name]].classList.add('active');
    if (name === 'browse') paginateRec();
}

function filterCat(cat) {
    currentCat = cat;
    recPage = 1;
    document.querySelectorAll('.cat-filter').forEach(function(b){ b.classList.remove('active'); });
    document.querySelector('.cat-filter[data-cat="'+cat+'"]').classList.add('active');
    paginateRec();
}

function paginateRec() {
    var cards = document.querySelectorAll('.rec-card');
    var visible = [];
    cards.forEach(function(c){
        if (currentCat === 'all' || c.dataset.cat === currentCat) {
            visible.push(c);
        }
        c.style.display = 'none';
    });
    var start = (recPage - 1) * recPerPage;
    var end = Math.min(start + recPerPage, visible.length);
    for (var i = start; i < end; i++) {
        visible[i].style.display = '';
    }
    var totalPages = Math.ceil(visible.length / recPerPage);
    var pag = document.getElementById('rec-pagination');
    if (totalPages <= 1) {
        pag.innerHTML = '<span class="page-info">' + visible.length + ' extensions</span>';
    } else {
        var html = '';
        if (recPage > 1) html += '<button class="btn btn-secondary btn-sm" onclick="recPage--;paginateRec()">&#9664; Prev</button>';
        html += '<span class="page-info">Page ' + recPage + ' / ' + totalPages + ' (' + visible.length + ' extensions)</span>';
        if (recPage < totalPages) html += '<button class="btn btn-secondary btn-sm" onclick="recPage++;paginateRec()">Next &#9654;</button>';
        pag.innerHTML = html;
    }
}

paginateRec();

function renderCards(items) {
    var html = '<div class="ext-grid">';
    items.forEach(function(ext){
        var pkg = ext.package.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
        var name = (ext.name||pkg).replace(/&/g,'&amp;').replace(/</g,'&lt;');
        var desc = (ext.desc||'No description').replace(/&/g,'&amp;').replace(/</g,'&lt;');
        html += '<div class="ext-card"><h4>'+name+'</h4>' +
            '<div class="ext-pkg">'+pkg+'</div>' +
            '<p>'+desc+'</p><div class="ext-actions">';
        if (ext.installed) {
            html += '<span class="tag tag-green">Installed</span>' +
                '<form method="post" action="/admin/extensions/uninstall" style="display:inline" onsubmit="return confirm(&quot;Uninstall '+pkg+'?&quot;)"><input type="hidden" name="package" value="'+pkg+'"><button class="btn btn-danger btn-sm">Uninstall</button></form>';
        } else {
            html += '<form method="post" action="/admin/extensions/install" style="display:inline"><input type="hidden" name="package" value="'+pkg+'"><button class="btn btn-success btn-sm">Install</button></form>';
        }
        html += '</div></div>';
    });
    html += '</div>';
    return html;
}

function doSearch() {
    var q = document.getElementById('search-input').value.trim();
    if (!q) {
        document.getElementById('recommended-section').style.display = '';
        document.getElementById('search-results').style.display = 'none';
        paginateRec();
        return;
    }
    var el = document.getElementById('search-results');
    var rec = document.getElementById('recommended-section');
    rec.style.display = 'none';
    el.style.display = '';
    el.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div><span style="margin-left:10px;color:#94a3b8">Searching...</span></div>';

    fetch('/admin/extensions/search?q='+encodeURIComponent(q))
    .then(function(r){ return r.json(); })
    .then(function(data){
        if (!data.results || !data.results.length) {
            el.innerHTML = '<div class="empty">No results for &quot;'+q+'&quot;. Try a different keyword or install manually from the Installed tab.</div>';
            return;
        }
        var html = '<div class="section-label">Results for &quot;'+q+'&quot; ('+data.results.length+' found)</div>';
        html += renderCards(data.results);
        el.innerHTML = html;
    })
    .catch(function(){ el.innerHTML = '<div class="empty">Search failed. Check server connection.</div>'; });
}
</script>
</body></html>"""

# ===========================================
# User S3 Config Template
# ===========================================

USER_S3_CONFIG = CSS + """<!DOCTYPE html><html><head><title>S3 Configuration</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span></h1>
<div class="nav-right"><span>{{ username }}</span><a href="/dashboard" class="btn btn-secondary btn-sm">Menu</a><a href="/logout" class="btn btn-danger btn-sm" style="margin-left:10px">Logout</a></div></nav>
<div class="container">
    {% if message %}<div class="alert {{ 'alert-success' if success else 'alert-error' }}">{{ message }}</div>{% endif %}

    {% if system_s3 %}
    <div class="alert alert-info">System S3 is configured. You can use it directly or set up your own S3 storage below.</div>
    {% endif %}

    <div class="card">
        <div class="card-header">
            <h2>&#9881; Personal S3 Configuration</h2>
            {% if has_personal %}
            <form method="post" action="/user/s3-config/delete" onsubmit="return confirm('Remove personal config and use system S3?')">
                <button class="btn btn-danger btn-sm">Remove Personal Config</button>
            </form>
            {% endif %}
        </div>
        <div class="card-body">
            <form method="post" action="/user/s3-config">
                <div class="form-row">
                    <div class="form-group"><label>Endpoint URL</label><input type="text" name="endpoint_url" class="form-control" value="{{ config.endpoint_url or '' }}" placeholder="https://s3.amazonaws.com"></div>
                    <div class="form-group"><label>Region</label><input type="text" name="region" class="form-control" value="{{ config.region or '' }}" placeholder="us-east-1"></div>
                </div>
                <div class="form-row">
                    <div class="form-group"><label>Access Key</label><input type="text" name="access_key" class="form-control" value="{{ config.access_key or '' }}"></div>
                    <div class="form-group"><label>Secret Key</label><input type="password" name="secret_key" class="form-control" value="{{ config.secret_key or '' }}"></div>
                </div>
                <div class="form-row">
                    <div class="form-group"><label>Bucket Name</label><input type="text" name="bucket_name" class="form-control" value="{{ config.bucket_name or '' }}"></div>
                    <div class="form-group"><label>Prefix (optional)</label><input type="text" name="prefix" class="form-control" value="{{ config.prefix or '' }}" placeholder="my-backups"></div>
                </div>
                <div style="display:flex;gap:10px">
                    <button type="submit" class="btn btn-primary">Save Configuration</button>
                    <button type="button" class="btn btn-success" onclick="testConnection()">Test Connection</button>
                </div>
            </form>
            <div id="test-result" style="margin-top:15px"></div>
        </div>
    </div>
</div>
<script>
function testConnection() {
    var form = document.querySelector('form');
    var data = new FormData(form);
    fetch('/user/s3-config/test', {method:'POST', body:data})
    .then(r => r.json()).then(d => {
        var el = document.getElementById('test-result');
        el.innerHTML = '<div class="alert '+(d.success?'alert-success':'alert-error')+'">'+d.message+'</div>';
    });
}
</script>
</body></html>"""

# ===========================================
# S3 Backup File Browser Template
# ===========================================

S3_BACKUP_PAGE = CSS + """<!DOCTYPE html><html><head><title>S3 Backup</title>
<style>
.drop-zone{border:2px dashed transparent;transition:all .2s;position:relative}
.drop-zone.drag-over{border-color:#6366f1;background:rgba(99,102,241,.1)}
.drop-zone.drag-over::after{content:'Drop files here';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(99,102,241,.9);color:#fff;padding:20px 40px;border-radius:10px;font-size:18px;z-index:100}
.upload-input{display:none}
.upload-progress{padding:8px 16px;border-top:1px solid #334155;font-size:13px;color:#94a3b8}
</style>
</head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span> - S3 Backup</h1>
<div class="nav-right"><span>{{ username }}</span>
    <span class="tag {{ 'tag-blue' if s3_source == 'personal' else 'tag-green' }}">{{ s3_source }} S3</span>
    <a href="/dashboard" class="btn btn-secondary btn-sm">Menu</a>
    <a href="/logout" class="btn btn-danger btn-sm" style="margin-left:10px">Logout</a>
</div></nav>
<div class="container-wide">
    <div class="split-pane">
        <!-- Workspace Panel -->
        <div class="pane drop-zone" id="ws-pane" data-target="workspace">
            <div class="pane-header">
                <h3>&#128193; Workspace</h3>
                <div style="display:flex;gap:6px">
                    <label class="btn btn-sm btn-success" style="cursor:pointer">&#11014; Upload<input type="file" class="upload-input" id="ws-upload" multiple onchange="handleUpload('workspace', this.files)"></label>
                    <button class="btn btn-sm btn-secondary" onclick="wsMkdir()">New Folder</button>
                    <button class="btn btn-sm btn-danger" onclick="wsDelete()">Delete</button>
                </div>
            </div>
            <div class="breadcrumb" id="ws-breadcrumb" style="padding:8px 16px"></div>
            <div class="file-list" id="ws-list"></div>
            <div class="upload-progress" id="ws-upload-progress" style="display:none"></div>
        </div>

        <!-- Transfer Controls -->
        <div style="display:flex;flex-direction:column;justify-content:center;align-items:center;gap:10px;padding:0 5px">
            <button class="btn btn-primary" onclick="transferTo('s3')" title="Upload to S3">&#10145; S3</button>
            <button class="btn btn-success" onclick="transferTo('workspace')" title="Download to Workspace">&#11013; WS</button>
        </div>

        <!-- S3 Panel -->
        <div class="pane drop-zone" id="s3-pane" data-target="s3">
            <div class="pane-header">
                <h3>&#9729; S3 Storage</h3>
                <div style="display:flex;gap:6px">
                    <label class="btn btn-sm btn-success" style="cursor:pointer">&#11014; Upload<input type="file" class="upload-input" id="s3-upload" multiple onchange="handleUpload('s3', this.files)"></label>
                    <button class="btn btn-sm btn-primary" onclick="s3Share()" title="Share selected item">&#128279; Share</button>
                    <button class="btn btn-sm btn-secondary" onclick="s3Mkdir()">New Folder</button>
                    <button class="btn btn-sm btn-danger" onclick="s3Delete()">Delete</button>
                </div>
            </div>
            <div class="breadcrumb" id="s3-breadcrumb" style="padding:8px 16px"></div>
            <div class="file-list" id="s3-list"></div>
            <div class="upload-progress" id="s3-upload-progress" style="display:none"></div>
        </div>
    </div>

    <!-- Progress -->
    <div id="transfer-progress" class="card" style="margin-top:15px;display:none">
        <div class="card-body" style="padding:12px 20px">
            <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
            <div class="progress-text" id="progress-text">Preparing...</div>
        </div>
    </div>
</div>

<script>
var wsPath = '';
var s3Path = '';

function formatSize(b) {
    if (b === 0) return '-';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b/1048576).toFixed(1) + ' MB';
    return (b/1073741824).toFixed(2) + ' GB';
}

function renderBreadcrumb(el, path, onClick) {
    var parts = path ? path.split('/').filter(Boolean) : [];
    var html = '<a href="#" onclick="'+onClick+'(\\'\\');return false">Home</a>';
    var acc = '';
    parts.forEach(function(p) {
        acc += (acc ? '/' : '') + p;
        html += ' / <a href="#" onclick="'+onClick+'(\\''+acc+'\\');return false">'+p+'</a>';
    });
    document.getElementById(el).innerHTML = html;
}

function renderList(el, items, path, navFn, isS3) {
    var html = '';
    items.forEach(function(item) {
        var icon = item.type === 'dir' ? '&#128193;' : '&#128196;';
        var clickAction = item.type === 'dir' ? 'onclick="'+navFn+'(\\''+(path ? path+'/':'')+item.name+'\\')"' : '';
        html += '<div class="file-item" '+clickAction+'>' +
            '<input type="checkbox" value="'+item.name+'" onclick="event.stopPropagation()" data-panel="'+(isS3?'s3':'ws')+'">' +
            '<span class="file-icon">'+icon+'</span>' +
            '<span>'+item.name+'</span>' +
            '<span class="file-size">'+formatSize(item.size)+'</span></div>';
    });
    document.getElementById(el).innerHTML = html || '<div class="empty">Empty</div>';
}

function loadWs(path) {
    wsPath = path || '';
    fetch('/api/workspace/list?path='+encodeURIComponent(wsPath))
    .then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); return; }
        renderBreadcrumb('ws-breadcrumb', wsPath, 'loadWs');
        renderList('ws-list', d.items, wsPath, 'loadWs', false);
    });
}

function loadS3(path) {
    s3Path = path || '';
    fetch('/api/s3/list?path='+encodeURIComponent(s3Path))
    .then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); return; }
        renderBreadcrumb('s3-breadcrumb', s3Path, 'loadS3');
        renderList('s3-list', d.items, s3Path, 'loadS3', true);
    });
}

function getChecked(panel) {
    var boxes = document.querySelectorAll('#'+(panel==='s3'?'s3':'ws')+'-list input[type=checkbox]:checked');
    return Array.from(boxes).map(function(b) { return b.value; });
}

function transferTo(dest) {
    var source = dest === 's3' ? 'workspace' : 's3';
    var items = getChecked(source === 'workspace' ? 'ws' : 's3');
    if (!items.length) { alert('Select files first'); return; }
    var body = JSON.stringify({
        source: source, dest: dest, items: items,
        source_path: source === 'workspace' ? wsPath : s3Path,
        dest_path: dest === 's3' ? s3Path : wsPath
    });
    fetch('/api/transfer', {method:'POST', headers:{'Content-Type':'application/json'}, body:body})
    .then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); return; }
        pollProgress(d.task_id);
    });
}

function pollProgress(taskId) {
    var el = document.getElementById('transfer-progress');
    el.style.display = 'block';
    var iv = setInterval(function() {
        fetch('/api/transfer/status/'+taskId).then(r => r.json()).then(d => {
            var pct = d.total ? Math.round(d.completed/d.total*100) : 0;
            document.getElementById('progress-fill').style.width = pct+'%';
            document.getElementById('progress-text').textContent = d.current_file ? ('Transferring: '+d.current_file+' ('+d.completed+'/'+d.total+')') : 'Preparing...';
            if (d.status === 'done') {
                clearInterval(iv);
                document.getElementById('progress-text').textContent = 'Transfer complete! ('+d.total+' items)';
                document.getElementById('progress-fill').style.width = '100%';
                loadWs(wsPath); loadS3(s3Path);
            } else if (d.status === 'error') {
                clearInterval(iv);
                document.getElementById('progress-text').textContent = 'Error: '+(d.error||'Unknown error');
            }
        });
    }, 1000);
}

function wsMkdir() {
    var name = prompt('Folder name:');
    if (!name) return;
    fetch('/api/workspace/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:(wsPath?wsPath+'/':'')+name})})
    .then(r => r.json()).then(function() { loadWs(wsPath); });
}
function s3Mkdir() {
    var name = prompt('Folder name:');
    if (!name) return;
    fetch('/api/s3/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:(s3Path?s3Path+'/':'')+name})})
    .then(r => r.json()).then(function() { loadS3(s3Path); });
}
function wsDelete() {
    var items = getChecked('ws');
    if (!items.length) return;
    if (!confirm('Delete '+items.length+' item(s)?')) return;
    fetch('/api/workspace/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:items, path:wsPath})})
    .then(r => r.json()).then(function() { loadWs(wsPath); });
}
function s3Delete() {
    var items = getChecked('s3');
    if (!items.length) return;
    if (!confirm('Delete '+items.length+' item(s) from S3?')) return;
    fetch('/api/s3/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:items, path:s3Path})})
    .then(r => r.json()).then(function() { loadS3(s3Path); });
}

function s3Share() {
    var items = getChecked('s3');
    if (items.length !== 1) { alert('Select exactly 1 item to share'); return; }
    var name = items[0];
    // Determine type from the file list
    var el = document.querySelector('#s3-list input[type=checkbox][value="'+name+'"]');
    var fileItem = el ? el.closest('.file-item') : null;
    var isDir = fileItem && !fileItem.querySelector('.file-size').textContent.trim().match(/^[0-9]/);
    var itemType = isDir ? 'dir' : 'file';
    // Check if it's a directory by icon
    var icon = fileItem ? fileItem.querySelector('.file-icon').innerHTML : '';
    if (icon.indexOf('128193') >= 0) itemType = 'dir';

    var password = prompt('Set password (leave empty for no password):');
    var hours = prompt('Expire after how many hours? (0 or empty = never):');
    var body = JSON.stringify({
        name: name,
        type: itemType,
        s3_path: s3Path,
        password: password || '',
        expires_hours: parseInt(hours) || 0
    });
    fetch('/api/share/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:body})
    .then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); return; }
        var link = location.origin + '/share/' + d.share_id;
        prompt('Share link (Ctrl+C to copy):', link);
    });
}

// Drag and drop upload
document.querySelectorAll('.drop-zone').forEach(function(zone) {
    ['dragenter', 'dragover'].forEach(function(evt) {
        zone.addEventListener(evt, function(e) {
            e.preventDefault();
            e.stopPropagation();
            zone.classList.add('drag-over');
        });
    });
    ['dragleave', 'drop'].forEach(function(evt) {
        zone.addEventListener(evt, function(e) {
            e.preventDefault();
            e.stopPropagation();
            zone.classList.remove('drag-over');
        });
    });
    zone.addEventListener('drop', function(e) {
        var target = zone.dataset.target;
        var files = e.dataTransfer.files;
        if (files.length) handleUpload(target, files);
    });
});

function handleUpload(target, files) {
    if (!files.length) return;
    var progressEl = document.getElementById(target === 's3' ? 's3-upload-progress' : 'ws-upload-progress');
    var path = target === 's3' ? s3Path : wsPath;
    var endpoint = target === 's3' ? '/api/s3/upload' : '/api/workspace/upload';
    var total = files.length;
    var done = 0;
    var errors = [];
    progressEl.style.display = 'block';
    progressEl.textContent = 'Uploading 0/' + total + '...';

    function uploadNext(i) {
        if (i >= total) {
            if (errors.length) {
                progressEl.textContent = 'Done with ' + errors.length + ' error(s): ' + errors[0];
            } else {
                progressEl.textContent = 'Uploaded ' + total + ' file(s)!';
                setTimeout(function() { progressEl.style.display = 'none'; }, 3000);
            }
            if (target === 's3') loadS3(s3Path); else loadWs(wsPath);
            return;
        }
        var formData = new FormData();
        formData.append('file', files[i]);
        formData.append('path', path);
        fetch(endpoint, {method: 'POST', body: formData})
        .then(function(r) { return r.json(); })
        .then(function(d) {
            done++;
            if (d.error) errors.push(files[i].name + ': ' + d.error);
            progressEl.textContent = 'Uploading ' + done + '/' + total + '...';
            uploadNext(i + 1);
        })
        .catch(function(e) {
            done++;
            errors.push(files[i].name + ': ' + e.message);
            progressEl.textContent = 'Uploading ' + done + '/' + total + '...';
            uploadNext(i + 1);
        });
    }
    uploadNext(0);
    // Clear file input
    document.getElementById(target === 's3' ? 's3-upload' : 'ws-upload').value = '';
}

// Init
loadWs(''); loadS3('');
</script>
</body></html>"""


# ===========================================
# Shared Space Templates
# ===========================================

SHARED_SPACE_NO_CONFIG = CSS + """<!DOCTYPE html><html><head><title>Shared Space</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span> - Shared Space</h1>
<div class="nav-right"><span>{{ username }}</span>
    <a href="/dashboard" class="btn btn-secondary btn-sm">Menu</a>
    <a href="/logout" class="btn btn-danger btn-sm" style="margin-left:10px">Logout</a>
</div></nav>
<div class="container">
    <div class="card" style="max-width:600px;margin:60px auto">
        <div class="card-body" style="text-align:center;padding:60px">
            <div style="font-size:64px;margin-bottom:20px">&#128101;</div>
            <h2 style="margin-bottom:15px">Shared Space Not Available</h2>
            <p style="color:#94a3b8">System S3 has not been configured yet. Please ask the administrator to set up S3 configuration.</p>
        </div>
    </div>
</div></body></html>"""

SHARED_SPACE_PAGE = CSS + """<!DOCTYPE html><html><head><title>Shared Space</title>
<style>
.drop-zone{border:2px dashed transparent;transition:all .2s;position:relative}
.drop-zone.drag-over{border-color:#6366f1;background:rgba(99,102,241,.1)}
.drop-zone.drag-over::after{content:'Drop files here';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(99,102,241,.9);color:#fff;padding:20px 40px;border-radius:10px;font-size:18px;z-index:100}
.upload-input{display:none}
.upload-progress{padding:8px 16px;border-top:1px solid #334155;font-size:13px;color:#94a3b8}
</style>
</head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span> - &#128101; Shared Space</h1>
<div class="nav-right"><span>{{ username }}</span>
    <a href="/dashboard" class="btn btn-secondary btn-sm">Menu</a>
    <a href="/logout" class="btn btn-danger btn-sm" style="margin-left:10px">Logout</a>
</div></nav>
<div class="container-wide">
    <div class="split-pane">
        <!-- Workspace Panel -->
        <div class="pane drop-zone" id="ws-pane" data-target="workspace">
            <div class="pane-header">
                <h3>&#128193; Workspace</h3>
                <div style="display:flex;gap:6px">
                    <label class="btn btn-sm btn-success" style="cursor:pointer">&#11014; Upload<input type="file" class="upload-input" id="ws-upload" multiple onchange="handleUpload('workspace', this.files)"></label>
                    <button class="btn btn-sm btn-secondary" onclick="wsMkdir()">New Folder</button>
                    <button class="btn btn-sm btn-danger" onclick="wsDelete()">Delete</button>
                </div>
            </div>
            <div class="breadcrumb" id="ws-breadcrumb" style="padding:8px 16px"></div>
            <div class="file-list" id="ws-list"></div>
            <div class="upload-progress" id="ws-upload-progress" style="display:none"></div>
        </div>

        <!-- Transfer Controls -->
        <div style="display:flex;flex-direction:column;justify-content:center;align-items:center;gap:10px;padding:0 5px">
            <button class="btn btn-primary" onclick="transferTo('s3')" title="Upload to Shared">&#10145; Shared</button>
            <button class="btn btn-success" onclick="transferTo('workspace')" title="Download to Workspace">&#11013; WS</button>
        </div>

        <!-- Shared S3 Panel -->
        <div class="pane drop-zone" id="s3-pane" data-target="s3">
            <div class="pane-header">
                <h3>&#128101; Shared Space</h3>
                <div style="display:flex;gap:6px">
                    <label class="btn btn-sm btn-success" style="cursor:pointer">&#11014; Upload<input type="file" class="upload-input" id="s3-upload" multiple onchange="handleUpload('s3', this.files)"></label>
                    <button class="btn btn-sm btn-secondary" onclick="s3Mkdir()">New Folder</button>
                    <button class="btn btn-sm btn-danger" onclick="s3Delete()">Delete</button>
                </div>
            </div>
            <div class="breadcrumb" id="s3-breadcrumb" style="padding:8px 16px"></div>
            <div class="file-list" id="s3-list"></div>
            <div class="upload-progress" id="s3-upload-progress" style="display:none"></div>
        </div>
    </div>

    <!-- Progress -->
    <div id="transfer-progress" class="card" style="margin-top:15px;display:none">
        <div class="card-body" style="padding:12px 20px">
            <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
            <div class="progress-text" id="progress-text">Preparing...</div>
        </div>
    </div>
</div>

<script>
var wsPath = '';
var s3Path = '';

function formatSize(b) {
    if (b === 0) return '-';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b/1048576).toFixed(1) + ' MB';
    return (b/1073741824).toFixed(2) + ' GB';
}

function renderBreadcrumb(el, path, onClick) {
    var parts = path ? path.split('/').filter(Boolean) : [];
    var html = '<a href="#" onclick="'+onClick+'(\\'\\');return false">Home</a>';
    var acc = '';
    parts.forEach(function(p) {
        acc += (acc ? '/' : '') + p;
        html += ' / <a href="#" onclick="'+onClick+'(\\''+acc+'\\');return false">'+p+'</a>';
    });
    document.getElementById(el).innerHTML = html;
}

function renderList(el, items, path, navFn, isS3) {
    var html = '';
    items.forEach(function(item) {
        var icon = item.type === 'dir' ? '&#128193;' : '&#128196;';
        var clickAction = item.type === 'dir' ? 'onclick="'+navFn+'(\\''+(path ? path+'/':'')+item.name+'\\')"' : '';
        html += '<div class="file-item" '+clickAction+'>' +
            '<input type="checkbox" value="'+item.name+'" onclick="event.stopPropagation()" data-panel="'+(isS3?'s3':'ws')+'">' +
            '<span class="file-icon">'+icon+'</span>' +
            '<span>'+item.name+'</span>' +
            '<span class="file-size">'+formatSize(item.size)+'</span></div>';
    });
    document.getElementById(el).innerHTML = html || '<div class="empty">Empty</div>';
}

function loadWs(path) {
    wsPath = path || '';
    fetch('/api/workspace/list?path='+encodeURIComponent(wsPath))
    .then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); return; }
        renderBreadcrumb('ws-breadcrumb', wsPath, 'loadWs');
        renderList('ws-list', d.items, wsPath, 'loadWs', false);
    });
}

function loadS3(path) {
    s3Path = path || '';
    fetch('/api/shared/list?path='+encodeURIComponent(s3Path))
    .then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); return; }
        renderBreadcrumb('s3-breadcrumb', s3Path, 'loadS3');
        renderList('s3-list', d.items, s3Path, 'loadS3', true);
    });
}

function getChecked(panel) {
    var boxes = document.querySelectorAll('#'+(panel==='s3'?'s3':'ws')+'-list input[type=checkbox]:checked');
    return Array.from(boxes).map(function(b) { return b.value; });
}

function transferTo(dest) {
    var source = dest === 's3' ? 'workspace' : 's3';
    var items = getChecked(source === 'workspace' ? 'ws' : 's3');
    if (!items.length) { alert('Select files first'); return; }
    var body = JSON.stringify({
        source: source, dest: dest, items: items,
        source_path: source === 'workspace' ? wsPath : s3Path,
        dest_path: dest === 's3' ? s3Path : wsPath
    });
    fetch('/api/shared/transfer', {method:'POST', headers:{'Content-Type':'application/json'}, body:body})
    .then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); return; }
        pollProgress(d.task_id);
    });
}

function pollProgress(taskId) {
    var el = document.getElementById('transfer-progress');
    el.style.display = 'block';
    var iv = setInterval(function() {
        fetch('/api/transfer/status/'+taskId).then(r => r.json()).then(d => {
            var pct = d.total ? Math.round(d.completed/d.total*100) : 0;
            document.getElementById('progress-fill').style.width = pct+'%';
            document.getElementById('progress-text').textContent = d.current_file ? ('Transferring: '+d.current_file+' ('+d.completed+'/'+d.total+')') : 'Preparing...';
            if (d.status === 'done') {
                clearInterval(iv);
                document.getElementById('progress-text').textContent = 'Transfer complete! ('+d.total+' items)';
                document.getElementById('progress-fill').style.width = '100%';
                loadWs(wsPath); loadS3(s3Path);
            } else if (d.status === 'error') {
                clearInterval(iv);
                document.getElementById('progress-text').textContent = 'Error: '+(d.error||'Unknown error');
            }
        });
    }, 1000);
}

function wsMkdir() {
    var name = prompt('Folder name:');
    if (!name) return;
    fetch('/api/workspace/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:(wsPath?wsPath+'/':'')+name})})
    .then(r => r.json()).then(function() { loadWs(wsPath); });
}
function s3Mkdir() {
    var name = prompt('Folder name:');
    if (!name) return;
    fetch('/api/shared/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:(s3Path?s3Path+'/':'')+name})})
    .then(r => r.json()).then(function() { loadS3(s3Path); });
}
function wsDelete() {
    var items = getChecked('ws');
    if (!items.length) return;
    if (!confirm('Delete '+items.length+' item(s)?')) return;
    fetch('/api/workspace/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:items, path:wsPath})})
    .then(r => r.json()).then(function() { loadWs(wsPath); });
}
function s3Delete() {
    var items = getChecked('s3');
    if (!items.length) return;
    if (!confirm('Delete '+items.length+' item(s) from Shared Space?')) return;
    fetch('/api/shared/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:items, path:s3Path})})
    .then(r => r.json()).then(function() { loadS3(s3Path); });
}

// Drag and drop upload
document.querySelectorAll('.drop-zone').forEach(function(zone) {
    ['dragenter', 'dragover'].forEach(function(evt) {
        zone.addEventListener(evt, function(e) {
            e.preventDefault();
            e.stopPropagation();
            zone.classList.add('drag-over');
        });
    });
    ['dragleave', 'drop'].forEach(function(evt) {
        zone.addEventListener(evt, function(e) {
            e.preventDefault();
            e.stopPropagation();
            zone.classList.remove('drag-over');
        });
    });
    zone.addEventListener('drop', function(e) {
        var target = zone.dataset.target;
        var files = e.dataTransfer.files;
        if (files.length) handleUpload(target, files);
    });
});

function handleUpload(target, files) {
    if (!files.length) return;
    var progressEl = document.getElementById(target === 's3' ? 's3-upload-progress' : 'ws-upload-progress');
    var path = target === 's3' ? s3Path : wsPath;
    var endpoint = target === 's3' ? '/api/shared/upload' : '/api/workspace/upload';
    var total = files.length;
    var done = 0;
    var errors = [];
    progressEl.style.display = 'block';
    progressEl.textContent = 'Uploading 0/' + total + '...';

    function uploadNext(i) {
        if (i >= total) {
            if (errors.length) {
                progressEl.textContent = 'Done with ' + errors.length + ' error(s): ' + errors[0];
            } else {
                progressEl.textContent = 'Uploaded ' + total + ' file(s)!';
                setTimeout(function() { progressEl.style.display = 'none'; }, 3000);
            }
            if (target === 's3') loadS3(s3Path); else loadWs(wsPath);
            return;
        }
        var formData = new FormData();
        formData.append('file', files[i]);
        formData.append('path', path);
        fetch(endpoint, {method: 'POST', body: formData})
        .then(function(r) { return r.json(); })
        .then(function(d) {
            done++;
            if (d.error) errors.push(files[i].name + ': ' + d.error);
            progressEl.textContent = 'Uploading ' + done + '/' + total + '...';
            uploadNext(i + 1);
        })
        .catch(function(e) {
            done++;
            errors.push(files[i].name + ': ' + e.message);
            progressEl.textContent = 'Uploading ' + done + '/' + total + '...';
            uploadNext(i + 1);
        });
    }
    uploadNext(0);
    // Clear file input
    document.getElementById(target === 's3' ? 's3-upload' : 'ws-upload').value = '';
}

// Init
loadWs(''); loadS3('');
</script>
</body></html>"""


# ===========================================
# Share Link Templates
# ===========================================

SHARE_PASSWORD_PAGE = CSS + """<!DOCTYPE html><html><head><title>Password Required</title></head><body>
<div class="login-container">
    <div class="login-box">
        <div class="login-header">
            <div class="icon">&#128274;</div>
            <h1>Password Required</h1>
            <p>This shared file is protected</p>
        </div>
        {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
        <form method="post">
            <div class="form-group"><label>Password</label><input type="password" name="password" class="form-control" required autofocus></div>
            <button type="submit" class="btn btn-primary" style="width:100%;padding:14px">Access</button>
        </form>
    </div>
</div></body></html>"""

SHARE_FILE_PAGE = CSS + """<!DOCTYPE html><html><head><title>{{ item_name }} - Shared File</title>
<style>
.preview-container{max-width:1000px;margin:0 auto;padding:20px}
.preview-box{background:#1e293b;border-radius:12px;overflow:hidden;margin-bottom:20px}
.preview-header{padding:16px 20px;border-bottom:1px solid #334155;display:flex;align-items:center;gap:12px}
.preview-header .icon{font-size:28px}
.preview-header h2{flex:1;font-size:16px;word-break:break-all;margin:0}
.preview-body{padding:20px;text-align:center}
.preview-body img{max-width:100%;max-height:70vh;border-radius:8px}
.preview-body video,.preview-body audio{max-width:100%;max-height:70vh}
.preview-body pre{text-align:left;background:#0f172a;padding:16px;border-radius:8px;overflow:auto;max-height:60vh;font-family:monospace;font-size:13px}
.preview-body iframe{width:100%;height:70vh;border:none;border-radius:8px;background:#fff}
.preview-actions{display:flex;gap:12px;justify-content:center;margin-top:20px}
.preview-info{text-align:center;color:#64748b;font-size:13px;margin-top:15px}
</style></head><body>
<div class="preview-container">
    <div class="preview-box">
        <div class="preview-header">
            <span class="icon">{{ icon|safe }}</span>
            <h2>{{ item_name }}</h2>
            <span style="color:#64748b;font-size:13px">Shared by {{ created_by }}</span>
        </div>
        <div class="preview-body">
            {% if preview_type == 'image' %}
            <img src="/share/{{ share_id }}/download" alt="{{ item_name }}">
            {% elif preview_type == 'video' %}
            <video src="/share/{{ share_id }}/download" controls autoplay></video>
            {% elif preview_type == 'audio' %}
            <div style="padding:60px 0"><div style="font-size:80px;margin-bottom:20px">&#127925;</div><audio src="/share/{{ share_id }}/download" controls autoplay></audio></div>
            {% elif preview_type == 'pdf' %}
            <iframe src="/share/{{ share_id }}/download"></iframe>
            {% elif preview_type == 'text' and content %}
            <pre>{{ content|e }}</pre>
            {% elif preview_type == 'html' and content %}
            <iframe srcdoc="{{ content|e }}"></iframe>
            {% else %}
            <div style="padding:60px 0"><div style="font-size:80px;margin-bottom:20px">{{ icon|safe }}</div><p style="color:#94a3b8">Preview not available for this file type</p></div>
            {% endif %}
        </div>
        <div class="preview-actions">
            <a href="/share/{{ share_id }}/download" class="btn btn-success" style="padding:12px 32px">&#11015; Download</a>
        </div>
    </div>
    <div class="preview-info">
        Downloaded {{ download_count }} time(s)
        {% if expires_at %} | Expires: {{ expires_at }}{% endif %}
    </div>
</div></body></html>"""

SHARE_FOLDER_PAGE = CSS + """<!DOCTYPE html><html><head><title>{{ item_name }} - Shared Folder</title>
<style>
.folder-container{max-width:900px;margin:0 auto;padding:30px}
.file-row{display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid #334155;gap:12px;cursor:pointer;transition:background .15s}
.file-row:hover{background:#1e293b}
.file-row .icon{font-size:20px;width:28px;text-align:center}
.file-row .name{flex:1;color:#e2e8f0}
.file-row .size{color:#64748b;font-size:13px}
.file-row .actions{display:flex;gap:8px}
.preview-modal{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.9);z-index:9999;display:none;flex-direction:column}
.preview-modal.show{display:flex}
.preview-modal-header{padding:16px 20px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #334155}
.preview-modal-header h3{flex:1;margin:0;font-size:16px}
.preview-modal-body{flex:1;overflow:auto;display:flex;align-items:center;justify-content:center;padding:20px}
.preview-modal-body img,.preview-modal-body video{max-width:100%;max-height:100%}
.preview-modal-body audio{width:80%;max-width:500px}
.preview-modal-body iframe{width:100%;height:100%;border:none;background:#fff;border-radius:8px}
.preview-modal-body pre{width:100%;height:100%;overflow:auto;background:#0f172a;padding:20px;border-radius:8px;font-family:monospace;text-align:left}
</style></head><body>
<div class="folder-container">
    <div class="card">
        <div class="card-header">
            <h2>&#128193; {{ item_name }}</h2>
            <div style="display:flex;gap:10px;align-items:center">
                <span style="color:#64748b;font-size:13px">{{ files|length }} file(s) | {{ download_count }} downloads</span>
                <a href="/share/{{ share_id }}/download/zip" class="btn btn-success btn-sm">&#11015; Download ZIP</a>
            </div>
        </div>
        <div class="card-body" style="padding:0">
            {% for f in files %}
            <div class="file-row" onclick="previewFile('{{ f.name|e }}','{{ f.icon|safe }}')">
                <span class="icon">{{ f.icon|safe }}</span>
                <span class="name">{{ f.name }}</span>
                <span class="size">{{ f.size_fmt }}</span>
                <div class="actions"><a href="/share/{{ share_id }}/download?file={{ f.name|urlencode }}" class="btn btn-sm btn-secondary" onclick="event.stopPropagation()">&#11015;</a></div>
            </div>
            {% endfor %}
            {% if not files %}<div class="empty" style="padding:40px;text-align:center">Empty folder</div>{% endif %}
        </div>
    </div>
    <div style="text-align:center;color:#64748b;font-size:13px;margin-top:15px">
        Shared by {{ created_by }}{% if expires_at %} | Expires: {{ expires_at }}{% endif %}
    </div>
</div>
<div class="preview-modal" id="previewModal" onclick="closePreview()">
    <div class="preview-modal-header" onclick="event.stopPropagation()">
        <span class="icon" id="previewIcon"></span>
        <h3 id="previewName"></h3>
        <a id="previewDownload" class="btn btn-success btn-sm">&#11015; Download</a>
        <button class="btn btn-secondary btn-sm" onclick="closePreview()">&#10005; Close</button>
    </div>
    <div class="preview-modal-body" id="previewBody" onclick="event.stopPropagation()"></div>
</div>
<script>
var previewTypes={'jpg':'image','jpeg':'image','png':'image','gif':'image','webp':'image','svg':'image','bmp':'image','mp4':'video','webm':'video','ogg':'video','mov':'video','mp3':'audio','wav':'audio','flac':'audio','m4a':'audio','pdf':'pdf','txt':'text','log':'text','json':'text','xml':'text','yaml':'text','yml':'text','md':'text','py':'text','js':'text','css':'text','html':'html','htm':'html'};
var iconMap={'jpg':'&#128444;','jpeg':'&#128444;','png':'&#128444;','gif':'&#128444;','webp':'&#128444;','mp4':'&#127916;','webm':'&#127916;','mov':'&#127916;','mp3':'&#127925;','wav':'&#127925;','flac':'&#127925;','pdf':'&#128462;','doc':'&#128462;','docx':'&#128462;','xls':'&#128202;','xlsx':'&#128202;','ppt':'&#128253;','pptx':'&#128253;','txt':'&#128196;','md':'&#128221;','html':'&#127760;','htm':'&#127760;'};
function previewFile(name,icon){
    var ext=(name.split('.').pop()||'').toLowerCase();
    var type=previewTypes[ext]||'unknown';
    var url='/share/{{ share_id }}/download?file='+encodeURIComponent(name);
    document.getElementById('previewIcon').innerHTML=icon;
    document.getElementById('previewName').textContent=name;
    document.getElementById('previewDownload').href=url;
    var body=document.getElementById('previewBody');
    if(type==='image'){body.innerHTML='<img src="'+url+'">';}
    else if(type==='video'){body.innerHTML='<video src="'+url+'" controls autoplay></video>';}
    else if(type==='audio'){body.innerHTML='<div style="text-align:center"><div style="font-size:80px;margin-bottom:20px">&#127925;</div><audio src="'+url+'" controls autoplay></audio></div>';}
    else if(type==='pdf'){body.innerHTML='<iframe src="'+url+'"></iframe>';}
    else if(type==='text'){fetch(url).then(r=>r.text()).then(t=>{body.innerHTML='<pre>'+t.replace(/</g,'&lt;')+'</pre>';});}
    else if(type==='html'){fetch(url).then(r=>r.text()).then(t=>{body.innerHTML='<iframe srcdoc="'+t.replace(/"/g,'&quot;')+'"></iframe>';});}
    else{body.innerHTML='<div style="text-align:center"><div style="font-size:80px;margin-bottom:20px">'+icon+'</div><p style="color:#94a3b8">Preview not available</p><a href="'+url+'" class="btn btn-success">&#11015; Download</a></div>';}
    document.getElementById('previewModal').classList.add('show');
}
function closePreview(){document.getElementById('previewModal').classList.remove('show');document.getElementById('previewBody').innerHTML='';}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePreview();});
</script></body></html>"""

SHARE_NOT_FOUND = CSS + """<!DOCTYPE html><html><head><title>Not Found</title></head><body>
<div class="login-container">
    <div class="login-box" style="text-align:center">
        <div style="font-size:64px;margin-bottom:20px">&#128533;</div>
        <h1>Link Not Found</h1>
        <p style="color:#94a3b8;margin-top:10px">This share link does not exist or has been removed.</p>
    </div>
</div></body></html>"""

SHARE_EXPIRED = CSS + """<!DOCTYPE html><html><head><title>Expired</title></head><body>
<div class="login-container">
    <div class="login-box" style="text-align:center">
        <div style="font-size:64px;margin-bottom:20px">&#9203;</div>
        <h1>Link Expired</h1>
        <p style="color:#94a3b8;margin-top:10px">This share link has expired and is no longer available.</p>
    </div>
</div></body></html>"""

MY_SHARES_PAGE = CSS + """<!DOCTYPE html><html><head><title>My Shares</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span> - &#128279; My Shares</h1>
<div class="nav-right"><span>{{ username }}</span>
    <a href="/dashboard" class="btn btn-secondary btn-sm">Menu</a>
    <a href="/logout" class="btn btn-danger btn-sm" style="margin-left:10px">Logout</a>
</div></nav>
<div class="container" style="max-width:1100px">
    {% if message %}<div class="alert {{ 'alert-success' if success else 'alert-error' }}">{{ message }}</div>{% endif %}

    <div class="card">
        <div class="card-header"><h2>&#128279; My Shared Links ({{ shares|length }})</h2></div>
        <div class="card-body" style="padding:0">
            {% if shares %}
            <table><thead><tr><th>Name</th><th>Type</th><th>Password</th><th>Expires</th><th>Downloads</th><th>Actions</th></tr></thead><tbody>
            {% for s in shares %}
            <tr>
                <td><strong>{{ s.item_name }}</strong></td>
                <td><span class="tag {{ 'tag-blue' if s.item_type == 'dir' else 'tag-green' }}">{{ s.item_type }}</span></td>
                <td>{{ '&#128274;' if s.has_password else '-' }}</td>
                <td style="font-size:13px;color:#94a3b8">{{ s.expires_at.strftime('%Y-%m-%d %H:%M') if s.expires_at else 'Never' }}</td>
                <td>{{ s.download_count }}</td>
                <td><div class="actions">
                    <button class="btn btn-primary btn-sm" onclick="copyLink('{{ s._id }}')">Copy Link</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteShare('{{ s._id }}')">Delete</button>
                </div></td>
            </tr>
            {% endfor %}
            </tbody></table>
            {% else %}<div class="empty">No shared links yet. Go to S3 Backup and use the Share button.</div>{% endif %}
        </div>
    </div>
</div>
<script>
function copyLink(id) {
    var url = location.origin + '/share/' + id;
    navigator.clipboard.writeText(url).then(function() {
        alert('Link copied!');
    }).catch(function() {
        prompt('Copy this link:', url);
    });
}
function deleteShare(id) {
    if (!confirm('Delete this share link?')) return;
    fetch('/api/share/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({share_id:id})})
    .then(r => r.json()).then(d => {
        if (d.success) location.reload();
        else alert(d.error || 'Failed');
    });
}
</script>
</body></html>"""


# ===========================================
# Embed Templates (for desktop UI iframes)
# ===========================================

EMBED_CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.container{padding:20px;max-width:100%}
.card{background:#1e293b;border-radius:12px;border:1px solid #334155;margin-bottom:16px}
.card-header{padding:16px 20px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center}
.card-header h2{font-size:16px;font-weight:600}
.card-body{padding:20px}
.form-group{margin-bottom:16px}
.form-group label{display:block;margin-bottom:6px;color:#94a3b8;font-size:13px}
.form-control{width:100%;padding:10px 14px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;font-size:14px}
.form-control:focus{outline:none;border-color:#6366f1}
.form-row{display:flex;gap:12px}
.form-row .form-group{flex:1}
.btn{padding:10px 18px;border:none;border-radius:6px;cursor:pointer;font-weight:500;font-size:14px;transition:all .15s}
.btn-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
.btn-success{background:#10b981;color:#fff}
.btn-danger{background:#ef4444;color:#fff}
.btn-secondary{background:#475569;color:#fff}
.btn-sm{padding:6px 12px;font-size:13px}
.btn:hover{filter:brightness(1.1)}
.alert{padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:13px}
.alert-success{background:rgba(16,185,129,.2);border:1px solid #10b981;color:#10b981}
.alert-error{background:rgba(239,68,68,.2);border:1px solid #ef4444;color:#ef4444}
.alert-info{background:rgba(99,102,241,.2);border:1px solid #6366f1;color:#818cf8}
table{width:100%;border-collapse:collapse}
th,td{padding:12px 14px;text-align:left;border-bottom:1px solid #334155}
th{color:#94a3b8;font-weight:500;font-size:12px;text-transform:uppercase}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500}
.tag-green{background:rgba(16,185,129,.2);color:#10b981}
.tag-blue{background:rgba(99,102,241,.2);color:#818cf8}
.empty{text-align:center;padding:40px;color:#64748b}
.actions{display:flex;gap:6px;flex-wrap:wrap}
/* File browser */
.split-pane{display:flex;gap:16px;height:calc(100vh - 40px)}
.pane{flex:1;background:#1e293b;border-radius:10px;border:1px solid #334155;display:flex;flex-direction:column;overflow:hidden}
.pane-header{padding:10px 14px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;background:#1e293b;flex-shrink:0}
.pane-header h3{font-size:13px;font-weight:600}
.breadcrumb{display:flex;gap:4px;align-items:center;font-size:12px;color:#94a3b8;padding:8px 14px;flex-wrap:wrap;flex-shrink:0}
.breadcrumb a{color:#818cf8;text-decoration:none}
.file-list{flex:1;overflow-y:auto;padding:6px}
.file-item{display:flex;align-items:center;padding:6px 10px;border-radius:5px;cursor:pointer;gap:8px;font-size:13px}
.file-item:hover{background:#334155}
.file-item input[type=checkbox]{accent-color:#6366f1}
.file-icon{width:18px;text-align:center}
.file-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-size{color:#64748b;font-size:11px;margin-left:auto;flex-shrink:0}
.progress-bar{height:5px;background:#334155;border-radius:3px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,#6366f1,#10b981);width:0%}
.progress-text{font-size:11px;color:#94a3b8;margin-top:4px}
.drop-zone{border:2px dashed transparent;transition:all .2s;position:relative}
.drop-zone.drag-over{border-color:#6366f1;background:rgba(99,102,241,.1)}
.upload-input{display:none}
.upload-progress{padding:6px 14px;border-top:1px solid #334155;font-size:12px;color:#94a3b8}
iframe{width:100%;height:100%;border:none}
</style>"""

EMBED_LAB = EMBED_CSS + """<!DOCTYPE html><html><head><title>JupyterLab</title></head><body style="overflow:hidden">
<iframe id="labframe" src="/user/{{ username }}/lab" style="width:100%;height:100vh"></iframe>
<script>
// Auto-retry on 502, stop after successful load
var retryCount = 0, maxRetry = 10, loaded = false;
function check502() {
    if (loaded || retryCount >= maxRetry) return;
    retryCount++;
    try {
        var f = document.getElementById('labframe');
        var d = f.contentDocument || f.contentWindow.document;
        if (d.body && d.body.innerHTML.length > 100 && !d.body.innerHTML.includes('502') && !d.body.innerHTML.includes('Bad Gateway')) {
            loaded = true; return;
        }
        if (d.body && (d.body.innerHTML.includes('502') || d.body.innerHTML.includes('Bad Gateway'))) {
            f.src = f.src;
        }
    } catch(e) { loaded = true; } // Cross-origin means JupyterLab loaded
    if (!loaded) setTimeout(check502, 3000);
}
setTimeout(check502, 3000);
</script>
</body></html>"""

EMBED_S3_BACKUP = EMBED_CSS + """<!DOCTYPE html><html><head><title>S3 Backup</title></head><body>
<div class="container" style="padding:12px;height:100vh;overflow:hidden">
    <div class="split-pane">
        <div class="pane drop-zone" id="ws-pane" data-target="workspace">
            <div class="pane-header">
                <h3>&#128193; Workspace</h3>
                <div style="display:flex;gap:4px">
                    <label class="btn btn-sm btn-success" style="cursor:pointer">&#11014;<input type="file" class="upload-input" id="ws-upload" multiple onchange="handleUpload('workspace',this.files)"></label>
                    <button class="btn btn-sm btn-secondary" onclick="wsMkdir()">+Folder</button>
                    <button class="btn btn-sm btn-danger" onclick="wsDelete()">Del</button>
                </div>
            </div>
            <div class="breadcrumb" id="ws-breadcrumb"></div>
            <div class="file-list" id="ws-list"></div>
            <div class="upload-progress" id="ws-upload-progress" style="display:none"></div>
        </div>
        <div style="display:flex;flex-direction:column;justify-content:center;gap:8px;padding:0 4px">
            <button class="btn btn-primary btn-sm" onclick="transferTo('s3')">&#10145;</button>
            <button class="btn btn-success btn-sm" onclick="transferTo('workspace')">&#11013;</button>
        </div>
        <div class="pane drop-zone" id="s3-pane" data-target="s3">
            <div class="pane-header">
                <h3>&#9729; S3 Storage</h3>
                <div style="display:flex;gap:4px">
                    <label class="btn btn-sm btn-success" style="cursor:pointer">&#11014;<input type="file" class="upload-input" id="s3-upload" multiple onchange="handleUpload('s3',this.files)"></label>
                    <button class="btn btn-sm btn-primary" onclick="s3Share()">Share</button>
                    <button class="btn btn-sm btn-secondary" onclick="s3Mkdir()">+Folder</button>
                    <button class="btn btn-sm btn-danger" onclick="s3Delete()">Del</button>
                </div>
            </div>
            <div class="breadcrumb" id="s3-breadcrumb"></div>
            <div class="file-list" id="s3-list"></div>
            <div class="upload-progress" id="s3-upload-progress" style="display:none"></div>
        </div>
    </div>
    <div id="transfer-progress" style="display:none;margin-top:8px">
        <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
        <div class="progress-text" id="progress-text"></div>
    </div>
</div>
<script>
var wsPath='',s3Path='';
function formatSize(b){if(b===0)return'-';if(b<1024)return b+' B';if(b<1048576)return(b/1024).toFixed(1)+' KB';if(b<1073741824)return(b/1048576).toFixed(1)+' MB';return(b/1073741824).toFixed(2)+' GB';}
function renderBreadcrumb(el,path,fn){var parts=path?path.split('/').filter(Boolean):[];var html='<a href="#" onclick="'+fn+'(\\'\\');return false">Home</a>';var acc='';parts.forEach(function(p){acc+=(acc?'/':'')+p;html+=' / <a href="#" onclick="'+fn+'(\\''+acc+'\\');return false">'+p+'</a>';});document.getElementById(el).innerHTML=html;}
function getFileIcon(name){var ext=(name.split('.').pop()||'').toLowerCase();var m={'jpg':'&#128444;','jpeg':'&#128444;','png':'&#128444;','gif':'&#128444;','webp':'&#128444;','svg':'&#128444;','bmp':'&#128444;','mp4':'&#127916;','webm':'&#127916;','mov':'&#127916;','avi':'&#127916;','mkv':'&#127916;','mp3':'&#127925;','wav':'&#127925;','flac':'&#127925;','m4a':'&#127925;','pdf':'&#128462;','doc':'&#128462;','docx':'&#128462;','xls':'&#128202;','xlsx':'&#128202;','ppt':'&#128253;','pptx':'&#128253;','md':'&#128221;','html':'&#127760;','htm':'&#127760;','py':'&#128196;','js':'&#128196;','json':'&#128196;','txt':'&#128196;','log':'&#128196;','zip':'&#128230;','rar':'&#128230;','7z':'&#128230;','tar':'&#128230;','gz':'&#128230;'};return m[ext]||'&#128196;';}
function openFile(source,path,name){if(window.parent&&window.parent.openFileViewer){window.parent.openFileViewer(source,path,name);}else{window.open('/viewer/'+source+'?path='+encodeURIComponent(path),'_blank');}}
function renderList(el,items,path,fn,isS3){var html='';var src=isS3?'s3':'workspace';items.forEach(function(i){var icon=i.type==='dir'?'&#128193;':getFileIcon(i.name);var fpath=(path?path+'/':'')+i.name;var click=i.type==='dir'?'onclick="'+fn+'(\\''+fpath+'\\');"':'ondblclick="openFile(\\''+src+'\\',\\''+fpath+'\\',\\''+i.name+'\\');"';html+='<div class="file-item" '+click+'><input type="checkbox" value="'+i.name+'" onclick="event.stopPropagation()"><span class="file-icon">'+icon+'</span><span class="file-name">'+i.name+'</span><span class="file-size">'+formatSize(i.size)+'</span></div>';});document.getElementById(el).innerHTML=html||'<div class="empty">Empty</div>';}
function loadWs(p){wsPath=p||'';fetch('/api/workspace/list?path='+encodeURIComponent(wsPath)).then(r=>r.json()).then(d=>{if(d.error){alert(d.error);return;}renderBreadcrumb('ws-breadcrumb',wsPath,'loadWs');renderList('ws-list',d.items,wsPath,'loadWs',false);});}
function loadS3(p){s3Path=p||'';fetch('/api/s3/list?path='+encodeURIComponent(s3Path)).then(r=>r.json()).then(d=>{if(d.error){alert(d.error);return;}renderBreadcrumb('s3-breadcrumb',s3Path,'loadS3');renderList('s3-list',d.items,s3Path,'loadS3',true);});}
function getChecked(p){return Array.from(document.querySelectorAll('#'+(p==='s3'?'s3':'ws')+'-list input:checked')).map(b=>b.value);}
function transferTo(dest){var src=dest==='s3'?'workspace':'s3';var items=getChecked(src==='workspace'?'ws':'s3');if(!items.length){alert('Select files');return;}fetch('/api/transfer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:src,dest:dest,items:items,source_path:src==='workspace'?wsPath:s3Path,dest_path:dest==='s3'?s3Path:wsPath})}).then(r=>r.json()).then(d=>{if(d.error){alert(d.error);return;}pollProgress(d.task_id);});}
function pollProgress(tid){var el=document.getElementById('transfer-progress');el.style.display='block';var iv=setInterval(function(){fetch('/api/transfer/status/'+tid).then(r=>r.json()).then(d=>{var pct=d.total?Math.round(d.completed/d.total*100):0;document.getElementById('progress-fill').style.width=pct+'%';document.getElementById('progress-text').textContent=d.current_file?'Transferring: '+d.current_file+' ('+d.completed+'/'+d.total+')':'Preparing...';if(d.status==='done'){clearInterval(iv);document.getElementById('progress-text').textContent='Done!';loadWs(wsPath);loadS3(s3Path);}else if(d.status==='error'){clearInterval(iv);document.getElementById('progress-text').textContent='Error: '+d.error;}});},1000);}
function wsMkdir(){var n=prompt('Folder name:');if(!n)return;fetch('/api/workspace/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:(wsPath?wsPath+'/':'')+n})}).then(()=>loadWs(wsPath));}
function s3Mkdir(){var n=prompt('Folder name:');if(!n)return;fetch('/api/s3/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:(s3Path?s3Path+'/':'')+n})}).then(()=>loadS3(s3Path));}
function wsDelete(){var items=getChecked('ws');if(!items.length||!confirm('Delete?'))return;fetch('/api/workspace/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items,path:wsPath})}).then(()=>loadWs(wsPath));}
function s3Delete(){var items=getChecked('s3');if(!items.length||!confirm('Delete from S3?'))return;fetch('/api/s3/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items,path:s3Path})}).then(()=>loadS3(s3Path));}
function s3Share(){var items=getChecked('s3');if(items.length!==1){alert('Select 1 item');return;}var name=items[0];var el=document.querySelector('#s3-list input[value="'+name+'"]');var fi=el?el.closest('.file-item'):null;var icon=fi?fi.querySelector('.file-icon').innerHTML:'';var type=icon.indexOf('128193')>=0?'dir':'file';var pw=prompt('Password (empty=none):');var hrs=prompt('Expire hours (0=never):');fetch('/api/share/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,type:type,s3_path:s3Path,password:pw||'',expires_hours:parseInt(hrs)||0})}).then(r=>r.json()).then(d=>{if(d.error){alert(d.error);return;}prompt('Share link:',location.origin+'/share/'+d.share_id);});}
document.querySelectorAll('.drop-zone').forEach(z=>{['dragenter','dragover'].forEach(e=>z.addEventListener(e,ev=>{ev.preventDefault();z.classList.add('drag-over');}));['dragleave','drop'].forEach(e=>z.addEventListener(e,ev=>{ev.preventDefault();z.classList.remove('drag-over');}));z.addEventListener('drop',e=>{handleUpload(z.dataset.target,e.dataTransfer.files);});});
function handleUpload(t,files){if(!files.length)return;var prog=document.getElementById(t==='s3'?'s3-upload-progress':'ws-upload-progress');var path=t==='s3'?s3Path:wsPath;var ep=t==='s3'?'/api/s3/upload':'/api/workspace/upload';var total=files.length,done=0,errs=[];prog.style.display='block';prog.textContent='0/'+total;function next(i){if(i>=total){prog.textContent=errs.length?'Errors: '+errs[0]:'Done!';setTimeout(()=>prog.style.display='none',2000);t==='s3'?loadS3(s3Path):loadWs(wsPath);return;}var fd=new FormData();fd.append('file',files[i]);fd.append('path',path);fetch(ep,{method:'POST',body:fd}).then(r=>r.json()).then(d=>{done++;if(d.error)errs.push(files[i].name);prog.textContent=done+'/'+total;next(i+1);}).catch(()=>{done++;errs.push(files[i].name);next(i+1);});}next(0);document.getElementById(t==='s3'?'s3-upload':'ws-upload').value='';}
loadWs('');loadS3('');
</script></body></html>"""

EMBED_SHARED_SPACE = EMBED_CSS + """<!DOCTYPE html><html><head><title>Shared Space</title></head><body>
<div class="container" style="padding:12px;height:100vh;overflow:hidden">
    <div class="split-pane">
        <div class="pane drop-zone" id="ws-pane" data-target="workspace">
            <div class="pane-header">
                <h3>&#128193; Workspace</h3>
                <div style="display:flex;gap:4px">
                    <label class="btn btn-sm btn-success" style="cursor:pointer">&#11014;<input type="file" class="upload-input" id="ws-upload" multiple onchange="handleUpload('workspace',this.files)"></label>
                    <button class="btn btn-sm btn-secondary" onclick="wsMkdir()">+Folder</button>
                    <button class="btn btn-sm btn-danger" onclick="wsDelete()">Del</button>
                </div>
            </div>
            <div class="breadcrumb" id="ws-breadcrumb"></div>
            <div class="file-list" id="ws-list"></div>
            <div class="upload-progress" id="ws-upload-progress" style="display:none"></div>
        </div>
        <div style="display:flex;flex-direction:column;justify-content:center;gap:8px;padding:0 4px">
            <button class="btn btn-primary btn-sm" onclick="transferTo('s3')">&#10145;</button>
            <button class="btn btn-success btn-sm" onclick="transferTo('workspace')">&#11013;</button>
        </div>
        <div class="pane drop-zone" id="s3-pane" data-target="s3">
            <div class="pane-header">
                <h3>&#128101; Shared Space</h3>
                <div style="display:flex;gap:4px">
                    <label class="btn btn-sm btn-success" style="cursor:pointer">&#11014;<input type="file" class="upload-input" id="s3-upload" multiple onchange="handleUpload('s3',this.files)"></label>
                    <button class="btn btn-sm btn-secondary" onclick="s3Mkdir()">+Folder</button>
                    <button class="btn btn-sm btn-danger" onclick="s3Delete()">Del</button>
                </div>
            </div>
            <div class="breadcrumb" id="s3-breadcrumb"></div>
            <div class="file-list" id="s3-list"></div>
            <div class="upload-progress" id="s3-upload-progress" style="display:none"></div>
        </div>
    </div>
    <div id="transfer-progress" style="display:none;margin-top:8px">
        <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
        <div class="progress-text" id="progress-text"></div>
    </div>
</div>
<script>
var wsPath='',s3Path='';
function formatSize(b){if(b===0)return'-';if(b<1024)return b+' B';if(b<1048576)return(b/1024).toFixed(1)+' KB';if(b<1073741824)return(b/1048576).toFixed(1)+' MB';return(b/1073741824).toFixed(2)+' GB';}
function renderBreadcrumb(el,path,fn){var parts=path?path.split('/').filter(Boolean):[];var html='<a href="#" onclick="'+fn+'(\\'\\');return false">Home</a>';var acc='';parts.forEach(function(p){acc+=(acc?'/':'')+p;html+=' / <a href="#" onclick="'+fn+'(\\''+acc+'\\');return false">'+p+'</a>';});document.getElementById(el).innerHTML=html;}
function getFileIcon(name){var ext=(name.split('.').pop()||'').toLowerCase();var m={'jpg':'&#128444;','jpeg':'&#128444;','png':'&#128444;','gif':'&#128444;','webp':'&#128444;','svg':'&#128444;','bmp':'&#128444;','mp4':'&#127916;','webm':'&#127916;','mov':'&#127916;','avi':'&#127916;','mkv':'&#127916;','mp3':'&#127925;','wav':'&#127925;','flac':'&#127925;','m4a':'&#127925;','pdf':'&#128462;','doc':'&#128462;','docx':'&#128462;','xls':'&#128202;','xlsx':'&#128202;','ppt':'&#128253;','pptx':'&#128253;','md':'&#128221;','html':'&#127760;','htm':'&#127760;','py':'&#128196;','js':'&#128196;','json':'&#128196;','txt':'&#128196;','log':'&#128196;','zip':'&#128230;','rar':'&#128230;','7z':'&#128230;','tar':'&#128230;','gz':'&#128230;'};return m[ext]||'&#128196;';}
function openFile(source,path,name){if(window.parent&&window.parent.openFileViewer){window.parent.openFileViewer(source,path,name);}else{window.open('/viewer/'+source+'?path='+encodeURIComponent(path),'_blank');}}
function renderList(el,items,path,fn,isS3){var html='';var src=isS3?'shared':'workspace';items.forEach(function(i){var icon=i.type==='dir'?'&#128193;':getFileIcon(i.name);var fpath=(path?path+'/':'')+i.name;var click=i.type==='dir'?'onclick="'+fn+'(\\''+fpath+'\\');"':'ondblclick="openFile(\\''+src+'\\',\\''+fpath+'\\',\\''+i.name+'\\');"';html+='<div class="file-item" '+click+'><input type="checkbox" value="'+i.name+'" onclick="event.stopPropagation()"><span class="file-icon">'+icon+'</span><span class="file-name">'+i.name+'</span><span class="file-size">'+formatSize(i.size)+'</span></div>';});document.getElementById(el).innerHTML=html||'<div class="empty">Empty</div>';}
function loadWs(p){wsPath=p||'';fetch('/api/workspace/list?path='+encodeURIComponent(wsPath)).then(r=>r.json()).then(d=>{if(d.error){alert(d.error);return;}renderBreadcrumb('ws-breadcrumb',wsPath,'loadWs');renderList('ws-list',d.items,wsPath,'loadWs',false);});}
function loadS3(p){s3Path=p||'';fetch('/api/shared/list?path='+encodeURIComponent(s3Path)).then(r=>r.json()).then(d=>{if(d.error){alert(d.error);return;}renderBreadcrumb('s3-breadcrumb',s3Path,'loadS3');renderList('s3-list',d.items,s3Path,'loadS3',true);});}
function getChecked(p){return Array.from(document.querySelectorAll('#'+(p==='s3'?'s3':'ws')+'-list input:checked')).map(b=>b.value);}
function transferTo(dest){var src=dest==='s3'?'workspace':'s3';var items=getChecked(src==='workspace'?'ws':'s3');if(!items.length){alert('Select files');return;}fetch('/api/shared/transfer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:src,dest:dest,items:items,source_path:src==='workspace'?wsPath:s3Path,dest_path:dest==='s3'?s3Path:wsPath})}).then(r=>r.json()).then(d=>{if(d.error){alert(d.error);return;}pollProgress(d.task_id);});}
function pollProgress(tid){var el=document.getElementById('transfer-progress');el.style.display='block';var iv=setInterval(function(){fetch('/api/transfer/status/'+tid).then(r=>r.json()).then(d=>{var pct=d.total?Math.round(d.completed/d.total*100):0;document.getElementById('progress-fill').style.width=pct+'%';document.getElementById('progress-text').textContent=d.current_file?'Transferring: '+d.current_file:'Preparing...';if(d.status==='done'){clearInterval(iv);document.getElementById('progress-text').textContent='Done!';loadWs(wsPath);loadS3(s3Path);}else if(d.status==='error'){clearInterval(iv);document.getElementById('progress-text').textContent='Error: '+d.error;}});},1000);}
function wsMkdir(){var n=prompt('Folder name:');if(!n)return;fetch('/api/workspace/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:(wsPath?wsPath+'/':'')+n})}).then(()=>loadWs(wsPath));}
function s3Mkdir(){var n=prompt('Folder name:');if(!n)return;fetch('/api/shared/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:(s3Path?s3Path+'/':'')+n})}).then(()=>loadS3(s3Path));}
function wsDelete(){var items=getChecked('ws');if(!items.length||!confirm('Delete?'))return;fetch('/api/workspace/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items,path:wsPath})}).then(()=>loadWs(wsPath));}
function s3Delete(){var items=getChecked('s3');if(!items.length||!confirm('Delete?'))return;fetch('/api/shared/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items,path:s3Path})}).then(()=>loadS3(s3Path));}
document.querySelectorAll('.drop-zone').forEach(z=>{['dragenter','dragover'].forEach(e=>z.addEventListener(e,ev=>{ev.preventDefault();z.classList.add('drag-over');}));['dragleave','drop'].forEach(e=>z.addEventListener(e,ev=>{ev.preventDefault();z.classList.remove('drag-over');}));z.addEventListener('drop',e=>{handleUpload(z.dataset.target,e.dataTransfer.files);});});
function handleUpload(t,files){if(!files.length)return;var prog=document.getElementById(t==='s3'?'s3-upload-progress':'ws-upload-progress');var path=t==='s3'?s3Path:wsPath;var ep=t==='s3'?'/api/shared/upload':'/api/workspace/upload';var total=files.length,done=0,errs=[];prog.style.display='block';prog.textContent='0/'+total;function next(i){if(i>=total){prog.textContent=errs.length?'Errors: '+errs[0]:'Done!';setTimeout(()=>prog.style.display='none',2000);t==='s3'?loadS3(s3Path):loadWs(wsPath);return;}var fd=new FormData();fd.append('file',files[i]);fd.append('path',path);fetch(ep,{method:'POST',body:fd}).then(r=>r.json()).then(d=>{done++;if(d.error)errs.push(files[i].name);prog.textContent=done+'/'+total;next(i+1);}).catch(()=>{done++;errs.push(files[i].name);next(i+1);});}next(0);document.getElementById(t==='s3'?'s3-upload':'ws-upload').value='';}
loadWs('');loadS3('');
</script></body></html>"""

EMBED_MY_SHARES = EMBED_CSS + """<!DOCTYPE html><html><head><title>My Shares</title></head><body>
<div class="container">
    <div class="card">
        <div class="card-header"><h2>&#128279; My Shared Links</h2></div>
        <div class="card-body" style="padding:0" id="shares-content">Loading...</div>
    </div>
</div>
<script>
function load(){
    fetch('/api/share/list').then(r=>r.json()).then(d=>{
        if(d.error){document.getElementById('shares-content').innerHTML='<div class="empty">'+d.error+'</div>';return;}
        if(!d.shares||!d.shares.length){document.getElementById('shares-content').innerHTML='<div class="empty">No shares yet</div>';return;}
        var html='<table><thead><tr><th>Name</th><th>Type</th><th>Password</th><th>Expires</th><th>Downloads</th><th>Actions</th></tr></thead><tbody>';
        d.shares.forEach(s=>{
            html+='<tr><td><strong>'+s.item_name+'</strong></td>';
            html+='<td><span class="tag '+(s.item_type==='dir'?'tag-blue':'tag-green')+'">'+s.item_type+'</span></td>';
            html+='<td>'+(s.has_password?'&#128274;':'-')+'</td>';
            html+='<td style="font-size:12px;color:#94a3b8">'+(s.expires_at?new Date(s.expires_at).toLocaleString():'Never')+'</td>';
            html+='<td>'+s.download_count+'</td>';
            html+='<td><div class="actions"><button class="btn btn-primary btn-sm" onclick="copyLink(\\''+s._id+'\\')">Copy</button><button class="btn btn-danger btn-sm" onclick="delShare(\\''+s._id+'\\')">Del</button></div></td></tr>';
        });
        html+='</tbody></table>';
        document.getElementById('shares-content').innerHTML=html;
    });
}
function copyLink(id){var url=location.origin+'/share/'+id;navigator.clipboard.writeText(url).then(()=>alert('Copied!')).catch(()=>prompt('Copy:',url));}
function delShare(id){if(!confirm('Delete?'))return;fetch('/api/share/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({share_id:id})}).then(r=>r.json()).then(d=>{if(d.success)load();else alert(d.error);});}
load();
</script></body></html>"""

EMBED_S3_CONFIG = EMBED_CSS + """<!DOCTYPE html><html><head><title>S3 Config</title></head><body>
<div class="container">
    {% if message %}<div class="alert {{ 'alert-success' if success else 'alert-error' }}">{{ message }}</div>{% endif %}
    {% if system_s3 %}<div class="alert alert-info">System S3 configured. Set personal config below to override.</div>{% endif %}
    <div class="card">
        <div class="card-header">
            <h2>&#9881; Personal S3</h2>
            {% if has_personal %}<form method="post" action="/user/s3-config/delete" onsubmit="return confirm('Remove?')"><button class="btn btn-danger btn-sm">Remove</button></form>{% endif %}
        </div>
        <div class="card-body">
            <form method="post" action="/embed/s3-config">
                <div class="form-row">
                    <div class="form-group"><label>Endpoint URL</label><input type="text" name="endpoint_url" class="form-control" value="{{ config.endpoint_url or '' }}" placeholder="https://s3.amazonaws.com"></div>
                    <div class="form-group"><label>Region</label><input type="text" name="region" class="form-control" value="{{ config.region or '' }}" placeholder="us-east-1"></div>
                </div>
                <div class="form-row">
                    <div class="form-group"><label>Access Key</label><input type="text" name="access_key" class="form-control" value="{{ config.access_key or '' }}"></div>
                    <div class="form-group"><label>Secret Key</label><input type="password" name="secret_key" class="form-control" value="{{ config.secret_key or '' }}"></div>
                </div>
                <div class="form-row">
                    <div class="form-group"><label>Bucket</label><input type="text" name="bucket_name" class="form-control" value="{{ config.bucket_name or '' }}"></div>
                    <div class="form-group"><label>Prefix</label><input type="text" name="prefix" class="form-control" value="{{ config.prefix or '' }}"></div>
                </div>
                <div style="display:flex;gap:10px">
                    <button type="submit" class="btn btn-primary">Save</button>
                    <button type="button" class="btn btn-success" onclick="testConn()">Test</button>
                </div>
            </form>
            <div id="test-result" style="margin-top:12px"></div>
        </div>
    </div>
</div>
<script>
function testConn(){var fd=new FormData(document.querySelector('form'));fetch('/user/s3-config/test',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{document.getElementById('test-result').innerHTML='<div class="alert '+(d.success?'alert-success':'alert-error')+'">'+d.message+'</div>';});}
</script></body></html>"""

EMBED_CHANGE_PW = EMBED_CSS + """<!DOCTYPE html><html><head><title>Change Password</title></head><body>
<div class="container">
    <div class="card">
        <div class="card-header"><h2>&#128274; Change Password</h2></div>
        <div class="card-body">
            {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
            {% if success %}<div class="alert alert-success">{{ success }}</div>{% endif %}
            <form method="post" action="/embed/change-password">
                <div class="form-group"><label>Current Password</label><input type="password" name="old_password" class="form-control" required></div>
                <div class="form-group"><label>New Password</label><input type="password" name="new_password" class="form-control" required></div>
                <div class="form-group"><label>Confirm Password</label><input type="password" name="confirm_password" class="form-control" required></div>
                <button type="submit" class="btn btn-primary">Change Password</button>
            </form>
        </div>
    </div>
</div></body></html>"""

# ===========================================
# File Viewer Templates
# ===========================================

VIEWER_BASE_CSS = """<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.viewer-container{width:100%;height:100vh;display:flex;flex-direction:column}
.viewer-header{background:#1e293b;padding:12px 16px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #334155;flex-shrink:0}
.viewer-header .icon{font-size:24px}
.viewer-header .filename{flex:1;font-size:14px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.viewer-header .btn{background:#334155;border:none;color:#e2e8f0;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:13px;display:flex;align-items:center;gap:6px}
.viewer-header .btn:hover{background:#475569}
.viewer-header .btn-primary{background:#6366f1}
.viewer-header .btn-primary:hover{background:#818cf8}
.viewer-body{flex:1;overflow:auto;position:relative}
</style>"""

VIEWER_IMAGE = """<!DOCTYPE html><html><head><title>{{ filename }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
""" + VIEWER_BASE_CSS + """
<style>
.image-container{width:100%;height:100%;display:flex;align-items:center;justify-content:center;overflow:auto;background:#000}
.image-container img{max-width:100%;max-height:100%;object-fit:contain;cursor:zoom-in;transition:transform .2s}
.image-container img.zoomed{max-width:none;max-height:none;cursor:zoom-out}
.zoom-controls{position:fixed;bottom:20px;right:20px;display:flex;gap:8px}
.zoom-controls button{background:rgba(30,41,59,.9);border:1px solid #334155;color:#e2e8f0;width:40px;height:40px;border-radius:8px;cursor:pointer;font-size:18px}
.zoom-controls button:hover{background:#334155}
</style></head><body>
<div class="viewer-container">
    <div class="viewer-header">
        <span class="icon">&#128444;</span>
        <span class="filename">{{ filename }}</span>
        <a href="{{ download_url }}" class="btn btn-primary" download><span>&#11015;</span> Download</a>
    </div>
    <div class="viewer-body">
        <div class="image-container" id="imgContainer">
            <img src="{{ file_url }}" id="img" onclick="toggleZoom()">
        </div>
        <div class="zoom-controls">
            <button onclick="zoomOut()">-</button>
            <button onclick="resetZoom()">&#8634;</button>
            <button onclick="zoomIn()">+</button>
        </div>
    </div>
</div>
<script>
let scale=1;const img=document.getElementById('img');
function toggleZoom(){img.classList.toggle('zoomed');scale=img.classList.contains('zoomed')?2:1;img.style.transform='scale('+scale+')';}
function zoomIn(){scale=Math.min(5,scale+0.5);img.style.transform='scale('+scale+')';if(scale>1)img.classList.add('zoomed');}
function zoomOut(){scale=Math.max(0.5,scale-0.5);img.style.transform='scale('+scale+')';if(scale<=1)img.classList.remove('zoomed');}
function resetZoom(){scale=1;img.style.transform='';img.classList.remove('zoomed');}
</script>
</body></html>"""

VIEWER_VIDEO = """<!DOCTYPE html><html><head><title>{{ filename }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
""" + VIEWER_BASE_CSS + """
<style>
.video-container{width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:#000}
.video-container video{max-width:100%;max-height:100%}
</style></head><body>
<div class="viewer-container">
    <div class="viewer-header">
        <span class="icon">&#127916;</span>
        <span class="filename">{{ filename }}</span>
        <a href="{{ download_url }}" class="btn btn-primary" download><span>&#11015;</span> Download</a>
    </div>
    <div class="viewer-body">
        <div class="video-container">
            <video src="{{ file_url }}" controls autoplay></video>
        </div>
    </div>
</div>
</body></html>"""

VIEWER_AUDIO = """<!DOCTYPE html><html><head><title>{{ filename }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
""" + VIEWER_BASE_CSS + """
<style>
.audio-container{width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;background:linear-gradient(135deg,#1e1b4b 0%,#0f172a 100%)}
.audio-icon{font-size:120px;margin-bottom:30px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.7}}
.audio-container audio{width:80%;max-width:500px}
.audio-name{font-size:18px;margin-bottom:30px;color:#94a3b8}
</style></head><body>
<div class="viewer-container">
    <div class="viewer-header">
        <span class="icon">&#127925;</span>
        <span class="filename">{{ filename }}</span>
        <a href="{{ download_url }}" class="btn btn-primary" download><span>&#11015;</span> Download</a>
    </div>
    <div class="viewer-body">
        <div class="audio-container">
            <div class="audio-icon">&#127925;</div>
            <div class="audio-name">{{ filename }}</div>
            <audio src="{{ file_url }}" controls autoplay></audio>
        </div>
    </div>
</div>
</body></html>"""

VIEWER_TEXT = """<!DOCTYPE html><html><head><title>{{ filename }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
""" + VIEWER_BASE_CSS + """
<style>
.code-container{padding:16px;background:#0d1117;height:100%;overflow:auto}
.code-container pre{margin:0;font-family:'JetBrains Mono',monospace;font-size:13px;line-height:1.5}
.code-container code{background:transparent!important;padding:0!important}
.line-numbers{counter-reset:line}
.line-numbers .line{counter-increment:line}
.line-numbers .line::before{content:counter(line);display:inline-block;width:40px;padding-right:16px;color:#6e7681;text-align:right;user-select:none}
</style></head><body>
<div class="viewer-container">
    <div class="viewer-header">
        <span class="icon">&#128196;</span>
        <span class="filename">{{ filename }}</span>
        <a href="{{ download_url }}" class="btn btn-primary" download><span>&#11015;</span> Download</a>
    </div>
    <div class="viewer-body">
        <div class="code-container">
            <pre><code id="code" class="{{ lang }}">{{ content|e }}</code></pre>
        </div>
    </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script>
hljs.highlightElement(document.getElementById('code'));
// Add line numbers
const code=document.getElementById('code');
const lines=code.innerHTML.split('\\n');
code.innerHTML=lines.map(l=>'<span class="line">'+l+'</span>').join('\\n');
code.parentElement.classList.add('line-numbers');
</script>
</body></html>"""

VIEWER_MARKDOWN = """<!DOCTYPE html><html><head><title>{{ filename }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
""" + VIEWER_BASE_CSS + """
<style>
.md-container{padding:32px;max-width:900px;margin:0 auto}
.md-container h1,.md-container h2,.md-container h3{margin:1.5em 0 0.5em;color:#f1f5f9}
.md-container h1{font-size:2em;border-bottom:1px solid #334155;padding-bottom:0.3em}
.md-container h2{font-size:1.5em;border-bottom:1px solid #334155;padding-bottom:0.3em}
.md-container p{margin:1em 0;line-height:1.7}
.md-container code{background:#1e293b;padding:2px 6px;border-radius:4px;font-family:monospace}
.md-container pre{background:#1e293b;padding:16px;border-radius:8px;overflow-x:auto;margin:1em 0}
.md-container pre code{background:none;padding:0}
.md-container a{color:#818cf8}
.md-container blockquote{border-left:4px solid #6366f1;padding-left:16px;margin:1em 0;color:#94a3b8}
.md-container ul,.md-container ol{margin:1em 0;padding-left:2em}
.md-container li{margin:0.5em 0}
.md-container table{border-collapse:collapse;width:100%;margin:1em 0}
.md-container th,.md-container td{border:1px solid #334155;padding:8px 12px;text-align:left}
.md-container th{background:#1e293b}
.md-container img{max-width:100%;border-radius:8px}
.md-container hr{border:none;border-top:1px solid #334155;margin:2em 0}
</style></head><body>
<div class="viewer-container">
    <div class="viewer-header">
        <span class="icon">&#128221;</span>
        <span class="filename">{{ filename }}</span>
        <button class="btn" onclick="toggleRaw()">&#128196; Raw</button>
        <a href="{{ download_url }}" class="btn btn-primary" download><span>&#11015;</span> Download</a>
    </div>
    <div class="viewer-body">
        <div class="md-container" id="rendered"></div>
        <div class="code-container" id="raw" style="display:none;padding:16px"><pre style="white-space:pre-wrap;font-family:monospace">{{ content|e }}</pre></div>
    </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
document.getElementById('rendered').innerHTML=marked.parse({{ content|tojson }});
let showRaw=false;
function toggleRaw(){
    showRaw=!showRaw;
    document.getElementById('rendered').style.display=showRaw?'none':'block';
    document.getElementById('raw').style.display=showRaw?'block':'none';
}
</script>
</body></html>"""

VIEWER_HTML = """<!DOCTYPE html><html><head><title>{{ filename }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
""" + VIEWER_BASE_CSS + """
<style>
.preview-frame{width:100%;height:100%;border:none;background:#fff}
.code-view{display:none;padding:16px;background:#0d1117;height:100%;overflow:auto}
.code-view pre{margin:0;font-family:monospace;font-size:13px;white-space:pre-wrap}
</style></head><body>
<div class="viewer-container">
    <div class="viewer-header">
        <span class="icon">&#127760;</span>
        <span class="filename">{{ filename }}</span>
        <button class="btn" id="toggleBtn" onclick="toggleView()">&#128196; Source</button>
        <a href="{{ download_url }}" class="btn btn-primary" download><span>&#11015;</span> Download</a>
    </div>
    <div class="viewer-body">
        <iframe id="preview" class="preview-frame" srcdoc="{{ content|e }}"></iframe>
        <div id="source" class="code-view"><pre>{{ content|e }}</pre></div>
    </div>
</div>
<script>
let showSource=false;
function toggleView(){
    showSource=!showSource;
    document.getElementById('preview').style.display=showSource?'none':'block';
    document.getElementById('source').style.display=showSource?'block':'none';
    document.getElementById('toggleBtn').innerHTML=showSource?'&#127760; Preview':'&#128196; Source';
}
</script>
</body></html>"""

VIEWER_PDF = """<!DOCTYPE html><html><head><title>{{ filename }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
""" + VIEWER_BASE_CSS + """
<style>
.pdf-frame{width:100%;height:100%;border:none}
</style></head><body>
<div class="viewer-container">
    <div class="viewer-header">
        <span class="icon">&#128462;</span>
        <span class="filename">{{ filename }}</span>
        <a href="{{ download_url }}" class="btn btn-primary" download><span>&#11015;</span> Download</a>
    </div>
    <div class="viewer-body">
        <iframe class="pdf-frame" src="{{ file_url }}"></iframe>
    </div>
</div>
</body></html>"""

VIEWER_OFFICE = """<!DOCTYPE html><html><head><title>{{ filename }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
""" + VIEWER_BASE_CSS + """
<style>
#onlyoffice-container{width:100%;height:100%}
.loading-office{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;text-align:center}
.loading-office .spinner{width:50px;height:50px;border:4px solid #334155;border-top-color:#6366f1;border-radius:50%;animation:spin 1s linear infinite;margin-bottom:20px}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
<script src="{{ onlyoffice_url }}/web-apps/apps/api/documents/api.js"></script>
</head><body>
<div class="viewer-container">
    <div class="viewer-header">
        <span class="icon">{{ icon|safe }}</span>
        <span class="filename">{{ filename }}</span>
        <a href="{{ download_url }}" class="btn btn-primary" download><span>&#11015;</span> Download</a>
    </div>
    <div class="viewer-body">
        <div id="onlyoffice-container">
            <div class="loading-office"><div class="spinner"></div><p>Loading document...</p></div>
        </div>
    </div>
</div>
<script>
var config = {{ config_json|safe }};
config.events = {
    onAppReady: function() { console.log('OnlyOffice App ready'); },
    onDocumentReady: function() {
        console.log('Document ready');
        document.querySelector('.loading-office').style.display = 'none';
    },
    onError: function(e) {
        console.error('OnlyOffice error:', e);
        var errMsg = e.data ? (typeof e.data === 'string' ? e.data : JSON.stringify(e.data)) : 'Unknown error';
        var errCode = e.data && e.data.errorCode ? ' (code: ' + e.data.errorCode + ')' : '';
        document.querySelector('.loading-office').innerHTML = '<div style="color:#ef4444;font-size:16px">Error: ' + errMsg + errCode + '</div><p style="margin-top:20px"><a href="{{ download_url }}" class="btn btn-primary" download>Download instead</a></p>';
    },
    onWarning: function(e) { console.warn('OnlyOffice warning:', e); },
    onDownloadAs: function(e) { console.log('Download:', e); }
};
console.log('OnlyOffice config:', config);
try {
    new DocsAPI.DocEditor("onlyoffice-container", config);
} catch(err) {
    console.error('DocsAPI error:', err);
    document.querySelector('.loading-office').innerHTML = '<div style="color:#ef4444">Failed to load editor: ' + err.message + '</div>';
}
</script>
</body></html>"""

VIEWER_UNSUPPORTED = """<!DOCTYPE html><html><head><title>{{ filename }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
""" + VIEWER_BASE_CSS + """
<style>
.unsupported{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;text-align:center;padding:40px}
.unsupported .icon{font-size:80px;margin-bottom:20px;opacity:0.5}
.unsupported h2{margin-bottom:16px;color:#f1f5f9}
.unsupported p{color:#94a3b8;margin-bottom:24px}
.unsupported .btn{padding:12px 24px;font-size:15px}
</style></head><body>
<div class="viewer-container">
    <div class="viewer-header">
        <span class="icon">&#128196;</span>
        <span class="filename">{{ filename }}</span>
        <a href="{{ download_url }}" class="btn btn-primary" download><span>&#11015;</span> Download</a>
    </div>
    <div class="viewer-body">
        <div class="unsupported">
            <div class="icon">&#128196;</div>
            <h2>{{ filename }}</h2>
            <p>Preview not available for this file type. Click Download to save the file.</p>
            <a href="{{ download_url }}" class="btn btn-primary" download><span>&#11015;</span> Download File</a>
        </div>
    </div>
</div>
</body></html>"""


# ===========================================
# Routes
# ===========================================

@app.route('/', methods=['GET', 'POST'])
def login():
    if session.get('user'):
        return redirect('/dashboard')
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if check_user_auth(username, password):
            session['user'] = username
            session['is_admin'] = (username == ADMIN_USER)
            return redirect('/dashboard')
        return render_template_string(LOGIN_PAGE, error="Invalid credentials")
    return render_template_string(LOGIN_PAGE)

@app.route('/dashboard')
def dashboard():
    if not session.get('user'):
        return redirect('/')
    try:
        db = get_db()
        shared_cfg = get_shared_s3_config(db)
        has_shared = shared_cfg is not None
    except Exception:
        has_shared = False
    if session.get('is_admin'):
        return render_template_string(ADMIN_DASH, users=get_users(), message=request.args.get('msg'), success=request.args.get('s')=='1', new_password=request.args.get('pwd'), has_shared=has_shared)
    username = session['user']
    try:
        db = get_db()
        s3_available = has_s3_config(db, username)
    except Exception:
        s3_available = False
    return render_template_string(USER_MENU, username=username, has_s3=s3_available, has_shared=has_shared)

@app.route('/lab')
def lab():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    username = session['user']
    port = start_jupyter(username)
    return render_template_string(USER_LAB, username=username, port=port)


# ===========================================
# Embed Routes (for desktop UI iframes)
# ===========================================

@app.route('/embed/lab')
def embed_lab():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    username = session['user']
    start_jupyter(username)
    return render_template_string(EMBED_LAB, username=username)

@app.route('/embed/s3-backup')
def embed_s3_backup():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    return render_template_string(EMBED_S3_BACKUP)

@app.route('/embed/shared-space')
def embed_shared_space():
    if not session.get('user'):
        return redirect('/')
    return render_template_string(EMBED_SHARED_SPACE)

@app.route('/embed/my-shares')
def embed_my_shares():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    return render_template_string(EMBED_MY_SHARES)

@app.route('/embed/s3-config', methods=['GET', 'POST'])
def embed_s3_config():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    username = session['user']
    db = get_db()
    message = None
    success = False
    if request.method == 'POST':
        cfg = {
            'username': username,
            'endpoint_url': request.form.get('endpoint_url', '').strip(),
            'access_key': request.form.get('access_key', '').strip(),
            'secret_key': request.form.get('secret_key', '').strip(),
            'region': request.form.get('region', '').strip(),
            'bucket_name': request.form.get('bucket_name', '').strip(),
            'prefix': request.form.get('prefix', '').strip(),
            'created_at': datetime.utcnow(),
        }
        db.s3_user_config.replace_one({'username': username}, cfg, upsert=True)
        message = "Saved!"
        success = True
    user_cfg = db.s3_user_config.find_one({'username': username}) or {}
    sys_cfg = db.s3_system_config.find_one({'_id': 'default'})
    return render_template_string(EMBED_S3_CONFIG, config=user_cfg, system_s3=bool(sys_cfg and sys_cfg.get('endpoint_url')), has_personal=bool(user_cfg.get('endpoint_url')), message=message, success=success)

@app.route('/embed/change-password', methods=['GET', 'POST'])
def embed_change_password():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    username = session['user']
    error = success = None
    if request.method == 'POST':
        old_pass = request.form.get('old_password', '')
        new_pass = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if new_pass != confirm:
            error = "Passwords don't match"
        elif len(new_pass) < 6:
            error = "Min 6 characters"
        elif not check_user_auth(username, old_pass):
            error = "Current password incorrect"
        elif set_user_password(username, new_pass):
            success = "Password changed!"
        else:
            error = "Failed"
    return render_template_string(EMBED_CHANGE_PW, error=error, success=success)


@app.route('/user/change-password', methods=['GET', 'POST'])
def user_change_password():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    username = session['user']
    error = success = None
    if request.method == 'POST':
        old_pass = request.form.get('old_password', '')
        new_pass = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if new_pass != confirm: error = "Passwords don't match"
        elif len(new_pass) < 6: error = "Min 6 characters"
        elif not check_user_auth(username, old_pass): error = "Current password is incorrect"
        elif set_user_password(username, new_pass): success = "Password changed successfully!"
        else: error = "Failed to change password"
    return render_template_string(USER_CHANGE_PW, username=username, error=error, success=success)

@app.route('/admin/create', methods=['POST'])
def admin_create():
    if not session.get('is_admin'): return redirect('/')
    username = request.form.get('username', '').strip().lower()
    if not username or not username.replace('_','').isalnum():
        return redirect('/dashboard?msg=Invalid username&s=0')
    if user_exists(username):
        return redirect('/dashboard?msg=User exists&s=0')
    password = generate_password(12)
    if create_system_user(username) and set_user_password(username, password):
        return redirect(f'/dashboard?msg=Created {username}&s=1&pwd={password}')
    return redirect('/dashboard?msg=Failed&s=0')

@app.route('/admin/reset', methods=['POST'])
def admin_reset():
    if not session.get('is_admin'): return redirect('/')
    username = request.form.get('username', '')
    password = generate_password(12)
    if set_user_password(username, password):
        return redirect(f'/dashboard?msg=Password reset for {username}&s=1&pwd={password}')
    return redirect('/dashboard?msg=Failed&s=0')

@app.route('/admin/delete', methods=['POST'])
def admin_delete():
    if not session.get('is_admin'): return redirect('/')
    username = request.form.get('username', '')
    if username and username != ADMIN_USER:
        delete_system_user(username)
        return redirect(f'/dashboard?msg=Deleted {username}&s=1')
    return redirect('/dashboard?msg=Cannot delete&s=0')

@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    error = success = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        old_pass = request.form.get('old_password', '')
        new_pass = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if new_pass != confirm: error = "Passwords don't match"
        elif len(new_pass) < 6: error = "Min 6 characters"
        elif not check_user_auth(username, old_pass): error = "Invalid credentials"
        elif set_user_password(username, new_pass): success = "Password changed!"
        else: error = "Failed"
    return render_template_string(CHANGE_PW, error=error, success=success)

@app.route('/logout')
def logout():
    if session.get('user') and not session.get('is_admin'):
        stop_jupyter(session['user'])
    session.clear()
    return redirect('/')


# ===========================================
# Extension Manager Routes (Admin)
# ===========================================

@app.route('/admin/extensions')
def admin_extensions():
    if not session.get('is_admin'): return redirect('/')
    msg = request.args.get('msg')
    s = request.args.get('s') == '1'
    exts = list_extensions()
    popular = get_popular_extensions()
    return render_template_string(ADMIN_EXTENSIONS, extensions=exts, popular=popular, message=msg, success=s)

@app.route('/admin/extensions/search')
def admin_ext_search():
    if not session.get('is_admin'): return jsonify({'results': []})
    q = request.args.get('q', '')
    results = search_catalog(q)
    installed = get_installed_packages()
    enriched = [{**ext, 'installed': ext['package'].lower() in installed} for ext in results]
    return jsonify({'results': enriched})

@app.route('/admin/extensions/install', methods=['POST'])
def admin_ext_install():
    if not session.get('is_admin'): return redirect('/')
    package = request.form.get('package', '').strip()
    if not package:
        return redirect('/admin/extensions?msg=No package specified&s=0')
    ok, msg = install_extension(package)
    return redirect(f'/admin/extensions?msg={msg}&s={"1" if ok else "0"}')

@app.route('/admin/extensions/uninstall', methods=['POST'])
def admin_ext_uninstall():
    if not session.get('is_admin'): return redirect('/')
    package = request.form.get('package', '').strip()
    if not package:
        return redirect('/admin/extensions?msg=No package specified&s=0')
    ok, msg = uninstall_extension(package)
    return redirect(f'/admin/extensions?msg={msg}&s={"1" if ok else "0"}')

@app.route('/admin/extensions/restart', methods=['POST'])
def admin_ext_restart():
    if not session.get('is_admin'): return redirect('/')
    restarted = restart_all_jupyterlab()
    msg = f"Restarted {len(restarted)} instance(s): {', '.join(restarted)}" if restarted else "No running instances"
    return redirect(f'/admin/extensions?msg={msg}&s=1')


# ===========================================
# Admin S3 Config Routes
# ===========================================

@app.route('/admin/s3-config', methods=['GET', 'POST'])
def admin_s3_config():
    if not session.get('is_admin'): return redirect('/')
    db = get_db()
    message = None
    success = False

    if request.method == 'POST':
        cfg = {
            '_id': 'default',
            'endpoint_url': request.form.get('endpoint_url', '').strip(),
            'access_key': request.form.get('access_key', '').strip(),
            'secret_key': request.form.get('secret_key', '').strip(),
            'region': request.form.get('region', '').strip(),
            'bucket_name': request.form.get('bucket_name', '').strip(),
            'prefix': request.form.get('prefix', '').strip(),
            'updated_at': datetime.utcnow(),
        }
        db.s3_system_config.replace_one({'_id': 'default'}, cfg, upsert=True)
        message = "S3 configuration saved"
        success = True

    config = db.s3_system_config.find_one({'_id': 'default'}) or {}
    return render_template_string(ADMIN_S3_CONFIG, config=config, message=message, success=success)

@app.route('/admin/s3-config/test', methods=['POST'])
def admin_s3_test():
    if not session.get('is_admin'): return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    cfg = {
        'endpoint_url': request.form.get('endpoint_url', '').strip(),
        'access_key': request.form.get('access_key', '').strip(),
        'secret_key': request.form.get('secret_key', '').strip(),
        'region': request.form.get('region', '').strip(),
        'bucket_name': request.form.get('bucket_name', '').strip(),
    }
    ok, msg = test_s3_connection(cfg)
    return jsonify({'success': ok, 'message': msg})


# ===========================================
# User S3 Config Routes
# ===========================================

@app.route('/user/s3-config', methods=['GET', 'POST'])
def user_s3_config():
    if not session.get('user') or session.get('is_admin'): return redirect('/')
    username = session['user']
    db = get_db()
    message = None
    success = False

    if request.method == 'POST':
        cfg = {
            'username': username,
            'endpoint_url': request.form.get('endpoint_url', '').strip(),
            'access_key': request.form.get('access_key', '').strip(),
            'secret_key': request.form.get('secret_key', '').strip(),
            'region': request.form.get('region', '').strip(),
            'bucket_name': request.form.get('bucket_name', '').strip(),
            'prefix': request.form.get('prefix', '').strip(),
            'created_at': datetime.utcnow(),
        }
        db.s3_user_config.replace_one({'username': username}, cfg, upsert=True)
        message = "Personal S3 configuration saved"
        success = True

    user_cfg = db.s3_user_config.find_one({'username': username}) or {}
    sys_cfg = db.s3_system_config.find_one({'_id': 'default'})
    has_personal = bool(user_cfg.get('endpoint_url'))
    return render_template_string(USER_S3_CONFIG, username=username, config=user_cfg, system_s3=bool(sys_cfg and sys_cfg.get('endpoint_url')), has_personal=has_personal, message=message, success=success)

@app.route('/user/s3-config/delete', methods=['POST'])
def user_s3_config_delete():
    if not session.get('user') or session.get('is_admin'): return redirect('/')
    username = session['user']
    db = get_db()
    db.s3_user_config.delete_one({'username': username})
    return redirect('/user/s3-config')

@app.route('/user/s3-config/test', methods=['POST'])
def user_s3_test():
    if not session.get('user'): return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    cfg = {
        'endpoint_url': request.form.get('endpoint_url', '').strip(),
        'access_key': request.form.get('access_key', '').strip(),
        'secret_key': request.form.get('secret_key', '').strip(),
        'region': request.form.get('region', '').strip(),
        'bucket_name': request.form.get('bucket_name', '').strip(),
    }
    ok, msg = test_s3_connection(cfg)
    return jsonify({'success': ok, 'message': msg})


# ===========================================
# S3 Backup File Browser
# ===========================================

@app.route('/s3-backup')
def s3_backup():
    if not session.get('user') or session.get('is_admin'): return redirect('/')
    username = session['user']
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception:
        cfg = None
    if not cfg:
        return redirect('/user/s3-config')
    return render_template_string(S3_BACKUP_PAGE, username=username, s3_source=cfg.get('source', 'system'))


# ===========================================
# API Endpoints
# ===========================================

@app.route('/api/workspace/list')
def api_ws_list():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    path = request.args.get('path', '')
    items = list_workspace(username, path)
    if items is None:
        return jsonify({'error': 'Invalid path'})
    return jsonify({'items': items})

@app.route('/api/s3/list')
def api_s3_list():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'No S3 configured'})
    path = request.args.get('path', '')
    try:
        items = list_s3(cfg, path)
        return jsonify({'items': items})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/transfer', methods=['POST'])
def api_transfer():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'})
    source = data.get('source')
    dest = data.get('dest')
    items = data.get('items', [])
    source_path = data.get('source_path', '')
    dest_path = data.get('dest_path', '')
    if source not in ('workspace', 's3') or dest not in ('workspace', 's3'):
        return jsonify({'error': 'Invalid source/dest'})
    if not items:
        return jsonify({'error': 'No items selected'})
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'No S3 configured'})
    task_id = start_transfer(username, cfg, source, dest, items, source_path, dest_path)
    return jsonify({'task_id': task_id})

@app.route('/api/transfer/status/<task_id>')
def api_transfer_status(task_id):
    if not session.get('user'): return jsonify({'error': 'Unauthorized'}), 403
    status = get_transfer_status(task_id)
    if not status:
        return jsonify({'error': 'Task not found'})
    return jsonify(status)

@app.route('/api/workspace/mkdir', methods=['POST'])
def api_ws_mkdir():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    path = data.get('path', '') if data else ''
    ok, msg = mkdir_workspace(username, path)
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/s3/mkdir', methods=['POST'])
def api_s3_mkdir():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    path = data.get('path', '') if data else ''
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'No S3 configured'})
    try:
        mkdir_s3(cfg, path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/workspace/delete', methods=['POST'])
def api_ws_delete():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    items = data.get('items', []) if data else []
    path = data.get('path', '') if data else ''
    deleted = delete_workspace(username, items, path)
    return jsonify({'deleted': deleted})

@app.route('/api/s3/delete', methods=['POST'])
def api_s3_delete():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    items = data.get('items', []) if data else []
    path = data.get('path', '') if data else ''
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'No S3 configured'})
    try:
        deleted = delete_s3(cfg, items, path)
        return jsonify({'deleted': deleted})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/workspace/upload', methods=['POST'])
def api_ws_upload():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'})
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'})
    path = request.form.get('path', '')
    ok, result = upload_to_workspace(username, path, f.filename, f)
    if ok:
        return jsonify({'success': True, 'filename': result})
    return jsonify({'error': result})

@app.route('/api/s3/upload', methods=['POST'])
def api_s3_upload():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'})
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'})
    path = request.form.get('path', '')
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'No S3 configured'})
    # Read file data
    file_data = f.read()
    ok, result = upload_to_s3(cfg, path, f.filename, file_data)
    if ok:
        return jsonify({'success': True, 'filename': result})
    return jsonify({'error': result})


# ===========================================
# Shared Space Routes
# ===========================================

@app.route('/shared-space')
def shared_space():
    if not session.get('user'): return redirect('/')
    username = session['user']
    try:
        db = get_db()
        cfg = get_shared_s3_config(db)
    except Exception:
        cfg = None
    if not cfg:
        return render_template_string(SHARED_SPACE_NO_CONFIG, username=username)
    return render_template_string(SHARED_SPACE_PAGE, username=username)

@app.route('/api/shared/list')
def api_shared_list():
    if not session.get('user'): return jsonify({'error': 'Unauthorized'}), 403
    try:
        db = get_db()
        cfg = get_shared_s3_config(db)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'Shared space not configured'})
    path = request.args.get('path', '')
    try:
        items = list_s3(cfg, path)
        return jsonify({'items': items})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/shared/mkdir', methods=['POST'])
def api_shared_mkdir():
    if not session.get('user'): return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    path = data.get('path', '') if data else ''
    try:
        db = get_db()
        cfg = get_shared_s3_config(db)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'Shared space not configured'})
    try:
        mkdir_s3(cfg, path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/shared/delete', methods=['POST'])
def api_shared_delete():
    if not session.get('user'): return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    items = data.get('items', []) if data else []
    path = data.get('path', '') if data else ''
    try:
        db = get_db()
        cfg = get_shared_s3_config(db)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'Shared space not configured'})
    try:
        deleted = delete_s3(cfg, items, path)
        return jsonify({'deleted': deleted})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/shared/transfer', methods=['POST'])
def api_shared_transfer():
    if not session.get('user'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'})
    source = data.get('source')
    dest = data.get('dest')
    items = data.get('items', [])
    source_path = data.get('source_path', '')
    dest_path = data.get('dest_path', '')
    if source not in ('workspace', 's3') or dest not in ('workspace', 's3'):
        return jsonify({'error': 'Invalid source/dest'})
    if not items:
        return jsonify({'error': 'No items selected'})
    try:
        db = get_db()
        cfg = get_shared_s3_config(db)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'Shared space not configured'})
    task_id = start_transfer(username, cfg, source, dest, items, source_path, dest_path)
    return jsonify({'task_id': task_id})

@app.route('/api/shared/upload', methods=['POST'])
def api_shared_upload():
    if not session.get('user'): return jsonify({'error': 'Unauthorized'}), 403
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'})
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'})
    path = request.form.get('path', '')
    try:
        db = get_db()
        cfg = get_shared_s3_config(db)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'Shared space not configured'})
    file_data = f.read()
    ok, result = upload_to_s3(cfg, path, f.filename, file_data)
    if ok:
        return jsonify({'success': True, 'filename': result})
    return jsonify({'error': result})


# ===========================================
# Share Link Routes (Public - no login required)
# ===========================================

def _init_shared_links_collection(db):
    """Ensure indexes on shared_links collection"""
    col = db.shared_links
    col.create_index('created_by')
    col.create_index('expires_at', expireAfterSeconds=0)
    return col

def _format_size(b):
    """Format bytes to human-readable string"""
    if b < 1024: return f"{b} B"
    if b < 1048576: return f"{b/1024:.1f} KB"
    if b < 1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.2f} GB"

@app.route('/share/<share_id>', methods=['GET', 'POST'])
def share_public(share_id):
    try:
        db = get_db()
    except Exception:
        return render_template_string(SHARE_NOT_FOUND), 404

    doc = db.shared_links.find_one({'_id': share_id, 'is_active': True})
    if not doc:
        return render_template_string(SHARE_NOT_FOUND), 404

    # Check expiry
    if doc.get('expires_at') and doc['expires_at'] < datetime.utcnow():
        return render_template_string(SHARE_EXPIRED), 410

    # Check password
    if doc.get('password_hash'):
        auth_key = f"share_auth_{share_id}"
        if not session.get(auth_key):
            if request.method == 'POST':
                password = request.form.get('password', '')
                if check_password_hash(doc['password_hash'], password):
                    session[auth_key] = True
                else:
                    return render_template_string(SHARE_PASSWORD_PAGE, error="Incorrect password")
            else:
                return render_template_string(SHARE_PASSWORD_PAGE, error=None)

    expires_str = doc['expires_at'].strftime('%Y-%m-%d %H:%M UTC') if doc.get('expires_at') else None

    if doc['item_type'] == 'file':
        # Determine preview type and icon
        ftype, ext = get_file_type(doc['item_name'])
        icon_map = {'image': '&#128444;', 'video': '&#127916;', 'audio': '&#127925;', 'text': '&#128196;', 'markdown': '&#128221;', 'html': '&#127760;', 'pdf': '&#128462;', 'office': '&#128462;'}
        icon = icon_map.get(ftype, '&#128196;')
        content = None
        if ftype in ['text', 'markdown', 'html']:
            try:
                config_snapshot = doc['s3_config_snapshot']
                content = read_s3_text(config_snapshot, doc['s3_key'])
            except:
                content = None
        return render_template_string(SHARE_FILE_PAGE,
            item_name=doc['item_name'],
            created_by=doc['created_by'],
            share_id=share_id,
            download_count=doc.get('download_count', 0),
            expires_at=expires_str,
            preview_type=ftype,
            icon=icon,
            content=content,
        )
    else:
        # Folder: list files
        config_snapshot = doc['s3_config_snapshot']
        icon_map = {'jpg': '&#128444;', 'jpeg': '&#128444;', 'png': '&#128444;', 'gif': '&#128444;', 'webp': '&#128444;', 'svg': '&#128444;', 'bmp': '&#128444;', 'mp4': '&#127916;', 'webm': '&#127916;', 'mov': '&#127916;', 'avi': '&#127916;', 'mkv': '&#127916;', 'mp3': '&#127925;', 'wav': '&#127925;', 'flac': '&#127925;', 'm4a': '&#127925;', 'pdf': '&#128462;', 'doc': '&#128462;', 'docx': '&#128462;', 'xls': '&#128202;', 'xlsx': '&#128202;', 'ppt': '&#128253;', 'pptx': '&#128253;', 'txt': '&#128196;', 'md': '&#128221;', 'html': '&#127760;', 'htm': '&#127760;', 'zip': '&#128230;', 'rar': '&#128230;', '7z': '&#128230;'}
        try:
            files = list_s3_recursive(config_snapshot, doc['s3_key'])
            for f in files:
                f['size_fmt'] = _format_size(f['size'])
                ext = f['name'].rsplit('.', 1)[-1].lower() if '.' in f['name'] else ''
                f['icon'] = icon_map.get(ext, '&#128196;')
        except Exception:
            files = []
        return render_template_string(SHARE_FOLDER_PAGE,
            item_name=doc['item_name'],
            created_by=doc['created_by'],
            share_id=share_id,
            files=files,
            download_count=doc.get('download_count', 0),
            expires_at=expires_str,
        )

@app.route('/share/<share_id>/download')
def share_download(share_id):
    try:
        db = get_db()
    except Exception:
        return render_template_string(SHARE_NOT_FOUND), 404

    doc = db.shared_links.find_one({'_id': share_id, 'is_active': True})
    if not doc:
        return render_template_string(SHARE_NOT_FOUND), 404
    if doc.get('expires_at') and doc['expires_at'] < datetime.utcnow():
        return render_template_string(SHARE_EXPIRED), 410
    # Check password auth
    if doc.get('password_hash') and not session.get(f"share_auth_{share_id}"):
        return redirect(f'/share/{share_id}')

    config_snapshot = doc['s3_config_snapshot']

    if doc['item_type'] == 'file':
        # Stream single file
        try:
            gen, length, ctype = stream_s3_object(config_snapshot, doc['s3_key'])
        except Exception as e:
            return str(e), 500
        db.shared_links.update_one({'_id': share_id}, {'$inc': {'download_count': 1}})
        return Response(gen, headers={
            'Content-Type': ctype,
            'Content-Length': str(length),
            'Content-Disposition': f'attachment; filename="{doc["item_name"]}"',
        })
    else:
        # Folder: download individual file if ?file= param
        file_rel = request.args.get('file', '')
        if not file_rel:
            return redirect(f'/share/{share_id}')
        # Validate path
        s3_prefix = doc['s3_key'].rstrip('/') + '/'
        s3_key = s3_prefix + file_rel
        if not s3_key.startswith(s3_prefix):
            return "Invalid path", 400
        try:
            gen, length, ctype = stream_s3_object(config_snapshot, s3_key)
        except Exception as e:
            return str(e), 500
        db.shared_links.update_one({'_id': share_id}, {'$inc': {'download_count': 1}})
        filename = file_rel.split('/')[-1]
        return Response(gen, headers={
            'Content-Type': ctype,
            'Content-Length': str(length),
            'Content-Disposition': f'attachment; filename="{filename}"',
        })

@app.route('/share/<share_id>/download/zip')
def share_download_zip(share_id):
    try:
        db = get_db()
    except Exception:
        return render_template_string(SHARE_NOT_FOUND), 404

    doc = db.shared_links.find_one({'_id': share_id, 'is_active': True})
    if not doc:
        return render_template_string(SHARE_NOT_FOUND), 404
    if doc.get('expires_at') and doc['expires_at'] < datetime.utcnow():
        return render_template_string(SHARE_EXPIRED), 410
    if doc.get('password_hash') and not session.get(f"share_auth_{share_id}"):
        return redirect(f'/share/{share_id}')
    if doc['item_type'] != 'dir':
        return redirect(f'/share/{share_id}/download')

    config_snapshot = doc['s3_config_snapshot']
    try:
        gen, zip_size = stream_s3_folder_as_zip(config_snapshot, doc['s3_key'])
    except ValueError as e:
        return str(e), 413
    except Exception as e:
        return str(e), 500

    db.shared_links.update_one({'_id': share_id}, {'$inc': {'download_count': 1}})
    zip_name = doc['item_name'] + '.zip'
    return Response(gen, headers={
        'Content-Type': 'application/zip',
        'Content-Length': str(zip_size),
        'Content-Disposition': f'attachment; filename="{zip_name}"',
    })


# ===========================================
# Share Link Routes (Authenticated)
# ===========================================

@app.route('/api/share/create', methods=['POST'])
def api_share_create():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'})

    name = data.get('name', '').strip()
    item_type = data.get('type', 'file')
    s3_path = data.get('s3_path', '')
    password = data.get('password', '')
    expires_hours = data.get('expires_hours', 0)

    if not name:
        return jsonify({'error': 'No item name'})
    if item_type not in ('file', 'dir'):
        return jsonify({'error': 'Invalid type'})

    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'No S3 configured'})

    # Build full S3 key
    base_prefix = cfg.get('prefix', '').strip('/')
    if base_prefix:
        s3_key = f"{base_prefix}/{s3_path}/{name}" if s3_path else f"{base_prefix}/{name}"
    else:
        s3_key = f"{s3_path}/{name}" if s3_path else name
    s3_key = s3_key.lstrip('/')

    # Build config snapshot (frozen credentials)
    config_snapshot = {
        'endpoint_url': cfg['endpoint_url'],
        'access_key': cfg['access_key'],
        'secret_key': cfg['secret_key'],
        'region': cfg.get('region', ''),
        'bucket_name': cfg['bucket_name'],
    }

    share_id = secrets.token_urlsafe(6)
    doc = {
        '_id': share_id,
        'created_by': username,
        's3_key': s3_key,
        's3_bucket': cfg['bucket_name'],
        's3_config_snapshot': config_snapshot,
        'item_name': name,
        'item_type': item_type,
        'password_hash': generate_password_hash(password) if password else None,
        'expires_at': datetime.utcnow() + timedelta(hours=expires_hours) if expires_hours > 0 else None,
        'created_at': datetime.utcnow(),
        'download_count': 0,
        'is_active': True,
    }

    col = _init_shared_links_collection(db)
    col.insert_one(doc)

    return jsonify({'share_id': share_id})

@app.route('/api/share/list')
def api_share_list():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    try:
        db = get_db()
        shares = list(db.shared_links.find({'created_by': username, 'is_active': True}).sort('created_at', -1))
        result = []
        for s in shares:
            result.append({
                '_id': s['_id'],
                'item_name': s['item_name'],
                'item_type': s['item_type'],
                'has_password': bool(s.get('password_hash')),
                'expires_at': s['expires_at'].isoformat() if s.get('expires_at') else None,
                'download_count': s.get('download_count', 0),
                'created_at': s['created_at'].isoformat(),
            })
        return jsonify({'shares': result})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/share/delete', methods=['POST'])
def api_share_delete():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    share_id = data.get('share_id', '') if data else ''
    if not share_id:
        return jsonify({'error': 'No share_id'})
    try:
        db = get_db()
        result = db.shared_links.update_one(
            {'_id': share_id, 'created_by': username},
            {'$set': {'is_active': False}}
        )
        if result.modified_count:
            return jsonify({'success': True})
        return jsonify({'error': 'Not found'})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/my-shares')
def my_shares():
    if not session.get('user') or session.get('is_admin'): return redirect('/')
    username = session['user']
    try:
        db = get_db()
        shares = list(db.shared_links.find({'created_by': username, 'is_active': True}).sort('created_at', -1))
        for s in shares:
            s['has_password'] = bool(s.get('password_hash'))
    except Exception:
        shares = []
    return render_template_string(MY_SHARES_PAGE, username=username, shares=shares, message=request.args.get('msg'), success=request.args.get('s')=='1')


# ===========================================
# File Viewer Routes
# ===========================================

FILE_TYPES = {
    'image': ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'ico'],
    'video': ['mp4', 'webm', 'ogg', 'mov', 'avi', 'mkv'],
    'audio': ['mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac', 'wma'],
    'text': ['txt', 'log', 'json', 'xml', 'yaml', 'yml', 'ini', 'cfg', 'conf', 'sh', 'bat', 'ps1',
             'py', 'js', 'ts', 'jsx', 'tsx', 'css', 'scss', 'less', 'sql', 'r', 'java', 'c', 'cpp',
             'h', 'hpp', 'cs', 'go', 'rs', 'php', 'rb', 'pl', 'swift', 'kt', 'scala', 'lua',
             'dockerfile', 'makefile', 'gitignore', 'env', 'toml', 'csv'],
    'markdown': ['md', 'markdown'],
    'html': ['html', 'htm'],
    'pdf': ['pdf'],
    'office': ['doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp'],
}

LANG_MAP = {
    'py': 'python', 'js': 'javascript', 'ts': 'typescript', 'jsx': 'javascript',
    'tsx': 'typescript', 'sh': 'bash', 'bat': 'batch', 'ps1': 'powershell',
    'yml': 'yaml', 'md': 'markdown', 'dockerfile': 'dockerfile', 'rs': 'rust',
    'rb': 'ruby', 'pl': 'perl', 'kt': 'kotlin', 'cs': 'csharp', 'cpp': 'cpp',
}

OFFICE_ICONS = {
    'doc': '&#128462;', 'docx': '&#128462;', 'odt': '&#128462;',
    'xls': '&#128202;', 'xlsx': '&#128202;', 'ods': '&#128202;',
    'ppt': '&#128253;', 'pptx': '&#128253;', 'odp': '&#128253;',
}

def get_file_type(filename):
    """Determine file type from extension"""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    for ftype, exts in FILE_TYPES.items():
        if ext in exts:
            return ftype, ext
    return 'unknown', ext


def generate_onlyoffice_token(source, path, username):
    """Generate a signed token for OnlyOffice file access"""
    payload = {
        'source': source,
        'path': path,
        'username': username,
        'exp': int(time.time()) + 14400  # 4 hour expiry
    }
    return jwt.encode(payload, ONLYOFFICE_JWT_SECRET, algorithm='HS256')


def verify_onlyoffice_token(token):
    """Verify OnlyOffice file access token"""
    try:
        payload = jwt.decode(token, ONLYOFFICE_JWT_SECRET, algorithms=['HS256'])
        if payload.get('exp', 0) < time.time():
            return None
        return payload
    except:
        return None


@app.route('/api/onlyoffice/file', methods=['GET', 'HEAD', 'OPTIONS'])
def onlyoffice_file_stream():
    """Stream file for OnlyOffice (token-based auth)"""
    # Handle OPTIONS preflight
    if request.method == 'OPTIONS':
        return Response('', headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization, Range',
            'Access-Control-Max-Age': '86400',
        })

    # Log request headers for debugging
    app.logger.info(f"OnlyOffice file request: {request.method} - headers: {dict(request.headers)}")

    token = request.args.get('token', '')
    payload = verify_onlyoffice_token(token)
    if not payload:
        return 'Invalid or expired token', 401

    source = payload['source']
    path = payload['path']
    username = payload['username']

    try:
        db = get_db()
        if source == 'workspace':
            result = stream_workspace_file(username, path)
            if not result:
                return 'File not found', 404
            gen, length, ctype, fname = result
        elif source == 's3':
            cfg = get_s3_config(db, username)
            if not cfg:
                return 'S3 not configured', 400
            prefix = cfg.get('prefix', '').strip('/')
            s3_key = f"{prefix}/{path}" if prefix else path
            gen, length, ctype = stream_s3_object(cfg, s3_key)
            fname = path.rsplit('/', 1)[-1] if '/' in path else path
        elif source == 'shared':
            cfg = get_shared_s3_config(db)
            if not cfg:
                return 'Shared space not configured', 400
            prefix = cfg.get('prefix', '').strip('/')
            s3_key = f"{prefix}/{path}" if prefix else path
            gen, length, ctype = stream_s3_object(cfg, s3_key)
            fname = path.rsplit('/', 1)[-1] if '/' in path else path
        else:
            return 'Invalid source', 400

        headers = {
            'Content-Type': ctype,
            'Content-Length': length,
            'Content-Disposition': f'inline; filename="{fname}"',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        }
        return Response(gen, headers=headers)
    except Exception as e:
        app.logger.error(f"OnlyOffice file error: {e}")
        return str(e), 500


@app.route('/api/workspace/file')
def workspace_file_stream():
    """Stream file from workspace"""
    if not session.get('user') or session.get('is_admin'):
        return 'Unauthorized', 401
    username = session['user']
    path = request.args.get('path', '')
    result = stream_workspace_file(username, path)
    if not result:
        return 'File not found', 404
    gen, length, ctype, fname = result
    headers = {
        'Content-Type': ctype,
        'Content-Length': length,
        'Content-Disposition': f'inline; filename="{fname}"',
    }
    return Response(gen, headers=headers)


@app.route('/api/workspace/download')
def workspace_file_download():
    """Download file from workspace"""
    if not session.get('user') or session.get('is_admin'):
        return 'Unauthorized', 401
    username = session['user']
    path = request.args.get('path', '')
    result = stream_workspace_file(username, path)
    if not result:
        return 'File not found', 404
    gen, length, ctype, fname = result
    headers = {
        'Content-Type': 'application/octet-stream',
        'Content-Length': length,
        'Content-Disposition': f'attachment; filename="{fname}"',
    }
    return Response(gen, headers=headers)


@app.route('/api/s3/file')
def s3_file_stream():
    """Stream file from user's S3"""
    if not session.get('user') or session.get('is_admin'):
        return 'Unauthorized', 401
    username = session['user']
    path = request.args.get('path', '')
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
        if not cfg:
            return 'S3 not configured', 400
        prefix = cfg.get('prefix', '').strip('/')
        s3_key = f"{prefix}/{path}" if prefix else path
        gen, length, ctype = stream_s3_object(cfg, s3_key)
        fname = path.rsplit('/', 1)[-1] if '/' in path else path
        headers = {
            'Content-Type': ctype,
            'Content-Length': length,
            'Content-Disposition': f'inline; filename="{fname}"',
        }
        return Response(gen, headers=headers)
    except Exception as e:
        return str(e), 500


@app.route('/api/s3/download')
def s3_file_download():
    """Download file from user's S3"""
    if not session.get('user') or session.get('is_admin'):
        return 'Unauthorized', 401
    username = session['user']
    path = request.args.get('path', '')
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
        if not cfg:
            return 'S3 not configured', 400
        prefix = cfg.get('prefix', '').strip('/')
        s3_key = f"{prefix}/{path}" if prefix else path
        gen, length, ctype = stream_s3_object(cfg, s3_key)
        fname = path.rsplit('/', 1)[-1] if '/' in path else path
        headers = {
            'Content-Type': 'application/octet-stream',
            'Content-Length': length,
            'Content-Disposition': f'attachment; filename="{fname}"',
        }
        return Response(gen, headers=headers)
    except Exception as e:
        return str(e), 500


@app.route('/api/shared/file')
def shared_file_stream():
    """Stream file from shared space"""
    if not session.get('user'):
        return 'Unauthorized', 401
    path = request.args.get('path', '')
    try:
        db = get_db()
        cfg = get_shared_s3_config(db)
        if not cfg:
            return 'Shared space not configured', 400
        prefix = cfg.get('prefix', '').strip('/')
        s3_key = f"{prefix}/{path}" if prefix else path
        gen, length, ctype = stream_s3_object(cfg, s3_key)
        fname = path.rsplit('/', 1)[-1] if '/' in path else path
        headers = {
            'Content-Type': ctype,
            'Content-Length': length,
            'Content-Disposition': f'inline; filename="{fname}"',
        }
        return Response(gen, headers=headers)
    except Exception as e:
        return str(e), 500


@app.route('/api/shared/download')
def shared_file_download():
    """Download file from shared space"""
    if not session.get('user'):
        return 'Unauthorized', 401
    path = request.args.get('path', '')
    try:
        db = get_db()
        cfg = get_shared_s3_config(db)
        if not cfg:
            return 'Shared space not configured', 400
        prefix = cfg.get('prefix', '').strip('/')
        s3_key = f"{prefix}/{path}" if prefix else path
        gen, length, ctype = stream_s3_object(cfg, s3_key)
        fname = path.rsplit('/', 1)[-1] if '/' in path else path
        headers = {
            'Content-Type': 'application/octet-stream',
            'Content-Length': length,
            'Content-Disposition': f'attachment; filename="{fname}"',
        }
        return Response(gen, headers=headers)
    except Exception as e:
        return str(e), 500


@app.route('/viewer/<source>')
def file_viewer(source):
    """Universal file viewer - source: workspace, s3, shared"""
    if not session.get('user'):
        return redirect('/')
    if source not in ['workspace', 's3', 'shared']:
        return 'Invalid source', 400
    if source != 'shared' and session.get('is_admin'):
        return redirect('/')

    username = session['user']
    path = request.args.get('path', '')
    filename = path.rsplit('/', 1)[-1] if '/' in path else path
    ftype, ext = get_file_type(filename)

    # Build URLs
    file_url = f'/api/{source}/file?path={path}'
    download_url = f'/api/{source}/download?path={path}'

    if ftype == 'image':
        return render_template_string(VIEWER_IMAGE, filename=filename, file_url=file_url, download_url=download_url)
    elif ftype == 'video':
        return render_template_string(VIEWER_VIDEO, filename=filename, file_url=file_url, download_url=download_url)
    elif ftype == 'audio':
        return render_template_string(VIEWER_AUDIO, filename=filename, file_url=file_url, download_url=download_url)
    elif ftype == 'pdf':
        return render_template_string(VIEWER_PDF, filename=filename, file_url=file_url, download_url=download_url)
    elif ftype == 'text':
        # Read content for text files
        content = None
        try:
            db = get_db()
            if source == 'workspace':
                content = read_workspace_text(username, path)
            elif source == 's3':
                cfg = get_s3_config(db, username)
                if cfg:
                    prefix = cfg.get('prefix', '').strip('/')
                    s3_key = f"{prefix}/{path}" if prefix else path
                    content = read_s3_text(cfg, s3_key)
            elif source == 'shared':
                cfg = get_shared_s3_config(db)
                if cfg:
                    prefix = cfg.get('prefix', '').strip('/')
                    s3_key = f"{prefix}/{path}" if prefix else path
                    content = read_s3_text(cfg, s3_key)
        except:
            content = None
        if content is None:
            content = '(Unable to load file content)'
        lang = LANG_MAP.get(ext, ext)
        return render_template_string(VIEWER_TEXT, filename=filename, content=content, lang=lang, download_url=download_url)
    elif ftype == 'markdown':
        content = None
        try:
            db = get_db()
            if source == 'workspace':
                content = read_workspace_text(username, path)
            elif source == 's3':
                cfg = get_s3_config(db, username)
                if cfg:
                    prefix = cfg.get('prefix', '').strip('/')
                    s3_key = f"{prefix}/{path}" if prefix else path
                    content = read_s3_text(cfg, s3_key)
            elif source == 'shared':
                cfg = get_shared_s3_config(db)
                if cfg:
                    prefix = cfg.get('prefix', '').strip('/')
                    s3_key = f"{prefix}/{path}" if prefix else path
                    content = read_s3_text(cfg, s3_key)
        except:
            content = None
        if content is None:
            content = '(Unable to load file content)'
        return render_template_string(VIEWER_MARKDOWN, filename=filename, content=content, download_url=download_url)
    elif ftype == 'html':
        content = None
        try:
            db = get_db()
            if source == 'workspace':
                content = read_workspace_text(username, path)
            elif source == 's3':
                cfg = get_s3_config(db, username)
                if cfg:
                    prefix = cfg.get('prefix', '').strip('/')
                    s3_key = f"{prefix}/{path}" if prefix else path
                    content = read_s3_text(cfg, s3_key)
            elif source == 'shared':
                cfg = get_shared_s3_config(db)
                if cfg:
                    prefix = cfg.get('prefix', '').strip('/')
                    s3_key = f"{prefix}/{path}" if prefix else path
                    content = read_s3_text(cfg, s3_key)
        except:
            content = None
        if content is None:
            content = '<p>Unable to load file content</p>'
        return render_template_string(VIEWER_HTML, filename=filename, content=content, download_url=download_url)
    elif ftype == 'office':
        icon = OFFICE_ICONS.get(ext, '&#128196;')
        # OnlyOffice document types
        doc_types = {'doc': 'word', 'docx': 'word', 'odt': 'word', 'rtf': 'word', 'txt': 'word',
                     'xls': 'cell', 'xlsx': 'cell', 'ods': 'cell', 'csv': 'cell',
                     'ppt': 'slide', 'pptx': 'slide', 'odp': 'slide'}
        doc_type = doc_types.get(ext, 'word')
        # Generate token for OnlyOffice file access
        file_token = generate_onlyoffice_token(source, path, username)
        file_url_full = f"{ONLYOFFICE_FILE_HOST}/api/onlyoffice/file?token={file_token}"
        # OnlyOffice config
        config = {
            "document": {
                "fileType": ext,
                "key": hashlib.md5(f"{source}:{path}:{time.time()//300}".encode()).hexdigest()[:20],
                "title": filename,
                "url": file_url_full,
                "permissions": {
                    "download": True,
                    "print": True,
                    "copy": True,
                    "edit": False,
                }
            },
            "documentType": doc_type,
            "editorConfig": {
                "mode": "view",
                "lang": "vi",
                "customization": {
                    "forcesave": False,
                    "hideRightMenu": True,
                    "compactHeader": True,
                    "toolbarNoTabs": True,
                    "compactToolbar": True,
                }
            },
            "height": "100%",
            "width": "100%",
        }
        # Sign with JWT for OnlyOffice API (disabled when JWT_ENABLED=false)
        # token = jwt.encode(config, ONLYOFFICE_JWT_SECRET, algorithm='HS256')
        # config['token'] = token
        return render_template_string(VIEWER_OFFICE, filename=filename, icon=icon, download_url=download_url,
                                      onlyoffice_url=ONLYOFFICE_URL, config_json=json.dumps(config))
    else:
        return render_template_string(VIEWER_UNSUPPORTED, filename=filename, download_url=download_url)


if __name__ == '__main__':
    port = int(os.environ.get('DASHBOARD_PORT', 9998))
    app.run(host='0.0.0.0', port=port, threaded=True)
