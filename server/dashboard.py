#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JupyterHub Multi-User Dashboard
A Flask-based dashboard for managing JupyterLab instances
"""

from flask import Flask, render_template_string, request, session, redirect, Response, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
import subprocess
import uuid
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
    get_shared_s3_config, get_chat_s3_config, list_s3_recursive,
    stream_s3_object, stream_s3_folder_as_zip, read_s3_text,
    move_s3_items, copy_s3_to_workspace,
    get_music_s3_config, list_audio_files, stream_audio, upload_music_file,
)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

# SocketIO for realtime chat
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

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

def get_usernames():
    """Get list of usernames only"""
    return [u['name'] for u in get_users()]

def user_exists(username):
    """Check if a user exists in the system"""
    return username in get_usernames()

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
.window-body{flex:1;overflow:auto;background:#0f172a;position:relative;scrollbar-width:none;-ms-overflow-style:none}
.window-body::-webkit-scrollbar{display:none}
.window-body iframe{width:100%;height:100%;border:none}
.jupyter-dropzone{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(15,23,42,.95);display:flex;align-items:center;justify-content:center;z-index:9999;cursor:pointer}
.jupyter-dropzone .dropzone-content{text-align:center;color:#fff;padding:60px 80px;background:rgba(99,102,241,.2);border:3px dashed #6366f1;border-radius:20px;max-width:500px;transition:all .2s}
.jupyter-dropzone .dropzone-content:hover{background:rgba(99,102,241,.4);transform:scale(1.02)}
.jupyter-dropzone .dropzone-icon{font-size:80px;margin-bottom:20px}
.jupyter-dropzone .dropzone-text{font-size:18px;margin-bottom:12px;font-weight:500}
.jupyter-dropzone .dropzone-hint{font-size:14px;color:#94a3b8;margin-bottom:8px}
.jupyter-dropzone .dropzone-hint strong{color:#fff}
.jupyter-dropzone.drag-over .dropzone-content{background:rgba(16,185,129,.3);border-color:#10b981}
.jupyter-dropzone .cancel-btn{margin-top:24px;background:#475569;border:none;color:#fff;padding:12px 32px;border-radius:8px;cursor:pointer;font-size:14px}
.jupyter-dropzone .cancel-btn:hover{background:#64748b}
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
    <div class="desktop-icon" ondblclick="openWindow('workspace')"><div class="icon">&#128193;</div><div class="label">Workspace</div></div>
    {% if has_s3 %}<div class="desktop-icon" ondblclick="openWindow('s3backup')"><div class="icon">&#9729;</div><div class="label">S3 Backup</div></div>{% endif %}
    {% if has_shared %}<div class="desktop-icon" ondblclick="openWindow('shared')"><div class="icon">&#128101;</div><div class="label">Shared Space</div></div>{% endif %}
    {% if has_s3 %}<div class="desktop-icon" ondblclick="openWindow('myshares')"><div class="icon">&#128279;</div><div class="label">My Shares</div></div>{% endif %}
    <div class="desktop-icon" ondblclick="openWindow('usershares')"><div class="icon">&#128229;</div><div class="label">User Shares</div></div>
    <div class="desktop-icon" ondblclick="openWindow('chat')"><div class="icon">&#128172;</div><div class="label">Chat</div></div>
    <div class="desktop-icon" ondblclick="openWindow('screenshare')"><div class="icon">&#128250;</div><div class="label">Screen Share</div></div>
    <div class="desktop-icon" ondblclick="openWindow('musicroom')"><div class="icon">&#127925;</div><div class="label">Music Room</div></div>
    <div class="desktop-icon" ondblclick="openWindow('todo')"><div class="icon">&#128203;</div><div class="label">Todo</div></div>
    <div class="desktop-icon" ondblclick="openWindow('browser')"><div class="icon">&#127760;</div><div class="label">Browser</div></div>
    <div class="desktop-icon" ondblclick="openWindow('balatro')"><div class="icon">&#127183;</div><div class="label">Balatro</div></div>
    <div class="desktop-icon" ondblclick="openWindow('gamehub')"><div class="icon">&#127918;</div><div class="label">GameHub</div></div>
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
        <a class="menu-item" href="#" onclick="openWindow('workspace');hideStartMenu()"><span class="icon">&#128193;</span><div class="text"><span>Workspace</span><small>File Manager</small></div></a>
        {% if has_s3 %}<a class="menu-item" href="#" onclick="openWindow('s3backup');hideStartMenu()"><span class="icon">&#9729;</span><div class="text"><span>S3 Backup</span><small>Backup & Restore</small></div></a>{% endif %}
        {% if has_shared %}<a class="menu-item" href="#" onclick="openWindow('shared');hideStartMenu()"><span class="icon">&#128101;</span><div class="text"><span>Shared Space</span><small>Team Storage</small></div></a>{% endif %}
        {% if has_s3 %}<a class="menu-item" href="#" onclick="openWindow('myshares');hideStartMenu()"><span class="icon">&#128279;</span><div class="text"><span>My Shares</span><small>Shared Links</small></div></a>{% endif %}
        <a class="menu-item" href="#" onclick="openWindow('usershares');hideStartMenu()"><span class="icon">&#128229;</span><div class="text"><span>User Shares</span><small>Shared with you</small></div></a>
        <a class="menu-item" href="#" onclick="openWindow('chat');hideStartMenu()"><span class="icon">&#128172;</span><div class="text"><span>Chat</span><small>Message users</small></div></a>
        <a class="menu-item" href="#" onclick="openWindow('screenshare');hideStartMenu()"><span class="icon">&#128250;</span><div class="text"><span>Screen Share</span><small>Share your screen</small></div></a>
        <a class="menu-item" href="#" onclick="openWindow('musicroom');hideStartMenu()"><span class="icon">&#127925;</span><div class="text"><span>Music Room</span><small>Listen together</small></div></a>
        <a class="menu-item" href="#" onclick="openWindow('todo');hideStartMenu()"><span class="icon">&#128203;</span><div class="text"><span>Todo</span><small>Tasks & Notes</small></div></a>
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
const APPS={jupyterlab:{title:'JupyterLab',icon:'&#128187;',url:'/embed/lab',w:1200,h:700},workspace:{title:'Workspace',icon:'&#128193;',url:'/embed/workspace',w:900,h:600},s3backup:{title:'S3 Backup',icon:'&#9729;',url:'/embed/s3-backup',w:1100,h:650},shared:{title:'Shared Space',icon:'&#128101;',url:'/embed/shared-space',w:1100,h:650},myshares:{title:'My Shares',icon:'&#128279;',url:'/embed/my-shares',w:900,h:600},usershares:{title:'User Shares',icon:'&#128229;',url:'/embed/user-shares',w:900,h:600},chat:{title:'Chat',icon:'&#128172;',url:'/embed/chat',w:1000,h:600},browser:{title:'Browser',icon:'&#127760;',url:'/embed/browser',w:1100,h:700},balatro:{title:'Balatro',icon:'&#127183;',url:'/balatro/',w:1320,h:800},gamehub:{title:'GameHub',icon:'&#127918;',url:'/gamehub/',w:1200,h:750},settings:{title:'S3 Config',icon:'&#9881;',url:'/embed/s3-config',w:700,h:550},password:{title:'Change Password',icon:'&#128274;',url:'/embed/change-password',w:500,h:450},screenshare:{title:'Screen Share',icon:'&#128250;',url:'/embed/screen-share',w:1000,h:700},musicroom:{title:'Music Room',icon:'&#127925;',url:'/embed/music-room',w:480,h:680},todo:{title:'Todo',icon:'&#128203;',url:'/embed/todo',w:900,h:600}};
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

// ===== CROSS-WINDOW FILE DRAG & DROP =====
var draggedFile=null;
window.addEventListener('message',function(e){
    if(e.data&&e.data.type==='file-drag-start'){
        draggedFile=e.data;
        showJupyterDropZone(true);
        // Update filename display
        var fn=document.getElementById('dz-filename');
        if(fn)fn.textContent=draggedFile.filename||'';
    }
    // Note: Don't hide on file-drag-end - user must click to confirm or cancel
});
function showJupyterDropZone(show){
    var dz=document.getElementById('global-dropzone');
    if(show&&!dz){
        // Open JupyterLab if not open
        if(!wins['jupyterlab']){
            openWindow('jupyterlab');
        }
        // Create full-screen dropzone
        dz=document.createElement('div');
        dz.id='global-dropzone';
        dz.className='jupyter-dropzone';
        dz.innerHTML='<div class="dropzone-content">'+
            '<div class="dropzone-icon">&#128229;</div>'+
            '<div class="dropzone-text">Nhấn vào đây để chuyển file vào JupyterLab</div>'+
            '<div class="dropzone-hint">File: <strong id="dz-filename"></strong></div>'+
            '<button class="cancel-btn" onclick="event.stopPropagation();cancelDrop()">Hủy</button>'+
            '</div>';
        dz.ondragover=function(ev){ev.preventDefault();ev.dataTransfer.dropEffect='copy';dz.classList.add('drag-over');};
        dz.ondragleave=function(ev){if(ev.target===dz)dz.classList.remove('drag-over');};
        dz.ondragenter=function(){dz.classList.add('drag-over');};
        dz.ondrop=function(ev){
            ev.preventDefault();
            if(draggedFile){
                transferToJupyter(draggedFile);
            }
            draggedFile=null;
            showJupyterDropZone(false);
        };
        // Click anywhere to transfer (for when drag doesn't work across iframes)
        dz.onclick=function(ev){
            if(ev.target===dz||ev.target.classList.contains('dropzone-content')){
                if(draggedFile){
                    transferToJupyter(draggedFile);
                }
                draggedFile=null;
                showJupyterDropZone(false);
            }
        };
        document.body.appendChild(dz);
        // Show filename
        if(draggedFile&&draggedFile.filename){
            document.getElementById('dz-filename').textContent=draggedFile.filename;
        }
    }else if(!show&&dz){
        dz.remove();
    }
}
function cancelDrop(){
    draggedFile=null;
    showJupyterDropZone(false);
}
function transferToJupyter(fileData){
    // Workspace files are already in JupyterLab - just refresh
    if(fileData.source==='workspace'){
        var jw=wins['jupyterlab'];
        if(jw){var iframe=jw.el.querySelector('iframe');if(iframe&&iframe.contentWindow)iframe.contentWindow.postMessage({type:'jupyterlab:refresh-filebrowser'},'*');}
        showStatus('File already in workspace: '+fileData.filename);
        return;
    }
    // Chat files use file_id instead of path
    if(fileData.source==='chat'){
        fetch('/api/chat/file-to-workspace',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:fileData.file_id,filename:fileData.filename})})
        .then(r=>r.json()).then(d=>{
            if(d.success){
                var jw=wins['jupyterlab'];
                if(jw){var iframe=jw.el.querySelector('iframe');if(iframe&&iframe.contentWindow)iframe.contentWindow.postMessage({type:'jupyterlab:refresh-filebrowser'},'*');}
                showStatus('File copied to workspace: '+fileData.filename);
            }else{
                alert('Transfer failed: '+(d.error||'Unknown error'));
            }
        });
        return;
    }
    var srcPath=fileData.path.replace(/[\\\/][^\\\/]+$/,'');
    var body={source:'s3',items:[fileData.filename],source_path:srcPath,dest:'workspace',dest_path:''};
    // Choose endpoint based on source
    var endpoint='/api/transfer';
    if(fileData.source==='shared')endpoint='/api/shared/transfer';
    fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(r=>r.json()).then(d=>{
        if(d.task_id){
            checkTransferStatus(d.task_id);
        }else if(d.error){
            alert('Transfer failed: '+d.error);
        }
    });
}
function checkTransferStatus(taskId){
    fetch('/api/transfer/status/'+taskId).then(r=>r.json()).then(d=>{
        if(d.status==='done'){
            // Refresh JupyterLab file browser
            var jw=wins['jupyterlab'];
            if(jw){
                var iframe=jw.el.querySelector('iframe');
                if(iframe&&iframe.contentWindow){
                    iframe.contentWindow.postMessage({type:'jupyterlab:refresh-filebrowser'},'*');
                }
            }
            showStatus('File transferred to JupyterLab');
        }else if(d.status==='error'){
            alert('Transfer failed: '+(d.error||'Unknown error'));
        }else{
            setTimeout(function(){checkTransferStatus(taskId);},1000);
        }
    });
}
function showStatus(msg){
    var bar=document.createElement('div');
    bar.style.cssText='position:fixed;bottom:60px;left:50%;transform:translateX(-50%);background:rgba(16,185,129,.9);color:#fff;padding:10px 20px;border-radius:8px;z-index:10000;';
    bar.textContent=msg;
    document.body.appendChild(bar);
    setTimeout(function(){bar.remove();},3000);
}
function refreshJupyterLab(){
    var jw=wins['jupyterlab'];
    if(jw){
        var iframe=jw.el.querySelector('iframe');
        if(iframe&&iframe.contentWindow){
            iframe.contentWindow.postMessage({type:'jupyterlab:refresh-filebrowser'},'*');
        }
    }
}

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
/* Modal System */
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;z-index:9999;opacity:0;visibility:hidden;transition:all .2s}
.modal-overlay.show{opacity:1;visibility:visible}
.modal-box{background:#1e293b;border-radius:12px;border:1px solid #334155;width:90%;max-width:400px;transform:scale(.9);transition:transform .2s;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal-overlay.show .modal-box{transform:scale(1)}
.modal-header{padding:16px 20px;border-bottom:1px solid #334155;display:flex;align-items:center;gap:10px}
.modal-header .modal-icon{font-size:20px}
.modal-header .modal-title{font-size:15px;font-weight:600;flex:1}
.modal-header .modal-close{background:none;border:none;color:#64748b;font-size:20px;cursor:pointer;padding:0;line-height:1}
.modal-header .modal-close:hover{color:#fff}
.modal-body{padding:20px}
.modal-body p{font-size:14px;color:#94a3b8;line-height:1.5;margin-bottom:16px}
.modal-body input{width:100%;padding:10px 14px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;font-size:14px;margin-bottom:16px}
.modal-body input:focus{outline:none;border-color:#6366f1}
.modal-footer{padding:12px 20px;border-top:1px solid #334155;display:flex;gap:10px;justify-content:flex-end}
.modal-type-error .modal-header{border-bottom-color:#ef4444}
.modal-type-error .modal-icon{color:#ef4444}
.modal-type-success .modal-header{border-bottom-color:#10b981}
.modal-type-success .modal-icon{color:#10b981}
.modal-type-info .modal-header{border-bottom-color:#6366f1}
.modal-type-info .modal-icon{color:#6366f1}
.modal-type-warning .modal-header{border-bottom-color:#f59e0b}
.modal-type-warning .modal-icon{color:#f59e0b}
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
// Modal System
(function(){
    var overlay=null;
    function createOverlay(){
        if(overlay)return overlay;
        overlay=document.createElement('div');
        overlay.className='modal-overlay';
        overlay.innerHTML='<div class="modal-box"><div class="modal-header"><span class="modal-icon"></span><span class="modal-title"></span><button class="modal-close">&times;</button></div><div class="modal-body"></div><div class="modal-footer"></div></div>';
        document.body.appendChild(overlay);
        overlay.querySelector('.modal-close').onclick=function(){hideModal();};
        overlay.onclick=function(e){if(e.target===overlay)hideModal();};
        return overlay;
    }
    function hideModal(){if(overlay){overlay.classList.remove('show');}}
    window.showModal=function(title,msg,type,callback){
        var o=createOverlay();
        var icons={error:'&#10060;',success:'&#9989;',info:'&#8505;',warning:'&#9888;'};
        o.querySelector('.modal-box').className='modal-box modal-type-'+(type||'info');
        o.querySelector('.modal-icon').innerHTML=icons[type]||icons.info;
        o.querySelector('.modal-title').textContent=title||'Thông báo';
        o.querySelector('.modal-body').innerHTML='<p>'+(msg||'')+'</p>';
        o.querySelector('.modal-footer').innerHTML='<button class="btn btn-primary">OK</button>';
        o.querySelector('.modal-footer button').onclick=function(){hideModal();if(callback)callback();};
        o.classList.add('show');
        o.querySelector('.modal-footer button').focus();
    };
    window.showConfirm=function(title,msg,onYes,onNo){
        var o=createOverlay();
        o.querySelector('.modal-box').className='modal-box modal-type-warning';
        o.querySelector('.modal-icon').innerHTML='&#9888;';
        o.querySelector('.modal-title').textContent=title||'Xác nhận';
        o.querySelector('.modal-body').innerHTML='<p>'+(msg||'')+'</p>';
        o.querySelector('.modal-footer').innerHTML='<button class="btn btn-secondary">Hủy</button><button class="btn btn-primary">OK</button>';
        var btns=o.querySelectorAll('.modal-footer button');
        btns[0].onclick=function(){hideModal();if(onNo)onNo();};
        btns[1].onclick=function(){hideModal();if(onYes)onYes();};
        o.classList.add('show');
        btns[1].focus();
    };
    window.showPrompt=function(title,placeholder,defaultVal,callback){
        var o=createOverlay();
        o.querySelector('.modal-box').className='modal-box modal-type-info';
        o.querySelector('.modal-icon').innerHTML='&#9998;';
        o.querySelector('.modal-title').textContent=title||'Nhập';
        o.querySelector('.modal-body').innerHTML='<input type="text" id="modal-input" placeholder="'+(placeholder||'')+'" value="'+(defaultVal||'')+'">';
        o.querySelector('.modal-footer').innerHTML='<button class="btn btn-secondary">Hủy</button><button class="btn btn-primary">OK</button>';
        var inp=o.querySelector('#modal-input');
        var btns=o.querySelectorAll('.modal-footer button');
        btns[0].onclick=function(){hideModal();if(callback)callback(null);};
        btns[1].onclick=function(){hideModal();if(callback)callback(inp.value);};
        inp.onkeydown=function(e){if(e.key==='Enter'){hideModal();if(callback)callback(inp.value);}};
        o.classList.add('show');
        inp.focus();inp.select();
    };
    window.hideModal=hideModal;
})();

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
        if (d.error) { showModal('Lỗi',d.error,'error'); return; }
        renderBreadcrumb('ws-breadcrumb', wsPath, 'loadWs');
        renderList('ws-list', d.items, wsPath, 'loadWs', false);
    });
}

function loadS3(path) {
    s3Path = path || '';
    fetch('/api/s3/list?path='+encodeURIComponent(s3Path))
    .then(r => r.json()).then(d => {
        if (d.error) { showModal('Lỗi',d.error,'error'); return; }
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
    if (!items.length) { showModal('Thông báo','Chọn file trước','warning'); return; }
    var body = JSON.stringify({
        source: source, dest: dest, items: items,
        source_path: source === 'workspace' ? wsPath : s3Path,
        dest_path: dest === 's3' ? s3Path : wsPath
    });
    fetch('/api/transfer', {method:'POST', headers:{'Content-Type':'application/json'}, body:body})
    .then(r => r.json()).then(d => {
        if (d.error) { showModal('Lỗi',d.error,'error'); return; }
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
    showPrompt('Tạo thư mục','Tên thư mục','',function(name){
        if (!name) return;
        fetch('/api/workspace/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:(wsPath?wsPath+'/':'')+name})})
        .then(r => r.json()).then(function() { loadWs(wsPath); });
    });
}
function s3Mkdir() {
    showPrompt('Tạo thư mục','Tên thư mục','',function(name){
        if (!name) return;
        fetch('/api/s3/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:(s3Path?s3Path+'/':'')+name})})
        .then(r => r.json()).then(function() { loadS3(s3Path); });
    });
}
function wsDelete() {
    var items = getChecked('ws');
    if (!items.length) return;
    showConfirm('Xóa file','Xóa '+items.length+' mục đã chọn?',function(){
        fetch('/api/workspace/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:items, path:wsPath})})
        .then(r => r.json()).then(function() { loadWs(wsPath); });
    });
}
function s3Delete() {
    var items = getChecked('s3');
    if (!items.length) return;
    showConfirm('Xóa file','Xóa '+items.length+' mục từ S3?',function(){
        fetch('/api/s3/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:items, path:s3Path})})
        .then(r => r.json()).then(function() { loadS3(s3Path); });
    });
}

function s3Share() {
    var items = getChecked('s3');
    if (items.length !== 1) { showModal('Thông báo','Chọn đúng 1 mục để chia sẻ','warning'); return; }
    var name = items[0];
    // Determine type from the file list
    var el = document.querySelector('#s3-list input[type=checkbox][value="'+name+'"]');
    var fileItem = el ? el.closest('.file-item') : null;
    var isDir = fileItem && !fileItem.querySelector('.file-size').textContent.trim().match(/^[0-9]/);
    var itemType = isDir ? 'dir' : 'file';
    // Check if it's a directory by icon
    var icon = fileItem ? fileItem.querySelector('.file-icon').innerHTML : '';
    if (icon.indexOf('128193') >= 0) itemType = 'dir';

    showPrompt('Mật khẩu','Để trống nếu không cần','',function(password){
        if(password===null)return;
        showPrompt('Thời hạn','Số giờ (0 = vĩnh viễn)','0',function(hours){
            if(hours===null)return;
            var body = JSON.stringify({
                name: name,
                type: itemType,
                s3_path: s3Path,
                password: password || '',
                expires_hours: parseInt(hours) || 0
            });
            fetch('/api/share/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:body})
            .then(r => r.json()).then(d => {
                if (d.error) { showModal('Lỗi',d.error,'error'); return; }
                var link = location.origin + '/share/' + d.share_id;
                navigator.clipboard.writeText(link).then(()=>showModal('Thành công','Link đã được copy:<br><code style="word-break:break-all;font-size:12px">'+link+'</code>','success')).catch(()=>showModal('Link chia sẻ','<code style="word-break:break-all;font-size:12px">'+link+'</code>','info'));
            });
        });
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
/* Modal System */
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;z-index:9999;opacity:0;visibility:hidden;transition:all .2s}
.modal-overlay.show{opacity:1;visibility:visible}
.modal-box{background:#1e293b;border-radius:12px;border:1px solid #334155;width:90%;max-width:400px;transform:scale(.9);transition:transform .2s;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal-overlay.show .modal-box{transform:scale(1)}
.modal-header{padding:16px 20px;border-bottom:1px solid #334155;display:flex;align-items:center;gap:10px}
.modal-header .modal-icon{font-size:20px}
.modal-header .modal-title{font-size:15px;font-weight:600;flex:1}
.modal-header .modal-close{background:none;border:none;color:#64748b;font-size:20px;cursor:pointer;padding:0;line-height:1}
.modal-header .modal-close:hover{color:#fff}
.modal-body{padding:20px}
.modal-body p{font-size:14px;color:#94a3b8;line-height:1.5;margin-bottom:16px}
.modal-body input{width:100%;padding:10px 14px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;font-size:14px;margin-bottom:16px}
.modal-body input:focus{outline:none;border-color:#6366f1}
.modal-footer{padding:12px 20px;border-top:1px solid #334155;display:flex;gap:10px;justify-content:flex-end}
.modal-type-error .modal-header{border-bottom-color:#ef4444}
.modal-type-error .modal-icon{color:#ef4444}
.modal-type-success .modal-header{border-bottom-color:#10b981}
.modal-type-success .modal-icon{color:#10b981}
.modal-type-info .modal-header{border-bottom-color:#6366f1}
.modal-type-info .modal-icon{color:#6366f1}
.modal-type-warning .modal-header{border-bottom-color:#f59e0b}
.modal-type-warning .modal-icon{color:#f59e0b}
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
// Modal System
(function(){
    var overlay=null;
    function createOverlay(){
        if(overlay)return overlay;
        overlay=document.createElement('div');
        overlay.className='modal-overlay';
        overlay.innerHTML='<div class="modal-box"><div class="modal-header"><span class="modal-icon"></span><span class="modal-title"></span><button class="modal-close">&times;</button></div><div class="modal-body"></div><div class="modal-footer"></div></div>';
        document.body.appendChild(overlay);
        overlay.querySelector('.modal-close').onclick=function(){hideModal();};
        overlay.onclick=function(e){if(e.target===overlay)hideModal();};
        return overlay;
    }
    function hideModal(){if(overlay){overlay.classList.remove('show');}}
    window.showModal=function(title,msg,type,callback){
        var o=createOverlay();
        var icons={error:'&#10060;',success:'&#9989;',info:'&#8505;',warning:'&#9888;'};
        o.querySelector('.modal-box').className='modal-box modal-type-'+(type||'info');
        o.querySelector('.modal-icon').innerHTML=icons[type]||icons.info;
        o.querySelector('.modal-title').textContent=title||'Thông báo';
        o.querySelector('.modal-body').innerHTML='<p>'+(msg||'')+'</p>';
        o.querySelector('.modal-footer').innerHTML='<button class="btn btn-primary">OK</button>';
        o.querySelector('.modal-footer button').onclick=function(){hideModal();if(callback)callback();};
        o.classList.add('show');
        o.querySelector('.modal-footer button').focus();
    };
    window.showConfirm=function(title,msg,onYes,onNo){
        var o=createOverlay();
        o.querySelector('.modal-box').className='modal-box modal-type-warning';
        o.querySelector('.modal-icon').innerHTML='&#9888;';
        o.querySelector('.modal-title').textContent=title||'Xác nhận';
        o.querySelector('.modal-body').innerHTML='<p>'+(msg||'')+'</p>';
        o.querySelector('.modal-footer').innerHTML='<button class="btn btn-secondary">Hủy</button><button class="btn btn-primary">OK</button>';
        var btns=o.querySelectorAll('.modal-footer button');
        btns[0].onclick=function(){hideModal();if(onNo)onNo();};
        btns[1].onclick=function(){hideModal();if(onYes)onYes();};
        o.classList.add('show');
        btns[1].focus();
    };
    window.showPrompt=function(title,placeholder,defaultVal,callback){
        var o=createOverlay();
        o.querySelector('.modal-box').className='modal-box modal-type-info';
        o.querySelector('.modal-icon').innerHTML='&#9998;';
        o.querySelector('.modal-title').textContent=title||'Nhập';
        o.querySelector('.modal-body').innerHTML='<input type="text" id="modal-input" placeholder="'+(placeholder||'')+'" value="'+(defaultVal||'')+'">';
        o.querySelector('.modal-footer').innerHTML='<button class="btn btn-secondary">Hủy</button><button class="btn btn-primary">OK</button>';
        var inp=o.querySelector('#modal-input');
        var btns=o.querySelectorAll('.modal-footer button');
        btns[0].onclick=function(){hideModal();if(callback)callback(null);};
        btns[1].onclick=function(){hideModal();if(callback)callback(inp.value);};
        inp.onkeydown=function(e){if(e.key==='Enter'){hideModal();if(callback)callback(inp.value);}};
        o.classList.add('show');
        inp.focus();inp.select();
    };
    window.hideModal=hideModal;
})();

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
        if (d.error) { showModal('Lỗi',d.error,'error'); return; }
        renderBreadcrumb('ws-breadcrumb', wsPath, 'loadWs');
        renderList('ws-list', d.items, wsPath, 'loadWs', false);
    });
}

function loadS3(path) {
    s3Path = path || '';
    fetch('/api/shared/list?path='+encodeURIComponent(s3Path))
    .then(r => r.json()).then(d => {
        if (d.error) { showModal('Lỗi',d.error,'error'); return; }
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
    if (!items.length) { showModal('Thông báo','Chọn file trước','warning'); return; }
    var body = JSON.stringify({
        source: source, dest: dest, items: items,
        source_path: source === 'workspace' ? wsPath : s3Path,
        dest_path: dest === 's3' ? s3Path : wsPath
    });
    fetch('/api/shared/transfer', {method:'POST', headers:{'Content-Type':'application/json'}, body:body})
    .then(r => r.json()).then(d => {
        if (d.error) { showModal('Lỗi',d.error,'error'); return; }
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
    showPrompt('Tạo thư mục','Tên thư mục','',function(name){
        if (!name) return;
        fetch('/api/workspace/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:(wsPath?wsPath+'/':'')+name})})
        .then(r => r.json()).then(function() { loadWs(wsPath); });
    });
}
function s3Mkdir() {
    showPrompt('Tạo thư mục','Tên thư mục','',function(name){
        if (!name) return;
        fetch('/api/shared/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:(s3Path?s3Path+'/':'')+name})})
        .then(r => r.json()).then(function() { loadS3(s3Path); });
    });
}
function wsDelete() {
    var items = getChecked('ws');
    if (!items.length) return;
    showConfirm('Xóa file','Xóa '+items.length+' mục đã chọn?',function(){
        fetch('/api/workspace/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:items, path:wsPath})})
        .then(r => r.json()).then(function() { loadWs(wsPath); });
    });
}
function s3Delete() {
    var items = getChecked('s3');
    if (!items.length) return;
    showConfirm('Xóa file','Xóa '+items.length+' mục từ Shared Space?',function(){
        fetch('/api/shared/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:items, path:s3Path})})
        .then(r => r.json()).then(function() { loadS3(s3Path); });
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
/* Modal System */
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;z-index:9999;opacity:0;visibility:hidden;transition:all .2s}
.modal-overlay.show{opacity:1;visibility:visible}
.modal-box{background:#1e293b;border-radius:12px;border:1px solid #334155;width:90%;max-width:400px;transform:scale(.9);transition:transform .2s;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal-overlay.show .modal-box{transform:scale(1)}
.modal-header{padding:16px 20px;border-bottom:1px solid #334155;display:flex;align-items:center;gap:10px}
.modal-header .modal-icon{font-size:20px}
.modal-header .modal-title{font-size:15px;font-weight:600;flex:1}
.modal-header .modal-close{background:none;border:none;color:#64748b;font-size:20px;cursor:pointer;padding:0;line-height:1}
.modal-header .modal-close:hover{color:#fff}
.modal-body{padding:20px}
.modal-body p{font-size:14px;color:#94a3b8;line-height:1.5;margin-bottom:16px}
.modal-body input{width:100%;padding:10px 14px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;font-size:14px;margin-bottom:16px}
.modal-body input:focus{outline:none;border-color:#6366f1}
.modal-footer{padding:12px 20px;border-top:1px solid #334155;display:flex;gap:10px;justify-content:flex-end}
.modal-type-error .modal-header{border-bottom-color:#ef4444}
.modal-type-error .modal-icon{color:#ef4444}
.modal-type-success .modal-header{border-bottom-color:#10b981}
.modal-type-success .modal-icon{color:#10b981}
.modal-type-info .modal-header{border-bottom-color:#6366f1}
.modal-type-info .modal-icon{color:#6366f1}
.modal-type-warning .modal-header{border-bottom-color:#f59e0b}
.modal-type-warning .modal-icon{color:#f59e0b}
</style>
<script>
// Modal System
(function(){
    var overlay=null;
    function createOverlay(){
        if(overlay)return overlay;
        overlay=document.createElement('div');
        overlay.className='modal-overlay';
        overlay.innerHTML='<div class="modal-box"><div class="modal-header"><span class="modal-icon"></span><span class="modal-title"></span><button class="modal-close">&times;</button></div><div class="modal-body"></div><div class="modal-footer"></div></div>';
        document.body.appendChild(overlay);
        overlay.querySelector('.modal-close').onclick=function(){hideModal();};
        overlay.onclick=function(e){if(e.target===overlay)hideModal();};
        return overlay;
    }
    function hideModal(){if(overlay){overlay.classList.remove('show');}}
    window.showModal=function(title,msg,type,callback){
        var o=createOverlay();
        var icons={error:'&#10060;',success:'&#9989;',info:'&#8505;',warning:'&#9888;'};
        o.querySelector('.modal-box').className='modal-box modal-type-'+(type||'info');
        o.querySelector('.modal-icon').innerHTML=icons[type]||icons.info;
        o.querySelector('.modal-title').textContent=title||'Thông báo';
        o.querySelector('.modal-body').innerHTML='<p>'+(msg||'')+'</p>';
        o.querySelector('.modal-footer').innerHTML='<button class="btn btn-primary">OK</button>';
        o.querySelector('.modal-footer button').onclick=function(){hideModal();if(callback)callback();};
        o.classList.add('show');
        o.querySelector('.modal-footer button').focus();
    };
    window.showConfirm=function(title,msg,onYes,onNo){
        var o=createOverlay();
        o.querySelector('.modal-box').className='modal-box modal-type-warning';
        o.querySelector('.modal-icon').innerHTML='&#9888;';
        o.querySelector('.modal-title').textContent=title||'Xác nhận';
        o.querySelector('.modal-body').innerHTML='<p>'+(msg||'')+'</p>';
        o.querySelector('.modal-footer').innerHTML='<button class="btn btn-secondary">Hủy</button><button class="btn btn-primary">OK</button>';
        var btns=o.querySelectorAll('.modal-footer button');
        btns[0].onclick=function(){hideModal();if(onNo)onNo();};
        btns[1].onclick=function(){hideModal();if(onYes)onYes();};
        o.classList.add('show');
        btns[1].focus();
    };
    window.showPrompt=function(title,placeholder,defaultVal,callback){
        var o=createOverlay();
        o.querySelector('.modal-box').className='modal-box modal-type-info';
        o.querySelector('.modal-icon').innerHTML='&#9998;';
        o.querySelector('.modal-title').textContent=title||'Nhập';
        o.querySelector('.modal-body').innerHTML='<input type="text" id="modal-input" placeholder="'+(placeholder||'')+'" value="'+(defaultVal||'')+'">';
        o.querySelector('.modal-footer').innerHTML='<button class="btn btn-secondary">Hủy</button><button class="btn btn-primary">OK</button>';
        var inp=o.querySelector('#modal-input');
        var btns=o.querySelectorAll('.modal-footer button');
        btns[0].onclick=function(){hideModal();if(callback)callback(null);};
        btns[1].onclick=function(){hideModal();if(callback)callback(inp.value);};
        inp.onkeydown=function(e){if(e.key==='Enter'){hideModal();if(callback)callback(inp.value);}};
        o.classList.add('show');
        inp.focus();inp.select();
    };
    window.hideModal=hideModal;
})();
</script>"""

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

EMBED_S3_BACKUP = EMBED_CSS + """<!DOCTYPE html><html><head><title>S3 Backup</title>
<style>
.file-item[draggable="true"]{cursor:grab}
.file-item.dragging{opacity:0.5}
.file-item.drag-over-item{background:rgba(99,102,241,.3);border:1px dashed #6366f1;border-radius:4px}
.breadcrumb-item.drag-over-bc{background:#6366f1;color:#fff;padding:2px 6px;border-radius:4px}
</style>
</head><body>
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
            <button class="btn btn-primary btn-sm" onclick="transferTo('s3')" title="Upload to S3">&#10145;</button>
            <button class="btn btn-success btn-sm" onclick="transferTo('workspace')" title="Download to Workspace">&#11013;</button>
        </div>
        <div class="pane drop-zone" id="s3-pane" data-target="s3">
            <div class="pane-header">
                <h3>&#9729; S3 Storage</h3>
                <div style="display:flex;gap:4px">
                    <label class="btn btn-sm btn-success" style="cursor:pointer">&#11014;<input type="file" class="upload-input" id="s3-upload" multiple onchange="handleUpload('s3',this.files)"></label>
                    <button class="btn btn-sm btn-warning" onclick="sendToLab()" title="Copy to Workspace root for JupyterLab">To Lab</button>
                    <button class="btn btn-sm btn-primary" onclick="s3Share()">Share</button>
                    <button class="btn btn-sm" style="background:#8b5cf6;color:#fff" onclick="s3ShareWithUser()">&#128101;</button>
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
var dragData=null;
function formatSize(b){if(b===0)return'-';if(b<1024)return b+' B';if(b<1048576)return(b/1024).toFixed(1)+' KB';if(b<1073741824)return(b/1048576).toFixed(1)+' MB';return(b/1073741824).toFixed(2)+' GB';}
function renderBreadcrumb(el,path,fn){var parts=path?path.split('/').filter(Boolean):[];var html='<a href="#" class="breadcrumb-item" data-path="" onclick="'+fn+'(\\'\\');return false">Home</a>';var acc='';parts.forEach(function(p){acc+=(acc?'/':'')+p;html+=' / <a href="#" class="breadcrumb-item" data-path="'+acc+'" onclick="'+fn+'(\\''+acc+'\\');return false">'+p+'</a>';});document.getElementById(el).innerHTML=html;if(el==='s3-breadcrumb'){setupBreadcrumbDrop();}}
function getFileIcon(name){var ext=(name.split('.').pop()||'').toLowerCase();var m={'jpg':'&#128444;','jpeg':'&#128444;','png':'&#128444;','gif':'&#128444;','webp':'&#128444;','svg':'&#128444;','bmp':'&#128444;','mp4':'&#127916;','webm':'&#127916;','mov':'&#127916;','avi':'&#127916;','mkv':'&#127916;','mp3':'&#127925;','wav':'&#127925;','flac':'&#127925;','m4a':'&#127925;','pdf':'&#128462;','doc':'&#128462;','docx':'&#128462;','xls':'&#128202;','xlsx':'&#128202;','ppt':'&#128253;','pptx':'&#128253;','md':'&#128221;','html':'&#127760;','htm':'&#127760;','py':'&#128196;','js':'&#128196;','json':'&#128196;','txt':'&#128196;','log':'&#128196;','zip':'&#128230;','rar':'&#128230;','7z':'&#128230;','tar':'&#128230;','gz':'&#128230;'};return m[ext]||'&#128196;';}
function openFile(source,path,name){if(window.parent&&window.parent.openFileViewer){window.parent.openFileViewer(source,path,name);}else{window.open('/viewer/'+source+'?path='+encodeURIComponent(path),'_blank');}}
function renderList(el,items,path,fn,isS3){var html='';var src=isS3?'s3':'workspace';items.forEach(function(i){var icon=i.type==='dir'?'&#128193;':getFileIcon(i.name);var fpath=(path?path+'/':'')+i.name;var dragAttr='';if(i.type==='file'){if(isS3){dragAttr=' draggable="true" ondragstart="onDragStart(event,\\''+i.name+'\\',\\''+i.type+'\\')" ondragend="onDragEnd(event)"';}else{dragAttr=' draggable="true" ondragstart="startWsDrag(event,\\''+fpath+'\\',\\''+i.name+'\\')" ondragend="endWsDrag()"';}}var dropAttr=isS3&&i.type==='dir'?' ondragover="onDragOverItem(event)" ondragleave="onDragLeaveItem(event)" ondrop="onDropItem(event,\\''+i.name+'\\')"':'';var click=i.type==='dir'?'onclick="'+fn+'(\\''+fpath+'\\');"':'ondblclick="openFile(\\''+src+'\\',\\''+fpath+'\\',\\''+i.name+'\\');"';html+='<div class="file-item" data-name="'+i.name+'" data-type="'+i.type+'"'+dragAttr+dropAttr+' '+click+'><input type="checkbox" value="'+i.name+'" onclick="event.stopPropagation()"><span class="file-icon">'+icon+'</span><span class="file-name">'+i.name+'</span><span class="file-size">'+formatSize(i.size)+'</span></div>';});document.getElementById(el).innerHTML=html||'<div class="empty">Empty</div>';}
function startWsDrag(e,path,filename){e.dataTransfer.setData('text/plain',filename);e.dataTransfer.effectAllowed='copy';if(window.parent)window.parent.postMessage({type:'file-drag-start',source:'workspace',path:path,filename:filename},'*');}
function endWsDrag(){if(window.parent)window.parent.postMessage({type:'file-drag-end'},'*');}
function loadWs(p){wsPath=p||'';fetch('/api/workspace/list?path='+encodeURIComponent(wsPath)).then(r=>r.json()).then(d=>{if(d.error){showModal('Lỗi',d.error,'error');return;}renderBreadcrumb('ws-breadcrumb',wsPath,'loadWs');renderList('ws-list',d.items,wsPath,'loadWs',false);});}
function loadS3(p){s3Path=p||'';fetch('/api/s3/list?path='+encodeURIComponent(s3Path)).then(r=>r.json()).then(d=>{if(d.error){showModal('Lỗi',d.error,'error');return;}renderBreadcrumb('s3-breadcrumb',s3Path,'loadS3');renderList('s3-list',d.items,s3Path,'loadS3',true);});}
function getChecked(p){return Array.from(document.querySelectorAll('#'+(p==='s3'?'s3':'ws')+'-list input:checked')).map(b=>b.value);}
function transferTo(dest){var src=dest==='s3'?'workspace':'s3';var items=getChecked(src==='workspace'?'ws':'s3');if(!items.length){showModal('Thông báo','Chọn file trước','warning');return;}fetch('/api/transfer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:src,dest:dest,items:items,source_path:src==='workspace'?wsPath:s3Path,dest_path:dest==='s3'?s3Path:wsPath})}).then(r=>r.json()).then(d=>{if(d.error){showModal('Lỗi',d.error,'error');return;}pollProgress(d.task_id);});}
function pollProgress(tid,cb){var el=document.getElementById('transfer-progress');el.style.display='block';var iv=setInterval(function(){fetch('/api/transfer/status/'+tid).then(r=>r.json()).then(d=>{var pct=d.total?Math.round(d.completed/d.total*100):0;document.getElementById('progress-fill').style.width=pct+'%';document.getElementById('progress-text').textContent=d.current_file?'Transferring: '+d.current_file+' ('+d.completed+'/'+d.total+')':'Preparing...';if(d.status==='done'){clearInterval(iv);document.getElementById('progress-text').textContent='Done!';loadWs(wsPath);loadS3(s3Path);if(cb)cb();}else if(d.status==='error'){clearInterval(iv);document.getElementById('progress-text').textContent='Error: '+d.error;}});},1000);}
function wsMkdir(){showPrompt('Tạo thư mục','Tên thư mục','',function(n){if(!n)return;fetch('/api/workspace/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:(wsPath?wsPath+'/':'')+n})}).then(()=>loadWs(wsPath));});}
function s3Mkdir(){showPrompt('Tạo thư mục','Tên thư mục','',function(n){if(!n)return;fetch('/api/s3/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:(s3Path?s3Path+'/':'')+n})}).then(()=>loadS3(s3Path));});}
function wsDelete(){var items=getChecked('ws');if(!items.length)return;showConfirm('Xóa file','Xóa '+items.length+' mục đã chọn?',function(){fetch('/api/workspace/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items,path:wsPath})}).then(()=>loadWs(wsPath));});}
function s3Delete(){var items=getChecked('s3');if(!items.length)return;showConfirm('Xóa file','Xóa '+items.length+' mục từ S3?',function(){fetch('/api/s3/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items,path:s3Path})}).then(()=>loadS3(s3Path));});}
function s3Share(){var items=getChecked('s3');if(items.length!==1){showModal('Thông báo','Chọn đúng 1 mục để chia sẻ','warning');return;}var name=items[0];var el=document.querySelector('#s3-list input[value="'+name+'"]');var fi=el?el.closest('.file-item'):null;var icon=fi?fi.querySelector('.file-icon').innerHTML:'';var type=icon.indexOf('128193')>=0?'dir':'file';showPrompt('Mật khẩu','Để trống nếu không cần','',function(pw){if(pw===null)return;showPrompt('Thời hạn','Số giờ (0 = vĩnh viễn)','0',function(hrs){if(hrs===null)return;fetch('/api/share/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,type:type,s3_path:s3Path,password:pw||'',expires_hours:parseInt(hrs)||0})}).then(r=>r.json()).then(d=>{if(d.error){showModal('Lỗi',d.error,'error');return;}var link=location.origin+'/share/'+d.share_id;navigator.clipboard.writeText(link).then(()=>showModal('Thành công','Link đã được copy:<br><code style="word-break:break-all;font-size:12px">'+link+'</code>','success')).catch(()=>showModal('Link chia sẻ','<code style="word-break:break-all;font-size:12px">'+link+'</code>','info'));});});});}
function s3ShareWithUser(){var items=getChecked('s3');if(items.length!==1){showModal('Thông báo','Chọn đúng 1 mục để chia sẻ','warning');return;}var name=items[0];var el=document.querySelector('#s3-list input[value="'+name+'"]');var fi=el?el.closest('.file-item'):null;var type=fi&&fi.dataset.type==='dir'?'dir':'file';
// Fetch friends list first
fetch('/api/friends/list').then(r=>r.json()).then(data=>{
    var friends=(data.friends||[]).filter(f=>f.status==='accepted').map(f=>f.friend);
    var html='<div style="margin-bottom:12px"><label style="font-size:13px;color:#94a3b8">Chọn bạn bè:</label>';
    if(friends.length){
        html+='<select id="share-friend-select" style="width:100%;padding:10px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;margin-top:6px"><option value="">-- Chọn từ danh sách --</option>';
        friends.forEach(f=>{html+='<option value="'+f+'">'+f+'</option>';});
        html+='</select>';
    }else{
        html+='<div style="color:#64748b;font-size:12px;margin-top:6px">Chưa có bạn bè</div>';
    }
    html+='</div><div style="margin-bottom:12px"><label style="font-size:13px;color:#94a3b8">Hoặc nhập username:</label><input type="text" id="share-user-input" placeholder="username" style="width:100%;padding:10px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;margin-top:6px"></div>';
    html+='<div><label style="font-size:13px;color:#94a3b8">Lời nhắn (tùy chọn):</label><input type="text" id="share-msg-input" placeholder="Lời nhắn..." style="width:100%;padding:10px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;margin-top:6px"></div>';
    showConfirm('Chia sẻ: '+name,html,function(){
        var toUser=document.getElementById('share-friend-select')?.value||document.getElementById('share-user-input').value;
        var msg=document.getElementById('share-msg-input').value;
        if(!toUser){showModal('Lỗi','Vui lòng chọn hoặc nhập username','warning');return;}
        fetch('/api/share-with-user',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_name:name,item_type:type,s3_path:s3Path,to_user:toUser,message:msg||''})}).then(r=>r.json()).then(d=>{if(d.error){showModal('Lỗi',d.error,'error');return;}showModal('Thành công','Đã chia sẻ với '+toUser+'!','success');});
    });
});}
function sendToLab(){var items=getChecked('s3');if(!items.length){showModal('Thông báo','Chọn file để gửi vào JupyterLab','warning');return;}fetch('/api/transfer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:'s3',dest:'workspace',items:items,source_path:s3Path,dest_path:''})}).then(r=>r.json()).then(d=>{if(d.error){showModal('Lỗi',d.error,'error');return;}pollProgress(d.task_id,function(){try{var labFrame=window.parent.document.querySelector('#win-jupyterlab iframe');if(labFrame&&labFrame.contentWindow){labFrame.contentWindow.postMessage({type:'jupyterlab:refresh-filebrowser'},'*');}}catch(e){}});});}
// S3 drag and drop within folders
function onDragStart(e,name,type){dragData={name:name,type:type,sourcePath:s3Path};e.target.classList.add('dragging');e.dataTransfer.effectAllowed=e.ctrlKey?'copy':'move';e.dataTransfer.setData('text/plain',name);var fpath=s3Path?(s3Path+'/'+name):name;if(window.parent&&type==='file')window.parent.postMessage({type:'file-drag-start',source:'s3',path:fpath,filename:name},'*');}
function onDragEnd(e){e.target.classList.remove('dragging');dragData=null;document.querySelectorAll('.drag-over-item,.drag-over-bc').forEach(el=>el.classList.remove('drag-over-item','drag-over-bc'));if(window.parent)window.parent.postMessage({type:'file-drag-end'},'*');}
function onDragOverItem(e){e.preventDefault();e.stopPropagation();e.currentTarget.classList.add('drag-over-item');e.dataTransfer.dropEffect=e.ctrlKey?'copy':'move';}
function onDragLeaveItem(e){e.currentTarget.classList.remove('drag-over-item');}
function onDropItem(e,folderName){e.preventDefault();e.stopPropagation();e.currentTarget.classList.remove('drag-over-item');if(!dragData)return;var destPath=s3Path?(s3Path+'/'+folderName):folderName;doS3Move([dragData.name],dragData.sourcePath,destPath,e.ctrlKey?'copy':'move');}
function setupBreadcrumbDrop(){document.querySelectorAll('#s3-breadcrumb .breadcrumb-item').forEach(function(bc){bc.addEventListener('dragover',function(e){e.preventDefault();e.stopPropagation();bc.classList.add('drag-over-bc');e.dataTransfer.dropEffect=e.ctrlKey?'copy':'move';});bc.addEventListener('dragleave',function(e){bc.classList.remove('drag-over-bc');});bc.addEventListener('drop',function(e){e.preventDefault();e.stopPropagation();bc.classList.remove('drag-over-bc');if(!dragData)return;var destPath=bc.dataset.path||'';if(destPath===dragData.sourcePath)return;doS3Move([dragData.name],dragData.sourcePath,destPath,e.ctrlKey?'copy':'move');});});}
function doS3Move(items,srcPath,destPath,op){fetch('/api/s3/move',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items,source_path:srcPath,dest_path:destPath,operation:op})}).then(r=>r.json()).then(d=>{if(d.error){showModal('Lỗi',d.error,'error');return;}loadS3(s3Path);});}
document.querySelectorAll('.drop-zone').forEach(z=>{['dragenter','dragover'].forEach(e=>z.addEventListener(e,ev=>{if(ev.dataTransfer.types.includes('Files')){ev.preventDefault();z.classList.add('drag-over');}}));['dragleave','drop'].forEach(e=>z.addEventListener(e,ev=>{z.classList.remove('drag-over');}));z.addEventListener('drop',e=>{if(e.dataTransfer.files.length)handleUpload(z.dataset.target,e.dataTransfer.files);});});
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
function renderList(el,items,path,fn,isS3){var html='';var src=isS3?'shared':'workspace';items.forEach(function(i){var icon=i.type==='dir'?'&#128193;':getFileIcon(i.name);var fpath=(path?path+'/':'')+i.name;var click=i.type==='dir'?'onclick="'+fn+'(\\''+fpath+'\\');"':'ondblclick="openFile(\\''+src+'\\',\\''+fpath+'\\',\\''+i.name+'\\');"';var drag=i.type==='file'?'draggable="true" ondragstart="startFileDrag(event,\\''+src+'\\',\\''+fpath+'\\',\\''+i.name+'\\');" ondragend="endFileDrag();"':'';html+='<div class="file-item" '+click+' '+drag+'><input type="checkbox" value="'+i.name+'" onclick="event.stopPropagation()"><span class="file-icon">'+icon+'</span><span class="file-name">'+i.name+'</span><span class="file-size">'+formatSize(i.size)+'</span></div>';});document.getElementById(el).innerHTML=html||'<div class="empty">Empty</div>';}
function startFileDrag(e,source,path,filename){e.dataTransfer.setData('text/plain',filename);e.dataTransfer.effectAllowed='copy';if(window.parent)window.parent.postMessage({type:'file-drag-start',source:source,path:path,filename:filename},'*');}
function endFileDrag(){if(window.parent)window.parent.postMessage({type:'file-drag-end'},'*');}
function loadWs(p){wsPath=p||'';fetch('/api/workspace/list?path='+encodeURIComponent(wsPath)).then(r=>r.json()).then(d=>{if(d.error){showModal('Lỗi',d.error,'error');return;}renderBreadcrumb('ws-breadcrumb',wsPath,'loadWs');renderList('ws-list',d.items,wsPath,'loadWs',false);});}
function loadS3(p){s3Path=p||'';fetch('/api/shared/list?path='+encodeURIComponent(s3Path)).then(r=>r.json()).then(d=>{if(d.error){showModal('Lỗi',d.error,'error');return;}renderBreadcrumb('s3-breadcrumb',s3Path,'loadS3');renderList('s3-list',d.items,s3Path,'loadS3',true);});}
function getChecked(p){return Array.from(document.querySelectorAll('#'+(p==='s3'?'s3':'ws')+'-list input:checked')).map(b=>b.value);}
function transferTo(dest){var src=dest==='s3'?'workspace':'s3';var items=getChecked(src==='workspace'?'ws':'s3');if(!items.length){showModal('Thông báo','Chọn file trước','warning');return;}fetch('/api/shared/transfer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:src,dest:dest,items:items,source_path:src==='workspace'?wsPath:s3Path,dest_path:dest==='s3'?s3Path:wsPath})}).then(r=>r.json()).then(d=>{if(d.error){showModal('Lỗi',d.error,'error');return;}pollProgress(d.task_id);});}
function pollProgress(tid){var el=document.getElementById('transfer-progress');el.style.display='block';var iv=setInterval(function(){fetch('/api/transfer/status/'+tid).then(r=>r.json()).then(d=>{var pct=d.total?Math.round(d.completed/d.total*100):0;document.getElementById('progress-fill').style.width=pct+'%';document.getElementById('progress-text').textContent=d.current_file?'Transferring: '+d.current_file:'Preparing...';if(d.status==='done'){clearInterval(iv);document.getElementById('progress-text').textContent='Done!';loadWs(wsPath);loadS3(s3Path);}else if(d.status==='error'){clearInterval(iv);document.getElementById('progress-text').textContent='Error: '+d.error;}});},1000);}
function wsMkdir(){showPrompt('Tạo thư mục','Tên thư mục','',function(n){if(!n)return;fetch('/api/workspace/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:(wsPath?wsPath+'/':'')+n})}).then(()=>loadWs(wsPath));});}
function s3Mkdir(){showPrompt('Tạo thư mục','Tên thư mục','',function(n){if(!n)return;fetch('/api/shared/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:(s3Path?s3Path+'/':'')+n})}).then(()=>loadS3(s3Path));});}
function wsDelete(){var items=getChecked('ws');if(!items.length)return;showConfirm('Xóa file','Xóa '+items.length+' mục đã chọn?',function(){fetch('/api/workspace/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items,path:wsPath})}).then(()=>loadWs(wsPath));});}
function s3Delete(){var items=getChecked('s3');if(!items.length)return;showConfirm('Xóa file','Xóa '+items.length+' mục đã chọn?',function(){fetch('/api/shared/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items,path:s3Path})}).then(()=>loadS3(s3Path));});}
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
function copyLink(id){var url=location.origin+'/share/'+id;navigator.clipboard.writeText(url).then(()=>showModal('Thành công','Đã copy link vào clipboard!','success')).catch(()=>showModal('Link chia sẻ','<code style="word-break:break-all;font-size:12px">'+url+'</code>','info'));}
function delShare(id){showConfirm('Xóa link','Xóa link chia sẻ này?',function(){fetch('/api/share/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({share_id:id})}).then(r=>r.json()).then(d=>{if(d.success)load();else showModal('Lỗi',d.error,'error');});});}
load();
</script></body></html>"""

# ===========================================
# EMBED_WORKSPACE - Standalone file manager
# ===========================================

EMBED_WORKSPACE = EMBED_CSS + """<!DOCTYPE html><html><head><title>Workspace</title></head><body>
<div class="container" style="padding:12px;height:100vh;overflow:hidden">
    <div class="pane drop-zone" style="height:calc(100vh - 24px)" id="ws-pane" data-target="workspace">
        <div class="pane-header">
            <h3>&#128193; Workspace</h3>
            <div style="display:flex;gap:4px">
                <label class="btn btn-sm btn-success" style="cursor:pointer">&#11014; Upload<input type="file" class="upload-input" id="ws-upload" multiple onchange="handleUpload(this.files)"></label>
                <button class="btn btn-sm btn-secondary" onclick="wsMkdir()">+Folder</button>
                <button class="btn btn-sm btn-danger" onclick="wsDelete()">Delete</button>
                <button class="btn btn-sm btn-primary" onclick="downloadSelected()">&#11015; Download</button>
            </div>
        </div>
        <div class="breadcrumb" id="ws-breadcrumb"></div>
        <div class="file-list" id="ws-list"></div>
        <div class="upload-progress" id="ws-upload-progress" style="display:none"></div>
    </div>
</div>
<script>
var wsPath='';
function formatSize(b){if(b===0)return'-';if(b<1024)return b+' B';if(b<1048576)return(b/1024).toFixed(1)+' KB';if(b<1073741824)return(b/1048576).toFixed(1)+' MB';return(b/1073741824).toFixed(2)+' GB';}
function renderBreadcrumb(path){var parts=path?path.split('/').filter(Boolean):[];var html='<a href="#" onclick="loadWs(\\'\\');return false">Home</a>';var acc='';parts.forEach(function(p){acc+=(acc?'/':'')+p;html+=' / <a href="#" onclick="loadWs(\\''+acc+'\\');return false">'+p+'</a>';});document.getElementById('ws-breadcrumb').innerHTML=html;}
function getFileIcon(name){var ext=(name.split('.').pop()||'').toLowerCase();var m={'jpg':'&#128444;','jpeg':'&#128444;','png':'&#128444;','gif':'&#128444;','webp':'&#128444;','svg':'&#128444;','bmp':'&#128444;','mp4':'&#127916;','webm':'&#127916;','mov':'&#127916;','avi':'&#127916;','mkv':'&#127916;','mp3':'&#127925;','wav':'&#127925;','flac':'&#127925;','m4a':'&#127925;','pdf':'&#128462;','doc':'&#128462;','docx':'&#128462;','xls':'&#128202;','xlsx':'&#128202;','ppt':'&#128253;','pptx':'&#128253;','md':'&#128221;','html':'&#127760;','htm':'&#127760;','py':'&#128196;','js':'&#128196;','json':'&#128196;','txt':'&#128196;','log':'&#128196;','zip':'&#128230;','rar':'&#128230;','7z':'&#128230;','tar':'&#128230;','gz':'&#128230;'};return m[ext]||'&#128196;';}
function openFile(path,name){if(window.parent&&window.parent.openFileViewer){window.parent.openFileViewer('workspace',path,name);}else{window.open('/viewer/workspace?path='+encodeURIComponent(path),'_blank');}}
function renderList(items,path){var html='';items.forEach(function(i){var icon=i.type==='dir'?'&#128193;':getFileIcon(i.name);var fpath=(path?path+'/':'')+i.name;var click=i.type==='dir'?'onclick="loadWs(\\''+fpath+'\\');"':'ondblclick="openFile(\\''+fpath+'\\',\\''+i.name+'\\');"';var drag=i.type==='file'?'draggable="true" ondragstart="startFileDrag(event,\\'workspace\\',\\''+fpath+'\\',\\''+i.name+'\\');" ondragend="endFileDrag();"':'';html+='<div class="file-item" '+click+' '+drag+'><input type="checkbox" value="'+i.name+'" data-type="'+i.type+'" onclick="event.stopPropagation()"><span class="file-icon">'+icon+'</span><span class="file-name">'+i.name+'</span><span class="file-size">'+formatSize(i.size)+'</span></div>';});document.getElementById('ws-list').innerHTML=html||'<div class="empty">Empty folder</div>';}
function startFileDrag(e,source,path,filename){e.dataTransfer.setData('text/plain',filename);e.dataTransfer.effectAllowed='copy';if(window.parent)window.parent.postMessage({type:'file-drag-start',source:source,path:path,filename:filename},'*');}
function endFileDrag(){if(window.parent)window.parent.postMessage({type:'file-drag-end'},'*');}
function loadWs(p){wsPath=p||'';fetch('/api/workspace/list?path='+encodeURIComponent(wsPath)).then(r=>r.json()).then(d=>{if(d.error){showModal('Lỗi',d.error,'error');return;}renderBreadcrumb(wsPath);renderList(d.items,wsPath);});}
function getChecked(){return Array.from(document.querySelectorAll('#ws-list input:checked')).map(b=>({name:b.value,type:b.dataset.type}));}
function wsMkdir(){showPrompt('Tạo thư mục','Tên thư mục','',function(n){if(!n)return;fetch('/api/workspace/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:(wsPath?wsPath+'/':'')+n})}).then(()=>loadWs(wsPath));});}
function wsDelete(){var items=getChecked().map(i=>i.name);if(!items.length)return;showConfirm('Xóa file','Xóa '+items.length+' mục đã chọn?',function(){fetch('/api/workspace/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items,path:wsPath})}).then(()=>loadWs(wsPath));});}
function downloadSelected(){var items=getChecked();if(items.length!==1){showModal('Thông báo','Chọn đúng 1 file để tải','warning');return;}var item=items[0];if(item.type==='dir'){showModal('Thông báo','Không thể tải thư mục trực tiếp','warning');return;}var fpath=(wsPath?wsPath+'/':'')+item.name;window.open('/api/workspace/download?path='+encodeURIComponent(fpath),'_blank');}
document.querySelector('.drop-zone').addEventListener('dragover',e=>{e.preventDefault();e.currentTarget.classList.add('drag-over');});
document.querySelector('.drop-zone').addEventListener('dragleave',e=>{e.currentTarget.classList.remove('drag-over');});
document.querySelector('.drop-zone').addEventListener('drop',e=>{e.preventDefault();e.currentTarget.classList.remove('drag-over');handleUpload(e.dataTransfer.files);});
function handleUpload(files){if(!files.length)return;var prog=document.getElementById('ws-upload-progress');var total=files.length,done=0,errs=[];prog.style.display='block';prog.textContent='0/'+total;function next(i){if(i>=total){prog.textContent=errs.length?'Errors: '+errs[0]:'Done!';setTimeout(()=>prog.style.display='none',2000);loadWs(wsPath);return;}var fd=new FormData();fd.append('file',files[i]);fd.append('path',wsPath);fetch('/api/workspace/upload',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{done++;if(d.error)errs.push(files[i].name);prog.textContent=done+'/'+total;next(i+1);}).catch(()=>{done++;errs.push(files[i].name);next(i+1);});}next(0);document.getElementById('ws-upload').value='';}
loadWs('');
</script></body></html>"""

# ===========================================
# ===========================================
# EMBED_BROWSER - Web Portal (opens in new tab)
# ===========================================

EMBED_BROWSER = EMBED_CSS + """<!DOCTYPE html><html><head><title>Web Portal</title>
<style>
.portal-container{padding:20px;height:100vh;overflow-y:auto;box-sizing:border-box}
.search-section{max-width:600px;margin:0 auto 30px;text-align:center}
.search-section h2{margin-bottom:16px;font-size:24px;color:#e2e8f0}
.search-box{display:flex;gap:8px}
.search-box input{flex:1;background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px 16px;color:#e2e8f0;font-size:14px}
.search-box input:focus{outline:none;border-color:#6366f1}
.search-box button{background:#6366f1;border:none;color:#fff;padding:12px 24px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:500}
.search-box button:hover{background:#4f46e5}
.bookmarks-section{max-width:900px;margin:0 auto}
.bookmarks-section h3{margin-bottom:16px;font-size:16px;color:#94a3b8;border-bottom:1px solid #334155;padding-bottom:8px}
.bookmarks-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px;margin-bottom:30px}
.bookmark-card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:16px;text-align:center;cursor:pointer;transition:all .2s}
.bookmark-card:hover{background:#334155;transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,.3)}
.bookmark-card .icon{font-size:32px;margin-bottom:8px}
.bookmark-card .name{font-size:13px;color:#e2e8f0;font-weight:500}
.tip{text-align:center;color:#64748b;font-size:12px;margin-top:20px}
</style>
</head><body>
<div class="portal-container">
    <div class="search-section">
        <h2>&#127760; Web Portal</h2>
        <div class="search-box">
            <input type="text" id="search-input" placeholder="Search Google or enter URL..." onkeydown="if(event.key==='Enter')search()">
            <button onclick="search()">Search</button>
        </div>
    </div>
    <div class="bookmarks-section">
        <h3>&#11088; Quick Links</h3>
        <div class="bookmarks-grid">
            <div class="bookmark-card" onclick="go('https://www.google.com')"><div class="icon">&#128269;</div><div class="name">Google</div></div>
            <div class="bookmark-card" onclick="go('https://chat.openai.com')"><div class="icon">&#129302;</div><div class="name">ChatGPT</div></div>
            <div class="bookmark-card" onclick="go('https://claude.ai')"><div class="icon">&#128172;</div><div class="name">Claude AI</div></div>
            <div class="bookmark-card" onclick="go('https://gemini.google.com')"><div class="icon">&#10024;</div><div class="name">Gemini</div></div>
            <div class="bookmark-card" onclick="go('https://github.com')"><div class="icon">&#128025;</div><div class="name">GitHub</div></div>
            <div class="bookmark-card" onclick="go('https://stackoverflow.com')"><div class="icon">&#128218;</div><div class="name">StackOverflow</div></div>
            <div class="bookmark-card" onclick="go('https://www.youtube.com')"><div class="icon">&#9658;</div><div class="name">YouTube</div></div>
            <div class="bookmark-card" onclick="go('https://translate.google.com')"><div class="icon">&#127760;</div><div class="name">Translate</div></div>
        </div>
        <h3>&#128187; Development</h3>
        <div class="bookmarks-grid">
            <div class="bookmark-card" onclick="go('https://docs.python.org/3/')"><div class="icon">&#128013;</div><div class="name">Python Docs</div></div>
            <div class="bookmark-card" onclick="go('https://developer.mozilla.org')"><div class="icon">&#128640;</div><div class="name">MDN Docs</div></div>
            <div class="bookmark-card" onclick="go('https://codepen.io')"><div class="icon">&#9997;</div><div class="name">CodePen</div></div>
            <div class="bookmark-card" onclick="go('https://replit.com')"><div class="icon">&#9654;</div><div class="name">Replit</div></div>
            <div class="bookmark-card" onclick="go('https://colab.research.google.com')"><div class="icon">&#128211;</div><div class="name">Colab</div></div>
            <div class="bookmark-card" onclick="go('https://kaggle.com')"><div class="icon">&#128202;</div><div class="name">Kaggle</div></div>
        </div>
        <h3>&#128736; Tools</h3>
        <div class="bookmarks-grid">
            <div class="bookmark-card" onclick="go('https://docs.google.com')"><div class="icon">&#128196;</div><div class="name">Google Docs</div></div>
            <div class="bookmark-card" onclick="go('https://sheets.google.com')"><div class="icon">&#128202;</div><div class="name">Sheets</div></div>
            <div class="bookmark-card" onclick="go('https://drive.google.com')"><div class="icon">&#128193;</div><div class="name">Drive</div></div>
            <div class="bookmark-card" onclick="go('https://notion.so')"><div class="icon">&#128221;</div><div class="name">Notion</div></div>
            <div class="bookmark-card" onclick="go('https://figma.com')"><div class="icon">&#127912;</div><div class="name">Figma</div></div>
            <div class="bookmark-card" onclick="go('https://canva.com')"><div class="icon">&#127912;</div><div class="name">Canva</div></div>
        </div>
    </div>
    <p class="tip">Links open in a new browser tab</p>
</div>
<script>
function go(url){window.open(url,'_blank');}
function search(){var q=document.getElementById('search-input').value.trim();if(!q)return;var url;if(q.startsWith('http://')||q.startsWith('https://')){url=q;}else if(q.includes('.')&&!q.includes(' ')){url='https://'+q;}else{url='https://www.google.com/search?q='+encodeURIComponent(q);}window.open(url,'_blank');}
</script>
</body></html>
"""

# EMBED_CHAT - Realtime chat (friends only, file approval, recall)
# ===========================================

EMBED_CHAT = EMBED_CSS + """<!DOCTYPE html><html><head><title>Chat</title>
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
.chat-container{display:flex;height:calc(100vh - 24px);gap:12px}
.sidebar{width:260px;background:#1e293b;border-radius:10px;border:1px solid #334155;display:flex;flex-direction:column;overflow:hidden}
.sidebar-header{padding:12px;border-bottom:1px solid #334155;font-size:13px;font-weight:600;display:flex;align-items:center;justify-content:space-between}
.sidebar-header .add-btn{background:#6366f1;border:none;color:#fff;width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:16px}
.sidebar-header .add-btn:hover{background:#4f46e5}
.search-box{padding:8px 12px;border-bottom:1px solid #334155}
.search-box input{width:100%;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:8px 10px;color:#e2e8f0;font-size:12px}
.search-box input:focus{outline:none;border-color:#6366f1}
.tabs{display:flex;border-bottom:1px solid #334155}
.tab{flex:1;padding:10px;text-align:center;font-size:12px;cursor:pointer;border-bottom:2px solid transparent}
.tab:hover{background:#334155}
.tab.active{border-bottom-color:#6366f1;color:#6366f1}
.contact-list{flex:1;overflow-y:auto}
.contact-item{display:flex;align-items:center;gap:10px;padding:10px 12px;cursor:pointer;border-bottom:1px solid #1e293b}
.contact-item:hover{background:#334155}
.contact-item.active{background:rgba(99,102,241,.2);border-left:3px solid #6366f1}
.contact-item .avatar{width:36px;height:36px;background:#334155;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0}
.contact-item .info{flex:1;min-width:0}
.contact-item .name{font-size:13px;font-weight:500;display:flex;align-items:center;gap:6px}
.contact-item .name .online-dot{width:8px;height:8px;background:#10b981;border-radius:50%}
.contact-item .last-msg{font-size:11px;color:#64748b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.contact-item .meta{text-align:right;flex-shrink:0}
.contact-item .time{font-size:10px;color:#64748b}
.contact-item .unread{background:#ef4444;color:#fff;font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600;min-width:18px;text-align:center}
.contact-item .friend-badge{font-size:10px;color:#10b981}
.contact-item .pending-badge{font-size:10px;color:#f59e0b}
.tab .badge{background:#ef4444;color:#fff;font-size:10px;padding:1px 6px;border-radius:8px;margin-left:4px;font-weight:600}
.contact-item .actions{display:flex;gap:4px}
.contact-item .actions button{padding:4px 8px;font-size:11px;border-radius:4px;border:none;cursor:pointer}
.chat-main{flex:1;background:#1e293b;border-radius:10px;border:1px solid #334155;display:flex;flex-direction:column;overflow:hidden;min-height:0}
#chat-area{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.chat-header{padding:12px 16px;border-bottom:1px solid #334155;display:flex;align-items:center;gap:12px}
.chat-header .avatar{width:40px;height:40px;background:#334155;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:16px}
.chat-header .info{flex:1}
.chat-header .name{font-weight:600;font-size:14px}
.chat-header .status{font-size:12px;color:#94a3b8}
.chat-header .header-actions button{background:transparent;border:1px solid #334155;color:#94a3b8;padding:6px 10px;border-radius:6px;cursor:pointer;font-size:12px}
.chat-header .header-actions button:hover{background:#334155;color:#fff}
.chat-messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px;min-height:0}
.message{max-width:70%;padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.4}
.message.sent{background:#6366f1;color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
.message.received{background:#334155;align-self:flex-start;border-bottom-left-radius:4px}
.message .time{font-size:10px;opacity:0.7;margin-top:4px}
.message.file{background:#0f172a;border:1px solid #334155;max-width:85%}
.message.file .file-box{display:flex;align-items:center;gap:10px;padding:8px}
.message.file .file-icon{font-size:28px}
.message.file .file-name{font-weight:500;word-break:break-all}
.message.file .file-size{font-size:11px;color:#94a3b8}
.message.file .file-actions{display:flex;gap:6px;margin-top:8px;padding-top:8px;border-top:1px solid #334155}
.chat-input{padding:12px;border-top:1px solid #334155;display:flex;gap:8px;align-items:center}
.chat-input input[type="text"]{flex:1;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:10px 14px;color:#e2e8f0;font-size:13px}
.chat-input input[type="text"]:focus{outline:none;border-color:#6366f1}
.chat-input .attach-btn{background:#334155;border:none;color:#94a3b8;width:38px;height:38px;border-radius:8px;cursor:pointer;font-size:18px;display:flex;align-items:center;justify-content:center}
.chat-input .attach-btn:hover{background:#475569;color:#fff}
.chat-input .send-btn{background:#6366f1;border:none;color:#fff;padding:10px 20px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500}
.chat-input .send-btn:hover{background:#4f46e5}
.chat-input .send-btn:disabled{background:#334155;cursor:not-allowed}
.no-chat{display:flex;align-items:center;justify-content:center;flex:1;color:#64748b;text-align:center}
.no-chat .icon{font-size:60px;margin-bottom:16px;opacity:0.5}
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:1000}
.modal-overlay.show{display:flex}
.modal{background:#1e293b;border-radius:12px;border:1px solid #334155;width:400px;max-height:80vh;display:flex;flex-direction:column}
.modal-header{padding:16px;border-bottom:1px solid #334155;display:flex;align-items:center;justify-content:space-between}
.modal-header h3{margin:0;font-size:16px}
.modal-header .close-btn{background:transparent;border:none;color:#94a3b8;font-size:20px;cursor:pointer}
.modal-body{padding:16px;flex:1;overflow-y:auto}
.modal-footer{padding:16px;border-top:1px solid #334155;display:flex;justify-content:flex-end;gap:8px}
.search-result{padding:10px;border-radius:6px;display:flex;align-items:center;gap:10px;cursor:pointer}
.search-result:hover{background:#334155}
.search-result .avatar{width:36px;height:36px;background:#334155;border-radius:50%;display:flex;align-items:center;justify-content:center}
.search-result .info{flex:1}
.search-result .name{font-size:13px;font-weight:500}
.search-result .status{font-size:11px;color:#64748b}
.upload-progress{background:#0f172a;border-radius:8px;padding:10px;margin-top:8px}
.upload-progress .filename{font-size:12px;margin-bottom:6px}
.upload-progress .bar{height:4px;background:#334155;border-radius:2px;overflow:hidden}
.upload-progress .bar-fill{height:100%;background:#6366f1;transition:width .3s}
</style>
</head><body>
<div class="container" style="padding:12px;height:100vh;overflow:hidden;box-sizing:border-box">
    <div class="chat-container">
        <div class="sidebar">
            <div class="sidebar-header">
                <span>&#128172; Tin nhắn</span>
                <button class="add-btn" onclick="showAddFriend()" title="Thêm bạn">+</button>
            </div>
            <div class="search-box">
                <input type="text" id="contact-search" placeholder="Tìm kiếm..." oninput="filterContacts()">
            </div>
            <div class="tabs">
                <div class="tab active" data-tab="friends" onclick="switchTab('friends')">Bạn bè</div>
                <div class="tab" data-tab="requests" onclick="switchTab('requests')">Lời mời<span id="request-count" class="badge" style="display:none"></span></div>
            </div>
            <div class="contact-list" id="contact-list"></div>
        </div>
        <div class="chat-main">
            <div id="chat-area">
                <div class="no-chat"><div><div class="icon">&#128172;</div><div>Chọn một người để bắt đầu trò chuyện</div></div></div>
            </div>
        </div>
    </div>
</div>

<!-- Add Friend Modal -->
<div class="modal-overlay" id="add-friend-modal">
    <div class="modal">
        <div class="modal-header">
            <h3>&#128269; Tìm kiếm người dùng</h3>
            <button class="close-btn" onclick="hideAddFriend()">&times;</button>
        </div>
        <div class="modal-body">
            <input type="text" id="user-search" placeholder="Nhập tên người dùng..." style="width:100%;padding:10px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;margin-bottom:12px" oninput="searchUsers()">
            <div id="search-results"></div>
        </div>
    </div>
</div>

<!-- File Preview Modal -->
<div class="modal-overlay" id="file-modal">
    <div class="modal">
        <div class="modal-header">
            <h3>&#128206; Gửi file</h3>
            <button class="close-btn" onclick="hideFileModal()">&times;</button>
        </div>
        <div class="modal-body">
            <div id="file-preview"></div>
            <div class="upload-progress" id="upload-progress" style="display:none">
                <div class="filename" id="upload-filename"></div>
                <div class="bar"><div class="bar-fill" id="upload-bar" style="width:0%"></div></div>
            </div>
        </div>
        <div class="modal-footer">
            <button class="btn btn-secondary" onclick="hideFileModal()">Hủy</button>
            <button class="btn btn-primary" id="send-file-btn" onclick="confirmSendFile()">Gửi</button>
        </div>
    </div>
</div>

<script>
var socket=io();
var currentUser='{{ username }}';
var selectedUser=null;
var currentTab='friends';
var contacts={};  // username -> {online, lastMsg, lastTime, unread}
var friends={};   // username -> 'accepted'|'pending_sent'|'pending_received'
var messages={};
var pendingFile=null;
var searchTimeout=null;

// ===== INITIALIZATION =====
function init(){
    loadFriends();
    loadContacts();
    loadPendingFiles();
}

function loadFriends(){
    fetch('/api/friends/list').then(r=>r.json()).then(data=>{
        friends={};
        (data.friends||[]).forEach(f=>{
            friends[f.friend]=f.status;
        });
        (data.pending_received||[]).forEach(f=>{
            friends[f.from_user]='pending_received';
        });
        (data.pending_sent||[]).forEach(f=>{
            friends[f.to_user]='pending_sent';
        });
        updateRequestCount();
        renderContacts();
    });
}

function loadContacts(){
    fetch('/api/chat/contacts').then(r=>r.json()).then(data=>{
        contacts={};
        (data.contacts||[]).forEach(c=>{
            contacts[c.username]={
                online:c.online,
                lastMsg:c.last_message||'',
                lastTime:c.last_time||'',
                unread:c.unread||0
            };
        });
        renderContacts();
    });
}

function loadPendingFiles(){
    fetch('/api/chat/pending-files').then(r=>r.json()).then(data=>{
        // Handle pending files if needed
    });
}

// ===== SOCKET EVENTS =====
socket.on('connect',function(){
    console.log('Connected to chat');
    socket.emit('get_online_users');
});

socket.on('online_users',function(data){
    var onlineList=data.users||[];
    Object.keys(contacts).forEach(u=>{
        contacts[u].online=onlineList.includes(u);
    });
    renderContacts();
});

socket.on('user_status',function(data){
    if(contacts[data.user]){
        contacts[data.user].online=(data.status==='online');
    }
    renderContacts();
    if(selectedUser===data.user)updateChatHeader();
});

socket.on('new_message',function(data){
    var from=data.from_user;
    if(!messages[from])messages[from]=[];
    messages[from].push(data);
    // Add to contacts if not exists
    if(!contacts[from]){
        contacts[from]={online:true,lastMsg:'',lastTime:'',unread:0};
    }
    contacts[from].lastMsg=data.message_type==='file'?'[File] '+data.file_info.filename:data.content;
    contacts[from].lastTime=data.created_at;
    if(selectedUser===from){
        renderMessages();
        scrollToBottom();
    }else{
        contacts[from].unread=(contacts[from].unread||0)+1;
    }
    renderContacts();
});

socket.on('message_sent',function(data){
    // Update temp_id with real id from server
    if(data.temp_id && data.id){
        var user=data.to_user;
        var msgs=messages[user]||[];
        for(var i=0;i<msgs.length;i++){
            if(msgs[i]._id===data.temp_id){
                msgs[i]._id=data.id;
                msgs[i].id=data.id;
                break;
            }
        }
        renderMessages();
    }
});

socket.on('message_history',function(data){
    messages[data.with_user]=data.messages||[];
    renderMessages();
    scrollToBottom();
});

socket.on('friend_request',function(data){
    friends[data.from_user]='pending_received';
    updateRequestCount();
    renderContacts();
});

socket.on('friend_accepted',function(data){
    friends[data.by_user]='accepted';
    if(!contacts[data.by_user])contacts[data.by_user]={online:false,lastMsg:'',lastTime:'',unread:0};
    renderContacts();
});

socket.on('file_uploaded',function(data){
    // File upload complete, now send via socket
    socket.emit('send_file_message',{to_user:selectedUser,file_id:data.file_id,filename:data.filename,size:data.size});
    hideFileModal();
});

// ===== TAB & FILTER =====
function switchTab(tab){
    currentTab=tab;
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelector('.tab[data-tab="'+tab+'"]').classList.add('active');
    renderContacts();
}

function filterContacts(){
    renderContacts();
}

function updateRequestCount(){
    var count=Object.values(friends).filter(s=>s==='pending_received').length;
    var el=document.getElementById('request-count');
    if(count>0){
        el.textContent=count;
        el.style.display='inline';
    }else{
        el.style.display='none';
    }
}

// ===== RENDER CONTACTS =====
function renderContacts(){
    var search=document.getElementById('contact-search').value.toLowerCase();
    var html='';
    var list=[];

    if(currentTab==='requests'){
        // Show pending friend requests
        Object.keys(friends).forEach(u=>{
            if(friends[u]==='pending_received'){
                list.push({username:u,type:'request'});
            }
        });
    }else{
        // Friends tab - show accepted friends only
        Object.keys(friends).forEach(u=>{
            if(friends[u]==='accepted'){
                var c=contacts[u]||{online:false,lastMsg:'',lastTime:'',unread:0};
                list.push({username:u,type:'friend',...c});
            }
        });
    }

    // Filter by search
    if(search){
        list=list.filter(x=>x.username.toLowerCase().includes(search));
    }

    // Sort: online first, then by last message time
    list.sort((a,b)=>{
        if(a.online!==b.online)return b.online-a.online;
        return(b.lastTime||'').localeCompare(a.lastTime||'');
    });

    // Render
    list.forEach(item=>{
        var u=item.username;
        var active=selectedUser===u?'active':'';
        var initial=u.charAt(0).toUpperCase();

        if(item.type==='request'){
            html+='<div class="contact-item" style="background:#1e3a5f">';
            html+='<div class="avatar">'+initial+'</div>';
            html+='<div class="info"><div class="name">'+escapeHtml(u)+'</div><div class="last-msg pending-badge">Muốn kết bạn</div></div>';
            html+='<div class="actions">';
            html+='<button style="background:#10b981;color:#fff" onclick="event.stopPropagation();acceptFriend(\\''+u+'\\')">✓</button>';
            html+='<button style="background:#ef4444;color:#fff" onclick="event.stopPropagation();rejectFriend(\\''+u+'\\')">✕</button>';
            html+='</div></div>';
        }else{
            html+='<div class="contact-item '+active+'" onclick="selectUser(\\''+u+'\\')">';
            html+='<div class="avatar">'+initial+'</div>';
            html+='<div class="info">';
            html+='<div class="name">'+(item.online?'<span class="online-dot"></span>':'')+escapeHtml(u)+' <span class="friend-badge">★</span></div>';
            html+='<div class="last-msg">'+(item.lastMsg?escapeHtml(item.lastMsg):'Chưa có tin nhắn')+'</div>';
            html+='</div>';
            html+='<div class="meta">';
            if(item.lastTime)html+='<div class="time">'+formatTime(item.lastTime)+'</div>';
            if(item.unread)html+='<div class="unread">'+item.unread+'</div>';
            html+='</div></div>';
        }
    });

    document.getElementById('contact-list').innerHTML=html||'<div style="padding:20px;text-align:center;color:#64748b">'+(currentTab==='requests'?'Không có lời mời':'Chưa có bạn bè. Nhấn + để tìm kiếm.')+'</div>';
}

// ===== SELECT USER & CHAT =====
function selectUser(u){
    selectedUser=u;
    if(contacts[u]&&contacts[u].unread>0){
        contacts[u].unread=0;
        socket.emit('mark_messages_read',{from_user:u});
    }
    renderContacts();
    socket.emit('get_messages',{with_user:u});

    var online=contacts[u]?.online;
    var isFriend=friends[u]==='accepted';
    var friendStatus=friends[u];

    var headerActions='';
    if(!isFriend&&friendStatus!=='pending_sent'){
        headerActions='<button onclick="addFriend(\\''+u+'\\')">+ Kết bạn</button>';
    }else if(friendStatus==='pending_sent'){
        headerActions='<button disabled>Đã gửi lời mời</button>';
    }else if(isFriend){
        headerActions='<button onclick="removeFriend(\\''+u+'\\')">Hủy kết bạn</button>';
    }

    document.getElementById('chat-area').innerHTML=
        '<div class="chat-header">'+
        '<div class="avatar">'+u.charAt(0).toUpperCase()+'</div>'+
        '<div class="info"><div class="name">'+escapeHtml(u)+(isFriend?' <span style="color:#10b981">★</span>':'')+'</div>'+
        '<div class="status">'+(online?'Đang online':'Offline')+'</div></div>'+
        '<div class="header-actions">'+headerActions+'</div>'+
        '</div>'+
        '<div class="chat-messages" id="chat-messages"></div>'+
        '<div class="chat-input">'+
        '<label class="attach-btn" title="Đính kèm file"><input type="file" style="display:none" onchange="previewFile(this.files[0])">&#128206;</label>'+
        '<input type="text" id="msg-input" placeholder="Nhập tin nhắn..." onkeydown="if(event.key===\\'Enter\\')sendMsg()">'+
        '<button class="send-btn" onclick="sendMsg()">Gửi</button>'+
        '</div>';
}

function updateChatHeader(){
    if(!selectedUser)return;
    var statusEl=document.querySelector('.chat-header .status');
    if(statusEl){
        statusEl.textContent=contacts[selectedUser]?.online?'Đang online':'Offline';
    }
}

function renderMessages(){
    var msgs=messages[selectedUser]||[];
    var html='';
    var lastDate='';

    msgs.forEach(function(m,idx){
        if(m.recalled){
            // Show recalled message placeholder
            html+='<div class="message '+(m.from_user===currentUser?'sent':'received')+'" style="opacity:0.5;font-style:italic">Tin nhắn đã thu hồi</div>';
            return;
        }

        var sent=m.from_user===currentUser;
        var msgDate=new Date(m.created_at).toLocaleDateString('vi-VN');
        var time=new Date(m.created_at).toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit'});
        var msgId=m._id||m.id||idx;

        // Date separator
        if(msgDate!==lastDate){
            html+='<div style="text-align:center;font-size:11px;color:#64748b;margin:16px 0">'+msgDate+'</div>';
            lastDate=msgDate;
        }

        if(m.message_type==='file'){
            var fi=m.file_info||{};
            var status=fi.status||'pending';
            html+='<div class="message file '+(sent?'sent':'received')+'" data-id="'+msgId+'">';
            html+='<div class="file-box"><span class="file-icon">&#128196;</span><div><div class="file-name">'+escapeHtml(fi.filename||'file')+'</div><div class="file-size">'+(fi.size?formatSize(fi.size):'')+'</div></div></div>';

            if(sent){
                // Sender can always download their own file
                html+='<div class="file-actions">';
                html+='<a href="/api/chat/file/'+fi.file_id+'" class="btn btn-sm btn-primary" download="'+escapeHtml(fi.filename||'file')+'">Tải xuống</a>';
                if(status==='pending'){
                    html+='<span style="font-size:11px;color:#f59e0b;margin-left:8px">Chờ duyệt</span>';
                }else if(status==='rejected'){
                    html+='<span style="font-size:11px;color:#ef4444;margin-left:8px">Bị từ chối</span>';
                }else if(status==='accepted'){
                    html+='<span style="font-size:11px;color:#10b981;margin-left:8px">Đã chấp nhận</span>';
                }
                html+='</div>';
            }else if(status==='pending'){
                // Receiver needs to approve
                html+='<div class="file-actions" style="border-top:1px solid #334155;padding-top:8px;margin-top:8px">';
                html+='<button class="btn btn-sm btn-success" onclick="acceptFile(\\''+fi.file_id+'\\')">Chấp nhận</button>';
                html+='<button class="btn btn-sm btn-danger" onclick="rejectFile(\\''+fi.file_id+'\\')">Từ chối</button>';
                html+='</div>';
            }else if(status==='accepted'){
                // Accepted - show download options
                html+='<div class="file-actions">';
                html+='<a href="/api/chat/file/'+fi.file_id+'" class="btn btn-sm btn-primary" download="'+escapeHtml(fi.filename||'file')+'">Tải xuống</a>';
                html+='<button class="btn btn-sm btn-secondary" onclick="showSaveDialog(\\''+fi.file_id+'\\',\\''+escapeHtml(fi.filename)+'\\')">Lưu vào...</button>';
                html+='</div>';
            }else if(status==='rejected'){
                html+='<div style="font-size:11px;color:#ef4444;margin-top:6px">Đã từ chối</div>';
            }

            html+='<div class="time">'+time;
            if(sent)html+=' <span style="cursor:pointer;margin-left:6px" onclick="recallMessage(\\''+msgId+'\\','+idx+')" title="Thu hồi">🗑</span>';
            html+='</div></div>';
        }else{
            html+='<div class="message '+(sent?'sent':'received')+'" data-id="'+msgId+'">';
            html+=escapeHtml(m.content);
            html+='<div class="time">'+time;
            if(sent)html+=' <span style="cursor:pointer;margin-left:6px" onclick="recallMessage(\\''+msgId+'\\','+idx+')" title="Thu hồi">🗑</span>';
            html+='</div></div>';
        }
    });

    var el=document.getElementById('chat-messages');
    if(el)el.innerHTML=html||'<div style="text-align:center;padding:40px;color:#64748b">Chưa có tin nhắn</div>';
}

function sendMsg(){
    var input=document.getElementById('msg-input');
    var text=input.value.trim();
    if(!text||!selectedUser)return;

    // Must be friends to send messages
    if(friends[selectedUser]!=='accepted'){
        showModal('Thông báo','Bạn phải kết bạn trước khi nhắn tin!','warning');
        return;
    }

    // Generate temp ID for local display (will be updated by server)
    var tempId='temp_'+Date.now();
    socket.emit('send_message',{to_user:selectedUser,content:text,temp_id:tempId});

    if(!messages[selectedUser])messages[selectedUser]=[];
    var now=new Date().toISOString();
    messages[selectedUser].push({_id:tempId,from_user:currentUser,to_user:selectedUser,content:text,message_type:'text',created_at:now});

    // Update contact
    if(!contacts[selectedUser])contacts[selectedUser]={online:false,lastMsg:'',lastTime:'',unread:0};
    contacts[selectedUser].lastMsg=text;
    contacts[selectedUser].lastTime=now;

    renderMessages();
    renderContacts();
    scrollToBottom();
    input.value='';
}

// ===== FILE UPLOAD =====
function previewFile(file){
    if(!file||!selectedUser)return;
    pendingFile=file;

    var preview='<div style="display:flex;align-items:center;gap:12px;padding:16px;background:#0f172a;border-radius:8px">';
    preview+='<span style="font-size:40px">&#128196;</span>';
    preview+='<div><div style="font-weight:500">'+escapeHtml(file.name)+'</div>';
    preview+='<div style="font-size:12px;color:#64748b">'+formatSize(file.size)+'</div></div></div>';
    preview+='<div style="margin-top:12px;font-size:12px;color:#94a3b8">Gửi đến: <strong>'+escapeHtml(selectedUser)+'</strong></div>';

    document.getElementById('file-preview').innerHTML=preview;
    document.getElementById('upload-progress').style.display='none';
    document.getElementById('send-file-btn').disabled=false;
    document.getElementById('file-modal').classList.add('show');
}

function hideFileModal(){
    document.getElementById('file-modal').classList.remove('show');
    pendingFile=null;
}

function confirmSendFile(){
    if(!pendingFile||!selectedUser)return;

    // Must be friends to send files
    if(friends[selectedUser]!=='accepted'){
        showModal('Thông báo','Bạn phải kết bạn trước khi gửi file!','warning');
        return;
    }

    document.getElementById('send-file-btn').disabled=true;
    document.getElementById('upload-progress').style.display='block';
    document.getElementById('upload-filename').textContent='Đang tải: '+pendingFile.name;

    var formData=new FormData();
    formData.append('file',pendingFile);
    formData.append('to_user',selectedUser);

    var xhr=new XMLHttpRequest();
    xhr.open('POST','/api/chat/upload');

    xhr.upload.onprogress=function(e){
        if(e.lengthComputable){
            var pct=Math.round((e.loaded/e.total)*100);
            document.getElementById('upload-bar').style.width=pct+'%';
        }
    };

    xhr.onload=function(){
        if(xhr.status===200){
            var resp=JSON.parse(xhr.responseText);
            if(resp.success){
                // Add file message locally with message_id for recall
                if(!messages[selectedUser])messages[selectedUser]=[];
                messages[selectedUser].push({
                    _id:resp.message_id,
                    from_user:currentUser,
                    to_user:selectedUser,
                    message_type:'file',
                    file_info:{
                        file_id:resp.file_id,
                        filename:resp.filename,
                        size:resp.size,
                        status:resp.status,
                        download_url:resp.download_url
                    },
                    created_at:new Date().toISOString()
                });
                // Update contact
                if(!contacts[selectedUser])contacts[selectedUser]={online:false,lastMsg:'',lastTime:'',unread:0};
                contacts[selectedUser].lastMsg='[File] '+resp.filename;
                contacts[selectedUser].lastTime=new Date().toISOString();
                renderMessages();
                renderContacts();
                scrollToBottom();
                hideFileModal();
            }else{
                showModal('Lỗi',resp.error||'Upload thất bại','error');
                document.getElementById('send-file-btn').disabled=false;
            }
        }else{
            showModal('Lỗi','Lỗi upload file','error');
            document.getElementById('send-file-btn').disabled=false;
        }
    };

    xhr.send(formData);
}

// ===== FRIENDS =====
function showAddFriend(){
    document.getElementById('user-search').value='';
    document.getElementById('search-results').innerHTML='<div style="color:#64748b;text-align:center;padding:20px">Nhập tên để tìm kiếm</div>';
    document.getElementById('add-friend-modal').classList.add('show');
    document.getElementById('user-search').focus();
}

function hideAddFriend(){
    document.getElementById('add-friend-modal').classList.remove('show');
}

function searchUsers(){
    clearTimeout(searchTimeout);
    var q=document.getElementById('user-search').value.trim();
    if(q.length<1){
        document.getElementById('search-results').innerHTML='<div style="color:#64748b;text-align:center;padding:20px">Nhập tên để tìm kiếm</div>';
        return;
    }
    searchTimeout=setTimeout(function(){
        fetch('/api/friends/search?q='+encodeURIComponent(q)).then(r=>r.json()).then(data=>{
            var html='';
            (data.users||[]).forEach(u=>{
                var status=friends[u.username];
                var statusText='';
                var actionBtn='';
                if(status==='accepted'){
                    statusText='<span class="friend-badge">Bạn bè</span>';
                }else if(status==='pending_sent'){
                    statusText='<span class="pending-badge">Đã gửi lời mời</span>';
                }else if(status==='pending_received'){
                    statusText='<span class="pending-badge">Đang chờ bạn chấp nhận</span>';
                    actionBtn='<button class="btn btn-success btn-sm" onclick="event.stopPropagation();acceptFriend(\\''+u.username+'\\')">Chấp nhận</button>';
                }else{
                    actionBtn='<button class="btn btn-primary btn-sm" onclick="event.stopPropagation();addFriend(\\''+u.username+'\\')">Kết bạn</button>';
                }
                html+='<div class="search-result" onclick="selectUser(\\''+u.username+'\\');hideAddFriend()">';
                html+='<div class="avatar">'+u.username.charAt(0).toUpperCase()+'</div>';
                html+='<div class="info"><div class="name">'+escapeHtml(u.username)+'</div><div class="status">'+(u.online?'Online':'Offline')+' '+statusText+'</div></div>';
                html+=actionBtn;
                html+='</div>';
            });
            document.getElementById('search-results').innerHTML=html||'<div style="color:#64748b;text-align:center;padding:20px">Không tìm thấy</div>';
        });
    },300);
}

function addFriend(username){
    fetch('/api/friends/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username})})
    .then(r=>r.json()).then(data=>{
        if(data.success){
            friends[username]='pending_sent';
            renderContacts();
            searchUsers();
            if(selectedUser===username)selectUser(username);
        }else{
            alert(data.error||'Lỗi');
        }
    });
}

function acceptFriend(username){
    fetch('/api/friends/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username})})
    .then(r=>r.json()).then(data=>{
        if(data.success){
            friends[username]='accepted';
            if(!contacts[username])contacts[username]={online:false,lastMsg:'',lastTime:'',unread:0};
            updateRequestCount();
            renderContacts();
        }else{
            alert(data.error||'Lỗi');
        }
    });
}

function rejectFriend(username){
    fetch('/api/friends/reject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username})})
    .then(r=>r.json()).then(data=>{
        if(data.success){
            delete friends[username];
            updateRequestCount();
            renderContacts();
        }
    });
}

function removeFriend(username){
    if(!confirm('Hủy kết bạn với '+username+'?'))return;
    fetch('/api/friends/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username})})
    .then(r=>r.json()).then(data=>{
        if(data.success){
            delete friends[username];
            renderContacts();
            if(selectedUser===username)selectUser(username);
        }
    });
}

// ===== FILE APPROVAL =====
function acceptFile(fileId){
    fetch('/api/chat/file/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:fileId})})
    .then(r=>r.json()).then(data=>{
        if(data.success){
            // Update local message
            var msgs=messages[selectedUser]||[];
            msgs.forEach(m=>{
                if(m.message_type==='file'&&m.file_info&&m.file_info.file_id===fileId){
                    m.file_info.status='accepted';
                    m.file_info.download_url=data.download_url;
                }
            });
            renderMessages();
            showModal('Thành công','File đã được chấp nhận','success');
        }else{
            showModal('Lỗi',data.error||'Không thể chấp nhận file','error');
        }
    });
}

function rejectFile(fileId){
    showConfirm('Từ chối file','Bạn chắc chắn muốn từ chối file này?',function(){
        fetch('/api/chat/file/reject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:fileId})})
        .then(r=>r.json()).then(data=>{
            if(data.success){
                var msgs=messages[selectedUser]||[];
                msgs.forEach(m=>{
                    if(m.message_type==='file'&&m.file_info&&m.file_info.file_id===fileId){
                        m.file_info.status='rejected';
                    }
                });
                renderMessages();
            }else{
                showModal('Lỗi',data.error||'Không thể từ chối','error');
            }
        });
    });
}

var saveDlg={fileId:'',filename:'',dest:'workspace',path:'',items:[]};

function showSaveDialog(fileId,filename){
    saveDlg.fileId=fileId;
    saveDlg.filename=filename;
    saveDlg.dest='workspace';
    saveDlg.path='';
    saveDlg.items=[];
    openSaveModal();
    loadSaveDlgFolder('');
}

function openSaveModal(){
    var ext=saveDlg.filename.split('.').pop().toLowerCase();
    var fileIcon='📄';
    if(['jpg','jpeg','png','gif','webp','svg'].includes(ext))fileIcon='🖼️';
    else if(['mp4','avi','mov','mkv'].includes(ext))fileIcon='🎬';
    else if(['mp3','wav','ogg'].includes(ext))fileIcon='🎵';
    else if(['pdf'].includes(ext))fileIcon='📕';
    else if(['doc','docx'].includes(ext))fileIcon='📘';
    else if(['xls','xlsx'].includes(ext))fileIcon='📗';
    else if(['zip','rar','7z'].includes(ext))fileIcon='📦';
    var html='<div class="svd">';
    html+='<div class="svd-file"><div class="svd-file-icon">'+fileIcon+'</div><div class="svd-file-detail"><div class="svd-file-name">'+escapeHtml(saveDlg.filename)+'</div><div class="svd-file-hint">Chọn vị trí lưu file</div></div></div>';
    html+='<div class="svd-tabs"><div class="svd-tab active" data-dest="workspace" onclick="switchSaveTab(\\'workspace\\')"><span class="svd-tab-icon">💼</span><span>Workspace</span></div>';
    html+='<div class="svd-tab" data-dest="s3" onclick="switchSaveTab(\\'s3\\')"><span class="svd-tab-icon">☁️</span><span>S3 Backup</span></div></div>';
    html+='<div class="svd-nav" id="save-breadcrumb"></div>';
    html+='<div class="svd-list" id="save-folder-list"></div>';
    html+='<div class="svd-dest"><span class="svd-dest-label">Lưu vào:</span><span class="svd-dest-path" id="save-dest-display">/</span></div>';
    html+='<div class="svd-foot"><button class="svd-btn svd-btn-new" onclick="createSaveFolder()"><span>+</span> Thư mục mới</button>';
    html+='<div class="svd-foot-right"><button class="svd-btn svd-btn-cancel" onclick="closeModal()">Hủy</button>';
    html+='<button class="svd-btn svd-btn-save" onclick="doSaveFile()">Lưu file</button></div></div></div>';
    html+='<style>';
    html+='.svd{width:100%;max-width:420px;display:flex;flex-direction:column;gap:12px;padding:16px}';
    html+='.svd-file{display:flex;align-items:center;gap:12px;padding:14px;background:linear-gradient(135deg,rgba(99,102,241,.12),rgba(139,92,246,.08));border-radius:10px;border:1px solid rgba(99,102,241,.2)}';
    html+='.svd-file-icon{width:44px;height:44px;display:flex;align-items:center;justify-content:center;font-size:24px;background:rgba(255,255,255,.08);border-radius:8px;flex-shrink:0}';
    html+='.svd-file-detail{flex:1;min-width:0;overflow:hidden}';
    html+='.svd-file-name{font-weight:600;color:#f1f5f9;font-size:13px;word-break:break-all;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}';
    html+='.svd-file-hint{font-size:11px;color:#64748b;margin-top:2px}';
    html+='.svd-tabs{display:flex;background:#0f172a;border-radius:8px;padding:3px;gap:3px}';
    html+='.svd-tab{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;padding:8px 12px;border-radius:6px;cursor:pointer;transition:all .2s;color:#64748b;font-size:12px;font-weight:500}';
    html+='.svd-tab:hover{color:#94a3b8;background:rgba(255,255,255,.03)}';
    html+='.svd-tab.active{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;box-shadow:0 2px 8px rgba(99,102,241,.3)}';
    html+='.svd-tab-icon{font-size:14px}';
    html+='.svd-nav{display:flex;align-items:center;gap:4px;padding:8px 10px;background:#0f172a;border-radius:6px;font-size:12px;flex-wrap:wrap;min-height:36px}';
    html+='.svd-nav-item{color:#94a3b8;cursor:pointer;padding:3px 6px;border-radius:4px;transition:all .15s;white-space:nowrap}';
    html+='.svd-nav-item:hover{background:rgba(99,102,241,.2);color:#a5b4fc}';
    html+='.svd-nav-sep{color:#334155;font-size:10px}';
    html+='.svd-list{min-height:120px;max-height:180px;overflow-y:auto;background:#0f172a;border-radius:8px;border:1px solid #1e293b;scrollbar-width:none;-ms-overflow-style:none}';
    html+='.svd-list::-webkit-scrollbar{display:none}';
    html+='.svd-item{display:flex;align-items:center;gap:10px;padding:10px 12px;cursor:pointer;transition:all .15s;border-left:2px solid transparent}';
    html+='.svd-item:hover{background:rgba(99,102,241,.1);border-left-color:#6366f1}';
    html+='.svd-item-icon{font-size:18px;opacity:.9}';
    html+='.svd-item-name{color:#e2e8f0;font-size:13px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}';
    html+='.svd-item-arrow{color:#475569;font-size:11px}';
    html+='.svd-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:120px;color:#475569;gap:6px;padding:20px}';
    html+='.svd-empty-icon{font-size:28px;opacity:.5}';
    html+='.svd-empty-text{font-size:12px}';
    html+='.svd-loading{display:flex;align-items:center;justify-content:center;min-height:120px;color:#64748b;font-size:13px}';
    html+='.svd-dest{display:flex;align-items:center;gap:8px;padding:10px 12px;background:linear-gradient(135deg,rgba(34,197,94,.1),rgba(16,185,129,.05));border-radius:6px;border:1px solid rgba(34,197,94,.2)}';
    html+='.svd-dest-label{color:#64748b;font-size:11px;font-weight:500;white-space:nowrap}';
    html+='.svd-dest-path{color:#4ade80;font-size:11px;font-family:monospace;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}';
    html+='.svd-foot{display:flex;justify-content:space-between;align-items:center;padding-top:12px;border-top:1px solid #1e293b;flex-wrap:wrap;gap:8px}';
    html+='.svd-foot-right{display:flex;gap:8px}';
    html+='.svd-btn{padding:8px 14px;border-radius:6px;font-size:12px;font-weight:500;cursor:pointer;transition:all .2s;border:none}';
    html+='.svd-btn-new{background:transparent;color:#94a3b8;border:1px dashed #334155;padding:8px 12px}';
    html+='.svd-btn-new:hover{border-color:#6366f1;color:#a5b4fc;background:rgba(99,102,241,.1)}';
    html+='.svd-btn-cancel{background:#1e293b;color:#94a3b8}';
    html+='.svd-btn-cancel:hover{background:#334155;color:#fff}';
    html+='.svd-btn-save{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;box-shadow:0 2px 8px rgba(99,102,241,.25)}';
    html+='.svd-btn-save:hover{box-shadow:0 3px 12px rgba(99,102,241,.35)}';
    html+='</style>';
    showModal('',html,'custom',true);
}

function switchSaveTab(dest){
    saveDlg.dest=dest;
    saveDlg.path='';
    document.querySelectorAll('.svd-tab').forEach(t=>t.classList.toggle('active',t.dataset.dest===dest));
    loadSaveDlgFolder('');
}

function loadSaveDlgFolder(path){
    saveDlg.path=path;
    var endpoint=saveDlg.dest==='workspace'?'/api/workspace/list':'/api/s3/list';
    document.getElementById('save-folder-list').innerHTML='<div class="svd-loading">Đang tải...</div>';
    fetch(endpoint+'?path='+encodeURIComponent(path)).then(r=>r.json()).then(data=>{
        saveDlg.items=data.items||[];
        renderSaveBreadcrumb();
        renderSaveFolderList();
        updateSaveDestDisplay();
    }).catch(()=>{
        document.getElementById('save-folder-list').innerHTML='<div class="svd-empty"><div class="svd-empty-icon">⚠️</div><div class="svd-empty-text">Không thể tải thư mục</div></div>';
    });
}

function updateSaveDestDisplay(){
    var el=document.getElementById('save-dest-display');
    if(el){
        var loc=saveDlg.dest==='workspace'?'Workspace':'S3 Backup';
        el.textContent=loc+':/'+saveDlg.path+(saveDlg.path?'/':'')+saveDlg.filename;
    }
}

function renderSaveBreadcrumb(){
    var bc=document.getElementById('save-breadcrumb');
    var parts=saveDlg.path?saveDlg.path.split('/'):[];
    var rootName=saveDlg.dest==='workspace'?'Workspace':'S3 Backup';
    var html='<span class="svd-nav-item" onclick="loadSaveDlgFolder(\\'\\')">🏠 '+rootName+'</span>';
    var accumulated='';
    parts.forEach((p,i)=>{
        accumulated+=(i>0?'/':'')+p;
        var accCopy=accumulated;
        html+='<span class="svd-nav-sep">›</span><span class="svd-nav-item" onclick="loadSaveDlgFolder(\\''+escapeHtml(accCopy)+'\\')">'+escapeHtml(p)+'</span>';
    });
    bc.innerHTML=html;
}

function renderSaveFolderList(){
    var list=document.getElementById('save-folder-list');
    var folders=saveDlg.items.filter(i=>i.type==='dir');
    if(!folders.length){
        list.innerHTML='<div class="svd-empty"><div class="svd-empty-icon">📂</div><div class="svd-empty-text">Thư mục trống</div></div>';
        return;
    }
    var html='';
    folders.forEach(f=>{
        var newPath=saveDlg.path?(saveDlg.path+'/'+f.name):f.name;
        html+='<div class="svd-item" onclick="loadSaveDlgFolder(\\''+escapeHtml(newPath)+'\\')">';
        html+='<span class="svd-item-icon">📁</span>';
        html+='<span class="svd-item-name">'+escapeHtml(f.name)+'</span>';
        html+='<span class="svd-item-arrow">›</span>';
        html+='</div>';
    });
    list.innerHTML=html;
}

function createSaveFolder(){
    var name=prompt('Tên thư mục mới:');
    if(!name||!name.trim())return;
    var endpoint=saveDlg.dest==='workspace'?'/api/workspace/mkdir':'/api/s3/mkdir';
    var newPath=saveDlg.path?(saveDlg.path+'/'+name.trim()):name.trim();
    fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:newPath})})
    .then(r=>r.json()).then(d=>{
        if(d.error){showModal('Lỗi',d.error,'error');return;}
        loadSaveDlgFolder(saveDlg.path);
    });
}

function doSaveFile(){
    closeModal();
    fetch('/api/chat/file/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:saveDlg.fileId,dest:saveDlg.dest,dest_path:saveDlg.path})})
    .then(r=>r.json()).then(data=>{
        if(data.success){
            var loc=saveDlg.dest==='workspace'?'Workspace':'S3 Backup';
            var path=saveDlg.path?(saveDlg.path+'/'+saveDlg.filename):saveDlg.filename;
            showModal('Thành công','Đã lưu vào '+loc+':<br><code style="word-break:break-all">'+escapeHtml(path)+'</code>','success');
        }else{
            showModal('Lỗi',data.error||'Không thể lưu file','error');
        }
    });
}

// ===== MESSAGE RECALL =====
function recallMessage(msgId,idx){
    showConfirm('Thu hồi tin nhắn','Bạn muốn thu hồi tin nhắn này?',function(){
        fetch('/api/chat/message/recall',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message_id:msgId,with_user:selectedUser})})
        .then(r=>r.json()).then(data=>{
            if(data.success){
                var msgs=messages[selectedUser]||[];
                if(msgs[idx])msgs[idx].recalled=true;
                renderMessages();
                // Notify via socket
                socket.emit('message_recalled',{message_id:msgId,to_user:selectedUser});
            }else{
                showModal('Lỗi',data.error||'Không thể thu hồi','error');
            }
        });
    });
}

socket.on('message_recalled',function(data){
    var msgs=messages[data.from_user]||[];
    msgs.forEach(m=>{
        if((m._id||m.id)===data.message_id)m.recalled=true;
    });
    if(selectedUser===data.from_user)renderMessages();
});

// ===== UTILS =====
function scrollToBottom(){
    var el=document.getElementById('chat-messages');
    if(el)setTimeout(()=>el.scrollTop=el.scrollHeight,50);
}

function formatTime(iso){
    if(!iso)return'';
    var d=new Date(iso);
    var now=new Date();
    if(d.toDateString()===now.toDateString()){
        return d.toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit'});
    }
    return d.toLocaleDateString('vi-VN',{day:'2-digit',month:'2-digit'});
}

function formatSize(b){if(b<1024)return b+' B';if(b<1048576)return(b/1024).toFixed(1)+' KB';return(b/1048576).toFixed(1)+' MB';}
function escapeHtml(t){return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,'&apos;');}

// ===== MODAL SYSTEM =====
var modalOverlay=null;
function createModalOverlay(){
    if(!modalOverlay){
        modalOverlay=document.createElement('div');
        modalOverlay.className='chat-modal-overlay';
        modalOverlay.innerHTML='<div class="chat-modal-box"></div>';
        modalOverlay.onclick=function(e){if(e.target===modalOverlay)closeModal();};
        document.body.appendChild(modalOverlay);
    }
    return modalOverlay;
}
function showModal(title,content,type,noWrap){
    var o=createModalOverlay();
    var m=o.querySelector('.chat-modal-box');
    var icons={error:'❌',success:'✅',info:'ℹ️',warning:'⚠️',custom:''};
    var icon=icons[type]||icons.info;
    m.className='chat-modal-box'+(type==='custom'?' cmb-custom':'');
    if(type==='custom'||noWrap){
        m.innerHTML=content;
    }else{
        m.innerHTML='<div class="cmb-header"><span class="cmb-icon">'+icon+'</span><span class="cmb-title">'+escapeHtml(title)+'</span><button class="cmb-close" onclick="closeModal()">×</button></div><div class="cmb-body">'+content+'</div><div class="cmb-footer"><button class="btn btn-primary" onclick="closeModal()">OK</button></div>';
    }
    o.classList.add('show');
}
function showConfirm(title,content,onYes,onNo){
    var o=createModalOverlay();
    var m=o.querySelector('.chat-modal-box');
    m.innerHTML='<div class="cmb-header"><span class="cmb-icon">⚠️</span><span class="cmb-title">'+escapeHtml(title)+'</span><button class="cmb-close" onclick="closeModal()">×</button></div><div class="cmb-body">'+content+'</div><div class="cmb-footer"><button class="btn btn-secondary" onclick="closeModal();if(window._cmbNo)window._cmbNo()">Hủy</button><button class="btn btn-primary" onclick="closeModal();if(window._cmbYes)window._cmbYes()">Xác nhận</button></div>';
    window._cmbYes=onYes;window._cmbNo=onNo;
    o.classList.add('show');
}
function closeModal(){if(modalOverlay)modalOverlay.classList.remove('show');}
(function(){var s=document.createElement('style');s.textContent=`
.chat-modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;z-index:9999;opacity:0;pointer-events:none;transition:opacity .2s;padding:16px;box-sizing:border-box}
.chat-modal-overlay.show{opacity:1;pointer-events:auto}
.chat-modal-box{background:linear-gradient(145deg,#1e293b,#0f172a);border-radius:14px;border:1px solid rgba(99,102,241,.2);box-shadow:0 20px 50px rgba(0,0,0,.5);max-width:min(400px,calc(100% - 32px));max-height:calc(100% - 32px);overflow:hidden;animation:cmbIn .2s ease;display:flex;flex-direction:column}
.chat-modal-box.cmb-custom{max-width:min(450px,calc(100% - 32px));border-radius:12px}
@keyframes cmbIn{from{transform:scale(.9) translateY(20px);opacity:0}to{transform:scale(1) translateY(0);opacity:1}}
.cmb-header{display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid #334155;background:rgba(0,0,0,.2);flex-shrink:0}
.cmb-icon{font-size:20px}
.cmb-title{flex:1;font-size:15px;font-weight:600;color:#f1f5f9}
.cmb-close{background:none;border:none;color:#64748b;font-size:24px;cursor:pointer;padding:0;line-height:1;transition:color .15s}
.cmb-close:hover{color:#ef4444}
.cmb-body{padding:20px;overflow-y:auto;flex:1;min-height:0;scrollbar-width:none;-ms-overflow-style:none}
.cmb-body::-webkit-scrollbar{display:none}
.cmb-footer{display:flex;justify-content:flex-end;gap:10px;padding:14px 20px;border-top:1px solid #334155;background:rgba(0,0,0,.1);flex-shrink:0}
`;document.head.appendChild(s);})();

// Start
init();
</script></body></html>"""

# ===========================================
# EMBED_TODO - Task/Notes Management
# ===========================================

EMBED_TODO = EMBED_CSS + """<!DOCTYPE html><html><head><title>Todo</title>
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
.todo-container{display:flex;height:calc(100vh - 24px);gap:12px}
.todo-sidebar{width:220px;background:#1e293b;border-radius:10px;border:1px solid #334155;display:flex;flex-direction:column;overflow:hidden}
.todo-sidebar .tabs{display:flex;flex-direction:column;padding:8px}
.todo-sidebar .tab{padding:12px 14px;border-radius:8px;cursor:pointer;font-size:13px;display:flex;align-items:center;gap:10px;margin-bottom:4px}
.todo-sidebar .tab:hover{background:#334155}
.todo-sidebar .tab.active{background:rgba(99,102,241,.2);color:#818cf8}
.todo-sidebar .tab .count{background:#334155;padding:2px 8px;border-radius:10px;font-size:11px;margin-left:auto}
.todo-sidebar .tab.active .count{background:#6366f1;color:#fff}
.todo-main{flex:1;background:#1e293b;border-radius:10px;border:1px solid #334155;display:flex;flex-direction:column;overflow:hidden}
.todo-header{padding:14px 16px;border-bottom:1px solid #334155;display:flex;align-items:center;justify-content:space-between}
.todo-header h2{font-size:16px;margin:0}
.todo-filters{display:flex;gap:8px;align-items:center}
.todo-filters select{background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:6px 10px;border-radius:6px;font-size:12px}
.todo-list{flex:1;overflow-y:auto;padding:12px}
.todo-item{background:#0f172a;border:1px solid #334155;border-radius:10px;padding:14px;margin-bottom:10px;cursor:pointer;transition:all .2s}
.todo-item:hover{border-color:#6366f1}
.todo-item.completed{opacity:0.6}
.todo-item .header{display:flex;align-items:flex-start;gap:10px}
.todo-item .checkbox{width:20px;height:20px;border:2px solid #475569;border-radius:50%;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:12px}
.todo-item .checkbox:hover{border-color:#6366f1}
.todo-item.completed .checkbox{background:#10b981;border-color:#10b981;color:#fff}
.todo-item .title{font-size:14px;font-weight:500;flex:1}
.todo-item .meta{display:flex;gap:8px;margin-top:8px;margin-left:30px;flex-wrap:wrap}
.todo-item .tag{font-size:11px;padding:2px 8px;border-radius:4px}
.todo-item .tag.priority-high{background:rgba(239,68,68,.2);color:#ef4444}
.todo-item .tag.priority-medium{background:rgba(245,158,11,.2);color:#f59e0b}
.todo-item .tag.priority-low{background:rgba(99,102,241,.2);color:#818cf8}
.todo-item .due{font-size:11px;color:#94a3b8}
.todo-item .due.overdue{color:#ef4444}
.todo-item .assignee{font-size:11px;color:#10b981}
.todo-empty{text-align:center;padding:40px;color:#64748b}
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:1000}
.modal-overlay.show{display:flex}
.modal{background:#1e293b;border-radius:12px;border:1px solid #334155;width:500px;max-height:90vh;display:flex;flex-direction:column}
.modal-header{padding:16px;border-bottom:1px solid #334155;display:flex;align-items:center;justify-content:space-between}
.modal-header h3{margin:0;font-size:16px}
.modal-header .close-btn{background:transparent;border:none;color:#94a3b8;font-size:20px;cursor:pointer}
.modal-body{padding:16px;flex:1;overflow-y:auto}
.modal-footer{padding:16px;border-top:1px solid #334155;display:flex;justify-content:space-between;gap:8px}
.form-group{margin-bottom:14px}
.form-group label{display:block;font-size:13px;color:#94a3b8;margin-bottom:6px}
.form-group input,.form-group textarea,.form-group select{width:100%;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:10px 12px;color:#e2e8f0;font-size:13px}
.form-group input:focus,.form-group textarea:focus,.form-group select:focus{outline:none;border-color:#6366f1}
.form-group textarea{min-height:80px;resize:vertical}
.form-row{display:flex;gap:12px}
.form-row .form-group{flex:1}
.comments-section{margin-top:16px;border-top:1px solid #334155;padding-top:16px}
.comments-section h4{font-size:13px;margin-bottom:10px;color:#94a3b8}
.comment{background:#0f172a;border-radius:8px;padding:10px;margin-bottom:8px}
.comment .header{display:flex;justify-content:space-between;font-size:11px;color:#64748b;margin-bottom:4px}
.comment .text{font-size:13px}
.add-comment{display:flex;gap:8px}
.add-comment input{flex:1}
.notification{position:fixed;bottom:20px;right:20px;background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px 18px;box-shadow:0 10px 40px rgba(0,0,0,.4);z-index:2000;display:none;max-width:300px}
.notification.show{display:block;animation:slideIn .3s ease}
@keyframes slideIn{from{transform:translateY(20px);opacity:0}to{transform:translateY(0);opacity:1}}
.notification .icon{font-size:20px;margin-bottom:8px}
.notification .title{font-weight:600;font-size:14px;margin-bottom:4px}
.notification .body{font-size:12px;color:#94a3b8}
</style>
</head><body>
<div class="container" style="padding:12px;height:100vh;overflow:hidden;box-sizing:border-box">
    <div class="todo-container">
        <div class="todo-sidebar">
            <div class="tabs">
                <div class="tab active" data-tab="my" onclick="switchTab('my')"><span>&#128203;</span> My Tasks <span class="count" id="count-my">0</span></div>
                <div class="tab" data-tab="assigned" onclick="switchTab('assigned')"><span>&#128229;</span> Assigned to Me <span class="count" id="count-assigned">0</span></div>
                <div class="tab" data-tab="created" onclick="switchTab('created')"><span>&#128228;</span> Created by Me <span class="count" id="count-created">0</span></div>
            </div>
        </div>
        <div class="todo-main">
            <div class="todo-header">
                <h2 id="current-tab-title">My Tasks</h2>
                <div class="todo-filters">
                    <select id="filter-status" onchange="loadTasks()">
                        <option value="">All Status</option>
                        <option value="pending">Pending</option>
                        <option value="in_progress">In Progress</option>
                        <option value="completed">Completed</option>
                    </select>
                    <select id="filter-priority" onchange="loadTasks()">
                        <option value="">All Priority</option>
                        <option value="high">High</option>
                        <option value="medium">Medium</option>
                        <option value="low">Low</option>
                    </select>
                    <button class="btn btn-success btn-sm" onclick="showNewTask()">+ New Task</button>
                </div>
            </div>
            <div class="todo-list" id="todo-list"></div>
        </div>
    </div>
</div>

<div class="modal-overlay" id="task-modal">
    <div class="modal">
        <div class="modal-header">
            <h3 id="modal-title">New Task</h3>
            <button class="close-btn" onclick="hideModal()">&times;</button>
        </div>
        <div class="modal-body">
            <input type="hidden" id="task-id">
            <div class="form-group">
                <label>Title *</label>
                <input type="text" id="task-title" placeholder="Task title...">
            </div>
            <div class="form-group">
                <label>Description</label>
                <textarea id="task-desc" placeholder="Task description..."></textarea>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>Assignee</label>
                    <select id="task-assignee">
                        <option value="">Self (My Task)</option>
                        <option value="__all__">Everyone</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Priority</label>
                    <select id="task-priority">
                        <option value="medium">Medium</option>
                        <option value="high">High</option>
                        <option value="low">Low</option>
                    </select>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>Status</label>
                    <select id="task-status">
                        <option value="pending">Pending</option>
                        <option value="in_progress">In Progress</option>
                        <option value="completed">Completed</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Due Date</label>
                    <input type="date" id="task-due">
                </div>
            </div>
            <div class="comments-section" id="comments-section" style="display:none">
                <h4>Comments</h4>
                <div id="comments-list"></div>
                <div class="add-comment">
                    <input type="text" id="new-comment" placeholder="Add a comment...">
                    <button class="btn btn-primary btn-sm" onclick="addComment()">Send</button>
                </div>
            </div>
        </div>
        <div class="modal-footer">
            <button class="btn btn-danger btn-sm" id="delete-task-btn" style="display:none" onclick="deleteTask()">Delete</button>
            <div style="display:flex;gap:8px">
                <button class="btn btn-secondary" onclick="hideModal()">Cancel</button>
                <button class="btn btn-primary" onclick="saveTask()">Save</button>
            </div>
        </div>
    </div>
</div>

<div class="notification" id="notification">
    <div class="icon" id="notif-icon">&#128203;</div>
    <div class="title" id="notif-title"></div>
    <div class="body" id="notif-body"></div>
</div>

<script>
var socket=io();
var currentUser='{{ username }}';
var currentTab='my';
var tasks=[];
var users=[];

function init(){
    loadUsers();
    loadTasks();
    setupSocket();
}

function loadUsers(){
    fetch('/api/todos/users').then(r=>r.json()).then(d=>{
        users=d.users||[];
        var sel=document.getElementById('task-assignee');
        sel.innerHTML='<option value="">Self (My Task)</option><option value="__all__">Everyone</option>';
        users.forEach(u=>{
            if(u!==currentUser)sel.innerHTML+='<option value="'+u+'">'+u+'</option>';
        });
    });
}

function loadTasks(){
    var status=document.getElementById('filter-status').value;
    var priority=document.getElementById('filter-priority').value;
    var url='/api/todos?tab='+currentTab;
    if(status)url+='&status='+status;
    if(priority)url+='&priority='+priority;
    fetch(url).then(r=>r.json()).then(d=>{
        tasks=d.tasks||[];
        renderTasks();
        updateCounts(d.counts||{});
    });
}

function updateCounts(counts){
    document.getElementById('count-my').textContent=counts.my||0;
    document.getElementById('count-assigned').textContent=counts.assigned||0;
    document.getElementById('count-created').textContent=counts.created||0;
}

function switchTab(tab){
    currentTab=tab;
    document.querySelectorAll('.todo-sidebar .tab').forEach(t=>t.classList.remove('active'));
    document.querySelector('.tab[data-tab="'+tab+'"]').classList.add('active');
    var titles={'my':'My Tasks','assigned':'Assigned to Me','created':'Created by Me'};
    document.getElementById('current-tab-title').textContent=titles[tab];
    loadTasks();
}

function renderTasks(){
    var list=document.getElementById('todo-list');
    if(!tasks.length){
        list.innerHTML='<div class="todo-empty"><div style="font-size:40px;margin-bottom:10px">&#128203;</div>No tasks found</div>';
        return;
    }
    var html='';
    tasks.forEach(t=>{
        var isCompleted=t.status==='completed';
        var priorityClass='priority-'+t.priority;
        var dueClass='';
        if(t.due_date&&!isCompleted){
            var due=new Date(t.due_date);
            var today=new Date();today.setHours(0,0,0,0);
            if(due<today)dueClass='overdue';
        }
        html+='<div class="todo-item'+(isCompleted?' completed':'')+'" onclick="showTask(\\''+t._id+'\\')">';
        html+='<div class="header">';
        html+='<div class="checkbox" onclick="event.stopPropagation();toggleStatus(\\''+t._id+'\\',\\''+t.status+'\\')">'+(isCompleted?'&#10003;':'')+'</div>';
        html+='<div class="title">'+escapeHtml(t.title)+'</div>';
        html+='</div>';
        html+='<div class="meta">';
        html+='<span class="tag '+priorityClass+'">'+t.priority+'</span>';
        if(t.due_date)html+='<span class="due '+dueClass+'">Due: '+formatDate(t.due_date)+'</span>';
        if(t.assignee&&t.assignee!==currentUser)html+='<span class="assignee">To: '+t.assignee+'</span>';
        if(t.creator&&t.creator!==currentUser)html+='<span class="assignee">From: '+t.creator+'</span>';
        html+='</div></div>';
    });
    list.innerHTML=html;
}

function showNewTask(){
    document.getElementById('modal-title').textContent='New Task';
    document.getElementById('task-id').value='';
    document.getElementById('task-title').value='';
    document.getElementById('task-desc').value='';
    document.getElementById('task-assignee').value='';
    document.getElementById('task-priority').value='medium';
    document.getElementById('task-status').value='pending';
    document.getElementById('task-due').value='';
    document.getElementById('comments-section').style.display='none';
    document.getElementById('delete-task-btn').style.display='none';
    document.getElementById('task-modal').classList.add('show');
}

function showTask(id){
    var t=tasks.find(x=>x._id===id);
    if(!t)return;
    document.getElementById('modal-title').textContent='Edit Task';
    document.getElementById('task-id').value=t._id;
    document.getElementById('task-title').value=t.title;
    document.getElementById('task-desc').value=t.description||'';
    document.getElementById('task-assignee').value=t.assignee||'';
    document.getElementById('task-priority').value=t.priority;
    document.getElementById('task-status').value=t.status;
    document.getElementById('task-due').value=t.due_date?t.due_date.split('T')[0]:'';
    document.getElementById('delete-task-btn').style.display=t.creator===currentUser?'block':'none';
    // Comments
    var canEdit=t.creator===currentUser||t.assignee===currentUser||t.assignee==='__all__';
    if(canEdit){
        document.getElementById('comments-section').style.display='block';
        renderComments(t.comments||[]);
    }else{
        document.getElementById('comments-section').style.display='none';
    }
    document.getElementById('task-modal').classList.add('show');
}

function renderComments(comments){
    var html='';
    comments.forEach(c=>{
        html+='<div class="comment"><div class="header"><span>'+c.user+'</span><span>'+formatDateTime(c.created_at)+'</span></div><div class="text">'+escapeHtml(c.text)+'</div></div>';
    });
    document.getElementById('comments-list').innerHTML=html||'<div style="color:#64748b;font-size:12px">No comments yet</div>';
}

function hideModal(){
    document.getElementById('task-modal').classList.remove('show');
}

function saveTask(){
    var id=document.getElementById('task-id').value;
    var data={
        title:document.getElementById('task-title').value.trim(),
        description:document.getElementById('task-desc').value.trim(),
        assignee:document.getElementById('task-assignee').value,
        priority:document.getElementById('task-priority').value,
        status:document.getElementById('task-status').value,
        due_date:document.getElementById('task-due').value||null
    };
    if(!data.title){showNotification('&#9888;','Error','Title is required');return;}
    var url=id?'/api/todos/'+id:'/api/todos';
    var method=id?'PUT':'POST';
    fetch(url,{method:method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
    .then(r=>r.json()).then(d=>{
        if(d.error){showNotification('&#9888;','Error',d.error);return;}
        showNotification('&#10004;','Success',id?'Task updated':'Task created');
        hideModal();
        loadTasks();
    });
}

function deleteTask(){
    var id=document.getElementById('task-id').value;
    if(!id||!confirm('Delete this task?'))return;
    fetch('/api/todos/'+id,{method:'DELETE'}).then(r=>r.json()).then(d=>{
        hideModal();
        loadTasks();
    });
}

function toggleStatus(id,current){
    var newStatus=current==='completed'?'pending':'completed';
    fetch('/api/todos/'+id+'/status',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:newStatus})})
    .then(r=>r.json()).then(d=>loadTasks());
}

function addComment(){
    var id=document.getElementById('task-id').value;
    var text=document.getElementById('new-comment').value.trim();
    if(!id||!text)return;
    fetch('/api/todos/'+id+'/comment',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})})
    .then(r=>r.json()).then(d=>{
        document.getElementById('new-comment').value='';
        // Reload task to get updated comments
        fetch('/api/todos/'+id).then(r=>r.json()).then(t=>{
            if(t.task)renderComments(t.task.comments||[]);
        });
    });
}

function setupSocket(){
    socket.on('task_assigned',function(data){
        showNotification('&#128229;','New Task Assigned',data.title+' from '+data.from_user);
        loadTasks();
    });
    socket.on('task_updated',function(data){
        loadTasks();
    });
    socket.on('task_completed',function(data){
        showNotification('&#9989;','Task Completed',data.title+' by '+data.by_user);
        loadTasks();
    });
    socket.on('comment_added',function(data){
        showNotification('&#128172;','New Comment',data.user+' commented on '+data.task_title);
        loadTasks();
    });
}

function showNotification(icon,title,body){
    var el=document.getElementById('notification');
    document.getElementById('notif-icon').innerHTML=icon;
    document.getElementById('notif-title').textContent=title;
    document.getElementById('notif-body').textContent=body;
    el.classList.add('show');
    setTimeout(function(){el.classList.remove('show');},5000);
}

function escapeHtml(s){return s?s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'):'';}
function formatDate(d){if(!d)return'';var dt=new Date(d);return dt.toLocaleDateString('vi-VN');}
function formatDateTime(d){if(!d)return'';var dt=new Date(d);return dt.toLocaleDateString('vi-VN')+' '+dt.toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit'});}

init();
</script></body></html>"""

# ===========================================
# EMBED_MUSIC_ROOM - Listen to music together
# ===========================================

EMBED_MUSIC_ROOM = EMBED_CSS + """<!DOCTYPE html><html><head><title>Music Room</title>
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
.music-container{max-width:460px;margin:0 auto;padding:10px;height:100vh;box-sizing:border-box;display:flex;flex-direction:column;overflow:hidden}
.toast-container{position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px}
.toast{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px 18px;box-shadow:0 8px 24px rgba(0,0,0,.4);display:flex;align-items:center;gap:12px;animation:slideIn .3s ease;max-width:320px}
.toast.success{border-color:#10b981;background:linear-gradient(135deg,#1e293b,#064e3b)}
.toast.error{border-color:#ef4444;background:linear-gradient(135deg,#1e293b,#7f1d1d)}
.toast.info{border-color:#6366f1;background:linear-gradient(135deg,#1e293b,#312e81)}
.toast .icon{font-size:20px}
.toast .message{flex:1;font-size:13px}
.toast .close{background:none;border:none;color:#64748b;cursor:pointer;font-size:16px;padding:0}
.toast .close:hover{color:#fff}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
.room-list{flex:1;overflow-y:auto}
.room-item{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px;margin-bottom:10px;cursor:pointer;transition:all .2s}
.room-item:hover{border-color:#6366f1}
.room-item .title{font-size:15px;font-weight:600;margin-bottom:4px}
.room-item .info{font-size:12px;color:#94a3b8;display:flex;gap:12px}
.room-create{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:16px;margin-bottom:16px}
.room-create h3{margin:0 0 12px 0;font-size:14px}
.room-join{display:flex;gap:8px;margin-bottom:16px}
.room-join input{flex:1;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:10px;color:#e2e8f0;font-size:13px;text-transform:uppercase;letter-spacing:2px}
.room-join input:focus{outline:none;border-color:#6366f1}
.player-view{display:none;flex-direction:column;height:100%}
.player-view.show{display:flex}
.player-header{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid #334155}
.player-header .room-info{display:flex;align-items:center;gap:12px}
.player-header .room-title{font-size:16px;font-weight:600}
.player-header .room-code{background:#6366f1;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600;letter-spacing:1px;cursor:pointer}
.player-header .room-code:hover{background:#4f46e5}
.now-playing{background:#1e293b;border-radius:10px;padding:12px;margin:10px 0;text-align:center}
.now-playing .icon{font-size:40px;margin-bottom:6px;opacity:0.5}
.now-playing .track-name{font-size:15px;font-weight:600;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.now-playing .track-info{font-size:11px;color:#94a3b8}
.progress-container{margin:8px 0}
.progress-bar{height:5px;background:#334155;border-radius:3px;cursor:pointer;position:relative}
.progress-fill{height:100%;background:linear-gradient(90deg,#6366f1,#8b5cf6);border-radius:3px;width:0%}
.progress-bar:hover .progress-fill{background:linear-gradient(90deg,#818cf8,#a78bfa)}
.time-display{display:flex;justify-content:space-between;font-size:11px;color:#94a3b8;margin-top:6px}
.controls{display:flex;align-items:center;justify-content:center;gap:12px;margin:8px 0}
.controls button{background:transparent;border:none;color:#e2e8f0;font-size:20px;cursor:pointer;padding:6px;border-radius:50%;transition:all .2s}
.controls button:hover{background:#334155;transform:scale(1.1)}
.controls button.active{color:#6366f1}
.controls .play-btn{background:#6366f1;width:48px;height:48px;border-radius:50%;font-size:22px;display:flex;align-items:center;justify-content:center}
.controls .play-btn:hover{background:#4f46e5;transform:scale(1.05)}
.secondary-controls{display:flex;align-items:center;justify-content:center;gap:20px;margin-bottom:8px}
.secondary-controls button{background:transparent;border:none;color:#94a3b8;font-size:16px;cursor:pointer;padding:4px}
.secondary-controls button:hover{color:#e2e8f0}
.secondary-controls button.active{color:#6366f1}
.playlist{flex:1;background:#1e293b;border-radius:8px;border:1px solid #334155;display:flex;flex-direction:column;overflow:hidden;min-height:120px}
.playlist-header{padding:12px 14px;border-bottom:1px solid #334155;display:flex;align-items:center;justify-content:space-between}
.playlist-header h4{margin:0;font-size:13px}
.playlist-actions{display:flex;gap:6px}
.playlist-actions button{background:#334155;border:none;color:#94a3b8;padding:6px 10px;border-radius:6px;font-size:11px;cursor:pointer}
.playlist-actions button:hover{background:#475569;color:#fff}
.playlist-list{flex:1;overflow-y:auto}
.playlist-item{display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;border-bottom:1px solid rgba(51,65,85,.5)}
.playlist-item:hover{background:#334155}
.playlist-item.playing{background:rgba(99,102,241,.2)}
.playlist-item .number{width:24px;text-align:center;color:#64748b;font-size:12px}
.playlist-item.playing .number{color:#6366f1}
.playlist-item .name{flex:1;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.playlist-item .duration{color:#64748b;font-size:12px}
.playlist-item .remove{background:transparent;border:none;color:#64748b;cursor:pointer;padding:4px;font-size:14px;opacity:0}
.playlist-item:hover .remove{opacity:1}
.playlist-item .remove:hover{color:#ef4444}
.members{background:#1e293b;border-radius:8px;border:1px solid #334155;margin-top:8px;max-height:100px;overflow-y:auto;flex-shrink:0}
.members-header{padding:10px 14px;border-bottom:1px solid #334155;display:flex;align-items:center;justify-content:space-between;font-size:13px}
.member-item{display:flex;align-items:center;gap:8px;padding:8px 14px;font-size:12px}
.member-item .dot{width:8px;height:8px;background:#10b981;border-radius:50%}
.member-item .host-badge{background:#f59e0b;color:#000;padding:1px 6px;border-radius:4px;font-size:10px;margin-left:auto}
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:1000}
.modal-overlay.show{display:flex}
.modal{background:#1e293b;border-radius:12px;border:1px solid #334155;width:400px;max-height:70vh;display:flex;flex-direction:column}
.modal-header{padding:14px 16px;border-bottom:1px solid #334155;display:flex;align-items:center;justify-content:space-between}
.modal-header h3{margin:0;font-size:15px}
.modal-header .close-btn{background:transparent;border:none;color:#94a3b8;font-size:18px;cursor:pointer}
.modal-body{padding:16px;flex:1;overflow-y:auto}
.modal-footer{padding:14px 16px;border-top:1px solid #334155;display:flex;justify-content:flex-end;gap:8px}
.s3-file{display:flex;align-items:center;gap:10px;padding:10px;border-radius:6px;cursor:pointer}
.s3-file:hover{background:#334155}
.s3-file.selected{background:rgba(99,102,241,.2);border:1px solid #6366f1}
.s3-file .icon{font-size:20px}
.s3-file .name{flex:1;font-size:13px}
.upload-area{border:2px dashed #334155;border-radius:10px;padding:30px;text-align:center;cursor:pointer;transition:all .2s;margin-bottom:12px}
.upload-area:hover{border-color:#6366f1;background:rgba(99,102,241,.1)}
.upload-area .icon{font-size:32px;margin-bottom:8px;opacity:0.5}
.upload-area .text{font-size:13px;color:#94a3b8}
.list-view{display:none}
.list-view.show{display:block}
</style>
</head><body>
<div class="music-container">
    <div class="list-view show" id="list-view">
        <div class="room-create">
            <h3>&#127925; Create Music Room</h3>
            <div style="display:flex;gap:8px">
                <input type="text" id="new-room-title" placeholder="Room name..." style="flex:1;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:10px;color:#e2e8f0;font-size:13px">
                <button class="btn btn-success" onclick="createRoom()">Create</button>
            </div>
        </div>
        <div class="room-join">
            <input type="text" id="join-code" placeholder="Enter code..." maxlength="6">
            <button class="btn btn-primary" onclick="joinByCode()">Join</button>
        </div>
        <h3 style="font-size:14px;margin-bottom:12px">Active Rooms</h3>
        <div class="room-list" id="room-list"></div>
    </div>

    <div class="player-view" id="player-view">
        <div class="player-header">
            <button class="btn btn-secondary btn-sm" onclick="leaveRoom()">&#9664; Leave</button>
            <div class="room-info">
                <span class="room-title" id="room-title">Music Room</span>
                <span class="room-code" id="room-code" onclick="copyCode()" title="Click to copy">ABC123</span>
            </div>
            <button class="btn btn-secondary btn-sm" onclick="showInvite()">&#128101; Invite</button>
        </div>

        <div class="now-playing">
            <div class="icon" id="playing-icon">&#127925;</div>
            <div class="track-name" id="track-name">No track playing</div>
            <div class="track-info" id="track-info">Add songs to playlist</div>
        </div>

        <div class="progress-container">
            <div class="progress-bar" id="progress-bar" onclick="seekTo(event)">
                <div class="progress-fill" id="progress-fill" style="width:0%"></div>
            </div>
            <div class="time-display">
                <span id="current-time">0:00</span>
                <span id="total-time">0:00</span>
            </div>
        </div>

        <div class="controls">
            <button onclick="prevTrack()" title="Previous">&#9198;</button>
            <button class="play-btn" id="play-btn" onclick="togglePlay()">&#9658;</button>
            <button onclick="nextTrack()" title="Next">&#9197;</button>
        </div>

        <div class="secondary-controls">
            <button id="shuffle-btn" onclick="toggleShuffle()" title="Shuffle">&#128256;</button>
            <button id="repeat-btn" onclick="toggleRepeat()" title="Repeat">&#128257;</button>
        </div>

        <div class="playlist">
            <div class="playlist-header">
                <h4>&#127926; Playlist</h4>
                <div class="playlist-actions">
                    <button onclick="showAddTrack()">+ Add</button>
                    <button onclick="showImportS3()">Import S3</button>
                </div>
            </div>
            <div class="playlist-list" id="playlist"></div>
        </div>

        <div class="members">
            <div class="members-header"><span id="member-count">Members (0)</span></div>
            <div id="members-list"></div>
        </div>
    </div>
</div>

<div class="modal-overlay" id="add-modal">
    <div class="modal">
        <div class="modal-header">
            <h3>&#127925; Add Track</h3>
            <button class="close-btn" onclick="hideAddModal()">&times;</button>
        </div>
        <div class="modal-body">
            <div class="upload-area" onclick="document.getElementById('file-input').click()">
                <div class="icon">&#128190;</div>
                <div class="text">Click to upload audio file<br><small>MP3, WAV, OGG, FLAC</small></div>
            </div>
            <input type="file" id="file-input" accept="audio/*" style="display:none" onchange="uploadTrack()">
            <div id="upload-progress" style="display:none;margin-top:12px">
                <div style="font-size:12px;margin-bottom:6px">Uploading...</div>
                <div style="height:4px;background:#334155;border-radius:2px"><div id="upload-bar" style="height:100%;background:#6366f1;border-radius:2px;width:0%;transition:width .3s"></div></div>
            </div>
        </div>
    </div>
</div>

<div class="modal-overlay" id="s3-modal">
    <div class="modal">
        <div class="modal-header">
            <h3>&#9729; Import from S3</h3>
            <button class="close-btn" onclick="hideS3Modal()">&times;</button>
        </div>
        <div class="modal-body" id="s3-files"></div>
        <div class="modal-footer">
            <button class="btn btn-secondary" onclick="hideS3Modal()">Cancel</button>
            <button class="btn btn-primary" onclick="importSelected()">Import</button>
        </div>
    </div>
</div>

<audio id="audio" style="display:none"></audio>
<div class="toast-container" id="toast-container"></div>

<script>
function showToast(message,type='info',duration=3000){
    var container=document.getElementById('toast-container');
    var toast=document.createElement('div');
    toast.className='toast '+type;
    var icons={success:'&#10004;',error:'&#10006;',info:'&#8505;'};
    toast.innerHTML='<span class="icon">'+icons[type]+'</span><span class="message">'+message+'</span><button class="close" onclick="this.parentElement.remove()">&times;</button>';
    container.appendChild(toast);
    if(duration>0)setTimeout(function(){toast.remove();},duration);
}

var socket=io();
var currentUser='{{ username }}';
var currentRoom=null;
var roomState={playlist:[],current_track:0,current_time:0,is_playing:false,shuffle:false,repeat:'none',control_mode:'host_only',host_user:'',members:[]};
var audio=document.getElementById('audio');
var isHost=false;
var canControl=false;
var selectedS3Files=[];
var syncInterval=null;

function init(){
    loadRooms();
    setupSocket();
    setupAudio();
}

function loadRooms(){
    fetch('/api/music/rooms').then(r=>r.json()).then(d=>{
        var list=document.getElementById('room-list');
        if(!d.rooms||!d.rooms.length){
            list.innerHTML='<div style="text-align:center;padding:30px;color:#64748b">No active rooms</div>';
            return;
        }
        var html='';
        d.rooms.forEach(r=>{
            html+='<div class="room-item" onclick="joinRoom(\\''+r._id+'\\')">';
            html+='<div class="title">'+escapeHtml(r.title)+'</div>';
            html+='<div class="info"><span>&#128101; '+r.member_count+'</span><span>Host: '+r.host_user+'</span></div>';
            html+='</div>';
        });
        list.innerHTML=html;
    });
}

function createRoom(){
    var title=document.getElementById('new-room-title').value.trim()||'Music Room';
    socket.emit('create_music_room',{title:title,control_mode:'everyone'});
}

function joinByCode(){
    var code=document.getElementById('join-code').value.trim().toUpperCase();
    if(code.length!==6){showToast('Enter 6-character code','error');return;}
    socket.emit('join_music_room',{code:code});
}

function joinRoom(roomId){
    socket.emit('join_music_room',{room_id:roomId});
}

function leaveRoom(){
    if(currentRoom)socket.emit('leave_music_room',{room_id:currentRoom});
    currentRoom=null;
    showListView();
}

function showListView(){
    document.getElementById('list-view').classList.add('show');
    document.getElementById('player-view').classList.remove('show');
    if(syncInterval){clearInterval(syncInterval);syncInterval=null;}
    loadRooms();
}

function showPlayerView(){
    document.getElementById('list-view').classList.remove('show');
    document.getElementById('player-view').classList.add('show');
}

function updateRoomUI(){
    document.getElementById('room-title').textContent=roomState.title||'Music Room';
    document.getElementById('room-code').textContent=roomState.code||'------';
    isHost=roomState.host_user===currentUser;
    canControl=isHost||roomState.control_mode==='everyone';
    updatePlaylist();
    updateMembers();
    updateNowPlaying();
    updateControls();
}

function updatePlaylist(){
    var list=document.getElementById('playlist');
    if(!roomState.playlist.length){
        list.innerHTML='<div style="text-align:center;padding:20px;color:#64748b;font-size:12px">Playlist is empty</div>';
        return;
    }
    var html='';
    roomState.playlist.forEach((t,i)=>{
        var playing=i===roomState.current_track&&roomState.is_playing;
        html+='<div class="playlist-item'+(playing?' playing':'')+'" onclick="playTrack('+i+')">';
        html+='<span class="number">'+(playing?'&#9658;':(i+1))+'</span>';
        html+='<span class="name">'+escapeHtml(t.name)+'</span>';
        html+='<span class="duration">'+formatTime(t.duration||0)+'</span>';
        if(canControl)html+='<button class="remove" onclick="event.stopPropagation();removeTrack('+i+')">&times;</button>';
        html+='</div>';
    });
    list.innerHTML=html;
}

function updateMembers(){
    document.getElementById('member-count').textContent='Members ('+roomState.members.length+')';
    var list=document.getElementById('members-list');
    var html='';
    roomState.members.forEach(m=>{
        html+='<div class="member-item"><span class="dot"></span><span>'+m+'</span>';
        if(m===roomState.host_user)html+='<span class="host-badge">Host</span>';
        html+='</div>';
    });
    list.innerHTML=html;
}

function updateNowPlaying(){
    var track=roomState.playlist[roomState.current_track];
    if(track){
        document.getElementById('playing-icon').innerHTML='&#127926;';
        document.getElementById('track-name').textContent=track.name;
        document.getElementById('track-info').textContent='Track '+(roomState.current_track+1)+' of '+roomState.playlist.length;
        // Don't override total-time here, let audio.onloadedmetadata handle it
        if(track.duration>0){
            document.getElementById('total-time').textContent=formatTime(track.duration);
        }
    }else{
        document.getElementById('playing-icon').innerHTML='&#127925;';
        document.getElementById('track-name').textContent='No track playing';
        document.getElementById('track-info').textContent='Add songs to playlist';
        document.getElementById('current-time').textContent='0:00';
        document.getElementById('total-time').textContent='0:00';
        document.getElementById('progress-fill').style.width='0%';
    }
}

function updateControls(){
    document.getElementById('play-btn').innerHTML=roomState.is_playing?'&#10074;&#10074;':'&#9658;';
    document.getElementById('shuffle-btn').classList.toggle('active',roomState.shuffle);
    document.getElementById('repeat-btn').classList.toggle('active',roomState.repeat!=='none');
}

function togglePlay(){
    if(!canControl)return;
    if(roomState.is_playing){
        socket.emit('music_pause',{room_id:currentRoom});
    }else{
        socket.emit('music_play',{room_id:currentRoom});
    }
}

function playTrack(index){
    if(!canControl)return;
    socket.emit('music_play',{room_id:currentRoom,track_index:index});
}

function prevTrack(){
    if(!canControl)return;
    socket.emit('music_prev',{room_id:currentRoom});
}

function nextTrack(){
    if(!canControl)return;
    socket.emit('music_next',{room_id:currentRoom});
}

function toggleShuffle(){
    if(!canControl)return;
    socket.emit('music_shuffle',{room_id:currentRoom,enabled:!roomState.shuffle});
}

function toggleRepeat(){
    if(!canControl)return;
    var modes=['none','one','all'];
    var next=modes[(modes.indexOf(roomState.repeat)+1)%3];
    socket.emit('music_repeat',{room_id:currentRoom,mode:next});
}

function seekTo(e){
    if(!canControl)return;
    var bar=document.getElementById('progress-bar');
    var rect=bar.getBoundingClientRect();
    var pct=(e.clientX-rect.left)/rect.width;
    var duration=audio.duration||0;
    if(duration>0){
        var time=pct*duration;
        audio.currentTime=time;
        socket.emit('music_seek',{room_id:currentRoom,time:time});
    }
}

function removeTrack(index){
    if(!canControl)return;
    var track=roomState.playlist[index];
    if(track)socket.emit('remove_track',{room_id:currentRoom,track_id:track.id});
}

function copyCode(){
    navigator.clipboard.writeText(roomState.code||'').then(()=>showToast('Code copied: '+(roomState.code||''),'success'));
}

function showAddTrack(){document.getElementById('add-modal').classList.add('show');}
function hideAddModal(){document.getElementById('add-modal').classList.remove('show');}

function uploadTrack(){
    var input=document.getElementById('file-input');
    var file=input.files[0];
    if(!file)return;
    var form=new FormData();
    form.append('file',file);
    form.append('room_id',currentRoom);
    document.getElementById('upload-progress').style.display='block';
    var xhr=new XMLHttpRequest();
    xhr.upload.onprogress=function(e){
        if(e.lengthComputable){
            var pct=Math.round(e.loaded/e.total*100);
            document.getElementById('upload-bar').style.width=pct+'%';
        }
    };
    xhr.onload=function(){
        document.getElementById('upload-progress').style.display='none';
        document.getElementById('upload-bar').style.width='0%';
        input.value='';
        if(xhr.status===200){
            var d=JSON.parse(xhr.responseText);
            if(d.track)socket.emit('add_track',{room_id:currentRoom,track:d.track});
            hideAddModal();
        }else{
            showToast('Upload failed','error');
        }
    };
    xhr.open('POST','/api/music/upload');
    xhr.send(form);
}

function showImportS3(){
    selectedS3Files=[];
    document.getElementById('s3-files').innerHTML='<div style="text-align:center;padding:20px;color:#64748b">Loading...</div>';
    document.getElementById('s3-modal').classList.add('show');
    fetch('/api/music/s3-audio').then(r=>r.json()).then(d=>{
        if(!d.files||!d.files.length){
            document.getElementById('s3-files').innerHTML='<div style="text-align:center;padding:20px;color:#64748b">No audio files found</div>';
            return;
        }
        var html='';
        d.files.forEach(f=>{
            html+='<div class="s3-file" data-key="'+f.s3_key+'" data-name="'+escapeHtml(f.name)+'" onclick="toggleS3File(this)">';
            html+='<span class="icon">&#127925;</span>';
            html+='<span class="name">'+escapeHtml(f.name)+'</span>';
            html+='</div>';
        });
        document.getElementById('s3-files').innerHTML=html;
    });
}

function hideS3Modal(){document.getElementById('s3-modal').classList.remove('show');}

function toggleS3File(el){
    el.classList.toggle('selected');
    var key=el.dataset.key;
    var name=el.dataset.name;
    var idx=selectedS3Files.findIndex(f=>f.s3_key===key);
    if(idx>=0)selectedS3Files.splice(idx,1);
    else selectedS3Files.push({s3_key:key,name:name});
}

function importSelected(){
    if(!selectedS3Files.length){showToast('Select files first','error');return;}
    var count=selectedS3Files.length;
    selectedS3Files.forEach(f=>{
        socket.emit('import_from_s3',{room_id:currentRoom,s3_key:f.s3_key,name:f.name});
    });
    showToast('Imported '+count+' track(s)','success');
    hideS3Modal();
}

function showInvite(){
    var code=roomState.code||'';
    prompt('Share this code to invite others:',code);
}

function setupAudio(){
    audio.onended=function(){
        if(isHost)socket.emit('music_next',{room_id:currentRoom});
    };
    audio.onloadedmetadata=function(){
        var duration=audio.duration;
        if(duration&&!isNaN(duration)){
            document.getElementById('total-time').textContent=formatTime(duration);
            // Update track duration in roomState for display
            var track=roomState.playlist[roomState.current_track];
            if(track)track.duration=duration;
        }
    };
    audio.ontimeupdate=function(){
        var duration=audio.duration||0;
        var current=audio.currentTime||0;
        document.getElementById('current-time').textContent=formatTime(current);
        if(duration>0){
            var pct=(current/duration)*100;
            document.getElementById('progress-fill').style.width=pct+'%';
            document.getElementById('total-time').textContent=formatTime(duration);
        }
    };
    // Sync time periodically if host
    setInterval(function(){
        if(isHost&&currentRoom&&roomState.is_playing&&audio.currentTime>0){
            socket.emit('music_time_sync',{room_id:currentRoom,time:audio.currentTime});
        }
    },3000);
}

function loadAndPlayTrack(){
    var track=roomState.playlist[roomState.current_track];
    if(!track)return;
    audio.src='/api/music/stream/'+encodeURIComponent(track.s3_key);
    audio.currentTime=roomState.current_time||0;
    if(roomState.is_playing){
        audio.play().catch(e=>console.log('Autoplay blocked'));
    }
}

function setupSocket(){
    socket.on('music_room_created',function(data){
        currentRoom=data.room_id;
        roomState=data.state;
        showPlayerView();
        updateRoomUI();
    });
    socket.on('music_room_joined',function(data){
        currentRoom=data.room_id;
        roomState=data.state;
        showPlayerView();
        updateRoomUI();
        loadAndPlayTrack();
    });
    socket.on('music_room_error',function(data){
        showToast(data.error||'Error','error');
    });
    socket.on('music_state',function(data){
        if(data.room_id!==currentRoom)return;
        var wasPlaying=roomState.is_playing;
        var oldTrack=roomState.current_track;
        var oldPlaylistLen=roomState.playlist?roomState.playlist.length:0;
        var oldTrackKey=roomState.playlist&&roomState.playlist[oldTrack]?roomState.playlist[oldTrack].s3_key:'';
        roomState=data.state;
        updateRoomUI();
        var newTrackKey=roomState.playlist&&roomState.playlist[roomState.current_track]?roomState.playlist[roomState.current_track].s3_key:'';
        // Reload if track changed OR playlist changed OR track key changed
        if(roomState.current_track!==oldTrack||roomState.playlist.length!==oldPlaylistLen||oldTrackKey!==newTrackKey){
            loadAndPlayTrack();
        }else if(roomState.is_playing!==wasPlaying){
            if(roomState.is_playing)audio.play().catch(e=>{});
            else audio.pause();
        }
    });
    socket.on('music_time_sync',function(data){
        if(data.room_id!==currentRoom||isHost)return;
        var diff=Math.abs(audio.currentTime-data.time);
        if(diff>2)audio.currentTime=data.time;
    });
    socket.on('music_room_left',function(){
        showListView();
    });
}

function formatTime(s){
    if(!s||isNaN(s)||s<=0)return'--:--';
    var m=Math.floor(s/60);
    var sec=Math.floor(s%60);
    return m+':'+(sec<10?'0':'')+sec;
}

function escapeHtml(s){return s?s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'):'';}

init();
</script></body></html>"""

# ===========================================
# EMBED_SCREEN_SHARE - WebRTC Screen Sharing
# ===========================================

EMBED_SCREEN_SHARE = EMBED_CSS + """<!DOCTYPE html><html><head><title>Screen Share</title>
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
.screen-container{max-width:1000px;margin:0 auto;padding:12px;height:100vh;box-sizing:border-box;display:flex;flex-direction:column}
.list-view,.host-view,.viewer-view{display:none;flex-direction:column;flex:1;min-height:0}
.list-view.show,.host-view.show,.viewer-view.show{display:flex}
.session-list{flex:1;overflow-y:auto}
.session-item{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px;margin-bottom:10px;cursor:pointer;transition:all .2s;position:relative}
.session-item:hover{border-color:#6366f1}
.session-item .title{font-size:15px;font-weight:600;margin-bottom:4px}
.session-item .info{font-size:12px;color:#94a3b8;display:flex;gap:12px}
.session-item .lock{color:#f59e0b}
.session-item .code{background:#6366f1;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;letter-spacing:1px;margin-left:8px}
.start-section{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:16px;margin-bottom:12px}
.start-section h3{margin:0 0 12px 0;font-size:14px}
.join-section{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:16px;margin-bottom:12px}
.join-section h3{margin:0 0 12px 0;font-size:14px}
.join-row{display:flex;gap:8px}
.join-row input{flex:1;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:10px;color:#e2e8f0;font-size:14px;text-transform:uppercase;letter-spacing:2px;text-align:center}
.join-row input:focus{outline:none;border-color:#6366f1}
.form-row{display:flex;gap:12px;margin-bottom:12px}
.form-row input{flex:1;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:10px;color:#e2e8f0;font-size:13px}
.form-row input:focus{outline:none;border-color:#6366f1}
.host-header,.viewer-header{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid #334155}
.host-header .title,.viewer-header .title{font-size:16px;font-weight:600}
.video-container{flex:1;background:#000;border-radius:10px;overflow:hidden;position:relative;margin:12px 0;min-height:300px}
.video-container video{width:100%;height:100%;object-fit:contain}
.video-placeholder{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;color:#64748b}
.video-placeholder .icon{font-size:60px;margin-bottom:12px;opacity:0.3}
.controls-bar{display:flex;align-items:center;justify-content:center;gap:12px;padding:12px;background:#1e293b;border-radius:10px}
.controls-bar button{background:#334155;border:none;color:#e2e8f0;padding:10px 16px;border-radius:8px;cursor:pointer;font-size:14px;display:flex;align-items:center;gap:8px}
.controls-bar button:hover{background:#475569}
.controls-bar button.active{background:#6366f1}
.controls-bar button.danger{background:#ef4444}
.controls-bar button.danger:hover{background:#dc2626}
.sidebar{width:280px;background:#1e293b;border-radius:10px;border:1px solid #334155;display:flex;flex-direction:column;max-height:400px}
.sidebar-header{padding:12px;border-bottom:1px solid #334155;font-size:13px;font-weight:600}
.viewer-list{padding:8px;overflow-y:auto;flex:1}
.viewer-item{display:flex;align-items:center;gap:8px;padding:8px 10px;font-size:13px}
.viewer-item .dot{width:8px;height:8px;background:#10b981;border-radius:50%}
.chat-section{flex:1;display:flex;flex-direction:column;min-height:0}
.chat-messages{flex:1;overflow-y:auto;padding:8px}
.chat-msg{padding:6px 10px;margin-bottom:6px;font-size:12px}
.chat-msg .user{font-weight:600;color:#6366f1}
.chat-msg .text{color:#e2e8f0}
.chat-input{display:flex;gap:6px;padding:8px}
.chat-input input{flex:1;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:8px;color:#e2e8f0;font-size:12px}
.chat-input input:focus{outline:none;border-color:#6366f1}
.share-link{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px;margin:12px 0;display:flex;align-items:center;gap:8px}
.share-link input{flex:1;background:transparent;border:none;color:#e2e8f0;font-size:12px}
.share-link button{background:#6366f1;border:none;color:#fff;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px}
.host-sidebar{display:flex;flex-direction:column;gap:12px}
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:1000}
.modal-overlay.show{display:flex}
.modal{background:#1e293b;border-radius:12px;border:1px solid #334155;width:360px}
.modal-header{padding:14px 16px;border-bottom:1px solid #334155;display:flex;align-items:center;justify-content:space-between}
.modal-header h3{margin:0;font-size:15px}
.modal-header .close-btn{background:transparent;border:none;color:#94a3b8;font-size:18px;cursor:pointer}
.modal-body{padding:16px}
.modal-footer{padding:14px 16px;border-top:1px solid #334155;display:flex;justify-content:flex-end;gap:8px}
.main-content{display:flex;gap:12px;flex:1;min-height:0}
.video-section{flex:1;display:flex;flex-direction:column;min-height:0}
.toast-container{position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px}
.toast{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px 18px;box-shadow:0 8px 24px rgba(0,0,0,.4);display:flex;align-items:center;gap:12px;animation:slideIn .3s ease;max-width:320px}
.toast.success{border-color:#10b981;background:linear-gradient(135deg,#1e293b,#064e3b)}
.toast.error{border-color:#ef4444;background:linear-gradient(135deg,#1e293b,#7f1d1d)}
.toast.info{border-color:#6366f1;background:linear-gradient(135deg,#1e293b,#312e81)}
.toast .icon{font-size:20px}
.toast .message{flex:1;font-size:13px}
.toast .close{background:none;border:none;color:#64748b;cursor:pointer;font-size:16px;padding:0}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
</style>
</head><body>
<div class="screen-container">
    <div class="list-view show" id="list-view">
        <div class="start-section">
            <h3>&#128250; Start Screen Share</h3>
            <div class="form-row">
                <input type="text" id="session-title" placeholder="Session title...">
                <input type="password" id="session-password" placeholder="Password (optional)">
            </div>
            <button class="btn btn-success" onclick="startShare()">&#128250; Start Sharing</button>
        </div>
        <div class="join-section">
            <h3>&#128279; Join by Code</h3>
            <div class="join-row">
                <input type="text" id="join-code" placeholder="ABC123" maxlength="6">
                <button class="btn btn-primary" onclick="joinByCode()">Join</button>
            </div>
        </div>
        <h3 style="font-size:14px;margin-bottom:12px">Active Sessions</h3>
        <div class="session-list" id="session-list"></div>
    </div>

    <div class="host-view" id="host-view">
        <div class="host-header">
            <div style="display:flex;align-items:center;gap:12px">
                <span class="title" id="host-title">Screen Share</span>
                <span style="background:#6366f1;padding:4px 12px;border-radius:6px;font-size:14px;font-weight:600;letter-spacing:2px;cursor:pointer" id="host-code" onclick="copyCode()" title="Click to copy">------</span>
            </div>
            <div style="display:flex;gap:8px">
                <button class="btn btn-secondary btn-sm" onclick="showGuestLink()">&#128279; Guest Link</button>
                <button class="btn btn-danger btn-sm" onclick="stopShare()">&#9632; Stop</button>
            </div>
        </div>
        <div class="share-link" id="share-link-container" style="display:none">
            <span style="font-size:12px;color:#94a3b8;margin-right:8px">Guest link:</span>
            <input type="text" id="share-link" readonly>
            <button onclick="copyLink()">Copy</button>
        </div>
        <div class="main-content">
            <div class="video-section">
                <div class="video-container">
                    <video id="host-preview" autoplay muted playsinline></video>
                </div>
                <div class="controls-bar">
                    <button id="mic-btn" onclick="toggleMic()">&#127908; Mic Off</button>
                    <button id="cam-btn" onclick="toggleCam()">&#128247; Cam Off</button>
                </div>
            </div>
            <div class="host-sidebar">
                <div class="sidebar">
                    <div class="sidebar-header">&#128101; Viewers (<span id="viewer-count">0</span>)</div>
                    <div class="viewer-list" id="viewer-list"></div>
                </div>
                <div class="sidebar" style="flex:1;min-height:150px">
                    <div class="sidebar-header">&#128172; Chat</div>
                    <div class="chat-section">
                        <div class="chat-messages" id="host-chat"></div>
                        <div class="chat-input">
                            <input type="text" id="host-chat-input" placeholder="Type message..." onkeydown="if(event.key==='Enter')sendChat('host')">
                            <button class="btn btn-primary btn-sm" onclick="sendChat('host')">Send</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="viewer-view" id="viewer-view">
        <div class="viewer-header">
            <span class="title" id="viewer-title">Watching: Screen Share</span>
            <button class="btn btn-secondary btn-sm" onclick="leaveSession()">Leave</button>
        </div>
        <div class="main-content">
            <div class="video-section">
                <div class="video-container">
                    <video id="viewer-video" autoplay playsinline></video>
                    <div class="video-placeholder" id="viewer-placeholder">
                        <div class="icon">&#128250;</div>
                        <div>Connecting to stream...</div>
                    </div>
                </div>
            </div>
            <div class="sidebar" style="flex:1;min-height:200px">
                <div class="sidebar-header">&#128172; Chat</div>
                <div class="chat-section">
                    <div class="chat-messages" id="viewer-chat"></div>
                    <div class="chat-input">
                        <input type="text" id="viewer-chat-input" placeholder="Type message..." onkeydown="if(event.key==='Enter')sendChat('viewer')">
                        <button class="btn btn-primary btn-sm" onclick="sendChat('viewer')">Send</button>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<div class="toast-container" id="toast-container"></div>

<div class="modal-overlay" id="password-modal">
    <div class="modal">
        <div class="modal-header">
            <h3>&#128274; Password Required</h3>
            <button class="close-btn" onclick="hidePasswordModal()">&times;</button>
        </div>
        <div class="modal-body">
            <input type="password" id="join-password" placeholder="Enter password..." style="width:100%;padding:10px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0">
        </div>
        <div class="modal-footer">
            <button class="btn btn-secondary" onclick="hidePasswordModal()">Cancel</button>
            <button class="btn btn-primary" onclick="submitPassword()">Join</button>
        </div>
    </div>
</div>

<script>
function showToast(message,type='info',duration=3000){
    var container=document.getElementById('toast-container');
    var toast=document.createElement('div');
    toast.className='toast '+type;
    var icons={success:'&#10004;',error:'&#10006;',info:'&#8505;'};
    toast.innerHTML='<span class="icon">'+icons[type]+'</span><span class="message">'+message+'</span><button class="close" onclick="this.parentElement.remove()">&times;</button>';
    container.appendChild(toast);
    if(duration>0)setTimeout(function(){toast.remove();},duration);
}

var socket=io();
var currentUser='{{ username }}';
var currentSession=null;
var isHost=false;
var localStream=null;
var peerConnections={};
var pendingJoinSession=null;

var iceServers=[{urls:'stun:stun.l.google.com:19302'},{urls:'stun:stun1.l.google.com:19302'}];

function init(){
    loadSessions();
    setupSocket();
}

function loadSessions(){
    fetch('/api/screen/sessions').then(r=>r.json()).then(d=>{
        var list=document.getElementById('session-list');
        if(!d.sessions||!d.sessions.length){
            list.innerHTML='<div style="text-align:center;padding:30px;color:#64748b">No active sessions</div>';
            return;
        }
        var html='';
        d.sessions.forEach(s=>{
            html+='<div class="session-item" onclick="joinSession(\\''+s._id+'\\','+s.has_password+')">';
            html+='<div class="title">'+escapeHtml(s.title)+(s.has_password?' <span class="lock">&#128274;</span>':'');
            if(s.code)html+='<span class="code">'+s.code+'</span>';
            html+='</div>';
            html+='<div class="info"><span>Host: '+s.host_user+'</span><span>&#128101; '+s.viewer_count+'</span></div>';
            html+='</div>';
        });
        list.innerHTML=html;
    });
}

function joinByCode(){
    var code=document.getElementById('join-code').value.trim().toUpperCase();
    if(code.length!==6){showToast('Enter 6-character code','error');return;}
    socket.emit('join_screen_by_code',{code:code});
}

async function startShare(){
    var title=document.getElementById('session-title').value.trim()||'Screen Share';
    var password=document.getElementById('session-password').value;
    try{
        localStream=await navigator.mediaDevices.getDisplayMedia({video:true,audio:true});
        document.getElementById('host-preview').srcObject=localStream;
        localStream.getVideoTracks()[0].onended=function(){stopShare();};
        socket.emit('start_screen_share',{title:title,password:password});
    }catch(e){
        showToast('Could not start: '+e.message,'error');
    }
}

function stopShare(){
    if(localStream){
        localStream.getTracks().forEach(t=>t.stop());
        localStream=null;
    }
    Object.values(peerConnections).forEach(pc=>pc.close());
    peerConnections={};
    if(currentSession)socket.emit('stop_screen_share',{session_id:currentSession});
    currentSession=null;
    isHost=false;
    showListView();
}

function joinSession(sessionId,hasPassword){
    pendingJoinSession=sessionId;
    if(hasPassword){
        document.getElementById('password-modal').classList.add('show');
    }else{
        socket.emit('join_screen_session',{session_id:sessionId});
    }
}

function submitPassword(){
    var password=document.getElementById('join-password').value;
    hidePasswordModal();
    socket.emit('join_screen_session',{session_id:pendingJoinSession,password:password});
}

function hidePasswordModal(){
    document.getElementById('password-modal').classList.remove('show');
    document.getElementById('join-password').value='';
}

function leaveSession(){
    if(currentSession)socket.emit('leave_screen_session',{session_id:currentSession});
    Object.values(peerConnections).forEach(pc=>pc.close());
    peerConnections={};
    currentSession=null;
    showListView();
}

function showListView(){
    document.getElementById('list-view').classList.add('show');
    document.getElementById('host-view').classList.remove('show');
    document.getElementById('viewer-view').classList.remove('show');
    loadSessions();
}

function showHostView(){
    document.getElementById('list-view').classList.remove('show');
    document.getElementById('host-view').classList.add('show');
    document.getElementById('viewer-view').classList.remove('show');
}

function showViewerView(){
    document.getElementById('list-view').classList.remove('show');
    document.getElementById('host-view').classList.remove('show');
    document.getElementById('viewer-view').classList.add('show');
}

function copyLink(){
    var input=document.getElementById('share-link');
    navigator.clipboard.writeText(input.value).then(()=>showToast('Guest link copied!','success'));
}

function copyCode(){
    var code=document.getElementById('host-code').textContent;
    if(code&&code!=='------'){
        navigator.clipboard.writeText(code).then(()=>showToast('Code copied: '+code,'success'));
    }
}

function showGuestLink(){
    var container=document.getElementById('share-link-container');
    container.style.display=container.style.display==='none'?'flex':'none';
}

var sessionCode='';

function toggleMic(){
    // Placeholder - would toggle mic track
    var btn=document.getElementById('mic-btn');
    btn.classList.toggle('active');
}

function toggleCam(){
    // Placeholder - would toggle cam track
    var btn=document.getElementById('cam-btn');
    btn.classList.toggle('active');
}

function sendChat(role){
    var inputId=role==='host'?'host-chat-input':'viewer-chat-input';
    var input=document.getElementById(inputId);
    var text=input.value.trim();
    if(!text||!currentSession)return;
    socket.emit('screen_chat',{session_id:currentSession,content:text});
    input.value='';
}

function addChatMessage(user,text,role){
    var chatId=role==='host'?'host-chat':'viewer-chat';
    var chat=document.getElementById(chatId);
    var div=document.createElement('div');
    div.className='chat-msg';
    div.innerHTML='<span class="user">'+escapeHtml(user)+':</span> <span class="text">'+escapeHtml(text)+'</span>';
    chat.appendChild(div);
    chat.scrollTop=chat.scrollHeight;
}

async function createPeerConnection(viewerId){
    var pc=new RTCPeerConnection({iceServers:iceServers});
    peerConnections[viewerId]=pc;
    if(localStream){
        localStream.getTracks().forEach(t=>pc.addTrack(t,localStream));
    }
    pc.onicecandidate=function(e){
        if(e.candidate){
            socket.emit('webrtc_ice',{session_id:currentSession,viewer_id:viewerId,candidate:e.candidate});
        }
    };
    var offer=await pc.createOffer();
    await pc.setLocalDescription(offer);
    socket.emit('webrtc_offer',{session_id:currentSession,viewer_id:viewerId,sdp:pc.localDescription});
}

async function handleOffer(hostId,sdp){
    var pc=new RTCPeerConnection({iceServers:iceServers});
    peerConnections[hostId]=pc;
    pc.onicecandidate=function(e){
        if(e.candidate){
            socket.emit('webrtc_ice',{session_id:currentSession,candidate:e.candidate});
        }
    };
    pc.ontrack=function(e){
        document.getElementById('viewer-video').srcObject=e.streams[0];
        document.getElementById('viewer-placeholder').style.display='none';
    };
    await pc.setRemoteDescription(new RTCSessionDescription(sdp));
    var answer=await pc.createAnswer();
    await pc.setLocalDescription(answer);
    socket.emit('webrtc_answer',{session_id:currentSession,sdp:pc.localDescription});
}

function setupSocket(){
    socket.on('screen_session_started',function(data){
        currentSession=data.session_id;
        sessionCode=data.code||'';
        isHost=true;
        document.getElementById('host-title').textContent=data.title;
        document.getElementById('host-code').textContent=sessionCode;
        document.getElementById('share-link').value=location.origin+'/screen-guest?code='+sessionCode;
        showHostView();
    });
    socket.on('screen_session_joined',function(data){
        currentSession=data.session_id;
        isHost=false;
        document.getElementById('viewer-title').textContent='Watching: '+data.title;
        showViewerView();
    });
    socket.on('screen_session_error',function(data){
        showToast(data.error||'Error','error');
    });
    socket.on('viewer_joined',function(data){
        if(!isHost)return;
        createPeerConnection(data.viewer_id);
        updateViewerList(data.viewers);
    });
    socket.on('viewer_left',function(data){
        if(peerConnections[data.viewer_id]){
            peerConnections[data.viewer_id].close();
            delete peerConnections[data.viewer_id];
        }
        updateViewerList(data.viewers);
    });
    socket.on('webrtc_offer',function(data){
        if(isHost)return;
        handleOffer(data.host_id,data.sdp);
    });
    socket.on('webrtc_answer',async function(data){
        if(!isHost)return;
        var pc=peerConnections[data.viewer_id];
        if(pc)await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
    });
    socket.on('webrtc_ice',async function(data){
        var pc=peerConnections[data.from_id]||peerConnections[Object.keys(peerConnections)[0]];
        if(pc&&data.candidate){
            try{await pc.addIceCandidate(new RTCIceCandidate(data.candidate));}catch(e){}
        }
    });
    socket.on('screen_chat_message',function(data){
        addChatMessage(data.from_user,data.content,isHost?'host':'viewer');
    });
    socket.on('screen_session_ended',function(){
        showToast('Host ended the session','info');
        leaveSession();
    });
}

function updateViewerList(viewers){
    document.getElementById('viewer-count').textContent=viewers.length;
    var list=document.getElementById('viewer-list');
    var html='';
    viewers.forEach(v=>{
        html+='<div class="viewer-item"><span class="dot"></span><span>'+escapeHtml(v)+'</span></div>';
    });
    list.innerHTML=html||'<div style="padding:10px;color:#64748b;font-size:12px">No viewers yet</div>';
}

function escapeHtml(s){return s?s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'):'';}

init();
</script></body></html>"""

# ===========================================
# EMBED_SCREEN_GUEST - Guest access for screen share
# ===========================================

EMBED_SCREEN_GUEST = """<!DOCTYPE html><html><head><title>Screen Share - Guest</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.container{max-width:500px;margin:0 auto;padding:40px 20px}
.card{background:#1e293b;border-radius:16px;border:1px solid #334155;overflow:hidden}
.card-header{background:#334155;padding:20px;text-align:center}
.card-header h1{font-size:24px;margin-bottom:8px}
.card-header p{color:#94a3b8;font-size:14px}
.card-body{padding:24px}
.form-group{margin-bottom:20px}
.form-group label{display:block;font-size:13px;color:#94a3b8;margin-bottom:8px}
.form-group input{width:100%;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:14px;color:#e2e8f0;font-size:15px}
.form-group input:focus{outline:none;border-color:#6366f1}
.form-group input.code-input{text-transform:uppercase;letter-spacing:4px;text-align:center;font-size:20px;font-weight:600}
.btn{width:100%;background:#6366f1;border:none;color:#fff;padding:14px;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer}
.btn:hover{background:#4f46e5}
.btn:disabled{background:#475569;cursor:not-allowed}
.error{background:#7f1d1d;border:1px solid #991b1b;color:#fca5a5;padding:12px;border-radius:8px;margin-bottom:16px;font-size:13px;display:none}
.error.show{display:block}
.viewer-container{display:none;height:100vh;flex-direction:column}
.viewer-container.show{display:flex}
.viewer-header{background:#1e293b;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #334155}
.viewer-header .title{font-size:16px;font-weight:600}
.viewer-header .btn-leave{background:#334155;border:none;color:#e2e8f0;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:13px}
.viewer-header .btn-leave:hover{background:#475569}
.video-container{flex:1;background:#000;display:flex;align-items:center;justify-content:center}
.video-container video{max-width:100%;max-height:100%;object-fit:contain}
.connecting{color:#64748b;text-align:center}
.connecting .icon{font-size:48px;margin-bottom:12px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:.5}50%{opacity:1}}
.join-container{display:block}
.join-container.hide{display:none}
</style>
</head><body>

<div class="join-container" id="join-container">
<div class="container">
    <div class="card">
        <div class="card-header">
            <h1>&#128250; Screen Share</h1>
            <p>Enter the code to watch a screen share</p>
        </div>
        <div class="card-body">
            <div class="error" id="error-msg"></div>
            <div class="form-group">
                <label>Room Code</label>
                <input type="text" id="code-input" class="code-input" placeholder="ABC123" maxlength="6" value="{{ code }}">
            </div>
            <div class="form-group">
                <label>Your Name</label>
                <input type="text" id="name-input" placeholder="Enter your name...">
            </div>
            <div class="form-group" id="password-group" style="display:none">
                <label>Password</label>
                <input type="password" id="password-input" placeholder="Enter password...">
            </div>
            <button class="btn" id="join-btn" onclick="joinSession()">Join Session</button>
        </div>
    </div>
</div>
</div>

<div class="viewer-container" id="viewer-container">
    <div class="viewer-header">
        <span class="title" id="session-title">Watching: Screen Share</span>
        <button class="btn-leave" onclick="leaveSession()">Leave</button>
    </div>
    <div class="video-container">
        <video id="remote-video" autoplay playsinline></video>
        <div class="connecting" id="connecting">
            <div class="icon">&#128250;</div>
            <div>Connecting to stream...</div>
        </div>
    </div>
</div>

<script>
var socket=io();
var guestName='';
var currentSession=null;
var peerConnection=null;
var iceServers=[{urls:'stun:stun.l.google.com:19302'},{urls:'stun:stun1.l.google.com:19302'}];

function generateGuestName(){
    var chars='ABCDEFGHIJKLMNOPQRSTUVWXYZ';
    var result='GUEST-';
    for(var i=0;i<5;i++)result+=chars.charAt(Math.floor(Math.random()*chars.length));
    return result;
}

function init(){
    document.getElementById('name-input').value=generateGuestName();
    var code=document.getElementById('code-input').value;
    if(code&&code.length===6){
        document.getElementById('code-input').focus();
    }
    setupSocket();
}

function showError(msg){
    var el=document.getElementById('error-msg');
    el.textContent=msg;
    el.classList.add('show');
}

function hideError(){
    document.getElementById('error-msg').classList.remove('show');
}

function joinSession(){
    hideError();
    var code=document.getElementById('code-input').value.trim().toUpperCase();
    guestName=document.getElementById('name-input').value.trim()||generateGuestName();
    var password=document.getElementById('password-input').value;

    if(code.length!==6){
        showError('Please enter a 6-character code');
        return;
    }

    document.getElementById('join-btn').disabled=true;
    document.getElementById('join-btn').textContent='Connecting...';

    socket.emit('join_screen_by_code',{code:code,guest_name:guestName,password:password});
}

function leaveSession(){
    if(currentSession)socket.emit('leave_screen_session',{session_id:currentSession});
    if(peerConnection){
        peerConnection.close();
        peerConnection=null;
    }
    currentSession=null;
    document.getElementById('join-container').classList.remove('hide');
    document.getElementById('viewer-container').classList.remove('show');
    document.getElementById('join-btn').disabled=false;
    document.getElementById('join-btn').textContent='Join Session';
}

function showViewer(title){
    document.getElementById('join-container').classList.add('hide');
    document.getElementById('viewer-container').classList.add('show');
    document.getElementById('session-title').textContent='Watching: '+title;
}

async function handleOffer(hostId,sdp){
    peerConnection=new RTCPeerConnection({iceServers:iceServers});
    peerConnection.onicecandidate=function(e){
        if(e.candidate){
            socket.emit('webrtc_ice',{session_id:currentSession,candidate:e.candidate});
        }
    };
    peerConnection.ontrack=function(e){
        document.getElementById('remote-video').srcObject=e.streams[0];
        document.getElementById('connecting').style.display='none';
    };
    await peerConnection.setRemoteDescription(new RTCSessionDescription(sdp));
    var answer=await peerConnection.createAnswer();
    await peerConnection.setLocalDescription(answer);
    socket.emit('webrtc_answer',{session_id:currentSession,sdp:peerConnection.localDescription});
}

function setupSocket(){
    socket.on('screen_session_joined',function(data){
        currentSession=data.session_id;
        showViewer(data.title||'Screen Share');
    });
    socket.on('screen_session_error',function(data){
        showError(data.error||'Failed to join');
        document.getElementById('join-btn').disabled=false;
        document.getElementById('join-btn').textContent='Join Session';
        if(data.error&&data.error.includes('password')){
            document.getElementById('password-group').style.display='block';
        }
    });
    socket.on('webrtc_offer',function(data){
        handleOffer(data.host_id,data.sdp);
    });
    socket.on('webrtc_ice',async function(data){
        if(peerConnection&&data.candidate){
            try{await peerConnection.addIceCandidate(new RTCIceCandidate(data.candidate));}catch(e){}
        }
    });
    socket.on('screen_session_ended',function(){
        showSessionEndedModal();
    });
}

function showSessionEndedModal(){
    var overlay=document.createElement('div');
    overlay.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.8);display:flex;align-items:center;justify-content:center;z-index:9999';
    overlay.innerHTML='<div style="background:#1e293b;border:1px solid #334155;border-radius:16px;padding:32px;text-align:center;max-width:360px"><div style="font-size:48px;margin-bottom:16px">&#128250;</div><h2 style="margin:0 0 12px 0;font-size:20px">Session Ended</h2><p style="color:#94a3b8;margin:0 0 24px 0">The host has ended this screen share session.</p><button onclick="location.reload()" style="background:#6366f1;border:none;color:#fff;padding:12px 24px;border-radius:8px;font-size:14px;cursor:pointer">OK</button></div>';
    document.body.appendChild(overlay);
}

init();
</script>
</body></html>"""

# ===========================================
# EMBED_USER_SHARES - Incoming shares from other users
# ===========================================

EMBED_USER_SHARES = EMBED_CSS + """<!DOCTYPE html><html><head><title>User Shares</title></head><body>
<div class="container">
    <div class="card">
        <div class="card-header"><h2>&#128229; Incoming Shares</h2></div>
        <div class="card-body" style="padding:0" id="incoming-content">Loading...</div>
    </div>
    <div class="card" style="margin-top:16px">
        <div class="card-header"><h2>&#128228; Sent Shares</h2></div>
        <div class="card-body" style="padding:0" id="sent-content">Loading...</div>
    </div>
</div>
<script>
function load(){
    fetch('/api/user-shares/incoming').then(r=>r.json()).then(d=>{
        if(d.error){document.getElementById('incoming-content').innerHTML='<div class="empty">'+d.error+'</div>';return;}
        if(!d.shares||!d.shares.length){document.getElementById('incoming-content').innerHTML='<div class="empty">No incoming shares</div>';return;}
        var html='<table><thead><tr><th>From</th><th>Item</th><th>Type</th><th>Message</th><th>Actions</th></tr></thead><tbody>';
        d.shares.forEach(s=>{
            html+='<tr><td><strong>'+s.from_user+'</strong></td>';
            html+='<td>'+s.item_name+'</td>';
            html+='<td><span class="tag '+(s.item_type==='dir'?'tag-blue':'tag-green')+'">'+s.item_type+'</span></td>';
            html+='<td style="font-size:12px;color:#94a3b8">'+(s.message||'-')+'</td>';
            html+='<td><div class="actions">';
            if(s.status==='pending'){
                html+='<button class="btn btn-success btn-sm" onclick="acceptShare(\\''+s._id+'\\')">Accept</button>';
                html+='<button class="btn btn-danger btn-sm" onclick="rejectShare(\\''+s._id+'\\')">Reject</button>';
            }else{
                html+='<span class="tag">'+(s.status==='accepted'?'Accepted':'Rejected')+'</span>';
            }
            html+='</div></td></tr>';
        });
        html+='</tbody></table>';
        document.getElementById('incoming-content').innerHTML=html;
    });
    fetch('/api/user-shares/sent').then(r=>r.json()).then(d=>{
        if(d.error){document.getElementById('sent-content').innerHTML='<div class="empty">'+d.error+'</div>';return;}
        if(!d.shares||!d.shares.length){document.getElementById('sent-content').innerHTML='<div class="empty">No sent shares</div>';return;}
        var html='<table><thead><tr><th>To</th><th>Item</th><th>Type</th><th>Status</th></tr></thead><tbody>';
        d.shares.forEach(s=>{
            html+='<tr><td><strong>'+s.to_user+'</strong></td>';
            html+='<td>'+s.item_name+'</td>';
            html+='<td><span class="tag '+(s.item_type==='dir'?'tag-blue':'tag-green')+'">'+s.item_type+'</span></td>';
            html+='<td><span class="tag">'+(s.status||'pending')+'</span></td></tr>';
        });
        html+='</tbody></table>';
        document.getElementById('sent-content').innerHTML=html;
    });
}
function acceptShare(id){
    var dest=prompt('Save to folder (leave empty for workspace root):','')||'';
    fetch('/api/user-shares/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({share_id:id,dest_path:dest})}).then(r=>r.json()).then(d=>{
        if(d.success){alert('File copied to workspace!');load();}
        else alert(d.error||'Failed');
    });
}
function rejectShare(id){
    if(!confirm('Reject this share?'))return;
    fetch('/api/user-shares/reject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({share_id:id})}).then(r=>r.json()).then(d=>{
        if(d.success)load();
        else alert(d.error||'Failed');
    });
}
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
# GameHub Embed
# ===========================================

EMBED_GAME_HUB = EMBED_CSS + """<!DOCTYPE html><html><head><title>GameHub</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);min-height:100vh;color:#fff}
.container{padding:20px;max-width:1200px;margin:0 auto}
.header{text-align:center;margin-bottom:30px}
.header h1{font-size:2.5rem;background:linear-gradient(90deg,#f093fb,#f5576c);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}
.header p{color:#a0a0a0;font-size:14px}
.games-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px}
.game-card{background:rgba(255,255,255,.05);border-radius:16px;overflow:hidden;cursor:pointer;transition:all .3s;border:1px solid rgba(255,255,255,.1)}
.game-card:hover{transform:translateY(-5px);box-shadow:0 20px 40px rgba(0,0,0,.4);border-color:rgba(255,255,255,.2)}
.game-preview{height:180px;display:flex;align-items:center;justify-content:center;font-size:80px;background:linear-gradient(135deg,var(--c1),var(--c2))}
.game-info{padding:16px}
.game-info h3{font-size:18px;margin-bottom:6px}
.game-info p{color:#888;font-size:13px}
.game-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);z-index:1000}
.game-modal.active{display:flex;flex-direction:column}
.modal-header{background:#1a1a2e;padding:12px 20px;display:flex;align-items:center;justify-content:space-between}
.modal-header h2{font-size:18px}
.modal-header .close-btn{background:#ff4757;border:none;color:#fff;width:36px;height:36px;border-radius:8px;cursor:pointer;font-size:20px}
.modal-body{flex:1;display:flex;align-items:center;justify-content:center;padding:20px}
.game-frame{background:#000;border-radius:12px;overflow:hidden;box-shadow:0 0 40px rgba(0,0,0,.5)}
/* 2048 styles */
.game-2048{width:400px;padding:20px;background:#faf8ef;border-radius:12px}
.game-2048 .score-board{display:flex;justify-content:space-between;margin-bottom:16px}
.game-2048 .score-box{background:#bbada0;color:#fff;padding:8px 16px;border-radius:6px;text-align:center}
.game-2048 .score-box span{display:block;font-size:12px;opacity:.8}
.game-2048 .score-box strong{font-size:20px}
.game-2048 .grid-2048{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;background:#bbada0;padding:10px;border-radius:8px}
.game-2048 .cell{aspect-ratio:1;display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:700;border-radius:6px;background:#cdc1b4;color:#776e65;transition:all .15s}
.game-2048 .cell[data-value="2"]{background:#eee4da}
.game-2048 .cell[data-value="4"]{background:#ede0c8}
.game-2048 .cell[data-value="8"]{background:#f2b179;color:#f9f6f2}
.game-2048 .cell[data-value="16"]{background:#f59563;color:#f9f6f2}
.game-2048 .cell[data-value="32"]{background:#f67c5f;color:#f9f6f2}
.game-2048 .cell[data-value="64"]{background:#f65e3b;color:#f9f6f2}
.game-2048 .cell[data-value="128"]{background:#edcf72;color:#f9f6f2;font-size:24px}
.game-2048 .cell[data-value="256"]{background:#edcc61;color:#f9f6f2;font-size:24px}
.game-2048 .cell[data-value="512"]{background:#edc850;color:#f9f6f2;font-size:24px}
.game-2048 .cell[data-value="1024"]{background:#edc53f;color:#f9f6f2;font-size:20px}
.game-2048 .cell[data-value="2048"]{background:#edc22e;color:#f9f6f2;font-size:20px}
.game-2048 .restart-btn{width:100%;margin-top:16px;padding:12px;background:#8f7a66;color:#fff;border:none;border-radius:6px;font-size:16px;font-weight:600;cursor:pointer}
/* Snake styles */
.game-snake{background:#1a1a2e;padding:20px;border-radius:12px}
.game-snake .snake-header{display:flex;justify-content:space-between;margin-bottom:12px;color:#fff}
.game-snake canvas{display:block;border-radius:8px}
.game-snake .controls{display:grid;grid-template-columns:repeat(3,60px);gap:8px;margin-top:16px;justify-content:center}
.game-snake .controls button{padding:12px;background:#2d2d44;border:none;color:#fff;border-radius:8px;font-size:20px;cursor:pointer}
.game-snake .controls button:active{background:#3d3d54}
/* Memory styles */
.game-memory{background:#1a1a2e;padding:20px;border-radius:12px}
.game-memory .memory-header{display:flex;justify-content:space-between;margin-bottom:16px;color:#fff}
.game-memory .memory-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;width:340px}
.game-memory .memory-card{aspect-ratio:1;background:#2d2d44;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:32px;transition:all .3s;transform-style:preserve-3d}
.game-memory .memory-card.flipped{background:#6366f1;transform:rotateY(180deg)}
.game-memory .memory-card.matched{background:#10b981}
.game-memory .memory-card .front{display:none}
.game-memory .memory-card.flipped .front,.game-memory .memory-card.matched .front{display:block}
.game-memory .restart-btn{width:100%;margin-top:16px;padding:12px;background:#6366f1;color:#fff;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer}
/* Minesweeper */
.game-mines{background:#c0c0c0;padding:12px;border-radius:8px;border:3px outset #fff}
.game-mines .mines-header{display:flex;justify-content:space-between;align-items:center;background:#c0c0c0;padding:8px;margin-bottom:8px;border:2px inset #808080}
.game-mines .counter{background:#000;color:#f00;font-family:monospace;font-size:24px;padding:4px 8px;min-width:60px;text-align:center}
.game-mines .face-btn{font-size:24px;cursor:pointer;background:#c0c0c0;border:2px outset #fff;padding:4px 8px}
.game-mines .mines-grid{display:grid;gap:0;border:3px inset #808080}
.game-mines .mine-cell{width:24px;height:24px;border:2px outset #fff;background:#c0c0c0;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700}
.game-mines .mine-cell.revealed{border:1px solid #808080;background:#c0c0c0}
.game-mines .mine-cell.mine{background:#f00}
.game-mines .mine-cell[data-n="1"]{color:#0000ff}
.game-mines .mine-cell[data-n="2"]{color:#008000}
.game-mines .mine-cell[data-n="3"]{color:#ff0000}
.game-mines .mine-cell[data-n="4"]{color:#000080}
.game-mines .mine-cell[data-n="5"]{color:#800000}
/* Tetris */
.game-tetris{background:#111;padding:20px;border-radius:12px;display:flex;gap:20px}
.game-tetris canvas{border:2px solid #333;border-radius:4px}
.game-tetris .side-panel{color:#fff;width:120px}
.game-tetris .side-panel h4{margin-bottom:8px;color:#888}
.game-tetris .side-panel .score{font-size:24px;margin-bottom:20px}
.game-tetris .next-piece{background:#222;padding:10px;border-radius:8px;margin-bottom:16px}
.game-tetris .next-piece canvas{display:block;margin:0 auto}
.game-tetris .controls-info{font-size:12px;color:#666;line-height:1.8}
</style></head><body>
<div class="container">
<div class="header"><h1>&#127918; GameHub</h1><p>Chon game de choi</p></div>
<div class="games-grid">
<div class="game-card" onclick="openGame('2048')" style="--c1:#f093fb;--c2:#f5576c">
<div class="game-preview">&#127922;</div>
<div class="game-info"><h3>2048</h3><p>Ghep so, dat 2048!</p></div>
</div>
<div class="game-card" onclick="openGame('snake')" style="--c1:#4facfe;--c2:#00f2fe">
<div class="game-preview">&#128013;</div>
<div class="game-info"><h3>Snake</h3><p>Ran san moi co dien</p></div>
</div>
<div class="game-card" onclick="openGame('memory')" style="--c1:#43e97b;--c2:#38f9d7">
<div class="game-preview">&#129504;</div>
<div class="game-info"><h3>Memory</h3><p>Tim cap the giong nhau</p></div>
</div>
<div class="game-card" onclick="openGame('minesweeper')" style="--c1:#fa709a;--c2:#fee140">
<div class="game-preview">&#128163;</div>
<div class="game-info"><h3>Minesweeper</h3><p>Do min kinh dien</p></div>
</div>
<div class="game-card" onclick="openGame('tetris')" style="--c1:#a18cd1;--c2:#fbc2eb">
<div class="game-preview">&#129513;</div>
<div class="game-info"><h3>Tetris</h3><p>Xep gach huyen thoai</p></div>
</div>
<div class="game-card" onclick="window.parent.postMessage({openApp:'balatro'},'*')" style="--c1:#ff6b6b;--c2:#feca57">
<div class="game-preview">&#127183;</div>
<div class="game-info"><h3>Balatro</h3><p>Poker roguelike</p></div>
</div>
</div>
</div>

<div class="game-modal" id="gameModal">
<div class="modal-header">
<h2 id="gameTitle">Game</h2>
<button class="close-btn" onclick="closeGame()">&times;</button>
</div>
<div class="modal-body">
<div class="game-frame" id="gameFrame"></div>
</div>
</div>

<script>
// Game Manager
function openGame(game){
document.getElementById('gameModal').classList.add('active');
document.getElementById('gameTitle').textContent=game.charAt(0).toUpperCase()+game.slice(1);
var frame=document.getElementById('gameFrame');
frame.innerHTML='';
if(game==='2048')init2048(frame);
else if(game==='snake')initSnake(frame);
else if(game==='memory')initMemory(frame);
else if(game==='minesweeper')initMinesweeper(frame);
else if(game==='tetris')initTetris(frame);
}
function closeGame(){
document.getElementById('gameModal').classList.remove('active');
document.getElementById('gameFrame').innerHTML='';
if(window.gameInterval)clearInterval(window.gameInterval);
}
document.addEventListener('keydown',function(e){
if(e.key==='Escape')closeGame();
});

// ===== 2048 =====
function init2048(container){
var g={grid:[[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]],score:0};
var html='<div class="game-2048"><div class="score-board"><div class="score-box"><span>SCORE</span><strong id="score2048">0</strong></div><div class="score-box"><span>BEST</span><strong id="best2048">'+(localStorage.getItem('best2048')||0)+'</strong></div></div><div class="grid-2048" id="grid2048"></div><button class="restart-btn" onclick="init2048(this.parentElement.parentElement)">New Game</button></div>';
container.innerHTML=html;
function addTile(){var empty=[];for(var i=0;i<4;i++)for(var j=0;j<4;j++)if(g.grid[i][j]===0)empty.push([i,j]);if(empty.length){var[r,c]=empty[Math.floor(Math.random()*empty.length)];g.grid[r][c]=Math.random()<0.9?2:4;}}
function render(){var grid=document.getElementById('grid2048');grid.innerHTML='';for(var i=0;i<4;i++)for(var j=0;j<4;j++){var cell=document.createElement('div');cell.className='cell';cell.dataset.value=g.grid[i][j];cell.textContent=g.grid[i][j]||'';grid.appendChild(cell);}document.getElementById('score2048').textContent=g.score;var best=parseInt(localStorage.getItem('best2048')||0);if(g.score>best){localStorage.setItem('best2048',g.score);document.getElementById('best2048').textContent=g.score;}}
function move(dir){var moved=false,newGrid=JSON.parse(JSON.stringify(g.grid));function slide(row){var arr=row.filter(x=>x);for(var i=0;i<arr.length-1;i++)if(arr[i]===arr[i+1]){arr[i]*=2;g.score+=arr[i];arr.splice(i+1,1);}while(arr.length<4)arr.push(0);return arr;}
if(dir==='left')for(var i=0;i<4;i++)newGrid[i]=slide(newGrid[i]);
else if(dir==='right')for(var i=0;i<4;i++)newGrid[i]=slide(newGrid[i].reverse()).reverse();
else if(dir==='up')for(var j=0;j<4;j++){var col=[newGrid[0][j],newGrid[1][j],newGrid[2][j],newGrid[3][j]];col=slide(col);for(var i=0;i<4;i++)newGrid[i][j]=col[i];}
else if(dir==='down')for(var j=0;j<4;j++){var col=[newGrid[3][j],newGrid[2][j],newGrid[1][j],newGrid[0][j]];col=slide(col);for(var i=0;i<4;i++)newGrid[3-i][j]=col[i];}
for(var i=0;i<4;i++)for(var j=0;j<4;j++)if(newGrid[i][j]!==g.grid[i][j])moved=true;
if(moved){g.grid=newGrid;addTile();render();}}
addTile();addTile();render();
document.onkeydown=function(e){if(['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();var dirs={ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right'};move(dirs[e.key]);}};
}

// ===== Snake =====
function initSnake(container){
var html='<div class="game-snake"><div class="snake-header"><span>Score: <span id="snakeScore">0</span></span><span>Best: <span id="snakeBest">'+(localStorage.getItem('snakeBest')||0)+'</span></span></div><canvas id="snakeCanvas" width="320" height="320"></canvas><div class="controls"><button onclick="snakeDir=\'up\'">&#9650;</button><div></div><div></div><button onclick="snakeDir=\'left\'">&#9664;</button><button onclick="initSnake(this.closest(\\\'.game-snake\\\').parentElement)">&#8635;</button><button onclick="snakeDir=\'right\'">&#9654;</button><div></div><button onclick="snakeDir=\'down\'">&#9660;</button><div></div></div></div>';
container.innerHTML=html;
var canvas=document.getElementById('snakeCanvas'),ctx=canvas.getContext('2d');
var size=20,snake=[{x:8,y:8}],food={x:12,y:8},score=0;
window.snakeDir='right';var nextDir='right';
function draw(){ctx.fillStyle='#1a1a2e';ctx.fillRect(0,0,320,320);ctx.fillStyle='#f5576c';ctx.beginPath();ctx.arc(food.x*size+size/2,food.y*size+size/2,size/2-2,0,Math.PI*2);ctx.fill();ctx.fillStyle='#4facfe';snake.forEach(function(s,i){ctx.fillRect(s.x*size+1,s.y*size+1,size-2,size-2);});}
function update(){nextDir=window.snakeDir;var head={x:snake[0].x,y:snake[0].y};if(nextDir==='up')head.y--;else if(nextDir==='down')head.y++;else if(nextDir==='left')head.x--;else if(nextDir==='right')head.x++;
if(head.x<0||head.x>=16||head.y<0||head.y>=16||snake.some(function(s){return s.x===head.x&&s.y===head.y;})){var best=parseInt(localStorage.getItem('snakeBest')||0);if(score>best)localStorage.setItem('snakeBest',score);snake=[{x:8,y:8}];window.snakeDir='right';score=0;food={x:Math.floor(Math.random()*16),y:Math.floor(Math.random()*16)};document.getElementById('snakeScore').textContent=0;document.getElementById('snakeBest').textContent=localStorage.getItem('snakeBest')||0;return;}
snake.unshift(head);if(head.x===food.x&&head.y===food.y){score++;document.getElementById('snakeScore').textContent=score;do{food={x:Math.floor(Math.random()*16),y:Math.floor(Math.random()*16)};}while(snake.some(function(s){return s.x===food.x&&s.y===food.y;}));}else{snake.pop();}}
function loop(){update();draw();}
if(window.gameInterval)clearInterval(window.gameInterval);
window.gameInterval=setInterval(loop,120);
document.onkeydown=function(e){var dirs={ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right'};if(dirs[e.key]){e.preventDefault();var opp={up:'down',down:'up',left:'right',right:'left'};if(dirs[e.key]!==opp[nextDir])window.snakeDir=dirs[e.key];}};
}

// ===== Memory =====
function initMemory(container){
var emojis=['&#128054;','&#128049;','&#128059;','&#128048;','&#128053;','&#128055;','&#128056;','&#128058;'];
var cards=[...emojis,...emojis].sort(function(){return Math.random()-0.5;});
var html='<div class="game-memory"><div class="memory-header"><span>Moves: <span id="memMoves">0</span></span><span>Pairs: <span id="memPairs">0</span>/8</span></div><div class="memory-grid" id="memGrid"></div><button class="restart-btn" onclick="initMemory(this.parentElement.parentElement)">New Game</button></div>';
container.innerHTML=html;
var grid=document.getElementById('memGrid'),flipped=[],moves=0,pairs=0,locked=false;
cards.forEach(function(emoji,i){var card=document.createElement('div');card.className='memory-card';card.innerHTML='<span class="front">'+emoji+'</span>';card.dataset.idx=i;card.onclick=function(){flipCard(this);};grid.appendChild(card);});
function flipCard(card){if(locked||card.classList.contains('flipped')||card.classList.contains('matched'))return;card.classList.add('flipped');flipped.push(card);if(flipped.length===2){moves++;document.getElementById('memMoves').textContent=moves;locked=true;setTimeout(checkMatch,600);}}
function checkMatch(){if(flipped[0].innerHTML===flipped[1].innerHTML){flipped[0].classList.add('matched');flipped[1].classList.add('matched');pairs++;document.getElementById('memPairs').textContent=pairs;if(pairs===8)setTimeout(function(){alert('You won in '+moves+' moves!');},300);}else{flipped[0].classList.remove('flipped');flipped[1].classList.remove('flipped');}flipped=[];locked=false;}
}

// ===== Minesweeper =====
function initMinesweeper(container){
var rows=9,cols=9,mines=10,grid=[],revealed=[],flagged=[],gameOver=false,firstClick=true;
var html='<div class="game-mines"><div class="mines-header"><div class="counter" id="mineCount">'+mines+'</div><button class="face-btn" id="faceBTN" onclick="initMinesweeper(this.closest(\\\'.game-mines\\\').parentElement)">&#128578;</button><div class="counter" id="timer">000</div></div><div class="mines-grid" id="minesGrid" style="grid-template-columns:repeat('+cols+',24px)"></div></div>';
container.innerHTML=html;
for(var i=0;i<rows;i++){grid[i]=[];revealed[i]=[];flagged[i]=[];for(var j=0;j<cols;j++){grid[i][j]=0;revealed[i][j]=false;flagged[i][j]=false;}}
function placeMines(sr,sc){var placed=0;while(placed<mines){var r=Math.floor(Math.random()*rows),c=Math.floor(Math.random()*cols);if(grid[r][c]!==-1&&!(r===sr&&c===sc)){grid[r][c]=-1;placed++;for(var dr=-1;dr<=1;dr++)for(var dc=-1;dc<=1;dc++){var nr=r+dr,nc=c+dc;if(nr>=0&&nr<rows&&nc>=0&&nc<cols&&grid[nr][nc]!==-1)grid[nr][nc]++;}}}}
function render(){var g=document.getElementById('minesGrid');g.innerHTML='';for(var i=0;i<rows;i++)for(var j=0;j<cols;j++){var cell=document.createElement('div');cell.className='mine-cell';cell.dataset.r=i;cell.dataset.c=j;if(revealed[i][j]){cell.classList.add('revealed');if(grid[i][j]===-1){cell.classList.add('mine');cell.innerHTML='&#128163;';}else if(grid[i][j]>0){cell.textContent=grid[i][j];cell.dataset.n=grid[i][j];}}else if(flagged[i][j]){cell.innerHTML='&#128681;';}cell.onclick=function(e){click(parseInt(this.dataset.r),parseInt(this.dataset.c));};cell.oncontextmenu=function(e){e.preventDefault();flag(parseInt(this.dataset.r),parseInt(this.dataset.c));};g.appendChild(cell);}}
function click(r,c){if(gameOver||revealed[r][c]||flagged[r][c])return;if(firstClick){firstClick=false;placeMines(r,c);}revealed[r][c]=true;if(grid[r][c]===-1){gameOver=true;document.getElementById('faceBTN').innerHTML='&#128565;';for(var i=0;i<rows;i++)for(var j=0;j<cols;j++)if(grid[i][j]===-1)revealed[i][j]=true;}else if(grid[r][c]===0){for(var dr=-1;dr<=1;dr++)for(var dc=-1;dc<=1;dc++){var nr=r+dr,nc=c+dc;if(nr>=0&&nr<rows&&nc>=0&&nc<cols)click(nr,nc);}}checkWin();render();}
function flag(r,c){if(gameOver||revealed[r][c])return;flagged[r][c]=!flagged[r][c];var cnt=0;for(var i=0;i<rows;i++)for(var j=0;j<cols;j++)if(flagged[i][j])cnt++;document.getElementById('mineCount').textContent=mines-cnt;render();}
function checkWin(){var unrevealed=0;for(var i=0;i<rows;i++)for(var j=0;j<cols;j++)if(!revealed[i][j]&&grid[i][j]!==-1)unrevealed++;if(unrevealed===0){gameOver=true;document.getElementById('faceBTN').innerHTML='&#128526;';}}
render();
}

// ===== Tetris =====
function initTetris(container){
var html='<div class="game-tetris"><canvas id="tetrisCanvas" width="200" height="400"></canvas><div class="side-panel"><h4>SCORE</h4><div class="score" id="tetrisScore">0</div><h4>NEXT</h4><div class="next-piece"><canvas id="nextCanvas" width="80" height="80"></canvas></div><div class="controls-info">&#9664; &#9654; Move<br>&#9650; Rotate<br>&#9660; Drop<br>Space Hard Drop</div></div></div>';
container.innerHTML=html;
var canvas=document.getElementById('tetrisCanvas'),ctx=canvas.getContext('2d');
var nextCanvas=document.getElementById('nextCanvas'),nextCtx=nextCanvas.getContext('2d');
var cols=10,rows=20,size=20,score=0;
var board=[];for(var i=0;i<rows;i++){board[i]=[];for(var j=0;j<cols;j++)board[i][j]=0;}
var pieces=[[[1,1,1,1]],[[1,1],[1,1]],[[0,1,0],[1,1,1]],[[1,0,0],[1,1,1]],[[0,0,1],[1,1,1]],[[0,1,1],[1,1,0]],[[1,1,0],[0,1,1]]];
var colors=['#00f0f0','#f0f000','#a000f0','#f0a000','#0000f0','#00f000','#f00000'];
var current,currentX,currentY,currentColor,next,nextColor;
function newPiece(){current=next||pieces[Math.floor(Math.random()*pieces.length)];currentColor=nextColor||colors[Math.floor(Math.random()*colors.length)];currentX=3;currentY=0;next=pieces[Math.floor(Math.random()*pieces.length)];nextColor=colors[Math.floor(Math.random()*colors.length)];drawNext();if(collide(current,currentX,currentY)){gameOverTetris();}}
function collide(piece,px,py){for(var y=0;y<piece.length;y++)for(var x=0;x<piece[y].length;x++)if(piece[y][x]&&(board[py+y]===undefined||board[py+y][px+x]===undefined||board[py+y][px+x]))return true;return false;}
function merge(){for(var y=0;y<current.length;y++)for(var x=0;x<current[y].length;x++)if(current[y][x])board[currentY+y][currentX+x]=currentColor;}
function rotate(){var newPiece=[];for(var x=0;x<current[0].length;x++){newPiece[x]=[];for(var y=current.length-1;y>=0;y--)newPiece[x].push(current[y][x]);}if(!collide(newPiece,currentX,currentY))current=newPiece;}
function clearLines(){var lines=0;for(var y=rows-1;y>=0;y--){var full=true;for(var x=0;x<cols;x++)if(!board[y][x])full=false;if(full){board.splice(y,1);board.unshift(Array(cols).fill(0));lines++;y++;}};if(lines)score+=lines*100;document.getElementById('tetrisScore').textContent=score;}
function draw(){ctx.fillStyle='#111';ctx.fillRect(0,0,200,400);for(var y=0;y<rows;y++)for(var x=0;x<cols;x++)if(board[y][x]){ctx.fillStyle=board[y][x];ctx.fillRect(x*size+1,y*size+1,size-2,size-2);}if(current)for(var y=0;y<current.length;y++)for(var x=0;x<current[y].length;x++)if(current[y][x]){ctx.fillStyle=currentColor;ctx.fillRect((currentX+x)*size+1,(currentY+y)*size+1,size-2,size-2);}}
function drawNext(){nextCtx.fillStyle='#222';nextCtx.fillRect(0,0,80,80);if(next)for(var y=0;y<next.length;y++)for(var x=0;x<next[y].length;x++)if(next[y][x]){nextCtx.fillStyle=nextColor;nextCtx.fillRect(x*20+10,y*20+10,18,18);}}
function drop(){if(!collide(current,currentX,currentY+1)){currentY++;}else{merge();clearLines();newPiece();}draw();}
function move(dir){if(!collide(current,currentX+dir,currentY))currentX+=dir;draw();}
function hardDrop(){while(!collide(current,currentX,currentY+1))currentY++;drop();}
function gameOverTetris(){if(window.gameInterval)clearInterval(window.gameInterval);alert('Game Over! Score: '+score);}
newPiece();draw();
if(window.gameInterval)clearInterval(window.gameInterval);
window.gameInterval=setInterval(drop,500);
document.onkeydown=function(e){if(e.key==='ArrowLeft'){e.preventDefault();move(-1);}else if(e.key==='ArrowRight'){e.preventDefault();move(1);}else if(e.key==='ArrowUp'){e.preventDefault();rotate();draw();}else if(e.key==='ArrowDown'){e.preventDefault();drop();}else if(e.key===' '){e.preventDefault();hardDrop();}};
}
</script>
</body></html>"""

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

@app.route('/embed/workspace')
def embed_workspace():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    return render_template_string(EMBED_WORKSPACE)

@app.route('/embed/user-shares')
def embed_user_shares():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    return render_template_string(EMBED_USER_SHARES)

@app.route('/embed/browser')
def embed_browser():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    return render_template_string(EMBED_BROWSER)

@app.route('/embed/chat')
def embed_chat():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    username = session['user']
    return render_template_string(EMBED_CHAT, username=username)

@app.route('/embed/screen-share')
def embed_screen_share():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    username = session['user']
    return render_template_string(EMBED_SCREEN_SHARE, username=username)

@app.route('/screen-guest')
def screen_guest():
    """Guest access page for screen share - no login required"""
    code = request.args.get('code', '')
    return render_template_string(EMBED_SCREEN_GUEST, code=code)

@app.route('/embed/music-room')
def embed_music_room():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    username = session['user']
    return render_template_string(EMBED_MUSIC_ROOM, username=username)

@app.route('/embed/todo')
def embed_todo():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    username = session['user']
    return render_template_string(EMBED_TODO, username=username)

@app.route('/embed/game-hub')
def embed_game_hub():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    return render_template_string(EMBED_GAME_HUB)


# ===========================================
# Balatro Game (Static files)
# ===========================================

BALATRO_DIR = '/opt/jupyterhub/static/balatro'

@app.route('/balatro/')
def balatro_index():
    """Serve Balatro game"""
    if not session.get('user'):
        return redirect('/')
    return send_from_directory(BALATRO_DIR, 'index.html')

@app.route('/balatro/<path:filename>')
def balatro_static(filename):
    """Serve Balatro static files"""
    if not session.get('user'):
        return 'Unauthorized', 401
    return send_from_directory(BALATRO_DIR, filename)


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
    with open('/tmp/transfer.log', 'a') as logf:
        logf.write(f"[Transfer] user={username}, source={source}, dest={dest}, items={items}, source_path='{source_path}'\n")
    if source not in ('workspace', 's3') or dest not in ('workspace', 's3'):
        return jsonify({'error': 'Invalid source/dest'})
    if not items:
        return jsonify({'error': 'No items selected'})
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
        with open('/tmp/transfer.log', 'a') as logf:
            logf.write(f"[Transfer] S3 prefix: {cfg.get('prefix', 'none') if cfg else 'NO CONFIG'}\n")
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

@app.route('/api/s3/move', methods=['POST'])
def api_s3_move():
    """Move or copy items within S3"""
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'})
    items = data.get('items', [])
    source_path = data.get('source_path', '')
    dest_path = data.get('dest_path', '')
    operation = data.get('operation', 'move')
    if not items:
        return jsonify({'error': 'No items specified'})
    if operation not in ('move', 'copy'):
        return jsonify({'error': 'Invalid operation'})
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'No S3 configured'})
    try:
        success_count, errors = move_s3_items(cfg, items, source_path, dest_path, operation)
        if errors:
            return jsonify({'success': success_count > 0, 'moved': success_count, 'errors': errors})
        return jsonify({'success': True, 'moved': success_count})
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
    with open('/tmp/transfer.log', 'a') as logf:
        logf.write(f"[Shared Transfer] user={username}, source={source}, dest={dest}, items={items}, source_path='{source_path}'\n")
    if source not in ('workspace', 's3') or dest not in ('workspace', 's3'):
        return jsonify({'error': 'Invalid source/dest'})
    if not items:
        return jsonify({'error': 'No items selected'})
    try:
        db = get_db()
        cfg = get_shared_s3_config(db)
        with open('/tmp/transfer.log', 'a') as logf:
            logf.write(f"[Shared Transfer] S3 prefix: {cfg.get('prefix', 'none') if cfg else 'NO CONFIG'}\n")
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
# User-to-User Shares (Share with specific users)
# ===========================================

def _init_user_shares_collection(db):
    """Ensure indexes on user_shares collection"""
    col = db.user_shares
    col.create_index('from_user')
    col.create_index('to_user')
    col.create_index('status')
    col.create_index('created_at')
    return col

def _init_notifications_collection(db):
    """Ensure indexes on notifications collection with TTL"""
    col = db.notifications
    col.create_index('user')
    col.create_index('is_read')
    col.create_index('created_at', expireAfterSeconds=7*24*60*60)  # 7 days TTL
    return col

@app.route('/api/users/search')
def api_users_search():
    """Search users for sharing (excludes admin and self)"""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    q = request.args.get('q', '').strip().lower()
    current_user = session['user']

    try:
        db = get_db()
        query = {'role': {'$ne': 'admin'}, 'username': {'$ne': current_user}}
        if q:
            query['username'] = {'$regex': q, '$options': 'i'}

        users = list(db.users.find(query, {'username': 1, '_id': 0}).limit(20))
        return jsonify({'users': [u['username'] for u in users]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/share-with-user', methods=['POST'])
def api_share_with_user():
    """Share file/folder with another user"""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    to_user = data.get('to_user', '').strip()
    item_name = data.get('item_name', '')
    item_type = data.get('item_type', 'file')  # file or dir
    s3_path = data.get('s3_path', '')  # Directory path
    s3_key = data.get('s3_key', '')    # Full key (optional)
    message = data.get('message', '')

    from_user = session['user']

    # Construct s3_key from s3_path and item_name if not provided
    if not s3_key and item_name:
        s3_key = f"{s3_path}/{item_name}" if s3_path else item_name

    if not to_user or not item_name:
        return jsonify({'error': 'Missing required fields'}), 400

    if to_user == from_user:
        return jsonify({'error': 'Cannot share with yourself'}), 400

    try:
        db = get_db()

        # Check recipient exists and is not admin
        recipient = db.users.find_one({'username': to_user, 'role': {'$ne': 'admin'}})
        if not recipient:
            return jsonify({'error': 'User not found'}), 404

        # Get sender's S3 config
        user_doc = db.users.find_one({'username': from_user})
        s3_config = user_doc.get('s3_config') if user_doc else None
        if not s3_config:
            return jsonify({'error': 'S3 not configured'}), 400

        _init_user_shares_collection(db)

        share_id = str(uuid.uuid4())[:12]
        share_doc = {
            '_id': share_id,
            'from_user': from_user,
            'to_user': to_user,
            'item_name': item_name,
            'item_type': item_type,
            's3_key': s3_key,
            's3_config_snapshot': s3_config,
            'status': 'pending',
            'message': message,
            'created_at': datetime.utcnow()
        }

        db.user_shares.insert_one(share_doc)

        # Create notification
        _init_notifications_collection(db)
        db.notifications.insert_one({
            'user': to_user,
            'type': 'file_share',
            'from_user': from_user,
            'share_id': share_id,
            'title': f'{from_user} shared "{item_name}" with you',
            'is_read': False,
            'created_at': datetime.utcnow()
        })

        # Emit via SocketIO if recipient is online
        if socketio:
            socketio.emit('new_share', {
                'share_id': share_id,
                'from_user': from_user,
                'item_name': item_name,
                'message': message
            }, room=to_user)

        return jsonify({'success': True, 'share_id': share_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/user-shares/incoming')
def api_user_shares_incoming():
    """Get shares received by current user"""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        db = get_db()
        shares = list(db.user_shares.find(
            {'to_user': session['user']},
            {'s3_config_snapshot': 0}
        ).sort('created_at', -1).limit(50))

        for s in shares:
            s['_id'] = str(s['_id'])
            s['created_at'] = s['created_at'].isoformat() if s.get('created_at') else None

        return jsonify({'shares': shares})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/user-shares/sent')
def api_user_shares_sent():
    """Get shares sent by current user"""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        db = get_db()
        shares = list(db.user_shares.find(
            {'from_user': session['user']},
            {'s3_config_snapshot': 0}
        ).sort('created_at', -1).limit(50))

        for s in shares:
            s['_id'] = str(s['_id'])
            s['created_at'] = s['created_at'].isoformat() if s.get('created_at') else None

        return jsonify({'shares': shares})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/user-shares/accept', methods=['POST'])
def api_user_shares_accept():
    """Accept a share and copy to workspace"""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    share_id = data.get('share_id', '')
    dest_path = data.get('dest_path', '')  # Optional subfolder in workspace

    try:
        db = get_db()
        share = db.user_shares.find_one({'_id': share_id, 'to_user': session['user']})

        if not share:
            return jsonify({'error': 'Share not found'}), 404

        if share['status'] != 'pending':
            return jsonify({'error': 'Share already processed'}), 400

        # Copy from sender's S3 to recipient's workspace
        ok, result = copy_s3_to_workspace(
            share['s3_config_snapshot'],
            share['s3_key'],
            share['item_type'],
            session['user'],
            dest_path,
            share['item_name']
        )

        if ok:
            db.user_shares.update_one(
                {'_id': share_id},
                {'$set': {'status': 'accepted', 'accepted_at': datetime.utcnow()}}
            )
            return jsonify({'success': True, 'path': result})
        else:
            return jsonify({'error': result}), 500

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/user-shares/reject', methods=['POST'])
def api_user_shares_reject():
    """Reject a share"""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    share_id = data.get('share_id', '')

    try:
        db = get_db()
        result = db.user_shares.update_one(
            {'_id': share_id, 'to_user': session['user'], 'status': 'pending'},
            {'$set': {'status': 'rejected', 'rejected_at': datetime.utcnow()}}
        )

        if result.modified_count:
            return jsonify({'success': True})
        return jsonify({'error': 'Share not found or already processed'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/notifications')
def api_notifications():
    """Get user notifications"""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        db = get_db()
        notifs = list(db.notifications.find(
            {'user': session['user']}
        ).sort('created_at', -1).limit(50))

        for n in notifs:
            n['_id'] = str(n['_id'])
            n['created_at'] = n['created_at'].isoformat() if n.get('created_at') else None

        unread = db.notifications.count_documents({'user': session['user'], 'is_read': False})

        return jsonify({'notifications': notifs, 'unread_count': unread})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/notifications/mark-read', methods=['POST'])
def api_notifications_mark_read():
    """Mark notifications as read"""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    notif_ids = data.get('ids', [])

    try:
        db = get_db()
        if notif_ids:
            db.notifications.update_many(
                {'_id': {'$in': notif_ids}, 'user': session['user']},
                {'$set': {'is_read': True}}
            )
        else:
            # Mark all as read
            db.notifications.update_many(
                {'user': session['user']},
                {'$set': {'is_read': True}}
            )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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


@app.route('/api/onlyoffice/callback', methods=['POST'])
def onlyoffice_callback():
    """Handle OnlyOffice save callback"""
    token = request.args.get('token', '')
    payload = verify_onlyoffice_token(token)
    if not payload:
        return jsonify({'error': 1}), 401

    source = payload['source']
    path = payload['path']
    username = payload['username']

    try:
        data = request.json
        status = data.get('status', 0)
        app.logger.info(f"OnlyOffice callback: status={status}, source={source}, path={path}")

        # Status 2 = document ready to save, 6 = forcesave
        if status in (2, 6):
            download_url = data.get('url')
            if not download_url:
                return jsonify({'error': 1})

            # Download modified file from OnlyOffice
            import requests as http_requests
            resp = http_requests.get(download_url, timeout=60)
            if resp.status_code != 200:
                app.logger.error(f"Failed to download from OnlyOffice: {resp.status_code}")
                return jsonify({'error': 1})

            file_data = resp.content
            db = get_db()

            if source == 'workspace':
                # Save to workspace
                workspace_path = f"/home/{username}/workspace/{path}"
                os.makedirs(os.path.dirname(workspace_path), exist_ok=True)
                with open(workspace_path, 'wb') as f:
                    f.write(file_data)
                app.logger.info(f"Saved to workspace: {workspace_path}")

            elif source == 's3':
                # Save to user's S3
                cfg = get_s3_config(db, username)
                if cfg:
                    ok, result = upload_to_s3(cfg, os.path.dirname(path), os.path.basename(path), file_data)
                    if ok:
                        app.logger.info(f"Saved to S3: {path}")
                    else:
                        app.logger.error(f"Failed to save to S3: {result}")
                        return jsonify({'error': 1})

            elif source == 'shared':
                # Save to shared space
                cfg = get_shared_s3_config(db)
                if cfg:
                    ok, result = upload_to_s3(cfg, os.path.dirname(path), os.path.basename(path), file_data)
                    if ok:
                        app.logger.info(f"Saved to shared: {path}")
                    else:
                        app.logger.error(f"Failed to save to shared: {result}")
                        return jsonify({'error': 1})

        return jsonify({'error': 0})  # Success

    except Exception as e:
        app.logger.error(f"OnlyOffice callback error: {e}")
        return jsonify({'error': 1})


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
        callback_url = f"{ONLYOFFICE_FILE_HOST}/api/onlyoffice/callback?token={file_token}"

        # Check if file is editable (office formats only)
        editable_exts = ['doc', 'docx', 'odt', 'rtf', 'xls', 'xlsx', 'ods', 'csv', 'ppt', 'pptx', 'odp']
        can_edit = ext in editable_exts

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
                    "edit": can_edit,
                    "review": False,
                    "comment": False,
                }
            },
            "documentType": doc_type,
            "editorConfig": {
                "mode": "edit" if can_edit else "view",
                "lang": "vi",
                "callbackUrl": callback_url if can_edit else None,
                "customization": {
                    "forcesave": True,
                    "autosave": True,
                    "hideRightMenu": True,
                    "compactHeader": True,
                    "toolbarNoTabs": False,
                    "compactToolbar": False,
                }
            },
            "height": "100%",
            "width": "100%",
        }
        # Remove None values
        if not can_edit:
            del config["editorConfig"]["callbackUrl"]
        # Sign with JWT for OnlyOffice API (disabled when JWT_ENABLED=false)
        # token = jwt.encode(config, ONLYOFFICE_JWT_SECRET, algorithm='HS256')
        # config['token'] = token
        return render_template_string(VIEWER_OFFICE, filename=filename, icon=icon, download_url=download_url,
                                      onlyoffice_url=ONLYOFFICE_URL, config_json=json.dumps(config))
    else:
        return render_template_string(VIEWER_UNSUPPORTED, filename=filename, download_url=download_url)


# ===========================================
# Chat WebSocket Handlers (Realtime)
# ===========================================

# Track online users: sid -> username
online_users = {}
# Track user sids: username -> set of sids
user_sids = {}

def _init_messages_collection(db):
    """Ensure indexes on messages collection with TTL"""
    col = db.messages
    col.create_index('from_user')
    col.create_index('to_user')
    col.create_index([('from_user', 1), ('to_user', 1)])
    col.create_index('created_at', expireAfterSeconds=7*24*60*60)  # 7 days TTL
    return col

def _init_pending_files_collection(db):
    """Ensure indexes on pending_files collection with TTL"""
    col = db.pending_files
    col.create_index('from_user')
    col.create_index('to_user')
    col.create_index('expires_at', expireAfterSeconds=0)  # TTL
    return col

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    # Get username from session
    username = session.get('user')
    if not username or session.get('is_admin'):
        return False  # Reject connection

    sid = request.sid
    online_users[sid] = username

    if username not in user_sids:
        user_sids[username] = set()
    user_sids[username].add(sid)

    # Join personal room for direct messages
    join_room(username)

    # Notify others that user came online (only if first connection)
    if len(user_sids[username]) == 1:
        emit('user_status', {'user': username, 'status': 'online'}, broadcast=True)

    app.logger.info(f"Chat: {username} connected (sid={sid})")

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    sid = request.sid
    username = online_users.pop(sid, None)

    if username and username in user_sids:
        user_sids[username].discard(sid)
        if not user_sids[username]:
            del user_sids[username]
            # Notify others that user went offline
            emit('user_status', {'user': username, 'status': 'offline'}, broadcast=True)

    app.logger.info(f"Chat: {username} disconnected (sid={sid})")

@socketio.on('get_online_users')
def handle_get_online_users():
    """Get list of online users"""
    username = session.get('user')
    if not username:
        return

    online_list = list(user_sids.keys())
    # Exclude self
    if username in online_list:
        online_list.remove(username)

    emit('online_users', {'users': online_list})

@socketio.on('send_message')
def handle_send_message(data):
    """Send a text message to another user"""
    from_user = session.get('user')
    if not from_user:
        return

    to_user = data.get('to_user', '').strip()
    content = data.get('content', '').strip()
    temp_id = data.get('temp_id', '')

    if not to_user or not content:
        return

    if to_user == from_user:
        return

    try:
        db = get_db()
        _init_messages_collection(db)

        msg_id = str(uuid.uuid4())[:16]
        msg_doc = {
            '_id': msg_id,
            'from_user': from_user,
            'to_user': to_user,
            'message_type': 'text',
            'content': content,
            'created_at': datetime.utcnow()
        }
        db.messages.insert_one(msg_doc)

        msg_data = {
            'id': msg_id,
            'from_user': from_user,
            'to_user': to_user,
            'message_type': 'text',
            'content': content,
            'created_at': datetime.utcnow().isoformat()
        }

        # Send to recipient
        emit('new_message', msg_data, room=to_user)
        # Echo back to sender with temp_id for mapping
        msg_data['temp_id'] = temp_id
        emit('message_sent', msg_data)

    except Exception as e:
        app.logger.error(f"Chat send_message error: {e}")

@socketio.on('send_file')
def handle_send_file(data):
    """Send a file transfer request to another user"""
    from_user = session.get('user')
    if not from_user:
        return

    to_user = data.get('to_user', '').strip()
    filename = data.get('filename', '')
    s3_path = data.get('s3_path', '')

    if not to_user or not filename or not s3_path:
        return

    if to_user == from_user:
        return

    try:
        db = get_db()

        # Get sender's S3 config
        user_doc = db.users.find_one({'username': from_user})
        s3_config = user_doc.get('s3_config') if user_doc else None
        if not s3_config:
            emit('error', {'message': 'S3 not configured'})
            return

        _init_pending_files_collection(db)

        pending_id = str(uuid.uuid4())[:12]
        expires_at = datetime.utcnow() + timedelta(minutes=30)

        pending_doc = {
            '_id': pending_id,
            'from_user': from_user,
            'to_user': to_user,
            'filename': filename,
            's3_path': s3_path,
            's3_config_snapshot': s3_config,
            'status': 'pending',
            'expires_at': expires_at,
            'created_at': datetime.utcnow()
        }

        db.pending_files.insert_one(pending_doc)

        # Also save as message for history
        _init_messages_collection(db)
        db.messages.insert_one({
            'from_user': from_user,
            'to_user': to_user,
            'message_type': 'file_transfer',
            'content': f'Sent file: {filename}',
            'file_info': {'filename': filename, 'pending_id': pending_id},
            'created_at': datetime.utcnow()
        })

        # Notify recipient
        emit('file_transfer_request', {
            'pending_id': pending_id,
            'from_user': from_user,
            'filename': filename,
            'expires_at': expires_at.isoformat()
        }, room=to_user)

        # Confirm to sender
        emit('file_sent', {'pending_id': pending_id, 'filename': filename, 'to_user': to_user})

    except Exception as e:
        app.logger.error(f"Chat send_file error: {e}")
        emit('error', {'message': str(e)})

@socketio.on('accept_file')
def handle_accept_file(data):
    """Accept a pending file transfer"""
    username = session.get('user')
    if not username:
        return

    pending_id = data.get('pending_id', '')
    dest_path = data.get('dest_path', '')  # Optional subfolder

    try:
        db = get_db()
        pending = db.pending_files.find_one({'_id': pending_id, 'to_user': username, 'status': 'pending'})

        if not pending:
            emit('error', {'message': 'File transfer not found or expired'})
            return

        # Copy file to recipient's workspace
        ok, result = copy_s3_to_workspace(
            pending['s3_config_snapshot'],
            pending['s3_path'],
            'file',
            username,
            dest_path,
            pending['filename']
        )

        if ok:
            db.pending_files.update_one(
                {'_id': pending_id},
                {'$set': {'status': 'accepted', 'accepted_at': datetime.utcnow()}}
            )

            # Notify sender
            emit('file_accepted', {
                'pending_id': pending_id,
                'filename': pending['filename'],
                'by_user': username
            }, room=pending['from_user'])

            emit('file_accept_success', {'pending_id': pending_id, 'path': result})
        else:
            emit('error', {'message': f'Failed to transfer file: {result}'})

    except Exception as e:
        app.logger.error(f"Chat accept_file error: {e}")
        emit('error', {'message': str(e)})

@socketio.on('reject_file')
def handle_reject_file(data):
    """Reject a pending file transfer"""
    username = session.get('user')
    if not username:
        return

    pending_id = data.get('pending_id', '')

    try:
        db = get_db()
        result = db.pending_files.update_one(
            {'_id': pending_id, 'to_user': username, 'status': 'pending'},
            {'$set': {'status': 'rejected', 'rejected_at': datetime.utcnow()}}
        )

        if result.modified_count:
            pending = db.pending_files.find_one({'_id': pending_id})
            if pending:
                emit('file_rejected', {
                    'pending_id': pending_id,
                    'filename': pending['filename'],
                    'by_user': username
                }, room=pending['from_user'])

            emit('file_reject_success', {'pending_id': pending_id})

    except Exception as e:
        app.logger.error(f"Chat reject_file error: {e}")

@socketio.on('get_messages')
def handle_get_messages(data):
    """Get message history with a specific user"""
    username = session.get('user')
    if not username:
        return

    with_user = data.get('with_user', '').strip()
    if not with_user:
        return

    try:
        db = get_db()
        messages = list(db.messages.find({
            '$or': [
                {'from_user': username, 'to_user': with_user},
                {'from_user': with_user, 'to_user': username}
            ]
        }).sort('created_at', 1).limit(100))

        # Mark messages from with_user as read
        db.messages.update_many(
            {'from_user': with_user, 'to_user': username, 'is_read': {'$ne': True}},
            {'$set': {'is_read': True}}
        )

        for m in messages:
            m['_id'] = str(m['_id'])
            # Convert all datetime fields to ISO format
            for key in ['created_at', 'recalled_at', 'accepted_at', 'rejected_at']:
                if m.get(key) and hasattr(m[key], 'isoformat'):
                    m[key] = m[key].isoformat()

        emit('message_history', {'with_user': with_user, 'messages': messages})

    except Exception as e:
        app.logger.error(f"Chat get_messages error: {e}")


@socketio.on('mark_messages_read')
def handle_mark_messages_read(data):
    """Mark all messages from a user as read"""
    username = session.get('user')
    if not username:
        return

    from_user = data.get('from_user', '').strip()
    if not from_user:
        return

    try:
        db = get_db()
        # Mark all messages from this user to current user as read
        db.messages.update_many(
            {'from_user': from_user, 'to_user': username, 'is_read': {'$ne': True}},
            {'$set': {'is_read': True}}
        )
    except Exception as e:
        app.logger.error(f"Chat mark_messages_read error: {e}")


# Chat API endpoints
@app.route('/api/chat/users')
def api_chat_users():
    """Get list of users with online status"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    current_user = session['user']

    try:
        # Get system users (not from MongoDB)
        system_users = get_usernames()

        result = []
        for username in system_users:
            if username != current_user:
                result.append({
                    'username': username,
                    'online': username in user_sids
                })

        # Sort: online first
        result.sort(key=lambda x: (not x['online'], x['username']))

        return jsonify({'users': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chat/pending-files')
def api_chat_pending_files():
    """Get pending file transfers for current user"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        db = get_db()
        pending = list(db.pending_files.find({
            'to_user': session['user'],
            'status': 'pending',
            'expires_at': {'$gt': datetime.utcnow()}
        }).sort('created_at', -1))

        for p in pending:
            p['_id'] = str(p['_id'])
            p['created_at'] = p['created_at'].isoformat() if p.get('created_at') else None
            p['expires_at'] = p['expires_at'].isoformat() if p.get('expires_at') else None
            # Don't expose s3_config_snapshot
            p.pop('s3_config_snapshot', None)

        return jsonify({'pending_files': pending})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===========================================
# Friends API
# ===========================================

def _init_friends_collection(db):
    """Ensure indexes on friends collection"""
    col = db.friends
    col.create_index([('user', 1), ('friend', 1)], unique=True)
    col.create_index('user')
    col.create_index('friend')
    col.create_index('status')
    return col

@app.route('/api/friends/list')
def api_friends_list():
    """Get friends list with pending requests"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']

    try:
        db = get_db()

        # Accepted friends (both directions)
        friends = list(db.friends.find({
            '$or': [
                {'user': username, 'status': 'accepted'},
                {'friend': username, 'status': 'accepted'}
            ]
        }))

        friend_list = []
        for f in friends:
            friend_username = f['friend'] if f['user'] == username else f['user']
            friend_list.append({
                'friend': friend_username,
                'status': 'accepted',
                'since': f.get('accepted_at', f.get('created_at')).isoformat() if f.get('accepted_at') or f.get('created_at') else None
            })

        # Pending requests I sent
        pending_sent = list(db.friends.find({'user': username, 'status': 'pending'}))
        sent_list = [{'to_user': f['friend'], 'created_at': f['created_at'].isoformat() if f.get('created_at') else None} for f in pending_sent]

        # Pending requests I received
        pending_received = list(db.friends.find({'friend': username, 'status': 'pending'}))
        received_list = [{'from_user': f['user'], 'created_at': f['created_at'].isoformat() if f.get('created_at') else None} for f in pending_received]

        return jsonify({
            'friends': friend_list,
            'pending_sent': sent_list,
            'pending_received': received_list
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/friends/search')
def api_friends_search():
    """Search users to add as friends"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    q = request.args.get('q', '').strip().lower()
    current_user = session['user']

    if len(q) < 1:
        return jsonify({'users': []})

    try:
        # Search from system users
        system_users = get_usernames()
        matched = [u for u in system_users if q in u.lower() and u != current_user][:20]

        result = []
        for username in matched:
            result.append({
                'username': username,
                'online': username in user_sids
            })

        return jsonify({'users': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/friends/add', methods=['POST'])
def api_friends_add():
    """Send friend request"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    target_user = data.get('username', '').strip()
    current_user = session['user']

    if not target_user or target_user == current_user:
        return jsonify({'error': 'Invalid user'}), 400

    try:
        db = get_db()
        _init_friends_collection(db)

        # Check if user exists (system users)
        if not user_exists(target_user):
            return jsonify({'error': 'User not found'}), 404

        # Check if already friends or pending
        existing = db.friends.find_one({
            '$or': [
                {'user': current_user, 'friend': target_user},
                {'user': target_user, 'friend': current_user}
            ]
        })

        if existing:
            if existing['status'] == 'accepted':
                return jsonify({'error': 'Already friends'}), 400
            elif existing['user'] == current_user:
                return jsonify({'error': 'Request already sent'}), 400
            else:
                # They sent us a request, auto-accept
                db.friends.update_one(
                    {'_id': existing['_id']},
                    {'$set': {'status': 'accepted', 'accepted_at': datetime.utcnow()}}
                )
                # Notify them
                if socketio:
                    socketio.emit('friend_accepted', {'by_user': current_user}, room=target_user)
                return jsonify({'success': True, 'auto_accepted': True})

        # Create friend request
        db.friends.insert_one({
            'user': current_user,
            'friend': target_user,
            'status': 'pending',
            'created_at': datetime.utcnow()
        })

        # Notify target user
        if socketio:
            socketio.emit('friend_request', {'from_user': current_user}, room=target_user)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/friends/accept', methods=['POST'])
def api_friends_accept():
    """Accept friend request"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    from_user = data.get('username', '').strip()
    current_user = session['user']

    try:
        db = get_db()
        result = db.friends.update_one(
            {'user': from_user, 'friend': current_user, 'status': 'pending'},
            {'$set': {'status': 'accepted', 'accepted_at': datetime.utcnow()}}
        )

        if result.modified_count:
            # Notify requester
            if socketio:
                socketio.emit('friend_accepted', {'by_user': current_user}, room=from_user)
            return jsonify({'success': True})
        return jsonify({'error': 'Request not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/friends/reject', methods=['POST'])
def api_friends_reject():
    """Reject friend request"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    from_user = data.get('username', '').strip()
    current_user = session['user']

    try:
        db = get_db()
        result = db.friends.delete_one({
            'user': from_user, 'friend': current_user, 'status': 'pending'
        })

        return jsonify({'success': result.deleted_count > 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/friends/remove', methods=['POST'])
def api_friends_remove():
    """Remove friend"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    friend_user = data.get('username', '').strip()
    current_user = session['user']

    try:
        db = get_db()
        result = db.friends.delete_one({
            '$or': [
                {'user': current_user, 'friend': friend_user},
                {'user': friend_user, 'friend': current_user}
            ]
        })

        return jsonify({'success': result.deleted_count > 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===========================================
# Chat Contacts & File Upload
# ===========================================

@app.route('/api/chat/contacts')
def api_chat_contacts():
    """Get contacts: only friends with message info"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']

    try:
        db = get_db()

        # Get only accepted friends
        friend_set = set()
        try:
            friends_docs = list(db.friends.find({
                '$or': [
                    {'user': username, 'status': 'accepted'},
                    {'friend': username, 'status': 'accepted'}
                ]
            }))
            for f in friends_docs:
                friend_set.add(f['friend'] if f['user'] == username else f['user'])
        except:
            pass

        # Use friend_set instead of all system users
        system_users = friend_set

        # Get distinct users from messages (both directions)
        pipeline = [
            {'$match': {'$or': [{'from_user': username}, {'to_user': username}]}},
            {'$sort': {'created_at': -1}},
            {'$group': {
                '_id': {'$cond': [{'$eq': ['$from_user', username]}, '$to_user', '$from_user']},
                'last_message': {'$first': '$content'},
                'last_time': {'$first': '$created_at'},
                'message_type': {'$first': '$message_type'},
                'file_info': {'$first': '$file_info'}
            }}
        ]

        contacts_from_msgs = {}
        try:
            for doc in db.messages.aggregate(pipeline):
                last_msg = doc.get('last_message', '')
                if doc.get('message_type') == 'file' and doc.get('file_info'):
                    last_msg = '[File] ' + doc['file_info'].get('filename', '')
                contacts_from_msgs[doc['_id']] = {
                    'last_message': last_msg,
                    'last_time': doc['last_time'].isoformat() if doc.get('last_time') else ''
                }
        except:
            pass

        # Get friends
        friend_set = set()
        try:
            friends_docs = list(db.friends.find({
                '$or': [
                    {'user': username, 'status': 'accepted'},
                    {'friend': username, 'status': 'accepted'}
                ]
            }))
            for f in friends_docs:
                friend_set.add(f['friend'] if f['user'] == username else f['user'])
        except:
            pass

        # Count unread messages
        unread_counts = {}
        try:
            unread_pipeline = [
                {'$match': {'to_user': username, 'is_read': {'$ne': True}}},
                {'$group': {'_id': '$from_user', 'count': {'$sum': 1}}}
            ]
            unread_counts = {doc['_id']: doc['count'] for doc in db.messages.aggregate(unread_pipeline)}
        except:
            pass

        result = []
        for contact in system_users:
            msg_info = contacts_from_msgs.get(contact, {})
            result.append({
                'username': contact,
                'online': contact in user_sids,
                'is_friend': contact in friend_set,
                'last_message': msg_info.get('last_message', ''),
                'last_time': msg_info.get('last_time', ''),
                'unread': unread_counts.get(contact, 0)
            })

        # Sort: friends first, then online, then by last_time
        result.sort(key=lambda x: (
            not x['is_friend'],      # Friends first
            not x['online'],          # Then online users
            not bool(x['last_time']), # Then users with messages
            x['username']             # Then alphabetically
        ))

        return jsonify({'contacts': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chat/upload', methods=['POST'])
def api_chat_upload():
    """Upload file for chat - stores in shared S3"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400

    file = request.files['file']
    to_user = request.form.get('to_user', '').strip()
    from_user = session['user']

    if not file.filename or not to_user:
        return jsonify({'error': 'Missing file or recipient'}), 400

    try:
        db = get_db()

        # Get chat S3 config (separate from shared space)
        cfg = get_chat_s3_config(db)
        if not cfg:
            return jsonify({'error': 'Chat file sharing not configured (no S3)'}), 400

        # Generate unique path for chat files
        file_id = str(uuid.uuid4())[:12]
        timestamp = datetime.utcnow().strftime('%Y%m%d')
        safe_filename = file.filename.replace('/', '_').replace('\\', '_')
        rel_dir = f"chat_files/{timestamp}/{from_user}"
        actual_filename = f"{file_id}_{safe_filename}"
        s3_path = f"{rel_dir}/{actual_filename}"

        # Upload to shared S3
        file_data = file.read()
        file_size = len(file_data)

        ok, result = upload_to_s3(cfg, rel_dir, actual_filename, file_data)

        if not ok:
            return jsonify({'error': f'Upload failed: {result}'}), 500

        # Generate download URL
        download_url = f"/api/chat/file/{file_id}"

        # Store file info in database (status: pending - needs approval)
        db.chat_files.insert_one({
            '_id': file_id,
            'from_user': from_user,
            'to_user': to_user,
            'filename': file.filename,
            'size': file_size,
            's3_path': s3_path,
            'status': 'pending',  # pending -> accepted/rejected
            'created_at': datetime.utcnow()
        })

        # Create message record
        _init_messages_collection(db)
        msg_id = str(uuid.uuid4())[:16]
        msg_doc = {
            '_id': msg_id,
            'from_user': from_user,
            'to_user': to_user,
            'message_type': 'file',
            'content': f'[File] {file.filename}',
            'file_info': {
                'file_id': file_id,
                'filename': file.filename,
                'size': file_size,
                'status': 'pending'  # No download_url until accepted
            },
            'created_at': datetime.utcnow()
        }
        db.messages.insert_one(msg_doc)

        # Notify recipient via WebSocket
        if socketio:
            socketio.emit('new_message', {
                'id': msg_id,
                'from_user': from_user,
                'to_user': to_user,
                'message_type': 'file',
                'content': f'[File] {file.filename}',
                'file_info': {
                    'file_id': file_id,
                    'filename': file.filename,
                    'size': file_size,
                    'status': 'pending'
                },
                'created_at': datetime.utcnow().isoformat()
            }, room=to_user)

        return jsonify({
            'success': True,
            'message_id': msg_id,  # For recall functionality
            'file_id': file_id,
            'filename': file.filename,
            'size': file_size,
            'status': 'pending',
            'download_url': download_url  # Sender can always download their own file
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chat/file/<file_id>')
def api_chat_file_download(file_id):
    """Download chat file (only if accepted)"""
    if 'user' not in session:
        return 'Unauthorized', 401

    username = session['user']

    try:
        db = get_db()
        file_doc = db.chat_files.find_one({'_id': file_id})

        if not file_doc:
            return 'File not found', 404

        # Check permission - only sender or recipient can download
        if username != file_doc['from_user'] and username != file_doc['to_user']:
            if not session.get('is_admin'):
                return 'Forbidden', 403

        # Recipient can only download if accepted (sender can always download)
        if username == file_doc['to_user'] and file_doc.get('status') != 'accepted':
            return 'File not accepted yet', 403

        # Find file in S3 (search multiple locations)
        cfg, s3_key = find_chat_file_in_s3(db, file_doc)
        if not cfg or not s3_key:
            return 'File not found in S3 storage', 404

        gen, length, ctype = stream_s3_object(cfg, s3_key)

        # Properly encode filename for Content-Disposition (RFC 5987)
        filename = file_doc['filename']
        ascii_filename = filename.encode('ascii', 'ignore').decode('ascii') or 'file'

        from urllib.parse import quote
        encoded_filename = quote(filename)

        headers = {
            'Content-Type': 'application/octet-stream',
            'Content-Length': length,
            'Content-Disposition': f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{encoded_filename}"
        }

        return Response(gen, headers=headers)

    except Exception as e:
        return str(e), 500


@app.route('/api/chat/file/accept', methods=['POST'])
def api_chat_file_accept():
    """Accept a received file"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    file_id = data.get('file_id', '')
    username = session['user']

    try:
        db = get_db()
        file_doc = db.chat_files.find_one({'_id': file_id, 'to_user': username})

        if not file_doc:
            return jsonify({'error': 'File not found'}), 404

        # Allow pending or missing status (backwards compatibility)
        current_status = file_doc.get('status', 'pending')
        if current_status not in ('pending', None):
            return jsonify({'error': 'File already processed'}), 400

        # Update status
        db.chat_files.update_one({'_id': file_id}, {'$set': {'status': 'accepted', 'accepted_at': datetime.utcnow()}})

        # Update message
        db.messages.update_one(
            {'file_info.file_id': file_id},
            {'$set': {'file_info.status': 'accepted', 'file_info.download_url': f'/api/chat/file/{file_id}'}}
        )

        # Notify sender
        if socketio:
            socketio.emit('file_status_changed', {
                'file_id': file_id,
                'status': 'accepted',
                'by_user': username
            }, room=file_doc['from_user'])

        return jsonify({'success': True, 'download_url': f'/api/chat/file/{file_id}'})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/chat/file/reject', methods=['POST'])
def api_chat_file_reject():
    """Reject a received file"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    file_id = data.get('file_id', '')
    username = session['user']

    try:
        db = get_db()
        file_doc = db.chat_files.find_one({'_id': file_id, 'to_user': username})

        if not file_doc:
            return jsonify({'error': 'File not found'}), 404

        # Allow pending or missing status (backwards compatibility)
        current_status = file_doc.get('status', 'pending')
        if current_status not in ('pending', None):
            return jsonify({'error': 'File already processed'}), 400

        # Update status
        db.chat_files.update_one({'_id': file_id}, {'$set': {'status': 'rejected', 'rejected_at': datetime.utcnow()}})

        # Update message
        db.messages.update_one(
            {'file_info.file_id': file_id},
            {'$set': {'file_info.status': 'rejected'}}
        )

        # Notify sender
        if socketio:
            socketio.emit('file_status_changed', {
                'file_id': file_id,
                'status': 'rejected',
                'by_user': username
            }, room=file_doc['from_user'])

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


def find_chat_file_in_s3(db, file_doc):
    """Search for chat file in multiple possible S3 locations"""
    import boto3

    sys_cfg = db.s3_system_config.find_one({'_id': 'default'})
    if not sys_cfg:
        return None, None

    s3 = boto3.client('s3',
        endpoint_url=sys_cfg['endpoint_url'],
        aws_access_key_id=sys_cfg['access_key'],
        aws_secret_access_key=sys_cfg['secret_key']
    )
    bucket = sys_cfg['bucket_name']

    s3_path = file_doc.get('s3_path', '')
    file_id = file_doc['_id']
    filename = file_doc.get('filename', '')
    from_user = file_doc.get('from_user', '')

    # Build list of possible keys to check
    possible_keys = []

    # 1. Standard chat location: _chat/chat_files/...
    if s3_path:
        possible_keys.append(f"_chat/{s3_path}")

    # 2. Shared space with full path: _shared/chat_files/...
    if s3_path:
        possible_keys.append(f"_shared/{s3_path}")

    # 3. Shared space with just filename: _shared/{file_id}_{filename}
    possible_keys.append(f"_shared/{file_id}_{filename}")

    # 4. In sender's folder: {from_user}/{filename} or {from_user}/{file_id}_{filename}
    if from_user:
        possible_keys.append(f"{from_user}/{file_id}_{filename}")
        possible_keys.append(f"{from_user}/{filename}")

    # Try each location
    for key in possible_keys:
        try:
            s3.head_object(Bucket=bucket, Key=key)
            # Found! Return config and key
            cfg = {
                'endpoint_url': sys_cfg['endpoint_url'],
                'access_key': sys_cfg['access_key'],
                'secret_key': sys_cfg['secret_key'],
                'bucket_name': bucket,
                'prefix': ''
            }
            return cfg, key
        except:
            continue

    return None, None


@app.route('/api/chat/file/save', methods=['POST'])
def api_chat_file_save():
    """Save accepted file to workspace or S3"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    file_id = data.get('file_id', '')
    dest = data.get('dest', 'workspace')  # 'workspace' or 's3'
    dest_path = data.get('dest_path', '').strip('/')  # Target folder
    username = session['user']

    try:
        db = get_db()
        file_doc = db.chat_files.find_one({'_id': file_id})

        if not file_doc:
            return jsonify({'error': 'File not found'}), 404

        # Check permission
        if username != file_doc['from_user'] and username != file_doc['to_user']:
            return jsonify({'error': 'Forbidden'}), 403

        # Must be accepted (or sender can always access)
        if file_doc.get('status') != 'accepted' and username == file_doc['to_user']:
            return jsonify({'error': 'File not accepted'}), 400

        # Find file in S3 (search multiple locations)
        cfg, s3_key = find_chat_file_in_s3(db, file_doc)
        if not cfg or not s3_key:
            return jsonify({'error': 'File not found in S3 storage'}), 404

        # Download file
        gen, length, ctype = stream_s3_object(cfg, s3_key)
        file_data = b''.join(gen)

        filename = file_doc['filename']

        if dest == 'workspace':
            # Save to user's workspace
            if dest_path:
                workspace_path = f"/home/{username}/workspace/{dest_path}/{filename}"
            else:
                workspace_path = f"/home/{username}/workspace/{filename}"
            os.makedirs(os.path.dirname(workspace_path), exist_ok=True)
            with open(workspace_path, 'wb') as f:
                f.write(file_data)
            return jsonify({'success': True, 'path': f"{dest_path}/{filename}" if dest_path else filename})

        elif dest == 's3':
            # Save to user's S3 backup
            user_s3_cfg = get_s3_config(db, username)
            if not user_s3_cfg:
                return jsonify({'error': 'S3 Backup not configured'}), 400

            ok, result = upload_to_s3(user_s3_cfg, dest_path, filename, file_data)
            if ok:
                return jsonify({'success': True, 'path': f"{dest_path}/{filename}" if dest_path else filename})
            else:
                return jsonify({'error': result}), 500

        return jsonify({'error': 'Invalid destination'}), 400

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/chat/file-to-workspace', methods=['POST'])
def api_chat_file_to_workspace():
    """Quick save chat file to workspace (for drag & drop)"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    file_id = data.get('file_id', '')
    username = session['user']

    try:
        db = get_db()
        file_doc = db.chat_files.find_one({'_id': file_id})

        if not file_doc:
            return jsonify({'error': 'File not found'}), 404

        # Check permission
        if username != file_doc['from_user'] and username != file_doc['to_user']:
            return jsonify({'error': 'Forbidden'}), 403

        # Must be accepted (for receiver) or sender
        if file_doc.get('status') != 'accepted' and username == file_doc['to_user']:
            return jsonify({'error': 'File not accepted yet'}), 400

        # Find file in S3 (search multiple locations)
        cfg, s3_key = find_chat_file_in_s3(db, file_doc)
        if not cfg or not s3_key:
            return jsonify({'error': 'File not found in S3 storage'}), 404

        # Stream from S3
        gen, length, ctype = stream_s3_object(cfg, s3_key)
        file_data = b''.join(gen)

        # Save to workspace
        workspace_path = f"/home/{username}/workspace/{file_doc['filename']}"
        os.makedirs(os.path.dirname(workspace_path), exist_ok=True)
        with open(workspace_path, 'wb') as f:
            f.write(file_data)

        return jsonify({'success': True, 'filename': file_doc['filename']})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/chat/message/recall', methods=['POST'])
def api_chat_message_recall():
    """Recall (delete) a sent message"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    message_id = data.get('message_id', '')
    with_user = data.get('with_user', '')
    username = session['user']

    try:
        db = get_db()

        # Find the message - must be from current user
        # Try string _id first, then ObjectId for old messages
        msg = db.messages.find_one({'_id': message_id, 'from_user': username})

        if not msg:
            # Try with ObjectId for old messages
            from bson import ObjectId
            try:
                oid = ObjectId(message_id)
                msg = db.messages.find_one({'_id': oid, 'from_user': username})
                if msg:
                    message_id = oid  # Use ObjectId for update
            except:
                pass

        if not msg:
            return jsonify({'error': 'Message not found or not yours'}), 404

        # Mark as recalled (don't delete, just mark)
        db.messages.update_one(
            {'_id': message_id},
            {'$set': {'recalled': True, 'recalled_at': datetime.utcnow()}}
        )

        # If it's a file message, delete from S3 and update chat_files
        if msg.get('message_type') == 'file' and msg.get('file_info', {}).get('file_id'):
            file_id = msg['file_info']['file_id']
            file_doc = db.chat_files.find_one({'_id': file_id})
            if file_doc:
                # Delete from S3
                try:
                    cfg = get_chat_s3_config(db)
                    if cfg:
                        import boto3
                        s3 = boto3.client('s3',
                            endpoint_url=cfg['endpoint_url'],
                            aws_access_key_id=cfg['access_key'],
                            aws_secret_access_key=cfg['secret_key'],
                            region_name=cfg.get('region', 'us-east-1')
                        )
                        prefix = cfg.get('prefix', '').strip('/')
                        s3_key = f"{prefix}/{file_doc['s3_path']}" if prefix else file_doc['s3_path']
                        s3.delete_object(Bucket=cfg['bucket_name'], Key=s3_key)
                except Exception as e:
                    app.logger.error(f"Error deleting file from S3: {e}")

                # Mark chat_file as recalled
                db.chat_files.update_one(
                    {'_id': file_id},
                    {'$set': {'recalled': True, 'recalled_at': datetime.utcnow()}}
                )

        # Notify recipient
        if socketio:
            socketio.emit('message_recalled', {
                'message_id': str(message_id),  # Convert ObjectId to string
                'from_user': username
            }, room=msg['to_user'])

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===========================================
# Todo/Notes API Endpoints
# ===========================================

def _init_todos_collection(db):
    """Ensure indexes on todos collection"""
    col = db.todos
    col.create_index('creator')
    col.create_index('assignee')
    col.create_index([('creator', 1), ('assignee', 1)])
    col.create_index('created_at')
    return col

@app.route('/api/todos', methods=['GET'])
def api_todos_list():
    """List tasks based on tab filter"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']
    tab = request.args.get('tab', 'my')
    status = request.args.get('status', '')
    priority = request.args.get('priority', '')

    try:
        db = get_db()
        _init_todos_collection(db)

        # Build query based on tab
        if tab == 'my':
            # My own tasks (assignee is empty or self)
            query = {'creator': username, '$or': [{'assignee': ''}, {'assignee': None}, {'assignee': username}]}
        elif tab == 'assigned':
            # Tasks assigned to me by others
            query = {'$or': [
                {'assignee': username, 'creator': {'$ne': username}},
                {'assignee': '__all__'}
            ]}
        elif tab == 'created':
            # Tasks I created and assigned to others
            query = {'creator': username, 'assignee': {'$nin': ['', None, username]}}
        else:
            query = {'$or': [{'creator': username}, {'assignee': username}, {'assignee': '__all__'}]}

        # Apply filters
        if status:
            query['status'] = status
        if priority:
            query['priority'] = priority

        tasks = list(db.todos.find(query).sort('created_at', -1).limit(100))

        # Convert ObjectId to string
        for t in tasks:
            t['_id'] = str(t['_id'])
            for key in ['created_at', 'updated_at', 'completed_at', 'due_date']:
                if t.get(key) and hasattr(t[key], 'isoformat'):
                    t[key] = t[key].isoformat()
            # Convert comment dates
            for c in t.get('comments', []):
                if c.get('created_at') and hasattr(c['created_at'], 'isoformat'):
                    c['created_at'] = c['created_at'].isoformat()

        # Get counts for each tab
        counts = {
            'my': db.todos.count_documents({'creator': username, '$or': [{'assignee': ''}, {'assignee': None}, {'assignee': username}]}),
            'assigned': db.todos.count_documents({'$or': [{'assignee': username, 'creator': {'$ne': username}}, {'assignee': '__all__'}]}),
            'created': db.todos.count_documents({'creator': username, 'assignee': {'$nin': ['', None, username]}})
        }

        return jsonify({'tasks': tasks, 'counts': counts})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/todos', methods=['POST'])
def api_todos_create():
    """Create a new task"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']
    data = request.json

    try:
        db = get_db()
        _init_todos_collection(db)

        task_id = str(uuid.uuid4())[:12]
        task = {
            '_id': task_id,
            'creator': username,
            'assignee': data.get('assignee', '') or '',
            'title': data.get('title', '').strip(),
            'description': data.get('description', '').strip(),
            'priority': data.get('priority', 'medium'),
            'status': data.get('status', 'pending'),
            'due_date': datetime.fromisoformat(data['due_date']) if data.get('due_date') else None,
            'comments': [],
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow()
        }

        if not task['title']:
            return jsonify({'error': 'Title is required'}), 400

        db.todos.insert_one(task)

        # Notify assignee if assigned to someone else
        assignee = task['assignee']
        if assignee and assignee not in ['', username]:
            if assignee == '__all__':
                # Notify all users
                socketio.emit('task_assigned', {
                    'task_id': task_id,
                    'title': task['title'],
                    'from_user': username
                }, broadcast=True)
            else:
                socketio.emit('task_assigned', {
                    'task_id': task_id,
                    'title': task['title'],
                    'from_user': username
                }, room=assignee)

        return jsonify({'success': True, 'task_id': task_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/todos/<task_id>', methods=['GET'])
def api_todos_get(task_id):
    """Get single task details"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']

    try:
        db = get_db()
        task = db.todos.find_one({'_id': task_id})
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        # Check permission
        if task['creator'] != username and task.get('assignee') not in [username, '__all__']:
            return jsonify({'error': 'Unauthorized'}), 403

        task['_id'] = str(task['_id'])
        for key in ['created_at', 'updated_at', 'completed_at', 'due_date']:
            if task.get(key) and hasattr(task[key], 'isoformat'):
                task[key] = task[key].isoformat()
        for c in task.get('comments', []):
            if c.get('created_at') and hasattr(c['created_at'], 'isoformat'):
                c['created_at'] = c['created_at'].isoformat()

        return jsonify({'task': task})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/todos/<task_id>', methods=['PUT'])
def api_todos_update(task_id):
    """Update a task"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']
    data = request.json

    try:
        db = get_db()
        task = db.todos.find_one({'_id': task_id})
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        # Check permission
        if task['creator'] != username and task.get('assignee') not in [username, '__all__']:
            return jsonify({'error': 'Unauthorized'}), 403

        update = {'$set': {'updated_at': datetime.utcnow()}}
        if 'title' in data:
            update['$set']['title'] = data['title'].strip()
        if 'description' in data:
            update['$set']['description'] = data['description'].strip()
        if 'priority' in data:
            update['$set']['priority'] = data['priority']
        if 'status' in data:
            update['$set']['status'] = data['status']
            if data['status'] == 'completed':
                update['$set']['completed_at'] = datetime.utcnow()
        if 'assignee' in data:
            update['$set']['assignee'] = data['assignee']
        if 'due_date' in data:
            update['$set']['due_date'] = datetime.fromisoformat(data['due_date']) if data['due_date'] else None

        db.todos.update_one({'_id': task_id}, update)

        # Notify about update
        if task['creator'] != username:
            socketio.emit('task_updated', {'task_id': task_id}, room=task['creator'])
        if task.get('assignee') and task['assignee'] not in ['', username]:
            if task['assignee'] == '__all__':
                socketio.emit('task_updated', {'task_id': task_id}, broadcast=True)
            else:
                socketio.emit('task_updated', {'task_id': task_id}, room=task['assignee'])

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/todos/<task_id>', methods=['DELETE'])
def api_todos_delete(task_id):
    """Delete a task"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']

    try:
        db = get_db()
        result = db.todos.delete_one({'_id': task_id, 'creator': username})
        return jsonify({'success': result.deleted_count > 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/todos/<task_id>/status', methods=['PUT'])
def api_todos_status(task_id):
    """Update task status only"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']
    data = request.json

    try:
        db = get_db()
        task = db.todos.find_one({'_id': task_id})
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        if task['creator'] != username and task.get('assignee') not in [username, '__all__']:
            return jsonify({'error': 'Unauthorized'}), 403

        update = {'$set': {'status': data['status'], 'updated_at': datetime.utcnow()}}
        if data['status'] == 'completed':
            update['$set']['completed_at'] = datetime.utcnow()

        db.todos.update_one({'_id': task_id}, update)

        # Notify if completed
        if data['status'] == 'completed':
            if task['creator'] != username:
                socketio.emit('task_completed', {
                    'task_id': task_id,
                    'title': task['title'],
                    'by_user': username
                }, room=task['creator'])

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/todos/<task_id>/comment', methods=['POST'])
def api_todos_comment(task_id):
    """Add a comment to a task"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']
    data = request.json

    try:
        db = get_db()
        task = db.todos.find_one({'_id': task_id})
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        if task['creator'] != username and task.get('assignee') not in [username, '__all__']:
            return jsonify({'error': 'Unauthorized'}), 403

        comment = {
            'user': username,
            'text': data.get('text', '').strip(),
            'created_at': datetime.utcnow()
        }

        db.todos.update_one(
            {'_id': task_id},
            {'$push': {'comments': comment}, '$set': {'updated_at': datetime.utcnow()}}
        )

        # Notify
        notify_user = task['creator'] if task['creator'] != username else task.get('assignee')
        if notify_user and notify_user not in ['', username]:
            if notify_user == '__all__':
                socketio.emit('comment_added', {
                    'task_id': task_id,
                    'task_title': task['title'],
                    'user': username
                }, broadcast=True)
            else:
                socketio.emit('comment_added', {
                    'task_id': task_id,
                    'task_title': task['title'],
                    'user': username
                }, room=notify_user)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/todos/users')
def api_todos_users():
    """Get list of friends for assignment (only accepted friends)"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']

    try:
        db = get_db()

        # Only get accepted friends
        friends = []
        friends_docs = list(db.friends.find({
            '$or': [
                {'user': username, 'status': 'accepted'},
                {'friend': username, 'status': 'accepted'}
            ]
        }))

        for f in friends_docs:
            friend_name = f['friend'] if f['user'] == username else f['user']
            if friend_name not in friends:
                friends.append(friend_name)

        return jsonify({'users': sorted(friends)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===========================================
# Music Room API Endpoints
# ===========================================

# Track active music rooms: room_id -> room_state
music_rooms = {}

def _init_music_rooms_collection(db):
    """Ensure indexes on music_rooms collection"""
    col = db.music_rooms
    col.create_index('code')
    col.create_index('host_user')
    col.create_index('created_at')
    return col

@app.route('/api/music/rooms')
def api_music_rooms():
    """List active music rooms"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        db = get_db()
        rooms = list(db.music_rooms.find({}).sort('created_at', -1).limit(50))
        result = []
        for r in rooms:
            result.append({
                '_id': str(r['_id']),
                'title': r.get('title', 'Music Room'),
                'code': r.get('code', ''),
                'host_user': r.get('host_user', ''),
                'member_count': len(r.get('members', []))
            })
        return jsonify({'rooms': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/music/upload', methods=['POST'])
def api_music_upload():
    """Upload audio file to music room"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']
    room_id = request.form.get('room_id', '')

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400

    try:
        db = get_db()
        cfg = get_music_s3_config(db)
        if not cfg:
            return jsonify({'error': 'Music storage not configured'}), 500

        ok, result = upload_music_file(cfg, room_id, file.filename, file)
        if not ok:
            return jsonify({'error': result}), 500

        # Create track info
        track = {
            'id': str(uuid.uuid4())[:8],
            'name': file.filename,
            's3_key': result,
            'duration': 0,  # Would need audio processing to get actual duration
            'uploader': username
        }

        return jsonify({'success': True, 'track': track})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/music/stream/<path:s3_key>')
def api_music_stream(s3_key):
    """Stream audio file from S3"""
    if 'user' not in session or session.get('is_admin'):
        return 'Unauthorized', 401

    try:
        db = get_db()
        cfg = get_music_s3_config(db)
        if not cfg:
            return 'Music storage not configured', 500

        range_header = request.headers.get('Range')
        result = stream_audio(cfg, s3_key, range_header)

        if not result:
            return 'File not found', 404

        gen, content_length, content_type, status_code, headers = result

        resp = Response(gen, status=status_code, mimetype=content_type)
        resp.headers['Content-Length'] = content_length
        for k, v in headers.items():
            resp.headers[k] = v

        return resp
    except Exception as e:
        app.logger.error(f"Music stream error: {e}")
        return str(e), 500

@app.route('/api/music/s3-audio')
def api_music_s3_audio():
    """List audio files from user's S3 for import"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = session['user']

    try:
        db = get_db()
        cfg = get_s3_config(db, username)
        if not cfg:
            return jsonify({'files': []})

        files = list_audio_files(cfg)
        return jsonify({'files': files})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===========================================
# Music Room Socket.IO Handlers
# ===========================================

@socketio.on('create_music_room')
def handle_create_music_room(data):
    """Create a new music room"""
    username = session.get('user')
    if not username:
        return

    title = data.get('title', 'Music Room')
    control_mode = data.get('control_mode', 'host_only')

    try:
        db = get_db()
        _init_music_rooms_collection(db)

        room_id = str(uuid.uuid4())[:8]
        code = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(6))

        room = {
            '_id': room_id,
            'code': code,
            'host_user': username,
            'title': title,
            'members': [username],
            'control_mode': control_mode,
            'playlist': [],
            'current_track': 0,
            'current_time': 0,
            'is_playing': False,
            'shuffle': False,
            'repeat': 'none',
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow()
        }

        db.music_rooms.insert_one(room)
        join_room(f'music_{room_id}')

        emit('music_room_created', {
            'room_id': room_id,
            'state': {
                'title': title,
                'code': code,
                'host_user': username,
                'members': [username],
                'control_mode': control_mode,
                'playlist': [],
                'current_track': 0,
                'current_time': 0,
                'is_playing': False,
                'shuffle': False,
                'repeat': 'none'
            }
        })

    except Exception as e:
        emit('music_room_error', {'error': str(e)})

@socketio.on('join_music_room')
def handle_join_music_room(data):
    """Join a music room by code or ID"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')
    code = data.get('code', '').upper()

    try:
        db = get_db()

        if code:
            room = db.music_rooms.find_one({'code': code})
        else:
            room = db.music_rooms.find_one({'_id': room_id})

        if not room:
            emit('music_room_error', {'error': 'Room not found'})
            return

        room_id = room['_id']

        # Add member if not already in
        if username not in room.get('members', []):
            db.music_rooms.update_one(
                {'_id': room_id},
                {'$addToSet': {'members': username}}
            )
            room['members'].append(username)

        join_room(f'music_{room_id}')

        state = {
            'title': room.get('title', 'Music Room'),
            'code': room.get('code', ''),
            'host_user': room.get('host_user', ''),
            'members': room.get('members', []),
            'control_mode': room.get('control_mode', 'host_only'),
            'playlist': room.get('playlist', []),
            'current_track': room.get('current_track', 0),
            'current_time': room.get('current_time', 0),
            'is_playing': room.get('is_playing', False),
            'shuffle': room.get('shuffle', False),
            'repeat': room.get('repeat', 'none')
        }

        emit('music_room_joined', {'room_id': room_id, 'state': state})

        # Notify others
        emit('music_state', {'room_id': room_id, 'state': state}, room=f'music_{room_id}', include_self=False)

    except Exception as e:
        emit('music_room_error', {'error': str(e)})

@socketio.on('leave_music_room')
def handle_leave_music_room(data):
    """Leave a music room"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')
    if not room_id:
        return

    try:
        db = get_db()
        room = db.music_rooms.find_one({'_id': room_id})

        if room:
            # Remove member
            db.music_rooms.update_one(
                {'_id': room_id},
                {'$pull': {'members': username}}
            )

            leave_room(f'music_{room_id}')

            # Delete room if host leaves or no members
            updated_room = db.music_rooms.find_one({'_id': room_id})
            if not updated_room.get('members') or room.get('host_user') == username:
                db.music_rooms.delete_one({'_id': room_id})
                emit('music_room_left', {}, room=f'music_{room_id}')
            else:
                # Notify remaining members
                state = {
                    'title': updated_room.get('title'),
                    'code': updated_room.get('code'),
                    'host_user': updated_room.get('host_user'),
                    'members': updated_room.get('members', []),
                    'control_mode': updated_room.get('control_mode'),
                    'playlist': updated_room.get('playlist', []),
                    'current_track': updated_room.get('current_track', 0),
                    'current_time': updated_room.get('current_time', 0),
                    'is_playing': updated_room.get('is_playing', False),
                    'shuffle': updated_room.get('shuffle', False),
                    'repeat': updated_room.get('repeat', 'none')
                }
                emit('music_state', {'room_id': room_id, 'state': state}, room=f'music_{room_id}')

        emit('music_room_left', {})

    except Exception as e:
        app.logger.error(f"Music leave error: {e}")

@socketio.on('music_play')
def handle_music_play(data):
    """Play or resume music"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')
    track_index = data.get('track_index')

    try:
        db = get_db()
        room = db.music_rooms.find_one({'_id': room_id})
        if not room:
            return

        # Check permission
        if room.get('control_mode') == 'host_only' and room.get('host_user') != username:
            return

        update = {'$set': {'is_playing': True, 'updated_at': datetime.utcnow()}}
        if track_index is not None:
            update['$set']['current_track'] = track_index
            update['$set']['current_time'] = 0

        db.music_rooms.update_one({'_id': room_id}, update)

        room = db.music_rooms.find_one({'_id': room_id})
        state = {
            'title': room.get('title'),
            'code': room.get('code'),
            'host_user': room.get('host_user'),
            'members': room.get('members', []),
            'control_mode': room.get('control_mode'),
            'playlist': room.get('playlist', []),
            'current_track': room.get('current_track', 0),
            'current_time': room.get('current_time', 0),
            'is_playing': room.get('is_playing', False),
            'shuffle': room.get('shuffle', False),
            'repeat': room.get('repeat', 'none')
        }
        emit('music_state', {'room_id': room_id, 'state': state}, room=f'music_{room_id}')

    except Exception as e:
        app.logger.error(f"Music play error: {e}")

@socketio.on('music_pause')
def handle_music_pause(data):
    """Pause music"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')

    try:
        db = get_db()
        room = db.music_rooms.find_one({'_id': room_id})
        if not room:
            return

        if room.get('control_mode') == 'host_only' and room.get('host_user') != username:
            return

        db.music_rooms.update_one(
            {'_id': room_id},
            {'$set': {'is_playing': False, 'updated_at': datetime.utcnow()}}
        )

        room = db.music_rooms.find_one({'_id': room_id})
        state = {
            'title': room.get('title'),
            'code': room.get('code'),
            'host_user': room.get('host_user'),
            'members': room.get('members', []),
            'control_mode': room.get('control_mode'),
            'playlist': room.get('playlist', []),
            'current_track': room.get('current_track', 0),
            'current_time': room.get('current_time', 0),
            'is_playing': False,
            'shuffle': room.get('shuffle', False),
            'repeat': room.get('repeat', 'none')
        }
        emit('music_state', {'room_id': room_id, 'state': state}, room=f'music_{room_id}')

    except Exception as e:
        app.logger.error(f"Music pause error: {e}")

@socketio.on('music_seek')
def handle_music_seek(data):
    """Seek to position"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')
    time_pos = data.get('time', 0)

    try:
        db = get_db()
        room = db.music_rooms.find_one({'_id': room_id})
        if not room:
            return

        if room.get('control_mode') == 'host_only' and room.get('host_user') != username:
            return

        db.music_rooms.update_one(
            {'_id': room_id},
            {'$set': {'current_time': time_pos, 'updated_at': datetime.utcnow()}}
        )

        emit('music_time_sync', {'room_id': room_id, 'time': time_pos}, room=f'music_{room_id}')

    except Exception as e:
        app.logger.error(f"Music seek error: {e}")

@socketio.on('music_next')
def handle_music_next(data):
    """Next track"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')

    try:
        db = get_db()
        room = db.music_rooms.find_one({'_id': room_id})
        if not room:
            return

        if room.get('control_mode') == 'host_only' and room.get('host_user') != username:
            return

        playlist = room.get('playlist', [])
        current = room.get('current_track', 0)
        repeat = room.get('repeat', 'none')

        if repeat == 'one':
            next_track = current
        elif room.get('shuffle'):
            import random
            next_track = random.randint(0, len(playlist) - 1) if playlist else 0
        else:
            next_track = current + 1
            if next_track >= len(playlist):
                if repeat == 'all':
                    next_track = 0
                else:
                    next_track = current

        db.music_rooms.update_one(
            {'_id': room_id},
            {'$set': {'current_track': next_track, 'current_time': 0, 'updated_at': datetime.utcnow()}}
        )

        room = db.music_rooms.find_one({'_id': room_id})
        state = {
            'title': room.get('title'),
            'code': room.get('code'),
            'host_user': room.get('host_user'),
            'members': room.get('members', []),
            'control_mode': room.get('control_mode'),
            'playlist': room.get('playlist', []),
            'current_track': room.get('current_track', 0),
            'current_time': 0,
            'is_playing': room.get('is_playing', False),
            'shuffle': room.get('shuffle', False),
            'repeat': room.get('repeat', 'none')
        }
        emit('music_state', {'room_id': room_id, 'state': state}, room=f'music_{room_id}')

    except Exception as e:
        app.logger.error(f"Music next error: {e}")

@socketio.on('music_prev')
def handle_music_prev(data):
    """Previous track"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')

    try:
        db = get_db()
        room = db.music_rooms.find_one({'_id': room_id})
        if not room:
            return

        if room.get('control_mode') == 'host_only' and room.get('host_user') != username:
            return

        current = room.get('current_track', 0)
        prev_track = max(0, current - 1)

        db.music_rooms.update_one(
            {'_id': room_id},
            {'$set': {'current_track': prev_track, 'current_time': 0, 'updated_at': datetime.utcnow()}}
        )

        room = db.music_rooms.find_one({'_id': room_id})
        state = {
            'title': room.get('title'),
            'code': room.get('code'),
            'host_user': room.get('host_user'),
            'members': room.get('members', []),
            'control_mode': room.get('control_mode'),
            'playlist': room.get('playlist', []),
            'current_track': prev_track,
            'current_time': 0,
            'is_playing': room.get('is_playing', False),
            'shuffle': room.get('shuffle', False),
            'repeat': room.get('repeat', 'none')
        }
        emit('music_state', {'room_id': room_id, 'state': state}, room=f'music_{room_id}')

    except Exception as e:
        app.logger.error(f"Music prev error: {e}")

@socketio.on('music_shuffle')
def handle_music_shuffle(data):
    """Toggle shuffle"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')
    enabled = data.get('enabled', False)

    try:
        db = get_db()
        room = db.music_rooms.find_one({'_id': room_id})
        if not room:
            return

        if room.get('control_mode') == 'host_only' and room.get('host_user') != username:
            return

        db.music_rooms.update_one(
            {'_id': room_id},
            {'$set': {'shuffle': enabled, 'updated_at': datetime.utcnow()}}
        )

        room = db.music_rooms.find_one({'_id': room_id})
        state = {
            'title': room.get('title'),
            'code': room.get('code'),
            'host_user': room.get('host_user'),
            'members': room.get('members', []),
            'control_mode': room.get('control_mode'),
            'playlist': room.get('playlist', []),
            'current_track': room.get('current_track', 0),
            'current_time': room.get('current_time', 0),
            'is_playing': room.get('is_playing', False),
            'shuffle': enabled,
            'repeat': room.get('repeat', 'none')
        }
        emit('music_state', {'room_id': room_id, 'state': state}, room=f'music_{room_id}')

    except Exception as e:
        app.logger.error(f"Music shuffle error: {e}")

@socketio.on('music_repeat')
def handle_music_repeat(data):
    """Set repeat mode"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')
    mode = data.get('mode', 'none')

    try:
        db = get_db()
        room = db.music_rooms.find_one({'_id': room_id})
        if not room:
            return

        if room.get('control_mode') == 'host_only' and room.get('host_user') != username:
            return

        db.music_rooms.update_one(
            {'_id': room_id},
            {'$set': {'repeat': mode, 'updated_at': datetime.utcnow()}}
        )

        room = db.music_rooms.find_one({'_id': room_id})
        state = {
            'title': room.get('title'),
            'code': room.get('code'),
            'host_user': room.get('host_user'),
            'members': room.get('members', []),
            'control_mode': room.get('control_mode'),
            'playlist': room.get('playlist', []),
            'current_track': room.get('current_track', 0),
            'current_time': room.get('current_time', 0),
            'is_playing': room.get('is_playing', False),
            'shuffle': room.get('shuffle', False),
            'repeat': mode
        }
        emit('music_state', {'room_id': room_id, 'state': state}, room=f'music_{room_id}')

    except Exception as e:
        app.logger.error(f"Music repeat error: {e}")

@socketio.on('add_track')
def handle_add_track(data):
    """Add track to playlist"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')
    track = data.get('track')

    if not track or not track.get('name'):
        return

    try:
        db = get_db()
        room = db.music_rooms.find_one({'_id': room_id})
        if not room:
            return

        if room.get('control_mode') == 'host_only' and room.get('host_user') != username:
            return

        db.music_rooms.update_one(
            {'_id': room_id},
            {'$push': {'playlist': track}, '$set': {'updated_at': datetime.utcnow()}}
        )

        room = db.music_rooms.find_one({'_id': room_id})
        state = {
            'title': room.get('title'),
            'code': room.get('code'),
            'host_user': room.get('host_user'),
            'members': room.get('members', []),
            'control_mode': room.get('control_mode'),
            'playlist': room.get('playlist', []),
            'current_track': room.get('current_track', 0),
            'current_time': room.get('current_time', 0),
            'is_playing': room.get('is_playing', False),
            'shuffle': room.get('shuffle', False),
            'repeat': room.get('repeat', 'none')
        }
        emit('music_state', {'room_id': room_id, 'state': state}, room=f'music_{room_id}')

    except Exception as e:
        app.logger.error(f"Add track error: {e}")

@socketio.on('remove_track')
def handle_remove_track(data):
    """Remove track from playlist"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')
    track_id = data.get('track_id')

    try:
        db = get_db()
        room = db.music_rooms.find_one({'_id': room_id})
        if not room:
            return

        if room.get('control_mode') == 'host_only' and room.get('host_user') != username:
            return

        playlist = room.get('playlist', [])
        new_playlist = [t for t in playlist if t.get('id') != track_id]

        db.music_rooms.update_one(
            {'_id': room_id},
            {'$set': {'playlist': new_playlist, 'updated_at': datetime.utcnow()}}
        )

        room = db.music_rooms.find_one({'_id': room_id})
        state = {
            'title': room.get('title'),
            'code': room.get('code'),
            'host_user': room.get('host_user'),
            'members': room.get('members', []),
            'control_mode': room.get('control_mode'),
            'playlist': new_playlist,
            'current_track': room.get('current_track', 0),
            'current_time': room.get('current_time', 0),
            'is_playing': room.get('is_playing', False),
            'shuffle': room.get('shuffle', False),
            'repeat': room.get('repeat', 'none')
        }
        emit('music_state', {'room_id': room_id, 'state': state}, room=f'music_{room_id}')

    except Exception as e:
        app.logger.error(f"Remove track error: {e}")

@socketio.on('import_from_s3')
def handle_import_from_s3(data):
    """Import audio from S3 to playlist"""
    username = session.get('user')
    if not username:
        return

    room_id = data.get('room_id')
    s3_key = data.get('s3_key')
    name = data.get('name')

    try:
        db = get_db()
        room = db.music_rooms.find_one({'_id': room_id})
        if not room:
            return

        if room.get('control_mode') == 'host_only' and room.get('host_user') != username:
            return

        track = {
            'id': str(uuid.uuid4())[:8],
            'name': name or s3_key.rsplit('/', 1)[-1],
            's3_key': s3_key,
            'duration': 0,
            'uploader': username
        }

        db.music_rooms.update_one(
            {'_id': room_id},
            {'$push': {'playlist': track}, '$set': {'updated_at': datetime.utcnow()}}
        )

        room = db.music_rooms.find_one({'_id': room_id})
        state = {
            'title': room.get('title'),
            'code': room.get('code'),
            'host_user': room.get('host_user'),
            'members': room.get('members', []),
            'control_mode': room.get('control_mode'),
            'playlist': room.get('playlist', []),
            'current_track': room.get('current_track', 0),
            'current_time': room.get('current_time', 0),
            'is_playing': room.get('is_playing', False),
            'shuffle': room.get('shuffle', False),
            'repeat': room.get('repeat', 'none')
        }
        emit('music_state', {'room_id': room_id, 'state': state}, room=f'music_{room_id}')

    except Exception as e:
        app.logger.error(f"Import S3 error: {e}")


# ===========================================
# Screen Share API & Socket.IO Handlers
# ===========================================

def _init_screen_sessions_collection(db):
    """Ensure indexes on screen_sessions collection with TTL"""
    col = db.screen_sessions
    col.create_index('host_user')
    col.create_index('created_at', expireAfterSeconds=24*60*60)  # 24h TTL
    return col

@app.route('/api/screen/sessions')
def api_screen_sessions():
    """List active screen share sessions"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        db = get_db()
        _init_screen_sessions_collection(db)

        sessions = list(db.screen_sessions.find({}).sort('created_at', -1).limit(50))
        result = []
        for s in sessions:
            result.append({
                '_id': str(s['_id']),
                'code': s.get('code', ''),
                'title': s.get('title', 'Screen Share'),
                'host_user': s.get('host_user', ''),
                'has_password': bool(s.get('password')),
                'viewer_count': len(s.get('viewers', []))
            })
        return jsonify({'sessions': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/screen/session/<session_id>')
def api_screen_session(session_id):
    """Get session details"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        db = get_db()
        sess = db.screen_sessions.find_one({'_id': session_id})
        if not sess:
            return jsonify({'error': 'Session not found'}), 404

        return jsonify({
            'session_id': session_id,
            'title': sess.get('title'),
            'host_user': sess.get('host_user'),
            'has_password': bool(sess.get('password')),
            'viewer_count': len(sess.get('viewers', []))
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/screen/verify-password', methods=['POST'])
def api_screen_verify_password():
    """Verify session password"""
    if 'user' not in session or session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    session_id = data.get('session_id')
    password = data.get('password', '')

    try:
        db = get_db()
        sess = db.screen_sessions.find_one({'_id': session_id})
        if not sess:
            return jsonify({'error': 'Session not found'}), 404

        if sess.get('password'):
            if not check_password_hash(sess['password'], password):
                return jsonify({'error': 'Incorrect password'}), 403

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def generate_screen_code():
    """Generate 6-char uppercase code for screen share"""
    import random
    import string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

@socketio.on('start_screen_share')
def handle_start_screen_share(data):
    """Start a screen share session"""
    username = session.get('user')
    if not username:
        return

    title = data.get('title', 'Screen Share')
    password = data.get('password', '')

    try:
        db = get_db()
        _init_screen_sessions_collection(db)

        session_id = str(uuid.uuid4())[:12]
        code = generate_screen_code()

        # Ensure code is unique
        while db.screen_sessions.find_one({'code': code}):
            code = generate_screen_code()

        sess = {
            '_id': session_id,
            'code': code,
            'host_user': username,
            'title': title,
            'password': generate_password_hash(password) if password else None,
            'viewers': [],
            'created_at': datetime.utcnow()
        }

        db.screen_sessions.insert_one(sess)
        join_room(f'screen_{session_id}')

        emit('screen_session_started', {
            'session_id': session_id,
            'code': code,
            'title': title
        })

    except Exception as e:
        emit('screen_session_error', {'error': str(e)})

@socketio.on('stop_screen_share')
def handle_stop_screen_share(data):
    """Stop screen share session"""
    username = session.get('user')
    if not username:
        return

    session_id = data.get('session_id')

    try:
        db = get_db()
        sess = db.screen_sessions.find_one({'_id': session_id, 'host_user': username})
        if sess:
            db.screen_sessions.delete_one({'_id': session_id})
            emit('screen_session_ended', {}, room=f'screen_{session_id}')
            leave_room(f'screen_{session_id}')

    except Exception as e:
        app.logger.error(f"Stop screen share error: {e}")

@socketio.on('join_screen_session')
def handle_join_screen_session(data):
    """Join a screen share session as viewer"""
    username = session.get('user')
    if not username:
        return

    session_id = data.get('session_id')
    password = data.get('password', '')

    try:
        db = get_db()
        sess = db.screen_sessions.find_one({'_id': session_id})

        if not sess:
            emit('screen_session_error', {'error': 'Session not found'})
            return

        # Check password
        if sess.get('password'):
            if not check_password_hash(sess['password'], password):
                emit('screen_session_error', {'error': 'Incorrect password'})
                return

        # Add viewer
        if username not in sess.get('viewers', []):
            db.screen_sessions.update_one(
                {'_id': session_id},
                {'$addToSet': {'viewers': username}}
            )

        join_room(f'screen_{session_id}')

        emit('screen_session_joined', {
            'session_id': session_id,
            'title': sess.get('title'),
            'host_user': sess.get('host_user')
        })

        # Notify host
        updated = db.screen_sessions.find_one({'_id': session_id})
        emit('viewer_joined', {
            'viewer_id': username,
            'viewers': updated.get('viewers', [])
        }, room=f'screen_{session_id}')

    except Exception as e:
        emit('screen_session_error', {'error': str(e)})

@socketio.on('join_screen_by_code')
def handle_join_screen_by_code(data):
    """Join screen share by 6-char code"""
    username = session.get('user') or data.get('guest_name')
    if not username:
        emit('screen_session_error', {'error': 'Username required'})
        return

    code = data.get('code', '').upper()
    password = data.get('password', '')

    try:
        db = get_db()
        sess = db.screen_sessions.find_one({'code': code})

        if not sess:
            emit('screen_session_error', {'error': 'Invalid code'})
            return

        # Check password
        if sess.get('password'):
            if not check_password_hash(sess['password'], password):
                emit('screen_session_error', {'error': 'Incorrect password'})
                return

        session_id = sess['_id']

        # Add viewer
        if username not in sess.get('viewers', []):
            db.screen_sessions.update_one(
                {'_id': session_id},
                {'$addToSet': {'viewers': username}}
            )

        join_room(f'screen_{session_id}')

        emit('screen_session_joined', {
            'session_id': session_id,
            'title': sess.get('title'),
            'host_user': sess.get('host_user')
        })

        # Notify host
        updated = db.screen_sessions.find_one({'_id': session_id})
        emit('viewer_joined', {
            'viewer_id': username,
            'viewers': updated.get('viewers', [])
        }, room=f'screen_{session_id}')

    except Exception as e:
        emit('screen_session_error', {'error': str(e)})

@socketio.on('leave_screen_session')
def handle_leave_screen_session(data):
    """Leave screen share session"""
    username = session.get('user')
    if not username:
        return

    session_id = data.get('session_id')

    try:
        db = get_db()
        db.screen_sessions.update_one(
            {'_id': session_id},
            {'$pull': {'viewers': username}}
        )

        leave_room(f'screen_{session_id}')

        updated = db.screen_sessions.find_one({'_id': session_id})
        if updated:
            emit('viewer_left', {
                'viewer_id': username,
                'viewers': updated.get('viewers', [])
            }, room=f'screen_{session_id}')

    except Exception as e:
        app.logger.error(f"Leave screen session error: {e}")

@socketio.on('webrtc_offer')
def handle_webrtc_offer(data):
    """Forward WebRTC offer to viewer"""
    username = session.get('user')
    if not username:
        return

    session_id = data.get('session_id')
    viewer_id = data.get('viewer_id')
    sdp = data.get('sdp')

    try:
        db = get_db()
        sess = db.screen_sessions.find_one({'_id': session_id, 'host_user': username})
        if not sess:
            return

        emit('webrtc_offer', {
            'host_id': username,
            'sdp': sdp
        }, room=viewer_id)

    except Exception as e:
        app.logger.error(f"WebRTC offer error: {e}")

@socketio.on('webrtc_answer')
def handle_webrtc_answer(data):
    """Forward WebRTC answer to host"""
    username = session.get('user')
    if not username:
        return

    session_id = data.get('session_id')
    sdp = data.get('sdp')

    try:
        db = get_db()
        sess = db.screen_sessions.find_one({'_id': session_id})
        if not sess:
            return

        emit('webrtc_answer', {
            'viewer_id': username,
            'sdp': sdp
        }, room=sess['host_user'])

    except Exception as e:
        app.logger.error(f"WebRTC answer error: {e}")

@socketio.on('webrtc_ice')
def handle_webrtc_ice(data):
    """Forward ICE candidate"""
    username = session.get('user')
    if not username:
        return

    session_id = data.get('session_id')
    viewer_id = data.get('viewer_id')
    candidate = data.get('candidate')

    try:
        db = get_db()
        sess = db.screen_sessions.find_one({'_id': session_id})
        if not sess:
            return

        if username == sess['host_user']:
            # Host sending to viewer
            emit('webrtc_ice', {
                'from_id': username,
                'candidate': candidate
            }, room=viewer_id)
        else:
            # Viewer sending to host
            emit('webrtc_ice', {
                'from_id': username,
                'candidate': candidate
            }, room=sess['host_user'])

    except Exception as e:
        app.logger.error(f"WebRTC ICE error: {e}")

@socketio.on('screen_chat')
def handle_screen_chat(data):
    """Send chat message in screen share session"""
    username = session.get('user')
    if not username:
        return

    session_id = data.get('session_id')
    content = data.get('content', '').strip()

    if not content:
        return

    try:
        db = get_db()
        sess = db.screen_sessions.find_one({'_id': session_id})
        if not sess:
            return

        # Check if user is in session
        if username != sess['host_user'] and username not in sess.get('viewers', []):
            return

        # Save to chat collection (optional)
        db.screen_chat.insert_one({
            'session_id': session_id,
            'from_user': username,
            'content': content,
            'created_at': datetime.utcnow()
        })

        emit('screen_chat_message', {
            'from_user': username,
            'content': content
        }, room=f'screen_{session_id}')

    except Exception as e:
        app.logger.error(f"Screen chat error: {e}")


if __name__ == '__main__':
    port = int(os.environ.get('DASHBOARD_PORT', 9998))
    # Use socketio.run for WebSocket support
    socketio.run(app, host='0.0.0.0', port=port)
